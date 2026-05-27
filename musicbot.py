from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import shutil
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
    for raw_line in path.read_text(encoding="utf-8").splitlines():
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
    ollama_url: str = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
    ollama_model: str = os.environ.get("OLLAMA_MODEL", "llama3.1")
    ollama_timeout_seconds: int = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "30"))
    generator_command: str = os.environ.get("MUSIC_GENERATOR_COMMAND", "").strip()
    generator_timeout_seconds: int = int(os.environ.get("MUSIC_GENERATOR_TIMEOUT_SECONDS", "3600"))
    fallback_tone_seconds: int = int(os.environ.get("MUSICBOT_FALLBACK_TONE_SECONDS", "18"))


CONFIG = Config()
JOB_QUEUE: "queue.Queue[str]" = queue.Queue()
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
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


def update_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.update(fields)
        job["updated_at"] = now_iso()


def get_job(job_id: str) -> dict[str, Any] | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def slugify(value: str, fallback: str = "track") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return (slug or fallback)[:60]


def clamp_duration(value: Any) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError):
        duration = 90
    return max(10, min(duration, 600))


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


def fallback_music_brief(prompt: str, duration_seconds: int, reference_path: Path | None) -> dict[str, Any]:
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
        "reference_path": str(reference_path) if reference_path else "",
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


def normalize_with_ollama(prompt: str, duration_seconds: int, reference_path: Path | None) -> dict[str, Any]:
    system = (
        "You turn Telegram music requests into safe, structured prompts for an original music generator. "
        "If the user asks for a living artist or copyrighted song style, describe musical traits instead of naming "
        "or copying that artist. Return only JSON."
    )
    user_prompt = {
        "telegram_prompt": prompt,
        "requested_duration_seconds": duration_seconds,
        "has_reference_audio": reference_path is not None,
        "json_schema": {
            "title": "short filename-safe title",
            "duration_seconds": "10 to 600",
            "genre": "genre or blend",
            "mood": "comma-separated mood traits",
            "bpm": "integer tempo estimate",
            "instruments": ["instrument names"],
            "lyrics": "lyrics with section tags, or empty string for instrumental",
            "prompt": "complete original generation prompt",
            "safety_note": "one short note about originality",
        },
    }
    body = {
        "system": system,
        "prompt": json.dumps(user_prompt),
        "stream": False,
        "format": "json",
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
    brief = parse_ollama_json(str(result.get("response", "")))
    brief["duration_seconds"] = clamp_duration(brief.get("duration_seconds", duration_seconds))
    brief.setdefault("prompt", prompt)
    brief.setdefault("title", "AI music sketch")
    brief["reference_path"] = str(reference_path) if reference_path else ""
    brief["ollama_model"] = str(result.get("model", attempted[-1] if attempted else CONFIG.ollama_model))
    return brief


def build_music_brief(prompt: str, duration_seconds: int, reference_path: Path | None) -> dict[str, Any]:
    try:
        return normalize_with_ollama(prompt, duration_seconds, reference_path)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        brief = fallback_music_brief(prompt, duration_seconds, reference_path)
        brief["ollama_warning"] = str(error)
        return brief


def choose_output_path(brief: dict[str, Any], suffix: str = ".wav") -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    title = slugify(str(brief.get("title") or brief.get("genre") or "track"))
    return CONFIG.output_dir / f"{timestamp}-{title}{suffix}"


def write_prompt_files(job_dir: Path, brief: dict[str, Any]) -> tuple[Path, Path]:
    json_path = job_dir / "music_brief.json"
    text_path = job_dir / "prompt.txt"
    json_path.write_text(json.dumps(brief, indent=2), encoding="utf-8")
    text_path.write_text(str(brief.get("prompt", "")), encoding="utf-8")
    return json_path, text_path


def render_fallback_tone(output_path: Path, brief: dict[str, Any]) -> None:
    duration = min(clamp_duration(brief.get("duration_seconds")), CONFIG.fallback_tone_seconds)
    sample_rate = 44100
    digest = hashlib.sha256(str(brief.get("prompt", "")).encode("utf-8")).digest()
    root = 220 + digest[0] % 180
    intervals = [0, 3, 7, 10] if "minor" in str(brief).lower() else [0, 4, 7, 12]
    freqs = [root * (2 ** (interval / 12)) for interval in intervals]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for index in range(duration * sample_rate):
            t = index / sample_rate
            beat = 0.5 + 0.5 * math.sin(2 * math.pi * 1.6 * t)
            chord = sum(math.sin(2 * math.pi * freq * t) for freq in freqs) / len(freqs)
            melody = math.sin(2 * math.pi * freqs[(index // sample_rate) % len(freqs)] * 2 * t)
            value = 0.18 * chord + 0.08 * melody * beat
            envelope = min(1.0, t / 1.5, (duration - t) / 1.5)
            sample = int(max(-1.0, min(1.0, value * envelope)) * 32767)
            wav.writeframesraw(sample.to_bytes(2, "little", signed=True) * 2)


def run_generator_command(job_dir: Path, brief: dict[str, Any], reference_path: Path | None) -> Path:
    output_path = choose_output_path(brief)
    prompt_json, prompt_text = write_prompt_files(job_dir, brief)
    env = os.environ.copy()
    env.update(
        {
            "MUSICBOT_JOB_DIR": str(job_dir),
            "MUSICBOT_PROMPT_JSON": str(prompt_json),
            "MUSICBOT_PROMPT_TEXT": str(prompt_text),
            "MUSICBOT_OUTPUT_PATH": str(output_path),
            "MUSICBOT_REFERENCE_PATH": str(reference_path or ""),
            "MUSICBOT_DURATION_SECONDS": str(clamp_duration(brief.get("duration_seconds"))),
            "MUSICBOT_TITLE": str(brief.get("title", "AI music sketch")),
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
            list(job_dir.glob("*.wav")) + list(job_dir.glob("*.mp3")) + list(CONFIG.output_dir.glob("*.wav")) + list(CONFIG.output_dir.glob("*.mp3")),
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
        reference_path = resolve_reference(payload, job_dir)

        update_job(job_id, stage="normalizing_prompt")
        brief = build_music_brief(prompt, duration_seconds, reference_path)
        write_prompt_files(job_dir, brief)

        update_job(job_id, stage="generating_audio", brief=brief)
        if CONFIG.generator_command:
            output_path = run_generator_command(job_dir, brief, reference_path)
            mode = "command"
        else:
            output_path = choose_output_path(brief)
            render_fallback_tone(output_path, brief)
            mode = "fallback_tone"

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
                    "output_dir": str(CONFIG.output_dir),
                    "queue_depth": JOB_QUEUE.qsize(),
                    "time": now_iso(),
                },
            )
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
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "status_url": f"/jobs/{job_id}",
        }
        with JOBS_LOCK:
            JOBS[job_id] = job
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
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args))


def main() -> None:
    ensure_worker()
    server = ThreadingHTTPServer((CONFIG.host, CONFIG.port), MusicBotHandler)
    print(f"TelegramMusicBot listening at http://{CONFIG.host}:{CONFIG.port}")
    print(f"Output folder: {CONFIG.output_dir}")
    print(f"Generator mode: {'command' if CONFIG.generator_command else 'fallback_tone'}")
    server.serve_forever()


if __name__ == "__main__":
    main()
