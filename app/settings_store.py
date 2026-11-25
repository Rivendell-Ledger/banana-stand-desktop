"""Household settings plus the Plaid credential store used by the desktop build.

Plaid keys used to be env-only. A desktop install should not ask anyone to edit
a .env file, so they are stored encrypted in the settings table and loaded into
the environment at startup. plaid.client_id()/secret() read os.environ on every
call, so saving a key takes effect without a restart.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from . import db, plaid

logger = logging.getLogger("banana.settings")

CLIENT_KEY = "plaid_client_id_enc"
SECRET_KEY = "plaid_secret_enc"
ENV_KEY = "plaid_env"
MAX_ITEMS_KEY = "plaid_max_items"

ENV_VARS = {
    "PLAID_CLIENT_ID": CLIENT_KEY,
    "PLAID_SECRET": SECRET_KEY,
    "PLAID_ENV": ENV_KEY,
    "PLAID_MAX_ITEMS": MAX_ITEMS_KEY,
}

_test_lock = threading.Lock()


def get(household_id: int, key: str, default=None):
    row = db.query_one(
        "SELECT value_json FROM settings WHERE household_id = ? AND key = ?",
        (household_id, key),
    )
    if row is None:
        return default
    try:
        return json.loads(row["value_json"])
    except (TypeError, ValueError):
        return default


def set(household_id: int, key: str, value) -> None:  # noqa: A001 - mirrors main.py
    db.execute(
        "INSERT INTO settings(household_id, key, value_json) VALUES (?,?,?)"
        " ON CONFLICT(household_id, key) DO UPDATE SET value_json = excluded.value_json",
        (household_id, key, json.dumps(value)),
    )


def household_id() -> int | None:
    row = db.query_one("SELECT id FROM households ORDER BY id LIMIT 1")
    return int(row["id"]) if row else None


def stored_credentials(household_id: int) -> dict:
    out = {"client_id": "", "secret": "", "env": "", "max_items": None, "unreadable": False}
    client = get(household_id, CLIENT_KEY)
    secret = get(household_id, SECRET_KEY)
    if client:
        try:
            out["client_id"] = plaid.decrypt_token(client)
        except plaid.PlaidError:
            out["unreadable"] = True
    if secret:
        try:
            out["secret"] = plaid.decrypt_token(secret)
        except plaid.PlaidError:
            out["unreadable"] = True
    out["env"] = get(household_id, ENV_KEY) or ""
    out["max_items"] = get(household_id, MAX_ITEMS_KEY)
    return out


def _apply_env(values: dict) -> None:
    for var, value in values.items():
        if value in (None, ""):
            continue
        os.environ[var] = str(value)


def plaid_bootstrap() -> None:
    """Load stored keys into the environment on startup (env still wins)."""
    try:
        hid = household_id()
    except Exception:  # noqa: BLE001 - table may not exist on a very first run
        return
    if hid is None:
        return
    values = {}
    for var, key in ENV_VARS.items():
        if os.environ.get(var):
            continue
        stored = get(hid, key)
        if stored in (None, ""):
            continue
        if key in (CLIENT_KEY, SECRET_KEY):
            try:
                stored = plaid.decrypt_token(stored)
            except plaid.PlaidError:
                logger.warning("stored Plaid credential %s cannot be decrypted; "
                               "a recovery kit may be needed", key)
                continue
        values[var] = stored
    _apply_env(values)


def test_credentials(client_id: str, secret: str, environment: str, max_items: int | None) -> tuple[bool, str]:
    """Mint a Link token with the supplied keys. Returns (ok, message)."""
    saved = {var: os.environ.get(var) for var in ENV_VARS}
    with _test_lock:
        try:
            os.environ["PLAID_CLIENT_ID"] = client_id
            os.environ["PLAID_SECRET"] = secret
            os.environ["PLAID_ENV"] = environment
            if max_items:
                os.environ["PLAID_MAX_ITEMS"] = str(max_items)
            plaid.create_link_token("setup-check")
            return True, ""
        except plaid.PlaidError as exc:
            return False, exc.message
        except Exception as exc:  # noqa: BLE001
            return False, f"Could not reach Plaid ({type(exc).__name__})."
        finally:
            for var, value in saved.items():
                if value is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = value


def save_credentials(household_id: int, client_id: str, secret: str,
                     environment: str, max_items: int | None) -> None:
    values = {
        CLIENT_KEY: plaid.encrypt_token(client_id),
        SECRET_KEY: plaid.encrypt_token(secret),
        ENV_KEY: environment,
        MAX_ITEMS_KEY: int(max_items) if max_items else 10,
    }
    # One transaction: a crash must not leave a new client id with an old secret.
    with db.session_scope() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(household_id, key, value_json) VALUES (?,?,?)"
                " ON CONFLICT(household_id, key) DO UPDATE SET value_json = excluded.value_json",
                (household_id, key, json.dumps(value)),
            )
    _apply_env({
        "PLAID_CLIENT_ID": client_id,
        "PLAID_SECRET": secret,
        "PLAID_ENV": environment,
        "PLAID_MAX_ITEMS": int(max_items) if max_items else 10,
    })


def clear_credentials(household_id: int) -> None:
    for key in (CLIENT_KEY, SECRET_KEY, ENV_KEY, MAX_ITEMS_KEY):
        db.execute("DELETE FROM settings WHERE household_id = ? AND key = ?", (household_id, key))
    for var in ENV_VARS:
        os.environ.pop(var, None)
