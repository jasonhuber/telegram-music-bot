# TelegramMusicBot QA

TelegramMusicBot provides the Telegram reporting path for the workspace QA
system, but the QA reporter does not require the music bot server or poller to
be running.

Canonical workspace QA lives at `../.sustav-sync/qa/`.

Key files:

- `../.sustav-sync/qa/send-telegram-report.ps1`
- `.env` for `MUSICBOT_TELEGRAM_BOT_TOKEN` or `TELEGRAM_BOT_TOKEN`
- `.runtime/*telegram*log` for fallback chat-id discovery

For handoff details, read `../.sustav-sync/qa/HANDOFF.md`.

Telegram reporting sends direct `sendMessage` calls with a redacted QA summary
and a local report path. Do not print or commit bot tokens.
