"""Telegram bot: rate on demand, "dollar is high" alerts, daily digest.

Commands
--------
/start                 welcome + how it works
/rate [CUR]            mid-market rate now and where it sits in the last 30 days
/compare [CUR] [amt]   which app gives the most pesos for that amount
/alert CUR RATE        message me when the mid-market rate is >= RATE
/alert off             remove my alert
/daily on|off          one message every morning (Manila time)
/help                  list of commands

The same handler works for webhooks (FastAPI route) and long polling
(``python -m app.bot``) - both just call :func:`handle_update`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from .config import CURRENCIES, DEFAULT_CURRENCY, PROVIDERS, SITE_NAME, SITE_URL, TELEGRAM_BOT_TOKEN
from .rates import Comparison, MarketSummary, fmt_amount
from .service import RateService
from .store import JsonStore

log = logging.getLogger(__name__)

MANILA = timezone(timedelta(hours=8))
DAILY_HOUR_MANILA = 8  # 08:00 Manila = 00:00 UTC

Sender = Callable[[int, str], bool]


# --------------------------------------------------------------------------- Telegram API


class TelegramClient:
    def __init__(self, token: str = TELEGRAM_BOT_TOKEN, client: httpx.Client | None = None):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = client or httpx.Client(timeout=15)

    def enabled(self) -> bool:
        return bool(self.token)

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        try:
            response = self.client.post(f"{self.base}/{method}", json=params)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Telegram {method}: {type(exc).__name__}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Telegram {method}: HTTP {response.status_code}, non-JSON body") from exc
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description', 'unknown error')}")
        return data

    def send(self, chat_id: int, text: str) -> bool:
        try:
            self.call(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except RuntimeError as exc:
            log.warning("send to %s failed: %s", chat_id, exc)
            return False

    def set_webhook(self, url: str, secret: str) -> dict[str, Any]:
        return self.call("setWebhook", url=url, secret_token=secret, allowed_updates=["message"])

    def delete_webhook(self) -> dict[str, Any]:
        return self.call("deleteWebhook")

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params).get("result", [])


# --------------------------------------------------------------------------- formatting


def fmt_money(value: float, decimals: int = 2) -> str:
    return f"{value:,.{decimals}f}"


def format_market(m: MarketSummary) -> str:
    cur = CURRENCIES[m.currency]
    if m.rate is None:
        return f"Sorry, I could not load the {m.currency}/PHP rate right now."
    lines = [f"<b>1 {m.currency} = ₱{m.rate:,.4g}</b>  {cur.flag}"]
    if m.high is not None and m.low is not None:
        lines.append(f"Last 30 days: low ₱{m.low:,.4g} · high ₱{m.high:,.4g}")
    p = m.percentile
    if p is not None:
        pct = round(p * 100)
        if m.label == "high":
            lines.append(f"🔥 <b>Rate is HIGH</b> – better than {pct}% of the last 30 days. Good time to send.")
        elif m.label == "low":
            lines.append(f"🧊 Rate is low – only better than {pct}% of the last 30 days. Wait if you can.")
        else:
            lines.append(f"Rate is normal – better than {pct}% of the last 30 days.")
    if m.stale:
        lines.append("(rate may be a few hours old)")
    return "\n".join(lines)


def format_comparison(c: Comparison, *, limit: int = 5) -> str:
    cur = CURRENCIES[c.currency]
    if not c.quotes:
        return f"Sorry, no provider quotes for {c.currency} right now. Try again in a few minutes."
    lines = [f"<b>Send {cur.symbol}{fmt_money(c.amount, 0)} {c.currency} → PHP</b>"]
    medals = ["🥇", "🥈", "🥉"]
    for i, q in enumerate(c.quotes[:limit]):
        medal = medals[i] if i < len(medals) else "•"
        promo = " (new-customer promo)" if q.promo else ""
        fee = f", fee {q.fee:g}" if q.fee else ", no fee"
        lines.append(f"{medal} {q.provider_name}: <b>₱{fmt_money(q.received)}</b>{promo}{fee}")
    if c.spread > 0:
        lines.append(f"\nDifference best vs worst shown: <b>₱{fmt_money(c.spread)}</b>")
    best = c.best
    if best is not None:
        provider = PROVIDERS.get(best.provider_key)
        link = f"{SITE_URL}/go/{best.provider_key}?cur={c.currency}"
        name = provider.name if provider else best.provider_name
        lines.append(f"👉 Open {name}: {link}")
    lines.append(f"Full table: {SITE_URL}/?cur={c.currency}&amount={fmt_amount(c.amount)}")
    if c.stale:
        lines.append("(rates may be a few minutes old)")
    return "\n".join(lines)


HELP_TEXT = (
    f"<b>{SITE_NAME} bot</b> – padala rates without the guesswork.\n\n"
    "/rate USD – rate now and whether it is high or low\n"
    "/compare USD 500 – which app gives the most pesos\n"
    "/alert USD 58.5 – tell me when 1 USD ≥ ₱58.50\n"
    "/alert off – remove my alert\n"
    "/daily on – morning rate message (8 AM Manila)\n"
    "/daily off – stop the morning message\n"
    "/help – this list\n\n"
    "Currencies: " + ", ".join(CURRENCIES) + "\n"
    f"Website: {SITE_URL}"
)


# --------------------------------------------------------------------------- command handling


def _parse_currency(token: str | None, default: str) -> str | None:
    if token is None:
        return default
    code = token.strip().upper().lstrip("/")
    return code if code in CURRENCIES else None


def handle_message(text: str, chat_id: int, service: RateService, store: JsonStore) -> str:
    """Return the reply for one incoming text message.  Pure w.r.t. Telegram."""
    parts = text.strip().split()
    if not parts:
        return HELP_TEXT
    command = parts[0].lower().split("@")[0]
    args = parts[1:]
    sub = store.subscriber(chat_id) or {}
    default_cur = sub.get("currency", DEFAULT_CURRENCY)

    if command in ("/start", "/help"):
        if command == "/start" and not sub:
            store.save_subscriber(chat_id, {"currency": DEFAULT_CURRENCY, "daily": False})
        return HELP_TEXT

    if command == "/rate":
        cur = _parse_currency(args[0] if args else None, default_cur)
        if cur is None:
            return f"I don't know that currency. Try one of: {', '.join(CURRENCIES)}"
        store.save_subscriber(chat_id, {**sub, "currency": cur})
        return format_market(service.market(cur))

    if command == "/compare":
        cur = _parse_currency(args[0] if args else None, default_cur)
        if cur is None:
            return f"I don't know that currency. Try one of: {', '.join(CURRENCIES)}"
        amount = args[1] if len(args) > 1 else CURRENCIES[cur].default_amount
        cur, amt = service.normalise(cur, amount)
        store.save_subscriber(chat_id, {**sub, "currency": cur})
        return format_comparison(service.compare(cur, amt))

    if command == "/alert":
        if args and args[0].lower() in ("off", "stop", "none"):
            sub.pop("alert", None)
            store.save_subscriber(chat_id, sub)
            return "Alert removed."
        if len(args) == 1:  # "/alert 58.5" -> use saved currency
            args = [default_cur, args[0]]
        if len(args) < 2:
            return "Usage: /alert USD 58.5  (message me when 1 USD ≥ ₱58.50)\nor /alert off"
        cur = _parse_currency(args[0], default_cur)
        if cur is None:
            return f"I don't know that currency. Try one of: {', '.join(CURRENCIES)}"
        try:
            threshold = float(args[1].replace(",", ""))
        except ValueError:
            return "The rate must be a number, e.g. /alert USD 58.5"
        if threshold <= 0:
            return "The rate must be above zero."
        sub.update({"currency": cur, "alert": {"currency": cur, "threshold": threshold, "last_sent": None}})
        store.save_subscriber(chat_id, sub)
        market = service.market(cur)
        now = f" Right now 1 {cur} = ₱{market.rate:,.4g}." if market.rate else ""
        return f"OK! I will message you when 1 {cur} ≥ ₱{threshold:g}.{now}"

    if command == "/daily":
        on = not args or args[0].lower() in ("on", "yes", "start")
        cur = _parse_currency(args[1] if len(args) > 1 else None, default_cur) or default_cur
        sub.update({"currency": cur, "daily": on})
        store.save_subscriber(chat_id, sub)
        return (
            f"Daily {cur} rate message is ON (about 8 AM Manila time). Send /daily off to stop."
            if on
            else "Daily message is OFF."
        )

    if command in ("/stop", "/unsubscribe"):
        store.remove_subscriber(chat_id)
        return "You are unsubscribed. Send /start any time to come back."

    # Plain text like "USD" or "500 USD" - be helpful.
    cur = _parse_currency(parts[-1], "")
    if cur:
        return format_market(service.market(cur))
    return HELP_TEXT


def handle_update(update: dict[str, Any], service: RateService, store: JsonStore, send: Sender) -> bool:
    """Process one Telegram update.  Returns True if a reply was sent."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return False
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text")
    if chat_id is None or not text:
        return False
    try:
        reply = handle_message(text, int(chat_id), service, store)
    except Exception:  # noqa: BLE001
        log.exception("handler crashed for chat %s", chat_id)
        reply = "Something went wrong on my side. Please try again in a minute."
    return send(int(chat_id), reply)


# --------------------------------------------------------------------------- scheduled jobs


def run_alerts(service: RateService, store: JsonStore, send: Sender, *, now: datetime | None = None) -> int:
    """Send threshold alerts.  One message per crossing per day; returns number sent."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(MANILA).date().isoformat()
    sent = 0
    markets: dict[str, MarketSummary] = {}
    for chat_id, sub in store.subscribers().items():
        alert = sub.get("alert")
        if not alert:
            continue
        cur = alert.get("currency", DEFAULT_CURRENCY)
        threshold = float(alert.get("threshold", 0))
        market = markets.setdefault(cur, service.market(cur))
        if market.rate is None or market.rate < threshold:
            if alert.get("armed") is False:
                alert["armed"] = True  # rate dropped below threshold -> re-arm
                store.save_subscriber(chat_id, sub)
            continue
        if alert.get("last_sent") == today or alert.get("armed") is False:
            continue
        cmp_ = service.compare(cur, CURRENCIES[cur].default_amount)
        best = cmp_.best
        best_line = (
            f"\nBest app right now: {best.provider_name} → {SITE_URL}/go/{best.provider_key}?cur={cur}"
            if best
            else ""
        )
        text = (
            f"🔔 <b>{cur} hit your target!</b>\n1 {cur} = ₱{market.rate:,.4g} (your alert: ≥ ₱{threshold:g})"
            f"{best_line}\nSend /alert off to stop this alert."
        )
        if send(int(chat_id), text):
            alert["last_sent"] = today
            alert["armed"] = False
            store.save_subscriber(chat_id, sub)
            sent += 1
    return sent


def run_daily(service: RateService, store: JsonStore, send: Sender, *, now: datetime | None = None, force: bool = False) -> int:
    """Send the morning digest once per Manila day after DAILY_HOUR_MANILA."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(MANILA)
    today = local.date().isoformat()
    if local.hour < DAILY_HOUR_MANILA and not force:
        return 0
    sent = 0
    cache: dict[str, str] = {}
    for chat_id, sub in store.subscribers().items():
        if not sub.get("daily"):
            continue
        if sub.get("daily_sent") == today and not force:
            continue
        cur = sub.get("currency", DEFAULT_CURRENCY)
        if cur not in cache:
            market = service.market(cur)
            cmp_ = service.compare(cur, CURRENCIES[cur].default_amount)
            cache[cur] = (
                f"☀️ <b>Good morning! {cur}/PHP today</b>\n"
                + format_market(market)
                + "\n\n"
                + format_comparison(cmp_, limit=3)
                + "\n\nSend /daily off to stop."
            )
        if send(int(chat_id), cache[cur]):
            sub["daily_sent"] = today
            store.save_subscriber(chat_id, sub)
            sent += 1
    return sent


def run_jobs(service: RateService, store: JsonStore, send: Sender, *, now: datetime | None = None) -> dict[str, int]:
    return {
        "alerts": run_alerts(service, store, send, now=now),
        "daily": run_daily(service, store, send, now=now),
    }


# --------------------------------------------------------------------------- long polling


def poll_forever(service: RateService, store: JsonStore, tg: TelegramClient, *, job_every: int = 900) -> None:
    """Run without a public URL: ask Telegram for new messages in a loop."""
    if not tg.enabled():
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first.")
    try:
        tg.delete_webhook()
    except RuntimeError as exc:
        log.warning("deleteWebhook: %s", exc)
    offset: int | None = None
    last_jobs = 0.0
    log.info("polling Telegram... press Ctrl+C to stop")
    while True:
        try:
            for update in tg.get_updates(offset, timeout=25):
                offset = int(update["update_id"]) + 1
                handle_update(update, service, store, tg.send)
        except RuntimeError as exc:
            log.warning("getUpdates failed: %s (retrying in 10 s)", exc)
            time.sleep(10)
        if time.time() - last_jobs >= job_every:
            try:
                log.info("jobs: %s", run_jobs(service, store, tg.send))
            except Exception:  # noqa: BLE001
                log.exception("scheduled jobs failed")
            last_jobs = time.time()


if __name__ == "__main__":  # python -m app.bot
    from .config import DATA_DIR, BASE_DIR

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _store = JsonStore(DATA_DIR / "padala.json")
    _service = RateService(_store, manual_path=BASE_DIR / "data" / "manual_rates.json")
    poll_forever(_service, _store, TelegramClient())
