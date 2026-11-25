"""Phase 9 acceptance: backup, restore drill, retention, audit, security headers."""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, maintenance
from app.main import app

PASSWORD = "correct horse battery staple"
TODAY = date.today()


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field"
    return m.group(1)


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_connections()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(db.config.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    db.migrate()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def sign_up(client):
    page = client.get("/setup")
    client.post("/setup", data={"csrf": csrf_of(page.text), "household": "The Banana Stand",
                                "name": "Alex", "email": "alex@example.com",
                                "password": PASSWORD, "password2": PASSWORD}, follow_redirects=False)
    page = client.get("/login")
    client.post("/login", data={"csrf": csrf_of(page.text), "email": "alex@example.com",
                                "password": PASSWORD}, follow_redirects=False)
    return client


def add_account(client, name="Checking"):
    page = client.get("/accounts/new")
    client.post("/accounts", data={"csrf": csrf_of(page.text), "type": "checking", "name": name,
                                   "institution": "Chase", "mask": "4821", "balance": "1000.00",
                                   "credit_limit": "", "include_net": "on", "include_budget": "on"}, follow_redirects=False)
    return db.query_one("SELECT id FROM accounts WHERE name = ?", (name,))["id"]


def add_txn(hid, account, cents, day, merchant="Test"):
    return db.execute(
        """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant, raw_merchant,
               created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
        (hid, account, day, cents, merchant, merchant, db.utcnow(), db.utcnow()),
    )


# ---------------------------------------------------------------- verify + drill
def test_backup_verifies(client):
    sign_up(client)
    add_account(client)
    target = maintenance.backup_to(config.BACKUP_DIR / "verify-me.db")
    info = maintenance.verify(target)
    assert info["ok"] is True
    assert info["integrity"] == "ok"
    assert info["counts"]["users"] == 1
    assert info["counts"]["accounts"] == 1
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_missing_or_broken_backup_fails_verification(tmp_path):
    assert maintenance.verify(tmp_path / "nope.db")["ok"] is False
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a database")
    info = maintenance.verify(broken)
    assert info["ok"] is False
    assert info["error"]


def test_restore_drill_matches_every_table(client):
    hid = None
    sign_up(client)
    account = add_account(client)
    hid = db.query_one("SELECT id FROM households")["id"]
    for i in range(5):
        add_txn(hid, account, -1000 - i, f"{TODAY.strftime('%Y-%m')}-{i + 1:02d}")
    result = maintenance.drill()
    assert result["ok"] is True
    assert result["mismatches"] == {}
    assert result["counts"]["transactions"] == 5
    # The drill copy (and its -wal/-shm sidecars) must not be left behind.
    drill_dir = config.DATA_DIR / "drill"
    assert not drill_dir.exists() or not any(drill_dir.iterdir())


# ---------------------------------------------------------------- restore
def test_restore_replaces_database_and_snapshots(tmp_path, client):
    sign_up(client)
    account = add_account(client)
    hid = db.query_one("SELECT id FROM households")["id"]
    add_txn(hid, account, -4200, str(TODAY), "Before backup")

    backup = maintenance.backup_to(config.BACKUP_DIR / "restore-src.db")

    # Everything after the backup is lost on restore.
    add_txn(hid, account, -9900, str(TODAY), "After backup")
    assert db.count("transactions") == 2

    target = tmp_path / "restored.db"
    result = maintenance.restore_from(backup, target=target, pre_backup=False)
    assert result["ok"] is True
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        assert conn.execute("SELECT merchant FROM transactions").fetchone()[0] == "Before backup"
    finally:
        conn.close()


def test_restore_refuses_a_bad_file(tmp_path, client):
    sign_up(client)
    broken = tmp_path / "bad.db"
    broken.write_bytes(b"nope")
    result = maintenance.restore_from(broken, target=tmp_path / "out.db", pre_backup=False)
    assert result["ok"] is False
    assert "verification" in result["error"]


def test_restore_keeps_a_pre_restore_snapshot(client):
    sign_up(client)
    account = add_account(client)
    hid = db.query_one("SELECT id FROM households")["id"]
    backup = maintenance.backup_to(config.BACKUP_DIR / "snap-src.db")
    add_txn(hid, account, -500, str(TODAY))
    result = maintenance.restore_from(backup)
    assert result["ok"] is True
    assert result["snapshot"] and Path(result["snapshot"]).exists()
    # The live database is the backup's state again.
    assert db.count("transactions") == 0


# ---------------------------------------------------------------- retention
def test_purge_removes_only_expired_rows(client):
    sign_up(client)
    account = add_account(client)
    hid = db.query_one("SELECT id FROM households")["id"]
    old = add_txn(hid, account, -100, str(TODAY))
    recent = add_txn(hid, account, -200, str(TODAY))
    live = add_txn(hid, account, -300, str(TODAY))

    old_stamp = (datetime.now() - timedelta(days=200)).isoformat(timespec="seconds")
    recent_stamp = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    db.execute("UPDATE transactions SET deleted_at = ? WHERE id = ?", (old_stamp, old))
    db.execute("UPDATE transactions SET deleted_at = ? WHERE id = ?", (recent_stamp, recent))

    db.execute("""INSERT INTO sessions(id, user_id, token_hash, created_at, last_seen_at, expires_at)
                  VALUES (?,?,?,?,?,?)""",
               ("s-expired", 1, "expired", db.utcnow(), db.utcnow(),
                (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")))
    db.execute("INSERT INTO audit_log(household_id, action, created_at) VALUES (?,?,?)",
               (hid, "ancient", (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")))
    db.execute("INSERT INTO audit_log(household_id, action, created_at) VALUES (?,?,?)",
               (hid, "fresh", db.utcnow()))

    result = maintenance.purge(deleted_days=90, audit_days=365)
    assert result["purged_transactions"] == 1
    assert result["purged_sessions"] >= 1
    assert result["purged_audit"] == 1

    remaining = {r["id"] for r in db.query_all("SELECT id FROM transactions")}
    assert old not in remaining
    assert recent in remaining and live in remaining
    assert db.query_one("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'ancient'")["n"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'fresh'")["n"] == 1


def test_purge_dry_run_changes_nothing(client):
    sign_up(client)
    account = add_account(client)
    hid = db.query_one("SELECT id FROM households")["id"]
    txn = add_txn(hid, account, -100, str(TODAY))
    db.execute("UPDATE transactions SET deleted_at = ? WHERE id = ?",
               ((datetime.now() - timedelta(days=200)).isoformat(timespec="seconds"), txn))
    result = maintenance.purge(dry_run=True)
    assert result["purged_transactions"] == 1
    assert db.count("transactions") == 1


def test_backup_status_and_permissions(client, tmp_path):
    sign_up(client)
    status = maintenance.backup_status()
    assert status["count"] >= 0
    maintenance.backup_to(config.BACKUP_DIR / "status-check.db")
    status = maintenance.backup_status()
    assert status["count"] >= 1
    assert status["latest"].endswith(".db")

    assert {"database", "secret dir", ".env", "fernet key"} <= {
        e["label"] for e in maintenance.permissions()}
    config.DB_PATH.chmod(0o600)
    entries = {e["label"]: e for e in maintenance.permissions()}
    assert entries["database"]["ok"] is True
    assert entries["database"]["mode"] == "0o600"
    config.DB_PATH.chmod(0o644)
    entries = {e["label"]: e for e in maintenance.permissions()}
    assert entries["database"]["ok"] is False   # world-readable is a finding
    config.DB_PATH.chmod(0o600)


# ---------------------------------------------------------------- security + audit UI
def test_security_headers(client):
    resp = client.get("/login")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["cross-origin-opener-policy"] == "same-origin"
    assert "geolocation=()" in resp.headers["permissions-policy"]
    csp = resp.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp


def test_session_cookie_flags(client):
    sign_up(client)
    page = client.get("/login")
    resp = client.post("/login", data={"csrf": csrf_of(page.text), "email": "alex@example.com",
                                       "password": PASSWORD}, follow_redirects=False)
    cookie = resp.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()


def test_cookie_is_secure_behind_https_proxy(client):
    sign_up(client)
    page = client.get("/login")
    resp = client.post("/login", data={"csrf": csrf_of(page.text), "email": "alex@example.com",
                                       "password": PASSWORD}, headers={"x-forwarded-proto": "https"},
                       follow_redirects=False)
    assert "secure" in resp.headers.get("set-cookie", "").lower()


def test_audit_log_records_and_renders(client):
    sign_up(client)
    add_account(client)
    page = client.get("/settings/audit")
    assert page.status_code == 200
    assert "Audit log" in page.text
    assert "account.created" in page.text or "account" in page.text
    assert db.query_one("SELECT COUNT(*) AS n FROM audit_log")["n"] >= 1
