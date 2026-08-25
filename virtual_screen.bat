@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run run.bat once before opening the virtual screen.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" virtual_screen.py %*
if errorlevel 1 pause
