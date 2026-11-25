"""Phase 6 acceptance: recurring detection, bills, forecast, safe-to-spend."""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import budgets, categories, config, db, forecast, recurring
from app.main import app

PASSWORD = "correct horse battery staple"
TODAY = date.today()


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


def setup_household(client, balance="1000.00", buffer="0.00"):
    sign_up(client)
    page = client.get("/accounts/new")
    client.post("/accounts", data={"csrf": csrf_of(page.text), "type": "checking", "name": "Checking",
                                   "institution": "Chase", "mask": "4821", "balance": balance,
                                   "credit_limit": "", "include_net": "on", "include_budget": "on"}, follow_redirects=False)
    hid = db.query_one("SELECT id FROM households")["id"]
    account = db.query_one("SELECT id FROM accounts")["id"]
    db.execute("UPDATE households SET buffer_cents = ? WHERE id = ?", (int(float(buffer) * 100), hid))
    categories.ensure_taxonomy(hid)
    return hid, account


def cat(hid, name):
    return db.query_one("SELECT id FROM categories WHERE household_id = ? AND lower(name) = ?",
                        (hid, name.lower()))["id"]


def add_txn(hid, account, cents, day, merchant, category_id=None):
    db.execute(
        """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant,
               raw_merchant, category_id, category_source, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,'import',?,?)""",
        (hid, account, day.isoformat(), cents, merchant, merchant, category_id, db.utcnow(), db.utcnow()),
    )


# ---------------------------------------------------------------- detection
def test_detects_monthly_subscription(client):
    hid, account = setup_household(client)
    for months_back in (3, 2, 1):
        day = TODAY - timedelta(days=30 * months_back)
        add_txn(hid, account, -1599, day, "Netflix")
    found = recurring.detect(hid)
    assert len(found) == 1
    cand = found[0]
    assert cand["name"] == "Netflix"
    assert cand["cadence"] == "monthly"
    assert cand["amount_cents"] == 1599
    assert cand["kind"] == "bill"
    assert cand["confidence"] >= 60


def test_ignores_irregular_charges(client):
    hid, account = setup_household(client)
    add_txn(hid, account, -500, TODAY - timedelta(days=61), "Corner Store")
    add_txn(hid, account, -900, TODAY - timedelta(days=23), "Corner Store")
    add_txn(hid, account, -200, TODAY - timedelta(days=7), "Corner Store")
    assert recurring.detect(hid) == []


def test_detects_biweekly_income(client):
    hid, account = setup_household(client)
    for i in range(4):
        add_txn(hid, account, 250000, TODAY - timedelta(days=14 * (3 - i)), "ACME PAYROLL")
    found = recurring.detect(hid)
    assert len(found) == 1
    assert found[0]["kind"] == "income"
    assert found[0]["cadence"] == "biweekly"


def test_scan_is_idempotent(client):
    hid, account = setup_household(client)
    for months_back in (3, 2, 1):
        add_txn(hid, account, -1599, TODAY - timedelta(days=30 * months_back), "Netflix")
    assert recurring.scan(hid) == 1
    assert recurring.scan(hid) == 0
    assert len(recurring.list_recurring(hid)) == 1
    assert recurring.list_recurring(hid)[0]["status"] == "suggested"


# ---------------------------------------------------------------- scheduling
def test_occurrences_and_roll_forward(client):
    hid, _ = setup_household(client)
    start = TODAY - timedelta(days=40)
    rid = recurring.create(hid, "Rent", 320000, "monthly", start.isoformat())
    row = recurring.get(hid, rid)
    due = recurring.occurrences(row, TODAY, TODAY + timedelta(days=90))
    assert len(due) == 3
    assert all(d >= TODAY for d in due)


def test_month_end_day_is_clamped(client):
    hid, _ = setup_household(client)
    rid = recurring.create(hid, "Insurance", 5000, "monthly", "2027-01-31")
    row = recurring.get(hid, rid)
    assert row["anchor_day"] == 31
    due = recurring.occurrences(row, date(2027, 1, 1), date(2027, 4, 1))
    assert date(2027, 2, 28) in due


def test_ignored_rows_do_not_reach_the_forecast(client):
    hid, _ = setup_household(client)
    rid = recurring.create(hid, "Gym", 4000, "monthly", (TODAY + timedelta(days=3)).isoformat())
    recurring.set_status(hid, rid, "ignored")
    fc = forecast.forecast(hid, 90)
    assert fc["events"] == []


# ---------------------------------------------------------------- forecast math
def test_forecast_running_balance_and_low_point(client):
    hid, _ = setup_household(client, balance="1000.00", buffer="0.00")
    recurring.create(hid, "Rent", 30000, "monthly", (TODAY + timedelta(days=5)).isoformat())
    recurring.create(hid, "Payroll", 50000, "monthly", (TODAY + timedelta(days=20)).isoformat(), kind="income")
    fc = forecast.forecast(hid, 30)
    assert fc["opening_cents"] == 100000
    assert fc["bills_total_cents"] == 30000
    assert fc["income_total_cents"] == 50000
    assert fc["closing_cents"] == 120000
    assert fc["low"]["balance_cents"] == 70000
    assert fc["low"]["date"] == (TODAY + timedelta(days=5)).isoformat()
    assert fc["breach"] is False


def test_forecast_breach_flag(client):
    hid, _ = setup_household(client, balance="100.00", buffer="500.00")
    recurring.create(hid, "Rent", 30000, "monthly", (TODAY + timedelta(days=5)).isoformat())
    fc = forecast.forecast(hid, 30)
    assert fc["breach"] is True
    assert fc["low"]["balance_cents"] == -20000


def test_next_bill(client):
    hid, _ = setup_household(client)
    recurring.create(hid, "Later", 9000, "monthly", (TODAY + timedelta(days=20)).isoformat())
    recurring.create(hid, "Sooner", 1000, "monthly", (TODAY + timedelta(days=2)).isoformat())
    nxt = forecast.next_bill(hid)
    assert nxt["name"] == "Sooner"
    assert nxt["days_out"] == 2


# ---------------------------------------------------------------- safe to spend
def test_safe_to_spend_subtracts_uncovered_bills(client):
    hid, _ = setup_household(client, balance="1000.00", buffer="0.00")
    recurring.create(hid, "Netflix", 5000, "monthly", (TODAY + timedelta(days=4)).isoformat())
    safe = budgets.safe_to_spend(hid)
    assert safe["bills_uncovered_cents"] == 5000
    assert safe["safe_cents"] == 95000


def test_safe_to_spend_ignores_bills_already_budgeted(client):
    hid, _ = setup_household(client, balance="1000.00", buffer="0.00")
    groceries = cat(hid, "groceries")
    budgets.set_budget(hid, budgets.month_key(), groceries, 20000)
    recurring.create(hid, "Groceries box", 15000, "monthly", (TODAY + timedelta(days=4)).isoformat(),
                     category_id=groceries)
    safe = budgets.safe_to_spend(hid)
    assert safe["bills_uncovered_cents"] == 0
    assert safe["safe_cents"] == 80000   # 1000 - 200 budgeted


def test_safe_to_spend_counts_only_the_shortfall(client):
    hid, _ = setup_household(client, balance="1000.00", buffer="0.00")
    groceries = cat(hid, "groceries")
    budgets.set_budget(hid, budgets.month_key(), groceries, 20000)
    recurring.create(hid, "Groceries box", 25000, "monthly", (TODAY + timedelta(days=4)).isoformat(),
                     category_id=groceries)
    safe = budgets.safe_to_spend(hid)
    assert safe["bills_uncovered_cents"] == 5000
    assert safe["safe_cents"] == 75000


# ---------------------------------------------------------------- UI
def test_bills_flow_and_pages(client):
    hid, account = setup_household(client)
    for months_back in (3, 2, 1):
        add_txn(hid, account, -1599, TODAY - timedelta(days=30 * months_back), "Netflix")

    page = client.get("/bills")
    assert page.status_code == 200
    csrf = csrf_of(page.text)

    client.post("/bills/scan", data={"csrf": csrf}, follow_redirects=False)
    page = client.get("/bills")
    assert "Bills &amp; Income" in page.text
    assert "Scheduled in" in page.text
    assert "Total Income" in page.text and "Total Bills" in page.text
    assert "Netflix" in page.text

    rid = db.query_one("SELECT id FROM recurring WHERE merchant_key LIKE '%netflix%'")["id"]
    client.post(f"/bills/{rid}/confirm", data={"csrf": csrf}, follow_redirects=False)
    assert recurring.get(hid, rid)["status"] == "confirmed"

    fc = client.get("/forecast")
    assert fc.status_code == 200
    assert "Netflix" in fc.text
    assert "Low point" in fc.text

    home = client.get("/")
    assert "Coming up" in home.text and "Netflix" in home.text


def test_create_bill_by_hand(client):
    hid, _ = setup_household(client)
    page = client.get("/bills/new")
    r = client.post("/bills/new", data={
        "csrf": csrf_of(page.text), "name": "Water", "amount": "64.20",
        "cadence": "quarterly", "next_due_on": (TODAY + timedelta(days=9)).isoformat(),
        "kind": "bill", "category_id": "", "account_id": "", "notes": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    row = db.query_one("SELECT * FROM recurring WHERE name = 'Water'")
    assert row["amount_cents"] == 6420
    assert row["cadence"] == "quarterly"
    assert row["status"] == "confirmed"
    assert row["anchor_day"] == (TODAY + timedelta(days=9)).day


def test_same_day_payments_become_separate_series(client):
    hid, account = setup_household(client)
    for months_back in (3, 2, 1):
        day = TODAY - timedelta(days=30 * months_back)
        add_txn(hid, account, 183300, day, "Social Security Administration")
        add_txn(hid, account, 183300, day, "Social Security Administration")
    found = [c for c in recurring.detect(hid) if c["merchant_key"] == "social security administration"]
    assert len(found) == 2
    assert sorted(c["series"] for c in found) == [0, 1]
    assert {c["amount_cents"] for c in found} == {183300}
    assert {c["name"] for c in found} == {"Social Security Administration",
                                          "Social Security Administration (2)"}
    assert recurring.scan(hid) == 2
    assert recurring.scan(hid) == 0


def test_one_series_per_account_when_a_merchant_hits_two(client):
    hid, account = setup_household(client)
    page = client.get("/accounts/new")
    client.post("/accounts", data={"csrf": csrf_of(page.text), "type": "savings", "name": "Escrow",
                                   "institution": "Chase", "mask": "0002", "balance": "0.00",
                                   "credit_limit": "", "include_net": "on", "include_budget": "on"},
                follow_redirects=False)
    second = db.query_one("SELECT id FROM accounts WHERE name = 'Escrow'")["id"]
    for months_back in (3, 2, 1):
        day = TODAY - timedelta(days=30 * months_back)
        add_txn(hid, account, 1838, day, "Monthly Interest Paid")
        add_txn(hid, second, 1253, day, "Monthly Interest Paid")
    found = [c for c in recurring.detect(hid) if c["merchant_key"] == "monthly interest paid"]
    assert len(found) == 2
    assert {c["account_id"] for c in found} == {account, second}
    assert {c["amount_cents"] for c in found} == {1838, 1253}


def test_two_prices_on_two_rhythms_become_two_series(client):
    hid, account = setup_household(client)
    for months_back in (3, 2, 1):
        start = TODAY - timedelta(days=30 * months_back)
        add_txn(hid, account, -12099, start, "Verizon")
        add_txn(hid, account, -10856, start + timedelta(days=3), "Verizon")
    found = [c for c in recurring.detect(hid) if c["merchant_key"] == "verizon"]
    assert len(found) == 2
    assert {c["amount_cents"] for c in found} == {12099, 10856}


def test_seasonal_bill_is_kept_but_marked_variable(client):
    hid, account = setup_household(client)
    for months_back, cents in ((3, 4656), (2, 4811), (1, 8125)):
        add_txn(hid, account, -cents, TODAY - timedelta(days=30 * months_back), "Columbia Gas")
    found = [c for c in recurring.detect(hid) if c["merchant_key"] == "columbia gas"]
    assert len(found) == 1
    assert found[0]["amount_varies"] == 1
    assert found[0]["amount_cents"] == 4811
    assert found[0]["cadence"] == "monthly"


def test_interest_dust_is_not_a_bill(client):
    hid, account = setup_household(client)
    for months_back in (3, 2, 1):
        add_txn(hid, account, 15, TODAY - timedelta(days=30 * months_back), "Monthly Interest Paid")
    assert [c for c in recurring.detect(hid) if "interest" in c["merchant_key"]] == []


def test_expected_income_comes_from_bills_and_income(client):
    hid, account = setup_household(client)
    for months_back in (3, 2, 1):
        add_txn(hid, account, 250000, TODAY - timedelta(days=30 * months_back), "ACME PAYROLL")
    assert recurring.scan(hid) == 1
    g = budgets.grid(hid, budgets.month_key())
    assert g["auto_income"] is True
    assert g["expected_income_cents"] >= 250000
    assert budgets.grid(hid, budgets.month_key())["left_to_budget_cents"] is not None


def test_expected_income_averages_the_previous_months(client):
    hid, account = setup_household(client)
    for back, cents in ((1, 100000), (2, 200000), (3, 300000)):
        month = budgets.shift_month(budgets.month_key(), -back)
        add_txn(hid, account, cents, date.fromisoformat(month + "-15"), "ACME PAYROLL")
    g = budgets.grid(hid, budgets.month_key())
    assert g["auto_income"] is True
    assert g["expected_income_cents"] == 200000
    assert g["income_note"] == "Average of the last 3 months of income"


def test_weekly_schedule_fills_earlier_days_in_the_month(client):
    hid, account = setup_household(client)
    # A weekly paycheck whose next due date is late in the month must still show
    # every earlier week — the month view is not only what remains.
    rid = recurring.create(hid, "ACME PAYROLL", 200000, "weekly", (TODAY + timedelta(days=6)).isoformat(),
                           kind="income")
    row = recurring.get(hid, rid)
    first, last = budgets.month_bounds(budgets.month_key())
    occ = recurring.occurrences_between(row, date.fromisoformat(first), date.fromisoformat(last))
    assert len(occ) >= 4
    assert occ[0] < date.fromisoformat(row["next_due_on"])
    assert all((b - a).days == 7 for a, b in zip(occ, occ[1:]))


def test_bills_page_is_a_month_view_with_navigation(client):
    hid, account = setup_household(client)
    for months_back in (3, 2, 1):
        add_txn(hid, account, -1599, TODAY - timedelta(days=30 * months_back), "Netflix")
    recurring.scan(hid)

    page = client.get("/bills")
    assert "Total Income" in page.text and "Total Bills" in page.text
    assert "Scheduled in" in page.text
    assert "Netflix" in page.text
    assert 'class="cal-grid"' in page.text
    assert "data-cal-day=" in page.text
    assert "data-cal-detail" in page.text

    empty = client.get("/bills?month=2020-01")
    assert "January 2020" in empty.text
    assert "Nothing scheduled" in empty.text


def test_bills_page_shows_collapsed_bills_and_income_with_add(client):
    hid, account = setup_household(client)
    recurring.create(hid, "Rent", 320000, "monthly", (TODAY + timedelta(days=5)).isoformat())
    recurring.create(hid, "Payroll", 500000, "monthly", (TODAY + timedelta(days=20)).isoformat(),
                     kind="income")
    page = client.get("/bills").text
    assert 'class="card rec-section"' in page
    assert "Bills <span class=\"count\">1</span>" in page
    assert "Income <span class=\"count\">1</span>" in page
    assert 'href="/bills/new?kind=bill"' in page and "Add a bill" in page
    assert 'href="/bills/new?kind=income"' in page and "Add income" in page
    # Manual rows open the editable form.
    rent = db.query_one("SELECT id FROM recurring WHERE name = 'Rent'")["id"]
    assert f'href="/bills/{rent}"' in page


def test_catches_a_subscription_whose_price_and_day_wobble(client):
    """Apple bills several products on drifting days — strict cadence rejects it, the
    subscription rule keeps it as a bill."""
    hid, account = setup_household(client)
    subs = cat(hid, "Subscriptions")
    for days_ago, cents in ((70, -999), (40, -1099), (9, -1399)):
        add_txn(hid, account, cents, TODAY - timedelta(days=days_ago), "Apple", subs)

    found = recurring.detect(hid)
    assert len(found) == 1
    cand = found[0]
    assert cand["name"] == "Apple" and cand["kind"] == "bill"
    assert cand["cadence"] == "monthly"
    assert cand["amount_cents"] == 1099

    assert recurring.scan(hid) == 1
    row = db.query_one("SELECT kind, status FROM recurring WHERE merchant_key LIKE '%apple%'")
    assert row["kind"] == "bill" and row["status"] == "suggested"


def test_a_stopped_pattern_is_not_suggested(client):
    hid, account = setup_household(client)
    for days_ago in (200, 170, 140):
        add_txn(hid, account, -1599, TODAY - timedelta(days=days_ago), "Old Gym")
    assert recurring.detect(hid) == []


def test_suggested_rows_open_the_section_with_a_confirm_box(client):
    hid, account = setup_household(client)
    rid = recurring.create(hid, "Netflix", 1599, "monthly", (TODAY + timedelta(days=4)).isoformat(),
                           status="suggested")
    page = client.get("/bills").text
    assert '<details class="card rec-section" open>' in page
    assert "1 to review" in page
    assert f'action="/bills/{rid}/confirm"' in page
    assert f'id="confirm-{rid}"' in page
    assert f'action="/bills/{rid}/ignore"' in page          # a suggestion can be denied too


def test_denying_a_suggestion_retires_it_without_touching_the_ledger(client):
    hid, account = setup_household(client)
    rid = recurring.create(hid, "Xai", 2250, "monthly", (TODAY + timedelta(days=6)).isoformat(),
                           status="suggested")
    page = client.get("/bills").text
    token = csrf_of(page)
    r = client.post(f"/bills/{rid}/ignore", data={"csrf": token}, follow_redirects=False)
    assert r.status_code in (302, 303)
    row = db.query_one("SELECT status FROM recurring WHERE id = ?", (rid,))
    assert row["status"] == "ignored"
    assert f'action="/bills/{rid}/confirm"' not in client.get("/bills").text


def test_a_subscription_is_a_bill_even_when_the_price_moves(client):
    hid, account = setup_household(client)
    subs = cat(hid, "Subscriptions")
    for days_ago, cents in ((60, -1500), (31, -3000), (2, -3000)):
        add_txn(hid, account, cents, TODAY - timedelta(days=days_ago), "Xai", subs)
    found = recurring.detect(hid)
    assert len(found) == 1
    assert found[0]["kind"] == "bill"
    assert found[0]["cadence"] == "monthly"


def test_a_refund_does_not_flip_a_subscription_to_income(client):
    hid, account = setup_household(client)
    subs = cat(hid, "Subscriptions")
    add_txn(hid, account, -3000, TODAY - timedelta(days=60), "Grok XAI", subs)
    add_txn(hid, account, -1500, TODAY - timedelta(days=31), "Xai", subs)
    add_txn(hid, account, 306, TODAY - timedelta(days=30), "GROK XAI", subs)
    found = recurring.detect(hid)
    assert found and found[0]["kind"] == "bill" and found[0]["name"] == "Xai"
