"""Phase 7 acceptance: holdings, allocation, XIRR, options, account sync."""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import config, db, networth, portfolio
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


def make_account(hid, name="Brokerage", type_="brokerage", balance="0.00"):
    return db.execute(
        """INSERT INTO accounts(household_id, institution, name, type, currency, current_balance_cents,
               include_in_net_worth, include_in_budget, is_manual, created_at, updated_at)
           VALUES (?,?,?,?,?,?,1,0,1,?,?)""",
        (hid, "Schwab", name, type_, "USD", int(float(balance) * 100), db.utcnow(), db.utcnow()),
    )


def setup_household(client):
    sign_up(client)
    hid = db.query_one("SELECT id FROM households")["id"]
    account = make_account(hid)
    return hid, account


# ---------------------------------------------------------------- holdings
def test_holding_value_and_gain(client):
    hid, account = setup_household(client)
    holding = portfolio.add_holding(hid, account, "SCHG", 10, 100000, 12000, name="Schwab Growth", kind="etf")
    row = next(r for r in portfolio.holdings(hid) if r["id"] == holding)
    assert row["value_cents"] == 120000
    assert row["gain_cents"] == 20000
    assert round(row["gain_pct"], 2) == 20.0


def test_fractional_shares_round_sanely(client):
    hid, account = setup_household(client)
    portfolio.add_holding(hid, account, "VTI", 1.2345, 25000, 21000)
    row = portfolio.holdings(hid)[0]
    assert row["value_cents"] == round(1.2345 * 21000)


def test_holdings_replace_account_balance(client):
    hid, account = setup_household(client)
    portfolio.add_holding(hid, account, "AAPL", 5, 50000, 19000)
    balance = db.query_one("SELECT current_balance_cents FROM accounts WHERE id = ?", (account,))["current_balance_cents"]
    assert balance == 95000
    portfolio.update_holding(hid, portfolio.holdings(hid)[0]["id"], price_cents=20000)
    assert db.query_one("SELECT current_balance_cents FROM accounts WHERE id = ?", (account,))["current_balance_cents"] == 100000


def test_purchases_average_into_existing_holding(client):
    hid, account = setup_household(client)
    portfolio.add_holding(hid, account, "SCHG", 10, 100000, 120000)
    portfolio.add_holding(hid, account, "SCHG", 15, 200000, 120000)
    assert len(portfolio.holdings(hid)) == 1
    row = portfolio.holdings(hid)[0]
    assert row["quantity"] == 25
    assert row["cost_basis_cents"] == 300000


def test_totals_and_allocation(client):
    hid, account = setup_household(client)
    portfolio.add_holding(hid, account, "SCHG", 10, 100000, 12000, kind="etf")
    portfolio.add_holding(hid, account, "SCHW", 20, 100000, 8000, kind="stock")
    rows = portfolio.holdings(hid)
    totals = portfolio.totals(rows)
    assert totals["value_cents"] == 120000 + 160000
    assert totals["cost_cents"] == 200000
    assert totals["gain_cents"] == 80000
    alloc = portfolio.allocation(rows, "kind")
    assert round(sum(a["pct"] for a in alloc), 6) == 100.0
    by_label = {a["label"]: a for a in alloc}
    assert round(by_label["ETF"]["pct"], 2) == round(120000 / 280000 * 100, 2)
    assert alloc[0]["label"] == "Stock"


# ---------------------------------------------------------------- XIRR
def test_xirr_one_year_exact():
    flows = [(date(2025, 1, 1), -100000), (date(2026, 1, 1), 110000)]
    rate = portfolio.xirr(flows)
    assert rate is not None
    assert abs(rate - 0.10) < 0.002


def test_xirr_doubling_in_two_years():
    flows = [(date(2024, 1, 1), -10000), (date(2026, 1, 1), 40000)]
    rate = portfolio.xirr(flows)
    assert rate is not None
    assert abs(rate - 0.9986) < 0.02   # ~100% annualised


def test_xirr_needs_both_directions():
    assert portfolio.xirr([(date(2025, 1, 1), -100)]) is None
    assert portfolio.xirr([(date(2025, 1, 1), 100), (date(2026, 1, 1), 110)]) is None


def test_performance_uses_flows(client):
    hid, account = setup_household(client)
    portfolio.add_holding(hid, account, "SCHG", 10, 100000, 12000)
    portfolio.add_flow(hid, (TODAY - timedelta(days=365)).isoformat(), 100000, note="seed")
    perf = portfolio.performance(hid, portfolio.holdings(hid))
    assert perf["contributed_cents"] == 100000
    assert perf["xirr"] is not None
    assert perf["xirr"] > 0


def test_withdrawal_is_positive_flow(client):
    hid, account = setup_household(client)
    portfolio.add_holding(hid, account, "SCHG", 10, 50000, 12000)
    portfolio.add_flow(hid, (TODAY - timedelta(days=180)).isoformat(), 100000)
    portfolio.add_flow(hid, (TODAY - timedelta(days=30)).isoformat(), -20000)
    perf = portfolio.performance(hid, portfolio.holdings(hid))
    assert perf["contributed_cents"] == 80000


# ---------------------------------------------------------------- options
def test_options_breakeven_and_collateral(client):
    hid, account = setup_household(client)
    portfolio.add_option(hid, "SCHW", "put", 7500, (TODAY + timedelta(days=30)).isoformat(),
                         2, 135, TODAY.isoformat())
    row = portfolio.options(hid)[0]
    assert row["premium_total_cents"] == 135 * 2 * 100
    assert row["breakeven_cents"] == 7500 - 135
    assert row["collateral_cents"] == 7500 * 2 * 100
    assert row["dte"] == 30
    summary = portfolio.option_summary(portfolio.options(hid))
    assert summary["open_count"] == 1
    assert summary["premium_cents"] == 27000
    assert summary["nearest_dte"] == 30


def test_call_breakeven_adds_premium(client):
    hid, account = setup_household(client)
    portfolio.add_option(hid, "SCHW", "call", 8000, (TODAY + timedelta(days=10)).isoformat(),
                         1, 250, TODAY.isoformat())
    row = portfolio.options(hid)[0]
    assert row["breakeven_cents"] == 8250
    assert row["collateral_cents"] == 0


def test_closing_an_option_removes_it_from_open(client):
    hid, account = setup_household(client)
    oid = portfolio.add_option(hid, "SCHW", "put", 7500, (TODAY + timedelta(days=30)).isoformat(),
                               1, 100, TODAY.isoformat())
    portfolio.close_option(hid, oid, 40, "closed")
    assert portfolio.option_summary(portfolio.options(hid))["open_count"] == 0
    assert portfolio.options(hid)[0]["status"] == "closed"


# ---------------------------------------------------------------- UI
def test_portfolio_page_and_http_flow(client):
    hid, account = setup_household(client)
    page = client.get("/portfolio")
    assert page.status_code == 200
    csrf = csrf_of(page.text)

    r = client.post("/portfolio/holdings", data={
        "csrf": csrf, "account_id": str(account), "symbol": "schg", "name": "Schwab Growth",
        "kind": "etf", "quantity": "10", "cost_basis": "1,000.00", "price": "120.00",
    }, follow_redirects=False)
    assert r.status_code == 303

    page = client.get("/portfolio")
    assert "SCHG" in page.text
    assert "$1,200.00" in page.text

    hid_row = portfolio.holdings(hid)[0]
    client.post("/portfolio/holdings/save", data={
        "csrf": csrf, f"qty_{hid_row['id']}": "12", f"price_{hid_row['id']}": "130.00",
    }, follow_redirects=False)
    assert portfolio.holdings(hid)[0]["value_cents"] == 12 * 13000

    # Options through the form
    r = client.post("/portfolio/options", data={
        "csrf": csrf, "underlying": "schw", "option_type": "put", "strike": "75.00",
        "expiry_on": (TODAY + timedelta(days=21)).isoformat(), "contracts": "1",
        "premium": "1.35", "opened_on": TODAY.isoformat(), "account_id": str(account),
    }, follow_redirects=False)
    assert r.status_code == 303
    assert portfolio.options(hid)[0]["underlying"] == "SCHW"


def test_home_shows_portfolio_value(client):
    hid, account = setup_household(client)
    portfolio.add_holding(hid, account, "SCHG", 10, 100000, 12000)
    home = client.get("/")
    assert "$1,200.00" in home.text
    assert "/portfolio" in home.text


def test_account_value_rows_take_the_account_type(client):
    hid, account = setup_household(client)
    db.execute("""UPDATE accounts SET include_in_portfolio = 1, type = 'crypto',
                  units_sats = 1000000, current_balance_cents = 500000 WHERE id = ?""", (account,))
    rows = portfolio.account_value_rows(hid, [])
    assert rows and rows[0]["kind_label"] == "Bitcoin"
    labels = [b["label"] for b in portfolio.allocation(rows, "kind")]
    assert labels == ["Bitcoin"]
