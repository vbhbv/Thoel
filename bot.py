import os
import re
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# فرض توفر الوحدات الخارجية الأساسية أو معالجتها بأمان
from pdf_epub_converter import (
    convert_epub_to_pdf,
    is_calibre_available,
    EbookConversionError,
)

# ----------------------------------------------------------------------
# الإعدادات العامة والتكوين المعماري
# ----------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("CRITICAL: يجب ضبط متغير البيئة BOT_TOKEN")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("CRITICAL: يجب ضبط متغير البيئة DATABASE_URL المربوط بقاعدة PostgreSQL")

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip()]

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
CONVERTED_DIR = BASE_DIR / "converted"
DOWNLOADS_DIR.mkdir(exist_ok=True)
CONVERTED_DIR.mkdir(exist_ok=True)

FILE_MAX_AGE = 3600  # ساعة واحدة
CLEANUP_INTERVAL = 1800  # 30 دقيقة
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 ميجابايت كحد أقصى لحماية الموارد

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".wma"}

# مجمع الخيوط (Thread Pool) المخصص لعزل العمليات الحسابية والثقيلة عن الـ Event Loop الرئيسي
thread_executor = ThreadPoolExecutor(max_workers=os.cpu_count() * 2)

# ----------------------------------------------------------------------
# إدارة قاعدة بيانات PostgreSQL عبر Connection Pool
# ----------------------------------------------------------------------

def fix_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url

async def init_db_pool(app: Application):
    """تهيئة مجمع الاتصالات وتخزينه في بوت داتا البوت بشكل آمن وعالمي"""
    url = fix_database_url(DATABASE_URL)
    try:
        pool = await asyncpg.create_pool(
            url,
            min_size=5,
            max_size=20,
            max_queries=500,
            max_inactive_connection_lifetime=300.0
        )
        app.bot_data["db_pool"] = pool
        
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_banned BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS stats_log (
                    id SERIAL PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
        logger.info("✅ تم إعداد Connection Pool وقاعدة البيانات PostgreSQL بنجاح كفء.")
    except Exception as e:
        logger.critical(f"❌ فشل تهيئة مجمع اتصالات قاعدة البيانات: {e}")
        raise e

async def close_db_pool(app: Application):
    pool = app.bot_data.get("db_pool")
    if pool:
        await pool.close()
        logger.info("🔒 تم إغلاق مجمع اتصالات قاعدة البيانات بنجاح.")

# دالات الاستعلام السريعة المعتمدة على الـ Pool الممرر عبر الـ context
async def register_user(pool: asyncpg.Pool, user_id: int, username: str):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username) 
            VALUES ($1, $2) 
            ON CONFLICT (user_id) DO UPDATE SET username = $2;
            """,
            user_id, username
        )

async def is_user_banned(pool: asyncpg.Pool, user_id: int) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT is_banned FROM users WHERE user_id = $1;", user_id) or False

async def log_action(pool: asyncpg.Pool, action_type: str):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO stats_log (action_type) VALUES ($1);", action_type)

async def get_setting(pool: asyncpg.Pool, key: str) -> str:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT value FROM settings WHERE key = $1;", key)

async def set_setting(pool: asyncpg.Pool, key: str, value: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2;",
            key, value
        )

# ----------------------------------------------------------------------
# نظام الاشتراك الإجباري الآمن
# ----------------------------------------------------------------------

async def check_force_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if user_id in ADMIN_IDS:
        return True
    pool = context.application.bot_data["db_pool"]
    channel_username = await get_setting(pool, "force_channel")
    if not channel_username:
        return True
        
    chat_id = f"@{channel_username.replace('@', '').strip()}"
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status in ["member", "administrator", "creator"]:
            return True
    except Exception as e:
        logger.error(f"خطأ التحقق من عضوية القناة {chat_id}: {e}")
        return True # تمرير المستخدم في حال تعطل صلاحية البوت حماية للنظام من التوقف الكامل
    return False

def get_sub_keyboard(channel_username: str) -> InlineKeyboardMarkup:
    clean_username = channel_username.replace("@", "").strip()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 اشترك في القناة أولاً", url=f"https://t.me/{clean_username}")],
        [InlineKeyboardButton("✅ تم الاشتراك (تفعيل)", callback_data="check_sub_again")]
    ])

# ----------------------------------------------------------------------
# أدوات الفتح والمعالجة غير الحاصرة (Non-blocking Engines)
# ----------------------------------------------------------------------

async def run_cmd(*args: str) -> tuple[int, str, str]:
    """تنفيذ أوامر النظام الحركية بشكل آمن دون حصر الـ Event Loop"""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode(errors="ignore"), stderr.decode(errors="ignore")
    except Exception as e:
        logger.error(f"خطأ فادح أثناء تشغيل الأمر الفرعي {args[0]}: {e}")
        return -1, "", str(e)

async def download_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str, file_unique_id: str, filename: str) -> Path:
    file_obj = await context.bot.get_file(file_id)
    if file_obj.file_size and file_obj.file_size > MAX_FILE_SIZE_BYTES:
        raise ValueError("❌ حجم الملف تجاوز الحد المسموح به (20 ميجابايت).")
        
    ext = Path(filename).suffix or ""
    local_path = DOWNLOADS_DIR / f"{file_unique_id}{ext}"
    await file_obj.download_to_drive(custom_path=str(local_path))
    return local_path

# ----------------------------------------------------------------------
# معالجات وسائط الـ PDF والأصوات المعزولة تماماً في خيوط مخصصة
# ----------------------------------------------------------------------

def _encrypt_pdf_sync(input_path: Path, output_path: Path, password: str):
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(input_path))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    with open(output_path, "wb") as f:
        writer.write(f)

async def encrypt_pdf_file(input_path: Path, output_path: Path, password: str):
    await asyncio.get_running_loop().run_in_executor(
        thread_executor, _encrypt_pdf_sync, input_path, output_path, password
    )

def _split_pdf_sync(input_path: Path, output_path: Path, start_page: int, end_page: int):
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(input_path))
    writer = PdfWriter()
    total_pages = len(reader.pages)
    start_idx = max(0, start_page - 1)
    end_idx = min(total_pages, end_page)
    for i in range(start_idx, end_idx):
        writer.add_page(reader.pages[i])
    with open(output_path, "wb") as f:
        writer.write(f)

async def split_pdf_pages(input_path: Path, output_path: Path, start_page: int, end_page: int):
    await asyncio.get_running_loop().run_in_executor(
        thread_executor, _split_pdf_sync, input_path, output_path, start_page, end_page
    )

async def compress_pdf_file_async(input_path: Path, output_path: Path, status_msg, context: ContextTypes.DEFAULT_TYPE):
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(input_path))
    writer = PdfWriter()
    total_pages = len(reader.pages)
    
    last_update = time.time()
    for idx, page in enumerate(reader.pages, start=1):
        try:
            page.compress_content_streams()
        except Exception as e:
            logger.warning(f"تخطي ضغط محتوى الصفحة {idx}: {e}")
        writer.add_page(page)
        
        # حماية معدل التحديث (Rate Limiting) لمنع حظر البوت من قِبل تليجرام عند الضغط
        if time.time() - last_update >= 2.5 or idx == total_pages:
            percent = int((idx / total_pages) * 100)
            bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
            try:
                await status_msg.edit_text(
                    f"⏳ **جاري ضغط ومعالجة صفحات الـ PDF...**\n\n📄 الصفحة: `{idx}` / `{total_pages}`\n`{bar} {percent}%`"
                )
            except Exception:
                pass
            last_update = time.time()
            
    await asyncio.get_running_loop().run_in_executor(
        thread_executor, lambda: writer.write(open(output_path, "wb"))
    )

# ----------------------------------------------------------------------
# فلاتر معالجة الصوتيات المتقدمة (FFmpeg غير حاصر)
# ----------------------------------------------------------------------

async def change_audio_speed(input_path: Path, output_path: Path, speed: float):
    code, _, err = await run_cmd("ffmpeg", "-y", "-i", str(input_path), "-filter:a", f"atempo={speed}", "-vn", str(output_path))
    if code != 0 or not output_path.exists():
        raise RuntimeError(f"فشل تعديل السرعة: {err[-200:]}")

async def change_audio_volume(input_path: Path, output_path: Path, volume_db: float):
    code, _, err = await run_cmd("ffmpeg", "-y", "-i", str(input_path), "-filter:a", f"volume={volume_db}dB", str(output_path))
    if code != 0 or not output_path.exists():
        raise RuntimeError(f"فشل تعديل مستوى الصوت: {err[-200:]}")

async def trim_audio_file(input_path: Path, output_path: Path, start_time: str, end_time: str):
    code, _, err = await run_cmd("ffmpeg", "-y", "-ss", start_time, "-to", end_time, "-i", str(input_path), "-acodec", "copy", str(output_path))
    if code != 0 or not output_path.exists():
        raise RuntimeError(f"فشل قص الصوت، يرجى مراجعة التوقيت المدخل.")

# ----------------------------------------------------------------------
# لوحات التحكم وقوائم الأزرار (Inline Keyboards)
# ----------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 أدوات تعديل الملفات", callback_data="sub_files")],
        [InlineKeyboardButton("🎵 أدوات تعديل الصوتيات", callback_data="sub_audio")]
    ])

def files_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✂️ قص صفحات PDF", callback_data="mode_split_pdf")],
        [InlineKeyboardButton("🔒 تشفير حماية الـ PDF", callback_data="mode_encrypt_pdf")],
        [InlineKeyboardButton("🗜️ ضغط ملف PDF", callback_data="mode_compress_pdf")],
        [InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="back_to_main")]
    ])

def audio_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✂️ قص مقطع صوتي (Trim)", callback_data="mode_trim_audio")],
        [InlineKeyboardButton("⚡ تغيير سرعة الصوت", callback_data="mode_audio_speed")],
        [InlineKeyboardButton("🔊 رفع / خفض الصوت", callback_data="mode_audio_volume")],
        [InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="back_to_main")]
    ])

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 إحصائيات البوت", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 إذاعة جماعية (Broadcast)", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔐 تعيين قناة الاشتراك الإجباري", callback_data="admin_set_sub")]
    ])

# ----------------------------------------------------------------------
# التوجيه المركزي ومعالجة الأحداث والمدخلات السليمة للمستخدمين والآدمن
# ----------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return
    user = update.effective_user
    pool = context.application.bot_data["db_pool"]
    
    await register_user(pool, user.id, user.username)
    if await is_user_banned(pool, user.id):
        await update.message.reply_text("🚫 نعتذر، حسابك محظور حاليًا من استخدام البوت.")
        return
        
    channel_username = await get_setting(pool, "force_channel")
    if channel_username and not await check_force_subscription(user.id, context):
        await update.message.reply_text(
            f"⚠️ **عذراً، يجب عليك الاشتراك في قناة البوت الرسمية أولاً لاستخدام ميزات التحويل الفورية!**",
            reply_markup=get_sub_keyboard(channel_username)
        )
        return

    context.user_data.clear()
    await update.message.reply_text("👋 أهلًا بك في بوت معالجة وتعديل الملفات المطور!\n💡 اختر القسم المطلوب للبدء:", reply_markup=main_menu_keyboard())

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user: return
    user_id = query.from_user.id
    pool = context.application.bot_data["db_pool"]
    
    if await is_user_banned(pool, user_id):
        await query.answer("🚫 حسابك محظور.", show_alert=True)
        return

    if query.data == "check_sub_again":
        channel_username = await get_setting(pool, "force_channel")
        if channel_username and not await check_force_subscription(user_id, context):
            await query.answer("❌ لم تشترك في القناة بعد! يرجى الاشتراك والمحاولة مجدداً.", show_alert=True)
            return
        await query.answer("✅ تم تفعيل حسابك بنجاح!")
        await query.edit_message_text("👋 أهلاً بك! اختر القسم المطلوب للبدء:", reply_markup=main_menu_keyboard())
        return

    # التحقق من الجدار الناري للاشتراك المشروط
    channel_username = await get_setting(pool, "force_channel")
    if channel_username and not await check_force_subscription(user_id, context):
        await query.answer("⚠️ يجب عليك الاشتراك في القناة الرسمية أولاً لتفعيل اللوحة!", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "sub_files":
        await query.edit_message_text("📂 **قسم أدوات تعديل الملفات:**", reply_markup=files_submenu_keyboard())
        return
    elif data == "sub_audio":
        await query.edit_message_text("🎵 **قسم أدوات تعديل الصوتيات:**", reply_markup=audio_submenu_keyboard())
        return
    elif data == "back_to_main":
        await query.edit_message_text("👋 اختر القسم المطلوب من الأزرار أدناه للبدء:", reply_markup=main_menu_keyboard())
        return

    # معالجة لوحة الآدمن
    if data.startswith("admin_"):
        if user_id not in ADMIN_IDS: return
        if data == "admin_stats":
            async with pool.acquire() as conn:
                total_users = await conn.fetchval("SELECT COUNT(*) FROM users;")
                banned_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = TRUE;")
                total_actions = await conn.fetchval("SELECT COUNT(*) FROM stats_log;")
            current_chan = await get_setting(pool, "force_channel") or "معطلة ❌"
            await query.edit_message_text(
                f"📊 **إحصائيات النظام الفورية:**\n\n👥 إجمالي المستخدمين: `{total_users}`\n🚫 المحظورين: `{banned_users}`\n⚙️ العمليات المنجزة: `{total_actions}`\n📢 قناة الاشتراك المشروط: `{current_chan}`", 
                reply_markup=admin_keyboard()
            )
        elif data == "admin_broadcast":
            context.user_data["admin_state"] = "WAITING_BROADCAST_MSG"
            await query.edit_message_text("📢 أرسل الآن نص أو وسيطة الإذاعة الجماعية لجميع المستخدمين:")
        elif data == "admin_set_sub":
            context.user_data["admin_state"] = "WAITING_CHANNEL_USER"
            await query.edit_message_text("🔐 أرسل الآن معرف القناة الجديد شامل العلامة (مثال: `@MyChannel`) أو اكتب `تعطيل` لتعطيل النظام:")
        return

    # حجز وتجهيز الأنماط الفرعية
    context.user_data["current_mode"] = data
    modes_messages = {
        "mode_compress_pdf": "🗜️ أرسل ملف الـ PDF الذي تود ضغطه وتقليص حجمه الآن.",
        "mode_split_pdf": "✂️ أرسل أولاً ملف الـ PDF الذي ترغب بقص صفحات منه.",
        "mode_encrypt_pdf": "🔒 أرسل الآن ملف PDF لحمايته وتشفيره بكلمة مرور.",
        "mode_trim_audio": "✂️ أرسل أولاً الملف الصوتي المراد قصه وثيقة أو ملف صوتي.",
        "mode_audio_speed": "⚡ أرسل الملف الصوتي لتعديل وتغيير سرعته الحركية.",
        "mode_audio_volume": "🔊 أرسل المقطع الصوتي المراد رفع أو خفض ديسيبل الصوت له."
    }
    if data in modes_messages:
        await query.edit_message_text(modes_messages[data])

async def handle_unified_document_and_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """المعالج الموحد للملفات والوثائق لمنع التداخل والتعامل مع العمليات بالتوازي"""
    if not update.effective_user or not update.message: return
    user_id = update.effective_user.id
    pool = context.application.bot_data["db_pool"]
    
    if await is_user_banned(pool, user_id): return
    channel_username = await get_setting(pool, "force_channel")
    if channel_username and not await check_force_subscription(user_id, context):
        await update.message.reply_text("⚠️ يرجى التفعيل والاشتراك أولاً عبر /start")
        return

    mode = context.user_data.get("current_mode")
    doc = update.message.document
    audio = update.message.audio or update.message.voice
    
    # تحديد الهوية والاسم والـ Extension للملف المرفق
    file_id = doc.file_id if doc else audio.file_id
    file_unique_id = doc.file_unique_id if doc else audio.file_unique_id
    filename = doc.file_name if doc else (getattr(audio, 'file_name', 'voice.ogg') or 'voice.ogg')
    
    if not mode:
        await update.message.reply_text("💡 من فضلك اختر الأداة المطلوبة من قائمة الأزرار أولاً عبر إرسال /start")
        return

    # 1. معالجة ضغط ملفات الـ PDF
    if mode == "mode_compress_pdf" and filename.lower().endswith('.pdf'):
        msg = await update.message.reply_text("⏳ جاري تهيئة وتحميل ملف الـ PDF لبدء الضغط...")
        try:
            lp = await download_telegram_file(context, file_id, file_unique_id, filename)
            out_p = CONVERTED_DIR / f"compressed_{file_unique_id}.pdf"
            await compress_pdf_file_async(lp, out_p, msg, context)
            await log_action(pool, "compress_pdf")
            with open(out_p, "rb") as f:
                await update.message.reply_document(document=f, filename=f"Compressed_{filename}", caption="✅ تم ضغط الملف المرفوع بنجاح!")
        except Exception as e:
            logger.error(f"فشل ضغط PDF: {e}")
            await update.message.reply_text("❌ تعذر ضغط هذا الملف نظراً لقيود التشفير والهيكلة الداخلية الخاصة به.")
        finally:
            try: await msg.delete() 
            except Exception: pass
        return

    # 2. معالجة قص صفحات الـ PDF
    if mode == "mode_split_pdf" and filename.lower().endswith('.pdf'):
        try:
            lp = await download_telegram_file(context, file_id, file_unique_id, filename)
            context.user_data["split_pdf_source"] = str(lp)
            context.user_data["pdf_state"] = "WAITING_SPLIT_RANGE"
            await update.message.reply_text("⏱️ تم حفظ المستند بنجاح.\nأرسل الآن نطاق الصفحات المطلوب قصها تماماً كالتالي: `1-15`")
        except Exception as e:
            await update.message.reply_text(f"{e}")
        return

    # 3. معالجة تشفير وحماية ملفات الـ PDF
    if mode == "mode_encrypt_pdf" and filename.lower().endswith('.pdf'):
        try:
            lp = await download_telegram_file(context, file_id, file_unique_id, filename)
            context.user_data["encrypt_pdf_source"] = str(lp)
            context.user_data["pdf_state"] = "WAITING_PASSWORD"
            await update.message.reply_text("🔒 تم استقبال الملف المرفق. أرسل الآن كلمة المرور التي تود حماية وتشفير الملف بها:")
        except Exception as e:
            await update.message.reply_text(f"{e}")
        return

    # 4. معالجة هندسة الصوتيات وتغيير سرعتها أو حجم ديسيبل الصوت له
    ext = Path(filename).suffix.lower()
    if ext in AUDIO_EXTENSIONS or audio:
        if mode == "mode_audio_speed":
            lp = await download_telegram_file(context, file_id, file_unique_id, filename)
            context.user_data["speed_source_path"] = str(lp)
            context.user_data["audio_state"] = "WAITING_SPEED_VALUE"
            await update.message.reply_text("⏱️ أرسل سرعة المعالجة الصوتية المطلوبة كرقم عشري بين `0.5` و `2.0` (مثال: `1.5` لتسريع المقطع):")
            return
        elif mode == "mode_audio_volume":
            lp = await download_telegram_file(context, file_id, file_unique_id, filename)
            context.user_data["volume_source_path"] = str(lp)
            context.user_data["audio_state"] = "WAITING_VOLUME_VALUE"
            await update.message.reply_text("🔊 أرسل مستوى الصوت المطلوب بالديسيبل (dB) كقيمة رقمية (مثلاً `6` لرفع الصوت، أو `-6` لخفضه):")
            return
        elif mode == "mode_trim_audio":
            lp = await download_telegram_file(context, file_id, file_unique_id, filename)
            context.user_data["trim_source_path"] = str(lp)
            context.user_data["audio_state"] = "WAITING_TRIM_TIME"
            await update.message.reply_text("⏱️ ممتاز، أرسل الآن توقيت القص بالصيغة الفنية التالية تماماً:\n`00:01:10 - 00:02:45`")
            return

    await update.message.reply_text("⚠️ صيغة الملف غير مدعومة للمود المحدد حالياً!")

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message or not update.message.text: return
    user_id = update.effective_user.id
    pool = context.application.bot_data["db_pool"]
    
    if await is_user_banned(pool, user_id): return

    text = update.message.text.strip()
    admin_state = context.user_data.get("admin_state")
    pdf_state = context.user_data.get("pdf_state")
    audio_state = context.user_data.get("audio_state")

    # [إدارة الآدمن] - تخصيص يوزر قناة الاشتراك الإجباري المشروط بنظام مرن حركي
    if admin_state == "WAITING_CHANNEL_USER" and user_id in ADMIN_IDS:
        context.user_data.pop("admin_state", None)
        if text in ["تعطيل", "disable"]:
            await set_setting(pool, "force_channel", "")
            await update.message.reply_text("🟢 تم إيقاف وتعطيل ميزة نظام الاشتراك الإجباري بنجاح.")
        else:
            clean_chan = text.replace("https://t.me/", "").replace("@", "").strip()
            await set_setting(pool, "force_channel", clean_chan)
            await update.message.reply_text(f"🔐 تم إعداد وتثبيت قناة الاشتراك المشروط بنجاح على: `@{clean_chan}`")
        return

    # [إدارة الآدمن] - الإذاعة الجماعية الكفؤ والمقاومة للانفجار عبر الـ Throttling
    if admin_state == "WAITING_BROADCAST_MSG" and user_id in ADMIN_IDS:
        context.user_data.pop("admin_state", None)
        status = await update.message.reply_text("⏳ جاري بدء البث الفوري للمستندات...")
        async with pool.acquire() as conn:
            users = await conn.fetch("SELECT user_id FROM users WHERE is_banned = FALSE;")
        
        success, failed = 0, 0
        for row in users:
            if row["user_id"] == user_id: continue
            try:
                await context.bot.copy_message(chat_id=row["user_id"], from_chat_id=update.message.chat_id, message_id=update.message.message_id)
                success += 1
                await asyncio.sleep(0.05) # تجنب تخطي ليميت التليجرام لكل ثانية (Per second limits)
            except Exception:
                failed += 1
        await status.edit_text(f"📢 **اكتملت عملية البث الجماعي الموحد:**\n\n✅ تم تسليمها بنجاح لـ: `{success}`\n❌ تعذر تسليمها لـ: `{failed}`")
        return

    # [تعديل الـ PDF] - معالجة تجميع قص الصفحات بحماية ضد الـ Regex والمدخلات الفاسدة
    if pdf_state == "WAITING_SPLIT_RANGE":
        if not re.match(r"^\d+-\d+$", text):
            await update.message.reply_text("⚠️ صيغة النطاق خاطئة، يرجى إدخال أرقام صحيحة، مثال: `1-15`")
            return
        src_path_str = context.user_data.get("split_pdf_source")
        if not src_path_str:
            await update.message.reply_text("❌ انتهت صلاحية الجلسة المرفوعة، يرجى إرسال الملف من جديد.")
            return
        context.user_data.pop("pdf_state", None)
        start_p, end_p = map(int, text.split("-"))
        
        msg = await update.message.reply_text("⏳ جاري استخراج وقص النطاق المستهدف من الـ PDF...")
        try:
            src_p = Path(src_path_str)
            out_p = CONVERTED_DIR / f"clipped_{start_p}_{end_p}_{src_p.name}"
            await split_pdf_pages(src_p, out_p, start_p, end_p)
            await log_action(pool, "pdf_split")
            with open(out_p, "rb") as f:
                await update.message.reply_document(document=f, filename=out_p.name, caption=f"✂️ تم قص الصفحات من {start_p} إلى {end_p} بنجاح!")
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ تعذر معالجة المستند: {e}")
        return

    # [تعديل الـ PDF] - معالجة التشفير وحقن الرقم السري الفوري
    if pdf_state == "WAITING_PASSWORD":
        src_path_str = context.user_data.get("encrypt_pdf_source")
        if not src_path_str:
            await update.message.reply_text("❌ الملف غير موجود، يرجى إعادة الرفع.")
            return
        context.user_data.pop("pdf_state", None)
        msg = await update.message.reply_text("🔒 جاري إغلاق وتشفير حاوية المستند...")
        try:
            src_p = Path(src_path_str)
            out_p = CONVERTED_DIR / f"secured_{src_p.name}"
            await encrypt_pdf_file(src_p, out_p, text)
            await log_action(pool, "pdf_encrypt")
            with open(out_p, "rb") as f:
                await update.message.reply_document(document=f, filename=out_p.name, caption="🔒 تم حماية وتشفير المستند بنجاح بالرمز المخصص!")
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ تعذر التشفير: {e}")
        return

    # [تعديل الصوت] - معالجة وتعديل وتغيير سرعة الملف
    if audio_state == "WAITING_SPEED_VALUE":
        try:
            speed_val = float(text)
            if not (0.5 <= speed_val <= 2.0): raise ValueError()
        except ValueError:
            await update.message.reply_text("⚠️ يرجى إدخال رقم عشري سليم بين 0.5 و 2.0 فقط:")
            return
        context.user_data.pop("audio_state", None)
        src_path = context.user_data.get("speed_source_path")
        msg = await update.message.reply_text("⏳ جاري تعديل سرعة المقطع الصوتي الحالية...")
        try:
            src_p = Path(src_path)
            out_p = CONVERTED_DIR / f"speed_{speed_val}_{src_p.name}"
            await change_audio_speed(src_p, out_p, speed_val)
            await log_action(pool, "audio_speed")
            with open(out_p, "rb") as f:
                await update.message.reply_audio(audio=f, caption=f"⚡ تم تعديل سرعة معالجة الملف إلى {speed_val}x بنجاح!")
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ فشل الإجراء: {e}")
        return

    # [تعديل الصوت] - معالجة وتغيير قوة وحجم الديسيبل للصوت
    if audio_state == "WAITING_VOLUME_VALUE":
        try:
            vol_val = float(text)
        except ValueError:
            await update.message.reply_text("⚠️ يرجى إدخال قيمة رقمية صحيحة نقية:")
            return
        context.user_data.pop("audio_state", None)
        src_path = context.user_data.get("volume_source_path")
        msg = await update.message.reply_text("⏳ جاري هندسة مستوى ديسيبل التردد الحالي للصوت...")
        try:
            src_p = Path(src_path)
            out_p = CONVERTED_DIR / f"vol_{vol_val}_{src_p.name}"
            await change_audio_volume(src_p, out_p, vol_val)
            await log_action(pool, "audio_volume")
            with open(out_p, "rb") as f:
                await update.message.reply_audio(audio=f, caption=f"🔊 تم تعديل حجم الصوت بمقدار {vol_val}dB بنجاح!")
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ فشل أثناء تعديل التردد: {e}")
        return

    # [تعديل الصوت] - معالجة دقة نطاق قص الأصوات (FFmpeg Trim String validation)
    if audio_state == "WAITING_TRIM_TIME":
        if " - " not in text:
            await update.message.reply_text("⚠️ يرجى إرسال التوقيت بالشكل الصحيح تماماً، مثال: `00:00:10 - 00:00:40`")
            return
        context.user_data.pop("audio_state", None)
        parts = text.split(" - ")
        start_t, end_t = parts[0].strip(), parts[1].strip()
        src_path = context.user_data.get("trim_source_path")
        msg = await update.message.reply_text("⏳ جاري اقتطاع المسار الموجه بدقة متناهية...")
        try:
            src_p = Path(src_path)
            out_p = CONVERTED_DIR / f"trimmed_{src_p.name}"
            await trim_audio_file(src_p, out_p, start_t, end_t)
            await log_action(pool, "audio_trim")
            with open(out_p, "rb") as f:
                await update.message.reply_audio(audio=f, caption="✂️ تم قص المقطع بنجاح حسب التوقيت المختار!")
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ فشل معالجة قص الصوت: {e}")
        return

async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """وظيفة دورية آمنة ومحمية من الانهيار لتنظيف الملفات من القرص بانتظام تفادياً لامتلاء السيرفر"""
    now = time.time()
    removed = 0
    for folder in (DOWNLOADS_DIR, CONVERTED_DIR):
        if not folder.exists(): continue
        for path in folder.glob("*"):
            try:
                if path.is_file() and (now - path.stat().st_mtime) > FILE_MAX_AGE:
                    path.unlink()
                    removed += 1
            except Exception as e:
                logger.error(f"فشل حذف الملف المؤقت {path.name}: {e}")
    if removed > 0:
        logger.info(f"🧹 تم تنظيف وإخلاء عدد ({removed}) من الملفات المؤقتة القديمة بنجاح من مساحة التخزين.")

# ----------------------------------------------------------------------
# إطلاق ومراقبة دورة حياة التطبيق الأساسي للبوت
# ----------------------------------------------------------------------

def main():
    request_config = HTTPXRequest(
        connect_timeout=30.0, 
        read_timeout=60.0, 
        write_timeout=30.0,
        pool_timeout=20.0
    )
    
    app = Application.builder().token(BOT_TOKEN).request(request_config).build()

    # ربط مهام بدء وإغلاق مجمع الاتصالات التابع لقاعدة البيانات بدورة حياة البوت الرسمية
    app.post_init = init_db_pool
    app.post_shutdown = close_db_pool

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", lambda u, c: u.message.reply_text("⚙️ لوحة التحكم", reply_markup=admin_keyboard()) if u.effective_user.id in ADMIN_IDS else None))
    app.add_handler(CallbackQueryHandler(menu_callback))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.AUDIO | filters.VOICE, handle_unified_document_and_audio))

    # جدولة تنظيف الملفات الدورية المؤقتة
    app.job_queue.run_repeating(cleanup_job, interval=CLEANUP_INTERVAL, first=60)

    logger.info("🚀 انطلق البوت رسمياً بأعلى كفاءة معمارية وحماية كاملة ضد الضغط واستنزاف الموارد...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
