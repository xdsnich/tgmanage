# backup.ps1 - polnaya rezervnaya kopiya ufo.gram
#
# Kladyot v C:\ufogram-backup\<data> vsyo, bez chego sistemu ne podnyat:
#   baza      - ves Postgres so shemoy i dannymi
#   pamyat    - reestr ogranicheniy, normy, schyotchiki iz Redis
#   sessii    - fayly vhoda v Telegram, ih nelzya vosstanovit zanovo
#   nastroyki - .env s klyuchami LLM i dostupami k proksi
#
# Kod v kopiyu ne kladyom - on v git. No .env v git NET.

$ErrorActionPreference = "Continue"
$root = "C:\ufogram-backup"
$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$dest = Join-Path $root $stamp
New-Item -ItemType Directory -Force -Path $dest | Out-Null

Write-Host "Rezervnaya kopiya -> $dest" -ForegroundColor Cyan
Write-Host ""

# --- 1. Baza ---
$pgdump = "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe"
if (-not (Test-Path $pgdump)) {
    $f = Get-ChildItem "C:\Program Files\PostgreSQL" -Recurse -Filter pg_dump.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($f) { $pgdump = $f.FullName }
}
if (Test-Path $pgdump) {
    Write-Host "[1/4] Baza dannyh..." -NoNewline
    $env:PGPASSWORD = "gramgpt"
    & $pgdump -h localhost -p 5432 -U gramgpt -d gramgpt -Fc -f (Join-Path $dest "gramgpt.dump") 2>$null
    $env:PGPASSWORD = $null
    $sz = Get-Item (Join-Path $dest "gramgpt.dump") -ErrorAction SilentlyContinue
    if ($sz) { Write-Host (" gotovo, {0:N0} MB" -f ($sz.Length/1MB)) -ForegroundColor Green }
    else { Write-Host " NE VYSHLO" -ForegroundColor Red }
} else {
    Write-Host "[1/4] pg_dump ne nayden - baza NE sohranena" -ForegroundColor Red
}

# --- 2. Pamyat (Redis) ---
Write-Host "[2/4] Pamyat Redis..." -NoNewline
$out = Join-Path $dest "redis-keys.json"
Push-Location "$PSScriptRoot\api"
$n = python "$PSScriptRoot\dump_redis.py" $out 2>$null
Pop-Location
if ($n) { Write-Host " gotovo, klyuchey $n" -ForegroundColor Green }
else { Write-Host " NE VYSHLO" -ForegroundColor Red }

# --- 3. Sessii Telegram ---
Write-Host "[3/4] Sessii Telegram..." -NoNewline
$cnt = 0
foreach ($p in @("$PSScriptRoot\api\sessions", "$PSScriptRoot\sessions")) {
    if (Test-Path $p) {
        Copy-Item $p -Destination (Join-Path $dest "sessions") -Recurse -Force -ErrorAction SilentlyContinue
        $cnt += (Get-ChildItem $p -File -Recurse -ErrorAction SilentlyContinue).Count
    }
}
if ($cnt) { Write-Host " gotovo, faylov $cnt" -ForegroundColor Green }
else { Write-Host " papka sessiy ne naydena" -ForegroundColor Yellow }

# --- 4. Nastroyki ---
Write-Host "[4/4] Nastroyki..." -NoNewline
foreach ($f in @("api\.env", ".env")) {
    $p = Join-Path $PSScriptRoot $f
    if (Test-Path $p) { Copy-Item $p (Join-Path $dest ($f -replace '\\','_')) -Force }
}
Copy-Item (Join-Path $PSScriptRoot "VOSSTANOVLENIE.md") $dest -Force -ErrorAction SilentlyContinue
Write-Host " gotovo" -ForegroundColor Green

$mb = (Get-ChildItem $dest -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ""
Write-Host ("Kopiya gotova: {0}  ({1:N0} MB)" -f $dest, $mb) -ForegroundColor Cyan
Write-Host "Otday etu papku Klodu - on podnimet vsyo obratno." -ForegroundColor Cyan

Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.CreationTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
