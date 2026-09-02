import numpy as np
import pandas as pd
import pytest

from trading_signal import models
from trading_signal.indicators import compute_indicators
from tests.conftest import make_ohlc


@pytest.fixture
def sig():
    return compute_indicators(make_ohlc(n=900, seed=0), 10, 30)


def test_training_frame_excludes_bar_being_predicted(sig):
    frame = models.build_training_frame(sig)
    assert frame.index[-1] != sig.index[-1]
    assert frame.index[-1] == sig.index[-2]
    # target must equal the sign of the *next* bar's return
    nxt = sig["Return"].shift(-1).loc[frame.index]
    assert ((nxt > 0).astype(int) == frame["target"]).all()


def test_latest_feature_row_shape(sig):
    x = models.latest_feature_row(sig)
    assert x.shape == (1, len(models.FEATURE_COLUMNS))
    assert np.isfinite(x).all()


@pytest.mark.parametrize("model_type", ["ML Lite", "ML Advanced", "Trend"])
def test_predict_direction_runs(sig, model_type):
    pred = models.predict_direction(sig, model_type)
    assert pred.ok, pred.reason
    assert pred.direction in ("UP", "DOWN")
    assert 0.5 <= pred.confidence <= 1.0
    assert pred.label in ("UP (Call)", "DOWN (Put)")
    if model_type != "Trend":
        assert pred.oos_accuracy is not None
        assert 0.0 <= pred.oos_accuracy <= 1.0
        assert pred.oos_samples > 0


def test_predict_direction_not_enough_data():
    sig = compute_indicators(make_ohlc(n=150), 10, 30)
    pred = models.predict_direction(sig, "ML Lite")
    assert not pred.ok
    assert "Not enough" in pred.reason


def test_predict_direction_empty():
    assert not models.predict_direction(pd.DataFrame(), "ML Lite").ok
    assert not models.predict_direction(None, "Trend").ok


def test_trend_prediction_direction_follows_ema_spread():
    up = make_ohlc(n=300, seed=5)
    up["Close"] = np.linspace(100, 150, 300)
    up["High"] = up["Close"] + 0.3
    up["Low"] = up["Close"] - 0.3
    pred = models.predict_direction(compute_indicators(up, 10, 30), "Trend")
    assert pred.ok and pred.direction == "UP"
    assert pred.confidence == 0.95  # straight line -> ADX saturates
    assert pred.extras["di_agrees"] is True


def test_linear_forecast(sig):
    fc = models.linear_forecast(sig)
    assert fc.ok
    assert fc.pred_return is not None
    assert fc.direction == ("UP" if fc.pred_return > 0 else "DOWN")
    assert fc.oos_accuracy is not None


def test_linear_forecast_short():
    fc = models.linear_forecast(compute_indicators(make_ohlc(n=70), 10, 30))
    assert not fc.ok


def test_ml_confidence_on_random_walk_is_not_extreme():
    """Regularised models should not claim ~90% certainty on pure noise."""
    confs = []
    for seed in range(4):
        s = compute_indicators(make_ohlc(n=900, seed=100 + seed), 10, 30)
        p = models.predict_direction(s, "ML Advanced")
        assert p.ok
        confs.append(p.confidence)
    assert max(confs) < 0.85
    assert np.mean(confs) < 0.75


def test_prediction_to_dict_roundtrip(sig):
    pred = models.predict_direction(sig, "ML Lite")
    d = pred.to_dict()
    assert d["direction"] == pred.direction
    assert d["model"].startswith("ML Lite")
