# filedropbot

A tiny self-hosted Telegram bot that moves files **both ways** between your
phone and a server — built for feeding files to (and getting files back from) an
AI coding agent like Claude Code or Codex running on a VM.

The hard part of working with an agent on a remote box is getting a file across:
a screenshot on your phone that the agent needs to see, or a chart the agent
generated that you want on your phone. filedropbot makes both a five-second step,
with **no open ports, no domain, and no SSH from your phone**.

**Phone → VM.** Send the bot photos, documents, albums, audio, voice notes or
videos. It saves each under `data/drops/<date>/` and replies with the **host
absolute path** — ready to paste into the agent running on the VM.

**VM → phone.** Anything dropped into `data/outbox/` is DM'd straight back to you
on Telegram, then archived under `data/sent/<date>/`. So the agent can hand a
file back to your phone with a single `cp`.

- **Single user only** — locked to your `ALLOWED_USER_ID`; everyone else is ignored.
- **Polling mode** — the bot dials out to Telegram; no inbound ports, no reverse proxy, no domain.
- **Auto-delete** — files older than `RETENTION_DAYS` (default 7) are purged (both received drops and the sent archive).
- **Albums** — media groups are batched and answered with all paths at once.
- **Limits** — Telegram caps bot downloads at 20 MB and uploads at 50 MB; files over the limit are skipped with a warning.

## How it works

The filesystem *is* the interface — there's no API to learn. Inbound files land
in `data/drops/`; to send one out, you (or an agent) drop it in `data/outbox/`.
The bot reports **host** paths, not container paths, so they're directly usable
from a shell or agent running on the VM. `HOST_DATA_DIR` tells the bot where its
data volume lives on the host to make that translation correct.

## Quick start

### 1. Create the bot and find your IDs

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the **bot token**.
2. Message [@userinfobot](https://t.me/userinfobot) → it replies with your numeric **user ID**.

### 2. Configure

```bash
git clone https://github.com/<you>/filedropbot.git
cd filedropbot
cp .env.example .env        # fill in BOT_TOKEN and ALLOWED_USER_ID
```

### 3. Run it — pick one route

#### Route A — Docker Compose (recommended)

```bash
mkdir -p data && sudo chown -R 10001:10001 data   # container runs as uid 10001
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` sets `HOST_DATA_DIR` to `/root/filedropbot/data` — change it
to wherever you cloned the repo so reported paths match your host. The container
is hardened (read-only rootfs, non-root user, all capabilities dropped, memory
and PID limits).

> Deploying a prebuilt image? `docker-compose.prod.yml` points at the maintainer's
> GHCR image — fork it to your own registry or switch to `build: .`.

#### Route B — Python + systemd (no Docker)

```bash
cd /root/filedropbot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Create `/etc/systemd/system/filedropbot.service`:

```ini
[Unit]
Description=filedropbot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/root/filedropbot
EnvironmentFile=/root/filedropbot/.env
Environment=DATA_DIR=/root/filedropbot/data
Environment=HOST_DATA_DIR=/root/filedropbot/data
ExecStart=/root/filedropbot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now filedropbot
journalctl -u filedropbot -f
```

(Running bare, `DATA_DIR` and `HOST_DATA_DIR` are the same real directory, so
paths line up automatically.)

## Sending a file back to your phone

From anywhere on the VM, drop a file into the outbox:

```bash
cp chart.png /root/filedropbot/data/outbox/      # or mv
```

Within a few seconds the bot DMs it to you and moves it to `data/sent/`. It waits
until a file has been untouched for `OUTBOX_SETTLE_SEC` (default 2s) before
sending, so a file still being written won't go out half-finished. If a tool
writes slowly, write to a `*.tmp` name and rename it into place when done —
partial suffixes (`.tmp`, `.part`, …) and dotfiles are ignored until renamed.
Files are sent as **documents** (no recompression), so screenshots and charts
arrive at full quality.

## Wiring your coding agent

So an agent sends files back without being told the path every time, add the
outbox instruction to its **global** standing-instructions file (it works in
every project, not just this repo). See [`AGENTS.md`](AGENTS.md) for the snippet.

| Agent | File to add it to |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` |
| Codex | `~/.codex/AGENTS.md` (or repo `AGENTS.md`) |
| Cursor | `.cursor/rules/filedrop.mdc` |
| Gemini CLI | `~/.gemini/GEMINI.md` |

The instruction is identical for every agent — it's just `cp <file>
/root/filedropbot/data/outbox/`. Any agent with a shell tool can use it.

## Commands
- `/start` — usage help
- `/list` — show the 20 most recent drops with their host paths

## Config (`.env`)
| Var | Default | Meaning |
|-----|---------|---------|
| `BOT_TOKEN` | — | BotFather token (required) |
| `ALLOWED_USER_ID` | — | only this Telegram user ID may use the bot (required) |
| `RETENTION_DAYS` | `7` | days before auto-delete (drops and sent archive) |
| `OUTBOX_SETTLE_SEC` | `2` | seconds a file must be idle before it's sent back |

## Security notes

- **No inbound ports.** The bot long-polls Telegram outbound, so nothing on your
  VM is reachable from the internet.
- **Single-user lock.** Every handler is gated on `ALLOWED_USER_ID`; strangers
  who find your bot are ignored. Don't remove this filter.
- **The token is the secret.** Anyone holding `BOT_TOKEN` controls the bot. Keep
  `.env` out of git (it's gitignored) and `chmod 600`; rotate via BotFather if it
  ever leaks. `ALLOWED_USER_ID` is *not* secret — it's just a number.
- **The outbox only sends to you.** Its destination is hardcoded to
  `ALLOWED_USER_ID`, so it can't be used to exfiltrate files to anyone else.

## License

[MIT](LICENSE).
