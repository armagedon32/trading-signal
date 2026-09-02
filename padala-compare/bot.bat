@echo off
REM Starts the Telegram bot on this computer (no website hosting needed).
REM 1) Create a bot with @BotFather in Telegram and copy the token.
REM 2) Open .env in Notepad and put it after TELEGRAM_BOT_TOKEN=
REM 3) Double-click this file, then message your bot:  /rate USD
cd /d "%~dp0"
if not exist .venv ( echo Run run.bat once first. & pause & exit /b 1 )
call .venv\Scripts\activate.bat
python -m app.bot
pause
