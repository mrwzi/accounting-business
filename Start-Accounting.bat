@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.14 is required. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)

py -3.14 -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo Python 3.14 is required. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local environment...
  py -3.14 -m venv .venv
)

echo Installing required packages if needed...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Installation failed. Check your internet connection and try again.
  pause
  exit /b 1
)

echo.
set PORT=5003
echo Accounting is starting at http://127.0.0.1:5003
echo Keep this window open while using the app.
start "Accounting" http://127.0.0.1:5003
".venv\Scripts\python.exe" app.py
pause
