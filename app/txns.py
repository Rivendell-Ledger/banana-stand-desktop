"""Transactions: filtering, editing, splits, tags, soft delete and undo."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterable

from . import db

PER_PAGE = 50
MAX_PER_PAGE = 200

SYSTEM_CATEGORIES = (
    ("Uncategorized", "expense"),
    ("Income", "income"),
    ("Transfer", "transfer"),
)


# ---------------------------------------------------------------- categories
def ensure_system_categories(household_id: int) -> dict[str, int]:
    """Make sure the three system categories exist; return name -> id."""
    existing = {
        r["name"]: r["id"]
        for r in db.query_all(
            "SELECT id, name FROM categories WHERE household_id = ? AND is_system = 1",
            (household_id,),
        )
    }
    now = db.utcnow()
    for name, kind in SYSTEM_CATEGORIES:
        if name not in existing:
            with db.session_scope() as conn:
                cur = conn.execute(
                    "INSERT INTO categories(household_id, name, kind, is_system, created_at)"
                    " VALUES (?,?,?,1,?)",
                    (household_id, name, kind, now),
                )
                existing[name] = cur.lastrowid
    return existing


def categories_in_use(household_id: int) -> list[dict]:
    """Categories that actually hold at least one live transaction — the filter list."""
    return [dict(r) for r in db.query_all(
        """SELECT c.id, c.name FROM categories c
           WHERE c.household_id = ?
             AND EXISTS (SELECT 1 FROM transactions t
                         WHERE t.category_id = c.id AND t.deleted_at IS NULL)
           ORDER BY c.name COLLATE NOCASE""",
        (household_id,))]


def list_categories(household_id: int) -> list[dict]:
    rows = db.query_all(
        """SELECT c.*, (SELECT COUNT(*) FROM transactions t
                          WHERE t.category_id = c.id AND t.deleted_at IS NULL) AS used
           FROM categories c WHERE c.household_id = ?
           ORDER BY c.kind, c.is_system DESC, c.name COLLATE NOCASE""",
        (household_id,),
    )
    return [dict(r) for r in rows]


def category_map(household_id: int) -> dict[int, dict]:
    return {c["id"]: c for c in list_categories(household_id)}


# ---------------------------------------------------------------- search
def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 prefix query. Quotes every token."""
    tokens = re.findall(r"[0-9A-Za-z_]+", text or "")
    return " ".join(f'"{t}"*' for t in tokens[:8])


# ---------------------------------------------------------------- list
def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _where(household_id: int, f: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses = ["t.household_id = ?", "t.deleted_at IS NULL"]
    params: list[Any] = [household_id]

    account_id = _as_int(f.get("account_id"))
    if account_id is not None:
        clauses.append("t.account_id = ?")
        params.append(account_id)
    if f.get("category_id") == "none":
        clauses.append("t.category_id IS NULL AND NOT EXISTS (SELECT 1 FROM transaction_splits s WHERE s.transaction_id = t.id)")
    elif _as_int(f.get("category_id")) is not None:
        clauses.append("(t.category_id = ? OR EXISTS (SELECT 1 FROM transaction_splits s WHERE s.transaction_id = t.id AND s.category_id = ?))")
        params.extend([_as_int(f["category_id"]), _as_int(f["category_id"])])
    if f.get("tag"):
        clauses.append(
            "EXISTS (SELECT 1 FROM transaction_tags tt JOIN tags g ON g.id = tt.tag_id"
            " WHERE tt.transaction_id = t.id AND g.household_id = ? AND g.name = ?)"
        )
        params.extend([household_id, f["tag"]])
    if f.get("from"):
        clauses.append("t.posted_on >= ?")
        params.append(f["from"])
    if f.get("to"):
        clauses.append("t.posted_on <= ?")
        params.append(f["to"])
    if f.get("pending") in ("0", "1"):
        clauses.append("t.is_pending = ?")
        params.append(int(f["pending"]))
    if f.get("kind") == "income":
        clauses.append("t.amount_cents > 0 AND t.is_transfer = 0")
    elif f.get("kind") == "expense":
        clauses.append("t.amount_cents < 0 AND t.is_transfer = 0")
    elif f.get("kind") == "transfer":
        clauses.append("t.is_transfer = 1")
    if f.get("q"):
        clauses.append("t.id IN (SELECT rowid FROM transactions_fts WHERE transactions_fts MATCH ?)")
        params.append(fts_query(f["q"]))
    return " AND ".join(clauses), params


def list_transactions(
    household_id: int, filters: dict[str, Any] | None = None, page: int = 0, per_page: int = PER_PAGE
) -> dict[str, Any]:
    f = filters or {}
    where, params = _where(household_id, f)
    per_page = max(1, min(int(per_page or PER_PAGE), MAX_PER_PAGE))
    offset = max(0, page) * per_page

    total_row = db.query_one(f"SELECT COUNT(*) AS n, COALESCE(SUM(t.amount_cents), 0) AS total FROM transactions t WHERE {where}", tuple(params))
    rows = db.query_all(
        f"""SELECT t.*, a.name AS account_name, a.type AS account_type,
                   c.name AS category_name, c.kind AS category_kind,
                   (SELECT COUNT(*) FROM transaction_splits s WHERE s.transaction_id = t.id) AS split_count,
                   (SELECT COALESCE(SUM(s.amount_cents), 0) FROM transaction_splits s WHERE s.transaction_id = t.id) AS split_total,
                   (SELECT GROUP_CONCAT(g.name, ', ') FROM transaction_tags tt JOIN tags g ON g.id = tt.tag_id
                     WHERE tt.transaction_id = t.id ORDER BY g.name) AS tag_list
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE {where}
            ORDER BY t.posted_on DESC, t.id DESC
            LIMIT ? OFFSET ?""",
        tuple(params + [per_page, offset]),
    )
    items = []
    for r in rows:
        d = dict(r)
        d["tags"] = [x for x in (d.get("tag_list") or "").split(", ") if x]
        items.append(d)
    return {
        "rows": items,
        "total": total_row["n"],
        "total_cents": total_row["total"],
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total_row["n"] + per_page - 1) // per_page),
    }


def get_transaction(household_id: int, txn_id: int) -> dict | None:
    row = db.query_one(
        """SELECT t.*, a.name AS account_name, a.type AS account_type,
                  c.name AS category_name, c.kind AS category_kind
           FROM transactions t
           JOIN accounts a ON a.id = t.account_id
           LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.id = ? AND t.household_id = ?""",
        (txn_id, household_id),
    )
    if row is None:
        return None
    d = dict(row)
    d["tags"] = [
        r["name"]
        for r in db.query_all(
            "SELECT g.name FROM transaction_tags tt JOIN tags g ON g.id = tt.tag_id"
            " WHERE tt.transaction_id = ? ORDER BY g.name",
            (txn_id,),
        )
    ]
    d["splits"] = [
        dict(r)
        for r in db.query_all(
            """SELECT s.*, c.name AS category_name FROM transaction_splits s
               LEFT JOIN categories c ON c.id = s.category_id
               WHERE s.transaction_id = ? ORDER BY s.id""",
            (txn_id,),
        )
    ]
    return d


# ---------------------------------------------------------------- write
def create(
    household_id: int,
    account_id: int,
    posted_on: str,
    amount_cents: int,
    merchant: str,
    category_id: int | None = None,
    notes: str | None = None,
    is_pending: bool = False,
) -> int:
    cats = ensure_system_categories(household_id)
    now = db.utcnow()
    with db.session_scope() as conn:
        cur = conn.execute(
            """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant,
                   notes, category_id, is_pending, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (household_id, account_id, posted_on, amount_cents, merchant.strip(),
             (notes or "").strip() or None, category_id or cats["Uncategorized"],
             1 if is_pending else 0, now, now),
        )
        return cur.lastrowid


EDITABLE = {"merchant", "notes", "posted_on", "amount_cents", "category_id", "is_pending", "excluded", "is_transfer"}


def update_field(household_id: int, txn_id: int, field: str, value: Any) -> bool:
    if field not in EDITABLE:
        return False
    db.execute(
        f"UPDATE transactions SET {field} = ?, updated_at = ? WHERE id = ? AND household_id = ?",
        (value, db.utcnow(), txn_id, household_id),
    )
    return True


def soft_delete(household_id: int, txn_id: int) -> bool:
    db.execute(
        "UPDATE transactions SET deleted_at = ?, updated_at = ? WHERE id = ? AND household_id = ? AND deleted_at IS NULL",
        (db.utcnow(), db.utcnow(), txn_id, household_id),
    )
    return True


def restore(household_id: int, txn_id: int) -> bool:
    db.execute(
        "UPDATE transactions SET deleted_at = NULL, updated_at = ? WHERE id = ? AND household_id = ?",
        (db.utcnow(), txn_id, household_id),
    )
    return True


def restore_many(household_id: int, ids: Iterable[int]) -> int:
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    with db.session_scope() as conn:
        cur = conn.execute(
            f"UPDATE transactions SET deleted_at = NULL, updated_at = ? WHERE household_id = ? AND id IN ({marks})",
            tuple([db.utcnow(), household_id] + ids),
        )
        return cur.rowcount


def list_deleted(household_id: int) -> list[dict]:
    rows = db.query_all(
        """SELECT t.*, a.name AS account_name FROM transactions t
           JOIN accounts a ON a.id = t.account_id
           WHERE t.household_id = ? AND t.deleted_at IS NOT NULL
           ORDER BY t.deleted_at DESC LIMIT 200""",
        (household_id,),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- tags
def set_tags(household_id: int, txn_id: int, names: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in names:
        name = re.sub(r"\s+", " ", (raw or "").strip())[:40]
        if name and name.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(name)
    now = db.utcnow()
    with db.session_scope() as conn:
        conn.execute("DELETE FROM transaction_tags WHERE transaction_id = ?", (txn_id,))
        for name in cleaned:
            row = conn.execute(
                "SELECT id FROM tags WHERE household_id = ? AND name = ? COLLATE NOCASE", (household_id, name)
            ).fetchone()
            tag_id = row["id"] if row else conn.execute(
                "INSERT INTO tags(household_id, name) VALUES (?,?)", (household_id, name)
            ).lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO transaction_tags(transaction_id, tag_id) VALUES (?,?)", (txn_id, tag_id)
            )
        conn.execute("DELETE FROM tags WHERE household_id = ? AND id NOT IN (SELECT tag_id FROM transaction_tags)", (household_id,))
    return cleaned


def list_tags(household_id: int) -> list[dict]:
    rows = db.query_all(
        """SELECT g.name, COUNT(tt.transaction_id) AS n FROM tags g
           LEFT JOIN transaction_tags tt ON tt.tag_id = g.id
           WHERE g.household_id = ? GROUP BY g.id ORDER BY n DESC, g.name""",
        (household_id,),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- splits
class SplitError(ValueError):
    pass


def set_splits(household_id: int, txn_id: int, rows: list[dict]) -> int:
    """Replace splits. Rows: {amount_cents, category_id, note}. Sum must equal the parent."""
    txn = get_transaction(household_id, txn_id)
    if txn is None:
        raise SplitError("No such transaction.")
    clean = []
    for r in rows:
        if not r.get("category_id"):
            raise SplitError("Every split needs a category.")
        if not r.get("amount_cents"):
            raise SplitError("Every split needs an amount.")
        clean.append((int(r["category_id"]), int(r["amount_cents"]), (r.get("note") or "").strip() or None))
    if len(clean) < 2:
        raise SplitError("A split needs at least two lines.")
    if sum(c[1] for c in clean) != txn["amount_cents"]:
        raise SplitError("Splits must add up to the transaction amount.")
    with db.session_scope() as conn:
        conn.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (txn_id,))
        for cat, cents, note in clean:
            conn.execute(
                "INSERT INTO transaction_splits(transaction_id, category_id, amount_cents, note) VALUES (?,?,?,?)",
                (txn_id, cat, cents, note),
            )
        conn.execute("UPDATE transactions SET category_id = NULL, updated_at = ? WHERE id = ?", (db.utcnow(), txn_id))
    return len(clean)


def clear_splits(household_id: int, txn_id: int, category_id: int | None) -> None:
    cats = ensure_system_categories(household_id)
    with db.session_scope() as conn:
        conn.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (txn_id,))
        conn.execute(
            "UPDATE transactions SET category_id = ?, updated_at = ? WHERE id = ? AND household_id = ?",
            (category_id or cats["Uncategorized"], db.utcnow(), txn_id, household_id),
        )


# ---------------------------------------------------------------- bulk
BULK_ACTIONS = {"category", "tag", "transfer", "untransfer", "exclude", "include", "delete", "restore", "pending", "posted"}


def bulk(household_id: int, ids: Iterable[int], action: str, value: str | None = None) -> int:
    ids = [int(i) for i in ids]
    if not ids or action not in BULK_ACTIONS:
        return 0
    marks = ",".join("?" for _ in ids)
    now = db.utcnow()
    with db.session_scope() as conn:
        if action == "category":
            if not value:
                return 0
            conn.execute(
                f"UPDATE transactions SET category_id = ?, category_source = 'manual', updated_at = ?"
                f" WHERE household_id = ? AND id IN ({marks})",
                tuple([int(value), now, household_id] + ids),
            )
        elif action == "tag":
            for name in [v.strip() for v in (value or "").split(",") if v.strip()][:5]:
                row = conn.execute(
                    "SELECT id FROM tags WHERE household_id = ? AND name = ? COLLATE NOCASE", (household_id, name)
                ).fetchone()
                tag_id = row["id"] if row else conn.execute(
                    "INSERT INTO tags(household_id, name) VALUES (?,?)", (household_id, name)
                ).lastrowid
                for txn_id in ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO transaction_tags(transaction_id, tag_id) VALUES (?,?)", (txn_id, tag_id)
                    )
        elif action in {"transfer", "untransfer", "exclude", "include", "pending", "posted"}:
            field, val = {
                "transfer": ("is_transfer", 1),
                "untransfer": ("is_transfer", 0),
                "exclude": ("excluded", 1),
                "include": ("excluded", 0),
                "pending": ("is_pending", 1),
                "posted": ("is_pending", 0),
            }[action]
            conn.execute(
                f"UPDATE transactions SET {field} = ?, updated_at = ? WHERE household_id = ? AND id IN ({marks})",
                tuple([val, now, household_id] + ids),
            )
        elif action == "delete":
            conn.execute(
                f"UPDATE transactions SET deleted_at = ?, updated_at = ? WHERE household_id = ? AND id IN ({marks}) AND deleted_at IS NULL",
                tuple([now, now, household_id] + ids),
            )
        elif action == "restore":
            conn.execute(
                f"UPDATE transactions SET deleted_at = NULL, updated_at = ? WHERE household_id = ? AND id IN ({marks})",
                tuple([now, household_id] + ids),
            )
        return len(ids)


def summary(household_id: int, filters: dict[str, Any] | None = None) -> dict:
    f = filters or {}
    where, params = _where(household_id, f)
    counted = db.query_one(f"SELECT COUNT(*) AS n FROM transactions t WHERE {where}", tuple(params))
    # Moving money between your own accounts is neither income nor spending. The Cash Flow
    # card has always dropped transfers (`spend_series`), so the summary must too — otherwise
    # one page shows two different nets. The count stays the number of rows listed below;
    # an explicit kind=transfer filter still reports the transfers themselves.
    money_where = where if f.get("kind") == "transfer" else where + " AND t.is_transfer = 0"
    row = db.query_one(
        f"""SELECT COALESCE(SUM(CASE WHEN t.amount_cents < 0 THEN t.amount_cents ELSE 0 END), 0) AS out_cents,
                   COALESCE(SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents ELSE 0 END), 0) AS in_cents
            FROM transactions t WHERE {money_where}""",
        tuple(params),
    )
    return {"count": counted["n"], "out": row["out_cents"], "in": row["in_cents"], "net": row["in_cents"] + row["out_cents"]}


SPEND_GROUPS = ("weeks", "months", "years")


def _spend_bucket(day: date, group: str) -> date:
    if group == "weeks":
        return day - timedelta(days=day.weekday())
    if group == "months":
        return day.replace(day=1)
    return day.replace(month=1, day=1)


def _next_bucket(start: date, group: str) -> date:
    if group == "weeks":
        return start + timedelta(days=7)
    if group == "months":
        return date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    return date(start.year + 1, 1, 1)


def _income_scope(f: dict[str, Any]) -> dict[str, Any]:
    """The filters that apply to money coming in.

    Income is a fact about the time frame, not about the selection: a category, an account,
    a search or a kind filter narrows what was spent, never what the household earned — with
    a card's refunds as the denominator the share would read 62,788% instead of 29%. Only
    the date range defines which income belongs to the frame.
    """
    return {k: v for k, v in f.items() if k in ("from", "to")}


def spend_series(household_id: int, filters: dict | None = None,
                 group: str = "auto", max_buckets: int = 260,
                 split_income: bool = False) -> dict | None:
    """Spending per week/month/year for whatever the ledger filters currently select.

    With `split_income` the same buckets also carry money in, so the card can draw a
    cash-flow chart: income above the line, outflow below it. The income side is the
    period's income (unfiltered by category/search), which lets each bar state the
    filtered spend as a share of what came in.
    """
    f = filters or {}
    where, params = _where(household_id, f)
    rows = db.query_all(
        f"SELECT t.posted_on, t.amount_cents FROM transactions t"
        f" WHERE {where} AND t.is_transfer = 0 AND t.amount_cents < 0",
        tuple(params))

    earned_rows: list[Any] = []
    if split_income:
        iwhere, iparams = _where(household_id, _income_scope(f))
        earned_rows = db.query_all(
            f"SELECT t.posted_on, t.amount_cents FROM transactions t"
            f" WHERE {iwhere} AND t.is_transfer = 0 AND t.amount_cents > 0",
            tuple(iparams))
    if not rows and not earned_rows:
        return None

    days = [date.fromisoformat(row["posted_on"]) for row in rows]
    earned_days = [date.fromisoformat(row["posted_on"]) for row in earned_rows]
    # The axis covers every period the view has data for — the filtered spend and the
    # income it is being measured against, so months without a match still show income.
    all_days = days + earned_days
    span = (max(all_days) - min(all_days)).days
    if group not in SPEND_GROUPS:
        group = "years" if span > 730 else ("months" if span > 62 else "weeks")

    totals: dict[date, int] = {}
    inflow: dict[date, int] = {}
    for day, row in zip(days, rows):
        key = _spend_bucket(day, group)
        totals[key] = totals.get(key, 0) + abs(row["amount_cents"])
    for day, row in zip(earned_days, earned_rows):
        key = _spend_bucket(day, group)
        inflow[key] = inflow.get(key, 0) + row["amount_cents"]

    marks = set(totals) | set(inflow)
    start, end = min(marks), max(marks)
    buckets = []
    cursor = start
    while cursor <= end and len(buckets) < max_buckets:
        buckets.append({"day": cursor, "cents": totals.get(cursor, 0),
                        "in_cents": inflow.get(cursor, 0)})
        cursor = _next_bucket(cursor, group)

    peak = max([b["cents"] for b in buckets] + [b["in_cents"] for b in buckets])
    step = max(1, -(-len(buckets) // 6))   # about six labels, however many bars
    for i, bucket in enumerate(buckets):
        day = bucket["day"]
        if group == "weeks":
            bucket["label"] = f"{day.strftime('%b')} {day.day}"
        elif group == "months":
            bucket["label"] = day.strftime("%b '%y")
        else:
            bucket["label"] = str(day.year)
        bucket["pct"] = round(bucket["cents"] / peak * 100, 1) if peak else 0
        bucket["in_pct"] = round(bucket["in_cents"] / peak * 100, 1) if peak else 0
        # What the filtered selection took out of that period's income.
        bucket["pct_income"] = (round(bucket["cents"] / bucket["in_cents"] * 100, 1)
                                if bucket["in_cents"] else None)
        bucket["show_label"] = i % step == 0 or i == len(buckets) - 1

    spent = sum(b["cents"] for b in buckets)
    earned = sum(b["in_cents"] for b in buckets)
    return {
        "group": group,
        "buckets": buckets,
        "split": bool(split_income and earned > 0),
        "total_cents": spent,
        "income_cents": earned,
        "net_cents": earned - spent,
        "pct_income": round(spent / earned * 100, 1) if earned else None,
        "peak_cents": peak,
        "span_days": span,
        "count": sum(1 for r in rows if r["amount_cents"] < 0),
    }


def today_iso() -> str:
    return date.today().isoformat()
