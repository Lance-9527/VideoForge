@echo off
chcp 65001 > nul
title VideoForge 环境诊断
cd /d "%~dp0"
echo ============================================
echo   VideoForge 环境诊断（零依赖，可离线运行）
echo ============================================
echo.
echo [1/3] 系统信息
ver
echo.
echo [2/3] 程序文件
if exist "%~dp0VideoForge.exe" (echo   找到: %~dp0VideoForge.exe) else (echo   未找到 VideoForge.exe)
if exist "%~dp0app\VideoForge.exe" (echo   找到: %~dp0app\VideoForge.exe) else (echo   未找到 app\VideoForge.exe)
echo.
echo [3/3] 运行详细诊断...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0环境诊断.ps1"