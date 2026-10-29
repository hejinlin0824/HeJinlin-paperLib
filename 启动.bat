@echo off
cd /d %~dp0

echo ========================================
echo   PaperLib - LunWen TuShuGuan
echo   Local paper reading workbench
echo ========================================

if not exist .venv\Scripts\python.exe (
  echo [First run] Creating Python venv...
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -q -r backend\requirements.txt || goto :err
)

if not exist frontend\dist\index.html (
  echo [First run] Building frontend...
  pushd frontend
  if not exist node_modules call npm install --no-fund --no-audit
  call npm run build || (popd & goto :err)
  popd
)

echo Starting: http://127.0.0.1:8300
start "" http://127.0.0.1:8300
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8300 --app-dir backend
goto :eof

:err
echo.
echo Failed. Check errors above. Requirements: Python 3.10+ and Node.js 18+.
pause
