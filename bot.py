"""
╔══════════════════════════════════════════╗
║     ADVANCED TELEGRAM SUPPORT BOT       ║
║     MongoDB + Railway Ready             ║
╚══════════════════════════════════════════╝
"""

import logging
import asyncio
import re
import os
import sys
from datetime import datetime, timezone, timedelta

# ── Telegram ──────────────────────────────
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ── MongoDB ───────────────────────────────
from motor.motor_asyncio import AsyncIOMotorClient

# ═══════════════════════════════════════════
#   CONFIG  (Railway → Variables tab)
# ═══════════════════════════════════════════
BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "").strip()
ADMIN_ID   = int(os.environ.get("ADMIN_ID",  "0").strip())
MONGO_URI  = os.environ.get("MONGO_URI",  "").strip()

DB_NAME    = "helpbot"
MAX_WARN   = 3
ONLINE_MIN = 5          # minutes

# ── Startup check ─────────────────────────
if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
    sys.exit("❌ BOT_TOKEN missing! Railway → Variables mein daalo.")
if ADMIN_ID == 0:
    sys.exit("❌ ADMIN_ID missing! Railway → Variables mein daalo.")
if not MONGO_URI or MONGO_URI == "YOUR_MONGODB_URI":
    sys.exit("❌ MONGO_URI missing! Railway → Variables mein daalo.")

# ── Logging ───────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════
#   MONGODB
# ═══════════════════════════════════════════
_mongo = None
db     = None

async def init_db():
    global _mongo, db
    try:
        _mongo = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=8000)
        await _mongo.admin.command("ping")          # connection test
        db = _mongo[DB_NAME]
        await db.users.create_index("user_id", unique=True)
        await db.users.create_index("last_seen")
        log.info("✅ MongoDB connected!")
    except Exception as e:
        log.error(f"❌ MongoDB connect failed: {e}")
        sys.exit("MongoDB connection fail. URI check karo!")

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

async def get_stats() -> dict:
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

# ═══════════════════════════════════════════
#   ABUSIVE WORD DETECTION
# ═══════════════════════════════════════════
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
    t = re.sub(r"[^a-z0-9\s]", "", t)
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
    return bool(
        m.animation
        or (m.document and m.document.mime_type == "image/gif")
    )

# ═══════════════════════════════════════════
#   WARNING MESSAGES
# ═══════════════════════════════════════════
def warn_text(n: int, name: str) -> str:
    rem = MAX_WARN - n
    if n == 1:
        return (
            f"⚠️ *WARNING {n}/{MAX_WARN}*\n\n"
            f"Hey {name}! Gaaliyan allowed nahi hai yahan! 🚫\n"
            f"Abhi *{rem} chances* bache hain. Sambhal ja! 😠"
        )
    if n == 2:
        return (
            f"🔴 *LAST WARNING {n}/{MAX_WARN}*\n\n"
            f"{name}, yeh teri *AAKHRI mauka* hai! ⚠️\n"
            f"Ek aur gaali = *PERMANENT BAN* 🔨"
        )
    return (
        f"🚫 *PERMANENT BAN!*\n\n"
        f"{name}, tune *{n} baar* gaali di!\n"
        f"Ab tu permanently ban hai is bot se! 🔨"
    )

# ═══════════════════════════════════════════
#   HELPER: get target user id
# ═══════════════════════════════════════════
async def _target_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        try:
            return int(ctx.args[0])
        except ValueError:
            return None
    if update.message.reply_to_message:
        rp  = update.message.reply_to_message
        uid = ctx.bot_data.get(f"msg_{rp.message_id}")
        if uid:
            return uid
        txt = rp.text or rp.caption or ""
        m   = re.search(r"ID.*?[:`]?\s*(\d{6,12})", txt)
        if m:
            return int(m.group(1))
        try:
            return rp.forward_origin.sender_user.id
        except Exception:
            pass
    return None

# ═══════════════════════════════════════════
#   /start
# ═══════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user

    # ── ADMIN ──────────────────────────────
    if u.id == ADMIN_ID:
        s = await get_stats()
        await update.message.reply_text(
            "👑 *ADMIN PANEL — BOT IS WORKING ✅*\n\n"
            "🟢 *Welcome to My Help Bot!*\n\n"
            f"📊 *Live Stats:*\n"
            f"👥 Total Users   : `{s['total']}`\n"
            f"🟢 Online (≤5m)  : `{s['online']}`\n"
            f"⚫ Offline        : `{s['offline']}`\n"
            f"🚫 Banned         : `{s['banned']}`\n\n"
            "📋 *Commands:*\n"
            "/stats /users /banned\n"
            "/ban /unban /warn /removewarn /broadcast",
            parse_mode="Markdown",
        )
        return

    # ── BANNED USER ─────────────────────────
    row = await get_user(u.id)
    if row and row.get("is_banned"):
        await update.message.reply_text(
            "🚫 *Aap is bot se permanently ban hain!*\n"
            "Gaaliyon ka yahi anjam hota hai. 😤",
            parse_mode="Markdown",
        )
        return

    # ── SAVE USER ───────────────────────────
    await save_user(u)

    # ── WELCOME MESSAGE ─────────────────────
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📞 Support Bhejo", callback_data="contact"),
    ]])

    welcome = (
        "🎉 *Welcome to My Help Bot!* 🎉\n\n"
        "✅ Bot bilkul sahi kaam kar raha hai!\n\n"
        "📩 Apna sawaal ya problem yahan bhejein,\n"
        "hum jaldi se reply karenge!\n\n"
        "❌ *Rules — Zaroor padho:*\n"
        "• 🗣 Gaaliyan allowed NAHI\n"
        "• 🔗 Links share mat karo\n"
        "• 🎥 GIFs allowed NAHI\n\n"
        "⚠️ Rules todne par *AUTO BAN* hoga!\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )

    await update.message.reply_text(
        welcome,
        parse_mode="Markdown",
        reply_markup=kb,
    )

# ═══════════════════════════════════════════
#   CALLBACK BUTTON
# ═══════════════════════════════════════════
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "contact":
        await q.message.reply_text(
            "📩 *Bas apna message type karo aur bhej do!*\n"
            "Hum jald se jald reply karenge. ✅",
            parse_mode="Markdown",
        )

# ═══════════════════════════════════════════
#   /stats
# ═══════════════════════════════════════════
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    s = await get_stats()
    await update.message.reply_text(
        "📊 *BOT STATISTICS*\n\n"
        f"👥 Total Users    : `{s['total']}`\n"
        f"🟢 Online (≤5min) : `{s['online']}`\n"
        f"⚫ Offline         : `{s['offline']}`\n"
        f"🚫 Banned          : `{s['banned']}`\n"
        f"📨 Msgs today      : `{ctx.bot_data.get('msg_count', 0)}`",
        parse_mode="Markdown",
    )

# ═══════════════════════════════════════════
#   /users
# ═══════════════════════════════════════════
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    threshold = datetime.now(timezone.utc) - timedelta(minutes=ONLINE_MIN)
    cursor    = db.users.find({}).sort("last_seen", -1).limit(60)
    rows      = [doc async for doc in cursor]

    if not rows:
        await update.message.reply_text("ℹ️ Abhi tak koi user nahi aaya!")
        return

    lines = []
    for u in rows:
        ls = u.get("last_seen")
        if u.get("is_banned"):
            icon = "🚫"
        elif ls and ls > threshold:
            icon = "🟢"
        else:
            icon = "⚫"
        name  = u.get("name", "?")
        uname = f" @{u['username']}" if u.get("username") else ""
        warn  = u.get("warnings", 0)
        jd    = u.get("joined_at", "")
        jd    = jd.strftime("%d/%m/%y") if hasattr(jd, "strftime") else "?"
        lines.append(
            f"{icon} `{u['user_id']}` {name}{uname} ⚠️{warn} 📅{jd}"
        )

    s    = await get_stats()
    foot = (
        f"\n🟢 Online: {s['online']} | "
        f"⚫ Offline: {s['offline']} | "
        f"🚫 Banned: {s['banned']} | "
        f"👥 Total: {s['total']}"
    )
    body = f"👥 *ALL USERS* ({len(rows)} shown)\n\n" + "\n".join(lines) + foot

    # Telegram 4096 char limit
    for i in range(0, len(body), 4000):
        await update.message.reply_text(body[i:i+4000], parse_mode="Markdown")

# ═══════════════════════════════════════════
#   /banned
# ═══════════════════════════════════════════
async def cmd_banned(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cursor = db.users.find({"is_banned": True})
    rows   = [doc async for doc in cursor]
    if not rows:
        await update.message.reply_text(
            "✅ Koi banned user nahi hai abhi!", parse_mode="Markdown"
        )
        return
    lines = []
    for u in rows:
        uname = f" @{u['username']}" if u.get("username") else ""
        lines.append(
            f"• `{u['user_id']}` {u.get('name','?')}{uname} ⚠️{u.get('warnings',0)}"
        )
    await update.message.reply_text(
        f"🚫 *BANNED USERS ({len(rows)})*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )

# ═══════════════════════════════════════════
#   /ban
# ═══════════════════════════════════════════
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    tid = await _target_id(update, ctx)
    if not tid:
        await update.message.reply_text(
            "❌ Usage: `/ban USER_ID`", parse_mode="Markdown"
        )
        return
    await db.users.update_one(
        {"user_id": tid}, {"$set": {"is_banned": True}}, upsert=True
    )
    await update.message.reply_text(
        f"🔨 User `{tid}` BAN kar diya gaya! ✅", parse_mode="Markdown"
    )
    try:
        await ctx.bot.send_message(
            tid,
            "🚫 *Aapko is bot se BAN kar diya gaya hai!*\n"
            "Admin ne manually ban kiya hai.",
            parse_mode="Markdown",
        )
    except Exception:
        pass

# ═══════════════════════════════════════════
#   /unban
# ═══════════════════════════════════════════
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    tid = await _target_id(update, ctx)
    if not tid:
        await update.message.reply_text(
            "❌ Usage: `/unban USER_ID`", parse_mode="Markdown"
        )
        return
    await db.users.update_one(
        {"user_id": tid},
        {"$set": {"is_banned": False, "warnings": 0}},
        upsert=True,
    )
    await update.message.reply_text(
        f"✅ User `{tid}` UNBAN kar diya gaya!", parse_mode="Markdown"
    )
    try:
        await ctx.bot.send_message(
            tid,
            "✅ *Aapka ban hataya gaya hai!*\n"
            "Ab aap message kar sakte hain.\n"
            "Dobara gaali mat dena! 😤",
            parse_mode="Markdown",
        )
    except Exception:
        pass

# ═══════════════════════════════════════════
#   /warn
# ═══════════════════════════════════════════
async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    tid = await _target_id(update, ctx)
    if not tid:
        await update.message.reply_text(
            "❌ Usage: `/warn USER_ID`", parse_mode="Markdown"
        )
        return
    row = await get_user(tid) or {}
    wc  = row.get("warnings", 0) + 1
    if wc >= MAX_WARN:
        await db.users.update_one(
            {"user_id": tid},
            {"$set": {"warnings": wc, "is_banned": True}},
            upsert=True,
        )
        await update.message.reply_text(
            f"🔨 User `{tid}` ko {wc} warnings ke baad AUTO BAN kar diya!",
            parse_mode="Markdown",
        )
        try:
            await ctx.bot.send_message(
                tid, "🚫 *Ban ho gaye!*", parse_mode="Markdown"
            )
        except Exception:
            pass
    else:
        await db.users.update_one(
            {"user_id": tid}, {"$set": {"warnings": wc}}, upsert=True
        )
        await update.message.reply_text(
            f"⚠️ User `{tid}` warned ({wc}/{MAX_WARN})", parse_mode="Markdown"
        )
        try:
            await ctx.bot.send_message(
                tid, warn_text(wc, "Aap"), parse_mode="Markdown"
            )
        except Exception:
            pass

# ═══════════════════════════════════════════
#   /removewarn
# ═══════════════════════════════════════════
async def cmd_removewarn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    tid = await _target_id(update, ctx)
    if not tid:
        await update.message.reply_text(
            "❌ Usage: `/removewarn USER_ID`", parse_mode="Markdown"
        )
        return
    row = await get_user(tid) or {}
    wc  = max(row.get("warnings", 0) - 1, 0)
    await db.users.update_one(
        {"user_id": tid}, {"$set": {"warnings": wc}}, upsert=True
    )
    await update.message.reply_text(
        f"✅ Warning remove ki! Ab `{wc}/{MAX_WARN}`", parse_mode="Markdown"
    )

# ═══════════════════════════════════════════
#   /broadcast
# ═══════════════════════════════════════════
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text(
            "❌ Usage: `/broadcast Apna message yahan`",
            parse_mode="Markdown",
        )
        return
    msg    = " ".join(ctx.args)
    cursor = db.users.find({"is_banned": False}, {"user_id": 1})
    uids   = [d["user_id"] async for d in cursor]
    sent = failed = 0
    for uid in uids:
        if uid == ADMIN_ID:
            continue
        try:
            await ctx.bot.send_message(
                uid,
                f"📢 *BROADCAST*\n\n{msg}",
                parse_mode="Markdown",
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(
        f"✅ Broadcast complete!\n📤 Sent: {sent} | ❌ Failed: {failed}",
        parse_mode="Markdown",
    )

# ═══════════════════════════════════════════
#   ADMIN REPLY → forward to user
# ═══════════════════════════════════════════
async def admin_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not update.message.reply_to_message:
        return
    mid = update.message.reply_to_message.message_id
    tid = ctx.bot_data.get(f"msg_{mid}")
    if not tid:
        await update.message.reply_text("❌ User ID nahi mila is message ke liye!")
        return
    try:
        m = update.message
        if m.text:
            await ctx.bot.send_message(tid, m.text)
        elif m.photo:
            await ctx.bot.send_photo(
                tid, m.photo[-1].file_id, caption=m.caption or ""
            )
        elif m.voice:
            await ctx.bot.send_voice(tid, m.voice.file_id)
        elif m.video:
            await ctx.bot.send_video(
                tid, m.video.file_id, caption=m.caption or ""
            )
        elif m.sticker:
            await ctx.bot.send_sticker(tid, m.sticker.file_id)
        elif m.document:
            await ctx.bot.send_document(
                tid, m.document.file_id, caption=m.caption or ""
            )
        c = await update.message.reply_text("✅ Reply sent!")
        await asyncio.sleep(2)
        await c.delete()
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ═══════════════════════════════════════════
#   USER MESSAGE HANDLER
# ═══════════════════════════════════════════
async def user_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id == ADMIN_ID:
        return

    ctx.bot_data["msg_count"] = ctx.bot_data.get("msg_count", 0) + 1

    # Update last_seen in MongoDB
    await save_user(u)

    row = await get_user(u.id)

    # Banned?
    if row and row.get("is_banned"):
        await update.message.reply_text(
            "🚫 *Aap permanently ban hain!* 😤", parse_mode="Markdown"
        )
        return

    # GIF check
    if is_gif(update):
        await update.message.reply_text(
            "❌ *GIF allowed nahi hai is bot mein!*", parse_mode="Markdown"
        )
        return

    txt = update.message.text or update.message.caption or ""

    # Link check
    if has_link(txt):
        await update.message.reply_text(
            "❌ *Links allowed nahi hain!*", parse_mode="Markdown"
        )
        return

    # ── Abusive check ───────────────────────
    if txt and is_abusive(txt):
        wc = (row.get("warnings", 0) if row else 0) + 1

        if wc >= MAX_WARN:
            await db.users.update_one(
                {"user_id": u.id},
                {"$set": {"warnings": wc, "is_banned": True}},
                upsert=True,
            )
            note = (
                f"🚫 *AUTO BAN!*\n"
                f"👤 [{u.first_name}](tg://user?id={u.id})\n"
                f"🆔 `{u.id}`\n"
                f"⚠️ {wc}/{MAX_WARN} warnings\n"
                f"_Unban: /unban {u.id}_"
            )
        else:
            await db.users.update_one(
                {"user_id": u.id},
                {"$set": {"warnings": wc}},
                upsert=True,
            )
            note = (
                f"⚠️ *GAALI DETECTED*\n"
                f"👤 [{u.first_name}](tg://user?id={u.id})\n"
                f"🆔 `{u.id}`\n"
                f"⚠️ {wc}/{MAX_WARN}"
            )

        try:
            await ctx.bot.send_message(ADMIN_ID, note, parse_mode="Markdown")
        except Exception:
            pass

        await update.message.reply_text(
            warn_text(wc, u.first_name or "Bhai"), parse_mode="Markdown"
        )
        return

    # ── Forward to admin ────────────────────
    try:
        warn_count = row.get("warnings", 0) if row else 0
        header = (
            f"📩 *NEW MESSAGE*\n"
            f"👤 [{u.first_name}](tg://user?id={u.id})\n"
            f"🆔 `{u.id}`"
        )
        if u.username:
            header += f"\n📛 @{u.username}"
        header += f"\n⚠️ {warn_count}/{MAX_WARN}\n━━━━━━━━━━━━\n"

        fwd = None
        m   = update.message
        if m.text:
            fwd = await ctx.bot.send_message(
                ADMIN_ID, header + m.text, parse_mode="Markdown"
            )
        elif m.photo:
            fwd = await ctx.bot.send_photo(
                ADMIN_ID,
                m.photo[-1].file_id,
                caption=header + (m.caption or ""),
                parse_mode="Markdown",
            )
        elif m.voice:
            await ctx.bot.send_message(ADMIN_ID, header, parse_mode="Markdown")
            fwd = await ctx.bot.send_voice(ADMIN_ID, m.voice.file_id)
        elif m.video:
            fwd = await ctx.bot.send_video(
                ADMIN_ID,
                m.video.file_id,
                caption=header + (m.caption or ""),
                parse_mode="Markdown",
            )
        elif m.sticker:
            await ctx.bot.send_message(ADMIN_ID, header, parse_mode="Markdown")
            fwd = await ctx.bot.send_sticker(ADMIN_ID, m.sticker.file_id)
        elif m.document:
            fwd = await ctx.bot.send_document(
                ADMIN_ID,
                m.document.file_id,
                caption=header + (m.caption or ""),
                parse_mode="Markdown",
            )
        elif m.audio:
            fwd = await ctx.bot.send_audio(
                ADMIN_ID,
                m.audio.file_id,
                caption=header + (m.caption or ""),
                parse_mode="Markdown",
            )

        if fwd:
            ctx.bot_data[f"msg_{fwd.message_id}"] = u.id

        c = await update.message.reply_text("MESSAGE SEND ✅")
        await asyncio.sleep(2)
        await c.delete()

    except Exception as e:
        log.error(f"Forward error: {e}")
        await update.message.reply_text("MESSAGE SEND ✅")

# ═══════════════════════════════════════════
#   POST INIT — runs after bot connects
# ═══════════════════════════════════════════
async def post_init(app: Application):
    await init_db()
    # Set bot command menu
    await app.bot.set_my_commands([
        BotCommand("start",       "Bot start karo"),
        BotCommand("stats",       "Statistics dekho (Admin)"),
        BotCommand("users",       "Saare users dekho (Admin)"),
        BotCommand("banned",      "Banned list (Admin)"),
        BotCommand("ban",         "User ban karo (Admin)"),
        BotCommand("unban",       "User unban karo (Admin)"),
        BotCommand("warn",        "Warning do (Admin)"),
        BotCommand("removewarn",  "Warning hatao (Admin)"),
        BotCommand("broadcast",   "Sabko message bhejo (Admin)"),
    ])
    log.info(f"🚀 Bot ready! Admin ID: {ADMIN_ID}")

# ═══════════════════════════════════════════
#   MAIN
# ═══════════════════════════════════════════
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("users",       cmd_users))
    app.add_handler(CommandHandler("banned",      cmd_banned))
    app.add_handler(CommandHandler("ban",         cmd_ban))
    app.add_handler(CommandHandler("unban",       cmd_unban))
    app.add_handler(CommandHandler("warn",        cmd_warn))
    app.add_handler(CommandHandler("removewarn",  cmd_removewarn))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))

    # Inline buttons
    app.add_handler(CallbackQueryHandler(on_button))

    # Admin reply (must be before user_msg)
    app.add_handler(MessageHandler(
        filters.User(ADMIN_ID) & filters.REPLY & ~filters.COMMAND,
        admin_reply,
    ))

    # All user messages
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
