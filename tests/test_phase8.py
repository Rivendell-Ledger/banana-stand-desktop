"""Phase 8 acceptance: reports, goals, alerts."""
from __future__ import annotations

import os
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import alerts, budgets, categories, config, db, goals, portfolio, reports, recurring
from app.main import app

PASSWORD = "correct horse battery staple"
TODAY = date.today()
M = budgets.month_key()
PREV = budgets.shift_month(M, -1)


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


def setup_household(client, balance="5000.00", buffer="0.00"):
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


def add_txn(hid, account, cents, day, merchant="Test", category_id=None, pending=0, transfer=0, excluded=0):
    day = day.isoformat() if hasattr(day, "isoformat") else day
    return db.execute(
        """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant, raw_merchant,
               category_id, is_pending, is_transfer, excluded, category_source, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,'import',?,?)""",
        (hid, account, day, cents, merchant, merchant, category_id, pending, transfer, excluded,
         db.utcnow(), db.utcnow()),
    )


# ---------------------------------------------------------------- reports
def test_month_summary_excludes_noise(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    paycheck = cat(hid, "paycheck")
    add_txn(hid, account, 500000, f"{M}-01", "Payroll", paycheck)
    add_txn(hid, account, -10000, f"{M}-03", "Whole Foods", groceries)
    add_txn(hid, account, -5000, f"{M}-04", "Kroger", groceries)
    add_txn(hid, account, -9999, f"{M}-05", "Pending", groceries, pending=1)
    add_txn(hid, account, -8888, f"{M}-06", "Transfer", groceries, transfer=1)
    add_txn(hid, account, -7777, f"{M}-07", "Excluded", groceries, excluded=1)

    summary = reports.month_summary(hid, M)
    assert summary["income_cents"] == 500000
    assert summary["expense_cents"] == 15000
    assert summary["net_cents"] == 485000
    row = next(r for r in summary["rows"] if r["id"] == groceries)
    assert row["spent_cents"] == 15000


def test_category_delta_against_previous_month(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    add_txn(hid, account, -20000, f"{PREV}-10", "Kroger", groceries)
    add_txn(hid, account, -30000, f"{M}-10", "Kroger", groceries)
    row = next(r for r in reports.month_summary(hid, M)["rows"] if r["id"] == groceries)
    assert row["prev_cents"] == 20000
    assert row["delta_cents"] == 10000
    assert round(row["delta_pct"], 1) == 50.0


def test_splits_are_attributed_to_their_lines(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    dining = cat(hid, "dining out")
    parent = db.execute(
        """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant, category_id,
               created_at, updated_at) VALUES (?,?,?,?,?,NULL,?,?)""",
        (hid, account, f"{M}-08", -9000, "Costco", db.utcnow(), db.utcnow()),
    )
    db.execute("INSERT INTO transaction_splits(transaction_id, category_id, amount_cents) VALUES (?,?,?)",
               (parent, groceries, -6000))
    db.execute("INSERT INTO transaction_splits(transaction_id, category_id, amount_cents) VALUES (?,?,?)",
               (parent, dining, -3000))
    rows = {r["id"]: r for r in reports.month_summary(hid, M)["rows"]}
    assert rows[groceries]["spent_cents"] == 6000
    assert rows[dining]["spent_cents"] == 3000


def test_trend_returns_twelve_ordered_months(client):
    hid, account = setup_household(client)
    add_txn(hid, account, -10000, f"{M}-10", "Kroger", cat(hid, "groceries"))
    trend = reports.trend(hid, 12)
    assert len(trend) == 12
    assert trend[-1]["month"] == M
    assert trend[-1]["expense_cents"] == 10000
    assert [t["month"] for t in trend] == sorted(t["month"] for t in trend)


def test_csv_export(client):
    hid, account = setup_household(client)
    add_txn(hid, account, -1234, f"{M}-11", "Blue Bottle", cat(hid, "coffee"))
    page = client.get("/reports")
    assert page.status_code == 200
    resp = client.get(f"/reports/export.csv?month={M}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "Blue Bottle" in resp.text
    assert "-12.34" in resp.text
    assert resp.text.splitlines()[0].startswith("Date,Merchant,Amount")


# ---------------------------------------------------------------- goals
def test_goal_progress_and_pace(client):
    hid, _ = setup_household(client)
    gid = goals.create(hid, "Emergency fund", 1000000, (TODAY + timedelta(days=305)).isoformat())
    goal = goals.get(hid, gid)
    assert goal["pct"] == 0
    assert goal["remaining_cents"] == 1000000
    assert goal["monthly_needed_cents"] is not None
    assert 90000 <= goal["monthly_needed_cents"] <= 110000   # ~10 months out

    goals.contribute(hid, gid, 250000)
    goal = goals.get(hid, gid)
    assert goal["saved_cents"] == 250000
    assert round(goal["pct"], 1) == 25.0


def test_goal_completes_when_target_reached(client):
    hid, _ = setup_household(client)
    gid = goals.create(hid, "New laptop", 100000)
    result = goals.contribute(hid, gid, 100000)
    assert result["achieved"] is True
    assert goals.get(hid, gid)["status"] == "achieved"


def test_goal_behind_pace_detection(client):
    hid, _ = setup_household(client)
    gid = goals.create(hid, "Trip", 1000000, (TODAY + timedelta(days=90)).isoformat())
    db.execute("UPDATE goals SET created_at = ? WHERE id = ?",
               ((TODAY - timedelta(days=270)).isoformat() + "T00:00:00", gid))
    goals.contribute(hid, gid, 100000)
    assert goals.get(hid, gid)["on_track"] is False


def test_goal_delete_removes_contributions(client):
    hid, _ = setup_household(client)
    gid = goals.create(hid, "Fund", 100000)
    goals.contribute(hid, gid, 5000)
    goals.delete(hid, gid)
    assert db.query_one("SELECT COUNT(*) AS n FROM goal_contributions")["n"] == 0


# ---------------------------------------------------------------- alerts
def test_budget_over_alert(client):
    hid, account = setup_household(client)
    groceries = cat(hid, "groceries")
    budgets.set_budget(hid, M, groceries, 10000)
    add_txn(hid, account, -15000, f"{M}-05", "Kroger", groceries)
    items = alerts.evaluate(hid)
    kinds = {a["kind"] for a in items}
    assert "budget_over" in kinds
    over = next(a for a in items if a["kind"] == "budget_over")
    assert over["amount_cents"] == 5000
    assert over["severity"] == "warning"


def test_forecast_breach_alert(client):
    hid, _ = setup_household(client, balance="100.00", buffer="500.00")
    recurring.create(hid, "Rent", 30000, "monthly", (TODAY + timedelta(days=5)).isoformat())
    items = alerts.evaluate(hid)
    breach = next((a for a in items if a["kind"] == "forecast_breach"), None)
    assert breach is not None
    assert breach["severity"] == "critical"
    assert items[0]["kind"] == "forecast_breach"   # critical sorts first


def test_bill_due_and_large_charge_alerts(client):
    hid, account = setup_household(client)
    recurring.create(hid, "Netflix", 1599, "monthly", (TODAY + timedelta(days=2)).isoformat())
    add_txn(hid, account, -60000, TODAY - timedelta(days=1), "Apple Store", cat(hid, "electronics"))
    items = alerts.evaluate(hid)
    kinds = {a["kind"] for a in items}
    assert "bill_due" in kinds
    assert "large_txn" in kinds


def test_large_charge_threshold_is_configurable(client):
    hid, account = setup_household(client)
    add_txn(hid, account, -60000, TODAY - timedelta(days=1), "Apple Store", cat(hid, "electronics"))
    assert any(a["kind"] == "large_txn" for a in alerts.evaluate(hid))
    alerts.set_setting(hid, "alert_large_txn_cents", 100000)
    assert not any(a["kind"] == "large_txn" for a in alerts.evaluate(hid))


def test_alerts_can_be_cleared_for_the_day(client):
    hid, account = setup_household(client)
    add_txn(hid, account, -60000, TODAY - timedelta(days=1), "Apple Store", cat(hid, "electronics"))
    items = alerts.evaluate(hid)
    assert items and all(a["key"] for a in items)

    alerts.dismiss(hid, [a["key"] for a in items])

    assert alerts.evaluate(hid) == []
    assert len(alerts.evaluate(hid, include_dismissed=True)) == len(items)
    # A condition that still holds tomorrow is worth showing again.
    assert alerts.evaluate(hid, today=TODAY + timedelta(days=1))


def test_alert_pages_and_goal_form(client):
    hid, _ = setup_household(client)
    assert client.get("/reports").status_code == 200
    assert client.get("/alerts").status_code == 200
    page = client.get("/goals")
    assert page.status_code == 200
    r = client.post("/goals", data={"csrf": csrf_of(page.text), "name": "Vacation",
                                    "target": "3,000.00", "target_date": "", "account_id": "", "notes": ""},
                    follow_redirects=False)
    assert r.status_code == 303
    row = db.query_one("SELECT * FROM goals WHERE name = 'Vacation'")
    assert row["target_cents"] == 300000

    gid = row["id"]
    page = client.get("/goals")
    client.post(f"/goals/{gid}/contribute", data={"csrf": csrf_of(page.text), "amount": "250.00",
                                                  "occurred_on": TODAY.isoformat(), "note": ""},
                follow_redirects=False)
    assert goals.get(hid, gid)["saved_cents"] == 25000


def test_breakdown_pie_rolls_the_tail_into_other(client):
    from app import categories

    hid, account = setup_household(client)
    for i in range(10):
        cid = categories.create(hid, f"Cat {i}", "expense")
        add_txn(hid, account, -(1000 + i * 100), f"{M}-1{i}", f"Shop {i}", cid)
    pie = reports.month_summary(hid, M)["pie"]
    assert len(pie) == 9, "eight named slices plus Other"
    assert pie[-1]["name"] == "Other"
    assert pie[-1]["cls"] == 8
    assert abs(sum(p["share_pct"] for p in pie) - 100) < 0.05
    assert pie[0]["offset"] == 25, "the first wedge starts at twelve o'clock"
    assert abs(pie[1]["offset"] - (25 - pie[0]["share_pct"])) < 0.01
    assert sum(1 for p in pie if not p["dash"]) == 0


def test_manual_account_can_join_the_portfolio(client):
    from app import portfolio

    hid, account = setup_household(client)
    db.execute("UPDATE accounts SET include_in_portfolio = 1, current_balance_cents = 250000 WHERE id = ?",
               (account,))
    rows = portfolio.holdings(hid)
    extra = portfolio.account_value_rows(hid, rows)
    assert len(extra) == 1
    assert extra[0]["is_account_value"] is True
    assert extra[0]["value_cents"] == 250000
    assert extra[0]["gain_cents"] == 0, "an unknown basis reads flat, not as a gain"
    groups = portfolio.group_by_account(rows + extra)
    assert groups[0]["has_gain"] is False

    db.execute("UPDATE accounts SET include_in_portfolio = 0 WHERE id = ?", (account,))
    assert portfolio.account_value_rows(hid, rows) == []


def test_review_is_one_tap_and_returns_where_it_came_from(client):
    hid, account = setup_household(client)
    txn = add_txn(hid, account, -1200, f"{M}-10", "Walmart", pending=1)
    home = client.get("/")
    assert "Walmart" in home.text
    r = client.post(f"/transactions/{txn}/posted",
                    data={"csrf": csrf_of(home.text), "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert db.query_one("SELECT is_pending FROM transactions WHERE id = ?", (txn,))["is_pending"] == 0


def test_other_rolls_up_the_tail_and_keeps_its_constituents(client):
    hid, account = setup_household(client)
    ids = []
    for i in range(10):
        ids.append(db.execute(
            "INSERT INTO categories(household_id, name, kind, is_system, created_at)"
            " VALUES (?,?, 'expense', 0, ?)", (hid, f"Tail {i}", db.utcnow())))
        add_txn(hid, account, -(100000 - i * 5000), f"{M}-1{i % 9}", f"Tail {i}", ids[-1])

    summary = reports.month_summary(hid, M)
    assert len(summary["pie"]) == reports.PIE_SLICES + 1
    other = summary["pie"][-1]
    assert other["other"] is True and other["name"] == "Other"
    assert len(summary["other_rows"]) == 2
    assert sum(r["share_pct"] for r in summary["other_rows"]) == pytest.approx(other["share_pct"])
    assert [r["name"] for r in summary["other_rows"]] == ["Tail 8", "Tail 9"]

    page = client.get(f"/reports?month={M}").text
    assert 'id="other-details"' in page
    assert "Tail 8" in page and "Share" in page


def test_trend_scale_rounds_the_top_and_labels_every_line(client):
    hid, account = setup_household(client)
    add_txn(hid, account, -10000, f"{M}-05", "Kroger", cat(hid, "groceries"))
    scale = reports.trend_scale(1034766)
    assert scale["top_cents"] >= 1034766
    assert scale["top_cents"] % 3 == 0
    assert len(scale["ticks"]) == 4
    assert scale["ticks"][0] == {"pct": 0.0, "label": "$0"}
    assert scale["ticks"][-1]["pct"] == 100.0
    page = client.get("/reports").text
    assert "trend-gridline" in page and "trend-yaxis" in page


def test_performance_curve_indexes_investments_against_both_benchmarks(client, monkeypatch):
    from app import performance

    hid, account = setup_household(client)
    portfolio.add_holding(hid, account, "NVDA", 2.0, 20000, 10000, kind="stock")

    days = ["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
    nvda = {days[0]: 10000, days[1]: 11000, days[2]: 9900, days[3]: 12100}
    spx = {days[0]: 500000, days[1]: 505000, days[2]: 500000, days[3]: 510000}
    ndx = {days[0]: 1800000, days[1]: 1818000, days[2]: 1790000, days[3]: 1854000}
    monkeypatch.setattr(performance.market, "ensure", lambda symbols, workers=6: None)
    monkeypatch.setattr(performance.market, "bars",
                        lambda s: {"NVDA": nvda, "^GSPC": spx, "^IXIC": ndx}.get(s, {}))

    curve = performance.curve(hid, days=3650)
    assert curve is not None
    assert [line["label"] for line in curve["lines"]] == ["Investments", "S&P 500", "Nasdaq"]
    assert curve["lines"][0]["values"][0] == 100.0
    assert curve["lines"][0]["change_pct"] == 21.0
    assert curve["lines"][1]["change_pct"] == 2.0
    assert curve["lines"][2]["change_pct"] == 3.0
    assert curve["value_cents"] == 24200
    page = client.get("/performance").text
    assert "Investments vs the market" in page and "perf-gspc" in page


def test_credit_card_series_walks_back_through_the_feed(client):
    from app import performance

    hid, account = setup_household(client)
    db.execute("UPDATE accounts SET type = 'credit_card', current_balance_cents = 15000 WHERE id = ?",
               (account,))
    today = date.today()
    for delta, cents in ((2, -10000), (1, -5000)):
        day = (today - timedelta(days=delta)).isoformat()
        add_txn(hid, account, cents, day, "Card shop")

    cc = performance.credit_cards(hid, days=30)
    assert cc is not None
    assert cc["current_cents"] == 15000
    assert cc["values"][0] == 10000, "the first charge lands on day one of the feed"
    assert cc["values"][-1] == 15000 and cc["change_cents"] == 5000
    page = client.get("/accounts/credit-cards").text
    assert "Total card balance" in page and "Owed today" in page


def test_retirement_projects_the_real_accounts_to_age_95(client):
    from app import retirement

    hid, account = setup_household(client)
    db.execute("UPDATE accounts SET name = 'Vanguard Brokerage', type = 'brokerage',"
               " current_balance_cents = 150000000 WHERE id = ?", (account,))
    plan = retirement.load_plan(hid)
    plan["people"][0]["birth_year"] = 1985
    plan["people"][0]["retire_age"] = 65
    plan["spending"] = 60000
    plan["income"][0].update(gross=150000, k401_pct=8, match_pct=50, match_up_to_pct=4)
    plan["ss"] = [{"pia": 3000, "claim_age": 67}, {"pia": 0, "claim_age": 67}]
    result = retirement.project(hid, plan)
    assert result["ready"] and result["succeeded"]
    assert result["totals"]["assets_cents"] == 150000000
    assert result["years"][0]["portfolio_cents"] > 150000000, "growth + contributions land in year one"
    assert result["retirement_year"] == 2050
    assert result["accounts"][0]["type"] == "brokerage"
    assert result["years"][-1]["year"] == 1985 + 95


def test_retirement_page_saves_the_plan_and_shows_the_verdict(client):
    from app import retirement

    hid, account = setup_household(client)
    db.execute("UPDATE accounts SET current_balance_cents = 150000000 WHERE id = ?", (account,))
    page = client.get("/retirement").text
    assert 'id="setup"' in page and "Enter the plan" not in page

    client.post("/retirement/save", data={
        "csrf": csrf_of(page), "p1_name": "Alex", "p1_birth_year": "1985", "p1_retire_age": "65",
        "p2_name": "", "p2_birth_year": "", "p2_retire_age": "65",
        "gross1": "150000", "k401_1": "8", "match_1": "50", "match_cap_1": "4",
        "gross2": "0", "k401_2": "0", "match_2": "0", "match_cap_2": "0",
        "spending": "60000", "return_pct": "6", "inflation_pct": "3", "state_tax_pct": "5.75",
        "max_age": "95", "taxable_basis_pct": "70",
        "ss_pia1": "3000", "ss_claim1": "67", "ss_pia2": "0", "ss_claim2": "67",
        "pension_monthly": "0", "pension_start": "65",
    }, follow_redirects=False)
    saved = retirement.load_plan(hid)
    assert saved["people"][0]["birth_year"] == 1985
    assert saved["spending"] == 60000
    page = client.get("/retirement").text
    assert "Projected at retirement" in page and "Portfolio through retirement" in page
    assert "Where the money is" in page


def test_an_explicit_tax_treatment_beats_the_account_name(client):
    from app import retirement

    hid, account = setup_household(client)
    db.execute("UPDATE accounts SET name = 'BTC IRA', type = 'brokerage',"
               " current_balance_cents = 5000000 WHERE id = ?", (account,))
    # Even inside a brokerage wrapper the name says IRA, so auto reads it as one.
    assert retirement.plan_accounts(hid)[0]["type"] == "trad-ira"

    page = client.get(f"/accounts/{account}").text
    client.post(f"/accounts/{account}/edit", data={
        "csrf": csrf_of(page), "name": "BTC IRA", "type": "brokerage",
        "tax_treatment": "roth", "include_net": "1", "include_portfolio": "1",
    })
    row = retirement.plan_accounts(hid)[0]
    assert row["type"] == "roth-ira" and row["tax_class"] == "taxfree"

    # An explicit call beats both the wrapper and the name.
    client.post(f"/accounts/{account}/edit", data={
        "csrf": csrf_of(page), "name": "BTC IRA", "type": "brokerage",
        "tax_treatment": "taxable", "include_net": "1", "include_portfolio": "1",
    })
    row = retirement.plan_accounts(hid)[0]
    assert row["type"] == "brokerage" and row["tax_class"] == "taxable"


def test_monte_carlo_is_seeded_and_bands_cover_every_year(client):
    from app import retirement

    hid, account = setup_household(client)
    db.execute("UPDATE accounts SET current_balance_cents = 150000000 WHERE id = ?", (account,))
    plan = retirement.load_plan(hid)
    plan["people"][0]["birth_year"] = 1985
    plan["people"][0]["retire_age"] = 60
    plan["spending"] = 60000
    plan["ss"] = [{"pia": 3000, "claim_age": 67}, {"pia": 0, "claim_age": 67}]

    first = retirement.monte_carlo(hid, plan, runs=25, seed=7)
    again = retirement.monte_carlo(hid, plan, runs=25, seed=7)
    assert first["success_pct"] == again["success_pct"]
    assert 0 <= first["success_pct"] <= 100
    assert first["runs"] == 25
    assert len(first["bands"]) == len(first["years"])
    assert first["bands"][0]["p10"] <= first["bands"][0]["p50"] <= first["bands"][0]["p90"]
    # A zero-volatility path must reproduce the deterministic ending balance.
    plan["return_volatility_pct"] = 0
    flat = retirement.monte_carlo(hid, plan, runs=3, seed=1)
    assert flat["median_cents"] == flat["baseline_cents"]


# ---------------------------------------------------------------- retirement depth
def _solo_plan(hid, **overrides):
    from app import retirement

    db.execute("UPDATE accounts SET current_balance_cents = 500000000 WHERE household_id = ?", (hid,))
    plan = retirement.load_plan(hid)
    plan["people"][0].update(birth_year=1985, retire_age=65)
    plan["people"][1].update(birth_year=None)
    plan["income"][0].update(gross=0, frequency="annual", raise_pct=0)
    plan["income"][1].update(gross=0)
    plan["spending"] = 60000
    plan["inflation_pct"] = 0
    plan["return_pct"] = 0
    plan["ss"] = [{"pia": 0, "claim_age": 67}, {"pia": 0, "claim_age": 67}]
    plan.update(overrides)
    return plan


def test_timed_spending_adjustments_stack_on_the_base(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[
        {"name": "College", "annual": 30000, "start_year": 2028, "end_year": 2030, "inflates": False},
    ])
    result = retirement.project(hid, plan)
    spend = {y["year"]: y["spending_cents"] for y in result["years"]}
    assert spend[2027] == 6_000_000
    assert spend[2028] == 9_000_000          # base + college
    assert spend[2030] == 9_000_000
    assert spend[2031] == 6_000_000          # ended


def test_an_adjustment_that_inflates_grows_from_its_start_year(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, inflation_pct=10, adjustments=[
        {"name": "Travel", "annual": 10000, "start_year": 2027, "end_year": None, "inflates": True},
    ])
    result = retirement.project(hid, plan)
    spend = {y["year"]: y["spending_cents"] for y in result["years"]}
    assert spend[2026] == 6_000_000          # travel hasn't started
    assert spend[2027] == 6_600_000 + 1_000_000   # first year: no inflation on the item yet
    assert spend[2028] == 7_260_000 + 1_100_000   # both grow with inflation


def test_frequency_and_raises_scale_the_salary(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[])
    plan["income"][0].update(gross=1000, frequency="weekly", raise_pct=3)
    result = retirement.project(hid, plan)
    first, second = result["years"][0]["employment_cents"], result["years"][1]["employment_cents"]
    assert first == 1000 * 52 * 100
    assert second == int(round(1000 * 52 * 1.03)) * 100


def test_post_tax_savings_land_in_the_named_account(client):
    from app import retirement

    hid, account = setup_household(client)
    db.execute("UPDATE accounts SET type = 'brokerage', name = 'Brokerage' WHERE id = ?", (account,))
    plan = _solo_plan(hid, adjustments=[])
    plan["income"][0].update(gross=100000, savings_monthly=500, savings_account_id=account)
    with_savings = retirement.project(hid, plan)
    plan["income"][0]["savings_monthly"] = 0
    without = retirement.project(hid, plan)
    delta = with_savings["years"][0]["portfolio_cents"] - without["years"][0]["portfolio_cents"]
    assert delta == 6000 * 100                # 12 × $500


def test_a_cost_basis_override_beats_the_default_percentage(client):
    from app import retirement

    hid, account = setup_household(client)
    db.execute("UPDATE accounts SET type = 'brokerage', current_balance_cents = 10000000 WHERE id = ?",
               (account,))
    plan = _solo_plan(hid, basis_by_account={str(account): 25000})
    row = next(r for r in retirement.plan_accounts(hid, plan) if r["id"] == account)
    assert row["basis_cents"] == 2_500_000    # $25,000 of a $100,000 account


def test_a_pretax_withdrawal_before_60_is_flagged(client):
    from app import retirement

    hid, account = setup_household(client)
    db.execute("UPDATE accounts SET type = '401k', tax_treatment = 'traditional',"
               " current_balance_cents = 600000000 WHERE id = ?", (account,))
    plan = _solo_plan(hid, adjustments=[])
    plan["people"][0]["retire_age"] = 45
    plan["spending"] = 400000
    result = retirement.project(hid, plan)
    early = [y for y in result["years"] if y["early"]]
    assert early and early[0]["year"] == 2026    # age 41, no salary, so pre-tax must pay
    assert not any(y["early"] for y in result["years"] if y["age1"] >= 60), \
        "past 59½ the penalty window is closed"


def test_the_retirement_page_is_grouped_into_setup_outlook_and_year_by_year(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = retirement.load_plan(hid)
    plan["people"][0]["birth_year"] = 1985
    retirement.save_plan(hid, plan)
    page = client.get("/retirement").text
    assert 'id="setup"' in page and 'id="outlook"' in page and 'id="year-by-year"' in page
    assert 'name="adj_name_0"' in page and "Timed adjustments" in page
    assert "Roth conversion / yr" in page and "Savings go to" in page


def test_the_save_route_persists_the_new_plan_fields(client):
    from app import retirement

    hid, account = setup_household(client)
    page = client.get("/retirement").text
    r = client.post("/retirement/save", data={
        "csrf": csrf_of(page),
        "p1_name": "Alex", "p1_birth_year": "1985", "p1_retire_age": "65",
        "p2_name": "", "p2_birth_year": "", "p2_retire_age": "65",
        "gross1": "2500", "freq1": "weekly", "raise_1": "3", "k401_1": "8",
        "match_1": "50", "match_cap_1": "4", "savings_1": "500",
        "savings_acct_1": str(account),
        "gross2": "0", "freq2": "annual", "k401_2": "0", "match_2": "0", "match_cap_2": "0",
        "spending": "60000", "return_pct": "6", "return_volatility_pct": "15",
        "inflation_pct": "3", "state_tax_pct": "5.75", "max_age": "95",
        "taxable_basis_pct": "70", "roth_conversion": "10000",
        "ss_pia1": "3000", "ss_claim1": "67", "ss_pia2": "0", "ss_claim2": "67",
        "pension_monthly": "0", "pension_start": "65",
        "adj_name_0": "College", "adj_amount_0": "30000",
        "adj_start_0": "2028", "adj_end_0": "2031", "adj_inflates_0": "1",
        "adj_name_1": "", "adj_amount_1": "",
        f"basis_acct_{account}": "25000",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)

    saved = retirement.load_plan(hid)
    assert saved["income"][0]["frequency"] == "weekly"
    assert saved["income"][0]["raise_pct"] == 3
    assert saved["income"][0]["savings_monthly"] == 500
    assert saved["income"][0]["savings_account_id"] == account
    assert saved["roth_conversion"] == 10000
    assert saved["adjustments"] == [{"name": "College", "annual": 30000,
                                     "start_year": 2028, "end_year": 2031, "inflates": True}]
    assert saved["basis_by_account"] == {str(account): 25000}


def test_crypto_can_grow_at_its_own_rate(client):
    from app import retirement

    hid, brokerage = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], return_pct=0, crypto_return_pct=50, spending=0)
    db.execute("UPDATE accounts SET type = 'brokerage', name = 'Brokerage',"
               " current_balance_cents = 10000000 WHERE id = ?", (brokerage,))
    now = db.utcnow()
    db.execute("INSERT INTO accounts (household_id, name, type, current_balance_cents,"
               " include_in_net_worth, include_in_budget, is_manual, created_at, updated_at)"
               " VALUES (?,?,?,?,1,0,1,?,?)", (hid, "BTC", "crypto", 10000000, now, now))
    result = retirement.project(hid, plan)
    # $100k brokerage flat + $100k BTC at +50%, nothing spent
    assert result["years"][0]["portfolio_cents"] == 25_000_000


def test_a_blank_crypto_rate_follows_the_expected_return(client):
    from app import retirement

    hid, brokerage = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], return_pct=10, crypto_return_pct=None, spending=0)
    db.execute("UPDATE accounts SET type = 'crypto', name = 'BTC',"
               " current_balance_cents = 10000000 WHERE id = ?", (brokerage,))
    result = retirement.project(hid, plan)
    assert result["years"][0]["portfolio_cents"] == 11_000_000


def test_earliest_retirement_is_the_last_age_the_money_still_funds(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], spending=90000, return_pct=5)
    db.execute("UPDATE accounts SET current_balance_cents = 150000000 WHERE household_id = ?", (hid,))
    plan["people"][0]["retire_age"] = 65
    plan["income"][0].update(gross=150000, k401_pct=10, match_pct=50, match_up_to_pct=4)

    e = retirement.earliest_retirement(hid, plan)
    assert e["ready"] and e["feasible"]
    assert e["ages"][0] < 65 and e["years_earlier"] > 0

    # The answer has to actually work...
    at_earliest = dict(plan, people=[dict(plan["people"][0], retire_age=e["ages"][0]),
                                     dict(plan["people"][1])])
    assert retirement.project(hid, at_earliest)["succeeded"]

    # ...and one year earlier must not, unless the search hit its floor.
    if not e["at_floor"]:
        one_earlier = dict(plan, people=[dict(plan["people"][0], retire_age=e["ages"][0] - 1),
                                         dict(plan["people"][1])])
        assert not retirement.project(hid, one_earlier)["succeeded"]


def test_earliest_retirement_says_so_when_the_plan_already_fails(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], spending=900000, return_pct=0)
    e = retirement.earliest_retirement(hid, plan)
    assert e["ready"] and not e["feasible"]
    assert e["fail_year"]


def test_the_retirement_page_uses_tabs_and_lands_on_the_outlook(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = retirement.load_plan(hid)
    plan["people"][0]["birth_year"] = 1985
    retirement.save_plan(hid, plan)
    page = client.get("/retirement").text
    assert page.count('role="tab"') == 3
    assert 'data-tab="setup"' in page and 'data-tab="outlook"' in page
    # Outlook is the landing tab; Setup is present but hidden.
    assert 'id="outlook" data-panel="outlook"' in page
    assert 'id="setup" data-panel="setup" role="tabpanel" aria-labelledby="tabbtn-setup" hidden' in page
    assert "Earliest you could retire" in page


def test_each_person_is_a_collapsible_block_in_order(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = retirement.load_plan(hid)
    plan["people"][0]["name"] = "Mariana"
    plan["people"][1].update(name="Alex", birth_year=1986, retire_age=52)
    retirement.save_plan(hid, plan)
    page = client.get("/retirement").text

    assert page.count('class="person-block"') == 2
    # Both start collapsed so the tab opens as an outline.
    assert 'data-person="1" open' not in page
    assert 'data-person="2" open' not in page
    assert "Person 1 — Mariana" in page and "Person 2 — Alex" in page

    order = [page.index('data-person="1"'), page.index('data-person="2"'),
             page.index("Retirement spending / yr"), page.index("Expected return %"),
             page.index("Social Security &amp; pension")]
    assert order == sorted(order), "people, then spending, then assumptions, then social"


def test_property_stays_out_of_the_projection_and_is_named_on_the_page(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = retirement.load_plan(hid)
    plan["people"][0]["birth_year"] = 1985
    retirement.save_plan(hid, plan)
    now = db.utcnow()
    db.execute("INSERT INTO accounts (household_id, name, type, current_balance_cents,"
               " include_in_net_worth, include_in_budget, is_manual, created_at, updated_at)"
               " VALUES (?,?,?,?,1,0,1,?,?)", (hid, "Zumieo Haus", "real_estate", 60670000, now, now))

    assert "Zumieo Haus" not in [a["name"] for a in retirement.plan_accounts(hid, plan)]
    page = client.get("/retirement").text
    assert "Not counted" in page and "606,700" in page


def test_spending_assumptions_and_social_are_collapsible_blocks(client):
    setup_household(client)
    page = client.get("/retirement").text
    assert page.count('class="section-title"') == 3
    for section in ("spending", "assumptions", "social"):
        assert f'data-section="{section}"' in page
    # Spending is the one people change most, so it opens by default.
    assert '<details class="section-block" data-section="spending" open>' in page
    assert '<details class="section-block" data-section="assumptions">' in page
    assert '<details class="section-block" data-section="social">' in page


def test_a_zero_salary_survives_a_reload(client):
    from app import retirement

    hid, account = setup_household(client)
    page = client.get("/retirement").text
    r = client.post("/retirement/save", data={
        "csrf": csrf_of(page),
        "p1_name": "Mariana", "p1_birth_year": "1971", "p1_retire_age": "55",
        "p2_name": "Alex", "p2_birth_year": "1986", "p2_retire_age": "52",
        "gross1": "0", "freq1": "annual", "gross2": "160000", "freq2": "annual",
        "spending": "83365", "return_pct": "6", "inflation_pct": "3",
        "max_age": "95", "taxable_basis_pct": "70",
        "ss_pia1": "0", "ss_claim1": "67", "ss_pia2": "0", "ss_claim2": "67",
    }, follow_redirects=False)
    assert r.status_code in (302, 303)

    saved = retirement.load_plan(hid)
    assert saved["income"][0]["gross"] == 0
    assert saved["people"][0]["name"] == "Mariana"

    again = client.get("/retirement").text
    assert 'id="gross1"' in again
    marker = again[again.index('id="gross1"'):]
    assert 'value="0.00"' in marker[:200]
    assert retirement.load_plan(hid)["income"][0]["gross"] == 0


def test_unspent_income_lands_in_the_portfolio_instead_of_vanishing(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], spending=40000, return_pct=0, inflation_pct=0)
    plan["income"][0].update(gross=100000, freq1="annual", k401_pct=0, match_pct=0,
                             match_up_to_pct=0, savings_monthly=0)
    db.execute("UPDATE accounts SET current_balance_cents = 1000000 WHERE household_id = ?", (hid,))

    row = retirement.project(hid, plan)["years"][0]
    # 0% growth, so the change is exactly what came in minus what went out.
    assert row["portfolio_cents"] - 1000000 == (
        row["employment_cents"] - row["spending_cents"] - row["tax_cents"])
    assert row["portfolio_cents"] > 1000000


def test_a_529_is_kept_out_of_the_projection_and_labelled(client):
    from app import networth, plaid_sync, retirement

    hid, account = setup_household(client)
    plan = retirement.load_plan(hid)
    plan["people"][0]["birth_year"] = 1985
    retirement.save_plan(hid, plan)
    now = db.utcnow()
    db.execute("INSERT INTO accounts (household_id, name, type, current_balance_cents,"
               " include_in_net_worth, include_in_budget, is_manual, created_at, updated_at)"
               " VALUES (?,?,?,?,1,0,1,?,?)", (hid, "Zoe 529", "529", 4500000, now, now))

    assert plaid_sync.local_type({"type": "investment", "subtype": "529"}) == "529"
    assert networth.TYPE_LABELS["529"] == "529 plan"
    assert "Zoe 529" not in [a["name"] for a in retirement.plan_accounts(hid, plan)]
    page = client.get("/retirement").text
    assert "Not counted" in page and "Zoe 529" in page


def test_the_monte_carlo_names_the_year_the_bottom_decile_runs_out(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], spending=200000, return_pct=4)
    plan["return_volatility_pct"] = 18
    mc = retirement.monte_carlo(hid, plan, runs=120)
    assert mc["success_pct"] < 100
    # Whenever the 10th-percentile ending is zero, say when that decile went dry.
    if mc["p10_cents"] == 0:
        assert mc["p10_runout_year"] >= 2026


def test_the_projection_chart_draws_the_quartile_band(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], spending=60000, return_pct=5)
    retirement.save_plan(hid, plan)
    mc = retirement.monte_carlo(hid, plan, runs=80)
    assert {"p25", "p50", "p75", "mean"} <= set(mc["bands"][0])
    for band in mc["bands"][:5]:
        assert band["p25"] <= band["p50"] <= band["p75"]
    assert mc["chart_bands"]["band"], "band polygon has points"
    assert len(mc["chart_bands"]["median"].split()) == len(mc["bands"])

    page = client.get("/retirement").text
    assert 'class="mc-band"' in page and 'class="mc-median"' in page
    assert "25th–75th percentile" in page and "Median balance" in page
    assert 'class="mc-mean"' in page and "Average balance" in page
    # The mean sits at or above the median once the distribution is skewed.
    assert mc["chart_bands"]["mean_end"] >= mc["chart_bands"]["p50_end"]


def test_the_legend_reads_the_band_when_the_chart_is_dragged(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], spending=60000, return_pct=5)
    retirement.save_plan(hid, plan)
    mc = retirement.monte_carlo(hid, plan, runs=60)
    assert [s["key"] for s in mc["chart_series"]] == ["p25", "p50", "mean", "p75"]
    assert all(len(s["values"]) == len(mc["bands"]) for s in mc["chart_series"])

    page = client.get("/retirement").text
    # Every legend figure must be addressable by the scrub, or it stays frozen.
    for key in ("portfolio", "p25", "p50", "p75", "mean"):
        assert f'data-chart-value="{key}"' in page
    assert "mc-mean" in page


def test_a_broken_plan_is_sanitized_instead_of_crashing(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], spending=50000, return_pct=6)
    plan["return_pct"] = float("nan")
    plan["return_volatility_pct"] = -10
    plan["inflation_pct"] = 400
    plan["max_age"] = 4
    plan["people"][0]["birth_year"] = 1800
    plan["adjustments"] = [{"name": "", "annual": "oops", "start_year": "x", "end_year": "y"}]
    plan["basis_by_account"] = {"3": "not a number"}
    retirement.save_plan(hid, plan)

    loaded = retirement.load_plan(hid)
    assert loaded["return_pct"] == 6.0          # NaN falls back to the default
    assert loaded["inflation_pct"] == 15.0      # clamped
    assert loaded["return_volatility_pct"] == 0
    assert 60 <= loaded["max_age"] <= 110
    assert loaded["adjustments"] == []
    assert loaded["basis_by_account"] == {}
    result = retirement.project(hid, loaded)     # must not raise
    assert result["ready"] in (True, False)


def test_a_negative_adjustment_survives_a_blank_name(client):
    from app import retirement

    hid, account = setup_household(client)
    page = client.get("/retirement").text
    client.post("/retirement/save", data={
        "csrf": csrf_of(page),
        "p1_birth_year": "1985", "p1_retire_age": "60",
        "gross1": "100000", "freq1": "annual",
        "spending": "80000", "return_pct": "6", "inflation_pct": "3", "max_age": "95",
        "ss_pia1": "0", "ss_claim1": "67",
        "adj_name_0": "", "adj_amount_0": "-25200", "adj_start_0": "2042",
        "ss_pia2": "0", "ss_claim2": "67",
    }, follow_redirects=False)
    saved = retirement.load_plan(hid)
    assert [a["annual"] for a in saved["adjustments"]] == [-25200.0]


def test_rmds_and_roth_conversions_are_taxed(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], spending=20000, return_pct=0)
    plan["people"][0].update(birth_year=1955, retire_age=60)   # RMDs start at 75
    plan["income"][0]["gross"] = 0
    db.execute("UPDATE accounts SET type = 'retirement', name = 'IRA',"
               " current_balance_cents = 200000000 WHERE id = ?", (account,))
    retirement.save_plan(hid, plan)
    rows = retirement.project(hid, retirement.load_plan(hid))["years"]
    rmd_year = next(r for r in rows if r["year"] == 2030)      # age 75
    assert rmd_year["rmd_cents"] > 0
    assert rmd_year["tax_cents"] > 0


def test_a_single_earner_keeps_their_own_salary(client):
    from app import retirement

    hid, account = setup_household(client)
    plan = _solo_plan(hid, adjustments=[], spending=30000, return_pct=0)
    # Only person 2 exists, and only person 2 earns: person 1's blank must not be used.
    plan["people"][0].update(birth_year=None, retire_age=65)
    plan["income"][0].update(gross=0)
    plan["people"][1].update(birth_year=1986, retire_age=60)
    plan["income"][1].update(gross=120000, k401_pct=0, match_pct=0, match_up_to_pct=0,
                             savings_monthly=0)
    retirement.save_plan(hid, plan)
    row = retirement.project(hid, retirement.load_plan(hid))["years"][0]
    assert row["employment_cents"] == 12_000_000


def test_the_engine_survives_hostile_plans(client):
    """Fuzz the inputs a stored plan can carry — none may raise."""
    import copy
    import itertools

    from app import retirement

    hid, account = setup_household(client)
    db.execute("UPDATE accounts SET current_balance_cents = 5000000 WHERE id = ?", (account,))
    base = _solo_plan(hid, adjustments=[], spending=40000, return_pct=6)

    weird = {
        "return_pct": [-50, 0, 30],
        "inflation_pct": [-5, 0, 15],
        "max_age": [60, 95, 110],
        "spending": [0, 1e9],
    }
    keys = list(weird)
    for combo in itertools.product(*(weird[k] for k in keys)):
        plan = copy.deepcopy(base)
        for key, value in zip(keys, combo):
            plan[key] = value
        plan["crypto_return_pct"] = -50
        plan["adjustments"] = [{"name": "x", "annual": -1e8, "start_year": 2030, "end_year": None,
                                "inflates": False}]
        sanitized = retirement.load_plan(hid)
        result = retirement.project(hid, {**plan})     # raw, unsanitized on purpose
        assert result["ready"] is True
        assert isinstance(result["final_cents"], int)


def test_review_all_clears_every_pending_charge(client):
    from app import db as appdb

    hid, account = setup_household(client)
    now = appdb.utcnow()

    def add(name, fingerprint):
        appdb.execute(
            "INSERT INTO transactions(household_id, account_id, merchant, amount_cents, posted_on,"
            " is_pending, created_at, updated_at, fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
            (hid, account, name, -1000, "2026-11-20", 1, now, now, fingerprint))

    for i in range(3):
        add(f"Pending {i}", f"bulk{i}")

    page = client.get("/").text
    assert "Reviewed all" in page and 'data-inline-review-all' in page
    assert page.count("pending-row") >= 3

    client.post("/transactions/review-all", data={"csrf": csrf_of(page)})
    left = appdb.query_one(
        "SELECT COUNT(*) AS n FROM transactions WHERE household_id = ? AND is_pending = 1"
        " AND deleted_at IS NULL", (hid,))
    assert left["n"] == 0
    assert "Reviewed all" not in client.get("/").text


def test_one_pending_charge_does_not_offer_review_all(client):
    from app import db as appdb

    hid, account = setup_household(client)
    now = appdb.utcnow()
    appdb.execute(
        "INSERT INTO transactions(household_id, account_id, merchant, amount_cents, posted_on,"
        " is_pending, created_at, updated_at, fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
        (hid, account, "Only one", -900, "2026-11-20", 1, now, now, "solo"))
    page = client.get("/").text
    assert "Only one" in page
    assert "Reviewed all" not in page


def test_auto_budget_averages_only_whole_covered_months(client):
    """A fixed bill must not be divided by a month the ledger never covered."""
    from datetime import date

    from app import budgets

    hid, account = setup_household(client)
    mortgage = cat(hid, "Rent / Mortgage")

    def months_back(delta: int) -> date:
        year, month = date.today().year, date.today().month - delta
        while month <= 0:
            month += 12
            year -= 1
        return date(year, month, 1)

    # The ledger starts on the 15th three months ago. That month holds a grocery run
    # but no mortgage payment, so it must not count as a zero for the mortgage.
    start = months_back(3)
    add_txn(hid, account, -5000, date(start.year, start.month, 15), "Groceries", cat(hid, "groceries"))
    for delta in (2, 1):
        add_txn(hid, account, -121000, months_back(delta), "Mortgage", mortgage)

    target = date.today().strftime("%Y-%m")
    budgets.auto_budget(hid, target, lookback=3)
    row = db.query_one(
        "SELECT budget_cents FROM budgets WHERE household_id = ? AND month = ? AND category_id = ?",
        (hid, target, mortgage))
    assert row["budget_cents"] == 121000        # the bill, not 2/3 of it


def test_performance_frames_pick_the_window(client):
    from datetime import date

    from app import performance

    today = date(2026, 11, 22)
    assert performance.frame_days("30d", today) == 30
    assert performance.frame_days("ytd", today) == 326      # Jan 1 is 325 days back
    assert performance.frame_days("1y", today) == 365
    assert performance.frame_days("nonsense", today) == 365
    # YTD must reach back to the prior year's close, not stop at Jan 1.
    assert today - __import__("datetime").timedelta(days=performance.frame_days("ytd", today)) < date(2026, 1, 1)
