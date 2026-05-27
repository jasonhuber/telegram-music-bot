# n8n Workflow Wiring

This assumes your existing Telegram hook already receives messages.

## Text Or Link Messages

1. Add an `HTTP Request` node after your Telegram trigger/filter.
2. Method: `POST`
3. URL:

```text
http://127.0.0.1:8710/generate
```

Use this instead if n8n runs in Docker:

```text
http://host.docker.internal:8710/generate
```

4. Body Content Type: JSON
5. Body:

```json
{
  "prompt": "={{ $json.message.text || $json.message.caption || '' }}",
  "source_url": "={{ ($json.message.text || $json.message.caption || '').match(/https?:\\/\\/\\S+/)?.[0] || '' }}",
  "duration_seconds": 90,
  "telegram": {
    "chat_id": "={{ $json.message.chat.id }}",
    "message_id": "={{ $json.message.message_id }}",
    "username": "={{ $json.message.from.username || '' }}"
  }
}
```

The response includes `id` and `status_url`. Poll `GET http://127.0.0.1:8710{{ $json.status_url }}` until `status` is `completed` or `failed`.

## Uploaded Telegram Files

1. Use Telegram `File -> Get`.
2. Turn `Download` on.
3. Write the binary file to a stable inbox folder with `Read/Write Files from Disk`.

Recommended inbox:

```text
C:\Users\Dubo\Dropbox\AI Music\Inbox
```

4. POST this JSON to the bot:

```json
{
  "prompt": "={{ $json.message.caption || $json.message.text || 'Use the attached audio as style reference and create an original track.' }}",
  "source_path": "={{ $json.savedFilePath }}",
  "duration_seconds": 90,
  "telegram": {
    "chat_id": "={{ $json.message.chat.id }}",
    "message_id": "={{ $json.message.message_id }}",
    "username": "={{ $json.message.from.username || '' }}"
  }
}
```

Use the actual output field from your disk-write node for `source_path`; the name varies by node version and configuration.

## Send The Result Back

When `GET /jobs/{id}` returns:

```json
{
  "status": "completed",
  "output_path": "C:\\Users\\Dubo\\Dropbox\\AI Music\\20260527-track.wav"
}
```

You can either:

- Send a Telegram text message with `output_path`.
- Use a disk-read node to load `output_path`, then Telegram `Send Audio`.

## One-Request Mode

For simple testing, include:

```json
{
  "wait": true,
  "wait_timeout_seconds": 600
}
```

Then the HTTP Request node will return only when the job is done or fails. Polling is better once real generation takes several minutes.
