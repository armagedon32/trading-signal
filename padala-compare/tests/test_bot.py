from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import bot


def msg(text: str, chat_id: int = 42) -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def test_start_and_help(service, store, sent, fast_settings):
    assert bot.handle_update(msg("/start"), service, store, sent)
    assert "/alert" in sent.messages[-1][1]
    assert store.subscriber(42)["currency"] == "USD"
    bot.handle_update(msg("/help@PadalaBot"), service, store, sent)
    assert "/compare" in sent.messages[-1][1]


def test_rate_command(service, store, sent, fast_settings):
    bot.handle_update(msg("/rate usd"), service, store, sent)
    text = sent.messages[-1][1]
    assert "1 USD = ₱62.51" in text and "Last 30 days" in text
    bot.handle_update(msg("/rate XYZ"), service, store, sent)
    assert "don't know that currency" in sent.messages[-1][1]
    bot.handle_update(msg("AED"), service, store, sent)  # plain text with a currency code
    assert "1 AED" in sent.messages[-1][1]


def test_compare_command_lists_best_first_with_links(service, store, sent, fast_settings):
    bot.handle_update(msg("/compare USD 500"), service, store, sent)
    text = sent.messages[-1][1]
    assert text.index("Remitly") < text.index("Instarem") < text.index("Wise")
    assert "🥇 Remitly: <b>₱32,080.00</b> (new-customer promo)" in text
    assert "/go/remitly?cur=USD" in text and "Difference best vs worst" in text
    bot.handle_update(msg("/compare"), service, store, sent)  # defaults: saved currency + default amount
    assert "Send $500 USD" in sent.messages[-1][1]


def test_alert_set_and_off(service, store, sent, fast_settings):
    bot.handle_update(msg("/alert"), service, store, sent)
    assert "Usage" in sent.messages[-1][1]
    bot.handle_update(msg("/alert USD abc"), service, store, sent)
    assert "must be a number" in sent.messages[-1][1]
    bot.handle_update(msg("/alert USD 0"), service, store, sent)
    assert "above zero" in sent.messages[-1][1]
    bot.handle_update(msg("/alert USD 63"), service, store, sent)
    assert "1 USD ≥ ₱63" in sent.messages[-1][1] and "Right now" in sent.messages[-1][1]
    assert store.subscriber(42)["alert"] == {"currency": "USD", "threshold": 63.0, "last_sent": None}
    bot.handle_update(msg("/alert 61"), service, store, sent)  # single arg = saved currency
    assert store.subscriber(42)["alert"]["threshold"] == 61.0
    bot.handle_update(msg("/alert off"), service, store, sent)
    assert "alert" not in store.subscriber(42)


def test_daily_toggle_and_stop(service, store, sent, fast_settings):
    bot.handle_update(msg("/daily on AED"), service, store, sent)
    assert store.subscriber(42)["daily"] is True and store.subscriber(42)["currency"] == "AED"
    bot.handle_update(msg("/daily off"), service, store, sent)
    assert store.subscriber(42)["daily"] is False
    bot.handle_update(msg("/stop"), service, store, sent)
    assert store.subscriber(42) is None


def test_ignores_non_text_updates_and_survives_crashes(service, store, sent, monkeypatch):
    assert bot.handle_update({"update_id": 1}, service, store, sent) is False
    assert bot.handle_update({"message": {"chat": {"id": 1}, "photo": []}}, service, store, sent) is False

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(bot, "handle_message", boom)
    assert bot.handle_update(msg("/rate"), service, store, sent) is True
    assert "Something went wrong" in sent.messages[-1][1]


# ------------------------------------------------------------------ scheduled jobs


def test_run_alerts_fires_once_then_rearms(service, store, sent, fast_settings, monkeypatch):
    store.save_subscriber(1, {"currency": "USD", "alert": {"currency": "USD", "threshold": 60.0, "last_sent": None}})
    store.save_subscriber(2, {"currency": "USD", "alert": {"currency": "USD", "threshold": 99.0, "last_sent": None}})
    now = datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc)
    assert bot.run_alerts(service, store, sent, now=now) == 1
    (chat_id, text), = sent.messages
    assert chat_id == 1 and "hit your target" in text and "/go/remitly" in text
    # same day, still above threshold -> nothing more
    assert bot.run_alerts(service, store, sent, now=now) == 0
    # next day but never dropped below -> still armed=False -> no repeat spam
    assert bot.run_alerts(service, store, sent, now=now + timedelta(days=1)) == 0
    # rate drops below threshold -> re-arm
    monkeypatch.setattr(service, "market", lambda cur, **k: bot.MarketSummary(cur, 59.0))
    assert bot.run_alerts(service, store, sent, now=now + timedelta(days=1)) == 0
    assert store.subscriber(1)["alert"]["armed"] is True
    # rises again on a later day -> fires again
    monkeypatch.undo()
    assert bot.run_alerts(service, store, sent, now=now + timedelta(days=2)) == 1


def test_run_daily_respects_manila_morning(service, store, sent, fast_settings):
    store.save_subscriber(7, {"currency": "USD", "daily": True})
    store.save_subscriber(8, {"currency": "USD", "daily": False})
    before_8am_manila = datetime(2026, 9, 2, 23, 30, tzinfo=timezone.utc)  # 07:30 Manila next day
    assert bot.run_daily(service, store, sent, now=before_8am_manila) == 0
    at_8am = datetime(2026, 9, 3, 0, 5, tzinfo=timezone.utc)  # 08:05 Manila
    assert bot.run_daily(service, store, sent, now=at_8am) == 1
    assert "Good morning" in sent.messages[-1][1] and "🥇 Remitly" in sent.messages[-1][1]
    assert bot.run_daily(service, store, sent, now=at_8am + timedelta(hours=5)) == 0  # once per day
    assert bot.run_daily(service, store, sent, now=at_8am + timedelta(days=1)) == 1
    jobs = bot.run_jobs(service, store, sent, now=at_8am + timedelta(days=2))
    assert jobs == {"alerts": 0, "daily": 1}


def test_failed_send_is_not_marked_sent(service, store, fast_settings):
    store.save_subscriber(9, {"currency": "USD", "alert": {"currency": "USD", "threshold": 1.0}})
    assert bot.run_alerts(service, store, lambda c, t: False) == 0
    assert store.subscriber(9)["alert"].get("last_sent") is None


# ------------------------------------------------------------------ Telegram client


def test_telegram_client_errors_are_soft():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("sendMessage"):
            return httpx.Response(200, json={"ok": False, "description": "chat not found"})
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5}]})
        return httpx.Response(502, text="bad gateway")

    tg = bot.TelegramClient("123:abc", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tg.enabled()
    assert tg.send(1, "hi") is False
    assert tg.get_updates(None) == [{"update_id": 5}]
    with pytest.raises(RuntimeError, match="non-JSON"):
        tg.call("setWebhook", url="x")
    assert not bot.TelegramClient("").enabled()
    with pytest.raises(RuntimeError, match="not set"):
        bot.TelegramClient("").call("getMe")


def test_format_market_handles_missing_rate():
    assert "could not load" in bot.format_market(bot.MarketSummary("USD", None))
    empty = bot.Comparison("USD", 500, [])
    assert "no provider quotes" in bot.format_comparison(empty)
