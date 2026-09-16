"""Bot 1 admin panel — every command is guarded by @admin_only.

ADMIN_IDS (env) plus any user promoted with /addadmin can run these.
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

import db
import scanner
from bot1 import do_post, schedule_daily
from utils import admin_only, human_duration, parse_duration

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


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = await db.get_settings()
    users = await db.count_users()
    verified = await db.count_verified()
    banned = await db.count_banned()
    files = await db.count_files()
    posted = await db.count_posted()
    pending = await db.count_pending()
    await update.message.reply_text(
        "📊 Statistics\n"
        f"👤 Users: {users}\n"
        f"✅ Verified: {verified}\n"
        f"🚫 Banned: {banned}\n"
        f"🎬 Total items: {files}\n"
        f"📤 Posted: {posted}\n"
        f"⏳ Queued: {pending}\n\n"
        f"Shortener gate: {'ON' if settings.get('shortener_enabled') else 'OFF'}\n"
        f"Force-sub: {settings.get('force_sub_channel_id') or 'off'}\n"
        f"Auto-delete: {human_duration(int(settings.get('auto_delete_minutes') or 0))}\n"
        f"Daily post time: {settings.get('post_time')} UTC"
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
    await schedule_daily(context.application)
    await update.message.reply_text(f"✅ Daily post time set to {args[0]} UTC.")


@admin_only
async def cmd_dripnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Posting the next queued item…")
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
