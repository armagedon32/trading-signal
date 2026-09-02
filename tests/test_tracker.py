from datetime import datetime, timedelta, timezone

import pandas as pd

from trading_signal import tracker

T0 = datetime(2026, 9, 1, 10, 2, 30, tzinfo=timezone.utc)  # click time: 2m30s into the 10:00 5m bar
BAR = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def bars(prices, start=BAR, freq="5min"):
    idx = pd.date_range(start, periods=len(prices), freq=freq, tz="UTC")
    return pd.DataFrame({"Close": prices}, index=idx)


def test_new_record_uses_click_time_not_bar_open():
    rec = tracker.new_record("EUR/USD", "5m", "UP", 1.1, BAR, now=T0, confidence=0.6, model="m")
    assert rec["entry_time"] == T0
    assert rec["resolve_time"] == T0 + timedelta(minutes=5)
    assert rec["entry_bar_time"] == BAR
    assert rec["direction"] == "UP"
    assert rec["stale_entry"] is False
    assert tracker.seconds_remaining(rec, now=T0 + timedelta(seconds=100)) == 200
    assert tracker.seconds_remaining(rec, now=T0 + timedelta(minutes=10)) == 0


def test_new_record_flags_stale_feed():
    rec = tracker.new_record("AAPL", "1m", "UP (Call)", 100.0, BAR, now=BAR + timedelta(minutes=10))
    assert rec["stale_entry"] is True
    assert rec["data_lag_seconds"] == 600


def test_not_resolved_before_expiry():
    rec = tracker.new_record("EUR/USD", "5m", "UP", 1.1, BAR, now=T0)
    calls = []

    def fetch(sym, exp):
        calls.append((sym, exp))
        return bars([1.1, 1.2, 1.3])

    recs, n = tracker.resolve_pending([rec], fetch, now=T0 + timedelta(minutes=4))
    assert n == 0 and not recs[0]["resolved"]
    assert calls == []  # no API call wasted before expiry


def test_never_resolves_against_entry_bar_only():
    """Regression: the old code resolved with the entry bar's own close as soon
    as the page reran, turning every UP into a LOSS."""
    rec = tracker.new_record("EUR/USD", "5m", "UP", 1.1, BAR, now=T0)
    # Only the entry bar exists in the feed, expiry (10:07:30) is not covered.
    recs, n = tracker.resolve_pending([rec], lambda s, e: bars([1.1]), now=T0 + timedelta(minutes=6))
    assert n == 0 and not recs[0]["resolved"]


def test_resolves_with_bar_containing_expiry():
    rec = tracker.new_record("EUR/USD", "5m", "UP", 1.1, BAR, now=T0)
    # bars 10:00, 10:05 (contains expiry 10:07:30), 10:10
    feed = bars([1.1, 1.15, 0.9])
    recs, n = tracker.resolve_pending([rec], lambda s, e: feed, now=T0 + timedelta(minutes=6))
    r = recs[0]
    assert n == 1 and r["resolved"]
    assert r["exit_bar_time"] == BAR + timedelta(minutes=5)
    assert r["exit_price"] == 1.15
    assert r["outcome"] == "WIN" and r["correct"] is True


def test_loss_and_tie():
    up = tracker.new_record("X", "1m", "UP", 10.0, BAR, now=BAR + timedelta(seconds=10))
    down = tracker.new_record("X", "1m", "DOWN", 10.0, BAR, now=BAR + timedelta(seconds=10))
    tie = tracker.new_record("X", "1m", "UP", 9.5, BAR, now=BAR + timedelta(seconds=10))
    feed = bars([10.0, 9.5, 9.7], freq="1min")
    recs, n = tracker.resolve_pending([up, down, tie], lambda s, e: feed, now=BAR + timedelta(minutes=2))
    assert n == 3
    assert recs[0]["outcome"] == "LOSS" and recs[0]["correct"] is False
    assert recs[1]["outcome"] == "WIN"
    assert recs[2]["outcome"] == "TIE" and recs[2]["correct"] is None


def test_settles_after_grace_when_market_closed():
    rec = tracker.new_record("SPY", "5m", "DOWN", 500.0, BAR, now=T0)
    feed = bars([500.0, 499.0])  # last bar 10:05 ends 10:10 -> expiry 10:07:30 is covered. Use a gap instead:
    feed = bars([500.0])  # only 10:00 bar; market closed afterwards
    # before grace: stays pending
    recs, n = tracker.resolve_pending([rec], lambda s, e: feed, now=T0 + timedelta(minutes=10))
    assert n == 0
    # after grace (3 bars = 15 min after expiry): settle on last available bar
    recs, n = tracker.resolve_pending([rec], lambda s, e: feed, now=T0 + timedelta(minutes=5 + 16))
    assert n == 1 and recs[0]["outcome"] == "TIE" and "settled" in recs[0]["note"]


def test_fetch_failure_keeps_pending():
    rec = tracker.new_record("X", "1m", "UP", 1.0, BAR, now=BAR)

    def boom(s, e):
        raise RuntimeError("api down")

    recs, n = tracker.resolve_pending([rec], boom, now=BAR + timedelta(minutes=5))
    assert n == 0 and not recs[0]["resolved"]


def test_fetch_is_shared_per_symbol_expiry():
    recs = [tracker.new_record("X", "1m", "UP", 1.0, BAR, now=BAR) for _ in range(5)]
    calls = []

    def fetch(s, e):
        calls.append(1)
        return bars([1.0, 1.1, 1.2], freq="1min")

    tracker.resolve_pending(recs, fetch, now=BAR + timedelta(minutes=3))
    assert len(calls) == 1


def test_summarize_and_buckets():
    recs = []
    for conf, outcome in [(0.55, "WIN"), (0.58, "LOSS"), (0.75, "WIN"), (0.75, "WIN"), (0.95, "TIE")]:
        r = tracker.new_record("X", "1m", "UP", 1.0, BAR, now=BAR, confidence=conf)
        r.update(resolved=True, outcome=outcome, correct=None if outcome == "TIE" else outcome == "WIN")
        recs.append(r)
    recs.append(tracker.new_record("X", "1m", "UP", 1.0, BAR, now=BAR, source="manual"))
    s = tracker.summarize(recs)
    assert s == {"total": 6, "pending": 1, "resolved": 5, "wins": 3, "losses": 1, "ties": 1, "win_rate": 75.0}
    assert tracker.summarize(recs, source="manual")["win_rate"] is None
    b = tracker.confidence_buckets(recs).set_index("bucket")
    assert b.loc["50-60", "count"] == 2 and b.loc["50-60", "win_rate"] == 50.0
    assert b.loc["70-80", "count"] == 2 and b.loc["70-80", "win_rate"] == 100.0
    assert b.loc["90-100", "count"] == 0  # ties excluded


def test_to_frame_columns():
    df = tracker.to_frame([tracker.new_record("X", "1m", "UP", 1.0, BAR, now=BAR)])
    assert list(df.columns)[:3] == ["entry_time", "symbol", "expiry"]
    assert tracker.to_frame([]).empty


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "sub" / "predictions.json"
    rec = tracker.new_record("EUR/USD", "5m", "UP", 1.1, BAR, now=T0, confidence=0.6)
    rec["factors"] = {"RSI": 55.0}
    tracker.save_records([rec], str(path))
    loaded = tracker.load_records(str(path))
    assert len(loaded) == 1
    assert loaded[0]["entry_time"] == T0
    assert loaded[0]["resolve_time"] == rec["resolve_time"]
    assert loaded[0]["resolved_at"] is None
    assert loaded[0]["factors"] == {"RSI": 55.0}


def test_load_records_bad_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json")
    assert tracker.load_records(str(p)) == []
    assert tracker.load_records(str(tmp_path / "missing.json")) == []
