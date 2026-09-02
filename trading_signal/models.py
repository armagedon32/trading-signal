"""Direction models used by both pages.

Design rules
------------
* One feature builder (:func:`build_features`) shared by every model.
* The bar being predicted is **never** part of the training set (the original
  code converted its missing label to ``0`` before dropping NaNs).
* Every learned model reports a walk-forward (time-ordered, out-of-sample)
  hit-rate next to its in-sample probability so the "confidence" number has a
  reality check beside it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .indicators import ema

FEATURE_COLUMNS = ["ret", "sma_diff", "rsi", "macd", "bb"]

MIN_ROWS_ML = 200
MIN_ROWS_LINEAR = 50
TRAIN_WINDOW_ML = 500
TRAIN_WINDOW_LINEAR = 200
WALK_FORWARD_SPLITS = 3

UP = "UP"
DOWN = "DOWN"


@dataclass
class Prediction:
    ok: bool
    direction: str = ""  # "UP" or "DOWN"
    prob_up: float = float("nan")
    confidence: float = float("nan")  # max(prob_up, 1 - prob_up) for ML; ADX-based for Trend
    model: str = ""
    train_samples: int = 0
    oos_accuracy: Optional[float] = None  # walk-forward hit-rate in [0, 1]
    oos_samples: int = 0
    pred_return: Optional[float] = None  # only for the linear forecast
    reason: str = ""
    extras: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        if not self.ok:
            return "N/A"
        return "UP (Call)" if self.direction == UP else "DOWN (Put)"

    @property
    def is_up(self) -> bool:
        return self.direction == UP

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "direction": self.direction,
            "prob_up": self.prob_up,
            "confidence": self.confidence,
            "model": self.model,
            "train_samples": self.train_samples,
            "oos_accuracy": self.oos_accuracy,
            "oos_samples": self.oos_samples,
            "pred_return": self.pred_return,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# Features / targets
# --------------------------------------------------------------------------- #
def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Model inputs. Price-denominated indicators are normalised by Close so the
    same model definition works for EUR/USD (~1.1) and BTC/USD (~60000)."""
    close = df["Close"].astype(float)
    return pd.DataFrame(
        {
            "ret": df["Return"].astype(float),
            "sma_diff": (df["SMA_Fast"] - df["SMA_Slow"]) / close,
            "rsi": df["RSI"].astype(float) / 100.0,
            "macd": df["MACD_Hist"].astype(float) / close,
            "bb": df["BB_Pct"].astype(float),
        },
        index=df.index,
    )


def build_training_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Features + ``future_return`` + binary ``target`` for every bar that has a
    *known* next-bar return. The final bar (unknown future) is excluded, which
    is the leak the original implementation had."""
    feats = build_features(df)
    frame = feats.copy()
    frame["future_return"] = df["Return"].astype(float).shift(-1)
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    frame["target"] = (frame["future_return"] > 0).astype(int)
    return frame


def latest_feature_row(df: pd.DataFrame) -> Optional[np.ndarray]:
    feats = build_features(df).replace([np.inf, -np.inf], np.nan)
    if feats.empty or feats.iloc[-1].isna().any():
        return None
    return feats.iloc[-1].to_numpy(dtype=float).reshape(1, -1)


def latest_factors(df: pd.DataFrame) -> dict:
    """Human-readable indicator snapshot for the last bar (for display only)."""
    last = df.iloc[-1]
    close = float(last["Close"])
    return {
        "Return %": float(last["Return"]) * 100,
        "SMA diff %": float((last["SMA_Fast"] - last["SMA_Slow"]) / close) * 100,
        "RSI": float(last["RSI"]),
        "MACD hist": float(last["MACD_Hist"]),
        "BB %B": float(last["BB_Pct"]),
        "ADX": float(last["ADX"]) if "ADX" in df.columns and not pd.isna(last["ADX"]) else float("nan"),
    }


# --------------------------------------------------------------------------- #
# Estimators
# --------------------------------------------------------------------------- #
def _make_estimator(model_type: str):
    """Return ``(estimator, resolved_model_name)``."""
    if model_type == "ML Advanced":
        try:
            import xgboost as xgb  # optional dependency

            est = xgb.XGBClassifier(
                n_estimators=150,
                max_depth=3,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_weight=5,
                reg_lambda=1.0,
                eval_metric="logloss",
                tree_method="hist",
                n_jobs=1,
                random_state=42,
                verbosity=0,
            )
            return est, "ML Advanced (XGBoost)"
        except Exception:
            est = RandomForestClassifier(
                n_estimators=200,
                max_depth=4,
                min_samples_leaf=10,
                random_state=42,
                n_jobs=1,
            )
            return est, "ML Advanced (RandomForest)"

    est = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, C=0.5))
    return est, "ML Lite (Logistic)"


def walk_forward_accuracy(estimator_factory, X: np.ndarray, y: np.ndarray, n_splits: int = WALK_FORWARD_SPLITS):
    """Expanding-window, time-ordered evaluation. Returns ``(accuracy, n_test)``
    or ``(None, 0)`` when it can't be computed."""
    if len(X) < (n_splits + 1) * 20:
        return None, 0
    tss = TimeSeriesSplit(n_splits=n_splits)
    hits = 0
    total = 0
    for train_idx, test_idx in tss.split(X):
        y_train = y[train_idx]
        if len(np.unique(y_train)) < 2:
            continue
        est = estimator_factory()
        try:
            est.fit(X[train_idx], y_train)
            pred = est.predict(X[test_idx])
        except Exception:
            continue
        hits += int((pred == y[test_idx]).sum())
        total += int(len(test_idx))
    if total == 0:
        return None, 0
    return hits / total, total


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def predict_direction(df: pd.DataFrame, model_type: str = "ML Lite") -> Prediction:
    """Next-bar direction for the last row of an indicator frame."""
    if df is None or df.empty:
        return Prediction(ok=False, reason="No data", model=model_type)

    if model_type == "Trend":
        return trend_prediction(df)

    frame = build_training_frame(df)
    if len(frame) < MIN_ROWS_ML:
        return Prediction(
            ok=False,
            reason=f"Not enough history for the ML model ({len(frame)} usable bars, need {MIN_ROWS_ML}).",
            model=model_type,
        )

    x_last = latest_feature_row(df)
    if x_last is None:
        return Prediction(ok=False, reason="Latest bar has incomplete indicators.", model=model_type)

    train = frame.tail(TRAIN_WINDOW_ML)
    X = train[FEATURE_COLUMNS].to_numpy(dtype=float)
    y = train["target"].to_numpy(dtype=int)
    if len(np.unique(y)) < 2:
        return Prediction(ok=False, reason="Training window has only one class.", model=model_type)

    factory = lambda: _make_estimator(model_type)[0]  # noqa: E731
    oos_acc, oos_n = walk_forward_accuracy(factory, X, y)

    est, name = _make_estimator(model_type)
    try:
        est.fit(X, y)
        prob_up = float(est.predict_proba(x_last)[0][1])
    except Exception as exc:
        return Prediction(ok=False, reason=f"Model fit failed: {exc}", model=name)

    prob_up = min(1.0, max(0.0, prob_up))
    direction = UP if prob_up >= 0.5 else DOWN
    confidence = prob_up if direction == UP else 1.0 - prob_up
    return Prediction(
        ok=True,
        direction=direction,
        prob_up=prob_up,
        confidence=confidence,
        model=name,
        train_samples=int(len(train)),
        oos_accuracy=oos_acc,
        oos_samples=oos_n,
    )


def trend_prediction(df: pd.DataFrame) -> Prediction:
    """Rule-based: direction from the EMA(12)-EMA(26) spread, conviction from
    Wilder ADX (``ADX/50`` clipped to [0.5, 0.95])."""
    close = df["Close"].astype(float)
    spread = float((ema(close, 12) - ema(close, 26)).iloc[-1])
    adx_val = df["ADX"].iloc[-1] if "ADX" in df.columns else float("nan")
    if adx_val is None or (isinstance(adx_val, float) and math.isnan(adx_val)) or pd.isna(adx_val):
        return Prediction(ok=False, reason="Not enough data for the trend model (ADX warm-up).", model="Trend")

    plus_di = float(df["Plus_DI"].iloc[-1]) if "Plus_DI" in df.columns else float("nan")
    minus_di = float(df["Minus_DI"].iloc[-1]) if "Minus_DI" in df.columns else float("nan")
    direction = UP if spread > 0 else DOWN
    confidence = min(0.95, max(0.5, float(adx_val) / 50.0))
    di_agrees = None
    if not (math.isnan(plus_di) or math.isnan(minus_di)):
        di_agrees = (plus_di > minus_di) == (direction == UP)

    return Prediction(
        ok=True,
        direction=direction,
        prob_up=float("nan"),
        confidence=confidence,
        model="Trend (EMA slope + ADX)",
        train_samples=int(len(df)),
        extras={"adx": float(adx_val), "ema_spread": spread, "di_agrees": di_agrees},
    )


def linear_forecast(df: pd.DataFrame) -> Prediction:
    """Least-squares forecast of the next bar's return (Dashboard "Forecast")."""
    if df is None or df.empty:
        return Prediction(ok=False, reason="No data", model="Linear")

    frame = build_training_frame(df)
    if len(frame) < MIN_ROWS_LINEAR:
        return Prediction(ok=False, reason=f"Not enough data ({len(frame)} bars, need {MIN_ROWS_LINEAR}).", model="Linear")

    x_last = latest_feature_row(df)
    if x_last is None:
        return Prediction(ok=False, reason="Latest bar has incomplete indicators.", model="Linear")

    train = frame.tail(TRAIN_WINDOW_LINEAR)
    X = train[FEATURE_COLUMNS].to_numpy(dtype=float)
    y = train["future_return"].to_numpy(dtype=float)

    def fit_predict(X_tr, y_tr, X_te):
        A = np.column_stack([np.ones(len(X_tr)), X_tr])
        coef, *_ = np.linalg.lstsq(A, y_tr, rcond=None)
        return np.column_stack([np.ones(len(X_te)), X_te]) @ coef

    # walk-forward hit-rate on the sign of the return
    oos_acc, oos_n = None, 0
    if len(X) >= 80:
        hits = total = 0
        for tr_idx, te_idx in TimeSeriesSplit(n_splits=WALK_FORWARD_SPLITS).split(X):
            try:
                pred = fit_predict(X[tr_idx], y[tr_idx], X[te_idx])
            except np.linalg.LinAlgError:
                continue
            hits += int((np.sign(pred) == np.sign(y[te_idx])).sum())
            total += len(te_idx)
        if total:
            oos_acc, oos_n = hits / total, total

    try:
        pred = float(fit_predict(X, y, x_last)[0])
    except np.linalg.LinAlgError:
        return Prediction(ok=False, reason="Model fit failed.", model="Linear")

    direction = UP if pred > 0 else DOWN
    return Prediction(
        ok=True,
        direction=direction,
        prob_up=float("nan"),
        confidence=float("nan"),
        model="Linear (OLS)",
        train_samples=int(len(train)),
        oos_accuracy=oos_acc,
        oos_samples=oos_n,
        pred_return=pred,
    )
