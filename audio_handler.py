import os
import logging
import asyncio
import contextlib
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
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def metadata_skip_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ تخطي هذه الخطوة", callback_data="meta_skip")]])


def video_target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎵 استخراج الصوت الآن", callback_data="vidtarget_extract")]])


async def _reject_callback_if_banned(query) -> bool:
    """فحص حظر موحّد لكل معالِجات الأزرار في هذه الوحدة. ضروري الآن بعد
    إعادة ترتيب التسجيل في bot.py بحيث تُستدعى معالِجات هذه الوحدة قبل
    menu_callback العام (الذي كان يقوم بهذا الفحص سابقًا لكنه لم يعد
    يُستدعى أولًا لهذه الأزرار تحديدًا)."""
    user_id = query.from_user.id
    if await is_user_banned(user_id):
        await query.answer("🚫 حسابك محظور من استخدام هذا البوت.", show_alert=True)
        return True
    return False


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
    if await is_user_banned(user.id):
        return

    audio = update.message.audio or update.message.voice
    filename = getattr(audio, "file_name", None) or "audio.mp3"
    await prompt_audio_format(update, context, audio, filename)


async def handle_video_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await register_user(user.id, user.username)
    if await is_user_banned(user.id):
        return

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


async def handle_audio_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE, target_format: str = None):
    query = update.callback_query
    if await _reject_callback_if_banned(query):
        return

    # يدعم الاستدعاء المباشر (target_format ممرَّر) أو عبر CallbackQueryHandler
    # عندما تُسجَّل هذه الدالة مباشرة بنمط "^audiofmt_" (تُستخرج الصيغة من data).
    if target_format is None:
        target_format = query.data.split("_", 1)[1]

    pending = context.user_data.get("pending_audio")
    if not pending:
        await query.answer()
        await query.edit_message_text("⚠️ لم يتم العثور على ملف صوتي. أرسل الملف من جديد.")
        return

    await query.answer()
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
    finally:
        context.user_data.pop("pending_audio", None)


async def prompt_video_target(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str):
    context.user_data["pending_video"] = {
        "file_id": tg_file.file_id,
        "file_unique_id": tg_file.file_unique_id,
        "filename": filename,
    }
    await update.message.reply_text("🎬 أكد رغبتك في استخراج مسار الصوت وتحويل الفيديو الحالي إلى ملف MP3 عالي النقاء:", reply_markup=video_target_keyboard())


async def handle_video_extraction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await _reject_callback_if_banned(query):
        return

    pending = context.user_data.get("pending_video")
    if not pending:
        await query.answer()
        await query.edit_message_text("⚠️ لم يتم العثور على ملف فيديو معلق.")
        return

    await query.answer()
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
    if await _reject_callback_if_banned(query):
        return

    await query.answer()
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
    else:
        # لا توجد عملية Metadata معلّقة فعليًا (مثلاً: زر قديم من محادثة
        # سابقة). نتجاهل بأمان بدل الفشل بصمت أو رمي استثناء.
        await query.edit_message_text("⚠️ لا توجد عملية معلّقة لتخطيها حاليًا.")


async def handle_photo_as_art(update: Update, context: ContextTypes.DEFAULT_TYPE, photo_obj):
    chat_id = update.message.chat_id
    status_msg = await update.message.reply_text("⏳ جاري معالجة وحقن البيانات الفنية المتقدمة داخل الغلاف...")

    download_failed = False
    try:
        filename = f"art_{photo_obj.file_unique_id}.jpg"
        art_path = await download_telegram_file(context, photo_obj.file_id, photo_obj.file_unique_id, filename)
        context.user_data["meta_art_path"] = str(art_path)
    except Exception as e:
        # كان هذا الخطأ يُسجَّل في اللوق فقط بصمت، والمستخدم لا يعرف أن
        # الغلاف لم يُحفظ فعليًا ويحصل على ملف صوتي بدون صورة دون تفسير.
        # الآن نبلغه صراحة بدل المتابعة بصمت.
        logger.error(f"فشل تنزيل الغلاف: {e}")
        download_failed = True

    if download_failed:
        await status_msg.edit_text("⚠️ تعذّر تنزيل الصورة، سيتم إرسال الملف الصوتي بدون غلاف.")
        await asyncio.sleep(1.5)

    await finalize_and_send_audio(chat_id, context)
    with contextlib.suppress(Exception):
        await status_msg.delete()


def register_audio_handlers(app):
    """ربط كول باكس الصوتيات والفيديوهات بالتطبيق الرئيسي.

    ⚠️ يجب استدعاء هذه الدالة قبل تسجيل CallbackQueryHandler(menu_callback)
    العام في bot.py (بلا نمط)، وإلا فإن menu_callback سيلتهم كل هذه
    الضغطات أولًا ولن تصل لمعالِجاتها الصحيحة هنا أبدًا."""
    app.add_handler(CallbackQueryHandler(handle_audio_conversion, pattern="^audiofmt_"))
    app.add_handler(CallbackQueryHandler(handle_video_extraction, pattern="^vidtarget_extract$"))
    app.add_handler(CallbackQueryHandler(handle_metadata_skip, pattern="^meta_skip$"))
