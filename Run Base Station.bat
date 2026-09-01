@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo .venv not found -- run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

rem The rover IP is entered in the app window itself, so no prompt here.
start "" ".venv\Scripts\pythonw.exe" main.py
