@echo off
setlocal
cd /d "%~dp0"
title YouTube View Monitor (dashboard)
set "PYEXE=python"
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
rem The browser opens http://localhost:8501 automatically. Press Ctrl+C to stop.
"%PYEXE%" -m streamlit run dashboard.py %*
echo.
pause
