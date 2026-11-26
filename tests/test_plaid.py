"""Phase 3 acceptance: Plaid link, encrypted tokens, sync, update mode, slot limits."""
from __future__ import annotations

import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from app import config, db, plaid, plaid_sync
from app.main import app

PASSWORD = "correct horse battery staple"

ACCOUNTS = [
    {
        "account_id": "plaid-checking",
        "name": "Everyday Checking",
        "mask": "4821",
        "type": "depository",
        "subtype": "checking",
        "balances": {"current": 2500.00, "available": 2400.00, "iso_currency_code": "USD"},
    },
    {
        "account_id": "plaid-card",
        "name": "Sapphire Reserve",
        "mask": "7742",
        "type": "credit",
        "subtype": "credit card",
        "balances": {"current": 1200.00, "available": 3800.00, "iso_currency_code": "USD"},
    },
]

TXNS_V1 = [
    {
        "transaction_id": "txn-a",
        "account_id": "plaid-checking",
        "date": "2026-11-10",
        "amount": 12.34,          # Plaid positive = money out
        "name": "STARBUCKS",
        "merchant_name": "Starbucks",
        "pending": False,
        "personal_finance_category": {"primary": "FOOD_AND_DRINK"},
    },
    {
        "transaction_id": "txn-b",
        "account_id": "plaid-checking",
        "date": "2026-11-09",
        "amount": -500.00,        # Plaid negative = money in
        "name": "PAYROLL",
        "merchant_name": "Whole Foods",
        "pending": False,
        "personal_finance_category": {"primary": "INCOME"},
    },
]


class FakePlaid:
    """Stand-in for plaid._post. Records payloads so tests can assert on them."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.cursor: str | None = None
        self.sync_payloads: list[dict] = []
        self.v1 = list(TXNS_V1)
        self.modified: list[dict] = []
        self.removed: list[dict] = []
        self.accounts = [dict(a) for a in ACCOUNTS]
        self.fail_with: str | None = None

    def __call__(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        if self.fail_with:
            raise plaid.PlaidError(self.fail_with, "nope")
        if path == "/link/token/create":
            return {"link_token": "link-sandbox-test"}
        if path == "/item/public_token/exchange":
            return {"access_token": "access-sandbox-secret-token", "item_id": "item-abc"}
        if path == "/item/get":
            return {"item": {"item_id": "item-abc", "institution_id": "ins_109508"}}
        if path == "/institutions/get_by_id":
            return {"institution": {"name": "Chase"}}
        if path == "/accounts/get":
            return {"accounts": self.accounts}
        if path == "/transactions/sync":
            self.sync_payloads.append(payload)
            if not payload.get("cursor"):
                self.cursor = "cursor-1"
                return {"added": self.v1, "modified": [], "removed": [],
                        "next_cursor": "cursor-1", "has_more": False}
            if payload["cursor"] == "cursor-1":
                return {"added": [], "modified": self.modified, "removed": self.removed,
                        "next_cursor": "cursor-2", "has_more": False}
            return {"added": [], "modified": [], "removed": [],
                    "next_cursor": payload["cursor"], "has_more": False}
        if path == "/item/remove":
            return {"removed": True}
        raise AssertionError(f"unexpected path {path}")


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    db.reset_connections()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(db.config.DB_PATH) + suffix)
        except FileNotFoundError:
            pass
    monkeypatch.setenv("PLAID_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PLAID_SECRET", "test-secret")
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.setenv("PLAID_MAX_ITEMS", "10")
    monkeypatch.setenv("BANANA_ENV", "dev")
    # Tests must never inherit the machine's real .env credentials or caps.
    monkeypatch.setattr(config, "PLAID_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config, "PLAID_SECRET", "test-secret")
    monkeypatch.setattr(config, "PLAID_ENV", "sandbox")
    monkeypatch.setattr(config, "PLAID_MAX_ITEMS", 10)
    monkeypatch.setattr(config, "SECRET_DIR", tmp_path / "secret")
    config.SECRET_DIR.mkdir(parents=True, exist_ok=True)
    db.migrate()
    # The link-token cache lives for the process; a stale token would cross tests.
    from app import main as main_module
    main_module._LINK_TOKENS.clear()
    yield


@pytest.fixture
def fake(monkeypatch):
    f = FakePlaid()
    monkeypatch.setattr(plaid, "_post", f)
    return f


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field"
    return m.group(1)


def sign_up(client):
    page = client.get("/setup")
    client.post("/setup", data={"csrf": csrf_of(page.text), "household": "The Banana Stand",
                                "name": "Alex", "email": "alex@example.com",
                                "password": PASSWORD, "password2": PASSWORD}, follow_redirects=False)
    page = client.get("/login")
    client.post("/login", data={"csrf": csrf_of(page.text), "email": "alex@example.com",
                                "password": PASSWORD}, follow_redirects=False)
    return client


def link_item(client, fake) -> int:
    page = client.get("/settings?tab=bank")
    r = client.post("/plaid/exchange", data={
        "csrf": csrf_of(page.text),
        "public_token": "public-sandbox-1",
        "institution_id": "ins_109508",
        "institution_name": "Chase",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(re.search(r"item=(\d+)", r.headers["location"]).group(1))


# ---------------------------------------------------------------- config
def test_settings_without_config(client, monkeypatch):
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.setattr(config, "PLAID_CLIENT_ID", "")
    sign_up(client)
    page = client.get("/settings?tab=bank")
    assert "not configured" in page.text
    # Desktop builds collect keys in-app instead of pointing at a .env file.
    assert "/settings/plaid" in page.text


def test_link_token_new(client, fake):
    sign_up(client)
    page = client.get("/settings?tab=bank")
    r = client.post("/plaid/link-token", data={"csrf": csrf_of(page.text), "mode": "new"},
                    headers={"X-Requested-With": "fetch"})
    body = r.json()
    assert body["ok"] is True
    assert body["link_token"] == "link-sandbox-test"
    payload = [c for c in fake.calls if c[0] == "/link/token/create"][0][1]
    assert payload["products"] == ["transactions"]
    assert payload["required_if_supported_products"] == ["investments"]   # 529 / brokerage holdings
    assert payload["transactions"] == {"days_requested": 730}   # not Plaid's 90-day default
    assert "access_token" not in payload


# ---------------------------------------------------------------- exchange
def test_exchange_stores_encrypted_token(client, fake, tmp_path):
    sign_up(client)
    item_id = link_item(client, fake)
    row = db.query_one("SELECT * FROM plaid_items WHERE id = ?", (item_id,))
    assert row["plaid_item_id"] == "item-abc"
    assert row["institution_name"] == "Chase"
    assert row["access_token_enc"] != "access-sandbox-secret-token"
    assert plaid.decrypt_token(row["access_token_enc"]) == "access-sandbox-secret-token"

    raw = open(config.DB_PATH, "rb").read()
    assert b"access-sandbox-secret-token" not in raw


def test_duplicate_item_is_refused(client, fake):
    sign_up(client)
    link_item(client, fake)
    page = client.get("/settings?tab=bank")
    r = client.post("/plaid/exchange", data={
        "csrf": csrf_of(page.text), "public_token": "public-sandbox-1",
        "institution_id": "ins_109508", "institution_name": "Chase",
    }, follow_redirects=False)
    assert "already_linked" in r.headers["location"]
    assert db.count("plaid_items") == 1


# ---------------------------------------------------------------- sync
def test_review_and_first_sync(client, fake):
    sign_up(client)
    item_id = link_item(client, fake)

    review = client.get(f"/plaid/review?item={item_id}")
    assert "Everyday Checking" in review.text
    assert "Sapphire Reserve" in review.text
    assert "Create a new account" in review.text

    page = client.get("/settings?tab=bank")
    r = client.post("/plaid/review", data={
        "csrf": csrf_of(page.text),
        "item": str(item_id),
        "plaid_account_id": ["plaid-checking", "plaid-card"],
        "mode_plaid-checking": "new",
        "mode_plaid-card": "new",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "linked=1" in r.headers["location"]

    accounts = db.query_all("SELECT * FROM accounts ORDER BY id")
    assert len(accounts) == 2
    assert accounts[0]["plaid_account_id"] == "plaid-checking"
    assert accounts[0]["is_manual"] == 0
    assert accounts[0]["current_balance_cents"] == 250000
    assert accounts[1]["type"] == "credit_card"
    assert accounts[1]["current_balance_cents"] == 120000

    txns = db.query_all("SELECT * FROM transactions ORDER BY posted_on DESC")
    assert len(txns) == 2
    # Plaid 12.34 out -> local -1234
    assert txns[0]["merchant"] == "Starbucks"
    assert txns[0]["amount_cents"] == -1234
    assert txns[0]["external_id"] == "txn-a"
    # Plaid -500 in -> local +50000, mapped to Income
    income = db.query_one("SELECT id FROM categories WHERE name = 'Income'")["id"]
    assert txns[1]["amount_cents"] == 50000
    assert txns[1]["category_id"] == income

    item = db.query_one("SELECT * FROM plaid_items WHERE id = ?", (item_id,))
    assert item["cursor"] == "cursor-1"
    assert item["status"] == "good"


def test_second_sync_adds_nothing(client, fake):
    sign_up(client)
    item_id = link_item(client, fake)
    page = client.get("/settings?tab=bank")
    client.post("/plaid/review", data={
        "csrf": csrf_of(page.text), "item": str(item_id),
        "plaid_account_id": ["plaid-checking", "plaid-card"],
        "mode_plaid-checking": "new", "mode_plaid-card": "new",
    }, follow_redirects=False)
    before = db.count("transactions")

    settings = client.get("/settings?tab=bank")
    client.post(f"/plaid/sync/{item_id}", data={"csrf": csrf_of(settings.text)}, follow_redirects=False)
    assert db.count("transactions") == before
    assert fake.sync_payloads[-1]["cursor"] == "cursor-1"


def test_modified_and_removed(client, fake):
    sign_up(client)
    item_id = link_item(client, fake)
    page = client.get("/settings?tab=bank")
    client.post("/plaid/review", data={
        "csrf": csrf_of(page.text), "item": str(item_id),
        "plaid_account_id": ["plaid-checking", "plaid-card"],
        "mode_plaid-checking": "new", "mode_plaid-card": "new",
    }, follow_redirects=False)

    fake.modified = [dict(TXNS_V1[0], amount=14.00, merchant_name="Starbucks Reserve")]
    fake.removed = [{"transaction_id": "txn-b"}]
    settings = client.get("/settings?tab=bank")
    client.post(f"/plaid/sync/{item_id}", data={"csrf": csrf_of(settings.text)}, follow_redirects=False)

    a = db.query_one("SELECT * FROM transactions WHERE external_id = 'txn-a'")
    assert a["amount_cents"] == -1400
    assert a["merchant"] == "Starbucks Reserve"
    b = db.query_one("SELECT * FROM transactions WHERE external_id = 'txn-b'")
    assert b["deleted_at"] is not None


def test_merge_into_existing_account(client, fake):
    sign_up(client)
    page = client.get("/accounts/new")
    client.post("/accounts", data={"csrf": csrf_of(page.text), "type": "checking",
                                   "name": "My Checking", "institution": "Chase", "mask": "4821",
                                   "balance": "2500.00", "credit_limit": "", "include_net": "on", "include_budget": "on"},
                follow_redirects=False)
    existing_id = db.query_one("SELECT id FROM accounts")["id"]

    item_id = link_item(client, fake)
    settings = client.get("/settings?tab=bank")
    client.post("/plaid/review", data={
        "csrf": csrf_of(settings.text), "item": str(item_id),
        "plaid_account_id": ["plaid-checking", "plaid-card"],
        "mode_plaid-checking": f"existing:{existing_id}",
        "mode_plaid-card": "new",
    }, follow_redirects=False)

    assert db.count("accounts") == 2  # one merged, one created
    merged = db.query_one("SELECT * FROM accounts WHERE id = ?", (existing_id,))
    assert merged["plaid_account_id"] == "plaid-checking"
    assert merged["name"] == "My Checking"


def test_update_mode_passes_access_token(client, fake):
    sign_up(client)
    item_id = link_item(client, fake)
    page = client.get("/settings?tab=bank")
    r = client.post("/plaid/link-token", data={
        "csrf": csrf_of(page.text), "mode": "update", "item_id": str(item_id),
    }, headers={"X-Requested-With": "fetch"})
    assert r.json()["ok"] is True
    payload = [c for c in fake.calls if c[0] == "/link/token/create"][-1][1]
    assert payload["access_token"] == "access-sandbox-secret-token"
    assert "products" not in payload


def test_login_required_marks_item(client, fake):
    sign_up(client)
    item_id = link_item(client, fake)
    page = client.get("/settings?tab=bank")
    client.post("/plaid/review", data={
        "csrf": csrf_of(page.text), "item": str(item_id),
        "plaid_account_id": ["plaid-checking"], "mode_plaid-checking": "new",
    }, follow_redirects=False)
    fake.fail_with = "ITEM_LOGIN_REQUIRED"
    settings = client.get("/settings?tab=bank")
    client.post(f"/plaid/sync/{item_id}", data={"csrf": csrf_of(settings.text)}, follow_redirects=False)
    row = db.query_one("SELECT status, error_code FROM plaid_items WHERE id = ?", (item_id,))
    assert row["status"] == "login_required"
    assert row["error_code"] == "ITEM_LOGIN_REQUIRED"


def test_slot_limit(client, fake, monkeypatch):
    sign_up(client)
    hid = db.query_one("SELECT household_id FROM users")["household_id"]
    now = db.utcnow()
    for i in range(10):
        db.execute(
            "INSERT INTO plaid_items(household_id, plaid_item_id, access_token_enc, institution_name,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (hid, f"item-{i}", plaid.encrypt_token(f"access-{i}"), f"Bank {i}", "good", now, now),
        )
    page = client.get("/settings?tab=bank")
    assert "10 of 10 Item slots used" in page.text
    r = client.post("/plaid/link-token", data={"csrf": csrf_of(page.text), "mode": "new"},
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 400
    assert "slots" in r.json()["error"]


def test_remove_unlinks_and_keeps_transactions(client, fake):
    sign_up(client)
    item_id = link_item(client, fake)
    page = client.get("/settings?tab=bank")
    client.post("/plaid/review", data={
        "csrf": csrf_of(page.text), "item": str(item_id),
        "plaid_account_id": ["plaid-checking"], "mode_plaid-checking": "new",
    }, follow_redirects=False)
    before = db.count("transactions")

    settings = client.get("/settings?tab=bank")
    client.post(f"/plaid/remove/{item_id}", data={"csrf": csrf_of(settings.text)}, follow_redirects=False)
    assert db.count("plaid_items") == 0
    assert db.count("transactions") == before
    assert db.query_one("SELECT plaid_item_id FROM accounts")["plaid_item_id"] is None


def test_reconnect_refreshes_the_existing_item_instead_of_refusing_it(client, fake):
    """Update mode hands back the same Item; that is a repair, not a duplicate."""
    sign_up(client)
    item_id = link_item(client, fake)
    db.execute("UPDATE plaid_items SET status = 'login_required', error_code = 'ITEM_LOGIN_REQUIRED'"
               " WHERE id = ?", (item_id,))

    page = client.get("/settings?tab=bank")
    r = client.post("/plaid/exchange", data={
        "csrf": csrf_of(page.text), "public_token": "public-sandbox-1",
        "institution_id": "ins_109508", "institution_name": "Chase",
        "item_id": str(item_id), "mode": "update",
    }, follow_redirects=False)
    assert r.headers["location"] == "/settings?reconnected=1", r.headers.get("location")
    assert db.count("plaid_items") == 1
    row = db.query_one("SELECT * FROM plaid_items WHERE id = ?", (item_id,))
    assert row["status"] == "good"
    assert row["error_code"] is None
    assert plaid.decrypt_token(row["access_token_enc"]) == "access-sandbox-secret-token"

    page = client.get("/settings?reconnected=1")
    assert "Reconnected" in page.text


def test_a_new_link_still_refuses_a_duplicate_without_an_item_id(client, fake):
    sign_up(client)
    link_item(client, fake)
    page = client.get("/settings?tab=bank")
    r = client.post("/plaid/exchange", data={
        "csrf": csrf_of(page.text), "public_token": "public-sandbox-1",
        "institution_id": "ins_109508", "institution_name": "Chase", "mode": "new",
    }, follow_redirects=False)
    assert "already_linked" in r.headers["location"]
    assert db.count("plaid_items") == 1


def test_reconnect_without_a_public_token_refreshes_in_place(client, fake):
    sign_up(client)
    item_id = link_item(client, fake)
    db.execute("UPDATE plaid_items SET status = 'login_required' WHERE id = ?", (item_id,))
    page = client.get("/settings?tab=bank")
    r = client.post("/plaid/reconnected", data={"csrf": csrf_of(page.text), "item_id": str(item_id)},
                    follow_redirects=False)
    assert r.headers["location"] == "/settings?reconnected=1"
    row = db.query_one("SELECT * FROM plaid_items WHERE id = ?", (item_id,))
    assert row["status"] == "good" and row["error_code"] is None


def test_link_token_is_reused_inside_its_window(client, fake):
    sign_up(client)
    page = client.get("/settings?tab=bank")
    csrf = csrf_of(page.text)
    first = client.post("/plaid/link-token", data={"csrf": csrf, "mode": "new"},
                        headers={"X-Requested-With": "fetch"}).json()
    second = client.post("/plaid/link-token", data={"csrf": csrf, "mode": "new"},
                         headers={"X-Requested-With": "fetch"}).json()
    assert first["link_token"] == second["link_token"]
    assert second.get("cached") is True
    assert len([c for c in fake.calls if c[0] == "/link/token/create"]) == 1


def test_a_rate_limited_link_token_says_so_plainly(client, fake):
    sign_up(client)
    page = client.get("/settings?tab=bank")
    fake.fail_with = "RATE_LIMIT"
    r = client.post("/plaid/link-token", data={"csrf": csrf_of(page.text), "mode": "new"},
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 429
    body = r.json()
    assert body["ok"] is False and body["rate_limited"] is True
    assert "rate-limiting" in body["error"]
