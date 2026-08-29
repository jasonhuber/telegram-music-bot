from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def discover_ffmpeg_dir() -> Path | None:
    configured = env("FFMPEG_BIN_DIR")
    if configured:
        configured_path = Path(configured)
        if (configured_path / "ffmpeg.exe").exists():
            return configured_path

    found = shutil.which("ffmpeg")
    if found:
        return Path(found).parent

    candidates = [
        Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links",
    ]
    packages = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if packages.exists():
        candidates.extend(path.parent for path in packages.glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe"))

    for candidate in candidates:
        if (candidate / "ffmpeg.exe").exists():
            return candidate
    return None


def ensure_ffmpeg_on_path() -> None:
    ffmpeg_dir = discover_ffmpeg_dir()
    if not ffmpeg_dir:
        return

    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if str(ffmpeg_dir) not in path_parts:
        os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")


ensure_ffmpeg_on_path()


ACE_ROOT = Path(env("ACE_STEP_ROOT", r"C:\AI\ACE-Step-1.5"))
UV = Path(env("ACE_STEP_UV", r"C:\Users\Dubo\.local\bin\uv.exe"))
API_BASE = env("ACE_STEP_API_URL", "http://127.0.0.1:8001").rstrip("/")
JOB_DIR = Path(env("MUSICBOT_JOB_DIR", str(ACE_ROOT / "api_logs")))
LOG_PATH = JOB_DIR / "ace-step-wrapper.log"


def log(message: str) -> None:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")


def request_json(method: str, path: str, payload: dict | None = None, timeout: int = 60) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{API_BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def api_healthy() -> bool:
    try:
        request_json("GET", "/health", timeout=5)
        return True
    except Exception:
        return False


def start_api() -> None:
    if not ACE_ROOT.exists():
        raise FileNotFoundError(f"ACE-Step root does not exist: {ACE_ROOT}")
    if not UV.exists():
        raise FileNotFoundError(f"uv.exe does not exist: {UV}")

    log("Starting ACE-Step API")
    log_dir = ACE_ROOT / "api_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = (log_dir / "ace-step-api.out.log").open("a", encoding="utf-8", errors="replace")
    stderr = (log_dir / "ace-step-api.err.log").open("a", encoding="utf-8", errors="replace")
    subprocess.Popen(
        [
            str(UV),
            "run",
            "acestep-api",
            "--host",
            env("ACE_STEP_API_HOST", "127.0.0.1"),
            "--port",
            env("ACE_STEP_API_PORT", "8001"),
            "--no-init",
        ],
        cwd=str(ACE_ROOT),
        stdout=stdout,
        stderr=stderr,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )

    deadline = time.time() + 180
    while time.time() < deadline:
        if api_healthy():
            return
        time.sleep(3)
    raise TimeoutError("ACE-Step API did not become healthy")


def newest_generated_audio(submitted_at: float) -> Path | None:
    audio_dir = ACE_ROOT / ".cache" / "acestep" / "tmp" / "api_audio"
    if not audio_dir.exists():
        return None
    candidates = [
        path
        for path in audio_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".wav", ".mp3", ".flac", ".opus", ".aac", ".m4a"}
        and path.stat().st_mtime >= submitted_at - 10
        and path.stat().st_size > 1024
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def resolve_generated_path(value: str) -> Path | None:
    if not value:
        return None
    if value.startswith("/v1/audio?"):
        parsed = urllib.parse.urlparse(value)
        path_values = urllib.parse.parse_qs(parsed.query).get("path", [])
        if path_values:
            return Path(path_values[0])
    return Path(value)


def format_with_llm(prompt: str, lyrics: str, duration_seconds: float) -> tuple[str, str]:
    if env("ACE_STEP_USE_FORMAT", "true").lower() != "true":
        return prompt, lyrics

    try:
        response = request_json(
            "POST",
            "/format_input",
            {
                "prompt": prompt,
                "lyrics": lyrics,
                "param_obj": {"duration": duration_seconds},
            },
            timeout=180,
        )
        data = response.get("data") or {}
        formatted_prompt = str(data.get("caption") or prompt)
        formatted_lyrics = str(data.get("lyrics") or lyrics)
        log("Formatted prompt with ACE-Step LLM")
        return formatted_prompt, formatted_lyrics
    except Exception as exc:
        log(f"ACE-Step LLM format failed, using original prompt: {exc}")
        return prompt, lyrics


def build_arrangement_prompt(brief: dict, prompt: str, duration_seconds: float, has_reference: bool) -> str:
    parts = [prompt.strip()]
    genre = str(brief.get("genre") or "").strip()
    mood = str(brief.get("mood") or "").strip()
    variation_plan = str(brief.get("variation_plan") or env("MUSICBOT_VARIATION_PLAN") or "").strip()
    bpm = brief.get("bpm")
    instruments = brief.get("instruments")

    if genre:
        parts.append(f"Genre/blend: {genre}.")
    if mood:
        parts.append(f"Mood: {mood}.")
    if bpm:
        parts.append(f"Tempo target: about {bpm} BPM.")
    if isinstance(instruments, list) and instruments:
        parts.append("Instrumentation: " + ", ".join(str(item) for item in instruments[:8]) + ".")
    if variation_plan:
        parts.append(f"Long-form progression plan: {variation_plan}.")

    sections = "intro, main idea, variation, breakdown or lift, final phrase"
    parts.append(
        f"Create a complete {int(duration_seconds)} second arrangement with {sections}. "
        "Avoid a single repeated bar; introduce small rhythmic, melodic, textural, and drum-pattern changes every 8 to 16 bars. "
        "Keep transitions musical and end intentionally."
    )
    if has_reference:
        parts.append(
            "Use the uploaded audio as strong source material for rhythm, melody, timbre, or gesture, "
            "but develop it into a fuller original arrangement instead of copying a short loop verbatim."
        )
    return " ".join(part for part in parts if part)


def max_single_duration_seconds() -> int:
    return max(30, int(env("ACE_STEP_MAX_SINGLE_DURATION_SECONDS", "120")))


def long_segment_seconds() -> int:
    return max(30, int(env("ACE_STEP_LONG_SEGMENT_SECONDS", str(max_single_duration_seconds()))))


def long_crossfade_seconds() -> int:
    return max(0, int(env("ACE_STEP_LONG_CROSSFADE_SECONDS", "3")))


def should_segment_long_audio(duration_seconds: float) -> bool:
    if env("ACE_STEP_SEGMENT_LONG_AUDIO", "true").lower() in {"0", "false", "no"}:
        return False
    return duration_seconds > max_single_duration_seconds()


def timeline_label(start_seconds: int, end_seconds: int) -> str:
    def fmt(value: int) -> str:
        minutes, seconds = divmod(value, 60)
        return f"{minutes:02d}:{seconds:02d}"

    return f"{fmt(start_seconds)}-{fmt(end_seconds)}"


def segment_energy_role(index: int, count: int, variation_plan: str) -> str:
    position = index / max(count - 1, 1)
    plan = variation_plan.lower()
    directives: list[str] = []
    if index == 0:
        directives.append("intro and groove establishment, leave headroom for later sections")
    elif position < 0.25:
        directives.append("lock the main groove and add new percussion or synth details")
    elif position < 0.45:
        directives.append("first energy lift with wider drums, bass movement, and melodic hooks")
    elif position < 0.60:
        directives.append("breakdown or contrast section that changes texture without stopping momentum")
    elif position < 0.80:
        directives.append("second build into a stronger drop or peak section")
    elif index == count - 1:
        directives.append("final lift and intentional outro that resolves the long mix")
    else:
        directives.append("late-track variation with renewed groove and fresh counter-melody")

    if re.search(r"\b(faster|tempo|bpm|accelerat|speed)\b", plan):
        directives.append("nudge perceived tempo or rhythmic density upward compared with earlier segments")
    if re.search(r"\b(slower|half-time|slow|relax)\b", plan):
        directives.append("use a slower-feeling groove or half-time pocket for contrast")
    if re.search(r"\b(higher|lift|rise|bright|climb)\b", plan):
        directives.append("raise the synth register, open filters, and brighten the harmonic color")
    if re.search(r"\b(lower|deeper|dark|heavy|underground)\b", plan):
        directives.append("emphasize deeper bass, darker harmony, and heavier low-end weight")
    if re.search(r"\b(breakdowns?|drop|reset|minimal|strip)\b", plan):
        directives.append("make the transition identity clear with either a breakdown, reset, or drop")
    return "; ".join(directives)


def split_long_duration(duration_seconds: float) -> list[float]:
    crossfade = long_crossfade_seconds()
    segment_target = long_segment_seconds()
    if duration_seconds <= segment_target:
        return [duration_seconds]
    usable_seconds = max(15, segment_target - crossfade)
    count = max(2, int((duration_seconds + usable_seconds - 1) // usable_seconds))
    total_render_seconds = duration_seconds + crossfade * (count - 1)
    base = total_render_seconds / count
    durations = [base for _ in range(count)]
    durations[-1] += total_render_seconds - sum(durations)
    return [round(max(10.0, value), 2) for value in durations]


def build_segment_prompt(
    brief: dict,
    base_prompt: str,
    total_duration: float,
    segment_index: int,
    segment_count: int,
    segment_start: int,
    segment_end: int,
) -> str:
    variation_plan = str(brief.get("variation_plan") or env("MUSICBOT_VARIATION_PLAN") or "").strip()
    role = segment_energy_role(segment_index, segment_count, variation_plan)
    timeline = timeline_label(segment_start, min(segment_end, int(total_duration)))
    return (
        f"{base_prompt.strip()} "
        f"This is segment {segment_index + 1} of {segment_count} for a continuous long-form track, covering {timeline}. "
        f"Segment role: {role}. "
        "Keep the same core song identity, key center, sound palette, and rhythmic DNA as the full track. "
        "Make this segment musically self-contained but clearly part of a longer DJ-style progression. "
        "Avoid hard endings except on the final segment; leave material suitable for a smooth crossfade into the next section."
    )


def transcode_mp3_bitrate(path: Path) -> None:
    if path.suffix.lower() != ".mp3":
        return
    bitrate = env("ACE_STEP_MP3_BITRATE", "96k")
    if not bitrate:
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return

    temp_path = path.with_suffix(".tmp.mp3")
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(path),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            str(temp_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0 or not temp_path.exists() or temp_path.stat().st_size <= 1024:
        if temp_path.exists():
            temp_path.unlink()
        log(f"MP3 bitrate transcode skipped: {completed.stderr[-500:]}")
        return
    for _ in range(10):
        try:
            os.replace(temp_path, path)
            log(f"Transcoded MP3 to {bitrate}")
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.5)
    if temp_path.exists():
        temp_path.unlink()
    log(f"MP3 bitrate transcode could not replace output: {last_error}")


def build_ace_payload(
    prompt: str,
    lyrics: str,
    duration_seconds: float,
    audio_format: str,
    seed: int,
    reference_path: str,
    has_reference: bool,
) -> dict:
    payload = {
        "prompt": prompt,
        "lyrics": lyrics,
        "audio_duration": duration_seconds,
        "inference_steps": int(env("ACE_STEP_INFERENCE_STEPS", "24")),
        "guidance_scale": float(env("ACE_STEP_GUIDANCE_SCALE", "7.0")),
        "infer_method": env("ACE_STEP_INFER_METHOD", "ode"),
        "sampler_mode": env("ACE_STEP_SAMPLER_MODE", "euler"),
        "velocity_norm_threshold": float(env("ACE_STEP_VELOCITY_NORM_THRESHOLD", "2.0")),
        "thinking": env("ACE_STEP_THINKING", "false").lower() == "true",
        "use_cot_caption": env("ACE_STEP_USE_COT_CAPTION", "true").lower() == "true",
        "use_cot_language": env("ACE_STEP_USE_COT_LANGUAGE", "true").lower() == "true",
        "lm_temperature": float(env("ACE_STEP_LM_TEMPERATURE", "0.95")),
        "lm_cfg_scale": float(env("ACE_STEP_LM_CFG_SCALE", "2.2")),
        "lm_top_p": float(env("ACE_STEP_LM_TOP_P", "0.92")),
        "lm_repetition_penalty": float(env("ACE_STEP_LM_REPETITION_PENALTY", "1.15")),
        "use_random_seed": False,
        "seed": seed,
        "batch_size": 1,
        "audio_format": audio_format,
        "task_type": env("ACE_STEP_TASK_TYPE", "text2music"),
    }
    if has_reference:
        payload.update(
            {
                "task_type": env("ACE_STEP_REFERENCE_TASK_TYPE", "cover"),
                "src_audio_path": reference_path,
                "reference_audio_path": reference_path,
                "audio_cover_strength": float(env("ACE_STEP_AUDIO_COVER_STRENGTH", "0.82")),
                "cover_noise_strength": float(env("ACE_STEP_COVER_NOISE_STRENGTH", "0.82")),
            }
        )
        log(
            "Using uploaded audio as strong source "
            f"task={payload['task_type']} cover_strength={payload['audio_cover_strength']} "
            f"cover_noise_strength={payload['cover_noise_strength']}"
        )
    return payload


def wait_for_ace_generation(payload: dict) -> Path:
    submitted_at = time.time()
    log(f"Submitting ACE-Step job duration={payload.get('audio_duration')} seed={payload.get('seed')}")
    create_response = request_json("POST", "/release_task", payload, timeout=180)
    task_id = (create_response.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"ACE-Step did not return a task id: {create_response}")
    log(f"Created ACE-Step task {task_id}")

    deadline = time.time() + int(env("ACE_STEP_JOB_TIMEOUT_SECONDS", "7200"))
    last_progress = ""
    while time.time() < deadline:
        time.sleep(5)
        query_response = request_json("POST", "/query_result", {"task_id_list": [task_id]}, timeout=60)
        item = (query_response.get("data") or [{}])[0]
        status = int(item.get("status", 0))
        last_progress = str(item.get("progress_text") or last_progress)
        log(f"Task {task_id} status={status} progress={last_progress}")

        if status == 1:
            results = json.loads(item.get("result") or "[]")
            generated = resolve_generated_path(results[0].get("file", "")) if results else None
            if not generated or not generated.exists():
                generated = newest_generated_audio(submitted_at)
            if not generated or not generated.exists():
                raise RuntimeError(f"ACE-Step completed without a readable file: {item}")
            if generated.stat().st_size <= 1024:
                raise RuntimeError(f"ACE-Step completed but wrote an empty/invalid file: {generated}")
            return generated

        if status == 2:
            raise RuntimeError(f"ACE-Step generation failed. Last progress: {last_progress}; result={item.get('result')}")

    raise TimeoutError(f"ACE-Step generation timed out. Last progress: {last_progress}")


def copy_generated_audio(generated: Path, output_path: Path) -> None:
    shutil.copyfile(generated, output_path)
    if output_path.stat().st_size <= 1024:
        raise RuntimeError(f"ACE-Step completed but wrote an empty/invalid file: {generated}")


def codec_args_for_output(output_path: Path) -> list[str]:
    suffix = output_path.suffix.lower()
    if suffix == ".mp3":
        return ["-codec:a", "libmp3lame", "-b:a", env("ACE_STEP_MP3_BITRATE", "96k")]
    if suffix == ".wav":
        return ["-codec:a", "pcm_s16le"]
    if suffix in {".m4a", ".aac"}:
        return ["-codec:a", "aac", "-b:a", env("ACE_STEP_AAC_BITRATE", "96k")]
    return ["-codec:a", "libmp3lame", "-b:a", env("ACE_STEP_MP3_BITRATE", "96k")]


def stitch_segments(segment_paths: list[Path], output_path: Path) -> None:
    if len(segment_paths) == 1:
        copy_generated_audio(segment_paths[0], output_path)
        transcode_mp3_bitrate(output_path)
        return

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg is required to stitch long-form segment renders.")

    crossfade = long_crossfade_seconds()
    if crossfade > 0:
        command = [ffmpeg, "-y"]
        for path in segment_paths:
            command.extend(["-i", str(path)])
        filter_parts = []
        previous = "[0:a]"
        for index in range(1, len(segment_paths)):
            output_label = f"[a{index}]"
            filter_parts.append(f"{previous}[{index}:a]acrossfade=d={crossfade}:c1=tri:c2=tri{output_label}")
            previous = output_label
        command.extend(["-filter_complex", ";".join(filter_parts), "-map", previous])
        command.extend(codec_args_for_output(output_path))
        command.append(str(output_path))
    else:
        concat_file = output_path.with_suffix(".segments.txt")
        concat_file.write_text(
            "\n".join(f"file '{path.as_posix()}'" for path in segment_paths),
            encoding="utf-8",
        )
        command = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            *codec_args_for_output(output_path),
            str(output_path),
        ]

    completed = subprocess.run(command, capture_output=True, text=True, timeout=int(env("ACE_STEP_STITCH_TIMEOUT_SECONDS", "3600")))
    if completed.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 1024:
        raise RuntimeError(f"ffmpeg stitching failed:\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}")
    log(f"Stitched {len(segment_paths)} segment(s) into {output_path}")


def render_single(
    output_path: Path,
    brief: dict,
    prompt: str,
    lyrics: str,
    audio_format: str,
    duration_seconds: float,
    reference_path: str,
    has_reference: bool,
    seed: int,
) -> None:
    prompt = build_arrangement_prompt(brief, prompt, duration_seconds, has_reference)
    prompt, lyrics = format_with_llm(prompt, lyrics, duration_seconds)
    payload = build_ace_payload(prompt, lyrics, duration_seconds, audio_format, seed, reference_path, has_reference)
    generated = wait_for_ace_generation(payload)
    copy_generated_audio(generated, output_path)
    transcode_mp3_bitrate(output_path)


def render_long_form(
    output_path: Path,
    brief: dict,
    prompt: str,
    lyrics: str,
    audio_format: str,
    duration_seconds: float,
    reference_path: str,
    has_reference: bool,
    seed: int,
) -> None:
    base_prompt = build_arrangement_prompt(brief, prompt, duration_seconds, has_reference)
    segment_durations = split_long_duration(duration_seconds)
    segment_dir = JOB_DIR / "segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    log(
        f"Rendering long-form audio as {len(segment_durations)} segment(s): "
        f"target={duration_seconds}s segment_target={long_segment_seconds()}s crossfade={long_crossfade_seconds()}s"
    )

    segment_paths: list[Path] = []
    elapsed = 0.0
    for index, segment_duration in enumerate(segment_durations):
        segment_start = int(max(0, elapsed - long_crossfade_seconds() * index))
        segment_end = int(segment_start + segment_duration)
        segment_prompt = build_segment_prompt(
            brief,
            base_prompt,
            duration_seconds,
            index,
            len(segment_durations),
            segment_start,
            segment_end,
        )
        segment_lyrics = lyrics
        segment_prompt, segment_lyrics = format_with_llm(segment_prompt, segment_lyrics, segment_duration)
        segment_seed = seed + index * 9973
        payload = build_ace_payload(
            segment_prompt,
            segment_lyrics,
            segment_duration,
            audio_format,
            segment_seed,
            reference_path,
            has_reference,
        )
        generated = wait_for_ace_generation(payload)
        segment_path = segment_dir / f"segment-{index + 1:02d}.{audio_format}"
        copy_generated_audio(generated, segment_path)
        segment_paths.append(segment_path)
        elapsed += segment_duration
        log(f"Finished long-form segment {index + 1}/{len(segment_durations)} at {segment_path}")

    stitch_segments(segment_paths, output_path)


def main() -> int:
    if not api_healthy():
        start_api()

    prompt_json = Path(env("MUSICBOT_PROMPT_JSON"))
    prompt_text = Path(env("MUSICBOT_PROMPT_TEXT"))
    output_path = Path(env("MUSICBOT_OUTPUT_PATH"))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    brief = json.loads(prompt_json.read_text(encoding="utf-8"))
    prompt = prompt_text.read_text(encoding="utf-8")
    lyrics = str(brief.get("lyrics") or "[Instrumental]")
    audio_format = output_path.suffix.lstrip(".").lower() or "wav"
    duration_seconds = float(env("MUSICBOT_DURATION_SECONDS", "30"))
    reference_path = env("MUSICBOT_REFERENCE_PATH")
    has_reference = bool(reference_path and Path(reference_path).exists())
    seed = int(env("MUSICBOT_SEED", "1") or "1")
    log(f"Preparing ACE-Step render for {output_path} duration={duration_seconds}s")
    if should_segment_long_audio(duration_seconds):
        render_long_form(output_path, brief, prompt, lyrics, audio_format, duration_seconds, reference_path, has_reference, seed)
    else:
        render_single(output_path, brief, prompt, lyrics, audio_format, duration_seconds, reference_path, has_reference, seed)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"ERROR: {exc}")
        print(str(exc), file=sys.stderr)
        raise
