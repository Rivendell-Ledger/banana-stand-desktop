"""SQLite access, migrations, and time helpers."""
from __future__ import annotations

import logging
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

logger = logging.getLogger("banana.db")

# One SQLite connection per worker thread, reused across requests. Opening a
# connection and setting four PRAGMAs costs more than most of the queries a page
# runs, and FastAPI's sync routes keep their threads alive. SELECTs do not start
# a transaction, so a reused connection still sees every committed write.
_local = threading.local()
# Tests replace the database file between cases, which a pooled handle cannot see.
# Pool for the app; open per-call connections under pytest.
_POOL = "pytest" not in sys.modules


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_now() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def _new_connection() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def connect() -> sqlite3.Connection:
    if not _POOL:
        return _new_connection()
    key = str(config.DB_PATH)
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "key", None) == key:
        return conn
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    conn = _new_connection()
    _local.conn = conn
    _local.key = key
    return conn


def reset_connections() -> None:
    """Drop this thread's cached connection (tests replace the DB file)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _local.conn = None
    _local.key = None


@contextmanager
def session_scope():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if not _POOL:
            conn.close()


def _pre_migration_backup(conn: sqlite3.Connection) -> None:
    """Snapshot the live DB before an upgrade changes its schema.

    A failed migration rolls back its own file, but a backup gives the user a way
    back if an upgrade goes wrong in a way SQLite cannot undo. Best effort: never
    block startup because a backup could not be written.
    """
    try:
        if not config.DB_PATH.exists() or config.DB_PATH.stat().st_size == 0:
            return
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:19]
        target = config.BACKUP_DIR / f"pre-migration-{stamp}.db"
        dest = sqlite3.connect(target)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        logger.info("pre-migration backup: %s", target.name)
        old = sorted(config.BACKUP_DIR.glob("pre-migration-*.db"))[:-10]
        for path in old:
            try:
                path.unlink()
            except OSError:
                pass
    except (OSError, sqlite3.Error) as exc:
        logger.warning("could not write a pre-migration backup: %s", exc)


def migrate() -> list[str]:
    """Apply numbered migration files in order. Returns the versions applied this run."""
    applied_now: list[str] = []
    conn = _new_connection()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.commit()
        done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
        pending = [p for p in sorted(config.MIGRATIONS_DIR.glob("*.sql")) if p.stem not in done]
        if pending and done:
            _pre_migration_backup(conn)
        for path in pending:
            version = path.stem
            sql = path.read_text(encoding="utf-8")
            safe_version = version.replace("'", "''")
            script = (
                "BEGIN;\n"
                + sql
                + f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ('{safe_version}', '{utcnow()}');\n"
                + "COMMIT;"
            )
            conn.executescript(script)
            applied_now.append(version)
    finally:
        conn.close()
    return applied_now


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with session_scope() as conn:
        return conn.execute(sql, params).fetchone()


def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with session_scope() as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql: str, params: tuple = ()) -> int:
    with session_scope() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or 0


def table_exists(name: str) -> bool:
    row = query_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return row is not None


def count(table: str) -> int:
    if not table_exists(table):
        return 0
    row = query_one(f"SELECT COUNT(*) AS n FROM {table}")
    return int(row["n"]) if row else 0


def db_size_bytes() -> int:
    try:
        return Path(config.DB_PATH).stat().st_size
    except FileNotFoundError:
        return 0
