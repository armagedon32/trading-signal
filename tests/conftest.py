import numpy as np
import pandas as pd
import pytest


def make_ohlc(n: int = 900, seed: int = 0, freq: str = "5min", start_price: float = 100.0, tz: str = "UTC") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-08-01", periods=n, freq=freq, tz=tz)
    close = start_price + np.cumsum(rng.normal(0, 0.2, n))
    high = close + np.abs(rng.normal(0, 0.1, n))
    low = close - np.abs(rng.normal(0, 0.1, n))
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": 1000.0}, index=idx)


@pytest.fixture
def ohlc() -> pd.DataFrame:
    return make_ohlc()


@pytest.fixture
def yf_multiindex_frame() -> pd.DataFrame:
    """Shape returned by yfinance.download() for a single ticker."""
    df = make_ohlc(n=300, tz="America/New_York")
    df.columns = pd.MultiIndex.from_product([df.columns, ["AAPL"]], names=["Price", "Ticker"])
    return df
