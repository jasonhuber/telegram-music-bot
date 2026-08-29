from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".runtime"
OFFSET_PATH = STATE_DIR / "telegram_offset.txt"
INBOX_DIR = STATE_DIR / "telegram-inbox"
MUSICBOT_URL = os.environ.get("MUSICBOT_GENERATE_URL", "http://127.0.0.1:8710/generate")
DEFAULT_DURATION_SECONDS = int(os.environ.get("MUSICBOT_DEFAULT_DURATION_SECONDS", "120"))
MAX_TELEGRAM_DURATION_SECONDS = int(os.environ.get("MUSICBOT_MAX_TELEGRAM_DURATION_SECONDS", "1800"))
DRAFT_VARIANTS = int(os.environ.get("MUSICBOT_DRAFT_VARIANTS", "3"))
MAX_DRAFT_VARIANTS = int(os.environ.get("MUSICBOT_MAX_DRAFT_VARIANTS", "4"))
LONG_RENDER_SECONDS = int(os.environ.get("MUSICBOT_LONG_RENDER_SECONDS", "600"))
LONG_DRAFT_VARIANTS = int(os.environ.get("MUSICBOT_LONG_DRAFT_VARIANTS", "1"))
POLL_SECONDS = int(os.environ.get("TELEGRAM_POLL_SECONDS", "3"))
CHAT_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
CHAT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")
CHAT_TIMEOUT_SECONDS = int(os.environ.get("DUBO_CHAT_TIMEOUT_SECONDS", "45"))
CHAT_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "0s")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def telegram_token() -> str:
    load_dotenv(ROOT / ".env")
    token = os.environ.get("MUSICBOT_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set MUSICBOT_TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN before starting the poller.")
    return token


def post_json(url: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url: str, timeout: int = 60) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def telegram_call(token: str, method: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    return post_json(f"https://api.telegram.org/bot{token}/{method}", payload, timeout=timeout)


def telegram_multipart_call(
    token: str,
    method: str,
    payload: dict[str, Any],
    file_field: str,
    file_path: str | Path,
    timeout: int = 180,
) -> dict[str, Any]:
    path = Path(file_path)
    boundary = f"----TelegramMusicBot{int(time.time() * 1000)}"
    body = bytearray()

    for key, value in payload.items():
        if value is None:
            continue
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{path.name}"\r\n'.encode("utf-8")
    )
    body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
    body.extend(path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def telegram_file_url(token: str, file_path: str) -> str:
    return f"https://api.telegram.org/file/bot{token}/{file_path}"


def read_offset() -> int | None:
    try:
        value = OFFSET_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return int(value) if value else None


def write_offset(offset: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset), encoding="utf-8")


def strip_music_command(text: str) -> str:
    return re.sub(r"^/(music|song|loop|beat)(@\w+)?\s*", "", text, flags=re.IGNORECASE).strip()


def is_music_text(text: str) -> bool:
    cleaned = text.strip().lower()
    if re.match(r"^/(music|song|loop|beat)(@\w+)?(\s|$)", cleaned):
        return True
    if cleaned.startswith(("music:", "song:", "loop:", "beat:")):
        return True
    music_words = ("song", "track", "beat", "loop", "music", "synthwave", "lofi", "techno", "house", "ambient", "piano")
    action_words = ("make", "create", "generate", "compose", "turn", "remix")
    return any(word in cleaned for word in music_words) and any(word in cleaned for word in action_words)


def is_help_text(text: str) -> bool:
    return text.strip().lower() in {"/help", "help", "what can you do", "commands"}


def clean_music_prompt(text: str, has_audio: bool) -> str:
    cleaned = strip_music_command(text)
    for prefix in ("music:", "song:", "loop:", "beat:"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    if cleaned:
        return cleaned
    if has_audio:
        return (
            "Create an original track from this uploaded audio clip. Preserve its rhythmic, melodic, or timbral idea, "
            "then build a fuller arrangement with a clear intro, development, variation, and ending."
        )
    return "Create an original instrumental music cue."


def clamp_telegram_duration(seconds: int) -> int:
    return max(20, min(seconds, MAX_TELEGRAM_DURATION_SECONDS))


def parse_requested_duration(text: str) -> int | None:
    cleaned = text.lower()
    clock = re.search(r"\b(\d{1,2}):(\d{2})\b", cleaned)
    if clock:
        return clamp_telegram_duration(int(clock.group(1)) * 60 + int(clock.group(2)))

    match = re.search(r"\b(\d+(?:\.\d+)?)\s*[- ]?\s*(hours?|hrs?|h)\b", cleaned)
    if match:
        return clamp_telegram_duration(round(float(match.group(1)) * 3600))

    match = re.search(r"\b(\d+(?:\.\d+)?)\s*[- ]?\s*(minutes?|mins?|m)\b", cleaned)
    if match:
        return clamp_telegram_duration(round(float(match.group(1)) * 60))

    match = re.search(r"\b(\d+(?:\.\d+)?)\s*[- ]?\s*(seconds?|secs?|s)\b", cleaned)
    if match:
        return clamp_telegram_duration(round(float(match.group(1))))

    if "half hour" in cleaned or "half-hour" in cleaned or "thirty minute" in cleaned:
        return clamp_telegram_duration(30 * 60)
    if "one hour" in cleaned or "an hour" in cleaned:
        return clamp_telegram_duration(60 * 60)
    if "minute and a half" in cleaned or "a minute and a half" in cleaned:
        return 90
    return None


def parse_quality(text: str) -> str:
    cleaned = text.lower()
    if re.search(r"\b(better|best|final|high quality|hq|master)\b", cleaned):
        return "better"
    return "draft"


def parse_variation_plan(text: str) -> str:
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
        directives.append("lift synth register, open filters, and raise melodic energy")
    if re.search(r"\b(lower|deeper|darker|descend|heavier|underground)\b", cleaned):
        directives.append("move into deeper bass, darker harmony, and heavier low-end sections")
    if re.search(r"\b(build|buildup|build-up|crescendo|intensify|harder|bigger|energy)\b", cleaned):
        directives.append("build energy in waves with larger drops and denser percussion")
    if re.search(r"\b(breakdowns?|drop|drops|reset|quiet|strip back|minimal)\b", cleaned):
        directives.append("include breakdowns, stripped-back resets, and clear drops")
    if re.search(r"\b(chill|calm|softer|mellow|gentle|relax)\b", cleaned):
        directives.append("periodically reduce intensity with calmer melodic passages")
    return "; ".join(dict.fromkeys(directives))


def clamp_draft_variants(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 1
    return max(1, min(count, MAX_DRAFT_VARIANTS))


def draft_variant_labels(count: int) -> list[str]:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return [alphabet[index] for index in range(clamp_draft_variants(count))]


def draft_variant_count_for_message(message: dict[str, Any]) -> int:
    duration = int(message.get("duration_seconds") or DEFAULT_DURATION_SECONDS)
    if duration >= LONG_RENDER_SECONDS:
        return clamp_draft_variants(LONG_DRAFT_VARIANTS)
    return clamp_draft_variants(DRAFT_VARIANTS)


def safe_filename(value: str, fallback: str = "telegram-audio") -> str:
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return name or fallback


def message_file_descriptor(message: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(message.get("voice"), dict):
        voice = message["voice"]
        return {"file_id": voice.get("file_id"), "name": f"voice-{message.get('message_id')}.ogg"}
    if isinstance(message.get("audio"), dict):
        audio = message["audio"]
        name = audio.get("file_name") or f"audio-{message.get('message_id')}.mp3"
        return {"file_id": audio.get("file_id"), "name": name}
    document = message.get("document")
    if isinstance(document, dict) and str(document.get("mime_type", "")).startswith("audio/"):
        name = document.get("file_name") or f"audio-document-{message.get('message_id')}"
        return {"file_id": document.get("file_id"), "name": name}
    return None


def download_telegram_file(token: str, file_descriptor: dict[str, Any], target_seconds: int | None = None) -> str | None:
    file_id = file_descriptor.get("file_id")
    if not file_id:
        return None
    file_info = telegram_call(token, "getFile", {"file_id": file_id}, timeout=30)
    file_path = file_info.get("result", {}).get("file_path")
    if not file_path:
        return None
    suffix = Path(urllib.parse.urlparse(file_path).path).suffix or Path(file_descriptor["name"]).suffix or ".bin"
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    destination = INBOX_DIR / f"{int(time.time())}-{safe_filename(Path(file_descriptor['name']).stem)}{suffix}"
    request = urllib.request.Request(telegram_file_url(token, file_path), headers={"User-Agent": "TelegramMusicBot/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("wb") as output:
            output.write(response.read())
    return normalize_audio(destination, target_seconds=target_seconds)


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
        *(
            Path.home()
            / "AppData"
            / "Local"
            / "Microsoft"
            / "WinGet"
            / "Packages"
        ).glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def normalize_audio(path: Path, target_seconds: int | None = None) -> str:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return str(path)
    normalized = path.with_suffix(".normalized.wav")
    duration = max(10, min(int(target_seconds or DEFAULT_DURATION_SECONDS), 180))
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "44100",
            "-t",
            str(duration),
            str(normalized),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0 or not normalized.exists():
        print(f"ffmpeg normalization failed for {path}: {completed.stderr[-500:]}", flush=True)
        return str(path)
    return str(normalized)


def extract_message(update: dict[str, Any], token: str) -> dict[str, Any] | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    raw_text = str(message.get("text") or message.get("caption") or "").strip()
    rerender = re.match(r"^/(better|final)(@\w+)?\s+([a-fA-F0-9]{8,})", raw_text, flags=re.IGNORECASE)
    if chat_id and rerender:
        sender = message.get("from") or {}
        return {
            "intent": "rerender",
            "chat_id": chat_id,
            "message_id": message.get("message_id"),
            "parent_job_id": rerender.group(3),
            "quality": "better",
            "username": sender.get("username", ""),
        }

    duration_seconds = parse_requested_duration(raw_text) or DEFAULT_DURATION_SECONDS
    file_descriptor = message_file_descriptor(message)
    source_path = download_telegram_file(token, file_descriptor, duration_seconds) if file_descriptor else None
    if not chat_id or (not raw_text and not source_path):
        return None
    sender = message.get("from") or {}
    is_music = bool(source_path) or is_music_text(raw_text)
    prompt = clean_music_prompt(raw_text, has_audio=bool(source_path)) if is_music else raw_text
    return {
        "intent": "music" if is_music else "help" if is_help_text(raw_text) else "chat",
        "chat_id": chat_id,
        "message_id": message.get("message_id"),
        "prompt": prompt,
        "duration_seconds": duration_seconds,
        "variation_plan": parse_variation_plan(raw_text),
        "quality": parse_quality(raw_text),
        "source_path": source_path,
        "username": sender.get("username", ""),
    }


def send_message(token: str, chat_id: int | str, text: str, reply_to_message_id: int | None = None) -> None:
    telegram_call(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "reply_to_message_id": reply_to_message_id,
            "text": text,
        },
        timeout=30,
    )


def send_generated_audio(token: str, chat_id: int | str, output_path: str, reply_to_message_id: int | None = None) -> None:
    path = Path(output_path)
    if not path.exists():
        send_message(token, chat_id, f"Done, but I could not find the file to upload: {output_path}", reply_to_message_id)
        return

    caption = f"Done: {path.name}"
    payload = {
        "chat_id": chat_id,
        "reply_to_message_id": reply_to_message_id,
        "caption": caption[:1024],
        "title": path.stem[:64],
    }
    try:
        telegram_multipart_call(token, "sendAudio", payload, "audio", path)
    except Exception as error:
        print(f"sendAudio failed, falling back to sendDocument: {error}", flush=True)
        telegram_multipart_call(token, "sendDocument", payload, "document", path)


def musicbot_job_url(status_url: str) -> str:
    return urllib.parse.urljoin(MUSICBOT_URL, status_url)


def stage_message(job: dict[str, Any]) -> str | None:
    stage = str(job.get("stage") or "")
    payload = job.get("payload", {}) if isinstance(job.get("payload"), dict) else {}
    quality = str(job.get("quality") or payload.get("quality") or "draft")
    duration = job.get("duration_seconds") or payload.get("duration_seconds")
    variant = str(job.get("variant_label") or payload.get("variant_label") or "").strip()
    prefix = f"Draft {variant}: " if variant and quality == "draft" else ""
    labels = {
        "queued": f"Queued {quality} render ({duration}s).",
        "resolving_input": "Reading prompt and reference audio.",
        "normalizing_prompt": "Rewriting the music brief with the local LLM.",
        "generating_draft": "Generating draft audio on ACE-Step.",
        "generating_better": "Generating better-quality audio on ACE-Step.",
        "generating_audio": "Generating audio on ACE-Step.",
        "publishing_to_dropbox": "Encoding finished; copying the final file to Dropbox.",
    }
    text = labels.get(stage)
    return f"{prefix}{text}" if text else None


def poll_music_job(token: str, message: dict[str, Any], initial: dict[str, Any]) -> dict[str, Any]:
    job_url = musicbot_job_url(str(initial.get("status_url") or ""))
    duration = int(message.get("duration_seconds") or DEFAULT_DURATION_SECONDS)
    deadline = time.time() + max(900, duration * 6 + 900)
    last_stage = ""
    while time.time() < deadline:
        job = get_json(job_url, timeout=30)
        stage = str(job.get("stage") or "")
        if stage and stage != last_stage:
            text = stage_message(job)
            if text:
                send_message(token, message["chat_id"], text, message.get("message_id"))
            last_stage = stage
        if job.get("status") in {"completed", "failed"}:
            return job
        time.sleep(5)
    return {"status": "failed", "error": "Timed out waiting for music job."}


def build_music_payload(
    message: dict[str, Any],
    *,
    quality: str | None = None,
    batch_id: str = "",
    variant_label: str = "",
    seed: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prompt": message["prompt"],
        "source_path": message.get("source_path") or "",
        "duration_seconds": message.get("duration_seconds") or DEFAULT_DURATION_SECONDS,
        "quality": quality or message.get("quality") or "draft",
        "wait": False,
        "telegram": {
            "chat_id": message["chat_id"],
            "message_id": message.get("message_id"),
            "username": message.get("username", ""),
        },
    }
    if message.get("variation_plan"):
        payload["variation_plan"] = message["variation_plan"]
    if batch_id:
        payload["batch_id"] = batch_id
    if variant_label:
        payload["variant_label"] = variant_label
    if seed is not None:
        payload["seed"] = seed
    return payload


def poll_music_jobs(token: str, message: dict[str, Any], initial_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for initial in initial_jobs:
        job_id = str(initial.get("id") or "")
        if not job_id:
            continue
        states[job_id] = {
            "url": musicbot_job_url(str(initial.get("status_url") or "")),
            "last_stage": "",
            "done": False,
            "job": initial,
        }

    duration = int(message.get("duration_seconds") or DEFAULT_DURATION_SECONDS)
    deadline = time.time() + max(900, (duration * 6 + 900) * max(1, len(states)))
    while time.time() < deadline and any(not state["done"] for state in states.values()):
        for job_id, state in states.items():
            if state["done"]:
                continue
            try:
                job = get_json(str(state["url"]), timeout=30)
            except Exception as error:  # noqa: BLE001 - transient polling errors should not kill other variants.
                print(f"Could not poll job {job_id}: {error}", flush=True)
                continue
            state["job"] = job
            stage = str(job.get("stage") or "")
            if stage and stage != state["last_stage"]:
                text = stage_message(job)
                if text:
                    send_message(token, message["chat_id"], text, message.get("message_id"))
                state["last_stage"] = stage
            if job.get("status") == "completed":
                state["done"] = True
                variant = str(job.get("variant_label") or job.get("payload", {}).get("variant_label") or "").strip()
                label = f"Draft {variant}" if variant else "Draft"
                send_generated_audio(token, message["chat_id"], str(job.get("output_path") or ""), message.get("message_id"))
                send_message(
                    token,
                    message["chat_id"],
                    f"{label} ready. For a higher quality pass, reply: /better {job.get('id')}",
                    message.get("message_id"),
                )
            elif job.get("status") == "failed":
                state["done"] = True
                variant = str(job.get("variant_label") or job.get("payload", {}).get("variant_label") or "").strip()
                label = f"Draft {variant}" if variant else "Draft"
                send_message(
                    token,
                    message["chat_id"],
                    f"{label} failed: {job.get('error') or job.get('status')}",
                    message.get("message_id"),
                )
        if any(not state["done"] for state in states.values()):
            time.sleep(5)

    results = [dict(state["job"]) for state in states.values()]
    if any(not state["done"] for state in states.values()):
        send_message(token, message["chat_id"], "Timed out waiting for one or more draft variants.", message.get("message_id"))
    return results


def submit_draft_batch(message: dict[str, Any]) -> list[dict[str, Any]]:
    batch_id = uuid.uuid4().hex
    submitted: list[dict[str, Any]] = []
    for label in draft_variant_labels(draft_variant_count_for_message(message)):
        payload = build_music_payload(
            message,
            quality="draft",
            batch_id=batch_id,
            variant_label=label,
            seed=random.randint(1, 2_147_483_647),
        )
        submitted.append(submit_music_job(payload))
    return submitted


def submit_music_job(payload: dict[str, Any]) -> dict[str, Any]:
    return post_json(MUSICBOT_URL, payload, timeout=60)


def chat_with_ollama(prompt: str) -> str:
    body = {
        "model": CHAT_MODEL,
        "system": "You are Dubo, Jason's concise, useful Telegram assistant. Be friendly and practical.",
        "prompt": prompt,
        "stream": False,
        "think": False,
        "keep_alive": CHAT_KEEP_ALIVE,
    }
    try:
        result = post_json(CHAT_URL, body, timeout=CHAT_TIMEOUT_SECONDS)
    except Exception:
        return "I can help with music now. Send `/music your prompt`, or upload an audio clip with a caption and I will turn it into a loop."
    return str(result.get("response") or result.get("thinking") or "").strip()[:3500] or "I am here."


def help_text() -> str:
    return (
        "Dubo commands:\n"
        "/music <prompt> - create draft variants in Dropbox; supports long durations like 30 minutes\n"
        "/loop <prompt> - same, tuned for loops\n"
        "/better <job_id> - render the draft you liked at higher quality\n"
        "Add progression notes like faster, slower, higher, darker, breakdowns, or bigger drops\n"
        "Upload a voice/audio clip with an optional caption - I will use it as source material\n"
        "Plain text - I will answer normally"
    )


def handle_message(token: str, message: dict[str, Any]) -> None:
    print(f"Handling Telegram message {message.get('message_id')} from chat {message['chat_id']}", flush=True)
    if message["intent"] == "help":
        send_message(token, message["chat_id"], help_text(), message.get("message_id"))
        return
    if message["intent"] == "chat":
        send_message(token, message["chat_id"], chat_with_ollama(message["prompt"]), message.get("message_id"))
        return

    if message["intent"] == "rerender":
        try:
            original = get_json(musicbot_job_url(f"/jobs/{message['parent_job_id']}"), timeout=30)
            payload = dict(original.get("payload") or {})
        except Exception as error:
            send_message(token, message["chat_id"], f"I could not find that draft job: {error}", message.get("message_id"))
            return
        payload["quality"] = "better"
        payload["parent_job_id"] = message["parent_job_id"]
        if original.get("brief", {}).get("seed") and not payload.get("seed"):
            payload["seed"] = original["brief"]["seed"]
        send_message(token, message["chat_id"], f"Rendering better-quality version of `{message['parent_job_id']}`.", message.get("message_id"))
        result = submit_music_job(payload)
        result = poll_music_job(token, message, result)
        if result.get("status") == "completed":
            send_generated_audio(token, message["chat_id"], str(result.get("output_path") or ""), message.get("message_id"))
        else:
            send_message(token, message["chat_id"], f"Better render failed: {result.get('error') or result.get('status')}", message.get("message_id"))
        return

    send_message(
        token,
        message["chat_id"],
        f"Starting a {message.get('quality', 'draft')} render. I will send progress here.",
        message.get("message_id"),
    )
    print("Sent Telegram acknowledgement", flush=True)

    draft_count = draft_variant_count_for_message(message)
    if message.get("quality") == "draft" and draft_count > 1:
        initial_jobs = submit_draft_batch(message)
        labels = ", ".join(draft_variant_labels(draft_count))
        send_message(
            token,
            message["chat_id"],
            f"Queued {len(initial_jobs)} draft variants ({labels}). I will send each one as it finishes.",
            message.get("message_id"),
        )
        poll_music_jobs(token, message, initial_jobs)
        print("Finished Telegram draft batch", flush=True)
        return

    result = submit_music_job(build_music_payload(message))
    result = poll_music_job(token, message, result)
    print(f"Music bot returned status {result.get('status')}", flush=True)

    if result.get("status") == "completed":
        send_generated_audio(token, message["chat_id"], str(result.get("output_path") or ""), message.get("message_id"))
        send_message(token, message["chat_id"], f"Draft ready. For a higher quality pass, reply: /better {result.get('id')}", message.get("message_id"))
        print("Sent Telegram audio completion reply", flush=True)
        return
    else:
        text = f"Music job failed: {result.get('error') or result.get('status') or 'unknown error'}"
    send_message(token, message["chat_id"], text, message.get("message_id"))
    print("Sent Telegram completion reply", flush=True)


def poll_once(token: str) -> None:
    payload: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "edited_message"]}
    offset = read_offset()
    if offset is not None:
        payload["offset"] = offset
    response = telegram_call(token, "getUpdates", payload, timeout=35)
    updates = response.get("result") or []
    if updates:
        print(f"Received {len(updates)} Telegram update(s)", flush=True)
    for update in updates:
        update_id = int(update["update_id"])
        write_offset(update_id + 1)
        message = extract_message(update, token)
        if message:
            handle_message(token, message)


def main() -> None:
    global CHAT_KEEP_ALIVE, CHAT_MODEL, CHAT_TIMEOUT_SECONDS, CHAT_URL, MUSICBOT_URL, POLL_SECONDS, DEFAULT_DURATION_SECONDS, MAX_TELEGRAM_DURATION_SECONDS, DRAFT_VARIANTS, MAX_DRAFT_VARIANTS, LONG_RENDER_SECONDS, LONG_DRAFT_VARIANTS
    token = telegram_token()
    MUSICBOT_URL = os.environ.get("MUSICBOT_GENERATE_URL", MUSICBOT_URL)
    DEFAULT_DURATION_SECONDS = int(os.environ.get("MUSICBOT_DEFAULT_DURATION_SECONDS", str(DEFAULT_DURATION_SECONDS)))
    MAX_TELEGRAM_DURATION_SECONDS = int(os.environ.get("MUSICBOT_MAX_TELEGRAM_DURATION_SECONDS", str(MAX_TELEGRAM_DURATION_SECONDS)))
    DRAFT_VARIANTS = int(os.environ.get("MUSICBOT_DRAFT_VARIANTS", str(DRAFT_VARIANTS)))
    MAX_DRAFT_VARIANTS = int(os.environ.get("MUSICBOT_MAX_DRAFT_VARIANTS", str(MAX_DRAFT_VARIANTS)))
    LONG_RENDER_SECONDS = int(os.environ.get("MUSICBOT_LONG_RENDER_SECONDS", str(LONG_RENDER_SECONDS)))
    LONG_DRAFT_VARIANTS = int(os.environ.get("MUSICBOT_LONG_DRAFT_VARIANTS", str(LONG_DRAFT_VARIANTS)))
    POLL_SECONDS = int(os.environ.get("TELEGRAM_POLL_SECONDS", str(POLL_SECONDS)))
    CHAT_URL = os.environ.get("OLLAMA_URL", CHAT_URL)
    CHAT_MODEL = os.environ.get("OLLAMA_MODEL", CHAT_MODEL)
    CHAT_TIMEOUT_SECONDS = int(os.environ.get("DUBO_CHAT_TIMEOUT_SECONDS", str(CHAT_TIMEOUT_SECONDS)))
    CHAT_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", CHAT_KEEP_ALIVE)
    print("Telegram poller running. Press Ctrl+C to stop.")
    while True:
        try:
            poll_once(token)
        except urllib.error.HTTPError as error:
            print(f"Telegram poller HTTP error: {error.code} {error.read().decode('utf-8', errors='replace')[:300]}")
            time.sleep(POLL_SECONDS)
        except Exception as error:  # noqa: BLE001 - keep poller alive.
            print(f"Telegram poller error: {error}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
