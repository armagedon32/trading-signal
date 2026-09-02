# Trading Signal Dashboard

Python + Streamlit dashboard that pulls market data (Yahoo Finance / Twelve Data), computes an SMA-crossover signal with supporting indicators, and tracks how its Up/Down predictions actually resolve.

> This is a demo. Signals are experimental, data is delayed, and nothing here is financial advice.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Optional: copy `.env.example` to `.env` and fill in your keys so you don't have to paste them in the sidebar every time.

## Project layout

```
app.py                     Streamlit UI only (widgets, charts, session state)
trading_signal/
  config.py                option tables, expiry/interval maps, lookback limits
  data.py                  yfinance + Twelve Data loaders, Telegram sender (no exceptions leak; errors are returned)
  indicators.py            SMA/EMA, Wilder RSI, MACD, Bollinger %B, Wilder ADX, backtest + summary
  models.py                Trend / ML Lite / ML Advanced / linear forecast, with walk-forward accuracy
  tracker.py               prediction records, expiry-based resolution, stats, JSON persistence
  market.py                FX / US-equity / crypto market-hours helper
tests/                     pytest suite (pure functions + headless Streamlit UI tests)
```

The `trading_signal` package has no Streamlit dependency, so it can be imported from notebooks or scripts.

## Features

**Dashboard (Yahoo Finance)**
- Watchlist and multi-timeframe (5m / 15m / 1h / 1d) SMA crossover signals with RSI and ADX
- Price chart with SMA lines and crossover markers
- Experimental least-squares next-bar forecast with a walk-forward hit-rate
- Backtest summary: strategy vs buy & hold, max drawdown, exposure, per-trade and in-market win rate
- Telegram alert once per new bar on the alert timeframe (delivery is verified, failures are shown)
- Auto-refresh every 60 s (fragment based; session state is preserved)

**Professional Signal (Twelve Data)**
- Pick an asset and expiry (1m / 5m / 1h), get an **UP (Call)** / **DOWN (Put)** prediction from
  - *Trend*: EMA(12/26) spread direction, conviction from Wilder ADX
  - *ML Lite*: standardised logistic regression
  - *ML Advanced*: XGBoost (falls back to RandomForest if xgboost is unavailable)
- Every learned model shows its **walk-forward (out-of-sample) accuracy** next to the in-sample confidence
- Log manual CALL / PUT trades to track your own hit-rate alongside the model
- Live countdown from the moment you click, live price vs entry, and automatic resolution against the bar that contains the expiry time (never the entry bar)
- Win / loss / tie stats, win-rate by confidence bucket, full history table
- History is persisted to `.data/predictions.json` (configurable via `TRADING_SIGNAL_HISTORY`) so a browser reload doesn't wipe it

## Configuration

| Setting | Where | Notes |
| --- | --- | --- |
| `TWELVE_DATA_API_KEY` | `.env`, `st.secrets`, or sidebar | Free tier: 8 credits/min, 800/day. Each prediction/refresh uses one credit per symbol+interval per minute (responses are cached for 60 s). |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `.env` or sidebar | Create a bot with BotFather; get the chat id from a `getUpdates` call. |
| `TRADING_SIGNAL_HISTORY` | env | Path of the prediction history JSON. |

### Data limits worth knowing
- Yahoo serves intraday history only for a trailing window (about 7 days for 1m, 60 days for 5m/15m, 730 days for 1h). The lookback is clamped automatically per interval.
- Yahoo intraday data is delayed for many exchanges; the dashboard shows the age of the last bar.
- Twelve Data timestamps are requested in UTC. Market status for FX follows the Sunday 17:00 – Friday 17:00 New York session.

## Development

```bash
pip install -r requirements-dev.txt
pytest                      # ~65 tests, runs offline (all network calls are mocked)
python -m pyflakes app.py trading_signal tests
```

## Notes on methodology
- The bar being predicted is never part of the training set.
- Indicators use conventional definitions (Wilder RSI/ADX) so they match common charting platforms; RSI is 100 (not NaN) in a pure up-move.
- Backtest is long-only, next-bar execution, no fees or slippage.
- Prediction outcomes are measured at expiry (bar containing the expiry instant). If no bar covers the expiry (market closed / feed gap) the record settles on the last available bar after a grace period and is annotated accordingly.

---

## Also in this repository: PadalaCompare (`padala-compare/`)

A separate, self-contained project: a remittance-rate comparison website + Telegram bot for
Filipinos abroad ("which app gives the most pesos today?"), monetised with referral links.
It shares no code with the trading dashboard. See [`padala-compare/README.md`](padala-compare/README.md)
for setup, free hosting and the Telegram bot.
