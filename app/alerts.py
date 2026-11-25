"""Alerts: evaluated on demand from the data already in the app."""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

from . import budgets, db, forecast, goals, recurring

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
DEFAULT_SETTINGS = {
    "alert_large_txn_cents": 50000,     # $500
    "alert_low_days": 3,                 # bills due within N days
    "alert_forecast_days": 30,
}


def get_setting(household_id: int, key: str):
    row = db.query_one("SELECT value_json FROM settings WHERE household_id = ? AND key = ?",
                       (household_id, key))
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    try:
        return json.loads(row["value_json"])
    except (TypeError, ValueError):
        return DEFAULT_SETTINGS.get(key)


def set_setting(household_id: int, key: str, value) -> None:
    db.execute(
        """INSERT INTO settings(household_id, key, value_json) VALUES (?,?,?)
           ON CONFLICT(household_id, key) DO UPDATE SET value_json = excluded.value_json""",
        (household_id, key, json.dumps(value)),
    )


def alert_key(alert: dict) -> str:
    """Stable across evaluations so a dismissal sticks to the same alert."""
    raw = "|".join(str(alert.get(part) or "") for part in ("kind", "title", "detail"))
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:12]


def dismissed_keys(household_id: int, day: str) -> set[str]:
    rows = db.query_all(
        "SELECT alert_key FROM alert_dismissals WHERE household_id = ? AND dismissal_date = ?",
        (household_id, day))
    return {row["alert_key"] for row in rows}


def dismiss(household_id: int, keys, day: str | None = None) -> int:
    day = day or date.today().isoformat()
    now = db.utcnow()
    count = 0
    for key in keys:
        if not key:
            continue
        db.execute(
            "INSERT INTO alert_dismissals(household_id, alert_key, dismissal_date, created_at)"
            " VALUES (?,?,?,?) ON CONFLICT(household_id, alert_key, dismissal_date) DO NOTHING",
            (household_id, key, day, now))
        count += 1
    return count


def evaluate(household_id: int, today: date | None = None,
             include_dismissed: bool = False) -> list[dict]:
    today = today or date.today()
    alerts: list[dict] = []

    # --- budgets over their envelope
    g = budgets.grid(household_id, budgets.month_key(today))
    for row in g["rows"]:
        if row["has_row"] and row["remaining_cents"] < 0:
            alerts.append({
                "kind": "budget_over",
                "severity": "warning",
                "title": f"{row['name']} is over budget",
                "detail": "Over its envelope for this month",
                "amount_cents": abs(row["remaining_cents"]),
                "href": "/budget",
            })

    # --- cash-flow breach
    fc = forecast.forecast(household_id, int(get_setting(household_id, "alert_forecast_days") or 30), today)
    if fc["has_recurring"] and fc["breach"]:
        short = fc["buffer_cents"] - fc["low"]["balance_cents"]
        alerts.append({
            "kind": "forecast_breach",
            "severity": "critical",
            "title": "Cash dips below the buffer",
            "detail": f"Lowest point {fc['low']['date']}",
            "amount_cents": short,
            "href": "/forecast",
        })

    # --- bills due soon
    horizon = int(get_setting(household_id, "alert_low_days") or 3)
    for bill in recurring.bills_between(household_id, today, today + timedelta(days=horizon)):
        days = (date.fromisoformat(bill["due_on"]) - today).days
        when = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")
        alerts.append({
            "kind": "bill_due",
            "severity": "info",
            "title": f"{bill['name']} is due {when}",
            "detail": bill["due_on"],
            "amount_cents": bill["amount_cents"],
            "href": "/bills",
        })

    # --- unusually large transactions in the last 7 days
    threshold = int(get_setting(household_id, "alert_large_txn_cents") or 50000)
    rows = db.query_all(
        """SELECT id, merchant, amount_cents, posted_on FROM transactions
           WHERE household_id = ? AND posted_on >= ? AND amount_cents <= ?
             AND deleted_at IS NULL AND is_pending = 0 AND is_transfer = 0 AND excluded = 0
           ORDER BY posted_on DESC LIMIT 5""",
        (household_id, (today - timedelta(days=7)).isoformat(), -threshold),
    )
    for row in rows:
        alerts.append({
            "kind": "large_txn",
            "severity": "info",
            "title": f"Large charge: {row['merchant']}",
            "detail": row["posted_on"],
            "amount_cents": abs(row["amount_cents"]),
            "href": f"/transactions/{row['id']}",
        })

    # --- goals behind pace
    for goal in goals.list_goals(household_id, include_finished=False):
        if goal["on_track"] is False and goal["remaining_cents"] > 0:
            alerts.append({
                "kind": "goal_behind",
                "severity": "info",
                "title": f"{goal['name']} is behind pace",
                "detail": "Needs a bigger monthly contribution to land on time",
                "amount_cents": goal["remaining_cents"],
                "href": "/goals",
            })

    alerts.sort(key=lambda a: (SEVERITY_ORDER.get(a["severity"], 3), -a["amount_cents"]))
    for alert in alerts:
        alert["key"] = alert_key(alert)
    if include_dismissed:
        return alerts
    hidden = dismissed_keys(household_id, today.isoformat())
    return [a for a in alerts if a["key"] not in hidden]


def counts(alerts: list[dict]) -> dict:
    return {
        "total": len(alerts),
        "critical": sum(1 for a in alerts if a["severity"] == "critical"),
        "warning": sum(1 for a in alerts if a["severity"] == "warning"),
        "info": sum(1 for a in alerts if a["severity"] == "info"),
    }
