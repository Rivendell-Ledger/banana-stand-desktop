"""Phase 1 acceptance: manual accounts, balances, net worth snapshots."""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app import db, money, networth
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
    token = csrf_of(page.text)
    client.post("/setup", data={"csrf": token, "household": "The Banana Stand", "name": "Alex",
                                "email": "alex@example.com", "password": PASSWORD, "password2": PASSWORD},
                follow_redirects=False)
    page = client.get("/login")
    client.post("/login", data={"csrf": csrf_of(page.text), "email": "alex@example.com",
                                "password": PASSWORD}, follow_redirects=False)
    return client


def add_account(client, **overrides):
    page = client.get("/accounts/new")
    data = {"csrf": csrf_of(page.text), "type": "checking", "name": "Everyday checking",
            "institution": "Chase", "mask": "4821", "balance": "2,500.00",
            "credit_limit": "", "include_net": "on", "include_budget": "on"}
    data.update(overrides)
    return client.post("/accounts", data=data, follow_redirects=False)


# ---------------------------------------------------------------- unit
def test_parse_money():
    assert money.parse_money("1,234.56") == 123456
    assert money.parse_money("$1,234") == 123400
    assert money.parse_money("1234.5") == 123450
    assert money.parse_money("-12.30") == -1230
    assert money.parse_money("(12.30)") == -1230
    assert money.parse_money("12.30-") == -1230
    assert money.parse_money("") is None
    assert money.parse_money("abc") is None
    assert money.parse_money("12.345") is None


def test_format_money():
    assert money.format_money(123456) == "$1,234.56"
    assert money.format_money(-123456) == "−$1,234.56"
    assert money.format_money(123456, signed=True) == "+$1,234.56"


# ---------------------------------------------------------------- flows
def test_accounts_require_login(client):
    r = client.get("/accounts", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_create_asset_account_updates_net_worth(client):
    sign_up(client)
    r = add_account(client)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/accounts/")

    account = db.query_one("SELECT * FROM accounts")
    assert account["name"] == "Everyday checking"
    assert account["current_balance_cents"] == 250000
    assert account["mask"] == "4821"

    snap = db.query_one("SELECT * FROM net_worth_snapshots")
    assert snap["assets_cents"] == 250000
    assert snap["liabilities_cents"] == 0
    assert snap["net_cents"] == 250000

    home = client.get("/")
    assert "$2,500.00" in home.text


def test_credit_card_is_a_liability(client):
    sign_up(client)
    add_account(client, type="credit_card", name="Sapphire", balance="1200.00")
    add_account(client, type="checking", name="Checking", balance="5000.00")
    snap = db.query_one("SELECT * FROM net_worth_snapshots")
    assert snap["assets_cents"] == 500000
    assert snap["liabilities_cents"] == 120000
    assert snap["net_cents"] == 380000


def test_update_balance_records_history(client):
    sign_up(client)
    add_account(client)
    account_id = db.query_one("SELECT id FROM accounts")["id"]

    page = client.get(f"/accounts/{account_id}")
    r = client.post(f"/accounts/{account_id}/balance",
                    data={"csrf": csrf_of(page.text), "balance": "3,100.50"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.query_one("SELECT current_balance_cents FROM accounts")["current_balance_cents"] == 310050
    assert db.query_one("SELECT net_cents FROM net_worth_snapshots")["net_cents"] == 310050
    assert db.query_one("SELECT COUNT(*) AS n FROM account_balances")["n"] == 1  # same-day upsert


def test_archive_excludes_from_net_worth(client):
    sign_up(client)
    add_account(client)
    account_id = db.query_one("SELECT id FROM accounts")["id"]
    page = client.get(f"/accounts/{account_id}")
    r = client.post(f"/accounts/{account_id}/archive", data={"csrf": csrf_of(page.text)},
                    follow_redirects=False)
    assert r.status_code == 303
    assert db.query_one("SELECT archived_at FROM accounts")["archived_at"] is not None
    assert db.query_one("SELECT net_cents FROM net_worth_snapshots")["net_cents"] == 0


def test_bad_balance_is_rejected(client):
    sign_up(client)
    r = add_account(client, balance="not money")
    assert r.status_code == 400
    assert "look like a number" in r.text
    assert db.count("accounts") == 0


def test_net_worth_page_renders(client):
    sign_up(client)
    add_account(client)
    r = client.get("/net-worth")
    assert r.status_code == 200
    assert "$2,500.00" in r.text
    assert "Assets vs liabilities" in r.text or "Not enough history" in r.text


def test_accounts_are_grouped_by_institution(client):
    sign_up(client)
    add_account(client, name="Everyday checking", institution="Chase", balance="100.00")
    add_account(client, name="Savings", institution="Chase", balance="200.00")
    add_account(client, name="Brokerage", institution="Fidelity", balance="300.00")
    add_account(client, name="Cash in a drawer", institution="", balance="5.00")

    page = client.get("/accounts").text
    # Institution headers are collapsible <summary> rows.
    assert '<summary class="group-head">Chase</summary>' in page
    assert '<summary class="group-head">Fidelity</summary>' in page
    assert '<summary class="group-head">No institution</summary>' in page
    assert page.count('<details class="acct-group" open>') == 3
    # Sections are alphabetical, unassigned last, and each header precedes its rows.
    chase = page.index('class="group-head">Chase')
    fidelity = page.index('class="group-head">Fidelity')
    other = page.index('class="group-head">No institution')
    assert chase < fidelity < other
    assert chase < page.index("Everyday checking") < fidelity
    assert fidelity < page.index("Brokerage") < other
    assert other < page.index("Cash in a drawer")


def test_real_estate_is_an_asset_type():
    """A house belongs on the balance sheet, not in safe to spend."""
    from app import networth

    assert networth.TYPE_LABELS["real_estate"] == "Real estate"
    assert networth.TYPE_GROUPS["real_estate"] == "asset"
    assert networth.is_liability("real_estate") is False


def test_net_worth_frames_clip_the_series_and_admit_short_history(client):
    """30D on a two-day history says so; a real 30-day history reports the full frame."""
    from datetime import date, timedelta
    sign_up(client)
    hid = db.query_one("SELECT id FROM households")["id"]
    last = date.today()
    points = [(last - timedelta(days=60), 100_000_00), (last - timedelta(days=20), 110_000_00),
              (last, 125_000_00)]
    for day, net in points:
        db.execute("INSERT OR REPLACE INTO net_worth_snapshots"
                   "(as_of_date, household_id, assets_cents, liabilities_cents, net_cents, created_at)"
                   " VALUES (?,?,?,?,?,?)", (day.isoformat(), hid, net, 0, net, db.utcnow()))

    # Nothing sits at the 30-day mark, so the base is the last snapshot before it and the
    # frame is reported as approximate ("since Sep 23") rather than a tidy 30 days.
    thirty = networth.window(hid, "30d")
    assert thirty["from"]["as_of_date"] == (last - timedelta(days=60)).isoformat()
    assert thirty["delta"] == 25_000_00
    assert thirty["approx"] is True and thirty["days"] == 60

    everything = networth.window(hid, "all")
    assert everything["from"]["as_of_date"] == (last - timedelta(days=60)).isoformat()
    assert everything["delta"] == 25_000_00 and everything["approx"] is False
    assert len(everything["rows"]) == 3


def test_the_nightly_snapshot_records_balances_once_per_day(client):
    """Re-running the nightly job updates the day's row instead of stacking duplicates."""
    from datetime import date
    sign_up(client)
    add_account(client)
    hid = db.query_one("SELECT id FROM households")["id"]

    first = networth.snapshot(hid)
    today = date.today().isoformat()
    assert first["as_of"] == today and first["accounts"] == 1
    networth.snapshot(hid)

    snapshots = db.query_all("SELECT * FROM net_worth_snapshots WHERE household_id = ? AND as_of_date = ?",
                             (hid, today))
    assert len(snapshots) == 1
    assert snapshots[0]["net_cents"] == 250_000  # the $2,500 account balance
    balances = db.query_all("SELECT * FROM account_balances WHERE as_of_date = ?", (today,))
    assert len(balances) == 1 and balances[0]["source"] == "nightly"
