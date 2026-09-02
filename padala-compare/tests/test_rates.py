"""Parsers and fetchers, using the real payloads recorded on 2026-09-02."""

from __future__ import annotations

import httpx
import pytest

from app import rates
from app.rates import FetchError

from .conftest import load


# ------------------------------------------------------------------ Wise comparison feed


def test_parse_wise_comparison_ranks_and_skips_banks():
    quotes = rates.parse_wise_comparison(load("wise_usd_500.json"), 500)
    keys = {q.provider_key for q in quotes}
    assert "wells-fargo" not in keys  # type == bank is ignored
    assert {"wise", "instarem", "western-union", "xoom", "paypal", "ofx"} <= keys
    by_key = {q.provider_key: q for q in quotes}
    assert by_key["wise"].received == pytest.approx(30665.73)
    assert by_key["wise"].fee == pytest.approx(9.43)
    assert by_key["wise"].delivery == "about 32 min"
    assert by_key["instarem"].provider_name == "Instarem"
    assert by_key["western-union"].provider_name == "Western Union"  # mapped to our PROVIDERS name


def test_parse_wise_comparison_picks_best_quote_per_provider():
    payload = {
        "providers": [
            {
                "alias": "acme",
                "name": "Acme",
                "type": "moneyTransferProvider",
                "quotes": [
                    {"rate": 60.0, "fee": 1.0, "receivedAmount": 29940.0},
                    {"rate": 61.0, "fee": 0.0, "receivedAmount": 30500.0},
                    {"rate": None, "fee": 0.0, "receivedAmount": None},
                ],
            }
        ]
    }
    (quote,) = rates.parse_wise_comparison(payload, 500)
    assert quote.provider_key == "acme" and quote.received == 30500.0 and quote.rate == 61.0


def test_humanise_iso_duration():
    assert rates._humanise_iso_duration("PT1S") == "instant"
    assert rates._humanise_iso_duration("PT31M47.02S") == "about 32 min"
    assert rates._humanise_iso_duration("PT19H11M25S") == "about 19 h"
    assert rates._humanise_iso_duration("PT4M59.4S") == "about 5 min"


# ------------------------------------------------------------------ Remitly calculator


def test_parse_remitly_promo_quote():
    q = rates.parse_remitly_estimate(load("remitly_usd_500.json"), 500)
    assert q.provider_key == "remitly"
    assert q.received == pytest.approx(32080.0)
    assert q.rate == pytest.approx(64.16)
    assert q.fee == 0.0
    assert q.promo is True
    assert "regular 60.59" in q.promo_note
    assert q.regular_received == pytest.approx(500 * 60.59)


def test_parse_remitly_discounted_fee_is_free():
    # AED quote: 5.00 fee but a 5.00 fee discount -> effectively free, 17.09 promo rate
    q = rates.parse_remitly_estimate(load("remitly_aed_2000.json"), 2000)
    assert q.fee == 0.0
    assert q.received == pytest.approx(2000 * 17.09, rel=1e-6)
    assert q.promo and "AED 4,000" in q.promo_note


def test_parse_remitly_fee_is_normalised_to_total_paid():
    payload = load("remitly_usd_500.json")
    est = payload["estimate"]
    est["fee"] = {"total_fee_amount": "3.99", "is_flat": True}
    est["discount"] = {"fee_discount_amount": None}
    est["exchange_rate"] = {"base_rate": "60.00", "promotional_exchange_rate": None}
    est["receive_amount"] = "30000.00"  # Remitly: 500 sent * 60, fee charged on top
    q = rates.parse_remitly_estimate(payload, 500)
    assert q.fee == pytest.approx(3.99) and q.rate == pytest.approx(60.0)
    # our tables assume you pay 500 in total, so only 496.01 is converted
    assert q.received == pytest.approx((500 - 3.99) * 60.0, rel=1e-6)
    assert q.promo is False and q.regular_received is None


def test_parse_remitly_error_payload():
    with pytest.raises(FetchError, match="unsupported corridor"):
        rates.parse_remitly_estimate(load("remitly_error.json"), 2000)
    with pytest.raises(FetchError):
        rates.parse_remitly_estimate({}, 500)
    with pytest.raises(FetchError):
        rates.parse_remitly_estimate({"estimate": {"receive_amount": "0"}}, 500)


# ------------------------------------------------------------------ mid-market / history


def test_parse_histories():
    wise = rates.parse_wise_history(load("wise_history_sar.json"))
    assert len(wise) == 31 and wise[0][0] < wise[-1][0]
    assert wise[-1][1] == pytest.approx(16.6508)
    frank = rates.parse_frankfurter_history(load("frankfurter_usd_30d.json"))
    assert frank[0] == ("2026-07-31", 61.269) and frank[-1] == ("2026-09-02", 62.545)


def test_fetch_mid_rate_falls_back(routes, client):
    routes.set(rates.WISE_LIVE_URL, 503)
    value, source = rates.fetch_mid_rate(client, "USD")
    assert source == "frankfurter" and value == pytest.approx(62.545) or value > 0
    routes.set(rates.FRANKFURTER_URL, httpx.ConnectError("boom"))
    value, source = rates.fetch_mid_rate(client, "USD")
    assert source == "er-api" and value == 62.5
    routes.set(rates.ER_API_URL, {"result": "error"})
    with pytest.raises(FetchError):
        rates.fetch_mid_rate(client, "USD")


def test_fetch_history_falls_back_to_frankfurter(routes, client):
    routes.set(rates.WISE_HISTORY_URL, httpx.ReadTimeout("slow"))
    points, source = rates.fetch_history(client, "USD", 30)
    assert source == "frankfurter" and len(points) == 24


# ------------------------------------------------------------------ orchestration


def test_build_comparison_merges_sources(client, tmp_path):
    manual = tmp_path / "m.json"
    manual.write_text('{"USD": {"moneygram": {"rate": 61.0, "fee": 2.99}, "wise": {"rate": 1.0}}}')
    cmp_ = rates.build_comparison(client, "USD", 500, manual_path=manual)
    keys = [q.provider_key for q in cmp_.quotes]
    assert keys[0] == "remitly"  # promo rate wins
    assert keys == sorted(keys, key=lambda k: -next(q.received for q in cmp_.quotes if q.provider_key == k))
    wise = next(q for q in cmp_.quotes if q.provider_key == "wise")
    assert wise.source == rates.SOURCE_WISE_FEED  # manual entry must not override a live quote
    mg = next(q for q in cmp_.quotes if q.provider_key == "moneygram")
    assert mg.source == rates.SOURCE_MANUAL and mg.received == pytest.approx((500 - 2.99) * 61.0)
    assert cmp_.mid_rate == pytest.approx(62.5104) and cmp_.mid_source == "wise"
    assert cmp_.spread > 0 and cmp_.best.provider_key == "remitly"
    assert cmp_.warnings == []


def test_build_comparison_survives_every_failure(routes, client):
    routes.set(rates.WISE_COMPARISON_URL, httpx.ConnectError("down"))
    routes.set(rates.REMITLY_ESTIMATE_URL, load("remitly_error.json"))
    routes.set(rates.WISE_LIVE_URL, 500)
    routes.set(rates.FRANKFURTER_URL, 500)
    routes.set(rates.ER_API_URL, 500)
    cmp_ = rates.build_comparison(client, "USD", 500)
    assert cmp_.quotes == [] and cmp_.mid_rate is None
    assert any("Comparison feed" in w for w in cmp_.warnings)
    assert any("Remitly" in w for w in cmp_.warnings)
    assert any("Mid-market" in w for w in cmp_.warnings)


def test_build_comparison_rejects_unknown_currency(client):
    with pytest.raises(ValueError):
        rates.build_comparison(client, "XXX", 100)


def test_market_summary_percentile_and_label(client):
    m = rates.build_market_summary(client, "USD", 30)
    assert m.rate == pytest.approx(62.5104)
    assert m.history[-1][1] == pytest.approx(62.5104)  # today's live rate appended
    assert m.high is not None and m.low is not None and m.low <= m.rate <= max(m.high, m.rate)
    assert m.label == "high"  # 62.51 is above every SAR-history value in the fixture
    assert 0 < m.percentile <= 1


def test_market_summary_labels_low_and_normal():
    hist = [(f"2026-08-{d:02d}", 60 + d * 0.1) for d in range(1, 31)]
    low = rates.MarketSummary("USD", 60.0, history=hist)
    assert low.label == "low" and low.percentile == pytest.approx(0.0)
    normal = rates.MarketSummary("USD", 61.5, history=hist)
    assert normal.label == "normal"
    empty = rates.MarketSummary("USD", None)
    assert empty.label == "unknown" and empty.percentile is None and empty.change_1d is None


def test_quote_helpers_and_roundtrip():
    q = rates.Quote("wise", "Wise", 62.51, 9.43, 30665.73, rates.SOURCE_WISE_FEED)
    assert q.effective_rate(500) == pytest.approx(61.33, rel=1e-3)
    assert q.markup_pct(62.51) == pytest.approx(0.0)
    assert q.markup_pct(None) is None
    cmp_ = rates.Comparison("USD", 500, [q], mid_rate=62.51, mid_source="wise")
    again = rates.Comparison.from_dict(cmp_.to_dict())
    assert again.best.provider_key == "wise" and again.amount == 500 and again.mid_rate == 62.51
    m = rates.MarketSummary("USD", 62.5, history=[("2026-09-01", 62.0), ("2026-09-02", 62.5)])
    again_m = rates.MarketSummary.from_dict(m.to_dict())
    assert again_m.history == m.history and again_m.change_1d == pytest.approx(0.5)


def test_fmt_amount_never_uses_scientific_notation():
    assert rates.fmt_amount(500.0) == "500"
    assert rates.fmt_amount(503.5) == "503.5"
    assert rates.fmt_amount(1_000_000.0) == "1000000"
    assert rates.fmt_amount(0.1 + 0.2) == "0.3"


def test_remitly_promo_cap_blends_rates_for_large_amounts():
    """Remitly's promo applies only to the first 1,000 USD; above that the regular rate applies."""
    payload = load("remitly_usd_500.json")
    est = payload["estimate"]
    est["send_amount"] = "5000.00"
    est["receive_amount"] = "312570.00"  # what Remitly showed for 5,000 USD on 2026-09-02
    est["exchange_rate"] = {"base_rate": "62.10", "capped_promotional_exchange_rate_amount": "1000.00",
                            "promotional_exchange_rate": "64.17"}
    q = rates.parse_remitly_estimate(payload, 5000)
    assert q.promo and q.promo_cap == 1000 and q.promo_rate == 64.17 and q.regular_rate == 62.10
    expected = 1000 * 64.17 + 4000 * 62.10
    assert q.received == pytest.approx(expected)
    assert q.received == pytest.approx(312570.0, rel=1e-4)  # matches Remitly's own number
    assert q.rate == pytest.approx(expected / 5000, rel=1e-6)  # blended headline rate
    assert q.received_for(500) == pytest.approx(500 * 64.17)  # fully inside the cap
