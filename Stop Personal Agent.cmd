@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" exit /b 1
set PYTHONUTF8=1
".venv\Scripts\python.exe" "scripts\desktop.py" stop
if errorlevel 1 pause
