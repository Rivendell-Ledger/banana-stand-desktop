"""Desktop entry point: serve the Stand on loopback, open the browser, sit in the tray."""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from . import runtime
from .scheduler import Scheduler

log = logging.getLogger("desktop")


def _configure_logging(root: Path) -> None:
    handlers: list[logging.Handler] = []
    file_handler = logging.FileHandler(root / "logs" / "desktop.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handlers.append(file_handler)
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        handlers.append(stream)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def _resource_dir() -> Path:
    if runtime.is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _notify_error(message: str) -> None:
    log.error(message)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, "The Banana Stand", 0x10)
        except Exception:  # noqa: BLE001
            pass


def _open(url: str) -> bool:
    try:
        opened = webbrowser.open(url)
    except Exception:  # noqa: BLE001
        opened = False
    if not opened:
        log.warning("could not open a browser; visit %s", url)
    return bool(opened)


def _open_folder(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:  # noqa: BLE001
        log.warning("could not open %s", path)


def _load_icon():
    from PIL import Image

    for name in ("banana.ico", "banana-mark.png"):
        path = _resource_dir() / "assets" / name
        if path.exists():
            try:
                return Image.open(path)
            except Exception:  # noqa: BLE001
                continue
    return Image.new("RGBA", (64, 64), (245, 197, 24, 255))


def run_tray(url: str, root: Path, server, scheduler: Scheduler) -> str:
    """Tray icon in the main thread; the server runs on a worker thread.

    Returns "restart" when the user asked to relaunch, otherwise "quit".
    """
    outcome = {"value": "quit"}
    try:
        import pystray
        from pystray import MenuItem as Item
    except Exception:  # noqa: BLE001
        log.warning("pystray unavailable; running without a tray icon")
        try:
            while server.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return "quit"

    def back_up() -> None:
        def work() -> None:
            from app import maintenance

            try:
                path = maintenance.backup_to()
                log.info("manual backup: %s", path)
            except Exception:  # noqa: BLE001
                log.exception("manual backup failed")
        threading.Thread(target=work, daemon=True).start()

    def shutdown(icon, restart: bool = False) -> None:
        log.info("restart requested" if restart else "quit requested")
        outcome["value"] = "restart" if restart else "quit"
        scheduler.stop()
        server.should_exit = True
        icon.stop()

    menu = pystray.Menu(
        Item("Open Banana Stand", lambda: _open(url), default=True),
        Item("Back up now", back_up),
        Item("Open data folder", lambda: _open_folder(root)),
        pystray.Menu.SEPARATOR,
        Item("Restart", lambda icon, item: shutdown(icon, True)),
        Item("Quit", shutdown),
    )
    icon = pystray.Icon("banana-stand", _load_icon(), "The Banana Stand", menu)
    try:
        icon.run()
    finally:
        scheduler.stop()
        server.should_exit = True
    return outcome["value"]


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except Exception as exc:  # noqa: BLE001 - never show a raw traceback to the user
        text = traceback.format_exc()
        fallback = None
        try:
            base = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
            fallback = base / "BananaStand-error.log"
            fallback.write_text(text, encoding="utf-8")
        except OSError:
            fallback = None
        try:
            log.exception("fatal startup error")
        except Exception:  # noqa: BLE001
            pass
        _notify_error(
            "The Banana Stand could not start.\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            + (f"Details were written to:\n{fallback}" if fallback else "")
        )
        return 1


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="BananaStand")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--lan", action="store_true",
                        help="listen on the local network so phones can reach it")
    args = parser.parse_args(argv)

    if args.data_dir:
        os.environ["BANANA_HOME"] = args.data_dir

    root = runtime.prepare_environment()
    _configure_logging(root)
    log.info("data folder: %s", root)

    state = runtime.read_state(root)
    instance_lock = runtime.acquire_instance_lock(root)
    if instance_lock is None:
        # Another launcher owns the lock; give it a moment to bind, then reuse it.
        port = state.get("port") if isinstance(state.get("port"), int) else None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            found = runtime.running_port(state)
            if found:
                _open(f"http://127.0.0.1:{found}/")
                log.info("already running on %s", found)
                return 0
            time.sleep(0.5)
        _notify_error("The Banana Stand is already starting. Give it a few seconds, then open it again.")
        return 0

    existing = runtime.running_port(state)
    if existing:
        url = f"http://127.0.0.1:{existing}/"
        log.info("already running on %s", existing)
        _open(url)
        return 0

    port = args.port or runtime.choose_port(state)
    state["port"] = port
    runtime.write_state(root, state)

    # Import only after the environment points at the real data folder.
    import uvicorn

    from app import config as app_config
    from app.main import app

    lan = args.lan or runtime.lan_requested(app_config.DB_PATH)
    host = "0.0.0.0" if lan else "127.0.0.1"
    os.environ["BANANA_LAN_ACTIVE"] = "1" if lan else "0"

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="banana-server", daemon=True)
    thread.start()

    if not runtime.wait_until_ready(port):
        _notify_error(
            f"The Banana Stand did not start.\n\nSee the log at:\n{root / 'logs' / 'desktop.log'}"
        )
        server.should_exit = True
        return 1

    url = f"http://127.0.0.1:{port}/"
    log.info("listening on %s", url)
    if not args.no_browser:
        _open(url)

    scheduler = Scheduler(root)
    scheduler.start()

    outcome = "quit"
    try:
        if args.no_tray:
            while thread.is_alive():
                time.sleep(1)
        else:
            outcome = run_tray(url, root, server, scheduler)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        server.should_exit = True
        thread.join(timeout=8)
        log.info("stopped")

    if outcome == "restart":
        log.info("relaunching")
        if runtime.is_frozen():
            os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
        else:
            os.execv(sys.executable, [sys.executable, *sys.argv])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
