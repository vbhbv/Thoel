import os
import logging
import asyncio
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler

logger = logging.getLogger(__name__)

from bot import (
    DOWNLOADS_DIR,
    CONVERTED_DIR,
    MAX_FILE_SIZE_MB,
    AUDIO_FORMATS,
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    is_user_banned,
    register_user,
    log_action,
    download_telegram_file,
    convert_audio,
    convert_video_to_audio,
    apply_audio_metadata,
    finalize_and_send_audio,
)

def audio_format_keyboard() -> InlineKeyboardMarkup:
    row, rows = [], []
    for fmt in AUDIO_FORMATS:
        row.append(InlineKeyboardButton(fmt.upper(), callback_data=f"audiofmt_{fmt}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def metadata_skip_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ تخطي هذه الخطوة", callback_data="meta_skip")]])

def video_target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎵 استخراج الصوت الآن", callback_data="vidtarget_extract")]])

async def handle_audio_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """معالجة المستندات التي تتبع صيغ الصوت أو الفيديو، تعيد True إذا تم التعامل معها هنا"""
    document = update.message.document
    filename = document.file_name or "file"
    ext = Path(filename).suffix.lower()

    if ext in AUDIO_EXTENSIONS:
        if document.file_size and document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
            return True
        await prompt_audio_format(update, context, document, filename)
        return True
    elif ext in VIDEO_EXTENSIONS:
        if document.file_size and document.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            await update.message.reply_text(f"⚠️ الملف أكبر من الحد المسموح ({MAX_FILE_SIZE_MB} ميغابايت).")
            return True
        await prompt_video_target(update, context, document, filename)
        return True
    return False

async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.username)
    if await is_user_banned(user.id): return

    audio = update.message.audio or update.message.voice
    filename = getattr(audio, "file_name", None) or "audio.mp3"
    await prompt_audio_format(update, context, audio, filename)

async def handle_video_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.username)
    if await is_user_banned(user.id): return

    video = update.message.video or update.message.video_note
    filename = getattr(video, "file_name", None) or "video.mp4"
    await prompt_video_target(update, context, video, filename)

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
        
        await log_action(f"audio_convert_{target_format}")
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
        
        await log_action("video_to_audio")
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

def register_audio_handlers(app):
    """ربط كول باكس الصوتيات والفيديوهات بالتطبيق الرئيسي"""
    app.add_handler(CallbackQueryHandler(handle_audio_conversion, pattern="^audiofmt_"))
    app.add_handler(CallbackQueryHandler(handle_video_extraction, pattern="^vidtarget_extract$"))
    app.add_handler(CallbackQueryHandler(handle_metadata_skip, pattern="^meta_skip$"))
