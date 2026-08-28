@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" living_margins_web.py
) else (
  python living_margins_web.py
)
pause
