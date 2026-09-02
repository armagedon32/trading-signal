"""Technical indicators and the SMA-crossover backtest.

All functions are pure pandas/numpy. Indicator maths follows the conventional
(Wilder / TradingView) definitions so values line up with what traders see on
their charting platform.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# Columns that must be non-NaN for a bar to be usable by the signal/model code.
REQUIRED_FEATURE_COLUMNS = ["SMA_Fast", "SMA_Slow", "RSI", "MACD_Hist", "BB_Pct", "Return"]


# --------------------------------------------------------------------------- #
# Frame normalisation
# --------------------------------------------------------------------------- #
def normalize_ohlc(df: pd.DataFrame | None) -> pd.DataFrame:
    """Return a clean single-level OHLC(V) frame sorted by a DatetimeIndex.

    Handles the MultiIndex columns that ``yfinance.download`` returns for a single
    ticker, mixed-case column names, string prices (Twelve Data) and duplicate
    timestamps. Rows without a Close price are dropped.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])

    data = df.copy()

    if isinstance(data.columns, pd.MultiIndex):
        level = None
        for lvl in range(data.columns.nlevels):
            values = {str(v).strip().title() for v in data.columns.get_level_values(lvl)}
            if "Close" in values:
                level = lvl
                break
        data.columns = data.columns.get_level_values(level if level is not None else 0)

    data.columns = [str(c).strip().title() for c in data.columns]
    data = data.loc[:, ~pd.Index(data.columns).duplicated()]

    keep = [c for c in OHLCV_COLUMNS if c in data.columns]
    if "Close" not in keep:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close"])
    data = data[keep]

    for col in keep:
        data[col] = pd.to_numeric(data[col], errors="coerce")

    if not isinstance(data.index, pd.DatetimeIndex):
        data.index = pd.to_datetime(data.index, errors="coerce")
        data = data[~data.index.isna()]

    data = data.dropna(subset=["Close"])
    data = data[~data.index.duplicated(keep="last")].sort_index()
    return data


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #
def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(int(window)).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=int(span), adjust=False).mean()


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (a.k.a. RMA / SMMA).

    Seeded with the simple mean of the first ``period`` observations, then
    ``rma_t = (rma_{t-1} * (period - 1) + x_t) / period``. Leading rows are NaN.
    """
    period = int(period)
    x = series.astype(float).dropna()
    out = pd.Series(np.nan, index=series.index, dtype=float)
    if period <= 0 or len(x) < period:
        return out
    seed = x.iloc[:period].mean()
    seeded = pd.concat([pd.Series([seed], index=[x.index[period - 1]]), x.iloc[period:]])
    smoothed = seeded.ewm(alpha=1.0 / period, adjust=False).mean()
    out.loc[smoothed.index] = smoothed.values
    return out


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI. Returns 100 when there were no losses in the window (never NaN
    after warm-up), matching TradingView's ``ta.rsi``."""
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss != 0, 100.0)  # no losses at all -> RSI 100
    out[avg_gain.isna() | avg_loss.isna()] = np.nan  # keep warm-up as NaN
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    ranges = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1, skipna=True)


def directional_movement(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Wilder +DM / -DM: only the larger of the two moves counts on a given bar."""
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    plus_dm[up.isna()] = np.nan
    minus_dm[down.isna()] = np.nan
    return plus_dm, minus_dm


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder ADX. Returns a frame with ``Plus_DI``, ``Minus_DI`` and ``ADX``."""
    plus_dm, minus_dm = directional_movement(df)
    atr = rma(true_range(df), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * rma(plus_dm, period) / atr
        minus_di = 100.0 * rma(minus_dm, period) / atr
        denom = plus_di + minus_di
        dx = 100.0 * (plus_di - minus_di).abs() / denom
    dx = dx.where(denom != 0, 0.0)
    dx[plus_di.isna() | minus_di.isna()] = np.nan
    result = pd.DataFrame({"Plus_DI": plus_di, "Minus_DI": minus_di, "ADX": rma(dx, period)})
    return result


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=int(signal), adjust=False).mean()
    return pd.DataFrame({"MACD": line, "MACD_Signal": sig, "MACD_Hist": line - sig})


def bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(int(window)).mean()
    dev = close.rolling(int(window)).std()
    upper = mid + num_std * dev
    lower = mid - num_std * dev
    band = upper - lower
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = (close - lower) / band
    pct = pct.where(band != 0, 0.5)  # perfectly flat window -> middle of the band
    pct[mid.isna()] = np.nan
    return pd.DataFrame({"BB_Mid": mid, "BB_Upper": upper, "BB_Lower": lower, "BB_Pct": pct})


# --------------------------------------------------------------------------- #
# Composite
# --------------------------------------------------------------------------- #
def min_bars_required(fast: int, slow: int, rsi_period: int = 14, bb_window: int = 20) -> int:
    """Rough number of bars needed before ``compute_indicators`` yields any rows."""
    return int(max(slow, bb_window, rsi_period + 1, 26 + 9)) + 1


def compute_indicators(
    df: pd.DataFrame,
    fast: int,
    slow: int,
    rsi_period: int = 14,
    bb_window: int = 20,
    bb_std: float = 2.0,
) -> pd.DataFrame:
    """Add SMA crossover signal, RSI, MACD, Bollinger %B, ADX and returns.

    Only indicator warm-up rows are dropped; indicators never produce NaN
    mid-series, so a strong trend no longer erases bars from the chart/backtest.
    """
    data = normalize_ohlc(df)
    if data.empty:
        return data

    close = data["Close"].astype(float)
    fast, slow = int(fast), int(slow)

    data["SMA_Fast"] = sma(close, fast)
    data["SMA_Slow"] = sma(close, slow)

    valid = data["SMA_Fast"].notna() & data["SMA_Slow"].notna()
    signal = pd.Series(np.where(data["SMA_Fast"] > data["SMA_Slow"], 1.0, 0.0), index=data.index)
    signal = signal.where(valid)  # NaN during warm-up so we don't fake a crossover
    data["Signal"] = signal
    data["Cross_Up"] = signal.diff() == 1
    data["Cross_Down"] = signal.diff() == -1

    data["RSI"] = rsi(close, rsi_period)
    data = data.join(macd(close))
    data = data.join(bollinger(close, bb_window, bb_std))
    data["Return"] = close.pct_change()

    if {"High", "Low"}.issubset(data.columns):
        data = data.join(adx(data, 14))
    else:
        data["Plus_DI"] = np.nan
        data["Minus_DI"] = np.nan
        data["ADX"] = np.nan

    data = data.dropna(subset=REQUIRED_FEATURE_COLUMNS)
    if not data.empty:
        data["Signal"] = data["Signal"].astype(int)
    return data


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
def backtest(df: pd.DataFrame) -> pd.DataFrame:
    """Long-only: hold while ``Signal`` == 1, acting on the *next* bar."""
    data = df.copy()
    if data.empty or "Signal" not in data.columns:
        return pd.DataFrame()
    data["Return"] = data["Close"].pct_change().fillna(0.0)
    data["Position"] = data["Signal"].shift(1).fillna(0).astype(int)
    data["Strategy_Return"] = data["Position"] * data["Return"]
    data["Equity"] = (1.0 + data["Strategy_Return"]).cumprod()
    data["BuyHold_Equity"] = (1.0 + data["Return"]).cumprod()
    return data


def backtest_summary(bt: pd.DataFrame) -> dict:
    """Per-trade and per-bar statistics for a frame produced by :func:`backtest`."""
    if bt is None or bt.empty:
        return {}

    equity = bt["Equity"]
    in_market = bt["Position"] == 1
    total_return = float(equity.iloc[-1] - 1.0) * 100
    buy_hold_return = float(bt["BuyHold_Equity"].iloc[-1] - 1.0) * 100
    max_drawdown = float((equity / equity.cummax() - 1.0).min()) * 100

    bar_returns = bt.loc[in_market, "Strategy_Return"]
    bar_win_rate = float((bar_returns > 0).mean() * 100) if len(bar_returns) else 0.0

    entries = (bt["Position"].diff().fillna(bt["Position"]) == 1)
    trade_id = entries.cumsum()[in_market]
    trade_returns = (
        bt.loc[in_market, "Return"].groupby(trade_id).apply(lambda r: float((1.0 + r).prod() - 1.0))
        if in_market.any()
        else pd.Series(dtype=float)
    )
    n_trades = int(len(trade_returns))
    trade_win_rate = float((trade_returns > 0).mean() * 100) if n_trades else 0.0
    avg_trade_return = float(trade_returns.mean() * 100) if n_trades else 0.0

    return {
        "total_return": total_return,
        "buy_hold_return": buy_hold_return,
        "max_drawdown": max_drawdown,
        "bar_win_rate": bar_win_rate,
        "bars_in_market": int(in_market.sum()),
        "exposure": float(in_market.mean() * 100),
        "n_trades": n_trades,
        "trade_win_rate": trade_win_rate,
        "avg_trade_return": avg_trade_return,
    }
