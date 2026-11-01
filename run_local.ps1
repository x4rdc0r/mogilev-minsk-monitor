# Локальный «быстрый» монитор: опрос каждые 45 секунд (GitHub cron даёт ~5 минут).
# Окно PowerShell должно быть открыто; состояние пишется в state.local.json.
#
# Запуск:
#   $env:TELEGRAM_BOT_TOKEN="123456:ABC..."; .\run_local.ps1
#   .\run_local.ps1 -IntervalSeconds 30

param(
    [int]$IntervalSeconds = 45
)

$ErrorActionPreference = "Stop"
$env:STATE_FILE = Join-Path $PSScriptRoot "state.local.json"

if (-not $env:TELEGRAM_BOT_TOKEN -and -not $env:DRY_RUN) {
    $env:TELEGRAM_BOT_TOKEN = Read-Host "Telegram bot token"
}

Write-Host "Мониторинг запущен, интервал $IntervalSeconds с. Ctrl+C для остановки." -ForegroundColor Green

while ($true) {
    $started = Get-Date
    python (Join-Path $PSScriptRoot "monitor.py")
    $elapsed = ((Get-Date) - $started).TotalSeconds
    $pause = [Math]::Max(5, $IntervalSeconds - [int]$elapsed)
    Start-Sleep -Seconds $pause
}
