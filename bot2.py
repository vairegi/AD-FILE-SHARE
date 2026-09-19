"""Bot 2 — the File Delivery bot.

* Handles /start deliver_<file_id>_<token> deep links from Bot 1.
* Validates the short-lived, single-use, user-bound token.
* Copies the video from the Database Channel straight to the user via
  copy_message (server-side, so large files and the 20 MB download limit
  are both irrelevant, and the source chat is never exposed).
* Queues the delivered messages for auto-deletion (restart-safe queue).
"""
import logging
import re

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
    "/setautodelete 5min · 2hour · 12hour · 7day · never\n"
    "/withfilemessages [time] <text> — set your own deletion notice"
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


# ── custom with-file notice (/withfilemessages) ───────────────
# {N Duration} (and a few aliases) is replaced with the real time left before
# the delivered file is auto-deleted.
_DURATION_TOKEN_RE = re.compile(
    r"\{\s*n?[ _-]?duration\s*\}|\{\s*time\s*\}|\{\s*delete[ _-]?time\s*\}",
    re.IGNORECASE)


def render_withfile_message(template, minutes):
    """Substitute the {N Duration} placeholder with the real deletion time."""
    try:
        m = int(minutes or 0)
    except (TypeError, ValueError):
        m = 0
    dur = human_duration(m * 60) if m > 0 else "never (kept forever)"
    return _DURATION_TOKEN_RE.sub(dur, template or "")


async def _apply_autodelete_all(minutes):
    """Set the auto-delete timer globally AND on every pipeline, so the
    'all delivered files' promise of /setautodelete is actually true even
    when a pipeline carries its own (older) timer."""
    minutes = int(minutes)
    await db.update_settings({"auto_delete_minutes": minutes})
    for c in await db.list_categories():
        await db.update_category(c["key"], {"auto_delete_minutes": minutes})


async def _withfile_template(category=None):
    """Custom post-delivery notice; a per-pipeline value wins over the global."""
    if category:
        cat = await db.get_category(category)
        if cat and cat.get("with_file_message"):
            return cat["with_file_message"]
    return (await db.get_settings()).get("with_file_message")


async def withfilemessages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: set the custom text posted after a file is delivered.

    Usage: /withfilemessages [time] <text>
      * an optional leading time (7day · 1hour · 30min · never) also sets the
        global auto-delete timer (all pipelines);
      * {N Duration} anywhere in <text> is replaced with the real time left
        before the file is deleted.
    """
    from utils import is_admin
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ This command can be used only by admins.")
        return
    args = list(context.args or [])
    if not args:
        cur = await _withfile_template()
        minutes = await _autodelete_minutes(None)
        sample = (render_withfile_message(cur, minutes) if cur
                  else f"⏳ This file will be auto-deleted in {human_duration(minutes * 60)}.")
        await update.message.reply_text(
            "Usage: /withfilemessages [time] <text>\n"
            "• optional leading time (7day · 1hour · 30min) also sets the "
            "global auto-delete timer\n"
            "• {N Duration} in your text is replaced with the real delete time\n\n"
            "Examples:\n"
            "  /withfilemessages 7day ⏳ This file is removed in {N Duration}.\n"
            "  /withfilemessages Grab it before {N Duration} — then it is gone!\n\n"
            f"Current message:\n{sample}")
        return
    seconds = parse_duration(args[0])
    if seconds is not None:
        args = args[1:]
        await _apply_autodelete_all(seconds // 60)
    text = " ".join(args).strip()
    if not text:
        await update.message.reply_text(
            "Please include the message text.\n"
            "Usage: /withfilemessages [time] <text>")
        return
    await db.update_settings({"with_file_message": text})
    minutes = seconds // 60 if seconds is not None else await _autodelete_minutes(None)
    preview = render_withfile_message(text, minutes)
    prefix = (f"Auto-delete timer set to {human_duration(seconds)} for all files.\n"
              if seconds is not None else "")
    await update.message.reply_text(
        f"✅ {prefix}Your with-file message is saved.\n\nPreview:\n{preview}")


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
        default_note = f"⏳ This file will be auto-deleted in {human_duration(minutes * 60)}."
    else:
        default_note = "📌 This file will stay in the chat."
    # An admin-set custom notice (/withfilemessages) overrides the default,
    # with its {N Duration} placeholder filled in.
    template = await _withfile_template(item.get("category"))
    note = render_withfile_message(template, minutes) if template else default_note
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
    # Reaches EVERY pipeline so "all delivered files" is actually true,
    # even when a pipeline carried its own older timer (the 1-hour bug).
    await _apply_autodelete_all(seconds // 60)
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
    app.add_handler(CommandHandler("withfilemessages", withfilemessages))
    app.add_handler(CallbackQueryHandler(on_download, pattern=r"^dl:"))
    return app
