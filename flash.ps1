# flash.ps1 - sobrat vsyo v odnu papku dlya fleshki
#
# Klodyot v C:\ufogram-flash\<data> polnyy komplekt:
#
#   kod         - git-bundle so vsey istoriey vetok i kommitov.
#   baza        - dump Postgres (accounts, channels, kampanii, ochered).
#   pamyat      - vygruzka Redis.
#   sessii      - fayly vhoda v Telegram (nezamenimy).
#   nastroyki   - .env s klyuchami.
#   pamyat-kloda- .claude memory: bez neyo novyy chat ne znaet kontekst.
#   bot         - bot-udalyalka (tg_selfdestruct_bot / tg_cleanbot_server).
#   ssh         - klyuch k serveru cleanbot (passwordless dostup).
#   akkaunty    - chitaemyy spisok akkauntov/kanalov iz bazy.
#   instrukciya - VOSSTANOVLENIE.md
#
# V konce - odin ZIP. Esli vstavlena fleshka E:\ - kopiya kladyotsya tuda.

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
Write-Host "[1/8] Kod (git bundle)..." -NoNewline
Push-Location $repo
git bundle create (Join-Path $dest "ufogram-code.bundle") --all 2>$null | Out-Null
Pop-Location
$b = Get-Item (Join-Path $dest "ufogram-code.bundle") -ErrorAction SilentlyContinue
if ($b) { Write-Host (" gotovo, {0:N0} MB" -f ($b.Length/1MB)) -ForegroundColor Green }
else { Write-Host " NE VYSHLO" -ForegroundColor Red }

# --- 2. Baza ---
Write-Host "[2/8] Baza dannyh..." -NoNewline
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

# --- 3. Pamyat Redis ---
Write-Host "[3/8] Pamyat Redis..." -NoNewline
$n = python (Join-Path $repo "dump_redis.py") (Join-Path $dest "redis-keys.json") 2>$null
if ($n) { Write-Host " gotovo, klyuchey $n" -ForegroundColor Green }
else { Write-Host " NE VYSHLO" -ForegroundColor Red }

# --- 4. Sessii ---
Write-Host "[4/8] Sessii Telegram..." -NoNewline
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
Write-Host "[5/8] Nastroyki i instrukciya..." -NoNewline
foreach ($f in @("api\.env", ".env")) {
    $p = Join-Path $repo $f
    if (Test-Path $p) { Copy-Item $p (Join-Path $dest ($f -replace '\\','_')) -Force }
}
Copy-Item (Join-Path $repo "VOSSTANOVLENIE.md") $dest -Force -ErrorAction SilentlyContinue
Write-Host " gotovo" -ForegroundColor Green

# --- 6. Pamyat Kloda + bot + ssh + spisok akkauntov ---
Write-Host "[6/8] Pamyat Kloda, bot, ssh, akkaunty..." -NoNewline

# 6a. Pamyat Kloda (.claude memory). Bez neyo novyy chat nachinaet s nulya.
$mem = "$env:USERPROFILE\.claude\projects\c--Users-ufo001-source-repos-tg-manager1\memory"
if (Test-Path $mem) {
    Copy-Item $mem (Join-Path $dest "claude-memory") -Recurse -Force -ErrorAction SilentlyContinue
}

# 6b. Papki bota-udalyalki (vne repozitoriya).
New-Item -ItemType Directory -Force -Path (Join-Path $dest "bot") | Out-Null
foreach ($bf in @("$env:USERPROFILE\tg_selfdestruct_bot", "$env:USERPROFILE\tg_cleanbot_server")) {
    if (Test-Path $bf) { Copy-Item $bf (Join-Path $dest "bot") -Recurse -Force -ErrorAction SilentlyContinue }
}
Get-ChildItem (Join-Path $dest "bot") -Recurse -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @(".venv", "__pycache__") } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 6c. SSH-klyuch k serveru cleanbot.
New-Item -ItemType Directory -Force -Path (Join-Path $dest "ssh") | Out-Null
foreach ($kf in @("$env:USERPROFILE\.ssh\cleanbot_deploy", "$env:USERPROFILE\.ssh\cleanbot_deploy.pub")) {
    if (Test-Path $kf) { Copy-Item $kf (Join-Path $dest "ssh") -Force -ErrorAction SilentlyContinue }
}

# 6d. Chitaemyy spisok akkauntov/kanalov iz bazy (best-effort).
$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py (Join-Path $repo "export_accounts.py") (Join-Path $dest "accounts-export.json") 2>$null | Out-Null

Write-Host " gotovo" -ForegroundColor Green

# --- 7. Odin arhiv ---
Write-Host "[7/8] Upakovyvayu v ZIP..." -NoNewline
$zip = Join-Path $root ("ufogram-" + $stamp + ".zip")
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $dest "*") -DestinationPath $zip -CompressionLevel Optimal
$z = Get-Item $zip -ErrorAction SilentlyContinue
if ($z) { Write-Host (" gotovo, {0:N0} MB" -f ($z.Length/1MB)) -ForegroundColor Green }
else { Write-Host " NE VYSHLO" -ForegroundColor Red }

# --- 8. Kopiya na fleshku E:\ (esli vstavlena) ---
if (Test-Path "E:\") {
    Write-Host "[8/8] Kopiruyu na fleshku E:\ ..." -NoNewline
    Copy-Item $zip "E:\" -Force -ErrorAction SilentlyContinue
    $eflash = "E:\ufogram-backup-$stamp"
    if (Test-Path $eflash) { Remove-Item $eflash -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path $eflash | Out-Null
    Copy-Item (Join-Path $dest "*") $eflash -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host " gotovo" -ForegroundColor Green
} else {
    Write-Host "[8/8] Fleshka E:\ ne vstavlena - skopiruy ZIP vruchnuyu" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "  NA FLESHKU KOPIRUY ETOT FAYL:" -ForegroundColor Cyan
Write-Host "  $zip" -ForegroundColor Yellow
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Vnutri: kod, baza, sessii, klyuchi, PAMYAT KLODA, bot, ssh, instrukciya."
Write-Host "Chtoby vsyo vernut: otday etot arhiv Klodu i skazhi 'vosstanavlivay'."
