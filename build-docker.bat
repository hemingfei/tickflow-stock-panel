@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo TickFlow Stock Panel - Docker Build
echo ========================================
echo.

echo [1/2] Building tickflow-stock-panel:latest ...
echo Using China mirror: YES (USE_CN_MIRROR=1)
echo.
docker build --build-arg USE_CN_MIRROR=1 -t tickflow-stock-panel:latest .
if errorlevel 1 goto :fail

echo.
echo [2/2] Saving image to tickflow-stock-panel.tar ...
docker save -o tickflow-stock-panel.tar tickflow-stock-panel:latest
if errorlevel 1 goto :fail

echo.
echo ========================================
echo [DONE] Build successful!
echo ========================================
echo Image: tickflow-stock-panel:latest
echo Archive: %CD%\tickflow-stock-panel.tar
echo.
echo To start the container:
echo   docker-compose up -d
echo.
dir tickflow-stock-panel.tar | findstr tickflow-stock-panel
exit /b 0

:fail
echo.
echo ========================================
echo [ERROR] Build failed.
echo ========================================
exit /b 1
