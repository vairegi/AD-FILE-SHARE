"""Bot 2 — the File Delivery bot.

* Handles /start deliver_<file_id>_<token> deep links from Bot 1.
* Validates the short-lived, single-use, user-bound token.
* Copies the video from the Database Channel straight to the user via
  copy_message (server-side, so large files and the 20 MB download limit
  are both irrelevant, and the source chat is never exposed).
* Queues the delivered messages for auto-deletion (restart-safe queue).
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
import db
from utils import human_duration, parse_duration

log = logging.getLogger("bot2")

WELCOME = (
    "👋 This bot delivers your files.\n\n"
    "Use the *Get File* button from the main bot to receive a file here.\n\n"
    "Configure how long files stay in this chat with:\n"
    "/setautodelete 5min · 2hour · 12hour · 7day · never"
)


# ── auto-delete queue ─────────────────────────────────────────
async def sweep_deletions(context: ContextTypes.DEFAULT_TYPE):
    """Delete any delivered messages whose timer has elapsed."""
    due = await db.due_deletions()
    for entry in due:
        for message_id in entry.get("message_ids", []):
            try:
                await context.bot.delete_message(entry["chat_id"], message_id)
            except Exception:
                pass
        await db.remove_deletion(entry["_id"])


def schedule_sweeper(application: Application):
    jq = application.job_queue
    if jq is None:
        return
    for job in jq.get_jobs_by_name("sweep"):
        job.schedule_removal()
    jq.run_repeating(sweep_deletions, interval=60, first=15, name="sweep")


async def _autodelete_minutes(user_id, category=None):
    """Auto-delete is an admin-controlled PER-CATEGORY setting — per-user
    overrides are intentionally ignored so regular users cannot keep files
    longer. Falls back to the global default when no category is resolved."""
    minutes = None
    if category:
        cat = await db.get_category(category)
        if cat:
            minutes = cat.get("auto_delete_minutes")
    if minutes is None:
        settings = await db.get_settings()
        minutes = settings.get("auto_delete_minutes")
    return int(minutes or 0)


async def _protect_flag(category):
    """Per-category /protect (block forwarding/saving); global fallback."""
    if category:
        cat = await db.get_category(category)
        if cat:
            return bool(cat.get("protect_content"))
    return bool((await db.get_settings()).get("protect_content"))


async def _db_channel_for(item):
    """Resolve the Database Channel that physically holds this item's files."""
    cat_key = item.get("category")
    if cat_key:
        cat = await db.get_category(cat_key)
        if cat and cat.get("db_channel_id"):
            return cat["db_channel_id"]
    # Legacy items (or a deleted category) fall back to the global setting.
    return (await db.get_settings()).get("db_channel_id")


# ── delivery ──────────────────────────────────────────────────
async def send_item(bot, chat_id, item, index):
    db_channel = await _db_channel_for(item)
    if not db_channel:
        await bot.send_message(chat_id, "❌ Delivery channel is not configured.")
        return

    # /protect on -> users cannot forward or save the delivered file
    protect = await _protect_flag(item.get("category"))

    video = item["videos"][index]
    try:
        sent = await bot.copy_message(
            chat_id=chat_id, from_chat_id=db_channel,
            message_id=video["db_message_id"],
            protect_content=protect,
        )
    except Exception as exc:
        log.error("copy_message failed: %s", exc)
        await bot.send_message(
            chat_id,
            "⚠️ Failed to deliver the file. Please go back and tap Download again.",
        )
        return

    delivered = [getattr(sent, "message_id", None)]

    # Attach the subtitle file(s) if the item has any.
    for srt in item.get("srts") or []:
        try:
            sent_srt = await bot.copy_message(
                chat_id=chat_id, from_chat_id=db_channel,
                message_id=srt["db_message_id"],
                protect_content=protect,
            )
            delivered.append(getattr(sent_srt, "message_id", None))
        except Exception as exc:
            log.warning("srt copy failed: %s", exc)

    minutes = await _autodelete_minutes(chat_id, item.get("category"))
    if minutes and minutes > 0:
        await db.add_deletion(chat_id, [m for m in delivered if m], db.now() + minutes * 60)
        note = f"⏳ This file will be auto-deleted in {human_duration(minutes * 60)}."
    else:
        note = "📌 This file will stay in the chat."
    await bot.send_message(chat_id, note)


async def process_delivery(bot, chat_id, user_id, file_id, token):
    _rec = await db.get_user(user_id)
    if _rec and _rec.get("banned"):
        await bot.send_message(
            chat_id, "🚫 You are banned from using this bot.")
        return
    doc = await db.get_token(token)
    if (not doc or doc.get("kind") != "deliver" or doc.get("used")
            or doc.get("expires_at", 0) < db.now()):
        await bot.send_message(
            chat_id,
            "⌛ This download link is invalid or has expired.\n"
            "Go back to the main bot and tap Download again.",
        )
        return
    if doc.get("user_id") != user_id or doc.get("file_id") != file_id:
        await bot.send_message(chat_id, "🚫 This link was not issued for you.")
        return

    item = await db.get_item_by_file_id(file_id)
    if not item or not item.get("videos"):
        await bot.send_message(chat_id, "❌ This file is no longer available.")
        return

    await db.mark_token_used(token)

    videos = item["videos"]
    if len(videos) > 1:
        rows = []
        for i, v in enumerate(videos):
            label = (v.get("caption") or f"Version {i + 1}").strip()[:40] or f"Version {i + 1}"
            rows.append([InlineKeyboardButton(label, callback_data=f"dl:{file_id}:{i}:{user_id}")])
        await bot.send_message(
            chat_id, "🎬 Choose which version you want:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    await send_item(bot, chat_id, item, 0)


# ── handlers ──────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.touch_user(user.id)
    record = await db.get_user(user.id)
    if record and record.get("banned"):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    args = context.args or []
    if args and args[0].startswith("deliver_"):
        rest = args[0][len("deliver_"):]
        # rpartition: file_id may itself contain '_' (category-prefixed ids);
        # the token is always the final '_' segment.
        file_id, _, token = rest.rpartition("_")
        await process_delivery(context.bot, user.id, user.id, file_id, token)
        return

    await update.message.reply_text(WELCOME)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.helpers import escape_markdown
    body = ("/start — Start the bot\n"
            "/help — Show this list\n\n"
            "Files are delivered when you tap 📥 Get File in the main bot.")
    await update.message.reply_text(
        "📖 *Commands*\n\n" + escape_markdown(body, version=2),
        parse_mode="MarkdownV2",
    )


async def setautodelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: sets the GLOBAL auto-delete timer for every delivered file."""
    from utils import is_admin
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ This command can be used only by admins.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /setautodelete <time>\nExamples: 5min · 15min · 2hour · 7day · never"
        )
        return
    seconds = parse_duration(args[0])
    if seconds is None:
        await update.message.reply_text("Could not parse that duration. Try 5min / 2hour / never.")
        return
    await db.update_settings({"auto_delete_minutes": seconds // 60})
    await update.message.reply_text(
        f"✅ All delivered files will now be auto-deleted after {human_duration(seconds)}."
    )


async def on_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _rec = await db.get_user(query.from_user.id)
    if _rec and _rec.get("banned"):
        await query.answer("🚫 You are banned from using this bot.",
                           show_alert=True)
        return
    _, file_id, index, owner = (query.data or "").split(":")
    if query.from_user.id != int(owner):
        await query.answer("This button was not issued for you.", show_alert=True)
        return
    await query.answer()
    item = await db.get_item_by_file_id(file_id)
    if not item or not item.get("videos"):
        await query.edit_message_text("❌ This file is no longer available.")
        return
    try:
        await query.edit_message_text("📤 Sending your file…")
    except Exception:
        pass
    await send_item(context.bot, query.from_user.id, item, int(index))


# ── application factory ───────────────────────────────────────
def build_bot2() -> Application:
    app = Application.builder().token(config.BOT2_TOKEN).updater(None).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setautodelete", setautodelete))
    app.add_handler(CallbackQueryHandler(on_download, pattern=r"^dl:"))
    return app
