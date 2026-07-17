"""
بوت تليجرام لتحويل الصيغ
- Word <-> PDF
- PDF <-> EPUB
- تحويل الصوت لعدة صيغ
- تحويل الصور إلى PDF أو Word
مصمم للنشر على Railway
"""

import os
import time
import logging
import asyncio
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from pdf_epub_converter import (
    convert_pdf_to_epub,
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

# أقصى مدة بقاء الملف قبل الحذف (بالثواني) - ساعة واحدة
FILE_MAX_AGE = 60 * 60
# كل كم مدة يشتغل التنظيف الدوري (بالثواني) - كل 30 دقيقة
CLEANUP_INTERVAL = 30 * 60

# حد حجم الملف الذي يقبله تليجرام للبوتات العادية (20 ميغا تنزيل)
MAX_FILE_SIZE_MB = 20

AUDIO_FORMATS = ["mp3", "wav", "ogg", "flac", "m4a", "aac"]

DOC_EXTENSIONS = {".doc", ".docx"}
PDF_EXTENSION = ".pdf"
EPUB_EXTENSION = ".epub"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".wma"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"}


# ----------------------------------------------------------------------
# أدوات مساعدة
# ----------------------------------------------------------------------

async def run_cmd(*args: str) -> tuple[int, str, str]:
    """تشغيل أمر خارجي بشكل غير متزامن (لا يعطل البوت أثناء التحويل)."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(errors="ignore"), stderr.decode(errors="ignore")


async def download_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str, file_unique_id: str, filename: str) -> Path:
    """تنزيل ملف من تليجرام إلى مجلد downloads المحلي."""
    file_obj = await context.bot.get_file(file_id)
    ext = Path(filename).suffix or ""
    local_path = DOWNLOADS_DIR / f"{file_unique_id}{ext}"
    await file_obj.download_to_drive(custom_path=str(local_path))
    return local_path


# ----------------------------------------------------------------------
# منطق التحويل
# ----------------------------------------------------------------------

async def convert_docx_to_pdf(input_path: Path, out_dir: Path) -> Path:
    """تحويل Word إلى PDF باستخدام LibreOffice (headless)."""
    code, out, err = await run_cmd(
        "libreoffice", "--headless", "--norestore",
        "--convert-to", "pdf", "--outdir", str(out_dir), str(input_path)
    )
    result = out_dir / (input_path.stem + ".pdf")
    if code != 0 or not result.exists():
        raise RuntimeError(f"فشل تحويل Word إلى PDF: {err or out}")
    return result


async def convert_pdf_to_docx(input_path: Path, out_dir: Path) -> Path:
    """تحويل PDF إلى Word باستخدام مكتبة pdf2docx."""
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
    """تحويل ملف صوتي إلى صيغة أخرى باستخدام ffmpeg."""
    output_path = out_dir / (input_path.stem + f".{target_format}")
    code, out, err = await run_cmd(
        "ffmpeg", "-y", "-i", str(input_path),
        "-vn", "-ar", "44100", "-ac", "2",
        str(output_path)
    )
    if code != 0 or not output_path.exists():
        raise RuntimeError(f"فشل تحويل الصوت: {err[-500:]}")
    return output_path


async def convert_image_to_pdf(input_path: Path, out_dir: Path) -> Path:
    """تحويل صورة إلى PDF باستخدام Pillow و img2pdf."""
    output_path = out_dir / (input_path.stem + ".pdf")

    def _convert():
        from PIL import Image
        import img2pdf

        img = Image.open(input_path)
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")

        tmp_path = input_path.with_suffix(".conv.jpg")
        img.save(tmp_path, "JPEG", quality=95)

        pdf_bytes = img2pdf.convert(str(tmp_path))
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)

        tmp_path.unlink(missing_ok=True)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _convert)

    if not output_path.exists():
        raise RuntimeError("فشل تحويل الصورة إلى PDF")
    return output_path


async def convert_image_to_docx(input_path: Path, out_dir: Path) -> Path:
    """تحويل صورة إلى ملف Word (تُدرج الصورة داخل مستند)."""
    output_path = out_dir / (input_path.stem + ".docx")

    def _convert():
        from docx import Document
        from docx.shared import Inches
        from PIL import Image

        img = Image.open(input_path)
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
            fixed_path = input_path.with_suffix(".conv.jpg")
            img.save(fixed_path, "JPEG", quality=95)
            source = fixed_path
        else:
            source = input_path

        doc = Document()
        doc.add_picture(str(source), width=Inches(6))
        doc.save(str(output_path))

        if source != input_path:
            source.unlink(missing_ok=True)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _convert)

    if not output_path.exists():
        raise RuntimeError("فشل تحويل الصورة إلى Word")
    return output_path


# ----------------------------------------------------------------------
# لوحات الأزرار
# ----------------------------------------------------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📄 Word ➜ PDF", callback_data="mode_word2pdf")],
        [InlineKeyboardButton("📄 PDF ➜ Word", callback_data="mode_pdf2word")],
        [InlineKeyboardButton("📚 PDF ⇄ EPUB", callback_data="mode_ebook")],
        [InlineKeyboardButton("🎵 تحويل صيغة صوتية", callback_data="mode_audio")],
        [InlineKeyboardButton("🖼️ تحويل صورة إلى PDF/Word", callback_data="mode_image")],
        [InlineKeyboardButton("ℹ️ مساعدة", callback_data="mode_help")],
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


def image_format_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📄 PDF", callback_data="imgfmt_pdf"),
            InlineKeyboardButton("📝 Word", callback_data="imgfmt_docx"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


def pdf_target_keyboard() -> InlineKeyboardMarkup:
    """عند استقبال PDF: نسأل المستخدم إن كان يريده كـ Word أو EPUB."""
    buttons = [
        [
            InlineKeyboardButton("📝 Word", callback_data="pdftarget_word"),
            InlineKeyboardButton("📚 EPUB", callback_data="pdftarget_epub"),
        ]
    ]
    return InlineKeyboardMarkup(buttons)


# ----------------------------------------------------------------------
# أوامر البوت
# ----------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    text = (
        "👋 أهلًا بك في بوت تحويل الصيغ!\n\n"
        "🔹 أرسل ملف Word وسيتحول تلقائيًا إلى PDF.\n"
        "🔹 أرسل ملف PDF وسأسألك: Word أم EPUB؟\n"
        "🔹 أرسل ملف EPUB وسيتحول تلقائيًا إلى PDF.\n"
        "🔹 أرسل ملف أو مقطع صوتي وسأعرض عليك أزرار الصيغ المتاحة.\n"
        "🔹 أرسل صورة وسأعرض عليك تحويلها إلى PDF أو Word.\n"
        "🔹 أو اختر من القائمة بالأسفل.\n\n"
        f"⚠️ الحد الأقصى لحجم الملف: {MAX_FILE_SIZE_MB} ميغابايت."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 طريقة الاستخدام:\n"
        "1. أرسل ملف Word (.doc/.docx) لتحويله إلى PDF تلقائيًا.\n"
        "2. أرسل ملف PDF ثم اختر تحويله إلى Word أو EPUB.\n"
        "3. أرسل ملف EPUB لتحويله إلى PDF تلقائيًا.\n"
        "4. أرسل ملف/مقطع صوتي، ثم اختر الصيغة المطلوبة من الأزرار.\n"
        "5. أرسل صورة، ثم اختر تحويلها إلى PDF أو Word.\n\n"
        "الأوامر:\n"
        "/start - القائمة الرئيسية\n"
        "/help - المساعدة"
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "mode_word2pdf":
        context.user_data["mode"] = "word2pdf"
        await query.edit_message_text("📄 أرسل الآن ملف Word (.doc أو .docx) لتحويله إلى PDF.")
    elif data == "mode_pdf2word":
        context.user_data["mode"] = "pdf2word"
        await query.edit_message_text("📄 أرسل الآن ملف PDF لتحويله إلى Word.")
    elif data == "mode_ebook":
        context.user_data["mode"] = "ebook"
        await query.edit_message_text(
            "📚 أرسل الآن ملف PDF (سأسألك عن الصيغة الهدف) أو ملف EPUB (يتحول تلقائيًا إلى PDF)."
        )
    elif data == "mode_audio":
        context.user_data["mode"] = "audio"
        await query.edit_message_text("🎵 أرسل الآن الملف أو المقطع الصوتي المراد تحويله.")
    elif data == "mode_image":
        context.user_data["mode"] = "image"
        await query.edit_message_text(
            "🖼️ أرسل الآن الصورة المراد تحويلها (يفضّل إرسالها كملف/Document للحفاظ على الجودة الأصلية)."
        )
    elif data == "mode_help":
        await query.edit_message_text(
            "أرسل ملف Word وسيتحول تلقائيًا إلى PDF.\n"
            "أرسل ملف PDF وسأسألك: Word أم EPUB؟\n"
            "أرسل ملف EPUB ويتحول تلقائيًا إلى PDF.\n"
            "أرسل ملفًا صوتيًا وستظهر لك أزرار لاختيار الصيغة الهدف.\n"
            "أرسل صورة وستظهر لك أزرار لتحويلها إلى PDF أو Word."
        )
    elif data.startswith("audiofmt_"):
        target_format = data.split("_", 1)[1]
        await handle_audio_conversion(update, context, target_format)
    elif data.startswith("imgfmt_"):
        target_format = data.split("_", 1)[1]
        await handle_image_conversion(update, context, target_format)
    elif data.startswith("pdftarget_"):
        target_format = data.split("_", 1)[1]
        await handle_pdf_target_conversion(update, context, target_format)


# ----------------------------------------------------------------------
# استقبال الملفات
# ----------------------------------------------------------------------

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if document.file_size and document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
        return

    filename = document.file_name or "file"
    ext = Path(filename).suffix.lower()

    # كشف تلقائي إن كان صوتيًا يُرسل كمستند
    if ext in AUDIO_EXTENSIONS:
        await prompt_audio_format(update, context, document, filename)
        return

    # كشف تلقائي إن كانت صورة تُرسل كمستند (لتفادي ضغط تليجرام للصور)
    if ext in IMAGE_EXTENSIONS:
        await prompt_image_format(update, context, document, filename)
        return

    if ext in DOC_EXTENSIONS:
        await process_word_to_pdf(update, context, document, filename)
    elif ext == PDF_EXTENSION:
        # PDF قد يُراد تحويله إلى Word أو EPUB، لذا نسأل المستخدم
        await prompt_pdf_target(update, context, document, filename)
    elif ext == EPUB_EXTENSION:
        await process_epub_to_pdf(update, context, document, filename)
    else:
        await update.message.reply_text(
            "⚠️ صيغة غير مدعومة حاليًا. أرسل ملف Word أو PDF أو EPUB أو ملفًا صوتيًا أو صورة."
        )


async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    audio = update.message.audio or update.message.voice
    filename = getattr(audio, "file_name", None) or "audio.ogg"
    await prompt_audio_format(update, context, audio, filename)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال صورة مرسلة كـ Photo (مضغوطة من تليجرام)."""
    photo = update.message.photo[-1]  # أعلى دقة متاحة
    filename = f"{photo.file_unique_id}.jpg"
    await prompt_image_format(update, context, photo, filename)


# ---------- Word -> PDF ----------

async def process_word_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    status_msg = await update.message.reply_text("⏳ جاري تحميل الملف وتحويله إلى PDF...")
    try:
        local_path = await download_telegram_file(context, tg_file.file_id, tg_file.file_unique_id, filename)
        result_path = await convert_docx_to_pdf(local_path, CONVERTED_DIR)
        with open(result_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=result_path.name,
                caption="✅ تم التحويل إلى PDF بنجاح.",
            )
        await status_msg.delete()
    except Exception as e:
        logger.exception("word2pdf error")
        await status_msg.edit_text(f"❌ حدث خطأ أثناء التحويل: {e}")


# ---------- PDF -> (Word / EPUB) ----------

async def prompt_pdf_target(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    if getattr(tg_file, "file_size", None) and tg_file.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
        return

    context.user_data["pending_pdf"] = {
        "file_id": tg_file.file_id,
        "file_unique_id": tg_file.file_unique_id,
        "filename": filename,
    }
    await update.message.reply_text(
        "📄 اختر الصيغة التي تريد تحويل PDF إليها:",
        reply_markup=pdf_target_keyboard(),
    )


async def handle_pdf_target_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE, target_format: str):
    query = update.callback_query
    pending = context.user_data.get("pending_pdf")
    if not pending:
        await query.edit_message_text("⚠️ لم يتم العثور على ملف PDF. أرسل الملف من جديد.")
        return

    label = "Word" if target_format == "word" else "EPUB"
    await query.edit_message_text(f"⏳ جاري التحويل إلى {label}...")
    try:
        local_path = await download_telegram_file(
            context, pending["file_id"], pending["file_unique_id"], pending["filename"]
        )

        if target_format == "word":
            result_path = await convert_pdf_to_docx(local_path, CONVERTED_DIR)
        else:
            if not is_calibre_available():
                raise EbookConversionError(
                    "خاصية EPUB غير مفعّلة على هذا الخادم (Calibre غير مثبت)."
                )
            result_path = await convert_pdf_to_epub(local_path, CONVERTED_DIR)

        with open(result_path, "rb") as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename=result_path.name,
                caption=f"✅ تم التحويل إلى {label} بنجاح.",
            )
        await query.delete_message()
    except EbookConversionError as e:
        logger.exception("pdf2epub error")
        await query.edit_message_text(f"❌ {e}")
    except Exception as e:
        logger.exception("pdf target conversion error")
        await query.edit_message_text(f"❌ حدث خطأ أثناء التحويل: {e}")
    finally:
        context.user_data.pop("pending_pdf", None)


# ---------- EPUB -> PDF ----------

async def process_epub_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    status_msg = await update.message.reply_text("⏳ جاري تحميل الملف وتحويله إلى PDF...")
    try:
        if not is_calibre_available():
            await status_msg.edit_text("❌ خاصية EPUB غير مفعّلة على هذا الخادم (Calibre غير مثبت).")
            return

        local_path = await download_telegram_file(context, tg_file.file_id, tg_file.file_unique_id, filename)
        result_path = await convert_epub_to_pdf(local_path, CONVERTED_DIR)
        with open(result_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=result_path.name,
                caption="✅ تم التحويل إلى PDF بنجاح.",
            )
        await status_msg.delete()
    except EbookConversionError as e:
        logger.exception("epub2pdf error")
        await status_msg.edit_text(f"❌ {e}")
    except Exception as e:
        logger.exception("epub2pdf error")
        await status_msg.edit_text(f"❌ حدث خطأ أثناء التحويل: {e}")


# ---------- الصوت ----------

async def prompt_audio_format(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    if getattr(tg_file, "file_size", None) and tg_file.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
        return

    context.user_data["pending_audio"] = {
        "file_id": tg_file.file_id,
        "file_unique_id": tg_file.file_unique_id,
        "filename": filename,
    }
    await update.message.reply_text(
        "🎵 اختر الصيغة التي تريد التحويل إليها:",
        reply_markup=audio_format_keyboard(),
    )


async def handle_audio_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE, target_format: str):
    query = update.callback_query
    pending = context.user_data.get("pending_audio")
    if not pending:
        await query.edit_message_text("⚠️ لم يتم العثور على ملف صوتي. أرسل الملف من جديد.")
        return

    await query.edit_message_text(f"⏳ جاري التحويل إلى {target_format.upper()}...")
    try:
        local_path = await download_telegram_file(
            context, pending["file_id"], pending["file_unique_id"], pending["filename"]
        )
        result_path = await convert_audio(local_path, CONVERTED_DIR, target_format)
        with open(result_path, "rb") as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename=result_path.name,
                caption=f"✅ تم التحويل إلى {target_format.upper()} بنجاح.",
            )
        await query.delete_message()
    except Exception as e:
        logger.exception("audio conversion error")
        await query.edit_message_text(f"❌ حدث خطأ أثناء التحويل: {e}")
    finally:
        context.user_data.pop("pending_audio", None)


# ---------- الصور ----------

async def prompt_image_format(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    if getattr(tg_file, "file_size", None) and tg_file.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
        return

    context.user_data["pending_image"] = {
        "file_id": tg_file.file_id,
        "file_unique_id": tg_file.file_unique_id,
        "filename": filename,
    }
    await update.message.reply_text(
        "🖼️ اختر الصيغة التي تريد تحويل الصورة إليها:",
        reply_markup=image_format_keyboard(),
    )


async def handle_image_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE, target_format: str):
    query = update.callback_query
    pending = context.user_data.get("pending_image")
    if not pending:
        await query.edit_message_text("⚠️ لم يتم العثور على صورة. أرسل الصورة من جديد.")
        return

    label = "PDF" if target_format == "pdf" else "Word"
    await query.edit_message_text(f"⏳ جاري التحويل إلى {label}...")
    try:
        local_path = await download_telegram_file(
            context, pending["file_id"], pending["file_unique_id"], pending["filename"]
        )

        if target_format == "pdf":
            result_path = await convert_image_to_pdf(local_path, CONVERTED_DIR)
        else:
            result_path = await convert_image_to_docx(local_path, CONVERTED_DIR)

        with open(result_path, "rb") as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename=result_path.name,
                caption=f"✅ تم التحويل إلى {label} بنجاح.",
            )
        await query.delete_message()
    except Exception as e:
        logger.exception("image conversion error")
        await query.edit_message_text(f"❌ حدث خطأ أثناء التحويل: {e}")
    finally:
        context.user_data.pop("pending_image", None)


# ----------------------------------------------------------------------
# التنظيف الدوري
# ----------------------------------------------------------------------

async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    removed = 0
    for folder in (DOWNLOADS_DIR, CONVERTED_DIR):
        for path in folder.glob("*"):
            try:
                if path.is_file() and (now - path.stat().st_mtime) > FILE_MAX_AGE:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
    if removed:
        logger.info(f"🧹 تم حذف {removed} ملف مؤقت خلال التنظيف الدوري.")


# ----------------------------------------------------------------------
# نقطة التشغيل
# ----------------------------------------------------------------------

def main():
    if not is_calibre_available():
        logger.warning(
            "⚠️ Calibre (ebook-convert) غير مثبت. ميزة PDF⇄EPUB لن تعمل حتى يتم تثبيته."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.job_queue.run_repeating(cleanup_job, interval=CLEANUP_INTERVAL, first=CLEANUP_INTERVAL)

    logger.info("🚀 البوت يعمل الآن...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
