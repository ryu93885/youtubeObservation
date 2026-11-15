@echo off
setlocal
cd /d "%~dp0"
echo === YouTube View Monitor : setup ===
echo.

set "PY=python"
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"

if not exist ".venv\Scripts\python.exe" (
  echo Creating a virtual environment in .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo.
    echo [ERROR] Python 3.10 or later was not found.
    echo         Install it from https://www.python.org/downloads/ and run setup.bat again.
    pause
    exit /b 1
  )
)

echo Installing the required packages ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] Package installation failed. Check your internet connection and try again.
  pause
  exit /b 1
)

if not exist "config.json" (
  copy /Y "config.example.json" "config.json" >nul
  echo.
  echo Created config.json.
)

echo.
echo Setup finished. Next steps:
echo   1. Open config.json with Notepad and set "api_key" and "videos".
echo   2. Double-click check.bat to test your settings.
echo   3. Double-click run_monitor.bat and run_dashboard.bat.
echo.
pause
