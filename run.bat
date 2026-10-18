@echo off
title Binance Options Dashboard
cd /d "%~dp0"

echo ===================================================
echo   Starting Binance Options Dashboard
echo ===================================================

:: Activate virtual environment if present
if exist "venv\Scripts\activate.bat" (
    echo Activating venv...
    call venv\Scripts\activate.bat
) else if exist ".venv\Scripts\activate.bat" (
    echo Activating .venv...
    call .venv\Scripts\activate.bat
)

:: Wait 2 seconds and open browser automatically
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8000"

:: Start Uvicorn Server
echo Running server on http://127.0.0.1:8000
python -m uvicorn main:app --reload --port 8000

pause
