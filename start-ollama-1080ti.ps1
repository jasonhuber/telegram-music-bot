param(
    [string]$OllamaExe = $env:OLLAMA_EXE,
    [string]$HostAddress = "127.0.0.1:11435",
    [string]$GpuUuid = "GPU-d73919a5-1b61-a97a-6a91-3e49e4dcbf01"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $OllamaExe) {
    $Command = Get-Command ollama -ErrorAction SilentlyContinue
    if ($Command) {
        $OllamaExe = $Command.Source
    }
}

if (-not $OllamaExe -or -not (Test-Path $OllamaExe)) {
    throw "Could not find ollama.exe. Set OLLAMA_EXE to the full path, then rerun this script."
}

$env:CUDA_VISIBLE_DEVICES = $GpuUuid
$env:OLLAMA_HOST = $HostAddress
$env:OLLAMA_KEEP_ALIVE = "0"
$env:OLLAMA_MAX_LOADED_MODELS = "1"
$env:OLLAMA_NUM_PARALLEL = "1"

Write-Host "Starting Ollama on $HostAddress pinned to $GpuUuid"
& $OllamaExe serve
