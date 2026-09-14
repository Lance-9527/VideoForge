@echo off
chcp 65001 > nul
title VideoForge 卸载程序
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0卸载.ps1"