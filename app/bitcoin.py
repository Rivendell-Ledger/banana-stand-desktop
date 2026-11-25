"""Live bitcoin price and the USD value of BTC-denominated accounts.

A bitcoin account stores its quantity in satoshis (`units_sats`); the dollars in
`current_balance_cents` are derived from the live price and refreshed on page
loads plus a timer. The Ledger's quote API is the primary source because it is
local and already the household's price of record; Coinbase and Kraken are
fallbacks for when it is down.
"""
from __future__ import annotations

import logging
import os
import sys
import time

import httpx

from . import db, networth

logger = logging.getLogger("banana.bitcoin")

BTC_TYPE = "crypto"
SATOSHIS = 100_000_000
PRICE_TTL_SECONDS = 60
TIMEOUT = 3.0

_cache: dict = {"price_cents": None, "at": 0.0, "source": None}


# ---------------------------------------------------------------- price sources
def _from_ledger() -> int | None:
    url = os.environ.get("LEDGER_QUOTES_URL", "http://127.0.0.1:8090/api/quotes")
    resp = httpx.get(url, params={"symbols": "BTC-USD"}, timeout=TIMEOUT)
    resp.raise_for_status()
    row = (resp.json().get("rows") or {}).get("BTC-USD") or {}
    price = row.get("price")
    return int(round(float(price) * 100)) if price else None


def _from_coinbase() -> int | None:
    resp = httpx.get(
        "https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=TIMEOUT
    )
    resp.raise_for_status()
    price = resp.json().get("price")
    return int(round(float(price) * 100)) if price else None


def _from_kraken() -> int | None:
    resp = httpx.get(
        "https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    result = resp.json().get("result") or {}
    for row in result.values():
        if isinstance(row, dict) and row.get("c"):
            return int(round(float(row["c"][0]) * 100))
    return None


SOURCES = (("ledger", _from_ledger), ("coinbase", _from_coinbase), ("kraken", _from_kraken))


def price_cents(force: bool = False) -> int | None:
    """Latest BTC price in cents, cached briefly. None when every source fails."""
    if (
        not force
        and _cache["price_cents"]
        and time.time() - _cache["at"] < PRICE_TTL_SECONDS
    ):
        return _cache["price_cents"]
    for name, fetch in SOURCES:
        try:
            cents = fetch()
        except Exception as exc:  # network, HTTP, JSON — try the next source
            logger.warning("btc price via %s failed: %s", name, type(exc).__name__)
            continue
        if cents and cents > 0:
            _cache.update(price_cents=cents, at=time.time(), source=name)
            return cents
    return _cache["price_cents"]  # stale beat is better than zero


def last_source() -> str | None:
    return _cache["source"]


# ---------------------------------------------------------------- valuation
def usd_cents(sats: int, price: int) -> int:
    """Convert satoshis to USD cents, rounded half-up, in integer arithmetic."""
    return (int(sats) * int(price) + SATOSHIS // 2) // SATOSHIS


def sats_for_cents(cents: int, price: int) -> int:
    """The satoshi quantity worth roughly this many cents at the given price."""
    if price <= 0:
        return 0
    return (int(cents) * SATOSHIS + price // 2) // price


def _btc_accounts(household_id: int | None = None) -> list:
    sql = (
        "SELECT id, household_id, units_sats FROM accounts"
        " WHERE type = ? AND units_sats IS NOT NULL AND archived_at IS NULL"
    )
    params: tuple = (BTC_TYPE,)
    if household_id is not None:
        sql += " AND household_id = ?"
        params += (household_id,)
    return db.query_all(sql, params)


def recompute(household_id: int | None = None, force: bool = False) -> int | None:
    """Re-price every BTC account and refresh the net-worth snapshot.

    Returns the price used (cents), or None when no price could be fetched and
    nothing was cached.
    """
    rows = _btc_accounts(household_id)
    if not rows:
        return None  # no network call for households with nothing to price
    price = price_cents(force=force)
    if not price:
        return None
    now = db.utcnow()
    for row in rows:
        db.execute(
            "UPDATE accounts SET current_balance_cents = ?, updated_at = ? WHERE id = ?",
            (usd_cents(row["units_sats"], price), now, row["id"]),
        )
    for hid in {row["household_id"] for row in rows}:
        networth.recalc(hid)
    return price


def refresh_all() -> int | None:
    """Timer entry point: refresh the price even when nobody holds bitcoin yet."""
    price = price_cents(force=True)
    if price:
        recompute(None)
    return price


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    price = refresh_all()
    if not price:
        print("BTC price unavailable; balances left unchanged.", file=sys.stderr)
        return 1
    print(f"BTC ${price / 100:,.2f} via {last_source()} — balances refreshed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
