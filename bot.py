"""
بوت تليجرام لتحويل الصيغ وتعديل الـ Metadata للصوتيات والمستندات والفيديوهات
- Word <-> PDF
- تحويل الفيديو إلى صوت MP3
- تحويل الصوت لعدة صيغ + تعديل البيانات (العنوان، الفنان، البوستر) باستخدام Mutagen
- تحويل الصور (المجمعة والمفردة) إلى PDF أو Word
- حماية ملفات PDF وتشفيرها بكلمة سر باستخدام pypdf
مصمم للنشر على Railway مع إدارة صارمة للذاكرة والتنظيف الدوري
"""

import os
import time
import logging
import asyncio
from pathlib import Path

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
# الإعدادات العامة
# ----------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("يجب ضبط متغير البيئة BOT_TOKEN")

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
# أدوات مساعدة للتشغيل
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


# ----------------------------------------------------------------------
# منطق الـ Metadata المتقدم والأغلفة النظيفة باستخدام Mutagen & Pillow
# ----------------------------------------------------------------------

def apply_audio_metadata(audio_path: Path, title: str = None, artist: str = None, album_art_path: Path = None):
    """
    تعديل الـ tags وحقن الغلاف بدقة لكل صيغة صوتية بعد معالجة الصورة وتنظيف الـ tags القديمة.
    """
    from PIL import Image
    from io import BytesIO
    
    ext = audio_path.suffix.lower()
    image_bytes = None
    
    # توحيد صيغة الصورة إلى JPEG قياسي مهما كان المصدر (PNG, WebP، إلخ) لمنع المشاكل في المشغلات
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
        # 1. معالجة ملفات MP3
        if ext == ".mp3":
            from mutagen.id3 import ID3, TIT2, TPE1, APIC, ID3NoHeaderError
            try:
                audio = ID3(str(audio_path))
            except ID3NoHeaderError:
                audio = ID3()
            
            if title:
                audio.add(TIT2(encoding=3, text=title))
            if artist:
                audio.add(TPE1(encoding=3, text=artist))
            
            if image_bytes:
                # حذف ألبومات الـ APIC القديمة لتجنب تراكم أو تضارب الصور المكررة
                audio.delall("APIC")
                audio.add(APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,  # Front Cover
                    desc="Cover",
                    data=image_bytes
                ))
            audio.save(str(audio_path))

        # 2. معالجة ملفات M4A / AAC
        elif ext in [".m4a", ".aac"]:
            from mutagen.mp4 import MP4, MP4Cover
            try:
                audio = MP4(str(audio_path))
                if title:
                    audio["\xa9nam"] = title
                if artist:
                    audio["\xa9ART"] = artist
                if image_bytes:
                    audio["covr"] = [MP4Cover(image_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
                audio.save()
            except Exception as mp4_err:
                logger.error(f"فشلت معالجة حاوية MP4/M4A: {mp4_err}")

        # 3. معالجة ملفات FLAC
        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(str(audio_path))
            if title:
                audio["title"] = title
            if artist:
                audio["artist"] = artist
            if image_bytes:
                pic = Picture()
                pic.data = image_bytes
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.description = "Cover"
                audio.clear_pictures()  # تنظيف كلي للصور السابقة
                audio.add_picture(pic)
            audio.save()

        # 4. معالجة ملفات OGG / OPUS
        elif ext in [".ogg", ".opus"]:
            from mutagen.oggvorbis import OggVorbis
            import base64
            from mutagen.flac import Picture
            
            audio = OggVorbis(str(audio_path))
            if title:
                audio["title"] = title
            if artist:
                audio["artist"] = artist
            if image_bytes:
                pic = Picture()
                pic.data = image_bytes
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.description = "Cover"
                # تنظيف وحقن الصورة مشفرة Base64 ككتلة بيانات متوافقة مع قواعد Vorbis
                audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
            audio.save()

        # 5. ملفات WAV
        elif ext == ".wav":
            if title or artist:
                logger.info("صيغة WAV لا تدعم الأغلفة بشكل قياسي، تم تخطي معالجة الغلاف.")

    except Exception as e:
        logger.error(f"خطأ غير متوقع أثناء معالجة بيانات الصوت عبر Mutagen للصيغة {ext}: {e}")


# ----------------------------------------------------------------------
# منطق عمليات التحويل الفنية والوسائط
# ----------------------------------------------------------------------

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

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _convert)
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
    """
    استخراج المسار الصوتي من ملف الفيديو وتحويله إلى صيغة MP3 نقية مباشرة.
    """
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

        for p in processed_paths:
            Path(p).unlink(missing_ok=True)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _convert)
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
            if source != path:
                source.unlink(missing_ok=True)

        doc.save(str(output_path))

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _convert)
    return output_path


def encrypt_pdf_file(input_path: Path, output_path: Path, password: str):
    """
    تشفير ملف الـ PDF وحمايته بكلمة سر باستخدام حزمة pypdf.
    """
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(input_path))
    writer = PdfWriter()

    for page in reader.pages:
        writer.add_page(page)

    writer.encrypt(password)
    with open(output_path, "wb") as f:
        writer.write(f)


# ----------------------------------------------------------------------
# اللوحات وأزرار التحكم التفاعلية
# ----------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📄 Word ➜ PDF", callback_data="mode_word2pdf")],
        [InlineKeyboardButton("📄 PDF ➜ Word", callback_data="mode_pdf2word")],
        [InlineKeyboardButton("🎬 فيديو ➜ صوت MP3", callback_data="mode_video2audio")],
        [InlineKeyboardButton("🎵 تحويل صيغة صوتية", callback_data="mode_audio")],
        [InlineKeyboardButton("📚 EPUB ➜ PDF", callback_data="mode_ebook")],
        [InlineKeyboardButton("🖼️ تحويل صور إلى PDF/Word", callback_data="mode_image")],
        [InlineKeyboardButton("🔒 تشفير حماية الـ PDF", callback_data="mode_encrypt_pdf")],
    ]
    return InlineKeyboardMarkup(buttons)


def audio_format_keyboard() -> InlineKeyboardMarkup:
    row = []
    rows = []
    for fmt in AUDIO_FORMATS:
        row.append(InlineKeyboardButton(fmt.upper(), callback_data=f"audiofmt_{fmt}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def metadata_skip_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ تخطي هذه الخطوة", callback_data="meta_skip")]])


def image_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 PDF واحد", callback_data="imgfmt_pdf"),
        InlineKeyboardButton("📝 Word واحد", callback_data="imgfmt_docx"),
    ]])


def pdf_target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 تحويل إلى Word", callback_data="pdftarget_word")],
        [InlineKeyboardButton("🔒 تشفير الملف بكلمة سر", callback_data="pdftarget_encrypt")]
    ])


def video_target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎵 استخراج الصوت الآن", callback_data="vidtarget_extract")]])


# ----------------------------------------------------------------------
# التعامل مع الرسائل والـ Callbacks
# ----------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    text = (
        "👋 أهلًا بك في بوت تحويل الصيغ المتقدم وتعديل وسائط الميديا المحترف!\n\n"
        "💡 أرسل أي ملف مباشرة (فيديو، صوت، مستند، صور) وسيتعامل معه البوت تلقائيًا وبذكاء هندسي عالي."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "mode_word2pdf":
        await query.edit_message_text("📄 أرسل الآن ملف Word (.doc أو .docx) لتحويله إلى PDF.")
    elif data == "mode_pdf2word":
        await query.edit_message_text("📄 أرسل الآن ملف PDF لتحويله إلى Word.")
    elif data == "mode_video2audio":
        await query.edit_message_text("🎬 أرسل الآن ملف الفيديو (.mp4, .mkv, إلخ) لاستخراج الصوت منه بصيغة MP3 نقي.")
    elif data == "mode_ebook":
        await query.edit_message_text("📚 أرسل الآن ملف EPUB ليتم تحويله تلقائيًا إلى PDF.")
    elif data == "mode_audio":
        await query.edit_message_text("🎵 أرسل الآن الملف الصوتي المراد تعديله أو تحويل صيغته.")
    elif data == "mode_image":
        await query.edit_message_text("🖼️ أرسل الآن الصورة أو مجموعة الصور المراد تجميعها.")
    elif data == "mode_encrypt_pdf":
        await query.edit_message_text("🔒 أرسل الآن ملف PDF لحمايته وتشفيره بكلمة مرور.")
    elif data.startswith("audiofmt_"):
        await handle_audio_conversion(update, context, data.split("_", 1)[1])
    elif data == "meta_skip":
        await handle_metadata_skip(update, context)
    elif data.startswith("imgfmt_"):
        await handle_image_conversion(update, context, data.split("_", 1)[1])
    elif data.startswith("pdftarget_"):
        await handle_pdf_target_conversion(update, context, data.split("_", 1)[1])
    elif data == "vidtarget_extract":
        await handle_video_extraction(update, context)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("audio_state") == "WATING_ART":
        await handle_photo_as_art(update, context, update.message.document)
        return

    document = update.message.document
    if document.file_size and document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
        return

    filename = document.file_name or "file"
    ext = Path(filename).suffix.lower()

    if ext in AUDIO_EXTENSIONS:
        await prompt_audio_format(update, context, document, filename)
    elif ext in VIDEO_EXTENSIONS:
        await prompt_video_target(update, context, document, filename)
    elif ext in IMAGE_EXTENSIONS:
        await queue_image_processing(update, context, document.file_id, document.file_unique_id, filename)
    elif ext in DOC_EXTENSIONS:
        await process_word_to_pdf(update, context, document, filename)
    elif ext == PDF_EXTENSION:
        await prompt_pdf_target(update, context, document, filename)
    elif ext == EPUB_EXTENSION:
        await process_epub_to_pdf(update, context, document, filename)


async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    audio = update.message.audio or update.message.voice
    filename = getattr(audio, "file_name", None) or "audio.mp3"
    await prompt_audio_format(update, context, audio, filename)


async def handle_video_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video or update.message.video_note
    filename = getattr(video, "file_name", None) or "video.mp4"
    await prompt_video_target(update, context, video, filename)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("audio_state") == "WATING_ART":
        await handle_photo_as_art(update, context, update.message.photo[-1])
        return

    photo = update.message.photo[-1]
    await queue_image_processing(update, context, photo.file_id, photo.file_unique_id, f"{photo.file_unique_id}.jpg")


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("audio_state")
    pdf_state = context.user_data.get("pdf_state")
    text = update.message.text.strip()

    if pdf_state == "WAITING_PASSWORD":
        await process_pdf_encryption(update, context, text)
        return

    if not state:
        return

    if state == "WATING_TITLE":
        context.user_data["meta_title"] = text
        context.user_data["audio_state"] = "WATING_ARTIST"
        await update.message.reply_text("👤 ممتاز، أرسل الآن **اسم الفنان** (أو المغني):", reply_markup=metadata_skip_keyboard())
    elif state == "WATING_ARTIST":
        context.user_data["meta_artist"] = text
        context.user_data["audio_state"] = "WATING_ART"
        await update.message.reply_text("🖼️ أرسل الآن **صورة الغلاف** المرجوة (كصورة أو كملف):", reply_markup=metadata_skip_keyboard())


# ----------------------------------------------------------------------
# استكمال مسار الصوت والفيديو والـ Metadata الصارم
# ----------------------------------------------------------------------

async def prompt_audio_format(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    context.user_data["pending_audio"] = {
        "file_id": tg_file.file_id,
        "file_unique_id": tg_file.file_unique_id,
        "filename": filename,
    }
    await update.message.reply_text("🎵 اختر الصيغة المراد التحويل إليها لبدء تعديل الـ Metadata:", reply_markup=audio_format_keyboard())


async def handle_audio_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE, target_format: str):
    query = update.callback_query
    pending = context.user_data.get("pending_audio")
    if not pending:
        await query.edit_message_text("⚠️ لم يتم العثور على ملف صوتی.")
        return

    await query.edit_message_text(f"⏳ جاري التحويل الرقمي إلى {target_format.upper()} عبر FFmpeg...")
    try:
        local_path = await download_telegram_file(context, pending["file_id"], pending["file_unique_id"], pending["filename"])
        result_path = await convert_audio(local_path, CONVERTED_DIR, target_format)
        
        context.user_data["ready_audio_path"] = str(result_path)
        context.user_data["audio_state"] = "WATING_TITLE"
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✏️ تم التحويل الرقمي بنجاح.\n\nالآن، أرسل **اسم الأغنية / العنوان الجديد**:",
            reply_markup=metadata_skip_keyboard()
        )
        await query.delete_message()
    except Exception as e:
        logger.exception("audio convert error")
        await query.edit_message_text(f"❌ حدث خطأ أثناء التحويل: {e}")


async def prompt_video_target(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    context.user_data["pending_video"] = {
        "file_id": tg_file.file_id,
        "file_unique_id": tg_file.file_unique_id,
        "filename": filename,
    }
    await update.message.reply_text("🎬 أكد رغبتك في استخراج مسار الصوت وتحويل الفيديو الحالي إلى ملف MP3 عالي النقاء:", reply_markup=video_target_keyboard())


async def handle_video_extraction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pending = context.user_data.get("pending_video")
    if not pending:
        await query.edit_message_text("⚠️ لم يتم العثور على ملف فيديو معلق.")
        return

    await query.edit_message_text("⏳ جاري فصل وفك ترميز الصوت من حاوية الفيديو عبر FFmpeg...")
    try:
        local_path = await download_telegram_file(context, pending["file_id"], pending["file_unique_id"], pending["filename"])
        result_path = await convert_video_to_audio(local_path, CONVERTED_DIR)
        
        context.user_data["ready_audio_path"] = str(result_path)
        context.user_data["audio_state"] = "WATING_TITLE"
        
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✏️ تم استخراج المسار الصوتي بنجاح وبأعلى جودة الماستر المتاحة.\n\nالآن، أرسل **اسم الأغنية / العنوان الجديد**:",
            reply_markup=metadata_skip_keyboard()
        )
        await query.delete_message()
    except Exception as e:
        logger.exception("video extract error")
        await query.edit_message_text(f"❌ حدث خطأ أثناء معالجة حاوية الفيديو: {e}")
    finally:
        context.user_data.pop("pending_video", None)


async def handle_metadata_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    state = context.user_data.get("audio_state")

    if state == "WATING_TITLE":
        context.user_data["audio_state"] = "WATING_ARTIST"
        await query.edit_message_text("👤 تم التخطي. أرسل الآن **اسم الفنان**:", reply_markup=metadata_skip_keyboard())
    elif state == "WATING_ARTIST":
        context.user_data["audio_state"] = "WATING_ART"
        await query.edit_message_text("🖼️ تم التخطي. أرسل الآن **صورة الغلاف**:", reply_markup=metadata_skip_keyboard())
    elif state == "WATING_ART":
        await finalize_and_send_audio(query.message.chat_id, context)
        await query.delete_message()


async def handle_photo_as_art(update: Update, context: ContextTypes.DEFAULT_TYPE, photo_obj):
    chat_id = update.message.chat_id
    status_msg = await update.message.reply_text("⏳ جاري معالجة وحقن البيانات الفنية المتقدمة داخل الغلاف...")
    
    try:
        filename = f"art_{photo_obj.file_unique_id}.jpg"
        art_path = await download_telegram_file(context, photo_obj.file_id, photo_obj.file_unique_id, filename)
        context.user_data["meta_art_path"] = str(art_path)
    except Exception as e:
        logger.error(f"فشل تنزيل الغلاف: {e}")

    await finalize_and_send_audio(chat_id, context)
    await status_msg.delete()


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

    # تشغيل منطق التعديل المبني على Mutagen و Pillow في Thread منفصل لتفادي تجميد البوت
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, apply_audio_metadata, audio_path, title, artist, art_path)

    # إرسال الملف النهائي
    with open(audio_path, "rb") as f:
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=f,
            title=title if title else audio_path.stem,
            performer=artist if artist else "فنان غير معروف",
            caption="✅ تم تحديث الـ Tags وحقن الغلاف بنجاح عبر Mutagen!"
        )

    # تنظيف متغيرات الجلسة الصوتية الحالية
    for key in ["audio_state", "ready_audio_path", "meta_title", "meta_artist", "meta_art_path", "pending_audio", "pending_video"]:
        context.user_data.pop(key, None)


# ----------------------------------------------------------------------
# بقية العمليات والوظائف الأساسية للبوت (الصور والمستندات)
# ----------------------------------------------------------------------

async def queue_image_processing(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, file_unique_id: str, filename: str):
    chat_id = update.message.chat_id
    if "image_album" not in context.chat_data:
        context.chat_data["image_album"] = []
    context.chat_data["image_album"].append({"file_id": file_id, "file_unique_id": file_unique_id, "filename": filename})
    
    job_name = f"img_job_{chat_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    context.job_queue.run_once(trigger_image_prompt, when=2.0, chat_id=chat_id, name=job_name, data={"message_id": update.message.message_id})


async def trigger_image_prompt(context: ContextTypes.DEFAULT_TYPE):
    album = context.chat_data.get("image_album", [])
    if not album: return
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"🖼️ تم استقبال {len(album)} صور بنجاح. اختر صيغة الإخراج المستهدفة المجمعة:",
        reply_to_message_id=context.job.data["message_id"],
        reply_markup=image_format_keyboard()
    )


async def handle_image_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE, target_format: str):
    query = update.callback_query
    album = context.chat_data.get("image_album")
    if not album:
        await query.edit_message_text("⚠️ لم يتم العثور على الألبوم المطلوب.")
        return
    await query.edit_message_text("⏳ جاري تحميل الصور المجمعة وبناء المستند الموحد...")
    try:
        local_paths = []
        for img in album:
            p = await download_telegram_file(context, img["file_id"], img["file_unique_id"], img["filename"])
            local_paths.append(p)
        base = f"bundle_{int(time.time())}"
        if target_format == "pdf":
            res = await convert_images_to_pdf(local_paths, CONVERTED_DIR, base)
        else:
            res = await convert_images_to_docx(local_paths, CONVERTED_DIR, base)
        with open(res, "rb") as f:
            await context.bot.send_document(chat_id=query.message.chat_id, document=f, filename=res.name)
        await query.delete_message()
    except Exception as e:
        await query.edit_message_text(f"❌ خطأ أثناء معالجة الصور: {e}")
    finally:
        context.chat_data.pop("image_album", None)


async def process_word_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    msg = await update.message.reply_text("⏳ جاري التحويل عبر LibreOffice...")
    try:
        lp = await download_telegram_file(context, tg_file.file_id, tg_file.file_unique_id, filename)
        res = await convert_docx_to_pdf(lp, CONVERTED_DIR)
        with open(res, "rb") as f:
            await update.message.reply_document(document=f, filename=res.name)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ خطأ أثناء التحويل: {e}")


async def prompt_pdf_target(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    context.user_data["pending_pdf"] = {"file_id": tg_file.file_id, "file_unique_id": tg_file.file_unique_id, "filename": filename}
    await update.message.reply_text("📄 اختر العملية المطلوبة لمستند الـ PDF الحالي:", reply_markup=pdf_target_keyboard())


async def handle_pdf_target_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE, target_format: str):
    query = update.callback_query
    pending = context.user_data.get("pending_pdf")
    if not pending: return

    if target_format == "word":
        await query.edit_message_text("⏳ جاري هندسة وتفكيك ملف الـ PDF إلى مستند Word...")
        try:
            lp = await download_telegram_file(context, pending["file_id"], pending["file_unique_id"], pending["filename"])
            res = await convert_pdf_to_docx(lp, CONVERTED_DIR)
            with open(res, "rb") as f:
                await context.bot.send_document(chat_id=query.message.chat_id, document=f, filename=res.name)
            await query.delete_message()
            context.user_data.pop("pending_pdf", None)
        except Exception as e:
            await query.edit_message_text(f"❌ خطأ: {e}")
            context.user_data.pop("pending_pdf", None)
            
    elif target_format == "encrypt":
        context.user_data["pdf_state"] = "WAITING_PASSWORD"
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🔒 ممتاز، أرسل الآن **كلمة السر** التي ترغب في تشفير وحماية ملف الـ PDF بها:"
        )
        await query.delete_message()


async def process_pdf_encryption(update: Update, context: ContextTypes.DEFAULT_TYPE, password: str):
    pending = context.user_data.get("pending_pdf")
    if not pending:
        await update.message.reply_text("⚠️ لم يتم العثور على مستند معلق لتشفيره.")
        context.user_data.pop("pdf_state", None)
        return

    msg = await update.message.reply_text("⏳ جاري تحميل وتشفير مستند الـ PDF بشكل آمن...")
    try:
        lp = await download_telegram_file(context, pending["file_id"], pending["file_unique_id"], pending["filename"])
        secured_name = f"protected_{Path(pending['filename']).stem}.pdf"
        output_path = CONVERTED_DIR / secured_name

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, encrypt_pdf_file, lp, output_path, password)

        with open(output_path, "rb") as f:
            await update.message.reply_document(
                document=f, 
                filename=secured_name, 
                caption="✅ تم تشفير ملف الـ PDF وحمايته بكلمة سر بنجاح!"
            )
        await msg.delete()
    except Exception as e:
        logger.exception("pdf encrypt error")
        await msg.edit_text(f"❌ حدث خطأ أثناء تشفير الملف: {e}")
    finally:
        context.user_data.pop("pending_pdf", None)
        context.user_data.pop("pdf_state", None)


async def process_epub_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    msg = await update.message.reply_text("⏳ جاري تحويل الكتاب الإلكتروني...")
    try:
        if not is_calibre_available():
            await msg.edit_text("❌ برمجية Calibre غير متوفرة على البيئة السحابية حاليًا.")
            return
        lp = await download_telegram_file(context, tg_file.file_id, tg_file.file_unique_id, filename)
        res = await convert_epub_to_pdf(lp, CONVERTED_DIR)
        with open(res, "rb") as f:
            await update.message.reply_document(document=f, filename=res.name)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ خطأ: {e}")


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
# دالة بدء التشغيل الأساسية للبوت
# ----------------------------------------------------------------------

def main():
    request_config = HTTPXRequest(connect_timeout=30.0, read_timeout=60.0, write_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(request_config).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio_message))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.job_queue.run_repeating(cleanup_job, interval=CLEANUP_INTERVAL, first=CLEANUP_INTERVAL)

    logger.info("🚀 البوت انطلق رسميًا مع دعم تحويل الفيديو الفوري عبر FFmpeg وتعديل الأغلفة...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
    main()
