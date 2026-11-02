# V3.1 focused acceptance runner for Windows PowerShell 5.1 / 7
# Run from the repository root. It does not start the server, call an LLM,
# migrate the production database, or modify paper/library data.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {
    Write-Host "== PaperLib V3.1 acceptance ==" -ForegroundColor Cyan
    $branch = (& git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Unable to read current Git branch." }
    if ($branch -ne "feat/paper-understanding-v3") {
        throw "Expected branch feat/paper-understanding-v3, current branch is '$branch'. Switch to the V3.1 PR branch first."
    }
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { $python = "python" }
    Write-Host "Branch: $branch" -ForegroundColor Gray
    Write-Host "Python: $python" -ForegroundColor Gray
    Write-Host ""
    Write-Host "== 1/2 V3.1 focused backend contracts ==" -ForegroundColor Cyan
    & $python -m pytest -q tests/test_kg_understanding_v31.py
    if ($LASTEXITCODE -ne 0) { throw "V3.1 focused backend tests failed (exit $LASTEXITCODE)." }
    Write-Host ""
    Write-Host "== 2/2 Full backend regression + frontend tests + production build ==" -ForegroundColor Cyan
    & (Join-Path $root "test-local.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Full local verification failed (exit $LASTEXITCODE)." }
    Write-Host ""
    Write-Host "ALL V3.1 AUTOMATED CHECKS PASSED" -ForegroundColor Green
    Write-Host "The script did not call the configured LLM or alter the production library database." -ForegroundColor Gray
    Write-Host "Next: start PaperLib, open /kg-understanding, and generate a NEW 3.1 draft for the Harness paper." -ForegroundColor Yellow
}
catch {
    Write-Error $_
    exit 1
}
finally {
    Pop-Location
}
