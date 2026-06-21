import os
import sys
import json
import subprocess
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultVideo, InputTextMessageContent
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, InlineQueryHandler,
    filters, ContextTypes
)

load_dotenv()

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
ADMIN_ID = 779073332
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
pending_urls = {}
shared_videos = {}

def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)

def track_user(user):
    users = {}
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            users = json.load(f)
    uid = str(user.id)
    users[uid] = {
        "id": user.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
    }
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user)
    await update.message.reply_text(
        "кинь ссылку и я скачаю\n\n"
        "поддерживаю: YouTube, TikTok, Instagram, Twitter и ещё сотни сайтов"
    )

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    if not users:
        await update.message.reply_text("никто еще не писал")
        return
    lines = []
    for uid, info in users.items():
        name = info.get("first_name", "")
        username = info.get("username", "")
        display = f"{name}"
        if username:
            display += f" (@{username})"
        lines.append(f"{uid} — {display}")
    await update.message.reply_text("\n".join(lines))

def get_user_dir(user_id):
    user_dir = os.path.join(DOWNLOADS_DIR, str(user_id))
    ensure_dir(user_dir)
    return user_dir

async def download_file(url, user_id, audio_only=False):
    user_dir = get_user_dir(user_id)
    out_path = os.path.join(user_dir, "%(title)s.%(ext)s")
    if audio_only:
        cmd = [sys.executable, "-m", "yt_dlp", "--js-runtimes", "nodejs", "-x", "--audio-format", "mp3", "-o", out_path, url]
    else:
        cmd = [sys.executable, "-m", "yt_dlp", "--js-runtimes", "nodejs", "-o", out_path, url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        return None, None, result.stderr[:500] if result.stderr else "неизвестная ошибка"
    files = sorted(os.listdir(user_dir), key=lambda x: os.path.getmtime(os.path.join(user_dir, x)), reverse=True)
    if not files:
        return None, None, "файл не найден"
    filepath = os.path.join(user_dir, files[0])
    size = os.path.getsize(filepath)
    size_mb = round(size / 1024 / 1024, 1)
    return filepath, size_mb, None

CAPTION = "скачано с помощью @SavePitBot_bot"

async def send_file(update, filepath, filename, audio_only):
    original = filepath
    size = os.path.getsize(filepath)
    if size > 50 * 1024 * 1024 and not audio_only:
        size_mb = round(size / 1024 / 1024, 1)
        await update.message.reply_text(
            f"файл {size_mb}мб — Telegram не отправляет больше 50мб. "
            f"Сжимаю чтобы влезло, качество может стать чуть хуже"
        )
        compressed = filepath.rsplit(".", 1)[0] + "_compressed.mp4"
        cmd = ["ffmpeg", "-i", filepath, "-fs", "48M", "-c:v", "libx264", "-preset", "fast", "-crf", "28", "-c:a", "aac", "-b:a", "128k", compressed, "-y"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and os.path.exists(compressed):
            filepath = compressed
            filename = os.path.basename(compressed)
        else:
            await update.message.reply_text("не удалось сжать, попробуй другое видео")
            _cleanup(original, None)
            return
    with open(filepath, "rb") as f:
        if audio_only:
            msg = await update.message.reply_audio(audio=f, filename=filename, caption=CAPTION)
        else:
            msg = await update.message.reply_video(video=f, filename=filename, caption=CAPTION)
    if msg.video:
        shared_videos[str(msg.message_id)] = {
            "file_id": msg.video.file_id,
            "caption": CAPTION
        }
    elif msg.audio:
        shared_videos[str(msg.message_id)] = {
            "file_id": msg.audio.file_id,
            "caption": CAPTION,
            "audio": True
        }
    keyboard = [[InlineKeyboardButton("поделиться", switch_inline_query="share")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("нажми чтобы переслать", reply_markup=reply_markup)
    _cleanup(original, filepath)

def _cleanup(original, compressed):
    for f in [original, compressed]:
        if f and os.path.exists(f):
            try:
                os.unlink(f)
            except Exception:
                pass

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query
    if not query:
        results = [
            InlineQueryResultVideo(
                id="help",
                video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                mime_type="video/mp4",
                thumbnail_url="https://img.youtube.com/vi/dQw4w9WgXcQ/0.jpg",
                title="кинь ссылку боту в личку",
                description="скачано с помощью @SavePitBot_bot",
                caption=CAPTION,
            )
        ]
        await update.inline_query.answer(results, cache_time=1)
        return
    user_id = update.inline_query.from_user.id
    await update.inline_query.answer([], cache_time=1)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user)
    text = update.message.text
    if not text:
        return

    url = text.strip()
    uid = update.effective_user.id

    if "tiktok.com" in url or "vm.tiktok" in url:
        await update.message.reply_text("скачиваю с TikTok...")
        try:
            filepath, size_mb, error = await download_file(url, uid, audio_only=False)
            if not filepath:
                await update.message.reply_text(f"ошибка: {error}")
                return
            await send_file(update, filepath, os.path.basename(filepath), False)
        except subprocess.TimeoutExpired:
            await update.message.reply_text("таймаут, попробуй позже")
        except Exception as e:
            await update.message.reply_text(f"ошибка: {e}")

    elif "youtube.com" in url or "youtu.be" in url:
        msg_id = f"{uid}_{update.message.message_id}"
        pending_urls[msg_id] = url
        keyboard = [
            [InlineKeyboardButton("видео", callback_data=f"dl_video|{msg_id}")],
            [InlineKeyboardButton("музыка (mp3)", callback_data=f"dl_audio|{msg_id}")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("что скачать?", reply_markup=reply_markup)

    elif "instagram.com" in url or "twitter.com" in url or "x.com" in url:
        await update.message.reply_text("скачиваю...")
        try:
            filepath, size_mb, error = await download_file(url, uid, audio_only=False)
            if not filepath:
                await update.message.reply_text(f"ошибка: {error}")
                return
            await send_file(update, filepath, os.path.basename(filepath), False)
        except subprocess.TimeoutExpired:
            await update.message.reply_text("таймаут")
        except Exception as e:
            await update.message.reply_text(f"ошибка: {e}")

    elif url.startswith("http://") or url.startswith("https://"):
        msg_id = f"{uid}_{update.message.message_id}"
        pending_urls[msg_id] = url
        keyboard = [
            [InlineKeyboardButton("видео", callback_data=f"dl_video|{msg_id}")],
            [InlineKeyboardButton("музыка (mp3)", callback_data=f"dl_audio|{msg_id}")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("что скачать с этого сайта?", reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split("|")
    action = data[0]
    msg_id = data[1]

    url = pending_urls.get(msg_id)
    if not url:
        await query.edit_message_text("ссылка устарела, отправь заново")
        return

    del pending_urls[msg_id]
    user_id = query.from_user.id

    if action == "dl_video":
        await query.edit_message_text("скачиваю видео...")
        try:
            filepath, size_mb, error = await download_file(url, user_id, audio_only=False)
            if not filepath:
                await query.edit_message_text(f"ошибка: {error}")
                return
            await send_file(query, filepath, os.path.basename(filepath), False)
        except subprocess.TimeoutExpired:
            await query.edit_message_text("таймаут")
        except Exception as e:
            await query.edit_message_text(f"ошибка: {e}")

    elif action == "dl_audio":
        await query.edit_message_text("скачиваю музыку...")
        try:
            filepath, size_mb, error = await download_file(url, user_id, audio_only=True)
            if not filepath:
                await query.edit_message_text(f"ошибка: {error}")
                return
            await send_file(query, filepath, os.path.basename(filepath), True)
        except subprocess.TimeoutExpired:
            await query.edit_message_text("таймаут")
        except Exception as e:
            await query.edit_message_text(f"ошибка: {e}")

if __name__ == "__main__":
    print(f"TOKEN: {BOT_TOKEN[:10]}...", flush=True)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("download bot started", flush=True)
    app.run_polling(drop_pending_updates=True)
