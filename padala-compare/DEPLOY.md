# Put PadalaCompare online (free)

Pick **one** host. All three read the code straight from GitHub and redeploy when you push.
Checked on 2026-09-03 – hosting plans change, so glance at the pricing page before you sign up.

| Host | Cost to test | Card needed? | Sleeps when idle? | Verdict |
|---|---|---|---|---|
| **Render** | ₱0, permanent free plan (750 h/month) | No for the free web service (some accounts are asked for one) | Yes – after 15 min; first visitor waits ~1 min | **Start here** – one click with the included `render.yaml` |
| **Railway** | $5 one-time trial credit (30 days), then a $1/month Free plan | Card usually asked at sign-up | No | Fine for a 2–4 week test; may cost ₱300/month after |
| **Koyeb** | ₱0, one free web service | Usually no | Yes – after 1 h idle | Good backup; the `Dockerfile` works there |

**Yes, Railway works** – but its free part is a *trial*. Railway's $5 credit lasts 30 days; after that you drop to a "Free" plan with only $1 of usage per month, which this app (0.5 GB RAM idle ≈ $2–3/month) will exceed, so Railway would then stop it or ask for the $5/month Hobby plan. That is why Render is the first recommendation for a ₱0 test.

---

## Option A – Render (recommended, ₱0)

1. Merge PR #1 first, or deploy from the `arena/01a0622e-trading-signal` branch – both contain the `render.yaml` at the repo root.
2. Go to https://dashboard.render.com/register and sign up with your **GitHub** account.
3. Click **New +** → **Blueprint**.
4. Click **Connect** next to `armagedon32/trading-signal`. (If it is not listed: **Configure account** → give Render access to that repo.)
5. Blueprint name: anything. Branch: `main` (after merging) or `arena/01a0622e-trading-signal`. Leave **Blueprint Path** as `render.yaml`.
6. Click **Deploy Blueprint**. Render installs Python, runs `pip install`, starts the app – 2–3 minutes.
7. Open the service → the URL looks like `https://padala-compare.onrender.com`. That is your live site.

Nothing else is required. The public URL, the `ADMIN_TOKEN` secret and the health check are set up by the blueprint.

Later, in the service's **Environment** tab, add:
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME` – when you create the bot.
- `AFF_REMITLY`, `AFF_WISE`, … – when your referral links are approved.

Things to know about Render's free plan:
- After 15 idle minutes the site sleeps; the next visitor sees a loading page for about a minute. The GitHub Action in this repo (`.github/workflows/padala-jobs.yml`) pings it every 15 minutes once you add the `PADALA_SITE_URL` / `PADALA_ADMIN_TOKEN` secrets, which mostly prevents that.
- Files written by the app (`.data/padala.json` – cache, Telegram subscribers, click counts) are lost on every restart. Rates simply reload; subscribers would need to `/start` again. For a real launch, add a persistent disk ($1/month) or move to Koyeb/Fly.io, which have free volumes.

---

## Option B – Railway (good for a few weeks)

1. https://railway.com → **Login** with GitHub. Complete the "verify" step it offers (verified accounts get full network access; unverified "limited" trials may block outbound calls to the rate providers).
2. **New Project** → **Deploy from GitHub repo** → `armagedon32/trading-signal`.
3. Railway creates one service. Open it → **Settings**:
   - **Root Directory**: `padala-compare` ← *this is the one thing you must set.*
   - Builder: Railway finds the `Dockerfile` in that folder and uses it automatically; nothing else to configure.
4. **Settings → Networking → Generate Domain** → you get `https://<something>.up.railway.app`. The app reads `RAILWAY_PUBLIC_DOMAIN` by itself, so links and the sitemap are correct.
5. **Variables** tab → add `ADMIN_TOKEN` = any long random text (needed for `/admin/*` and the Telegram webhook). Optional: Telegram and `AFF_*` variables as above.
6. Optional: **Volumes** → add a volume mounted at `/data` so subscribers and click counts survive redeploys (`PADALA_DATA_DIR` already points there in the Dockerfile).

The `railway.toml` file in `padala-compare/` is only a hint for older Railway setups; the dashboard settings above are what count. (Railway's config-as-code files are deprecated and stop being read on 2026-12-01.)

---

## Option C – Koyeb (₱0, alternative)

1. https://app.koyeb.com → sign up with GitHub → **Create Web Service** → **GitHub** → pick the repo and branch.
2. Builder: **Dockerfile**. **Work directory**: `padala-compare`.
3. Instance: **Free**. Region: Frankfurt or Washington (only choices on the free plan).
4. Exposed port: 8000 (Koyeb also injects `PORT`; the Dockerfile handles both).
5. Environment variables: `ADMIN_TOKEN` (required for admin/bot), Telegram and `AFF_*` optional. The public URL is read from `KOYEB_PUBLIC_DOMAIN` automatically.

---

## After it is online – 5-minute check

1. Open `https://<your-url>/` – the USD table should fill with today's live quotes within ~5 seconds on first load (later loads are instant from the cache).
2. `https://<your-url>/api/rate?cur=USD` – JSON with `"rate_source": "wise"` (or `frankfurter`) proves the host can reach the rate providers.
3. Switch to AED, GBP, JPY; change the amount; click **Open** on a provider.
4. `https://<your-url>/admin/stats?token=<your ADMIN_TOKEN>` – shows subscribers/clicks and which affiliate links are set.
5. Telegram (optional): after setting `TELEGRAM_BOT_TOKEN`, run once
   `curl -X POST -H "X-Admin-Token: <ADMIN_TOKEN>" https://<your-url>/admin/webhook`
   then message your bot `/rate USD`. Add the two GitHub secrets (`PADALA_SITE_URL`, `PADALA_ADMIN_TOKEN`) so alerts and the 8 AM digest run every 15 minutes.

If something fails, the host's **Logs** tab shows the reason; the app prints a warning line for every provider it could not reach.
