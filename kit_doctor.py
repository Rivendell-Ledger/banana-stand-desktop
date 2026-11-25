"""KitDoctor - diagnose and repair the Banana Stand recovery kit.

Runs before the web app and without FastAPI, so it still works when the server
will not start. It never prints the Fernet key, Plaid secret, or access tokens;
the key fingerprint is a one-way hash that is safe to share with support.

Usage:
  KitDoctor.exe check [--data-dir PATH]
  KitDoctor.exe inspect KIT [--passphrase PASS] [--data-dir PATH]
  KitDoctor.exe reexport [--out PATH] [--passphrase PASS] [--force] [--data-dir PATH]
  KitDoctor.exe restore KIT [--passphrase PASS] [--data-dir PATH]
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

MIN_FREE = 0  # kept for older imports


def default_root() -> Path:
    override = (os.environ.get("BANANA_HOME") or "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "BananaStand"
    return Path.home() / ".local" / "share" / "BananaStand"


def configure(root: Path) -> None:
    root = root.expanduser().resolve()
    os.environ["BANANA_HOME"] = str(root)
    os.environ["BANANA_DATA"] = str(root / "data")
    os.environ["BANANA_DB"] = str(root / "data" / "banana-stand.db")
    os.environ["BANANA_SECRETS"] = str(root / "secret")
    os.environ["BANANA_BACKUPS"] = str(root / "backups")
    os.environ["BANANA_LOGS"] = str(root / "logs")


def fingerprint(key_bytes: bytes) -> str:
    return hashlib.sha256(key_bytes.strip()).hexdigest()[:12].upper()


def say(ok: bool, text: str) -> None:
    print(("[ok]   " if ok else "[FAIL] ") + text)


def read_key() -> bytes | None:
    from app import config

    path = Path(config.SECRET_DIR) / "fernet.key"
    try:
        return path.read_bytes().strip()
    except OSError:
        return None


def open_db():
    from app import config

    if not Path(config.DB_PATH).exists():
        return None
    conn = sqlite3.connect(str(config.DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def local_fingerprint() -> str | None:
    raw = read_key()
    return fingerprint(raw) if raw else None


# ---------------------------------------------------------------- check
def cmd_check(args) -> int:
    from app import config, plaid

    root = Path(config.DATA_DIR).parent
    print(f"Data folder: {root}")
    print(f"Database:    {config.DB_PATH}")
    ok = True
    key_missing = False

    key = read_key()
    if key:
        try:
            from cryptography.fernet import Fernet

            Fernet(key)
            say(True, f"Encryption key: present, fingerprint {fingerprint(key)}")
        except Exception:
            say(False, "Encryption key: file exists but is not a valid Fernet key")
            ok = False
    else:
        key_missing = True

    conn = open_db()
    if conn is None:
        say(False, "Database: not found")
        return 1

    stored_tokens = 0
    try:
        items = conn.execute(
            "SELECT id, institution_id, status, access_token_enc FROM plaid_items ORDER BY id"
        ).fetchall()
        stored_tokens = len(items)
        if not items:
            print("Bank connections: none stored")
        good = bad = 0
        for row in items:
            label = row["institution_id"] or "bank"
            try:
                plaid.decrypt_token(row["access_token_enc"])
                good += 1
                say(True, f"Item {row['id']} ({label}): access token decrypts")
            except Exception as exc:  # noqa: BLE001 - report, never crash
                bad += 1
                code = getattr(exc, "code", type(exc).__name__)
                say(False, f"Item {row['id']} ({label}): {code}")
        print(f"Bank connections: {good} ok, {bad} failed")
        if bad:
            ok = False

        creds = {r["key"]: r["value_json"] for r in conn.execute(
            "SELECT key, value_json FROM settings "
            "WHERE key IN ('plaid_client_id_enc','plaid_secret_enc')"
        )}
        if not creds:
            print("Plaid credentials: not saved in the database (env or not set up)")
        else:
            stored_tokens += len(creds)
            for name, value in creds.items():
                try:
                    plain = json.loads(value)
                    plaid.decrypt_token(plain)
                    say(True, f"{name}: decrypts")
                except Exception as exc:  # noqa: BLE001
                    say(False, f"{name}: {getattr(exc, 'code', type(exc).__name__)}")
                    ok = False
    finally:
        conn.close()

    if key_missing:
        if stored_tokens:
            say(False, "Encryption key: missing, but this data folder has stored secrets")
            ok = False
        else:
            say(True, "Encryption key: not created yet (normal before the first bank or kit)")

    print("\nVerdict: " + ("healthy" if ok else "problems found"))
    return 0 if ok else 1


# ---------------------------------------------------------------- inspect
def _passphrase(args, prompt: str = "Kit passphrase: ") -> str:
    if getattr(args, "passphrase", None):
        return args.passphrase
    return getpass.getpass(prompt)


def cmd_inspect(args) -> int:
    from app import recovery
    from cryptography.fernet import Fernet

    path = Path(args.kit)
    if not path.exists():
        print(f"Kit file not found: {path}")
        return 2
    text = path.read_text(encoding="utf-8", errors="replace")
    ok = True

    lines = text.splitlines()
    say(bool(lines) and lines[0].strip() == recovery.HEADER,
        f"Header: {lines[0].strip() if lines else '(empty file)'}")
    if not lines or lines[0].strip() != recovery.HEADER:
        print("\nVerdict: not a Banana Stand recovery kit")
        return 1

    try:
        fields = recovery._parse(text)
    except ValueError as exc:
        say(False, f"Structure: {exc}")
        print("\nVerdict: unreadable kit")
        return 1
    say(True, f"Structure: created {fields.get('created', 'unknown')}")

    raw_salt = fields["salt"]
    try:
        import base64

        salt = base64.b64decode(raw_salt)
        say(True, f"Salt: {len(salt)} bytes")
    except Exception:
        say(False, "Salt: not valid base64")
        return 1

    try:
        iterations = int(fields.get("iterations") or recovery.ITERATIONS)
        good = 10_000 <= iterations <= 5_000_000
        say(good, f"Work factor: {iterations} iterations")
        ok = ok and good
    except ValueError:
        say(False, f"Work factor: {fields.get('iterations')!r}")
        return 1

    try:
        import base64

        payload_raw = base64.b64decode(fields["payload"], validate=True)
        say(True, f"Payload: {len(payload_raw)} bytes")
    except Exception:
        say(False, "Payload: not valid base64 (file truncated or edited)")
        print("\nVerdict: damaged kit")
        return 1

    passphrase = _passphrase(args)
    if fields.get("verify"):
        matches = fields["verify"] == recovery._digest(passphrase, salt, iterations)
        say(matches, "Passphrase: matches the kit's verifier" if matches else
                     "Passphrase: does not match the kit's verifier")
        if not matches:
            print("\nVerdict: wrong passphrase (the kit itself may be fine)")
            return 1
    else:
        say(True, "Passphrase: no verifier in this kit (older format)")

    try:
        raw = Fernet(recovery._derive(passphrase, salt, iterations)).decrypt(payload_raw)
        payload = json.loads(raw.decode("utf-8"))
        say(isinstance(payload, dict), "Contents: decrypts to readable JSON")
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except Exception as exc:  # noqa: BLE001
        say(False, f"Contents: {type(exc).__name__}")
        print("\nVerdict: the passphrase is right but the payload cannot be opened")
        return 1

    kit_key = (payload.get("fernet_key") or "").strip()
    try:
        Fernet(kit_key.encode("ascii"))
        say(True, f"Key in kit: valid, fingerprint {fingerprint(kit_key.encode())}")
    except Exception:
        say(False, "Key in kit: missing or invalid")
        return 1

    local = local_fingerprint()
    if local:
        same = local == fingerprint(kit_key.encode())
        say(same, f"Matches this computer: {'yes' if same else 'no'} "
                  f"(local key {local})")
        if not same:
            ok = False
    else:
        print("This computer: no key to compare against")

    conn = open_db()
    if conn is not None:
        try:
            rows = conn.execute("SELECT id, access_token_enc FROM plaid_items").fetchall()
            candidate = Fernet(kit_key.encode("ascii"))
            good = bad = 0
            for row in rows:
                try:
                    candidate.decrypt(row["access_token_enc"].encode("ascii"))
                    good += 1
                except Exception:
                    bad += 1
            if rows:
                say(bad == 0, f"Stored bank tokens: {good} open, {bad} do not")
                ok = ok and bad == 0
        finally:
            conn.close()

    print("\nVerdict: " + ("kit is usable" if ok else "kit does not match this installation"))
    return 0 if ok else 1


def _household_or_none():
    """Return the household id, or None without creating a database file."""
    from app import config, settings_store

    if not Path(config.DB_PATH).exists():
        print("No data folder found; finish setup first.")
        return None
    try:
        hid = settings_store.household_id()
    except sqlite3.DatabaseError:
        print("This data folder's database cannot be read; it may be damaged. "
              "Restore a backup or your recovery kit.")
        return None
    if hid is None:
        print("No household in this data folder; finish setup first.")
        return None
    return hid


# ---------------------------------------------------------------- reexport
def cmd_reexport(args) -> int:
    from app import config, db, recovery, settings_store

    hid = _household_or_none()
    if hid is None:
        return 2

    passphrase = _passphrase(args, "New kit passphrase (8+ chars): ")
    if len(passphrase) < recovery.MIN_PASSPHRASE:
        print(f"Use at least {recovery.MIN_PASSPHRASE} characters.")
        return 2
    if not getattr(args, "passphrase", None):
        if passphrase != getpass.getpass("Confirm passphrase: "):
            print("The passphrases do not match.")
            return 2

    try:
        content = recovery.export(passphrase, hid)
    except (ValueError, OSError) as exc:
        print(f"Could not export: {exc}")
        return 1

    out = Path(args.out) if args.out else (
        Path(config.DATA_DIR).parent / f"recovery-kit-{db.utcnow()[:19].replace(':', '')}.txt"
    )
    if out.exists() and not args.force:
        print(f"{out} already exists; use --force to overwrite.")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    print(f"New kit: {out}")
    print(f"Key fingerprint: {local_fingerprint()}")
    print("Move it off this computer and keep the passphrase separate.")
    return 0


# ---------------------------------------------------------------- restore
def cmd_restore(args) -> int:
    from app import recovery, settings_store

    path = Path(args.kit)
    if not path.exists():
        print(f"Kit file not found: {path}")
        return 2
    hid = _household_or_none()
    if hid is None:
        return 2
    passphrase = _passphrase(args)
    try:
        recovery.import_kit(path.read_text(encoding="utf-8"), passphrase, hid)
    except (ValueError, OSError) as exc:
        print(f"Restore failed: {exc}")
        return 1
    print("Kit restored.")
    print(f"Key fingerprint: {local_fingerprint()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="KitDoctor",
        description="Diagnose and repair The Banana Stand recovery kit.")
    p.add_argument("--data-dir", help="Banana Stand data folder (default: %%LOCALAPPDATA%%\\BananaStand)")
    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("check", help="verify this computer's key against its stored bank tokens")
    c.add_argument("--data-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    c.set_defaults(func=cmd_check)

    i = sub.add_parser("inspect", help="explain why a kit does or does not open")
    i.add_argument("kit")
    i.add_argument("--passphrase", help="passphrase (omit to be prompted)")
    i.add_argument("--data-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    i.set_defaults(func=cmd_inspect)

    r = sub.add_parser("reexport", help="write a fresh kit from the key on this computer")
    r.add_argument("--out")
    r.add_argument("--passphrase", help="passphrase (omit to be prompted)")
    r.add_argument("--force", action="store_true")
    r.add_argument("--data-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    r.set_defaults(func=cmd_reexport)

    s = sub.add_parser("restore", help="restore a kit from the terminal")
    s.add_argument("kit")
    s.add_argument("--passphrase", help="passphrase (omit to be prompted)")
    s.add_argument("--data-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_restore)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    root = Path(args.data_dir) if getattr(args, "data_dir", None) else default_root()
    configure(root)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
