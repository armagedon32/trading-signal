"""Trading Signal Dashboard - Streamlit UI.

All indicator / model / tracker logic lives in the ``trading_signal`` package; this
file only wires it to widgets. Run with ``streamlit run app.py``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from trading_signal.config import (
    ASSET_OPTIONS,
    DATA_TTL_SECONDS,
    EXPIRY_OPTIONS,
    MODEL_OPTIONS,
    TIMEFRAME_OPTIONS,
    TWELVE_INTERVAL_MAP,
    expiry_to_seconds,
    max_lookback_days,
)
from trading_signal.data import (
    LoadResult,
    load_twelvedata,
    load_yfinance,
    send_telegram_message,
    utc_now_floor,
)
from trading_signal.indicators import backtest, backtest_summary, compute_indicators, min_bars_required
from trading_signal.market import market_status
from trading_signal.models import Prediction, latest_factors, linear_forecast, predict_direction
from trading_signal import tracker

try:  # optional: .env support
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

st.set_page_config(page_title="Trading Signal Dashboard", layout="wide")

HISTORY_FILE = os.getenv("TRADING_SIGNAL_HISTORY", os.path.join(".data", "predictions.json"))


# --------------------------------------------------------------------------- #
# Cached data access (thin wrappers so the library stays Streamlit-free)
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=DATA_TTL_SECONDS, show_spinner=False)
def cached_yf(symbol: str, interval: str, lookback_days: int, bucket: datetime) -> LoadResult:
    # ``bucket`` is only part of the cache key (see utc_now_floor); it is also a
    # sensible "now" for the request window.
    return load_yfinance(symbol, interval, lookback_days, now=bucket)


@st.cache_data(ttl=DATA_TTL_SECONDS, show_spinner=False)
def cached_td(symbol: str, interval: str, api_key: str, bucket: datetime) -> LoadResult:
    return load_twelvedata(symbol, interval, api_key)


@st.cache_data(ttl=DATA_TTL_SECONDS, show_spinner=False)
def cached_indicators(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    return compute_indicators(df, fast, slow)


def load_signals_yf(symbol: str, interval: str, lookback_days: int, fast: int, slow: int):
    """Return ``(indicator_frame, error_message)`` for a yfinance symbol."""
    res = cached_yf(symbol, interval, int(lookback_days), utc_now_floor())
    if not res.ok:
        return pd.DataFrame(), res.error
    sig = cached_indicators(res.df, int(fast), int(slow))
    if sig.empty:
        need = min_bars_required(fast, slow)
        return pd.DataFrame(), f"{symbol} {interval}: only {len(res.df)} bars, need about {need} for the indicators."
    return sig, None


# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
def init_state() -> None:
    if "predictions" not in st.session_state:
        st.session_state["predictions"] = tracker.load_records(HISTORY_FILE)
    st.session_state.setdefault("last_prediction_id", None)
    st.session_state.setdefault("last_alert_key", None)
    st.session_state.setdefault("alert_log", [])


def persist_predictions() -> None:
    try:
        tracker.save_records(st.session_state["predictions"], HISTORY_FILE)
    except OSError as exc:  # read-only FS etc. - history still lives in session
        st.session_state["persist_error"] = str(exc)


def get_record(rec_id):
    for rec in st.session_state["predictions"]:
        if rec["id"] == rec_id:
            return rec
    return None


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
def sidebar() -> dict:
    with st.sidebar:
        page = st.selectbox("Page", ["Dashboard", "Professional Signal"], index=0)

        st.header("Settings")
        st.subheader("Data Provider")
        secrets_key = ""
        try:
            secrets_key = st.secrets.get("TWELVE_DATA_API_KEY", "")
        except Exception:
            secrets_key = ""
        default_key = secrets_key or os.getenv("TWELVE_DATA_API_KEY", "")
        twelvedata_key = st.text_input("Twelve Data API Key", value=default_key, type="password")

        tickers_raw = st.text_area("Tickers (comma or newline)", value="AAPL, MSFT, NVDA")
        tickers = []
        for t in tickers_raw.replace("\n", ",").split(","):
            t = t.strip().upper()
            if t and t not in tickers:
                tickers.append(t)
        if not tickers:
            tickers = ["AAPL"]

        active_ticker = st.selectbox("Active Ticker", tickers, index=0)

        timeframes = st.multiselect("Timeframes", TIMEFRAME_OPTIONS, default=TIMEFRAME_OPTIONS)
        if not timeframes:
            timeframes = list(TIMEFRAME_OPTIONS)

        chart_timeframe = st.selectbox("Chart Timeframe", timeframes, index=0)
        alert_timeframe = st.selectbox("Alert Timeframe", timeframes, index=0)

        lookback_days = st.number_input("Lookback (days)", min_value=1, max_value=730, value=59)
        limit = max_lookback_days(chart_timeframe)
        if lookback_days > limit:
            st.caption(f"Yahoo only serves ~{limit} days of {chart_timeframe} bars; lookback is capped automatically.")

        sma_fast = st.number_input("Fast SMA", min_value=2, max_value=200, value=10)
        sma_slow = st.number_input("Slow SMA", min_value=3, max_value=400, value=30)
        if sma_fast >= sma_slow:
            st.warning("Fast SMA should be less than Slow SMA.")

        st.subheader("Alerts")
        enable_alerts = st.checkbox("Telegram alerts", value=False)
        telegram_token = st.text_input("Bot token", value=os.getenv("TELEGRAM_BOT_TOKEN", ""), type="password")
        telegram_chat_id = st.text_input("Chat ID", value=os.getenv("TELEGRAM_CHAT_ID", ""))

        st.subheader("Refresh")
        auto_refresh = st.checkbox("Auto-refresh (60s)", value=False)

    return dict(
        page=page,
        twelvedata_key=twelvedata_key.strip(),
        tickers=tickers,
        active_ticker=active_ticker,
        timeframes=timeframes,
        chart_timeframe=chart_timeframe,
        alert_timeframe=alert_timeframe,
        lookback_days=int(lookback_days),
        sma_fast=int(sma_fast),
        sma_slow=int(sma_slow),
        enable_alerts=enable_alerts,
        telegram_token=telegram_token.strip(),
        telegram_chat_id=telegram_chat_id.strip(),
        auto_refresh=auto_refresh,
    )


# --------------------------------------------------------------------------- #
# Dashboard page
# --------------------------------------------------------------------------- #
def signal_label(value) -> str:
    return "BUY" if int(value) == 1 else "SELL"


def fmt_price(p: float) -> str:
    return f"{p:,.5f}" if abs(p) < 10 else f"{p:,.2f}"


def render_dashboard(cfg: dict) -> None:
    fast, slow, lookback = cfg["sma_fast"], cfg["sma_slow"], cfg["lookback_days"]

    # ---- Watchlist -------------------------------------------------------
    watch_rows, watch_errors = [], []
    for t in cfg["tickers"]:
        sig, err = load_signals_yf(t, cfg["chart_timeframe"], lookback, fast, slow)
        if err:
            watch_errors.append(err)
            continue
        latest = sig.iloc[-1]
        watch_rows.append(
            {
                "Ticker": t,
                "Price": float(latest["Close"]),
                "Signal": signal_label(latest["Signal"]),
                "RSI": round(float(latest["RSI"]), 1),
                "ADX": round(float(latest["ADX"]), 1) if pd.notna(latest["ADX"]) else None,
                "Last Bar": sig.index[-1].strftime("%Y-%m-%d %H:%M %Z"),
            }
        )

    st.subheader(f"Watchlist ({cfg['chart_timeframe']})")
    if watch_rows:
        st.dataframe(pd.DataFrame(watch_rows), width="stretch", hide_index=True)
    if watch_errors:
        with st.expander(f"{len(watch_errors)} ticker(s) could not be loaded"):
            for e in watch_errors:
                st.write("- " + e)
    if not watch_rows:
        st.info("No watchlist data. Try different tickers or a different timeframe.")

    # ---- Multi-timeframe ---------------------------------------------------
    mt_rows, mt_errors = [], []
    for tf in cfg["timeframes"]:
        sig, err = load_signals_yf(cfg["active_ticker"], tf, lookback, fast, slow)
        if err:
            mt_errors.append(err)
            continue
        latest = sig.iloc[-1]
        mt_rows.append(
            {
                "Timeframe": tf,
                "Signal": signal_label(latest["Signal"]),
                "Price": float(latest["Close"]),
                "RSI": round(float(latest["RSI"]), 1),
                "Last Bar": sig.index[-1].strftime("%Y-%m-%d %H:%M %Z"),
            }
        )
    if mt_rows:
        st.subheader(f"Multi-Timeframe Signals - {cfg['active_ticker']}")
        st.dataframe(pd.DataFrame(mt_rows), width="stretch", hide_index=True)
    if mt_errors:
        with st.expander(f"{len(mt_errors)} timeframe(s) could not be loaded"):
            for e in mt_errors:
                st.write("- " + e)

    # ---- Active ticker -------------------------------------------------------
    signal_data, err = load_signals_yf(cfg["active_ticker"], cfg["chart_timeframe"], lookback, fast, slow)
    if err:
        st.warning(err)
        return

    latest = signal_data.iloc[-1]
    latest_signal = signal_label(latest["Signal"])
    latest_time = signal_data.index[-1]
    age = datetime.now(timezone.utc) - latest_time.tz_convert("UTC").to_pydatetime()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Latest Price", fmt_price(float(latest["Close"])))
    c2.metric("Signal", latest_signal)
    c3.metric("Last Bar", latest_time.strftime("%Y-%m-%d %H:%M %Z"))
    c4.metric("Data age", f"{int(age.total_seconds() // 60)} min")

    # ---- Forecast -----------------------------------------------------------
    forecast = linear_forecast(signal_data)
    if forecast.ok:
        oos = (
            f" | walk-forward hit-rate {forecast.oos_accuracy * 100:.0f}% (n={forecast.oos_samples})"
            if forecast.oos_accuracy is not None
            else ""
        )
        st.info(
            f"Forecast (next bar, experimental): **{forecast.label}** | "
            f"predicted return {forecast.pred_return * 100:.3f}%{oos}"
        )
    else:
        st.info(f"Forecast unavailable: {forecast.reason}")

    # ---- Telegram ------------------------------------------------------------
    if cfg["enable_alerts"]:
        handle_alert(cfg)

    # ---- Chart -----------------------------------------------------------------
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=signal_data.index, y=signal_data["Close"], mode="lines", name="Close"))
    fig.add_trace(go.Scatter(x=signal_data.index, y=signal_data["SMA_Fast"], mode="lines", name=f"SMA {fast}"))
    fig.add_trace(go.Scatter(x=signal_data.index, y=signal_data["SMA_Slow"], mode="lines", name=f"SMA {slow}"))
    ups = signal_data[signal_data["Cross_Up"]]
    downs = signal_data[signal_data["Cross_Down"]]
    fig.add_trace(
        go.Scatter(
            x=ups.index, y=ups["Close"], mode="markers", marker=dict(symbol="triangle-up", size=11, color="green"),
            name="Cross Up",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=downs.index, y=downs["Close"], mode="markers", marker=dict(symbol="triangle-down", size=11, color="red"),
            name="Cross Down",
        )
    )
    fig.update_layout(height=520, margin=dict(l=20, r=20, t=30, b=20), legend=dict(orientation="h"))
    st.plotly_chart(fig, width="stretch")

    # ---- Backtest ----------------------------------------------------------------
    bt = backtest(signal_data)
    summary = backtest_summary(bt)
    if summary:
        st.subheader("Backtest Summary (long-only SMA crossover, next-bar execution)")
        b1, b2, b3, b4 = st.columns(4)
        b1.metric("Strategy Return", f"{summary['total_return']:.2f}%")
        b2.metric("Buy & Hold", f"{summary['buy_hold_return']:.2f}%")
        b3.metric("Max Drawdown", f"{summary['max_drawdown']:.2f}%")
        b4.metric("Exposure", f"{summary['exposure']:.0f}%")
        b5, b6, b7, b8 = st.columns(4)
        b5.metric("Trades", str(summary["n_trades"]))
        b6.metric("Trade Win Rate", f"{summary['trade_win_rate']:.1f}%")
        b7.metric("Avg Trade", f"{summary['avg_trade_return']:.2f}%")
        b8.metric("Bar Win Rate (in market)", f"{summary['bar_win_rate']:.1f}%")

        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=bt.index, y=bt["Equity"], mode="lines", name="Strategy"))
        fig2.add_trace(go.Scatter(x=bt.index, y=bt["BuyHold_Equity"], mode="lines", name="Buy & Hold"))
        fig2.update_layout(height=300, margin=dict(l=20, r=20, t=30, b=20), legend=dict(orientation="h"))
        st.plotly_chart(fig2, width="stretch")
        st.caption("No fees, slippage or shorting. Past performance of a demo strategy is not financial advice.")


def handle_alert(cfg: dict) -> None:
    """Send one Telegram message per new bar on the alert timeframe."""
    if not (cfg["telegram_token"] and cfg["telegram_chat_id"]):
        st.warning("Telegram alerts are enabled but bot token / chat id are empty.")
        return

    sig, err = load_signals_yf(cfg["active_ticker"], cfg["alert_timeframe"], cfg["lookback_days"], cfg["sma_fast"], cfg["sma_slow"])
    if err:
        st.warning(f"Alert skipped: {err}")
        return

    latest = sig.iloc[-1]
    bar_time = sig.index[-1].strftime("%Y-%m-%d %H:%M %Z")
    alert_key = f"{cfg['active_ticker']}|{cfg['alert_timeframe']}|{bar_time}"
    if st.session_state.get("last_alert_key") == alert_key:
        st.caption(f"Telegram: already alerted for bar {bar_time}.")
        return

    fc = linear_forecast(sig)
    extra = f" | forecast next: {fc.label} ({fc.pred_return * 100:.3f}%)" if fc.ok else ""
    msg = (
        f"{cfg['active_ticker']} {cfg['alert_timeframe']} signal: {signal_label(latest['Signal'])} "
        f"at {fmt_price(float(latest['Close']))} ({bar_time}){extra}"
    )
    ok, detail = send_telegram_message(cfg["telegram_token"], cfg["telegram_chat_id"], msg)
    if ok:
        st.session_state["last_alert_key"] = alert_key
        st.success(f"Telegram alert sent for bar {bar_time}.")
    else:
        st.error(f"Telegram alert failed: {detail}")


# --------------------------------------------------------------------------- #
# Professional Signal page
# --------------------------------------------------------------------------- #
def td_bars(symbol: str, expiry: str, api_key: str) -> LoadResult:
    return cached_td(symbol, TWELVE_INTERVAL_MAP.get(expiry, "5min"), api_key, utc_now_floor())


def resolve_tracker(api_key: str) -> None:
    if not api_key or not st.session_state["predictions"]:
        return

    def fetch(symbol: str, expiry: str):
        res = td_bars(symbol, expiry, api_key)
        return res.df if res.ok else None

    records, n = tracker.resolve_pending(st.session_state["predictions"], fetch)
    st.session_state["predictions"] = records
    if n:
        persist_predictions()
        st.toast(f"Resolved {n} prediction(s).")


def add_record(rec: dict) -> None:
    st.session_state["predictions"].append(rec)
    st.session_state["last_prediction_id"] = rec["id"]
    persist_predictions()


def render_professional(cfg: dict) -> None:
    api_key = cfg["twelvedata_key"]
    st.subheader("Professional Signal")
    st.caption("Pocket Option style view: pick an asset and expiry, get an Up/Down call and track how it resolves.")

    if not api_key:
        st.warning("Add your Twelve Data API key in the sidebar (or a `.env` with `TWELVE_DATA_API_KEY`).")

    a1, a2, a3 = st.columns([2, 1, 1])
    with a1:
        asset_choice = st.selectbox("Select Asset", ASSET_OPTIONS, index=0)
        symbol = asset_choice
        if asset_choice == "Custom":
            symbol = st.text_input("Custom Symbol (e.g. GBP/USD, TSLA, XAG/USD)", value="GBP/USD").strip().upper()
    with a2:
        expiry = st.selectbox("Expiry", EXPIRY_OPTIONS, index=1)
    with a3:
        model_type = st.selectbox("Model", MODEL_OPTIONS, index=1)

    cls, is_open = market_status(symbol)
    st.caption(
        f"Market status for **{symbol}** ({cls}): {'OPEN' if is_open else 'CLOSED'} - "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    if not is_open:
        st.warning("This market looks closed right now; new bars will not arrive and predictions cannot resolve properly.")

    live = st.checkbox("Live countdown / auto-resolve (refresh every 15s)", value=True)

    # ---- Actions -------------------------------------------------------------
    b1, b2, b3 = st.columns(3)
    predict_btn = b1.button("Predict Direction", type="primary", width="stretch", disabled=not api_key)
    call_btn = b2.button("Log manual CALL (UP)", width="stretch", disabled=not api_key)
    put_btn = b3.button("Log manual PUT (DOWN)", width="stretch", disabled=not api_key)

    if predict_btn or call_btn or put_btn:
        handle_professional_action(symbol, expiry, model_type, api_key, cfg, predict_btn, call_btn, put_btn)

    # Resolve expired predictions before rendering the tracker views.
    resolve_tracker(api_key)

    if live:
        live_status(api_key)
    else:
        render_status(api_key)

    render_history()


def handle_professional_action(symbol, expiry, model_type, api_key, cfg, predict_btn, call_btn, put_btn) -> None:
    res = td_bars(symbol, expiry, api_key)
    if not res.ok:
        st.error(res.error)
        return
    sig = cached_indicators(res.df, cfg["sma_fast"], cfg["sma_slow"])
    if sig.empty:
        st.error(f"Not enough bars to compute indicators (have {len(res.df)}, need ~{min_bars_required(cfg['sma_fast'], cfg['sma_slow'])}).")
        return

    entry_price = float(sig["Close"].iloc[-1])
    entry_bar_time = sig.index[-1]
    now = datetime.now(timezone.utc)

    if predict_btn:
        pred: Prediction = predict_direction(sig, model_type=model_type)
        if not pred.ok:
            st.info(f"Prediction unavailable: {pred.reason}")
            return
        rec = tracker.new_record(
            symbol=symbol,
            expiry=expiry,
            direction=pred.direction,
            entry_price=entry_price,
            entry_bar_time=entry_bar_time,
            now=now,
            confidence=pred.confidence,
            model=pred.model,
            source="model",
            oos_accuracy=pred.oos_accuracy,
        )
        rec["factors"] = latest_factors(sig)
        add_record(rec)
        oos = (
            f" | walk-forward hit-rate {pred.oos_accuracy * 100:.0f}% (n={pred.oos_samples})"
            if pred.oos_accuracy is not None
            else " | no out-of-sample estimate"
        )
        st.success(f"Prediction: **{pred.label}** | model confidence {pred.confidence * 100:.1f}%{oos}")
        if pred.oos_accuracy is not None and pred.oos_accuracy < 0.52:
            st.warning("Recent out-of-sample accuracy is near coin-flip; treat the confidence number with suspicion.")
    else:
        direction = "UP" if call_btn else "DOWN"
        rec = tracker.new_record(
            symbol=symbol,
            expiry=expiry,
            direction=direction,
            entry_price=entry_price,
            entry_bar_time=entry_bar_time,
            now=now,
            confidence=None,
            model="manual",
            source="manual",
        )
        rec["factors"] = latest_factors(sig)
        add_record(rec)
        st.success(f"Logged manual {'CALL (UP)' if call_btn else 'PUT (DOWN)'} at {fmt_price(entry_price)}.")

    if rec["stale_entry"]:
        st.warning(
            f"Latest bar is {rec['data_lag_seconds'] / 60:.0f} min old - the feed may be delayed or the market closed. "
            "Entry price may not reflect the live quote."
        )


@st.fragment(run_every="15s")
def live_status(api_key: str) -> None:
    resolve_tracker(api_key)
    render_status(api_key)


def render_status(api_key: str) -> None:
    st.subheader("Current Signal")
    rec = get_record(st.session_state.get("last_prediction_id"))
    if rec is None:
        st.info("No prediction yet. Click **Predict Direction** (or log a manual CALL/PUT) to start.")
        return

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Asset", rec["symbol"])
    c2.metric("Expiry", rec["expiry"])
    c3.metric("Direction", "UP (Call)" if rec["direction"] == "UP" else "DOWN (Put)")
    conf = rec.get("confidence")
    c4.metric("Confidence", f"{conf * 100:.1f}%" if conf is not None else "manual")
    c5.metric("Entry", fmt_price(rec["entry_price"]))
    st.caption(
        f"Model: {rec.get('model', '')} | entered {tracker._utc(rec['entry_time']).strftime('%H:%M:%S UTC')} "
        f"on bar {tracker._utc(rec['entry_bar_time']).strftime('%H:%M UTC')} | "
        f"expires {tracker._utc(rec['resolve_time']).strftime('%H:%M:%S UTC')}"
    )

    if rec["resolved"]:
        outcome = rec["outcome"]
        colour = {"WIN": "green", "LOSS": "red"}.get(outcome, "gray")
        st.markdown(f"Result: :{colour}[**{outcome}**] - exit {fmt_price(rec['exit_price'])} " + (f"({rec['note']})" if rec.get("note") else ""))
    else:
        remaining = tracker.seconds_remaining(rec)
        total = expiry_to_seconds(rec["expiry"])
        st.progress(1 - remaining / total if total else 1.0, text=f"Time remaining {remaining // 60:02d}:{remaining % 60:02d}")
        if remaining == 0:
            st.info("Expired - waiting for the bar that covers the expiry time to resolve the outcome.")

    # Live price vs entry
    if api_key:
        res = td_bars(rec["symbol"], rec["expiry"], api_key)
        if res.ok:
            entry_bar = tracker._utc(rec["entry_bar_time"])
            since = res.df[res.df.index >= pd.Timestamp(entry_bar)]
            if not since.empty:
                cur = float(since["Close"].iloc[-1])
                delta = cur - rec["entry_price"]
                pct = delta / rec["entry_price"] * 100 if rec["entry_price"] else 0.0
                in_money = (delta > 0 and rec["direction"] == "UP") or (delta < 0 and rec["direction"] == "DOWN")
                m1, m2 = st.columns([1, 3])
                m1.metric("Current", fmt_price(cur), f"{pct:+.3f}%")
                m1.caption("In the money" if in_money else ("At entry" if delta == 0 else "Out of the money"))
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=since.index, y=since["Close"], mode="lines+markers", name="Price"))
                fig.add_hline(y=rec["entry_price"], line_dash="dash", line_color="gray", annotation_text="Entry")
                # add_vline(annotation_text=...) cannot handle datetime x values in plotly 5,
                # so draw the expiry marker as a shape + annotation with an ISO timestamp.
                expiry_x = pd.Timestamp(tracker._utc(rec["resolve_time"])).isoformat()
                fig.add_shape(type="line", x0=expiry_x, x1=expiry_x, y0=0, y1=1, yref="paper", line=dict(dash="dot", color="orange"))
                fig.add_annotation(x=expiry_x, y=1, yref="paper", text="Expiry", showarrow=False, yanchor="bottom")
                fig.update_layout(height=280, margin=dict(l=20, r=20, t=30, b=20))
                m2.plotly_chart(fig, width="stretch")
        else:
            st.caption(f"Live price unavailable: {res.error}")

    factors = rec.get("factors")
    if factors:
        with st.expander("Signal factors at entry"):
            st.dataframe(pd.DataFrame([factors]), width="stretch", hide_index=True)


def render_history() -> None:
    st.subheader("Prediction Accuracy")
    records = st.session_state["predictions"]
    if not records:
        st.info("No predictions yet.")
        return

    overall = tracker.summarize(records)
    model_stats = tracker.summarize(records, source="model")
    manual_stats = tracker.summarize(records, source="manual")

    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("Wins", overall["wins"])
    s2.metric("Losses", overall["losses"])
    s3.metric("Ties", overall["ties"])
    s4.metric("Pending", overall["pending"])
    s5.metric("Win Rate", f"{overall['win_rate']:.1f}%" if overall["win_rate"] is not None else "-")
    st.caption(
        f"Model: {model_stats['wins']}W/{model_stats['losses']}L"
        + (f" ({model_stats['win_rate']:.0f}%)" if model_stats["win_rate"] is not None else "")
        + f" | Manual: {manual_stats['wins']}W/{manual_stats['losses']}L"
        + (f" ({manual_stats['win_rate']:.0f}%)" if manual_stats["win_rate"] is not None else "")
        + f" | history file: {HISTORY_FILE}"
    )
    if st.session_state.get("persist_error"):
        st.caption(f"Could not write history file: {st.session_state['persist_error']}")

    buckets = tracker.confidence_buckets(records)
    if not buckets.empty and buckets["count"].sum() > 0:
        fig = go.Figure(
            go.Bar(x=buckets["bucket"], y=buckets["win_rate"], text=buckets["count"], textposition="auto", name="Win rate")
        )
        fig.add_hline(y=50, line_dash="dot", line_color="gray")
        fig.update_layout(height=300, yaxis_title="Win Rate (%)", xaxis_title="Model confidence bucket", margin=dict(l=20, r=20, t=30, b=20))
        st.plotly_chart(fig, width="stretch")

    with st.expander("History"):
        st.dataframe(tracker.to_frame(records), width="stretch", hide_index=True)

    h1, h2 = st.columns([1, 5])
    if h1.button("Reset History"):
        st.session_state["predictions"] = []
        st.session_state["last_prediction_id"] = None
        persist_predictions()
        st.rerun()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    init_state()
    cfg = sidebar()
    st.title("Trading Signal Dashboard")

    if cfg["page"] == "Dashboard":
        if cfg["auto_refresh"]:

            @st.fragment(run_every="60s")
            def dashboard_fragment():
                st.caption(f"Auto-refresh on - last run {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
                render_dashboard(cfg)

            dashboard_fragment()
        else:
            render_dashboard(cfg)
    else:
        render_professional(cfg)

    st.caption("Data via Yahoo Finance (delayed) and Twelve Data. Signals are experimental and not financial advice.")


main()
