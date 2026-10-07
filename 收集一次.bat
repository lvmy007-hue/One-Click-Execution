@echo off
chcp 65001 >nul
cd /d "%~dp0"
python collect_wechat.py --once
pause
