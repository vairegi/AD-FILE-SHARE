"""Bot 1 admin panel — every command is guarded by @admin_only.

ADMIN_IDS (env) plus any user promoted with /addadmin can run these.
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

import db
import scanner
from utils import admin_only, human_duration, parse_duration
from telegram.helpers import escape_markdown

# NOTE: do_post / schedule_daily are imported lazily inside the commands that
# need them — importing them here at module level creates a circular import
# (bot1 -> bot1_admin -> bot1) that crashes the process on startup.

log = logging.getLogger("bot1.admin")


# ── shortener gate ────────────────────────────────────────────
@admin_only
async def cmd_shortener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    settings = await db.get_settings()
    if not args or args[0].lower() not in ("on", "off", "status"):
        await update.message.reply_text("Usage: /shortener on | off | status")
        return
    if args[0].lower() == "status":
        await update.message.reply_text(
            f"Shortener gate: {'ON' if settings.get('shortener_enabled') else 'OFF'}\n"
            f"API base: {settings.get('shortener_api_base')}\n"
            f"Verify validity: {settings.get('verify_hours')} h\n"
            f"Token TTL: {settings.get('token_ttl_minutes')} min\n"
            f"Extra buttons: {len(settings.get('shortener_buttons') or [])}"
        )
        return
    enabled = args[0].lower() == "on"
    await db.update_settings({"shortener_enabled": enabled})
    await update.message.reply_text(
        f"✅ Shortener gate {'enabled' if enabled else 'disabled'}."
    )


@admin_only
async def cmd_shortenerapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /shortenerapi https://vplink.in/api")
        return
    await db.update_settings({"shortener_api_base": args[0].strip()})
    await update.message.reply_text(f"✅ Shortener API base set to {args[0].strip()}")


@admin_only
async def cmd_setverifytime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /setverifytime <hours>")
        return
    await db.update_settings({"verify_hours": int(args[0])})
    await update.message.reply_text(f"✅ Verification validity set to {args[0]} hours.")


@admin_only
async def cmd_settokenttl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /settokenttl <minutes>")
        return
    await db.update_settings({"token_ttl_minutes": int(args[0])})
    await update.message.reply_text(f"✅ Handoff token TTL set to {args[0]} minutes.")


@admin_only
async def cmd_shortenermsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /shortenermsg <heading text>")
        return
    await db.update_settings({"shortener_msg": text})
    await update.message.reply_text("✅ Heading updated.")


@admin_only
async def cmd_shortenerbotmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /shortenerbotmsg <text>")
        return
    await db.update_settings({"shortenerbot_msg": text})
    await update.message.reply_text("✅ Gate message updated.")


@admin_only
async def cmd_verifymsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /verifymsg <text>")
        return
    await db.update_settings({"verify_msg": text})
    await update.message.reply_text("✅ Success message updated.")


@admin_only
async def cmd_shortenerbtn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.partition(" ")[2]
    if "|" not in raw:
        await update.message.reply_text("Usage: /shortenerbtn <label> | <url>")
        return
    label, _, url = raw.partition("|")
    label, url = label.strip(), url.strip()
    if not label or not url.startswith("http"):
        await update.message.reply_text("Both a label and an http(s) URL are required.")
        return
    settings = await db.get_settings()
    buttons = list(settings.get("shortener_buttons") or [])
    buttons.append({"label": label, "url": url})
    await db.update_settings({"shortener_buttons": buttons})
    await update.message.reply_text(f"✅ Added button “{label}”.")


@admin_only
async def cmd_clearshortenerbtns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.update_settings({"shortener_buttons": []})
    await update.message.reply_text("✅ All extra buttons removed.")


@admin_only
async def cmd_protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/protect on|off - ON: users cannot forward/save delivered files
    (Telegram protect_content on every message the bots send)."""
    args = context.args or []
    if not args or args[0].lower() not in ("on", "off"):
        settings = await db.get_settings()
        await update.message.reply_text(
            "Usage: /protect on | off\n"
            f"Content protection is currently "
            f"{'ON' if settings.get('protect_content') else 'OFF'}.")
        return
    enabled = args[0].lower() == "on"
    await db.update_settings({"protect_content": enabled})
    await update.message.reply_text(
        f"✅ Content protection {'enabled' if enabled else 'disabled'}.\n"
        + ("Users can no longer forward or save delivered files."
           if enabled else "Users can forward and save delivered files again."))


# ── general admin ─────────────────────────────────────────────
@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    ids = await db.all_user_ids()
    sent = failed = 0
    await update.message.reply_text(f"📣 Broadcasting to {len(ids)} users…")
    for uid in ids:
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"✅ Done. Sent: {sent} · Failed: {failed}")


def _h(text) -> str:
    """Escape a string for HTML parse_mode."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def _channel_info(bot, channel_id) -> str:
    """Channel title with its invite link embedded, e.g. '<a href=...>Title</a>'.
    Falls back to the raw id when the bot cannot resolve the channel."""
    if not channel_id:
        return "not set"
    title, link = None, None
    try:
        chat = await bot.get_chat(channel_id)
        title = getattr(chat, "title", None)
        username = getattr(chat, "username", None)
        if username:
            link = f"https://t.me/{username}"
    except Exception:
        pass
    if not link:
        try:
            link = await bot.export_chat_invite_link(channel_id)
        except Exception:
            pass
    label = _h(title) if title else str(channel_id)
    if link:
        label = f'<a href="{link}">{label}</a>'
    return label


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full bot overview: counters, feature toggles and every connected
    channel shown with its title + embedded invite link."""
    settings = await db.get_settings()
    users = await db.count_users()
    verified = await db.count_verified()
    banned = await db.count_banned()
    files = await db.count_files()
    posted = await db.count_posted()
    pending = await db.count_pending()

    db_ch = await _channel_info(context.bot, settings.get("db_channel_id"))
    post_ch = await _channel_info(context.bot, settings.get("post_channel_id"))
    main_ch = await _channel_info(context.bot, settings.get("post_main_channel_id"))
    fsub_ch = await _channel_info(context.bot, settings.get("force_sub_channel_id"))

    if not settings.get("schedule_enabled", True):
        schedule = "OFF"
    elif settings.get("schedule_paused"):
        schedule = "PAUSED"
    else:
        schedule = "ON"

    await update.message.reply_text(
        "📊 <b>Bot Statistics</b>\n\n"
        f"👤 Users: {users}\n"
        f"✅ Verified: {verified}\n"
        f"🚫 Banned: {banned}\n"
        f"🎬 Total items: {files}\n"
        f"📤 Posted: {posted}\n"
        f"⏳ Queued: {pending}\n\n"
        "🔗 <b>Connected Channels</b>\n"
        f"Database: {db_ch}\n"
        f"Main Posting Channel: {main_ch}\n"
        f"Post Channel: {post_ch}\n"
        f"Force-Sub Channel: {fsub_ch}\n\n"
        "⚙️ <b>Settings</b>\n"
        f"Shortener gate: {'ON' if settings.get('shortener_enabled') else 'OFF'}\n"
        f"Content protection (/protect): {'ON' if settings.get('protect_content') else 'OFF'}\n"
        f"Auto-delete: {human_duration(int(settings.get('auto_delete_minutes') or 0) * 60)}\n"
        f"Daily post time: {_h(settings.get('post_time') or '18:00')} "
        f"({_h(settings.get('post_timezone') or 'Asia/Kolkata')})\n"
        f"Schedule: {schedule}\n"
        f"Post tag: {_h(settings.get('post_tag')) if settings.get('post_tag') else 'not set'}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    await db.set_banned(int(args[0]), True)
    await update.message.reply_text(f"🚫 Banned {args[0]}.")


@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    await db.set_banned(int(args[0]), False)
    await update.message.reply_text(f"✅ Unbanned {args[0]}.")


@admin_only
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    await db.add_db_admin(int(args[0]))
    await update.message.reply_text(f"✅ {args[0]} is now an admin.")


@admin_only
async def cmd_setforcesub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /setforcesub <channel_id | off>")
        return
    if args[0].lower() == "off":
        await db.update_settings({"force_sub_channel_id": None})
        await update.message.reply_text("✅ Force-subscribe disabled.")
        return
    if not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Provide a numeric channel id or 'off'.")
        return
    channel_id = int(args[0])
    await db.update_settings({"force_sub_channel_id": channel_id})
    # Ask the admin to verify the bot's rights on that channel.
    try:
        me = await context.bot.get_chat_member(channel_id, context.bot.id)
        note = f"Bot status in channel: {me.status}"
    except Exception as exc:
        note = f"⚠️ Could not verify bot membership there: {exc}"
    await update.message.reply_text(f"✅ Force-subscribe channel set.\n{note}")


@admin_only
async def cmd_setautodelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /setautodelete <time>\nExamples: 30min · 2hour · 1day · 7day · never"
        )
        return
    seconds = parse_duration(args[0])
    if seconds is None:
        await update.message.reply_text("Could not parse that duration. Try 30min / 2hour / 7day.")
        return
    minutes = seconds // 60
    await db.update_settings({"auto_delete_minutes": minutes})
    await update.message.reply_text(
        f"✅ Default auto-delete set to {human_duration(seconds)}."
    )


@admin_only
async def cmd_setpostchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /setpostchannel <channel_id>")
        return
    await db.update_settings({"post_channel_id": int(args[0])})
    await update.message.reply_text(f"✅ Post channel set to {args[0]}.")


@admin_only
async def cmd_setdbchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /setdbchannel <channel_id>")
        return
    await db.update_settings({"db_channel_id": int(args[0])})
    await update.message.reply_text(
        f"✅ Database channel set to {args[0]}.\nRun /rescandb to index it."
    )


@admin_only
async def cmd_setposttime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or ":" not in args[0]:
        await update.message.reply_text("Usage: /setposttime HH:MM  (UTC)")
        return
    try:
        h, m = args[0].split(":")
        int(h), int(m)
    except Exception:
        await update.message.reply_text("Use HH:MM, e.g. /setposttime 20:30")
        return
    await db.update_settings({"post_time": args[0]})
    from bot1 import schedule_daily  # lazy import avoids circular dependency
    await schedule_daily(context.application)
    await update.message.reply_text(f"✅ Daily post time set to {args[0]} UTC.")


@admin_only
async def cmd_dripnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Posting the next queued item…")
    from bot1 import do_post  # lazy import avoids circular dependency
    item = await do_post(context.bot)
    if item:
        await update.message.reply_text(f"✅ Posted item {item['file_id']}.")
    else:
        await update.message.reply_text(
            "Nothing posted (queue empty or channels not configured)."
        )


async def _run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id):
    async def progress(n):
        try:
            await update.message.reply_text(f"…scanned {n} messages")
        except Exception:
            pass
    try:
        result = await scanner.scan_channel(channel_id, progress=progress)
    except Exception as exc:
        await update.message.reply_text(f"❌ Scan failed: {exc}")
        return
    await update.message.reply_text(
        f"✅ Scan complete. Messages indexed: {result['scanned']} · "
        f"Items in queue: {result['items']}"
    )


@admin_only
async def cmd_rescandb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = await db.get_settings()
    channel = settings.get("db_channel_id")
    if not channel:
        await update.message.reply_text(
            "No database channel set. Use /setdbchannel <channel_id> first."
        )
        return
    await update.message.reply_text("🔍 Re-indexing the database channel…")
    await _run_scan(update, context, channel)


@admin_only
async def cmd_scandb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /scandb <db_channel_id>")
        return
    channel_id = int(args[0])
    await db.update_settings({"db_channel_id": channel_id})
    await update.message.reply_text("🔍 Scanning the database channel…")
    await _run_scan(update, context, channel_id)


# Command name -> handler, registered by bot1.build_bot1()
COMMANDS = {
    "shortener": cmd_shortener,
    "shortenerapi": cmd_shortenerapi,
    "setverifytime": cmd_setverifytime,
    "settokenttl": cmd_settokenttl,
    "shortenermsg": cmd_shortenermsg,
    "shortenerbotmsg": cmd_shortenerbotmsg,
    "verifymsg": cmd_verifymsg,
    "shortenerbtn": cmd_shortenerbtn,
    "clearshortenerbtns": cmd_clearshortenerbtns,
    "protect": cmd_protect,
    "broadcast": cmd_broadcast,
    "stats": cmd_stats,
    "ban": cmd_ban,
    "unban": cmd_unban,
    "addadmin": cmd_addadmin,
    "setforcesub": cmd_setforcesub,
    "setautodelete": cmd_setautodelete,
    "setpostchannel": cmd_setpostchannel,
    "setdbchannel": cmd_setdbchannel,
    "setposttime": cmd_setposttime,
    "dripnow": cmd_dripnow,
    "rescandb": cmd_rescandb,
    "scandb": cmd_scandb,
}


# === v1.3 : schedule / queue / main-channel admin commands ===


def _parse_hhmm(text):
    try:
        hh, mm = text.split(":")[:2]
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    except Exception:
        pass
    return None


@admin_only
async def cmd_setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setschedule HH:MM [TZ] - e.g. /setschedule 07:00 IST or 07:00 Asia/Kolkata"""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /setschedule HH:MM [timezone]\n"
            "Example: /setschedule 07:00 IST")
        return
    t = _parse_hhmm(args[0])
    if not t:
        await update.message.reply_text(
            "\u274c Invalid time. Use HH:MM (00:00-23:59).")
        return
    s = await db.get_settings()
    tz_label = " ".join(args[1:]).strip() or s.get("post_timezone", "Asia/Kolkata")
    from bot1 import _resolve_tz, schedule_daily
    _, canonical = _resolve_tz(tz_label)
    warn = ""
    if tz_label and canonical == "Asia/Kolkata" and tz_label.upper() not in (
            "IST", "INDIA", "CHENNAI", "ASIA/KOLKATA", "ASIA KOLKATA"):
        warn = f"\n\u26a0\ufe0f Unknown timezone '{tz_label}', fell back to Asia/Kolkata."
    await db.update_settings({
        "post_time": t, "post_timezone": canonical,
        "schedule_enabled": True, "schedule_paused": False})
    await schedule_daily(context.application.job_queue)
    await update.message.reply_text(
        f"\u2705 Daily post scheduled at {t} ({canonical}).{warn}")


@admin_only
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/schedule on|off"""
    args = context.args or []
    if not args or args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /schedule on|off")
        return
    on = args[0].lower() == "on"
    await db.update_settings(
        {"schedule_enabled": on, "schedule_paused": False})
    from bot1 import schedule_daily
    await schedule_daily(context.application.job_queue)
    await update.message.reply_text(
        "\u2705 Daily posting enabled." if on
        else "\u23f8\ufe0f Daily posting disabled (job removed).")


@admin_only
async def cmd_pauseposting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.update_settings({"schedule_paused": True})
    await update.message.reply_text(
        "\u23f8\ufe0f Posting paused. Daily job will skip until /resumeposting.")


@admin_only
async def cmd_resumeposting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.update_settings({"schedule_paused": False})
    await update.message.reply_text("\u25b6\ufe0f Posting resumed.")


@admin_only
async def cmd_queueinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the next 10 queued posts. Always read fresh from Mongo, so it
    self-updates after /dripnow or /queue_reset."""
    sm = await db.queue_summary(10)
    items = sm["items"]
    if not items:
        await update.message.reply_text("Queue is empty.")
        return
    settings = await db.get_settings()
    db_ch = settings.get("db_channel_id")
    base = None
    if db_ch and str(db_ch).startswith("-100"):
        base = f"https://t.me/c/{str(db_ch)[4:]}"
    lines = ["📋 <b>Queue info</b>",
             f"Position: #{sm['position']} — Remaining: {sm['remaining']}", ""]
    for i, it in enumerate(items, 1):
        caption = (it.get("caption") or "").strip().split("\n")[0][:60]
        label = _h(caption or str(it.get("file_id") or it["db_message_id"]))
        if base:
            label = f'<a href="{base}/{it["db_message_id"]}">{label}</a>'
        lines.append(f"{i}. {label}")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@admin_only
async def cmd_queue_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/queue_reset N - move cursor to the Nth item (1-indexed over the full
    ordered queue); earlier unposted items are marked posted."""
    args = context.args or []
    try:
        n = int(args[0])
    except Exception:
        await update.message.reply_text("Usage: /queue_reset N (e.g. /queue_reset 50)")
        return
    res = await db.queue_reset_to_position(n)
    if not res:
        await update.message.reply_text("\u274c Invalid queue position.")
        return
    posted = await db.count_posted()
    pending = await db.count_pending()
    await update.message.reply_text(
        f"\u2705 Queue reset. Cursor set to db_message_id={res['db_message_id']}\n"
        f"Posted={posted} - Queued={pending}")


@admin_only
async def cmd_setpostmainchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpostmainchannel <channel_id|off> - forward every post-channel cover
    (with the tag) to this main channel. Bot must be admin in BOTH channels."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /setpostmainchannel <channel_id|off>")
        return
    if args[0].lower() == "off":
        await db.update_settings({"post_main_channel_id": None})
        await update.message.reply_text("\u2705 Main-channel forwarding disabled.")
        return
    try:
        cid = int(args[0])
    except ValueError:
        await update.message.reply_text("\u274c channel_id must be a number.")
        return
    note = ""
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(cid, me.id)
        note = f" Bot status there: {member.status}."
        if member.status not in ("administrator", "creator"):
            note += " \u26a0\ufe0f Bot is NOT admin - forwarding will fail."
    except Exception as exc:
        note = f" \u26a0\ufe0f Could not verify channel: {exc}"
    await db.update_settings({"post_main_channel_id": cid})
    await update.message.reply_text(
        f"\u2705 Main channel set to {cid}.{note}")


@admin_only
async def cmd_setposttag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setposttag <text|off> - tag line sent above each main-channel forward."""
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /setposttag <text|off>")
        return
    tag = " ".join(args)
    if tag.lower() == "off":
        await db.update_settings({"post_tag": None})
        await update.message.reply_text("\u2705 Post tag cleared.")
        return
    await db.update_settings({"post_tag": tag})
    await update.message.reply_text(f"\u2705 Post tag set to: {tag}")


COMMANDS.update({
    "setschedule": cmd_setschedule,
    "schedule": cmd_schedule,
    "pauseposting": cmd_pauseposting,
    "resumeposting": cmd_resumeposting,
    "queueinfo": cmd_queueinfo,
    "queue_reset": cmd_queue_reset,
    "setpostmainchannel": cmd_setpostmainchannel,
    "setposttag": cmd_setposttag,
})
