"""
ملف إدارة قاعدة البيانات PostgreSQL ولوحة التحكم (Admin Panel) داخل التلجرام.
يحتوي على نظام الإحصائيات، الحظر، والإذاعة الجماعية (Broadcast).
"""

import os
import logging
import asyncio
from datetime import datetime
import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# جلب رابط قاعدة البيانات ومعرفات الأدمن من متغيرات البيئة
DATABASE_URL = os.environ.get("DATABASE_URL")
# قم بوضع معرف التلجرام الخاص بك هنا كقائمة، أو اجلبه من متغيرات البيئة
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "123456789").split(",") if x.strip()]

# ----------------------------------------------------------------------
# تهيئة وإعداد قاعدة البيانات
# ----------------------------------------------------------------------

async def init_db():
    """تهيئة الجداول الأساسية في PostgreSQL عند إقلاع البوت"""
    if not DATABASE_URL:
        logger.error("❌ متغير البيئة DATABASE_URL غير مضبوط!")
        return

    # معالجة الرابط ليناسب asyncpg إذا كان يبدأ بـ postgres://
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    try:
        conn = await asyncpg.connect(url)
        # جدول المستخدمين
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_banned BOOLEAN DEFAULT FALSE
            );
        """)
        # جدول لتدقيق العمليات والإحصائيات المخزنة
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


async def get_db_connection():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return await asyncpg.connect(url)


# ----------------------------------------------------------------------
# دالات إدارة المستخدمين والعمليات
# ----------------------------------------------------------------------

async def register_user(user_id: BIGINT, username: str):
    """تسجيل المستخدم تلقائيًا عند تفاعله مع البوت"""
    try:
        conn = await get_db_connection()
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


async def is_user_banned(user_id: BIGINT) -> bool:
    """التحقق مما إذا كان المستخدم محظورًا"""
    try:
        conn = await get_db_connection()
        row = await conn.fetchrow("SELECT is_banned FROM users WHERE user_id = $1;", user_id)
        await conn.close()
        return row["is_banned"] if row else False
    except Exception as e:
        logger.error(f"خطأ أثناء فحص حظر المستخدم: {e}")
        return False


async def log_action(action_type: str):
    """تسجيل العمليات (مثل تحويل ملف، دمج) لحساب الإحصائيات بدقة"""
    try:
        conn = await get_db_connection()
        await conn.execute("INSERT INTO stats_log (action_type) VALUES ($1);", action_type)
        await conn.close()
    except Exception as e:
        logger.error(f"خطأ أثناء تسجيل العملية {action_type}: {e}")


# ----------------------------------------------------------------------
# لوحة التحكم والأزرار التفاعلية للأدمن
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
    """أمر /admin لفتح لوحة التحكم للآدمنز فقط"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return  # تجاهل تام لغير الأدمن لحماية البوت

    await update.message.reply_text(
        "⚙️ **مرحبًا بك في لوحة تحكم الإدارة الاحترافية**\nاختر الإجراء المطلوبة من الأزرار أدناه:",
        reply_markup=admin_keyboard(),
        parse_mode="Markdown"
    )


async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الضغط على أزرار لوحة التحكم"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.answer("❌ غير مسموح لك بالوصول.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "admin_stats":
        await query.edit_message_text("⏳ جاري جلب البيانات من PostgreSQL...")
        try:
            conn = await get_db_connection()
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
            await query.edit_message_text(f"❌ خطأ أثناء جلب الإحصائيات: {e}", reply_markup=admin_keyboard())

    elif data == "admin_broadcast":
        context.user_data["admin_state"] = "WAITING_BROADCAST_MSG"
        await query.edit_message_text("📢 أرسل الآن الرسالة التي تريد إذاعتها لجميع المستخدمين (نص، صورة، أو ملف):")

    elif data == "admin_ban":
        context.user_data["admin_state"] = "WAITING_BAN_ID"
        await query.edit_message_text("🚫 أرسل الـ `User ID` الخاص بالمستخدم الذي تريد حظره نهائيًا:")

    elif data == "admin_unban":
        context.user_data["admin_state"] = "WAITING_UNBAN_ID"
        await query.edit_message_text("🟢 أرسل الـ `User ID` الخاص بالمستخدم لإلغاء الحظر عنه:")


# ----------------------------------------------------------------------
# معالجة المدخلات النصية والإذاعة
# ----------------------------------------------------------------------

async def handle_admin_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    تتعامل مع مدخلات الأدمن المنتظرة.
    ترجع True إذا تمت معالجة الإدخال هنا، و False إذا لم يكن هناك مدخل خاص بالإدارة.
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return False

    state = context.user_data.get("admin_state")
    if not state:
        return False

    if state == "WAITING_BROADCAST_MSG":
        context.user_data.pop("admin_state", None)
        status_msg = await update.message.reply_text("⏳ جاري بدء عملية الإذاعة الجماعية، يرجى عدم إرسال أوامر أخرى...")
        
        try:
            conn = await get_db_connection()
            rows = await conn.fetch("SELECT user_id FROM users WHERE is_banned = FALSE;")
            await conn.close()
        except Exception as e:
            await status_msg.edit_text(f"❌ فشل جلب المستخدمين من القاعدة: {e}")
            return True

        success, failed = 0, 0
        for row in rows:
            target_id = row["user_id"]
            if target_id == user_id:
                continue # تخطي الأدمن نفسه
            try:
                # محاكاة إرسال الرسالة مهما كان نوعها (نص، ميديا...)
                await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=update.message.chat_id,
                    message_id=update.message.message_id
                )
                success += 1
                await asyncio.sleep(0.05) # حماية من الـ Flood Antispam الخاص بتلجرام
            except Exception:
                failed += 1

        await status_msg.edit_text(
            f"📢 **اكتملت الإذاعة الجماعية بنجاح!**\n\n"
            f"✅ تم الإرسال إلى: `{success}` مستخدم.\n"
            f"❌ فشل الإرسال إلى: `{failed}` مستخدم (قاموا بحظر البوت غالباً).",
            parse_mode="Markdown"
        )
        return True

    elif state == "WAITING_BAN_ID":
        context.user_data.pop("admin_state", None)
        target_input = update.message.text.strip()
        if not target_input.isdigit():
            await update.message.reply_text("⚠️ المعرف يجب أن يكون أرقامًا فقط.")
            return True
        
        target_id = int(target_input)
        try:
            conn = await get_db_connection()
            await conn.execute("UPDATE users SET is_banned = TRUE WHERE user_id = $1;", target_id)
            await conn.close()
            await update.message.reply_text(f"✅ تم حظر المستخدم `{target_id}` بنجاح ومنعه من استخدام البوت.", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ حدث خطأ أثناء الحظر: {e}")
        return True

    elif state == "WAITING_UNBAN_ID":
        context.user_data.pop("admin_state", None)
        target_input = update.message.text.strip()
        if not target_input.isdigit():
            await update.message.reply_text("⚠️ المعرف يجب أن يكون أرقامًا فقط.")
            return True
        
        target_id = int(target_input)
        try:
            conn = await get_db_connection()
            await conn.execute("UPDATE users SET is_banned = FALSE WHERE user_id = $1;", target_id)
            await conn.close()
            await update.message.reply_text(f"✅ تم إلغاء حظر المستخدم `{target_id}` بنجاح ونقل حسابه إلى الوضع النشط.", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ حدث خطأ أثناء إلغاء الحظر: {e}")
        return True

    return False
