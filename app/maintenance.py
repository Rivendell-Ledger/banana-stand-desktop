"""Backups, restore, retention and the restore drill.

The rule this module exists to enforce: a backup you have never restored is not a backup.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db

CORE_TABLES = [
    "households", "users", "accounts", "transactions", "categories", "category_rules",
    "budgets", "recurring", "holdings", "option_positions", "goals", "net_worth_snapshots",
]


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def counts(path: Path | None = None) -> dict[str, int]:
    path = Path(path or config.DB_PATH)
    conn = _connect_ro(path)
    try:
        existing = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        out = {}
        for table in CORE_TABLES:
            if table in existing:
                out[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return out
    finally:
        conn.close()


def verify(path: Path | str) -> dict:
    path = Path(path)
    if not path.exists():
        return {"ok": False, "error": "file not found", "path": str(path)}
    try:
        conn = _connect_ro(path)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        migrations = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"] \
            if conn.execute("SELECT name FROM sqlite_master WHERE name = 'schema_migrations'").fetchone() else 0
        conn.close()
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}
    return {
        "ok": integrity == "ok",
        "integrity": integrity,
        "migrations": migrations,
        "counts": counts(path),
        "bytes": path.stat().st_size,
        "path": str(path),
    }


def backup_to(target: Path | None = None) -> Path:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if target is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = config.BACKUP_DIR / f"banana-stand-{stamp}.db"
    target = Path(target)
    src = sqlite3.connect(config.DB_PATH)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def drill(keep: bool = False) -> dict:
    """Back up, reopen the copy, verify it, and compare every table against the live database."""
    tmp_dir = config.DATA_DIR / "drill"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        tmp_dir.chmod(0o700)
    except OSError:
        pass
    copy = tmp_dir / f"drill-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    try:
        backup_to(copy)
        check = verify(copy)
        live = counts()
        copied = check.get("counts", {})
        mismatches = {t: (live.get(t), copied.get(t)) for t in live if live.get(t) != copied.get(t)}
        return {
            "ok": bool(check.get("ok")) and not mismatches,
            "integrity": check.get("integrity"),
            "mismatches": mismatches,
            "allowed_drift": ["audit_log", "sessions"],   # writes that legitimately continue
            "counts": copied,
            "bytes": check.get("bytes"),
            "path": str(copy),
        }
    finally:
        if not keep:
            # SQLite leaves -wal/-shm sidecars; the copy is useless without them and should not linger.
            for suffix in ("", "-wal", "-shm"):
                Path(str(copy) + suffix).unlink(missing_ok=True)
            try:
                tmp_dir.rmdir()
            except OSError:
                pass


def restore_from(src: Path | str, target: Path | None = None, pre_backup: bool = True) -> dict:
    """Replace the live database with a verified backup. Assumes the service is stopped."""
    src = Path(src)
    target = Path(target or config.DB_PATH)
    check = verify(src)
    if not check.get("ok"):
        return {"ok": False, "error": f"backup failed verification: {check.get('error') or check.get('integrity')}"}

    snapshot = None
    if pre_backup and target.exists():
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = config.BACKUP_DIR / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        live = sqlite3.connect(target)
        dst = sqlite3.connect(snapshot)
        try:
            with dst:
                live.backup(dst)
        finally:
            dst.close()
            live.close()

    # Close any pooled connection before its file is replaced: an open handle to the
    # old inode plus a stale WAL is what turns a restore into a disk I/O error.
    db.reset_connections()
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    shutil.copy2(src, target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    db.reset_connections()
    return {"ok": True, "restored_from": str(src), "snapshot": str(snapshot) if snapshot else None,
            "counts": check.get("counts", {})}


def purge(deleted_days: int = 90, audit_days: int = 365, dry_run: bool = False) -> dict:
    """Retention: hard-delete old trash, expired sessions and old audit rows."""
    now = datetime.now()
    deleted_cutoff = (now - timedelta(days=deleted_days)).isoformat(timespec="seconds")
    audit_cutoff = (now - timedelta(days=audit_days)).isoformat(timespec="seconds")

    with db.session_scope() as conn:
        trash = conn.execute("SELECT COUNT(*) FROM transactions WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                             (deleted_cutoff,)).fetchone()[0]
        sessions = conn.execute("SELECT COUNT(*) FROM sessions WHERE expires_at < ?",
                                (now.isoformat(timespec="seconds"),)).fetchone()[0]
        audit = conn.execute("SELECT COUNT(*) FROM audit_log WHERE created_at < ?",
                             (audit_cutoff,)).fetchone()[0]
        if not dry_run:
            conn.execute("DELETE FROM transactions WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                         (deleted_cutoff,))
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(timespec="seconds"),))
            conn.execute("DELETE FROM audit_log WHERE created_at < ?", (audit_cutoff,))

    files = 0
    if not dry_run:
        from . import csv_import
        files = csv_import.sweep_old_files(24)

    size_after = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    return {
        "purged_transactions": trash,
        "purged_sessions": sessions,
        "purged_audit": audit,
        "swept_import_files": files,
        "dry_run": dry_run,
        "bytes_after": size_after,
    }


def checkpoint() -> None:
    conn = db.connect()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def backup_status() -> dict:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(config.BACKUP_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime)
    if not files:
        return {"count": 0, "latest": None, "age_days": None, "bytes": 0}
    latest = files[-1]
    age = (datetime.now() - datetime.fromtimestamp(latest.stat().st_mtime)).total_seconds() / 86400
    return {
        "count": len(files),
        "latest": latest.name,
        "age_days": round(age, 1),
        "bytes": latest.stat().st_size,
    }


def permissions() -> list[dict]:
    """Files that must not be world-readable."""
    checks = [
        ("database", config.DB_PATH, 0o600),
        ("secret dir", Path(config.SECRET_DIR), 0o700),
        (".env", Path(config.BASE_DIR) / ".env", 0o600),
        ("fernet key", Path(config.SECRET_DIR) / "fernet.key", 0o600),
    ]
    out = []
    for label, path, want in checks:
        if not path.exists():
            out.append({"label": label, "path": str(path), "ok": True, "mode": None, "missing": True})
            continue
        mode = path.stat().st_mode & 0o777
        out.append({"label": label, "path": str(path), "ok": mode <= want,
                    "mode": oct(mode), "missing": False})
    return out
