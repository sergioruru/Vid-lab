#!/usr/bin/env python3
"""
vid-lab Telegram Bot
"""
import os, sys, logging, asyncio, re
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from core import download_video, estimate_sizes
from s3_upload import upload_file

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

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
            donator INTEGER DEFAULT 0,
            default_quality TEXT DEFAULT '720'
        )
    """)
    # Добавить колонку default_quality, если её нет (старые БД)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN default_quality TEXT DEFAULT '720'")
    except sqlite3.OperationalError:
        pass  # колонка уже есть
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
    row = conn.execute("SELECT tier, tier_expires FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if not row or row[0] not in ("pro", "agency"):
        return False
    # Если есть срок действия — проверяем
    if row[1]:
        try:
            expires = datetime.date.fromisoformat(row[1])
            if datetime.date.today() > expires:
                return False  # тариф истёк
        except (ValueError, TypeError):
            pass
    return True


QUALITY_VALUES = {"360", "480", "720", "1080", "best"}


def get_user_quality(user_id: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT default_quality FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row and row[0] else "720"


def set_user_quality(user_id: int, quality: str):
    if quality not in QUALITY_VALUES:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET default_quality=? WHERE id=?", (quality, user_id))
    conn.commit()
    conn.close()
    return True


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id, update.effective_user.username)
    await update.message.reply_text(
        f"🎬 vid-lab — загружаю видео с YouTube, Instagram, TikTok\n\n"
        f"Просто отправь ссылку — бот оценит размеры ДО скачивания:\n"
        f"▫️ ≤42 MiB → сразу в Telegram\n"
        f"▫️ >42 MiB → ссылка в облаке на 24ч\n\n"
        f"▫️ Бесплатно: {FREE_LIMIT} видео/день\n"
        f"▫️ Pro: безлимит — 990₽/мес\n"
        f"▫️ Agency: мониторинг + пакетная обработка — 2490₽/мес\n\n"
        f"Помощь: /help\n"
        f"Pro → @sergioru"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 vid-lab | B2B-инструмент для видео\n\n"
        "📥 Отправь ссылку — бот скачает видео\n"
        "📺 YouTube, Instagram (Reels), TikTok\n\n"
        f"Команды:\n"
        f"/start — приветствие\n"
        f"/help — эта справка\n"
        f"/quality — качество видео (360/480/720/1080)\n"
        f"/stats — моя статистика\n"
        f"/donate — подписка Pro\n\n"
        f"Pro → @sergioru"
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
    await update.message.reply_text(
        "💎 vid-lab Pro — безлимит на скачивания\n\n"
        "▫️ 990₽/мес — безлимит\n"
        "▫️ 2490₽/мес — Agency (мониторинг + пакетная обработка)\n\n"
        "По вопросам: @sergioru"
    )


def _quality_keyboard(current: str) -> list:
    """Кнопки выбора качества. Текущее — без ссылки."""
    rows = []
    for q in ["360", "480", "720", "1080", "best"]:
        label = f"{'✅ ' if q == current else ''}{q}p"
        rows.append([InlineKeyboardButton(label, callback_data=f"qlty_{q}")])
    rows.append([InlineKeyboardButton("❌ Закрыть", callback_data="qlty_close")])
    return rows


async def quality_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current = get_user_quality(user_id)
    premium = is_premium(user_id)
    args = context.args

    if args:
        q = args[0].lower().replace("p", "")
        if q not in QUALITY_VALUES:
            await update.message.reply_text(
                f"Доступные форматы: 360, 480, 720, 1080, best\n"
                f"Текущий: {current}p\n\n"
                f"Пример: /quality 360"
            )
            return
        if q in ("1080", "best") and not premium:
            await update.message.reply_text(
                "1080p и best доступны только для Pro-подписки.\n"
                f"Текущий: {current}p\nПо вопросам: @sergioru"
            )
            return
        set_user_quality(user_id, q)
        await update.message.reply_text(f"✅ Качество изменено на {q}p")
        return

    # Без аргументов — показываем клавиатуру
    premium = is_premium(user_id)
    text = (
        f"🎬 Качество видео\n\n"
        f"Текущее: {current}p\n"
        f"Тариф: {'Pro' if premium else 'Free'}\n\n"
        f"{'▫️ 1080p и best — только Pro' if not premium else '▫️ Все форматы доступны'}"
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(_quality_keyboard(current)))


async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия кнопок качества."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "qlty_close":
        await query.edit_message_text("❌ Изменения не сохранены")
        return

    q = data.replace("qlty_", "")
    premium = is_premium(user_id)
    if q in ("1080", "best") and not premium:
        await query.edit_message_text(
            f"1080p и best доступны только для Pro.\n"
            f"Текущий: {get_user_quality(user_id)}p\nПо вопросам: @sergioru"
        )
        return

    set_user_quality(user_id, q)
    await query.edit_message_text(f"✅ Качество изменено на {q}p")


async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    register_user(user_id, update.effective_user.username)
    url = update.message.text.strip()
    quality = get_user_quality(user_id)
    premium = is_premium(user_id)

    # Проверка лимита
    if not premium:
        daily = get_daily_count(user_id)
        if daily >= FREE_LIMIT:
            await update.message.reply_text(
                f"❌ Дневной лимит ({FREE_LIMIT} видео) исчерпан.\n"
                f"Подписка Pro: 990₽/мес — безлимит.\n"
                f"По вопросам: @sergioru"
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

    # Предварительная оценка размеров (без скачивания)
    msg = await update.message.reply_text("🔍 Оцениваю размеры...")
    loop = asyncio.get_event_loop()
    sizes = await loop.run_in_executor(None, lambda: estimate_sizes(url))

    if sizes:
        current_size = sizes.get(quality)
        if current_size and current_size > S3_THRESHOLD_MB:
            # Не влезает — показываем панель с вариантами ДО скачивания
            await _show_size_options(update, context, user_id, msg, quality, premium, sizes, url)
            return

    # ≤42 MiB или нет оценки — скачиваем
    async def update_status(text):
        nonlocal msg
        try:
            await msg.edit_text(text[:200])
        except:
            pass

    quality_label = f"{quality.replace('best', 'max')}p"
    await msg.edit_text(f"⏳ Скачиваю ({quality_label})...")

    result = await loop.run_in_executor(
        None,
        lambda: download_video(url, quality=quality, is_premium=premium,
                               progress_callback=lambda t: asyncio.run_coroutine_threadsafe(update_status(t), loop))
    )

    if result["error"]:
        await msg.edit_text(f"❌ Ошибка: {result['error']}")
        return

    file_size = result["size_mb"]
    file_path = result["path"]
    actual_quality = result.get("quality_used", quality)

    try:
        if file_size <= S3_THRESHOLD_MB:
            caption = f"✅ {result['title'][:60]}\n{file_size} MiB ({actual_quality}p)"
            with open(file_path, "rb") as f:
                await update.message.reply_video(
                    video=f, caption=caption,
                    supports_streaming=True,
                    read_timeout=180, write_timeout=180,
                )
            increment_daily(user_id)
            await msg.delete()
        else:
            # >42 MiB — fallback: показываем выбор (без повторного скачивания)
            await _offer_quality_choice(update, context, user_id, msg, result, quality, url)
            increment_daily(user_id)
            return
    finally:
        try:
            os.remove(file_path)
            compressed = file_path.rsplit(".", 1)[0] + "_compressed.mp4"
            if os.path.exists(compressed):
                os.remove(compressed)
        except:
            pass


# --- Хранилище для выбора качества при больших файлах ---
_pending_downloads = {}  # msg_id -> {user_id, url, title, file_path, size, quality, chat_id, message_id}

_PREVIEW_CACHE = {}  # url -> {quality: size_mb}


async def _show_size_options(update, context, user_id, msg, current_quality, premium, sizes, url):
    """Показать размеры ДО скачивания. Только для предварительной оценки."""
    _PREVIEW_CACHE[url] = sizes

    lines = [f"📹 Размеры для каждого качества (без скачивания):\n"]
    for q in ["360", "480", "720", "1080", "best"]:
        s = sizes.get(q)
        if s is None:
            continue
        if q in ("1080", "best") and not premium:
            lines.append(f"▫️ {q}p: ~{s} MiB 🔒 Pro")
        else:
            mark = "✅" if q == current_quality else "▫️"
            fits = "✓ в Telegram" if s <= S3_THRESHOLD_MB else f"⚡ ссылка в облаке ({s} MiB)"
            lines.append(f"{mark} {q}p: ~{s} MiB — {fits}")

    lines.append("\nВыбери качество для скачивания:")

    buttons = []
    for q in ["360", "480", "720", "1080", "best"]:
        s = sizes.get(q)
        if s is None:
            continue
        if q in ("1080", "best") and not premium:
            continue
        label = f"{q}p (~{s} MiB)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"psize_{q}")])

    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="psize_cancel")])

    await msg.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )

    # Сохраняем контекст
    _pending_downloads[msg.message_id] = {
        "user_id": user_id,
        "url": url,
        "chat_id": update.effective_chat.id,
        "preview": True,
    }


async def _handle_size_choice(update, context):
    """Обработка выбора качества из предварительной панели."""
    query = update.callback_query
    await query.answer()
    data = query.data
    msg_id = query.message.message_id
    user_id = update.effective_user.id

    if data == "psize_cancel":
        await query.edit_message_text("❌ Отменено")
        _cleanup_pending(msg_id)
        return

    q = data.replace("psize_", "")
    ctx = _pending_downloads.get(msg_id)
    if not ctx:
        await query.edit_message_text("❌ Устарело, отправь ссылку заново.")
        return

    url = ctx["url"]
    await query.edit_message_text(f"⏳ Скачиваю {q}p...")

    loop = asyncio.get_event_loop()
    async def update_status(text):
        try:
            await query.edit_message_text(text[:200])
        except:
            pass

    result = await loop.run_in_executor(
        None,
        lambda: download_video(url, quality=q, is_premium=is_premium(user_id),
                               progress_callback=lambda t: asyncio.run_coroutine_threadsafe(update_status(t), loop))
    )

    if result["error"]:
        await query.edit_message_text(f"❌ Ошибка: {result['error']}")
        _cleanup_pending(msg_id)
        return

    file_size = result["size_mb"]
    file_path = result["path"]

    try:
        if file_size <= S3_THRESHOLD_MB:
            caption = f"✅ {result['title'][:60]}\n{file_size} MiB ({q}p)"
            with open(file_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=ctx["chat_id"], video=f, caption=caption,
                    supports_streaming=True, read_timeout=180, write_timeout=180,
                )
            increment_daily(user_id)
            await query.delete_message()
        else:
            safe_name = re.sub(r'[^\w\.-]', '_', f"{result['title'][:50]}.mp4")
            await query.edit_message_text("⏳ Загружаю в облако...")
            s3_url = await loop.run_in_executor(None, lambda: upload_file(file_path, safe_name))
            if s3_url:
                await query.edit_message_text(
                    f"✅ {result['title'][:60]}\n{file_size} MiB ({q}p)\n"
                    f"Скачать (24ч):\n{s3_url}"
                )
                increment_daily(user_id)
            else:
                await query.edit_message_text("❌ Не удалось загрузить.")
    finally:
        try:
            os.remove(file_path)
        except:
            pass

    _cleanup_pending(msg_id)


async def _offer_quality_choice(update, context, user_id, msg, result, quality, url):
    """Показать кнопки выбора, если видео >42 MiB и качество >360p."""
    file_size = result["size_mb"]
    file_path = result["path"]
    actual_quality = result.get("quality_used", quality)

    # Сохраняем контекст
    sent = await msg.edit_text(
        f"📹 Видео большое — {file_size} MiB\n"
        f"Сейчас: {actual_quality}p\n\n"
        f"Выбери действие:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 Скачать 360p (прямо в Telegram)", callback_data="dl_360")],
            [InlineKeyboardButton(f"💾 Скачать {actual_quality}p (ссылка в облаке)", callback_data="dl_s3")],
            [InlineKeyboardButton("❌ Отмена", callback_data="dl_cancel")],
        ])
    )

    _pending_downloads[sent.message_id] = {
        "user_id": user_id,
        "url": url,
        "title": result["title"],
        "file_path": file_path,
        "size": file_size,
        "quality": quality,
        "actual_quality": actual_quality,
        "chat_id": update.effective_chat.id,
    }


async def dl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора: перекачать в 360p или залить на S3."""
    query = update.callback_query
    await query.answer()
    data = query.data
    msg_id = query.message.message_id
    user_id = update.effective_user.id

    ctx = _pending_downloads.get(msg_id)
    if not ctx:
        await query.edit_message_text("❌ Ссылка устарела, отправь видео заново.")
        return

    if data == "dl_cancel":
        await query.edit_message_text("❌ Отменено")
        _cleanup_pending(msg_id)
        return

    if data == "dl_360":
        await query.edit_message_text("⏳ Скачиваю в 360p...")
        loop = asyncio.get_event_loop()
        async def update_status(text):
            try:
                await query.edit_message_text(text[:200])
            except:
                pass

        result = await loop.run_in_executor(
            None,
            lambda: download_video(ctx["url"], quality="360", is_premium=is_premium(user_id),
                                   progress_callback=lambda t: asyncio.run_coroutine_threadsafe(update_status(t), loop))
        )

        if result["error"]:
            await query.edit_message_text(f"❌ Ошибка: {result['error']}")
            _cleanup_pending(msg_id)
            return

        new_size = result["size_mb"]
        new_path = result["path"]
        try:
            if new_size <= S3_THRESHOLD_MB:
                caption = f"✅ {result['title'][:60]}\n{new_size} MiB (360p)"
                with open(new_path, "rb") as f:
                    await context.bot.send_video(
                        chat_id=ctx["chat_id"], video=f, caption=caption,
                        supports_streaming=True, read_timeout=180, write_timeout=180,
                    )
                increment_daily(user_id)
                await query.delete_message()
            else:
                # Даже 360p >42 MiB → S3
                safe_name = re.sub(r'[^\w\.-]', '_', f"{result['title'][:50]}.mp4")
                s3_url = await loop.run_in_executor(None, lambda: upload_file(new_path, safe_name))
                if s3_url:
                    await query.edit_message_text(
                        f"✅ {result['title'][:60]}\n{new_size} MiB (360p)\n"
                        f"Скачать (24ч):\n{s3_url}"
                    )
                    increment_daily(user_id)
                else:
                    await query.edit_message_text("❌ Не удалось загрузить.")
        finally:
            try:
                os.remove(new_path)
            except:
                pass
        _cleanup_pending(msg_id)
        return

    if data == "dl_s3":
        await query.edit_message_text("⏳ Загружаю в облако...")
        loop = asyncio.get_event_loop()
        safe_name = re.sub(r'[^\w\.-]', '_', f"{ctx['title'][:50]}.mp4")
        s3_url = await loop.run_in_executor(None, lambda: upload_file(ctx["file_path"], safe_name))
        if s3_url:
            await query.edit_message_text(
                f"✅ {ctx['title'][:60]}\n{ctx['size']} MiB ({ctx['actual_quality']}p)\n"
                f"Скачать (24ч):\n{s3_url}"
            )
            increment_daily(user_id)
        else:
            await query.edit_message_text("❌ Не удалось загрузить в облако.")
        _cleanup_pending(msg_id)
        return


def _cleanup_pending(msg_id: int):
    ctx = _pending_downloads.pop(msg_id, None)
    if ctx:
        try:
            os.remove(ctx["file_path"])
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
    app.add_handler(CommandHandler("quality", quality_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("donate", donate))
    app.add_handler(CallbackQueryHandler(quality_callback, pattern=r"^qlty_"))
    app.add_handler(CallbackQueryHandler(dl_callback, pattern=r"^dl_"))
    app.add_handler(CallbackQueryHandler(_handle_size_choice, pattern=r"^psize_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_url))
    app.add_error_handler(error_handler)

    logger.info("🤖 vid-lab бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
