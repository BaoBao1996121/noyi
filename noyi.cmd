@echo off
cd /d "%~dp0"
if "%~1"=="" goto usage
if not "%~1"=="start" if not "%~1"=="stop" if not "%~1"=="restart" if not "%~1"=="status" goto usage
if not exist ".venv\Scripts\python.exe" (
  echo Please run Install.cmd first.
  pause
  exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" "scripts\desktop.py" %~1
if errorlevel 1 if not "%~1"=="status" pause
exit /b 0

:usage
".venv\Scripts\python.exe" -c "print('用法：noyi start | stop | restart | status')" 2>nul
if errorlevel 1 echo usage: noyi start / stop / restart / status
exit /b 1
