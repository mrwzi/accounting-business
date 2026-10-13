@echo off
setlocal
cd /d "%~dp0"
title Accounting Setup

call :find_python
if defined PYTHON_EXE goto python_ready

echo.
echo Accounting needs Python 3.14, but it is not installed on this computer.
echo Python is required because the accounting engine was built for version 3.14.
echo.
set "INSTALL_PYTHON="
set /p "INSTALL_PYTHON=Install Python 3.14 automatically now? [Y/N]: "
if /I not "%INSTALL_PYTHON%"=="Y" goto install_declined

winget --version >nul 2>nul
if errorlevel 1 goto manual_install

echo.
echo Installing Python 3.14 from the official Windows package source...
winget install --id Python.Python.3.14 --exact --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto install_failed

call :find_python
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python314\python.exe"
if not defined PYTHON_EXE goto restart_needed

:python_ready
echo.
echo Using Python: %PYTHON_EXE%
if not exist ".venv\Scripts\python.exe" (
  echo Creating the private Accounting environment...
  "%PYTHON_EXE%" -m venv .venv
  if errorlevel 1 goto environment_failed
)

echo Installing required packages if needed...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto packages_failed

if "%ACCOUNTING_SETUP_ONLY%"=="1" exit /b 0

echo.
set PORT=5003
echo Accounting is starting at http://127.0.0.1:5003
echo Keep this window open while using the app.
start "Accounting" http://127.0.0.1:5003
".venv\Scripts\python.exe" app.py
echo.
echo Accounting has stopped.
pause
exit /b

:find_python
set "PYTHON_EXE="
if "%ACCOUNTING_FORCE_NO_PYTHON%"=="1" exit /b
for /f "usebackq delims=" %%P in (`py -3.14 -c "import sys; print(sys.executable)" 2^>nul`) do set "PYTHON_EXE=%%P"
if defined PYTHON_EXE exit /b
for /f "usebackq delims=" %%P in (`python -c "import sys; print(sys.executable if sys.version_info[:2] == (3, 14) else '')" 2^>nul`) do set "PYTHON_EXE=%%P"
exit /b

:install_declined
echo.
echo Nothing was installed. You can run this file again whenever you are ready.
pause
exit /b 1

:manual_install
echo.
echo Windows Package Manager is unavailable, so automatic installation cannot continue.
echo Opening the official Python download page. Install Python 3.14, then run this file again.
start "Python 3.14" "https://www.python.org/downloads/"
pause
exit /b 1

:install_failed
echo.
echo Automatic installation did not finish successfully.
echo Opening the official Python download page. Install Python 3.14, then run this file again.
start "Python 3.14" "https://www.python.org/downloads/"
pause
exit /b 1

:restart_needed
echo.
echo Python was installed successfully. Close this window and run Start-Accounting.bat again.
pause
exit /b

:environment_failed
echo.
echo The private Accounting environment could not be created.
pause
exit /b 1

:packages_failed
echo.
echo Required packages could not be installed. Check your internet connection and try again.
pause
exit /b 1
