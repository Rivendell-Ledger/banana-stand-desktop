"""Category rules and merchant aliases.

The invariant: a category set by a human is never overwritten. 'manual' wins over rules,
imports and syncs.
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import db, money

FIELD_LABELS = {
    "merchant": "Merchant",
    "amount": "Amount",
    "direction": "Direction",
    "account_id": "Account",
    "pending": "Status",
}

OPS = {
    "contains": "contains",
    "not_contains": "does not contain",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "equals": "is exactly",
    "regex": "matches regex",
    "gt": "is more than",
    "lt": "is less than",
}

FIRST_MATCH_WINS = True


# ---------------------------------------------------------------- matching
def parse_conditions(raw: list[dict]) -> list[dict]:
    clean: list[dict] = []
    for c in raw:
        field = (c.get("field") or "").strip()
        op = (c.get("op") or "").strip()
        value = str(c.get("value") or "").strip()
        if not field or not op or not value:
            continue
        if field not in FIELD_LABELS or op not in OPS:
            continue
        clean.append({"field": field, "op": op, "value": value[:120]})
    if not clean:
        raise ValueError("A rule needs at least one condition.")
    return clean


def _amount_cents(value: str) -> int | None:
    cents = money.parse_money(value)
    if cents is None:
        return None
    return abs(cents)


def matches(conditions: list[dict], txn: dict) -> bool:
    for c in conditions:
        if not _one(c, txn):
            return False
    return True


def _one(c: dict, txn: dict) -> bool:
    field, op, value = c["field"], c["op"], c["value"]

    if field == "merchant":
        hay = (txn.get("raw_merchant") or txn.get("merchant") or "").lower()
        needle = value.lower()
        if op == "contains":
            return needle in hay
        if op == "not_contains":
            return needle not in hay
        if op == "starts_with":
            return hay.startswith(needle)
        if op == "ends_with":
            return hay.endswith(needle)
        if op == "equals":
            return hay == needle
        if op == "regex":
            try:
                return re.search(value, hay, re.IGNORECASE) is not None
            except re.error:
                return False
        return False

    if field == "amount":
        target = _amount_cents(value)
        if target is None:
            return False
        amount = abs(int(txn["amount_cents"]))
        if op == "gt":
            return amount > target
        if op == "lt":
            return amount < target
        if op == "equals":
            return amount == target
        return False

    if field == "direction":
        want = value.lower()
        if want in ("in", "income", "inflow"):
            return int(txn["amount_cents"]) > 0
        if want in ("out", "expense", "outflow"):
            return int(txn["amount_cents"]) < 0
        return False

    if field == "account_id":
        return str(txn["account_id"]) == value

    if field == "pending":
        want = value.lower() in ("1", "yes", "true", "pending")
        return bool(txn["is_pending"]) == want

    return False


# ---------------------------------------------------------------- CRUD
def list_rules(household_id: int, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM category_rules WHERE household_id = ?"
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY priority, id"
    out = []
    for row in db.query_all(sql, (household_id,)):
        d = dict(row)
        try:
            d["conditions"] = json.loads(d["conditions_json"])
        except ValueError:
            d["conditions"] = []
        out.append(d)
    return out


def get_rule(household_id: int, rule_id: int) -> dict | None:
    row = db.query_one(
        "SELECT * FROM category_rules WHERE id = ? AND household_id = ?", (rule_id, household_id)
    )
    if row is None:
        return None
    d = dict(row)
    d["conditions"] = json.loads(d["conditions_json"])
    return d


def create_rule(household_id: int, name: str, conditions: list[dict], action_type: str,
                action_value: str | None, apply_to: str = "uncategorized") -> int:
    now = db.utcnow()
    max_priority = db.query_one(
        "SELECT COALESCE(MAX(priority), 0) AS p FROM category_rules WHERE household_id = ?", (household_id,)
    )["p"]
    with db.session_scope() as conn:
        cur = conn.execute(
            """INSERT INTO category_rules(household_id, name, enabled, priority, conditions_json,
                   action_type, action_value, apply_to, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (household_id, name.strip()[:80], 1, max_priority + 10, json.dumps(conditions),
             action_type, action_value, apply_to, now, now),
        )
        return cur.lastrowid


def update_rule(household_id: int, rule_id: int, **fields) -> None:
    rule = get_rule(household_id, rule_id)
    if rule is None:
        raise ValueError("No such rule.")
    allowed = {"name", "enabled", "conditions_json", "action_type", "action_value", "apply_to", "priority"}
    sets, params = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return
    params.extend([db.utcnow(), rule_id, household_id])
    db.execute(f"UPDATE category_rules SET {', '.join(sets)}, updated_at = ? WHERE id = ? AND household_id = ?",
               tuple(params))


def delete_rule(household_id: int, rule_id: int) -> None:
    db.execute("DELETE FROM category_rules WHERE id = ? AND household_id = ?", (rule_id, household_id))


# ---------------------------------------------------------------- applying
def _candidates(household_id: int, apply_to: str) -> list[dict]:
    sql = """SELECT t.* FROM transactions t
             WHERE t.household_id = ? AND t.deleted_at IS NULL AND t.category_source != 'manual'"""
    params: list[Any] = [household_id]
    if apply_to == "uncategorized":
        sql += """ AND (t.category_id IS NULL OR t.category_id IN
                        (SELECT id FROM categories WHERE household_id = ? AND name = 'Uncategorized'))"""
        params.append(household_id)
    return [dict(r) for r in db.query_all(sql, tuple(params))]


def preview(household_id: int, conditions: list[dict], apply_to: str = "uncategorized",
            sample: int = 5) -> dict:
    hits = [t for t in _candidates(household_id, apply_to) if matches(conditions, t)]
    return {"count": len(hits), "sample": hits[:sample]}


def _apply_action(household_id: int, rule: dict, txn_id: int) -> None:
    action = rule["action_type"]
    value = rule.get("action_value")
    now = db.utcnow()
    if action == "category" and value:
        # Filing something under the Transfer category *is* declaring it a transfer.
        db.execute(
            "UPDATE transactions SET category_id = ?, category_source = 'rule', is_transfer = CASE"
            " WHEN (SELECT kind FROM categories WHERE id = ?) = 'transfer' THEN 1 ELSE is_transfer END,"
            " updated_at = ? WHERE id = ?",
            (int(value), int(value), now, txn_id),
        )
    elif action == "tag" and value:
        row = db.query_one("SELECT id FROM tags WHERE household_id = ? AND name = ? COLLATE NOCASE",
                           (household_id, value))
        tag_id = row["id"] if row else db.execute(
            "INSERT INTO tags(household_id, name) VALUES (?,?)", (household_id, value)
        )
        db.execute("INSERT OR IGNORE INTO transaction_tags(transaction_id, tag_id) VALUES (?,?)",
                   (txn_id, tag_id))
    elif action == "transfer":
        db.execute("UPDATE transactions SET is_transfer = 1, updated_at = ? WHERE id = ?", (now, txn_id))
    elif action == "exclude":
        db.execute("UPDATE transactions SET excluded = 1, updated_at = ? WHERE id = ?", (now, txn_id))


def apply_to_transaction(household_id: int, txn_id: int) -> int | None:
    """Run the rule list over one transaction. Returns the rule id that fired."""
    txn = db.query_one("SELECT * FROM transactions WHERE id = ? AND household_id = ?", (txn_id, household_id))
    if txn is None or txn["category_source"] == "manual":
        return None
    for rule in list_rules(household_id, enabled_only=True):
        if matches(rule["conditions"], dict(txn)):
            _apply_action(household_id, rule, txn_id)
            db.execute(
                "UPDATE category_rules SET match_count = match_count + 1, last_run_at = ? WHERE id = ?",
                (db.utcnow(), rule["id"]),
            )
            if FIRST_MATCH_WINS:
                return rule["id"]
    return None


def run_history(household_id: int, rule_id: int) -> int:
    rule = get_rule(household_id, rule_id)
    if rule is None:
        raise ValueError("No such rule.")
    hits = [t for t in _candidates(household_id, rule["apply_to"]) if matches(rule["conditions"], t)]
    for txn in hits:
        _apply_action(household_id, rule, txn["id"])
    db.execute(
        "UPDATE category_rules SET match_count = match_count + ?, last_run_at = ? WHERE id = ?",
        (len(hits), db.utcnow(), rule_id),
    )
    return len(hits)


def rule_summary(rule: dict) -> str:
    parts = []
    for c in rule["conditions"]:
        parts.append(f"{FIELD_LABELS.get(c['field'], c['field'])} {OPS.get(c['op'], c['op'])} “{c['value']}”")
    return " and ".join(parts)


# ---------------------------------------------------------------- aliases
_PREFIX_NOISE = re.compile(r"\b(sq|tst|pos|ach|ppd|web|tel|debit|credit|purchase|payment|pmt|ref)\b[\s*]*", re.I)
_STAR_SUFFIX = re.compile(r"[*#]\s*[0-9A-Za-z]{3,}\s*$")
_TRAILING_NOISE = re.compile(r"[\s#*\-]+\d{2,}.*$")


def normalize_merchant(raw: str) -> str:
    text = (raw or "").lower().strip()
    text = _PREFIX_NOISE.sub(" ", text)
    text = _STAR_SUFFIX.sub("", text)
    text = _TRAILING_NOISE.sub("", text)
    text = re.sub(r"[^a-z0-9&'\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:60]


def alias_for(household_id: int, raw: str) -> str | None:
    row = db.query_one(
        "SELECT alias FROM merchant_aliases WHERE household_id = ? AND pattern = ?",
        (household_id, normalize_merchant(raw)),
    )
    return row["alias"] if row else None


def set_alias(household_id: int, raw: str, alias: str) -> None:
    pattern = normalize_merchant(raw)
    if not pattern or not alias.strip():
        raise ValueError("Both the raw merchant and the alias are required.")
    db.execute(
        "INSERT INTO merchant_aliases(household_id, pattern, alias, created_at) VALUES (?,?,?,?)"
        " ON CONFLICT(household_id, pattern) DO UPDATE SET alias = excluded.alias",
        (household_id, pattern, alias.strip()[:60], db.utcnow()),
    )


def list_aliases(household_id: int) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM merchant_aliases WHERE household_id = ? ORDER BY alias COLLATE NOCASE", (household_id,)
    )
    return [dict(r) for r in rows]


def apply_alias_to_history(household_id: int) -> int:
    """Rewrite merchant names for rows whose raw string matches an alias."""
    changed = 0
    for row in db.query_all(
        """SELECT t.id, t.merchant, t.raw_merchant FROM transactions t
           WHERE t.household_id = ? AND t.deleted_at IS NULL""",
        (household_id,),
    ):
        raw = row["raw_merchant"] or row["merchant"]
        alias = alias_for(household_id, raw)
        if alias and alias != row["merchant"]:
            db.execute(
                "UPDATE transactions SET merchant = ?, raw_merchant = COALESCE(raw_merchant, ?), updated_at = ?"
                " WHERE id = ?",
                (alias, raw, db.utcnow(), row["id"]),
            )
            changed += 1
    return changed
