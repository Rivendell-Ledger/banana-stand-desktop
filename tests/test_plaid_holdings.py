"""Plaid Investments: map broker positions into the Portfolio without double counting."""
from __future__ import annotations

import os

import pytest

from app import db, plaid, plaid_sync


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    db.reset_connections()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(db.config.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    db.migrate()
    monkeypatch.setattr(plaid, "decrypt_token", lambda value: "access-fake")
    yield


def seed(archived: bool = False):
    now = db.utcnow()
    hid = db.execute(
        "INSERT INTO households(name, currency, timezone, buffer_cents, week_start, created_at)"
        " VALUES ('H','USD','America/New_York',0,6,?)", (now,))
    db.execute(
        "INSERT INTO plaid_items(household_id, plaid_item_id, access_token_enc, institution_id,"
        " institution_name, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,'good',?,?)",
        (hid, "item-1", "enc", "ins_11", "Charles Schwab", now, now))
    item = dict(db.query_one("SELECT * FROM plaid_items WHERE plaid_item_id = 'item-1'"))
    account_id = db.execute(
        "INSERT INTO accounts(household_id, institution, name, type, currency,"
        " current_balance_cents, include_in_net_worth, include_in_budget, is_manual,"
        " plaid_item_id, plaid_account_id, archived_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,1,1,0,?,?,?,?,?)",
        (hid, "Charles Schwab", "Brokerage", "brokerage", "USD", 1_000_000,
         "item-1", "acct-1", db.utcnow() if archived else None, now, now))
    return hid, item, account_id


def body(quantity=10, price=50.0, cost=450.0):
    return {
        "securities": [
            {"security_id": "sec-1", "ticker_symbol": "SCHG", "name": "Schwab Growth",
             "type": "etf"},
        ],
        "holdings": [
            {"account_id": "acct-1", "security_id": "sec-1", "quantity": quantity,
             "institution_price": price, "cost_basis": cost},
        ],
    }


def test_holdings_land_in_the_portfolio(monkeypatch):
    hid, item, account_id = seed()
    monkeypatch.setattr(plaid, "investments_holdings_get", lambda token: body())

    summary = plaid_sync.sync_holdings(hid, item)

    assert summary == {"securities": 1, "holdings": 1, "removed": 0, "error": None}
    row = db.query_one(
        "SELECT h.*, s.symbol, s.kind, s.external_id FROM holdings h"
        " JOIN securities s ON s.id = h.security_id")
    assert row["symbol"] == "SCHG"
    assert row["kind"] == "etf"
    assert row["external_id"] == "sec-1"
    assert row["quantity"] == 10
    assert row["price_cents"] == 5000            # $50.00 per share
    # Plaid's cost_basis is the position's total cost ($450), not per share.
    assert row["cost_basis_cents"] == 45_000
    assert row["quantity"] * row["price_cents"] - row["cost_basis_cents"] == 5_000
    assert row["source"] == "plaid"

    # The institution's balance stays authoritative: positions do not overwrite it.
    assert db.query_one("SELECT current_balance_cents FROM accounts WHERE id = ?",
                        (account_id,))["current_balance_cents"] == 1_000_000


def test_resync_updates_in_place_and_drops_closed_positions(monkeypatch):
    hid, item, _ = seed()
    monkeypatch.setattr(plaid, "investments_holdings_get", lambda token: body())
    plaid_sync.sync_holdings(hid, item)

    monkeypatch.setattr(plaid, "investments_holdings_get", lambda token: body(quantity=12, price=55.0))
    plaid_sync.sync_holdings(hid, item)
    assert db.count("holdings") == 1
    assert db.query_one("SELECT quantity, price_cents FROM holdings")["price_cents"] == 5500

    monkeypatch.setattr(plaid, "investments_holdings_get",
                        lambda token: {"securities": [], "holdings": []})
    summary = plaid_sync.sync_holdings(hid, item)
    assert summary["removed"] == 1
    assert db.count("holdings") == 0


def test_archived_accounts_are_skipped(monkeypatch):
    hid, item, _ = seed(archived=True)
    monkeypatch.setattr(plaid, "investments_holdings_get", lambda token: body())
    summary = plaid_sync.sync_holdings(hid, item)
    assert summary["holdings"] == 0
    assert db.count("holdings") == 0


def test_missing_consent_is_reported_not_raised(monkeypatch):
    hid, item, _ = seed()

    def deny(token):
        raise plaid.PlaidError("ADDITIONAL_CONSENT_REQUIRED", "consent needed")

    monkeypatch.setattr(plaid, "investments_holdings_get", deny)
    summary = plaid_sync.sync_holdings(hid, item)
    assert summary["error"] == "ADDITIONAL_CONSENT_REQUIRED"
    assert db.count("holdings") == 0
