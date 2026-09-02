"""Shared fixtures: a fake HTTP transport that serves the recorded provider payloads."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app import config, rates
from app.service import RateService
from app.store import JsonStore

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeRoutes:
    """Maps URL prefixes to JSON payloads (or an int status / exception) and counts calls."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []

    def set(self, prefix: str, payload) -> None:
        self.routes[prefix] = payload

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        for prefix, payload in self.routes.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, int):
                    return httpx.Response(payload, text="error")
                if callable(payload):
                    return payload(request)
                return httpx.Response(200, json=payload)
        return httpx.Response(404, text="no fake route for " + url)


@pytest.fixture
def routes() -> FakeRoutes:
    r = FakeRoutes()
    r.set(rates.WISE_COMPARISON_URL, load("wise_usd_500.json"))
    r.set(rates.REMITLY_ESTIMATE_URL, load("remitly_usd_500.json"))
    r.set(rates.WISE_LIVE_URL, load("wise_live_usd.json"))
    r.set(rates.WISE_HISTORY_URL, load("wise_history_sar.json"))
    r.set(rates.FRANKFURTER_URL, load("frankfurter_usd_30d.json"))
    r.set(rates.ER_API_URL, {"result": "success", "rates": {"PHP": 62.5}})
    return r


@pytest.fixture
def client(routes: FakeRoutes) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(routes.handler))


@pytest.fixture
def store(tmp_path: Path) -> JsonStore:
    return JsonStore(tmp_path / "padala.json")


@pytest.fixture
def service(store: JsonStore, client: httpx.Client, tmp_path: Path) -> RateService:
    manual = tmp_path / "manual.json"
    manual.write_text(json.dumps({"USD": {"moneygram": {"rate": 61.0, "fee": 2.99, "as_of": "2026-09-02"}}}))
    return RateService(store, client=client, manual_path=manual)


@pytest.fixture
def fast_settings(monkeypatch):
    monkeypatch.setattr(config.settings, "quote_ttl", 600)
    monkeypatch.setattr(config.settings, "history_ttl", 3600)
    return config.settings


@pytest.fixture
def sent():
    """Collects outgoing Telegram messages instead of sending them."""
    messages: list[tuple[int, str]] = []

    def send(chat_id: int, text: str) -> bool:
        messages.append((chat_id, text))
        return True

    send.messages = messages  # type: ignore[attr-defined]
    return send
