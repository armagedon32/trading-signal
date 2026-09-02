from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import bot, main


@pytest.fixture
def web(service, store, monkeypatch, fast_settings):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "secret")
    monkeypatch.setenv("AFF_REMITLY", "https://remitly.example/ref?code=ME")
    sent: list[tuple[int, str]] = []

    class FakeTG(bot.TelegramClient):
        def __init__(self):
            super().__init__("123:abc")

        def send(self, chat_id, text):  # noqa: D401
            sent.append((chat_id, text))
            return True

        def set_webhook(self, url, secret):
            return {"ok": True, "url": url, "secret": secret}

    app = main.create_app(store=store, service=service, telegram=FakeTG())
    client = TestClient(app)
    client.sent = sent  # type: ignore[attr-defined]
    return client


def test_index_renders_ranking(web):
    r = web.get("/?cur=USD&amount=500")
    assert r.status_code == 200
    html = r.text
    assert "Magkano ang matatanggap?" in html
    assert html.index("Remitly") < html.index("Instarem")
    assert "₱32,080.00" in html
    assert "new-customer promo" in html
    assert 'href="/go/remitly?cur=USD"' in html
    assert "<svg" in html and "last 30 days" in html
    assert "Disclosure" in html
    assert 'rel="canonical" href="http://localhost:8000/?cur=USD&amp;amount=500"' in html
    assert 'name="amount" value="500"' in html


def test_index_bad_inputs_fall_back(web):
    r = web.get("/?cur=zzz&amount=banana")
    assert r.status_code == 200 and "USD" in r.text


def test_about_and_seo_routes(web):
    assert web.get("/about").status_code == 200
    robots = web.get("/robots.txt").text
    assert "Disallow: /go/" in robots and "sitemap.xml" in robots
    sitemap = web.get("/sitemap.xml")
    assert sitemap.status_code == 200 and "cur=AED" in sitemap.text
    assert web.get("/health").json()["ok"] is True


def test_go_redirects_to_affiliate_and_counts(web, store):
    r = web.get("/go/remitly?cur=USD", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://remitly.example/ref?code=ME"
    r = web.get("/go/wise", follow_redirects=False)
    assert r.headers["location"].startswith("https://wise.com")  # no affiliate set -> plain site
    assert web.get("/go/nope").status_code == 404
    assert store.click_stats()["total"] == {"remitly": 1, "wise": 1}


def test_json_api(web):
    data = web.get("/api/compare?cur=USD&amount=500").json()
    assert data["best"] == "remitly" and data["quotes"][0]["received"] == 32080.0
    assert data["spread"] > 0
    rate = web.get("/api/rate?cur=USD").json()
    assert rate["rate"] == pytest.approx(62.5104) and rate["label"] in {"high", "normal", "low"}


def test_webhook_requires_secret_and_replies(web):
    update = {"update_id": 1, "message": {"chat": {"id": 5}, "text": "/rate USD"}}
    assert web.post("/telegram/webhook", json=update).status_code == 403
    r = web.post("/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "secret"})
    assert r.status_code == 200 and web.sent[-1][0] == 5 and "1 USD" in web.sent[-1][1]


def test_admin_routes(web, store):
    assert web.post("/admin/jobs").status_code == 403
    assert web.post("/admin/jobs?token=wrong").status_code == 403
    store.save_subscriber(3, {"currency": "USD", "alert": {"currency": "USD", "threshold": 1.0}})
    r = web.post("/admin/jobs", headers={"X-Admin-Token": "secret"})
    assert r.status_code == 200 and r.json()["alerts"] == 1
    stats = web.get("/admin/stats?token=secret").json()
    assert stats["subscribers"] == 1 and stats["with_alert"] == 1
    assert stats["affiliate_links_set"]["remitly"] is True and stats["affiliate_links_set"]["wise"] is False
    hook = web.post("/admin/webhook?token=secret").json()
    assert hook["url"].endswith("/telegram/webhook") and hook["secret"] == "secret"
    refresh = web.post("/admin/refresh?token=secret").json()
    assert "USD" in refresh


def test_admin_disabled_without_token(service, store, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "")
    client = TestClient(main.create_app(store=store, service=service, telegram=bot.TelegramClient("")))
    assert client.post("/admin/jobs").status_code == 403
    assert client.post("/telegram/webhook", json={}).status_code == 403
