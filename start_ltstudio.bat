@echo off
title LTstudio AI
cd /d "%~dp0"

where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python not found in PATH! Please ensure Python is installed.
    pause
    exit /b 1
)

python ltstudio_cli.py %*
if %ERRORLEVEL% neq 0 (
    pause
)
