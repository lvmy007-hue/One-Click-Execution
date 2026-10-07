@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 安装开机自启：登录后自动收集「今天到达」的店表到当天目录
python collect_wechat.py --install-startup
echo.
echo 请立刻在两个电脑微信里各开一次（以后不用再点表格）：
echo   设置 → 文件 / 通用 / 存储 → 勾选「文件自动下载」
echo   店表只有几十 KB，会自动落到 D:\文档\xwechat_files
echo.
echo 现在启动后台监听...
start "" pythonw tray_apps.py
start "" pythonw collect_wechat.py --supervise
echo 已在后台运行（崩溃会自动拉起），日志: collect.log
pause
