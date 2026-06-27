# Agent instructions — filedropbot

This file documents the one convention an AI coding agent (Claude Code, Codex,
Cursor, Aider, Gemini CLI, …) needs to know to use filedropbot. The mechanism is
just the filesystem, so it works for any agent that can run shell commands — no
API, SDK, or plugin required.

## Receiving files from my phone

Files I send to the bot land under `/root/filedropbot/data/drops/<date>/`. The
bot replies to me with the host path; I paste it to you. Nothing for you to do.

## Sending a file to my phone

To deliver a file to me on Telegram — a screenshot, a generated chart, a log, a
build artifact — copy it into the outbox:

```bash
cp /path/to/file.png /root/filedropbot/data/outbox/
```

Within a few seconds the bot DMs it to my phone and moves it to
`data/sent/<date>/`. Do this whenever I ask you to "send me", "DM me", or "show
me on my phone" a file.

Notes:
- **Limit:** 50 MB per file (Telegram bot upload cap). Larger files are skipped.
- **Atomic writes:** if you generate the file slowly, write it under a `*.tmp`
  name and `mv` it into the outbox when finished — the bot ignores `.tmp`,
  `.part`, and dotfiles until they're renamed, and otherwise waits ~2s of
  inactivity before sending so it never ships a half-written file.
- Filenames are preserved exactly and sent as documents (no recompression).
- **Message caption:** by default the caption equals the filename. To keep a
  clean filename *and* add context, drop a sidecar `<filename>.caption.txt`
  next to the file — its text (≤1024 chars) becomes the caption, and the
  sidecar is removed after sending. Example:

  ```bash
  cp widget.zip /root/filedropbot/data/outbox/
  printf 'ProverkaCheka widget v3.0.0' > /root/filedropbot/data/outbox/widget.zip.caption.txt
  ```

## Wiring this into your agent's global config

So an agent knows this in *every* project (not just this repo), add the
"Sending a file to my phone" instruction to its global standing-instructions
file:

- Claude Code: `~/.claude/CLAUDE.md`
- Codex: `~/.codex/AGENTS.md` (or repo-root `AGENTS.md`)
- Cursor: `.cursor/rules/filedrop.mdc`
- Gemini CLI: `~/.gemini/GEMINI.md`
