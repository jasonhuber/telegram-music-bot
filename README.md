# Telegram Music Bot

Local bridge between your existing Telegram/n8n flow, Ollama, a music generator, and Dropbox.

## What It Does

- Accepts JSON from n8n at `POST /generate`.
- Resolves text prompts, links, or local reference-audio paths.
- Asks local Ollama to turn the Telegram message into a structured music brief.
- Runs a configurable local music generator command.
- Writes the finished audio file into your Dropbox folder.
- Exposes job status at `GET /jobs/{job_id}`.

If `MUSIC_GENERATOR_COMMAND` is empty, the service writes a short WAV tone sketch. That fallback is only for testing the Telegram -> n8n -> service -> Dropbox loop before ACE-Step is wired in.

## Start It

```powershell
cd "C:\Users\Dubo\Dropbox\Personal\Sustav Dev\TelegramMusicBot"
.\scripts\start.ps1
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8710/health
```

## Ollama

The service talks to Ollama over HTTP, so `ollama.exe` does not need to be on the Windows PATH. By default it calls:

```text
http://localhost:11434/api/generate
```

If `OLLAMA_MODEL` is not installed, the bot asks `http://localhost:11434/api/tags` for local models and retries with the first model Ollama reports. To pin a specific model, edit `.env`:

```text
OLLAMA_MODEL=llama3.2:latest
```

If Ollama is running somewhere else, set:

```text
OLLAMA_URL=http://127.0.0.1:11434/api/generate
```

Test a synchronous job:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8710/generate `
  -ContentType "application/json" `
  -Body '{"prompt":"90 second instrumental synthwave cue, neon night drive, pulsing bass, no vocals","duration_seconds":90,"wait":true}'
```

The test output should appear in:

```text
C:\Users\Dubo\Dropbox\AI Music
```

## n8n Integration

For the shortest Telegram-first test, see [TELEGRAM_TEST.md](TELEGRAM_TEST.md).

Recommended flow:

```text
Telegram Trigger
  -> Switch on message type
  -> Telegram Get File, when audio/document/voice exists
  -> Read/Write Files from Disk, write binary to a stable local inbox folder
  -> HTTP Request, POST JSON to http://127.0.0.1:8710/generate
  -> Wait / polling loop
  -> Telegram Send Message or Send Audio
```

Send this JSON to `/generate`:

```json
{
  "prompt": "{{$json.message.text || $json.message.caption || ''}}",
  "source_url": "",
  "source_path": "C:\\Users\\Dubo\\Dropbox\\AI Music\\Inbox\\telegram-reference.wav",
  "duration_seconds": 90,
  "telegram": {
    "chat_id": "{{$json.message.chat.id}}",
    "message_id": "{{$json.message.message_id}}",
    "username": "{{$json.message.from.username}}"
  }
}
```

If n8n runs in Docker, `127.0.0.1` points at the container. Use `http://host.docker.internal:8710/generate`, and make sure the path you send as `source_path` exists on the host where this service runs.

URLs inside `prompt` are treated as prompt context. To download a reference file, pass the URL explicitly as `source_url`; this avoids accidentally trying to download general pages such as YouTube or streaming-service links during a quick Telegram test.

## Optional Direct Telegram Poller

If n8n inbound webhooks are not active yet, you can let this service poll the Telegram bot directly:

```powershell
$env:MUSICBOT_TELEGRAM_BOT_TOKEN = "your-bot-token"
python .\telegram_poller.py
```

The poller sends each Telegram text prompt to the local `/generate` endpoint and replies with the output path.

## Plug In ACE-Step

Install ACE-Step in its own environment, then set `MUSIC_GENERATOR_COMMAND` in `.env`. The bot passes data through environment variables:

```text
MUSICBOT_PROMPT_JSON
MUSICBOT_PROMPT_TEXT
MUSICBOT_OUTPUT_PATH
MUSICBOT_REFERENCE_PATH
MUSICBOT_DURATION_SECONDS
MUSICBOT_TITLE
```

The most robust pattern is to create a tiny wrapper script for your chosen generator:

```powershell
MUSIC_GENERATOR_COMMAND=powershell -ExecutionPolicy Bypass -File C:\path\to\render-with-ace-step.ps1
```

Your wrapper should read `MUSICBOT_PROMPT_TEXT` or `MUSICBOT_PROMPT_JSON` and write the final `.wav` or `.mp3` to `MUSICBOT_OUTPUT_PATH`.

## API

`POST /generate`

```json
{
  "prompt": "lofi piano loop for studying",
  "source_url": "https://example.com/reference.mp3",
  "source_path": "C:\\path\\to\\reference.wav",
  "duration_seconds": 90,
  "wait": false
}
```

Response:

```json
{
  "id": "job-id",
  "status": "queued",
  "status_url": "/jobs/job-id"
}
```

`GET /jobs/{job_id}`

```json
{
  "status": "completed",
  "output_path": "C:\\Users\\Dubo\\Dropbox\\AI Music\\20260527-track.wav",
  "generator_mode": "command"
}
```

## Notes

- Ollama is optional at runtime. If it is down or no installed model can be reached, the service falls back to a simple prompt parser and keeps the job moving.
- Avoid using artist names as the actual generator instruction. The Ollama prompt asks for musical traits instead.
- Keep Telegram reference files in a local inbox folder that both n8n and this service can see.
