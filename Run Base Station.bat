@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo .venv not found -- run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

set /p ROVER_HOST=Rover IP address (e.g. 192.168.1.50):
if "%ROVER_HOST%"=="" (
    echo No rover IP entered, exiting.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" main.py --rover-host %ROVER_HOST%
