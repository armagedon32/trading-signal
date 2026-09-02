#!/usr/bin/env bash
# PadalaCompare - one-command local start for macOS / Linux.
#   ./run.sh        start the website at http://localhost:8000
#   ./run.sh bot    start the Telegram bot (needs TELEGRAM_BOT_TOKEN in .env)
#   ./run.sh test   run the test suite
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it from https://www.python.org/downloads/"; exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "Your Python is too old. Please install Python 3.11 or newer."; exit 1
fi

[ -d .venv ] || { echo "Creating a private Python environment (first time only)..."; python3 -m venv .venv; }
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env

case "${1:-web}" in
  bot)
    exec python -m app.bot ;;
  test)
    python -m pip install -q -r requirements-dev.txt
    exec python -m pytest ;;
  *)
    echo
    echo "  PadalaCompare is starting. Open  http://localhost:8000  in your browser."
    echo "  Press Ctrl+C to stop."
    echo
    exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 ;;
esac
