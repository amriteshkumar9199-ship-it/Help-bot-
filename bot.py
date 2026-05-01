"""
OTPKing Solution Bot — FINAL VERSION
- User message aata hai → Admin ko inline REPLY button ke saath aata hai
- Admin sirf "Reply" button dabata hai → apna jawab likhta hai → Send
- Message BOT se user ko jaata hai — koi number expose nahi hota
- Mapping MongoDB mein save hoti hai — restart pe bhi kaam karta hai
"""

import logging
import asyncio
import re
import os
import sys
import json
from datetime import datetime, timezone, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    ForceReply,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from motor.motor_asyncio import AsyncIOMotorClient

# ══════════════════════════════════════════════
#   CONFIG
# ══════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0").strip())
MONGO_URI = os.environ.get("MONGO_URI", "").strip()

DB_NAME    = "otpkingbot"
MAX_WARN   = 3
ONLINE_MIN = 5

if not BOT_TOKEN:
    sys.exit("❌ BOT_TOKEN missing!")
if ADMIN_ID == 0:
    sys.exit("❌ ADMIN_ID missing!")
if not MONGO_URI:
    sys.exit("❌ MONGO_URI missing!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════
#   MONGODB
# ══════════════════════════════════════════════
_mongo = None
db     = None

async def init_db():
    global _mongo, db
    try:
        _mongo = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=8000)
        await _mongo.admin.command("ping")
        db = _mongo[DB_NAME]
        await db.users.create_index("user_id", unique=True)
        await db.users.create_index("last_seen")
        # msg_map: admin_msg_id → user_id (persistent across restarts!)
        await db.msg_map.create_index("admin_msg_id", unique=True)
        await db.msg_map.create_index(
            "created_at",
            expireAfterSeconds=604800  # 7 din baad auto delete
        )
        log.info("✅ MongoDB connected!")
    except Exception as e:
        sys.exit(f"MongoDB fail: {e}")

async def save_msg_map(admin_msg_id: int, user_id: int):
    """Admin message ID → User ID mapping MongoDB mein save karo"""
    try:
        await db.msg_map.update_one(
            {"admin_msg_id": admin_msg_id},
            {"$set": {
                "admin_msg_id": admin_msg_id,
                "user_id":      user_id,
                "created_at":   datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except Exception as e:
        log.warning(f"save_msg_map error: {e}")

async def get_user_from_msg(admin_msg_id: int):
    """Admin message ID se user ID nikalo"""
    try:
        doc = await db.msg_map.find_one({"admin_msg_id": admin_msg_id})
        return doc["user_id"] if doc else None
    except Exception:
        return None

async def get_user(uid: int):
    try:
        return await db.users.find_one({"user_id": uid})
    except Exception:
        return None

async def save_user(tg_user):
    try:
        await db.users.update_one(
            {"user_id": tg_user.id},
            {
                "$set": {
                    "name":      (tg_user.first_name or "").strip() or "Unknown",
                    "username":  tg_user.username or "",
                    "last_seen": datetime.now(timezone.utc),
                },
                "$setOnInsert": {
                    "user_id":   tg_user.id,
                    "joined_at": datetime.now(timezone.utc),
                    "warnings":  0,
                    "is_banned": False,
                },
            },
            upsert=True,
        )
    except Exception as e:
        log.warning(f"save_user error: {e}")

async def get_stats():
    try:
        threshold = datetime.now(timezone.utc) - timedelta(minutes=ONLINE_MIN)
        total   = await db.users.count_documents({})
        banned  = await db.users.count_documents({"is_banned": True})
        online  = await db.users.count_documents({
            "last_seen": {"$gte": threshold},
            "is_banned": False,
        })
        offline = max(total - online - banned, 0)
        return {"total": total, "online": online,
                "offline": offline, "banned": banned}
    except Exception:
        return {"total": 0, "online": 0, "offline": 0, "banned": 0}

# ══════════════════════════════════════════════
#   ABUSIVE DETECTION
# ══════════════════════════════════════════════
_BAD = [
    "chutiya","chutiye","bhosdike","bhosdi","madarchod","madarchodd",
    "behenchod","behnchod","randi","harami","haraami","gandu","gaandu",
    "gaand","lund","lauda","lawda","lavda","teri maa ki","teri maa ko",
    "behen ke","teri behen","kutte ki aulad","kutiya","kamina","kamine",
    "bhadwa","bhadwe","hijda","hijra","tharki","chhinaal","bakrichod",
    "maa chod","teri maa di","lavde","haramzada","haramzadi","jhant",
    "maa ki aankh","maa ka bhosda","teri maa behen ek",
    "mc","bc","bkl","bklod","bhkl",
    "fuck","fucker","fucking","motherfucker","bitch","bastard",
    "asshole","shit","dick","cock","pussy","whore","slut",
]

def _norm(t: str):
    t = t.lower()
    for k, v in {"@":"a","4":"a","3":"e","1":"i","0":"o",
                 "5":"s","$":"s","7":"t","8":"b"}.items():
        t = t.replace(k, v)
    t  = re.sub(r"[^a-z0-9\s]", "", t)
    ns = t.replace(" ", "")
    nd = re.sub(r"(.)\1+", r"\1", ns)
    return ns, nd, t

def is_abusive(text: str) -> bool:
    if not text:
        return False
    if re.fullmatch(r"[\d\s,.\-+₹$%]+", text.strip()):
        return False
    ns, nd, tc = _norm(text)
    for w in _BAD:
        wn, wd, _ = _norm(w)
        if len(wn) <= 4:
            if re.search(r"(?<![a-z])" + re.escape(wn) + r"(?![a-z])", tc):
                return True
        else:
            if wn in ns or wn in nd or wd in ns or wd in nd:
                return True
    return False

def has_link(text: str) -> bool:
    return bool(re.search(
        r"(https?://|www\.|t\.me/|@\w+\.\w+)"
        r"|(\b\w+\.(com|org|net|io|co|in|me|xyz|tv)\b)",
        text, re.I,
    ))

def is_gif(update: Update) -> bool:
    m = update.message
    return bool(m.animation or
                (m.document and m.document.mime_type == "image/gif"))

def warn_text(n: int, name: str) -> str:
    rem = MAX_WARN - n
    if n == 1:
        return (f"⚠️ *WARNING {n}/{MAX_WARN}*\n\n"
                f"Hey {name}! Gaaliyan allowed nahi! 🚫\n"
                f"Abhi *{rem} chances* bache hain.")
    if n == 2:
        return (f"🔴 *LAST WARNING {n}/{MAX_WARN}*\n\n"
                f"{name}, yeh teri *AAKHRI mauka* hai!\n"
                f"Ek aur gaali = *PERMANENT BAN* 🔨")
    return (f"🚫 *PERMANENT BAN!*\n\n"
            f"{name}, {n} warnings ke baad tu ban hai! 🔨")

# ══════════════════════════════════════════════
#   HELPER — target user id
# ══════════════════════════════════════════════
async def _target_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        try:
            return int(ctx.args[0])
        except ValueError:
            return None
    if update.message.reply_to_message:
        rp  = update.message.reply_to_message
        uid = await get_user_from_msg(rp.message_id)
        if uid:
            return uid
        txt = rp.text or rp.caption or ""
        m   = re.search(r"🆔\s*`(\d{5,12})`", txt)
        if m:
            return int(m.group(1))
        m2  = re.search(r"ID[^\d]*(\d{5,12})", txt)
        if m2:
            return int(m2.group(1))
    return None

# ══════════════════════════════════════════════
#   ADMIN REPLY STATE  (waiting for reply text)
# ══════════════════════════════════════════════
# Format: "reply_{user_id}"
REPLY_PREFIX = "reply_"

# ══════════════════════════════════════════════
#   /start
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user

    if u.id == ADMIN_ID:
        s = await get_stats()
        await update.message.reply_text(
            "👑 *OTPKing Solution Bot — ADMIN*\n\n"
            "✅ *Bot sahi kaam kar raha hai!*\n\n"
            "📊 *Live Stats:*\n"
            f"👥 Total   : `{s['total']}`\n"
            f"🟢 Online  : `{s['online']}`\n"
            f"⚫ Offline : `{s['offline']}`\n"
            f"🚫 Banned  : `{s['banned']}`\n\n"
            "💡 *Reply kaise kare:*\n"
            "User ka message aayega — neeche\n"
            "*📨 Reply* button dabao → type karo → Send!\n\n"
            "📋 Commands: /stats /users /banned\n"
            "/ban /unban /warn /broadcast",
            parse_mode="Markdown",
        )
        return

    row = await get_user(u.id)
    if row and row.get("is_banned"):
        await update.message.reply_text(
            "🚫 *Aap OTPKing Bot se permanently ban hain!*",
            parse_mode="Markdown",
        )
        return

    await save_user(u)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📞 Support Bhejo", callback_data="contact"),
        InlineKeyboardButton("ℹ️ Help",          callback_data="help"),
    ]])

    name = u.first_name or "Dost"
    await update.message.reply_text(
        f"🎉 *Welcome to OTPKing Solution Bot!* 🎉\n\n"
        f"Namaste *{name}*! 👋\n\n"
        f"✅ *Bot bilkul sahi kaam kar raha hai!*\n\n"
        f"📩 Apna sawaal ya OTP problem yahan bhejein,\n"
        f"hum jaldi se reply karenge!\n\n"
        f"❌ *Rules:*\n"
        f"• Gaaliyan allowed NAHI — AUTO BAN 🔨\n"
        f"• Links share mat karo 🔗\n"
        f"• GIFs allowed NAHI 🎥\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 Neeche button dabao ya message karo!",
        parse_mode="Markdown",
        reply_markup=kb,
    )

# ══════════════════════════════════════════════
#   CALLBACK BUTTONS
# ══════════════════════════════════════════════
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    # ── Admin reply button ─────────────────────
    # data = "reply_USER_ID"
    if data.startswith(REPLY_PREFIX):
        if q.from_user.id != ADMIN_ID:
            await q.answer("❌ Sirf admin reply kar sakta hai!", show_alert=True)
            return

        user_id = int(data[len(REPLY_PREFIX):])
        row     = await get_user(user_id)
        name    = row.get("name", "User") if row else "User"

        # Admin ko ForceReply bhejo — type karo aur send
        sent = await ctx.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"✍️ *{name}* (`{user_id}`) ko reply karo:\n\n"
                f"Neeche apna jawab type karo aur Send karo 👇"
            ),
            parse_mode="Markdown",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Apna reply yahan likhо..."),
        )
        # Save: is ForceReply message ka ID → user_id
        await save_msg_map(sent.message_id, user_id)
        return

    # ── User buttons ───────────────────────────
    if data == "contact":
        await q.message.reply_text(
            "📩 *Apna sawaal type karo aur bhej do!*\n"
            "Hum jald se jald reply karenge. ✅\n"
            "⏰ Reply time: 5-15 minutes",
            parse_mode="Markdown",
        )
    elif data == "help":
        await q.message.reply_text(
            "ℹ️ *OTPKing Bot Help*\n\n"
            "• OTP problem? → Message karo\n"
            "• Koi service chahiye? → Message karo\n"
            "• Koi bhi sawaal? → Message karo\n\n"
            "Hum 24/7 available hain! 💪",
            parse_mode="Markdown",
        )

# ══════════════════════════════════════════════
#   ADMIN MESSAGE HANDLER
#   — Reply to ForceReply → send to user
#   — Reply to forwarded msg → send to user
# ══════════════════════════════════════════════
async def admin_msg_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    m = update.message

    # ── Not a reply — ignore ───────────────────
    if not m.reply_to_message:
        return

    rp  = m.reply_to_message
    mid = rp.message_id

    # ── Find target user ───────────────────────
    tid = await get_user_from_msg(mid)

    # Fallback: parse from message text
    if not tid:
        txt = rp.text or rp.caption or ""
        match = re.search(r"🆔\s*`(\d{5,12})`", txt)
        if match:
            tid = int(match.group(1))
        else:
            match2 = re.search(r"`(\d{5,12})`.*ko reply", txt)
            if match2:
                tid = int(match2.group(1))

    if not tid:
        # Silent ignore — admin ke doosre replies pe koi error nahi
        return

    # ── Send to user via BOT ───────────────────
    try:
        reply_text = m.text or m.caption or ""

        if m.text:
            await ctx.bot.send_message(
                chat_id=tid,
                text=f"💬 *OTPKing Support:*\n\n{reply_text}",
                parse_mode="Markdown",
            )
        elif m.photo:
            await ctx.bot.send_photo(
                chat_id=tid,
                photo=m.photo[-1].file_id,
                caption=reply_text or None,
            )
        elif m.voice:
            await ctx.bot.send_voice(chat_id=tid, voice=m.voice.file_id)
        elif m.video:
            await ctx.bot.send_video(
                chat_id=tid, video=m.video.file_id,
                caption=reply_text or None,
            )
        elif m.sticker:
            await ctx.bot.send_sticker(chat_id=tid, sticker=m.sticker.file_id)
        elif m.document:
            await ctx.bot.send_document(
                chat_id=tid, document=m.document.file_id,
                caption=reply_text or None,
            )
        elif m.audio:
            await ctx.bot.send_audio(chat_id=tid, audio=m.audio.file_id)

        # Confirm to admin
        c = await m.reply_text(
            f"✅ *Reply bhej diya!* User `{tid}` ko message gaya. 📨",
            parse_mode="Markdown",
        )
        await asyncio.sleep(3)
        try:
            await c.delete()
        except Exception:
            pass

    except Exception as e:
        err_txt = str(e)
        if "bot was blocked" in err_txt or "user is deactivated" in err_txt:
            await m.reply_text(
                f"⚠️ User `{tid}` ne bot block kar diya ya account delete ho gaya.",
                parse_mode="Markdown",
            )
        else:
            await m.reply_text(
                f"❌ Reply nahi gaya!\nError: `{err_txt}`",
                parse_mode="Markdown",
            )

# ══════════════════════════════════════════════
#   /stats
# ══════════════════════════════════════════════
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    s = await get_stats()
    await update.message.reply_text(
        "📊 *OTPKing Bot — STATISTICS*\n\n"
        f"👥 Total Users    : `{s['total']}`\n"
        f"🟢 Online (≤5min) : `{s['online']}`\n"
        f"⚫ Offline         : `{s['offline']}`\n"
        f"🚫 Banned          : `{s['banned']}`\n"
        f"📨 Msgs today      : `{ctx.bot_data.get('msg_count', 0)}`",
        parse_mode="Markdown",
    )

# ══════════════════════════════════════════════
#   /users
# ══════════════════════════════════════════════
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    threshold = datetime.now(timezone.utc) - timedelta(minutes=ONLINE_MIN)
    cursor    = db.users.find({}).sort("last_seen", -1).limit(60)
    rows      = [doc async for doc in cursor]
    if not rows:
        await update.message.reply_text("ℹ️ Koi user nahi abhi tak!")
        return
    lines = []
    for u in rows:
        ls = u.get("last_seen")
        icon  = "🚫" if u.get("is_banned") else ("🟢" if (ls and ls > threshold) else "⚫")
        uname = f" @{u['username']}" if u.get("username") else ""
        jd    = u.get("joined_at", "")
        jd    = jd.strftime("%d/%m/%y") if hasattr(jd, "strftime") else "?"
        lines.append(
            f"{icon} `{u['user_id']}` {u.get('name','?')}{uname} "
            f"⚠️{u.get('warnings',0)} 📅{jd}"
        )
    s    = await get_stats()
    body = (f"👥 *ALL USERS* ({len(rows)})\n\n" + "\n".join(lines) +
            f"\n━━━━━━━━━━━━\n"
            f"🟢{s['online']} ⚫{s['offline']} 🚫{s['banned']} 👥{s['total']}")
    for i in range(0, len(body), 4000):
        await update.message.reply_text(body[i:i+4000], parse_mode="Markdown")

# ══════════════════════════════════════════════
#   /banned
# ══════════════════════════════════════════════
async def cmd_banned(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    rows = [doc async for doc in db.users.find({"is_banned": True})]
    if not rows:
        await update.message.reply_text("✅ Koi banned nahi!", parse_mode="Markdown")
        return
    lines = [
        f"• `{u['user_id']}` {u.get('name','?')}"
        f"{' @'+u['username'] if u.get('username') else ''} ⚠️{u.get('warnings',0)}"
        for u in rows
    ]
    await update.message.reply_text(
        f"🚫 *BANNED ({len(rows)})*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )

# ══════════════════════════════════════════════
#   /ban  /unban  /warn  /removewarn
# ══════════════════════════════════════════════
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    tid = await _target_id(update, ctx)
    if not tid:
        await update.message.reply_text("❌ `/ban USER_ID`", parse_mode="Markdown"); return
    await db.users.update_one({"user_id": tid}, {"$set": {"is_banned": True}}, upsert=True)
    await update.message.reply_text(f"🔨 `{tid}` BAN! ✅", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(tid, "🚫 *Aapko ban kar diya gaya!*", parse_mode="Markdown")
    except: pass

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    tid = await _target_id(update, ctx)
    if not tid:
        await update.message.reply_text("❌ `/unban USER_ID`", parse_mode="Markdown"); return
    await db.users.update_one(
        {"user_id": tid}, {"$set": {"is_banned": False, "warnings": 0}}, upsert=True)
    await update.message.reply_text(f"✅ `{tid}` UNBAN!", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(tid,
            "✅ *Ban hataya gaya!* Ab message kar sakte ho.", parse_mode="Markdown")
    except: pass

async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    tid = await _target_id(update, ctx)
    if not tid:
        await update.message.reply_text("❌ `/warn USER_ID`", parse_mode="Markdown"); return
    row = await get_user(tid) or {}
    wc  = row.get("warnings", 0) + 1
    if wc >= MAX_WARN:
        await db.users.update_one(
            {"user_id": tid}, {"$set": {"warnings": wc, "is_banned": True}}, upsert=True)
        await update.message.reply_text(
            f"🔨 `{tid}` AUTO BAN ({wc} warnings)!", parse_mode="Markdown")
        try: await ctx.bot.send_message(tid, "🚫 *Ban ho gaye!*", parse_mode="Markdown")
        except: pass
    else:
        await db.users.update_one(
            {"user_id": tid}, {"$set": {"warnings": wc}}, upsert=True)
        await update.message.reply_text(
            f"⚠️ `{tid}` warned ({wc}/{MAX_WARN})", parse_mode="Markdown")
        try: await ctx.bot.send_message(tid, warn_text(wc, "Aap"), parse_mode="Markdown")
        except: pass

async def cmd_removewarn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    tid = await _target_id(update, ctx)
    if not tid:
        await update.message.reply_text("❌ `/removewarn USER_ID`", parse_mode="Markdown"); return
    row = await get_user(tid) or {}
    wc  = max(row.get("warnings", 0) - 1, 0)
    await db.users.update_one({"user_id": tid}, {"$set": {"warnings": wc}}, upsert=True)
    await update.message.reply_text(
        f"✅ Warning removed! Ab `{wc}/{MAX_WARN}`", parse_mode="Markdown")

# ══════════════════════════════════════════════
#   /broadcast
# ══════════════════════════════════════════════
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not ctx.args:
        await update.message.reply_text(
            "❌ `/broadcast Apna message`", parse_mode="Markdown"); return
    msg  = " ".join(ctx.args)
    uids = [d["user_id"] async for d in db.users.find({"is_banned": False}, {"user_id": 1})]
    sent = failed = 0
    for uid in uids:
        if uid == ADMIN_ID: continue
        try:
            await ctx.bot.send_message(
                uid, f"📢 *OTPKing — ANNOUNCEMENT*\n\n{msg}", parse_mode="Markdown")
            sent += 1
        except: failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(
        f"✅ Done! 📤 Sent: `{sent}` | ❌ Failed: `{failed}`", parse_mode="Markdown")

# ══════════════════════════════════════════════
#   USER MESSAGE HANDLER — MAIN
# ══════════════════════════════════════════════
async def user_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id == ADMIN_ID:
        return

    ctx.bot_data["msg_count"] = ctx.bot_data.get("msg_count", 0) + 1
    await save_user(u)

    row = await get_user(u.id)

    if row and row.get("is_banned"):
        await update.message.reply_text(
            "🚫 *Aap permanently ban hain!* 😤", parse_mode="Markdown")
        return

    if is_gif(update):
        await update.message.reply_text(
            "❌ *GIF allowed nahi!*", parse_mode="Markdown")
        return

    txt = update.message.text or update.message.caption or ""

    if has_link(txt):
        await update.message.reply_text(
            "❌ *Links allowed nahi!*", parse_mode="Markdown")
        return

    # ── Abusive check ──────────────────────────
    if txt and is_abusive(txt):
        wc = (row.get("warnings", 0) if row else 0) + 1
        if wc >= MAX_WARN:
            await db.users.update_one(
                {"user_id": u.id},
                {"$set": {"warnings": wc, "is_banned": True}},
                upsert=True,
            )
            note = (f"🚫 *AUTO BAN!*\n"
                    f"👤 [{u.first_name}](tg://user?id={u.id})\n"
                    f"🆔 `{u.id}`\n⚠️ {wc}/{MAX_WARN}\n_/unban {u.id}_")
        else:
            await db.users.update_one(
                {"user_id": u.id}, {"$set": {"warnings": wc}}, upsert=True)
            note = (f"⚠️ *GAALI*\n"
                    f"👤 [{u.first_name}](tg://user?id={u.id})\n"
                    f"🆔 `{u.id}`\n⚠️ {wc}/{MAX_WARN}")
        try:
            await ctx.bot.send_message(ADMIN_ID, note, parse_mode="Markdown")
        except: pass
        await update.message.reply_text(
            warn_text(wc, u.first_name or "Bhai"), parse_mode="Markdown")
        return

    # ══════════════════════════════════════════
    #   FORWARD TO ADMIN with 📨 REPLY BUTTON
    # ══════════════════════════════════════════
    try:
        warn_count = row.get("warnings", 0) if row else 0
        uname_str  = f"\n📛 @{u.username}" if u.username else ""
        header = (
            f"📩 *NEW MESSAGE*\n"
            f"👤 [{u.first_name}](tg://user?id={u.id})\n"
            f"🆔 `{u.id}`{uname_str}\n"
            f"⚠️ {warn_count}/{MAX_WARN}\n"
            f"━━━━━━━━━━━━\n"
        )

        # ── INLINE REPLY BUTTON ────────────────
        reply_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📨 Reply Karen",
                callback_data=f"{REPLY_PREFIX}{u.id}"
            )
        ]])

        fwd = None
        m   = update.message

        if m.text:
            fwd = await ctx.bot.send_message(
                ADMIN_ID,
                header + m.text,
                parse_mode="Markdown",
                reply_markup=reply_kb,
            )
        elif m.photo:
            fwd = await ctx.bot.send_photo(
                ADMIN_ID,
                m.photo[-1].file_id,
                caption=header + (m.caption or ""),
                parse_mode="Markdown",
                reply_markup=reply_kb,
            )
        elif m.voice:
            fwd_hdr = await ctx.bot.send_message(
                ADMIN_ID, header, parse_mode="Markdown")
            fwd = await ctx.bot.send_voice(
                ADMIN_ID, m.voice.file_id, reply_markup=reply_kb)
            await save_msg_map(fwd_hdr.message_id, u.id)
        elif m.video:
            fwd = await ctx.bot.send_video(
                ADMIN_ID,
                m.video.file_id,
                caption=header + (m.caption or ""),
                parse_mode="Markdown",
                reply_markup=reply_kb,
            )
        elif m.sticker:
            fwd_hdr = await ctx.bot.send_message(
                ADMIN_ID, header, parse_mode="Markdown")
            fwd = await ctx.bot.send_sticker(
                ADMIN_ID, m.sticker.file_id, reply_markup=reply_kb)
            await save_msg_map(fwd_hdr.message_id, u.id)
        elif m.document:
            fwd = await ctx.bot.send_document(
                ADMIN_ID,
                m.document.file_id,
                caption=header + (m.caption or ""),
                parse_mode="Markdown",
                reply_markup=reply_kb,
            )
        elif m.audio:
            fwd = await ctx.bot.send_audio(
                ADMIN_ID,
                m.audio.file_id,
                caption=header + (m.caption or ""),
                parse_mode="Markdown",
                reply_markup=reply_kb,
            )

        # ── Save mapping MongoDB mein ──────────
        if fwd:
            await save_msg_map(fwd.message_id, u.id)
            log.info(f"Mapped admin_msg {fwd.message_id} → user {u.id}")

        # Ack to user
        c = await update.message.reply_text(
            "✅ *Message mila! Jaldi reply milega.* 📨",
            parse_mode="Markdown",
        )
        await asyncio.sleep(3)
        try:
            await c.delete()
        except Exception:
            pass

    except Exception as e:
        log.error(f"Forward error: {e}")
        await update.message.reply_text("✅ Message mila! Jaldi reply milega.")

# ══════════════════════════════════════════════
#   POST INIT
# ══════════════════════════════════════════════
async def post_init(app: Application):
    await init_db()
    await app.bot.set_my_commands([
        BotCommand("start",      "OTPKing Bot start karo"),
        BotCommand("stats",      "Statistics (Admin)"),
        BotCommand("users",      "User list (Admin)"),
        BotCommand("banned",     "Banned list (Admin)"),
        BotCommand("ban",        "Ban karo (Admin)"),
        BotCommand("unban",      "Unban karo (Admin)"),
        BotCommand("warn",       "Warning do (Admin)"),
        BotCommand("removewarn", "Warning hatao (Admin)"),
        BotCommand("broadcast",  "Broadcast (Admin)"),
    ])
    log.info(f"🚀 OTPKing Bot ready! Admin: {ADMIN_ID}")

# ══════════════════════════════════════════════
#   MAIN
# ══════════════════════════════════════════════
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("users",       cmd_users))
    app.add_handler(CommandHandler("banned",      cmd_banned))
    app.add_handler(CommandHandler("ban",         cmd_ban))
    app.add_handler(CommandHandler("unban",       cmd_unban))
    app.add_handler(CommandHandler("warn",        cmd_warn))
    app.add_handler(CommandHandler("removewarn",  cmd_removewarn))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))

    # Buttons (both user & admin reply buttons)
    app.add_handler(CallbackQueryHandler(on_button))

    # Admin messages — reply handler (MUST be before user_msg)
    app.add_handler(MessageHandler(
        filters.User(ADMIN_ID) & ~filters.COMMAND,
        admin_msg_handler,
    ))

    # User messages
    app.add_handler(MessageHandler(
        ~filters.User(ADMIN_ID) & ~filters.COMMAND,
        user_msg,
    ))

    log.info("🤖 Polling started...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
