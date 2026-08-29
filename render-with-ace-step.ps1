Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonWrapper = Join-Path $Root "render_with_ace_step.py"
$WingetFfmpeg = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"

if (Test-Path (Join-Path $WingetFfmpeg "ffmpeg.exe")) {
    $env:PATH = "$WingetFfmpeg;$env:PATH"
}

python $PythonWrapper
