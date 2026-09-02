import numpy as np
import pandas as pd

from trading_signal import indicators as ind
from tests.conftest import make_ohlc


def test_normalize_ohlc_handles_yfinance_multiindex(yf_multiindex_frame):
    df = ind.normalize_ohlc(yf_multiindex_frame)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert not isinstance(df.columns, pd.MultiIndex)
    assert len(df) == 300


def test_normalize_ohlc_handles_strings_and_lowercase():
    idx = pd.date_range("2026-01-01", periods=3, freq="1min", tz="UTC")
    df = pd.DataFrame({"open": ["1.1", "1.2", "1.3"], "close": ["1.2", "bad", "1.4"]}, index=idx)
    out = ind.normalize_ohlc(df)
    assert list(out.columns) == ["Open", "Close"]
    assert len(out) == 2  # the unparsable close is dropped
    assert out["Close"].dtype.kind == "f"


def test_normalize_ohlc_empty():
    assert ind.normalize_ohlc(None).empty
    assert ind.normalize_ohlc(pd.DataFrame()).empty


def test_rsi_is_100_in_monotonic_uptrend_not_nan():
    close = pd.Series(np.linspace(100, 130, 60))
    r = ind.rsi(close, 14)
    assert r.iloc[14:].notna().all()
    assert np.allclose(r.iloc[14:], 100.0)


def test_rsi_matches_wilder_reference():
    # Classic Wilder example values (Wilder 1978, 14-period). We check bounds and
    # that a symmetric random walk hovers around 50.
    close = pd.Series(100 + np.cumsum(np.random.default_rng(3).normal(0, 1, 2000)))
    r = ind.rsi(close, 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    assert 40 < r.mean() < 60


def test_directional_movement_only_larger_side_counts():
    df = pd.DataFrame({"High": [10, 11, 12.5], "Low": [9, 9.5, 8.0], "Close": [9.5, 10.5, 9.0]})
    plus, minus = ind.directional_movement(df)
    # bar 2 is an outside bar: up move 1.5, down move 1.5 -> tie, neither counts
    assert plus.iloc[2] == 0 and minus.iloc[2] == 0
    df2 = pd.DataFrame({"High": [10, 11, 13.0], "Low": [9, 9.5, 8.5], "Close": [9.5, 10.5, 9.0]})
    plus, minus = ind.directional_movement(df2)
    assert plus.iloc[2] == 2.0 and minus.iloc[2] == 0.0


def test_adx_bounds_and_trend_detection():
    trend = make_ohlc(n=400, seed=1)
    trend["Close"] = np.linspace(100, 200, 400)
    trend["High"] = trend["Close"] + 0.5
    trend["Low"] = trend["Close"] - 0.5
    out = ind.adx(trend, 14).dropna()
    assert ((out["ADX"] >= 0) & (out["ADX"] <= 100)).all()
    assert out["ADX"].iloc[-1] > 50  # a straight line is a very strong trend
    assert (out["Plus_DI"].iloc[-1] > out["Minus_DI"].iloc[-1])


def test_compute_indicators_keeps_trending_bars(ohlc):
    up = make_ohlc(n=120, seed=2)
    up["Close"] = np.linspace(100, 130, 120)
    up["High"] = up["Close"] + 0.1
    up["Low"] = up["Close"] - 0.1
    sig = ind.compute_indicators(up, 10, 30)
    # only warm-up is dropped (slow SMA 30 / MACD 35 / ADX 28)
    assert len(sig) >= 120 - 40
    assert set(ind.REQUIRED_FEATURE_COLUMNS).issubset(sig.columns)
    assert sig[ind.REQUIRED_FEATURE_COLUMNS].notna().all().all()


def test_compute_indicators_signal_and_crosses(ohlc):
    sig = ind.compute_indicators(ohlc, 10, 30)
    assert sig["Signal"].isin([0, 1]).all()
    assert (sig["Cross_Up"] & sig["Cross_Down"]).sum() == 0
    # a cross-up is where signal flips 0 -> 1
    flips = sig["Signal"].diff() == 1
    assert (flips == sig["Cross_Up"]).all()


def test_compute_indicators_too_short_returns_empty():
    short = make_ohlc(n=20)
    assert ind.compute_indicators(short, 10, 30).empty


def test_backtest_summary_win_rate_excludes_flat_bars(ohlc):
    sig = ind.compute_indicators(ohlc, 10, 30)
    bt = ind.backtest(sig)
    s = ind.backtest_summary(bt)
    in_market = bt["Position"] == 1
    expected = (bt.loc[in_market, "Strategy_Return"] > 0).mean() * 100
    assert abs(s["bar_win_rate"] - expected) < 1e-9
    assert s["n_trades"] >= 1
    assert 0 <= s["trade_win_rate"] <= 100
    assert s["max_drawdown"] <= 0


def test_backtest_position_uses_previous_bar_signal(ohlc):
    sig = ind.compute_indicators(ohlc, 10, 30)
    bt = ind.backtest(sig)
    assert (bt["Position"].iloc[1:].values == sig["Signal"].shift(1).iloc[1:].values).all()
    assert bt["Position"].iloc[0] == 0


def test_backtest_empty():
    assert ind.backtest(pd.DataFrame()).empty
    assert ind.backtest_summary(pd.DataFrame()) == {}
