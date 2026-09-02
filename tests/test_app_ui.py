"""End-to-end smoke tests of the Streamlit UI via ``streamlit.testing`` with all
network access mocked. These guard the wiring between widgets and the library.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import make_ohlc

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def yf_frame(symbol, n=800):
    df = make_ohlc(n=n, tz="America/New_York", seed=abs(hash(symbol)) % 1000)
    df.columns = pd.MultiIndex.from_product([df.columns, [symbol]], names=["Price", "Ticker"])
    return df


class FakeResp:
    def __init__(self, payload, status_code=200):
        self._p = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._p


def td_payload(n=800, end=None, interval_min=5):
    end = end or datetime.now(timezone.utc)
    end = end.replace(second=0, microsecond=0)
    end = end - timedelta(minutes=end.minute % interval_min)
    idx = pd.date_range(end=end, periods=n, freq=f"{interval_min}min", tz="UTC")
    rng = np.random.default_rng(7)
    close = 1.1 + np.cumsum(rng.normal(0, 0.0003, n))
    vals = [
        {
            "datetime": t.strftime("%Y-%m-%d %H:%M:%S"),
            "open": f"{c:.5f}",
            "high": f"{c + 0.0002:.5f}",
            "low": f"{c - 0.0002:.5f}",
            "close": f"{c:.5f}",
        }
        for t, c in zip(idx, close)
    ]
    return {"meta": {"symbol": "EUR/USD"}, "values": vals, "status": "ok"}


@pytest.fixture(autouse=True)
def clear_streamlit_caches():
    # st.cache_data is process-global; without this, one test's mocked data
    # would be served to the next test.
    import streamlit as st

    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture
def isolated_history(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_SIGNAL_HISTORY", str(tmp_path / "predictions.json"))
    return tmp_path / "predictions.json"


@pytest.fixture
def app(isolated_history):
    def make():
        return AppTest.from_file(APP, default_timeout=180)

    return make


def run_with_mocks(at, yf_side_effect=None, td_side_effect=None, post_side_effect=None):
    yf_side_effect = yf_side_effect or (lambda symbol, **kw: yf_frame(symbol))
    td_side_effect = td_side_effect or (lambda url, params=None, timeout=None: FakeResp(td_payload()))
    post_side_effect = post_side_effect or (lambda url, json=None, timeout=None: FakeResp({"ok": True}))
    with mock.patch("yfinance.download", side_effect=yf_side_effect), mock.patch(
        "requests.get", side_effect=td_side_effect
    ), mock.patch("requests.post", side_effect=post_side_effect):
        at.run()
    return at


def sidebar_checkbox(at, label):
    return [c for c in at.sidebar.checkbox if c.label == label][0]


def sidebar_text(at, label):
    return [t for t in at.sidebar.text_input if t.label == label][0]


def button(at, label):
    return [b for b in at.button if b.label == label][0]


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def test_dashboard_renders_without_errors(app):
    at = run_with_mocks(app())
    assert not at.exception
    assert not at.error
    labels = [m.label for m in at.metric]
    assert "Latest Price" in labels and "Signal" in labels
    assert "Strategy Return" in labels and "Trade Win Rate" in labels
    assert any("Forecast" in i.value for i in at.info)


def test_dashboard_auto_refresh_no_longer_crashes(app):
    at = app()
    run_with_mocks(at)
    sidebar_checkbox(at, "Auto-refresh (60s)").check()
    run_with_mocks(at)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("Auto-refresh on" in c.value for c in at.caption)


def test_dashboard_short_history_shows_warning_not_traceback(app):
    def short(symbol, **kw):
        return yf_frame(symbol, n=15)

    at = run_with_mocks(app(), yf_side_effect=short)
    assert not at.exception
    assert any("need about" in w.value for w in at.warning)


def test_dashboard_yfinance_failure_is_explained(app):
    def boom(symbol, **kw):
        raise RuntimeError("Too Many Requests. Rate limited.")

    at = run_with_mocks(app(), yf_side_effect=boom)
    assert not at.exception
    assert any("Rate limited" in w.value for w in at.warning)


def test_telegram_alert_sent_once_per_bar_and_failure_reported(app):
    at = app()
    run_with_mocks(at)
    sidebar_checkbox(at, "Telegram alerts").check()
    sidebar_text(at, "Bot token").set_value("123:ABC")
    sidebar_text(at, "Chat ID").set_value("42")
    posts = []

    def post(url, json=None, timeout=None):
        posts.append((url, json))
        return FakeResp({"ok": True})

    run_with_mocks(at, post_side_effect=post)
    assert not at.exception
    assert len(posts) == 1
    assert posts[0][0].endswith("/bot123:ABC/sendMessage")
    assert any("alert sent" in s.value for s in at.success)

    # same bar again -> no second message
    run_with_mocks(at, post_side_effect=post)
    assert len(posts) == 1
    assert any("already alerted" in c.value for c in at.caption)

    # failure path: Telegram rejects -> error shown, no "sent"
    at2 = app()
    run_with_mocks(at2)
    sidebar_checkbox(at2, "Telegram alerts").check()
    sidebar_text(at2, "Bot token").set_value("bad")
    sidebar_text(at2, "Chat ID").set_value("42")
    run_with_mocks(at2, post_side_effect=lambda url, json=None, timeout=None: FakeResp({"ok": False, "description": "Unauthorized"}, 401))
    assert any("Unauthorized" in e.value for e in at2.error)
    assert not any("alert sent" in s.value for s in at2.success)


# --------------------------------------------------------------------------- #
# Professional Signal
# --------------------------------------------------------------------------- #
def open_professional(at, key="dummy"):
    run_with_mocks(at)
    at.sidebar.selectbox[0].set_value("Professional Signal")
    sidebar_text(at, "Twelve Data API Key").set_value(key)
    run_with_mocks(at)
    return at


def test_professional_predict_creates_pending_record(app, isolated_history):
    at = open_professional(app())
    assert not at.exception
    button(at, "Predict Direction").click()
    gets = []

    def td_get(url, params=None, timeout=None):
        gets.append(params)
        return FakeResp(td_payload())

    run_with_mocks(at, td_side_effect=td_get)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any(s.value.startswith("Prediction:") for s in at.success)

    # request hygiene: UTC + one output size, and cached (single API call per run)
    assert all(p["timezone"] == "UTC" for p in gets)
    assert len({p["outputsize"] for p in gets}) == 1

    preds = at.session_state["predictions"]
    assert len(preds) == 1
    rec = preds[0]
    assert rec["resolved"] is False  # regression: used to resolve (as a LOSS) instantly
    assert rec["source"] == "model"
    assert rec["entry_time"] >= rec["entry_bar_time"]
    assert (rec["resolve_time"] - rec["entry_time"]).total_seconds() == 300
    labels = [m.label for m in at.metric]
    assert "Pending" in labels
    assert isolated_history.exists()  # persisted to disk


def test_professional_manual_call_and_reset(app):
    at = open_professional(app())
    button(at, "Log manual CALL (UP)").click()
    run_with_mocks(at)
    assert not at.exception
    rec = at.session_state["predictions"][0]
    assert rec["source"] == "manual" and rec["direction"] == "UP" and rec["confidence"] is None
    button(at, "Reset History").click()
    run_with_mocks(at)
    assert at.session_state["predictions"] == []


def test_professional_api_error_is_surfaced(app):
    at = open_professional(app(), key="badkey")
    button(at, "Predict Direction").click()
    run_with_mocks(
        at,
        td_side_effect=lambda url, params=None, timeout=None: FakeResp(
            {"code": 429, "message": "You have run out of API credits for the current minute.", "status": "error"}
        ),
    )
    assert not at.exception
    assert any("429" in e.value and "Rate limit" in e.value for e in at.error)
    assert at.session_state["predictions"] == []


def test_professional_without_key_disables_buttons(app):
    at = app()
    run_with_mocks(at)
    at.sidebar.selectbox[0].set_value("Professional Signal")
    sidebar_text(at, "Twelve Data API Key").set_value("")
    run_with_mocks(at)
    assert not at.exception
    assert button(at, "Predict Direction").disabled
    assert any("API key" in w.value for w in at.warning)


def test_history_survives_new_session(app, isolated_history):
    at = open_professional(app())
    button(at, "Predict Direction").click()
    run_with_mocks(at)
    assert len(at.session_state["predictions"]) == 1

    # brand-new session (what a browser reload does) -> record is reloaded from disk
    at2 = open_professional(app())
    assert len(at2.session_state["predictions"]) == 1
    assert at2.session_state["predictions"][0]["symbol"] == "EUR/USD"
