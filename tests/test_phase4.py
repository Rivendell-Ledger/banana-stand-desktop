"""Phase 4 acceptance: taxonomy, rules, aliases, CSV import and dedupe."""
from __future__ import annotations

import os
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import categories, config, csv_import, db, rules
from app.main import app

PASSWORD = "correct horse battery staple"

CHASE_CSV = """Transaction Date,Post Date,Description,Category,Type,Amount,Memo
11/10/2026,11/11/2026,SQ *BLUE BOTTLE 4471,Food & Drink,Sale,-6.42,
11/09/2026,11/10/2026,PAYROLL DEPOSIT,Income,Deposit,5000.00,
11/08/2026,11/09/2026,WHOLEFDS MKT 10234,Groceries,Sale,-142.38,weekly shop
"""

CHASE_CSV_V2 = CHASE_CSV + "11/07/2026,11/08/2026,SHELL OIL 57444,Gas,Sale,-58.20,\n"

DEBIT_CREDIT_CSV = """Date,Description,Debit,Credit
11/10/2026,STARBUCKS,6.42,
11/09/2026,PAYROLL,,5000.00
"""


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field"
    return m.group(1)


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    db.reset_connections()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(db.config.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(csv_import, "imports_dir", lambda: config.DATA_DIR / "imports")
    (config.DATA_DIR / "imports").mkdir(parents=True, exist_ok=True)
    db.migrate()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def sign_up(client):
    page = client.get("/setup")
    client.post("/setup", data={"csrf": csrf_of(page.text), "household": "The Banana Stand",
                                "name": "Alex", "email": "alex@example.com",
                                "password": PASSWORD, "password2": PASSWORD}, follow_redirects=False)
    page = client.get("/login")
    client.post("/login", data={"csrf": csrf_of(page.text), "email": "alex@example.com",
                                "password": PASSWORD}, follow_redirects=False)
    return client


def add_account(client, name="Checking"):
    page = client.get("/accounts/new")
    client.post("/accounts", data={"csrf": csrf_of(page.text), "type": "checking", "name": name,
                                   "institution": "Chase", "mask": "4821", "balance": "5000.00",
                                   "credit_limit": "", "include_net": "on", "include_budget": "on"}, follow_redirects=False)
    return db.query_one("SELECT id FROM accounts WHERE name = ?", (name,))["id"]


def household_id():
    return db.query_one("SELECT id AS household_id FROM households")["household_id"]


def add_txn(hid, account_id, merchant="Starbucks", cents=-500, category_id=None,
            source="import", day="2026-11-10", raw=None):
    return db.execute(
        """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant,
               raw_merchant, category_id, category_source, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (hid, account_id, day, cents, merchant, raw or merchant, category_id, source,
         db.utcnow(), db.utcnow()),
    )


# ---------------------------------------------------------------- taxonomy
def test_taxonomy_is_idempotent(client):
    sign_up(client)
    hid = household_id()
    first = categories.ensure_taxonomy(hid)
    count = len(categories.list_categories(hid))
    second = categories.ensure_taxonomy(hid)
    assert len(categories.list_categories(hid)) == count
    assert count > 20
    assert "groceries" in second and second["groceries"] == first["groceries"]


def test_category_tree_has_children(client):
    sign_up(client)
    hid = household_id()
    categories.ensure_taxonomy(hid)
    tree = categories.tree(hid)
    food = next(c for c in tree if c["name"] == "Food")
    assert {c["name"] for c in food["children"]} >= {"Groceries", "Dining out"}


def test_create_merge_and_archive(client):
    sign_up(client)
    acc = add_account(client)
    hid = household_id()
    cats = categories.ensure_taxonomy(hid)
    gardens = categories.create(hid, "Garden")
    txn = add_txn(hid, acc, category_id=gardens)

    moved = categories.merge(hid, gardens, cats["home maintenance"])
    assert moved == 1
    assert db.query_one("SELECT category_id FROM transactions WHERE id = ?", (txn,))["category_id"] == cats["home maintenance"]
    assert categories.get_category(hid, gardens)["archived_at"] is not None

    with pytest.raises(ValueError):
        categories.merge(hid, cats["uncategorized"], cats["income"])


def test_merge_rewrites_rule_actions(client):
    sign_up(client)
    hid = household_id()
    cats = categories.ensure_taxonomy(hid)
    custom = categories.create(hid, "Coffee habit")
    rid = rules.create_rule(hid, "Coffee", [{"field": "merchant", "op": "contains", "value": "starbucks"}],
                            "category", str(custom))
    categories.merge(hid, custom, cats["coffee"])
    assert rules.get_rule(hid, rid)["action_value"] == str(cats["coffee"])


# ---------------------------------------------------------------- rules
def test_rule_matching(client):
    sign_up(client)
    acc = add_account(client)
    hid = household_id()
    cats = categories.ensure_taxonomy(hid)
    rid = rules.create_rule(hid, "Coffee shops",
                            [{"field": "merchant", "op": "contains", "value": "starbucks"}],
                            "category", str(cats["coffee"]))
    txn = add_txn(hid, acc, merchant="Starbucks #123", cents=-642, source="import", raw="SQ *STARBUCKS 123")
    fired = rules.apply_to_transaction(hid, txn)
    assert fired == rid
    row = db.query_one("SELECT category_id, category_source FROM transactions WHERE id = ?", (txn,))
    assert row["category_id"] == cats["coffee"]
    assert row["category_source"] == "rule"


def test_manual_category_wins(client):
    sign_up(client)
    acc = add_account(client)
    hid = household_id()
    cats = categories.ensure_taxonomy(hid)
    rules.create_rule(hid, "Coffee", [{"field": "merchant", "op": "contains", "value": "starbucks"}],
                      "category", str(cats["coffee"]))
    txn = add_txn(hid, acc, merchant="Starbucks", source="manual", category_id=cats["dining out"])
    assert rules.apply_to_transaction(hid, txn) is None
    assert db.query_one("SELECT category_id FROM transactions WHERE id = ?", (txn,))["category_id"] == cats["dining out"]


def test_rule_preview_and_history_apply(client):
    sign_up(client)
    acc = add_account(client)
    hid = household_id()
    cats = categories.ensure_taxonomy(hid)
    for i in range(4):
        add_txn(hid, acc, merchant=f"Starbucks {i}", source="import")
    add_txn(hid, acc, merchant="Shell", source="import")
    rid = rules.create_rule(hid, "Coffee", [{"field": "merchant", "op": "contains", "value": "starbucks"}],
                            "category", str(cats["coffee"]))
    preview = rules.preview(hid, rules.get_rule(hid, rid)["conditions"])
    assert preview["count"] == 4
    assert rules.run_history(hid, rid) == 4
    assert db.query_one("SELECT COUNT(*) AS n FROM transactions WHERE category_id = ?",
                        (cats["coffee"],))["n"] == 4


def test_amount_and_direction_conditions(client):
    sign_up(client)
    acc = add_account(client)
    hid = household_id()
    cats = categories.ensure_taxonomy(hid)
    add_txn(hid, acc, merchant="Rent", cents=-200000, source="import")
    add_txn(hid, acc, merchant="Coffee", cents=-500, source="import")
    add_txn(hid, acc, merchant="Refund", cents=2000, source="import")
    cond = [{"field": "amount", "op": "gt", "value": "100"}]
    assert rules.preview(hid, cond)["count"] == 1
    cond2 = [{"field": "direction", "op": "equals", "value": "in"}]
    assert rules.preview(hid, cond2)["count"] == 1
    cond3 = [{"field": "merchant", "op": "regex", "value": "^cof"}]
    assert rules.preview(hid, cond3)["count"] == 1


def test_rules_page_renders(client):
    sign_up(client)
    hid = household_id()
    cats = categories.ensure_taxonomy(hid)
    rules.create_rule(hid, "Coffee", [{"field": "merchant", "op": "contains", "value": "starbucks"}],
                      "category", str(cats["coffee"]))
    page = client.get("/rules")
    assert page.status_code == 200
    assert "Coffee" in page.text
    assert "Merchant aliases" in page.text


# ---------------------------------------------------------------- aliases
def test_merchant_normalisation():
    assert rules.normalize_merchant("SQ *BLUE BOTTLE 4471") == "blue bottle"
    assert rules.normalize_merchant("AMZN Mktp US*2H4KL9") == "amzn mktp us"
    assert rules.normalize_merchant("Whole Foods Market #10234") == "whole foods market"


def test_alias_applies_to_history(client):
    sign_up(client)
    acc = add_account(client)
    hid = household_id()
    txn = add_txn(hid, acc, merchant="SQ *BLUE BOTTLE 4471", raw="SQ *BLUE BOTTLE 4471")
    rules.set_alias(hid, "SQ *BLUE BOTTLE 4471", "Blue Bottle")
    changed = rules.apply_alias_to_history(hid)
    assert changed == 1
    row = db.query_one("SELECT merchant, raw_merchant FROM transactions WHERE id = ?", (txn,))
    assert row["merchant"] == "Blue Bottle"
    assert row["raw_merchant"] == "SQ *BLUE BOTTLE 4471"


# ---------------------------------------------------------------- CSV
def test_csv_detection_and_guessing():
    token = csv_import.save_upload("chase.csv", CHASE_CSV.encode())
    data = csv_import.read_rows(token)
    assert data["has_header"] is True
    assert data["columns"] == 7
    guess = csv_import.guess_columns(data["header"], data["rows"])
    assert guess["date_col"] == 0
    assert guess["desc_col"] == 2
    assert guess["amount_col"] == 5
    assert guess["category_col"] == 3
    assert csv_import.guess_date_format(data["rows"], 0) == "%m/%d/%Y"


def test_csv_parse_row_signs():
    token = csv_import.save_upload("chase.csv", CHASE_CSV.encode())
    data = csv_import.read_rows(token)
    mapping = {"date_col": 0, "desc_col": 2, "amount_col": 5, "date_format": "%m/%d/%Y"}
    out = csv_import.parse_row(data["rows"][0], mapping)
    money_in = csv_import.parse_row(data["rows"][1], mapping)
    assert out["amount_cents"] == -642
    assert out["posted_on"] == "2026-11-10"
    assert money_in["amount_cents"] == 500000


def test_debit_credit_columns():
    token = csv_import.save_upload("dc.csv", DEBIT_CREDIT_CSV.encode())
    data = csv_import.read_rows(token)
    mapping = {"date_col": 0, "desc_col": 1, "debit_col": 2, "credit_col": 3,
               "date_format": "%m/%d/%Y"}
    spend = csv_import.parse_row(data["rows"][0], mapping)
    income = csv_import.parse_row(data["rows"][1], mapping)
    assert spend["amount_cents"] == -642
    assert income["amount_cents"] == 500000


def test_full_import_flow_and_reimport(client):
    sign_up(client)
    acc = add_account(client)
    page = client.get("/import")
    token_csrf = csrf_of(page.text)

    r = client.post("/import/upload", data={"csrf": token_csrf, "account_id": str(acc)},
                    files={"file": ("chase.csv", CHASE_CSV, "text/csv")}, follow_redirects=False)
    assert r.status_code == 200
    assert "Map the columns" in r.text
    token = re.search(r'name="token" value="([0-9a-f]{32})"', r.text).group(1)

    mapping = {"csrf": token_csrf, "token": token, "account_id": str(acc), "filename": "chase.csv",
               "date_col": "0", "desc_col": "2", "amount_col": "5", "date_format": "%m/%d/%Y",
               "profile_name": "Chase test"}
    preview = client.post("/import/preview", data=mapping)
    assert preview.status_code == 200
    assert "3</strong> new" in preview.text or "3 new" in preview.text

    commit = client.post("/import/commit", data=mapping, follow_redirects=False)
    assert commit.status_code == 303
    assert "imported=3" in commit.headers["location"]
    assert db.count("transactions") == 3

    # Re-import the identical file: zero new rows.
    page = client.get("/import")
    r = client.post("/import/upload", data={"csrf": csrf_of(page.text), "account_id": str(acc)},
                    files={"file": ("chase.csv", CHASE_CSV, "text/csv")}, follow_redirects=False)
    token2 = re.search(r'name="token" value="([0-9a-f]{32})"', r.text).group(1)
    mapping2 = dict(mapping, token=token2, csrf=csrf_of(page.text))
    again = client.post("/import/commit", data=mapping2, follow_redirects=False)
    assert "imported=0" in again.headers["location"]
    assert db.count("transactions") == 3


def test_overlapping_import_adds_only_the_tail(client):
    sign_up(client)
    acc = add_account(client)
    hid = household_id()
    mapping = {"date_col": 0, "desc_col": 2, "amount_col": 5, "date_format": "%m/%d/%Y"}

    t1 = csv_import.save_upload("a.csv", CHASE_CSV.encode())
    first = csv_import.commit(t1, hid, acc, mapping)
    assert first["imported"] == 3

    t2 = csv_import.save_upload("b.csv", CHASE_CSV_V2.encode())
    second = csv_import.commit(t2, hid, acc, mapping)
    assert second["imported"] == 1
    assert second["duplicates"] == 3


def test_import_applies_rules_and_aliases(client):
    sign_up(client)
    acc = add_account(client)
    hid = household_id()
    cats = categories.ensure_taxonomy(hid)
    rules.create_rule(hid, "Coffee", [{"field": "merchant", "op": "contains", "value": "blue bottle"}],
                      "category", str(cats["coffee"]))
    rules.set_alias(hid, "SQ *BLUE BOTTLE 4471", "Blue Bottle")

    token = csv_import.save_upload("c.csv", CHASE_CSV.encode())
    result = csv_import.commit(token, hid, acc,
                               {"date_col": 0, "desc_col": 2, "amount_col": 5, "date_format": "%m/%d/%Y"})
    assert result["imported"] == 3
    coffee = db.query_one("SELECT * FROM transactions WHERE raw_merchant LIKE '%BLUE BOTTLE%'")
    assert coffee["merchant"] == "Blue Bottle"
    assert coffee["category_id"] == cats["coffee"]
    assert coffee["category_source"] == "rule"


def test_csv_category_from_file_is_respected(client):
    sign_up(client)
    acc = add_account(client)
    hid = household_id()
    cats = categories.ensure_taxonomy(hid)
    token = csv_import.save_upload("d.csv", CHASE_CSV.encode())
    csv_import.commit(token, hid, acc,
                      {"date_col": 0, "desc_col": 2, "amount_col": 5,
                       "category_col": 3, "date_format": "%m/%d/%Y"})
    row = db.query_one("SELECT * FROM transactions WHERE raw_merchant LIKE '%WHOLEFDS%'")
    assert row["category_id"] == cats["groceries"]
    assert row["category_source"] == "manual"


def test_import_page_renders(client):
    sign_up(client)
    add_account(client)
    page = client.get("/import")
    assert page.status_code == 200
    assert "Import a CSV" in page.text
