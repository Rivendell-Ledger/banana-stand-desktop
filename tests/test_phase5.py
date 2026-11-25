"""Phase 5 acceptance: budget math, rollover, copy, auto-budget, safe-to-spend."""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import budgets, categories, config, db
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


def setup_household(client, balance="5000.00", buffer="1500.00"):
    sign_up(client)
    page = client.get("/accounts/new")
    client.post("/accounts", data={"csrf": csrf_of(page.text), "type": "checking", "name": "Checking",
                                   "institution": "Chase", "mask": "4821", "balance": balance,
                                   "credit_limit": "", "include_net": "on", "include_budget": "on"}, follow_redirects=False)
    hid = db.query_one("SELECT id FROM households")["id"]
    account = db.query_one("SELECT id FROM accounts")["id"]
    db.execute("UPDATE households SET buffer_cents = ? WHERE id = ?",
               (int(float(buffer) * 100), hid))
    categories.ensure_taxonomy(hid)
    return hid, account


def cat(hid, name):
    row = db.query_one("SELECT id FROM categories WHERE household_id = ? AND lower(name) = ?",
                       (hid, name.lower()))
    assert row, name
    return row["id"]


def add_txn(hid, account, cents, category_id, day, pending=0, transfer=0, excluded=0):
    return db.execute(
        """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant,
               category_id, is_pending, is_transfer, excluded, category_source, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (hid, account, day, cents, "Test", category_id, pending, transfer, excluded, "manual",
         db.utcnow(), db.utcnow()),
    )


M = budgets.month_key()
PREV = budgets.shift_month(M, -1)
TWO_AGO = budgets.shift_month(M, -2)
THREE_AGO = budgets.shift_month(M, -3)


def day_in(month: str, day: int) -> str:
    return f"{month}-{day:02d}"


# ---------------------------------------------------------------- spend math
def test_spent_reconciles_exactly(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    income = cat(hid, "income")
    add_txn(hid, account, -10000, groceries, day_in(M, 3))
    add_txn(hid, account, -2500, groceries, day_in(M, 4))
    add_txn(hid, account, 500000, income, day_in(M, 5))          # income: not spend
    add_txn(hid, account, -9999, groceries, day_in(M, 6), pending=1)   # pending: ignored
    add_txn(hid, account, -8888, groceries, day_in(M, 7), transfer=1)  # transfer: ignored
    add_txn(hid, account, -7777, groceries, day_in(M, 8), excluded=1)  # excluded: ignored
    # split parent: category NULL, 2 lines
    parent = db.execute(
        """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant,
               category_id, created_at, updated_at) VALUES (?,?,?,?,?,NULL,?,?)""",
        (hid, account, day_in(M, 9), -6000, "Split", db.utcnow(), db.utcnow()),
    )
    dining = cat(hid, "dining out")
    db.execute("INSERT INTO transaction_splits(transaction_id, category_id, amount_cents) VALUES (?,?,?)",
               (parent, groceries, -4000))
    db.execute("INSERT INTO transaction_splits(transaction_id, category_id, amount_cents) VALUES (?,?,?)",
               (parent, dining, -2000))

    spent = budgets.spent_map(hid)
    assert budgets.spent_in(spent, M, groceries) == 10000 + 2500 + 4000
    assert budgets.spent_in(spent, M, dining) == 2000
    assert sum(v for (m, _c), v in spent.items() if m == M) == 18500


def test_month_boundaries(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    first = day_in(M, 1)
    next_first = day_in(budgets.shift_month(M, 1), 1)
    add_txn(hid, account, -1000, groceries, first)
    add_txn(hid, account, -2000, groceries, next_first)
    spent = budgets.spent_map(hid)
    assert budgets.spent_in(spent, M, groceries) == 1000
    assert budgets.spent_in(spent, budgets.shift_month(M, 1), groceries) == 2000


def test_remaining_and_totals(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    add_txn(hid, account, -20000, groceries, day_in(M, 2))
    budgets.set_budget(hid, M, groceries, 50000)
    budgets.set_income(hid, M, 500000)
    g = budgets.grid(hid, M)
    row = next(r for r in g["rows"] if r["id"] == groceries)
    assert row["spent_cents"] == 20000
    assert row["remaining_cents"] == 30000
    assert g["budgeted_cents"] == 50000
    assert g["left_to_budget_cents"] == 450000


# ---------------------------------------------------------------- rollover
def test_rollover_carries_unspent(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    add_txn(hid, account, -6000, groceries, day_in(PREV, 5))
    budgets.set_budget(hid, PREV, groceries, 10000, rollover=True)
    budgets.set_budget(hid, M, groceries, 10000, rollover=True)
    g = budgets.grid(hid, M)
    row = next(r for r in g["rows"] if r["id"] == groceries)
    assert row["carry_cents"] == 4000
    assert row["available_cents"] == 14000
    assert row["remaining_cents"] == 14000


def test_rollover_carries_overspend(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    add_txn(hid, account, -15000, groceries, day_in(PREV, 5))
    budgets.set_budget(hid, PREV, groceries, 10000, rollover=True)
    budgets.set_budget(hid, M, groceries, 10000, rollover=True)
    row = next(r for r in budgets.grid(hid, M)["rows"] if r["id"] == groceries)
    assert row["carry_cents"] == -5000
    assert row["remaining_cents"] == 5000


def test_rollover_stops_when_previous_month_is_off(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    add_txn(hid, account, -6000, groceries, day_in(TWO_AGO, 5))
    budgets.set_budget(hid, TWO_AGO, groceries, 10000, rollover=True)
    budgets.set_budget(hid, PREV, groceries, 10000, rollover=False)
    budgets.set_budget(hid, M, groceries, 10000, rollover=True)
    row = next(r for r in budgets.grid(hid, M)["rows"] if r["id"] == groceries)
    assert row["carry_cents"] == 0


# ---------------------------------------------------------------- copy + auto
def test_copy_month_only_missing(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    dining = cat(hid, "dining out")
    budgets.set_budget(hid, PREV, groceries, 40000)
    budgets.set_budget(hid, PREV, dining, 15000)
    budgets.set_budget(hid, M, groceries, 12345)

    copied = budgets.copy_month(hid, PREV, M)
    assert copied == 1  # only dining was missing
    g = budgets.grid(hid, M)
    by_id = {r["id"]: r for r in g["rows"]}
    assert by_id[groceries]["budget_cents"] == 12345
    assert by_id[dining]["budget_cents"] == 15000


def test_auto_budget_averages_three_months(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    for month, cents in ((THREE_AGO, 30000), (TWO_AGO, 60000), (PREV, 90000)):
        add_txn(hid, account, -cents, groceries, day_in(month, 10))
    applied = budgets.auto_budget(hid, M, lookback=3)
    assert applied >= 1
    row = next(r for r in budgets.grid(hid, M)["rows"] if r["id"] == groceries)
    assert row["budget_cents"] == 60000


# ---------------------------------------------------------------- safe to spend
def test_safe_to_spend(client):
    hid, account = setup_household(client, balance="5000.00", buffer="1500.00")
    groceries = cat(hid, "groceries")
    budgets.set_budget(hid, M, groceries, 20000)
    add_txn(hid, account, -5000, groceries, day_in(M, 2))
    safe = budgets.safe_to_spend(hid, M)
    # cash 500000 - buffer 150000 - remaining 15000
    assert safe["cash_cents"] == 500000
    assert safe["budget_remaining_cents"] == 15000
    assert safe["safe_cents"] == 335000


def test_safe_to_spend_never_negative(client):
    hid, account = setup_household(client, balance="100.00", buffer="1500.00")
    cat_id = cat(hid, "groceries")
    budgets.set_budget(hid, M, cat_id, 50000)
    assert budgets.safe_to_spend(hid, M)["safe_cents"] == 0


# ---------------------------------------------------------------- UI
def test_budget_page_and_save(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    page = client.get("/budget")
    assert page.status_code == 200
    assert "Safe to spend" not in page.text, "the envelope page no longer carries a safe-to-spend hero"
    assert 'name="expected_income"' in page.text
    csrf = csrf_of(page.text)

    r = client.post("/budget/save", data={
        "csrf": csrf, "month": M, "expected_income": "5,000.00",
        f"cat_{groceries}": "450.00",
    }, follow_redirects=False)
    assert r.status_code == 303
    row = db.query_one("SELECT * FROM budgets WHERE household_id = ? AND month = ? AND category_id = ?",
                       (hid, M, groceries))
    assert row["budget_cents"] == 45000
    plan = db.query_one("SELECT expected_income_cents FROM monthly_plans WHERE household_id = ? AND month = ?",
                        (hid, M))
    assert plan["expected_income_cents"] == 500000

    again = client.get("/budget")
    assert "$450.00" in again.text
    assert "Left to budget" in again.text


def test_zeroing_a_budget_removes_it(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    budgets.set_budget(hid, M, groceries, 20000)
    page = client.get("/budget")
    client.post("/budget/save", data={"csrf": csrf_of(page.text), "month": M,
                                      f"cat_{groceries}": "0"}, follow_redirects=False)
    assert db.query_one("SELECT COUNT(*) AS n FROM budgets WHERE category_id = ?", (groceries,))["n"] == 0


def test_copy_last_month_is_on_by_default_and_seeds_a_new_month(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    budgets.set_budget(hid, M, groceries, 20000)
    assert budgets.copy_previous_enabled(hid) is True

    nxt = budgets.shift_month(M, 1)
    assert client.get(f"/budget?month={nxt}").status_code == 200
    row = db.query_one("SELECT budget_cents FROM budgets WHERE category_id = ? AND month = ?",
                       (groceries, nxt))
    assert row["budget_cents"] == 20000, "a fresh month starts from last month's plan"


def test_copy_last_month_can_be_switched_off(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    budgets.set_budget(hid, M, groceries, 20000)
    page = client.get("/budget")
    assert "Copy last month" in page.text and ">On<" in page.text

    client.post("/budget/copy-previous",
                data={"csrf": csrf_of(page.text), "month": M, "enabled": ""},
                follow_redirects=False)
    assert budgets.copy_previous_enabled(hid) is False
    assert ">Off<" in client.get("/budget").text

    nxt = budgets.shift_month(M, 1)
    client.get(f"/budget?month={nxt}")
    assert db.query_one("SELECT COUNT(*) AS n FROM budgets WHERE month = ?", (nxt,))["n"] == 0


def test_json_save_returns_the_new_totals(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    page = client.get("/budget")
    r = client.post("/budget/save", data={"csrf": csrf_of(page.text), "month": M,
                                          "expected_income": "5,000.00",
                                          f"cat_{groceries}": "450.00"},
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200
    body = r.json()
    assert body["budgeted_cents"] == 45000
    assert body["expected_income_cents"] == 500000
    assert body["left_to_budget_cents"] == 455000


def test_month_spend_curve_tracks_both_months(client):
    hid, account = setup_household(client)
    today = date.today()
    this_month = budgets.month_key(today)
    last_month = budgets.shift_month(this_month, -1)

    def add(day, cents):
        db.execute(
            """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant,
                   is_pending, is_transfer, excluded, created_at, updated_at)
               VALUES (?,?,?,?,?,0,0,0,?,?)""",
            (hid, account, day, cents, "Shop", db.utcnow(), db.utcnow()))

    add(f"{last_month}-02", -10000)
    add(f"{last_month}-20", -5000)
    add(f"{last_month}-21", -99999)    # transfer noise, ignored below
    db.execute("UPDATE transactions SET is_transfer = 1 WHERE amount_cents = -99999")
    add(f"{this_month}-01", -4000)

    curve = budgets.month_spend_curve(hid, today)
    assert curve["last"]["total_cents"] == 15000
    assert curve["this"]["total_cents"] == 4000
    assert curve["last_label"] == budgets.month_label(last_month)
    assert curve["this_label"] == budgets.month_label(this_month)
    assert len(curve["last"]["points"].split()) == curve["days"]
    assert len(curve["this"]["points"].split()) == curve["today"]
    assert client.get("/").status_code == 200


def test_hiding_a_budget_category_leaves_the_ledger_filter_alone(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    budgets.set_budget(hid, M, groceries, 20000)
    page = client.get("/budget")
    assert f'name="cat_{groceries}"' in page.text

    client.post("/budget/hide", data={"csrf": csrf_of(page.text), "month": M,
                                      "category_id": groceries}, follow_redirects=False)
    page = client.get("/budget")
    assert f'name="cat_{groceries}"' not in page.text, "no budget row once removed"
    assert "Hidden from budget (1)" in page.text
    assert categories.hidden_from_budget(hid)[0]["name"] == "Groceries"
    # Still a category the ledger can filter by.
    assert "Groceries" in client.get("/transactions").text

    client.post("/budget/unhide", data={"csrf": csrf_of(page.text), "month": M,
                                        "category_id": groceries}, follow_redirects=False)
    assert f'name="cat_{groceries}"' in client.get("/budget").text


def test_month_total_counts_spend_filed_on_a_parent_category(client):
    """A transaction filed straight on a parent is real spend; skipping parents hid it."""
    hid, account = setup_household(client)
    parent = categories.create(hid, "Household Stuff", kind="expense")
    child = categories.create(hid, "Cleaning", kind="expense", parent_id=parent)
    add_txn(hid, account, -5000, child, day_in(M, 3))
    add_txn(hid, account, -2500, parent, day_in(M, 4))
    summary = budgets.grid(hid, M)
    assert summary["all_spent_cents"] == 7500
