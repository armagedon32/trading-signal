"""Very small market-hours helpers (no holiday calendars)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

CRYPTO_QUOTES = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "BNB", "DOT", "AVAX", "MATIC", "LINK"}
METALS = {"XAU", "XAG", "XPT", "XPD"}


def asset_class(symbol: str) -> str:
    s = (symbol or "").upper()
    if "/" in s:
        base = s.split("/")[0]
        if base in CRYPTO_QUOTES:
            return "crypto"
        if base in METALS:
            return "metal"
        return "fx"
    return "stock"


def fx_is_open(now: datetime | None = None) -> bool:
    """FX trades continuously from Sunday 17:00 to Friday 17:00 New York time."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    wd = now.weekday()  # Mon=0 .. Sun=6
    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday
        return now.hour >= 17
    if wd == 4:  # Friday
        return now.hour < 17
    return True


def us_stocks_are_open(now: datetime | None = None) -> bool:
    """Regular session Mon-Fri 09:30-16:00 New York (ignores holidays)."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def market_status(symbol: str, now: datetime | None = None) -> tuple[str, bool]:
    """Return ``(asset_class, is_open)`` for the given symbol."""
    cls = asset_class(symbol)
    if cls == "crypto":
        return cls, True
    if cls in ("fx", "metal"):
        return cls, fx_is_open(now)
    return cls, us_stocks_are_open(now)
