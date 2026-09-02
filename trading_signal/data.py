"""Market-data loaders (yfinance + Twelve Data) with explicit error reporting.

Loaders never raise for expected failures (rate limit, bad symbol, network). They
return a :class:`LoadResult` whose ``error`` explains what went wrong so the UI can
show something more useful than "No data returned".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd
import requests

from .config import DATA_TTL_SECONDS, TWELVE_OUTPUTSIZE, max_lookback_days
from .indicators import normalize_ohlc

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
TWELVE_MAX_OUTPUTSIZE = 5000
YF_INTRADAY_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}


@dataclass
class LoadResult:
    """Outcome of a data request."""

    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    error: Optional[str] = None
    source: str = ""
    fetched_at: Optional[datetime] = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.df.empty

    @property
    def empty(self) -> bool:
        return self.df.empty


def utc_now_floor(seconds: int = DATA_TTL_SECONDS) -> datetime:
    """Current UTC time rounded *down* to a multiple of ``seconds``.

    Using this as a function argument keeps cache keys stable between reruns
    instead of carrying microseconds that defeat ``st.cache_data``.
    """
    now = datetime.now(timezone.utc)
    epoch = int(now.timestamp()) // seconds * seconds
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def yf_window(interval: str, lookback_days: int, now: Optional[datetime] = None) -> tuple[datetime, datetime, int]:
    """Return ``(start, end, effective_lookback_days)`` for a yfinance request.

    ``end`` is *inclusive of now* (yfinance treats ``end`` as exclusive, so a
    date-only string would cut off the current day). ``lookback_days`` is
    clamped to what Yahoo actually serves for the interval.
    """
    now = now or datetime.now(timezone.utc)
    limit = max_lookback_days(interval)
    days = max(1, min(int(lookback_days), limit))
    end = now + timedelta(minutes=1)
    start = now - timedelta(days=days)
    return start, end, days


# --------------------------------------------------------------------------- #
# yfinance
# --------------------------------------------------------------------------- #
def load_yfinance(
    symbol: str,
    interval: str,
    lookback_days: int,
    now: Optional[datetime] = None,
    downloader: Optional[Callable[..., pd.DataFrame]] = None,
) -> LoadResult:
    """Download OHLC bars from Yahoo Finance.

    ``downloader`` is injectable for tests; defaults to ``yfinance.download``.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return LoadResult(error="Empty symbol.", source="yfinance")

    if downloader is None:
        import yfinance as yf  # imported lazily so tests don't need network setup

        downloader = yf.download

    start, end, _ = yf_window(interval, lookback_days, now)
    try:
        raw = downloader(
            symbol,
            start=start,
            end=end,
            interval=interval,
            progress=False,
            auto_adjust=True,
            threads=False,
        )
    except Exception as exc:  # network, rate limit, parsing...
        return LoadResult(error=f"yfinance error for {symbol} ({interval}): {exc}", source="yfinance")

    df = normalize_ohlc(raw)
    if df.empty:
        hint = ""
        if interval in YF_INTRADAY_INTERVALS:
            hint = " Intraday history is limited (about 60 days for 5m/15m, 730 days for 1h)."
        return LoadResult(
            error=f"No {interval} data returned for {symbol}. Check the symbol, market hours or lookback.{hint}",
            source="yfinance",
        )
    return LoadResult(df=df, source="yfinance", fetched_at=datetime.now(timezone.utc))


# --------------------------------------------------------------------------- #
# Twelve Data
# --------------------------------------------------------------------------- #
def parse_twelvedata_payload(payload: dict) -> LoadResult:
    """Convert a Twelve Data ``time_series`` JSON payload into a UTC-indexed frame."""
    if not isinstance(payload, dict):
        return LoadResult(error="Unexpected response from Twelve Data.", source="twelvedata")

    if payload.get("status") == "error" or "code" in payload and "values" not in payload:
        code = payload.get("code")
        message = payload.get("message", "Unknown error")
        if code == 429:
            message = "Rate limit reached (free tier allows 8 calls/min). " + message
        elif code == 401:
            message = "Invalid API key. " + message
        return LoadResult(error=f"Twelve Data error {code}: {message}", source="twelvedata")

    values = payload.get("values") or []
    if not values:
        return LoadResult(error="Twelve Data returned no bars for this symbol/interval.", source="twelvedata")

    df = pd.DataFrame(values)
    if "datetime" not in df.columns or "close" not in df.columns:
        return LoadResult(error="Twelve Data payload is missing datetime/close fields.", source="twelvedata")

    # We always request timezone=UTC, so the naive strings really are UTC.
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    df = df.dropna(subset=["datetime"]).set_index("datetime")
    df = normalize_ohlc(df)
    if df.empty:
        return LoadResult(error="Twelve Data returned bars without usable prices.", source="twelvedata")
    return LoadResult(df=df, source="twelvedata", fetched_at=datetime.now(timezone.utc))


def load_twelvedata(
    symbol: str,
    interval: str,
    api_key: str,
    outputsize: int = TWELVE_OUTPUTSIZE,
    timeout: float = 15.0,
    session: Optional[requests.Session] = None,
) -> LoadResult:
    """Fetch intraday bars from Twelve Data in UTC."""
    symbol = (symbol or "").strip().upper()
    if not api_key:
        return LoadResult(error="Twelve Data API key missing.", source="twelvedata")
    if not symbol:
        return LoadResult(error="Empty symbol.", source="twelvedata")

    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": int(max(1, min(outputsize, TWELVE_MAX_OUTPUTSIZE))),
        "apikey": api_key,
        "format": "JSON",
        "timezone": "UTC",
        "order": "asc",
    }
    http = session or requests
    try:
        resp = http.get(TWELVE_DATA_URL, params=params, timeout=timeout)
    except requests.RequestException as exc:
        return LoadResult(error=f"Twelve Data request failed: {exc}", source="twelvedata")

    try:
        payload = resp.json()
    except ValueError:
        return LoadResult(
            error=f"Twelve Data returned a non-JSON response (HTTP {resp.status_code}).", source="twelvedata"
        )
    return parse_twelvedata_payload(payload)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    timeout: float = 10.0,
    session: Optional[requests.Session] = None,
) -> tuple[bool, str]:
    """Send a Telegram message; returns ``(ok, detail)`` instead of raising."""
    if not token or not chat_id:
        return False, "Telegram token or chat id missing."
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    http = session or requests
    try:
        resp = http.post(url, json={"chat_id": chat_id, "text": text}, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"Telegram request failed: {exc}"
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code == 200 and body.get("ok", False):
        return True, "sent"
    return False, f"Telegram API HTTP {resp.status_code}: {body.get('description', resp.text[:200])}"
