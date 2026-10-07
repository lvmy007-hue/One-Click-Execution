@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
start "" pythonw tray_apps.py
start "" pythonw collect_wechat.py --supervise
pythonw collect_wechat.py --once
pythonw tray_apps.py --launch-daily
