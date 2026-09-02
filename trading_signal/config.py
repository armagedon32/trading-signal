"""Shared constants and option tables."""

from __future__ import annotations

from datetime import timedelta

# --- Dashboard (yfinance) --------------------------------------------------

TIMEFRAME_OPTIONS = ["5m", "15m", "1h", "1d"]

# Yahoo only serves intraday history for a limited trailing window. Anything
# outside it returns an error (or nothing), so we clamp the lookback per interval.
# Use slightly less than the hard limit to leave room for time-of-day drift.
MAX_LOOKBACK_DAYS = {
    "1m": 7,
    "2m": 59,
    "5m": 59,
    "15m": 59,
    "30m": 59,
    "90m": 59,
    "1h": 729,
    "1d": 365 * 20,
}

# --- Professional Signal (Twelve Data) --------------------------------------

EXPIRY_OPTIONS = ["1m", "5m", "1h"]
TWELVE_INTERVAL_MAP = {"1m": "1min", "5m": "5min", "1h": "1h"}
EXPIRY_TIMEDELTA = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "1h": timedelta(hours=1),
}

# One output size for every Twelve Data call so cached responses are shared
# between prediction, tracker and status views (one API credit instead of three).
TWELVE_OUTPUTSIZE = 800

MODEL_OPTIONS = ["Trend", "ML Lite", "ML Advanced"]

ASSET_OPTIONS = [
    # FX (24/5)
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "AUD/USD",
    "USD/CAD",
    "NZD/USD",
    "EUR/GBP",
    "EUR/JPY",
    "GBP/JPY",
    # Crypto (24/7)
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
    "ADA/USD",
    "DOGE/USD",
    # Metals
    "XAU/USD",
    "XAG/USD",
    # Stocks/ETF
    "AAPL",
    "MSFT",
    "NVDA",
    "TSLA",
    "SPY",
    "QQQ",
    "Custom",
]

# Cache TTL (seconds) for remote data.
DATA_TTL_SECONDS = 60


def expiry_to_timedelta(expiry: str) -> timedelta:
    return EXPIRY_TIMEDELTA.get(expiry, timedelta(minutes=5))


def expiry_to_seconds(expiry: str) -> int:
    return int(expiry_to_timedelta(expiry).total_seconds())


def max_lookback_days(interval: str) -> int:
    return MAX_LOOKBACK_DAYS.get(interval, 59)
