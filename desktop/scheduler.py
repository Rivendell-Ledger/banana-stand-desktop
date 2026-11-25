"""In-process replacement for the Linux systemd timers.

The desktop build has no cron/systemd, so the same work runs on a small
background thread: bank sync, market refresh, bitcoin repricing, the nightly
snapshot, backups and retention. State lives in the data folder so a restart
does not re-run yesterday's nightly jobs.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("desktop.scheduler")

TZ = ZoneInfo("America/New_York")
MARKET_HOURS = (10, 11, 12, 13, 14, 15, 16, 21)


class Job:
    def __init__(self, name: str, kind: str, run, *, seconds: int = 0,
                 at: tuple[int, int] | None = None, hours: tuple[int, ...] = ()) -> None:
        self.name = name
        self.kind = kind
        self.run = run
        self.seconds = seconds
        self.at = at
        self.hours = hours

    def occurrence(self, now: datetime) -> datetime | None:
        """The most recent moment this job should have fired at or before now."""
        if self.kind == "interval":
            return None  # handled by last-run arithmetic
        if self.kind == "daily":
            hour, minute = self.at or (0, 0)
            occ = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if occ > now:
                occ -= timedelta(days=1)
            return occ
        if self.kind == "hourly":
            options = [now.replace(hour=h, minute=0, second=0, microsecond=0)
                       for h in self.hours if h <= now.hour]
            return max(options) if options else None
        return None

    def due(self, now: datetime, last: datetime | None) -> bool:
        if self.kind == "interval":
            if last is None:
                return True
            return (now - last).total_seconds() >= self.seconds
        occ = self.occurrence(now)
        if occ is None:
            return False
        return last is None or last < occ


# ------------------------------------------------------------------ the work
def _households() -> list[int]:
    from app import db

    return [int(r["id"]) for r in db.query_all("SELECT id FROM households ORDER BY id")]


def job_sync() -> None:
    from app import db, plaid, plaid_sync

    if not plaid.configured():
        return
    rows = db.query_all("SELECT * FROM plaid_items ORDER BY id")
    for row in rows:
        item = dict(row)
        summary = plaid_sync.sync_item(item["household_id"], item)
        name = item.get("institution_name") or item.get("plaid_item_id")
        if summary.get("error"):
            log.warning("sync %s: %s", name, summary["error"])
        else:
            log.info("sync %s: %s new, %s updated", name, summary["added"], summary["modified"])


def job_market() -> None:
    from app import db, plaid, plaid_sync

    if not plaid.configured():
        return
    for row in db.query_all("SELECT DISTINCT household_id FROM plaid_items"):
        for result in plaid_sync.poll_market(int(row["household_id"])):
            if result.get("error"):
                log.warning("market %s: %s", result.get("item"), result["error"])


def job_btc() -> None:
    from app import bitcoin

    price = bitcoin.refresh_all()
    if price:
        log.debug("bitcoin repriced at %s cents", price)


def job_snapshot() -> None:
    from app import networth

    for hid in _households():
        result = networth.snapshot(hid)
        log.info("snapshot household %s: %s accounts", hid, result.get("accounts"))


def _prune_backups(keep: int = 30) -> None:
    """The scheduler adds one file a day; keep the newest N and drop the rest."""
    from app import config

    try:
        files = sorted(Path(config.BACKUP_DIR).glob("banana-stand-*.db"),
                       key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in files[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def job_backup() -> None:
    from app import maintenance

    path = maintenance.backup_to()
    _prune_backups()
    log.info("backup written: %s", path)


def job_maintain() -> None:
    from app import maintenance

    result = maintenance.purge()
    maintenance.checkpoint()
    log.info("retention: %s trashed txns, %s sessions, %s audit rows",
             result.get("purged_transactions"), result.get("purged_sessions"),
             result.get("purged_audit"))
    # Prove the newest backup is restorable, the way the Linux timer does.
    try:
        drill = maintenance.drill()
        if drill.get("ok") and not drill.get("mismatches"):
            log.info("restore drill passed")
        else:
            log.warning("restore drill failed: %s", drill.get("integrity") or drill.get("mismatches"))
    except Exception:  # noqa: BLE001 - a drill failure must not kill the loop
        log.exception("restore drill errored")


JOBS = [
    Job("btc", "interval", job_btc, seconds=300),
    Job("sync", "interval", job_sync, seconds=6 * 3600),
    Job("market", "hourly", job_market, hours=MARKET_HOURS),
    Job("snapshot", "daily", job_snapshot, at=(23, 45)),
    Job("backup", "daily", job_backup, at=(3, 10)),
    Job("maintain", "daily", job_maintain, at=(3, 40)),
]


class Scheduler(threading.Thread):
    def __init__(self, root: Path, interval: float = 20.0) -> None:
        super().__init__(name="banana-scheduler", daemon=True)
        self.root = Path(root)
        self.interval = interval
        self._stop_event = threading.Event()
        self._state_file = self.root / "scheduler-state.json"
        self._last: dict[str, datetime] = {}
        self._retry: dict[str, datetime] = {}
        self._load()

    # -------------------------------------------------------------- state
    def _load(self) -> None:
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for name, value in raw.items():
            try:
                self._last[name] = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        payload = {k: v.isoformat() for k, v in self._last.items()}
        try:
            tmp = self._state_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self._state_file)
        except OSError:
            log.warning("could not write scheduler state", exc_info=True)

    # -------------------------------------------------------------- loop
    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("scheduler started (%s jobs)", len(JOBS))
        while not self._stop_event.is_set():
            now = datetime.now(TZ)
            for job in JOBS:
                retry_at = self._retry.get(job.name)
                if retry_at and now < retry_at:
                    continue
                last = self._last.get(job.name)
                if not job.due(now, last):
                    continue
                ok = False
                try:
                    job.run()
                    ok = True
                except Exception:  # noqa: BLE001 - a job must never kill the loop
                    log.exception("job %s failed", job.name)
                    self._retry[job.name] = now + timedelta(minutes=5)
                if ok:
                    self._retry.pop(job.name, None)
                    self._last[job.name] = now
                    self._save()
            self._stop_event.wait(self.interval)
        log.info("scheduler stopped")
