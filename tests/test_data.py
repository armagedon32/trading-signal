from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from trading_signal import data
from tests.conftest import make_ohlc


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text="", raise_json=False):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params, timeout))
        if self.exc:
            raise self.exc
        return self.response

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json, timeout))
        if self.exc:
            raise self.exc
        return self.response


# --------------------------------------------------------------------------- #
# yfinance
# --------------------------------------------------------------------------- #
def test_yf_window_includes_today_and_clamps():
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    start, end, days = data.yf_window("5m", 365, now)
    assert days == 59
    assert end > now  # exclusive end must be after "now" so today's bars are included
    assert start == now - timedelta(days=59)
    _, _, d1 = data.yf_window("1d", 365, now)
    assert d1 == 365
    _, _, d2 = data.yf_window("1m", 30, now)
    assert d2 == 7


def test_load_yfinance_success_with_multiindex(yf_multiindex_frame):
    captured = {}

    def downloader(symbol, **kw):
        captured.update(kw, symbol=symbol)
        return yf_multiindex_frame

    res = data.load_yfinance("aapl", "5m", 59, downloader=downloader)
    assert res.ok and res.error is None
    assert captured["symbol"] == "AAPL"
    assert isinstance(captured["end"], datetime)  # datetime, not a date string
    assert captured["interval"] == "5m"
    assert list(res.df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_load_yfinance_empty_gives_hint():
    res = data.load_yfinance("ZZZZ", "5m", 59, downloader=lambda *a, **k: pd.DataFrame())
    assert not res.ok
    assert "No 5m data" in res.error and "Intraday" in res.error


def test_load_yfinance_exception_is_reported():
    def boom(*a, **k):
        raise RuntimeError("rate limited")

    res = data.load_yfinance("AAPL", "1d", 30, downloader=boom)
    assert not res.ok and "rate limited" in res.error


def test_load_yfinance_empty_symbol():
    assert data.load_yfinance("  ", "1d", 30, downloader=lambda *a, **k: make_ohlc()).error == "Empty symbol."


# --------------------------------------------------------------------------- #
# Twelve Data
# --------------------------------------------------------------------------- #
def td_payload(n=5):
    idx = pd.date_range("2026-09-01 10:00", periods=n, freq="5min")
    values = [
        {"datetime": t.strftime("%Y-%m-%d %H:%M:%S"), "open": "1.1", "high": "1.2", "low": "1.0", "close": f"{1.1 + i / 100:.5f}"}
        for i, t in enumerate(idx)
    ]
    return {"meta": {"symbol": "EUR/USD"}, "values": values[::-1], "status": "ok"}  # newest first, as the API does


def test_parse_twelvedata_payload_sorted_utc():
    res = data.parse_twelvedata_payload(td_payload())
    assert res.ok
    assert res.df.index.is_monotonic_increasing
    assert str(res.df.index.tz) == "UTC"
    assert res.df["Close"].iloc[-1] == 1.14


def test_parse_twelvedata_error_payloads():
    r = data.parse_twelvedata_payload({"code": 429, "message": "You have run out of API credits", "status": "error"})
    assert not r.ok and "Rate limit" in r.error and "429" in r.error
    r = data.parse_twelvedata_payload({"code": 401, "message": "Invalid apikey", "status": "error"})
    assert "Invalid API key" in r.error
    r = data.parse_twelvedata_payload({"code": 400, "message": "symbol not found", "status": "error"})
    assert "400" in r.error and "symbol not found" in r.error
    r = data.parse_twelvedata_payload({"values": [], "status": "ok"})
    assert "no bars" in r.error
    assert not data.parse_twelvedata_payload("garbage").ok


def test_load_twelvedata_requests_utc_and_asc():
    sess = FakeSession(FakeResponse(td_payload()))
    res = data.load_twelvedata("eur/usd", "5min", "KEY", session=sess)
    assert res.ok
    _, url, params, timeout = sess.calls[0]
    assert url == data.TWELVE_DATA_URL
    assert params["timezone"] == "UTC"
    assert params["order"] == "asc"
    assert params["symbol"] == "EUR/USD"
    assert params["outputsize"] == data.TWELVE_OUTPUTSIZE


def test_load_twelvedata_missing_key_makes_no_call():
    sess = FakeSession(FakeResponse(td_payload()))
    res = data.load_twelvedata("EUR/USD", "5min", "", session=sess)
    assert not res.ok and sess.calls == []


def test_load_twelvedata_network_and_nonjson():
    sess = FakeSession(exc=requests.ConnectionError("dns"))
    assert "request failed" in data.load_twelvedata("EUR/USD", "5min", "K", session=sess).error
    sess = FakeSession(FakeResponse(status_code=502, raise_json=True))
    assert "non-JSON" in data.load_twelvedata("EUR/USD", "5min", "K", session=sess).error


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def test_send_telegram_message_checks_response():
    ok_sess = FakeSession(FakeResponse({"ok": True}, 200))
    assert data.send_telegram_message("t", "c", "hi", session=ok_sess) == (True, "sent")
    bad = FakeSession(FakeResponse({"ok": False, "description": "chat not found"}, 400))
    ok, detail = data.send_telegram_message("t", "c", "hi", session=bad)
    assert not ok and "chat not found" in detail
    ok, detail = data.send_telegram_message("", "c", "hi")
    assert not ok
    down = FakeSession(exc=requests.Timeout("slow"))
    ok, detail = data.send_telegram_message("t", "c", "hi", session=down)
    assert not ok and "failed" in detail


def test_utc_now_floor_is_stable_within_bucket():
    a = data.utc_now_floor(60)
    b = data.utc_now_floor(60)
    assert a == b
    assert a.second == 0 and a.microsecond == 0
