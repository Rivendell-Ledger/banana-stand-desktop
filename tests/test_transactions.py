"""Phase 2 acceptance: transactions, filters, inline edits, tags, splits, bulk, undo."""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db, money, txns
from app.main import app

PASSWORD = "correct horse battery staple"


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field"
    return m.group(1)


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_connections()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(db.config.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
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


def add_account(client, name="Checking", typ="checking", balance="5000.00"):
    page = client.get("/accounts/new")
    client.post("/accounts", data={"csrf": csrf_of(page.text), "type": typ, "name": name,
                                   "institution": "Chase", "mask": "4821", "balance": balance,
                                   "credit_limit": "", "include_net": "on", "include_budget": "on"}, follow_redirects=False)
    return db.query_one("SELECT id FROM accounts WHERE name = ?", (name,))["id"]


def add_txn(client, account_id, merchant="Starbucks", amount="6.42", direction="out", day=None, category=""):
    page = client.get("/transactions")
    return client.post("/transactions", data={
        "csrf": csrf_of(page.text), "account_id": str(account_id),
        "posted_on": day or date.today().isoformat(), "direction": direction,
        "amount": amount, "merchant": merchant, "category_id": category,
    }, follow_redirects=False)


def seed_many(household_id, account_id, category_id, n=500):
    today = date.today()
    now = db.utcnow()
    merchants = ["Starbucks", "Trader Joe's", "Shell", "Amazon", "Netflix", "Kroger"]
    with db.session_scope() as conn:
        for i in range(n):
            conn.execute(
                """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant,
                       category_id, is_pending, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (household_id, account_id, (today - timedelta(days=i % 120)).isoformat(),
                 -(100 + i * 7), merchants[i % len(merchants)] + f" {i}",
                 category_id if i % 5 else None, 1 if i % 11 == 0 else 0, now, now),
            )


# ---------------------------------------------------------------- writes
def test_quick_add_out_and_in(client):
    sign_up(client)
    acc = add_account(client)
    add_txn(client, acc, amount="6.42", direction="out")
    add_txn(client, acc, merchant="Payroll", amount="2,000.00", direction="in")
    rows = db.query_all("SELECT merchant, amount_cents FROM transactions ORDER BY id")
    assert rows[0]["amount_cents"] == -642
    assert rows[1]["amount_cents"] == 200000
    home = client.get("/transactions")
    assert "Starbucks" in home.text and "Payroll" in home.text


def test_quick_add_rejects_bad_input(client):
    sign_up(client)
    acc = add_account(client)
    r = add_txn(client, acc, amount="abc")
    assert r.status_code == 303
    assert "error=fields" in r.headers["location"]
    assert db.count("transactions") == 0


def test_field_edit_json(client):
    sign_up(client)
    acc = add_account(client)
    add_txn(client, acc)
    txn_id = db.query_one("SELECT id FROM transactions")["id"]
    page = client.get("/transactions")
    csrf = csrf_of(page.text)
    r = client.post(f"/transactions/{txn_id}/field",
                    data={"csrf": csrf, "field": "merchant", "value": "Blue Bottle"},
                    headers={"X-Requested-With": "fetch"})
    assert r.json() == {"ok": True, "display": "Blue Bottle", "kind": None}
    assert db.query_one("SELECT merchant FROM transactions")["merchant"] == "Blue Bottle"

    r = client.post(f"/transactions/{txn_id}/field",
                    data={"csrf": csrf, "field": "amount_cents", "value": "-12.50"},
                    headers={"X-Requested-With": "fetch"})
    assert r.json()["kind"] == "neg"
    assert db.query_one("SELECT amount_cents FROM transactions")["amount_cents"] == -1250

    bad = client.post(f"/transactions/{txn_id}/field",
                      data={"csrf": csrf, "field": "amount_cents", "value": "nope"},
                      headers={"X-Requested-With": "fetch"})
    assert bad.status_code == 400


def test_update_route(client):
    sign_up(client)
    acc = add_account(client)
    add_txn(client, acc)
    txn_id = db.query_one("SELECT id FROM transactions")["id"]
    page = client.get(f"/transactions/{txn_id}")
    r = client.post(f"/transactions/{txn_id}/update", data={
        "csrf": csrf_of(page.text), "merchant": "Whole Foods", "posted_on": "2026-11-01",
        "amount": "-88.10", "category_id": "", "notes": "stock up",
        "is_pending": "on", "excluded": "", "is_transfer": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    row = db.query_one("SELECT * FROM transactions")
    assert row["merchant"] == "Whole Foods"
    assert row["posted_on"] == "2026-11-01"
    assert row["amount_cents"] == -8810
    assert row["is_pending"] == 1
    assert row["notes"] == "stock up"


# ---------------------------------------------------------------- filters
def test_filters_and_search(client):
    sign_up(client)
    acc = add_account(client)
    hid = db.query_one("SELECT household_id FROM accounts")["household_id"]
    cats = txns.ensure_system_categories(hid)
    seed_many(hid, acc, cats["Uncategorized"], 500)

    listing = client.get("/transactions")
    assert listing.status_code == 200
    assert "500" in listing.text

    # FTS prefix search
    hit = client.get("/transactions?q=starb")
    assert "Starbucks" in hit.text
    res = txns.list_transactions(hid, {"q": "starb"}, per_page=200)
    assert res["total"] == len([i for i in range(500) if i % 6 == 0])
    assert all(r["merchant"].startswith("Starbucks") for r in res["rows"])

    # account filter
    other = add_account(client, name="Savings", typ="savings")
    only_other = client.get(f"/transactions?account_id={other}")
    assert "Nothing matches" in only_other.text

    # kind + date window
    recent = client.get(f"/transactions?from={(date.today() - timedelta(days=6)).isoformat()}")
    assert "Nothing matches" not in recent.text

    # needs a category
    uncat = client.get("/transactions?category_id=none")
    assert "Nothing matches" not in uncat.text
    assert txns.list_transactions(hid, {"category_id": "none"}, per_page=200)["total"] == 100


def test_pending_filter(client):
    sign_up(client)
    acc = add_account(client)
    add_txn(client, acc, merchant="Pending Thing")
    txn_id = db.query_one("SELECT id FROM transactions")["id"]
    db.execute("UPDATE transactions SET is_pending = 1 WHERE id = ?", (txn_id,))
    assert "Pending Thing" in client.get("/transactions?pending=1").text
    assert "Pending Thing" not in client.get("/transactions?pending=0").text


# ---------------------------------------------------------------- tags & splits
def test_tags(client):
    sign_up(client)
    acc = add_account(client)
    add_txn(client, acc)
    txn_id = db.query_one("SELECT id FROM transactions")["id"]
    page = client.get(f"/transactions/{txn_id}")
    client.post(f"/transactions/{txn_id}/tags",
                data={"csrf": csrf_of(page.text), "tags": "work, reimbursable"}, follow_redirects=False)
    txn = txns.get_transaction(db.query_one("SELECT household_id FROM accounts")["household_id"], txn_id)
    assert txn["tags"] == ["reimbursable", "work"]
    page = client.get(f"/transactions/{txn_id}")
    assert 'value="reimbursable, work"' in page.text

    # replacing tags drops the orphan
    client.post(f"/transactions/{txn_id}/tags",
                data={"csrf": csrf_of(page.text), "tags": "work"}, follow_redirects=False)
    assert db.query_one("SELECT COUNT(*) AS n FROM tags")["n"] == 1


def test_splits_must_balance(client):
    sign_up(client)
    acc = add_account(client)
    add_txn(client, acc, amount="100.00")
    txn_id = db.query_one("SELECT id FROM transactions")["id"]
    page = client.get(f"/transactions/{txn_id}")
    csrf = csrf_of(page.text)
    cats = txns.list_categories(db.query_one("SELECT household_id FROM accounts")["household_id"])
    c1, c2 = [c["id"] for c in cats if c["kind"] != "transfer"][:2]

    bad = client.post(f"/transactions/{txn_id}/splits", data={
        "csrf": csrf, "split_category": [str(c1), str(c2)],
        "split_amount": ["-60.00", "-30.00"], "split_note": ["", ""],
    }, follow_redirects=False)
    assert "splits=" in bad.headers["location"]
    assert db.query_one("SELECT COUNT(*) AS n FROM transaction_splits")["n"] == 0

    good = client.post(f"/transactions/{txn_id}/splits", data={
        "csrf": csrf, "split_category": [str(c1), str(c2)],
        "split_amount": ["-60.00", "-40.00"], "split_note": ["food", "house"],
    }, follow_redirects=False)
    assert good.status_code == 303
    assert db.query_one("SELECT COUNT(*) AS n FROM transaction_splits")["n"] == 2
    assert db.query_one("SELECT category_id FROM transactions")["category_id"] is None


def test_split_single_line_rejected(client):
    sign_up(client)
    acc = add_account(client)
    hid = db.query_one("SELECT household_id FROM accounts")["household_id"]
    cats = txns.ensure_system_categories(hid)
    add_txn(client, acc, amount="50.00")
    txn_id = db.query_one("SELECT id FROM transactions")["id"]
    with pytest.raises(txns.SplitError):
        txns.set_splits(hid, txn_id, [{"category_id": cats["Uncategorized"], "amount_cents": -5000}])


# ---------------------------------------------------------------- delete & bulk
def test_soft_delete_and_restore(client):
    sign_up(client)
    acc = add_account(client)
    add_txn(client, acc, merchant="Ghost")
    txn_id = db.query_one("SELECT id FROM transactions")["id"]
    page = client.get("/transactions")
    r = client.post(f"/transactions/{txn_id}/delete",
                    data={"csrf": csrf_of(page.text), "next": "/transactions"}, follow_redirects=False)
    assert "deleted=" in r.headers["location"]
    assert "Ghost" not in client.get("/transactions").text
    assert "Ghost" in client.get("/trash").text

    trash = client.get("/trash")
    client.post(f"/transactions/{txn_id}/restore",
                data={"csrf": csrf_of(trash.text), "next": "/trash"}, follow_redirects=False)
    assert "Ghost" in client.get("/transactions").text


def test_bulk_actions(client):
    sign_up(client)
    acc = add_account(client)
    hid = db.query_one("SELECT household_id FROM accounts")["household_id"]
    cats = txns.ensure_system_categories(hid)
    seed_many(hid, acc, cats["Uncategorized"], 20)
    ids = [str(r["id"]) for r in db.query_all("SELECT id FROM transactions LIMIT 10")]
    page = client.get("/transactions")
    csrf = csrf_of(page.text)

    client.post("/transactions/bulk", data={
        "csrf": csrf, "ids": ids, "action": "tag", "value": "audit", "next": "/transactions",
    }, follow_redirects=False)
    assert db.query_one("SELECT COUNT(*) AS n FROM transaction_tags")["n"] == 10

    client.post("/transactions/bulk", data={
        "csrf": csrf, "ids": ids, "action": "category", "value": str(cats["Income"]),
        "next": "/transactions",
    }, follow_redirects=False)
    assert db.query_one("SELECT COUNT(*) AS n FROM transactions WHERE category_id = ?", (cats["Income"],))["n"] == 10

    client.post("/transactions/bulk", data={
        "csrf": csrf, "ids": ids[:5], "action": "delete", "next": "/transactions",
    }, follow_redirects=False)
    assert db.query_one("SELECT COUNT(*) AS n FROM transactions WHERE deleted_at IS NOT NULL")["n"] == 5

    client.post("/transactions/bulk", data={
        "csrf": csrf, "ids": ids[:5], "action": "restore", "next": "/transactions",
    }, follow_redirects=False)
    assert db.query_one("SELECT COUNT(*) AS n FROM transactions WHERE deleted_at IS NOT NULL")["n"] == 0


def test_500_transactions_every_filter(client):
    """The Phase 2 definition of done: 500 rows, all filters usable."""
    sign_up(client)
    acc = add_account(client)
    hid = db.query_one("SELECT household_id FROM accounts")["household_id"]
    cats = txns.ensure_system_categories(hid)
    seed_many(hid, acc, cats["Uncategorized"], 500)
    db.execute("UPDATE transactions SET is_transfer = 1 WHERE id % 97 = 0")

    for qs in ("", "?q=shell", "?kind=expense", "?kind=income", "?kind=transfer",
               "?pending=1", "?category_id=none", f"?account_id={acc}",
               f"?from={date.today().isoformat()}", "?page=3"):
        r = client.get("/transactions" + qs)
        assert r.status_code == 200, qs

    res = txns.list_transactions(hid, {"kind": "transfer"}, per_page=200)
    assert res["total"] == len([i for i in range(1, 501) if i % 97 == 0])
    page3 = client.get("/transactions?page=3")
    assert "Page 4 of" in page3.text or "Page 3 of" in page3.text


def test_search_handles_quotes_and_symbols(client):
    sign_up(client)
    acc = add_account(client)
    add_txn(client, acc, merchant='Bob"s "Diner"')
    r = client.get('/transactions?q=%22Diner%22+OR+*')
    assert r.status_code == 200


# ---------------------------------------------------------------- spend chart
def test_spend_series_buckets_months_and_skips_transfers():
    hid = db.execute("INSERT INTO households(name, currency, timezone, buffer_cents, week_start, created_at)"
                     " VALUES ('H','USD','America/New_York',0,6,?)", (db.utcnow(),))
    account = db.execute(
        "INSERT INTO accounts(household_id, name, type, currency, current_balance_cents,"
        " is_manual, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
        (hid, "Checking", "checking", "USD", 0, db.utcnow(), db.utcnow()))
    now = db.utcnow()
    rows = [
        ("2026-09-03", -10000, 0),   # Sep: $100 out
        ("2026-09-20", -5000, 0),    # Sep: $50 out
        ("2026-10-04", -2500, 0),    # Oct: $25 out
        ("2026-10-05", -90000, 1),   # a transfer — not spending
        ("2026-10-06", 40000, 0),    # income — not spending
    ]
    with db.session_scope() as conn:
        for day, cents, transfer in rows:
            conn.execute(
                "INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant,"
                " is_pending, is_transfer, created_at, updated_at) VALUES (?,?,?,?,?,0,?,?,?)",
                (hid, account, day, cents, "Shop", transfer, now, now))

    series = txns.spend_series(hid, {}, "months")

    assert series["group"] == "months"
    assert [(b["label"], b["cents"]) for b in series["buckets"]] == [("Sep '26", 15000), ("Oct '26", 2500)]
    assert series["total_cents"] == 17500
    assert series["buckets"][0]["pct"] == 100.0

    # Weeks and years are available too, and auto picks by span.
    assert txns.spend_series(hid, {}, "years")["buckets"][0]["label"] == "2026"
    assert len(txns.spend_series(hid, {}, "weeks")["buckets"]) >= 5
    assert txns.spend_series(hid, {"from": "2026-10-01"})["total_cents"] == 2500


def test_spend_series_is_empty_without_expenses(client):
    sign_up(client)
    assert txns.spend_series(1, {}) is None


def test_category_filter_lists_only_categories_in_use(client):
    from app import categories

    sign_up(client)
    acc = add_account(client)
    hid = db.query_one("SELECT id FROM households")["id"]
    categories.ensure_taxonomy(hid)
    coffee = db.query_one(
        "SELECT id FROM categories WHERE household_id = ? AND name = 'Coffee'", (hid,))["id"]
    add_txn(client, acc, merchant="Starbucks", category=str(coffee))

    block = client.get("/transactions").text.split('id="f-category"', 1)[1].split("</select>", 1)[0]
    assert "Coffee" in block
    assert "Electronics" not in block, "an empty category should not clutter the filter"
    assert "All categories" in block and "Needs a category" in block


def test_summary_counts_transfers_but_leaves_them_out_of_the_money(client):
    """The boxes above the ledger and the Cash Flow boxes must report one net, not two."""
    sign_up(client)
    acc = add_account(client)
    savings = add_account(client, name="Savings")
    add_txn(client, acc, merchant="Payroll", amount="1000.00", direction="in")
    add_txn(client, acc, merchant="Kroger", amount="200.00", direction="out")
    add_txn(client, acc, merchant="Transfer", amount="500.00", direction="out")
    add_txn(client, savings, merchant="Transfer", amount="500.00", direction="in")
    db.execute("UPDATE transactions SET is_transfer = 1 WHERE merchant = 'Transfer'")
    hid = db.query_one("SELECT id FROM households")["id"]

    s = txns.summary(hid, {})
    assert s["count"] == 4, "the box counts the rows the list shows"
    assert (s["in"], s["out"], s["net"]) == (100000, -20000, 80000)

    # Asking for transfers explicitly still reports them.
    t = txns.summary(hid, {"kind": "transfer"})
    assert (t["count"], t["in"], t["out"], t["net"]) == (2, 50000, -50000, 0)


def test_cash_flow_series_carries_income_and_outflow(client):
    sign_up(client)
    acc = add_account(client)
    add_txn(client, acc, merchant="Payroll", amount="2000.00", direction="in",
            day=(date.today() - timedelta(days=62)).isoformat())
    add_txn(client, acc, merchant="Payroll", amount="2000.00", direction="in",
            day=(date.today() - timedelta(days=1)).isoformat())
    add_txn(client, acc, merchant="Kroger", amount="300.00", direction="out",
            day=(date.today() - timedelta(days=30)).isoformat())
    hid = db.query_one("SELECT id FROM households")["id"]

    both = txns.spend_series(hid, {}, "months", split_income=True)
    assert both["split"] is True
    assert both["income_cents"] == 400000
    assert both["total_cents"] == 30000
    assert both["net_cents"] == 370000
    assert any(b["in_cents"] for b in both["buckets"])
    assert max(b["in_pct"] for b in both["buckets"]) == 100.0

    spend_only = txns.spend_series(hid, {}, "months")
    assert spend_only["split"] is False
    assert spend_only["income_cents"] == 0

    # A filter narrows the data, not the chart's shape: cash in still draws.
    by_account = txns.spend_series(hid, {"account_id": str(acc)}, "months", split_income=True)
    assert (by_account["income_cents"], by_account["total_cents"]) == (400000, 30000)

    # An income-only filter must still return a series (the page would otherwise hide the card).
    income_only = txns.spend_series(hid, {"kind": "income"}, "months", split_income=True)
    assert income_only is not None
    assert (income_only["income_cents"], income_only["total_cents"]) == (400000, 0)

    # And the route wires it: income filter and expense filter both draw cash flow.
    assert 'class="flow-plot"' in client.get("/transactions?kind=income").text
    assert 'class="flow-plot"' in client.get("/transactions?kind=expense").text


def test_income_bars_are_the_period_not_the_filter(client):
    """A spending filter must not blank the income side — it is the period's income."""
    from app import categories

    sign_up(client)
    acc = add_account(client)
    hid = db.query_one("SELECT id FROM households")["id"]
    categories.ensure_taxonomy(hid)
    groceries = db.query_one(
        "SELECT id FROM categories WHERE household_id = ? AND name = 'Groceries'", (hid,))["id"]
    coffee = db.query_one(
        "SELECT id FROM categories WHERE household_id = ? AND name = 'Coffee'", (hid,))["id"]
    add_txn(client, acc, merchant="Payroll", amount="1000.00", direction="in")
    add_txn(client, acc, merchant="Kroger", amount="250.00", direction="out", category=str(groceries))
    add_txn(client, acc, merchant="Starbucks", amount="50.00", direction="out", category=str(coffee))

    series = txns.spend_series(hid, {"category_id": str(groceries)}, "months", split_income=True)
    assert series["total_cents"] == 25000, "out is what the filter selected"
    assert series["income_cents"] == 100000, "in is the period's income, not the category's"
    assert series["buckets"][0]["in_cents"] == 100000
    assert series["buckets"][0]["pct_income"] == 25
    assert series["pct_income"] == 25

    page = client.get(f"/transactions?category_id={groceries}").text
    assert "25.0% of income" in page
    # The boxes above the chart read the same numbers, not the filtered-row zeros.
    assert "$1,000.00" in page and "$250.00" in page
