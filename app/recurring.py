"""Recurring bills and income: detection from history, scheduling, next occurrences."""
from __future__ import annotations

import calendar
import statistics
from datetime import date, timedelta

from . import categories, db, rules

CADENCES: dict[str, tuple[str, int]] = {
    "weekly": ("Weekly", 7),
    "biweekly": ("Every 2 weeks", 14),
    "monthly": ("Monthly", 30),
    "quarterly": ("Quarterly", 91),
    "semiannual": ("Twice a year", 182),
    "annual": ("Yearly", 365),
}

BANDS = [
    ("weekly", 6, 8),
    ("biweekly", 12, 16),
    ("monthly", 26, 33),
    ("quarterly", 80, 100),
    ("semiannual", 165, 200),
    ("annual", 340, 395),
]

MIN_TXNS = 3
MIN_SPAN_DAYS = 21
MIN_AMOUNT_CENTS = 100       # under a dollar is interest dust, not a bill
AMOUNT_TOLERANCE = 0.30      # max spread around the median, as a fraction
INTERVAL_TOLERANCE = 0.20    # max deviation from the median interval


def classify(interval: float) -> str | None:
    for name, low, high in BANDS:
        if low <= interval <= high:
            return name
    return None


def _month_add(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def roll_forward(due: date, cadence: str, anchor_day: int | None, until: date) -> date:
    """Move a due date forward until it is on or after `until`."""
    guard = 0
    while due < until and guard < 500:
        guard += 1
        if cadence == "monthly":
            due = _month_add(due, 1)
            if anchor_day:
                due = due.replace(day=min(anchor_day, calendar.monthrange(due.year, due.month)[1]))
        elif cadence == "quarterly":
            due = _month_add(due, 3)
        elif cadence == "semiannual":
            due = _month_add(due, 6)
        elif cadence == "annual":
            due = _month_add(due, 12)
        else:
            due = due + timedelta(days=CADENCES.get(cadence, ("", 30))[1])
    return due


def occurrences(row: dict, start: date, end: date) -> list[date]:
    """Every due date for a recurring row inside [start, end]."""
    cadence = row["cadence"] or "monthly"
    due = date.fromisoformat(row["next_due_on"])
    due = roll_forward(due, cadence, row["anchor_day"], start)
    out: list[date] = []
    guard = 0
    while due <= end and guard < 400:
        guard += 1
        out.append(due)
        if cadence == "monthly":
            due = _month_add(due, 1)
            if row["anchor_day"]:
                due = due.replace(day=min(row["anchor_day"], calendar.monthrange(due.year, due.month)[1]))
        elif cadence == "quarterly":
            due = _month_add(due, 3)
        elif cadence == "semiannual":
            due = _month_add(due, 6)
        elif cadence == "annual":
            due = _month_add(due, 12)
        else:
            due = due + timedelta(days=CADENCES.get(cadence, ("", 30))[1])
    return out


def _roll_back(due: date, cadence: str, anchor_day: int | None) -> date:
    """One cadence earlier than `due`."""
    if cadence in ("monthly", "quarterly", "semiannual", "annual"):
        months = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}[cadence]
        return _month_add(due, -months)
    return due - timedelta(days=CADENCES.get(cadence, ("", 30))[1])


LOOKBACK_DAYS = 366


def occurrences_between(row: dict, start: date, end: date) -> list[date]:
    """Every occurrence inside [start, end], including ones already past.

    `next_due_on` points forward, but it can land *inside* the window (a weekly
    paycheck whose next date is the 27th still paid on the 6th, 13th and 20th).
    Walk back to the first occurrence at or before `start`, not merely until
    `due <= end`, then expand forward again. The walk stops a year back — a
    schedule is not projected into the distant past.
    """
    floor = date.today() - timedelta(days=LOOKBACK_DAYS)
    if end < floor:
        return []
    cadence = row.get("cadence") or "monthly"
    due = date.fromisoformat(row["next_due_on"])
    guard = 0
    while due > start and guard < 400 and due > floor:
        guard += 1
        prior = _roll_back(due, cadence, row.get("anchor_day"))
        if prior == due:
            break
        due = prior
    shifted = dict(row)
    shifted["next_due_on"] = due.isoformat()
    return occurrences(shifted, start, end)


# ---------------------------------------------------------------- read
def list_recurring(household_id: int, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM recurring WHERE household_id = ?"
    args: list = [household_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY kind DESC, next_due_on ASC, name ASC"
    rows = [dict(r) for r in db.query_all(sql, tuple(args))]
    for row in rows:
        row["due_on"] = effective_due(row)
    return rows


def get(household_id: int, recurring_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM recurring WHERE household_id = ? AND id = ?",
                       (household_id, recurring_id))
    if row is None:
        return None
    out = dict(row)
    out["due_on"] = effective_due(out)
    return out


def effective_due(row: dict, today: date | None = None) -> str:
    """The next real occurrence on or after today."""
    if not row.get("next_due_on"):
        return ""
    due = date.fromisoformat(row["next_due_on"])
    return roll_forward(due, row.get("cadence") or "monthly", row.get("anchor_day"), today or date.today()).isoformat()


def income_for_month(household_id: int, month: str) -> int:
    """Expected income for a month from the Bills & Income schedule."""
    year, mon = (int(x) for x in month.split("-"))
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    total = 0
    for row in list_recurring(household_id):
        if row["kind"] != "income" or row["status"] == "ended":
            continue
        total += row["amount_cents"] * len(occurrences_between(row, first, last))
    return total


def active(household_id: int) -> list[dict]:
    return [r for r in list_recurring(household_id) if r["status"] == "confirmed"]


# ---------------------------------------------------------------- detect
# One brand, several descriptors on the statement ("GROK XAI", "XAI") — group them
# together or a 2-charge subscription never reaches the detector.
BRAND_ALIASES = {
    "grok": "xai", "grok xai": "xai", "x ai": "xai",
    "apple com bill": "apple", "apple itunes": "apple", "itunes": "apple",
}


def _merchant_groups(household_id: int) -> dict[str, list[dict]]:
    rows = db.query_all(
        """SELECT id, posted_on, amount_cents, merchant, raw_merchant, account_id, category_id
           FROM transactions
           WHERE household_id = ? AND deleted_at IS NULL AND is_pending = 0
             AND is_transfer = 0 AND excluded = 0
           ORDER BY posted_on""",
        (household_id,),
    )
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = rules.normalize_merchant(row["raw_merchant"] or row["merchant"] or "")
        key = BRAND_ALIASES.get(key, key)
        if not key:
            continue
        groups.setdefault(key, []).append(dict(row))
    return groups


def _split_series(rows: list[dict]) -> list[tuple[int, list[dict]]]:
    """Split a merchant billed the same number of times on every date into parallel series.

    Two identical deposits on the same day are two tracks, not one — but only when that
    repeat count is constant. A merchant with an occasional second charge stays one series.
    """
    accounts: dict[int | None, list[dict]] = {}
    for row in rows:
        accounts.setdefault(row["account_id"], []).append(row)
    if len(accounts) > 1:
        # Same merchant, different accounts: one track each, in a stable order.
        order = sorted(accounts, key=lambda a: (a is None, a))
        return [(i, accounts[a]) for i, a in enumerate(order)]
    per_day: dict[str, list[dict]] = {}
    for row in rows:
        per_day.setdefault(row["posted_on"], []).append(row)
    for bucket in per_day.values():
        bucket.sort(key=lambda r: r["id"])
    sizes = {len(bucket) for bucket in per_day.values()}
    if len(sizes) == 1 and 1 not in sizes:
        slots = sizes.pop()
        return [(slot, [per_day[day][slot] for day in sorted(per_day)]) for slot in range(slots)]

    # Two rhythms from one merchant at different prices — a wireless bill and a home
    # internet bill from the same carrier land on different days for different amounts.
    strong = [sorted(c, key=lambda r: (r["posted_on"], r["id"]))
              for c in _amount_clusters(rows) if len(c) >= MIN_TXNS]
    if len(strong) >= 2:
        return [(i, chunk) for i, chunk in enumerate(strong)]
    return [(0, rows)]


def _amount_clusters(rows: list[dict]) -> list[list[dict]]:
    """Group a merchant's charges into price bands, cheapest first."""
    ordered = sorted(rows, key=lambda r: abs(r["amount_cents"]))
    clusters: list[dict] = []
    for row in ordered:
        amount = abs(row["amount_cents"])
        if clusters and abs(amount - clusters[-1]["base"]) <= max(100, clusters[-1]["base"] * 0.05):
            clusters[-1]["rows"].append(row)
        else:
            clusters.append({"base": amount, "rows": [row]})
    return [c["rows"] for c in clusters]


def _candidate(key: str, rows: list[dict], series: int = 0) -> dict | None:
    """Turn one merchant's history into a recurring candidate, or None if it is not a pattern."""
    if len(rows) < MIN_TXNS:
        return None
    days = [date.fromisoformat(r["posted_on"]) for r in rows]
    if (days[-1] - days[0]).days < MIN_SPAN_DAYS:
        return None
    intervals = [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0]
    if len(intervals) < 2:
        return None
    median_interval = statistics.median(intervals)
    if any(abs(i - median_interval) > median_interval * INTERVAL_TOLERANCE for i in intervals):
        return None
    cadence = classify(median_interval)
    if cadence is None:
        return None
    amounts = [abs(r["amount_cents"]) for r in rows]
    median_amount = int(statistics.median(amounts))
    if median_amount < MIN_AMOUNT_CENTS:
        return None
    spread = max(amounts) - min(amounts)
    if spread > max(500, median_amount * 3):
        return None
    # Utilities swing with the season; keep them but say so rather than pretending
    # the median is the exact price.
    varies = spread > max(100, median_amount * AMOUNT_TOLERANCE)
    positive = sum(1 for a in rows if a["amount_cents"] > 0)
    kind = "income" if positive > len(rows) / 2 else "bill"
    last = days[-1]
    interval_days = CADENCES[cadence][1]
    next_due = last
    guard = 0
    while next_due <= last and guard < 12:
        guard += 1
        next_due = (next_due + timedelta(days=round(median_interval))) if cadence in ("weekly", "biweekly") \
            else _month_add(next_due, {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}[cadence])
    # A pattern whose next date is months old has stopped, not recurred — don't suggest it.
    # (Weekly/biweekly next dates are computed per interval, so a gap shows up here too.)
    lookahead = next_due + timedelta(days={"weekly": 14, "biweekly": 21}.get(cadence, 45))
    if lookahead < date.today():
        return None
    confidence = min(99, 45 + len(rows) * 8 + int(20 * (1 - min(1.0, statistics.pstdev(intervals) / max(median_interval, 1)))))
    name = _title(rows[-1]["merchant"])
    if series:
        name = f"{name} ({series + 1})"
    return {
        "name": name,
        "merchant_key": key,
        "series": series,
        "account_id": rows[-1]["account_id"],
        "category_id": rows[-1]["category_id"],
        "cadence": cadence,
        "interval_days": interval_days,
        "anchor_day": last.day if cadence in ("monthly", "quarterly", "semiannual", "annual") else None,
        "amount_cents": median_amount,
        "amount_varies": 1 if varies else 0,
        "kind": kind,
        "next_due_on": next_due.isoformat(),
        "last_seen_on": last.isoformat(),
        "confidence": confidence,
    }


# Fixed commitments whose amount drifts (a subscription bundle, a utility) still deserve a
# line in Bills; the categories and brands below get a relaxed cadence test.
SUBSCRIPTION_WORDS = (
    "netflix", "spotify", "hulu", "disney", "hbo", "paramount", "peacock", "youtube",
    "apple.com/bill", "itunes", "google", "adobe", "microsoft", "openai", "anthropic",
    "openrouter", "dropbox", "icloud", "audible", "patreon", "substack", "nytimes",
    "wsj", "1password", "notion", "github", "vercel", "cursor", "crunchyroll",
    "xai", "grok", "anthropic", "claude", "perplexity", "midjourney", "elevenlabs",
)


def _next_month_after(day: date) -> date:
    """The first monthly anniversary after today, so the suggestion lands in the future."""
    nxt = _month_add(day, 1)
    for _ in range(24):
        if nxt > date.today():
            break
        nxt = _month_add(nxt, 1)
    return nxt


def _subscription_candidate(key: str, rows: list[dict], series: int = 0) -> dict | None:
    """A recurring charge whose amount or billing day wobbles — subscriptions, utilities.

    The strict test rejects these (a plan changes price, Apple bills several products on
    different days). Here the rhythm only has to be roughly monthly: three charges inside
    a couple of months, the latest still active.
    """
    charges = [r for r in rows if r["amount_cents"] < 0]
    if len(charges) < 2:
        return None
    days = sorted({date.fromisoformat(r["posted_on"]) for r in charges})
    if len(days) < 2 or (days[-1] - days[0]).days < 20:
        return None
    if (date.today() - days[-1]).days > 75:
        return None
    intervals = [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0]
    median_interval = statistics.median(intervals) if intervals else 30
    cadence = classify(median_interval) or "monthly"
    amounts = [abs(r["amount_cents"]) for r in charges]
    median_amount = int(statistics.median(amounts[-4:]))
    if median_amount < MIN_AMOUNT_CENTS:
        return None
    spread = max(amounts) - min(amounts)
    last = days[-1]
    return {
        "name": _title(charges[-1]["merchant"]) + (f" ({series + 1})" if series else ""),
        "merchant_key": key,
        "series": series,
        "account_id": charges[-1]["account_id"],
        "category_id": charges[-1]["category_id"],
        "cadence": cadence,
        "interval_days": CADENCES[cadence][1],
        "anchor_day": last.day,
        "amount_cents": median_amount,
        "amount_varies": 1 if spread > max(100, median_amount * AMOUNT_TOLERANCE) else 0,
        # A subscription is a bill; a refund on the same card never changes that.
        "kind": "bill",
        "next_due_on": _next_month_after(last).isoformat(),
        "last_seen_on": last.isoformat(),
        "confidence": 55,
    }


def detect(household_id: int) -> list[dict]:
    """Return candidate recurring rows (not yet stored)."""
    known = {(r["merchant_key"], r.get("series") or 0) for r in list_recurring(household_id)}
    found: list[dict] = []
    for key, rows in _merchant_groups(household_id).items():
        for series, chunk in _split_series(rows):
            if (key, series) in known:
                continue
            cand = _candidate(key, chunk, series)
            if not cand:
                cat = chunk[-1].get("category_id")
                name = db.query_one("SELECT name FROM categories WHERE id = ?", (cat,))["name"] if cat else ""
                signed_up = (name or "").lower() == "subscriptions" or any(w in key for w in SUBSCRIPTION_WORDS)
                if signed_up:
                    cand = _subscription_candidate(key, chunk, series)
            if cand:
                found.append(cand)
    found.sort(key=lambda r: (-r["confidence"], r["name"]))
    return found


def _title(value: str | None) -> str:
    text = (value or "Recurring").strip()
    if text.isupper():
        text = text.title()
    return text[:60]


def scan(household_id: int) -> int:
    """Detect and store suggestions. Returns how many were added."""
    added = 0
    now = db.utcnow()
    for cand in detect(household_id):
        db.execute(
            """INSERT INTO recurring(household_id, name, merchant_key, series, account_id, category_id,
                   cadence, interval_days, anchor_day, amount_cents, amount_varies, kind,
                   next_due_on, last_seen_on, confidence, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'suggested',?,?)""",
            (household_id, cand["name"], cand["merchant_key"], cand["series"], cand["account_id"],
             cand["category_id"], cand["cadence"], cand["interval_days"], cand["anchor_day"],
             cand["amount_cents"], cand["amount_varies"], cand["kind"], cand["next_due_on"],
             cand["last_seen_on"], cand["confidence"], now, now),
        )
        added += 1
    return added


# ---------------------------------------------------------------- write
def create(household_id: int, name: str, amount_cents: int, cadence: str, next_due_on: str,
           kind: str = "bill", category_id: int | None = None, account_id: int | None = None,
           merchant_key: str | None = None, status: str = "confirmed", notes: str | None = None) -> int:
    if cadence not in CADENCES:
        raise ValueError("Pick a cadence.")
    if amount_cents <= 0:
        raise ValueError("Amount must be greater than zero.")
    try:
        due = date.fromisoformat(next_due_on)
    except ValueError as exc:
        raise ValueError("Pick a valid next date.") from exc
    now = db.utcnow()
    return db.execute(
        """INSERT INTO recurring(household_id, name, merchant_key, account_id, category_id, cadence,
               interval_days, anchor_day, amount_cents, kind, next_due_on, confidence, status, notes,
               created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,100,?,?,?,?)""",
        (household_id, name.strip()[:60], merchant_key or rules.normalize_merchant(name),
         account_id, category_id, cadence, CADENCES[cadence][1],
         due.day if cadence in ("monthly", "quarterly", "semiannual", "annual") else None,
         int(amount_cents), kind, due.isoformat(), status, notes, now, now),
    )


def update(household_id: int, recurring_id: int, **fields) -> None:
    allowed = {"name", "amount_cents", "cadence", "next_due_on", "kind", "category_id",
               "account_id", "status", "notes", "anchor_day", "interval_days"}
    sets, args = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key} = ?")
        args.append(value)
    if not sets:
        return
    if "cadence" in fields and fields["cadence"] in CADENCES:
        sets.append("interval_days = ?")
        args.append(CADENCES[fields["cadence"]][1])
    sets.append("updated_at = ?")
    args.append(db.utcnow())
    args.extend([household_id, recurring_id])
    db.execute(f"UPDATE recurring SET {', '.join(sets)} WHERE household_id = ? AND id = ?", tuple(args))


def set_status(household_id: int, recurring_id: int, status: str) -> None:
    if status not in ("suggested", "confirmed", "ignored", "ended"):
        return
    db.execute("UPDATE recurring SET status = ?, updated_at = ? WHERE household_id = ? AND id = ?",
               (status, db.utcnow(), household_id, recurring_id))


def delete(household_id: int, recurring_id: int) -> None:
    db.execute("DELETE FROM recurring WHERE household_id = ? AND id = ?", (household_id, recurring_id))


# ---------------------------------------------------------------- bills in a window
def bills_between(household_id: int, start: date, end: date) -> list[dict]:
    out: list[dict] = []
    for row in active(household_id):
        if row["kind"] != "bill":
            continue
        for due in occurrences(row, start, end):
            out.append({
                "id": row["id"],
                "name": row["name"],
                "due_on": due.isoformat(),
                "amount_cents": row["amount_cents"],
                "category_id": row["category_id"],
                "cadence": row["cadence"],
            })
    out.sort(key=lambda r: (r["due_on"], r["name"]))
    return out


def month_uncovered(household_id: int, month: str,
                    plan_grid: dict | None = None) -> tuple[int, list[dict]]:
    """Bills this month that their category budget does not already cover."""
    from . import budgets

    year, mon = (int(x) for x in month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    start, end = date(year, mon, 1), date(year, mon, last)
    g = plan_grid if plan_grid is not None else budgets.grid(household_id, month)
    remaining = {r["id"]: max(0, r["remaining_cents"]) for r in g["rows"]}
    uncovered = 0
    explained: list[dict] = []
    for bill in bills_between(household_id, start, end):
        left_in_category = remaining.get(bill["category_id"], 0)
        covered = min(left_in_category, bill["amount_cents"])
        if bill["category_id"] in remaining:
            remaining[bill["category_id"]] = left_in_category - covered
        shortfall = bill["amount_cents"] - covered
        if shortfall > 0:
            uncovered += shortfall
            explained.append({**bill, "uncovered_cents": shortfall})
    return uncovered, explained
