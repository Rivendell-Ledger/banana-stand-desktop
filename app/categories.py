"""Category taxonomy: seeding, editing, merging, archiving."""
from __future__ import annotations

from . import db

# (name, kind, children)
DEFAULT_TREE: list[tuple[str, str, list[str]]] = [
    ("Housing", "expense", ["Rent / Mortgage", "Utilities", "Home maintenance"]),
    ("Food", "expense", ["Groceries", "Dining out", "Coffee"]),
    ("Transport", "expense", ["Gas", "Auto", "Parking & tolls"]),
    ("Shopping", "expense", ["Household", "Clothing", "Electronics"]),
    ("Kids", "expense", ["School", "Activities", "Childcare"]),
    ("Health", "expense", ["Medical", "Pharmacy", "Fitness"]),
    ("Subscriptions", "expense", []),
    ("Insurance", "expense", []),
    ("Travel", "expense", []),
    ("Fees & Interest", "expense", []),
    ("Taxes", "expense", []),
]

MINIMAL = ["Uncategorized", "Income", "Transfer"]


def ensure_taxonomy(household_id: int) -> dict[str, int]:
    """Create any missing default categories. Safe to call on every page load."""
    existing = {
        (r["name"].lower(), r["parent_id"]): r["id"]
        for r in db.query_all("SELECT id, name, parent_id FROM categories WHERE household_id = ?", (household_id,))
    }
    now = db.utcnow()
    order = 0
    with db.session_scope() as conn:
        # Make sure the three system buckets exist (Phase 2 databases already have them).
        for name in MINIMAL:
            if (name.lower(), None) not in existing:
                kind = "income" if name == "Income" else ("transfer" if name == "Transfer" else "expense")
                cur = conn.execute(
                    "INSERT INTO categories(household_id, name, kind, is_system, sort_order, created_at)"
                    " VALUES (?,?,?,1,?,?)",
                    (household_id, name, kind, order, now),
                )
                existing[(name.lower(), None)] = cur.lastrowid
            order += 1

        for name, kind, children in DEFAULT_TREE:
            key = (name.lower(), None)
            if key not in existing:
                cur = conn.execute(
                    "INSERT INTO categories(household_id, name, kind, sort_order, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (household_id, name, kind, order, now),
                )
                existing[key] = cur.lastrowid
            parent_id = existing[key]
            order += 1
            for child in children:
                ckey = (child.lower(), parent_id)
                if ckey not in existing:
                    cur = conn.execute(
                        "INSERT INTO categories(household_id, name, kind, parent_id, sort_order, created_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (household_id, child, kind, parent_id, order, now),
                    )
                    existing[ckey] = cur.lastrowid
                order += 1

        # Income children live under the system Income category.
        income_id = existing[("income", None)]
        for child in ("Paycheck", "Interest & dividends", "Other income"):
            ckey = (child.lower(), income_id)
            if ckey not in existing:
                conn.execute(
                    "INSERT INTO categories(household_id, name, kind, parent_id, sort_order, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (household_id, child, "income", income_id, order, now),
                )
                order += 1
    return {
        r["name"].lower(): r["id"]
        for r in db.query_all("SELECT id, name FROM categories WHERE household_id = ?", (household_id,))
    }


def list_categories(household_id: int, include_archived: bool = False) -> list[dict]:
    sql = """SELECT c.*,
                    (SELECT COUNT(*) FROM transactions t
                       WHERE t.category_id = c.id AND t.deleted_at IS NULL) AS txn_count,
                    (SELECT COUNT(*) FROM transaction_splits s
                       JOIN transactions t2 ON t2.id = s.transaction_id
                       WHERE s.category_id = c.id AND t2.deleted_at IS NULL) AS split_count
             FROM categories c WHERE c.household_id = ?"""
    if not include_archived:
        sql += " AND c.archived_at IS NULL"
    sql += " ORDER BY c.sort_order, c.name COLLATE NOCASE"
    rows = db.query_all(sql, (household_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["uses"] = d["txn_count"] + d["split_count"]
        out.append(d)
    return out


def tree(household_id: int) -> list[dict]:
    cats = list_categories(household_id)
    by_id = {c["id"]: c for c in cats}
    roots: list[dict] = []
    for c in cats:
        c["children"] = []
    for c in cats:
        parent = by_id.get(c["parent_id"])
        if parent is not None:
            parent["children"].append(c)
        else:
            roots.append(c)
    return roots


def get_category(household_id: int, category_id: int) -> dict | None:
    row = db.query_one(
        "SELECT * FROM categories WHERE id = ? AND household_id = ?", (category_id, household_id)
    )
    return dict(row) if row else None


def create(household_id: int, name: str, kind: str = "expense", parent_id: int | None = None,
           color: str | None = None) -> int:
    name = name.strip()[:60]
    if not name:
        raise ValueError("A category needs a name.")
    now = db.utcnow()
    max_order = db.query_one(
        "SELECT COALESCE(MAX(sort_order), 0) AS n FROM categories WHERE household_id = ?", (household_id,)
    )["n"]
    with db.session_scope() as conn:
        cur = conn.execute(
            "INSERT INTO categories(household_id, name, kind, parent_id, color, sort_order, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (household_id, name, kind, parent_id, color, max_order + 1, now),
        )
        return cur.lastrowid


def update(household_id: int, category_id: int, name: str | None = None, kind: str | None = None,
           parent_id: int | None = None, color: str | None = None) -> None:
    row = get_category(household_id, category_id)
    if row is None:
        raise ValueError("No such category.")
    if row["is_system"] and (name or kind):
        raise ValueError("System categories cannot be renamed or retyped.")
    db.execute(
        "UPDATE categories SET name = ?, kind = ?, parent_id = ?, color = ? WHERE id = ? AND household_id = ?",
        (name or row["name"], kind or row["kind"], parent_id, color, category_id, household_id),
    )


def merge(household_id: int, source_id: int, target_id: int) -> int:
    """Move everything from source into target, then archive source."""
    if source_id == target_id:
        raise ValueError("Pick two different categories.")
    source = get_category(household_id, source_id)
    target = get_category(household_id, target_id)
    if source is None or target is None:
        raise ValueError("No such category.")
    if source["is_system"]:
        raise ValueError("System categories cannot be merged away.")
    now = db.utcnow()
    with db.session_scope() as conn:
        moved = conn.execute(
            "UPDATE transactions SET category_id = ?, category_source = 'manual' WHERE category_id = ? AND household_id = ?",
            (target_id, source_id, household_id),
        ).rowcount
        conn.execute("UPDATE transaction_splits SET category_id = ? WHERE category_id = ?", (target_id, source_id))
        conn.execute(
            "UPDATE category_rules SET action_value = ? WHERE household_id = ? AND action_type = 'category' AND action_value = ?",
            (str(target_id), household_id, str(source_id)),
        )
        conn.execute("UPDATE categories SET parent_id = ? WHERE parent_id = ?", (target_id, source_id))
        conn.execute("UPDATE categories SET archived_at = ?, parent_id = NULL WHERE id = ?", (now, source_id))
    return moved


def archive(household_id: int, category_id: int) -> None:
    row = get_category(household_id, category_id)
    if row is None:
        raise ValueError("No such category.")
    if row["is_system"]:
        raise ValueError("System categories cannot be archived.")
    db.execute("UPDATE categories SET archived_at = ? WHERE id = ? AND household_id = ?",
               (db.utcnow(), category_id, household_id))


def restore(household_id: int, category_id: int) -> None:
    db.execute("UPDATE categories SET archived_at = NULL WHERE id = ? AND household_id = ?",
               (category_id, household_id))


def name_map(household_id: int) -> dict[str, int]:
    return {
        r["name"].lower(): r["id"]
        for r in db.query_all("SELECT id, name FROM categories WHERE household_id = ?", (household_id,))
    }


# ---------------------------------------------------------------- budget visibility
def set_budget_hidden(household_id: int, category_id: int, hidden: bool) -> None:
    """Keep a category out of the Budget tab. It stays in the taxonomy, so the ledger's
    filters and the reports still see it."""
    db.execute(
        "UPDATE categories SET hide_from_budget = ? WHERE id = ? AND household_id = ?",
        (1 if hidden else 0, category_id, household_id),
    )


def hidden_from_budget(household_id: int) -> list[dict]:
    return [dict(r) for r in db.query_all(
        """SELECT id, name FROM categories
           WHERE household_id = ? AND hide_from_budget = 1 AND archived_at IS NULL
           ORDER BY name COLLATE NOCASE""",
        (household_id,),
    )]
