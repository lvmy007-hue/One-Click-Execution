@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
python collect_wechat.py --watch
pause
