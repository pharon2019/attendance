@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo تشغيل نظام الحضور والانصراف / Starting the attendance system...
py server.py %*
pause
