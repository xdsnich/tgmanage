# GramGPT — остановка всех процессов.
#
# Читает .pids/*.pid файлы и убивает каждый процесс.
# Это критично: celery beat — ОТДЕЛЬНЫЙ процесс, и если убить только
# uvicorn (например, Ctrl+C в одном окне), beat продолжит крутиться
# и слать dispatch_plans каждые 60 сек.
#
# Инцидент 2026-06-12 был частично вызван этим: пользователь "остановил
# прогу", но beat продолжал работать → накапливал отложенные планы,
# и при следующем запуске воркера всё стрельнуло burst'ом.

$ErrorActionPreference = "Continue"
$ROOT = $PSScriptRoot
$PID_DIR = Join-Path $ROOT ".pids"

if (-not (Test-Path $PID_DIR)) {
    Write-Host "Папки .pids нет — нечего останавливать." -ForegroundColor Yellow
    exit 0
}

$pids = Get-ChildItem $PID_DIR -Filter "*.pid" -ErrorAction SilentlyContinue
if (-not $pids) {
    Write-Host "PID-файлов нет." -ForegroundColor Yellow
    exit 0
}

foreach ($pidFile in $pids) {
    $name = $pidFile.BaseName
    $pidValue = (Get-Content $pidFile.FullName -Raw).Trim()
    if (-not $pidValue) { continue }
    try {
        $proc = Get-Process -Id $pidValue -ErrorAction Stop
        Write-Host "⏹ Убиваю $name (PID=$pidValue)" -ForegroundColor Cyan
        # Сначала graceful через CloseMainWindow, потом force
        $proc | Stop-Process -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Host "  ($name PID=$pidValue уже не работает)" -ForegroundColor Gray
    }
    Remove-Item $pidFile.FullName -Force -ErrorAction SilentlyContinue
}

# Подчищаем зомби-celery воркеров (они иногда форкаются под -P threads
# и не попадают в .pid). Стандартный паттерн в командной строке:
#   "celery_app worker" или "celery_app beat"
$stragglers = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match "celery_app\s+(worker|beat)" -or
                   $_.CommandLine -match "uvicorn\s+main:app" }

foreach ($s in $stragglers) {
    Write-Host "⚠ Зомби-процесс: PID=$($s.ProcessId) — $($s.Name)" -ForegroundColor Yellow
    Stop-Process -Id $s.ProcessId -Force -ErrorAction SilentlyContinue
}

# Удаляем папку .pids целиком, чтобы start_all.ps1 видел чистое состояние
Remove-Item $PID_DIR -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "✅ Остановлено." -ForegroundColor Green
