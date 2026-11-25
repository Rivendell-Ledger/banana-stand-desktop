"""Desktop-only behaviour: credential storage, recovery kit, scheduler, paths."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import config, db, recovery, settings_store
from desktop import runtime
from desktop.scheduler import Job

TZ = ZoneInfo("America/New_York")


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRET_DIR", tmp_path / "secret")
    (tmp_path / "secret").mkdir(parents=True, exist_ok=True)
    db.reset_connections()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(db.config.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    db.migrate()
    now = db.utcnow()
    db.execute(
        "INSERT INTO households(name, currency, timezone, buffer_cents, week_start, created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("Test House", "USD", config.TIMEZONE, 150000, 6, now),
    )
    yield


def _hid() -> int:
    return int(db.query_one("SELECT id FROM households")["id"])


def test_recovery_kit_round_trip(clean_db):
    hid = _hid()
    settings_store.save_credentials(hid, "client-abc", "secret-xyz", "production", 20)
    kit = recovery.export("correct horse", hid)
    assert "BANANA-STAND RECOVERY KIT" in kit
    original_key = recovery.key_path().read_bytes()
    assert not original_key.endswith(b"\n")

    # A wrong passphrase is refused.
    with pytest.raises(ValueError):
        recovery.import_kit(kit, "wrong horse", hid)

    # Restore into a fresh secret folder.
    recovery.key_path().unlink()
    recovery.import_kit(kit, "correct horse", hid)
    assert recovery.key_path().read_bytes() == original_key
    creds = settings_store.stored_credentials(hid)
    assert creds["client_id"] == "client-abc"
    assert creds["secret"] == "secret-xyz"
    assert creds["env"] == "production"


def test_export_does_not_create_a_replacement_key(clean_db):
    hid = _hid()
    settings_store.save_credentials(hid, "client-abc", "secret-xyz", "production", 10)
    recovery.key_path().unlink()

    with pytest.raises(ValueError, match="key is missing"):
        recovery.export("correct horse", hid)
    assert not recovery.key_path().exists()


def test_import_rejects_a_wrong_header(clean_db):
    kit = recovery.export("correct horse", _hid())
    bad = "SOMETHING ELSE\n" + kit.split("\n", 1)[1]
    with pytest.raises(ValueError, match="does not look like"):
        recovery.import_kit(bad, "correct horse", _hid())


def test_import_rejects_a_non_string_key(clean_db):
    import base64
    import json

    from cryptography.fernet import Fernet

    passphrase = "correct horse"
    salt = os.urandom(16)
    token = Fernet(recovery._derive(passphrase, salt)).encrypt(
        json.dumps({"fernet_key": 123, "plaid": {}}).encode("utf-8")
    )
    kit = "\n".join([
        recovery.HEADER,
        "created: test",
        f"iterations: {recovery.ITERATIONS}",
        f"salt: {base64.b64encode(salt).decode('ascii')}",
        f"verify: {recovery._digest(passphrase, salt)}",
        "payload:",
        base64.b64encode(token).decode("ascii"),
        "",
        "prose",
    ])
    with pytest.raises(ValueError, match="usable encryption key"):
        recovery.import_kit(kit, passphrase, _hid())


def test_kit_doctor_reports_an_uninitialized_folder(tmp_path):
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    empty = tmp_path / "empty"
    kit = tmp_path / "kit.txt"
    kit.write_text("not a real kit", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(root / "kit_doctor.py"), "restore", str(kit),
         "--passphrase", "xxxxxxxx", "--data-dir", str(empty)],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "No data folder found" in result.stdout + result.stderr
    assert not (empty / "data" / "banana-stand.db").exists()
    assert "Traceback" not in result.stderr


def test_kit_doctor_reports_a_database_without_a_household(tmp_path):
    import sqlite3
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    data = tmp_path / "nohouse" / "data"
    data.mkdir(parents=True)
    conn = sqlite3.connect(data / "banana-stand.db")
    conn.execute("CREATE TABLE households (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    kit = tmp_path / "kit.txt"
    kit.write_text("not a real kit", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(root / "kit_doctor.py"), "restore", str(kit),
         "--passphrase", "xxxxxxxx", "--data-dir", str(tmp_path / "nohouse")],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "No household" in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_kit_doctor_reports_a_corrupt_database(tmp_path):
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    data = tmp_path / "corrupt" / "data"
    data.mkdir(parents=True)
    (data / "banana-stand.db").write_text("this is not a sqlite database", encoding="utf-8")
    kit = tmp_path / "kit.txt"
    kit.write_text("not a real kit", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(root / "kit_doctor.py"), "restore", str(kit),
         "--passphrase", "xxxxxxxx", "--data-dir", str(tmp_path / "corrupt")],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "cannot be read" in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_credentials_are_encrypted_at_rest(clean_db):
    hid = _hid()
    settings_store.save_credentials(hid, "client-abc", "secret-xyz", "production", 20)
    row = db.query_one(
        "SELECT value_json FROM settings WHERE household_id = ? AND key = 'plaid_secret_enc'",
        (hid,),
    )
    assert row is not None
    assert "secret-xyz" not in row["value_json"]
    assert settings_store.stored_credentials(hid)["secret"] == "secret-xyz"


def test_scheduler_interval_and_daily_due():
    now = datetime(2026, 11, 23, 23, 50, tzinfo=TZ)
    interval = Job("sync", "interval", lambda: None, seconds=6 * 3600)
    assert interval.due(now, None)
    assert not interval.due(now, now - timedelta(hours=1))
    assert interval.due(now, now - timedelta(hours=7))

    daily = Job("backup", "daily", lambda: None, at=(3, 10))
    assert daily.due(now, None)
    assert not daily.due(now, now)                 # already ran today
    assert daily.due(now, now - timedelta(days=2))

    hourly = Job("market", "hourly", lambda: None, hours=(10, 11, 21))
    at_noon = datetime(2026, 11, 23, 12, 5, tzinfo=TZ)
    assert hourly.due(at_noon, at_noon - timedelta(hours=2))
    assert not hourly.due(at_noon, at_noon - timedelta(minutes=10))
    # 09:00 is before the first window; nothing is due.
    assert not hourly.due(datetime(2026, 11, 23, 9, 0, tzinfo=TZ), None)


def test_lan_setting_is_read_before_startup(clean_db):
    hid = _hid()
    assert runtime.lan_requested(config.DB_PATH) is False
    settings_store.set(hid, "desktop_lan", True)
    assert runtime.lan_requested(config.DB_PATH) is True


def test_data_root_honours_override(tmp_path, monkeypatch):
    monkeypatch.setenv("BANANA_HOME", str(tmp_path / "home"))
    assert runtime.data_root() == (tmp_path / "home").resolve()


def test_no_glibc_only_strftime_directives():
    """%-d is a glibc extension; the Windows CRT raises ValueError on it."""
    import re
    from pathlib import Path as _Path

    pattern = re.compile(r"(?<!\{)%-[a-zA-Z]")
    offenders = []
    for path in _Path("app").rglob("*"):
        if path.suffix in {".py", ".html"}:
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path))
    assert not offenders, offenders


def test_plaid_cap_and_env_validation():
    from app.main import _plaid_cap, _plaid_env

    assert _plaid_cap("20") == 20
    assert _plaid_cap("0") == 10
    assert _plaid_cap("9999") == 10
    assert _plaid_cap("nonsense") == 10
    assert _plaid_cap("") == 10
    assert _plaid_env("Production") == "production"
    assert _plaid_env("productionn") == "production"
    assert _plaid_env("sandbox") == "sandbox"


def test_password_minimum_is_eight():
    from app import security

    assert security.password_problem("abcdefgh") is None
    assert security.password_problem("abcdefg") is not None


def test_private_lan_address_filter():
    from app.main import _private_v4

    assert _private_v4("192.168.1.24")
    assert _private_v4("10.0.0.5")
    assert _private_v4("172.20.1.2")
    assert not _private_v4("172.32.0.1")
    assert not _private_v4("100.64.0.1")   # Tailscale / CGNAT is not the home LAN
    assert not _private_v4("127.0.0.1")
    assert not _private_v4("8.8.8.8")


def test_importing_a_foreign_kit_is_refused(clean_db):
    from cryptography.fernet import Fernet

    hid = _hid()
    settings_store.save_credentials(hid, "client", "secret", "production", 20)
    kit = recovery.export("correct horse", hid)  # a genuine kit from this install
    # Now this database gains a bank token encrypted with a different key.
    other = Fernet(Fernet.generate_key())
    db.execute(
        "INSERT INTO plaid_items(household_id, plaid_item_id, access_token_enc, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (hid, "item-1", other.encrypt(b"access-token").decode("ascii"), "good", db.utcnow(), db.utcnow()),
    )
    with pytest.raises(ValueError, match="does not match"):
        recovery.import_kit(kit, "correct horse", hid)


def test_missing_key_is_not_silently_recreated(clean_db):
    from app import plaid

    token = plaid.encrypt_token("access-token")
    recovery.key_path().unlink()
    with pytest.raises(plaid.PlaidError):
        plaid.decrypt_token(token)
    assert not recovery.key_path().exists()


def test_export_refuses_a_key_that_does_not_match(clean_db):
    from cryptography.fernet import Fernet

    hid = _hid()
    other = Fernet(Fernet.generate_key())
    db.execute(
        "INSERT INTO plaid_items(household_id, plaid_item_id, access_token_enc, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (hid, "item-1", other.encrypt(b"access-token").decode("ascii"), "good", db.utcnow(), db.utcnow()),
    )
    settings_store.save_credentials(hid, "client", "secret", "production", 20)
    with pytest.raises(ValueError, match="does not match"):
        recovery.export("correct horse", hid)
