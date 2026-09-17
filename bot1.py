"""Bot 1 — the Link / Gate bot.

Responsibilities
----------------
* Daily scheduled drip-posting of covers to the Posting Channel (oldest first).
* /dripnow manual posting.
* Force-subscribe gate (active-member check + pending join-request check).
* Shortener verification gate (one verification == one file, bound to the user).
* Issues short-lived single-use tokens deep-linking into Bot 2.
* Live indexing of the Database Channel + the full admin panel.
"""
import datetime
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db
import scanner
import shortener
from bot1_admin import COMMANDS as ADMIN_COMMANDS
from utils import is_admin

log = logging.getLogger("bot1")

WELCOME = (
    "👋 Welcome!\n\n"
    "This bot gives access to the files posted on the channel.\n"
    "Tap the Download button under any post to get started."
)

# /help text is sent with parse_mode=MarkdownV2 — every special character
# outside a formatting span is escaped (telegram.helpers.escape_markdown
# handles this; we escape once here so the send can never 400).
from telegram.helpers import escape_markdown


def _md2(t: str) -> str:
    return escape_markdown(t, version=2)


HELP_USER = (
    "📖 *Commands*\n\n"
    + _md2("/start — Start the bot\n"
           "/help — Show this list\n")
    + "\n*How to get a file*\n"
    + _md2("1. Tap ⬇️ Download under any channel post\n"
           "2. Join the channel if asked, then tap ✅\n"
           "3. Complete the quick verification\n"
           "4. Tap 📥 Get File — the delivery bot sends it")
)

HELP_ADMIN = (
    "\n\n🛠 *Admin commands*\n\n"
    + "*Shortener gate*\n"
    + _md2("/shortener on | off | status — toggle the gate\n"
           "/shortenerapi <url> — shortener API base\n"
           "/setverifytime <hours> — verification validity\n"
           "/settokenttl <minutes> — handoff token TTL\n"
           "/shortenermsg <text> — gate heading\n"
           "/shortenerbotmsg <text> — gate DM text\n"
           "/verifymsg <text> — message after verification\n"
           "/shortenerbtn <label> | <url> — add extra button\n"
           "/clearshortenerbtns — remove extra buttons\n")
    + "\n*General*\n"
    + _md2("/broadcast <message> — message all users\n"
           "/stats — full overview + all connected channels\n"
           "/ban <user_id> · /unban <user_id>\n"
           "/addadmin <user_id> — promote an admin\n"
           "/setforcesub <channel_id | off>\n"
           "/setautodelete <time> — e.g. 30min, 2hour, 7day, never\n"
           "/setpostchannel <channel_id>\n"
           "/setdbchannel <channel_id>\n"
           "/setposttime <HH:MM> — daily post time (UTC)\n"
           "/protect on | off — block forwarding/saving of files\n"
           "/dripnow — post the next queued item now\n"
           "/rescandb — re-index the database channel\n"
           "/scandb <channel_id> — index a channel + set as DB")
)


# ── small helpers ─────────────────────────────────────────────
def _hhmm(value: str):
    try:
        h, m = (value or "18:00").strip().split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return 18, 0


async def _channel_link(bot, channel_id):
    """Best-effort public link for the force-subscribe channel."""
    try:
        chat = await bot.get_chat(channel_id)
        if chat.username:
            return f"https://t.me/{chat.username}"
    except Exception:
        pass
    try:
        return await bot.export_chat_invite_link(channel_id)
    except Exception:
        return None


async def _is_member(bot, channel_id, user_id):
    """True/False for a definitive answer, None when the check itself failed."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        # 'restricted' counts only when the member is still inside the chat
        if member.status == "restricted":
            return bool(getattr(member, "is_member", False))
        return member.status in ("member", "administrator", "creator")
    except Exception as exc:
        log.warning("get_chat_member failed for %s: %s", channel_id, exc)
        return None


async def gate_ok(bot, user_id) -> bool:
    """Force-subscribe gate: active member OR a pending join request passes."""
    settings = await db.get_settings()
    channel = settings.get("force_sub_channel_id")
    if not channel:
        return True
    member = await _is_member(bot, channel, user_id)
    if member is True:
        return True
    if await db.has_join_request(user_id):
        return True
    if member is None:
        # Misconfiguration (bot not admin / wrong id) — fail open so real users
        # are not locked out. Log it so the admin can fix the setup.
        log.warning("Force-sub check inconclusive; failing open for user %s", user_id)
        return True
    return False


async def send_force_sub(bot, chat_id, file_id):
    settings = await db.get_settings()
    channel = settings.get("force_sub_channel_id")
    url = await _channel_link(bot, channel) if channel else None
    rows = []
    if url:
        rows.append([InlineKeyboardButton("📢 Join Channel", url=url)])
    rows.append([InlineKeyboardButton("✅ I've Joined", callback_data=f"checksub:{file_id}")])
    await bot.send_message(
        chat_id,
        "🔒 To continue you must join our channel first.\n\n"
        "Tap *Join Channel*, then tap *I've Joined*.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def send_shortener_gate(bot, chat_id, user_id, item, settings):
    ttl = max(int(settings.get("token_ttl_minutes") or 10), 60)
    token = await db.create_token(user_id, item["file_id"], ttl, kind="verify")
    deep = (f"https://t.me/{config.BOT1_USERNAME}"
            f"?start=verify_{item['file_id']}_{token}")
    short = await shortener.shorten(deep)

    rows = [[InlineKeyboardButton("🔓 Verify & Download", url=short or deep)]]
    for b in settings.get("shortener_buttons") or []:
        rows.append([InlineKeyboardButton(b["label"], url=b["url"])])

    heading = settings.get("shortener_msg") or "Verification required"
    body = settings.get("shortenerbot_msg") or ""
    await bot.send_message(
        chat_id, f"{heading}\n\n{body}",
        reply_markup=InlineKeyboardMarkup(rows),
        disable_web_page_preview=True,
    )


async def deliver_now(bot, chat_id, user_id, item):
    """Issue a single-use handoff token into Bot 2 and send the Get File button."""
    settings = await db.get_settings()
    ttl = int(settings.get("token_ttl_minutes") or 10)
    token = await db.create_token(user_id, item["file_id"], ttl, kind="deliver")
    deep = (f"https://t.me/{config.BOT2_USERNAME}"
            f"?start=deliver_{item['file_id']}_{token}")
    text = settings.get("verify_msg") or "✅ Tap below to get your file."
    await bot.send_message(
        chat_id, text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📥 Get File", url=deep)]]
        ),
    )


async def process_file(bot, chat_id, user_id, file_id):
    item = await db.get_item_by_file_id(file_id)
    if not item:
        await bot.send_message(chat_id, "❌ This file is no longer available.")
        return
    if not await gate_ok(bot, user_id):
        await send_force_sub(bot, chat_id, file_id)
        return
    settings = await db.get_settings()
    if not settings.get("shortener_enabled"):
        await deliver_now(bot, chat_id, user_id, item)
        return
    await send_shortener_gate(bot, chat_id, user_id, item, settings)


async def process_verify(bot, chat_id, user_id, file_id, token):
    doc = await db.get_token(token)
    if (not doc or doc.get("kind") != "verify" or doc.get("used")
            or doc.get("expires_at", 0) < db.now()):
        await bot.send_message(
            chat_id,
            "⌛ This verification link has expired.\n"
            "Go back and tap Download again.",
        )
        return
    if doc.get("user_id") != user_id or doc.get("file_id") != file_id:
        await bot.send_message(chat_id, "🚫 This verification link belongs to another user.")
        return

    await db.mark_token_used(token)
    settings = await db.get_settings()
    await db.mark_verified(user_id, int(settings.get("verify_hours") or 6))

    item = await db.get_item_by_file_id(file_id)
    if not item:
        await bot.send_message(chat_id, "❌ This file is no longer available.")
        return
    if not await gate_ok(bot, user_id):
        await send_force_sub(bot, chat_id, file_id)
        return
    await deliver_now(bot, chat_id, user_id, item)


# ── posting logic (shared by the scheduler and /dripnow) ──────
async def do_post(bot):
    """Post the next unposted cover to the Posting Channel. Returns the item."""
    settings = await db.get_settings()
    post_channel = settings.get("post_channel_id")
    db_channel = settings.get("db_channel_id")
    if not post_channel or not db_channel:
        log.warning("post/db channel not configured; skipping post.")
        return None

    item = await db.next_unposted()
    if not item:
        log.info("No unposted items left in the queue.")
        return None

    cover_id = item.get("cover_message_id")
    if not cover_id and item.get("videos"):
        cover_id = item["videos"][0]["db_message_id"]
    if not cover_id:
        # Nothing to post for this item; mark it so we don't loop forever.
        await db.mark_posted(item["db_message_id"])
        return None

    link = f"https://t.me/{config.BOT1_USERNAME}?start=file_{item['file_id']}"
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬇️ Download", url=link)]]
    )
    if config.BOT1_USERNAME:
        kwargs = {"reply_markup": markup}
    else:
        log.warning("BOT1_USERNAME not set; posting cover without Download button.")
        kwargs = {}
    try:
        msg = await bot.copy_message(
            chat_id=post_channel, from_chat_id=db_channel,
            message_id=cover_id, **kwargs,
        )
    except Exception as exc:
        log.error("Failed to post item %s: %s", item["file_id"], exc)
        return None
    await db.mark_posted(item["db_message_id"], getattr(msg, "message_id", None))
    log.info("Posted item %s", item["file_id"])
    return item


async def schedule_daily(application):
    """(Re)schedule the daily drip post from settings.post_time (UTC)."""
    jq = application.job_queue
    if jq is None:
        return
    for job in jq.get_jobs_by_name("daily_post"):
        job.schedule_removal()
    settings = await db.get_settings()
    hour, minute = _hhmm(settings.get("post_time"))
    jq.run_daily(
        _daily_cb,
        time=datetime.time(hour=hour, minute=minute, tzinfo=datetime.timezone.utc),
        name="daily_post",
    )
    log.info("Daily post scheduled for %02d:%02d UTC", hour, minute)


async def _daily_cb(context: ContextTypes.DEFAULT_TYPE):
    await do_post(context.bot)


# ── command / update handlers ─────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.touch_user(user.id)
    record = await db.get_user(user.id)
    if record and record.get("banned"):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    args = context.args or []
    if args:
        payload = args[0]
        if payload.startswith("file_"):
            await process_file(context.bot, user.id, user.id, payload[5:])
            return
        if payload.startswith("verify_"):
            rest = payload[len("verify_"):]
            file_id, _, token = rest.partition("_")
            await process_verify(context.bot, user.id, user.id, file_id, token)
            return

    await update.message.reply_text(WELCOME)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command list; admins also see the full admin panel list."""
    user = update.effective_user
    text = HELP_USER
    if user and await is_admin(user.id):
        text += HELP_ADMIN
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def on_checksub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    file_id = (query.data or "").split(":", 1)[-1]
    if await gate_ok(context.bot, user.id):
        await query.answer("Thanks for joining!")
        try:
            await query.edit_message_text("✅ Membership confirmed.")
        except Exception:
            pass
        await process_file(context.bot, user.id, user.id, file_id)
    else:
        await query.answer("You haven't joined yet. Please join, then tap again.",
                           show_alert=True)


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record pending join requests so they also satisfy the force-sub gate."""
    req = update.chat_join_request
    await db.record_join_request(req.from_user.id)
    log.info("Recorded join request from %s", req.from_user.id)


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Live-index new messages appearing in the Database Channel."""
    msg = update.channel_post
    settings = await db.get_settings()
    db_channel = settings.get("db_channel_id")
    if not db_channel or msg.chat_id != db_channel:
        return
    kind = scanner.classify_from_botapi(msg)
    if not kind:
        return
    await db.ingest_raw({
        "message_id": msg.message_id,
        "kind": kind,
        "caption": msg.caption or "",
    })
    await db.rebuild_items()


# ── application factory ───────────────────────────────────────
def build_bot1() -> Application:
    app = Application.builder().token(config.BOT1_TOKEN).updater(None).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    for name, func in ADMIN_COMMANDS.items():
        app.add_handler(CommandHandler(name, func))
    app.add_handler(CallbackQueryHandler(on_checksub, pattern=r"^checksub:"))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    return app


# === v1.3 : tz-aware scheduling + main-channel forward (overrides) ===

_TZ_ALIASES = {
    "IST": "Asia/Kolkata", "INDIA": "Asia/Kolkata", "CHENNAI": "Asia/Kolkata",
    "PKT": "Asia/Karachi", "BST": "Asia/Dhaka", "ICT": "Asia/Bangkok",
    "JST": "Asia/Tokyo", "KST": "Asia/Seoul", "WIB": "Asia/Jakarta",
    "GMT": "Etc/GMT", "UTC": "UTC",
}


def _resolve_tz(name):
    """Return (tzinfo, canonical_label). Falls back to Asia/Kolkata."""
    from zoneinfo import ZoneInfo
    raw = (name or "Asia/Kolkata").strip()
    cand = _TZ_ALIASES.get(raw.upper(), raw)
    for c in (cand, cand.replace(" ", "/")):
        try:
            return ZoneInfo(c), c
        except Exception:
            pass
    return ZoneInfo("Asia/Kolkata"), "Asia/Kolkata"


async def _forward_with_tag(bot, post_channel, post_msg_id, main_id, tag,
                            markup, item_caption=""):
    """Forward the just-published cover from the post channel to the main
    channel, prefixed with the tag as its own message. Raises on failure.

    If the tag message itself cannot be sent (e.g. restricted rights), the
    tag is embedded as the forwarded post's caption instead, so the main
    channel NEVER receives an untagged forward."""
    if tag:
        try:
            await bot.send_message(main_id, tag)
        except Exception as exc:
            log.warning("tag message failed (%s); embedding tag as caption", exc)
            caption = f"{tag}\n\n{item_caption}" if item_caption else str(tag)
            return await bot.copy_message(
                chat_id=main_id, from_chat_id=post_channel,
                message_id=post_msg_id, caption=caption[:1000],
                reply_markup=markup)
    return await bot.copy_message(
        chat_id=main_id, from_chat_id=post_channel,
        message_id=post_msg_id, reply_markup=markup)


async def do_post(bot):
    """Post the next queued cover to the post channel, then (optionally)
    forward it to the main channel with the tag. Cursor-safe via Mongo."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    item = await db.next_unposted()
    if not item:
        log.info("do_post: queue empty")
        return None
    s = await db.get_settings()
    post_channel = s.get("post_channel_id") or config.POST_CHANNEL_ID
    db_channel = s.get("db_channel_id") or config.DB_CHANNEL_ID
    link = f"https://t.me/{config.BOT1_USERNAME}?start=file_{item['file_id']}"
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("\u2b07\ufe0f Download", url=link)]])
    msg = await bot.copy_message(
        chat_id=post_channel, from_chat_id=db_channel,
        message_id=item["cover_message_id"], reply_markup=markup,
        protect_content=bool(s.get("protect_content")))
    await db.mark_posted(item["db_message_id"], post_message_id=msg.message_id)
    log.info("posted %s (db id %s) -> %s", item["file_id"],
             item["db_message_id"], post_channel)
    main_id = s.get("post_main_channel_id")
    if main_id:
        try:
            await _forward_with_tag(bot, post_channel, msg.message_id,
                                    main_id, s.get("post_tag"), markup,
                                    item.get("caption") or "")
            log.info("forwarded %s to main channel %s", item["file_id"], main_id)
        except Exception as exc:
            log.warning("main-channel forward failed: %s", exc)
            try:
                admin = (config.ADMIN_IDS or [None])[0]
                if admin:
                    await bot.send_message(
                        admin,
                        "\u26a0\ufe0f Forward to main channel failed for "
                        f"{item['file_id']}: {exc}\n"
                        "(Is the bot admin in the main channel?)")
            except Exception:
                pass
    return item


async def schedule_daily(job_queue):
    """(Re)install the daily drip job from settings.post_time in
    settings.post_timezone. Removes the job when schedule is disabled."""
    job_queue = getattr(job_queue, "job_queue", job_queue)  # accept Application or JobQueue
    if job_queue is None:
        log.warning("schedule_daily: no job_queue available; skipping")
        return
    import datetime as _dt
    for j in job_queue.jobs():
        if j.name == "daily_post":
            j.schedule_removal()
    s = await db.get_settings()
    if not s.get("schedule_enabled", True):
        log.info("daily schedule disabled")
        return
    hh, mm = (s.get("post_time", "18:00").split(":") + ["0"])[:2]
    tz, label = _resolve_tz(s.get("post_timezone"))
    job_queue.run_daily(
        _daily_cb,
        time=_dt.time(hour=int(hh), minute=int(mm), tzinfo=tz),
        name="daily_post")
    log.info("daily post scheduled at %02d:%02d %s", int(hh), int(mm), label)


async def _daily_cb(context):
    s = await db.get_settings()
    if s.get("schedule_paused"):
        log.info("schedule_paused=True; skipping today's post")
        return
    await do_post(context.bot)


_EXTRA_HELP = (
    "Scheduling and queue\n"
    "/setschedule HH:MM TZ - set daily time (e.g. /setschedule 07:00 IST)\n"
    "/schedule on|off - enable or disable daily posting\n"
    "/pauseposting - pause the daily job\n"
    "/resumeposting - resume the daily job\n"
    "/queueinfo - next 10 queued posts\n"
    "/queue_reset N - reset queue to post number N\n"
    "Main channel forward\n"
    "/setpostmainchannel id|off - forward posts to a main channel\n"
    "/setposttag text|off - tag line sent above each forward"
)
try:
    HELP_ADMIN += "\n\n" + escape_markdown(_EXTRA_HELP, version=2)
except NameError:
    pass
