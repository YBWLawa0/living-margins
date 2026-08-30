@echo off
cd /d "%~dp0"
if not exist "web\node_modules\vite\bin\vite.js" (
  call npm install --prefix web --no-audit --no-fund
  if errorlevel 1 goto :build_failed
)
call npm run build --prefix web
if errorlevel 1 goto :build_failed
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" living_margins_web.py
) else (
  python living_margins_web.py
)
pause
exit /b

:build_failed
echo.
echo Vue frontend build failed. Check Node.js and npm, then try again.
pause
exit /b 1
