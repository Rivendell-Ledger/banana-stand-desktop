"""Monthly budgets: envelopes, rollover, copy, auto-budget, safe-to-spend."""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any

from . import categories, db, money, networth, recurring

LIABILITY_TYPES = networth.LIABILITY_TYPES


# ---------------------------------------------------------------- months
def month_key(value: date | str | None = None) -> str:
    if isinstance(value, str) and len(value) == 7:
        return value
    return (value if isinstance(value, date) else date.today()).strftime("%Y-%m")


def parse_month(value: str) -> tuple[int, int]:
    try:
        year, month = value.split("-")
        return int(year), int(month)
    except (ValueError, AttributeError):
        today = date.today()
        return today.year, today.month


def shift_month(value: str, delta: int) -> str:
    year, month = parse_month(value)
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_bounds(value: str) -> tuple[str, str]:
    year, month = parse_month(value)
    first = date(year, month, 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def month_label(value: str) -> str:
    year, month = parse_month(value)
    return date(year, month, 1).strftime("%B %Y")


def month_grid(value: str) -> list[list[dict | None]]:
    """Calendar cells for a month, Sunday-first, padded to whole weeks."""
    year, month = parse_month(value)
    last_day = calendar.monthrange(year, month)[1]
    first = date(year, month, 1)
    cells: list[dict | None] = [None] * ((first.weekday() + 1) % 7)
    cells += [{"day": d, "iso": date(year, month, d).isoformat()} for d in range(1, last_day + 1)]
    while len(cells) % 7:
        cells.append(None)
    return [cells[i:i + 7] for i in range(0, len(cells), 7)]


def income_history(household_id: int, month: str, months: int = 3) -> tuple[int, int]:
    """Average actual income over the months before `month`.

    Returns (average cents, months counted). Months with no activity at all are
    skipped so a household with one month of history is not divided by three.
    """
    total = 0
    used = 0
    for back in range(1, months + 1):
        start, end = month_bounds(shift_month(month, -back))
        row = db.query_one(
            """SELECT COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END), 0) AS income,
                      COUNT(*) AS n
               FROM transactions
               WHERE household_id = ? AND posted_on BETWEEN ? AND ?
                 AND deleted_at IS NULL AND is_pending = 0 AND is_transfer = 0 AND excluded = 0""",
            (household_id, start, end),
        )
        if row and row["n"]:
            total += int(row["income"])
            used += 1
    return (round(total / used) if used else 0, used)


# ---------------------------------------------------------------- spend
def spent_map(household_id: int) -> dict[tuple[str, int], int]:
    """Positive cents spent per (month, category), splits included."""
    spent: dict[tuple[str, int], int] = {}
    base = """SELECT substr(t.posted_on, 1, 7) AS m, %s AS category_id, SUM(%s) AS total
              FROM %s
              WHERE %s AND t.deleted_at IS NULL AND t.is_pending = 0 AND t.excluded = 0
                AND t.is_transfer = 0 AND %s IS NOT NULL
              GROUP BY m, %s"""
    own = db.query_all(
        base % ("t.category_id", "t.amount_cents", "transactions t",
                "t.household_id = ?", "t.category_id", "t.category_id"),
        (household_id,),
    )
    splits = db.query_all(
        base % ("s.category_id", "s.amount_cents", "transaction_splits s JOIN transactions t ON t.id = s.transaction_id",
                "t.household_id = ?", "s.category_id", "s.category_id"),
        (household_id,),
    )
    for row in list(own) + list(splits):
        if row["total"] is None:
            continue
        key = (row["m"], row["category_id"])
        # Expenses are negative locally; present them as positive spend.
        spent[key] = spent.get(key, 0) + max(0, -int(row["total"]))
    return spent


def spent_in(spent: dict[tuple[str, int], int], month: str, category_id: int) -> int:
    return spent.get((month, category_id), 0)


# ---------------------------------------------------------------- rows
def _budget_rows(household_id: int) -> list[dict]:
    rows = db.query_all("SELECT * FROM budgets WHERE household_id = ?", (household_id,))
    return [dict(r) for r in rows]


def grid(household_id: int, month: str, spent: dict | None = None) -> dict[str, Any]:
    month = month_key(month)
    cats = categories.list_categories(household_id)
    has_children = {c["parent_id"] for c in cats if c["parent_id"]}
    budgets = {(r["month"], r["category_id"]): r for r in _budget_rows(household_id)}
    spent = spent if spent is not None else spent_map(household_id)

    rows: list[dict] = []
    for c in cats:
        if c["kind"] != "expense":
            continue
        # A category the household removed from the Budget tab stays in the ledger's
        # filters and reports, but never takes a row here.
        if c.get("hide_from_budget"):
            continue
        row = budgets.get((month, c["id"]))
        # Budget leaves by default; show a parent only if it already carries a budget row.
        if c["id"] in has_children and row is None:
            continue
        # System buckets (Uncategorized) only appear once someone budgets them.
        if c.get("is_system") and row is None:
            continue
        budget_cents = row["budget_cents"] if row else 0
        rollover = bool(row["rollover"]) if row else False
        carry = carry_in(household_id, month, c["id"], budgets, spent) if row else 0
        used = spent_in(spent, month, c["id"])
        available = budget_cents + carry
        rows.append({
            "id": c["id"],
            "name": c["name"],
            "parent_id": c["parent_id"],
            "budget_cents": budget_cents,
            "rollover": rollover,
            "carry_cents": carry,
            "available_cents": available,
            "spent_cents": used,
            "remaining_cents": available - used,
            "has_row": row is not None,
            "pct": min(999, round(used / available * 100)) if available > 0 else None,
        })

    # Every expense category, parents included: a transaction filed straight on a parent
    # is real spend, and skipping parents made the month total disagree with the ledger.
    all_spent = sum(
        spent_in(spent, month, c["id"]) for c in cats if c["kind"] == "expense"
    )
    budgeted = sum(r["budget_cents"] for r in rows)
    carry_total = sum(r["carry_cents"] for r in rows)
    spent_total = sum(r["spent_cents"] for r in rows)
    plan = db.query_one(
        "SELECT expected_income_cents FROM monthly_plans WHERE household_id = ? AND month = ?",
        (household_id, month),
    )
    expected = plan["expected_income_cents"] if plan else 0
    auto_income = False
    income_note = ""
    if not expected:
        hist, months_used = income_history(household_id, month)
        if hist:
            expected = hist
            auto_income = True
            income_note = (f"Average of the last {months_used} month"
                           f"{'' if months_used == 1 else 's'} of income")
        else:
            expected = recurring.income_for_month(household_id, month)
            auto_income = expected > 0
            if auto_income:
                income_note = "From Bills & Income"
    return {
        "month": month,
        "label": month_label(month),
        "prev": shift_month(month, -1),
        "next": shift_month(month, 1),
        "rows": rows,
        "budgeted_cents": budgeted,
        "carry_cents": carry_total,
        "spent_cents": spent_total,
        "all_spent_cents": all_spent,
        "remaining_cents": budgeted + carry_total - spent_total,
        "expected_income_cents": expected,
        "auto_income": auto_income,
        "income_note": income_note,
        "left_to_budget_cents": (expected - budgeted) if expected else None,
        "is_current": month == month_key(),
    }


def carry_in(household_id: int, month: str, category_id: int,
             budgets: dict | None = None, spent: dict | None = None, limit: int = 36) -> int:
    """Unspent (or overspent) balance carried into `month`. Only when rollover is on."""
    budgets = budgets if budgets is not None else {
        (r["month"], r["category_id"]): r for r in _budget_rows(household_id)
    }
    spent = spent if spent is not None else spent_map(household_id)
    current = budgets.get((month, category_id))
    if current is None or not current["rollover"]:
        return 0
    total = 0
    cursor = shift_month(month, -1)
    for _ in range(limit):
        row = budgets.get((cursor, category_id))
        if row is None or not row["rollover"]:
            break
        total += row["budget_cents"] - spent_in(spent, cursor, category_id)
        cursor = shift_month(cursor, -1)
    return total


# ---------------------------------------------------------------- writes
def set_budget(household_id: int, month: str, category_id: int, cents: int,
               rollover: bool | None = None) -> None:
    now = db.utcnow()
    existing = db.query_one(
        "SELECT * FROM budgets WHERE household_id = ? AND month = ? AND category_id = ?",
        (household_id, month, category_id),
    )
    cents = max(0, int(cents))
    if existing:
        db.execute(
            "UPDATE budgets SET budget_cents = ?, rollover = ?, updated_at = ? WHERE id = ?",
            (cents, int(rollover) if rollover is not None else existing["rollover"], now, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO budgets(household_id, month, category_id, budget_cents, rollover, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (household_id, month, category_id, cents, 1 if rollover else 0, now, now),
        )


def set_income(household_id: int, month: str, cents: int) -> None:
    now = db.utcnow()
    db.execute(
        "INSERT INTO monthly_plans(household_id, month, expected_income_cents, created_at, updated_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(household_id, month) DO UPDATE SET"
        " expected_income_cents = excluded.expected_income_cents, updated_at = excluded.updated_at",
        (household_id, month, max(0, int(cents)), now, now),
    )


def copy_month(household_id: int, source: str, target: str, only_missing: bool = True) -> int:
    existing = {
        r["category_id"] for r in db.query_all(
            "SELECT category_id FROM budgets WHERE household_id = ? AND month = ?", (household_id, target)
        )
    }
    source_rows = db.query_all(
        "SELECT * FROM budgets WHERE household_id = ? AND month = ?", (household_id, source)
    )
    now = db.utcnow()
    copied = 0
    for row in source_rows:
        if only_missing and row["category_id"] in existing:
            continue
        db.execute(
            "INSERT INTO budgets(household_id, month, category_id, budget_cents, rollover, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(household_id, month, category_id) DO UPDATE SET"
            " budget_cents = excluded.budget_cents, rollover = excluded.rollover, updated_at = excluded.updated_at",
            (household_id, target, row["category_id"], row["budget_cents"], row["rollover"], now, now),
        )
        copied += 1
    return copied


def _coverage(household_id: int, month: str, lookback: int) -> tuple[list[str], list[str]]:
    """Split the lookback window into whole months and the partly-covered first one.

    A month before the ledger starts reads as zero spending, which quietly divides a
    fixed bill by one month too many. The first month is only trustworthy for a
    category that actually shows a charge in it — otherwise it is excluded.
    """
    month = month_key(month)
    first = db.query_one(
        "SELECT MIN(posted_on) AS d FROM transactions WHERE household_id = ? AND deleted_at IS NULL",
        (household_id,))["d"]
    today = db.utcnow()[:10]
    whole, partial = [], []
    for i in range(1, lookback + 1):
        candidate = shift_month(month, -i)
        _, end = month_bounds(candidate)
        if end >= today:
            continue                      # still in progress
        if not first or candidate > first[:7]:
            whole.append(candidate)
        elif candidate == first[:7] and first[8:10] == "01":
            whole.append(candidate)
        else:
            partial.append(candidate)     # the ledger starts inside this month
    return whole, partial


def usable_months(household_id: int, month: str, lookback: int = 3) -> list[str]:
    return _coverage(household_id, month, lookback)[0]


def auto_budget(household_id: int, month: str, lookback: int = 3) -> int:
    """Set this month's budget from the average spend of the previous N whole months."""
    month = month_key(month)
    spent = spent_map(household_id)
    cats = [c for c in categories.list_categories(household_id) if c["kind"] == "expense"]
    has_children = {c["parent_id"] for c in cats if c["parent_id"]}
    whole, partial = _coverage(household_id, month, lookback)
    if not whole and not partial:
        return 0
    applied = 0
    for c in cats:
        if c["id"] in has_children:
            continue
        # A partial month counts only when this category has data in it.
        window = sorted(whole + [m for m in partial if spent_in(spent, m, c["id"])])
        totals = [spent_in(spent, m, c["id"]) for m in window]
        if not any(totals):
            continue
        average = int(round(sum(totals) / len(window)))
        if average <= 0:
            continue
        set_budget(household_id, month, c["id"], average)
        applied += 1
    return applied


# ---------------------------------------------------------------- safe to spend
def safe_to_spend(household_id: int, month: str | None = None,
                  plan_grid: dict | None = None) -> dict:
    month = month_key(month)
    cash = db.query_one(
        """SELECT COALESCE(SUM(current_balance_cents), 0) AS total FROM accounts
           WHERE household_id = ? AND archived_at IS NULL AND include_in_budget = 1""",
        (household_id,),
    )["total"]
    household = db.query_one("SELECT buffer_cents FROM households WHERE id = ?", (household_id,))
    buffer = household["buffer_cents"] if household else 0
    g = plan_grid if plan_grid is not None else grid(household_id, month)
    spoken_for = sum(max(0, r["remaining_cents"]) for r in g["rows"])
    from . import recurring
    bills_uncovered, bill_rows = recurring.month_uncovered(household_id, month, plan_grid=g)
    available = cash - buffer - spoken_for - bills_uncovered
    return {
        "cash_cents": cash,
        "buffer_cents": buffer,
        "budget_remaining_cents": spoken_for,
        "bills_uncovered_cents": bills_uncovered,
        "bill_rows": bill_rows,
        "safe_cents": max(0, available),
        "month": month,
        "budgeted_cents": g["budgeted_cents"],
        "spent_cents": g["spent_cents"],
        "has_budgets": any(r["has_row"] for r in g["rows"]),
    }


# ---------------------------------------------------------------- copy previous month
COPY_PREVIOUS_KEY = "budget_copy_previous"


def copy_previous_enabled(household_id: int) -> bool:
    """Whether a fresh month starts from last month's plan. On unless switched off."""
    from .alerts import get_setting
    value = get_setting(household_id, COPY_PREVIOUS_KEY)
    return True if value is None else bool(value)


def set_copy_previous(household_id: int, enabled: bool) -> None:
    from .alerts import set_setting
    set_setting(household_id, COPY_PREVIOUS_KEY, bool(enabled))


def fill_from_previous(household_id: int, month: str) -> int:
    """Seed a month that has no budget rows of its own from the month before.

    Only an empty month is seeded: once it holds rows, a category the household
    zeroed on purpose stays zeroed instead of being resurrected on the next view.
    """
    month = month_key(month)
    if not copy_previous_enabled(household_id):
        return 0
    if db.query_one("SELECT COUNT(*) AS n FROM budgets WHERE household_id = ? AND month = ?",
                    (household_id, month))["n"]:
        return 0
    return copy_month(household_id, shift_month(month, -1), month)


# ---------------------------------------------------------------- home curve
def month_spend_curve(household_id: int, today: date | None = None) -> dict[str, Any]:
    """Cumulative daily spend for this month so far against the whole previous month.

    Points share one x scale (day of month) and one dollar scale, so the two lines
    are directly comparable: the solid line simply stops at today.
    """
    today = today or date.today()
    this_month = month_key(today)
    last_month = shift_month(this_month, -1)
    last_year, last_mon = parse_month(last_month)
    last_days = calendar.monthrange(last_year, last_mon)[1]
    axis_days = max(last_days, calendar.monthrange(today.year, today.month)[1])

    def daily(start: str, end: str) -> dict[int, int]:
        rows = db.query_all(
            """SELECT posted_on, SUM(amount_cents) AS total FROM transactions
               WHERE household_id = ? AND posted_on BETWEEN ? AND ? AND amount_cents < 0
                 AND deleted_at IS NULL AND is_pending = 0 AND is_transfer = 0 AND excluded = 0
               GROUP BY posted_on""",
            (household_id, start, end))
        return {int(row["posted_on"][8:10]): abs(int(row["total"])) for row in rows}

    last_daily = daily(*month_bounds(last_month))
    this_daily = daily(month_bounds(this_month)[0], today.isoformat())

    def cumulative(source: dict[int, int], through: int) -> list[int]:
        out, running = [], 0
        for day in range(1, through + 1):
            running += source.get(day, 0)
            out.append(running)
        return out

    last_cum = cumulative(last_daily, last_days)
    this_cum = cumulative(this_daily, today.day)
    peak = money.nice_top(max([last_cum[-1] if last_cum else 0,
                               this_cum[-1] if this_cum else 0, 1]))

    def points(values: list[int]) -> str:
        width, height, pad = 320, 120, 6
        step = (width - 2 * pad) / max(1, axis_days - 1)
        return " ".join(
            f"{round(pad + i * step, 1)},"
            f"{round(height - pad - (value / peak) * (height - 2 * pad), 1)}"
            for i, value in enumerate(values))

    # The scrub reads dollars off the same scale the lines were drawn on; this month's
    # line stops at today, so its tail is null rather than a repeated last value.
    this_scrub = [round(v / 100, 2) for v in this_cum] + [None] * (axis_days - len(this_cum))

    return {
        "days": axis_days,
        "today": today.day,
        "last": {"points": points(last_cum),
                 "total_cents": last_cum[-1] if last_cum else 0},
        "this": {"points": points(this_cum),
                 "total_cents": this_cum[-1] if this_cum else 0},
        "hi": round(peak / 100, 2),
        "labels": [f"Day {d}" for d in range(1, axis_days + 1)],
        "series": [
            {"key": "this", "label": calendar.month_abbr[today.month], "values": this_scrub},
            {"key": "last", "label": calendar.month_abbr[last_mon],
             "values": [round(v / 100, 2) for v in last_cum]},
        ],
        "peak_cents": peak,
        "ticks": [{"pct": round(i / 3 * 100, 1), "label": money.axis_label(peak * i // 3)}
                  for i in range(4)],
        "last_label": month_label(last_month),
        "this_label": month_label(this_month),
        "last_short": calendar.month_abbr[last_mon],
        "this_short": calendar.month_abbr[today.month],
    }
