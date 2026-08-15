@echo off
title TickFlow Stock Panel - Quick Start

cd /d "%~dp0"

echo ========================================
echo   TickFlow Stock Panel Quick Start
echo ========================================
echo.

echo [1/2] Starting backend...
start "TickFlow Backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 3018"
timeout /t 3 /nobreak >nul

echo [2/2] Starting frontend...
start "TickFlow Frontend" cmd /k "cd /d %~dp0frontend && pnpm dev --host 0.0.0.0 --port 3012"

echo.
echo ========================================
echo   All services started!
echo   Backend: http://localhost:3018
echo   Frontend: http://localhost:3012
echo ========================================
echo.
echo Tips:
echo   - Close the windows to stop services
echo   - Run this script again to restart
echo.
pause
