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
# /releases/latest skips prereleases, and every early build here is one: it answers
# 404 and nothing is ever announced. List instead, then pick the newest ourselves.
API_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
RELEASES_URL = f"https://github.com/{REPO}/releases"
ENABLED_KEY = "update_check_enabled"
CACHE_KEY = "update_check_cache"
TTL_SECONDS = 6 * 60 * 60
RETRY_SECONDS = 15 * 60
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


def _pick_latest(releases: list) -> dict | None:
    """Newest by version, drafts skipped and prereleases included."""
    best: dict | None = None
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag = str(rel.get("tag_name") or "").strip()
        if not tag:
            continue
        if best is None or parse_version(tag) > parse_version(best["latest"]):
            best = {
                "latest": tag.lstrip("vV"),
                "url": rel.get("html_url") or RELEASES_URL,
                "published": rel.get("published_at") or "",
                "prerelease": bool(rel.get("prerelease")),
            }
    return best


def _fetch_latest() -> dict | None:
    request = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"BananaStand/{config.VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310 - fixed host
        payload = json.loads(resp.read(400_000).decode("utf-8"))
    if not isinstance(payload, list):
        return None
    return _pick_latest(payload)


def _record(household_id: int) -> dict:
    return settings_store.get(household_id, CACHE_KEY) or {}


def _is_fresh(record: dict) -> bool:
    now = time.time()
    # A new build checks immediately instead of trusting the old build's answer.
    if record.get("app_version") != config.VERSION:
        return False
    try:
        retry_at = float(record.get("retry_at") or 0)
    except (TypeError, ValueError):
        retry_at = 0
    if retry_at:
        # A failed check retries sooner than a successful one.
        return now < retry_at
    try:
        return (now - float(record.get("checked_at") or 0)) < TTL_SECONDS
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
            # Keep whatever we knew; back off briefly instead of retrying per launch.
            record = dict(record)
            record["checked_at"] = time.time()
            record["retry_at"] = time.time() + RETRY_SECONDS
            record["app_version"] = config.VERSION
        else:
            record = dict(fetched)
            record["checked_at"] = time.time()
            record["app_version"] = config.VERSION
            record.pop("retry_at", None)
        settings_store.set(household_id, CACHE_KEY, record)
        return record


def refresh_async(household_id: int, force: bool = False) -> None:
    """Kick a check from a request or startup without blocking on the network."""
    if not enabled(household_id):
        return
    if not force and _is_fresh(_record(household_id)):
        return
    threading.Thread(target=refresh, args=(household_id, force), name="update-check", daemon=True).start()


def check_now(household_id: int) -> dict:
    """Force one check and say plainly what came back."""
    record = refresh(household_id, force=True)
    if record is None:
        return {"status": "off", "message": "Update checks are turned off."}
    if record.get("retry_at"):
        return {"status": "error",
                "message": "Couldn't reach GitHub just now. Try again in a few minutes."}
    latest = str(record.get("latest") or "")
    if latest and is_newer(latest):
        return {"status": "newer", "latest": latest,
                "message": f"Version {latest} is available — see the release notes."}
    return {"status": "latest", "latest": latest or config.VERSION,
            "message": f"You're on the latest version ({config.VERSION})."}


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
