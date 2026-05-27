from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".runtime"
OFFSET_PATH = STATE_DIR / "telegram_offset.txt"
INBOX_DIR = STATE_DIR / "telegram-inbox"
MUSICBOT_URL = os.environ.get("MUSICBOT_GENERATE_URL", "http://127.0.0.1:8710/generate")
POLL_SECONDS = int(os.environ.get("TELEGRAM_POLL_SECONDS", "3"))
CHAT_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
CHAT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")
CHAT_TIMEOUT_SECONDS = int(os.environ.get("DUBO_CHAT_TIMEOUT_SECONDS", "45"))


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


def telegram_call(token: str, method: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    return post_json(f"https://api.telegram.org/bot{token}/{method}", payload, timeout=timeout)


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
        return "Create an original loop from this uploaded audio clip. Preserve its rhythmic or melodic idea and build a fuller musical sketch around it."
    return "Create an original instrumental music cue."


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


def download_telegram_file(token: str, file_descriptor: dict[str, Any]) -> str | None:
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
    return normalize_audio(destination)


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


def normalize_audio(path: Path) -> str:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return str(path)
    normalized = path.with_suffix(".normalized.wav")
    completed = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "44100",
            "-t",
            "30",
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
    file_descriptor = message_file_descriptor(message)
    source_path = download_telegram_file(token, file_descriptor) if file_descriptor else None
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


def chat_with_ollama(prompt: str) -> str:
    body = {
        "model": CHAT_MODEL,
        "system": "You are Dubo, Jason's concise, useful Telegram assistant. Be friendly and practical.",
        "prompt": prompt,
        "stream": False,
    }
    try:
        result = post_json(CHAT_URL, body, timeout=CHAT_TIMEOUT_SECONDS)
    except Exception:
        return "I can help with music now. Send `/music your prompt`, or upload an audio clip with a caption and I will turn it into a loop."
    return str(result.get("response") or "").strip()[:3500] or "I am here."


def help_text() -> str:
    return (
        "Dubo commands:\n"
        "/music <prompt> - create a music file in Dropbox\n"
        "/loop <prompt> - same, tuned for loops\n"
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

    send_message(
        token,
        message["chat_id"],
        "Working on the music. I will drop the file into Dropbox and reply with the path.",
        message.get("message_id"),
    )
    print("Sent Telegram acknowledgement", flush=True)

    result = post_json(
        MUSICBOT_URL,
        {
            "prompt": message["prompt"],
            "source_path": message.get("source_path") or "",
            "duration_seconds": 90,
            "wait": True,
            "wait_timeout_seconds": 600,
            "telegram": {
                "chat_id": message["chat_id"],
                "message_id": message.get("message_id"),
                "username": message.get("username", ""),
            },
        },
        timeout=700,
    )
    print(f"Music bot returned status {result.get('status')}", flush=True)

    if result.get("status") == "completed":
        text = f"Done: {result.get('output_path')}"
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
    global CHAT_MODEL, CHAT_TIMEOUT_SECONDS, CHAT_URL, MUSICBOT_URL, POLL_SECONDS
    token = telegram_token()
    MUSICBOT_URL = os.environ.get("MUSICBOT_GENERATE_URL", MUSICBOT_URL)
    POLL_SECONDS = int(os.environ.get("TELEGRAM_POLL_SECONDS", str(POLL_SECONDS)))
    CHAT_URL = os.environ.get("OLLAMA_URL", CHAT_URL)
    CHAT_MODEL = os.environ.get("OLLAMA_MODEL", CHAT_MODEL)
    CHAT_TIMEOUT_SECONDS = int(os.environ.get("DUBO_CHAT_TIMEOUT_SECONDS", str(CHAT_TIMEOUT_SECONDS)))
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
