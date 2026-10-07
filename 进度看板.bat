@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" pythonw dashboard.py
timeout /t 1 /nobreak >nul
start http://127.0.0.1:8765/
