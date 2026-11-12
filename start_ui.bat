@echo off
cd /d "%~dp0"
title Local Typeless UI (Debug Console)
echo [INFO] Starting Local Typeless UI...

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m local_typeless.ui
) else (
    echo [ERROR] Virtual environment not found at .venv\Scripts\python.exe
    pause
)
