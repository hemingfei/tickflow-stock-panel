@echo off
title TickFlow Stock Panel - Stop Services

cd /d "%~dp0"

echo ========================================
echo   Stopping TickFlow Stock Panel...
echo ========================================
echo.

echo Finding and stopping processes...

for /f "tokens=2" %%p in ('tasklist /fi "windowtitle eq TickFlow*" /fo list ^| find "PID"') do (
    echo Stopping process %%p (TickFlow window)...
    taskkill /f /t /pid %%p >nul 2>&1
)

for /f "tokens=2" %%p in ('tasklist /fi "imagename eq python.exe" /fo list ^| find "PID"') do (
    wmic process where "processid=%%p" get commandline 2>nul | find "uvicorn" >nul
    if not errorlevel 1 (
        echo Stopping process %%p (uvicorn backend)...
        taskkill /f /t /pid %%p >nul 2>&1
    )
)

for /f "tokens=2" %%p in ('tasklist /fi "imagename eq node.exe" /fo list ^| find "PID"') do (
    wmic process where "processid=%%p" get commandline 2>nul | find "vite" >nul
    if not errorlevel 1 (
        echo Stopping process %%p (vite frontend)...
        taskkill /f /t /pid %%p >nul 2>&1
    )
)

echo.
echo ========================================
echo   All services stopped!
echo ========================================
echo.
pause
