@echo off
title TickFlow Stock Panel

cd /d "%~dp0"

echo ========================================
echo   TickFlow Stock Panel Launcher
echo ========================================
echo.

:menu
echo Please select an option:
echo   1. Start All Services
echo   2. Start Backend Only
echo   3. Start Frontend Only
echo   4. Restart All Services
echo   5. Stop All Services
echo   0. Exit
echo.
set /p choice=Enter your choice [0-5]:

if "%choice%"=="1" goto start_all
if "%choice%"=="2" goto start_backend
if "%choice%"=="3" goto start_frontend
if "%choice%"=="4" goto restart_all
if "%choice%"=="5" goto stop_all
if "%choice%"=="0" goto end
echo.
echo Invalid option, please try again!
echo.
goto menu

:start_all
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
goto end

:start_backend
echo.
echo Starting backend...
start "TickFlow Backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 3018"

echo.
echo ========================================
echo   Backend started!
echo   URL: http://localhost:3018
echo ========================================
echo.
goto end

:start_frontend
echo.
echo Starting frontend...
start "TickFlow Frontend" cmd /k "cd /d %~dp0frontend && pnpm dev --host 0.0.0.0 --port 3012"

echo.
echo ========================================
echo   Frontend started!
echo   URL: http://localhost:3012
echo ========================================
echo.
goto end

:restart_all
echo.
echo Stopping all services...
call :stop_processes

echo.
echo [1/2] Starting backend...
start "TickFlow Backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 3018"
timeout /t 3 /nobreak >nul

echo [2/2] Starting frontend...
start "TickFlow Frontend" cmd /k "cd /d %~dp0frontend && pnpm dev --host 0.0.0.0 --port 3012"

echo.
echo ========================================
echo   All services restarted!
echo   Backend: http://localhost:3018
echo   Frontend: http://localhost:3012
echo ========================================
echo.
goto end

:stop_all
echo.
echo Stopping all services...
call :stop_processes
echo.
echo ========================================
echo   All services stopped!
echo ========================================
echo.
goto end

:stop_processes
for /f "tokens=2" %%p in ('tasklist /fi "windowtitle eq TickFlow*" /fo list ^| find "PID"') do (
    taskkill /f /t /pid %%p >nul 2>&1
)
for /f "tokens=2" %%p in ('tasklist /fi "imagename eq python.exe" /fo list ^| find "PID"') do (
    wmic process where "processid=%%p" get commandline 2>nul | find "uvicorn" >nul && taskkill /f /t /pid %%p >nul 2>&1
)
for /f "tokens=2" %%p in ('tasklist /fi "imagename eq node.exe" /fo list ^| find "PID"') do (
    wmic process where "processid=%%p" get commandline 2>nul | find "vite" >nul && taskkill /f /t /pid %%p >nul 2>&1
)
goto :eof

:end
echo.
pause
