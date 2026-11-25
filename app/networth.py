"""Accounts domain logic and net worth snapshots."""
from __future__ import annotations

from datetime import date, timedelta

from . import db

LIABILITY_TYPES = {"credit_card", "loan", "mortgage", "other_liability"}
BITCOIN_TYPE = "crypto"

ACCOUNT_TYPES: list[tuple[str, str, str]] = [
    # (value, label, group)
    ("checking", "Checking", "asset"),
    ("savings", "Savings", "asset"),
    ("brokerage", "Brokerage", "asset"),
    ("retirement", "Retirement", "asset"),
    ("hsa", "HSA", "asset"),
    ("529", "529 plan", "asset"),
    ("cash", "Cash", "asset"),
    ("crypto", "Bitcoin", "asset"),
    ("real_estate", "Real estate", "asset"),
    ("other_asset", "Other asset", "asset"),
    ("credit_card", "Credit card", "liability"),
    ("loan", "Loan", "liability"),
    ("mortgage", "Mortgage", "liability"),
    ("other_liability", "Other debt", "liability"),
]

TYPE_LABELS = {value: label for value, label, _ in ACCOUNT_TYPES}
TYPE_GROUPS = {value: group for value, _, group in ACCOUNT_TYPES}


def is_liability(account_type: str) -> bool:
    return account_type in LIABILITY_TYPES


def is_bitcoin(account_type: str) -> bool:
    return account_type == BITCOIN_TYPE


def list_accounts(household_id: int, include_archived: bool = False) -> list[dict]:
    sql = (
        "SELECT * FROM accounts WHERE household_id = ?"
        + ("" if include_archived else " AND archived_at IS NULL")
        + " ORDER BY type, display_order, name COLLATE NOCASE"
    )
    rows = db.query_all(sql, (household_id,))
    accounts = []
    for row in rows:
        item = dict(row)
        item["is_liability"] = is_liability(item["type"])
        item["type_label"] = TYPE_LABELS.get(item["type"], item["type"].title())
        accounts.append(item)
    return accounts


def get_account(household_id: int, account_id: int) -> dict | None:
    row = db.query_one(
        "SELECT * FROM accounts WHERE id = ? AND household_id = ?", (account_id, household_id)
    )
    if row is None:
        return None
    item = dict(row)
    item["is_liability"] = is_liability(item["type"])
    item["type_label"] = TYPE_LABELS.get(item["type"], item["type"].title())
    return item


def totals(accounts: list[dict]) -> dict:
    assets = sum(a["current_balance_cents"] for a in accounts if not a["is_liability"] and a["include_in_net_worth"])
    liabilities = sum(a["current_balance_cents"] for a in accounts if a["is_liability"] and a["include_in_net_worth"])
    return {
        "assets": assets,
        "liabilities": liabilities,
        "net": assets - liabilities,
        "asset_count": sum(1 for a in accounts if not a["is_liability"]),
        "liability_count": sum(1 for a in accounts if a["is_liability"]),
    }


def record_balance(account_id: int, balance_cents: int, as_of: str | None = None, source: str = "manual") -> None:
    as_of = as_of or date.today().isoformat()
    db.execute(
        """
        INSERT INTO account_balances(account_id, as_of_date, balance_cents, available_cents, source, created_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(account_id, as_of_date)
        DO UPDATE SET balance_cents = excluded.balance_cents, source = excluded.source
        """,
        (account_id, as_of, balance_cents, None, source, db.utcnow()),
    )


def recalc(household_id: int, as_of: str | None = None) -> dict:
    """Recompute the household net worth snapshot for a date. Idempotent."""
    as_of = as_of or date.today().isoformat()
    accounts = list_accounts(household_id)
    t = totals(accounts)
    db.execute(
        """
        INSERT INTO net_worth_snapshots(as_of_date, household_id, assets_cents, liabilities_cents, net_cents, created_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(as_of_date, household_id)
        DO UPDATE SET assets_cents = excluded.assets_cents,
                      liabilities_cents = excluded.liabilities_cents,
                      net_cents = excluded.net_cents
        """,
        (as_of, household_id, t["assets"], t["liabilities"], t["net"], db.utcnow()),
    )
    return t


def snapshot(household_id: int, as_of: str | None = None) -> dict:
    """Record every account's balance and the household net worth for one day.

    Run nightly so the trend has an unbroken line: page-load snapshots only capture the
    moments someone happened to look, and an evening's balance updates can land after one.
    Idempotent — re-running for the same date updates the row in place.
    """
    as_of = as_of or date.today().isoformat()
    accounts = list_accounts(household_id)
    for account in accounts:
        record_balance(account["id"], account["current_balance_cents"], as_of=as_of, source="nightly")
    result = recalc(household_id, as_of)
    result["accounts"] = len(accounts)
    result["as_of"] = as_of
    return result


def series(household_id: int, limit: int = 400) -> list[dict]:
    rows = db.query_all(
        "SELECT as_of_date, assets_cents, liabilities_cents, net_cents FROM net_worth_snapshots"
        " WHERE household_id = ? ORDER BY as_of_date DESC LIMIT ?",
        (household_id, limit),
    )
    # A snapshot taken before the household had any accounts is not history: an
    # all-zero row would draw every chart up from the origin.
    return [dict(r) for r in reversed(rows)
            if r["assets_cents"] or r["liabilities_cents"]]


RANGES: list[tuple[str, str]] = [("30d", "30D"), ("ytd", "YTD"), ("1y", "1Y"), ("3y", "3Y"), ("all", "All")]
RANGE_LABELS = {
    "30d": "over the last 30 days",
    "ytd": "year to date",
    "1y": "over the last year",
    "3y": "over the last 3 years",
    "all": "all time",
}


def _range_start(key: str, latest: date) -> date | None:
    if key == "ytd":
        return date(latest.year, 1, 1)
    if key == "1y":
        return latest - timedelta(days=365)
    if key == "3y":
        return latest - timedelta(days=1095)
    if key == "30d":
        return latest - timedelta(days=30)
    return None


def window(household_id: int, key: str = "30d") -> dict | None:
    """The series clipped to a standard frame, plus the change across that frame.

    `partial` is true when history does not reach back to the frame's start yet, so
    the UI can say "since Nov 21" instead of pretending to a full 30 days.
    """
    if key not in RANGE_LABELS:
        key = "30d"
    rows = series(household_id)
    if not rows:
        return None
    latest = rows[-1]
    latest_date = date.fromisoformat(latest["as_of_date"])
    start = _range_start(key, latest_date)
    if start is None:
        base = rows[0]
        clipped = rows
    else:
        older = [r for r in rows if r["as_of_date"] <= start.isoformat()]
        base = older[-1] if older else rows[0]
        clipped = [r for r in rows if r["as_of_date"] >= base["as_of_date"]]
    base_date = date.fromisoformat(base["as_of_date"])
    span = (latest_date - base_date).days
    delta = latest["net_cents"] - base["net_cents"]
    pct = (delta / abs(base["net_cents"]) * 100) if base["net_cents"] else None
    # Snapshots are sparse, so the nearest base can sit well outside the frame. When it
    # does, name the real date rather than claiming a full 30 days.
    gap = (base_date - start).days if start else 0
    return {
        "key": key, "label": RANGE_LABELS[key], "rows": clipped,
        "from": base, "to": latest, "delta": delta, "pct": pct, "days": span,
        "partial": bool(start) and gap > 0,
        "approx": bool(start) and abs(gap) > 2,
    }


def change_since(household_id: int, days: int = 30) -> dict | None:
    """Compare the latest snapshot with the oldest one within the window."""
    win = window(household_id, "30d" if days == 30 else "all")
    if win is None or win["from"]["as_of_date"] == win["to"]["as_of_date"]:
        return None
    return win


def sparkline_points(rows: list[dict], width: int = 640, height: int = 120, pad: int = 8) -> dict:
    """Build SVG polyline points for the net worth series."""
    if not rows:
        return {"points": "", "area": "", "min": 0, "max": 0}
    values = [r["net_cents"] for r in rows]
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    n = len(rows)
    coords = []
    for i, value in enumerate(values):
        x = pad + (width - 2 * pad) * (i / max(1, n - 1))
        y = height - pad - (height - 2 * pad) * ((value - lo) / span)
        coords.append((round(x, 1), round(y, 1)))
    points = " ".join(f"{x},{y}" for x, y in coords)
    area = f"{pad},{height - pad} " + points + f" {width - pad},{height - pad}"
    return {"points": points, "area": area, "min": lo, "max": hi, "first": coords[0], "last": coords[-1]}


def line_points(values, width: int = 640, height: int = 160, pad: float = 6.0,
                lo: int | None = None, hi: int | None = None) -> str:
    n = len(values)
    if not n:
        return ""
    if n == 1:
        return f"{width / 2:.1f},{height / 2:.1f}"
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    if hi == lo:
        hi = lo + 1
    step = (width - 2 * pad) / (n - 1)
    return " ".join(
        f"{pad + i * step:.1f},{height - pad - (v - lo) * (height - 2 * pad) / (hi - lo):.1f}"
        for i, v in enumerate(values)
    )


def chart_geometry(rows: list[dict], width: int = 640, height: int = 160) -> dict:
    """SVG geometry for the two net worth charts.

    The net worth line is drawn in dollars on its own scale. Assets and
    liabilities are drawn as percent growth from the first snapshot, otherwise
    their difference in magnitude flattens both lines.
    """
    if not rows:
        return {}
    net = [r["net_cents"] for r in rows]
    assets = [r["assets_cents"] for r in rows]
    liabilities = [r["liabilities_cents"] for r in rows]

    def growth(values):
        base = values[0]
        return [0.0 if not base else (v - base) / base * 100.0 for v in values]

    g_assets, g_liabilities = growth(assets), growth(liabilities)
    lo = min(g_assets + g_liabilities)
    hi = max(g_assets + g_liabilities)
    if hi == lo:
        hi = lo + 1
    net_points = line_points(net, width, height)
    zero_y = None
    if lo <= 0 <= hi:
        zero_y = round(height - 6 - (0 - lo) * (height - 12) / (hi - lo), 1)
    return {
        "net": {"points": net_points,
                "area": f"6,{height - 6} {net_points} {width - 6},{height - 6}"},
        "assets": {"points": line_points(g_assets, width, height, lo=lo, hi=hi)},
        "liabilities": {"points": line_points(g_liabilities, width, height, lo=lo, hi=hi)},
        "zero_y": zero_y,
        "growth_range": {"lo": round(lo, 2), "hi": round(hi, 2)},
    }


def chart_payload(rows: list[dict]) -> dict:
    """The numbers the drag scrubber reads out, one entry per snapshot."""
    return {
        "labels": [r["as_of_date"] for r in rows],
        "series": [
            {"key": "net", "name": "Net worth", "values": [r["net_cents"] for r in rows]},
            {"key": "assets", "name": "Assets", "values": [r["assets_cents"] for r in rows]},
            {"key": "liabilities", "name": "Liabilities",
             "values": [r["liabilities_cents"] for r in rows]},
        ],
    }


def group_by_institution(accounts):
    """Balance-sheet sections: institutions A-Z, accounts without one last.

    Order inside a section is whatever ``list_accounts`` produced (assets before
    liabilities), so a Chase section reads top to bottom like the sheet.
    """
    buckets: dict[str, list] = {}
    for account in accounts:
        name = (account.get("institution") or "").strip()
        buckets.setdefault(name or "No institution", []).append(account)
    return [
        {"name": name, "accounts": buckets[name]}
        for name in sorted(buckets, key=lambda n: (n == "No institution", n.lower()))
    ]
