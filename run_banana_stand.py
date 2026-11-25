"""PyInstaller entry point. Kept at the top level so `desktop` is imported as a package."""
from desktop.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
