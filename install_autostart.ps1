# GramGPT -- установка/переустановка автозапуска через Планировщик заданий.
#
# Создаёт задание "GramGPT Autostart", которое при ВХОДЕ В СИСТЕМУ (после
# того как ты ввёл пароль и попал на рабочий стол) запускает autostart.ps1.
#
# Почему "при входе", а не "при старте системы":
#   - тип входа Interactive => пароль НИГДЕ не хранится (безопасно);
#   - задание работает в твоём сеансе, видит сеть/прокси как обычно.
# Цена: после перезагрузки надо один раз залогиниться. Дальше всё само.
#
# Запуск (один раз):  powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
# Удалить автозапуск:  Unregister-ScheduledTask -TaskName "GramGPT Autostart" -Confirm:$false

$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot
$autostart = Join-Path $ROOT "autostart.ps1"
$TASK = "GramGPT Autostart"
$user = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $autostart)) { throw "не найден autostart.ps1 рядом" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$autostart`"" `
    -WorkingDirectory $ROOT

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user

# LogonType Interactive — пароль не требуется и не сохраняется.
# RunLevel Limited — обычный уровень: регистрация не требует админа, а все
# действия (убить/прочитать свои же процессы, запись в папку проекта) и так
# идут от твоего имени.
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TASK -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "[OK] Задание '$TASK' создано." -ForegroundColor Green
Write-Host "     Триггер: при входе пользователя $user" -ForegroundColor Gray
Write-Host "     Запускает: $autostart" -ForegroundColor Gray
Write-Host ""
Write-Host "Проверить руками прямо сейчас (не дожидаясь перезагрузки):" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask -TaskName '$TASK'" -ForegroundColor Gray
Write-Host "Посмотреть лог:" -ForegroundColor Yellow
Write-Host "  Get-Content '$ROOT\logs\autostart.log' -Tail 40" -ForegroundColor Gray
