"""Portfolio: holdings, allocation, returns (XIRR) and option positions."""
from __future__ import annotations

from datetime import date

from . import db, networth

INVESTMENT_TYPES = {"brokerage", "retirement", "hsa", "crypto"}
KINDS = [
    ("stock", "Stock"),
    ("etf", "ETF"),
    ("fund", "Mutual fund"),
    ("bond", "Bond"),
    ("crypto", "Crypto"),
    ("cash", "Cash"),
    ("other", "Other"),
]
KIND_LABELS = dict(KINDS)
# Account-value rows borrow their account's type; these never belong in the add-holding picker.
KIND_LABELS.update({"bitcoin": "Bitcoin", "other_asset": "Other asset"})
OPTION_TYPES = [("put", "Put"), ("call", "Call")]


# ---------------------------------------------------------------- securities
def ensure_security(household_id: int, symbol: str, name: str | None = None, kind: str = "stock") -> int:
    symbol = (symbol or "").strip().upper()[:20]
    if not symbol:
        raise ValueError("Symbol is required.")
    row = db.query_one("SELECT id FROM securities WHERE household_id = ? AND symbol = ?",
                       (household_id, symbol))
    if row:
        if name or kind:
            db.execute("UPDATE securities SET name = COALESCE(?, name), kind = COALESCE(?, kind) WHERE id = ?",
                       (name, kind, row["id"]))
        return row["id"]
    return db.execute(
        "INSERT INTO securities(household_id, symbol, name, kind, created_at) VALUES (?,?,?,?,?)",
        (household_id, symbol, (name or symbol).strip()[:80], kind, db.utcnow()),
    )


def holdings(household_id: int) -> list[dict]:
    rows = db.query_all(
        """SELECT h.*, s.symbol, s.name AS security_name, s.kind, a.name AS account_name, a.type AS account_type
           FROM holdings h
           JOIN securities s ON s.id = h.security_id
           JOIN accounts a ON a.id = h.account_id
           WHERE h.household_id = ? AND a.archived_at IS NULL
           ORDER BY s.symbol""",
        (household_id,),
    )
    out = []
    for row in rows:
        item = dict(row)
        qty = float(item["quantity"] or 0)
        item["value_cents"] = int(round(qty * item["price_cents"]))
        item["gain_cents"] = item["value_cents"] - item["cost_basis_cents"]
        item["gain_pct"] = (item["gain_cents"] / item["cost_basis_cents"] * 100) if item["cost_basis_cents"] else None
        item["kind_label"] = KIND_LABELS.get(item["kind"], item["kind"].title())
        out.append(item)
    return out


def group_by_account(rows: list[dict]) -> list[dict]:
    """Sections for the holdings table: one per account, alphabetically."""
    groups: dict[str, dict] = {}
    for row in rows:
        group = groups.setdefault(
            row["account_name"],
            {"name": row["account_name"], "rows": [], "value_cents": 0, "gain_cents": 0},
        )
        group["rows"].append(row)
        group["value_cents"] += row["value_cents"]
        group["gain_cents"] += row["gain_cents"]
    for group in groups.values():
        group["has_gain"] = any(not r.get("is_account_value") for r in group["rows"])
    return [groups[name] for name in sorted(groups)]


def account_value_rows(household_id: int, rows: list[dict]) -> list[dict]:
    """Accounts marked "add to portfolio" that have no holdings of their own.

    A linked brokerage already reports positions; adding its balance too would count the
    same money twice, so those accounts are skipped here.
    """
    tracked = {r["account_id"] for r in rows}
    out = []
    for account in db.query_all(
        """SELECT id, name, type, units_sats, current_balance_cents FROM accounts
           WHERE household_id = ? AND archived_at IS NULL AND include_in_portfolio = 1
           ORDER BY name COLLATE NOCASE""",
        (household_id,),
    ):
        if account["id"] in tracked or not account["current_balance_cents"]:
            continue
        value = int(account["current_balance_cents"])
        # The account's type is the better label: three crypto accounts should read as
        # Bitcoin, not three anonymous "Account" rows.
        if account["type"] == "crypto":
            kind, kind_label = "bitcoin", "Bitcoin"
        elif account["type"] == "other_asset":
            kind, kind_label = "other_asset", "Other asset"
        else:
            kind, kind_label = "other", "Account"
        out.append({
            "id": None,
            "account_id": account["id"],
            "account_name": account["name"],
            "account_type": account["type"],
            "units_sats": account["units_sats"],
            "symbol": "—",
            "security_name": account["name"],
            "kind": kind,
            "kind_label": kind_label,
            "quantity": None,
            "price_cents": None,
            "value_cents": value,
            "cost_basis_cents": value,
            "gain_cents": 0,
            "gain_pct": None,
            "source": "account",
            "is_account_value": True,
        })
    return out


def get_holding(household_id: int, holding_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM holdings WHERE household_id = ? AND id = ?", (household_id, holding_id))
    return dict(row) if row else None


def add_holding(household_id: int, account_id: int, symbol: str, quantity: float,
                cost_basis_cents: int, price_cents: int, name: str | None = None,
                kind: str = "stock") -> int:
    account = db.query_one("SELECT * FROM accounts WHERE household_id = ? AND id = ?", (household_id, account_id))
    if account is None:
        raise ValueError("Pick an account.")
    security_id = ensure_security(household_id, symbol, name, kind)
    existing = db.query_one("SELECT id FROM holdings WHERE account_id = ? AND security_id = ?",
                            (account_id, security_id))
    now = db.utcnow()
    if existing:
        # Another purchase of the same security adds to the position.
        db.execute(
            """UPDATE holdings SET quantity = quantity + ?, cost_basis_cents = cost_basis_cents + ?,
                   price_cents = ?, priced_on = ?, updated_at = ? WHERE id = ?""",
            (float(quantity), int(cost_basis_cents), int(price_cents), date.today().isoformat(), now, existing["id"]),
        )
        holding_id = existing["id"]
    else:
        holding_id = db.execute(
            """INSERT INTO holdings(household_id, account_id, security_id, quantity, cost_basis_cents,
                   price_cents, priced_on, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (household_id, account_id, security_id, float(quantity), int(cost_basis_cents),
             int(price_cents), date.today().isoformat(), now, now),
        )
    sync_account_balance(household_id, account_id)
    return holding_id


def update_holding(household_id: int, holding_id: int, quantity: float | None = None,
                   price_cents: int | None = None, cost_basis_cents: int | None = None) -> None:
    row = get_holding(household_id, holding_id)
    if row is None:
        return
    sets, args = [], []
    if quantity is not None:
        sets.append("quantity = ?")
        args.append(float(quantity))
    if price_cents is not None:
        sets.append("price_cents = ?")
        args.append(int(price_cents))
        sets.append("priced_on = ?")
        args.append(date.today().isoformat())
    if cost_basis_cents is not None:
        sets.append("cost_basis_cents = ?")
        args.append(int(cost_basis_cents))
    if not sets:
        return
    sets.append("updated_at = ?")
    args.append(db.utcnow())
    args.append(holding_id)
    db.execute(f"UPDATE holdings SET {', '.join(sets)} WHERE id = ?", tuple(args))
    sync_account_balance(household_id, row["account_id"])


def delete_holding(household_id: int, holding_id: int) -> None:
    row = get_holding(household_id, holding_id)
    if row is None:
        return
    db.execute("DELETE FROM holdings WHERE id = ?", (holding_id,))
    sync_account_balance(household_id, row["account_id"])


def sync_account_balance(household_id: int, account_id: int) -> None:
    """An account that holds securities takes its balance from them."""
    total = db.query_one(
        """SELECT COALESCE(SUM(ROUND(quantity * price_cents)), 0) AS total
           FROM holdings WHERE household_id = ? AND account_id = ?""",
        (household_id, account_id),
    )["total"]
    count = db.query_one("SELECT COUNT(*) AS n FROM holdings WHERE account_id = ?", (account_id,))["n"]
    if not count:
        return
    db.execute("UPDATE accounts SET current_balance_cents = ?, updated_at = ? WHERE id = ?",
               (int(total), db.utcnow(), account_id))
    networth.recalc(household_id)


# ---------------------------------------------------------------- totals + allocation
def totals(rows: list[dict]) -> dict:
    value = sum(r["value_cents"] for r in rows)
    cost = sum(r["cost_basis_cents"] for r in rows)
    gain = value - cost
    return {
        "value_cents": value,
        "cost_cents": cost,
        "gain_cents": gain,
        "gain_pct": (gain / cost * 100) if cost else None,
    }


def allocation(rows: list[dict], key: str = "kind") -> list[dict]:
    total = sum(r["value_cents"] for r in rows) or 1
    buckets: dict[str, int] = {}
    for r in rows:
        label = KIND_LABELS.get(r[key], r[key]) if key == "kind" else str(r[key])
        buckets[label] = buckets.get(label, 0) + r["value_cents"]
    out = [{"label": k, "value_cents": v, "pct": v / total * 100} for k, v in buckets.items()]
    out.sort(key=lambda b: -b["value_cents"])
    return out


def investment_accounts(household_id: int) -> list[dict]:
    return [a for a in networth.list_accounts(household_id) if a["type"] in INVESTMENT_TYPES]


# ---------------------------------------------------------------- flows + XIRR
def add_flow(household_id: int, occurred_on: str, amount_cents: int,
             account_id: int | None = None, note: str | None = None) -> int:
    return db.execute(
        """INSERT INTO investment_flows(household_id, account_id, occurred_on, amount_cents, note, created_at)
           VALUES (?,?,?,?,?,?)""",
        (household_id, account_id, occurred_on, int(amount_cents), note, db.utcnow()),
    )


def list_flows(household_id: int) -> list[dict]:
    return [dict(r) for r in db.query_all(
        "SELECT * FROM investment_flows WHERE household_id = ? ORDER BY occurred_on DESC", (household_id,))]


def delete_flow(household_id: int, flow_id: int) -> None:
    db.execute("DELETE FROM investment_flows WHERE household_id = ? AND id = ?", (household_id, flow_id))


def xirr(flows: list[tuple[date, int]]) -> float | None:
    """Money-weighted annual return. Negative = contributed, positive = returned."""
    if len(flows) < 2:
        return None
    if not (any(f[1] < 0 for f in flows) and any(f[1] > 0 for f in flows)):
        return None
    base = min(f[0] for f in flows)

    def npv(rate: float) -> float:
        total = 0.0
        for day, amount in flows:
            years = (day - base).days / 365.0
            total += amount / ((1.0 + rate) ** years)
        return total

    lo, hi = -0.9999, 100.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 0.000001:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def performance(household_id: int, rows: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    value = sum(r["value_cents"] for r in rows)
    flows = list_flows(household_id)
    contributed = sum(f["amount_cents"] for f in flows)
    cost = sum(r["cost_basis_cents"] for r in rows)
    series: list[tuple[date, int]] = []
    for f in flows:
        try:
            day = date.fromisoformat(f["occurred_on"])
        except ValueError:
            continue
        # Contributing money is a negative cash flow; withdrawing is positive.
        series.append((day, -int(f["amount_cents"])))
    if series and value:
        series.append((today, value))
    return {
        "value_cents": value,
        "contributed_cents": contributed,
        "flows": len(flows),
        "xirr": xirr(series),
        "has_flows": bool(flows),
        "cost_cents": cost,
        # If contributions are far below cost, the flow history is incomplete and XIRR lies.
        "flows_cover_cost": bool(flows) and contributed >= cost * 0.9,
    }


# ---------------------------------------------------------------- options
def options(household_id: int, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM option_positions WHERE household_id = ?"
    args: list = [household_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY status, expiry_on"
    today = date.today()
    out = []
    for row in db.query_all(sql, tuple(args)):
        item = dict(row)
        contracts = int(item["contracts"] or 0)
        premium = int(item["premium_cents"] or 0)
        item["premium_total_cents"] = premium * contracts * 100
        item["premium_per_share_cents"] = premium
        item["dte"] = (date.fromisoformat(item["expiry_on"]) - today).days
        if item["option_type"] == "put":
            item["breakeven_cents"] = item["strike_cents"] - premium
            item["collateral_cents"] = item["strike_cents"] * contracts * 100
        else:
            item["breakeven_cents"] = item["strike_cents"] + premium
            item["collateral_cents"] = 0
        out.append(item)
    return out


def option_summary(rows: list[dict]) -> dict:
    open_rows = [r for r in rows if r["status"] == "open"]
    return {
        "open_count": len(open_rows),
        "premium_cents": sum(r["premium_total_cents"] for r in open_rows),
        "collateral_cents": sum(r["collateral_cents"] for r in open_rows),
        "nearest_dte": min((r["dte"] for r in open_rows), default=None),
    }


def add_option(household_id: int, underlying: str, option_type: str, strike_cents: int,
               expiry_on: str, contracts: int, premium_cents: int, opened_on: str,
               account_id: int | None = None, notes: str | None = None) -> int:
    if option_type not in ("call", "put"):
        raise ValueError("Pick call or put.")
    if contracts <= 0:
        raise ValueError("Contracts must be at least 1.")
    try:
        date.fromisoformat(expiry_on)
        date.fromisoformat(opened_on)
    except ValueError as exc:
        raise ValueError("Pick valid dates.") from exc
    now = db.utcnow()
    return db.execute(
        """INSERT INTO option_positions(household_id, account_id, underlying, option_type, strike_cents,
               expiry_on, contracts, premium_cents, opened_on, status, notes, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?)""",
        (household_id, account_id, underlying.strip().upper()[:10], option_type, int(strike_cents),
         expiry_on, int(contracts), int(premium_cents), opened_on, notes, now, now),
    )


def close_option(household_id: int, option_id: int, close_price_cents: int = 0,
                 status: str = "closed") -> None:
    db.execute(
        """UPDATE option_positions SET status = ?, closed_on = ?, close_price_cents = ?, updated_at = ?
           WHERE household_id = ? AND id = ?""",
        (status, date.today().isoformat(), int(close_price_cents), db.utcnow(), household_id, option_id),
    )
