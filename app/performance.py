"""Long-run views: the portfolio against the market, and credit-card debt over time."""
from __future__ import annotations

import bisect
from datetime import date, timedelta

from . import market, networth, portfolio

SATOSHIS = 100_000_000


FRAMES: list[tuple[str, str]] = [("30d", "30D"), ("ytd", "YTD"), ("1y", "1Y"), ("3y", "3Y"), ("all", "All")]


def frame_days(key: str, today: date | None = None) -> int:
    """How far back a named frame reaches. YTD runs from New Year's Day."""
    today = today or date.today()
    return {
        "30d": 30,
        "ytd": (today - date(today.year, 1, 1)).days + 1,
        "1y": 365,
        "3y": 1095,
        "all": 3650,
    }.get((key or "1y").lower(), 365)


def _positions(household_id: int) -> tuple[list[tuple[str | None, float, int]], dict[str, int]]:
    """(symbol, quantity, fixed value) for everything in the portfolio.

    Cash symbols (`CUR:USD`) and accounts with no market history become fixed values so
    the total stays complete; everything else is quantity × daily close.
    """
    held = portfolio.holdings(household_id)
    rows = held + portfolio.account_value_rows(household_id, held)
    positions: list[tuple[str | None, float, int]] = []
    prices: dict[str, int] = {}
    for row in rows:
        value = int(row.get("value_cents") or 0)
        if not value:
            continue
        if row.get("is_account_value"):
            sats = row.get("units_sats")
            if sats:
                positions.append(("BTC-USD", float(sats) / SATOSHIS, 0))
            else:
                positions.append((None, 0.0, value))
            continue
        quantity = float(row.get("quantity") or 0)
        symbol = (row.get("symbol") or "").upper()
        if quantity <= 0:
            continue
        if symbol.startswith("CUR:"):
            positions.append((None, 0.0, int(round(quantity * 100))))
            continue
        positions.append((symbol, quantity, 0))
        prices[symbol] = int(row.get("price_cents") or 0)
    return positions, prices


def curve(household_id: int, days: int = 365) -> dict | None:
    """Index today's holdings and both benchmarks to 100 at the window's first day."""
    positions, prices = _positions(household_id)
    if not positions:
        return None
    symbols = sorted({s for s, _, _ in positions if s})
    market.ensure(symbols + [sym for sym, _ in market.BENCHMARKS])
    closes = {symbol: market.bars(symbol) for symbol in symbols}
    bench = {symbol: market.bars(symbol) for symbol, _ in market.BENCHMARKS}
    if not bench.get("^GSPC"):
        return None

    # A symbol with no history still belongs in the total — pin it at today's value.
    resolved: list[tuple[str | None, float, int]] = []
    for symbol, quantity, fixed in positions:
        if symbol and not closes.get(symbol):
            resolved.append((None, 0.0, int(round(quantity * prices.get(symbol, 0)))))
        else:
            resolved.append((symbol, quantity, fixed))

    dates = sorted(day for day in bench["^GSPC"]
                   if day >= (date.today() - timedelta(days=days)).isoformat())
    if len(dates) < 2:
        return None
    keys = {symbol: sorted(rows) for symbol, rows in closes.items() if rows}

    def value_on(day: str) -> int:
        total = sum(fixed for _, _, fixed in resolved)
        for symbol, quantity, _ in resolved:
            if symbol is None:
                continue
            series = keys.get(symbol) or []
            i = bisect.bisect_right(series, day) - 1
            if i >= 0:
                total += int(round(quantity * closes[symbol][series[i]]))
        return total

    values = [value_on(day) for day in dates]
    base_index = next((i for i, v in enumerate(values) if v > 0), None)
    if base_index is None:
        return None
    start_day = dates[base_index]
    base = values[base_index]
    dates = dates[base_index:]
    values = values[base_index:]

    def indexed(series: dict[str, int], base_day: str) -> list[float]:
        first = series.get(base_day)
        if not first:
            return []
        ordered = sorted(series)
        out = []
        for day in dates:
            i = bisect.bisect_right(ordered, day) - 1
            out.append(series[ordered[i]] / first * 100 if i >= 0 else 100.0)
        return out

    lines = [{"key": "portfolio", "label": "Investments",
              "values": [v / base * 100 for v in values]}]
    for symbol, label in market.BENCHMARKS:
        series = indexed(bench.get(symbol) or {}, start_day)
        if series:
            lines.append({"key": symbol.strip("^").lower(), "label": label, "values": series})

    everything = [v for line in lines for v in line["values"]]
    lo = min(everything)
    hi = max(everything)
    pad = max(1.0, (hi - lo) * 0.06)
    lo, hi = lo - pad, hi + pad
    width, height = 1100, 260
    for line in lines:
        line["points"] = networth.line_points(line["values"], width, height, lo=lo, hi=hi)
        line["change_pct"] = round(line["values"][-1] - 100, 1)
        line["last"] = round(line["values"][-1], 1)

    ticks = []
    for i in range(5):
        level = lo + (hi - lo) * i / 4
        ticks.append({"pct": round(i / 4 * 100, 1), "label": f"{level:.0f}"})

    return {
        "dates": dates,
        "start": dates[0],
        "end": dates[-1],
        "days": len(dates),
        "width": width,
        "height": height,
        "lines": lines,
        "ticks": ticks,
        "value_cents": values[-1],
        "symbols": len(symbols),
        "lo": round(lo, 1),
        "hi": round(hi, 1),
        "pad": 6.0,
        # The chart hands this to the browser so a drag can read values off the lines it drew.
        "pick": [{"key": line["key"], "label": line["label"],
                  "values": [round(v, 2) for v in line["values"]]} for line in lines],
    }


def credit_cards(household_id: int, days: int = 180) -> dict | None:
    """Total credit-card balance by day, walked back from today's balance through the feed.

    The card's own transactions include charges (negative) and payments (positive), so
    `balance_before = balance_after + amount` reconstructs the balance without needing a
    nightly snapshot to have existed.
    """
    cards = db_cards(household_id)
    if not cards:
        return None
    end = date.today()
    start = end - timedelta(days=days)
    earliest = None
    for card in cards:
        row = card.get("first_on")
        if row and (earliest is None or row < earliest):
            earliest = row
    if earliest:
        try:
            start = max(start, date.fromisoformat(earliest))
        except ValueError:
            pass

    daily: dict[int, dict[str, int]] = {}
    rows = card_totals(household_id, start.isoformat(), end.isoformat())
    for row in rows:
        daily.setdefault(row["account_id"], {})[row["posted_on"]] = int(row["total"])

    timeline = []
    day = end
    balances = {card["id"]: int(card["current_balance_cents"]) for card in cards}
    while day >= start:
        timeline.append((day.isoformat(), sum(balances.values())))
        # Walk to the previous day: today's balance less what posted today.
        for card_id, per_day in daily.items():
            # balance_before = balance_after + the day's signed amounts (charges are negative).
            balances[card_id] += per_day.get(day.isoformat(), 0)
        day -= timedelta(days=1)
    timeline.reverse()
    values = [value for _, value in timeline]
    peak = max(values) if values else 1
    peak = peak or 1
    width, height = 1100, 200
    ticks = [{"pct": round(i / 3 * 100, 1), "label": _short(peak * i // 3)} for i in range(4)]
    return {
        "cards": cards,
        "dates": [day for day, _ in timeline],
        "points": networth.line_points(values, width, height, lo=0, hi=peak * 1.05),
        "values": values,
        "width": width,
        "height": height,
        "ticks": ticks,
        "current_cents": values[-1] if values else 0,
        "change_cents": (values[-1] - values[0]) if len(values) > 1 else 0,
        "start": timeline[0][0] if timeline else end.isoformat(),
        "end": timeline[-1][0] if timeline else end.isoformat(),
        "days": len(timeline),
    }


def _short(cents: int) -> str:
    from . import money
    return money.axis_label(int(cents))


def db_cards(household_id: int) -> list[dict]:
    from . import db
    rows = db.query_all(
        """SELECT a.id, a.name, a.institution, a.mask, a.current_balance_cents,
                  (SELECT MIN(posted_on) FROM transactions t
                     WHERE t.account_id = a.id AND t.deleted_at IS NULL AND t.is_pending = 0) AS first_on
           FROM accounts a
           WHERE a.household_id = ? AND a.archived_at IS NULL AND a.type = 'credit_card'
           ORDER BY a.current_balance_cents DESC""",
        (household_id,),
    )
    return [dict(r) for r in rows]


def card_totals(household_id: int, start: str, end: str) -> list[dict]:
    from . import db
    return [dict(r) for r in db.query_all(
        """SELECT t.account_id, t.posted_on, SUM(t.amount_cents) AS total
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.household_id = ? AND a.type = 'credit_card'
             AND t.deleted_at IS NULL AND t.is_pending = 0
             AND t.posted_on BETWEEN ? AND ?
           GROUP BY t.account_id, t.posted_on""",
        (household_id, start, end),
    )]
