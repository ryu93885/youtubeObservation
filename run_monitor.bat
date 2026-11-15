@echo off
setlocal
cd /d "%~dp0"
title YouTube View Monitor (monitor)
set "PYEXE=python"
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
rem Keep this window open while monitoring. Press Ctrl+C to stop.
"%PYEXE%" main.py %*
echo.
pause
