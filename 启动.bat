@echo off
setlocal
cd /d %~dp0

echo ========================================
echo   PaperLib - LunWen TuShuGuan
echo   http://127.0.0.1:8300
echo ========================================

rem 1) Service already running? Just open the browser.
netstat -ano | findstr /R /C:":8300 .*LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo Service is already running - opening browser.
  start "" http://127.0.0.1:8300
  goto :eof
)

rem 2) Python venv (created on first run)
if not exist .venv\Scripts\python.exe (
  echo [First run] Creating Python venv...
  python -m venv .venv || goto :err
)

rem 3) Backend deps - re-synced every start (fast when already installed)
.venv\Scripts\python -m pip install -q -r backend\requirements.txt || goto :err

rem 4) Frontend build (first run only; use dev.bat to rebuild after source edits)
if not exist frontend\dist\index.html (
  echo [First run] Building frontend - needs Node.js 18+...
  pushd frontend
  if not exist node_modules call npm install --no-fund --no-audit
  call npm run build || (popd & goto :err)
  popd
)

echo Starting server... this window shows logs. Press Ctrl+C to stop.
rem Browser opens automatically once the server is up.
start "" cmd /c "timeout /t 4 /nobreak >nul & start "" http://127.0.0.1:8300"
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8300 --app-dir backend
goto :eof

:err
echo.
echo Startup failed. Requirements: Python 3.10+ and Node.js 18+.
echo Check the messages above, then re-run this script.
pause
