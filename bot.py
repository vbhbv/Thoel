"""
بوت تليجرام متقدم لتحويل الصيغ، تعديل الـ Metadata للوسائط، وحماية ملفات PDF.
تمت إضافة ميزات: ضغط الـ PDF بشريط تقدم حقيقي، قص الصوت (Trim)، وتحويل النص إلى صوت (TTS).
مدمج بالكامل مع قاعدة بيانات PostgreSQL ونظام لوحة تحكم الآدمن التفاعلية (Inline Panel).
مصمم للنشر المستقر على Railway مع إدارة صارمة للذاكرة والتنظيف الدوري.
"""

import os
import time
import logging
import asyncio
from pathlib import Path
from datetime import datetime

import asyncpg  # مكتبة التعامل مع PostgreSQL بكفاءة وسرعة عالية
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

ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x.strip()]

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
CONVERTED_DIR = BASE_DIR / "converted"
DOWNLOADS_DIR.mkdir(exist_ok=True)
CONVERTED_DIR.mkdir(exist_ok=True)

FILE_MAX_AGE = 60 * 60  # ساعة واحدة
CLEANUP_INTERVAL = 30 * 60  # 30 دقيقة
MAX_FILE_SIZE_MB = 20

AUDIO_FORMATS = ["mp3", "m4a", "flac", "ogg", "wav", "aac"]
DOC_EXTENSIONS = {".doc", ".docx"}
PDF_EXTENSION = ".pdf"
EPUB_EXTENSION = ".epub"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".wma"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".3gp", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"}


# ----------------------------------------------------------------------
# منطق معالجة وقاعدة بيانات PostgreSQL
# ----------------------------------------------------------------------

def fix_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


async def init_db():
    url = fix_database_url(DATABASE_URL)
    try:
        conn = await asyncpg.connect(url)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_banned BOOLEAN DEFAULT FALSE
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stats_log (
                id SERIAL PRIMARY KEY,
                action_type TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.close()
        logger.info("✅ تم الاتصال بقاعدة البيانات PostgreSQL وتهيئة الجداول بنجاح.")
    except Exception as e:
        logger.error(f"❌ فشل تهيئة قاعدة البيانات: {e}")


async def register_user(user_id: int, username: str):
    try:
        conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
        await conn.execute(
            """
            INSERT INTO users (user_id, username) 
            VALUES ($1, $2) 
            ON CONFLICT (user_id) DO UPDATE SET username = $2;
            """,
            user_id, username
        )
        await conn.close()
    except Exception as e:
        logger.error(f"خطأ أثناء تسجيل المستخدم {user_id}: {e}")


async def is_user_banned(user_id: int) -> bool:
    try:
        conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
        row = await conn.fetchrow("SELECT is_banned FROM users WHERE user_id = $1;", user_id)
        await conn.close()
        return row["is_banned"] if row else False
    except Exception as e:
        logger.error(f"خطأ أثناء فحص حظر المستخدم: {e}")
        return False


async def log_action(action_type: str):
    try:
        conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
        await conn.execute("INSERT INTO stats_log (action_type) VALUES ($1);", action_type)
        await conn.close()
    except Exception as e:
        logger.error(f"خطأ أثناء تسجيل العملية {action_type}: {e}")


# ----------------------------------------------------------------------
# أدوات مساعدة للتشغيل والتحويل الفني والوسائط
# ----------------------------------------------------------------------

async def run_cmd(*args: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(errors="ignore"), stderr.decode(errors="ignore")


async def download_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str, file_unique_id: str, filename: str) -> Path:
    file_obj = await context.bot.get_file(file_id)
    ext = Path(filename).suffix or ""
    local_path = DOWNLOADS_DIR / f"{file_unique_id}{ext}"
    await file_obj.download_to_drive(custom_path=str(local_path))
    return local_path


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
        except Exception as img_err:
            logger.error(f"خطأ أثناء معالجة الصورة وتحويلها إلى JPEG: {img_err}")

    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, TIT2, TPE1, APIC, ID3NoHeaderError
            try:
                audio = ID3(str(audio_path))
            except ID3NoHeaderError:
                audio = ID3()
            if title: audio.add(TIT2(encoding=3, text=title))
            if artist: audio.add(TPE1(encoding=3, text=artist))
            if image_bytes:
                audio.delall("APIC")
                audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=image_bytes))
            audio.save(str(audio_path))

        elif ext in [".m4a", ".aac"]:
            from mutagen.mp4 import MP4, MP4Cover
            try:
                audio = MP4(str(audio_path))
                if title: audio["\xa9nam"] = title
                if artist: audio["\xa9ART"] = artist
                if image_bytes: audio["covr"] = [MP4Cover(image_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
                audio.save()
            except Exception as mp4_err:
                logger.error(f"فشلت معالجة حاوية MP4/M4A: {mp4_err}")

        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(str(audio_path))
            if title: audio["title"] = title
            if artist: audio["artist"] = artist
            if image_bytes:
                pic = Picture()
                pic.data = image_bytes
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.description = "Cover"
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()

        elif ext in [".ogg", ".opus"]:
            from mutagen.oggvorbis import OggVorbis
            import base64
            from mutagen.flac import Picture
            audio = OggVorbis(str(audio_path))
            if title: audio["title"] = title
            if artist: audio["artist"] = artist
            if image_bytes:
                pic = Picture()
                pic.data = image_bytes
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.description = "Cover"
                audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
            audio.save()
    except Exception as e:
        logger.error(f"خطأ غير متوقع in Mutagen للصيغة {ext}: {e}")


async def convert_docx_to_pdf(input_path: Path, out_dir: Path) -> Path:
    code, out, err = await run_cmd(
        "libreoffice", "--headless", "--norestore",
        "--convert-to", "pdf", "--outdir", str(out_dir), str(input_path)
    )
    result = out_dir / (input_path.stem + ".pdf")
    if code != 0 or not result.exists():
        raise RuntimeError(f"فشل تحويل Word إلى PDF: {err or out}")
    return result


async def convert_pdf_to_docx(input_path: Path, out_dir: Path) -> Path:
    output_path = out_dir / (input_path.stem + ".docx")
    def _convert():
        from pdf2docx import Converter
        cv = Converter(str(input_path))
        cv.convert(str(output_path))
        cv.close()
    await asyncio.get_running_loop().run_in_executor(None, _convert)
    if not output_path.exists():
        raise RuntimeError("فشل تحويل PDF إلى Word")
    return output_path


async def convert_audio(input_path: Path, out_dir: Path, target_format: str) -> Path:
    output_path = out_dir / (input_path.stem + f".{target_format}")
    code, out, err = await run_cmd(
        "ffmpeg", "-y", "-i", str(input_path),
        "-vn", "-ar", "44100", "-ac", "2",
        str(output_path)
    )
    if code != 0 or not output_path.exists():
        raise RuntimeError(f"فشل تحويل الصوت: {err[-500:]}")
    return output_path


async def convert_video_to_audio(input_path: Path, out_dir: Path) -> Path:
    output_path = out_dir / (input_path.stem + ".mp3")
    code, out, err = await run_cmd(
        "ffmpeg", "-y", "-i", str(input_path),
        "-vn", "-acodec", "libmp3lame", "-q:a", "2",
        "-ar", "44100", "-ac", "2",
        str(output_path)
    )
    if code != 0 or not output_path.exists():
        raise RuntimeError(f"فشل استخراج الصوت من الفيديو: {err[-500:]}")
    return output_path


async def convert_images_to_pdf(input_paths: list[Path], out_dir: Path, base_name: str) -> Path:
    output_path = out_dir / (base_name + ".pdf")
    def _convert():
        from PIL import Image
        import img2pdf
        processed_paths = []
        for path in input_paths:
            with Image.open(path) as img:
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB")
                tmp_path = path.with_suffix(".conv.jpg")
                img.save(tmp_path, "JPEG", quality=95)
                processed_paths.append(str(tmp_path))
        pdf_bytes = img2pdf.convert(processed_paths)
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)
        for p in processed_paths: Path(p).unlink(missing_ok=True)
    await asyncio.get_running_loop().run_in_executor(None, _convert)
    return output_path


async def convert_images_to_docx(input_paths: list[Path], out_dir: Path, base_name: str) -> Path:
    output_path = out_dir / (base_name + ".docx")
    def _convert():
        from docx import Document
        from docx.shared import Inches
        from PIL import Image
        doc = Document()
        for path in input_paths:
            with Image.open(path) as img:
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB")
                    fixed_path = path.with_suffix(".conv.jpg")
                    img.save(fixed_path, "JPEG", quality=95)
                    source = fixed_path
                else:
                    source = path
            doc.add_picture(str(source), width=Inches(6))
            if source != path: source.unlink(missing_ok=True)
        doc.save(str(output_path))
    await asyncio.get_running_loop().run_in_executor(None, _convert)
    return output_path


def encrypt_pdf_file(input_path: Path, output_path: Path, password: str):
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(input_path))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    with open(output_path, "wb") as f:
        writer.write(f)


def make_progress_bar(percent: int) -> str:
    """توليد شريط تقدم رسومي يعتمد على النسبة المئوية الممررة"""
    total_blocks = 10
    filled_blocks = int(percent / 10)
    empty_blocks = total_blocks - filled_blocks
    bar = "█" * filled_blocks + "░" * empty_blocks
    return f"{bar} {percent}%"


async def compress_pdf_file_async(input_path: Path, output_path: Path, update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg):
    """الميزة الثانية المحدثة: ضغط ملف PDF لتقليل الحجم بشكل كبير مع تفعيل شريط تقدم حقيقي آمن"""
    from pypdf import PdfReader, PdfWriter
    
    reader = PdfReader(str(input_path))
    writer = PdfWriter()
    total_pages = len(reader.pages)
    
    last_update_time = time.time()
    
    for idx, page in enumerate(reader.pages, start=1):
        try:
            # ضغط محتوى الصفحة الداخلي
            page.compress_content_streams()
        except Exception as page_err:
            # استثناء وحماية كاملة ضد انهيار البنية الداخلية للصفحات المتضررة
            logger.warning(f"تم تخطي ضغط تيارات الصفحة {idx} لتجنب الانهيار: {page_err}")
            
        writer.add_page(page)
        
        # تحديث شريط التقدم التفاعلي كل صفحة (بشرط مرور ثانية على الأقل لتفادي حظر التليجرام Flood)
        percent = int((idx / total_pages) * 100)
        current_time = time.time()
        if current_time - last_update_time >= 1.5 or idx == total_pages:
            p_bar = make_progress_bar(percent)
            try:
                await status_msg.edit_text(
                    f"⏳ **جاري معالجة وضغط صفحات الـ PDF...**\n\n"
                    f"📄 الصفحة: `{idx}` من `{total_pages}`\n"
                    f"`{p_bar}`",
                    parse_mode="Markdown"
                )
                last_update_time = current_time
            except Exception:
                pass
                
    # حفظ وتخزين الملف النهائي على السيرفر سحابياً
    def _write_file():
        with open(output_path, "wb") as f:
            writer.write(f)
            
    await asyncio.get_running_loop().run_in_executor(None, _write_file)


async def trim_audio_file(input_path: Path, output_path: Path, start_time: str, end_time: str):
    """الميزة الثالثة: قص مقطع صوتي باستخدام ffmpeg عبر التوقيت المرسل"""
    code, out, err = await run_cmd(
        "ffmpeg", "-y", "-ss", start_time, "-to", end_time, 
        "-i", str(input_path), "-acodec", "copy", str(output_path)
    )
    if code != 0 or not output_path.exists():
        raise RuntimeError(f"فشل قص الصوت، تأكد من صحة كتابة الوقت المتطابق: {err[-300:]}")


async def process_epub_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    msg = await update.message.reply_text("⏳ جاري تحويل الكتاب الإلكتروني...")
    try:
        if not is_calibre_available():
            await msg.edit_text("❌ برمجية Calibre غير متوفرة على البيئة السحابية حاليًا.")
            return
        lp = await download_telegram_file(context, tg_file.file_id, tg_file.file_unique_id, filename)
        res = await convert_epub_to_pdf(lp, CONVERTED_DIR)
        await log_action("epub_to_pdf")
        with open(res, "rb") as f:
            await update.message.reply_document(document=f, filename=res.name)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ خطأ: {e}")


async def finalize_and_send_audio(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    audio_path_str = context.user_data.get("ready_audio_path")
    if not audio_path_str:
        await context.bot.send_message(chat_id=chat_id, text="❌ لم يتم العثور على ملف جاهز للبث.")
        return

    audio_path = Path(audio_path_str)
    title = context.user_data.get("meta_title")
    artist = context.user_data.get("meta_artist")
    art_path_str = context.user_data.get("meta_art_path")
    art_path = Path(art_path_str) if art_path_str else None

    await asyncio.get_running_loop().run_in_executor(None, apply_audio_metadata, audio_path, title, artist, art_path)

    with open(audio_path, "rb") as f:
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=f,
            title=title if title else audio_path.stem,
            performer=artist if artist else "فنان غير معروف",
            caption="✅ تم تحديث الـ Tags وحقن الغلاف بنجاح!"
        )

    for key in ["audio_state", "ready_audio_path", "meta_title", "meta_artist", "meta_art_path", "pending_audio", "pending_video"]:
        context.user_data.pop(key, None)


# ----------------------------------------------------------------------
# كيبورد اللوحات والقوائم التفاعلية
# ----------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📂 أدوات تعديل الملفات", callback_data="sub_files")],
        [InlineKeyboardButton("🎵 أدوات تعديل الصوتيات", callback_data="sub_audio")]
    ]
    return InlineKeyboardMarkup(buttons)


def files_submenu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📄 Word ➜ PDF", callback_data="mode_word2pdf")],
        [InlineKeyboardButton("📄 PDF ➜ Word", callback_data="mode_pdf2word")],
        [InlineKeyboardButton("📚 EPUB ➜ PDF", callback_data="mode_ebook")],
        [InlineKeyboardButton("🖼️ تحويل صور إلى PDF/Word", callback_data="mode_image")],
        [InlineKeyboardButton("🔒 تشفير حماية الـ PDF", callback_data="mode_encrypt_pdf")],
        [InlineKeyboardButton("🗜️ ضغط ملف PDF (تقليل الحجم)", callback_data="mode_compress_pdf")],
        [InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(buttons)


def audio_submenu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🎬 فيديو ➜ صوت MP3", callback_data="mode_video2audio")],
        [InlineKeyboardButton("🎵 تحويل صيغة صوتية", callback_data="mode_audio")],
        [InlineKeyboardButton("✂️ قص مقطع صوتي (Trim)", callback_data="mode_trim_audio")],
        [InlineKeyboardButton("🗣️ تحويل نص إلى صوت (TTS)", callback_data="mode_tts")],
        [InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="back_to_main")]
    ]
    return InlineKeyboardMarkup(buttons)


# ----------------------------------------------------------------------
# لوحة تحكم وإدارة الآدمن (Inline Admin Panel)
# ----------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📊 إحصائيات البوت", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 إذاعة جماعية (Broadcast)", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🚫 حظر مستخدم", callback_data="admin_ban"),
         InlineKeyboardButton("🟢 إلغاء حظر", callback_data="admin_unban")]
    ]
    return InlineKeyboardMarkup(buttons)


async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "⚙️ **لوحة تحكم الإدارة وقاعدة البيانات الاحترافية**\nاختر من الأزرار الإجراء المطلوب:",
        reply_markup=admin_keyboard(),
        parse_mode="Markdown"
    )


import files_handler
import audio_handler


# ----------------------------------------------------------------------
# معالجة الرسائل والـ Callbacks الشاملة
# ----------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.username)
    if await is_user_banned(user.id):
        await update.message.reply_text("🚫 نعتذر، حسابك محظور حاليًا.")
        return
    context.user_data.clear()
    await update.message.reply_text("👋 أهلًا بك في بوت تحويل الصيغ والوسائط المحترف!\n💡 اختر القسم المطلوب للبدء:", reply_markup=main_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if await is_user_banned(user_id): return

    await query.answer()
    data = query.data

    if data == "sub_files":
        await query.edit_message_text("📂 **قسم أدوات تعديل الملفات:**\nاختر الأداة المطلوبة:", reply_markup=files_submenu_keyboard(), parse_mode="Markdown")
        return
    elif data == "sub_audio":
        await query.edit_message_text("🎵 **قسم أدوات تعديل الصوتيات:**\nاختر الأداة المطلوبة:", reply_markup=audio_submenu_keyboard(), parse_mode="Markdown")
        return
    elif data == "back_to_main":
        await query.edit_message_text("👋 اختر القسم المطلوب من الأزرار أدناه للبدء:", reply_markup=main_menu_keyboard())
        return

    # معالجة الآدمن الفورية
    if data.startswith("admin_"):
        if not is_admin(user_id): return
        if data == "admin_stats":
            conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users;")
            banned_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = TRUE;")
            total_actions = await conn.fetchval("SELECT COUNT(*) FROM stats_log;")
            await conn.close()
            await query.edit_message_text(f"📊 **الإحصائيات:**\n👥 مستخدمين: `{total_users}`\n🚫 محظورين: `{banned_users}`\n⚙️ عمليات ناجحة: `{total_actions}`", reply_markup=admin_keyboard(), parse_mode="Markdown")
        elif data == "admin_broadcast":
            context.user_data["admin_state"] = "WAITING_BROADCAST_MSG"
            await query.edit_message_text("📢 أرسل الآن رسالة الإذاعة:")
        elif data == "admin_ban":
            context.user_data["admin_state"] = "WAITING_BAN_ID"
            await query.edit_message_text("🚫 أرسل الـ User ID لحظره:")
        elif data == "admin_unban":
            context.user_data["admin_state"] = "WAITING_UNBAN_ID"
            await query.edit_message_text("🟢 أرسل الـ User ID لإلغاء حظره:")
        return

    # تفعيل المودات المختارة وتوجيه المستخدم
    context.user_data["current_mode"] = data
    if data == "mode_word2pdf":
        await query.edit_message_text("📄 أرسل الآن ملف Word (.doc أو .docx) لتحويله إلى PDF.")
    elif data == "mode_pdf2word":
        await query.edit_message_text("📄 أرسل الآن ملف PDF لتحويله إلى Word.")
    elif data == "mode_video2audio":
        await query.edit_message_text("🎬 أرسل ملف الفيديو لاستخراج الصوت منه بصيغة MP3.")
    elif data == "mode_ebook":
        await query.edit_message_text("📚 أرسل ملف EPUB ليتم تحويله تلقائيًا إلى PDF.")
    elif data == "mode_audio":
        await query.edit_message_text("🎵 أرسل الملف الصوتي المراد تعديله أو تحويل صيغته.")
    elif data == "mode_image":
        await query.edit_message_text("🖼️ أرسل الصورة أو مجموعة الصور المراد تجميعها.")
    elif data == "mode_encrypt_pdf":
        await query.edit_message_text("🔒 أرسل الآن ملف PDF لحمايته وتشفيره بكلمة مرور.")
    elif data == "mode_compress_pdf":
        await query.edit_message_text("🗜️ أرسل ملف الـ PDF الذي تود ضغطه وتقليص حجمه الآن.")
    elif data == "mode_trim_audio":
        await query.edit_message_text("✂️ أرسل أولاً الملف الصوتي المراد قصه.")
    elif data == "mode_tts":
        context.user_data["audio_state"] = "WAITING_TTS_TEXT"
        await query.edit_message_text("🗣️ أرسل الآن النص (بالعربية أو الإنجليزية) لتحويله إلى مقطع صوتي مسموع.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await is_user_banned(user_id): return
    mode = context.user_data.get("current_mode")

    # معالجة ضغط PDF (الميزة الثانية المحمية والمزودة بشريط تقدم حقيقي)
    if mode == "mode_compress_pdf" and update.message.document.file_name.lower().endswith('.pdf'):
        msg = await update.message.reply_text("⏳ جاري تهيئة وتحميل ملف الـ PDF لبدء الضغط الحركي...")
        doc = update.message.document
        
        try:
            lp = await download_telegram_file(context, doc.file_id, doc.file_unique_id, doc.file_name)
            out_p = CONVERTED_DIR / f"compressed_{doc.file_name}"
            
            # استدعاء دالة المعالجة المحدثة
            await compress_pdf_file_async(lp, out_p, update, context, msg)
            
            await log_action("compress_pdf")
            with open(out_p, "rb") as f:
                await update.message.reply_document(document=f, filename=out_p.name, caption="✅ تم ضغط الملف وتقليل الحجم بنجاح!")
        except Exception as e:
            logger.error(f"فشل ضغط ملف PDF بالكامل: {e}")
            await update.message.reply_text(f"❌ تعذر ضغط هذا الملف بالكامل بسبب قيود هيكلية لبنيته الداخلية.")
        finally:
            await msg.delete()
        return

    # معالجة قص الصوت في حال أرسل كملف وثيقة (الميزة الثالثة)
    if mode == "mode_trim_audio" and (Path(update.message.document.file_name).suffix.lower() in AUDIO_EXTENSIONS):
        doc = update.message.document
        lp = await download_telegram_file(context, doc.file_id, doc.file_unique_id, doc.file_name)
        context.user_data["trim_source_path"] = str(lp)
        context.user_data["audio_state"] = "WAITING_TRIM_TIME"
        await update.message.reply_text("⏱️ ممتاز، أرسل الآن توقيت القص بالصيغة التالية تماماً:\n`00:01:10 - 00:02:45`\n(أي من الدقيقة 1 و10 ثواني إلى الدقيقة 2 و45 ثانية)", parse_mode="Markdown")
        return

    is_handled = await files_handler.handle_files_document(update, context)
    if not is_handled:
        await audio_handler.handle_audio_document(update, context)


async def handle_audio_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """توجيه الرسائل الصوتية للقص أو التعديل العادي"""
    if await is_user_banned(update.effective_user.id): return
    mode = context.user_data.get("current_mode")
    
    if mode == "mode_trim_audio":
        audio = update.message.audio or update.message.voice
        lp = await download_telegram_file(context, audio.file_id, audio.file_unique_id, getattr(audio, 'file_name', 'voice.ogg'))
        context.user_data["trim_source_path"] = str(lp)
        context.user_data["audio_state"] = "WAITING_TRIM_TIME"
        await update.message.reply_text("⏱️ ممتاز، أرسل الآن توقيت القص بالصيغة التالية تماماً:\n`00:01:10 - 00:02:45`", parse_mode="Markdown")
        return
        
    await audio_handler.handle_audio_message(update, context)


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await is_user_banned(user_id): return

    admin_state = context.user_data.get("admin_state")
    state = context.user_data.get("audio_state")
    pdf_state = context.user_data.get("pdf_state")
    text = update.message.text.strip()

    # معالجة النص الموجه لـ TTS (الميزة الرابعة)
    if state == "WAITING_TTS_TEXT":
        context.user_data.pop("audio_state", None)
        msg = await update.message.reply_text("⏳ جاري توليد مقطع الصوت من النص النطقي الفصيح...")
        try:
            from gtts import gTTS
            out_p = CONVERTED_DIR / f"tts_{update.message.message_id}.mp3"
            # فحص تلقائي للغة: إذا احتوى على حروف عربية ينطق بالعربية وإلا بالإنجليزية
            lang = 'ar' if any(u'\u0600' <= c <= u'\u06FF' for c in text) else 'en'
            
            def _generate_tts():
                tts = gTTS(text=text, lang=lang, slow=False)
                tts.save(str(out_p))
            
            await asyncio.get_running_loop().run_in_executor(None, _generate_tts)
            await log_action("tts_generated")
            with open(out_p, "rb") as f:
                await update.message.reply_audio(audio=f, caption="🗣️ تم توليد النطق الصوتي التلقائي بنجاح!")
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ فشل توليد الصوت: {e}")
        return

    # معالجة توقيت قص الصوت (الميزة الثالثة)
    if state == "WAITING_TRIM_TIME":
        if " - " not in text:
            await update.message.reply_text("⚠️ يرجى إرسال التوقيت بشكل صحيح متضمناً الفاصلة الوسطية، مثال:\n`00:00:10 - 00:00:40`", parse_mode="Markdown")
            return
        context.user_data.pop("audio_state", None)
        parts = text.split(" - ")
        start_t, end_t = parts[0].strip(), parts[1].strip()
        src_path = context.user_data.get("trim_source_path")
        
        if not src_path:
            await update.message.reply_text("❌ حدث خطأ، لم يتم العثور على الملف الصوتي الأصلي.")
            return
            
        msg = await update.message.reply_text("⏳ جاري قطع المقطع المحدد بدقة متناهية عبر ffmpeg...")
        src_p = Path(src_path)
        out_p = CONVERTED_DIR / f"trimmed_{src_p.name}"
        
        try:
            await trim_audio_file(src_p, out_p, start_t, end_t)
            await log_action("audio_trim")
            with open(out_p, "rb") as f:
                await update.message.reply_audio(audio=f, caption=f"✂️ تم قص المقطع بنجاح من {start_t} إلى {end_t}!")
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ خطأ: {e}")
        return

    # باقي معالجات الآدمن وتعديل الميتاداتا
    if admin_state and is_admin(user_id):
        if admin_state == "WAITING_BROADCAST_MSG":
            context.user_data.pop("admin_state", None)
            status_msg = await update.message.reply_text("⏳ جاري بدء الإذاعة الجماعية...")
            conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
            rows = await conn.fetch("SELECT user_id FROM users WHERE is_banned = FALSE;")
            await conn.close()
            success, failed = 0, 0
            for row in rows:
                if row["user_id"] == user_id: continue
                try:
                    await context.bot.copy_message(chat_id=row["user_id"], from_chat_id=update.message.chat_id, message_id=update.message.message_id)
                    success += 1
                    await asyncio.sleep(0.04)
                except: failed += 1
            await status_msg.edit_text(f"📢 **اكتملت الإذاعة!**\n✅ تم إرسال لـ: `{success}`\n❌ فشل لـ: `{failed}`", parse_mode="Markdown")
            return
        elif admin_state == "WAITING_BAN_ID":
            context.user_data.pop("admin_state", None)
            conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
            await conn.execute("UPDATE users SET is_banned = TRUE WHERE user_id = $1;", int(text))
            await conn.close()
            await update.message.reply_text("✅ تم حظر المستخدم بنجاح.")
            return
        elif admin_state == "WAITING_UNBAN_ID":
            context.user_data.pop("admin_state", None)
            conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
            await conn.execute("UPDATE users SET is_banned = FALSE WHERE user_id = $1;", int(text))
            await conn.close()
            await update.message.reply_text("🟢 تم إلغاء حظر المستخدم.")
            return

    if pdf_state == "WAITING_PASSWORD":
        await files_handler.process_pdf_encryption(update, context, text)
        return

    if state == "WATING_TITLE":
        context.user_data["meta_title"] = text
        context.user_data["audio_state"] = "WATING_ARTIST"
        await update.message.reply_text("👤 ممتاز، أرسل الآن **اسم الفنان**:", reply_markup=audio_handler.metadata_skip_keyboard())
    elif state == "WATING_ARTIST":
        context.user_data["meta_artist"] = text
        context.user_data["audio_state"] = "WATING_ART"
        await update.message.reply_text("🖼️ أرسل الآن **صورة الغلاف**:", reply_markup=audio_handler.metadata_skip_keyboard())


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_user_banned(update.effective_user.id): return
    if context.user_data.get("audio_state") == "WATING_ART":
        await audio_handler.handle_photo_as_art(update, context, update.message.photo[-1])
        return
    await files_handler.handle_files_photo(update, context)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    removed = 0
    for folder in (DOWNLOADS_DIR, CONVERTED_DIR):
        for path in folder.glob("*"):
            try:
                if path.is_file() and (now - path.stat().st_mtime) > FILE_MAX_AGE:
                    path.unlink()
                    removed += 1
            except OSError: pass
    if removed: logger.info(f"🧹 تم تنظيف {removed} ملف مؤقت.")


# ----------------------------------------------------------------------
# دالة بدء التشغيل الأساسية للبوت وإطلاق قاعدة البيانات
# ----------------------------------------------------------------------

def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())

    request_config = HTTPXRequest(connect_timeout=30.0, read_timeout=60.0, write_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(request_config).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel_command))
    app.add_handler(CallbackQueryHandler(menu_callback))
    
    files_handler.register_files_handlers(app)
    audio_handler.register_audio_handlers(app)
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio_message_router))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, audio_handler.handle_video_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.job_queue.run_repeating(cleanup_job, interval=CLEANUP_INTERVAL, first=CLEANUP_INTERVAL)

    logger.info("🚀 البوت انطلق رسميًا بكافة ميزات الضغط، القص والـ TTS المضافة...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
    main()
