"""Passphrase-wrapped recovery kit for the Plaid token-encryption key.

The Fernet key in secret/fernet.key decrypts every stored bank access token.
Losing it does not lose transactions, but every institution must be re-linked.
This module exports that key (and the Plaid credentials) encrypted under a
passphrase chosen by the user, so the kit can be kept off-machine separately
from the database backups.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import config, db, plaid, settings_store

HEADER = "BANANA-STAND RECOVERY KIT v1"
ITERATIONS = 300_000
MIN_PASSPHRASE = 8

INSTRUCTIONS = """\
How to use this file
--------------------
1. Keep it somewhere off this computer: a USB drive, a password manager, or
   cloud storage. Do not keep it in the Banana Stand data folder.
2. It is encrypted with the passphrase you chose. Without the passphrase it is
   useless to anyone who finds it.
3. To restore: start the Banana Stand on a new computer, finish the account
   setup step, then go to Settings -> Recovery kit -> Restore from kit and paste
   this file's contents with the passphrase.
4. Without this key the app still has all of its transaction history, but every
   bank connection must be linked again.
"""


def _derive(passphrase: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _digest(passphrase: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    """A cheap verifier so a wrong passphrase says so plainly."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=16, salt=salt + b"verify",
                     iterations=iterations)
    return base64.b64encode(kdf.derive(passphrase.encode("utf-8"))).decode("ascii")


def key_path() -> Path:
    return Path(config.SECRET_DIR) / "fernet.key"


def has_key() -> bool:
    return key_path().exists()


def export(passphrase: str, household_id: int) -> str:
    creds = settings_store.stored_credentials(household_id)
    has_items = db.query_one("SELECT 1 AS one FROM plaid_items LIMIT 1") is not None
    if not has_key():
        # Creating a key here would silently turn an unrecoverable install into one
        # that looks healthy while every old token is undecryptable.
        if has_items or creds.get("client_id") or creds.get("secret") or creds.get("unreadable"):
            raise ValueError("The encryption key is missing from this data folder. Restore "
                             "your existing recovery kit instead of making a new one.")
        plaid.ensure_key()
    # A kit made with a key that cannot open the current data is worse than no kit:
    # it looks like a backup but is refused at restore time.
    if creds.get("unreadable"):
        raise ValueError("The key in this data folder does not match the stored Plaid "
                         "credentials. Restore your existing kit instead of making a new one.")
    for row in db.query_all("SELECT access_token_enc FROM plaid_items"):
        try:
            plaid.decrypt_token(row["access_token_enc"])
        except plaid.PlaidError:
            raise ValueError("The key in this data folder does not match the stored bank "
                             "connections. Restore your existing kit instead of making a new one.") from None
    payload = {
        "fernet_key": key_path().read_text(encoding="utf-8").strip(),
        # Preserve the exact bytes (some installs end the file with a newline).
        "fernet_key_b64": base64.b64encode(key_path().read_bytes()).decode("ascii"),
        "plaid": creds,
        "created_at": db.utcnow(),
        "app": config.APP_NAME,
    }
    salt = os.urandom(16)
    token = Fernet(_derive(passphrase, salt)).encrypt(json.dumps(payload).encode("utf-8"))
    return "\n".join([
        HEADER,
        f"created: {db.utcnow()}",
        f"iterations: {ITERATIONS}",
        f"salt: {base64.b64encode(salt).decode('ascii')}",
        f"verify: {_digest(passphrase, salt)}",
        "payload:",
        base64.b64encode(token).decode("ascii"),
        "",
        INSTRUCTIONS,
    ])


def _parse(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    payload_lines: list[str] = []
    in_payload = False
    lines = text.splitlines()
    if not lines or lines[0].strip() != HEADER:
        raise ValueError("This does not look like a Banana Stand recovery kit.")
    for line in lines[1:]:
        if in_payload:
            if not line.strip():
                if payload_lines:
                    break  # the payload is one base64 block, then prose
                continue
            payload_lines.append(line.strip())
            continue
        if line.startswith("payload:"):
            in_payload = True
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    if not fields.get("salt") or not payload_lines:
        raise ValueError("This does not look like a Banana Stand recovery kit.")
    fields["payload"] = "".join(payload_lines)
    return fields


def import_kit(text: str, passphrase: str, household_id: int) -> dict:
    fields = _parse(text)
    try:
        iterations = int(fields.get("iterations") or ITERATIONS)
    except ValueError:
        raise ValueError("This kit has an unreadable work factor.") from None
    if iterations < 10_000 or iterations > 5_000_000:
        raise ValueError("This kit has an unsupported work factor.")
    salt = base64.b64decode(fields["salt"])
    if fields.get("verify") and fields["verify"] != _digest(passphrase, salt, iterations):
        raise ValueError("That passphrase does not open this kit.")
    try:
        raw = Fernet(_derive(passphrase, salt, iterations)).decrypt(base64.b64decode(fields["payload"]))
    except (InvalidToken, ValueError) as exc:
        raise ValueError("That passphrase does not open this kit.") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("The kit's contents are not readable.") from exc
    if not isinstance(payload, dict):
        raise ValueError("The kit's contents are not readable.")

    fernet_key = payload.get("fernet_key")
    if not isinstance(fernet_key, str) or not fernet_key.strip():
        raise ValueError("The kit does not contain a usable encryption key.")
    fernet_key = fernet_key.strip()
    try:
        candidate = Fernet(fernet_key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError("The kit does not contain a usable encryption key.") from exc

    # Refuse a kit from a different installation: swapping the key would make
    # every stored bank token unreadable with no way back through the UI.
    row = db.query_one("SELECT access_token_enc FROM plaid_items LIMIT 1")
    if row:
        try:
            candidate.decrypt(row["access_token_enc"].encode("ascii"))
        except (InvalidToken, ValueError):
            raise ValueError(
                "This kit's key does not match the banks stored in this database. "
                "Use the kit made by this installation, then reconnect your banks."
            ) from None

    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old_key = path.read_bytes() if path.exists() else None
    if old_key is not None:
        backup = path.with_suffix(f".key.bak-{db.utcnow().replace(':', '')}")
        try:
            backup.write_bytes(old_key)
            backup.chmod(0o600)
        except OSError:
            pass
    # Temp + replace so a crash cannot leave a truncated key.
    temp = path.with_suffix(".key.tmp")
    raw_key = payload.get("fernet_key_b64")
    if isinstance(raw_key, str) and raw_key:
        try:
            key_bytes = base64.b64decode(raw_key)
        except ValueError:
            raise ValueError("The kit does not contain a usable encryption key.") from None
        if key_bytes.strip() != fernet_key.encode("ascii"):
            raise ValueError("The kit's key fields disagree; refusing to restore it.")
    else:
        key_bytes = fernet_key.encode("ascii")
    temp.write_bytes(key_bytes)
    os.replace(temp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass

    creds = payload.get("plaid") or {}
    if not isinstance(creds, dict):
        creds = {}
    try:
        if creds.get("client_id") and creds.get("secret"):
            environment = creds.get("env") or "production"
            settings_store.save_credentials(
                household_id,
                creds["client_id"],
                creds["secret"],
                environment,
                creds.get("max_items") or 10,
            )
            if environment == "production":
                # The kit came from a production install that already accepted the warning.
                # Without this the next Link token call is refused with no visible control.
                settings_store.set(household_id, "plaid_production_ack", True)
    except Exception:
        # Do not leave a key that opens the old tokens but not the newly written credentials.
        if old_key is not None:
            path.write_bytes(old_key)
        else:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return payload
