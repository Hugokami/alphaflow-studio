@echo off
title LTstudio AI (Local FastAPI + FFmpeg Engine)
cd /d "%~dp0"
echo Starting LTstudio AI on http://127.0.0.1:8090 ...
python run_studio.py
pause
