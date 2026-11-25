"""Reports: monthly summaries, category deltas, top merchants, multi-month trend."""
from __future__ import annotations

import calendar
from datetime import date

from . import budgets, categories, db, money


def _month_bounds(month: str) -> tuple[str, str]:
    year, mon = (int(x) for x in month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def _category_names(household_id: int) -> dict[int, dict]:
    return {c["id"]: c for c in categories.list_categories(household_id)}


def _spend_by_category(household_id: int, start: str, end: str) -> dict[int, int]:
    """Positive spend per category for a date range, splits attributed to their line."""
    out: dict[int, int] = {}
    rows = db.query_all(
        """SELECT category_id, SUM(amount_cents) AS total FROM transactions
           WHERE household_id = ? AND posted_on BETWEEN ? AND ?
             AND deleted_at IS NULL AND is_pending = 0 AND is_transfer = 0 AND excluded = 0
             AND category_id IS NOT NULL AND amount_cents < 0
           GROUP BY category_id""",
        (household_id, start, end),
    )
    for row in rows:
        out[row["category_id"]] = out.get(row["category_id"], 0) + abs(int(row["total"]))
    split_rows = db.query_all(
        """SELECT s.category_id, SUM(s.amount_cents) AS total
           FROM transaction_splits s JOIN transactions t ON t.id = s.transaction_id
           WHERE t.household_id = ? AND t.posted_on BETWEEN ? AND ?
             AND t.deleted_at IS NULL AND t.is_pending = 0 AND t.is_transfer = 0 AND t.excluded = 0
             AND s.amount_cents < 0
           GROUP BY s.category_id""",
        (household_id, start, end),
    )
    for row in split_rows:
        out[row["category_id"]] = out.get(row["category_id"], 0) + abs(int(row["total"]))
    return out


def valid_month(value: str | None) -> bool:
    try:
        year, mon = str(value).split("-")
        return 2000 <= int(year) <= 2100 and 1 <= int(mon) <= 12
    except (ValueError, TypeError):
        return False


PIE_SLICES = 8


def breakdown_pie(rows: list[dict]) -> list[dict]:
    """Pie segments for the breakdown card: biggest slices, long tail rolled into Other."""
    total = sum(r["spent_cents"] for r in rows) or 1
    slices = [dict(r) for r in rows[:PIE_SLICES]]
    tail = rows[PIE_SLICES:]
    if tail:
        spent = sum(r["spent_cents"] for r in tail)
        before = sum(r["prev_cents"] for r in tail)
        slices.append({
            "name": "Other",
            "other": True,
            "spent_cents": spent,
            "prev_cents": before,
            "delta_cents": spent - before,
            "delta_pct": ((spent - before) / before * 100) if before else None,
            "share_pct": spent / total * 100,
            "cls": 8,
        })
    running = 0.0
    out = []
    for index, row in enumerate(slices):
        row.setdefault("cls", index)
        row["share_pct"] = row["spent_cents"] / total * 100
        dash = round(row["share_pct"], 3)
        row["dash"] = dash
        row["rest"] = round(100 - dash, 3)
        row["offset"] = round(25 - running, 3)
        running += row["share_pct"]
        out.append(row)
    return out


def month_summary(household_id: int, month: str) -> dict:
    if not valid_month(month):
        month = budgets.month_key()
    start, end = _month_bounds(month)
    prev = budgets.shift_month(month, -1)
    prev_start, prev_end = _month_bounds(prev)
    names = _category_names(household_id)

    income = db.query_one(
        """SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transactions
           WHERE household_id = ? AND posted_on BETWEEN ? AND ? AND amount_cents > 0
             AND deleted_at IS NULL AND is_pending = 0 AND is_transfer = 0 AND excluded = 0""",
        (household_id, start, end),
    )["total"]
    expense = db.query_one(
        """SELECT COALESCE(SUM(amount_cents), 0) AS total FROM transactions
           WHERE household_id = ? AND posted_on BETWEEN ? AND ? AND amount_cents < 0
             AND deleted_at IS NULL AND is_pending = 0 AND is_transfer = 0 AND excluded = 0""",
        (household_id, start, end),
    )["total"]

    current = _spend_by_category(household_id, start, end)
    previous = _spend_by_category(household_id, prev_start, prev_end)
    rows = []
    for cat_id, spent in current.items():
        cat = names.get(cat_id, {"name": "Unknown", "kind": "expense"})
        if cat.get("kind") == "income":
            continue
        before = previous.get(cat_id, 0)
        rows.append({
            "id": cat_id,
            "name": cat["name"],
            "spent_cents": spent,
            "prev_cents": before,
            "delta_cents": spent - before,
            "delta_pct": ((spent - before) / before * 100) if before else None,
        })
    rows.sort(key=lambda r: -r["spent_cents"])
    total_spent = sum(r["spent_cents"] for r in rows) or 1
    for row in rows:
        row["share_pct"] = row["spent_cents"] / total_spent * 100
    pie = breakdown_pie(rows)
    other_rows = [dict(r) for r in rows[PIE_SLICES:]]

    merchants = db.query_all(
        """SELECT merchant, COUNT(*) AS n, SUM(amount_cents) AS total FROM transactions
           WHERE household_id = ? AND posted_on BETWEEN ? AND ? AND amount_cents < 0
             AND deleted_at IS NULL AND is_pending = 0 AND is_transfer = 0 AND excluded = 0
           GROUP BY lower(merchant)
           ORDER BY total ASC LIMIT 10""",
        (household_id, start, end),
    )

    return {
        "month": month,
        "label": budgets.month_label(month),
        "prev": prev,
        "next": budgets.shift_month(month, 1),
        "start": start,
        "end": end,
        "income_cents": int(income),
        "expense_cents": abs(int(expense)),
        "net_cents": int(income) + int(expense),
        "rows": rows,
        "pie": pie,
        "other_rows": other_rows,
        "pie_total_cents": sum(r["spent_cents"] for r in rows),
        "merchants": [{"name": m["merchant"], "count": m["n"], "total_cents": abs(int(m["total"]))} for m in merchants],
        "txn_count": db.query_one(
            """SELECT COUNT(*) AS n FROM transactions WHERE household_id = ? AND posted_on BETWEEN ? AND ?
               AND deleted_at IS NULL AND is_pending = 0""",
            (household_id, start, end),
        )["n"],
    }


def trend(household_id: int, months: int = 12) -> list[dict]:
    current = budgets.month_key()
    keys = [budgets.shift_month(current, -i) for i in range(months - 1, -1, -1)]
    start = keys[0] + "-01"
    end = _month_bounds(keys[-1])[1]
    rows = db.query_all(
        """SELECT substr(posted_on, 1, 7) AS m,
                  SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) AS income,
                  SUM(CASE WHEN amount_cents < 0 THEN amount_cents ELSE 0 END) AS expense
           FROM transactions
           WHERE household_id = ? AND posted_on BETWEEN ? AND ?
             AND deleted_at IS NULL AND is_pending = 0 AND is_transfer = 0 AND excluded = 0
           GROUP BY m""",
        (household_id, start, end),
    )
    by_month = {r["m"]: r for r in rows}
    out = []
    for key in keys:
        row = by_month.get(key)
        income = int(row["income"]) if row else 0
        expense = abs(int(row["expense"])) if row else 0
        out.append({
            "month": key,
            "label": date.fromisoformat(key + "-01").strftime("%b"),
            "full_label": budgets.month_label(key),
            "income_cents": income,
            "expense_cents": expense,
            "net_cents": income - expense,
        })
    return out


def export_rows(household_id: int, start: str, end: str) -> list[dict]:
    return [dict(r) for r in db.query_all(
        """SELECT t.posted_on, t.merchant, t.amount_cents, c.name AS category, a.name AS account, t.notes
           FROM transactions t
           LEFT JOIN categories c ON c.id = t.category_id
           LEFT JOIN accounts a ON a.id = t.account_id
           WHERE t.household_id = ? AND t.posted_on BETWEEN ? AND ? AND t.deleted_at IS NULL
           ORDER BY t.posted_on, t.id""",
        (household_id, start, end),
    )]


def trend_scale(max_cents: int, ticks: int = 3) -> dict:
    """Y-axis for the 12-month bars: a friendly top plus evenly spaced gridlines.

    Returns the top in cents (the bars divide by it) and tick labels from 0 upward.
    """
    step = money.nice_step(max(1, int(max_cents)) / ticks)
    return {
        "top_cents": step * ticks,
        "ticks": [
            {"pct": round(i / ticks * 100, 1), "label": money.axis_label(i * step)}
            for i in range(ticks + 1)
        ],
    }
