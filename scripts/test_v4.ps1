[CmdletBinding()]
param(
    [switch]$InstallDependencies,
    [switch]$SkipFrontend,
    [switch]$LauncherPreflight
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportDir = Join-Path $env:TEMP "paperlib-v4-test-$stamp"
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$transcript = Join-Path $reportDir "test.log"
$summaryPath = Join-Path $reportDir "summary.json"
$results = [ordered]@{}
$started = Get-Date
$exitCode = 0

function Invoke-TestStep {
    param([string]$Name, [scriptblock]$Action)
    Write-Host ""
    Write-Host "========== $Name ==========" -ForegroundColor Cyan
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
    $timer.Stop()
    $results[$Name] = [ordered]@{
        status = "passed"
        duration_seconds = [math]::Round($timer.Elapsed.TotalSeconds, 2)
    }
    Write-Host "[PASS] $Name ($([math]::Round($timer.Elapsed.TotalSeconds, 2))s)" -ForegroundColor Green
}

try {
    Start-Transcript -Path $transcript -Force | Out-Null
    Set-Location $root

    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw "Python 3.10+ was not found. Install Python or create .venv first." }
        $python = $pythonCommand.Source
    }
    $pythonVersion = & $python --version
    Write-Host "Repository: $root"
    Write-Host "Python: $pythonVersion"
    Write-Host "Report: $reportDir"

    if ($InstallDependencies) {
        Invoke-TestStep "Install backend test dependencies" {
            & $python -m pip install -r (Join-Path $root "backend\requirements.txt") -r (Join-Path $root "backend\requirements-dev.txt")
        }
    }

    $env:PYTHONPATH = (Join-Path $root "backend")
    Invoke-TestStep "Backend regression tests" {
        & $python -m pytest -q (Join-Path $root "tests")
    }

    if (-not $SkipFrontend) {
        $frontend = Join-Path $root "frontend"
        if ($InstallDependencies -or -not (Test-Path (Join-Path $frontend "node_modules"))) {
            Invoke-TestStep "Install frontend dependencies" {
                Push-Location $frontend
                try { npm ci } finally { Pop-Location }
            }
        }
        Invoke-TestStep "Frontend graph tests" {
            Push-Location $frontend
            try { npm run test:graph } finally { Pop-Location }
        }
        Invoke-TestStep "Frontend production build" {
            Push-Location $frontend
            try { npm run build } finally { Pop-Location }
        }
    }

    if ($LauncherPreflight) {
        if (-not (Test-Path (Join-Path $root ".venv\Scripts\python.exe"))) {
            Invoke-TestStep "Create local virtual environment" {
                & $python -m venv (Join-Path $root ".venv")
            }
        }
        Invoke-TestStep "Windows launcher build preflight" {
            $oldNoBrowser = $env:PAPERLIB_NO_BROWSER
            $oldBuildOnly = $env:PAPERLIB_BUILD_ONLY
            try {
                $env:PAPERLIB_NO_BROWSER = "1"
                $env:PAPERLIB_BUILD_ONLY = "1"
                & $env:ComSpec /c "启动.bat"
            } finally {
                if ($null -eq $oldNoBrowser) { Remove-Item Env:PAPERLIB_NO_BROWSER -ErrorAction SilentlyContinue }
                else { $env:PAPERLIB_NO_BROWSER = $oldNoBrowser }
                if ($null -eq $oldBuildOnly) { Remove-Item Env:PAPERLIB_BUILD_ONLY -ErrorAction SilentlyContinue }
                else { $env:PAPERLIB_BUILD_ONLY = $oldBuildOnly }
            }
        }
    }

    Write-Host ""
    Write-Host "All requested checks passed." -ForegroundColor Green
}
catch {
    $exitCode = 1
    $results["failure"] = [ordered]@{ status = "failed"; message = $_.Exception.Message }
    Write-Host "[FAIL] $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    $finished = Get-Date
    $summary = [ordered]@{
        project = "HeJinlin-paperLib"
        test_scope = "offline regression and frontend build; no real model API calls"
        started_at = $started.ToString("o")
        finished_at = $finished.ToString("o")
        duration_seconds = [math]::Round(($finished - $started).TotalSeconds, 2)
        python = if ($python) { $python } else { $null }
        results = $results
        exit_code = $exitCode
        log = $transcript
    }
    $summary | ConvertTo-Json -Depth 8 | Set-Content -Path $summaryPath -Encoding UTF8
    try { Stop-Transcript | Out-Null } catch { }
    Write-Host ""
    Write-Host "Summary: $summaryPath"
    Write-Host "Log:     $transcript"
}
exit $exitCode
