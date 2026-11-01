@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   PaperLib - LunWen TuShuGuan
echo   Production launcher
echo   http://127.0.0.1:8300
echo ========================================
echo.

rem 1) Check required tools before doing any work.
where python >nul 2>&1
if errorlevel 1 goto :err_python
where node >nul 2>&1
if errorlevel 1 goto :err_node
where npm.cmd >nul 2>&1
if errorlevel 1 goto :err_npm

rem 2) Create the Python environment when needed.
if not exist ".venv\Scripts\python.exe" (
  echo [1/5] Creating Python virtual environment...
  python -m venv .venv
  if errorlevel 1 goto :err_venv
) else (
  echo [1/5] Python virtual environment found.
)

rem 2.5) Read-only safety check: never start against a suspicious empty database.
echo [SAFE] Checking the existing paper library (read-only)...
".venv\Scripts\python.exe" tools\check_library.py
if errorlevel 1 goto :err_library

rem 3) Synchronize backend dependencies.
echo [2/5] Checking backend dependencies...
".venv\Scripts\python.exe" -m pip install -q -r backend\requirements.txt
if errorlevel 1 goto :err_backend_deps
echo [ERROR] Library integrity check failed. No database or paper files were changed.
goto :fail
:err_library
echo [ERROR] Library integrity check failed. No database or paper files were changed.
goto :fail
:err_backend_deps

rem 4) Ensure frontend dependencies exist, then ALWAYS build the production frontend.
rem     Do not trust an old frontend\dist\index.html: it may belong to an older checkout.
echo [3/5] Checking frontend dependencies...
pushd frontend
if not exist node_modules (
  echo Installing frontend dependencies...
  call npm.cmd install --no-fund --no-audit
  if errorlevel 1 (
    popd
    goto :err_frontend_deps
  )
)

echo [4/5] Building the latest production frontend...
call npm.cmd run build
if errorlevel 1 (
  popd
  goto :err_frontend_build
)
popd

if not exist "frontend\dist\index.html" goto :err_dist

rem 5) Check the port only AFTER the build. Never silently open a stale dev server.
netstat -ano | findstr /R /C:":8300 .*LISTENING" >nul 2>&1
if not errorlevel 1 goto :err_port

echo [5/5] Starting PaperLib server...
echo This window shows server logs. Press Ctrl+C to stop.
start "" cmd /c "timeout /t 4 /nobreak >nul & start "" http://127.0.0.1:8300"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8300 --app-dir backend
goto :eof

:err_python
echo [ERROR] Python was not found. Install Python 3.10+ and add it to PATH.
goto :fail
:err_node
echo [ERROR] Node.js was not found. Install Node.js 18+ and add it to PATH.
goto :fail
:err_npm
echo [ERROR] npm.cmd was not found. Repair your Node.js installation.
goto :fail
:err_venv
echo [ERROR] Failed to create the Python virtual environment.
goto :fail
:err_backend_deps
echo [ERROR] Backend dependency installation failed.
goto :fail
:err_frontend_deps
echo [ERROR] Frontend dependency installation failed. Check Node.js/npm and network access.
goto :fail
:err_frontend_build
echo [ERROR] Frontend production build failed. The server was NOT started.
goto :fail
:err_dist
echo [ERROR] Build finished but frontend\dist\index.html was not generated.
goto :fail
:err_port
echo [ERROR] Port 8300 is already occupied.
echo Close the PaperLib backend window started by dev.bat or another PaperLib instance,
echo then double-click this file again.
goto :fail

:fail
echo.
echo Startup did not complete. Read the error above, fix it, and retry.
pause
exit /b 1
