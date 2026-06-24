"""Telegram bot that downloads video/audio from YouTube, TikTok, Instagram, X and
hundreds of other sites via yt-dlp. Send a link, pick video or audio — done.
"""

import os
import re
import sys
import html
import time
import json
import shutil
import asyncio
import logging
import tempfile

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultArticle,
    InputTextMessageContent,
    BotCommand,
    BotCommandScopeChat,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    Defaults,
    filters,
    ContextTypes,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

load_dotenv()

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
ADMIN_ID = 779073332
BOT_USERNAME = "SavePitBot_bot"
CAPTION = f"скачано с помощью @{BOT_USERNAME}"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
USERS_FILE = os.path.join(BASE_DIR, "users.json")

# Telegram bot API caps uploads at 50 MB. Keep a margin when compressing.
LIMIT_BYTES = 50 * 1024 * 1024
SAFE_BYTES = 49 * 1024 * 1024     # accept a compressed file up to this size
TARGET_BYTES = 46 * 1024 * 1024   # aim for this when computing bitrate

DOWNLOAD_TIMEOUT = 300
COMPRESS_TIMEOUT = 420
PROBE_TIMEOUT = 30

MAX_CONCURRENT = 3   # parallel downloads/encodes across all users
RECENT_CAP = 20      # how many recent files each user can re-share inline
PENDING_TTL = 3600   # forget unanswered "what to download?" prompts after 1h

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("download_bot")

# --------------------------------------------------------------------------- #
# Runtime state (single-process, asyncio — plain dicts/sets are safe)
# --------------------------------------------------------------------------- #

pending_urls: dict[str, tuple[str, float]] = {}   # msg_id -> (url, created_at)
recent_media: dict[int, list[dict]] = {}           # user_id -> [{file_id,type,title}]
active_users: set[int] = set()                     # users with a download in flight
USERS: dict[str, dict] = {}
users_lock = asyncio.Lock()
SEM: asyncio.Semaphore | None = None               # created in post_init

esc = html.escape


# --------------------------------------------------------------------------- #
# External tools discovery (ffmpeg / ffprobe / node)
# --------------------------------------------------------------------------- #

def _find_bin(name: str) -> str | None:
    """Locate an executable: env override -> PATH -> local .exe on Windows."""
    override = os.getenv(name.upper() + "_PATH")
    if override and os.path.exists(override):
        return override
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        local = os.path.join(BASE_DIR, name + ".exe")
        if os.path.exists(local):
            return local
    return None


def _find_node_dir() -> str | None:
    found = shutil.which("node")
    if found:
        return os.path.dirname(found)
    if sys.platform == "win32":
        guess = r"C:\Program Files\nodejs"
        if os.path.exists(os.path.join(guess, "node.exe")):
            return guess
    return None


FFMPEG = _find_bin("ffmpeg")
FFPROBE = _find_bin("ffprobe")
if not FFPROBE and FFMPEG:  # ffprobe usually ships next to ffmpeg
    sibling = os.path.join(
        os.path.dirname(FFMPEG), "ffprobe" + (".exe" if sys.platform == "win32" else "")
    )
    if os.path.exists(sibling):
        FFPROBE = sibling
FFMPEG_DIR = os.path.dirname(FFMPEG) if FFMPEG else None
NODE_DIR = _find_node_dir()
NODE_OK = NODE_DIR is not None


def build_env() -> dict:
    """Environment for subprocesses with ffmpeg/node folders on PATH."""
    env = os.environ.copy()
    extra = [p for p in (FFMPEG_DIR, NODE_DIR) if p]
    if extra:
        env["PATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PATH", "")
    return env


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def mb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} МБ"


URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(").,;!?'\"")


def platform_label(url: str) -> str:
    u = url.lower()
    table = [
        (("youtube.com", "youtu.be"), "YouTube ▶️"),
        (("tiktok.com", "vm.tiktok"), "TikTok 🎵"),
        (("instagram.com", "instagr.am"), "Instagram 📸"),
        (("twitter.com", "x.com", "t.co/"), "X (Twitter) 🐦"),
        (("facebook.com", "fb.watch"), "Facebook 📘"),
        (("vk.com", "vk.ru", "vkvideo"), "VK 🅥"),
        (("soundcloud.com",), "SoundCloud 🎧"),
        (("reddit.com", "redd.it"), "Reddit 👽"),
        (("twitch.tv",), "Twitch 🎮"),
    ]
    for needles, label in table:
        if any(n in u for n in needles):
            return label
    return "по ссылке 🔗"


async def run(cmd: list[str], timeout: float, env: dict | None = None):
    """Run a subprocess without blocking the event loop. Raises on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    return (
        proc.returncode,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def safe_edit(message, text: str, reply_markup=None) -> None:
    """Edit a message, swallowing the harmless 'not modified' error."""
    try:
        await message.edit_text(
            text, reply_markup=reply_markup, disable_web_page_preview=True
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit_text failed: %s", e)
    except Exception as e:  # network etc. — never crash the handler over a status edit
        log.warning("edit_text failed: %s", e)


def friendly_error(stderr: str) -> str:
    """Turn a raw yt-dlp stderr blob into a short human message."""
    s = (stderr or "").lower()
    if "private" in s:
        return "🔒 Это видео приватное — скачать не получится."
    if "age" in s and "restrict" in s:
        return "🔞 Видео с возрастным ограничением — не могу его скачать."
    if "sign in" in s or "log in" in s or "cookies" in s or "account" in s:
        return "🔑 Для этого видео нужна авторизация — скачать не выйдет."
    if "geo" in s or "not available in your country" in s or "region" in s:
        return "🌍 Видео недоступно в регионе сервера."
    if "unsupported url" in s or "no video" in s or "no media" in s:
        return "🤷 С этой ссылки не получается ничего скачать. Проверь, что она ведёт на видео."
    if "video unavailable" in s or "removed" in s or "deleted" in s:
        return "🚫 Видео недоступно или удалено."
    if "unable to download" in s or "http error 4" in s:
        return "📡 Не удалось загрузить — возможно, ссылка устарела."
    tail = ""
    for line in reversed((stderr or "").strip().splitlines()):
        line = line.strip()
        if line.lower().startswith("error"):
            tail = line
            break
    tail = tail or ((stderr or "").strip().splitlines() or [""])[-1]
    return "⚠️ Не получилось скачать.\n<code>{}</code>".format(esc(tail[:200]))


# --------------------------------------------------------------------------- #
# User tracking (in-memory cache, atomic + non-blocking persistence)
# --------------------------------------------------------------------------- #

def load_users() -> dict:
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_users() -> None:
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(USERS, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)  # atomic on the same filesystem


async def track_user(user) -> None:
    if user is None:
        return
    uid = str(user.id)
    info = {
        "id": user.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
    }
    if USERS.get(uid) == info:
        return  # nothing changed — skip the disk write
    USERS[uid] = info
    async with users_lock:
        await asyncio.to_thread(save_users)


def remember_media(user_id: int, file_id: str, mtype: str, title: str) -> None:
    bucket = recent_media.setdefault(user_id, [])
    bucket.append({"file_id": file_id, "type": mtype, "title": (title or "")[:60]})
    del bucket[:-RECENT_CAP]  # keep only the most recent RECENT_CAP entries


def prune_pending() -> None:
    now = time.time()
    for key, (_, created) in list(pending_urls.items()):
        if now - created > PENDING_TTL:
            pending_urls.pop(key, None)


# --------------------------------------------------------------------------- #
# Downloading & media processing
# --------------------------------------------------------------------------- #

def _newest_real_file(folder: str) -> str | None:
    candidates = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and not name.endswith((".part", ".ytdl", ".tmp")):
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=os.path.getsize)


async def download(url: str, job_dir: str, audio_only: bool):
    """Download into a private job dir. Returns (filepath, error_message)."""
    out_tmpl = os.path.join(job_dir, "%(title).100B.%(ext)s")
    cookies_file = os.path.join(BASE_DIR, "cookies.txt")
    # Write cookies from env var if file doesn't exist yet
    if not os.path.exists(cookies_file):
        cookies_env = os.getenv("YOUTUBE_COOKIES", "")
        if cookies_env:
            with open(cookies_file, "w", encoding="utf-8") as f:
                f.write(cookies_env)
    base = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--no-warnings", "--no-progress",
        "--retries", "3", "--fragment-retries", "3",
        "--no-simulate", "--print", "after_move:filepath",
        "-o", out_tmpl,
    ]
    if os.path.exists(cookies_file):
        base += ["--cookies", cookies_file]
    if FFMPEG_DIR:
        base += ["--ffmpeg-location", FFMPEG_DIR]
    if audio_only:
        base += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        base += [
            "-f", "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
            "--merge-output-format", "mp4",
        ]

    env = build_env()
    js = ["--js-runtimes", "node"] if NODE_OK else []
    rc, out, err = await run(base + js + [url], DOWNLOAD_TIMEOUT, env)

    # Older/newer yt-dlp may not know --js-runtimes; retry once without it.
    if rc != 0 and js and re.search(
        r"no such option|unrecognized|js[-_]runtimes", err, re.IGNORECASE
    ):
        rc, out, err = await run(base + [url], DOWNLOAD_TIMEOUT, env)

    if rc != 0:
        return None, err

    # Prefer the exact path yt-dlp printed; fall back to scanning the job dir.
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line and os.path.isfile(line):
            return line, None
    found = _newest_real_file(job_dir)
    if found:
        return found, None
    return None, "Файл не найден после скачивания."


async def probe_duration(path: str) -> float | None:
    if not FFPROBE:
        return None
    try:
        rc, out, _ = await run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            PROBE_TIMEOUT,
        )
        if rc == 0 and out.strip():
            return float(out.strip())
    except Exception:
        pass
    return None


async def probe_video_codec(path: str) -> tuple[str, str]:
    """Returns (codec_name, pix_fmt) for the first video stream."""
    if not FFPROBE:
        return "", ""
    try:
        rc, out, _ = await run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt",
             "-of", "default=nw=1:nk=0", path],
            PROBE_TIMEOUT,
        )
        if rc != 0:
            return "", ""
        codec, pix = "", ""
        for line in out.splitlines():
            if line.startswith("codec_name="):
                codec = line.split("=", 1)[1].strip()
            elif line.startswith("pix_fmt="):
                pix = line.split("=", 1)[1].strip()
        return codec, pix
    except Exception:
        return "", ""


async def ensure_ios_compat(src: str, job_dir: str) -> str:
    """Ensure video plays on iOS: H.264 yuv420p MP4 with faststart.

    Fast path: stream-copy remux when already H.264 + 8-bit (~1 s overhead).
    Slow path: full re-encode for HEVC, VP9, 10-bit, etc.
    """
    if not FFMPEG:
        return src
    codec, pix_fmt = await probe_video_codec(src)
    out = os.path.join(job_dir, "compat.mp4")
    if codec == "h264" and pix_fmt in ("yuv420p", "yuvj420p"):
        rc, _, _ = await run(
            [FFMPEG, "-y", "-i", src, "-c", "copy", "-movflags", "+faststart", out],
            60, env=build_env(),
        )
    else:
        rc, _, _ = await run(
            [FFMPEG, "-y", "-i", src,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", out],
            COMPRESS_TIMEOUT, env=build_env(),
        )
    if rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    return src  # ffmpeg failed — send original, better than nothing


async def _encode_video(src, dst, vbitrate=None, crf=None, width=1280) -> bool:
    vf = f"scale='min({width},iw)':-2"  # downscale only, keep height even
    cmd = [FFMPEG, "-y", "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast"]
    if vbitrate:
        cmd += ["-b:v", str(vbitrate),
                "-maxrate", str(int(vbitrate * 1.5)),
                "-bufsize", str(int(vbitrate * 2))]
    else:
        cmd += ["-crf", str(crf)]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst]
    rc, _, err = await run(cmd, COMPRESS_TIMEOUT, env=build_env())
    if rc != 0:
        log.warning("ffmpeg encode failed: %s", err[-300:])
        return False
    return os.path.exists(dst) and os.path.getsize(dst) > 0


async def compress_video(src: str, job_dir: str) -> str | None:
    """Re-encode a video to fit under the Telegram limit. None if impossible."""
    if not FFMPEG:
        return None
    out = os.path.join(job_dir, "fit.mp4")
    duration = await probe_duration(src)
    if duration and duration > 0:
        audio_bps = 128_000
        video_bps = max(int(TARGET_BYTES * 8 / duration - audio_bps), 150_000)
        for factor in (1.0, 0.8):  # second pass if the first overshoots
            if await _encode_video(src, out, vbitrate=int(video_bps * factor)):
                if os.path.getsize(out) <= SAFE_BYTES:
                    return out
    else:
        for crf, width in ((30, 1280), (34, 854)):  # no duration -> CRF ladder
            if await _encode_video(src, out, crf=crf, width=width):
                if os.path.getsize(out) <= SAFE_BYTES:
                    return out
    if os.path.exists(out) and os.path.getsize(out) <= SAFE_BYTES:
        return out
    return None


async def compress_audio(src: str, job_dir: str) -> str | None:
    if not FFMPEG:
        return None
    out = os.path.join(job_dir, "fit.mp3")
    duration = await probe_duration(src)
    if duration and duration > 0:
        bitrate = max(48_000, min(int(TARGET_BYTES * 8 / duration), 192_000))
    else:
        bitrate = 96_000
    rc, _, err = await run(
        [FFMPEG, "-y", "-i", src, "-c:a", "libmp3lame", "-b:a", str(bitrate), out],
        COMPRESS_TIMEOUT, env=build_env(),
    )
    if rc == 0 and os.path.exists(out) and os.path.getsize(out) <= SAFE_BYTES:
        return out
    return None


async def send_media(context, chat_id, status_msg, path, audio_only, user, title):
    filename = os.path.basename(path)
    timeouts = dict(read_timeout=120, write_timeout=300,
                    connect_timeout=30, pool_timeout=120)
    # The share button rides on the media message itself, so it always shows up
    # together with the file — never as a separate message above it.
    share_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📤 Переслать в чат", switch_inline_query="")]]
    )
    with open(path, "rb") as f:
        if audio_only:
            sent = await context.bot.send_audio(
                chat_id=chat_id, audio=f, filename=filename,
                caption=CAPTION, reply_markup=share_kb, **timeouts,
            )
        else:
            sent = await context.bot.send_video(
                chat_id=chat_id, video=f, filename=filename,
                caption=CAPTION, supports_streaming=True,
                reply_markup=share_kb, **timeouts,
            )

    file_id, mtype = None, None
    if sent.video:
        file_id, mtype = sent.video.file_id, "video"
    elif sent.audio:
        file_id, mtype = sent.audio.file_id, "audio"
    elif sent.document:
        file_id, mtype = sent.document.file_id, "document"
    if file_id:
        remember_media(user.id, file_id, mtype, title)

    # Remove the "📤 Отправляю…" status so only the media (with its button)
    # remains, in the correct order — right after the user's link.
    try:
        await status_msg.delete()
    except Exception:
        await safe_edit(status_msg, f"✅ <b>{esc(title)}</b> — готово!")


async def process(context, status_msg, chat_id, user, url, audio_only):
    """Full pipeline for one request: download -> (compress) -> send -> cleanup."""
    active_users.add(user.id)
    job_dir = tempfile.mkdtemp(prefix="job_", dir=DOWNLOADS_DIR)
    try:
        async with SEM:
            kind = "музыку 🎵" if audio_only else "видео 🎬"
            await safe_edit(status_msg, f"⏳ Скачиваю {kind}…")

            path, err = await download(url, job_dir, audio_only)
            if not path:
                await safe_edit(status_msg, friendly_error(err))
                return

            if not audio_only:
                path = await ensure_ios_compat(path, job_dir)

            title = os.path.splitext(os.path.basename(path))[0]
            size = os.path.getsize(path)

            if size > LIMIT_BYTES:
                await safe_edit(
                    status_msg,
                    f"📦 Файл большой ({mb(size)}) — ужимаю под лимит Telegram…",
                )
                if audio_only:
                    fitted = await compress_audio(path, job_dir)
                else:
                    fitted = await compress_video(path, job_dir)
                if not fitted:
                    await safe_edit(
                        status_msg,
                        "😔 Не получилось уместить файл в 50 МБ.\n"
                        "Попробуй ролик покороче или скачай как музыку.",
                    )
                    return
                path, size = fitted, os.path.getsize(fitted)

            await safe_edit(status_msg, "📤 Отправляю…")
            await send_media(context, chat_id, status_msg, path, audio_only, user, title)

    except asyncio.TimeoutError:
        await safe_edit(
            status_msg,
            "⌛️ Слишком долго качается. Попробуй ещё раз или выбери что-то покороче.",
        )
    except Exception:
        log.exception("process failed for %s", url)
        await safe_edit(status_msg, "⚠️ Что-то пошло не так. Попробуй ещё раз чуть позже.")
    finally:
        active_users.discard(user.id)
        shutil.rmtree(job_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await track_user(update.effective_user)
    name = esc(update.effective_user.first_name or "")
    await update.message.reply_text(
        f"👋 Привет, <b>{name}</b>!\n\n"
        "Я скачаю для тебя видео или музыку почти откуда угодно:\n\n"
        "▶️ YouTube\n🎵 TikTok\n📸 Instagram\n🐦 X (Twitter)\n"
        "…и ещё сотни сайтов.\n\n"
        "📎 Просто пришли ссылку — а я спрошу, что скачать: "
        "<b>видео</b> или <b>музыку</b>.\n\n"
        "ℹ️ Лимит Telegram — 50 МБ. Файлы побольше я сжимаю автоматически."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Как пользоваться</b>\n\n"
        "1️⃣ Пришли мне ссылку на видео.\n"
        "2️⃣ Выбери <b>🎬 Видео</b> или <b>🎵 Музыку</b>.\n"
        "3️⃣ Получи файл и при желании жми <b>📤 Поделиться</b>, "
        "чтобы переслать его в любой чат.\n\n"
        "Поддерживаю YouTube, TikTok, Instagram, X (Twitter) и сотни других сайтов.\n\n"
        "ℹ️ Telegram не принимает файлы больше 50 МБ — крупные видео я "
        "автоматически сжимаю, поэтому качество может немного снизиться."
    )


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not USERS:
        await update.message.reply_text("Пока никто не писал.")
        return
    lines = [f"👥 Пользователей: <b>{len(USERS)}</b>", ""]
    for uid, info in USERS.items():
        display = esc(info.get("first_name", "") or "—")
        username = info.get("username", "")
        if username:
            display += f" (@{esc(username)})"
        lines.append(f"<code>{uid}</code> — {display}")
    text = "\n".join(lines)
    for i in range(0, len(text), 3900):  # stay under Telegram's 4096 char limit
        await update.message.reply_text(text[i:i + 3900])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await track_user(update.effective_user)
    url = extract_url(update.message.text or "")
    if not url:
        await update.message.reply_text(
            "🤔 Это не похоже на ссылку.\n\n"
            "Пришли ссылку на видео — например, с YouTube, TikTok или Instagram, "
            "и я всё скачаю."
        )
        return

    prune_pending()
    msg_id = f"{update.effective_user.id}_{update.message.message_id}"
    pending_urls[msg_id] = (url, time.time())
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Видео", callback_data=f"dl_video|{msg_id}")],
        [InlineKeyboardButton("🎵 Музыка (MP3)", callback_data=f"dl_audio|{msg_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel|{msg_id}")],
    ])
    await update.message.reply_text(
        f"🔗 <b>{esc(platform_label(url))}</b>\nЧто скачать?",
        reply_markup=keyboard,
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        action, msg_id = query.data.split("|", 1)
    except ValueError:
        await query.answer()
        return

    if action == "cancel":
        pending_urls.pop(msg_id, None)
        await query.answer("Отменено")
        await safe_edit(query.message, "❌ Отменено.")
        return

    entry = pending_urls.get(msg_id)
    if not entry:
        await query.answer()
        await safe_edit(query.message, "🔗 Ссылка устарела — пришли её ещё раз.")
        return

    user = query.from_user
    if user.id in active_users:
        await query.answer("⏳ Я ещё качаю предыдущее, подожди секунду 🙏")
        return  # keep the pending entry so the user can retry afterwards

    await query.answer()
    pending_urls.pop(msg_id, None)
    await process(
        context, query.message, query.message.chat_id,
        user, entry[0], audio_only=(action == "dl_audio"),
    )


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Let users forward their recently downloaded files to any chat."""
    needle = (update.inline_query.query or "").strip().lower()
    media = recent_media.get(update.inline_query.from_user.id, [])
    results = []
    for i, item in enumerate(reversed(media)):
        title = item.get("title") or ("Аудио" if item["type"] == "audio" else "Видео")
        if needle and needle not in title.lower():
            continue
        rid = str(i)
        if item["type"] == "video":
            results.append(InlineQueryResultCachedVideo(
                id=rid, video_file_id=item["file_id"], title=title, caption=CAPTION))
        elif item["type"] == "audio":
            results.append(InlineQueryResultCachedAudio(
                id=rid, audio_file_id=item["file_id"], caption=CAPTION))
        else:
            results.append(InlineQueryResultCachedDocument(
                id=rid, title=title, document_file_id=item["file_id"], caption=CAPTION))
        if len(results) >= RECENT_CAP:
            break

    if not results:
        results = [InlineQueryResultArticle(
            id="empty",
            title="Пока нечего пересылать",
            description="Открой бота и пришли ссылку — потом сможешь делиться отсюда",
            input_message_content=InputTextMessageContent(
                f"Скачиваю видео и музыку по ссылке: @{BOT_USERNAME}"),
        )]
    await update.inline_query.answer(results, cache_time=0, is_personal=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled exception", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #

def cleanup_downloads() -> None:
    """Remove leftover job dirs from a previous (possibly crashed) run."""
    if not os.path.isdir(DOWNLOADS_DIR):
        return
    for name in os.listdir(DOWNLOADS_DIR):
        path = os.path.join(DOWNLOADS_DIR, name)
        try:
            shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)
        except Exception:
            pass


async def post_init(app) -> None:
    global SEM
    SEM = asyncio.Semaphore(MAX_CONCURRENT)
    cleanup_downloads()

    commands = [BotCommand("start", "Запустить бота"), BotCommand("help", "Помощь")]
    await app.bot.set_my_commands(commands)
    try:
        await app.bot.set_my_commands(
            commands + [BotCommand("users", "Список пользователей")],
            scope=BotCommandScopeChat(ADMIN_ID),
        )
    except Exception as e:
        log.warning("could not set admin commands: %s", e)

    if not FFMPEG:
        log.warning("ffmpeg not found — audio extraction, merging and "
                    "compression will not work!")
    log.info("ready: ffmpeg=%s ffprobe=%s node=%s",
             bool(FFMPEG), bool(FFPROBE), NODE_OK)


def main() -> None:
    if not BOT_TOKEN:
        sys.exit("❌ TG_BOT_TOKEN не задан. Создай .env со строкой "
                 "TG_BOT_TOKEN=<токен от @BotFather>")
    ensure_dir(DOWNLOADS_DIR)
    USERS.update(load_users())

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)

    log.info("download bot started")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
