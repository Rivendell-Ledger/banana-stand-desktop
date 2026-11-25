"""Savings goals: targets, progress, and the monthly pace needed to arrive on time."""
from __future__ import annotations

import math
from datetime import date

from . import db

STATUSES = [("active", "Active"), ("achieved", "Achieved"), ("paused", "Paused")]


def list_goals(household_id: int, include_finished: bool = True) -> list[dict]:
    sql = "SELECT * FROM goals WHERE household_id = ?"
    if not include_finished:
        sql += " AND status = 'active'"
    sql += " ORDER BY status = 'achieved', COALESCE(target_date, '9999-12-31'), name"
    return [progress(dict(r)) for r in db.query_all(sql, (household_id,))]


def get(household_id: int, goal_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM goals WHERE household_id = ? AND id = ?", (household_id, goal_id))
    return progress(dict(row)) if row else None


def progress(goal: dict, today: date | None = None) -> dict:
    today = today or date.today()
    target = max(0, int(goal["target_cents"]))
    saved = max(0, int(goal["saved_cents"]))
    remaining = max(0, target - saved)
    pct = (saved / target * 100) if target else 0.0
    months_left = None
    monthly_needed = None
    if goal.get("target_date"):
        try:
            due = date.fromisoformat(goal["target_date"])
        except ValueError:
            due = None
        if due:
            days = (due - today).days
            months_left = max(0.0, days / 30.44)
            monthly_needed = int(math.ceil(remaining / months_left)) if months_left > 0.1 else remaining
    goal = dict(goal)
    goal.update({
        "saved_cents": saved,
        "target_cents": target,
        "remaining_cents": remaining,
        "pct": min(100.0, pct),
        "months_left": months_left,
        "monthly_needed_cents": monthly_needed,
        "is_done": saved >= target and target > 0,
        "on_track": _on_track(goal, saved, target, today),
    })
    return goal


def _on_track(goal: dict, saved: int, target: int, today: date) -> bool | None:
    if not goal.get("target_date") or target <= 0:
        return None
    try:
        due = date.fromisoformat(goal["target_date"])
        started = date.fromisoformat((goal.get("created_at") or "")[:10])
    except ValueError:
        return None
    span = (due - started).days
    if span <= 0:
        return saved >= target
    elapsed = max(0, (today - started).days)
    expected = target * min(1.0, elapsed / span)
    return saved >= expected * 0.95


def create(household_id: int, name: str, target_cents: int, target_date: str | None = None,
           account_id: int | None = None, notes: str | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the goal a name.")
    if target_cents <= 0:
        raise ValueError("Set a target above zero.")
    if target_date:
        try:
            date.fromisoformat(target_date)
        except ValueError as exc:
            raise ValueError("Pick a valid target date.") from exc
    now = db.utcnow()
    return db.execute(
        """INSERT INTO goals(household_id, name, target_cents, saved_cents, target_date, account_id,
               notes, status, created_at, updated_at) VALUES (?,?,?,0,?,?,?,'active',?,?)""",
        (household_id, name[:60], int(target_cents), target_date, account_id, notes, now, now),
    )


def update(household_id: int, goal_id: int, **fields) -> None:
    allowed = {"name", "target_cents", "saved_cents", "target_date", "account_id", "notes", "status"}
    sets, args = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            args.append(value)
    if not sets:
        return
    sets.append("updated_at = ?")
    args.append(db.utcnow())
    args.extend([household_id, goal_id])
    db.execute(f"UPDATE goals SET {', '.join(sets)} WHERE household_id = ? AND id = ?", tuple(args))
    row = db.query_one("SELECT * FROM goals WHERE id = ?", (goal_id,))
    if row and row["saved_cents"] >= row["target_cents"] and row["status"] == "active":
        db.execute("UPDATE goals SET status = 'achieved', achieved_at = ? WHERE id = ?", (db.utcnow(), goal_id))


def contribute(household_id: int, goal_id: int, cents: int, occurred_on: str | None = None,
               note: str | None = None) -> dict:
    goal = db.query_one("SELECT * FROM goals WHERE household_id = ? AND id = ?", (household_id, goal_id))
    if goal is None:
        raise ValueError("Goal not found.")
    minutes = int(cents)
    if minutes == 0:
        raise ValueError("Enter an amount.")
    day = occurred_on or date.today().isoformat()
    now = db.utcnow()
    db.execute(
        """INSERT INTO goal_contributions(household_id, goal_id, amount_cents, occurred_on, note, created_at)
           VALUES (?,?,?,?,?,?)""",
        (household_id, goal_id, minutes, day, note, now),
    )
    new_saved = max(0, int(goal["saved_cents"]) + minutes)
    status = "achieved" if new_saved >= goal["target_cents"] else goal["status"]
    db.execute(
        "UPDATE goals SET saved_cents = ?, status = ?, achieved_at = ?, updated_at = ? WHERE id = ?",
        (new_saved, status, now if status == "achieved" else goal["achieved_at"], now, goal_id),
    )
    return {"saved_cents": new_saved, "achieved": status == "achieved"}


def contributions(household_id: int, goal_id: int) -> list[dict]:
    return [dict(r) for r in db.query_all(
        """SELECT * FROM goal_contributions WHERE household_id = ? AND goal_id = ?
           ORDER BY occurred_on DESC, id DESC""", (household_id, goal_id))]


def delete(household_id: int, goal_id: int) -> None:
    db.execute("DELETE FROM goals WHERE household_id = ? AND id = ?", (household_id, goal_id))
