@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run Install.cmd first.
  pause
  exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" "scripts\desktop.py" start
if errorlevel 1 pause
