@echo off
chcp 65001 > nul
title VideoForge · 开发版启动
cd /d "%~dp0"

echo ============================================
echo   VideoForge · 启动（桌面窗口）
echo ============================================
echo.

REM 优先使用项目自带 venv，其次系统 python
set PY=python
if exist "%~dp0.venv\Scripts\python.exe" set PY=%~dp0.venv\Scripts\python.exe

echo 使用解释器: %PY%
%PY% -c "import fastapi, uvicorn, httpx" 2>nul
if errorlevel 1 (
  echo [错误] 缺少依赖，请先运行: pip install -r requirements.txt
  pause
  exit /b 1
)

echo 启动中（首次启动约 5-10 秒）...
%PY% launcher.py %*

if errorlevel 1 (
  echo.
  echo [提示] 启动失败。日志: %USERPROFILE%\.videoforge\videoforge.log
  pause
)
