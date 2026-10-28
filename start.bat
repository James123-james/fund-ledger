@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Fund Ledger
echo.
echo   Starting Fund Ledger ...
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 server.py %*
    goto end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python server.py %*
    goto end
)

echo   [ERROR] Python 3.10+ not found.
echo   Download: https://www.python.org/downloads/
echo   Remember to check "Add python.exe to PATH" during install.
echo.
pause

:end
if errorlevel 1 pause
