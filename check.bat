@echo off
setlocal
cd /d "%~dp0"
title YouTube View Monitor (check)
set "PYEXE=python"
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
"%PYEXE%" main.py --check
echo.
pause
