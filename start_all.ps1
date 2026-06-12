# GramGPT — единый старт всех процессов
#
# Что запускает:
#   1. uvicorn (API + фронт)            — порт 8000
#   2. celery worker  (-P threads -c 40)
#   3. celery beat    (диспетчер планов каждые 60 сек)
#
# ВАЖНО:
#   - PID'ы пишутся в .pids/*.pid чтобы stop_all.ps1 знал что убивать.
#   - Логи каждого процесса в logs/<имя>.console.log (раздельно от
#     ротируемого logs/api.log — там structured logger).
#
# Запуск:
#   .\start_all.ps1
# Остановка:
#   .\stop_all.ps1
#
# После инцидента 2026-06-12 строго запрещено стартовать только uvicorn:
# celery beat — отдельный процесс. Если не убить beat при "stop проги",
# он продолжает слать dispatch_plans каждую минуту и копит pile-up.

$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot
$PID_DIR = Join-Path $ROOT ".pids"
$LOG_DIR = Join-Path $ROOT "logs"

# Создаём папки если нет
New-Item -ItemType Directory -Force -Path $PID_DIR | Out-Null
New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null

# Проверка, не запущено ли уже
$existing = Get-ChildItem $PID_DIR -Filter "*.pid" -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "⚠ Найдены PID-файлы в .pids/. Возможно уже запущено." -ForegroundColor Yellow
    Write-Host "  Сначала запусти .\stop_all.ps1 (или удали .pids вручную)." -ForegroundColor Yellow
    exit 1
}

$python = (Get-Command python).Source
$apiDir = Join-Path $ROOT "api"

function Start-BgProcess {
    param([string]$Name, [string[]]$Args, [string]$WorkDir)
    $logFile = Join-Path $LOG_DIR "$Name.console.log"
    Write-Host "▶ Запускаю $Name → $logFile" -ForegroundColor Cyan
    $proc = Start-Process -FilePath $python -ArgumentList $Args `
        -WorkingDirectory $WorkDir `
        -RedirectStandardOutput $logFile `
        -RedirectStandardError "$logFile.err" `
        -PassThru -WindowStyle Hidden
    $proc.Id | Out-File -FilePath (Join-Path $PID_DIR "$Name.pid") -Encoding ascii
    Write-Host "  PID = $($proc.Id)" -ForegroundColor Gray
}

# 1. Uvicorn (FastAPI)
Start-BgProcess -Name "uvicorn" -WorkDir $apiDir -Args @(
    "-m", "uvicorn", "main:app",
    "--host", "0.0.0.0", "--port", "8000"
)

# 2. Celery worker — обрабатывает таски из всех очередей
Start-BgProcess -Name "celery_worker" -WorkDir $apiDir -Args @(
    "-m", "celery", "-A", "celery_app", "worker",
    "-l", "info", "-P", "threads", "-c", "40",
    "-Q", "plans,warmup,parsers,ai_dialogs,high_priority,bulk_actions,subscribe"
)

# 3. Celery beat — отдельный планировщик. Шлёт dispatch_plans каждые 60с.
Start-BgProcess -Name "celery_beat" -WorkDir $apiDir -Args @(
    "-m", "celery", "-A", "celery_app", "beat",
    "-l", "info"
)

Start-Sleep -Seconds 2
Write-Host ""
Write-Host "✅ Запущено. PID-файлы в $PID_DIR" -ForegroundColor Green
Write-Host "   Логи: $LOG_DIR\*.console.log" -ForegroundColor Gray
Write-Host "   API: http://localhost:8000" -ForegroundColor Gray
Write-Host ""
Write-Host "Чтобы остановить всё разом: .\stop_all.ps1" -ForegroundColor Yellow
