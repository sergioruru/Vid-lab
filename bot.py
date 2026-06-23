#!/usr/bin/env python3
"""
vid-lab Telegram Bot
"""
import os, sys, logging, asyncio, re
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from core import download_video
from s3_upload import upload_file

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- Config ---
BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, ".env")) as f:
    BOT_TOKEN = f.read().strip().split("=", 1)[1].strip('"\' \n')
FREE_LIMIT = 5
S3_THRESHOLD_MB = 42

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- DB ---
import sqlite3, datetime

DB_PATH = os.path.join(BASE, "bot.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            tier TEXT DEFAULT 'free',
            donator INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            user_id INTEGER,
            date TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date)
        )
    """)
    conn.commit()
    conn.close()


def register_user(user_id: int, username: str = None):
    """Зарегистрировать пользователя при первом обращении."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)",
        (user_id, username or str(user_id))
    )
    conn.commit()
    conn.close()


def get_daily_count(user_id: int) -> int:
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT count FROM downloads WHERE user_id=? AND date=?", (user_id, today)).fetchone()
    conn.close()
    return row[0] if row else 0


def increment_daily(user_id: int):
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO downloads (user_id, date, count) VALUES (?, ?, 1) "
        "ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1",
        (user_id, today)
    )
    conn.commit()
    conn.close()


def is_premium(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT tier FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row and row[0] in ("pro", "agency")


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id, update.effective_user.username)
    await update.message.reply_text(
        f"🎬 vid-lab — скачиваю и обрабатываю видео\n\n"
        f"Просто отправь ссылку на YouTube / Instagram / TikTok\n\n"
        f"▫️ Бесплатно: {FREE_LIMIT} видео/день\n"
        f"▫️ Pro: безлимит — 990₽/мес\n"
        f"▫️ Agency: мониторинг + пакетная обработка — 2490₽/мес\n\n"
        f"Помощь: /help"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 vid-lab | B2B-инструмент для видео\n\n"
        "📥 Отправь ссылку — бот скачает видео\n"
        "📺 YouTube, Instagram, TikTok\n\n"
        "Команды:\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/stats — моя статистика\n"
        "/donate — поддержать проект\n\n"
        "Pro → @rusinov_s"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    daily = get_daily_count(user_id)
    remaining = FREE_LIMIT - daily if not is_premium(user_id) else "∞"
    await update.message.reply_text(
        f"📊 Статистика\n\n"
        f"Скачиваний сегодня: {daily}\n"
        f"Осталось: {remaining}\n"
        f"Тариф: {'Pro' if is_premium(user_id) else 'Free'}"
    )


async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚧 Донат-модель скоро. Пока пиши @rusinov_s")


async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    register_user(user_id, update.effective_user.username)
    url = update.message.text.strip()

    # Проверка лимита
    if not is_premium(user_id):
        daily = get_daily_count(user_id)
        if daily >= FREE_LIMIT:
            await update.message.reply_text(
                f"❌ Дневной лимит ({FREE_LIMIT} видео) исчерпан.\n"
                f"Подписка Pro: 990₽/мес — безлимит.\n"
                f"По вопросам: @rusinov_s"
            )
            return

    # Валидация URL
    patterns = [
        r"(https?://)?(www\.)?(youtube\.com|youtu\.be|m\.youtube\.com)/",
        r"(https?://)?(www\.)?instagram\.com/",
        r"(https?://)?(www\.)?tiktok\.com/",
        r"(https?://)?(vm\.|vt\.)?tiktok\.com/",
    ]
    if not any(re.match(p, url) for p in patterns):
        await update.message.reply_text("⚠️ Отправь ссылку на YouTube / Instagram / TikTok")
        return

    # Прогресс-бар
    async def update_status(text):
        nonlocal msg
        try:
            await msg.edit_text(text[:200])
        except:
            pass

    msg = await update.message.reply_text("⏳ Скачиваю...")

    # Скачиваем в потоке с callback в главный event loop
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: download_video(url, progress_callback=lambda t: asyncio.run_coroutine_threadsafe(update_status(t), loop))
    )

    if result["error"]:
        await msg.edit_text(f"❌ Ошибка: {result['error']}")
        return

    file_size = result["size_mb"]
    file_path = result["path"]

    try:
        if file_size <= S3_THRESHOLD_MB:
            # Отправляем напрямую
            caption = f"✅ {result['title'][:60]}\n{file_size} MiB"
            with open(file_path, "rb") as f:
                await update.message.reply_video(
                    video=f, caption=caption,
                    supports_streaming=True,
                    read_timeout=180, write_timeout=180,
                )
            increment_daily(user_id)
            await msg.delete()
        else:
            # >42 MiB → S3
            await msg.edit_text("⏳ Загружаю в облако...")
            safe_name = re.sub(r'[^\w\.-]', '_', f"{result['title'][:50]}.mp4")
            s3_url = await loop.run_in_executor(None, lambda: upload_file(file_path, safe_name))
            if s3_url:
                await msg.edit_text(
                    f"✅ {result['title'][:60]}\n{file_size} MiB\n"
                    f"Ссылка на 24ч:\n{s3_url}"
                )
                increment_daily(user_id)
            else:
                await msg.edit_text("❌ Не удалось загрузить в облако.")
    finally:
        # Чистка
        try:
            os.remove(file_path)
            compressed = file_path.rsplit(".", 1)[0] + "_compressed.mp4"
            if os.path.exists(compressed):
                os.remove(compressed)
        except:
            pass


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")


# --- Main ---
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("donate", donate))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_url))
    app.add_error_handler(error_handler)

    logger.info("🤖 vid-lab бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
