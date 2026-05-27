# Test From Telegram

Yes, the intended test is from Telegram. The local service must be running, and your n8n Telegram workflow must call it.

## 1. Start The Music Bot Service

In PowerShell:

```powershell
cd "C:\Users\Dubo\Dropbox\Personal\Sustav Dev\TelegramMusicBot"
.\scripts\start.ps1
```

Leave that window open.

## 2. Add This To Your Existing n8n Telegram Flow

After your Telegram trigger, add an `HTTP Request` node.

Method:

```text
POST
```

URL:

```text
http://127.0.0.1:8710/generate
```

If n8n is running in Docker, use:

```text
http://host.docker.internal:8710/generate
```

Body Content Type:

```text
JSON
```

Body:

```json
{
  "prompt": "={{ $json.message.text || $json.message.caption || '' }}",
  "duration_seconds": 90,
  "wait": true,
  "wait_timeout_seconds": 600,
  "telegram": {
    "chat_id": "={{ $json.message.chat.id }}",
    "message_id": "={{ $json.message.message_id }}",
    "username": "={{ $json.message.from.username || '' }}"
  }
}
```

Then add a Telegram `Send Message` node.

Chat ID:

```text
={{ $('Telegram Trigger').item.json.message.chat.id }}
```

Text:

```text
=Done: {{ $json.output_path }}
```

## 3. Send A Telegram Message

Send your bot a message like:

```text
90 second instrumental synthwave cue, neon night drive, pulsing bass, no vocals
```

Expected result:

- n8n receives the Telegram message.
- n8n posts the prompt to the local service.
- The service creates an audio file in `C:\Users\Dubo\Dropbox\AI Music`.
- Telegram replies with the local output path.

## 4. Later: Send Audio Back To Telegram

The first test replies with the file path because it is the least fragile. Once that works:

1. Add a disk-read node after the HTTP Request node.
2. Read the file at `{{$json.output_path}}`.
3. Add Telegram `Send Audio`.
4. Send the binary file from the disk-read node.

## Common Failure Checks

If Telegram does not reply, check:

- The service PowerShell window is still open.
- `http://127.0.0.1:8710/health` returns `ok: true`.
- n8n can reach the host URL. Docker n8n usually needs `host.docker.internal`.
- The n8n Telegram workflow is active.

If the reply says the job failed, open the job URL:

```text
http://127.0.0.1:8710/jobs/JOB_ID
```

The response includes the error message.
