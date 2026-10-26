@echo off
chcp 65001 >nul
title VideoForge · 删库后一键重建
cd /d "%~dp0"

echo ============================================================
echo   VideoForge · 重建仓库
echo   （前提：你已经在网页上删掉了旧仓库）
echo ============================================================
echo.

where bash >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 bash。Git for Windows 自带 bash，请在 Git Bash 里跑这个脚本。
  pause
  exit /b 1
)

bash "%~dp0重新发布.sh"
echo.
pause
