@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Fund Ledger (LAN)
echo.
echo   Starting Fund Ledger for LAN access (phone on same WiFi) ...
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 server.py --host 0.0.0.0
    goto end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python server.py --host 0.0.0.0
    goto end
)

echo   [ERROR] Python 3.10+ not found.
echo   Download: https://www.python.org/downloads/
echo.
pause

:end
if errorlevel 1 pause
