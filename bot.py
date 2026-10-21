"""filedropbot — personal file-drop bot.

Locked to a single Telegram user. Two directions:

* Device -> VM: forward/send photos, documents, albums, audio, voice or video;
  the bot saves each to the server and replies with the HOST absolute path so
  you can paste it into Claude Code running on the VM.
* VM -> device: drop any file into the outbox directory (data/outbox/) and the
  bot DMs it straight to you — so an agent on the VM can hand a file back.

Drops older than RETENTION_DAYS are deleted automatically.
"""
import asyncio
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)
from telegram.request import HTTPXRequest

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
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
# How often to scan the outbox, and how long a file must sit untouched before we
# send it (so we never grab one an agent is still writing).
OUTBOX_POLL_SEC = float(os.environ.get("OUTBOX_POLL_SEC", "3"))
OUTBOX_SETTLE_SEC = float(os.environ.get("OUTBOX_SETTLE_SEC", "2"))
DROPS_DIR = DATA_DIR / "drops"
OUTBOX_DIR = DATA_DIR / "outbox"   # agent drops files here -> bot DMs them to you
SENT_DIR = DATA_DIR / "sent"       # archive of already-delivered outbox files

# Telegram Bot API getFile (download) limit is 20 MB unless using a local API
# server. Sending out is allowed up to 50 MB.
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024
TELEGRAM_CAPTION_LIMIT = 1024
# Suffixes that signal a file is still being written — skip until renamed.
_PARTIAL_SUFFIXES = (".tmp", ".part", ".partial", ".writing", ".crdownload")
# Optional sidecar: drop "<filename>.caption.txt" next to a file to set the
# Telegram message caption while keeping the document's filename untouched.
# Without a sidecar the caption defaults to the filename (legacy behaviour).
_CAPTION_SUFFIX = ".caption.txt"

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
    # Telegram CDN is occasionally slow; default 5s read_timeout used to fail
    # mid-download and leave the user with no reply. Retry a few times.
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=str(dest))
            return _host_path(dest)
        except (TimedOut, NetworkError) as e:
            last_err = e
            log.warning("download attempt %d/3 failed for %s: %s", attempt, name, e)
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            if attempt < 3:
                await asyncio.sleep(1.5 * attempt)
    raise last_err  # type: ignore[misc]


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

    try:
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
    except (TimedOut, NetworkError) as e:
        log.exception("media save failed after retries: %s", e)
        await message.reply_text(
            "⚠️ Telegram timed out while downloading your file. Please send it again.",
            reply_to_message_id=message.message_id,
        )


async def _flush_album(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    group_id = data["group_id"]
    items = context.application.bot_data.get("albums", {}).pop(group_id, [])
    paths, oversized = [], 0
    failed = 0
    for info in items:
        try:
            saved = await _save(context, *info)
            if saved:
                paths.append(saved)
            else:
                oversized += 1
        except (TimedOut, NetworkError) as e:
            failed += 1
            log.warning("album item failed: %s", e)
    text = _format_reply(paths, oversized)
    if failed:
        note = f"\n⚠️ {failed} file(s) failed (Telegram timeout). Please resend them."
        text = (text + note) if text else note.strip()
    if text:
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text=text,
            parse_mode="Markdown",
            reply_to_message_id=data["reply_to"],
        )


def _outbox_ready(path: Path, now: float) -> bool:
    """A file is sendable once it exists, is regular, not a partial/hidden file,
    and hasn't been modified for at least OUTBOX_SETTLE_SEC seconds."""
    if not path.is_file() or path.name.startswith("."):
        return False
    if path.suffix.lower() in _PARTIAL_SUFFIXES:
        return False
    # Caption sidecars are metadata for another file, never sent on their own.
    if path.name.endswith(_CAPTION_SUFFIX):
        return False
    try:
        return (now - path.stat().st_mtime) >= OUTBOX_SETTLE_SEC
    except OSError:
        return False


async def _drain_outbox(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send any settled files in the outbox to the user, then archive them."""
    if not OUTBOX_DIR.exists():
        return
    inflight = context.application.bot_data.setdefault("outbox_inflight", set())
    now = datetime.now(timezone.utc).timestamp()
    for path in sorted(OUTBOX_DIR.rglob("*")):
        if path in inflight or not _outbox_ready(path, now):
            continue
        inflight.add(path)
        try:
            size = path.stat().st_size
            if size > TELEGRAM_UPLOAD_LIMIT:
                await context.bot.send_message(
                    chat_id=ALLOWED_USER_ID,
                    text=f"⚠️ `{path.name}` is {size // (1024*1024)} MB — over "
                         f"Telegram's 50 MB send limit. Skipped.",
                    parse_mode="Markdown",
                )
                _archive_outbox(path)
                continue
            # Send as a document so bytes and filename are preserved exactly
            # (no recompression — important for screenshots and charts).
            caption = _read_caption(path)
            with path.open("rb") as fh:
                await context.bot.send_document(
                    chat_id=ALLOWED_USER_ID,
                    document=fh,
                    filename=path.name,
                    caption=caption,
                )
            _archive_outbox(path)
            log.info("outbox sent %s", path.name)
        except Exception as e:  # noqa: BLE001 — keep the loop alive on any one failure
            log.warning("outbox send failed for %s: %s", path, e)
        finally:
            inflight.discard(path)


def _read_caption(path: Path) -> str:
    """Caption from an optional "<filename>.caption.txt" sidecar, else the
    filename. Lets the document keep a clean name (e.g. widget.zip) while the
    message still discloses context. Trimmed to Telegram's caption limit."""
    sidecar = path.with_name(path.name + _CAPTION_SUFFIX)
    if sidecar.is_file():
        try:
            text = sidecar.read_text(encoding="utf-8").strip()
            if text:
                return text[:TELEGRAM_CAPTION_LIMIT]
        except OSError as e:  # noqa: BLE001 — fall back to the filename
            log.warning("caption read failed for %s: %s", sidecar, e)
    return path.name


def _archive_outbox(path: Path) -> None:
    """Move a delivered file out of the outbox into the dated sent/ archive."""
    # Drop any caption sidecar once its file has been delivered.
    sidecar = path.with_name(path.name + _CAPTION_SUFFIX)
    if sidecar.is_file():
        try:
            sidecar.unlink()
        except OSError as e:  # noqa: BLE001
            log.warning("caption cleanup failed for %s: %s", sidecar, e)
    try:
        rel = path.relative_to(OUTBOX_DIR)
    except ValueError:
        rel = Path(path.name)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = SENT_DIR / day / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest = dest.with_name(f"{_stamp()}_{dest.name}")
    shutil.move(str(path), str(dest))
    # Tidy up any empty subdirectories left behind in the outbox.
    parent = path.parent
    while parent != OUTBOX_DIR and parent.is_dir():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me photos, files, albums, audio or voice notes and I'll save them "
        f"on the server and reply with the host path.\n\nThe other way works too: "
        f"anything an agent drops into the outbox on the VM gets DM'd back to you "
        f"here.\n\nFiles auto-delete after {RETENTION_DAYS} days. Use /list to see "
        f"recent drops."
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
    cutoff = datetime.now(timezone.utc).timestamp() - RETENTION_DAYS * 86400
    removed = 0
    # Expire both received drops and the archive of files we've already sent.
    for root in (DROPS_DIR, SENT_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError as e:
                    log.warning("cleanup failed for %s: %s", path, e)
        # Drop empty day-directories.
        for d in sorted(root.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass
    if removed:
        log.info("cleanup removed %d expired file(s)", removed)


async def _post_init(app: Application) -> None:
    # Enforce a clean command menu, replacing any commands left by a prior bot
    # that used this token.
    await app.bot.set_my_commands([
        BotCommand("start", "Usage help"),
        BotCommand("list", "Show recent drops"),
    ])


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last-resort handler so silent exceptions don't leave the user hanging."""
    log.error("unhandled error: %s", context.error, exc_info=context.error)
    if not isinstance(update, Update) or not update.effective_message:
        return
    if update.effective_user and update.effective_user.id != ALLOWED_USER_ID:
        return
    try:
        await update.effective_message.reply_text(
            "⚠️ Something went wrong saving that file. Please try again.",
        )
    except Exception as e:  # noqa: BLE001 — never crash the error handler
        log.warning("error reply failed: %s", e)


def main() -> None:
    DROPS_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    # Defaults are connect/read/write = 5s — too tight for getFile + photo CDN.
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=20.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=10.0,
        media_write_timeout=120.0,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(HTTPXRequest(
            connection_pool_size=2,
            connect_timeout=20.0,
            read_timeout=60.0,
            write_timeout=30.0,
            pool_timeout=10.0,
        ))
        .post_init(_post_init)
        .build()
    )

    only_me = filters.User(user_id=ALLOWED_USER_ID)
    media = (
        filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO
        | filters.VOICE | filters.VIDEO_NOTE | filters.ANIMATION | filters.Sticker.ALL
    )
    app.add_handler(CommandHandler("start", cmd_start, filters=only_me))
    app.add_handler(CommandHandler("list", cmd_list, filters=only_me))
    app.add_handler(MessageHandler(only_me & media, handle_media))
    app.add_error_handler(_on_error)

    app.job_queue.run_repeating(cleanup, interval=6 * 3600, first=10)
    app.job_queue.run_repeating(_drain_outbox, interval=OUTBOX_POLL_SEC, first=5)

    log.info("filedropbot starting (user=%s, retention=%dd)", ALLOWED_USER_ID, RETENTION_DAYS)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
