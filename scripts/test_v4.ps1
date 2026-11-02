[CmdletBinding()]
param(
    [switch]$InstallDependencies,
    [switch]$SkipFrontend,
    [switch]$LauncherPreflight,
    [switch]$RunLiveModel,
    [string]$PaperId,
    [int]$LiveTimeoutMinutes = 30
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
$ownedServer = $null

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

    if ($RunLiveModel) {
        if ([string]::IsNullOrWhiteSpace($PaperId)) {
            throw "-RunLiveModel requires -PaperId <existing-paper-id>."
        }
        $api = "http://127.0.0.1:8300"
        try {
            $null = Invoke-RestMethod -Uri "$api/api/papers" -TimeoutSec 2
        } catch {
            Write-Host "Starting a temporary local API server for the live model test..."
            $serverArgs = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8300", "--app-dir", "backend")
            $ownedServer = Start-Process -FilePath $python -ArgumentList $serverArgs -WorkingDirectory $root -PassThru -WindowStyle Hidden
            $ready = $false
            for ($i = 0; $i -lt 40; $i++) {
                Start-Sleep -Seconds 1
                if ($ownedServer.HasExited) { throw "Temporary API server exited before becoming ready." }
                try {
                    $null = Invoke-RestMethod -Uri "$api/api/papers" -TimeoutSec 2
                    $ready = $true
                    break
                } catch { }
            }
            if (-not $ready) { throw "API server did not become ready at $api within 40 seconds." }
        }

        Invoke-TestStep "Live V4 model analysis" {
            $encodedPaperId = [uri]::EscapeDataString($PaperId)
            $startedTask = Invoke-RestMethod -Method Post -Uri "$api/api/knowledge-graph/v4/papers/$encodedPaperId/understand" -TimeoutSec 30
            $draftId = $startedTask.draft.id
            if ([string]::IsNullOrWhiteSpace($draftId)) { throw "V4 API did not return a draft ID." }
            Write-Host "Live V4 draft: $draftId"
            $deadline = (Get-Date).AddMinutes($LiveTimeoutMinutes)
            $detail = $null
            do {
                Start-Sleep -Seconds 3
                $detail = Invoke-RestMethod -Uri "$api/api/knowledge-graph/v4/drafts/$draftId" -TimeoutSec 10
                if ($detail.status -in @("draft", "failed")) { break }
                if ((Get-Date) -ge $deadline) { throw "Live model analysis timed out after $LiveTimeoutMinutes minutes. Draft $draftId is retained for inspection." }
            } while ($true)

            $payload = $detail.payload
            $coverage = $payload.coverage
            $pipeline = $payload.pipeline
            if (-not $pipeline.llm_operations -or -not $coverage.completed_chunks) {
                throw "The live test did not complete any model-backed chunk analysis. Check the configured provider/model and inspect the retained draft $draftId."
            }
            if ($coverage.failed_chunks -gt 0) {
                throw "The live test completed with $($coverage.failed_chunks) failed chunks. Draft $draftId is retained for inspection."
            }
            $liveReport = [ordered]@{
                status = $detail.status
                draft_id = $draftId
                model = $detail.model_name
                chunk_count = $coverage.chunk_count
                completed_chunks = $coverage.completed_chunks
                failed_chunks = $coverage.failed_chunks
                input_coverage = $coverage.input_coverage
                llm_operations = $pipeline.llm_operations
                chunk_batches = $pipeline.chunk_batches
                synthesis_input = $pipeline.synthesis_input
                timings_seconds = $pipeline.timings_seconds
                warnings = $detail.warnings
            }
            $liveReport | ConvertTo-Json -Depth 10 | Set-Content -Path (Join-Path $reportDir "live-model-report.json") -Encoding UTF8
            $results["Live V4 model analysis"] = $liveReport
            if ($detail.status -ne "draft") { throw "Live V4 task finished with status '$($detail.status)'. Draft $draftId is retained." }
            Write-Host "Live model report: $(Join-Path $reportDir 'live-model-report.json')"
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
    if ($ownedServer -and -not $ownedServer.HasExited) {
        Stop-Process -Id $ownedServer.Id -Force -ErrorAction SilentlyContinue
    }
    $finished = Get-Date
    $summary = [ordered]@{
        project = "HeJinlin-paperLib"
        test_scope = if ($RunLiveModel) { "offline regression, frontend build, and explicitly requested live model analysis" } else { "offline regression and frontend build; no real model API calls" }
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
