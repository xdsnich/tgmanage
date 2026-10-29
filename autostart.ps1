# GramGPT -- автозапуск после перезагрузки Windows.
#
# Что делает по шагам:
#   1. Ждёт, пока поднимутся службы Postgres (5432) и Redis (6379).
#   2. Добивает выживших зомби (celery/uvicorn/subscribe_run), если есть.
#   3. Удаляет устаревшие .pids (после ребута процессы мертвы, файлы остались).
#   4. Снимает застрявшие замки realtime в Redis (иначе realtime молчит).
#   5. Запускает стек: start_all.ps1 (uvicorn + celery worker + beat).
#      Realtime поднимается сам, т.к. кампания 148 активна, а beat раздаёт планы.
#   6. Запускает подписчик subscribe_run.py в "лёгком" режиме (не душит realtime).
#
# Вызывается Планировщиком заданий при входе в систему (см. install_autostart.ps1).
# Лог: logs\autostart.log

$ErrorActionPreference = "Continue"
$ROOT   = $PSScriptRoot
$API    = Join-Path $ROOT "api"
$PYTHON = Join-Path $ROOT ".venv\Scripts\python.exe"
$LOGDIR = Join-Path $ROOT "logs"
New-Item -ItemType Directory -Force -Path $LOGDIR | Out-Null
$LOG = Join-Path $LOGDIR "autostart.log"

function Записать($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "dd.MM HH:mm:ss"), $msg
    $line | Out-File -FilePath $LOG -Append -Encoding utf8
    Write-Host $line
}

Записать "=== АВТОЗАПУСК начат ==="

# 1. Ждём службы (до ~90с). После холодного старта они могут ещё грузиться.
foreach ($port in 5432, 6379) {
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        $ok = Test-NetConnection -ComputerName 127.0.0.1 -Port $port -WarningAction SilentlyContinue -InformationLevel Quiet
        if ($ok) { break }
        Start-Sleep -Seconds 3
    }
    if ($ok) { Записать "порт $port готов" }
    else     { Записать "ВНИМАНИЕ: порт $port не поднялся за 90с — продолжаю всё равно" }
}

# 2. Добить зомби от прошлой сессии (идемпотентность: если задание сработает
#    повторно или стек уже запущен — сначала чистим).
$zombies = Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -match "celery_app\s+(worker|beat)") -or
    ($_.CommandLine -match "uvicorn\s+main:app") -or
    ($_.CommandLine -match "subscribe_run\.py")
}
foreach ($z in $zombies) {
    Записать "убит зомби PID=$($z.ProcessId)"
    Stop-Process -Id $z.ProcessId -Force -ErrorAction SilentlyContinue
}

# 3. Устаревшие .pids (процессы уже мертвы — просто удаляем папку)
$pidDir = Join-Path $ROOT ".pids"
if (Test-Path $pidDir) {
    Remove-Item $pidDir -Recurse -Force -ErrorAction SilentlyContinue
    Записать "старые .pids удалены"
}

# 4. Снять застрявшие замки realtime
try {
    Push-Location $API
    $out = & $PYTHON "..\tools\clear_locks.py" 2>&1 | Out-String
    Pop-Location
    foreach ($ln in $out.Trim().Split("`n")) { if ($ln.Trim()) { Записать ("  замки: " + $ln.Trim()) } }
} catch {
    Записать "не удалось снять замки: $_"
}

# 5. Поднять стек
try {
    $out = & (Join-Path $ROOT "start_all.ps1") 2>&1 | Out-String
    foreach ($ln in $out.Trim().Split("`n")) {
        $t = $ln.Trim()
        if ($t -match "PID|start\]|Stack started|WARN|Error") { Записать ("  стек: " + $t) }
    }
    Записать "start_all.ps1 отработал"
} catch {
    Записать "start_all.ps1 упал: $_"
}

# 6. Подписчик в лёгком режиме (маленькие пачки, большие паузы — чтобы
#    realtime получал свободные прокси между подписками).
$subOut = Join-Path $LOGDIR "subscribe_run.console.log"
$env:SUB_CAMPAIGN   = "148"
$env:SUB_PER_VISIT  = "2"
$env:SUB_ROUND_GAP  = "600"
try {
    $p = Start-Process -FilePath $PYTHON -ArgumentList @("..\tools\subscribe_run.py") `
        -WorkingDirectory $API `
        -RedirectStandardOutput (Join-Path $LOGDIR "subscribe_run.stdout.log") `
        -RedirectStandardError  $subOut `
        -PassThru -WindowStyle Hidden
    Записать "подписчик запущен PID=$($p.Id) (лёгкий режим: пачка 2, пауза 600с)"
} catch {
    Записать "подписчик не запустился: $_"
}

Записать "=== АВТОЗАПУСК завершён ==="
