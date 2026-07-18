"""
ملف إدارة قاعدة البيانات PostgreSQL ولوحة التحكم (Admin Panel) داخل التلجرام.
يحتوي على نظام الإحصائيات، الحظر، الإذاعة الجماعية (Broadcast)، والاشتراك الإجباري الفعال.
"""

import os
import logging
import asyncio
from datetime import datetime
import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import TelegramError

logger = logging.getLogger(__name__)

# جلب رابط قاعدة البيانات ومعرفات الأدمن من متغيرات البيئة
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "123456789").split(",") if x.strip()]

# ----------------------------------------------------------------------
# تهيئة وإعداد قاعدة البيانات
# ----------------------------------------------------------------------

async def init_db():
    """تهيئة الجداول الأساسية في PostgreSQL عند إقلاع البوت"""
    if not DATABASE_URL:
        logger.error("❌ متغير البيئة DATABASE_URL غير مضبوط!")
        return

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
        # جدول القنوات الإجبارية
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS forced_channels (
                channel_id BIGINT PRIMARY KEY,
                channel_invite_link TEXT NOT NULL,
                channel_name TEXT
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
# دالات إدارة المستخدمين وقنوات الاشتراك الإجباري
# ----------------------------------------------------------------------

async def register_user(user_id: int, username: str):
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


async def is_user_banned(user_id: int) -> bool:
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
    """تسجيل العمليات لحساب الإحصائيات بدقة"""
    try:
        conn = await get_db_connection()
        await conn.execute("INSERT INTO stats_log (action_type) VALUES ($1);", action_type)
        await conn.close()
    except Exception as e:
        logger.error(f"خطأ أثناء تسجيل العملية {action_type}: {e}")


# ----------------------------------------------------------------------
# محرك التحقق من الاشتراك الإجباري (Force Subscribe Core)
# ----------------------------------------------------------------------

async def check_force_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    تتحقق مما إذا كان المستخدم مشتركًا في القنوات الإجبارية.
    إذا لم يكن مشتركًا، ترسل له رسالة تنبيه بالأزرار وتعيد False.
    إذا كان مشتركًا (أو أدمن) تعيد True.
    """
    user_id = update.effective_user.id
    
    # استثناء الأدمنز من الاشتراك الإجباري لتسهيل عملهم
    if user_id in ADMIN_IDS:
        return True

    try:
        conn = await get_db_connection()
        channels = await conn.fetch("SELECT channel_id, channel_invite_link, channel_name FROM forced_channels;")
        await conn.close()
    except Exception as e:
        logger.error(f"خطأ أثناء جلب قنوات الاشتراك الإجباري: {e}")
        return True # تمرير المستخدم مؤقتاً في حال تعطل القاعدة منعاً لتوقف البوت

    if not channels:
        return True # لا توجد قنوات مفروضة حالياً

    not_joined_buttons = []
    
    for ch in channels:
        ch_id = ch["channel_id"]
        invite_link = ch["channel_invite_link"]
        name = ch["channel_name"] or "اضغط هنا للاشتراك"
        
        try:
            # التحقق من رتبة المستخدم في القناة عبر غيت شات ميمبر
            member = await context.bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ["left", "kicked"]:
                not_joined_buttons.append([InlineKeyboardButton(text=name, url=invite_link)])
        except TelegramError as e:
            # إذا لم يكن البوت مشرفاً في القناة لن يتمكن من فحص المشتركين
            logger.error(f"البوت يحتاج صلاحيات أدمن في القناة {ch_id} لفحص المشتركين: {e}")
            continue

    if not_joined_buttons:
        # إضافة زر التأكيد (Refresh) بعد قنوات الاشتراك
        not_joined_buttons.append([InlineKeyboardButton(text="🔄 تم الاشتراك، اضغط للتأكيد", callback_data="check_subscription_again")])
        
        msg_text = "⚠️ **عذرًا عزيزي، يجب عليك الاشتراك في قنوات البوت أولاً لتتمكن من استخدامه!**\n\nاشترك في القنوات أدناه ثم اضغط على زر التأكيد المتواجد بالأسفل 👇"
        
        if update.callback_query:
            await update.callback_query.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(not_joined_buttons), parse_mode="Markdown")
        else:
            await update.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(not_joined_buttons), parse_mode="Markdown")
        return False

    return True


# ----------------------------------------------------------------------
# لوحة التحكم والأزرار التفاعلية للأدمن
# ----------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📊 إحصائيات البوت", callback_data="admin_stats")],
        [InlineKeyboardButton("📢 إذاعة جماعية (Broadcast)", callback_data="admin_broadcast")],
        [InlineKeyboardButton("➕ إضافة قناة إجبارية", callback_data="admin_add_channel"),
         InlineKeyboardButton("❌ حذف قناة إجبارية", callback_data="admin_del_channel")],
        [InlineKeyboardButton("📋 عرض قنوات الاشتراك", callback_data="admin_list_channels")],
        [InlineKeyboardButton("🚫 حظر مستخدم", callback_data="admin_ban"),
         InlineKeyboardButton("🟢 إلغاء حظر", callback_data="admin_unban")]
    ]
    return InlineKeyboardMarkup(buttons)


async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /admin لفتح لوحة التحكم للآدمنز فقط"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    await update.message.reply_text(
        "⚙️ **مرحبًا بك في لوحة تحكم الإدارة الاحترافية**\nاختر الإجراء المطلوبة من الأزرار أدناه:",
        reply_markup=admin_keyboard(),
        parse_mode="Markdown"
    )


async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الضغط على أزرار لوحة التحكم والتحقق العام"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # معالجة الضغط على زر التحقق من قبل المستخدم العادي أولاً
    if query.data == "check_subscription_again":
        await query.answer("🔄 جاري إعادة الفحص...")
        is_subscribed = await check_force_subscribe(update, context)
        if is_subscribed:
            await query.message.delete()
            await query.message.reply_text("✅ تهانينا! تم تفعيل البوت بنجاح، يمكنك الآن استخدام كافة الأوامر والمميزات مجددًا. أرسل /start")
        return

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
            total_channels = await conn.fetchval("SELECT COUNT(*) FROM forced_channels;")
            await conn.close()

            stats_text = (
                "📊 **إحصائيات النظام الحالية:**\n\n"
                f"👥 إجمالي المستخدمين المسجلين: `{total_users}`\n"
                f"🚫 عدد المستخدمين المحظورين: `{banned_users}`\n"
                f"📢 قنوات الاشتراك الإجباري: `{total_channels}`\n"
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

    elif data == "admin_add_channel":
        context.user_data["admin_state"] = "WAITING_ADD_CHANNEL"
        await query.edit_message_text(
            "➕ **لإضافة قناة اشتراك إجباري جديدة:**\n\n"
            "أرسل البيانات بصيغة نصية واحدة مفصولة بفاصلة كالتالي:\n"
            "`ID_القناة, رابط_الدعوة, اسم_الزر`\n\n"
            "💡 مثال:\n"
            "`-100123456789, https://t.me/MyChannel, تابع جديدنا 📢`\n\n"
            "⚠️ تأكد تماماً أن البوت يمتلك صلاحية مشرف (Admin) داخل القناة المضافة ليفحص الأعضاء!"
        )

    elif data == "admin_del_channel":
        context.user_data["admin_state"] = "WAITING_DEL_CHANNEL"
        await query.edit_message_text("❌ أرسل الـ `Channel ID` الخاص بالقناة التي تود إزالتها من نظام الاشتراك الإجباري:")

    elif data == "admin_list_channels":
        try:
            conn = await get_db_connection()
            rows = await conn.fetch("SELECT channel_id, channel_name, channel_invite_link FROM forced_channels;")
            await conn.close()
            
            if not rows:
                await query.edit_message_text("📋 لا توجد أي قنوات اشتراك إجباري مضافة حاليًا.", reply_markup=admin_keyboard())
                return
                
            text = "📋 **قنوات الاشتراك الإجباري الحالية:**\n\n"
            for index, row in enumerate(rows, 1):
                text += f"{index}- الاسم: [{row['channel_name']}]({row['channel_invite_link']})\nID: `{row['channel_id']}`\n\n"
                
            await query.edit_message_text(text, reply_markup=admin_keyboard(), parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            await query.edit_message_text(f"❌ خطأ أثناء عرض القنوات: {e}", reply_markup=admin_keyboard())


# ----------------------------------------------------------------------
# معالجة المدخلات النصية والإذاعة وإدارة الاشتراك الإجباري
# ----------------------------------------------------------------------

async def handle_admin_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    تتعامل مع مدخلات الأدمن المنتظرة لعمليات الإذاعة، الحظر وإدارة الاشتراك.
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
                continue
            try:
                await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=update.message.chat_id,
                    message_id=update.message.message_id
                )
                success += 1
                await asyncio.sleep(0.05)
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
        if not target_input.replace('-', '').isdigit():
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
        if not target_input.replace('-', '').isdigit():
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

    elif state == "WAITING_ADD_CHANNEL":
        context.user_data.pop("admin_state", None)
        input_data = update.message.text.strip().split(",")
        if len(input_data) < 3:
            await update.message.reply_text("⚠️ صيغة الإدخال خاطئة. يرجى الالتزام بالصيغة: `ID_القناة, رابط_الدعوة, اسم_الزر` وصناعة طلب جديد.")
            return True
            
        ch_id_str, invite_link, name = input_data[0].strip(), input_data[1].strip(), input_data[2].strip()
        try:
            ch_id = int(ch_id_str)
            conn = await get_db_connection()
            await conn.execute(
                """
                INSERT INTO forced_channels (channel_id, channel_invite_link, channel_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (channel_id) DO UPDATE SET channel_invite_link = $2, channel_name = $3;
                """,
                ch_id, invite_link, name
            )
            await conn.close()
            await update.message.reply_text(f"✅ تم إضافة/تحديث القناة الإجبارية بنجاح باسم: *{name}*", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("⚠️ يجب أن يكون معرف القناة عبارة عن أرقام (بدءاً بـ -100 غالباً).")
        except Exception as e:
            await update.message.reply_text(f"❌ حدث خطأ في قاعدة البيانات أثناء الإضافة: {e}")
        return True

    elif state == "WAITING_DEL_CHANNEL":
        context.user_data.pop("admin_state", None)
        target_input = update.message.text.strip()
        try:
            ch_id = int(target_input)
            conn = await get_db_connection()
            result = await conn.execute("DELETE FROM forced_channels WHERE channel_id = $1;", ch_id)
            await conn.close()
            
            if "DELETE 0" in result:
                await update.message.reply_text("⚠️ لم يتم العثور على قناة بهذا المعرف في النظام.")
            else:
                await update.message.reply_text(f"✅ تم حذف القناة ذات المعرف `{ch_id}` من نظام الاشتراك الإجباري بنجاح.", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("⚠️ المعرف يجب أن يكون أرقامًا صحيحة فقط.")
        except Exception as e:
            await update.message.reply_text(f"❌ حدث خطأ أثناء الحذف: {e}")
        return True

    return False
