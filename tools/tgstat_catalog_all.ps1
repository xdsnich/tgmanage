# Каталоги TGStat, каналы от 10к, без повторов — одна «полоса» хостов.
#
# Полосы запускаются параллельно, каждая проходит свои хосты по очереди.
# Хосты разные, так что нагрузка на каждый сайт не растёт, а общая
# скорость — во столько раз, сколько полос. Повторы между полосами
# отсекает сама вставка: канал пишется, только если его ещё нет в базе.
# Каждый хост продолжит с того места, где остановился — место хранится
# в tools\tgstat_<хост>_state.json.
#
# Запуск одной полосы:
#   powershell -File tools\tgstat_catalog_all.ps1 -Hosts "tgstat.com,uk.tgstat.com"
param([string]$Hosts = "tgstat.ru,tgstat.com,uk.tgstat.com,kaz.tgstat.com,uz.tgstat.com,by.tgstat.com,kg.tgstat.com,in.tgstat.com",
      [int]$WaitPid = 0,
      [int]$Port = 9222,
      [switch]$Private)

$api = Join-Path $PSScriptRoot "..\api"
$py = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"

if ($WaitPid -gt 0) {
    Wait-Process -Id $WaitPid -ErrorAction SilentlyContinue
}

foreach ($h in ($Hosts -split ",")) {
    $h = $h.Trim()
    if (-not $h) { continue }
    $name = ($h -replace "\.tgstat\.com$", "") -replace "^tgstat\.", ""
    if ($Private) { $name = $name + "priv" }
    $out = Join-Path $PSScriptRoot ("tgstat_{0}_stdout.txt" -f $name)
    $err = Join-Path $PSScriptRoot ("tgstat_{0}_stderr.txt" -f $name)
    $extra = @()
    if ($Private) { $extra = @("--private") }
    Push-Location $api
    & $py "..\tools\tgstat_ru_catalog.py" --host $h --port $Port @extra 1> $out 2> $err
    Pop-Location
    Start-Sleep -Seconds 5
}
