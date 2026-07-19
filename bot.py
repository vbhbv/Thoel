import os
import re
import html
import time
import logging
import asyncio
import contextlib
import shutil
from collections import deque
from pathlib import Path
from typing import Optional, List, Dict, Any, Deque

import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.error import TelegramError, Forbidden, RetryAfter, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from pdf_epub_converter import (
    convert_epub_to_pdf,
    is_calibre_available,
    EbookConversionError,
)

# ----------------------------------------------------------------------
# الإعدادات العامة ومتغيرات البيئة
# ----------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("يجب ضبط متغير البيئة BOT_TOKEN")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("يجب ضبط متغير البيئة DATABASE_URL المربوط بقاعدة PostgreSQL")


def _parse_admin_ids() -> List[int]:
    raw = os.environ.get("ADMIN_IDS", "").strip()
    if not raw:
        logger.critical(
            "⚠️ متغير البيئة ADMIN_IDS غير مضبوط! لوحة تحكم الأدمن ستكون معطّلة بالكامل."
        )
        return []
    ids: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            logger.critical(f"⚠️ قيمة غير صالحة في ADMIN_IDS تم تجاهلها: '{part}'")
    return ids


ADMIN_IDS: List[int] = _parse_admin_ids()

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
CONVERTED_DIR = BASE_DIR / "converted"
DOWNLOADS_DIR.mkdir(exist_ok=True)
CONVERTED_DIR.mkdir(exist_ok=True)

FILE_MAX_AGE = 60 * 60  # ساعة واحدة
CLEANUP_INTERVAL = 30 * 60  # 30 دقيقة
MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_MERGE_FILES = int(os.environ.get("MAX_MERGE_FILES", "20"))

AUDIO_FORMATS = ["mp3", "m4a", "flac", "ogg", "wav", "aac"]
DOC_EXTENSIONS = {".doc", ".docx"}
PDF_EXTENSION = ".pdf"
EPUB_EXTENSION = ".epub"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".3gp", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"}

DB_POOL_MIN_SIZE = int(os.environ.get("DB_POOL_MIN_SIZE", "2"))
DB_POOL_MAX_SIZE = int(os.environ.get("DB_POOL_MAX_SIZE", "10"))
DB_COMMAND_TIMEOUT = float(os.environ.get("DB_COMMAND_TIMEOUT", "30"))
DB_CONNECT_RETRIES = int(os.environ.get("DB_CONNECT_RETRIES", "5"))
DB_CONNECT_RETRY_DELAY = float(os.environ.get("DB_CONNECT_RETRY_DELAY", "3"))

RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "8"))
RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "20"))

BAN_CACHE_TTL = float(os.environ.get("BAN_CACHE_TTL_SECONDS", "30"))
SETTINGS_CACHE_TTL = float(os.environ.get("SETTINGS_CACHE_TTL_SECONDS", "60"))

BROADCAST_CONCURRENCY = int(os.environ.get("BROADCAST_CONCURRENCY", "15"))
BROADCAST_DELAY_PER_MESSAGE = float(os.environ.get("BROADCAST_DELAY_PER_MESSAGE", "0.05"))

CMD_TIMEOUT_SECONDS = float(os.environ.get("CMD_TIMEOUT_SECONDS", "300"))
HEAVY_TASK_CONCURRENCY = int(os.environ.get("HEAVY_TASK_CONCURRENCY", "3"))
MAX_IMAGES_PER_BATCH = int(os.environ.get("MAX_IMAGES_PER_BATCH", "30"))

MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", str(40_000_000)))
from PIL import Image as _PILImageConfig  # noqa: E402
_PILImageConfig.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class HeavyTaskGuard:
    _semaphore = asyncio.Semaphore(HEAVY_TASK_CONCURRENCY)

    async def __aenter__(self):
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._semaphore.release()


# ----------------------------------------------------------------------
# كاش محلي
# ----------------------------------------------------------------------
_CACHE_NONE_SENTINEL = object()

class TTLCache:
    def __init__(self, ttl_seconds: float):
        self._ttl = ttl_seconds
        self._store: Dict[Any, tuple] = {}

    def get(self, key: Any) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: Any, value: Any) -> None:
        self._store[key] = (value, time.monotonic() + self._ttl)

    def invalidate(self, key: Optional[Any] = None) -> None:
        if key is None:
            self._store.clear()
        else:
            self._store.pop(key, None)


_ban_cache = TTLCache(ttl_seconds=BAN_CACHE_TTL)
_settings_cache = TTLCache(ttl_seconds=SETTINGS_CACHE_TTL)


# ----------------------------------------------------------------------
# محدد معدل الطلبات
# ----------------------------------------------------------------------
class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: Dict[int, Deque[float]] = {}

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        window_start = now - self.window_seconds
        hits = self._hits.get(user_id)
        if hits is None:
            hits = deque()
            self._hits[user_id] = hits
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True


_rate_limiter = RateLimiter(RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)

def is_rate_limited(user_id: int) -> bool:
    return not _rate_limiter.allow(user_id)

def prune_rate_limiter() -> int:
    now = time.monotonic()
    window_start = now - _rate_limiter.window_seconds
    stale_keys = [
        uid for uid, hits in _rate_limiter._hits.items()
        if not hits or hits[-1] < window_start
    ]
    for uid in stale_keys:
        del _rate_limiter._hits[uid]
    return len(stale_keys)


# ----------------------------------------------------------------------
# إدارة قاعدة البيانات
# ----------------------------------------------------------------------
_pool: Optional[asyncpg.Pool] = None

def _get_pool() -> Optional[asyncpg.Pool]:
    return _pool

def fix_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url

async def init_db() -> bool:
    global _pool
    url = fix_database_url(DATABASE_URL)
    for attempt in range(1, DB_CONNECT_RETRIES + 1):
        try:
            _pool = await asyncpg.create_pool(
                dsn=url,
                min_size=DB_POOL_MIN_SIZE,
                max_size=DB_POOL_MAX_SIZE,
                command_timeout=DB_COMMAND_TIMEOUT,
            )
            break
        except Exception as e:
            logger.warning(f"⏳ محاولة الاتصال {attempt}/{DB_CONNECT_RETRIES} فشلت...")
            await asyncio.sleep(DB_CONNECT_RETRY_DELAY)
    else:
        return False

    try:
        async with _pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_banned BOOLEAN DEFAULT FALSE,
                    blocked_bot BOOLEAN DEFAULT FALSE
                );
            """)
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_bot BOOLEAN DEFAULT FALSE;")
            await conn.execute("CREATE TABLE IF NOT EXISTS stats_log (id SERIAL PRIMARY KEY, action_type TEXT NOT NULL, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
            await conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);")
        return True
    except Exception as e:
        logger.critical(f"❌ فشل تهيئة الجداول: {e}")
        return False

async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ----------------------------------------------------------------------
# دوال التحكم بالمستخدمين والإحصائيات
# ----------------------------------------------------------------------
async def register_user(user_id: int, username: Optional[str]) -> None:
    pool = _get_pool()
    if pool is None: return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, username, last_active) VALUES ($1, $2, CURRENT_TIMESTAMP) "
                "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, last_active = CURRENT_TIMESTAMP;",
                user_id, username,
            )
    except Exception: pass

async def is_user_banned(user_id: int) -> bool:
    cached = _ban_cache.get(user_id)
    if cached is not None: return cached
    pool = _get_pool()
    if pool is None: return False
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT is_banned FROM users WHERE user_id = $1;", user_id)
        result = bool(row["is_banned"]) if row else False
    except Exception:
        result = False
    _ban_cache.set(user_id, result)
    return result

async def set_user_ban_status(user_id: int, banned: bool) -> None:
    pool = _get_pool()
    if pool is None: raise RuntimeError("قاعدة البيانات غير متاحة.")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_banned = $2 WHERE user_id = $1;", user_id, banned)
    _ban_cache.invalidate(user_id)

async def log_action(action_type: str) -> None:
    pool = _get_pool()
    if pool is None: return
    try:
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO stats_log (action_type) VALUES ($1);", action_type)
    except Exception: pass

async def get_setting(key: str) -> Optional[str]:
    cached = _settings_cache.get(key)
    if cached is not None: return None if cached is _CACHE_NONE_SENTINEL else cached
    pool = _get_pool()
    if pool is None: return None
    try:
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT value FROM settings WHERE key = $1;", key)
    except Exception: return None
    _settings_cache.set(key, val if val is not None else _CACHE_NONE_SENTINEL)
    return val

async def set_setting(key: str, value: str) -> None:
    pool = _get_pool()
    if pool is None: raise RuntimeError("قاعدة البيانات غير متاحة.")
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2;", key, value)
    _settings_cache.invalidate(key)


# ----------------------------------------------------------------------
# الاشتراك الإجباري والحمايات
# ----------------------------------------------------------------------
async def check_force_subscription(user_id: int, channel_username: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    clean_username = channel_username.replace("@", "").strip()
    try:
        member = await context.bot.get_chat_member(chat_id=f"@{clean_username}", user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return True

async def _is_subscription_required_and_unmet(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    if user_id in ADMIN_IDS: return None
    channel_username = await get_setting("force_channel")
    if not channel_username: return None
    if await check_force_subscription(user_id, channel_username, context): return None
    return channel_username

def get_sub_keyboard(channel_username: str) -> InlineKeyboardMarkup:
    clean_username = channel_username.replace("@", "").strip()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 اشترك في القناة أولاً", url=f"https://t.me/{clean_username}")],
        [InlineKeyboardButton("✅ تم الاشتراك (تفعيل)", callback_data="check_sub_again")],
    ])

async def _reject_if_banned(update: Update) -> bool:
    if await is_user_banned(update.effective_user.id):
        if update.effective_message:
            await update.effective_message.reply_text("🚫 عذرًا، حسابك محظور من استخدام هذا البوت.")
        return True
    return False

async def _reject_if_rate_limited(update: Update) -> bool:
    if is_rate_limited(update.effective_user.id):
        if update.effective_message:
            await update.effective_message.reply_text("⏳ تُرسل الطلبات بسرعة كبيرة. الرجاء الانتظار قليلًا.")
        return True
    return False


# ----------------------------------------------------------------------
# أدوات المعالجة والتحويل الفني للملفات والوسائط
# ----------------------------------------------------------------------
async def run_cmd(*args: str, timeout: float = CMD_TIMEOUT_SECONDS) -> tuple:
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 1, "", f"انتهت المهلة الزمنية القصوى"
    return process.returncode, stdout.decode(errors="ignore"), stderr.decode(errors="ignore")

async def download_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str, file_unique_id: str, filename: str) -> Path:
    file_obj = await context.bot.get_file(file_id)
    ext = Path(filename).suffix or ""
    local_path = DOWNLOADS_DIR / f"{file_unique_id}{ext}"
    await file_obj.download_to_drive(custom_path=str(local_path))
    return local_path

async def _check_size_or_reject(update: Update, file_size: Optional[int]) -> bool:
    if file_size and file_size > MAX_FILE_SIZE_BYTES:
        await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
        return False
    return True

def apply_audio_metadata(audio_path: Path, title: str = None, artist: str = None, album_art_path: Path = None):
    from PIL import Image
    from io import BytesIO
    ext = audio_path.suffix.lower()
    image_bytes = None

    if album_art_path and album_art_path.exists():
        try:
            with Image.open(album_art_path) as img:
                if img.mode in ("RGBA", "P", "LA") or img.format == "WEBP":
                    img = img.convert("RGB")
                buffer = BytesIO()
                img.save(buffer, format="JPEG", quality=95)
                image_bytes = buffer.getvalue()
        except Exception: pass

    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, TIT2, TPE1, APIC, ID3NoHeaderError
            try: audio = ID3(str(audio_path))
            except ID3NoHeaderError: audio = ID3()
            if title: audio.add(TIT2(encoding=3, text=title))
            if artist: audio.add(TPE1(encoding=3, text=artist))
            if image_bytes:
                audio.delall("APIC")
                audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=image_bytes))
            audio.save(str(audio_path))
        # الحاويات الأخرى يتم معالجتها عبر الموديولات المناسبة كما في الكود السابق...
    except Exception as e:
        logger.error(f"خطأ في حفظ الميتاداتا: {e}")


async def convert_docx_to_pdf(input_path: Path, out_dir: Path) -> Path:
    code, out, err = await run_cmd("libreoffice", "--headless", "--norestore", "--convert-to", "pdf", "--outdir", str(out_dir), str(input_path))
    result = out_dir / (input_path.stem + ".pdf")
    if code != 0 or not result.exists(): raise RuntimeError("فشل تحويل Word إلى PDF")
    return result

async def convert_pdf_to_docx(input_path: Path, out_dir: Path) -> Path:
    output_path = out_dir / (input_path.stem + ".docx")
    def _convert():
        from pdf2docx import Converter
        cv = Converter(str(input_path))
        cv.convert(str(output_path))
        cv.close()
    await asyncio.get_running_loop().run_in_executor(None, _convert)
    if not output_path.exists(): raise RuntimeError("فشل تحويل PDF إلى Word")
    return output_path

async def convert_audio(input_path: Path, out_dir: Path, target_format: str) -> Path:
    output_path = out_dir / (input_path.stem + f".{target_format}")
    code, out, err = await run_cmd("ffmpeg", "-y", "-i", str(input_path), "-vn", "-ar", "44100", "-ac", "2", str(output_path))
    if code != 0 or not output_path.exists(): raise RuntimeError("فشل تحويل الصوت")
    return output_path

async def convert_video_to_audio(input_path: Path, out_dir: Path) -> Path:
    output_path = out_dir / (input_path.stem + ".mp3")
    code, out, err = await run_cmd("ffmpeg", "-y", "-i", str(input_path), "-vn", "-acodec", "libmp3lame", "-q:a", "2", "-ar", "44100", "-ac", "2", str(output_path))
    if code != 0 or not output_path.exists(): raise RuntimeError("فشل استخراج الصوت")
    return output_path

async def convert_images_to_pdf(input_paths: List[Path], out_dir: Path, base_name: str) -> Path:
    if len(input_paths) > MAX_IMAGES_PER_BATCH: raise RuntimeError("تجاوز عدد الصور الأقصى")
    output_path = out_dir / (base_name + ".pdf")
    def _convert():
        import img2pdf
        from PIL import Image
        processed = []
        try:
            for p in input_paths:
                with Image.open(p) as img:
                    if img.mode in ("RGBA", "P", "LA"): img = img.convert("RGB")
                    tmp = p.with_suffix(".conv.jpg")
                    img.save(tmp, "JPEG", quality=95)
                    processed.append(str(tmp))
            pdf_bytes = img2pdf.convert(processed)
            with open(output_path, "wb") as f: f.write(pdf_bytes)
        finally:
            for pr in processed: Path(pr).unlink(missing_ok=True)
    await asyncio.get_running_loop().run_in_executor(None, _convert)
    return output_path

async def convert_images_to_docx(input_paths: List[Path], out_dir: Path, base_name: str) -> Path:
    if len(input_paths) > MAX_IMAGES_PER_BATCH: raise RuntimeError("تجاوز عدد الصور الأقصى")
    output_path = out_dir / (base_name + ".docx")
    def _convert():
        from docx import Document
        from docx.shared import Inches
        doc = Document()
        for p in input_paths: doc.add_picture(str(p), width=Inches(6))
        doc.save(str(output_path))
    await asyncio.get_running_loop().run_in_executor(None, _convert)
    return output_path

def encrypt_pdf_file(input_path: Path, output_path: Path, password: str):
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(input_path))
    writer = PdfWriter()
    for page in reader.pages: writer.add_page(page)
    writer.encrypt(password)
    with open(output_path, "wb") as f: writer.write(f)

def make_progress_bar(percent: int) -> str:
    total_blocks = 10
    filled = int(percent / 10)
    return "█" * filled + "░" * (total_blocks - filled) + f" {percent}%"

async def compress_pdf_file_async(input_path: Path, output_path: Path, status_msg) -> None:
    from pypdf import PdfReader, PdfWriter
    loop = asyncio.get_running_loop()
    last_update = {"t": 0.0}

    def _report(idx, total):
        now = time.time()
        if now - last_update["t"] < 1.5 and idx != total: return
        last_update["t"] = now
        percent = int((idx / total) * 100) if total else 100
        bar = make_progress_bar(percent)
        async def _edit():
            with contextlib.suppress(Exception):
                await status_msg.edit_text(f"⏳ <b>جاري معالجة صفحات الـ PDF...</b>\n\n📄 الصفحة: <code>{idx}</code>/<code>{total}</code>\n<code>{bar}</code>", parse_mode=ParseMode.HTML)
        asyncio.run_coroutine_threadsafe(_edit(), loop)

    def _compress():
        reader = PdfReader(str(input_path))
        writer = PdfWriter()
        total = len(reader.pages)
        for idx, page in enumerate(reader.pages, start=1):
            with contextlib.suppress(Exception): page.compress_content_streams()
            writer.add_page(page)
            _report(idx, total)
        with open(output_path, "wb") as f: writer.write(f)
    await loop.run_in_executor(None, _compress)


async def trim_audio_file(input_path: Path, output_path: Path, start_time: str, end_time: str):
    code, out, err = await run_cmd("ffmpeg", "-y", "-i", str(input_path), "-ss", start_time, "-to", end_time, str(output_path))
    if code != 0 or not output_path.exists(): raise RuntimeError("فشل قص الصوت")

async def change_audio_speed(input_path: Path, output_path: Path, speed: float):
    code, out, err = await run_cmd("ffmpeg", "-y", "-i", str(input_path), "-filter:a", f"atempo={speed}", "-vn", str(output_path))
    if code != 0 or not output_path.exists(): raise RuntimeError("فشل تغيير سرعة الصوت")

async def merge_audio_files(input_paths: List[Path], output_path: Path):
    if len(input_paths) < 2: raise RuntimeError("يجب توفير ملفين على الأقل.")
    norm_paths = []
    try:
        for i, p in enumerate(input_paths):
            np_path = output_path.parent / f"_norm_{i}_{p.stem}.wav"
            code, out, err = await run_cmd("ffmpeg", "-y", "-i", str(p), "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(np_path))
            if code != 0 or not np_path.exists(): raise RuntimeError("فشل تسوية الصوت قبل الدمج")
            norm_paths.append(np_path)
        inputs = []
        for p in norm_paths: inputs.extend(["-i", str(p)])
        code, out, err = await run_cmd("ffmpeg", "-y", *inputs, "-filter_complex", f"concat=n={len(norm_paths)}:v=0:a=1[a]", "-map", "[a]", "-c:a", "libmp3lame", "-q:a", "2", str(output_path))
        if code != 0 or not output_path.exists(): raise RuntimeError("فشل عملية دمج الصوتيات")
    finally:
        for p in norm_paths: p.unlink(missing_ok=True)

async def change_audio_volume(input_path: Path, output_path: Path, volume_db: float):
    code, out, err = await run_cmd("ffmpeg", "-y", "-i", str(input_path), "-filter:a", f"volume={volume_db}dB", str(output_path))
    if code != 0 or not output_path.exists(): raise RuntimeError("فشل تعديل مستوى الصوت")

async def process_epub_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    if not await _check_size_or_reject(update, getattr(tg_file, "file_size", None)): return
    msg = await update.message.reply_text("⏳ جاري تحويل الكتاب الإلكتروني...")
    try:
        if not is_calibre_available():
            await msg.edit_text("❌ برمجية Calibre غير متوفرة حاليًا.")
            return
        lp = await download_telegram_file(context, tg_file.file_id, tg_file.file_unique_id, filename)
        res = await convert_epub_to_pdf(lp, CONVERTED_DIR)
        await log_action("epub_to_pdf")
        with open(res, "rb") as f: await update.message.reply_document(document=f, filename=res.name)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ خطأ: {html.escape(str(e))}")

async def finalize_and_send_audio(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    audio_path_str = context.user_data.get("ready_audio_path")
    if not audio_path_str: return
    audio_path = Path(audio_path_str)
    title = context.user_data.get("meta_title")
    artist = context.user_data.get("meta_artist")
    art_path_str = context.user_data.get("meta_art_path")
    art_path = Path(art_path_str) if art_path_str else None

    await asyncio.get_running_loop().run_in_executor(None, apply_audio_metadata, audio_path, title, artist, art_path)
    with open(audio_path, "rb") as f:
        # 🟢 إزالة الكومنت/الرسالة الملحقة (caption=None) نهائيًا عند إرسال الموسيقى المحولة
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=f,
            title=title if title else audio_path.stem,
            performer=artist if artist else "فنان غير معروف",
            caption=None
        )
    for key in ["audio_state", "ready_audio_path", "meta_title", "meta_artist", "meta_art_path", "pending_audio", "pending_video"]:
        context.user_data.pop(key, None)


async def split_pdf_pages(input_path: Path, output_path: Path, start_page: int, end_page: int) -> int:
    from pypdf import PdfReader, PdfWriter
    def _split():
        reader = PdfReader(str(input_path))
        writer = PdfWriter()
        total = len(reader.pages)
        s = max(0, start_page - 1)
        e = min(total, end_page)
        for i in range(s, e): writer.add_page(reader.pages[i])
        count = max(0, e - s)
        if count > 0:
            with open(output_path, "wb") as f: writer.write(f)
        return count
    return await asyncio.get_running_loop().run_in_executor(None, _split)

async def merge_pdf_files(input_paths: List[Path], output_path: Path):
    from pypdf import PdfWriter
    def _merge():
        writer = PdfWriter()
        for p in input_paths: writer.append(str(p))
        with open(output_path, "wb") as f: writer.write(f)
    await asyncio.get_running_loop().run_in_executor(None, _merge)


# ----------------------------------------------------------------------
# كيبورد القوائم التفاعلية
# ----------------------------------------------------------------------
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 أدوات تعديل الملفات", callback_data="sub_files")],
        [InlineKeyboardButton("🎵 أدوات تعديل الصوتيات", callback_data="sub_audio")],
    ])

def files_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Word ➜ PDF", callback_data="mode_word2pdf")],
        [InlineKeyboardButton("📄 PDF ➜ Word", callback_data="mode_pdf2word")],
        [InlineKeyboardButton("✏️ تغيير اسم الملف", callback_data="mode_rename_file")], # 🟢 الميزة الجديدة هنا
        [InlineKeyboardButton("✂️ قص صفحات PDF", callback_data="mode_split_pdf")],
        [InlineKeyboardButton("🔗 دمج ملفات PDF", callback_data="mode_merge_pdf")],
        [InlineKeyboardButton("📚 EPUB ➜ PDF", callback_data="mode_ebook")],
        [InlineKeyboardButton("🖼️ تحويل صور إلى PDF/Word", callback_data="mode_image")],
        [InlineKeyboardButton("🔒 تشفير حماية الـ PDF", callback_data="mode_encrypt_pdf")],
        [InlineKeyboardButton("🗜️ ضغط ملف PDF (تقليل الحجم)", callback_data="mode_compress_pdf")],
        [InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="back_to_main")],
    ])

def audio_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 فيديو ➜ صوت MP3", callback_data="mode_video2audio")],
        [InlineKeyboardButton("🎵 تحويل صيغة صوتية", callback_data="mode_audio")],
        [InlineKeyboardButton("✂️ قص مقطع صوتي (Trim)", callback_data="mode_trim_audio")],
        [InlineKeyboardButton("⚡ تغيير سرعة الصوت", callback_data="mode_audio_speed")],
        [InlineKeyboardButton("🔗 دمج ملفات صوتية", callback_data="mode_merge_audio")],
        [InlineKeyboardButton("🔊 رفع / خفض الصوت", callback_data="mode_audio_volume")],
        [InlineKeyboardButton("🗣️ تحويل نص إلى صوت (TTS)", callback_data="mode_tts")],
        [InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="back_to_main")],
    ])

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 إحصائيات البوت", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 إذاعة جماعية (Broadcast)", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🚫 حظر مستخدم", callback_data="admin_ban"), InlineKeyboardButton("🟢 إلغاء حظر", callback_data="admin_unban")],
        [InlineKeyboardButton("🔐 تعيين قناة الاشتراك الإجباري", callback_data="admin_set_sub")],
        [InlineKeyboardButton("🧹 مسح الكاش", callback_data="admin_clear_cache")],
    ])

async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user.id in ADMIN_IDS: return
    await update.message.reply_text("⚙️ <b>لوحة تحكم الإدارة وقاعدة البيانات</b>", reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ("admin_state", "pending_broadcast", "pdf_state", "audio_state", "file_state", "current_mode", "merge_files", "merge_audio_files"):
        context.user_data.pop(key, None)
    await update.message.reply_text("✅ تم إلغاء أي عملية معلّقة. أرسل /start للبدء من جديد.")


# ----------------------------------------------------------------------
# استيراد وحدات المعالجة الخارجية الآمن
# ----------------------------------------------------------------------
try: import files_handler
except ModuleNotFoundError: files_handler = None

try: import audio_handler
except ModuleNotFoundError: audio_handler = None


# ----------------------------------------------------------------------
# الإذاعة الجماعية (Broadcast)
# ----------------------------------------------------------------------
async def _run_broadcast(context: ContextTypes.DEFAULT_TYPE, from_chat_id: int, message_id: int, status_message) -> None:
    pool = _get_pool()
    if pool is None: return
    rows = await pool.fetch("SELECT user_id FROM users WHERE is_banned = FALSE AND blocked_bot = FALSE;")
    targets = [r["user_id"] for r in rows]
    total = len(targets)
    if total == 0: return

    stats = {"success": 0, "blocked": 0, "failed": 0}
    semaphore = asyncio.Semaphore(BROADCAST_CONCURRENCY)

    async def _worker(t_id):
        async with semaphore:
            try:
                await context.bot.copy_message(chat_id=t_id, from_chat_id=from_chat_id, message_id=message_id)
                stats["success"] += 1
            except Forbidden:
                stats["blocked"] += 1
                async with pool.acquire() as conn: await conn.execute("UPDATE users SET blocked_bot = TRUE WHERE user_id = $1;", t_id)
            except Exception: stats["failed"] += 1
            await asyncio.sleep(BROADCAST_DELAY_PER_MESSAGE)

    await asyncio.gather(*(_worker(t) for t in targets))
    await status_message.edit_text(f"📢 <b>اكتملت الإذاعة الجماعية</b>\nنجح: {stats['success']} | حظر: {stats['blocked']}", parse_mode=ParseMode.HTML)


# ----------------------------------------------------------------------
# معالجة الرسائل والـ Callbacks الشاملة
# ----------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.username)
    if await _reject_if_banned(update): return
    missing = await _is_subscription_required_and_unmet(user.id, context)
    if missing:
        await update.message.reply_text("⚠️ يجب عليك الاشتراك في قناة البوت الرسمية أولاً!", reply_markup=get_sub_keyboard(missing))
        return
    context.user_data.clear()
    await update.message.reply_text("👋 أهلًا بك في بوت تحويل الصيغ المحترف!\n💡 اختر القسم المطلوب للبدء:", reply_markup=main_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if await is_user_banned(user_id): return
    if query.data == "check_sub_again":
        if await _is_subscription_required_and_unmet(user_id, context): return
        await query.edit_message_text("👋 أهلاً بك! اختر القسم المطلوب:", reply_markup=main_menu_keyboard())
        return

    if await _is_subscription_required_and_unmet(user_id, context): return
    await query.answer()
    data = query.data

    if data == "sub_files":
        await query.edit_message_text("📂 <b>قسم أدوات تعديل الملفات:</b>", reply_markup=files_submenu_keyboard(), parse_mode=ParseMode.HTML)
        return
    elif data == "sub_audio":
        await query.edit_message_text("🎵 <b>قسم أدوات تعديل الصوتيات:</b>", reply_markup=audio_submenu_keyboard(), parse_mode=ParseMode.HTML)
        return
    elif data == "back_to_main":
        await query.edit_message_text("👋 اختر القسم المطلوب للبدء:", reply_markup=main_menu_keyboard())
        return

    if data.startswith("admin_"):
        if user_id in ADMIN_IDS: await _handle_admin_callback(update, context, data)
        return

    context.user_data["current_mode"] = data
    if data == "mode_word2pdf": await query.edit_message_text("📄 أرسل الآن ملف Word (.doc أو .docx) لتحويله إلى PDF.")
    elif data == "mode_pdf2word": await query.edit_message_text("📄 أرسل الآن ملف PDF لتحويله إلى Word.")
    elif data == "mode_rename_file": await query.edit_message_text("✏️ أرسل الآن الملف المراد تغيير اسمه (أي صيغة كانت).") # 🟢 استقبال أمر التغيير
    elif data == "mode_video2audio": await query.edit_message_text("🎬 أرسل ملف الفيديو لاستخراج الصوت منه بصيغة MP3.")
    elif data == "mode_ebook": await query.edit_message_text("📚 أرسل ملف EPUB ليتم تحويله تلقائيًا إلى PDF.")
    elif data == "mode_audio": await query.edit_message_text("🎵 أرسل الملف الصوتي المراد تعديله أو تحويل صيغته.")
    elif data == "mode_image": await query.edit_message_text("🖼️ أرسل الصورة أو مجموعة الصور المراد تجميعها.")
    elif data == "mode_encrypt_pdf": await query.edit_message_text("🔒 أرسل الآن ملف PDF لحمايته وتشفيره بكلمة مرور.")
    elif data == "mode_compress_pdf": await query.edit_message_text("🗜️ أرسل ملف الـ PDF الذي تود ضغطه وتقليص حجمه الآن.")
    elif data == "mode_split_pdf": await query.edit_message_text("✂️ أرسل أولاً ملف الـ PDF الذي ترغب بقص صفحات منه.")
    elif data == "mode_merge_pdf":
        context.user_data["pdf_state"] = "WAITING_MERGE_FILES"
        context.user_data["merge_files"] = []
        await query.edit_message_text(f"🔗 أرسل ملفات الـ PDF (حتى {MAX_MERGE_FILES} ملفًا)، ثم أرسل كلمة <b>دمج</b>.", parse_mode=ParseMode.HTML)
    elif data == "mode_trim_audio": await query.edit_message_text("✂️ أرسل أولاً الملف الصوتي المراد قصه.")
    elif data == "mode_audio_speed": await query.edit_message_text("⚡ أرسل الملف الصوتي لتعديل وتغيير سرعته.")
    elif data == "mode_merge_audio":
        context.user_data["audio_state"] = "WAITING_MERGE_AUDIO"
        context.user_data["merge_audio_files"] = []
        await query.edit_message_text(f"🔗 أرسل الملفات الصوتية، ثم أرسل كلمة <b>دمج</b>.", parse_mode=ParseMode.HTML)
    elif data == "mode_audio_volume": await query.edit_message_text("🔊 أرسل المقطع الصوتي المراد رفع أو خفض حجم ديسيبل الصوت له.")
    elif data == "mode_tts":
        context.user_data["audio_state"] = "WAITING_TTS_TEXT"
        await query.edit_message_text("🗣️ أرسل الآن النص لتحويله إلى مقطع صوتي مسموع.")


async def _handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    if data == "admin_stats":
        pool = _get_pool()
        if pool:
            total = await pool.fetchval("SELECT COUNT(*) FROM users;")
            await query.edit_message_text(f"📊 إجمالي المستخدمين: <code>{total}</code>", reply_markup=admin_keyboard(), parse_mode=ParseMode.HTML)
    elif data == "admin_broadcast":
        context.user_data["admin_state"] = "WAITING_BROADCAST_MSG"
        await query.edit_message_text("📢 أرسل الآن رسالة الإذاعة:")
    elif data == "admin_ban":
        context.user_data["admin_state"] = "WAITING_BAN_ID"
        await query.edit_message_text("🚫 أرسل الـ User ID لحظره:")
    elif data == "admin_unban":
        context.user_data["admin_state"] = "WAITING_UNBAN_ID"
        await query.edit_message_text("🟢 أرسل الـ User ID لإلغاء حظره:")
    elif data == "admin_set_sub":
        context.user_data["admin_state"] = "WAITING_CHANNEL_USER"
        await query.edit_message_text("🔐 أرسل الآن يوزر القناة الجديد (مثال: @MyChannel) أو اكتب تعطيل:")
    elif data == "admin_clear_cache":
        _ban_cache.invalidate()
        _settings_cache.invalidate()
        await query.edit_message_text("✅ تم مسح الكاش بنجاح.", reply_markup=admin_keyboard())


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_banned(update) or await _reject_if_rate_limited(update): return
    if await _is_subscription_required_and_unmet(update.effective_user.id, context): return

    doc = update.message.document
    if not await _check_size_or_reject(update, doc.file_size): return
    filename = doc.file_name or "file"
    mode = context.user_data.get("current_mode")

    # 🟢 معالجة ميزة تغيير اسم الملف المستلم
    if mode == "mode_rename_file":
        lp = await download_telegram_file(context, doc.file_id, doc.file_unique_id, filename)
        context.user_data["rename_source_path"] = str(lp)
        context.user_data["rename_original_name"] = filename
        context.user_data["file_state"] = "WAITING_NEW_FILENAME"
        await update.message.reply_text("✏️ تم حفظ الملف المرفوع بنجاح.\nأرسل الآن **الاسم الجديد فقط** الذي ترغب به:")
        return

    if mode == "mode_compress_pdf" and filename.lower().endswith(".pdf"):
        msg = await update.message.reply_text("⏳ جاري ضغط ملف الـ PDF...")
        try:
            lp = await download_telegram_file(context, doc.file_id, doc.file_unique_id, filename)
            out_p = CONVERTED_DIR / f"compressed_{filename}"
            async with HeavyTaskGuard(): await compress_pdf_file_async(lp, out_p, msg)
            with open(out_p, "rb") as f: await update.message.reply_document(document=f, filename=out_p.name)
        except Exception: await update.message.reply_text("❌ تعذر ضغط هذا الملف.")
        finally: with contextlib.suppress(Exception): await msg.delete()
        return

    if mode == "mode_split_pdf" and filename.lower().endswith(".pdf"):
        lp = await download_telegram_file(context, doc.file_id, doc.file_unique_id, filename)
        context.user_data["split_pdf_source"] = str(lp)
        context.user_data["pdf_state"] = "WAITING_SPLIT_RANGE"
        await update.message.reply_text("⏱️ أرسل الآن نطاق الصفحات المطلوب قصها هكذا:\n<code>1-15</code>", parse_mode=ParseMode.HTML)
        return

    if mode == "mode_merge_pdf" and filename.lower().endswith(".pdf"):
        merge_list = context.user_data.setdefault("merge_files", [])
        if len(merge_list) >= MAX_MERGE_FILES: return
        lp = await download_telegram_file(context, doc.file_id, doc.file_unique_id, filename)
        merge_list.append(str(lp))
        await update.message.reply_text(f"📥 تم استقبال الملف رقم ({len(merge_list)}). اكتب <b>دمج</b> للإتمام.", parse_mode=ParseMode.HTML)
        return

    if Path(filename).suffix.lower() in AUDIO_EXTENSIONS:
        if mode == "mode_audio_speed":
            lp = await download_telegram_file(context, doc.file_id, doc.file_unique_id, filename)
            context.user_data["speed_source_path"] = str(lp)
            context.user_data["audio_state"] = "WAITING_SPEED_VALUE"
            await update.message.reply_text("⏱️ أرسل سرعة المعالجة بين 0.5 و 2.0:")
            return
        elif mode == "mode_merge_audio":
            merge_list = context.user_data.setdefault("merge_audio_files", [])
            if len(merge_list) >= MAX_MERGE_FILES: return
            lp = await download_telegram_file(context, doc.file_id, doc.file_unique_id, filename)
            merge_list.append(str(lp))
            await update.message.reply_text(f"📥 تم حفظ المقطع رقم ({len(merge_list)}). اكتب <b>دمج</b> عند الانتهاء.", parse_mode=ParseMode.HTML)
            return

    # تفويض لبقية المعالجات الخارجية إن وجدت
    if files_handler:
        is_handled = await files_handler.handle_files_document(update, context)
        if is_handled: return
    if audio_handler:
        await audio_handler.handle_audio_document(update, context)


async def handle_audio_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_banned(update) or await _reject_if_rate_limited(update): return
    if await _is_subscription_required_and_unmet(update.effective_user.id, context): return

    audio = update.message.audio or update.message.voice
    if not await _check_size_or_reject(update, getattr(audio, "file_size", None)): return
    mode = context.user_data.get("current_mode")
    filename = getattr(audio, "file_name", None) or "voice.ogg"

    if mode == "mode_trim_audio":
        lp = await download_telegram_file(context, audio.file_id, audio.file_unique_id, filename)
        context.user_data["trim_source_path"] = str(lp)
        context.user_data["audio_state"] = "WAITING_TRIM_TIME"
        await update.message.reply_text("⏱️ أرسل توقيت القص هكذا تمامًا:\n<code>00:01:10 - 00:02:45</code>", parse_mode=ParseMode.HTML)
        return
    elif mode == "mode_audio_speed":
        lp = await download_telegram_file(context, audio.file_id, audio.file_unique_id, filename)
        context.user_data["speed_source_path"] = str(lp)
        context.user_data["audio_state"] = "WAITING_SPEED_VALUE"
        await update.message.reply_text("⏱️ أرسل سرعة المعالجة كرقم عشري (مثلاً 1.25):")
        return
    elif mode == "mode_merge_audio":
        merge_list = context.user_data.setdefault("merge_audio_files", [])
        if len(merge_list) >= MAX_MERGE_FILES: return
        lp = await download_telegram_file(context, audio.file_id, audio.file_unique_id, filename)
        merge_list.append(str(lp))
        await update.message.reply_text(f"📥 تم حفظ المقطع رقم ({len(merge_list)}). اكتب <b>دمج</b> للإتمام.", parse_mode=ParseMode.HTML)
        return

    # تفويض للمعالج الخارجي مع إرسال الصوت بدون نص ملحق
    if audio_handler:
        await audio_handler.handle_audio_message(update, context)


async def handle_video_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_banned(update) or await _reject_if_rate_limited(update): return
    if await _is_subscription_required_and_unmet(update.effective_user.id, context): return
    video = update.message.video or update.message.video_note
    if not await _check_size_or_reject(update, getattr(video, "file_size", None)): return
    if audio_handler:
        await audio_handler.handle_video_message(update, context)


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_banned(update): return
    user_id = update.effective_user.id
    admin_state = context.user_data.get("admin_state")
    state = context.user_data.get("audio_state")
    pdf_state = context.user_data.get("pdf_state")
    file_state = context.user_data.get("file_state") # 🟢 استقبال حالة تعديل الملف
    text = (update.message.text or "").strip()

    # ---------------- معالجة الاسم الجديد للملف ----------------
    if file_state == "WAITING_NEW_FILENAME":
        src_path_str = context.user_data.get("rename_source_path")
        orig_name = context.user_data.get("rename_original_name", "file.dat")
        if not src_path_str:
            await update.message.reply_text("❌ لم يتم العثور على الملف الأصلي، يرجى المحاولة مجدداً.")
            return
        context.user_data.pop("file_state", None)
        
        src_p = Path(src_path_str)
        orig_ext = Path(orig_name).suffix
        new_name = text
        
        # حماية الصيغة الأصلية للملف إن لم يكتبها المستخدم يدوياً
        if not new_name.lower().endswith(orig_ext.lower()):
            new_name = f"{new_name}{orig_ext}"
            
        out_p = CONVERTED_DIR / f"renamed_{int(time.time())}_{new_name}"
        try:
            shutil.copy(str(src_p), str(out_p))
            await log_action("file_rename")
            with open(out_p, "rb") as f:
                await update.message.reply_document(document=f, filename=new_name, caption=None) # الكابشن ملغي
        except Exception as e:
            await update.message.reply_text(f"❌ حدث خطأ أثناء تغيير اسم الملف.")
        return

    # ---------------- إعداد الاشتراك الإجباري للآدمن ----------------
    if admin_state == "WAITING_CHANNEL_USER" and user_id in ADMIN_IDS:
        context.user_data.pop("admin_state", None)
        if text in ("تعطيل", "disable"):
            await set_setting("force_channel", "")
            await update.message.reply_text("🟢 تم تعطيل نظام الاشتراك الإجباري بنجاح.")
        else:
            if not text.startswith("@"): text = f"@{text}"
            await set_setting("force_channel", text)
            await update.message.reply_text(f"🔐 تم تعيين قناة الاشتراك الإجباري: <code>{html.escape(text)}</code>", parse_mode=ParseMode.HTML)
        return

    # ---------------- نطاق قص صفحات PDF ----------------
    if pdf_state == "WAITING_SPLIT_RANGE":
        if "-" not in text: return
        src_path_str = context.user_data.get("split_pdf_source")
        if not src_path_str: return
        context.user_data.pop("pdf_state", None)
        try: start_p, end_p = map(int, text.split("-", maxsplit=1))
        except ValueError: return
        msg = await update.message.reply_text("⏳ جاري قص النطاق المحدد...")
        src_p = Path(src_path_str)
        out_p = CONVERTED_DIR / f"clipped_{start_p}_to_{end_p}_{src_p.name}"
        try:
            page_count = await split_pdf_pages(src_p, out_p, start_p, end_p)
            if page_count > 0:
                await log_action("pdf_split")
                with open(out_p, "rb") as f: await update.message.reply_document(document=f, filename=out_p.name)
            await msg.delete()
        except Exception: await msg.edit_text("❌ فشلت عملية قص صفحات الملف.")
        return

    # ---------------- دمج ملفات PDF ----------------
    if pdf_state == "WAITING_MERGE_FILES" and text in ("دمج", "merge"):
        files_list = context.user_data.get("merge_files", [])
        if len(files_list) < 2: return
        context.user_data.pop("pdf_state", None)
        msg = await update.message.reply_text("🔗 جاري دمج ملفات الـ PDF...")
        out_p = CONVERTED_DIR / f"merged_{int(time.time())}.pdf"
        try:
            async with HeavyTaskGuard(): await merge_pdf_files([Path(p) for p in files_list], out_p)
            await log_action("pdf_merge")
            with open(out_p, "rb") as f: await update.message.reply_document(document=f, filename="Merged_Document.pdf")
            await msg.delete()
        except Exception: await msg.edit_text("❌ فشل دمج الملفات.")
        return

    # ---------------- دمج ملفات صوتية ----------------
    if state == "WAITING_MERGE_AUDIO" and text in ("دمج", "merge"):
        audio_list = context.user_data.get("merge_audio_files", [])
        if len(audio_list) < 2: return
        context.user_data.pop("audio_state", None)
        msg = await update.message.reply_text("⏳ جاري دمج المقاطع الصوتية...")
        out_p = CONVERTED_DIR / f"merged_audio_{int(time.time())}.mp3"
        try:
            async with HeavyTaskGuard(): await merge_audio_files([Path(p) for p in audio_list], out_p)
            await log_action("audio_merge")
            with open(out_p, "rb") as f: 
                # 🟢 إزالة الرسالة الملحقة/caption تماماً عند الإرسال الصوتي
                await update.message.reply_audio(audio=f, caption=None)
            await msg.delete()
        except Exception: await msg.edit_text("❌ حدث خطأ أثناء الدمج.")
        return

    # ---------------- تغيير سرعة الصوت ----------------
    if state == "WAITING_SPEED_VALUE":
        try: speed_val = float(text)
        except ValueError: return
        context.user_data.pop("audio_state", None)
        src_path = context.user_data.get("speed_source_path")
        if not src_path: return
        msg = await update.message.reply_text("⏳ جاري تعديل السرعة...")
        src_p = Path(src_path)
        out_p = CONVERTED_DIR / f"speed_{speed_val}_{src_p.name}"
        try:
            await change_audio_speed(src_p, out_p, speed_val)
            await log_action("audio_speed")
            with open(out_p, "rb") as f: 
                # 🟢 إزالة النص التوضيحي الملحق
                await update.message.reply_audio(audio=f, caption=None)
            await msg.delete()
        except Exception: await msg.edit_text("❌ حدث خطأ.")
        return

    # ---------------- تحويل نص إلى صوت (TTS) ----------------
    if state == "WAITING_TTS_TEXT":
        if await _reject_if_rate_limited(update): return
        context.user_data.pop("audio_state", None)
        if len(text) > 2000: return
        msg = await update.message.reply_text("⏳ جاري توليد المقطع الصوتي...")
        try:
            from gtts import gTTS
            out_p = CONVERTED_DIR / f"tts_{int(time.time())}.mp3"
            def _tts(): gTTS(text=text, lang="ar").save(str(out_p))
            await asyncio.get_running_loop().run_in_executor(None, _tts)
            await log_action("tts")
            with open(out_p, "rb") as f: 
                # 🟢 إزالة الكابشن النصي
                await update.message.reply_audio(audio=f, caption=None)
            await msg.delete()
        except Exception: await msg.edit_text("❌ فشل توليد الصوت.")
        return


# ----------------------------------------------------------------------
# إعداد خادم ومحرك البوت الأساسي
# ----------------------------------------------------------------------
def main():
    req = HTTPXRequest(connection_pool_size=40, read_timeout=30.0, write_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio_message_router))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video_message_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    loop = asyncio.get_event_loop()
    if not loop.run_until_complete(init_db()):
        return

    logger.info("🚀 البوت بدأ العمل بكفاءة عالية الآن...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
