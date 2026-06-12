# GramGPT -- single-shot stopper.
#
# Reads .pids/*.pid and kills each process.
# This is critical: celery beat is a SEPARATE process. If you only
# Ctrl+C uvicorn, beat keeps firing dispatch_plans every 60 seconds.
#
# Incident 2026-06-12 was partly caused by exactly this: user thought
# the program was stopped, but beat kept running and accumulating
# delayed plans, which then burst-launched on next worker startup.

$ErrorActionPreference = "Continue"
$ROOT = $PSScriptRoot
$PID_DIR = Join-Path $ROOT ".pids"

if (-not (Test-Path $PID_DIR)) {
    Write-Host "[skip] .pids dir not found -- nothing to stop." -ForegroundColor Yellow
    # Still hunt for zombies below, in case .pids was deleted manually
}

$pids = Get-ChildItem $PID_DIR -Filter "*.pid" -ErrorAction SilentlyContinue
if ($pids) {
    foreach ($pidFile in $pids) {
        $name = $pidFile.BaseName
        $pidValue = (Get-Content $pidFile.FullName -Raw).Trim()
        if (-not $pidValue) { continue }
        try {
            $proc = Get-Process -Id $pidValue -ErrorAction Stop
            Write-Host "[kill] $name (PID=$pidValue)" -ForegroundColor Cyan
            $proc | Stop-Process -Force -ErrorAction SilentlyContinue
        } catch {
            Write-Host "       ($name PID=$pidValue not running)" -ForegroundColor Gray
        }
        Remove-Item $pidFile.FullName -Force -ErrorAction SilentlyContinue
    }
}

# Hunt for zombie celery workers and uvicorn that didn't end up in .pids
$stragglers = Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -match "celery_app\s+(worker|beat)") -or
    ($_.CommandLine -match "uvicorn\s+main:app")
}

foreach ($s in $stragglers) {
    Write-Host "[zombie] PID=$($s.ProcessId) -- killing" -ForegroundColor Yellow
    Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
}

if (Test-Path $PID_DIR) {
    Remove-Item $PID_DIR -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "[OK] Stack stopped." -ForegroundColor Green
