"""Tell the owner when a newer release exists — only if they asked to be told.

The Banana Stand is local-first, so this check is **off by default**. When it is
on, the only thing that leaves the machine is an unauthenticated GET to the
public Releases API for this repository: no account, no identifier, no usage
data. The answer is cached for six hours in the household settings.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request

from . import config, settings_store

logger = logging.getLogger("banana.updates")

REPO = "Rivendell-Ledger/banana-stand-desktop"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
ENABLED_KEY = "update_check_enabled"
CACHE_KEY = "update_check_cache"
TTL_SECONDS = 6 * 60 * 60
TIMEOUT_SECONDS = 6

_lock = threading.Lock()


def enabled(household_id: int) -> bool:
    return bool(settings_store.get(household_id, ENABLED_KEY, False))


def set_enabled(household_id: int, on: bool) -> None:
    settings_store.set(household_id, ENABLED_KEY, bool(on))
    if not on:
        # Drop the cached answer so turning it back on re-checks from scratch.
        settings_store.set(household_id, CACHE_KEY, None)


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.21.12' -> (1, 21, 12). Pre-release suffixes are ignored."""
    head = (text or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    parts = re.findall(r"\d+", head)
    return tuple(int(p) for p in parts[:4]) or (0,)


def is_newer(latest: str, current: str | None = None) -> bool:
    return parse_version(latest) > parse_version(current or config.VERSION)


def _fetch_latest() -> dict | None:
    request = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"BananaStand/{config.VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310 - fixed host
        payload = json.loads(resp.read(200_000).decode("utf-8"))
    tag = str(payload.get("tag_name") or "").strip()
    if not tag:
        return None
    return {
        "latest": tag.lstrip("vV"),
        "url": payload.get("html_url") or RELEASES_URL,
        "published": payload.get("published_at") or "",
        "checked_at": time.time(),
    }


def _record(household_id: int) -> dict:
    return settings_store.get(household_id, CACHE_KEY) or {}


def _is_fresh(record: dict) -> bool:
    try:
        return (time.time() - float(record.get("checked_at") or 0)) < TTL_SECONDS
    except (TypeError, ValueError):
        return False


def refresh(household_id: int, force: bool = False) -> dict | None:
    """Ask GitHub at most once per TTL. Never raises."""
    if not enabled(household_id):
        return None
    record = _record(household_id)
    if not force and _is_fresh(record):
        return record
    with _lock:
        # Another thread may have refreshed while we waited.
        record = _record(household_id)
        if not force and _is_fresh(record):
            return record
        try:
            fetched = _fetch_latest()
        except Exception as exc:  # noqa: BLE001 - offline is not an error here
            logger.debug("update check failed: %s", exc)
            fetched = None
        if fetched is None:
            # Keep whatever we knew; stamp the attempt so we do not retry in a loop.
            record = dict(record)
            record["checked_at"] = time.time()
        else:
            record = dict(fetched)
            record["checked_at"] = time.time()
        settings_store.set(household_id, CACHE_KEY, record)
        return record


def refresh_async(household_id: int, force: bool = False) -> None:
    """Kick a check from a request or startup without blocking on the network."""
    if not enabled(household_id):
        return
    if not force and _is_fresh(_record(household_id)):
        return
    threading.Thread(target=refresh, args=(household_id, force), name="update-check", daemon=True).start()


def notice(household_id: int | None) -> dict | None:
    """What the banner needs, from cache only. No network call on a page render."""
    if not household_id or not enabled(household_id):
        return None
    record = _record(household_id)
    latest = str(record.get("latest") or "")
    if not latest or not is_newer(latest):
        return None
    return {
        "latest": latest,
        "current": config.VERSION,
        "url": record.get("url") or RELEASES_URL,
        "published": record.get("published") or "",
    }
