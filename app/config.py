"""Application settings and paths. Environment overrides keep tests isolated."""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: existing environment wins, comments and blanks ignored."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

APP_NAME = "The Banana Stand"
APP_SLUG = "banana-stand"
VERSION = "1.21.12"  # keep in step with the release tag; CI fails a tag that disagrees

# Guard: a pytest run that skipped tests/conftest.py has no BANANA_DATA set and would
# otherwise point at the live database (the test fixture deletes DB_PATH before migrating).
if "pytest" in sys.modules and not os.environ.get("BANANA_DATA"):
    raise RuntimeError(
        "Refusing to run the app against the live database under pytest. "
        "Run tests from the repo root (tests/conftest.py sets BANANA_DATA), never a file elsewhere."
    )

DATA_DIR = Path(os.environ.get("BANANA_DATA", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("BANANA_DB", DATA_DIR / "banana-stand.db"))
BACKUP_DIR = Path(os.environ.get("BANANA_BACKUPS", BASE_DIR / "backups"))
LOG_DIR = Path(os.environ.get("BANANA_LOGS", BASE_DIR / "logs"))
SECRET_DIR = Path(os.environ.get("BANANA_SECRETS", BASE_DIR / "secret"))
MIGRATIONS_DIR = BASE_DIR / "migrations"
TEMPLATES_DIR = BASE_DIR / "app" / "templates"
STATIC_DIR = BASE_DIR / "app" / "static"

TIMEZONE = "America/New_York"
SESSION_DAYS = 30
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_MINUTES = 15
MIN_PASSWORD_LENGTH = 8

PLAID_CLIENT_ID = os.environ.get("PLAID_CLIENT_ID", "").strip()
PLAID_SECRET = os.environ.get("PLAID_SECRET", "").strip()
PLAID_ENV = os.environ.get("PLAID_ENV", "sandbox").strip().lower()
PLAID_MAX_ITEMS = int(os.environ.get("PLAID_MAX_ITEMS", "10") or 10)

for _d in (DATA_DIR, BACKUP_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def is_production() -> bool:
    return os.environ.get("BANANA_ENV", "dev").lower() == "production"
