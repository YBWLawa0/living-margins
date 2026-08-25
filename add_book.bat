@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run run.bat once before adding a book.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" enroll_book.py
if errorlevel 1 (
  echo.
  echo Enrollment failed. Please copy the error above when reporting the issue.
)
pause
