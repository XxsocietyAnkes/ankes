import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import asyncpg
from telegram import (
    Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    MessageHandler, ChatMemberHandler, filters
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID", "").strip()

if not TOKEN:
    raise SystemExit("BOT_TOKEN belum diisi.")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL belum diisi.")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("ankes_v3")

pool = None
flood_cache = defaultdict(deque)
welcome_cache = {}

DEFAULTS = {
    "enabled": True,
    "anti_gcast": True,
    "anti_link": False,
    "anti_flood": True,
    "anti_mention": False,
    "anti_spam": True,
    "welcome": True,
    "expert": False,
    "flood_limit": 5,
    "flood_window": 8,
    "flood_action": "mute",
    "mute_seconds": 300,
    "max_strikes": 3,
    "strike_action": "ban",
    "mention_limit": 5,
    "max_message_length": 4096,
    "allow_telegram_links": False,
    "allow_youtube_links": False,
    "allow_instagram_links": False,
    "allow_admins": True,
}

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id BIGINT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            anti_gcast BOOLEAN NOT NULL DEFAULT TRUE,
            anti_link BOOLEAN NOT NULL DEFAULT FALSE,
            anti_flood BOOLEAN NOT NULL DEFAULT TRUE,
            anti_mention BOOLEAN NOT NULL DEFAULT FALSE,
            anti_spam BOOLEAN NOT NULL DEFAULT TRUE,
            welcome BOOLEAN NOT NULL DEFAULT TRUE,
            expert BOOLEAN NOT NULL DEFAULT FALSE,
            flood_limit INTEGER NOT NULL DEFAULT 5,
            flood_window INTEGER NOT NULL DEFAULT 8,
            flood_action TEXT NOT NULL DEFAULT 'mute',
            mute_seconds INTEGER NOT NULL DEFAULT 300,
            max_strikes INTEGER NOT NULL DEFAULT 3,
            strike_action TEXT NOT NULL DEFAULT 'ban',
            mention_limit INTEGER NOT NULL DEFAULT 5,
            max_message_length INTEGER NOT NULL DEFAULT 4096,
            allow_telegram_links BOOLEAN NOT NULL DEFAULT FALSE,
            allow_youtube_links BOOLEAN NOT NULL DEFAULT FALSE,
            allow_instagram_links BOOLEAN NOT NULL DEFAULT FALSE,
            allow_admins BOOLEAN NOT NULL DEFAULT TRUE
        );
        CREATE TABLE IF NOT EXISTS whitelist_users (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            PRIMARY KEY(chat_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS blacklist_users (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            PRIMARY KEY(chat_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS blacklist_text (
            chat_id BIGINT NOT NULL,
            phrase TEXT NOT NULL,
            PRIMARY KEY(chat_id,phrase)
        );
        CREATE TABLE IF NOT EXISTS strikes (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS allowed_domains (
            chat_id BIGINT NOT NULL,
            domain TEXT NOT NULL,
            PRIMARY KEY(chat_id,domain)
        );
        CREATE TABLE IF NOT EXISTS action_logs (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT,
            action TEXT NOT NULL,
            reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

async def ensure_chat(chat_id):
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO chats(chat_id) VALUES($1) ON CONFLICT DO NOTHING", chat_id
        )

async def get_settings(chat_id):
    await ensure_chat(chat_id)
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT * FROM chats WHERE chat_id=$1", chat_id)
        return dict(row)

async def set_setting(chat_id, key, value):
    allowed = set(DEFAULTS)
    if key not in allowed:
        raise ValueError(key)
    await ensure_chat(chat_id)
    async with pool.acquire() as c:
        await c.execute(f"UPDATE chats SET {key}=$1 WHERE chat_id=$2", value, chat_id)

async def log_action(chat_id, user_id, action, reason):
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO action_logs(chat_id,user_id,action,reason) VALUES($1,$2,$3,$4)",
            chat_id, user_id, action, reason
        )

def norm(text):
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()

def message_text(m):
    return "\n".join(x for x in [m.text, m.caption] if x)

def urls(text):
    return re.findall(
        r"(?:https?://|www\.|t\.me/|telegram\.me/)[^\s<>()]+", text or "", re.I
    )

def mentions(text):
    return re.findall(r"(?<!\w)@\w{3,}", text or "")

def is_forward(m):
    return (
        getattr(m, "forward_origin", None) is not None
        or getattr(m, "forward_from", None) is not None
    )

def url_domain(raw):
    x = raw
    if x.startswith("www."):
        x = "https://" + x
    if x.startswith("t.me/") or x.startswith("telegram.me/"):
        x = "https://" + x
    try:
        from urllib.parse import urlparse
        host = urlparse(x).netloc.lower().split(":")[0]
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""

async def admin(update, context, user_id=None):
    chat = update.effective_chat
    uid = user_id or update.effective_user.id
    try:
        m = await context.bot.get_chat_member(chat.id, uid)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        return False

async def whitelist(chat_id, user_id):
    async with pool.acquire() as c:
        return await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM whitelist_users WHERE chat_id=$1 AND user_id=$2)",
            chat_id, user_id
        )

async def add_strike(chat_id, user_id):
    async with pool.acquire() as c:
        row = await c.fetchrow("""
        INSERT INTO strikes(chat_id,user_id,count) VALUES($1,$2,1)
        ON CONFLICT(chat_id,user_id)
        DO UPDATE SET count=strikes.count+1
        RETURNING count
        """, chat_id, user_id)
        return row["count"]

async def clear_strike(chat_id, user_id):
    async with pool.acquire() as c:
        await c.execute(
            "DELETE FROM strikes WHERE chat_id=$1 AND user_id=$2", chat_id, user_id
        )

async def mute(context, chat_id, user_id, seconds):
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    perms = ChatPermissions(can_send_messages=False)
    await context.bot.restrict_chat_member(
        chat_id, user_id, permissions=perms, until_date=until
    )

async def ban(context, chat_id, user_id):
    await context.bot.ban_chat_member(chat_id, user_id, revoke_messages=True)

async def delete(m):
    try:
        await m.delete()
        return True
    except TelegramError:
        return False

async def audit(context, chat_id, user_id, action, reason):
    await log_action(chat_id, user_id, action, reason)
    if LOG_CHAT_ID:
        try:
            await context.bot.send_message(
                int(LOG_CHAT_ID),
                f"🛡️ ANKES V3\nChat: {chat_id}\nUser: {user_id}\n"
                f"Action: {action}\nReason: {reason}"
            )
        except Exception:
            pass

async def start(update, context):
    await update.effective_message.reply_text(
        "🛡️ ANKES V3\n"
        "Anti-GCast • Anti-Link • Anti-Flood • Anti-Mention • Anti-Spam\n"
        "Mute/Ban • Strike • Whitelist • Welcome • PostgreSQL\n\n"
        "Gunakan /settings di grup."
    )

async def help_cmd(update, context):
    await update.effective_message.reply_text(
        "🛡️ ANKES V3 COMMAND\n\n"
        "/settings — panel lengkap\n"
        "/status — ringkasan\n"
        "/whitelist ID — whitelist user\n"
        "/unwhitelist ID — hapus whitelist\n"
        "/blacklist ID — blacklist user\n"
        "/unblacklist ID — hapus blacklist\n"
        "/bltext kata/frasa — blacklist teks\n"
        "/unbltext kata/frasa — hapus teks\n"
        "/bltextlist — daftar teks\n"
        "/allowdomain domain.com — izinkan domain\n"
        "/delallowdomain domain.com — hapus domain\n"
        "/allowlist — daftar domain\n"
        "/mute [detik] — reply untuk mute\n"
        "/ban — reply untuk ban\n"
        "/warn — reply untuk strike\n"
        "/clearstrike — reply untuk reset strike"
    )

async def status(update, context):
    if not await admin(update, context):
        await update.effective_message.reply_text("❌ Admin only.")
        return
    s = await get_settings(update.effective_chat.id)
    await update.effective_message.reply_text(
        "🛡️ ANKES V3 STATUS\n\n"
        f"Master: {'ON' if s['enabled'] else 'OFF'}\n"
        f"Anti-GCast: {'ON' if s['anti_gcast'] else 'OFF'}\n"
        f"Anti-Link: {'ON' if s['anti_link'] else 'OFF'}\n"
        f"Anti-Flood: {'ON' if s['anti_flood'] else 'OFF'}\n"
        f"Anti-Mention: {'ON' if s['anti_mention'] else 'OFF'}\n"
        f"Anti-Spam: {'ON' if s['anti_spam'] else 'OFF'}\n"
        f"Welcome: {'ON' if s['welcome'] else 'OFF'}\n"
        f"Expert: {'ON' if s['expert'] else 'OFF'}\n\n"
        f"Flood: {s['flood_limit']} pesan/{s['flood_window']} detik\n"
        f"Flood action: {s['flood_action']}\n"
        f"Mute: {s['mute_seconds']} detik\n"
        f"Max strike: {s['max_strikes']}\n"
        f"Strike action: {s['strike_action']}\n"
        f"Mention limit: {s['mention_limit']}\n"
        f"Max message: {s['max_message_length']}"
    )

def kb(s):
    def t(label, key):
        return InlineKeyboardButton(
            f"{'🟢' if s[key] else '🔴'} {label}", callback_data=f"tg:{key}"
        )
    return InlineKeyboardMarkup([
        [t("Master", "enabled"), t("GCast", "anti_gcast")],
        [t("Anti-Link", "anti_link"), t("Anti-Flood", "anti_flood")],
        [t("Anti-Mention", "anti_mention"), t("Anti-Spam", "anti_spam")],
        [t("Welcome", "welcome"), t("Expert", "expert")],
        [
            InlineKeyboardButton("⚙️ Flood", callback_data="menu:flood"),
            InlineKeyboardButton("⚠️ Strike", callback_data="menu:strike")
        ],
        [
            InlineKeyboardButton("🔗 Link Allowlist", callback_data="menu:links"),
            InlineKeyboardButton("👥 User", callback_data="menu:user")
        ],
        [
            InlineKeyboardButton("📊 Logs", callback_data="menu:logs"),
            InlineKeyboardButton("🔄 Refresh", callback_data="refresh")
        ]
    ])

async def settings_cmd(update, context):
    if not await admin(update, context):
        await update.effective_message.reply_text("❌ Admin only.")
        return
    s = await get_settings(update.effective_chat.id)
    await update.effective_message.reply_text(
        "🛡️ ANKES V3 SETTINGS\nPilih kategori:",
        reply_markup=kb(s)
    )

async def settings_cb(update, context):
    q = update.callback_query
    if not await admin(update, context):
        await q.answer("Admin only.", show_alert=True)
        return
    await q.answer()
    chat_id = update.effective_chat.id
    data = q.data
    s = await get_settings(chat_id)

    if data.startswith("tg:"):
        key = data.split(":",1)[1]
        await set_setting(chat_id, key, not s[key])
        s = await get_settings(chat_id)
        await q.edit_message_reply_markup(reply_markup=kb(s))
        return

    if data == "refresh":
        s = await get_settings(chat_id)
        await q.edit_message_text("🛡️ ANKES V3 SETTINGS", reply_markup=kb(s))
        return

    if data == "menu:flood":
        await q.edit_message_text(
            f"⚙️ FLOOD\n\n"
            f"Limit: {s['flood_limit']} pesan\n"
            f"Window: {s['flood_window']} detik\n"
            f"Action: {s['flood_action']}\n"
            f"Mute: {s['mute_seconds']} detik\n\n"
            "Command:\n"
            "/setflood LIMIT WINDOW\n"
            "/floodaction delete|mute|ban\n"
            "/setmute DETIK",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Kembali", callback_data="back")
            ]])
        )
        return

    if data == "menu:strike":
        await q.edit_message_text(
            f"⚠️ STRIKE\n\n"
            f"Max strike: {s['max_strikes']}\n"
            f"Action: {s['strike_action']}\n\n"
            "Command:\n"
            "/setstrike JUMLAH\n"
            "/strikeaction mute|ban|delete",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Kembali", callback_data="back")
            ]])
        )
        return

    if data == "menu:links":
        async with pool.acquire() as c:
            rows = await c.fetch(
                "SELECT domain FROM allowed_domains WHERE chat_id=$1 ORDER BY domain",
                chat_id
            )
        domains = "\n".join("• "+r["domain"] for r in rows) or "Kosong"
        await q.edit_message_text(
            "🔗 LINK ALLOWLIST\n\n" + domains[:3500] +
            "\n\nCommand:\n/allowdomain example.com\n/delallowdomain example.com\n/allowlist",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Kembali", callback_data="back")
            ]])
        )
        return

    if data == "menu:user":
        await q.edit_message_text(
            "👥 USER MANAGEMENT\n\n"
            "/whitelist USER_ID\n"
            "/unwhitelist USER_ID\n"
            "/blacklist USER_ID\n"
            "/unblacklist USER_ID",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Kembali", callback_data="back")
            ]])
        )
        return

    if data == "menu:logs":
        async with pool.acquire() as c:
            rows = await c.fetch("""
            SELECT user_id,action,reason,created_at
            FROM action_logs WHERE chat_id=$1
            ORDER BY id DESC LIMIT 10
            """, chat_id)
        txt = "\n".join(
            f"• {r['action']} | {r['user_id']} | {r['reason']}"
            for r in rows
        ) or "Belum ada log."
        await q.edit_message_text(
            "📊 10 LOG TERAKHIR\n\n" + txt[:3800],
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Kembali", callback_data="back")
            ]])
        )
        return

    if data == "back":
        s = await get_settings(chat_id)
        await q.edit_message_text("🛡️ ANKES V3 SETTINGS", reply_markup=kb(s))

async def setflood(update, context):
    if not await admin(update, context) or len(context.args) != 2:
        await update.effective_message.reply_text("Gunakan /setflood 5 8")
        return
    limit, window = map(int, context.args)
    if not (2 <= limit <= 100 and 1 <= window <= 300):
        await update.effective_message.reply_text("❌ Nilai tidak valid.")
        return
    await set_setting(update.effective_chat.id, "flood_limit", limit)
    await set_setting(update.effective_chat.id, "flood_window", window)
    await update.effective_message.reply_text("✅ Flood settings diperbarui.")

async def floodaction(update, context):
    if not await admin(update, context) or not context.args:
        return
    action = context.args[0].lower()
    if action not in ("delete","mute","ban"):
        await update.effective_message.reply_text("Pilih: delete, mute, ban")
        return
    await set_setting(update.effective_chat.id, "flood_action", action)
    await update.effective_message.reply_text("✅ Flood action diperbarui.")

async def setmute(update, context):
    if not await admin(update, context) or not context.args:
        return
    sec = int(context.args[0])
    if not 10 <= sec <= 86400:
        await update.effective_message.reply_text("❌ Mute 10–86400 detik.")
        return
    await set_setting(update.effective_chat.id, "mute_seconds", sec)
    await update.effective_message.reply_text("✅ Durasi mute diperbarui.")

async def setstrike(update, context):
    if not await admin(update, context) or not context.args:
        return
    n = int(context.args[0])
    if not 1 <= n <= 100:
        return
    await set_setting(update.effective_chat.id, "max_strikes", n)
    await update.effective_message.reply_text("✅ Max strike diperbarui.")

async def strikeaction(update, context):
    if not await admin(update, context) or not context.args:
        return
    a = context.args[0].lower()
    if a not in ("delete","mute","ban"):
        return
    await set_setting(update.effective_chat.id, "strike_action", a)
    await update.effective_message.reply_text("✅ Strike action diperbarui.")

async def userlist_cmd(update, context):
    if not await admin(update, context):
        return
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT user_id FROM whitelist_users WHERE chat_id=$1 ORDER BY user_id",
            update.effective_chat.id
        )
    await update.effective_message.reply_text(
        "👥 WHITELIST\n\n" + ("\n".join("• "+str(r["user_id"]) for r in rows) or "Kosong")
    )

async def user_addremove(update, context, table, add):
    if not await admin(update, context) or not context.args:
        return
    uid = int(context.args[0])
    async with pool.acquire() as c:
        if add:
            await c.execute(
                f"INSERT INTO {table}(chat_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                update.effective_chat.id, uid
            )
        else:
            await c.execute(
                f"DELETE FROM {table} WHERE chat_id=$1 AND user_id=$2",
                update.effective_chat.id, uid
            )
    await update.effective_message.reply_text("✅ Berhasil.")

async def bltext(update, context):
    if not await admin(update, context):
        return
    p = norm(" ".join(context.args))
    if not p:
        return
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO blacklist_text(chat_id,phrase) VALUES($1,$2) ON CONFLICT DO NOTHING",
            update.effective_chat.id, p
        )
    await update.effective_message.reply_text("🚫 Blacklist teks ditambahkan.")

async def unbltext(update, context):
    if not await admin(update, context):
        return
    p = norm(" ".join(context.args))
    async with pool.acquire() as c:
        await c.execute(
            "DELETE FROM blacklist_text WHERE chat_id=$1 AND phrase=$2",
            update.effective_chat.id, p
        )
    await update.effective_message.reply_text("✅ Dihapus.")

async def bltextlist(update, context):
    if not await admin(update, context):
        return
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT phrase FROM blacklist_text WHERE chat_id=$1 ORDER BY phrase",
            update.effective_chat.id
        )
    await update.effective_message.reply_text(
        "🚫 BLACKLIST TEXT\n\n" +
        ("\n".join("• "+r["phrase"] for r in rows) or "Kosong")
    )

async def allowdomain(update, context):
    if not await admin(update, context) or not context.args:
        return
    d = context.args[0].lower().replace("https://","").replace("http://","").split("/")[0]
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO allowed_domains(chat_id,domain) VALUES($1,$2) ON CONFLICT DO NOTHING",
            update.effective_chat.id, d
        )
    await update.effective_message.reply_text("✅ Domain diizinkan.")

async def delallowdomain(update, context):
    if not await admin(update, context) or not context.args:
        return
    d = context.args[0].lower().replace("https://","").replace("http://","").split("/")[0]
    async with pool.acquire() as c:
        await c.execute(
            "DELETE FROM allowed_domains WHERE chat_id=$1 AND domain=$2",
            update.effective_chat.id, d
        )
    await update.effective_message.reply_text("✅ Domain dihapus.")

async def allowlist(update, context):
    if not await admin(update, context):
        return
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT domain FROM allowed_domains WHERE chat_id=$1 ORDER BY domain",
            update.effective_chat.id
        )
    await update.effective_message.reply_text(
        "🔗 ALLOWED DOMAINS\n\n" +
        ("\n".join("• "+r["domain"] for r in rows) or "Kosong")
    )

async def manual_mute(update, context):
    if not await admin(update, context):
        return
    r = update.effective_message.reply_to_message
    if not r or not r.from_user:
        return
    sec = int(context.args[0]) if context.args else (await get_settings(update.effective_chat.id))["mute_seconds"]
    await mute(context, update.effective_chat.id, r.from_user.id, sec)
    await audit(context, update.effective_chat.id, r.from_user.id, "mute", "manual")
    await update.effective_message.reply_text(f"🔇 Dimute {sec} detik.")

async def manual_ban(update, context):
    if not await admin(update, context):
        return
    r = update.effective_message.reply_to_message
    if not r or not r.from_user:
        return
    await ban(context, update.effective_chat.id, r.from_user.id)
    await audit(context, update.effective_chat.id, r.from_user.id, "ban", "manual")
    await update.effective_message.reply_text("🚫 Dibanned.")

async def manual_warn(update, context):
    if not await admin(update, context):
        return
    r = update.effective_message.reply_to_message
    if not r or not r.from_user:
        return
    n = await add_strike(update.effective_chat.id, r.from_user.id)
    s = await get_settings(update.effective_chat.id)
    if n >= s["max_strikes"]:
        try:
            if s["strike_action"] == "ban":
                await ban(context, update.effective_chat.id, r.from_user.id)
            elif s["strike_action"] == "mute":
                await mute(context, update.effective_chat.id, r.from_user.id, s["mute_seconds"])
            await audit(context, update.effective_chat.id, r.from_user.id, s["strike_action"], f"strike {n}")
            await update.effective_message.reply_text(f"⚠️ Strike {n}: aksi {s['strike_action']}.")
            return
        except TelegramError:
            pass
    await update.effective_message.reply_text(f"⚠️ Strike {n}/{s['max_strikes']}.")

async def clearstrike(update, context):
    if not await admin(update, context):
        return
    r = update.effective_message.reply_to_message
    if not r or not r.from_user:
        return
    await clear_strike(update.effective_chat.id, r.from_user.id)
    await update.effective_message.reply_text("✅ Strike direset.")

async def welcome(update, context):
    cm = update.chat_member
    if not cm:
        return
    old, new = cm.old_chat_member.status, cm.new_chat_member.status
    if new not in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        return
    if old in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        return
    chat = update.effective_chat
    s = await get_settings(chat.id)
    if not s["welcome"]:
        return
    u = cm.new_chat_member.user
    key = (chat.id, u.id)
    now = time.monotonic()
    if now - welcome_cache.get(key, 0) < 10:
        return
    welcome_cache[key] = now
    await context.bot.send_message(
        chat.id,
        f"👋 Selamat datang {u.mention_html()} di <b>{chat.title}</b>!",
        parse_mode="HTML"
    )

async def filter_message(update, context):
    m = update.effective_message
    chat = update.effective_chat
    u = update.effective_user
    if not m or not chat or chat.type not in ("group","supergroup") or not u:
        return
    s = await get_settings(chat.id)
    if not s["enabled"]:
        return
    if s["allow_admins"] and await admin(update, context):
        return
    if await whitelist(chat.id, u.id):
        return

    text = norm(message_text(m))
    reason = None
    action = "delete"

    async with pool.acquire() as c:
        if await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM blacklist_users WHERE chat_id=$1 AND user_id=$2)",
            chat.id, u.id
        ):
            reason = "blacklist user"

        if not reason and text:
            rows = await c.fetch(
                "SELECT phrase FROM blacklist_text WHERE chat_id=$1", chat.id
            )
            if any(r["phrase"] in text for r in rows):
                reason = "blacklist text"

    if not reason and s["anti_gcast"] and is_forward(m):
        reason = "gcast/forward"

    found_urls = urls(text)
    if not reason and s["anti_link"] and found_urls:
        bad = []
        async with pool.acquire() as c:
            allowed = {r["domain"] for r in await c.fetch(
                "SELECT domain FROM allowed_domains WHERE chat_id=$1", chat.id
            )}
        for raw in found_urls:
            d = url_domain(raw)
            ok = d in allowed
            if s["allow_telegram_links"] and (d == "t.me" or d == "telegram.me"):
                ok = True
            if s["allow_youtube_links"] and d in ("youtube.com","youtu.be"):
                ok = True
            if s["allow_instagram_links"] and d in ("instagram.com","instagr.am"):
                ok = True
            if not ok:
                bad.append(d)
        if bad:
            reason = "anti-link"

    if not reason and s["anti_mention"] and len(mentions(text)) > s["mention_limit"]:
        reason = "anti-mention"

    if not reason and s["anti_spam"]:
        if len(text) > s["max_message_length"]:
            reason = "message too long"

    if not reason and s["anti_flood"]:
        now = time.monotonic()
        q = flood_cache[(chat.id,u.id)]
        q.append(now)
        while q and now-q[0] > s["flood_window"]:
            q.popleft()
        if len(q) >= s["flood_limit"]:
            q.clear()
            reason = "anti-flood"

    if not reason:
        return

    await delete(m)
    strike = await add_strike(chat.id, u.id)

    if reason == "anti-flood":
        action = s["flood_action"]
    elif strike >= s["max_strikes"]:
        action = s["strike_action"]

    try:
        if action == "mute":
            await mute(context, chat.id, u.id, s["mute_seconds"])
        elif action == "ban":
            await ban(context, chat.id, u.id)
        elif action == "delete":
            pass
    except TelegramError as e:
        log.warning("moderation action failed: %s", e)

    await audit(context, chat.id, u.id, action, f"{reason}; strike={strike}")

async def post_init(app):
    await init_db()
    await app.bot.set_my_commands([
        ("start","Mulai"),
        ("help","Bantuan"),
        ("settings","Panel"),
        ("status","Status"),
        ("whitelist","Whitelist"),
        ("blacklist","Blacklist"),
        ("bltext","Blacklist teks"),
        ("allowdomain","Izinkan domain"),
        ("mute","Mute via reply"),
        ("ban","Ban via reply"),
        ("warn","Strike via reply")
    ])

async def post_shutdown(app):
    global pool
    if pool:
        await pool.close()

def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(CommandHandler("setflood", setflood))
    app.add_handler(CommandHandler("floodaction", floodaction))
    app.add_handler(CommandHandler("setmute", setmute))
    app.add_handler(CommandHandler("setstrike", setstrike))
    app.add_handler(CommandHandler("strikeaction", strikeaction))

    app.add_handler(CommandHandler("whitelist", lambda u,c: user_addremove(u,c,"whitelist_users",True)))
    app.add_handler(CommandHandler("unwhitelist", lambda u,c: user_addremove(u,c,"whitelist_users",False)))
    app.add_handler(CommandHandler("blacklist", lambda u,c: user_addremove(u,c,"blacklist_users",True)))
    app.add_handler(CommandHandler("unblacklist", lambda u,c: user_addremove(u,c,"blacklist_users",False)))

    app.add_handler(CommandHandler("bltext", bltext))
    app.add_handler(CommandHandler("unbltext", unbltext))
    app.add_handler(CommandHandler("bltextlist", bltextlist))

    app.add_handler(CommandHandler("allowdomain", allowdomain))
    app.add_handler(CommandHandler("delallowdomain", delallowdomain))
    app.add_handler(CommandHandler("allowlist", allowlist))

    app.add_handler(CommandHandler("mute", manual_mute))
    app.add_handler(CommandHandler("ban", manual_ban))
    app.add_handler(CommandHandler("warn", manual_warn))
    app.add_handler(CommandHandler("clearstrike", clearstrike))

    app.add_handler(CallbackQueryHandler(settings_cb))
    app.add_handler(ChatMemberHandler(welcome, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, filter_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
