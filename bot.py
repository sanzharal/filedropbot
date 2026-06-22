"""filedropbot — personal file-drop bot.

Locked to a single Telegram user. Forward/send photos, documents, albums,
audio, voice or video; the bot saves each to the server and replies with the
HOST absolute path so you can paste it into Claude Code running on the VM.
Drops older than RETENTION_DAYS are deleted automatically.
"""
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("filedropbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
# Path inside the container where files are written (mapped to a host volume).
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
# Path of that same volume as seen on the HOST — what we show the user so the
# path is usable from Claude Code running directly on the VM.
HOST_DATA_DIR = os.environ.get("HOST_DATA_DIR", str(DATA_DIR)).rstrip("/")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
# Media-group (album) debounce: wait this long for all items to arrive.
ALBUM_DEBOUNCE_SEC = float(os.environ.get("ALBUM_DEBOUNCE_SEC", "2.5"))
DROPS_DIR = DATA_DIR / "drops"

# Telegram Bot API getFile limit is 20 MB unless using a local API server.
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name: str) -> str:
    name = _SAFE.sub("_", name).strip("._") or "file"
    return name[:120]


def _host_path(path: Path) -> str:
    """Translate a container path under DATA_DIR to its host equivalent."""
    rel = path.relative_to(DATA_DIR)
    return f"{HOST_DATA_DIR}/{rel.as_posix()}"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H%M%S%f")[:-3]


async def _extract(message) -> tuple | None:
    """Return (file_id, suggested_name, size) for the media in a message."""
    if message.photo:
        photo = message.photo[-1]  # largest size
        return photo.file_id, f"photo_{_stamp()}.jpg", photo.file_size
    if message.document:
        d = message.document
        return d.file_id, d.file_name or f"document_{_stamp()}", d.file_size
    if message.video:
        v = message.video
        return v.file_id, v.file_name or f"video_{_stamp()}.mp4", v.file_size
    if message.audio:
        a = message.audio
        name = a.file_name or f"{a.title or 'audio'}_{_stamp()}.mp3"
        return a.file_id, name, a.file_size
    if message.voice:
        vo = message.voice
        return vo.file_id, f"voice_{_stamp()}.ogg", vo.file_size
    if message.video_note:
        vn = message.video_note
        return vn.file_id, f"videonote_{_stamp()}.mp4", vn.file_size
    if message.animation:
        an = message.animation
        return an.file_id, an.file_name or f"animation_{_stamp()}.mp4", an.file_size
    if message.sticker:
        s = message.sticker
        ext = "webm" if s.is_video else ("tgs" if s.is_animated else "webp")
        return s.file_id, f"sticker_{_stamp()}.{ext}", s.file_size
    return None


async def _save(context: ContextTypes.DEFAULT_TYPE, file_id: str, name: str, size) -> str | None:
    if size and size > TELEGRAM_DOWNLOAD_LIMIT:
        return None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest_dir = DROPS_DIR / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{_stamp()}_{_safe_name(name)}"
    tg_file = await context.bot.get_file(file_id)
    await tg_file.download_to_drive(custom_path=str(dest))
    return _host_path(dest)


def _format_reply(paths: list[str], oversized: int) -> str:
    lines = []
    if paths:
        lines.append(f"✅ Saved {len(paths)} file{'s' if len(paths) > 1 else ''}:")
        for p in paths:
            lines.append(f"`{p}`")
    if oversized:
        lines.append(
            f"\n⚠️ {oversized} file(s) skipped — over Telegram's 20 MB bot download limit."
        )
    return "\n".join(lines)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    info = await _extract(message)
    if not info:
        return

    # Album: collect items sharing a media_group_id, reply once after a debounce.
    if message.media_group_id:
        groups = context.application.bot_data.setdefault("albums", {})
        bucket = groups.setdefault(message.media_group_id, [])
        bucket.append(info)
        # Schedule a single flush; replace any pending one for this group.
        jobs = context.job_queue.get_jobs_by_name(f"album_{message.media_group_id}")
        for j in jobs:
            j.schedule_removal()
        context.job_queue.run_once(
            _flush_album,
            ALBUM_DEBOUNCE_SEC,
            name=f"album_{message.media_group_id}",
            data={"group_id": message.media_group_id, "chat_id": message.chat_id,
                  "reply_to": message.message_id},
        )
        return

    paths, oversized = [], 0
    saved = await _save(context, *info)
    if saved:
        paths.append(saved)
    else:
        oversized += 1
    await message.reply_text(
        _format_reply(paths, oversized), parse_mode="Markdown",
        reply_to_message_id=message.message_id,
    )


async def _flush_album(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    group_id = data["group_id"]
    items = context.application.bot_data.get("albums", {}).pop(group_id, [])
    paths, oversized = [], 0
    for info in items:
        saved = await _save(context, *info)
        if saved:
            paths.append(saved)
        else:
            oversized += 1
    await context.bot.send_message(
        chat_id=data["chat_id"],
        text=_format_reply(paths, oversized),
        parse_mode="Markdown",
        reply_to_message_id=data["reply_to"],
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me photos, files, albums, audio or voice notes and I'll save them "
        f"on the server and reply with the host path.\nFiles auto-delete after "
        f"{RETENTION_DAYS} days. Use /list to see recent drops."
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    files = sorted(DROPS_DIR.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    files = [f for f in files if f.is_file()][:20]
    if not files:
        await update.message.reply_text("No drops yet.")
        return
    lines = ["Recent drops:"] + [f"`{_host_path(f)}`" for f in files]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not DROPS_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc).timestamp() - RETENTION_DAYS * 86400
    removed = 0
    for path in DROPS_DIR.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError as e:
                log.warning("cleanup failed for %s: %s", path, e)
    # Drop empty day-directories.
    for d in sorted(DROPS_DIR.rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            try:
                d.rmdir()
            except OSError:
                pass
    if removed:
        log.info("cleanup removed %d expired file(s)", removed)


def main() -> None:
    DROPS_DIR.mkdir(parents=True, exist_ok=True)
    app = Application.builder().token(BOT_TOKEN).build()

    only_me = filters.User(user_id=ALLOWED_USER_ID)
    media = (
        filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO
        | filters.VOICE | filters.VIDEO_NOTE | filters.ANIMATION | filters.Sticker.ALL
    )
    app.add_handler(CommandHandler("start", cmd_start, filters=only_me))
    app.add_handler(CommandHandler("list", cmd_list, filters=only_me))
    app.add_handler(MessageHandler(only_me & media, handle_media))

    app.job_queue.run_repeating(cleanup, interval=6 * 3600, first=10)

    log.info("filedropbot starting (user=%s, retention=%dd)", ALLOWED_USER_ID, RETENTION_DAYS)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
