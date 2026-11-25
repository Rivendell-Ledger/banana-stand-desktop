"""Regression tests from the red-team audit (security + dead UI)."""
from __future__ import annotations

import os
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import budgets, categories, config, db, networth
from app.main import app, safe_next

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


def add_account(client, name="Checking", balance="1000.00"):
    page = client.get("/accounts/new")
    client.post("/accounts", data={"csrf": csrf_of(page.text), "type": "checking", "name": name,
                                   "institution": "Chase", "mask": "4821", "balance": balance,
                                   "credit_limit": "", "include_net": "on", "include_budget": "on"}, follow_redirects=False)
    return db.query_one("SELECT id FROM accounts WHERE name = ?", (name,))["id"]


def add_txn(client, account, cents, merchant, day=None):
    page = client.get("/transactions")
    hid = db.query_one("SELECT id FROM households")["id"]
    return db.execute(
        """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant, raw_merchant,
               created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
        (hid, account, (day or TODAY).isoformat(), cents, merchant, merchant, db.utcnow(), db.utcnow()),
    )


# ---------------------------------------------------------------- open redirect
def test_safe_next_rejects_protocol_relative_and_backslashes():
    assert safe_next("/transactions?q=x") == "/transactions?q=x"
    assert safe_next("//evil.com") == "/transactions"
    assert safe_next("/\\evil.com") == "/transactions"
    assert safe_next("https://evil.com") == "/transactions"
    assert safe_next("") == "/transactions"
    assert safe_next("javascript:alert(1)") == "/transactions"


def test_bulk_redirect_ignores_hostile_next(client):
    sign_up(client)
    account = add_account(client)
    tid = add_txn(client, account, -500, "Shop")
    page = client.get("/transactions")
    r = client.post("/transactions/bulk", data={
        "csrf": csrf_of(page.text), "ids": str(tid), "op": "delete", "next": "//evil.com/x",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/transactions")


# ---------------------------------------------------------------- bulk undo
def test_bulk_delete_offers_a_working_undo(client):
    sign_up(client)
    account = add_account(client)
    a = add_txn(client, account, -500, "Alpha")
    b = add_txn(client, account, -700, "Beta")
    page = client.get("/transactions")
    csrf = csrf_of(page.text)

    r = client.post("/transactions/bulk", data={
        "csrf": csrf, "ids": [str(a), str(b)], "op": "delete", "next": "/transactions",
    }, follow_redirects=False)
    location = r.headers["location"]
    assert "undo=2" in location and f"ids={a}%2C{b}" in location or "ids=" in location
    assert db.query_one("SELECT COUNT(*) AS n FROM transactions WHERE deleted_at IS NOT NULL")["n"] == 2

    # The banner renders a form carrying those ids back to restore.
    page = client.get(location)
    assert "Undo" in page.text
    und_ids = re.findall(r'name="ids" value="(\d+)"', page.text)
    assert sorted(und_ids) == sorted([str(a), str(b)])
    r = client.post("/transactions/bulk", data={
        "csrf": csrf, "ids": und_ids, "op": "restore", "next": "/transactions",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert db.query_one("SELECT COUNT(*) AS n FROM transactions WHERE deleted_at IS NOT NULL")["n"] == 0


def test_bulk_still_accepts_legacy_action_field(client):
    sign_up(client)
    account = add_account(client)
    tid = add_txn(client, account, -500, "Gamma")
    page = client.get("/transactions")
    client.post("/transactions/bulk", data={
        "csrf": csrf_of(page.text), "ids": str(tid), "action": "exclude", "next": "/transactions",
    }, follow_redirects=False)
    assert db.query_one("SELECT excluded FROM transactions WHERE id = ?", (tid,))["excluded"] == 1


# ---------------------------------------------------------------- archived accounts
def test_archived_accounts_are_excluded_from_totals_and_restorable(client):
    sign_up(client)
    keep = add_account(client, "Checking", "1000.00")
    gone = add_account(client, "Old Savings", "500.00")
    page = client.get(f"/accounts/{gone}")
    csrf = csrf_of(page.text)
    client.post(f"/accounts/{gone}/archive", data={"csrf": csrf}, follow_redirects=False)

    page = client.get("/accounts?archived=1")
    assert "Archived" in page.text
    assert "Old Savings" in page.text
    assert "excluded from totals" in page.text
    # Totals must only count the live account.
    assert "$1,000.00" in page.text
    assert "$1,500.00" not in page.text

    # And the restore control actually restores.
    r = client.post(f"/accounts/{gone}/restore", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert db.query_one("SELECT archived_at FROM accounts WHERE id = ?", (gone,))["archived_at"] is None


# ---------------------------------------------------------------- bad input
@pytest.mark.parametrize("url", [
    "/transactions?account_id=abc",
    "/transactions?category_id=xyz",
    "/transactions?tag=%00",
    "/reports?month=99-99",
    "/reports?month=nonsense",
    "/forecast?days=abc",
    "/forecast?days=-500",
    "/budget?month=zzz",
])
def test_malformed_query_params_do_not_500(client, url):
    sign_up(client)
    r = client.get(url)
    assert r.status_code == 200, f"{url} -> {r.status_code}"


# ---------------------------------------------------------------- rules feedback
def test_rule_apply_reports_what_happened(client):
    sign_up(client)
    account = add_account(client)
    add_txn(client, account, -500, "STARBUCKS #123")
    page = client.get("/rules/new")
    client.post("/rules/new", data={
        "csrf": csrf_of(page.text), "name": "Coffee",
        "field0": "merchant", "op0": "contains", "value0": "starbucks",
        "field1": "", "op1": "contains", "value1": "", "field2": "", "op2": "contains", "value2": "",
        "action_type": "category", "action_value": str(categories.list_categories(
            db.query_one("SELECT id FROM households")["id"])[0]["id"]),
    }, follow_redirects=False)
    rid = db.query_one("SELECT id FROM category_rules")["id"]
    page = client.get("/rules")
    client.post(f"/rules/{rid}/apply", data={"csrf": csrf_of(page.text)}, follow_redirects=False)
    page = client.get(f"/rules?applied=1")
    assert page.status_code == 200
    assert "transaction" in page.text and ("updated by the rule" in page.text or "No uncategorised" in page.text)


# ---------------------------------------------------------------- settings household
def test_household_name_and_buffer_are_editable(client):
    sign_up(client)
    page = client.get("/settings")
    assert "Buffer" in page.text
    r = client.post("/settings/household", data={
        "csrf": csrf_of(page.text), "household": "The Stand", "buffer": "2,500.00",
    }, follow_redirects=False)
    assert r.status_code == 303
    row = db.query_one("SELECT name, buffer_cents FROM households")
    assert row["name"] == "The Stand"
    assert row["buffer_cents"] == 250000

    # And the buffer changes safe-to-spend.
    hid = db.query_one("SELECT id FROM households")["id"]
    safe = budgets.safe_to_spend(hid)
    assert safe["buffer_cents"] == 250000


# ---------------------------------------------------------------- webhook + export
def test_plaid_webhook_is_closed_when_unconfigured(client, monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "")
    monkeypatch.setenv("PLAID_SECRET", "")
    monkeypatch.setattr(config, "PLAID_CLIENT_ID", "")
    monkeypatch.setattr(config, "PLAID_SECRET", "")
    r = client.post("/webhooks/plaid", json={"item_id": "nope"})
    assert r.status_code == 404


def test_csv_export_neutralises_formula_cells(client):
    sign_up(client)
    account = add_account(client)
    add_txn(client, account, -500, "=HYPERLINK(\"http://evil\")")
    month = budgets.month_key()
    r = client.get(f"/reports/export.csv?month={month}")
    assert r.status_code == 200
    assert "'=HYPERLINK" in r.text
    assert "\n=HYPERLINK" not in r.text


# ---------------------------------------------------------------- CSP-safe UI
def test_no_inline_event_handlers_or_scripts():
    """Strict CSP (script-src 'self') silently disables inline handlers, so they must not exist."""
    import re as _re
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parent.parent / "app" / "templates"
    offenders = []
    for f in root.rglob("*.html"):
        text = f.read_text()
        for m in _re.finditer(r"\bon(click|change|submit|input|load)=", text):
            offenders.append((f.name, m.group(0)))
        # External <script src=...> is fine; an inline block is not.
        for m2 in _re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>", text):
            offenders.append((f.name, "inline script"))
    assert offenders == [], offenders


def test_audit_filter_and_confirm_use_data_attributes(client):
    sign_up(client)
    page = client.get("/settings/audit")
    assert "data-autosubmit" in page.text
    assert "onchange=" not in page.text
    page = client.get("/goals")
    client.post("/goals", data={"csrf": csrf_of(page.text), "name": "Car", "target": "1000.00",
                                "target_date": "", "account_id": "", "notes": ""}, follow_redirects=False)
    page = client.get("/goals")
    assert "data-confirm=" in page.text
    assert "onclick=" not in page.text


def test_parent_tab_stays_active_on_child_pages(client):
    sign_up(client)
    add_account(client)
    for path in ("/bills", "/forecast", "/goals"):
        page = client.get(path)
        assert page.status_code == 200
        assert 'class="active" href="/budget"' in page.text or 'href="/budget" class="active"' in page.text, path
    # Reports graduated to a primary tab of its own.
    page = client.get("/reports")
    assert page.status_code == 200
    assert 'href="/reports" class="active"' in page.text or 'class="active" href="/reports"' in page.text
    assert 'href="/budget" class="active"' not in page.text
    page = client.get("/portfolio")
    assert 'href="/accounts" class="active"' in page.text or 'class="active" href="/accounts"' in page.text
    # Net worth is a primary tab of its own, not part of the Accounts group.
    page = client.get("/net-worth")
    assert 'href="/net-worth" class="active"' in page.text or 'class="active" href="/net-worth"' in page.text
    assert 'href="/accounts" class="active"' not in page.text


def test_archived_section_renders_with_restore(client):
    sign_up(client)
    gone = add_account(client, "Old Savings", "500.00")
    page = client.get(f"/accounts/{gone}")
    client.post(f"/accounts/{gone}/archive", data={"csrf": csrf_of(page.text)}, follow_redirects=False)
    page = client.get("/accounts")
    assert 'href="/accounts?archived=1"' in page.text
    page = client.get("/accounts?archived=1")
    assert f'/accounts/{gone}/restore' in page.text
    assert "excluded from totals" in page.text


# ---------------------------------------------------------------- username login
def test_login_handle_can_be_a_plain_username(client):
    from app import security
    assert security.valid_login("alex")
    assert security.valid_login("alex@example.com")
    assert security.valid_login("z.m.c-1")
    assert not security.valid_login("a")
    assert not security.valid_login("has space")
    assert not security.valid_login("bad!")

    page = client.get("/setup")
    r = client.post("/setup", data={"csrf": csrf_of(page.text), "household": "The Stand",
                                    "name": "Alex Rivera",
                                    "password": PASSWORD, "password2": PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 303
    user = db.query_one("SELECT email, display_name FROM users")
    assert user["email"] == "alex rivera"
    assert user["display_name"] == "Alex Rivera"

    page = client.get("/login")
    r = client.post("/login", data={"csrf": csrf_of(page.text), "email": "  ALEX   RIVERA ",
                                    "password": PASSWORD}, follow_redirects=False)
    # Desktop builds route a fresh household into the guided bank-connect step.
    assert r.headers["location"] == "/setup/connect"
