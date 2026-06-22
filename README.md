# filedropbot

A tiny personal Telegram bot for dropping files onto the server. Send it photos,
documents, albums, audio, voice notes or videos and it saves them under
`/root/filedropbot/data/drops/<date>/` and replies with the **host absolute
path** — ready to paste into Claude Code running on the VM.

- **Single user only** — locked to `ALLOWED_USER_ID` (default `773023`). Everyone
  else is ignored.
- **Polling mode** — no inbound ports, no Caddy, no domain.
- **Auto-delete** — files older than `RETENTION_DAYS` (default 7) are purged by a
  job that runs every 6 hours.
- **Albums** — media groups are batched and answered with all paths at once.
- **Limit** — Telegram's bot download API caps files at 20 MB; larger files are
  skipped with a warning.

## Commands
- `/start` — usage help
- `/list` — show the 20 most recent drops with their host paths

## Setup
```bash
cd /root/filedropbot
cp .env.example .env        # then fill in BOT_TOKEN
mkdir -p data && chown -R 10001:10001 data
docker compose up -d --build
docker compose logs -f
```

## Config (`.env`)
| Var | Default | Meaning |
|-----|---------|---------|
| `BOT_TOKEN` | — | BotFather token |
| `ALLOWED_USER_ID` | `773023` | only this user may use the bot |
| `RETENTION_DAYS` | `7` | days before auto-delete |

`HOST_DATA_DIR` (set in compose) tells the bot where the data volume lives on the
host so the paths it reports are usable from the host shell.
