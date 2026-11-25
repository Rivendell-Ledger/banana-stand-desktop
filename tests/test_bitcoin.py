"""Bitcoin accounts: BTC quantities entered in sats, priced live into net worth."""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from app import bitcoin, config, db, money, networth
from app.main import app

PASSWORD = "correct horse battery staple"
BTC_PRICE = 6_000_000  # $60,000.00


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
    monkeypatch.setattr(config, "SECRET_DIR", tmp_path / "secret")
    config.SECRET_DIR.mkdir(parents=True, exist_ok=True)
    db.migrate()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def stub_price(monkeypatch):
    """Never touch the network from tests."""
    monkeypatch.setattr(bitcoin, "price_cents", lambda force=False: BTC_PRICE)


def sign_up(client) -> None:
    page = client.get("/setup")
    r = client.post(
        "/setup",
        data={
            "csrf": csrf_of(page.text),
            "household": "The Banana Stand",
            "name": "Mariana",
            "email": "mariana",
            "password": PASSWORD,
            "password2": PASSWORD,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get("/login")
    client.post(
        "/login",
        data={"csrf": csrf_of(page.text), "email": "mariana", "password": PASSWORD},
        follow_redirects=False,
    )


def add_account(client, **fields) -> int:
    page = client.get("/accounts/new")
    data = {"csrf": csrf_of(page.text), "type": "checking", "name": "Account",
            "institution": "", "mask": "", "balance": "0.00", "include_net": "on",
            "include_budget": "on"}
    data.update(fields)
    r = client.post("/accounts", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(re.search(r"/accounts/(\d+)", r.headers["location"]).group(1))


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    "text,sats",
    [
        ("0.25", 25_000_000),
        (".5", 50_000_000),
        ("1,234.56789012", 123_456_789_012),
        ("0.5 BTC", 50_000_000),
        ("0.00000001", 1),
        ("1", 100_000_000),
        (" 2.0 ", 200_000_000),
    ],
)
def test_parse_btc(text, sats):
    assert money.parse_btc(text) == sats


@pytest.mark.parametrize("text", ["", "-0.5", "abc", "0.123456789", "1.2.3", None])
def test_parse_btc_rejects(text):
    assert money.parse_btc(text) is None


def test_format_btc_trims_and_commas():
    assert money.format_btc(25_000_000) == "0.25"
    assert money.format_btc(1) == "0.00000001"
    assert money.format_btc(123_456_789_012) == "1,234.56789012"
    assert money.format_btc(100_000_000) == "1"


# ---------------------------------------------------------------- accounts
def test_bitcoin_account_is_entered_in_btc(client):
    sign_up(client)
    account_id = add_account(client, type="crypto", name="Cold storage", balance="0.25")

    row = db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    assert row["units_sats"] == 25_000_000
    assert row["current_balance_cents"] == 1_500_000  # 0.25 x $60,000
    assert row["include_in_budget"] == 1  # BTC counts toward safe to spend
    assert networth.TYPE_LABELS[row["type"]] == "Bitcoin"

    page = client.get("/accounts")
    assert "Bitcoin" in page.text
    assert "0.25 BTC" in page.text
    assert "$15,000.00" in page.text


def test_balance_update_accepts_btc_units(client):
    sign_up(client)
    account_id = add_account(client, type="crypto", name="BTC", balance="0.25")
    page = client.get(f"/accounts/{account_id}")
    assert "0.25 BTC" in page.text
    assert "$15,000.00" in page.text

    r = client.post(f"/accounts/{account_id}/balance",
                    data={"csrf": csrf_of(page.text), "balance": "1.5"},
                    follow_redirects=False)
    assert r.status_code == 303
    row = db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    assert row["units_sats"] == 150_000_000
    assert row["current_balance_cents"] == 9_000_000  # 1.5 x $60,000


def test_balance_update_rejects_dollars_for_bitcoin(client):
    sign_up(client)
    account_id = add_account(client, type="crypto", name="BTC", balance="0.25")
    page = client.get(f"/accounts/{account_id}")
    r = client.post(f"/accounts/{account_id}/balance",
                    data={"csrf": csrf_of(page.text), "balance": "abc"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error=balance" in r.headers["location"]
    row = db.query_one("SELECT units_sats FROM accounts WHERE id = ?", (account_id,))
    assert row["units_sats"] == 25_000_000


def test_price_refresh_moves_net_worth(client, monkeypatch):
    sign_up(client)
    add_account(client, type="crypto", name="BTC", balance="0.5")  # $30,000
    assert networth.totals(networth.list_accounts(1))["assets"] == 3_000_000

    monkeypatch.setattr(bitcoin, "price_cents", lambda force=False: 12_000_000)  # $120k
    tot = networth.totals(networth.list_accounts(1, ))
    assert tot["assets"] == 3_000_000  # nothing repriced until refresh runs
    bitcoin.recompute(1)
    assert networth.totals(networth.list_accounts(1))["assets"] == 6_000_000


def test_switching_an_account_to_bitcoin_preserves_value(client):
    sign_up(client)
    account_id = add_account(client, name="Savings", balance="600.00")
    page = client.get(f"/accounts/{account_id}")
    r = client.post(f"/accounts/{account_id}/edit", data={
        "csrf": csrf_of(page.text), "name": "Savings", "institution": "", "mask": "",
        "type": "crypto", "credit_limit": "", "include_net": "on",
    }, follow_redirects=False)
    assert r.status_code == 303
    row = db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    assert row["units_sats"] == 1_000_000  # $600 at $60k = 0.01 BTC
    assert row["current_balance_cents"] == 60_000

    # And switching back clears the quantity so the dollars are literal again.
    page = client.get(f"/accounts/{account_id}")
    client.post(f"/accounts/{account_id}/edit", data={
        "csrf": csrf_of(page.text), "name": "Savings", "institution": "", "mask": "",
        "type": "savings", "credit_limit": "", "include_net": "on",
    }, follow_redirects=False)
    row = db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    assert row["units_sats"] is None
    assert row["current_balance_cents"] == 60_000


def test_bitcoin_can_be_kept_out_of_safe_to_spend(client):
    from app import budgets

    sign_up(client)
    add_account(client, type="crypto", name="BTC", balance="0.5")  # $30,000, counted
    assert budgets.safe_to_spend(1)["cash_cents"] == 3_000_000

    # Bitcoin held inside an IRA is not spendable cash.
    add_account(client, type="crypto", name="BTC IRA", balance="1.0", include_budget="")
    assert budgets.safe_to_spend(1)["cash_cents"] == 3_000_000
    row = db.query_one("SELECT include_in_budget FROM accounts WHERE name = 'BTC IRA'")
    assert row["include_in_budget"] == 0


def test_safe_to_spend_toggle_from_account_edit(client):
    from app import budgets

    sign_up(client)
    account_id = add_account(client, type="crypto", name="BTC IRA", balance="1.0")
    assert budgets.safe_to_spend(1)["cash_cents"] == 6_000_000

    page = client.get(f"/accounts/{account_id}")
    assert 'name="include_budget"' in page.text
    r = client.post(f"/accounts/{account_id}/edit", data={
        "csrf": csrf_of(page.text), "name": "BTC IRA", "institution": "", "mask": "",
        "type": "crypto", "credit_limit": "", "include_net": "on", "include_budget": "",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert budgets.safe_to_spend(1)["cash_cents"] == 0
