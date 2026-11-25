"""Daily closes for the benchmarks and every portfolio symbol.

Three free sources, no API keys:

* FRED for the S&P 500 and Nasdaq Composite (daily index levels, published with a short lag)
* Nasdaq's own quote API for stocks and ETFs
* Coinbase's public candles for BTC-USD

Rows land in `market_bars`, so a page load only waits on the network once per symbol per
TTL; after that the chart is pure SQLite. Failures keep whatever was cached and are marked
so a dead symbol does not get retried on every request.
"""
from __future__ import annotations

import csv
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from . import db

logger = logging.getLogger("banana.market")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/csv,*/*"}
TTL_HOURS = 12
TIMEOUT = 25.0
SENTINEL = "1900-01-01"   # "we tried" marker, never a market day

BENCHMARKS = (("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"))
FRED_SERIES = {"^GSPC": "SP500", "^IXIC": "NASDAQCOM"}

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
NASDAQ = ("https://api.nasdaq.com/api/quote/{sym}/historical"
          "?assetclass={cls}&fromdate={start}&todate={end}&limit=9999")
COINBASE = ("https://api.exchange.coinbase.com/products/{product}/candles"
            "?granularity=86400&start={start}&end={end}")


# ---------------------------------------------------------------- fetchers
def _fred_text(url: str) -> str:
    """FRED is picky about client identity; a plain, honest UA works and keeps this
    build free of a curl.exe dependency on Windows."""
    resp = httpx.get(url, headers={"User-Agent": "BananaStand/1.0"}, timeout=TIMEOUT,
                     follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _fred(series: str, start: date) -> dict[str, int]:
    text = _fred_text(FRED.format(series=series))
    out: dict[str, int] = {}
    for row in csv.DictReader(io.StringIO(text)):
        day = (row.get("observation_date") or "").strip()
        value = (row.get(series) or "").strip()
        if not day or not value or value == "." or day < start.isoformat():
            continue
        try:
            out[day] = int(round(float(value) * 100))
        except ValueError:
            continue
    return out


def _nasdaq(symbol: str, start: date, end: date) -> dict[str, int]:
    for asset_class in ("stocks", "etf"):
        resp = httpx.get(
            NASDAQ.format(sym=quote(symbol, safe=""), cls=asset_class,
                          start=start.isoformat(), end=end.isoformat()),
            headers=HEADERS, timeout=TIMEOUT, follow_redirects=True,
        )
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        rows = ((data.get("tradesTable") or {}).get("rows")) or []
        out: dict[str, int] = {}
        for row in rows:
            try:
                day = datetime.strptime(row["date"], "%m/%d/%Y").date().isoformat()
                close = float(str(row["close"]).replace("$", "").replace(",", ""))
            except (KeyError, ValueError, TypeError):
                continue
            out[day] = int(round(close * 100))
        if out:
            return out
    return {}


def _coinbase(product: str, start: date, end: date) -> dict[str, int]:
    out: dict[str, int] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=299), end)
        resp = httpx.get(
            COINBASE.format(product=product,
                            start=int(datetime(cursor.year, cursor.month, cursor.day,
                                               tzinfo=timezone.utc).timestamp()),
                            end=int(datetime(chunk_end.year, chunk_end.month, chunk_end.day,
                                             tzinfo=timezone.utc).timestamp())),
            headers=HEADERS, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        for row in resp.json() or []:
            try:
                day = datetime.fromtimestamp(int(row[0]), timezone.utc).date().isoformat()
                out[day] = int(round(float(row[4]) * 100))
            except (IndexError, ValueError, TypeError):
                continue
        cursor = chunk_end + timedelta(days=1)
    return out


def _fetch(symbol: str, days: int = 730) -> dict[str, int]:
    start = date.today() - timedelta(days=days)
    if symbol in FRED_SERIES:
        return _fred(FRED_SERIES[symbol], start)
    if symbol.upper() == "BTC-USD":
        return _coinbase("BTC-USD", start, date.today())
    return _nasdaq(symbol, start, date.today())


# ---------------------------------------------------------------- cache
def _is_fresh(symbol: str) -> bool:
    row = db.query_one("SELECT MAX(fetched_at) AS at FROM market_bars WHERE symbol = ?", (symbol,))
    if not row or not row["at"]:
        return False
    try:
        fetched = datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched < timedelta(hours=TTL_HOURS)


def _mark_attempt(symbol: str) -> None:
    db.execute(
        """INSERT INTO market_bars(symbol, as_of_date, close_cents, fetched_at)
           VALUES (?,?,0,?) ON CONFLICT(symbol, as_of_date) DO UPDATE SET
           fetched_at = excluded.fetched_at""",
        (symbol, SENTINEL, db.utcnow()),
    )


def refresh(symbol: str) -> int:
    """Fetch one symbol into the cache. Returns how many bars were stored."""
    try:
        bars = _fetch(symbol)
    except Exception as exc:  # network, HTTP, JSON, rate limit — keep the cache
        logger.warning("market bars for %s failed: %s", symbol, type(exc).__name__)
        _mark_attempt(symbol)
        return 0
    if not bars:
        _mark_attempt(symbol)
        return 0
    now = db.utcnow()
    for day, close in bars.items():
        db.execute(
            """INSERT INTO market_bars(symbol, as_of_date, close_cents, fetched_at)
               VALUES (?,?,?,?) ON CONFLICT(symbol, as_of_date) DO UPDATE SET
               close_cents = excluded.close_cents, fetched_at = excluded.fetched_at""",
            (symbol, day, close, now),
        )
    return len(bars)


def ensure(symbols: list[str], workers: int = 3) -> None:
    """Refresh every stale symbol; cached symbols cost one small query."""
    stale = [s for s in dict.fromkeys(symbols) if s and not _is_fresh(s)]
    if not stale:
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(refresh, stale))


def bars(symbol: str) -> dict[str, int]:
    """{iso date: close cents} from the cache, refreshing first when stale."""
    if not _is_fresh(symbol):
        refresh(symbol)
    rows = db.query_all(
        "SELECT as_of_date, close_cents FROM market_bars"
        " WHERE symbol = ? AND as_of_date > '1990-01-01' ORDER BY as_of_date",
        (symbol,),
    )
    return {r["as_of_date"]: int(r["close_cents"]) for r in rows}
