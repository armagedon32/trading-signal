"""Glue between the raw fetchers, the JSON cache, and the web/bot layers.

Rules:
* Never call a provider more than once per ``QUOTE_TTL_SECONDS`` for the same
  (currency, amount) - free hosting has tiny CPU budgets and we should be a
  polite client.
* If fetching fails, serve the last good result and flag it ``stale``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import rates
from .config import CURRENCIES, DEFAULT_CURRENCY, HIGH_RATE_DAYS, MAX_AMOUNT, MIN_AMOUNT, settings
from .store import JsonStore


class RateService:
    def __init__(self, store: JsonStore, *, client: httpx.Client | None = None, manual_path: Path | None = None):
        self.store = store
        self._client = client
        self.manual_path = manual_path

    # ----------------------------------------------------------------- helpers
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(follow_redirects=True)
        return self._client

    @staticmethod
    def normalise(currency: str | None, amount: float | int | str | None) -> tuple[str, float]:
        cur = (currency or DEFAULT_CURRENCY).upper()
        if cur not in CURRENCIES:
            cur = DEFAULT_CURRENCY
        try:
            amt = float(str(amount).replace(",", "")) if amount not in (None, "") else float(CURRENCIES[cur].default_amount)
        except ValueError:
            amt = float(CURRENCIES[cur].default_amount)
        amt = max(MIN_AMOUNT, min(MAX_AMOUNT, amt))
        return cur, round(amt, 2)

    @staticmethod
    def _cache_amount(amount: float) -> float:
        """Round the amount so nearby values share a cache entry (quotes scale linearly)."""
        if amount < 100:
            return round(amount)
        return float(round(amount / 10) * 10)

    # ------------------------------------------------------------- comparison
    def compare(self, currency: str, amount: float, *, force: bool = False) -> rates.Comparison:
        currency, amount = self.normalise(currency, amount)
        key_amount = self._cache_amount(amount)
        cache_key = f"cmp:{currency}:{key_amount:g}"
        cached, fresh = self.store.cache_get(cache_key, settings.quote_ttl)
        if cached and fresh and not force:
            return self._rescale(rates.Comparison.from_dict(cached), amount)

        market = self.market(currency)  # also refreshes the mid-rate
        mid = (market.rate, market.rate_source) if market.rate else None
        comparison = rates.build_comparison(
            self.client, currency, key_amount, manual_path=self.manual_path, mid=mid
        )
        live = [q for q in comparison.quotes if q.source != rates.SOURCE_MANUAL]
        if live:
            self.store.cache_put(cache_key, comparison.to_dict())
            return self._rescale(comparison, amount)
        # Every live source failed. Serve the last good copy for this amount, or the
        # newest one we have for this currency (quotes scale with the amount), flagged.
        fallback = cached
        if fallback is None:
            found = self.store.cache_find(f"cmp:{currency}:")
            fallback = found[1] if found else None
        if fallback:
            old = rates.Comparison.from_dict(fallback)
            old.stale = True
            old.warnings = comparison.warnings + ["Showing the last saved rates."]
            return self._rescale(old, amount)
        return comparison

    @staticmethod
    def _rescale(comparison: rates.Comparison, amount: float) -> rates.Comparison:
        """Quotes were fetched for a rounded amount; scale pesos to the exact amount."""
        if abs(comparison.amount - amount) < 1e-9 or comparison.amount <= 0:
            return comparison
        for q in comparison.quotes:
            if q.promo and q.promo_rate and q.regular_rate:
                # promo quotes carry their own per-unit rates; the headline `rate`
                # may be a blend when the amount exceeds the promo cap
                q.received = q.received_for(amount)
                q.rate = round(q.received / max(amount - q.fee, 1e-9), 6)
                q.regular_received = round(max(amount - q.fee, 0.0) * q.regular_rate, 2)
            else:
                q.received = round(max(amount - q.fee, 0.0) * q.rate, 2)
                if q.regular_received is not None:
                    q.regular_received = round(max(amount - q.fee, 0.0) * (q.regular_received / max(comparison.amount - q.fee, 1e-9)), 2)
        comparison.quotes.sort(key=lambda q: q.received, reverse=True)
        comparison.amount = amount
        return comparison

    # ----------------------------------------------------------------- market
    def market(self, currency: str, *, force: bool = False) -> rates.MarketSummary:
        currency, _ = self.normalise(currency, None)
        cache_key = f"mkt:{currency}"
        cached, fresh = self.store.cache_get(cache_key, settings.history_ttl)
        if cached and fresh and not force:
            return rates.MarketSummary.from_dict(cached)
        summary = rates.build_market_summary(
            self.client, currency, HIGH_RATE_DAYS, own_history=self.store.rate_log(currency)
        )
        if summary.rate is not None:
            self.store.cache_put(cache_key, summary.to_dict())
            self.store.log_rate(currency, datetime.now(timezone.utc).date().isoformat(), summary.rate)
            return summary
        if cached:
            old = rates.MarketSummary.from_dict(cached)
            old.stale = True
            old.warnings = summary.warnings + ["Showing the last saved rate."]
            return old
        return summary

    def refresh_all(self) -> dict[str, str]:
        """Warm the cache for every currency at its default amount (used by the cron job)."""
        results = {}
        for code, cur in CURRENCIES.items():
            try:
                cmp_ = self.compare(code, cur.default_amount, force=True)
                best = cmp_.best
                results[code] = f"{best.provider_name} {best.received:,.2f}" if best else "no quotes"
            except Exception as exc:  # noqa: BLE001 - keep the loop going
                results[code] = f"error: {exc}"
        return results
