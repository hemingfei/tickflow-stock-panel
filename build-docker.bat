@echo off
setlocal
cd /d "%~dp0"

echo [1/2] Building tickflow-stock-panel:latest ...
docker build -t tickflow-stock-panel:latest .
if errorlevel 1 goto :fail

echo [2/2] Saving image to tickflow-stock-panel.tar ...
docker save -o tickflow-stock-panel.tar tickflow-stock-panel:latest
if errorlevel 1 goto :fail

echo [DONE] Output: %CD%\tickflow-stock-panel.tar
dir tickflow-stock-panel.tar | findstr tickflow-stock-panel
exit /b 0

:fail
echo [ERROR] Build failed.
exit /b 1
