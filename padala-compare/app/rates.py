"""Fetch and normalise remittance quotes and mid-market exchange rates.

Free sources (no API key needed):

* Wise comparison feed   ``api.wise.com/v3/comparisons``          many providers incl. Wise
* Remitly calculator     ``api.remitly.io/v3/calculator/estimate`` Remitly's own quote incl. promo
* Wise live / history    ``wise.com/rates/...``                    mid-market rate, all currencies
* Frankfurter (ECB)      ``api.frankfurter.dev/v1``                mid-market fallback (no AED/SAR)
* open.er-api.com        ``open.er-api.com/v6/latest``             daily mid-market, last resort

Only Frankfurter and er-api are documented public APIs; the others are the
endpoints behind the providers' own web calculators and may change without
notice.  Every fetcher therefore *fails soft*: it raises :class:`FetchError`
and the caller records a warning and carries on with whatever it has.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import CURRENCIES, IGNORED_WISE_TYPES, PROVIDERS, SITE_URL, settings

WISE_COMPARISON_URL = "https://api.wise.com/v3/comparisons/"
WISE_LIVE_URL = "https://wise.com/rates/live"
WISE_HISTORY_URL = "https://wise.com/rates/history+live"
REMITLY_ESTIMATE_URL = "https://api.remitly.io/v3/calculator/estimate"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1"
ER_API_URL = "https://open.er-api.com/v6/latest"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/124.0 Safari/537.36 PadalaCompare/0.1 (+{SITE_URL})"
    ),
    "Accept": "application/json",
}

SOURCE_WISE_FEED = "wise-comparison"
SOURCE_REMITLY = "remitly-calculator"
SOURCE_MANUAL = "manual"


class FetchError(RuntimeError):
    """A data source could not be used (network, HTTP status, bad payload)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_amount(value: float) -> str:
    """500.0 -> '500', 503.5 -> '503.5', 1000000.0 -> '1000000' (never scientific notation)."""
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text or "0"


# --------------------------------------------------------------------------- models


@dataclass
class Quote:
    """What one provider gives when you pay ``amount`` of the source currency."""

    provider_key: str
    provider_name: str
    rate: float                  # provider's exchange rate, PHP per 1 unit
    fee: float                   # fee in source currency (already reflected in `received`)
    received: float              # PHP the recipient gets
    source: str                  # SOURCE_* constant
    collected_at: str | None = None   # ISO-8601 time the quote was collected
    promo: bool = False
    promo_note: str = ""
    regular_received: float | None = None  # what you'd get without a new-customer promo
    delivery: str = ""

    def effective_rate(self, amount: float) -> float:
        """Pesos per unit actually paid, fee included."""
        return self.received / amount if amount else 0.0

    def markup_pct(self, mid_rate: float | None) -> float | None:
        """How far below the mid-market rate the provider's rate is (hidden cost)."""
        if not mid_rate:
            return None
        return (mid_rate - self.rate) / mid_rate * 100.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Quote":
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]


@dataclass
class Comparison:
    currency: str
    amount: float
    quotes: list[Quote]                  # sorted best (most pesos) first
    mid_rate: float | None = None
    mid_source: str = ""
    fetched_at: str = field(default_factory=lambda: utc_now().isoformat(timespec="seconds"))
    warnings: list[str] = field(default_factory=list)
    stale: bool = False

    @property
    def best(self) -> Quote | None:
        return self.quotes[0] if self.quotes else None

    @property
    def worst(self) -> Quote | None:
        return self.quotes[-1] if self.quotes else None

    @property
    def spread(self) -> float:
        """Pesos difference between the best and worst option shown."""
        if len(self.quotes) < 2:
            return 0.0
        return self.quotes[0].received - self.quotes[-1].received

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["best"] = self.best.provider_key if self.best else None
        data["spread"] = round(self.spread, 2)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Comparison":
        return cls(
            currency=data["currency"],
            amount=float(data["amount"]),
            quotes=[Quote.from_dict(q) for q in data.get("quotes", [])],
            mid_rate=data.get("mid_rate"),
            mid_source=data.get("mid_source", ""),
            fetched_at=data.get("fetched_at", ""),
            warnings=list(data.get("warnings", [])),
            stale=bool(data.get("stale", False)),
        )


@dataclass
class MarketSummary:
    """Mid-market rate plus where it sits in the recent range."""

    currency: str
    rate: float | None
    rate_source: str = ""
    history: list[tuple[str, float]] = field(default_factory=list)  # [(YYYY-MM-DD, rate)]
    as_of: str = field(default_factory=lambda: utc_now().isoformat(timespec="seconds"))
    warnings: list[str] = field(default_factory=list)
    stale: bool = False

    @property
    def high(self) -> float | None:
        return max((v for _, v in self.history), default=None)

    @property
    def low(self) -> float | None:
        return min((v for _, v in self.history), default=None)

    @property
    def average(self) -> float | None:
        return sum(v for _, v in self.history) / len(self.history) if self.history else None

    @property
    def percentile(self) -> float | None:
        """Share of recent days whose rate was at or below today's rate (0..1)."""
        if self.rate is None or not self.history:
            return None
        below = sum(1 for _, v in self.history if v <= self.rate)
        return below / len(self.history)

    @property
    def label(self) -> str:
        p = self.percentile
        if p is None:
            return "unknown"
        if p >= 0.8:
            return "high"
        if p <= 0.2:
            return "low"
        return "normal"

    @property
    def change_1d(self) -> float | None:
        if self.rate is None or len(self.history) < 2:
            return None
        return self.rate - self.history[-2][1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "rate": self.rate,
            "rate_source": self.rate_source,
            "history": self.history,
            "as_of": self.as_of,
            "warnings": self.warnings,
            "stale": self.stale,
            "high": self.high,
            "low": self.low,
            "average": self.average,
            "percentile": self.percentile,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarketSummary":
        return cls(
            currency=data["currency"],
            rate=data.get("rate"),
            rate_source=data.get("rate_source", ""),
            history=[(d, float(v)) for d, v in data.get("history", [])],
            as_of=data.get("as_of", ""),
            warnings=list(data.get("warnings", [])),
            stale=bool(data.get("stale", False)),
        )


# --------------------------------------------------------------------------- HTTP helper


def get_json(client: httpx.Client, url: str, params: dict[str, Any] | None = None) -> Any:
    try:
        response = client.get(url, params=params, headers=HEADERS, timeout=settings.http_timeout)
    except httpx.HTTPError as exc:  # network / timeout / DNS
        raise FetchError(f"{type(exc).__name__} while calling {url}") from exc
    if response.status_code != 200:
        raise FetchError(f"HTTP {response.status_code} from {url}")
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"invalid JSON from {url}") from exc


# --------------------------------------------------------------------------- parsers (pure)


def _provider_identity(alias: str, feed_name: str) -> tuple[str, str]:
    for provider in PROVIDERS.values():
        if provider.wise_alias == alias:
            return provider.key, provider.name
    return alias, feed_name


def parse_wise_comparison(payload: dict[str, Any], amount: float) -> list[Quote]:
    """Turn the Wise comparison payload into one Quote per provider (best quote wins)."""
    quotes: list[Quote] = []
    for entry in payload.get("providers", []) or []:
        if entry.get("type") in IGNORED_WISE_TYPES:
            continue
        candidates = []
        for raw in entry.get("quotes", []) or []:
            received = raw.get("receivedAmount")
            rate = raw.get("rate")
            if received is None or rate is None or float(received) <= 0:
                continue
            candidates.append(raw)
        if not candidates:
            continue
        best = max(candidates, key=lambda q: float(q["receivedAmount"]))
        key, name = _provider_identity(entry.get("alias", ""), entry.get("name", entry.get("alias", "?")))
        delivery = ""
        est = best.get("deliveryEstimation") or {}
        duration = (est.get("duration") or {}).get("min")
        if duration:
            delivery = _humanise_iso_duration(duration)
        quotes.append(
            Quote(
                provider_key=key,
                provider_name=name,
                rate=float(best["rate"]),
                fee=float(best.get("fee") or 0.0),
                received=float(best["receivedAmount"]),
                source=SOURCE_WISE_FEED,
                collected_at=best.get("dateCollected"),
                delivery=delivery,
            )
        )
    return quotes


def _humanise_iso_duration(value: str) -> str:
    """'PT31M47S' -> 'about 32 min'; 'PT19H11M' -> 'about 19 h'; 'PT1S' -> 'instant'."""
    hours = minutes = seconds = 0.0
    number = ""
    for ch in value.replace("PT", ""):
        if ch.isdigit() or ch == ".":
            number += ch
        elif ch == "H":
            hours = float(number or 0); number = ""
        elif ch == "M":
            minutes = float(number or 0); number = ""
        elif ch == "S":
            seconds = float(number or 0); number = ""
        else:
            number = ""
    total_min = round(hours * 60 + minutes + seconds / 60)
    if total_min <= 1:
        return "instant"
    if total_min < 90:
        return f"about {total_min} min"
    return f"about {round(total_min / 60)} h"


def parse_remitly_estimate(payload: Any, amount: float) -> Quote:
    """Parse Remitly's calculator response. Raises FetchError on an error payload."""
    if isinstance(payload, list):  # Remitly returns a list of error objects
        message = payload[0].get("message", "unknown error") if payload else "empty error list"
        raise FetchError(f"Remitly: {message}")
    est = (payload or {}).get("estimate")
    if not est:
        raise FetchError("Remitly: no estimate in response")

    fx = est.get("exchange_rate") or {}
    base_rate = float(fx.get("base_rate") or 0)
    promo_rate = fx.get("promotional_exchange_rate")
    cap = fx.get("capped_promotional_exchange_rate_amount")
    fee_total = float((est.get("fee") or {}).get("total_fee_amount") or 0)
    discount = (est.get("discount") or {}).get("fee_discount_amount")
    fee = max(fee_total - float(discount or 0), 0.0)
    received = float(est.get("receive_amount") or 0)
    send_amount = float(est.get("send_amount") or amount)
    if received <= 0 or send_amount <= 0:
        raise FetchError("Remitly: empty quote")

    # Remitly charges any fee on top of the amount sent; the Wise feed treats the
    # amount as the total you pay.  Normalise to "you pay `amount` in total".
    conversion_rate = received / send_amount
    if fee > 0:
        received = (amount - fee) * conversion_rate

    quote = Quote(
        provider_key="remitly",
        provider_name=PROVIDERS["remitly"].name,
        rate=round(conversion_rate, 6),
        fee=fee,
        received=round(received, 2),
        source=SOURCE_REMITLY,
        collected_at=utc_now().isoformat(timespec="seconds"),
    )
    if promo_rate and base_rate and float(promo_rate) > base_rate:
        quote.promo = True
        cur = (est.get("conduit") or {}).get("source_currency", {}).get("alpha3", "")
        cap_text = f" on your first {cur} {float(cap):,.0f}" if cap else ""
        quote.promo_note = (
            f"New-customer promo rate {float(promo_rate):.4g} (regular {base_rate:.4g}){cap_text}."
        )
        quote.regular_received = round((amount - fee) * base_rate, 2)
    return quote


def parse_wise_history(payload: Any) -> list[tuple[str, float]]:
    points: dict[str, float] = {}
    for item in payload or []:
        ts = item.get("time")
        value = item.get("value")
        if ts is None or value is None:
            continue
        day = datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).date().isoformat()
        points[day] = float(value)  # later entries (live) overwrite earlier same-day ones
    return sorted(points.items())


def parse_frankfurter_history(payload: dict[str, Any], target: str = "PHP") -> list[tuple[str, float]]:
    rates = payload.get("rates") or {}
    points = [(day, float(v[target])) for day, v in rates.items() if target in v]
    return sorted(points)


# --------------------------------------------------------------------------- fetchers


def fetch_wise_comparison(client: httpx.Client, currency: str, amount: float) -> list[Quote]:
    payload = get_json(
        client,
        WISE_COMPARISON_URL,
        {"sourceCurrency": currency, "targetCurrency": "PHP", "sendAmount": amount},
    )
    if not isinstance(payload, dict):
        raise FetchError("Wise comparison: unexpected payload")
    return parse_wise_comparison(payload, amount)


def fetch_remitly_quote(client: httpx.Client, currency: str, amount: float) -> Quote:
    cur = CURRENCIES[currency]
    payload = get_json(
        client,
        REMITLY_ESTIMATE_URL,
        {
            "conduit": f"{cur.country_iso3}:{currency}-PHL:PHP",
            "anchor": "SEND",
            "amount": fmt_amount(amount),
            "purpose": "OTHER",
            "customer_segment": "UNRECOGNIZED",
            "strict_promo": "false",
        },
    )
    return parse_remitly_estimate(payload, amount)


def fetch_mid_rate(client: httpx.Client, currency: str) -> tuple[float, str]:
    """Mid-market PHP per 1 unit.  Tries three sources in order."""
    errors: list[str] = []
    try:
        payload = get_json(client, WISE_LIVE_URL, {"source": currency, "target": "PHP"})
        value = float(payload["value"])
        if value > 0:
            return value, "wise"
    except (FetchError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"wise: {exc}")
    try:
        payload = get_json(client, f"{FRANKFURTER_URL}/latest", {"from": currency, "to": "PHP"})
        value = float(payload["rates"]["PHP"])
        if value > 0:
            return value, "frankfurter"
    except (FetchError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"frankfurter: {exc}")
    try:
        payload = get_json(client, f"{ER_API_URL}/{currency}")
        value = float(payload["rates"]["PHP"])
        if value > 0:
            return value, "er-api"
    except (FetchError, KeyError, TypeError, ValueError) as exc:
        errors.append(f"er-api: {exc}")
    raise FetchError("; ".join(errors))


def fetch_history(client: httpx.Client, currency: str, days: int) -> tuple[list[tuple[str, float]], str]:
    errors: list[str] = []
    try:
        payload = get_json(
            client,
            WISE_HISTORY_URL,
            {"source": currency, "target": "PHP", "length": days, "resolution": "daily", "unit": "day"},
        )
        points = parse_wise_history(payload)
        if len(points) >= 2:
            return points, "wise"
    except (FetchError, AttributeError, TypeError, ValueError) as exc:
        errors.append(f"wise: {exc}")
    try:
        end = utc_now().date()
        start = end - timedelta(days=days)
        payload = get_json(client, f"{FRANKFURTER_URL}/{start}..{end}", {"from": currency, "to": "PHP"})
        points = parse_frankfurter_history(payload)
        if len(points) >= 2:
            return points, "frankfurter"
    except (FetchError, AttributeError, TypeError, ValueError) as exc:
        errors.append(f"frankfurter: {exc}")
    raise FetchError("; ".join(errors) or "no history available")


# --------------------------------------------------------------------------- manual quotes


def load_manual_quotes(path: Path, currency: str, amount: float) -> list[Quote]:
    """Quotes you typed in yourself (see data/manual_rates.json) for providers with no API."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    quotes = []
    for key, raw in (data.get(currency) or {}).items():
        try:
            rate = float(raw["rate"])
            fee = float(raw.get("fee", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if rate <= 0 or amount - fee <= 0:
            continue
        provider = PROVIDERS.get(key)
        quotes.append(
            Quote(
                provider_key=key,
                provider_name=provider.name if provider else raw.get("name", key),
                rate=rate,
                fee=fee,
                received=round((amount - fee) * rate, 2),
                source=SOURCE_MANUAL,
                collected_at=raw.get("as_of"),
                promo_note=raw.get("note", ""),
            )
        )
    return quotes


# --------------------------------------------------------------------------- orchestration


def build_comparison(
    client: httpx.Client,
    currency: str,
    amount: float,
    *,
    manual_path: Path | None = None,
    mid: tuple[float, str] | None = None,
) -> Comparison:
    """Collect quotes from every source and rank them by pesos received."""
    if currency not in CURRENCIES:
        raise ValueError(f"unsupported currency {currency}")
    warnings: list[str] = []
    quotes: dict[str, Quote] = {}

    try:
        for q in fetch_wise_comparison(client, currency, amount):
            quotes[q.provider_key] = q
    except FetchError as exc:
        warnings.append(f"Comparison feed unavailable ({exc}).")

    try:
        remitly = fetch_remitly_quote(client, currency, amount)
        quotes["remitly"] = remitly  # fresher than the feed and includes the promo
    except FetchError as exc:
        if "remitly" not in quotes:
            warnings.append(f"Remitly quote unavailable ({exc}).")

    if manual_path is not None:
        for q in load_manual_quotes(manual_path, currency, amount):
            quotes.setdefault(q.provider_key, q)

    if mid is None:
        try:
            mid = fetch_mid_rate(client, currency)
        except FetchError as exc:
            warnings.append(f"Mid-market rate unavailable ({exc}).")

    ranked = sorted(quotes.values(), key=lambda q: q.received, reverse=True)
    if not ranked:
        warnings.append("No provider quotes could be loaded right now.")
    return Comparison(
        currency=currency,
        amount=amount,
        quotes=ranked,
        mid_rate=mid[0] if mid else None,
        mid_source=mid[1] if mid else "",
        warnings=warnings,
    )


def build_market_summary(client: httpx.Client, currency: str, days: int, *, own_history: dict[str, float] | None = None) -> MarketSummary:
    warnings: list[str] = []
    rate: float | None = None
    source = ""
    try:
        rate, source = fetch_mid_rate(client, currency)
    except FetchError as exc:
        warnings.append(f"Mid-market rate unavailable ({exc}).")

    history: list[tuple[str, float]] = []
    try:
        history, _ = fetch_history(client, currency, days)
    except FetchError as exc:
        warnings.append(f"Rate history unavailable ({exc}).")
        if own_history:
            history = sorted(own_history.items())

    cutoff = (utc_now().date() - timedelta(days=days)).isoformat()
    history = [(d, v) for d, v in history if d >= cutoff]
    if rate is not None:
        today = utc_now().date().isoformat()
        history = [(d, v) for d, v in history if d != today] + [(today, rate)]
    return MarketSummary(currency=currency, rate=rate, rate_source=source, history=history, warnings=warnings)


def as_date(value: str) -> date:
    return date.fromisoformat(value)
