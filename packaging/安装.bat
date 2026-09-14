@echo off
chcp 65001 > nul
title VideoForge 安装程序
cd /d "%~dp0"
echo 正在启动安装程序...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装.ps1"
if errorlevel 1 (
  echo.
  echo [提示] 安装程序异常退出。可尝试：右键本文件 -^> 以管理员身份运行
  pause
)