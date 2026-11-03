[CmdletBinding()]
param(
    [switch]$SkipDependencyInstall,
    [switch]$SkipLauncherPreflight
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "test_v4.ps1"
$reportDir = Join-Path $env:TEMP ("paperlib-oneclick-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$summaryPath = Join-Path $reportDir "oneclick-summary.json"
$results = [ordered]@{}
$started = Get-Date
$exitCode = 0

function Invoke-PaperLibTier {
    param(
        [Parameter(Mandatory = $true)][string]$Tier,
        [switch]$Install,
        [switch]$LauncherPreflight
    )

    $powershell = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if (-not $powershell) {
        $powershell = Get-Command powershell.exe -ErrorAction SilentlyContinue
    }
    if (-not $powershell) {
        throw "PowerShell executable was not found."
    }

    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $runner,
        "-Tier", $Tier
    )
    if ($Install) { $arguments += "-InstallDependencies" }
    if ($LauncherPreflight) { $arguments += "-LauncherPreflight" }

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "PaperLib test tier: $Tier" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan

    & $powershell.Source @arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "Tier '$Tier' failed with exit code $code. See the detailed report printed by test_v4.ps1."
    }
    $results[$Tier] = "passed"
    Write-Host "[PASS] $Tier" -ForegroundColor Green
}

try {
    Set-Location $root

    if (-not (Test-Path $runner)) {
        throw "Cannot find scripts/test_v4.ps1. Please update your feat/paper-intelligence-v4 branch."
    }

    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) { throw "Git was not found. Install Git for Windows first." }

    $branch = (& $git.Source -C $root branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Unable to determine the current Git branch." }
    if ($branch -ne "feat/paper-intelligence-v4") {
        throw "Wrong branch '$branch'. Switch to feat/paper-intelligence-v4 before testing. This script will not switch branches or modify Git history."
    }

    $head = (& $git.Source -C $root rev-parse --short HEAD).Trim()
    $changes = & $git.Source -C $root status --short
    Write-Host "Repository : $root"
    Write-Host "Branch     : $branch"
    Write-Host "HEAD       : $head"
    Write-Host "Report dir : $reportDir"

    if ($changes) {
        Write-Warning "Working tree has local changes. The test runner will not reset, clean, commit, or overwrite them."
        $changes | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    }

    if (-not (Get-Command python -ErrorAction SilentlyContinue) -and
        -not (Test-Path (Join-Path $root ".venv\Scripts\python.exe"))) {
        throw "Python was not found. Install Python 3.10+ and make sure 'python' is on PATH."
    }

    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw "npm was not found. Install Node.js LTS (which includes npm), then reopen PowerShell."
    }

    # Fast, synthetic-only checks first. Dependency installation happens once.
    $unitArgs = @{}
    if (-not $SkipDependencyInstall) { $unitArgs.Install = $true }
    Invoke-PaperLibTier -Tier "unit" @unitArgs

    # Full backend regression + frontend graph tests + production build.
    $fullArgs = @{}
    if (-not $SkipLauncherPreflight) { $fullArgs.LauncherPreflight = $true }
    Invoke-PaperLibTier -Tier "full-sim" @fullArgs

    Write-Host ""
    Write-Host "ALL OFFLINE TESTS PASSED." -ForegroundColor Green
    Write-Host "No real paper was selected and no live model request was made."
}
catch {
    $exitCode = 1
    $results["failure"] = $_.Exception.Message
    Write-Host ""
    Write-Host "[FAILED] $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    $finished = Get-Date
    $branchValue = $null
    $headValue = $null
    if ($branch) { $branchValue = $branch }
    if ($head) { $headValue = $head }
    $summary = [ordered]@{
        project = "HeJinlin-paperLib"
        branch = $branchValue
        head = $headValue
        test_scope = "synthetic unit contracts + full backend regression + frontend tests/build + optional Windows launcher preflight; no real paper/model calls"
        started_at = $started.ToString("o")
        finished_at = $finished.ToString("o")
        duration_seconds = [math]::Round(($finished - $started).TotalSeconds, 2)
        results = $results
        exit_code = $exitCode
    }
    $summary | ConvertTo-Json -Depth 8 | Set-Content -Path $summaryPath -Encoding UTF8

    Write-Host ""
    Write-Host "One-click summary: $summaryPath"
    Write-Host "Detailed per-tier logs are in the paperlib-v4-test-* folders under $env:TEMP."
}
exit $exitCode
