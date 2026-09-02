# PadalaCompare

**"Magkano ang matatanggap?"** – a small website + Telegram bot for Filipinos abroad.
Type the amount you want to send home; it shows which remittance app (Remitly, Wise,
Instarem, Western Union, Xoom, PayPal, WorldRemit …) delivers the **most pesos today**,
whether the dollar/dirham/pound is high or low compared with the last 30 days, and it
can message you on Telegram when the rate hits your target.

It earns money through **referral links**: when a visitor clicks "Send with Remitly"
and becomes a new customer, Remitly (or Wise, etc.) pays you a referral fee.
Visitors pay nothing extra, and the ranking is never influenced by the links.

Running cost: **₱0** (free hosting, free Telegram bot, free rate sources). A domain
name (~₱600/year) is optional but recommended.

> This folder is a self-contained project. It lives inside the `trading-signal`
> repository only because that is the repo this workspace can push to – it shares
> no code with the trading dashboard. All commands below are run from inside
> `padala-compare/`.

---

## What is inside

| Part | What it does |
|---|---|
| `app/rates.py` | Fetches quotes from the Wise comparison feed and Remitly's calculator, plus mid-market rates (Wise live, Frankfurter/ECB, er-api). Every source can fail without breaking the page. |
| `app/service.py` | Caches results in a JSON file (10 min for quotes, 1 h for history), serves the last good copy if a provider is down, rescales quotes to the exact amount. |
| `app/main.py` | The website (FastAPI + plain HTML, fast and Google-friendly), JSON API, `/go/<provider>` referral redirects with click counting, Telegram webhook, admin endpoints. |
| `app/bot.py` | Telegram commands `/rate`, `/compare`, `/alert`, `/daily`; the alert and morning-digest jobs; a long-polling mode for running on your own PC. |
| `app/chart.py` | 30-day rate chart drawn as SVG on the server (no JavaScript). |
| `data/manual_rates.json` | Optional hand-typed rates for providers without a public calculator. |
| `tests/` | 54 tests using real provider payloads recorded on 2026-09-02; no network needed. |

Supported sending currencies: USD, AED, SAR, CAD, GBP, AUD, SGD, JPY, EUR, HKD.

Checked live on 2026-09-02: the Wise comparison feed returns 5–9 providers for USD, GBP, CAD,
AUD, SGD, JPY, EUR and HKD but **nothing for AED and SAR**; Remitly's calculator answers for
USD, GBP, CAD, AUD, JPY, EUR and AED, **not for SAR** (and SGD gave an error at test time).
So AED shows Remitly + manual rates, and SAR relies on the rates you type into
`data/manual_rates.json` (two are included as examples – update them).

---

## Run it on your computer (5 minutes)

**Easiest way – no typing:**

1. Install Python 3.11+ from https://www.python.org/downloads/ (Windows: tick **"Add python.exe to PATH"** in the installer).
2. Get the code: https://github.com/armagedon32/trading-signal → green **Code** button → **Download ZIP** → unzip.
3. Open the `padala-compare` folder inside it and double-click **`run.bat`** (Windows) or run **`./run.sh`** (macOS/Linux).
   The first start installs a few packages (about a minute); after that it opens http://localhost:8000 in your browser.

**Command-line way:**

```bash
git clone https://github.com/armagedon32/trading-signal.git
cd trading-signal/padala-compare
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # edit later
uvicorn app.main:app --reload
```

Open http://localhost:8000 – rates load live from the providers.

Run the tests: `./run.sh test` (or `pip install -r requirements-dev.txt && python -m pytest`).

### 10-minute test checklist

| # | Do this | You should see |
|---|---|---|
| 1 | Open http://localhost:8000 | A table for 500 USD, Remitly / Wise / Instarem … ranked by pesos, and a 30-day chart. Check the numbers against https://www.remitly.com and https://wise.com/ph/currency-converter – they should match to within a few pesos (rates move all day). |
| 2 | Change the amount to 1000 and press Compare | Numbers roughly double; the order may change. |
| 3 | Pick AED, GBP, JPY in the dropdown | Each corridor loads its own providers. AED and SAR show fewer (see the note above). |
| 4 | Click **Open** next to any provider | A new tab opens on that provider's website. (Once you add your referral link to `.env`, this is where it goes.) |
| 5 | Open http://localhost:8000/api/compare?cur=USD&amount=500 | The same data as JSON – handy to confirm the site is fetching live. |
| 6 | Turn off your Wi-Fi and reload the page | The last rates still show, marked "may be outdated". Nothing crashes. |
| 7 | *(optional)* Create a bot with @BotFather, put the token in `.env`, double-click `bot.bat` (or `./run.sh bot`), then message the bot `/rate USD` and `/compare USD 500` | Replies within a second; `/alert USD 1` makes it message you at the next 15-minute job run. |

---

## Put it online for free (Render)

1. Go to https://render.com → **New → Blueprint** → pick the `trading-signal` repo.
   `render.yaml` (in this folder) sets everything up on the free plan – choose it as the blueprint file
   (`padala-compare/render.yaml`). It already points Render at this sub-folder.
2. In the Render dashboard set the environment variables `SITE_URL` (your Render URL, e.g.
   `https://padala-compare.onrender.com`) and, later, your affiliate links and Telegram token.
3. Visit the URL. Done.

Other free options that work the same way: Fly.io (`Dockerfile`), Railway/Koyeb (`Procfile`) – set the
project root to `padala-compare`.
Free Render instances sleep after 15 minutes without visitors; the GitHub Action below wakes it every 15
minutes, which also keeps the rates fresh.

> Note on data: the free tier has no permanent disk, so subscriber data lives in `.data/padala.json`
> and is lost on redeploy. Either add a free persistent disk / volume and set `PADALA_DATA_DIR`,
> or accept re-subscriptions early on. (Fly.io volumes and Koyeb are free options with disks.)

---

## Telegram bot (free)

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts. Copy the token.
2. Put `TELEGRAM_BOT_TOKEN=...` and `TELEGRAM_BOT_USERNAME=YourBotName` in `.env` (or Render env vars).
3. Set `ADMIN_TOKEN` to any long random text (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
4. Tell Telegram where your site is (one time, after deploying):
   `curl -X POST -H "X-Admin-Token: <your ADMIN_TOKEN>" https://<your-site>/admin/webhook`
5. Alerts and the 8 AM Manila digest are sent when something calls `POST /admin/jobs`.
   The included GitHub Action (`.github/workflows/padala-jobs.yml` at the repo root) does this every
   15 minutes for free – add repository secrets `PADALA_SITE_URL` and `PADALA_ADMIN_TOKEN`.
   Any free cron service (cron-job.org) works too.

No public URL yet? Run the bot from your own PC instead: `python -m app.bot` (long polling, jobs run every 15 min while it is open).

Commands users can send:

```
/rate USD            rate now + high/low badge
/compare USD 500     which app gives the most pesos
/alert USD 58.5      message me when 1 USD >= 58.50 pesos
/alert off
/daily on            morning summary, 8 AM Manila
/stop
```

---

## Turning on the income (affiliate links)

An **affiliate / referral link** is a normal link with your ID in it. When someone signs up through it and sends money, the company pays you a small fee. Apply once the site is live (they like to see a real website):

| Provider | Where to apply | Typical payout* |
|---|---|---|
| Remitly | Impact network → search "Remitly" (or Remitly "Refer a friend" from inside the app to start) | ~US$5–20 per new customer's first transfer |
| Wise | https://wise.com/partnerwise (affiliate) or your in-app invite link | varies; often a fixed fee per new user who transfers |
| WorldRemit | Impact / CJ – search "WorldRemit" | fixed fee per first transfer |
| Instarem, Xoom, MoneyGram, Western Union | Impact / CJ / Rakuten networks | varies |

\*Figures reported publicly by affiliate networks in 2025; check the exact terms when you apply.

Paste each link into `.env` (`AFF_REMITLY=https://...`). Until a link is set, the button opens the provider's normal website. The footer already carries the required disclosure.

**Rule of thumb for $3–5/day:** about 10–15 first-transfers per month through your links, i.e. roughly 4,000–8,000 visitors a month. Realistic timeline: month 1 build and publish, month 2–3 first payouts, month 4–6 reach the target – *if* you promote it (see below).

---

## Getting visitors (this is the real work)

* Post the daily "USD is high today – best app is X" screenshot in OFW Facebook groups, TikTok, Reddit r/phinvest / r/OFW. Every page has share buttons that pre-write the text.
* Each currency has its own page (`/?cur=AED`), which Google can index. Titles are already written as questions people search ("AED to PHP today: best remittance rate…").
* Ask friends abroad to set a Telegram alert. Every alert message ends with the best-app link.
* Keep the page honest and boring: no hype, no "guaranteed", show the disclosure.

---

## Admin & API

* `GET /api/compare?cur=USD&amount=500` – JSON of the ranking. `GET /api/rate?cur=USD` – market summary.
* `GET /admin/stats?token=…` – subscribers, alerts, clicks per provider, which affiliate links are set.
* `POST /admin/refresh` – re-fetch every currency now. `POST /admin/jobs` – run alerts + digest.
* Everything is cached in `.data/padala.json`; delete the file to start fresh.

## Data sources & honesty notes

* Wise comparison feed and Remitly calculator are the endpoints behind their public web calculators, not documented APIs. They can change without notice – the app degrades to whatever is still working and marks results "may be outdated". Re-check quotes on the provider sites from time to time.
* Promotional first-transfer rates are marked as such and the regular amount is shown next to them.
* This site never touches anyone's money; it only compares published rates.
