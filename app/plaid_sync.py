"""Apply Plaid data to the local ledger: accounts, balances, transactions."""
from __future__ import annotations

import logging
import time
from datetime import date

from . import db, networth, plaid, portfolio, txns

logger = logging.getLogger("banana.plaid")

REFRESH_SETTLE_SECONDS = 6   # give the institution a moment to answer the refresh
MAX_PAGES = 20

TYPE_MAP = {
    ("depository", "checking"): "checking",
    ("depository", "savings"): "savings",
    ("depository", "cd"): "savings",
    ("depository", "money market"): "savings",
    ("depository", "hsa"): "hsa",
    ("credit", "credit card"): "credit_card",
    ("loan", "mortgage"): "mortgage",
    ("loan", "student"): "loan",
    ("loan", "auto"): "loan",
    ("loan", "personal"): "loan",
    ("investment", "brokerage"): "brokerage",
    ("investment", "401k"): "retirement",
    ("investment", "ira"): "retirement",
    ("investment", "roth"): "retirement",
    ("investment", "roth 401k"): "retirement",
    ("investment", "529"): "529",
}


def local_type(account: dict) -> str:
    kind = (account.get("type") or "").lower()
    sub = (account.get("subtype") or "").lower()
    if (kind, sub) in TYPE_MAP:
        return TYPE_MAP[(kind, sub)]
    if kind == "depository":
        return "checking"
    if kind == "investment":
        return "brokerage"
    if kind == "credit":
        return "credit_card"
    if kind == "loan":
        return "loan"
    return "other_asset"


def cents(amount) -> int:
    try:
        return int(round(float(amount) * 100))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- accounts
def import_accounts(household_id: int, item_pk: int, choices: list[dict]) -> dict:
    """Create or link local accounts for a freshly linked Item.

    choices: [{"plaid_account_id", "mode": "new"|"existing"|"skip", "account_id"?}]
    """
    now = db.utcnow()
    created = linked = skipped = 0
    for choice in choices:
        plaid_id = choice.get("plaid_account_id")
        mode = choice.get("mode") or "new"
        if not plaid_id or mode == "skip":
            skipped += 1
            continue
        acct = choice.get("plaid_account") or {}
        if mode == "existing" and choice.get("account_id"):
            db.execute(
                "UPDATE accounts SET plaid_item_id = ?, plaid_account_id = ?, is_manual = 0, updated_at = ?"
                " WHERE id = ? AND household_id = ?",
                (acct.get("item_id"), plaid_id, now, int(choice["account_id"]), household_id),
            )
            linked += 1
            continue

        balance = cents((acct.get("balances") or {}).get("current"))
        if local_type(acct) in networth.LIABILITY_TYPES:
            balance = abs(balance)
        cur = db.execute(
            """INSERT INTO accounts(household_id, institution, name, mask, type, currency,
                   current_balance_cents, available_balance_cents, include_in_net_worth,
                   include_in_budget, is_manual, manual_balance_as_of, plaid_item_id,
                   plaid_account_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (household_id, acct.get("_institution"), acct.get("name") or "Account",
             (acct.get("mask") or "")[-4:] or None, local_type(acct),
             (acct.get("balances") or {}).get("iso_currency_code") or "USD",
             balance, cents((acct.get("balances") or {}).get("available")),
             1, 0 if local_type(acct) in networth.LIABILITY_TYPES else 1, 0,
             date.today().isoformat(), acct.get("item_id"), plaid_id, now, now),
        )
        account_id = cur if isinstance(cur, int) else getattr(cur, "lastrowid", None)
        created += 1
    networth.recalc(household_id)
    return {"created": created, "linked": linked, "skipped": skipped}


def refresh_balances(household_id: int, access_token: str) -> int:
    accounts = plaid.accounts_get(access_token)
    today = date.today().isoformat()
    updated = 0
    for acct in accounts:
        row = db.query_one(
            "SELECT id, type FROM accounts WHERE household_id = ? AND plaid_account_id = ?"
            " AND archived_at IS NULL",
            (household_id, acct.get("account_id")),
        )
        if row is None:
            continue
        balance = cents((acct.get("balances") or {}).get("current"))
        if row["type"] in networth.LIABILITY_TYPES:
            balance = abs(balance)
        db.execute(
            "UPDATE accounts SET current_balance_cents = ?, available_balance_cents = ?,"
            " manual_balance_as_of = ?, updated_at = ? WHERE id = ?",
            (balance, cents((acct.get("balances") or {}).get("available")), today, db.utcnow(), row["id"]),
        )
        networth.record_balance(row["id"], balance, today, source="plaid")
        updated += 1
    if updated:
        networth.recalc(household_id)
    return updated


# ---------------------------------------------------------------- transactions
def _category_for(txn: dict, cats: dict[str, int]) -> tuple[int | None, bool]:
    pfc = ((txn.get("personal_finance_category") or {}).get("primary") or "").upper()
    if pfc.startswith("TRANSFER"):
        return cats.get("Transfer"), True
    if pfc == "INCOME":
        return cats.get("Income"), False
    return cats.get("Uncategorized"), False


def upsert_transaction(household_id: int, item_pk: int, txn: dict, cats: dict[str, int]) -> str:
    external_id = txn.get("transaction_id")
    account = db.query_one(
        "SELECT id FROM accounts WHERE household_id = ? AND plaid_account_id = ?",
        (household_id, txn.get("account_id")),
    )
    if not external_id or account is None:
        return "skipped"

    # Plaid: positive amount = money out. Local: negative = money out.
    amount = -cents(txn.get("amount"))
    merchant = (txn.get("merchant_name") or txn.get("name") or "Unknown").strip()[:120]
    posted_on = txn.get("date") or txn.get("authorized_date") or date.today().isoformat()
    category_id, is_transfer = _category_for(txn, cats)
    now = db.utcnow()

    row = db.query_one(
        "SELECT id FROM transactions WHERE household_id = ? AND external_id = ?",
        (household_id, external_id),
    )
    if row:
        db.execute(
            """UPDATE transactions SET account_id = ?, posted_on = ?, amount_cents = ?, merchant = ?,
                   category_id = CASE WHEN category_id IS NULL OR category_id = ? THEN ? ELSE category_id END,
                   is_pending = ?, is_transfer = ?, plaid_item_id = ?, deleted_at = NULL, updated_at = ?
               WHERE id = ?""",
            (account["id"], posted_on, amount, merchant, cats.get("Uncategorized"), category_id,
             1 if txn.get("pending") else 0, 1 if is_transfer else 0, item_pk, now, row["id"]),
        )
        return "modified"

    db.execute(
        """INSERT INTO transactions(household_id, account_id, posted_on, amount_cents, merchant,
               category_id, is_pending, is_transfer, plaid_item_id, external_id, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (household_id, account["id"], posted_on, amount, merchant, category_id,
         1 if txn.get("pending") else 0, 1 if is_transfer else 0, item_pk, external_id, now, now),
    )
    return "added"


def mark_removed(household_id: int, external_id: str) -> bool:
    row = db.query_one(
        "SELECT id FROM transactions WHERE household_id = ? AND external_id = ? AND deleted_at IS NULL",
        (household_id, external_id),
    )
    if row is None:
        return False
    db.execute("UPDATE transactions SET deleted_at = ?, updated_at = ? WHERE id = ?",
               (db.utcnow(), db.utcnow(), row["id"]))
    return True


# ---------------------------------------------------------------- item sync
def sync_item(household_id: int, item: dict) -> dict:
    """Run /transactions/sync to exhaustion for one Item. Returns a summary."""
    item_pk = item["id"]
    summary = {"added": 0, "modified": 0, "removed": 0, "pages": 0, "error": None}
    try:
        token = plaid.decrypt_token(item["access_token_enc"])
        cats = txns.ensure_system_categories(household_id)
        cursor = item.get("cursor")
        for _ in range(MAX_PAGES):
            page = plaid.transactions_sync(token, cursor)
            summary["pages"] += 1
            for txn in page.get("added", []):
                if upsert_transaction(household_id, item_pk, txn, cats) == "added":
                    summary["added"] += 1
            for txn in page.get("modified", []):
                if upsert_transaction(household_id, item_pk, txn, cats) == "modified":
                    summary["modified"] += 1
            for txn in page.get("removed", []):
                if mark_removed(household_id, txn.get("transaction_id", "")):
                    summary["removed"] += 1
            cursor = page.get("next_cursor") or cursor
            if not page.get("has_more"):
                break

        refresh_balances(household_id, token)
        if item.get("holdings_synced_at"):
            # The Item already consented to investments; keep positions fresh too.
            sync_holdings(household_id, item)
        db.execute(
            "UPDATE plaid_items SET cursor = ?, status = 'good', error_code = NULL,"
            " last_sync_at = ?, last_sync_summary = ?, updated_at = ? WHERE id = ?",
            (cursor, db.utcnow(),
             f"{summary['added']} new · {summary['modified']} updated · {summary['removed']} removed",
             db.utcnow(), item_pk),
        )
        return summary
    except plaid.PlaidError as exc:
        status = "login_required" if exc.code in {"ITEM_LOGIN_REQUIRED", "INVALID_ACCESS_TOKEN"} else "error"
        db.execute(
            "UPDATE plaid_items SET status = ?, error_code = ?, updated_at = ? WHERE id = ?",
            (status, exc.code, db.utcnow(), item_pk),
        )
        summary["error"] = exc.code
        logger.warning("sync failed for item %s: %s", item_pk, exc.code)
        return summary


# ---------------------------------------------------------------- market polls
def poll_market(household_id: int) -> list[dict]:
    """Refresh balances + positions for every Item, without touching transactions.

    Positions only change when the institution says so, so this spends one of the
    Item's 20 daily /investments/refresh calls, waits for the pull to land, then
    re-reads holdings and balances. Errors stay per-Item: one stale login must not
    stop the rest of the poll.
    """
    out: list[dict] = []
    items = db.query_all(
        "SELECT * FROM plaid_items WHERE household_id = ? AND status != 'login_required'",
        (household_id,))
    for row in items:
        item = dict(row)
        result = {"item": item["institution_name"], "refreshed": False,
                  "balances": 0, "holdings": 0, "error": None}
        try:
            token = plaid.decrypt_token(item["access_token_enc"])
            if item.get("holdings_synced_at"):
                try:
                    plaid.refresh_investments(token)
                    result["refreshed"] = True
                    time.sleep(REFRESH_SETTLE_SECONDS)
                except plaid.PlaidError as exc:
                    # 429 / PRODUCT_NOT_READY: the cached copy is still usable.
                    result["error"] = exc.code
            result["balances"] = refresh_balances(household_id, token)
            if item.get("holdings_synced_at"):
                summary = sync_holdings(household_id, item)
                result["holdings"] = summary["holdings"]
                if summary["error"] and not result["error"]:
                    result["error"] = summary["error"]
        except plaid.PlaidError as exc:
            result["error"] = exc.code
        except Exception as exc:  # noqa: BLE001
            logger.warning("market poll failed for item %s: %s", item["id"], type(exc).__name__)
            result["error"] = "error"
        out.append(result)
    return out


# ---------------------------------------------------------------- investments
SECURITY_KINDS = {
    "equity": "stock",
    "etf": "etf",
    "mutual fund": "fund",
    "fixed income": "bond",
    "bond": "bond",
    "cash": "cash",
    "derivative": "other",
    "loan": "other",
    "other": "other",
}


def sync_holdings(household_id: int, item: dict) -> dict:
    """Pull /investments/holdings/get into the Portfolio.

    Plaid stays the source of truth for these rows: they are written with
    source='plaid' so the Portfolio form leaves them alone, and the account
    balance keeps coming from the institution rather than the sum of positions.
    """
    summary = {"securities": 0, "holdings": 0, "removed": 0, "error": None}
    try:
        token = plaid.decrypt_token(item["access_token_enc"])
        body = plaid.investments_holdings_get(token)
    except plaid.PlaidError as exc:
        summary["error"] = exc.code
        logger.warning("holdings sync failed for item %s: %s", item["id"], exc.code)
        return summary

    securities = {s.get("security_id"): s for s in body.get("securities", [])}
    accounts = {
        a["plaid_account_id"]: a
        for a in db.query_all(
            "SELECT id, plaid_account_id FROM accounts WHERE household_id = ?"
            " AND plaid_item_id = ? AND archived_at IS NULL",
            (household_id, item["plaid_item_id"]),
        )
    }
    now = db.utcnow()
    today = date.today().isoformat()
    seen: dict[int, set[int]] = {}

    for pos in body.get("holdings", []):
        account = accounts.get(pos.get("account_id"))
        if account is None:
            continue
        sec = securities.get(pos.get("security_id")) or {}
        symbol = (sec.get("ticker_symbol") or "").strip().upper()
        if not symbol:
            # Money-market and other untickered positions still need a stable key.
            symbol = "SEC-" + str(pos.get("security_id") or "")[:8].upper()
        kind = SECURITY_KINDS.get((sec.get("type") or "").lower(), "other")
        security_id = portfolio.ensure_security(
            household_id, symbol, sec.get("name") or sec.get("official_name"), kind
        )
        if sec.get("security_id"):
            db.execute("UPDATE securities SET external_id = ? WHERE id = ?",
                       (sec["security_id"], security_id))

        qty = float(pos.get("quantity") or 0)
        price = cents(pos.get("institution_price"))
        # Plaid's cost_basis is the position's total cost, not per share. Cash has
        # none at all, and an unknown basis is best reported flat, not as pure gain.
        cost = cents(pos.get("cost_basis")) or int(round(qty * price))
        existing = db.query_one(
            "SELECT id FROM holdings WHERE account_id = ? AND security_id = ?",
            (account["id"], security_id),
        )
        if existing:
            db.execute(
                "UPDATE holdings SET quantity = ?, cost_basis_cents = ?, price_cents = ?,"
                " priced_on = ?, source = 'plaid', updated_at = ? WHERE id = ?",
                (qty, cost, price, today, now, existing["id"]),
            )
            holding_id = existing["id"]
        else:
            holding_id = db.execute(
                "INSERT INTO holdings(household_id, account_id, security_id, quantity,"
                " cost_basis_cents, price_cents, priced_on, source, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,'plaid',?,?)",
                (household_id, account["id"], security_id, qty, cost, price, today, now, now),
            )
            summary["securities"] += 1
        seen.setdefault(account["id"], set()).add(holding_id)
        summary["holdings"] += 1

    # Positions sold out of the account disappear from the response; drop our copies.
    for account in accounts.values():
        keep = seen.get(account["id"], set())
        rows = db.query_all(
            "SELECT id FROM holdings WHERE account_id = ? AND source = 'plaid'",
            (account["id"],),
        )
        for row in rows:
            if row["id"] not in keep:
                db.execute("DELETE FROM holdings WHERE id = ?", (row["id"],))
                summary["removed"] += 1

    db.execute("UPDATE plaid_items SET holdings_synced_at = ?, updated_at = ? WHERE id = ?",
               (now, now, item["id"]))
    return summary
