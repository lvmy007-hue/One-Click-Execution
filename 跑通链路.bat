@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
python collect_wechat.py --once
python run_pipeline.py
pause
