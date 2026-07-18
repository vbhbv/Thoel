import os
import time
import logging
import asyncio
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters

logger = logging.getLogger(__name__)

# استيراد الثوابت والدوال المشتركة من الملف الرئيسي لضمان عدم التكرار والاتساق
from bot import (
    DOWNLOADS_DIR,
    CONVERTED_DIR,
    MAX_FILE_SIZE_MB,
    DOC_EXTENSIONS,
    PDF_EXTENSION,
    EPUB_EXTENSION,
    IMAGE_EXTENSIONS,
    is_user_banned,
    register_user,
    log_action,
    download_telegram_file,
    convert_docx_to_pdf,
    convert_pdf_to_docx,
    convert_images_to_pdf,
    convert_images_to_docx,
    encrypt_pdf_file,
    process_epub_to_pdf,
)

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

async def handle_files_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """معالجة المستندات الخاصة بالملفات والتحقق من الصيغ، تعيد True إذا تم التعامل معها هنا"""
    document = update.message.document
    filename = document.file_name or "file"
    ext = Path(filename).suffix.lower()

    if ext in IMAGE_EXTENSIONS:
        if document.file_size and document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
            return True
        await queue_image_processing(update, context, document.file_id, document.file_unique_id, filename)
        return True
    elif ext in DOC_EXTENSIONS:
        if document.file_size and document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
            return True
        await process_word_to_pdf(update, context, document, filename)
        return True
    elif ext == PDF_EXTENSION:
        if document.file_size and document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
            return True
        await prompt_pdf_target(update, context, document, filename)
        return True
    elif ext == EPUB_EXTENSION:
        if document.file_size and document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
            return True
        await process_epub_to_pdf(update, context, document, filename)
        return True
    return False

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
        
        await log_action(f"image_to_{target_format}")
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
        await log_action("word_to_pdf")
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
            await log_action("pdf_to_word")
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

        await asyncio.get_running_loop().run_in_executor(None, encrypt_pdf_file, lp, output_path, password)
        await log_action("pdf_encrypt")

        with open(output_path, "rb") as f:
            await update.message.reply_document(document=f, filename=secured_name, caption="✅ تم تشفير ملف الـ PDF وحمايته بكلمة سر بنجاح!")
        await msg.delete()
    except Exception as e:
        logger.exception("pdf encrypt error")
        await msg.edit_text(f"❌ حدث خطأ أثناء تشفير الملف: {e}")
    finally:
        context.user_data.pop("pending_pdf", None)
        context.user_data.pop("pdf_state", None)

async def handle_files_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استلام ومعالجة الصور المباشرة المرفوعة كألبوم صور"""
    photo = update.message.photo[-1]
    await queue_image_processing(update, context, photo.file_id, photo.file_unique_id, f"{photo.file_unique_id}.jpg")

def register_files_handlers(app):
    """ربط كول باكس الملفات بالتطبيق الرئيسي"""
    app.add_handler(CallbackQueryHandler(handle_image_conversion, pattern="^imgfmt_"))
    app.add_handler(CallbackQueryHandler(handle_pdf_target_conversion, pattern="^pdftarget_"))
