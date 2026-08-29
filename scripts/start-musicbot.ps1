Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Starts the TelegramMusicBot stack:
#   1. musicbot.py        - local HTTP render server on 127.0.0.1:8710
#   2. telegram_poller.py - long-polls Telegram getUpdates and calls the server
#
# Idempotent: skips anything already running. Launched at logon by the
# Startup-folder shortcut TelegramMusicBot.vbs, and safe to run by hand.
# ASCII-only on purpose - PS 5.1 mangles UTF-8-no-BOM files.

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$LogDir = Join-Path $Root ".runtime"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$Python = "C:\Users\Dubo\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $cmd) { throw "python.exe not found" }
    $Python = $cmd.Source
}

function Write-SupervisorLog {
    param([string]$Message)
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path (Join-Path $LogDir "supervisor.log") -Value "$stamp $Message" -Encoding utf8
}

Write-SupervisorLog "start-musicbot.ps1 invoked"

# Child processes inherit this - without it Python block-buffers stdout when
# redirected to a file and the .out.log files stay empty until exit.
$env:PYTHONUNBUFFERED = "1"

function Test-ScriptRunning {
    param([string]$ScriptName)
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        if ($p.CommandLine -and $p.CommandLine -like "*$ScriptName*") { return $true }
    }
    return $false
}

function Start-BotProcess {
    param([string]$ScriptName, [string]$LogPrefix)

    if (Test-ScriptRunning -ScriptName $ScriptName) {
        Write-SupervisorLog "$ScriptName already running - skipped"
        return
    }

    $out = Join-Path $LogDir "$LogPrefix.out.log"
    $err = Join-Path $LogDir "$LogPrefix.err.log"

    # Pass the bare script name and rely on -WorkingDirectory: the project path
    # contains a space ("Sustav Dev") and Start-Process does not quote -ArgumentList.
    Start-Process -FilePath $Python `
        -ArgumentList $ScriptName `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $out `
        -RedirectStandardError $err | Out-Null

    Write-SupervisorLog "started $ScriptName"
}

# 1. Render server first - the poller depends on it.
Start-BotProcess -ScriptName "musicbot.py" -LogPrefix "musicbot"

$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
    $listening = $null
    try { $listening = Get-NetTCPConnection -LocalPort 8710 -State Listen -ErrorAction Stop } catch { }
    if ($listening) { break }
    Start-Sleep -Seconds 2
}

# 2. Telegram listener.
Start-BotProcess -ScriptName "telegram_poller.py" -LogPrefix "telegram-poller"
