"""Paths, environment and single-instance helpers for the desktop build."""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR_NAME = "BananaStand"
DEFAULT_PORT = 8093
PORT_RANGE = range(8093, 8110)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_root() -> Path:
    """Writable home for the database, backups, logs and the Fernet key.

    Windows: %LOCALAPPDATA%\\BananaStand (never Program Files, which is read-only
    for a per-user install, and never a cloud-synced Documents folder, where
    SQLite WAL files can corrupt).
    """
    override = (os.environ.get("BANANA_HOME") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    return Path.home() / ".banana-stand"


def prepare_environment() -> Path:
    """Create the data tree and point the app at it. Must run before app import."""
    root = data_root()
    for sub in ("data", "backups", "logs", "secret"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("BANANA_DATA", str(root / "data"))
    os.environ.setdefault("BANANA_BACKUPS", str(root / "backups"))
    os.environ.setdefault("BANANA_LOGS", str(root / "logs"))
    os.environ.setdefault("BANANA_SECRETS", str(root / "secret"))

    # The desktop build is served over http://127.0.0.1. Production mode would
    # mark the session cookie Secure and send HSTS, which breaks login there.
    if (os.environ.get("BANANA_ENV") or "").strip().lower() == "production":
        os.environ["BANANA_ENV"] = "dev"
    os.environ.setdefault("BANANA_ENV", "dev")

    # Keep the Fernet key and the database private on POSIX; Windows ACLs on
    # %LOCALAPPDATA% already restrict the folder to the user.
    if os.name != "nt":
        for sub, mode in (("secret", 0o700), ("data", 0o700)):
            try:
                (root / sub).chmod(mode)
            except OSError:
                pass
    return root


def state_path(root: Path) -> Path:
    return root / "desktop-state.json"


def read_state(root: Path) -> dict:
    try:
        return json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(root: Path, state: dict) -> None:
    try:
        state_path(root).write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _health(port: int, timeout: float = 0.8) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback
            body = resp.read(512)
        return (resp.status == 200 and b'"status"' in body
                and b"banana-stand" in body)
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def running_port(state: dict) -> int | None:
    """If our app already answers on the remembered port, return it.

    Only the saved port is probed: scanning thirteen ports on every launch made
    a cold start feel broken (each miss waits out the socket timeout).
    """
    saved = state.get("port")
    candidates = [saved] if isinstance(saved, int) else [DEFAULT_PORT]
    for port in candidates:
        if _health(port):
            return port
    return None


def _can_bind(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def choose_port(state: dict) -> int:
    saved = state.get("port")
    ordered = ([saved] if isinstance(saved, int) and saved in PORT_RANGE else []) + [
        p for p in PORT_RANGE if p != saved
    ]
    for port in ordered:
        if _can_bind(port):
            return port
    # Last resort: let the OS pick a free one.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until_ready(port: int, timeout: float = 40.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health(port, timeout=1.2):
            return True
        time.sleep(0.25)
    return False


def acquire_instance_lock(root: Path):
    """Hold an exclusive lock for the process lifetime. Returns None if another
    launcher already owns it. This closes the several-second window where two
    double-clicks both pass the health probe and then fight over the port."""
    path = Path(root) / "instance.lock"
    handle = open(path, "a+")  # noqa: SIM115 - held for the process lifetime
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def lan_ip() -> str:
    """The machine's address on the local network (not its loopback)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))  # TEST-NET; no packet is sent
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def lan_requested(db_path: Path) -> bool:
    """Read the Settings toggle straight from SQLite, before the app starts."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT value_json FROM settings WHERE key = 'desktop_lan' LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if not row:
        return False
    return str(row[0]).strip().lower() in {"true", "1"}
