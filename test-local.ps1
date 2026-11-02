# PaperLib local verification entry point (Windows PowerShell)
# Runs backend tests, frontend graph tests, and a production build.
# It never opens or migrates the user's production library database.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { $python = "python" }

    Write-Host "== 1/3 Backend regression tests ==" -ForegroundColor Cyan
    & $python -m pytest -q tests/
    if ($LASTEXITCODE -ne 0) { throw "Backend tests failed (exit $LASTEXITCODE)." }

    Write-Host "== 2/3 Frontend graph tests ==" -ForegroundColor Cyan
    Push-Location (Join-Path $root "frontend")
    try {
        if (-not (Test-Path "node_modules")) {
            npm ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed (exit $LASTEXITCODE)." }
        }
        npm run test:graph
        if ($LASTEXITCODE -ne 0) { throw "Frontend graph tests failed (exit $LASTEXITCODE)." }

        Write-Host "== 3/3 Frontend production build ==" -ForegroundColor Cyan
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed (exit $LASTEXITCODE)." }
    }
    finally {
        Pop-Location
    }
    Write-Host "ALL LOCAL CHECKS PASSED" -ForegroundColor Green
}
catch {
    Write-Error $_
    exit 1
}
finally {
    Pop-Location
}
