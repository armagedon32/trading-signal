from __future__ import annotations

import threading

import httpx
import pytest

from app import rates
from app.service import RateService
from app.store import JsonStore

from .conftest import load


# ------------------------------------------------------------------ JsonStore


def test_store_roundtrip_and_atomic_write(tmp_path):
    s = JsonStore(tmp_path / "x.json")
    s.put("a", "k", {"v": 1})
    assert s.get("a", "k") == {"v": 1}
    assert JsonStore(tmp_path / "x.json").get("a", "k") == {"v": 1}  # persisted
    assert s.increment("c", "n") == 1 and s.increment("c", "n", 4) == 5
    assert s.delete("a", "k") is True and s.delete("a", "k") is False
    assert not list(tmp_path.glob("*.tmp"))


def test_store_cache_freshness(tmp_path):
    s = JsonStore(tmp_path / "x.json")
    assert s.cache_get("nope", 10) == (None, False)
    s.cache_put("k", [1, 2])
    value, fresh = s.cache_get("k", 10)
    assert value == [1, 2] and fresh
    s._data["cache"]["k"]["saved_at"] -= 100  # age it
    assert s.cache_get("k", 10) == ([1, 2], False)
    assert s.cache_get("k", None) == ([1, 2], True)


def test_store_corrupt_file_is_tolerated(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{not json")
    s = JsonStore(path)
    assert s.section("subscribers") == {}
    s.save_subscriber(1, {"currency": "USD"})
    assert s.subscriber(1)["currency"] == "USD" and "since" in s.subscriber(1)
    assert s.remove_subscriber(1) and s.subscriber(1) is None


def test_store_rate_log_trims(tmp_path):
    s = JsonStore(tmp_path / "x.json")
    for i in range(810):
        s.log_rate("USD", f"2020-{(i // 28) % 12 + 1:02d}-{i % 28 + 1:02d}-{i:04d}", 1.0 + i)
    assert len(s.rate_log("USD")) == 800


def test_store_thread_safety(tmp_path):
    s = JsonStore(tmp_path / "x.json")

    def work():
        for _ in range(50):
            s.increment("c", "n")

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert s.get("c", "n") == 400


def test_store_clicks(tmp_path):
    s = JsonStore(tmp_path / "x.json")
    s.record_click("remitly", "USD")
    s.record_click("remitly", "USD")
    s.record_click("wise", "AED")
    stats = s.click_stats()
    assert stats["total"] == {"remitly": 2, "wise": 1}
    assert sum(stats["daily"].values()) == 3


# ------------------------------------------------------------------ RateService


def test_normalise_inputs():
    n = RateService.normalise
    assert n(None, None) == ("USD", 500.0)
    assert n("aed", "1,250.50") == ("AED", 1250.5)
    assert n("XXX", "abc") == ("USD", 500.0)
    assert n("JPY", "-5") == ("JPY", 1.0)
    assert n("USD", "99999999") == ("USD", 1_000_000.0)


def test_compare_uses_cache_then_rescales(service, routes, fast_settings):
    first = service.compare("USD", 500)
    calls_after_first = len(routes.calls)
    assert first.best.provider_key == "remitly" and first.best.received == pytest.approx(32080.0)

    second = service.compare("USD", 503)  # rounds to the same 500 bucket -> no new HTTP calls
    assert len(routes.calls) == calls_after_first
    assert second.amount == 503
    assert second.best.received == pytest.approx(503 * 64.16, rel=1e-6)
    wise = next(q for q in second.quotes if q.provider_key == "wise")
    assert wise.received == pytest.approx((503 - 9.43) * 62.5104, rel=1e-6)

    third = service.compare("USD", 1000)  # different bucket -> fetch again
    assert len(routes.calls) > calls_after_first and third.amount == 1000


def test_compare_serves_stale_copy_when_sources_fail(service, routes, fast_settings):
    good = service.compare("USD", 500)
    assert not good.stale
    service.store._data["cache"]["cmp:USD:500"]["saved_at"] -= 10_000  # expire it
    routes.set(rates.WISE_COMPARISON_URL, httpx.ConnectError("down"))
    routes.set(rates.REMITLY_ESTIMATE_URL, 500)
    stale = service.compare("USD", 500)
    assert stale.stale is True
    assert stale.best.provider_key == "remitly"
    assert any("last saved" in w for w in stale.warnings)


def test_market_caches_and_logs_rate(service, routes, fast_settings):
    m = service.market("USD")
    n = len(routes.calls)
    assert m.rate == pytest.approx(62.5104) and m.label in {"high", "normal", "low"}
    assert service.store.rate_log("USD")  # today's rate logged for our own history
    service.market("USD")
    assert len(routes.calls) == n  # cached


def test_market_uses_own_log_when_history_sources_fail(service, routes, fast_settings):
    routes.set(rates.WISE_HISTORY_URL, 500)
    routes.set(rates.FRANKFURTER_URL, 500)
    service.store.log_rate("USD", "2026-08-30", 61.9)
    service.store.log_rate("USD", "2026-08-31", 62.0)
    m = service.market("USD", force=True)
    assert m.rate == pytest.approx(62.5104)
    assert [d for d, _ in m.history][:2] == ["2026-08-30", "2026-08-31"]
    assert any("history unavailable" in w for w in m.warnings)


def test_refresh_all_reports_every_currency(service, fast_settings):
    result = service.refresh_all()
    assert set(result) == set(rates.CURRENCIES)
    assert all(isinstance(v, str) and v for v in result.values())


def test_manual_only_when_live_missing(service, routes, fast_settings):
    routes.set(rates.WISE_COMPARISON_URL, {"providers": []})
    routes.set(rates.REMITLY_ESTIMATE_URL, load("remitly_error.json"))
    cmp_ = service.compare("USD", 500, force=True)
    assert [q.provider_key for q in cmp_.quotes] == ["moneygram"]
    assert cmp_.quotes[0].source == rates.SOURCE_MANUAL


def test_cache_amount_buckets():
    b = RateService._cache_amount
    assert b(57.4) == 57 and b(503) == 500 and b(1005) == 1000 and b(1006) == 1010


def test_service_lazy_client_is_real_httpx(store):
    svc = RateService(store)
    assert isinstance(svc.client, httpx.Client)


def test_compare_falls_back_to_other_amount_when_offline(service, routes, fast_settings):
    """Nothing cached for 1000 USD, every source down -> reuse the 500 USD copy, rescaled and flagged."""
    service.compare("USD", 500)
    routes.set(rates.WISE_COMPARISON_URL, httpx.ConnectError("down"))
    routes.set(rates.REMITLY_ESTIMATE_URL, 500)
    out = service.compare("USD", 1000)
    assert out.stale is True and out.amount == 1000
    assert out.best.provider_key == "remitly" and out.best.received == pytest.approx(1000 * 64.16)
    wise = next(q for q in out.quotes if q.provider_key == "wise")
    assert wise.received == pytest.approx((1000 - 9.43) * 62.5104, rel=1e-6)


def test_store_cache_find_returns_newest(tmp_path):
    s = JsonStore(tmp_path / "x.json")
    assert s.cache_find("cmp:USD:") is None
    s.cache_put("cmp:USD:500", {"a": 1})
    s._data["cache"]["cmp:USD:500"]["saved_at"] -= 50
    s.cache_put("cmp:USD:1000", {"a": 2})
    s.cache_put("cmp:AED:2000", {"a": 3})
    key, value, age = s.cache_find("cmp:USD:")
    assert key == "cmp:USD:1000" and value == {"a": 2} and age < 5
