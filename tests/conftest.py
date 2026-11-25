"""Test isolation: point the app at a throwaway database before it is imported."""
import os
import tempfile
from pathlib import Path

_BASE = Path(tempfile.mkdtemp(prefix="banana-test-"))

os.environ["BANANA_DATA"] = str(_BASE / "data")
os.environ["BANANA_DB"] = str(_BASE / "data" / "test.db")
os.environ["BANANA_BACKUPS"] = str(_BASE / "backups")
os.environ["BANANA_LOGS"] = str(_BASE / "logs")
os.environ["BANANA_ENV"] = "dev"
