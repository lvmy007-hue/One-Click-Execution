@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
python run_detect.py
pause
