import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import plotly.graph_objects as go
import requests
from datetime import datetime, timedelta
import time
import os
from datetime import timezone
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import RandomForestClassifier
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

st.set_page_config(page_title="Trading Signal Dashboard", layout="wide")

PAGES = ["Dashboard", "Professional Signal"]
with st.sidebar:
    page = st.selectbox("Page", PAGES, index=0)

st.title("Trading Signal Dashboard")

TIMEFRAME_OPTIONS = ["5m", "15m", "1h", "1d"]
EXPIRY_OPTIONS = ["1m", "5m", "1h"]
TWELVE_INTERVAL_MAP = {"1m": "1min", "5m": "5min", "1h": "1h"}

with st.sidebar:
    st.header("Settings")
    st.subheader("Data Provider")
    secrets_key = ""
    try:
        secrets_key = st.secrets.get("TWELVE_DATA_API_KEY", "")
    except Exception:
        secrets_key = ""
    env_key = os.getenv("TWELVE_DATA_API_KEY", "")
    default_key = secrets_key or env_key
    twelvedata_key = st.text_input("Twelve Data API Key", value=default_key, type="password")

    tickers_raw = st.text_area("Tickers (comma or newline)", value="AAPL, MSFT, NVDA")
    tickers = [t.strip().upper() for t in tickers_raw.replace("\n", ",").split(",") if t.strip()]
    if not tickers:
        tickers = ["AAPL"]

    active_ticker = st.selectbox("Active Ticker", tickers, index=0)

    timeframes = st.multiselect(
        "Timeframes",
        TIMEFRAME_OPTIONS,
        default=["5m", "15m", "1h", "1d"],
    )
    if not timeframes:
        timeframes = ["5m", "15m", "1h", "1d"]

    chart_timeframe = st.selectbox("Chart Timeframe", timeframes, index=0)
    alert_timeframe = st.selectbox("Alert Timeframe", timeframes, index=0)

    lookback_days = st.number_input("Lookback (days)", min_value=1, max_value=365, value=60)
    sma_fast = st.number_input("Fast SMA", min_value=2, max_value=200, value=10)
    sma_slow = st.number_input("Slow SMA", min_value=3, max_value=400, value=30)

    st.subheader("Alerts")
    enable_alerts = st.checkbox("Telegram alerts", value=False)
    telegram_token = st.text_input("Bot token", value="", type="password")
    telegram_chat_id = st.text_input("Chat ID", value="")

    st.subheader("Refresh")
    refresh = st.checkbox("Auto-refresh", value=False)

    # Signal controls removed (back to previous setup)

if sma_fast >= sma_slow:
    st.warning("Fast SMA should be less than Slow SMA.")

end = datetime.utcnow()
start = end - timedelta(days=int(lookback_days))

@st.cache_data(ttl=60)
def load_data(symbol: str, start_dt: datetime, end_dt: datetime, interval: str) -> pd.DataFrame:
    df = yf.download(
        symbol,
        start=start_dt.strftime("%Y-%m-%d"),
        end=end_dt.strftime("%Y-%m-%d"),
        interval=interval,
        progress=False,
        auto_adjust=True,
    )
    df = df.rename(columns=str.title)
    df = df.dropna()
    return df

@st.cache_data(ttl=60)
def load_twelvedata(symbol: str, interval: str, outputsize: int, api_key: str) -> pd.DataFrame:
    if not api_key:
        return pd.DataFrame()
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": api_key,
        "format": "JSON",
    }
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()
    values = data.get("values", [])
    if not values:
        return pd.DataFrame()
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.sort_values("datetime")
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
    })
    df = df.set_index("datetime").dropna()
    return df

@st.cache_data(ttl=60)
def compute_indicators(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    data = df.copy()
    # yfinance can return multi-index columns; ensure we get a 1D Close series
    if isinstance(data.columns, pd.MultiIndex):
        # Prefer the first level labeled "Close"
        if "Close" in data.columns.get_level_values(0):
            data.columns = data.columns.get_level_values(0)
        else:
            data.columns = data.columns.get_level_values(-1)
    close = data["Close"].squeeze()

    data["SMA_Fast"] = close.rolling(window=int(fast)).mean()
    data["SMA_Slow"] = close.rolling(window=int(slow)).mean()
    data["Signal"] = (data["SMA_Fast"] > data["SMA_Slow"]).astype(int)
    data["Cross_Up"] = data["Signal"].diff() == 1
    data["Cross_Down"] = data["Signal"].diff() == -1

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    roll = 14
    avg_gain = gain.rolling(roll).mean()
    avg_loss = loss.rolling(roll).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    data["RSI"] = 100 - (100 / (1 + rs))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    data["MACD"] = ema12 - ema26
    data["MACD_Signal"] = data["MACD"].ewm(span=9, adjust=False).mean()
    data["MACD_Hist"] = data["MACD"] - data["MACD_Signal"]

    bb_window = 20
    bb_std = 2
    bb_mid = close.rolling(bb_window).mean()
    bb_dev = close.rolling(bb_window).std()
    bb_upper = bb_mid + bb_std * bb_dev
    bb_lower = bb_mid - bb_std * bb_dev
    data["BB_Upper"] = bb_upper
    data["BB_Lower"] = bb_lower
    bb_range = (bb_upper - bb_lower).replace(0, np.nan)
    data["BB_Pct"] = (close - bb_lower) / bb_range

    data["Return"] = close.pct_change()
    data = data.dropna()
    return data

@st.cache_data(ttl=60)
def backtest(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    data["Return"] = data["Close"].pct_change().fillna(0)
    data["Position"] = data["Signal"].shift(1).fillna(0)
    data["Strategy_Return"] = data["Position"] * data["Return"]
    data["Equity"] = (1 + data["Strategy_Return"]).cumprod()
    return data

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    plus_dm = (high.diff()).clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    tr = pd.concat([
        (high - low),
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di)).replace([np.inf, -np.inf], np.nan)
    adx = dx.rolling(period).mean()
    return adx

def predict_direction(df: pd.DataFrame, model_type: str = "ML Lite") -> dict:
    data = df.copy()
    features = pd.DataFrame({
        "ret": data["Return"],
        "sma_diff": (data["SMA_Fast"] - data["SMA_Slow"]) / data["Close"],
        "rsi": data["RSI"],
        "macd": data["MACD_Hist"],
        "bb": data["BB_Pct"],
    })
    target = (data["Return"].shift(-1) > 0).astype(int)
    model_df = pd.concat([features, target.rename("target")], axis=1).dropna()

    if model_type == "Trend":
        # EMA slope + ADX trend model (rule-based)
        ema_fast = data["Close"].ewm(span=12, adjust=False).mean()
        ema_slow = data["Close"].ewm(span=26, adjust=False).mean()
        slope = (ema_fast - ema_slow).iloc[-1]
        adx = compute_adx(data).iloc[-1]
        if np.isnan(adx):
            return {"ok": False, "reason": "Not enough data for trend model"}
        direction = "UP (Call)" if slope > 0 else "DOWN (Put)"
        confidence = min(0.95, max(0.5, float(adx) / 50))
        return {"ok": True, "direction": direction, "confidence": confidence}

    if len(model_df) < 200:
        return {"ok": False, "reason": "Not enough data for ML model"}

    train = model_df.tail(500)
    X = train[["ret", "sma_diff", "rsi", "macd", "bb"]].values
    y = train["target"].values

    if model_type == "ML Advanced":
        try:
            import xgboost as xgb  # optional
            model = xgb.XGBClassifier(
                n_estimators=200,
                max_depth=4,
                learning_rate=0.1,
                subsample=0.9,
                colsample_bytree=0.9,
                eval_metric="logloss",
                tree_method="hist",
            )
            model.fit(X, y)
            last_feat = features.iloc[-1].values.astype(float).reshape(1, -1)
            prob_up = float(model.predict_proba(last_feat)[0][1])
        except Exception:
            model = RandomForestClassifier(n_estimators=200, random_state=42)
            model.fit(X, y)
            last_feat = features.iloc[-1].values.astype(float).reshape(1, -1)
            prob_up = float(model.predict_proba(last_feat)[0][1])
    else:
        scaler = StandardScaler()
        Xs = scaler.fit_transform(X)

        model = LogisticRegression(max_iter=500)
        model.fit(Xs, y)

        last_feat = features.iloc[-1].values.astype(float).reshape(1, -1)
        last_feat = scaler.transform(last_feat)
        prob_up = float(model.predict_proba(last_feat)[0][1])
    direction = "UP (Call)" if prob_up >= 0.5 else "DOWN (Put)"
    confidence = prob_up if prob_up >= 0.5 else 1 - prob_up
    return {"ok": True, "direction": direction, "confidence": confidence}

def expiry_to_timedelta(expiry_str: str) -> timedelta:
    if expiry_str == "1m":
        return timedelta(minutes=1)
    if expiry_str == "5m":
        return timedelta(minutes=5)
    if expiry_str == "1h":
        return timedelta(hours=1)
    return timedelta(minutes=5)

def expiry_to_seconds(expiry_str: str) -> int:
    if expiry_str == "1m":
        return 60
    if expiry_str == "5m":
        return 300
    if expiry_str == "1h":
        return 3600
    return 300

def update_accuracy_tracker(api_key: str):
    if "predictions" not in st.session_state:
        st.session_state["predictions"] = []

    pending = st.session_state["predictions"]
    if not pending:
        return

    updated = []
    last_loss_time = st.session_state.get("last_loss_time")
    for p in pending:
        if p.get("resolved"):
            updated.append(p)
            continue

        now = datetime.now(timezone.utc)
        resolve_time = p["resolve_time"]
        if resolve_time.tzinfo is None:
            resolve_time = resolve_time.replace(tzinfo=timezone.utc)
        if now < resolve_time:
            updated.append(p)
            continue

        interval = TWELVE_INTERVAL_MAP.get(p["expiry"], "5min")
        df_td = load_twelvedata(p["symbol"], interval, outputsize=500, api_key=api_key)
        if df_td.empty:
            updated.append(p)
            continue

        last_price = float(df_td["Close"].iloc[-1])
        outcome_up = last_price > p["entry_price"]
        predicted_up = p["direction"].startswith("UP")
        p["resolved"] = True
        p["exit_price"] = last_price
        p["correct"] = outcome_up == predicted_up
        if not p["correct"]:
            last_loss_time = datetime.now(timezone.utc)
        updated.append(p)

    st.session_state["predictions"] = updated
    if last_loss_time:
        st.session_state["last_loss_time"] = last_loss_time

def render_professional_signal():
    st.subheader("Professional Signal")
    st.caption("Pocket Option style view: select asset, choose expiry, predict direction.")
    # Market status indicator (simple FX vs Crypto heuristic)
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()  # 0=Mon ... 6=Sun
    fx_open = weekday < 5  # Mon-Fri
    st.info(f"Market status (UTC): FX {'OPEN' if fx_open else 'CLOSED'} | Crypto OPEN 24/7")
    live_refresh = False
    asset_options = [
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
    asset_choice = st.selectbox("Select Asset", asset_options, index=0)
    custom_asset = ""
    if asset_choice == "Custom":
        custom_asset = st.text_input("Custom Symbol (e.g., GBP/USD, TSLA, XAG/USD)", value="GBP/USD")

    expiry = st.selectbox("Choose Expiry Time", EXPIRY_OPTIONS, index=1)
    model_type = st.selectbox("Model", ["Trend", "ML Lite", "ML Advanced"], index=1)
    auto_refresh = st.checkbox("Auto-refresh at expiry", value=True)
    if auto_refresh:
        refresh_seconds = expiry_to_seconds(expiry)
        st.markdown(f"<meta http-equiv='refresh' content='{refresh_seconds}'>", unsafe_allow_html=True)


    colp1, colp2 = st.columns(2)
    with colp1:
        predict_btn = st.button("Predict Direction")
    with colp2:
        st.caption("Prediction uses a simple ML model on recent indicators.")

    symbol = custom_asset.strip().upper() if asset_choice == "Custom" else asset_choice

    if predict_btn:
        if not twelvedata_key:
            st.warning("Add your Twelve Data API key in the sidebar.")
        else:
            interval = TWELVE_INTERVAL_MAP.get(expiry, "5min")
            df_td = load_twelvedata(symbol, interval, outputsize=800, api_key=twelvedata_key)
            if df_td.empty:
                st.error("No data returned. Check symbol, API key, or try a different expiry.")
            else:
                sig_td = compute_indicators(df_td, int(sma_fast), int(sma_slow))
                if sig_td.empty:
                    st.error("Not enough data to compute indicators.")
                else:
                    pred = predict_direction(sig_td, model_type=model_type)
                    if pred.get("ok"):
                        conf_pct = pred["confidence"] * 100
                        st.success(f"Prediction: {pred['direction']} | Confidence: {conf_pct:.1f}%")
                        entry_price = float(sig_td["Close"].iloc[-1])
                        entry_time = pd.Timestamp(sig_td.index[-1]).to_pydatetime()
                        resolve_time = entry_time + expiry_to_timedelta(expiry)
                        factors = {
                            "Return": float(sig_td["Return"].iloc[-1]),
                            "SMA_Diff": float(((sig_td["SMA_Fast"] - sig_td["SMA_Slow"]) / sig_td["Close"]).iloc[-1]),
                            "RSI": float(sig_td["RSI"].iloc[-1]),
                            "MACD_Hist": float(sig_td["MACD_Hist"].iloc[-1]),
                            "BB_Pct": float(sig_td["BB_Pct"].iloc[-1]),
                        }
                        st.session_state["last_prediction"] = {
                            "symbol": symbol,
                            "expiry": expiry,
                            "direction": pred["direction"],
                            "confidence": pred["confidence"],
                            "entry_time": entry_time,
                            "entry_price": entry_price,
                            "resolve_time": resolve_time,
                            "factors": factors,
                        }
                        st.session_state.setdefault("predictions", []).append({
                            "symbol": symbol,
                            "expiry": expiry,
                            "direction": pred["direction"],
                            "confidence": pred["confidence"],
                            "entry_time": entry_time,
                            "resolve_time": resolve_time,
                            "entry_price": entry_price,
                            "resolved": False,
                            "correct": None,
                            "exit_price": None,
                        })
                    else:
                        st.info(f"Prediction unavailable: {pred.get('reason')}")

    if twelvedata_key:
        update_accuracy_tracker(twelvedata_key)

    st.subheader("Pocket Option Style")
    last_pred = st.session_state.get("last_prediction")
    if last_pred:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Asset", last_pred["symbol"])
        c2.metric("Expiry", last_pred["expiry"])
        c3.metric("Direction", last_pred["direction"])
        c4.metric("Confidence", f"{last_pred['confidence']*100:.1f}%")
        st.caption(f"Entry time: {last_pred['entry_time']} | Entry price: {last_pred['entry_price']:.5f}")

        valid_seconds = expiry_to_seconds(last_pred["expiry"])
        now_utc = datetime.now(timezone.utc)
        entry_time = last_pred["entry_time"]
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        elapsed = (now_utc - entry_time).total_seconds()
        remaining = max(0, int(valid_seconds - elapsed))
        minutes = remaining // 60
        seconds = remaining % 60
        st.info(f"Signal valid for {valid_seconds} seconds. Time remaining: {minutes:02d}:{seconds:02d}")
        if remaining == 0:
            st.warning("Signal expired. Generate a new prediction.")
    else:
        st.info("No prediction yet. Click Predict Direction to generate one.")

    st.subheader("Signal Factors")
    if last_pred and last_pred.get("factors"):
        f = last_pred["factors"]
        f_df = pd.DataFrame([{
            "Return": f["Return"],
            "SMA_Diff": f["SMA_Diff"],
            "RSI": f["RSI"],
            "MACD_Hist": f["MACD_Hist"],
            "BB_Pct": f["BB_Pct"],
        }])
        st.dataframe(f_df, use_container_width=True)
    else:
        st.info("No factors yet. Make a prediction first.")

    st.subheader("Trade Status (Ups/Downs)")
    if last_pred and twelvedata_key:
        interval = TWELVE_INTERVAL_MAP.get(last_pred["expiry"], "5min")
        status_df = load_twelvedata(last_pred["symbol"], interval, outputsize=200, api_key=twelvedata_key)
        if not status_df.empty:
            entry_time = last_pred["entry_time"]
            status_df = status_df[status_df.index >= entry_time]
            if not status_df.empty:
                current_price = float(status_df["Close"].iloc[-1])
                entry_price = float(last_pred["entry_price"])
                delta = current_price - entry_price
                pct = (delta / entry_price) * 100
                st.metric("Current Price", f"{current_price:.5f}", f"{pct:.3f}%")

                fig_status = go.Figure()
                fig_status.add_trace(go.Scatter(
                    x=status_df.index,
                    y=status_df["Close"],
                    mode="lines",
                    name="Price",
                ))
                fig_status.add_hline(y=entry_price, line_dash="dash", line_color="gray", annotation_text="Entry")
                fig_status.update_layout(height=300, margin=dict(l=20, r=20, t=40, b=20))
                st.plotly_chart(fig_status, use_container_width=True)
            else:
                st.info("Waiting for new bars to track movement.")
        else:
            st.info("No status data available yet.")
    elif last_pred and not twelvedata_key:
        st.info("Add your Twelve Data API key to see live trade status.")
    else:
        st.info("Make a prediction to start tracking the trade status.")

    st.subheader("Prediction Accuracy")
    preds = st.session_state.get("predictions", [])
    if preds:
        dfp = pd.DataFrame(preds)
        resolved = dfp[dfp["resolved"]]
        total = len(resolved)
        wins = int(resolved["correct"].sum()) if total > 0 else 0
        losses = int(total - wins) if total > 0 else 0
        win_rate = (wins / total * 100) if total > 0 else 0

        s1, s2, s3 = st.columns(3)
        s1.metric("Wins", str(wins))
        s2.metric("Losses", str(losses))
        s3.metric("Win Rate", f"{win_rate:.1f}%")

        st.markdown("**History**")
        show_history = st.checkbox("Show prediction history", value=False)
        if show_history:
            display_cols = ["symbol", "expiry", "direction", "confidence", "entry_time", "resolved", "correct"]
            st.dataframe(dfp[display_cols], use_container_width=True)

        st.markdown("**Accuracy by Confidence Bucket**")
        if total > 0:
            resolved = resolved.copy()
            resolved["confidence_pct"] = resolved["confidence"] * 100
            buckets = pd.cut(
                resolved["confidence_pct"],
                bins=[50, 60, 70, 80, 90, 100],
                right=False,
                labels=["50-60", "60-70", "70-80", "80-90", "90-100"],
            )
            bucket_stats = resolved.groupby(buckets)["correct"].agg(["count", "mean"]).reset_index()
            bucket_stats["win_rate"] = bucket_stats["mean"] * 100

            fig = go.Figure()
            fig.add_trace(go.Bar(
                x=bucket_stats["confidence_pct"].astype(str),
                y=bucket_stats["win_rate"],
                text=bucket_stats["count"].astype(int),
                textposition="auto",
                name="Win Rate",
            ))
            fig.update_layout(
                height=320,
                yaxis_title="Win Rate (%)",
                xaxis_title="Confidence Bucket",
                margin=dict(l=20, r=20, t=30, b=20),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No resolved predictions yet for bucket stats.")
    else:
        st.info("No predictions yet. Click Predict to start tracking accuracy.")

    st.subheader("Quick Action")
    cbtn1, cbtn2 = st.columns(2)
    with cbtn1:
        st.button("CALL (UP)", type="primary")
    with cbtn2:
        st.button("PUT (DOWN)")

    if st.button("Reset History"):
        st.session_state["predictions"] = []
        st.session_state["last_prediction"] = None
        st.success("History cleared.")

if page == "Professional Signal":
    render_professional_signal()

@st.cache_data(ttl=60)
def forecast_next_signal(df: pd.DataFrame) -> dict:
    data = df.copy()
    features = pd.DataFrame({
        "ret": data["Return"],
        "sma_diff": (data["SMA_Fast"] - data["SMA_Slow"]) / data["Close"],
        "rsi": data["RSI"],
        "macd": data["MACD_Hist"],
        "bb": data["BB_Pct"],
    })
    target = data["Return"].shift(-1)

    model_df = pd.concat([features, target.rename("target")], axis=1).dropna()
    if len(model_df) < 50:
        return {"ok": False, "reason": "Not enough data"}

    train = model_df.tail(200)
    X = train[["ret", "sma_diff", "rsi", "macd", "bb"]].values
    y = train["target"].values

    X = np.column_stack([np.ones(len(X)), X])
    try:
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    except Exception:
        return {"ok": False, "reason": "Model fit failed"}

    last_feat = features.iloc[-1].values.astype(float)
    last_feat = np.insert(last_feat, 0, 1.0)
    pred = float(np.dot(last_feat, coef))
    direction = "BUY" if pred > 0 else "SELL"

    return {"ok": True, "pred_return": pred, "direction": direction}

if page == "Dashboard":
    # Watchlist summary (chart timeframe)
    watch_rows = []
    for t in tickers:
        try:
            df_t = load_data(t, start, end, chart_timeframe)
            if df_t.empty:
                continue
            sig_t = compute_indicators(df_t, int(sma_fast), int(sma_slow))
            latest = sig_t.iloc[-1]
            watch_rows.append({
                "Ticker": t,
                "Price": float(latest["Close"]),
                "Signal": "BUY" if int(latest["Signal"]) == 1 else "SELL",
                "Last Time": str(sig_t.index[-1]),
            })
        except Exception:
            continue

    if watch_rows:
        st.subheader("Watchlist (Chart Timeframe)")
        st.dataframe(pd.DataFrame(watch_rows), use_container_width=True)
    else:
        st.info("No watchlist data. Try different tickers or interval.")

    # Multi-timeframe signals for active ticker
    mt_rows = []
    for tf in timeframes:
        try:
            df_tf = load_data(active_ticker, start, end, tf)
            if df_tf.empty:
                continue
            sig_tf = compute_indicators(df_tf, int(sma_fast), int(sma_slow))
            latest_tf = sig_tf.iloc[-1]
            mt_rows.append({
                "Timeframe": tf,
                "Signal": "BUY" if int(latest_tf["Signal"]) == 1 else "SELL",
                "Price": float(latest_tf["Close"]),
                "Last Time": str(sig_tf.index[-1]),
            })
        except Exception:
            continue

    if mt_rows:
        st.subheader("Multi-Timeframe Signals")
        st.dataframe(pd.DataFrame(mt_rows), use_container_width=True)

    # Active ticker details (chart timeframe)
    try:
        data = load_data(active_ticker, start, end, chart_timeframe)
    except Exception as e:
        st.error(f"Failed to load data: {e}")
        st.stop()

    if data.empty:
        st.warning("No data returned. Try a different ticker/interval or increase lookback.")
        st.stop()

    signal_data = compute_indicators(data, int(sma_fast), int(sma_slow))
    latest = signal_data.iloc[-1]
    latest_signal = "BUY" if int(latest["Signal"]) == 1 else "SELL"
    latest_price = float(latest["Close"])
    latest_time = signal_data.index[-1]

    col1, col2, col3 = st.columns(3)
    col1.metric("Latest Price", f"{latest_price:.2f}")
    col2.metric("Signal", latest_signal)
    col3.metric("Last Time", str(latest_time))

    # Forecast (experimental)
    forecast = forecast_next_signal(signal_data)
    if forecast.get("ok"):
        pred_dir = forecast["direction"]
        pred_ret = forecast["pred_return"] * 100
        st.info(f"Forecast (next bar): {pred_dir} | Predicted return {pred_ret:.3f}%")
    else:
        st.info(f"Forecast: {forecast.get('reason', 'Unavailable')}")

    # Telegram alert every bar while BUY/SELL (alert timeframe)
    if enable_alerts and telegram_token and telegram_chat_id:
        try:
            alert_df = load_data(active_ticker, start, end, alert_timeframe)
            if not alert_df.empty:
                alert_sig = compute_indicators(alert_df, int(sma_fast), int(sma_slow))
                alert_latest = alert_sig.iloc[-1]
                alert_signal = "BUY" if int(alert_latest["Signal"]) == 1 else "SELL"
                alert_price = float(alert_latest["Close"])
                alert_time = str(alert_sig.index[-1])

                forecast_alert = forecast_next_signal(alert_sig)
                if forecast_alert.get("ok"):
                    extra = f" | Forecast next: {forecast_alert['direction']} ({forecast_alert['pred_return']*100:.3f}%)"
                else:
                    extra = ""

                alert_key = f"{active_ticker}|{alert_timeframe}|{alert_time}"
                last_sent = st.session_state.get("last_alert_key")

                if last_sent != alert_key:
                    msg = f"{active_ticker} {alert_timeframe} signal: {alert_signal} at {alert_price:.2f} ({alert_time}){extra}"
                    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
                    payload = {"chat_id": telegram_chat_id, "text": msg}
                    requests.post(url, json=payload, timeout=10)
                    st.session_state["last_alert_key"] = alert_key
                    st.success("Telegram alert sent.")
        except Exception as e:
            st.error(f"Telegram alert failed: {e}")

    # Price chart
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=signal_data.index, y=signal_data["Close"], mode="lines", name="Close"))
    fig.add_trace(go.Scatter(x=signal_data.index, y=signal_data["SMA_Fast"], mode="lines", name=f"SMA {sma_fast}"))
    fig.add_trace(go.Scatter(x=signal_data.index, y=signal_data["SMA_Slow"], mode="lines", name=f"SMA {sma_slow}"))
    fig.add_trace(go.Scatter(
        x=signal_data.index[signal_data["Cross_Up"]],
        y=signal_data.loc[signal_data["Cross_Up"], "Close"],
        mode="markers",
        marker=dict(symbol="triangle-up", size=10),
        name="Cross Up",
    ))
    fig.add_trace(go.Scatter(
        x=signal_data.index[signal_data["Cross_Down"]],
        y=signal_data.loc[signal_data["Cross_Down"], "Close"],
        mode="markers",
        marker=dict(symbol="triangle-down", size=10),
        name="Cross Down",
    ))
    fig.update_layout(height=600, margin=dict(l=20, r=20, t=40, b=20))
    st.plotly_chart(fig, use_container_width=True)

    # Backtest summary
    bt = backtest(signal_data)
    if not bt.empty:
        total_return = (bt["Equity"].iloc[-1] - 1) * 100
        max_drawdown = (bt["Equity"] / bt["Equity"].cummax() - 1).min() * 100
        win_rate = (bt["Strategy_Return"] > 0).mean() * 100

        st.subheader("Backtest Summary")
        s1, s2, s3 = st.columns(3)
        s1.metric("Total Return", f"{total_return:.2f}%")
        s2.metric("Max Drawdown", f"{max_drawdown:.2f}%")
        s3.metric("Win Rate", f"{win_rate:.2f}%")

        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=bt.index, y=bt["Equity"], mode="lines", name="Equity"))
        fig2.update_layout(height=300, margin=dict(l=20, r=20, t=40, b=20))
        st.plotly_chart(fig2, use_container_width=True)

    if refresh:
        st.info("Auto-refresh is enabled (60s).")
        st.autorefresh(interval=60000, key="refresh")
