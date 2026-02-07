# Trading Signal Dashboard

Python + Streamlit dashboard that pulls live-ish market data (via yfinance) and computes an SMA crossover signal.

## Quick start
1. Create and activate a virtual environment
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `streamlit run app.py`

## Features
- SMA crossover signal with chart
- Multi-ticker watchlist summary
- Multi-timeframe signals (5m/15m/1h/1d)
- Telegram alerts every bar while BUY/SELL (select timeframe)
- Experimental forecast of next-bar direction
- Professional signal (Twelve Data + ML lite, Up/Down with confidence)
- Accuracy tracker for predictions (resolved after expiry)
- Backtest summary (total return, max drawdown, win rate)

## Telegram setup
1. Create a bot with BotFather and copy the token
2. Get your chat id (e.g., from a `getUpdates` call)
3. Paste token + chat id in the sidebar and enable alerts

## Twelve Data setup
1. Create a Twelve Data API key
2. Option A (temporary): paste the key in the sidebar
3. Option B (persistent): create a `.env` file in this folder with:
   `TWELVE_DATA_API_KEY=your_key_here`
4. Use the Professional Signal page to select asset + expiry and get a prediction

## Notes
- yfinance provides delayed market data for many exchanges.
- Forecasts are experimental and can be wrong.
- This is a demo signal; not financial advice.
