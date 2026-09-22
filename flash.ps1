# flash.ps1 - sobrat vsyo v odnu papku dlya fleshki
#
# Klodyot v C:\ufogram-flash\<data> polnyy komplekt:
#
#   kod        - git-bundle so vsey istoriey vetok i kommitov.
#                Eto vazhno: push na GitHub zablokirovan, i kod
#                sushchestvuet tolko na etom diske.
#   baza       - dump Postgres
#   pamyat     - vygruzka Redis
#   sessii     - fayly vhoda v Telegram (nezamenimy)
#   nastroyki  - .env s klyuchami
#   instrukciya
#
# V konce - odin ZIP, kotoryy mozhno prosto skopirovat na fleshku.

$ErrorActionPreference = "Continue"
$repo = $PSScriptRoot
$root = "C:\ufogram-flash"
$stamp = Get-Date -Format "yyyy-MM-dd"
$dest = Join-Path $root $stamp
New-Item -ItemType Directory -Force -Path $dest | Out-Null

Write-Host ""
Write-Host "Sobirayu komplekt dlya fleshki -> $dest" -ForegroundColor Cyan
Write-Host ""

# --- 1. Kod: git bundle ---
# Bundle - eto ves repozitoriy v odnom fayle: vetki, kommity, istoriya.
# Vosstanavlivaetsya obychnym git clone iz etogo fayla.
Write-Host "[1/6] Kod (git bundle)..." -NoNewline
Push-Location $repo
git bundle create (Join-Path $dest "ufogram-code.bundle") --all 2>$null | Out-Null
Pop-Location
$b = Get-Item (Join-Path $dest "ufogram-code.bundle") -ErrorAction SilentlyContinue
if ($b) { Write-Host (" gotovo, {0:N0} MB" -f ($b.Length/1MB)) -ForegroundColor Green }
else { Write-Host " NE VYSHLO" -ForegroundColor Red }

# --- 2. Baza ---
Write-Host "[2/6] Baza dannyh..." -NoNewline
$pgdump = "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe"
if (-not (Test-Path $pgdump)) {
    $f = Get-ChildItem "C:\Program Files\PostgreSQL" -Recurse -Filter pg_dump.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($f) { $pgdump = $f.FullName }
}
if (Test-Path $pgdump) {
    $env:PGPASSWORD = "gramgpt"
    & $pgdump -h localhost -p 5432 -U gramgpt -d gramgpt -Fc -f (Join-Path $dest "gramgpt.dump") 2>$null
    $env:PGPASSWORD = $null
    $sz = Get-Item (Join-Path $dest "gramgpt.dump") -ErrorAction SilentlyContinue
    if ($sz) { Write-Host (" gotovo, {0:N0} MB" -f ($sz.Length/1MB)) -ForegroundColor Green }
    else { Write-Host " NE VYSHLO" -ForegroundColor Red }
} else { Write-Host " pg_dump ne nayden" -ForegroundColor Red }

# --- 3. Pamyat ---
Write-Host "[3/6] Pamyat Redis..." -NoNewline
$n = python (Join-Path $repo "dump_redis.py") (Join-Path $dest "redis-keys.json") 2>$null
if ($n) { Write-Host " gotovo, klyuchey $n" -ForegroundColor Green }
else { Write-Host " NE VYSHLO" -ForegroundColor Red }

# --- 4. Sessii ---
Write-Host "[4/6] Sessii Telegram..." -NoNewline
$cnt = 0
foreach ($p in @("$repo\api\sessions", "$repo\sessions")) {
    if (Test-Path $p) {
        Copy-Item $p -Destination (Join-Path $dest "sessions") -Recurse -Force -ErrorAction SilentlyContinue
        $cnt += (Get-ChildItem $p -File -Recurse -ErrorAction SilentlyContinue).Count
    }
}
if ($cnt) { Write-Host " gotovo, faylov $cnt" -ForegroundColor Green }
else { Write-Host " ne naydeny" -ForegroundColor Yellow }

# --- 5. Nastroyki i instrukciya ---
Write-Host "[5/6] Nastroyki i instrukciya..." -NoNewline
foreach ($f in @("api\.env", ".env")) {
    $p = Join-Path $repo $f
    if (Test-Path $p) { Copy-Item $p (Join-Path $dest ($f -replace '\\','_')) -Force }
}
Copy-Item (Join-Path $repo "VOSSTANOVLENIE.md") $dest -Force -ErrorAction SilentlyContinue
Write-Host " gotovo" -ForegroundColor Green

# --- 6. Odin arhiv ---
Write-Host "[6/6] Upakovyvayu v ZIP..." -NoNewline
$zip = Join-Path $root ("ufogram-" + $stamp + ".zip")
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $dest "*") -DestinationPath $zip -CompressionLevel Optimal
$z = Get-Item $zip -ErrorAction SilentlyContinue
if ($z) { Write-Host (" gotovo, {0:N0} MB" -f ($z.Length/1MB)) -ForegroundColor Green }
else { Write-Host " NE VYSHLO" -ForegroundColor Red }

Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "  NA FLESHKU KOPIRUY ETOT FAYL:" -ForegroundColor Cyan
Write-Host "  $zip" -ForegroundColor Yellow
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Vnutri - kod, baza, sessii, klyuchi i instrukciya."
Write-Host "Chtoby vsyo vernut: otday etot arhiv Klodu."
