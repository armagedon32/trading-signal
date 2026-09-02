"""Prediction / trade tracker.

A record is created when the user asks for a prediction (or logs a manual CALL
/ PUT). It is resolved later against the bar that *contains* its expiry time,
so the outcome is measured at expiry rather than "whenever the page happened to
rerun" and never against the entry bar itself.

Records are plain dicts so they can live in ``st.session_state`` and be
serialised to JSON. Timestamps are timezone-aware UTC ``datetime`` objects in
memory and ISO-8601 strings on disk.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

import pandas as pd

from .config import expiry_to_seconds, expiry_to_timedelta

OUTCOME_WIN = "WIN"
OUTCOME_LOSS = "LOSS"
OUTCOME_TIE = "TIE"

# How long after expiry we keep waiting for a bar that contains the expiry
# instant before settling on the last available bar (e.g. market closed).
SETTLE_GRACE_BARS = 3

# Data older than this many bars at click time is flagged as stale.
STALE_BARS = 2


def _utc(dt) -> datetime:
    """Coerce to an aware UTC ``datetime``."""
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def new_record(
    symbol: str,
    expiry: str,
    direction: str,
    entry_price: float,
    entry_bar_time,
    now: Optional[datetime] = None,
    confidence: Optional[float] = None,
    model: str = "",
    source: str = "model",
    oos_accuracy: Optional[float] = None,
) -> dict:
    """Create a pending record. ``entry_time`` is the moment of the click, which
    is when a binary-option position would actually open."""
    now = _utc(now or datetime.now(timezone.utc))
    entry_bar_time = _utc(entry_bar_time)
    bar_seconds = expiry_to_seconds(expiry)
    lag = (now - entry_bar_time).total_seconds()
    return {
        "id": uuid.uuid4().hex[:10],
        "symbol": symbol,
        "expiry": expiry,
        "direction": "UP" if str(direction).upper().startswith("UP") else "DOWN",
        "confidence": None if confidence is None else float(confidence),
        "model": model,
        "source": source,
        "oos_accuracy": None if oos_accuracy is None else float(oos_accuracy),
        "entry_time": now,
        "entry_bar_time": entry_bar_time,
        "entry_price": float(entry_price),
        "resolve_time": now + expiry_to_timedelta(expiry),
        "data_lag_seconds": float(lag),
        "stale_entry": lag > STALE_BARS * bar_seconds,
        "resolved": False,
        "resolved_at": None,
        "exit_bar_time": None,
        "exit_price": None,
        "outcome": None,  # WIN / LOSS / TIE
        "correct": None,  # True / False / None (tie or pending)
        "note": "",
    }


def seconds_remaining(record: dict, now: Optional[datetime] = None) -> int:
    now = _utc(now or datetime.now(timezone.utc))
    return max(0, int((_utc(record["resolve_time"]) - now).total_seconds()))


def _locate_exit_bar(bars: pd.DataFrame, resolve_time: datetime, bar_seconds: int, now: datetime):
    """Return ``(bar_time, close, note)`` or ``None`` if we should keep waiting."""
    if bars is None or bars.empty:
        return None
    idx = bars.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")

    before = idx <= resolve_time
    if not before.any():
        return None
    pos = int(before.nonzero()[0][-1])
    bar_time = idx[pos].to_pydatetime()
    bar_end = bar_time + timedelta(seconds=bar_seconds)

    if resolve_time < bar_end:
        # The bar containing the expiry instant. If it is still forming, its
        # current close is the live price at (or just after) expiry, which is
        # exactly what we want; if it is complete, the error is at most one bar.
        return bar_time, float(bars["Close"].iloc[pos]), ""

    # No bar covers the expiry (feed gap or market closed). After a grace
    # period settle on the last bar before expiry instead of waiting forever.
    if now >= resolve_time + timedelta(seconds=SETTLE_GRACE_BARS * bar_seconds):
        return bar_time, float(bars["Close"].iloc[pos]), "settled on last available bar (market closed / feed gap)"
    return None


def resolve_pending(
    records: Iterable[dict],
    fetch_bars: Callable[[str, str], Optional[pd.DataFrame]],
    now: Optional[datetime] = None,
) -> tuple[list[dict], int]:
    """Resolve every pending record whose expiry has passed.

    ``fetch_bars(symbol, expiry)`` must return a UTC-indexed OHLC frame (or None).
    Returns ``(records, number_resolved)``. Records are mutated in place.
    """
    now = _utc(now or datetime.now(timezone.utc))
    records = list(records)
    resolved_count = 0
    cache: dict[tuple[str, str], Optional[pd.DataFrame]] = {}

    for rec in records:
        if rec.get("resolved"):
            continue
        resolve_time = _utc(rec["resolve_time"])
        if now < resolve_time:
            continue

        key = (rec["symbol"], rec["expiry"])
        if key not in cache:
            try:
                cache[key] = fetch_bars(*key)
            except Exception:  # keep the record pending; try again next run
                cache[key] = None
        bars = cache[key]
        if bars is None or bars.empty:
            continue

        located = _locate_exit_bar(bars, resolve_time, expiry_to_seconds(rec["expiry"]), now)
        if located is None:
            continue
        bar_time, exit_price, note = located

        entry_price = float(rec["entry_price"])
        if exit_price > entry_price:
            outcome = OUTCOME_WIN if rec["direction"] == "UP" else OUTCOME_LOSS
        elif exit_price < entry_price:
            outcome = OUTCOME_WIN if rec["direction"] == "DOWN" else OUTCOME_LOSS
        else:
            outcome = OUTCOME_TIE

        rec.update(
            resolved=True,
            resolved_at=now,
            exit_bar_time=bar_time,
            exit_price=exit_price,
            outcome=outcome,
            correct=None if outcome == OUTCOME_TIE else outcome == OUTCOME_WIN,
            note=note,
        )
        resolved_count += 1

    return records, resolved_count


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def summarize(records: Iterable[dict], source: Optional[str] = None) -> dict:
    recs = [r for r in records if source is None or r.get("source") == source]
    resolved = [r for r in recs if r.get("resolved")]
    wins = sum(1 for r in resolved if r.get("outcome") == OUTCOME_WIN)
    losses = sum(1 for r in resolved if r.get("outcome") == OUTCOME_LOSS)
    ties = sum(1 for r in resolved if r.get("outcome") == OUTCOME_TIE)
    decided = wins + losses
    return {
        "total": len(recs),
        "pending": len(recs) - len(resolved),
        "resolved": len(resolved),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate": (wins / decided * 100.0) if decided else None,
    }


def confidence_buckets(records: Iterable[dict]) -> pd.DataFrame:
    """Win-rate per confidence bucket for resolved, non-tie model predictions."""
    rows = [
        r
        for r in records
        if r.get("resolved") and r.get("outcome") in (OUTCOME_WIN, OUTCOME_LOSS) and r.get("confidence") is not None
    ]
    if not rows:
        return pd.DataFrame(columns=["bucket", "count", "win_rate"])
    df = pd.DataFrame(rows)
    pct = df["confidence"].astype(float) * 100
    labels = ["50-60", "60-70", "70-80", "80-90", "90-100"]
    buckets = pd.cut(pct.clip(50, 99.999), bins=[50, 60, 70, 80, 90, 100], right=False, labels=labels)
    wins = (df["outcome"] == OUTCOME_WIN).astype(float)
    stats = wins.groupby(buckets, observed=False).agg(["count", "mean"]).reset_index()
    stats.columns = ["bucket", "count", "win_rate"]
    stats["bucket"] = stats["bucket"].astype(str)
    stats["count"] = stats["count"].astype(int)
    stats["win_rate"] = stats["win_rate"] * 100.0
    return stats


def to_frame(records: Iterable[dict]) -> pd.DataFrame:
    cols = [
        "entry_time",
        "symbol",
        "expiry",
        "source",
        "model",
        "direction",
        "confidence",
        "oos_accuracy",
        "entry_price",
        "exit_price",
        "outcome",
        "resolve_time",
        "note",
    ]
    recs = list(records)
    if not recs:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(recs)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols].sort_values("entry_time", ascending=False)
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Persistence (optional, local JSON file)
# --------------------------------------------------------------------------- #
_DATETIME_FIELDS = ("entry_time", "entry_bar_time", "resolve_time", "resolved_at", "exit_bar_time")


def _serialise(rec: dict) -> dict:
    out = dict(rec)
    for f in _DATETIME_FIELDS:
        v = out.get(f)
        if v is not None:
            out[f] = _utc(v).isoformat()
    return out


def _deserialise(rec: dict) -> dict:
    out = dict(rec)
    for f in _DATETIME_FIELDS:
        v = out.get(f)
        if v:
            out[f] = _utc(v)
    return out


def save_records(records: Iterable[dict], path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    payload = [_serialise(r) for r in records]
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".predictions-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_records(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    out = []
    for rec in payload:
        try:
            out.append(_deserialise(rec))
        except (ValueError, TypeError, KeyError):
            continue
    return out
