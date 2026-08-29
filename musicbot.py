from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(ROOT / ".env")


def default_dropbox_output_dir() -> Path:
    return Path.home() / "Dropbox" / "AI Music"


@dataclass(frozen=True)
class Config:
    host: str = os.environ.get("MUSICBOT_HOST", "127.0.0.1")
    port: int = int(os.environ.get("MUSICBOT_PORT", "8710"))
    output_dir: Path = Path(os.environ.get("MUSICBOT_OUTPUT_DIR", str(default_dropbox_output_dir()))).expanduser()
    temp_dir: Path = Path(os.environ.get("MUSICBOT_TEMP_DIR", str(ROOT / ".runtime" / "temp"))).expanduser()
    history_db: Path = Path(os.environ.get("MUSICBOT_HISTORY_DB", str(ROOT / ".runtime" / "musicbot.sqlite3"))).expanduser()
    ollama_url: str = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
    ollama_model: str = os.environ.get("OLLAMA_MODEL", "llama3.1")
    ollama_timeout_seconds: int = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "30"))
    ollama_keep_alive: str = os.environ.get("OLLAMA_KEEP_ALIVE", "0s")
    generator_command: str = os.environ.get("MUSIC_GENERATOR_COMMAND", "").strip()
    generator_timeout_seconds: int = int(os.environ.get("MUSIC_GENERATOR_TIMEOUT_SECONDS", "3600"))
    output_format: str = os.environ.get("MUSICBOT_OUTPUT_FORMAT", "wav").strip().lower().lstrip(".") or "wav"
    default_quality: str = os.environ.get("MUSICBOT_DEFAULT_QUALITY", "draft").strip().lower() or "draft"
    max_duration_seconds: int = int(os.environ.get("MUSICBOT_MAX_DURATION_SECONDS", "1800"))
    fallback_tone_seconds: int = int(os.environ.get("MUSICBOT_FALLBACK_TONE_SECONDS", "18"))
    fallback_variation: bool = os.environ.get("MUSICBOT_FALLBACK_VARIATION", "true").lower() not in {"0", "false", "no"}


CONFIG = Config()
QUALITY_PRESETS: dict[str, dict[str, str]] = {
    "draft": {
        "ACE_STEP_INFERENCE_STEPS": "12",
        "ACE_STEP_SAMPLER_MODE": "euler",
        "ACE_STEP_MP3_BITRATE": "80k",
        "ACE_STEP_USE_FORMAT": "false",
        "ACE_STEP_VELOCITY_NORM_THRESHOLD": "2.0",
    },
    "better": {
        "ACE_STEP_INFERENCE_STEPS": "32",
        "ACE_STEP_SAMPLER_MODE": "heun",
        "ACE_STEP_MP3_BITRATE": "128k",
        "ACE_STEP_USE_FORMAT": "true",
        "ACE_STEP_VELOCITY_NORM_THRESHOLD": "2.0",
    },
}
JOB_QUEUE: "queue.Queue[str]" = queue.Queue()
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
HISTORY_LOCK = threading.Lock()
HISTORY_INITIALIZED_PATH: Path | None = None
WORKER_STARTED = False


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def json_response(handler: BaseHTTPRequestHandler, status: int, data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw)


def init_history_db() -> None:
    global HISTORY_INITIALIZED_PATH
    with HISTORY_LOCK:
        if HISTORY_INITIALIZED_PATH == CONFIG.history_db:
            return
        CONFIG.history_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(CONFIG.history_db) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    job_json TEXT NOT NULL,
                    parent_job_id TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT '',
                    variant_label TEXT NOT NULL DEFAULT '',
                    quality TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_job_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id)")
        HISTORY_INITIALIZED_PATH = CONFIG.history_db


def save_job(job: dict[str, Any]) -> None:
    init_history_db()
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    parent_job_id = str(job.get("parent_job_id") or payload.get("parent_job_id") or "")
    batch_id = str(job.get("batch_id") or payload.get("batch_id") or "")
    variant_label = str(job.get("variant_label") or payload.get("variant_label") or "")
    quality = str(job.get("quality") or payload.get("quality") or "")
    output_path = str(job.get("output_path") or "")
    created_at = str(job.get("created_at") or now_iso())
    updated_at = str(job.get("updated_at") or now_iso())
    completed_at = str(job.get("completed_at") or "")
    with sqlite3.connect(CONFIG.history_db) as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, status, stage, payload_json, job_json, parent_job_id, batch_id,
                variant_label, quality, output_path, created_at, updated_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                stage = excluded.stage,
                payload_json = excluded.payload_json,
                job_json = excluded.job_json,
                parent_job_id = excluded.parent_job_id,
                batch_id = excluded.batch_id,
                variant_label = excluded.variant_label,
                quality = excluded.quality,
                output_path = excluded.output_path,
                updated_at = excluded.updated_at,
                completed_at = excluded.completed_at
            """,
            (
                str(job["id"]),
                str(job.get("status") or "queued"),
                str(job.get("stage") or "queued"),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                json.dumps(job, ensure_ascii=False, sort_keys=True, default=str),
                parent_job_id,
                batch_id,
                variant_label,
                quality,
                output_path,
                created_at,
                updated_at,
                completed_at,
            ),
        )


def persist_job(job: dict[str, Any]) -> None:
    try:
        save_job(job)
    except Exception as error:  # noqa: BLE001 - persistence should not kill active renders.
        print(f"Could not persist job {job.get('id')}: {error}", file=sys.stderr)


def load_job(job_id: str) -> dict[str, Any] | None:
    init_history_db()
    with sqlite3.connect(CONFIG.history_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    job = json.loads(str(row["job_json"]))
    job.setdefault("id", row["id"])
    job.setdefault("status", row["status"])
    job.setdefault("stage", row["stage"])
    job.setdefault("payload", json.loads(str(row["payload_json"])))
    job.setdefault("status_url", f"/jobs/{row['id']}")
    return job


def list_recent_jobs(limit: int = 20) -> list[dict[str, Any]]:
    init_history_db()
    safe_limit = max(1, min(int(limit), 100))
    with sqlite3.connect(CONFIG.history_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (safe_limit,)).fetchall()
    jobs: list[dict[str, Any]] = []
    for row in rows:
        job = json.loads(str(row["job_json"]))
        job.setdefault("id", row["id"])
        job.setdefault("status", row["status"])
        job.setdefault("stage", row["stage"])
        job.setdefault("payload", json.loads(str(row["payload_json"])))
        job.setdefault("status_url", f"/jobs/{row['id']}")
        jobs.append(job)
    return jobs


def load_active_jobs() -> list[dict[str, Any]]:
    init_history_db()
    with sqlite3.connect(CONFIG.history_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at ASC"
        ).fetchall()
    jobs: list[dict[str, Any]] = []
    for row in rows:
        job = json.loads(str(row["job_json"]))
        job.setdefault("id", row["id"])
        job.setdefault("payload", json.loads(str(row["payload_json"])))
        job["status"] = "queued"
        job["stage"] = "queued_after_restart"
        job["updated_at"] = now_iso()
        jobs.append(job)
    return jobs


def recover_active_jobs() -> int:
    recovered = 0
    for job in load_active_jobs():
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        with JOBS_LOCK:
            if job_id in JOBS:
                continue
            JOBS[job_id] = job
        persist_job(job)
        JOB_QUEUE.put(job_id)
        recovered += 1
    return recovered


def update_job(job_id: str, **fields: Any) -> None:
    saved: dict[str, Any] | None = None
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.update(fields)
        job["updated_at"] = now_iso()
        saved = dict(job)
    persist_job(saved)


def get_job(job_id: str) -> dict[str, Any] | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            return dict(job)
    return load_job(job_id)


def slugify(value: str, fallback: str = "track") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return (slug or fallback)[:60]


def clamp_duration(value: Any) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError):
        duration = 90
    return max(10, min(duration, CONFIG.max_duration_seconds))


def infer_variation_plan(text: str, duration_seconds: int) -> str:
    cleaned = (text or "").lower()
    directives: list[str] = []
    explicit = re.search(
        r"\b(?:variation|variations|progression|progress|evolution|evolve|journey|arc)\s*[:=-]\s*(.+)",
        text or "",
        flags=re.IGNORECASE,
    )
    if explicit:
        directives.append(explicit.group(1).strip()[:300])
    if re.search(r"\b(faster|speed up|accelerat|tempo up|bpm up|quicker)\b", cleaned):
        directives.append("gradually increase tempo, drum density, and perceived urgency")
    if re.search(r"\b(slower|slow down|decelerate|tempo down|half[- ]time)\b", cleaned):
        directives.append("gradually slow or relax the groove with half-time sections")
    if re.search(r"\b(higher|rise|rising|lift|uplift|climb|brighter)\b", cleaned):
        directives.append("lift the synth register, open filters, and raise melodic energy over time")
    if re.search(r"\b(lower|deeper|darker|descend|heavier|underground)\b", cleaned):
        directives.append("move into deeper bass, darker harmony, and heavier low-end sections")
    if re.search(r"\b(build|buildup|build-up|crescendo|intensify|harder|bigger|energy)\b", cleaned):
        directives.append("build energy in waves with larger drops and denser percussion")
    if re.search(r"\b(breakdowns?|drop|drops|reset|quiet|strip back|minimal)\b", cleaned):
        directives.append("include breakdowns, stripped-back resets, and clear drops")
    if re.search(r"\b(chill|calm|softer|mellow|gentle|relax)\b", cleaned):
        directives.append("periodically reduce intensity with calmer melodic passages")

    if directives:
        return "; ".join(dict.fromkeys(directives))
    if duration_seconds >= 600:
        return (
            "long-form DJ-style progression: establish the groove, introduce subtle variations every 8 to 16 bars, "
            "build energy in waves, include breakdowns and drops, add mid-track contrast, then finish with a final lift and outro"
        )
    return "evolve continuously with clear section changes and avoid static looping"


def normalize_variation_plan(payload: dict[str, Any], prompt: str, duration_seconds: int) -> str:
    for key in ("variation_plan", "progression", "evolution", "arc"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return infer_variation_plan(prompt, duration_seconds)


def normalize_quality(value: Any) -> str:
    quality = str(value or CONFIG.default_quality or "draft").strip().lower()
    if quality in {"best", "final", "high", "hq", "master"}:
        return "better"
    if quality not in QUALITY_PRESETS:
        return "draft"
    return quality


def first_url(text: str) -> str | None:
    match = re.search(r"https?://\S+", text or "")
    return match.group(0).rstrip(").,;") if match else None


def download_reference(source_url: str, job_dir: Path) -> Path:
    parsed = urllib.parse.urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https source URLs are supported.")
    suffix = Path(parsed.path).suffix or ".bin"
    destination = job_dir / f"reference{suffix}"
    request = urllib.request.Request(source_url, headers={"User-Agent": "TelegramMusicBot/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    return destination


def resolve_reference(payload: dict[str, Any], job_dir: Path) -> Path | None:
    source_path = payload.get("source_path") or payload.get("file_path")
    if source_path:
        path = Path(str(source_path)).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Reference path does not exist: {path}")
        return path

    source_url = payload.get("source_url")
    if source_url:
        return download_reference(str(source_url), job_dir)
    return None


def fallback_music_brief(
    prompt: str,
    duration_seconds: int,
    reference_path: Path | None,
    variation_seed: int,
    variation_plan: str = "",
) -> dict[str, Any]:
    title_source = prompt.splitlines()[0] if prompt.strip() else "AI music sketch"
    traits = []
    lower_prompt = prompt.lower()
    for word in ["ambient", "cinematic", "synthwave", "lofi", "jazz", "techno", "house", "rock", "piano", "orchestral"]:
        if word in lower_prompt:
            traits.append(word)
    return {
        "title": title_source[:80],
        "duration_seconds": duration_seconds,
        "genre": traits[0] if traits else "instrumental",
        "mood": ", ".join(traits[1:]) if len(traits) > 1 else "expressive",
        "bpm": 96,
        "instruments": ["synth pad", "bass", "soft percussion"],
        "lyrics": "",
        "prompt": prompt.strip() or "Create an original instrumental music cue.",
        "variation_plan": variation_plan or infer_variation_plan(prompt, duration_seconds),
        "reference_path": str(reference_path) if reference_path else "",
        "seed": variation_seed,
        "safety_note": "Generated from musical traits rather than copying a specific artist.",
    }


def parse_ollama_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("Ollama did not return JSON.")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Ollama JSON response was not an object.")
    return parsed


def ollama_base_url() -> str:
    parsed = urllib.parse.urlparse(CONFIG.ollama_url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def list_ollama_models() -> list[str]:
    tags_url = f"{ollama_base_url()}/api/tags"
    request = urllib.request.Request(tags_url, method="GET")
    with urllib.request.urlopen(request, timeout=CONFIG.ollama_timeout_seconds) as response:
        result = json.loads(response.read().decode("utf-8"))
    models = result.get("models", [])
    names = []
    for model in models:
        if isinstance(model, dict) and model.get("name"):
            names.append(str(model["name"]))
    return names


def call_ollama(model: str, body: dict[str, Any]) -> dict[str, Any]:
    request_body = dict(body)
    request_body["model"] = model
    request_body.setdefault("keep_alive", CONFIG.ollama_keep_alive)
    request = urllib.request.Request(
        CONFIG.ollama_url,
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=CONFIG.ollama_timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ValueError(f"Ollama HTTP {error.code}: {detail[:500]}") from error


def normalize_with_ollama(
    prompt: str,
    duration_seconds: int,
    reference_path: Path | None,
    variation_seed: int,
    variation_plan: str,
) -> dict[str, Any]:
    system = (
        "You turn Telegram music requests into safe, structured prompts for an original music generator. "
        "If the user asks for a living artist or copyrighted song style, describe musical traits instead of naming "
        "or copying that artist. Write prompts that ask for a complete evolving arrangement, not a tiny repeated loop. "
        "Return only JSON."
    )
    user_prompt = {
        "telegram_prompt": prompt,
        "requested_duration_seconds": duration_seconds,
        "has_reference_audio": reference_path is not None,
        "variation_seed": variation_seed,
        "variation_plan": variation_plan,
        "json_schema": {
            "title": "short filename-safe title",
            "duration_seconds": f"10 to {CONFIG.max_duration_seconds}",
            "genre": "genre or blend",
            "mood": "comma-separated mood traits",
            "bpm": "integer tempo estimate",
            "instruments": ["instrument names"],
            "lyrics": "lyrics with section tags, or empty string for instrumental",
            "variation_plan": "how energy, tempo, pitch range, sections, and drops should progress over the full requested duration",
            "sections": ["optional timeline section descriptions for long-form arrangements"],
            "prompt": "complete original generation prompt with arrangement sections, changes over time, and anti-repetition guidance",
            "seed": "integer seed",
            "safety_note": "one short note about originality",
        },
    }
    body = {
        "system": system,
        "prompt": json.dumps(user_prompt),
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"num_predict": 600},
    }
    attempted: list[str] = []
    try:
        attempted.append(CONFIG.ollama_model)
        result = call_ollama(CONFIG.ollama_model, body)
    except ValueError as first_error:
        installed_models = [name for name in list_ollama_models() if name not in attempted]
        if not installed_models:
            raise first_error
        fallback_model = installed_models[0]
        attempted.append(fallback_model)
        result = call_ollama(fallback_model, body)
    model_text = str(result.get("response") or result.get("thinking") or "")
    brief = parse_ollama_json(model_text)
    brief["duration_seconds"] = clamp_duration(duration_seconds)
    brief["variation_plan"] = str(brief.get("variation_plan") or variation_plan)
    brief.setdefault("prompt", prompt)
    brief.setdefault("title", "AI music sketch")
    brief.setdefault("seed", variation_seed)
    brief["reference_path"] = str(reference_path) if reference_path else ""
    brief["ollama_model"] = str(result.get("model", attempted[-1] if attempted else CONFIG.ollama_model))
    return brief


def build_music_brief(
    prompt: str,
    duration_seconds: int,
    reference_path: Path | None,
    variation_seed: int,
    variation_plan: str,
) -> dict[str, Any]:
    try:
        return normalize_with_ollama(prompt, duration_seconds, reference_path, variation_seed, variation_plan)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        brief = fallback_music_brief(prompt, duration_seconds, reference_path, variation_seed, variation_plan)
        brief["ollama_warning"] = str(error)
        return brief


def choose_output_path(brief: dict[str, Any], suffix: str = ".wav") -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    title = brief_filename_stem(brief)
    return CONFIG.output_dir / f"{timestamp}-{title}{suffix}"


def choose_render_path(job_dir: Path, brief: dict[str, Any], suffix: str) -> Path:
    title = brief_filename_stem(brief)
    return job_dir / f"render-{title}{suffix}"


def brief_filename_stem(brief: dict[str, Any]) -> str:
    title = slugify(str(brief.get("title") or brief.get("genre") or "track"))
    quality = slugify(str(brief.get("quality") or ""), "")
    variant = slugify(str(brief.get("variant_label") or ""), "")
    extras = [value for value in (quality, variant) if value]
    if extras:
        return "-".join([title, *extras])[:90]
    return title


def publish_output(render_path: Path, brief: dict[str, Any]) -> Path:
    if not render_path.exists() or render_path.stat().st_size <= 1024:
        raise FileNotFoundError(f"Rendered audio is missing or empty: {render_path}")
    final_path = choose_output_path(brief, suffix=render_path.suffix)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(render_path, final_path)
    return final_path


def write_prompt_files(job_dir: Path, brief: dict[str, Any]) -> tuple[Path, Path]:
    json_path = job_dir / "music_brief.json"
    text_path = job_dir / "prompt.txt"
    json_path.write_text(json.dumps(brief, indent=2), encoding="utf-8")
    text_path.write_text(str(brief.get("prompt", "")), encoding="utf-8")
    return json_path, text_path


def render_fallback_tone(output_path: Path, brief: dict[str, Any]) -> None:
    duration = min(clamp_duration(brief.get("duration_seconds")), CONFIG.fallback_tone_seconds)
    sample_rate = 44100
    seed_source = f"{brief.get('prompt', '')}|{brief.get('seed', '')}"
    digest = hashlib.sha256(seed_source.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    root = 160 + rng.randint(0, 280)
    interval_sets = ([0, 3, 7, 10], [0, 4, 7, 11], [0, 2, 7, 9], [0, 5, 7, 12])
    intervals = rng.choice(interval_sets)
    freqs = [root * (2 ** (interval / 12)) for interval in intervals]
    beat_rate = rng.choice([1.1, 1.35, 1.6, 1.85, 2.2])
    wobble_rate = rng.uniform(0.07, 0.21)
    pan_rate = rng.uniform(0.11, 0.37)
    lead_index = rng.randrange(len(freqs))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for index in range(duration * sample_rate):
            t = index / sample_rate
            beat = 0.5 + 0.5 * math.sin(2 * math.pi * beat_rate * t)
            wobble = 0.85 + 0.15 * math.sin(2 * math.pi * wobble_rate * t)
            chord = sum(math.sin(2 * math.pi * freq * wobble * t) for freq in freqs) / len(freqs)
            melody_freq = freqs[(lead_index + index // max(sample_rate // 2, 1)) % len(freqs)] * rng.choice([1, 2])
            melody = math.sin(2 * math.pi * melody_freq * t)
            noise_tick = 0.18 if int(t * beat_rate * 4) % 4 == 0 else 0.0
            value = 0.16 * chord + 0.1 * melody * beat + noise_tick * math.sin(2 * math.pi * root * 0.5 * t)
            envelope = min(1.0, t / 1.5, (duration - t) / 1.5)
            left_pan = 0.75 + 0.25 * math.sin(2 * math.pi * pan_rate * t)
            right_pan = 1.0 - (left_pan - 0.75)
            left = int(max(-1.0, min(1.0, value * envelope * left_pan)) * 32767)
            right = int(max(-1.0, min(1.0, value * envelope * right_pan)) * 32767)
            wav.writeframesraw(left.to_bytes(2, "little", signed=True) + right.to_bytes(2, "little", signed=True))


def run_generator_command(job_dir: Path, brief: dict[str, Any], reference_path: Path | None, quality: str) -> Path:
    suffix = "." + CONFIG.output_format
    output_path = choose_render_path(job_dir, brief, suffix=suffix)
    prompt_json, prompt_text = write_prompt_files(job_dir, brief)
    env = os.environ.copy()
    env.update(QUALITY_PRESETS.get(quality, QUALITY_PRESETS["draft"]))
    env.update(
        {
            "MUSICBOT_JOB_DIR": str(job_dir),
            "MUSICBOT_PROMPT_JSON": str(prompt_json),
            "MUSICBOT_PROMPT_TEXT": str(prompt_text),
            "MUSICBOT_OUTPUT_PATH": str(output_path),
            "MUSICBOT_REFERENCE_PATH": str(reference_path or ""),
            "MUSICBOT_DURATION_SECONDS": str(clamp_duration(brief.get("duration_seconds"))),
            "MUSICBOT_VARIATION_PLAN": str(brief.get("variation_plan") or ""),
            "MUSICBOT_QUALITY": quality,
            "MUSICBOT_TITLE": str(brief.get("title", "AI music sketch")),
            "MUSICBOT_SEED": str(brief.get("seed", "")),
        }
    )

    command = CONFIG.generator_command
    for key, value in env.items():
        if key.startswith("MUSICBOT_"):
            command = command.replace("{" + key.lower().removeprefix("musicbot_") + "}", subprocess.list2cmdline([value]))
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        env=env,
        shell=True,
        capture_output=True,
        text=True,
        timeout=CONFIG.generator_timeout_seconds,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Generator command failed.\n"
            f"Command: {CONFIG.generator_command}\n"
            f"STDOUT:\n{completed.stdout}\n"
            f"STDERR:\n{completed.stderr}"
        )
    if not output_path.exists():
        candidates = sorted(
            list(job_dir.glob("*.wav")) + list(job_dir.glob("*.mp3")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise FileNotFoundError(f"Generator finished but did not create {output_path}")
        output_path = candidates[0]
    return output_path


def process_job(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    payload = dict(job["payload"])
    job_dir = CONFIG.temp_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    update_job(job_id, status="running", stage="resolving_input")

    try:
        prompt = str(payload.get("prompt") or payload.get("text") or "").strip()
        duration_seconds = clamp_duration(payload.get("duration_seconds"))
        quality = normalize_quality(payload.get("quality") or payload.get("preset"))
        parent_job_id = str(payload.get("parent_job_id") or "")
        batch_id = str(payload.get("batch_id") or "")
        variant_label = str(payload.get("variant_label") or "")
        reference_path = resolve_reference(payload, job_dir)
        variation_seed = int(payload.get("seed") or random.randint(1, 2_147_483_647))
        variation_plan = normalize_variation_plan(payload, prompt, duration_seconds)
        update_job(
            job_id,
            quality=quality,
            parent_job_id=parent_job_id,
            batch_id=batch_id,
            variant_label=variant_label,
            duration_seconds=duration_seconds,
            variation_plan=variation_plan,
            job_dir=str(job_dir),
            started_at=now_iso(),
        )

        update_job(job_id, stage="normalizing_prompt")
        brief = build_music_brief(prompt, duration_seconds, reference_path, variation_seed, variation_plan)
        brief["quality"] = quality
        brief["parent_job_id"] = parent_job_id
        brief["batch_id"] = batch_id
        brief["variant_label"] = variant_label
        brief["variation_plan"] = variation_plan
        write_prompt_files(job_dir, brief)

        update_job(job_id, stage=f"generating_{quality}", brief=brief)
        if CONFIG.generator_command:
            render_path = run_generator_command(job_dir, brief, reference_path, quality)
            mode = "command"
        else:
            render_path = choose_render_path(job_dir, brief, suffix=".wav")
            render_fallback_tone(render_path, brief)
            mode = "fallback_tone"

        update_job(job_id, stage="publishing_to_dropbox", render_path=str(render_path), generator_mode=mode)
        output_path = publish_output(render_path, brief)
        update_job(
            job_id,
            status="completed",
            stage="done",
            output_path=str(output_path),
            generator_mode=mode,
            completed_at=now_iso(),
        )
    except Exception as error:  # noqa: BLE001 - job errors should be captured for polling.
        update_job(job_id, status="failed", stage="failed", error=str(error), completed_at=now_iso())


def worker() -> None:
    while True:
        job_id = JOB_QUEUE.get()
        try:
            process_job(job_id)
        finally:
            JOB_QUEUE.task_done()


def ensure_worker() -> None:
    global WORKER_STARTED
    if WORKER_STARTED:
        return
    CONFIG.output_dir.mkdir(parents=True, exist_ok=True)
    CONFIG.temp_dir.mkdir(parents=True, exist_ok=True)
    init_history_db()
    recovered = recover_active_jobs()
    if recovered:
        print(f"Recovered {recovered} queued/running music job(s) from history.", flush=True)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    WORKER_STARTED = True


class MusicBotHandler(BaseHTTPRequestHandler):
    server_version = "TelegramMusicBot/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler method name.
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "generator_mode": "command" if CONFIG.generator_command else "fallback_tone",
                    "default_quality": CONFIG.default_quality,
                    "output_dir": str(CONFIG.output_dir),
                    "history_db": str(CONFIG.history_db),
                    "queue_depth": JOB_QUEUE.qsize(),
                    "time": now_iso(),
                },
            )
            return
        if parsed.path in {"/history", "/jobs"}:
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            json_response(self, 200, {"jobs": list_recent_jobs(limit)})
            return
        if parsed.path.startswith("/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            job = get_job(job_id)
            if not job:
                json_response(self, 404, {"error": "Job not found"})
                return
            json_response(self, 200, job)
            return
        json_response(self, 404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler method name.
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/generate":
            json_response(self, 404, {"error": "Not found"})
            return
        try:
            payload = read_json(self)
        except json.JSONDecodeError as error:
            json_response(self, 400, {"error": f"Invalid JSON: {error}"})
            return

        prompt = str(payload.get("prompt") or payload.get("text") or "")
        if not prompt.strip() and not payload.get("source_url") and not payload.get("source_path") and not payload.get("file_path"):
            json_response(self, 400, {"error": "Send prompt, source_url, source_path, or file_path."})
            return

        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "status": "queued",
            "stage": "queued",
            "payload": payload,
            "parent_job_id": str(payload.get("parent_job_id") or ""),
            "batch_id": str(payload.get("batch_id") or ""),
            "variant_label": str(payload.get("variant_label") or ""),
            "quality": normalize_quality(payload.get("quality") or payload.get("preset")),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "status_url": f"/jobs/{job_id}",
        }
        with JOBS_LOCK:
            JOBS[job_id] = job
        persist_job(job)
        JOB_QUEUE.put(job_id)

        wait = bool(payload.get("wait"))
        if wait:
            deadline = time.time() + min(int(payload.get("wait_timeout_seconds", 600)), 600)
            while time.time() < deadline:
                current = get_job(job_id)
                if current and current.get("status") in {"completed", "failed"}:
                    json_response(self, 200 if current.get("status") == "completed" else 500, current)
                    return
                time.sleep(1)
        json_response(self, 202, job)

    def log_message(self, format: str, *args: Any) -> None:
        try:
            sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args))
            sys.stderr.flush()
        except Exception:
            return


def main() -> None:
    ensure_worker()
    server = ThreadingHTTPServer((CONFIG.host, CONFIG.port), MusicBotHandler)
    print(f"TelegramMusicBot listening at http://{CONFIG.host}:{CONFIG.port}")
    print(f"Output folder: {CONFIG.output_dir}")
    print(f"Generator mode: {'command' if CONFIG.generator_command else 'fallback_tone'}")
    server.serve_forever()


if __name__ == "__main__":
    main()
