# GramGPT -- single-shot starter for the whole stack.
#
# Starts:
#   1. uvicorn   (FastAPI + frontend) on port 8000
#   2. celery worker  (-P threads -c 40)
#   3. celery beat    (dispatches plans every 60s)
#
# PIDs are written to .pids/*.pid so that stop_all.ps1 can kill them.
# Console output goes to logs/<name>.console.log.
#
# Usage:
#   .\start_all.ps1
# Stop:
#   .\stop_all.ps1
#
# Why this script exists: incident 2026-06-12 was caused partly by
# leaving celery beat running while the user thought the program was
# stopped. Beat kept firing dispatch_plans every minute, piling up
# tasks, which then burst-launched on next worker startup. This
# script kills the whole stack together.

$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot
$PID_DIR = Join-Path $ROOT ".pids"
$LOG_DIR = Join-Path $ROOT "logs"

New-Item -ItemType Directory -Force -Path $PID_DIR | Out-Null
New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null

$existing = Get-ChildItem $PID_DIR -Filter "*.pid" -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[WARN] PID files found in .pids/. Already running?" -ForegroundColor Yellow
    Write-Host "       Run .\stop_all.ps1 first (or delete .pids manually)." -ForegroundColor Yellow
    exit 1
}

$python = (Get-Command python).Source
$apiDir = Join-Path $ROOT "api"

function Start-BgProcess {
    # NOTE: parameter is NOT named $Args -- that's a reserved automatic
    # variable in PowerShell functions and silently gets overridden,
    # which makes -ArgumentList receive $null inside Start-Process.
    param([string]$Name, [string[]]$ProcArgs, [string]$WorkDir)
    # Python (uvicorn, celery) defaults to logging on stderr.
    # We point user to the .console.log (stderr) by name.
    # .stdout.log will usually stay empty.
    $stdoutFile = Join-Path $LOG_DIR "$Name.stdout.log"
    $stderrFile = Join-Path $LOG_DIR "$Name.console.log"
    Write-Host "[start] $Name -> $stderrFile" -ForegroundColor Cyan
    # Important: Python (uvicorn, celery) defaults to logging on stderr.
    # We name the stderr file as the "main" .log so the user looks at
    # the right place. stdout (.out.log) usually stays empty.
    $proc = Start-Process -FilePath $python -ArgumentList $ProcArgs `
        -WorkingDirectory $WorkDir `
        -RedirectStandardOutput $stdoutFile `
        -RedirectStandardError $stderrFile `
        -PassThru -WindowStyle Hidden
    $proc.Id | Out-File -FilePath (Join-Path $PID_DIR "$Name.pid") -Encoding ascii
    Write-Host "        PID = $($proc.Id)" -ForegroundColor Gray
}

# 1. Uvicorn (FastAPI)
Start-BgProcess -Name "uvicorn" -WorkDir $apiDir -ProcArgs @(
    "-m", "uvicorn", "main:app",
    "--host", "0.0.0.0", "--port", "8000"
)

# 2. Celery worker -- all queues
Start-BgProcess -Name "celery_worker" -WorkDir $apiDir -ProcArgs @(
    "-m", "celery", "-A", "celery_app", "worker",
    "-l", "info", "-P", "threads", "-c", "40",
    "-Q", "plans,warmup,parsers,ai_dialogs,high_priority,bulk_actions,subscribe"
)

# 3. Celery beat -- separate scheduler process
Start-BgProcess -Name "celery_beat" -WorkDir $apiDir -ProcArgs @(
    "-m", "celery", "-A", "celery_app", "beat",
    "-l", "info"
)

Start-Sleep -Seconds 2
Write-Host ""
Write-Host "[OK] Stack started." -ForegroundColor Green
Write-Host "     API:  http://localhost:8000" -ForegroundColor Gray
Write-Host "     Live log files (Python -> stderr, watch *.console.log):" -ForegroundColor Gray
Write-Host "       $LOG_DIR\uvicorn.console.log" -ForegroundColor DarkGray
Write-Host "       $LOG_DIR\celery_worker.console.log" -ForegroundColor DarkGray
Write-Host "       $LOG_DIR\celery_beat.console.log" -ForegroundColor DarkGray
Write-Host ""
Write-Host "Tail logs in real time:" -ForegroundColor Yellow
Write-Host "  Get-Content $LOG_DIR\celery_worker.console.log -Wait -Tail 30" -ForegroundColor Gray
Write-Host ""
Write-Host "To stop everything: .\stop_all.ps1" -ForegroundColor Yellow
