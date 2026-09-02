"""FastAPI web app: comparison page, JSON API, affiliate redirects, Telegram webhook."""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, bot
from .chart import svg_line_chart
from .rates import fmt_amount
from .config import (
    ADMIN_TOKEN,
    BASE_DIR,
    CURRENCIES,
    DATA_DIR,
    DEFAULT_CURRENCY,
    PROVIDERS,
    SITE_NAME,
    SITE_TAGLINE,
    SITE_URL,
    TELEGRAM_BOT_USERNAME,
)
from .service import RateService
from .store import JsonStore

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
MANUAL_RATES = BASE_DIR / "data" / "manual_rates.json"


def create_app(*, store: JsonStore | None = None, service: RateService | None = None, telegram: bot.TelegramClient | None = None) -> FastAPI:
    store = store or JsonStore(DATA_DIR / "padala.json")
    service = service or RateService(store, manual_path=MANUAL_RATES)
    telegram = telegram or bot.TelegramClient()

    app = FastAPI(title=SITE_NAME, version=__version__, docs_url="/api/docs", redoc_url=None)
    app.state.store = store
    app.state.service = service
    app.state.telegram = telegram
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.globals.update(
        site_name=SITE_NAME,
        site_url=SITE_URL,
        tagline=SITE_TAGLINE,
        currencies=CURRENCIES,
        providers=PROVIDERS,
        bot_username=TELEGRAM_BOT_USERNAME,
        year=datetime.now(timezone.utc).year,
    )
    templates.env.filters["money"] = lambda v, d=2: f"{v:,.{d}f}" if v is not None else "—"
    templates.env.filters["rate4"] = lambda v: f"{v:,.4g}" if v is not None else "—"

    def require_admin(x_admin_token: str | None = Header(default=None), token: str | None = Query(default=None)) -> None:
        supplied = x_admin_token or token or ""
        if not ADMIN_TOKEN or not hmac.compare_digest(supplied, ADMIN_TOKEN):
            raise HTTPException(status_code=403, detail="admin token required")

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, cur: str = DEFAULT_CURRENCY, amount: str | None = None):
        currency, amt = service.normalise(cur, amount)
        comparison = service.compare(currency, amt)
        market = service.market(currency)
        chart = svg_line_chart(market.history, label=f"{currency}/PHP last 30 days")
        best = comparison.best
        title = f"{currency} to PHP today: best remittance rate for {CURRENCIES[currency].symbol}{amt:,.0f}"
        description = (
            f"Compare Remitly, Wise, WorldRemit and more. Send {amt:,.0f} {currency} and get up to "
            f"₱{best.received:,.0f} with {best.provider_name}." if best else SITE_TAGLINE
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "currency": CURRENCIES[currency],
                "amount": amt,
                "comparison": comparison,
                "market": market,
                "chart": chart,
                "page_title": title,
                "page_description": description,
                "canonical": f"{SITE_URL}/?cur={currency}&amount={fmt_amount(amt)}",
                "share_text": quote_plus(
                    f"{currency} is at ₱{market.rate:,.4g} today. Best app for {amt:,.0f} {currency}: "
                    f"{best.provider_name} (₱{best.received:,.0f}). {SITE_URL}/?cur={currency}"
                    if best and market.rate else SITE_URL
                ),
            },
        )

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request):
        return templates.TemplateResponse(request, "about.html", {"page_title": f"About {SITE_NAME}"})

    @app.get("/go/{provider_key}")
    def go(provider_key: str, cur: str = DEFAULT_CURRENCY):
        provider = PROVIDERS.get(provider_key)
        if provider is None:
            raise HTTPException(status_code=404, detail="unknown provider")
        currency, _ = service.normalise(cur, None)
        store.record_click(provider_key, currency)
        return RedirectResponse(provider.affiliate_url, status_code=302)

    # -------------------------------------------------------------------- API
    @app.get("/api/compare")
    def api_compare(cur: str = DEFAULT_CURRENCY, amount: str | None = None):
        currency, amt = service.normalise(cur, amount)
        return JSONResponse(service.compare(currency, amt).to_dict())

    @app.get("/api/rate")
    def api_rate(cur: str = DEFAULT_CURRENCY):
        currency, _ = service.normalise(cur, None)
        return JSONResponse(service.market(currency).to_dict())

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, "time": datetime.now(timezone.utc).isoformat()}

    # ------------------------------------------------------------------ SEO
    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots():
        return f"User-agent: *\nAllow: /\nDisallow: /go/\nDisallow: /admin/\nSitemap: {SITE_URL}/sitemap.xml\n"

    @app.get("/sitemap.xml")
    def sitemap():
        urls = [f"{SITE_URL}/", f"{SITE_URL}/about"] + [
            f"{SITE_URL}/?cur={code}&amount={c.default_amount}" for code, c in CURRENCIES.items()
        ]
        body = "".join(f"<url><loc>{u.replace('&', '&amp;')}</loc><changefreq>daily</changefreq></url>" for u in urls)
        return Response(
            f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>',
            media_type="application/xml",
        )

    # ------------------------------------------------------------- Telegram
    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
        if not ADMIN_TOKEN or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", ADMIN_TOKEN):
            raise HTTPException(status_code=403, detail="bad secret")
        update = await request.json()
        bot.handle_update(update, service, store, telegram.send)
        return {"ok": True}

    # ---------------------------------------------------------------- admin
    @app.post("/admin/jobs", dependencies=[Depends(require_admin)])
    def admin_jobs():
        """Call this from a free cron service every 15 min: sends alerts + daily digest."""
        return bot.run_jobs(service, store, telegram.send)

    @app.post("/admin/refresh", dependencies=[Depends(require_admin)])
    def admin_refresh():
        return service.refresh_all()

    @app.post("/admin/webhook", dependencies=[Depends(require_admin)])
    def admin_set_webhook():
        if not telegram.enabled():
            raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN not set")
        return telegram.set_webhook(f"{SITE_URL}/telegram/webhook", ADMIN_TOKEN)

    @app.get("/admin/stats", dependencies=[Depends(require_admin)])
    def admin_stats():
        subs = store.subscribers()
        return {
            "subscribers": len(subs),
            "with_alert": sum(1 for s in subs.values() if s.get("alert")),
            "daily": sum(1 for s in subs.values() if s.get("daily")),
            "clicks": store.click_stats(),
            "affiliate_links_set": {k: p.has_affiliate for k, p in PROVIDERS.items()},
        }

    return app


app = create_app()
