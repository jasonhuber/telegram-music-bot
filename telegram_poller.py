from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".runtime"
OFFSET_PATH = STATE_DIR / "telegram_offset.txt"
MUSICBOT_URL = os.environ.get("MUSICBOT_GENERATE_URL", "http://127.0.0.1:8710/generate")
POLL_SECONDS = int(os.environ.get("TELEGRAM_POLL_SECONDS", "3"))


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


def read_offset() -> int | None:
    try:
        value = OFFSET_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return int(value) if value else None


def write_offset(offset: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(str(offset), encoding="utf-8")


def extract_message(update: dict[str, Any]) -> dict[str, Any] | None:
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    prompt = str(message.get("text") or message.get("caption") or "").strip()
    if not chat_id or not prompt:
        return None
    sender = message.get("from") or {}
    return {
        "chat_id": chat_id,
        "message_id": message.get("message_id"),
        "prompt": prompt,
        "username": sender.get("username", ""),
    }


def handle_message(token: str, message: dict[str, Any]) -> None:
    print(f"Handling Telegram message {message.get('message_id')} from chat {message['chat_id']}", flush=True)
    telegram_call(
        token,
        "sendMessage",
        {
            "chat_id": message["chat_id"],
            "reply_to_message_id": message.get("message_id"),
            "text": "Working on it. I will drop the file into Dropbox and reply with the path.",
        },
        timeout=30,
    )
    print("Sent Telegram acknowledgement", flush=True)

    result = post_json(
        MUSICBOT_URL,
        {
            "prompt": message["prompt"],
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
    telegram_call(
        token,
        "sendMessage",
        {
            "chat_id": message["chat_id"],
            "reply_to_message_id": message.get("message_id"),
            "text": text,
        },
        timeout=30,
    )
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
        message = extract_message(update)
        if message:
            handle_message(token, message)


def main() -> None:
    token = telegram_token()
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
