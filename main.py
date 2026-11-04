"""
FREE STARS bot — aiogram 3 + SQLite.

Env:
  BOT_TOKEN          (required)
  ADMIN_ID           (required, numeric Telegram ID)
  OCR_SPACE_API_KEY  (optional; without it every screenshot goes to manual review)
  DB_PATH            (optional; defaults to /var/data/free_stars.sqlite3 if that dir exists)
  PORT               (optional; if set, a tiny health-check HTTP server is started — for Render Web Service)
  LOG_LEVEL          (optional, default INFO)
"""
import asyncio
import hashlib
import html
import io
import logging
import math
import os
import re
import sqlite3
import threading
from collections import defaultdict
from contextlib import suppress
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, Message,
)
from PIL import Image, UnidentifiedImageError

# -------------------- Logging --------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("free-stars")

# -------------------- Configuration --------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "0").strip()
OCR_API_KEY = os.getenv("OCR_SPACE_API_KEY", "").strip()

REQUIRED_COUNT = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
OCR_MAX_BYTES = 1000 * 1024          # OCR.space free tier limit is 1 MB
OCR_CONCURRENCY = 3
DHASH_SIZE = 16                      # 16x16 -> 256-bit hash (much fewer false positives than 8x8)
DHASH_THRESHOLD = 6                  # max Hamming distance (of 256) to call two screenshots "similar"
CHANNEL_USERNAME = "@oppcountgift"
CHANNEL_URL = "https://t.me/oppcountgift"
OCR_URL = "https://api.ocr.space/parse/image"

REWARDS = {
    "stars": {
        "title": "75 Telegram Stars ⭐",
        "comment": "Кому бесплатно звезды @oppcountgifts_bot",
    },
    "cups": {
        "title": "2 подарка-кубка 🏆",
        "comment": "Кому мишка @oppcountgifts_bot",
    },
}

STATUS_RU = {
    "collecting": "сбор скриншотов",
    "pending": "на проверке у администрации",
    "approved": "одобрена ✅",
    "rejected": "отклонена ❌",
    "cancelled": "отменена",
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Render Environment.")
try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID.") from exc
if ADMIN_ID <= 0:
    raise RuntimeError("ADMIN_ID is missing or invalid.")


def resolve_db_path() -> str:
    """Pick a writable DB path; fall back instead of crashing on a bad mount."""
    candidates = []
    if os.getenv("DB_PATH"):
        candidates.append(os.environ["DB_PATH"].strip())
    if Path("/var/data").is_dir():
        candidates.append("/var/data/free_stars.sqlite3")
    candidates.append("free_stars.sqlite3")

    for index, candidate in enumerate(candidates):
        try:
            path = Path(candidate).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / ".write_probe"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            log.error("DB path %s is not usable: %s", candidate, exc)
            continue
        if index > 0 and os.getenv("DB_PATH"):
            log.warning("DB_PATH was unusable, falling back to %s", path)
        if not str(path).startswith("/var/data") and os.getenv("RENDER"):
            log.warning(
                "SQLite lives on an ephemeral filesystem. Attach a Render disk and set "
                "DB_PATH=/var/data/free_stars.sqlite3 or data will be lost on redeploy."
            )
        return str(path)
    raise RuntimeError("No writable location for the SQLite database.")


DB_PATH = resolve_db_path()

# -------------------- Database --------------------


class Database:
    """Thread-safe SQLite wrapper; blocking work runs in a worker thread so the event loop never stalls."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:
            log.warning("WAL mode is not supported here, using the default journal")
        self._init_schema()

    def _add_column(self, table: str, column: str, ddl: str):
        cols = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _init_schema(self):
        c = self._conn
        # Tables FIRST, indexes only after the tables exist
        # (the old version created an index on `screenshots` before the table -> crash on a fresh DB).
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                reward TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'collecting',
                created_at TEXT NOT NULL
            )""")
        c.execute("""
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
            )""")
        # Migrations for databases created by older versions.
        self._add_column("screenshots", "review_status", "TEXT NOT NULL DEFAULT 'unreviewed'")
        self._add_column("screenshots", "file_unique_id", "TEXT")
        self._add_column("screenshots", "created_at", "TEXT")
        self._add_column("applications", "updated_at", "TEXT")

        c.execute("CREATE INDEX IF NOT EXISTS idx_apps_user_status ON applications(user_id, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shots_application ON screenshots(application_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shots_sha ON screenshots(sha256)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_shots_unique ON screenshots(file_unique_id)")
        c.commit()

    def _run(self, func):
        with self._lock:
            try:
                result = func(self._conn)
                self._conn.commit()
                return result
            except BaseException:
                self._conn.rollback()
                raise

    async def tx(self, func):
        """Run func(connection) atomically in a worker thread."""
        return await asyncio.to_thread(self._run, func)

    async def one(self, sql, params=()):
        return await self.tx(lambda c: c.execute(sql, params).fetchone())

    async def all(self, sql, params=()):
        return await self.tx(lambda c: c.execute(sql, params).fetchall())

    async def scalar(self, sql, params=()):
        row = await self.one(sql, params)
        return row[0] if row else None

    async def run(self, sql, params=()):
        def work(c):
            cur = c.execute(sql, params)
            return cur.lastrowid, cur.rowcount
        return await self.tx(work)

    def close(self):
        with suppress(Exception):
            self._conn.close()


db = Database(DB_PATH)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
dp.message.filter(F.chat.type == "private")   # ignore groups/channels completely

http_session = None                      # created in main()
ocr_semaphore = asyncio.Semaphore(OCR_CONCURRENCY)
user_locks = defaultdict(asyncio.Lock)   # serialises one user's actions -> no counter races


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -------------------- Helpers --------------------

def normalize(value: str) -> str:
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^a-zа-яіїєґ0-9@]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def html_safe(value, limit=700) -> str:
    return html.escape(str(value or "")[:limit])


def progress_bar(done: int) -> str:
    done = max(0, min(done, REQUIRED_COUNT))
    return "🟨" * done + "⬜" * (REQUIRED_COUNT - done)


def difference_hash(data: bytes) -> str:
    resample = getattr(Image, "Resampling", Image).LANCZOS
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("L").resize((DHASH_SIZE + 1, DHASH_SIZE), resample)
        pixels = list(image.getdata())
    width = DHASH_SIZE + 1
    bits = "".join(
        "1" if pixels[y * width + x] > pixels[y * width + x + 1] else "0"
        for y in range(DHASH_SIZE) for x in range(DHASH_SIZE)
    )
    return f"{int(bits, 2):0{DHASH_SIZE * DHASH_SIZE // 4}x}"


def hamming_distance(a: str, b: str) -> int:
    if len(a) != len(b):          # hashes from the old 8x8 version are not comparable
        return 10 ** 6
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def prepare_for_ocr(data: bytes) -> bytes:
    """Shrink to a JPEG under the OCR.space size limit."""
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGB")
    image.thumbnail((2000, 2000))
    for quality in (85, 75, 65, 50):
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
        if buffer.tell() <= OCR_MAX_BYTES:
            return buffer.getvalue()
    image.thumbnail((1400, 1400))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=55, optimize=True)
    return buffer.getvalue()


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def comment_found(expected_text: str, recognized_text: str) -> bool:
    """
    OCR often garbles letters, punctuation and spaces. Match tokens fuzzily, but
    always require the target @username and at least 75% of the phrase.
    """
    expected = normalize(expected_text).split()
    found_text = normalize(recognized_text)
    found = found_text.split()
    if not expected or not found:
        return False
    compact = found_text.replace(" ", "")

    def token_ok(token: str, threshold: float) -> bool:
        bare = token.lstrip("@")
        if not bare:
            return False
        for candidate in found:
            cand = candidate.lstrip("@")
            if cand == bare:
                return True
            if len(bare) > 3 and _similar(bare, cand) >= threshold:
                return True
        return False

    handles = [t for t in expected if t.startswith("@")]
    for handle in handles:
        if not (token_ok(handle, 0.85) or handle.lstrip("@") in compact):
            return False
    matched = sum(1 for token in expected if token_ok(token, 0.8))
    return matched >= max(2, math.ceil(len(expected) * 0.75))


async def _ocr_request(data: bytes, engine: str) -> str:
    form = aiohttp.FormData()
    form.add_field("apikey", OCR_API_KEY)
    form.add_field("language", "rus")
    form.add_field("OCREngine", engine)
    form.add_field("scale", "true")
    form.add_field("isOverlayRequired", "false")
    form.add_field("file", data, filename="screenshot.jpg", content_type="image/jpeg")
    async with http_session.post(OCR_URL, data=form) as response:
        if response.status != 200:
            log.warning("OCR (engine %s) returned HTTP %s", engine, response.status)
            return ""
        payload = await response.json(content_type=None)
    if payload.get("IsErroredOnProcessing"):
        log.warning("OCR (engine %s) error: %s", engine, payload.get("ErrorMessage"))
        return ""
    return "\n".join(item.get("ParsedText", "") for item in (payload.get("ParsedResults") or []))


async def recognize_text(data: bytes) -> str:
    if not OCR_API_KEY or http_session is None:
        return ""
    prepared = await asyncio.to_thread(prepare_for_ocr, data)
    async with ocr_semaphore:
        # Engine 2 is better, engine 1 is the safety net (wider language support).
        for engine in ("2", "1"):
            try:
                text = await _ocr_request(prepared, engine)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                log.exception("OCR request failed (engine %s)", engine)
                continue
            if normalize(text):
                return text
    return ""


# -------------------- Keyboards --------------------

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
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Принять заявку", callback_data=f"admin:accept:{app_id}")],
        [InlineKeyboardButton(text="🔴 Отклонить заявку", callback_data=f"admin:reject:{app_id}")],
    ])


def screenshot_review_keyboard(screenshot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Принять скриншот", callback_data=f"shot:accept:{screenshot_id}")],
        [InlineKeyboardButton(text="🔴 Отклонить заявку", callback_data=f"shot:reject:{screenshot_id}")],
    ])


def cancel_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, отменить заявку", callback_data="cancel:yes")],
        [InlineKeyboardButton(text="↩️ Нет, продолжить", callback_data="cancel:no")],
    ])


# -------------------- Telegram / DB utilities --------------------

async def safe_send(chat_id: int, text: str, **kwargs):
    """send_message that never raises (blocked bot, flood limits, etc.)."""
    for attempt in range(2):
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramForbiddenError:
            log.info("User %s blocked the bot or never started it", chat_id)
            return None
        except Exception:
            log.exception("Could not send message to %s", chat_id)
            return None
    return None


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
    except Exception:
        log.exception("Subscription check failed for user %s "
                      "(is the bot an admin of %s?)", user_id, CHANNEL_USERNAME)
        return False
    if member.status in ("member", "administrator", "creator"):
        return True
    return member.status == "restricted" and bool(getattr(member, "is_member", False))


async def touch_user(user) -> None:
    now = now_iso()
    await db.run(
        """INSERT INTO users(user_id, username, first_name, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username,
             first_name=excluded.first_name,
             last_seen_at=excluded.last_seen_at""",
        (user.id, user.username, user.first_name, now, now),
    )


async def get_active(user_id: int):
    return await db.one(
        "SELECT * FROM applications WHERE user_id=? AND status='collecting' "
        "ORDER BY id DESC LIMIT 1", (user_id,))


async def get_pending(user_id: int):
    return await db.one(
        "SELECT * FROM applications WHERE user_id=? AND status='pending' "
        "ORDER BY id DESC LIMIT 1", (user_id,))


async def get_application(app_id: int):
    return await db.one("SELECT * FROM applications WHERE id=?", (app_id,))


async def count_shots(app_id: int) -> int:
    return await db.scalar("SELECT COUNT(*) FROM screenshots WHERE application_id=?", (app_id,))


async def close_markup(callback: CallbackQuery):
    if callback.message:
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)


async def send_application_to_admin(app_id: int) -> bool:
    app = await get_application(app_id)
    if not app:
        log.error("Application %s disappeared before admin delivery", app_id)
        return False

    shots = await db.all("SELECT * FROM screenshots WHERE application_id=? ORDER BY id", (app_id,))
    username = f"@{html_safe(app['username'], 64)}" if app["username"] else "не указан"
    suspicious_count = sum(1 for s in shots if s["suspicious"])
    unreviewed = sum(1 for s in shots if s["suspicious"] and s["review_status"] == "unreviewed")

    try:
        await bot.send_message(
            ADMIN_ID,
            f"╭━━━ ✦ <b>НОВАЯ ЗАЯВКА</b> ✦ ━━━╮\n"
            f"│ Номер: <code>#{app_id}</code>\n"
            f"│ Пользователь: {username}\n"
            f"│ Telegram ID: <code>{app['user_id']}</code>\n"
            f"│ Награда: <b>{html_safe(REWARDS[app['reward']]['title'])}</b>\n"
            f"│ Скриншотов: <b>{len(shots)}/{REQUIRED_COUNT}</b>\n"
            f"│ Подозрительных: <b>{suspicious_count}</b> (не проверено: <b>{unreviewed}</b>)\n"
            f"╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "Проверь материалы и выбери решение по заявке.",
            parse_mode="HTML",
        )
        # Albums accept 2..10 items, so chunk accordingly and never send a 1-item album.
        for start in range(0, len(shots), 10):
            chunk = shots[start:start + 10]
            if len(chunk) == 1:
                await bot.send_photo(ADMIN_ID, chunk[0]["file_id"],
                                     caption=f"Заявка #{app_id} · скрин #{chunk[0]['id']}")
                continue
            media = []
            for index, shot in enumerate(chunk, start=start + 1):
                flag = " ⚠️" if shot["suspicious"] and shot["review_status"] == "unreviewed" else ""
                media.append(InputMediaPhoto(
                    media=shot["file_id"],
                    caption=f"Заявка #{app_id} · {index}/{len(shots)} · скрин #{shot['id']}{flag}",
                ))
            await bot.send_media_group(ADMIN_ID, media)
        await bot.send_message(
            ADMIN_ID, f"Решение по заявке #{app_id}:", reply_markup=admin_keyboard(app_id))
        return True
    except Exception:
        log.exception("Could not deliver application %s to the admin "
                      "(admin must press /start in the bot). Use /resend %s", app_id, app_id)
        return False


# -------------------- User flow --------------------

@dp.message(CommandStart())
async def start(message: Message):
    await touch_user(message.from_user)
    active = await get_active(message.from_user.id)
    if active:
        count = await count_shots(active["id"])
        await message.answer(
            f"👋 У тебя уже есть активная заявка №<code>{active['id']}</code>.\n"
            f"Награда: <b>{html_safe(REWARDS[active['reward']]['title'])}</b>\n"
            f"Прогресс: <b>{count}/{REQUIRED_COUNT}</b>\n{progress_bar(count)}\n\n"
            "Продолжай отправлять скриншоты фотографиями. Прогресс сохранён.\n"
            "Отменить заявку: /cancel",
            parse_mode="HTML",
        )
        return
    pending = await get_pending(message.from_user.id)
    if pending:
        await message.answer(
            f"⏳ Твоя заявка №<code>{pending['id']}</code> уже на проверке. "
            "Результат придёт сюда.",
            parse_mode="HTML",
        )
        return
    await message.answer(
        "✦ <b>FREE STARS</b> ✦\n\n"
        "Выбери награду, ознакомься с условиями и отправь заявку. "
        "Результат сообщит администрация после проверки.\n\n"
        "🔐 <b>Первый шаг — подписка на наш канал:</b>",
        reply_markup=subscription_menu(),
        parse_mode="HTML",
    )


@dp.message(Command("status"))
async def status_cmd(message: Message):
    last = await db.one(
        "SELECT * FROM applications WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (message.from_user.id,))
    if not last:
        await message.answer("У тебя пока нет заявок. Нажми /start.")
        return
    count = await count_shots(last["id"])
    await message.answer(
        f"📋 Заявка №<code>{last['id']}</code>\n"
        f"Награда: <b>{html_safe(REWARDS[last['reward']]['title'])}</b>\n"
        f"Статус: <b>{STATUS_RU.get(last['status'], last['status'])}</b>\n"
        f"Скриншоты: <b>{count}/{REQUIRED_COUNT}</b>\n{progress_bar(count)}",
        parse_mode="HTML",
    )


@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    active = await get_active(message.from_user.id)
    if not active:
        await message.answer("Активной заявки нет — отменять нечего.")
        return
    await message.answer(
        f"Отменить заявку №{active['id']}? Отправленные скриншоты будут потеряны.",
        reply_markup=cancel_confirm_keyboard(),
    )


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel_decision(callback: CallbackQuery):
    answer = callback.data.split(":", 1)[1]
    await close_markup(callback)
    if answer != "yes":
        await callback.answer("Продолжаем!")
        return
    async with user_locks[callback.from_user.id]:
        _, changed = await db.run(
            "UPDATE applications SET status='cancelled', updated_at=? "
            "WHERE user_id=? AND status='collecting'",
            (now_iso(), callback.from_user.id),
        )
    await callback.answer("Заявка отменена." if changed else "Активной заявки нет.")
    if changed:
        await safe_send(callback.from_user.id, "🗑 Заявка отменена. Чтобы начать заново, нажми /start.")


@dp.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer(
            "Подписка пока не найдена. Подпишись на канал и попробуй ещё раз.",
            show_alert=True,
        )
        return
    await safe_send(
        callback.from_user.id,
        "✦ <b>Подписка подтверждена!</b>\n\nВыбери награду:",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )
    await callback.answer("Подписка подтверждена!")


@dp.callback_query(F.data.startswith("reward:"))
async def choose_reward(callback: CallbackQuery):
    reward = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    if reward not in REWARDS:
        await callback.answer("Неизвестная награда.", show_alert=True)
        return
    if not await is_subscribed(user_id):
        await callback.answer("Сначала подпишись на канал.", show_alert=True)
        await safe_send(user_id, "🔐 Для продолжения нужна подписка.",
                        reply_markup=subscription_menu())
        return

    async with user_locks[user_id]:          # double-tap safe: no duplicate applications
        await touch_user(callback.from_user)
        active = await get_active(user_id)
        if active:
            if active["reward"] != reward:
                await callback.answer(
                    "У тебя уже есть активная заявка с другой наградой. "
                    "Заверши её или отмени через /cancel.",
                    show_alert=True,
                )
                return
            app_id = active["id"]
            count = await count_shots(app_id)
        else:
            if await get_pending(user_id):
                await callback.answer(
                    "Твоя предыдущая заявка ещё на проверке. Дождись решения.",
                    show_alert=True,
                )
                return
            app_id, _ = await db.run(
                "INSERT INTO applications(user_id, username, reward, created_at) VALUES (?, ?, ?, ?)",
                (user_id, callback.from_user.username, reward, now_iso()),
            )
            count = 0

    await safe_send(
        user_id,
        f"🎁 <b>Твой выбор: {html_safe(REWARDS[reward]['title'])}</b>\n\n"
        f"Отправь <b>{REQUIRED_COUNT} скриншотов</b> комментариев.\n\n"
        "📝 <b>Текст комментария:</b>\n"
        f"<code>{html_safe(REWARDS[reward]['comment'])}</code>\n\n"
        "Отправляй скриншоты по одному как фотографии. "
        "Если распознавание текста ошибётся, файл будет отправлен "
        "администратору на ручную проверку.\n\n"
        f"Прогресс: <b>{count}/{REQUIRED_COUNT}</b>\n{progress_bar(count)}",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(F.photo)
async def receive_photo(message: Message):
    # Albums / rapid sends are processed strictly one by one per user.
    async with user_locks[message.from_user.id]:
        await handle_photo(message)


async def handle_photo(message: Message):
    user_id = message.from_user.id
    row = await get_active(user_id)
    if not row:
        if await get_pending(user_id):
            await message.answer("⏳ Твоя заявка уже на проверке. Дождись решения администрации.")
        else:
            await message.answer("Чтобы начать, нажми /start.")
        return

    count = await count_shots(row["id"])
    if count >= REQUIRED_COUNT:
        await message.answer("Все скриншоты уже получены. Дождись решения администрации.")
        return

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
        dhash = await asyncio.to_thread(difference_hash, data)
    except (UnidentifiedImageError, OSError, ValueError):
        await message.answer("Не удалось обработать изображение. Попробуй другое фото.")
        return
    except Exception:
        log.exception("Failed to download/process photo from %s", user_id)
        await message.answer("Не удалось обработать фото. Попробуй отправить ещё раз.")
        return

    sha = hashlib.sha256(data).hexdigest()
    unique_id = photo.file_unique_id
    previous = await db.all(
        "SELECT sha256, dhash, file_unique_id FROM screenshots WHERE application_id=?",
        (row["id"],))

    # Exact copy inside this application: not counted, user is asked for a different one.
    if any(p["sha256"] == sha or (unique_id and p["file_unique_id"] == unique_id) for p in previous):
        await message.answer(
            "🔁 Этот скриншот ты уже отправлял в этой заявке. Пришли другой.\n"
            f"Прогресс: <b>{count}/{REQUIRED_COUNT}</b>",
            parse_mode="HTML",
        )
        return

    reason = None
    reused = await db.one(
        "SELECT a.user_id FROM screenshots s JOIN applications a ON a.id = s.application_id "
        "WHERE (s.sha256=? OR s.file_unique_id=?) AND s.application_id != ? LIMIT 1",
        (sha, unique_id or "", row["id"]),
    )
    if reused:
        reason = ("этот скриншот уже отправлял другой пользователь"
                  if reused["user_id"] != user_id
                  else "этот скриншот уже был в одной из прошлых заявок")
    elif any(hamming_distance(p["dhash"], dhash) <= DHASH_THRESHOLD for p in previous):
        reason = "изображение похоже на ранее отправленный скриншот"

    ocr_text = ""
    if not reason:
        try:
            ocr_text = await recognize_text(data)
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
    screenshot_id, _ = await db.run(
        "INSERT INTO screenshots(application_id, file_id, file_unique_id, sha256, dhash, "
        "ocr_text, suspicious, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], photo.file_id, unique_id, sha, dhash, ocr_text[:4000],
         suspicious, reason, now_iso()),
    )

    new_count = count + 1
    await message.answer(
        f"📸 <b>Скриншот получен</b>\n\n{progress_bar(new_count)}\n"
        f"<b>{new_count}/{REQUIRED_COUNT}</b>"
        + ("\n\n⚠️ Файл отправлен администратору на ручную проверку."
           if suspicious else "\n\n✅ Предварительная проверка пройдена."),
        parse_mode="HTML",
    )

    if suspicious:
        try:
            await bot.send_photo(
                ADMIN_ID,
                photo.file_id,
                caption=(
                    f"⚠️ <b>ТРЕБУЕТСЯ ПРОВЕРКА СКРИНШОТА</b>\n"
                    f"Заявка: <code>#{row['id']}</code>\n"
                    f"Скриншот: <code>#{screenshot_id}</code>\n"
                    f"Пользователь ID: <code>{user_id}</code>\n"
                    f"Причина: {html_safe(reason, 250)}\n"
                    f"OCR: <code>{html_safe(ocr_text or 'текст не распознан', 650)}</code>"
                ),
                parse_mode="HTML",
                reply_markup=screenshot_review_keyboard(screenshot_id),
            )
        except Exception:
            log.exception("Could not send flagged screenshot %s to the admin", screenshot_id)

    if new_count >= REQUIRED_COUNT:
        _, changed = await db.run(
            "UPDATE applications SET status='pending', updated_at=? "
            "WHERE id=? AND status='collecting'",
            (now_iso(), row["id"]),
        )
        if changed:   # the admin may have rejected a flagged shot a moment ago
            await message.answer(
                f"🎉 <b>Все {REQUIRED_COUNT} скриншотов отправлены!</b>\n\n"
                "Заявка передана администрации. Мы сообщим результат здесь.",
                parse_mode="HTML",
            )
            await send_application_to_admin(row["id"])


# -------------------- Admin: moderation --------------------

@dp.callback_query(F.data.startswith("shot:"))
async def screenshot_decision(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        _, action, raw_id = callback.data.split(":")
        screenshot_id = int(raw_id)
    except (ValueError, AttributeError):
        await callback.answer("Некорректная команда.", show_alert=True)
        return
    if action not in ("accept", "reject"):
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    def work(conn):
        shot = conn.execute(
            "SELECT s.*, a.user_id, a.status AS app_status FROM screenshots s "
            "JOIN applications a ON a.id=s.application_id WHERE s.id=?",
            (screenshot_id,),
        ).fetchone()
        if not shot:
            return "missing", None
        if shot["review_status"] != "unreviewed":
            return "reviewed", shot
        if shot["app_status"] not in ("collecting", "pending"):
            return "closed", shot
        if action == "accept":
            conn.execute(
                "UPDATE screenshots SET review_status='accepted', suspicious=0 WHERE id=?",
                (screenshot_id,))
        else:
            conn.execute(
                "UPDATE screenshots SET review_status='rejected' WHERE id=?", (screenshot_id,))
            conn.execute(
                "UPDATE applications SET status='rejected', updated_at=? WHERE id=?",
                (now_iso(), shot["application_id"]))
        return "ok", shot

    state, shot = await db.tx(work)
    if state == "missing":
        await callback.answer("Скриншот не найден.", show_alert=True)
        return
    if state == "reviewed":
        await callback.answer("Этот скриншот уже проверен.", show_alert=True)
        return
    if state == "closed":
        await callback.answer("Заявка уже закрыта.", show_alert=True)
        await close_markup(callback)
        return

    if action == "accept":
        await safe_send(shot["user_id"], f"🟢 Твой скриншот №{screenshot_id} подтверждён администрацией.")
        await callback.answer("Скриншот принят.")
        response = "🟢 Скриншот принят. Остальные материалы можно проверить отдельно."
    else:
        await safe_send(
            shot["user_id"],
            f"🔴 Твой скриншот №{screenshot_id} отклонён администрацией. "
            "Заявка отклонена. Если считаешь, что произошла ошибка, "
            "свяжись с администрацией канала.",
        )
        await callback.answer("Заявка отклонена.", show_alert=True)
        response = "🔴 Скриншот отклонён, заявка отклонена."

    await close_markup(callback)
    await safe_send(ADMIN_ID, response)


@dp.callback_query(F.data.startswith("admin:"))
async def admin_decision(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        _, action, raw_id = callback.data.split(":")
        app_id = int(raw_id)
    except (ValueError, TypeError):
        await callback.answer("Некорректная команда.", show_alert=True)
        return

    if action == "accept":
        status = "approved"
        user_text = ("🎉 <b>Твоя заявка одобрена!</b>\n\n"
                     "Администрация подтвердила выполнение условий. Спасибо за участие!")
        admin_text = f"🟢 Заявка #{app_id} одобрена."
    elif action == "reject":
        status = "rejected"
        user_text = ("К сожалению, заявка не прошла проверку. "
                     "Если считаешь, что произошла ошибка, свяжись с администрацией канала.")
        admin_text = f"🔴 Заявка #{app_id} отклонена."
    else:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    app = await get_application(app_id)
    _, changed = await db.run(      # atomic: a double click can't process an application twice
        "UPDATE applications SET status=?, updated_at=? WHERE id=? AND status='pending'",
        (status, now_iso(), app_id),
    )
    if not app or not changed:
        await callback.answer("Заявка ещё не готова или уже обработана.", show_alert=True)
        await close_markup(callback)
        return

    await safe_send(app["user_id"], user_text, parse_mode="HTML")
    await close_markup(callback)
    await safe_send(ADMIN_ID, admin_text)
    await callback.answer("Решение сохранено.")


# -------------------- Admin: commands --------------------

@dp.message(Command("stats"), F.from_user.id == ADMIN_ID)
async def admin_stats(message: Message):
    users = await db.scalar("SELECT COUNT(*) FROM users")
    by_status = await db.all("SELECT status, COUNT(*) AS c FROM applications GROUP BY status")
    shots = await db.scalar("SELECT COUNT(*) FROM screenshots")
    flagged = await db.scalar(
        "SELECT COUNT(*) FROM screenshots WHERE suspicious=1 AND review_status='unreviewed'")
    lines = "\n".join(
        f"• {STATUS_RU.get(r['status'], r['status'])}: <b>{r['c']}</b>" for r in by_status
    ) or "• заявок нет"
    await message.answer(
        "📊 <b>Статистика</b>\n"
        f"Пользователей: <b>{users}</b>\n{lines}\n"
        f"Скриншотов: <b>{shots}</b> (ждут проверки: <b>{flagged}</b>)",
        parse_mode="HTML",
    )


@dp.message(Command("pending"), F.from_user.id == ADMIN_ID)
async def admin_pending(message: Message):
    rows = await db.all(
        "SELECT a.id, a.user_id, a.username, a.reward, "
        "(SELECT COUNT(*) FROM screenshots s WHERE s.application_id=a.id) AS n "
        "FROM applications a WHERE a.status='pending' ORDER BY a.id LIMIT 30")
    if not rows:
        await message.answer("Заявок на проверке нет.")
        return
    lines = [
        f"#{r['id']} · {'@' + html_safe(r['username'], 64) if r['username'] else r['user_id']} · "
        f"{html_safe(REWARDS[r['reward']]['title'])} · {r['n']}/{REQUIRED_COUNT}"
        for r in rows
    ]
    await message.answer(
        "⏳ <b>На проверке:</b>\n" + "\n".join(lines) + "\n\nПовторно получить заявку: /resend &lt;номер&gt;",
        parse_mode="HTML",
    )


@dp.message(Command("resend"), F.from_user.id == ADMIN_ID)
async def admin_resend(message: Message, command: CommandObject):
    try:
        app_id = int((command.args or "").strip().lstrip("#"))
    except ValueError:
        await message.answer("Использование: /resend <номер заявки>")
        return
    app = await get_application(app_id)
    if not app or app["status"] != "pending":
        await message.answer("Такой заявки на проверке нет.")
        return
    await send_application_to_admin(app_id)


# -------------------- Fallback + errors --------------------

@dp.message()
async def other_message(message: Message):
    if await get_active(message.from_user.id):
        await message.answer("Пожалуйста, отправляй скриншоты как фотографии (не файлом).")
    elif await get_pending(message.from_user.id):
        await message.answer("⏳ Твоя заявка на проверке. Статус: /status")
    else:
        await message.answer("Нажми /start, чтобы открыть меню FREE STARS.")


@dp.error()
async def on_error(event):
    log.error("Unhandled error while processing update", exc_info=event.exception)
    return True


# -------------------- Startup / shutdown --------------------

async def start_health_server():
    """Render Web Services require an open port; harmless elsewhere."""
    port = os.getenv("PORT")
    if not port:
        return None

    async def ok(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Health server listening on :%s", port)
    return runner


async def preflight():
    me = await bot.get_me()
    log.info("Logged in as @%s", me.username)
    try:
        await bot.send_chat_action(ADMIN_ID, "typing")
    except Exception:
        log.warning("Admin %s is unreachable — open the bot and press /start from the admin account", ADMIN_ID)
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, me.id)
        if member.status not in ("administrator", "creator"):
            log.warning("Bot is not an admin of %s — subscription checks may fail", CHANNEL_USERNAME)
    except Exception:
        log.warning("Bot cannot see %s — add it to the channel as an administrator", CHANNEL_USERNAME)


async def main():
    global http_session
    http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40, connect=10))
    runner = await start_health_server()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await preflight()
        log.info("FREE STARS bot started (db: %s)", DB_PATH)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if runner:
            await runner.cleanup()
        await http_session.close()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
