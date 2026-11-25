"""Money parsing and formatting. All values are integer cents."""
from __future__ import annotations

import re

_NUM = re.compile(r"^-?\d+(\.\d{1,2})?$")


def parse_money(text: str | None) -> int | None:
    """Parse a human amount into cents. Returns None when unparseable.

    Accepts: 1234, 1,234.56, $1,234.56, 1234.5, -12.30, (12.30), 12.30-
    """
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        return None
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]
    if raw.endswith("-"):
        negative = True
        raw = raw[:-1]
    raw = raw.replace("$", "").replace(",", "").replace(" ", "")
    if not _NUM.match(raw):
        return None
    value = float(raw) if "." in raw else int(raw)
    cents = int(round(value * 100))
    return -cents if negative else cents


def format_money(cents: int | None, signed: bool = False, dash: str = "—") -> str:
    if cents is None:
        return dash
    sign = ""
    if cents < 0:
        sign = "−"
    elif signed and cents > 0:
        sign = "+"
    whole = f"{abs(cents) / 100:,.2f}"
    return f"{sign}${whole}"


def format_percent(value: float | None, places: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:+.{places}f}%"


SATOSHIS = 100_000_000  # 1 BTC = 1e8 sats


def parse_btc(text: str | None) -> int | None:
    """Parse a BTC amount into satoshis. Returns None when unparseable.

    Accepts: 0.5, .5, 1,234.56789012, 0.5 BTC. Rejects negatives and >8 decimals.
    """
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        return None
    if raw.lower().endswith("btc"):
        raw = raw[:-3].strip()
    raw = raw.replace("₿", "").replace(",", "").replace(" ", "")
    if raw.startswith("+"):
        raw = raw[1:]
    if not raw or raw.startswith("-"):
        return None
    if not re.match(r"^\d+(\.\d{1,8})?$|^\.\d{1,8}$", raw):
        return None
    whole, _, frac = raw.partition(".")
    sats = int(whole or 0) * SATOSHIS + int((frac + "0" * 8)[:8] or 0)
    return sats


def format_btc(sats: int | None, dash: str = "—") -> str:
    """Render satoshis as a trimmed BTC amount: 0.5, 0.00012345, 1.23456789."""
    if sats is None:
        return dash
    sign = "−" if sats < 0 else ""
    value = abs(int(sats))
    whole, frac = divmod(value, SATOSHIS)
    if frac == 0:
        return f"{sign}{whole:,}"
    decimals = f"{frac:08d}".rstrip("0")
    return f"{sign}{whole:,}.{decimals}"


def parse_quantity(value: str) -> float:
    """Share counts: 10, 10.5, 1,234.56789."""
    text = (value or "").strip().replace(",", "").replace("$", "")
    if not text:
        raise ValueError("Quantity is required.")
    try:
        qty = float(text)
    except ValueError as exc:
        raise ValueError("That quantity is not a number.") from exc
    if qty < 0:
        raise ValueError("Quantity cannot be negative.")
    return qty


# ---------------------------------------------------------------- chart axes
def nice_step(raw: int) -> int:
    """Round a step up to 1/2/2.5/5 x a power of ten so axis labels stay readable."""
    if raw <= 0:
        return 1
    magnitude = 10 ** (len(str(int(raw))) - 1)
    for mult in (1, 2, 2.5, 5, 10):
        if magnitude * mult >= raw:
            return int(magnitude * mult)
    return int(magnitude * 10)


def nice_top(cents: int, ticks: int = 3) -> int:
    """A friendly ceiling for a chart: three or four even steps above the peak."""
    return nice_step(max(1, int(cents)) / ticks) * ticks


def axis_label(cents: int) -> str:
    """Short money for an axis: $0, $5k, $1.5M — no cents, no four digits."""
    dollars = cents / 100
    if dollars >= 1_000_000:
        text = f"{dollars / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"${text}M"
    if dollars >= 1000:
        text = f"{dollars / 1000:.1f}".rstrip("0").rstrip(".")
        return f"${text}k"
    return f"${dollars:.0f}"
