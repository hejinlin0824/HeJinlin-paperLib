@echo off
cd /d %~dp0
echo Dev mode: backend 8300 (reload) + frontend 5173 (hmr)
if not exist .venv\Scripts\python.exe (
  python -m venv .venv
  .venv\Scripts\python -m pip install -q -r backend\requirements.txt
)
start "paperlib-backend" cmd /k ".venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8300 --reload --app-dir backend"
pushd frontend
if not exist node_modules call npm install --no-fund --no-audit
start "paperlib-frontend" cmd /k "npm run dev"
popd
echo Frontend: http://localhost:5173  (API proxied to 8300)
pause
