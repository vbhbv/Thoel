"""
بوت تليجرام متقدم لتحويل الصيغ، تعديل الـ Metadata للوسائط، وحماية ملفات PDF.
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
        logger.error(f"خطأ غير متوقع في Mutagen للصيغة {ext}: {e}")


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
            caption="✅ تم تحديث الـ Tags وحقن الغلاف بنجاح عبر Mutagen!"
        )

    for key in ["audio_state", "ready_audio_path", "meta_title", "meta_artist", "meta_art_path", "pending_audio", "pending_video"]:
        context.user_data.pop(key, None)


# ----------------------------------------------------------------------
# كيبورد اللوحات والقوائم التفاعلية للمخدمين (تم التعديل بطلبك)
# ----------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        # القسم الأول للمستندات والملفات
        [InlineKeyboardButton("📂 --- قسم أدوات تعديل الملفات ---", callback_data="ignore_section")],
        [InlineKeyboardButton("📄 Word ➜ PDF", callback_data="mode_word2pdf")],
        [InlineKeyboardButton("📄 PDF ➜ Word", callback_data="mode_pdf2word")],
        [InlineKeyboardButton("📚 EPUB ➜ PDF", callback_data="mode_ebook")],
        [InlineKeyboardButton("🖼️ تحويل صور إلى PDF/Word", callback_data="mode_image")],
        [InlineKeyboardButton("🔒 تشفير حماية الـ PDF", callback_data="mode_encrypt_pdf")],
        
        # القسم الثاني للصوتيات والوسائط
        [InlineKeyboardButton("🎵 --- قسم أدوات تعديل الصوتيات ---", callback_data="ignore_section")],
        [InlineKeyboardButton("🎬 فيديو ➜ صوت MP3", callback_data="mode_video2audio")],
        [InlineKeyboardButton("🎵 تحويل صيغة صوتية", callback_data="mode_audio")]
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


# استيراد موديولات التقسيم بعد ثبات تعريفات ثوابت ودوال bot المشتركة منعاً للـ Circular Import
import files_handler
import audio_handler


# ----------------------------------------------------------------------
# معالجة الرسائل والـ Callbacks الشاملة
# ----------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.username)
    
    if await is_user_banned(user.id):
        await update.message.reply_text("🚫 نعتذر، حسابك محظور حاليًا من استخدام خدمات البوت.")
        return

    context.user_data.clear()
    text = (
        "👋 أهلًا بك في بوت تحويل الصيغ المتقدم وتعديل وسائط الميديا المحترف!\n\n"
        "💡 أرسل أي ملف مباشرة (فيديو، صوت، مستند، صور) وسيتعامل معه البوت تلقائيًا وبذكاء هندسي عالي."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if await is_user_banned(user_id):
        await query.answer("🚫 حسابك محظور.", show_alert=True)
        return

    if query.data == "ignore_section":
        await query.answer("💡 هذا عنوان للقسم فقط، اختر أحد الأزرار بالأسفل.", show_alert=False)
        return

    await query.answer()
    data = query.data

    if data.startswith("admin_"):
        if not is_admin(user_id): return
        
        if data == "admin_stats":
            await query.edit_message_text("⏳ جاري جلب الإحصائيات الحية من PostgreSQL...")
            try:
                conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
                total_users = await conn.fetchval("SELECT COUNT(*) FROM users;")
                banned_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = TRUE;")
                total_actions = await conn.fetchval("SELECT COUNT(*) FROM stats_log;")
                await conn.close()

                stats_text = (
                    "📊 **إحصائيات النظام الحالية:**\n\n"
                    f"👥 إجمالي المستخدمين المسجلين: `{total_users}`\n"
                    f"🚫 عدد المستخدمين المحظورين: `{banned_users}`\n"
                    f"⚙️ إجمالي العمليات الناجحة: `{total_actions}`\n"
                )
                await query.edit_message_text(stats_text, reply_markup=admin_keyboard(), parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"❌ خطأ الإحصائيات: {e}", reply_markup=admin_keyboard())
        
        elif data == "admin_broadcast":
            context.user_data["admin_state"] = "WAITING_BROADCAST_MSG"
            await query.edit_message_text("📢 أرسل الآن الرسالة أو الملف الذي تود إذاعته جماعيًا لجميع المستخدمين:")
        
        elif data == "admin_ban":
            context.user_data["admin_state"] = "WAITING_BAN_ID"
            await query.edit_message_text("🚫 أرسل الـ `User ID` الخاص بالمستخدم المراد حظره:")
        
        elif data == "admin_unban":
            context.user_data["admin_state"] = "WAITING_UNBAN_ID"
            await query.edit_message_text("🟢 أرسل الـ `User ID` الخاص بالمستخدم لإلغاء الحظر عنه:")
        return

    if data == "mode_word2pdf":
        await query.edit_message_text("📄 أرسل الآن ملف Word (.doc أو .docx) لتحويله إلى PDF.")
    elif data == "mode_pdf2word":
        await query.edit_message_text("📄 أرسل الآن ملف PDF لتحويله إلى Word.")
    elif data == "mode_video2audio":
        await query.edit_message_text("🎬 أرسل الآن ملف الفيديو لاستخراج الصوت منه بصيغة MP3 نقي.")
    elif data == "mode_ebook":
        await query.edit_message_text("📚 أرسل الآن ملف EPUB ليتم تحويله تلقائيًا إلى PDF.")
    elif data == "mode_audio":
        await query.edit_message_text("🎵 أرسل الآن الملف الصوتي المراد تعديله أو تحويل صيغته.")
    elif data == "mode_image":
        await query.edit_message_text("🖼️ أرسل الآن الصورة أو مجموعة الصور المراد تجميعها.")
    elif data == "mode_encrypt_pdf":
        await query.edit_message_text("🔒 أرسل الآن ملف PDF لحمايته وتشفيره بكلمة مرور.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.username)
    if await is_user_banned(user.id): return

    if context.user_data.get("audio_state") == "WATING_ART":
        await audio_handler.handle_photo_as_art(update, context, update.message.document)
        return

    is_handled = await files_handler.handle_files_document(update, context)
    if not is_handled:
        await audio_handler.handle_audio_document(update, context)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.username)
    if await is_user_banned(user.id): return

    if context.user_data.get("audio_state") == "WATING_ART":
        await audio_handler.handle_photo_as_art(update, context, update.message.photo[-1])
        return

    await files_handler.handle_files_photo(update, context)


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = update.effective_user
    await register_user(user.id, user.username)
    if await is_user_banned(user_id): return

    admin_state = context.user_data.get("admin_state")
    state = context.user_data.get("audio_state")
    pdf_state = context.user_data.get("pdf_state")
    text = update.message.text.strip()

    if admin_state and is_admin(user_id):
        if admin_state == "WAITING_BROADCAST_MSG":
            context.user_data.pop("admin_state", None)
            status_msg = await update.message.reply_text("⏳ جاري بدء الإذاعة الجماعية لكافة المستخدمين النشطين...")
            try:
                conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
                rows = await conn.fetch("SELECT user_id FROM users WHERE is_banned = FALSE;")
                await conn.close()
            except Exception as e:
                await status_msg.edit_text(f"❌ فشل جلب المشتركين من القاعدة: {e}")
                return

            success, failed = 0, 0
            for row in rows:
                target_id = row["user_id"]
                if target_id == user_id: continue
                try:
                    await context.bot.copy_message(chat_id=target_id, from_chat_id=update.message.chat_id, message_id=update.message.message_id)
                    success += 1
                    await asyncio.sleep(0.05)
                except Exception:
                    failed += 1

            await status_msg.edit_text(f"📢 **اكتملت الإذاعة الجماعية بنجاح!**\n\n✅ تم الإرسال إلى: `{success}`\n❌ فشل الإرسال إلى: `{failed}`", parse_mode="Markdown")
            return

        elif admin_state == "WAITING_BAN_ID":
            context.user_data.pop("admin_state", None)
            if not text.isdigit():
                await update.message.reply_text("⚠️ المعرف يجب أن يكون رقمًا فقط.")
                return
            try:
                conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
                await conn.execute("UPDATE users SET is_banned = TRUE WHERE user_id = $1;", int(text))
                await conn.close()
                await update.message.reply_text(f"✅ تم حظر المستخدم `{text}` بنجاح ومنعه من البوت.", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ خطأ أثناء الحظر: {e}")
            return

        elif admin_state == "WAITING_UNBAN_ID":
            context.user_data.pop("admin_state", None)
            if not text.isdigit():
                await update.message.reply_text("⚠️ المعرف يجب أن يكون رقمًا فقط.")
                return
            try:
                conn = await asyncpg.connect(fix_database_url(DATABASE_URL))
                await conn.execute("UPDATE users SET is_banned = FALSE WHERE user_id = $1;", int(text))
                await conn.close()
                await update.message.reply_text(f"🟢 تم إلغاء حظر المستخدم `{text}` بنجاح وعاد للوضع النشط.", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ خطأ إلغاء الحظر: {e}")
            return

    if pdf_state == "WAITING_PASSWORD":
        await files_handler.process_pdf_encryption(update, context, text)
        return

    if not state: return

    if state == "WATING_TITLE":
        context.user_data["meta_title"] = text
        context.user_data["audio_state"] = "WATING_ARTIST"
        await update.message.reply_text("👤 ممتاز، أرسل الآن **اسم الفنان** (أو المغني):", reply_markup=audio_handler.metadata_skip_keyboard())
    elif state == "WATING_ARTIST":
        context.user_data["meta_artist"] = text
        context.user_data["audio_state"] = "WATING_ART"
        await update.message.reply_text("🖼️ أرسل الآن **صورة الغلاف** المرجوة (كصورة أو كملف):", reply_markup=audio_handler.metadata_skip_keyboard())


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
    if removed:
        logger.info(f"🧹 تم حذف {removed} من الملفات المؤقتة القديمة بنجاح.")


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
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, audio_handler.handle_audio_message))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, audio_handler.handle_video_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.job_queue.run_repeating(cleanup_job, interval=CLEANUP_INTERVAL, first=CLEANUP_INTERVAL)

    logger.info("🚀 البوت انطلق رسميًا بدعم PostgreSQL ولوحة التحكم المتكاملة الحية...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
    main()
