import logging
import asyncio
import re
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from motor.motor_asyncio import AsyncIOMotorClient
import os

# =============================================
#   CONFIG - Environment Variables se load hoga
# =============================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://user:pass@cluster.mongodb.net/")
DB_NAME = "telegram_bot"
# =============================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MAX_WARNINGS = 3
ONLINE_TIMEOUT_MINUTES = 5  # Itne minute baad offline maana jayega

# =============================================
#   MONGODB SETUP
# =============================================
mongo_client = None
db = None

async def init_db():
    global mongo_client, db
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client[DB_NAME]
    # Indexes banao fast queries ke liye
    await db.users.create_index("user_id", unique=True)
    await db.users.create_index("last_seen")
    logger.info("✅ MongoDB connected!")

# =============================================
#   DB HELPER FUNCTIONS
# =============================================
async def get_user(user_id: int):
    return await db.users.find_one({"user_id": user_id})

async def upsert_user(user_id: int, data: dict):
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": data, "$setOnInsert": {"joined_at": datetime.now(timezone.utc), "user_id": user_id}},
        upsert=True
    )

async def update_last_seen(user_id: int):
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"last_seen": datetime.now(timezone.utc), "is_active": True}}
    )

async def get_stats():
    total = await db.users.count_documents({})
    banned = await db.users.count_documents({"is_banned": True})
    
    # Online = last 5 minute mein active
    from datetime import timedelta
    online_threshold = datetime.now(timezone.utc) - timedelta(minutes=ONLINE_TIMEOUT_MINUTES)
    online = await db.users.count_documents({
        "last_seen": {"$gte": online_threshold},
        "is_banned": {"$ne": True}
    })
    offline = total - online - banned
    
    return {
        "total": total,
        "online": online,
        "offline": max(offline, 0),
        "banned": banned
    }

async def get_all_users():
    cursor = db.users.find({"is_banned": {"$ne": True}}, {"user_id": 1})
    return [doc["user_id"] async for doc in cursor]

async def get_banned_users_db():
    cursor = db.users.find({"is_banned": True})
    return [doc async for doc in cursor]

# =============================================
#   ABUSIVE WORDS LIST
# =============================================
ABUSIVE_WORDS = [
    "chutiya", "chutiye", "bhosdike", "bhosdi", "bhosdiwale",
    "madarchod", "madarchodd", "maderchod",
    "behenchod", "behnchod",
    "randi", "harami", "haraami",
    "gandu", "gaandu", "gaand",
    "lund", "chutmarike", "chutmariki",
    "lauda", "lawda", "lavda",
    "teri maa ki", "teri maa ko", "teri ma ki",
    "behen ke", "teri behen", "teri bhan",
    "kutte ki aulad", "kutiya",
    "kamina", "kamini", "kamine",
    "gadha", "gadhaa",
    "bhadwa", "bhadwe", "bhadwaa",
    "rakhail", "hijda", "hijra", "hijraa",
    "tharki", "chhinaal",
    "bakrichod", "bakrichoda", "maa chod",
    "teri maa di", "teri bhen di",
    "lavde", "lawde", "khasma nuun khaaney",
    "haramzada", "haramzadi", "jhant", "teri pen",
    "maa ki aankh", "maa ka bhosda",
    "behen ka bhosda", "teri maa behen ek",
    "mc", "bc", "lc", "bkl", "bklod", "bhkl",
    "fuck", "fucker", "fucking", "motherfucker",
    "bitch", "bastard", "asshole", "shit",
    "dick", "cock", "pussy", "whore", "slut",
]

# =============================================
#   TEXT NORMALIZATION
# =============================================
def normalize_text(text: str):
    text = text.lower()
    unicode_map = {'а':'a','е':'e','о':'o','р':'p','с':'c','у':'y','х':'x','ѕ':'s','і':'i','ј':'j','ԁ':'d','ɡ':'g','ո':'n','ս':'u'}
    for k, v in unicode_map.items():
        text = text.replace(k, v)
    leet_map = {'@':'a','4':'a','3':'e','1':'i','!':'i','0':'o','5':'s','$':'s','7':'t','+':'t','8':'b','6':'g','9':'g'}
    for k, v in leet_map.items():
        text = text.replace(k, v)
    text = re.sub(r'[^a-z0-9\s]', '', text)
    text_nospace = text.replace(' ', '')
    text_dedup = re.sub(r'(.)\1+', r'\1', text_nospace)
    return text_nospace, text_dedup, text

def contains_abusive_word(text: str) -> bool:
    if re.fullmatch(r'[\d\s,.\-+₹$%]+', text.strip()):
        return False
    text_nospace, text_dedup, text_clean = normalize_text(text)
    for word in ABUSIVE_WORDS:
        word_norm, word_dedup, _ = normalize_text(word)
        min_len = min(len(word_norm), len(word_dedup))
        if min_len <= 4:
            if re.search(r'(?<![a-z])' + re.escape(word_norm) + r'(?![a-z])', text_clean):
                return True
        else:
            if word_norm in text_nospace or word_norm in text_dedup or word_dedup in text_nospace:
                return True
    return False

def contains_link(text: str) -> bool:
    return bool(re.search(
        r'(https?://|www\.|t\.me/|@\w+\.\w+)|(\b\w+\.(com|org|net|io|co|in|info|biz|xyz|me|tv)\b)',
        text, re.IGNORECASE
    ))

def is_gif(update: Update) -> bool:
    msg = update.message
    return bool(msg.animation or (msg.document and msg.document.mime_type == "image/gif"))

def get_warning_message(warn_count: int, user_name: str) -> str:
    remaining = MAX_WARNINGS - warn_count
    if warn_count == 1:
        return (f"⚠️ *WARNING {warn_count}/{MAX_WARNINGS}*\n\nHey {user_name}! Gaaliyan allowed nahi! 🚫\nAbhi {remaining} chances bache hain.\nAgle baar seedha BAN! 😤")
    elif warn_count == 2:
        return (f"🔴 *WARNING {warn_count}/{MAX_WARNINGS} - LAST CHANCE!*\n\n{user_name}, yeh teri AAKHRI mauka hai! ⚠️\nEk aur gaali = PERMANENT BAN 🔨\nSoch samajh ke type karo! 🤐")
    else:
        return (f"🚫 *PERMANENT BAN!*\n\n{user_name}, tune {warn_count} baar gaali di!\nAb tu PERMANENTLY BAN hai! 🔨")

# =============================================
#   /START
# =============================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if user_id == ADMIN_ID:
        s = await get_stats()
        await update.message.reply_text(
            f"👑 *ADMIN PANEL* ✅\n\n"
            f"📊 *Stats:*\n"
            f"👥 Total Users: `{s['total']}`\n"
            f"🟢 Online: `{s['online']}`\n"
            f"⚫ Offline: `{s['offline']}`\n"
            f"🚫 Banned: `{s['banned']}`\n\n"
            f"📋 *Commands:*\n"
            f"/ban /unban /warn /removewarn\n"
            f"/banned /stats /broadcast /users",
            parse_mode="Markdown"
        )
        return

    user_data = await get_user(user_id)
    if user_data and user_data.get("is_banned"):
        await update.message.reply_text("🚫 *Aap PERMANENTLY BAN hain!*", parse_mode="Markdown")
        return

    # User save karo MongoDB mein
    await upsert_user(user_id, {
        "name": user.first_name or "Unknown",
        "username": user.username or "",
        "last_seen": datetime.now(timezone.utc),
        "is_active": True
    })
    await update_last_seen(user_id)

    keyboard = [[InlineKeyboardButton("📞 Contact Support", callback_data="contact")]]
    welcome_text = (
        "🙏 *Welcome! Bot mein aapka swagat hai!*\n\n"
        "✅ Apna message bhejein, hum jaldi reply karenge.\n\n"
        "❌ *Rules:*\n"
        "• Gaaliyan bilkul allowed nahi\n"
        "• Links share mat karo\n"
        "• GIFs allowed nahi hain\n\n"
        "Rules todne par automatic BAN hoga! 🔨"
    )
    try:
        await update.message.reply_animation(
            animation="https://media.giphy.com/media/3o7abKhOpu0NwenH3O/giphy.gif",
            caption=welcome_text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception:
        await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# =============================================
#   CALLBACK
# =============================================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "contact":
        await query.message.reply_text("📩 Bas apna message type karein! Hum jald reply karenge. ✅")

# =============================================
#   ADMIN: /stats
# =============================================
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    s = await get_stats()
    await update.message.reply_text(
        f"📊 *BOT STATISTICS*\n\n"
        f"👥 *Total Users:* `{s['total']}`\n"
        f"🟢 *Online (last 5 min):* `{s['online']}`\n"
        f"⚫ *Offline:* `{s['offline']}`\n"
        f"🚫 *Banned:* `{s['banned']}`\n"
        f"📨 *Messages Today:* `{context.bot_data.get('msg_count', 0)}`",
        parse_mode="Markdown"
    )

# =============================================
#   ADMIN: /users - Saare users dekho
# =============================================
async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    from datetime import timedelta
    online_threshold = datetime.now(timezone.utc) - timedelta(minutes=ONLINE_TIMEOUT_MINUTES)

    cursor = db.users.find({}, {"user_id": 1, "name": 1, "username": 1, "last_seen": 1, "is_banned": 1, "warnings": 1}).limit(50)
    users = [doc async for doc in cursor]

    if not users:
        await update.message.reply_text("ℹ️ Abhi koi user nahi!", parse_mode="Markdown")
        return

    text = f"👥 *ALL USERS* (first 50)\n\n"
    for u in users:
        last_seen = u.get("last_seen")
        if u.get("is_banned"):
            status = "🚫"
        elif last_seen and last_seen > online_threshold:
            status = "🟢"
        else:
            status = "⚫"

        name = u.get("name", "Unknown")
        uname = f" @{u['username']}" if u.get("username") else ""
        warn = u.get("warnings", 0)
        text += f"{status} `{u['user_id']}` - {name}{uname} [⚠️{warn}]\n"

    s = await get_stats()
    text += f"\n🟢 Online: {s['online']} | ⚫ Offline: {s['offline']} | 🚫 Banned: {s['banned']}"
    await update.message.reply_text(text, parse_mode="Markdown")

# =============================================
#   ADMIN: /banned list
# =============================================
async def banned_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    banned = await get_banned_users_db()
    if not banned:
        await update.message.reply_text("✅ Koi banned user nahi!", parse_mode="Markdown")
        return
    text = f"🚫 *BANNED USERS* ({len(banned)} total)\n\n"
    for u in banned[:50]:
        name = u.get("name", "Unknown")
        uname = f" @{u['username']}" if u.get("username") else ""
        warn = u.get("warnings", 0)
        text += f"• `{u['user_id']}` - {name}{uname} [⚠️{warn}]\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# =============================================
#   ADMIN: /warn
# =============================================
async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    target_id = await _get_target_id(update, context)
    if not target_id:
        await update.message.reply_text("❌ User ID nahi mila! `/warn USER_ID`", parse_mode="Markdown")
        return

    user_data = await get_user(target_id) or {}
    warn_count = user_data.get("warnings", 0) + 1

    if warn_count >= MAX_WARNINGS:
        await db.users.update_one({"user_id": target_id}, {"$set": {"warnings": warn_count, "is_banned": True}})
        await update.message.reply_text(f"🔨 User `{target_id}` AUTO BAN ({warn_count} warnings)!", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=target_id, text="🚫 *Aap BAN ho gaye!*", parse_mode="Markdown")
        except Exception:
            pass
    else:
        await db.users.update_one({"user_id": target_id}, {"$set": {"warnings": warn_count}}, upsert=True)
        await update.message.reply_text(f"⚠️ User `{target_id}` warned ({warn_count}/{MAX_WARNINGS})", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=target_id, text=get_warning_message(warn_count, "Aap"), parse_mode="Markdown")
        except Exception:
            pass

# =============================================
#   ADMIN: /removewarn
# =============================================
async def remove_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ `/removewarn USER_ID`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Sahi ID daalo!", parse_mode="Markdown")
        return
    user_data = await get_user(target_id) or {}
    warn_count = max(user_data.get("warnings", 0) - 1, 0)
    await db.users.update_one({"user_id": target_id}, {"$set": {"warnings": warn_count}}, upsert=True)
    await update.message.reply_text(f"✅ Warning removed! Ab `{warn_count}/{MAX_WARNINGS}`", parse_mode="Markdown")

# =============================================
#   ADMIN: /ban
# =============================================
async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    target_id = await _get_target_id(update, context)
    if not target_id:
        await update.message.reply_text("❌ `/ban USER_ID`", parse_mode="Markdown")
        return
    await db.users.update_one({"user_id": target_id}, {"$set": {"is_banned": True}}, upsert=True)
    await update.message.reply_text(f"🔨 User `{target_id}` BAN! ✅", parse_mode="Markdown")
    try:
        await context.bot.send_message(chat_id=target_id, text="🚫 *Aapko BAN kar diya gaya!*", parse_mode="Markdown")
    except Exception:
        pass

# =============================================
#   ADMIN: /unban
# =============================================
async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    target_id = await _get_target_id(update, context)
    if not target_id:
        await update.message.reply_text("Kisi message reply ya `/unban USER_ID`", parse_mode="Markdown")
        return
    await db.users.update_one({"user_id": target_id}, {"$set": {"is_banned": False, "warnings": 0}}, upsert=True)
    await update.message.reply_text(f"✅ User `{target_id}` UNBAN!", parse_mode="Markdown")
    try:
        await context.bot.send_message(chat_id=target_id, text="✅ *Ban hataya gaya! Ab message kar sakte ho.*\nDobara gaali mat dena! 😤", parse_mode="Markdown")
    except Exception:
        pass

# =============================================
#   ADMIN: /broadcast
# =============================================
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ `/broadcast Aapka message`", parse_mode="Markdown")
        return
    message = ' '.join(context.args)
    all_users = await get_all_users()
    sent, failed = 0, 0
    for uid in all_users:
        if uid != ADMIN_ID:
            try:
                await context.bot.send_message(chat_id=uid, text=f"📢 *ANNOUNCEMENT*\n\n{message}", parse_mode="Markdown")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
    await update.message.reply_text(f"✅ Broadcast done!\n📤 Sent: {sent} | ❌ Failed: {failed}", parse_mode="Markdown")

# =============================================
#   ADMIN REPLY TO USER
# =============================================
async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not update.message.reply_to_message:
        return
    msg_id = update.message.reply_to_message.message_id
    target_user_id = context.bot_data.get(f"msg_{msg_id}")
    if not target_user_id:
        await update.message.reply_text("❌ User ID nahi mila!")
        return
    try:
        if update.message.text:
            await context.bot.send_message(chat_id=target_user_id, text=update.message.text)
        elif update.message.photo:
            await context.bot.send_photo(chat_id=target_user_id, photo=update.message.photo[-1].file_id, caption=update.message.caption or "")
        elif update.message.voice:
            await context.bot.send_voice(chat_id=target_user_id, voice=update.message.voice.file_id)
        elif update.message.video:
            await context.bot.send_video(chat_id=target_user_id, video=update.message.video.file_id, caption=update.message.caption or "")
        elif update.message.sticker:
            await context.bot.send_sticker(chat_id=target_user_id, sticker=update.message.sticker.file_id)
        confirm = await update.message.reply_text("✅ Sent!")
        await asyncio.sleep(2)
        await confirm.delete()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# =============================================
#   HELPER: Target user ID nikalna
# =============================================
async def _get_target_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        try:
            return int(context.args[0])
        except ValueError:
            return None
    if update.message.reply_to_message:
        if update.message.reply_to_message.forward_origin:
            try:
                return update.message.reply_to_message.forward_origin.sender_user.id
            except Exception:
                pass
        msg_id = update.message.reply_to_message.message_id
        uid = context.bot_data.get(f"msg_{msg_id}")
        if uid:
            return uid
        reply_text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        id_match = re.search(r'ID.*?[:`]?\s*(\d{6,12})', reply_text)
        if id_match:
            return int(id_match.group(1))
    return None

# =============================================
#   USER MESSAGE HANDLER
# =============================================
async def user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    if user_id == ADMIN_ID:
        return

    context.bot_data['msg_count'] = context.bot_data.get('msg_count', 0) + 1

    # MongoDB mein user update karo (last_seen)
    await upsert_user(user_id, {
        "name": user.first_name or "Unknown",
        "username": user.username or "",
        "last_seen": datetime.now(timezone.utc),
        "is_active": True
    })

    # Check banned
    user_data = await get_user(user_id)
    if user_data and user_data.get("is_banned"):
        await update.message.reply_text("🚫 *Aap PERMANENTLY BAN hain!* 😤", parse_mode="Markdown")
        return

    if is_gif(update):
        await update.message.reply_text("❌ *GIF allowed nahi!*", parse_mode="Markdown")
        return

    text_to_check = update.message.text or update.message.caption or ""

    if contains_link(text_to_check):
        await update.message.reply_text("❌ *Links allowed nahi!*", parse_mode="Markdown")
        return

    # Abusive word check
    if text_to_check and contains_abusive_word(text_to_check):
        warn_count = (user_data.get("warnings", 0) if user_data else 0) + 1

        if warn_count >= MAX_WARNINGS:
            await db.users.update_one({"user_id": user_id}, {"$set": {"warnings": warn_count, "is_banned": True}}, upsert=True)
            user_info = (f"🚫 *AUTO BAN!*\n👤 [{user.first_name}](tg://user?id={user_id})\n🆔 `{user_id}`\n"
                        f"⚠️ Warnings: {warn_count}/{MAX_WARNINGS}\n_/unban {user_id}_")
        else:
            await db.users.update_one({"user_id": user_id}, {"$set": {"warnings": warn_count}}, upsert=True)
            user_info = (f"⚠️ *GAALI DETECTED*\n👤 [{user.first_name}](tg://user?id={user_id})\n🆔 `{user_id}`\n"
                        f"⚠️ Warnings: {warn_count}/{MAX_WARNINGS}")

        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=user_info, parse_mode="Markdown")
        except Exception:
            pass

        await update.message.reply_text(
            get_warning_message(warn_count, user.first_name or "Bhai"),
            parse_mode="Markdown"
        )
        return

    # Normal message forward to admin
    try:
        user_info = (f"📩 *NEW MESSAGE*\n👤 [{user.first_name}](tg://user?id={user_id})\n🆔 `{user_id}`\n")
        if user.username:
            user_info += f"📛 @{user.username}\n"
        warn_count = user_data.get("warnings", 0) if user_data else 0
        user_info += f"⚠️ Warnings: {warn_count}/{MAX_WARNINGS}\n─────────────────\n"

        forwarded = None
        if update.message.text:
            forwarded = await context.bot.send_message(chat_id=ADMIN_ID, text=user_info + update.message.text, parse_mode="Markdown")
        elif update.message.photo:
            forwarded = await context.bot.send_photo(chat_id=ADMIN_ID, photo=update.message.photo[-1].file_id, caption=user_info + (update.message.caption or ""), parse_mode="Markdown")
        elif update.message.voice:
            await context.bot.send_message(chat_id=ADMIN_ID, text=user_info, parse_mode="Markdown")
            forwarded = await context.bot.send_voice(chat_id=ADMIN_ID, voice=update.message.voice.file_id)
        elif update.message.video:
            forwarded = await context.bot.send_video(chat_id=ADMIN_ID, video=update.message.video.file_id, caption=user_info + (update.message.caption or ""), parse_mode="Markdown")
        elif update.message.sticker:
            await context.bot.send_message(chat_id=ADMIN_ID, text=user_info, parse_mode="Markdown")
            forwarded = await context.bot.send_sticker(chat_id=ADMIN_ID, sticker=update.message.sticker.file_id)
        elif update.message.document:
            forwarded = await context.bot.send_document(chat_id=ADMIN_ID, document=update.message.document.file_id, caption=user_info + (update.message.caption or ""), parse_mode="Markdown")

        if forwarded:
            context.bot_data[f"msg_{forwarded.message_id}"] = user_id

        confirm = await update.message.reply_text("MESSAGE SEND ✅")
        await asyncio.sleep(2)
        await confirm.delete()

    except Exception as e:
        logger.error(f"Forward error: {e}")
        await update.message.reply_text("MESSAGE SEND ✅")

# =============================================
#   MAIN
# =============================================
async def post_init(application: Application):
    await init_db()

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("warn", warn_user))
    app.add_handler(CommandHandler("removewarn", remove_warn))
    app.add_handler(CommandHandler("banned", banned_list))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.User(ADMIN_ID) & filters.REPLY & ~filters.COMMAND, admin_reply))
    app.add_handler(MessageHandler(~filters.User(ADMIN_ID) & ~filters.COMMAND, user_message))

    print("✅ Bot chal pada! MongoDB connected.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
