@echo off
REM PadalaCompare - one-click local start for Windows.
REM Double-click this file. It sets everything up the first time (takes ~1 minute),
REM then starts the website at http://localhost:8000
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found.
  echo Install it from https://www.python.org/downloads/  and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" 2>nul
if errorlevel 1 (
  echo Your Python is too old. Please install Python 3.11 or newer from https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating a private Python environment ^(first time only^)...
  python -m venv .venv
  if errorlevel 1 ( pause & exit /b 1 )
)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo Installing the requirements failed. Check your internet connection and run this file again.
  pause
  exit /b 1
)
if not exist .env copy .env.example .env >nul

echo.
echo  ============================================================
echo   PadalaCompare is starting.
echo   Your browser will open  http://localhost:8000  in a moment.
echo   Keep this window open. Press Ctrl+C here to stop.
echo  ============================================================
echo.
start "" cmd /c "timeout /t 4 >nul & start http://localhost:8000"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
