# V4 local acceptance runner for Windows PowerShell 5.1 / 7
# Safe: no LLM calls, no production DB migration, no paper-content mutation.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {
    Write-Host "== PaperLib V4 Hybrid Intelligence acceptance ==" -ForegroundColor Cyan
    $branch = (& git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Unable to read current Git branch." }
    if ($branch -ne "feat/paper-intelligence-v4") {
        throw "Expected feat/paper-intelligence-v4, current branch is '$branch'."
    }
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { $python = "python" }
    Write-Host "Branch: $branch" -ForegroundColor Gray
    Write-Host "== 1/3 V4 retrieval and resilience contracts ==" -ForegroundColor Cyan
    & $python -m pytest -q tests/test_paper_intelligence_v4.py
    if ($LASTEXITCODE -ne 0) { throw "V4 focused tests failed (exit $LASTEXITCODE)." }

    Write-Host "== 2/3 V3/V3.1 compatibility contracts ==" -ForegroundColor Cyan
    & $python -m pytest -q tests/test_kg_understanding_v3.py tests/test_kg_understanding_v31.py
    if ($LASTEXITCODE -ne 0) { throw "V3 compatibility tests failed (exit $LASTEXITCODE)." }

    Write-Host "== 3/3 Full backend + frontend + production build ==" -ForegroundColor Cyan
    & (Join-Path $root "test-local.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Full local verification failed (exit $LASTEXITCODE)." }

    Write-Host ""
    Write-Host "ALL V4 LOCAL CHECKS PASSED" -ForegroundColor Green
    Write-Host "No configured LLM was called and production paper data was not modified." -ForegroundColor Gray
    Write-Host "For real acceptance, open http://127.0.0.1:8300/kg-intelligence." -ForegroundColor Yellow
}
catch {
    Write-Error $_
    exit 1
}
finally {
    Pop-Location
}
