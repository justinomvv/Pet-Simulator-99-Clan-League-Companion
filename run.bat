@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found on PATH. Install it from https://python.org and re-run this file.
    pause
    exit /b 1
)

pip show requests >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    pip install -r requirements.txt
)
pip show pillow >nul 2>nul
if errorlevel 1 (
    pip install -r requirements.txt
)

start "" pythonw ps99_overlay.py
