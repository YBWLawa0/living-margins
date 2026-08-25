@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run run.bat once before opening the comment editor.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" comment_editor.py
if errorlevel 1 pause
