@echo off
title Jackpot Tracker - Unified
echo ========================================
echo   Jackpot Tracker (Unified)
echo   Zitobox + CarnivalCiti
echo ========================================
echo.
echo Dashboard will be at http://127.0.0.1:5055
echo.
echo Close this window to stop the server.
echo ========================================
echo.

cd /d "%~dp0"

if not exist "data" mkdir data

python -c "import playwright" 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r requirements.txt
    playwright install chromium
)

python app.py
pause
