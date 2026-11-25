"""90-day cash-flow projection from confirmed recurring bills and income."""
from __future__ import annotations

from datetime import date, timedelta

from . import db, recurring


def cash_on_hand(household_id: int) -> int:
    row = db.query_one(
        """SELECT COALESCE(SUM(current_balance_cents), 0) AS total FROM accounts
           WHERE household_id = ? AND archived_at IS NULL AND include_in_budget = 1""",
        (household_id,),
    )
    return int(row["total"] if row else 0)


def buffer_cents(household_id: int) -> int:
    row = db.query_one("SELECT buffer_cents FROM households WHERE id = ?", (household_id,))
    return int(row["buffer_cents"] if row else 0)


def events(household_id: int, start: date, end: date) -> list[dict]:
    out: list[dict] = []
    for row in recurring.active(household_id):
        sign = 1 if row["kind"] == "income" else -1
        for due in recurring.occurrences(row, start, end):
            out.append({
                "date": due.isoformat(),
                "name": row["name"],
                "kind": row["kind"],
                "cadence": row["cadence"],
                "amount_cents": sign * abs(row["amount_cents"]),
                "recurring_id": row["id"],
            })
    out.sort(key=lambda e: (e["date"], -abs(e["amount_cents"])))
    return out


def forecast(household_id: int, days: int = 90, today: date | None = None) -> dict:
    start = today or date.today()
    days = max(7, min(180, int(days)))
    end = start + timedelta(days=days)
    opening = cash_on_hand(household_id)
    buffer = buffer_cents(household_id)
    evs = events(household_id, start, end)

    by_day: dict[str, list[dict]] = {}
    for e in evs:
        by_day.setdefault(e["date"], []).append(e)

    balance = opening
    series: list[dict] = []
    for offset in range(days + 1):
        day = (start + timedelta(days=offset)).isoformat()
        for e in by_day.get(day, []):
            balance += e["amount_cents"]
        series.append({"date": day, "balance_cents": balance})

    low = min(series, key=lambda p: p["balance_cents"]) if series else {"date": start.isoformat(), "balance_cents": opening}
    bills_total = sum(abs(e["amount_cents"]) for e in evs if e["amount_cents"] < 0)
    income_total = sum(e["amount_cents"] for e in evs if e["amount_cents"] > 0)

    # Group by month for display.
    months: list[dict] = []
    index: dict[str, dict] = {}
    running = opening
    for e in evs:
        month = e["date"][:7]
        if month not in index:
            index[month] = {"month": month, "label": date.fromisoformat(month + "-01").strftime("%B %Y"),
                            "events": [], "net_cents": 0}
            months.append(index[month])
        running += e["amount_cents"]
        index[month]["events"].append({**e, "balance_cents": running})
        index[month]["net_cents"] += e["amount_cents"]

    balances = [p["balance_cents"] for p in series] or [opening]
    lo = min(min(balances), buffer)
    hi = max(max(balances), buffer)
    span = max(1, hi - lo)
    n = max(1, len(series) - 1)
    points = " ".join(
        f"{i / n * 300:.1f},{80 - (b - lo) / span * 80:.1f}" for i, b in enumerate(balances)
    )
    chart = {
        "points": points,
        "buffer_y": round(80 - (buffer - lo) / span * 80, 1),
        "area": f"0,80 {points} 300,80",
    }

    return {
        "chart": chart,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        "opening_cents": opening,
        "buffer_cents": buffer,
        "events": evs,
        "months": months,
        "series": series,
        "low": low,
        "bills_total_cents": bills_total,
        "income_total_cents": income_total,
        "closing_cents": series[-1]["balance_cents"] if series else opening,
        "breach": low["balance_cents"] < buffer,
        "has_recurring": bool(recurring.active(household_id)),
    }


def next_bill(household_id: int, today: date | None = None, horizon: int = 45) -> dict | None:
    start = today or date.today()
    for e in events(household_id, start, start + timedelta(days=horizon)):
        if e["amount_cents"] < 0:
            days_out = (date.fromisoformat(e["date"]) - start).days
            return {**e, "days_out": days_out}
    return None
