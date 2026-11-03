import asyncio
import hashlib
import html
import io
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto,
)
from PIL import Image, UnidentifiedImageError

# -------------------- Configuration --------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "0").strip()
OCR_API_KEY = os.getenv("OCR_SPACE_API_KEY", "").strip()
DB_PATH = os.getenv("DB_PATH", "free_stars.sqlite3")

REQUIRED_COUNT = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
CHANNEL_USERNAME = "@oppcountgift"
CHANNEL_URL = "https://t.me/oppcountgift"

REWARDS = {
    "stars": {
        "title": "75 Telegram Stars ⭐",
        "comment": "Кому бесплатно звезды @OppcountGIFTS",
    },
    "cups": {
        "title": "2 подарка-кубка 🏆",
        "comment": "Кому мишка @OppcountGIFTS",
    },
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Render Environment.")
try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID.") from exc
if ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ID is missing or invalid.")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("free-stars")

# -------------------- Database --------------------

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")
db.execute("PRAGMA journal_mode = WAL")
db.execute("""
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    reward TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'collecting',
    created_at TEXT NOT NULL
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    dhash TEXT NOT NULL,
    ocr_text TEXT,
    suspicious INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    FOREIGN KEY(application_id) REFERENCES applications(id)
)
""")
# Safe migration for databases created by older versions.
columns = {row["name"] for row in db.execute("PRAGMA table_info(screenshots)")}
if "review_status" not in columns:
    db.execute(
        "ALTER TABLE screenshots ADD COLUMN review_status TEXT NOT NULL DEFAULT 'unreviewed'"
    )
db.commit()

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# -------------------- Helpers --------------------

def normalize(value: str) -> str:
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^a-zа-яіїєґ0-9@]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def html_safe(value, limit=700) -> str:
    return html.escape(str(value or "")[:limit])


def difference_hash(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("L").resize((9, 8))
        pixels = list(image.getdata())
    bits = []
    for y in range(8):
        for x in range(8):
            bits.append("1" if pixels[y * 9 + x] > pixels[y * 9 + x + 1] else "0")
    return f"{int(''.join(bits), 2):016x}"


def hamming_distance(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def comment_found(expected_text: str, recognized_text: str) -> bool:
    """
    OCR often inserts/removes punctuation and spaces. Compare normalized
    required words, but require the distinctive @username and most of the phrase.
    """
    expected = normalize(expected_text)
    found = normalize(recognized_text)
    if not expected or not found:
        return False
    expected_tokens = expected.split()
    found_tokens = set(found.split())
    if not expected_tokens:
        return False
    # Require the exact target username and at least 75% of expected tokens.
    target_handles = [token for token in expected_tokens if token.startswith("@")]
    if target_handles and not all(handle in found_tokens for handle in target_handles):
        return False
    matched = sum(1 for token in expected_tokens if token in found_tokens)
    return matched >= max(2, int(len(expected_tokens) * 0.75 + 0.5))


async def recognize_text(data: bytes) -> str:
    if not OCR_API_KEY:
        return ""
    timeout = aiohttp.ClientTimeout(total=40, connect=10)
    form = aiohttp.FormData()
    form.add_field("apikey", OCR_API_KEY)
    form.add_field("language", "rus")
    form.add_field("OCREngine", "2")
    form.add_field("isOverlayRequired", "false")
    form.add_field("file", data, filename="screenshot.jpg",
                   content_type="application/octet-stream")
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            "https://api.ocr.space/parse/image", data=form
        ) as response:
            if response.status != 200:
                log.warning("OCR service returned HTTP %s", response.status)
                return ""
            payload = await response.json(content_type=None)
    if payload.get("IsErroredOnProcessing"):
        log.warning("OCR processing error: %s", payload.get("ErrorMessage"))
        return ""
    return "\n".join(
        item.get("ParsedText", "")
        for item in (payload.get("ParsedResults") or [])
    )


def subscription_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Открыть канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")],
    ])


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Получить 75 Stars", callback_data="reward:stars")],
        [InlineKeyboardButton(text="🏆 Получить 2 подарка", callback_data="reward:cups")],
    ])


def admin_keyboard(app_id: int) -> InlineKeyboardMarkup:
    # One button per row, as requested.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Принять заявку", callback_data=f"admin:accept:{app_id}")],
        [InlineKeyboardButton(text="🔴 Отклонить заявку", callback_data=f"admin:reject:{app_id}")],
    ])


def screenshot_review_keyboard(screenshot_id: int) -> InlineKeyboardMarkup:
    # These buttons are attached directly to EACH suspicious screenshot.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🟢 Принять скриншот",
            callback_data=f"shot:accept:{screenshot_id}",
        )],
        [InlineKeyboardButton(
            text="🔴 Отклонить заявку",
            callback_data=f"shot:reject:{screenshot_id}",
        )],
    ])


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        log.exception("Subscription check failed for user %s", user_id)
        return False


def get_application(app_id: int):
    return db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()


async def send_application_to_admin(app_id: int):
    app = get_application(app_id)
    if not app:
        log.error("Application %s disappeared before admin delivery", app_id)
        return

    shots = db.execute(
        "SELECT * FROM screenshots WHERE application_id=? ORDER BY id", (app_id,)
    ).fetchall()
    username = f"@{html_safe(app['username'], 64)}" if app["username"] else "не указан"
    suspicious_count = sum(1 for shot in shots if shot["suspicious"])

    await bot.send_message(
        ADMIN_ID,
        f"╭━━━ ✦ <b>НОВАЯ ЗАЯВКА</b> ✦ ━━━╮\n"
        f"│ Номер: <code>#{app_id}</code>\n"
        f"│ Пользователь: {username}\n"
        f"│ Telegram ID: <code>{app['user_id']}</code>\n"
        f"│ Награда: <b>{html_safe(REWARDS[app['reward']]['title'])}</b>\n"
        f"│ Скриншотов: <b>{len(shots)}/{REQUIRED_COUNT}</b>\n"
        f"│ Подозрительных: <b>{suspicious_count}</b>\n"
        f"╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Проверь материалы и выбери решение по заявке.",
        parse_mode="HTML",
    )

    # Deliver the complete set for review in groups of up to 10.
    for start in range(0, len(shots), 10):
        chunk = shots[start:start + 10]
        media = []
        for index, shot in enumerate(chunk):
            caption = (
                f"Заявка #{app_id} · скриншоты "
                f"{start + 1}–{start + len(chunk)}"
            ) if index == 0 else None
            media.append(InputMediaPhoto(media=shot["file_id"], caption=caption))
        await bot.send_media_group(ADMIN_ID, media)

    await bot.send_message(
        ADMIN_ID,
        f"Решение по заявке #{app_id}:",
        reply_markup=admin_keyboard(app_id),
    )


# -------------------- User flow --------------------

@dp.message(CommandStart())
async def start(message: Message):
    db.execute(
        "UPDATE applications SET status='cancelled' "
        "WHERE user_id=? AND status='collecting'",
        (message.from_user.id,),
    )
    db.commit()

    await message.answer(
        "✦ <b>FREE STARS</b> ✦\n\n"
        "Выбери награду, ознакомься с условиями и отправь заявку. "
        "Результат сообщит администрация после проверки.\n\n"
        "🔐 <b>Первый шаг — подписка на наш канал:</b>",
        reply_markup=subscription_menu(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer(
            "Подписка пока не найдена. Подпишись на канал и попробуй ещё раз.",
            show_alert=True,
        )
        return
    await callback.message.answer(
        "✦ <b>Подписка подтверждена!</b>\n\nВыбери награду:",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )
    await callback.answer("Подписка подтверждена!")


@dp.callback_query(F.data.startswith("reward:"))
async def choose_reward(callback: CallbackQuery):
    reward = callback.data.split(":", 1)[1]
    if reward not in REWARDS:
        await callback.answer("Неизвестная награда.", show_alert=True)
        return
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("Сначала подпишись на канал.", show_alert=True)
        await callback.message.answer(
            "🔐 Для продолжения нужна подписка.",
            reply_markup=subscription_menu(),
        )
        return

    db.execute(
        "UPDATE applications SET status='cancelled' "
        "WHERE user_id=? AND status='collecting'",
        (callback.from_user.id,),
    )
    cursor = db.execute(
        "INSERT INTO applications(user_id, username, reward, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            callback.from_user.id,
            callback.from_user.username,
            reward,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()

    await callback.message.answer(
        f"🎁 <b>Твой выбор: {html_safe(REWARDS[reward]['title'])}</b>\n\n"
        f"Отправь <b>{REQUIRED_COUNT} скриншотов</b> комментариев.\n\n"
        "📝 <b>Текст комментария:</b>\n"
        f"<code>{html_safe(REWARDS[reward]['comment'])}</code>\n\n"
        "Отправляй скриншоты по одному как фотографии. "
        "Если распознавание текста ошибётся, файл будет отправлен "
        "администратору на ручную проверку.\n\n"
        f"Прогресс: <b>0/{REQUIRED_COUNT}</b>",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(F.photo)
async def receive_photo(message: Message):
    row = db.execute(
        "SELECT * FROM applications WHERE user_id=? AND status='collecting' "
        "ORDER BY id DESC LIMIT 1",
        (message.from_user.id,),
    ).fetchone()
    if not row:
        await message.answer("Чтобы начать, нажми /start.")
        return

    count = db.execute(
        "SELECT COUNT(*) FROM screenshots WHERE application_id=?", (row["id"],)
    ).fetchone()[0]
    if count >= REQUIRED_COUNT:
        await message.answer(
            "Все скриншоты уже получены. Дождись решения администрации."
        )
        return

    # Download with size/type checks; Telegram photo file_id is not trusted input.
    photo = message.photo[-1]
    if photo.file_size and photo.file_size > MAX_IMAGE_BYTES:
        await message.answer("Изображение слишком большое. Отправь фото меньшего размера.")
        return

    try:
        telegram_file = await bot.get_file(photo.file_id)
        buffer = io.BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        data = buffer.getvalue()
        if not data or len(data) > MAX_IMAGE_BYTES:
            await message.answer("Файл пустой или слишком большой. Отправь другое фото.")
            return
        dhash = difference_hash(data)
    except (UnidentifiedImageError, OSError, ValueError):
        await message.answer("Не удалось обработать изображение. Попробуй другое фото.")
        return
    except Exception:
        log.exception("Failed to download/process photo from %s", message.from_user.id)
        await message.answer("Не удалось обработать фото. Попробуй отправить ещё раз.")
        return

    sha = hashlib.sha256(data).hexdigest()
    previous = db.execute(
        "SELECT sha256, dhash FROM screenshots WHERE application_id=?",
        (row["id"],),
    ).fetchall()

    reason = None
    if any(item["sha256"] == sha for item in previous):
        reason = "точный дубликат изображения"
    elif any(hamming_distance(item["dhash"], dhash) <= 4 for item in previous):
        reason = "изображение похоже на ранее отправленный скриншот"

    ocr_text = ""
    if not reason:
        try:
            ocr_text = await recognize_text(data)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            log.exception("OCR request failed")
        except Exception:
            log.exception("Unexpected OCR error")

        expected = REWARDS[row["reward"]]["comment"]
        if not OCR_API_KEY:
            reason = "OCR не настроен — требуется ручная проверка"
        elif not normalize(ocr_text):
            reason = "OCR не смог распознать текст — требуется ручная проверка"
        elif not comment_found(expected, ocr_text):
            reason = "OCR не подтвердил текст задания — требуется ручная проверка"

    suspicious = int(reason is not None)
    cursor = db.execute(
        "INSERT INTO screenshots("
        "application_id, file_id, sha256, dhash, ocr_text, suspicious, reason"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"], photo.file_id, sha, dhash,
            ocr_text[:4000], suspicious, reason,
        ),
    )
    screenshot_id = cursor.lastrowid
    db.commit()

    new_count = count + 1
    progress = "🟨" * new_count + "⬜" * (REQUIRED_COUNT - new_count)
    await message.answer(
        f"📸 <b>Скриншот получен</b>\n\n{progress}\n"
        f"<b>{new_count}/{REQUIRED_COUNT}</b>"
        + (
            "\n\n⚠️ Файл отправлен администратору на ручную проверку."
            if suspicious else "\n\n✅ Предварительная проверка пройдена."
        ),
        parse_mode="HTML",
    )

    # CRITICAL FIX: moderation buttons are attached directly to each suspicious
    # image. Callback handlers below work even while the application is collecting.
    if suspicious:
        await bot.send_photo(
            ADMIN_ID,
            photo.file_id,
            caption=(
                f"⚠️ <b>ТРЕБУЕТСЯ ПРОВЕРКА СКРИНШОТА</b>\n"
                f"Заявка: <code>#{row['id']}</code>\n"
                f"Скриншот: <code>#{screenshot_id}</code>\n"
                f"Пользователь ID: <code>{message.from_user.id}</code>\n"
                f"Причина: {html_safe(reason, 250)}\n"
                f"OCR: <code>{html_safe(ocr_text or 'текст не распознан', 650)}</code>",
            ),
            parse_mode="HTML",
            reply_markup=screenshot_review_keyboard(screenshot_id),
        )

    if new_count == REQUIRED_COUNT:
        db.execute(
            "UPDATE applications SET status='pending' WHERE id=? AND status='collecting'",
            (row["id"],),
        )
        db.commit()
        await message.answer(
            "🎉 <b>Все 10 скриншотов отправлены!</b>\n\n"
            "Заявка передана администрации. Мы сообщим результат здесь.",
            parse_mode="HTML",
        )
        await send_application_to_admin(row["id"])


@dp.message()
async def other_message(message: Message):
    row = db.execute(
        "SELECT id FROM applications WHERE user_id=? AND status='collecting' "
        "ORDER BY id DESC LIMIT 1",
        (message.from_user.id,),
    ).fetchone()
    if row:
        await message.answer("Пожалуйста, отправляй скриншоты как фотографии.")
    else:
        await message.answer("Нажми /start, чтобы открыть меню FREE STARS.")


# -------------------- Admin moderation --------------------

@dp.callback_query(F.data.startswith("shot:"))
async def screenshot_decision(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    try:
        _, action, screenshot_id_raw = callback.data.split(":")
        screenshot_id = int(screenshot_id_raw)
    except (ValueError, AttributeError):
        await callback.answer("Некорректная команда.", show_alert=True)
        return

    shot = db.execute(
        "SELECT s.*, a.user_id, a.status AS app_status "
        "FROM screenshots s JOIN applications a ON a.id=s.application_id "
        "WHERE s.id=?",
        (screenshot_id,),
    ).fetchone()
    if not shot:
        await callback.answer("Скриншот не найден.", show_alert=True)
        return
    if shot["review_status"] != "unreviewed":
        await callback.answer("Этот скриншот уже проверен.", show_alert=True)
        return

    if action == "accept":
        db.execute(
            "UPDATE screenshots SET review_status='accepted', suspicious=0 "
            "WHERE id=? AND review_status='unreviewed'",
            (screenshot_id,),
        )
        response = "🟢 Скриншот принят. Остальные материалы можно проверить отдельно."
        await callback.answer("Скриншот принят.")
    elif action == "reject":
        db.execute(
            "UPDATE screenshots SET review_status='rejected' "
            "WHERE id=? AND review_status='unreviewed'",
            (screenshot_id,),
        )
        # Rejecting a flagged screenshot rejects the whole application.
        db.execute(
            "UPDATE applications SET status='rejected' "
            "WHERE id=? AND status IN ('collecting', 'pending')",
            (shot["application_id"],),
        )
        try:
            await bot.send_message(
                shot["user_id"],
                "К сожалению, один из скриншотов не прошёл проверку, "
                "поэтому заявка отклонена.",
            )
        except Exception:
            log.exception("Could not notify user %s about rejection", shot["user_id"])
        response = "🔴 Скриншот отклонён, заявка отклонена."
        await callback.answer("Заявка отклонена.", show_alert=True)
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    db.commit()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(response)
    except Exception:
        log.exception("Could not update screenshot moderation message")


@dp.callback_query(F.data.startswith("admin:"))
async def admin_decision(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    try:
        _, action, app_id_raw = callback.data.split(":")
        app_id = int(app_id_raw)
    except (ValueError, TypeError):
        await callback.answer("Некорректная команда.", show_alert=True)
        return

    app = get_application(app_id)
    if not app or app["status"] != "pending":
        await callback.answer(
            "Заявка ещё не готова или уже обработана.", show_alert=True
        )
        return

    if action == "accept":
        status = "approved"
        user_text = (
            "🎉 <b>Твоя заявка одобрена!</b>\n\n"
            "Администрация подтвердила выполнение условий. Спасибо за участие!"
        )
        admin_text = f"🟢 Заявка #{app_id} одобрена."
    elif action == "reject":
        status = "rejected"
        user_text = (
            "К сожалению, заявка не прошла проверку. "
            "Если считаешь, что произошла ошибка, свяжись с администрацией канала."
        )
        admin_text = f"🔴 Заявка #{app_id} отклонена."
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    db.execute(
        "UPDATE applications SET status=? WHERE id=? AND status='pending'",
        (status, app_id),
    )
    db.commit()
    try:
        await bot.send_message(app["user_id"], user_text, parse_mode="HTML")
    except Exception:
        log.exception("Could not notify user %s", app["user_id"])
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(admin_text)
    except Exception:
        log.exception("Could not update admin decision message")
    await callback.answer("Решение сохранено.")


# -------------------- Startup / shutdown --------------------

async def main():
    log.info("FREE STARS bot started")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
