@echo off
title AlphaFlow AI Studio (Local FastAPI + FFmpeg Engine)
cd /d "%~dp0"
echo Starting AlphaFlow AI Studio on http://127.0.0.1:8090 ...
python run_studio.py
pause
