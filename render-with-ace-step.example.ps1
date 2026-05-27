Set-StrictMode -Version Latest

# Copy this file elsewhere, adjust it to your ACE-Step install, then point
# MUSIC_GENERATOR_COMMAND at your copy.

$AceStepRoot = "C:\AI\ACE-Step"
$Python = "$AceStepRoot\venv\Scripts\python.exe"
$Brief = Get-Content $env:MUSICBOT_PROMPT_JSON -Raw | ConvertFrom-Json
$Prompt = Get-Content $env:MUSICBOT_PROMPT_TEXT -Raw
$Output = $env:MUSICBOT_OUTPUT_PATH
$Duration = $env:MUSICBOT_DURATION_SECONDS
$Lyrics = if ($Brief.lyrics) { [string]$Brief.lyrics } else { "" }
$CheckpointPath = if ($env:ACE_STEP_CHECKPOINT_PATH) { $env:ACE_STEP_CHECKPOINT_PATH } else { "" }

Set-Location $AceStepRoot

$TempScript = Join-Path $env:MUSICBOT_JOB_DIR "run_ace_step.py"
@"
from acestep.pipeline_ace_step import ACEStepPipeline

pipeline = ACEStepPipeline(
    checkpoint_dir=r'''$CheckpointPath''',
    dtype='bfloat16',
    torch_compile=False,
    cpu_offload=True,
    overlapped_decode=True,
)

pipeline(
    audio_duration=int($Duration),
    prompt=r'''$Prompt''',
    lyrics=r'''$Lyrics''',
    infer_step=27,
    guidance_scale=15.0,
    scheduler_type='euler',
    cfg_type='apg',
    omega_scale=10.0,
    manual_seeds='',
    guidance_interval=0.5,
    guidance_interval_decay=0.0,
    min_guidance_scale=3.0,
    use_erg_tag=True,
    use_erg_lyric=True,
    use_erg_diffusion=True,
    oss_steps='',
    guidance_scale_text=0.0,
    guidance_scale_lyric=0.0,
    save_path=r'''$Output''',
)
"@ | Set-Content -Encoding UTF8 $TempScript

& $Python $TempScript

if (-not (Test-Path $Output)) {
    throw "ACE-Step did not create expected output: $Output"
}
