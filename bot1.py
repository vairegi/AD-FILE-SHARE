"""Bot 1 — the Link / Gate bot (multi-category).

Responsibilities
----------------
* One independent drip-posting pipeline PER CATEGORY (each with its own DB
  channel -> Posting channel -> optional Main channel, time, pause state).
* /dripnow manual posting (per category).
* Force-subscribe gate (global default, optional per-category override).
* Shortener verification gate (per category: one verification unlocks that
  category only, for verify_hours).
* Issues short-lived single-use tokens deep-linking into Bot 2.
* Live indexing of EVERY registered Database Channel + the full admin panel.
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

# A genuine shortener visit (redirect chain + on-page timer + captcha)
# cannot complete faster than this. Faster == bypass script.
BYPASS_MIN_SECONDS = 150

# Category post times are always interpreted in IST (Asia/Kolkata).
IST = "Asia/Kolkata"

WELCOME = (
    "👋 Welcome!\n\n"
    "This bot gives access to the files posted on the channel.\n"
    "Tap the Download button under any post to get started."
)

# /help is sent with parse_mode=HTML (no MarkdownV2 escaping pitfalls — HTML
# only needs & < > escaped, which the section builder below already does).

def _hh(t: str) -> str:
    """HTML-escape a plain-text line (we never put & < > in command names)."""
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


HELP_USER = (
    "📖 <b>Commands</b>\n\n"
    "/start — Start the bot\n"
    "/help — Show this list\n\n"
    "<b>How to get a file</b>\n"
    "1. Tap ⬇️ Download under any channel post\n"
    "2. Join the channel if asked, then tap ✅\n"
    "3. Complete the quick verification\n"
    "4. Tap 📥 Get File — the delivery bot sends it"
)

HELP_ADMIN = (
    "\n\n🛠 <b>Admin commands</b>\n"
    "\n<b>▸ Pipelines (categories)</b>\n"
    "/addcategory &lt;key&gt; &lt;label&gt; — guided setup wizard\n"
    "/categories — dashboard of every pipeline\n"
    "/editcategory &lt;key&gt; — edit a pipeline\n"
    "/delcategory &lt;key&gt; — remove a pipeline\n"
    "/use &lt;key&gt; — set the active pipeline\n"
    "\n<b>▸ Posting &amp; queue</b> (scoped to the active pipeline, or append a key)\n"
    "/dripnow · /rescandb · /scandb &lt;id&gt;\n"
    "/queueinfo · /queue_reset &lt;N&gt;\n"
    "/pauseposting · /resumeposting\n"
    "/schedule on|off · /setschedule HH:MM\n"
    "/setposttime HH:MM (IST)\n"
    "/setdbchannel · /setpostchannel · /setpostmainchannel · /setposttag\n"
    "\n<b>▸ Access &amp; content</b>\n"
    "/protect on|off — per-category content protection\n"
    "/setautodelete &lt;time&gt; — per-category auto-delete\n"
    "/setforcesub &lt;channel_id | off&gt; [category] — force-join\n"
    "\n<b>▸ Shortener gate</b> (global)\n"
    "/shortener on|off|status · /shortenerapi &lt;url&gt;\n"
    "/setverifytime &lt;hours&gt; · /settokenttl &lt;minutes&gt;\n"
    "/shortenermsg · /shortenerbotmsg · /verifymsg\n"
    "/shortenerbtn &lt;label&gt; | &lt;url&gt; · /clearshortenerbtns\n"
    "\n<b>▸ General</b>\n"
    "/broadcast &lt;message&gt; — copy to all users\n"
    "/stats — overview + per-pipeline breakdown\n"
    "/ban &lt;user_id&gt; · /unban &lt;user_id&gt;\n"
    "/addadmin &lt;user_id&gt; — promote an admin"
)


# ── small helpers ─────────────────────────────────────────────
def _hhmm(value: str):
    try:
        h, m = (value or "18:00").strip().split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return 18, 0


async def _channel_link(bot, channel_id):
    """Best-effort public link for a channel."""
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
        if member.status == "restricted":
            return bool(getattr(member, "is_member", False))
        return member.status in ("member", "administrator", "creator")
    except Exception as exc:
        log.warning("get_chat_member failed for %s: %s", channel_id, exc)
        return None


async def _force_sub_channel_for(category):
    """Per-category override, else the global default, else None."""
    if category:
        cat = await db.get_category(category)
        if cat and cat.get("force_sub_channel_id"):
            return cat["force_sub_channel_id"]
    return (await db.get_settings()).get("force_sub_channel_id")


async def gate_ok(bot, user_id, category=None) -> bool:
    """Force-subscribe gate: active member OR a pending join request passes."""
    channel = await _force_sub_channel_for(category)
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


async def send_force_sub(bot, chat_id, file_id, category=None):
    channel = await _force_sub_channel_for(category)
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
    category = item.get("category") or await db.resolve_category(file_id)
    if not await gate_ok(bot, user_id, category):
        await send_force_sub(bot, chat_id, file_id, category)
        return
    settings = await db.get_settings()
    if not settings.get("shortener_enabled"):
        await deliver_now(bot, chat_id, user_id, item)
        return
    # Per-category verification: a valid pass for THIS category skips the
    # shortener; otherwise (or for a different category) the gate is shown.
    if category and await db.is_verified(user_id, category):
        await deliver_now(bot, chat_id, user_id, item)
        return
    await send_shortener_gate(bot, chat_id, user_id, item, settings)


async def process_verify(bot, chat_id, user_id, file_id, token, username=None):
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

    # ── anti-bypass timing gate ───────────────────────────────
    elapsed = db.now() - float(doc.get("created_at") or 0)
    if elapsed < BYPASS_MIN_SECONDS:
        await db.mark_token_used(token)  # burn the bypassed link
        strikes = await db.add_strike(user_id)
        if strikes >= 3:
            await db.set_banned(user_id, True)
            await db.reset_strikes(user_id)
            uname = f"@{username}" if username else "(no username)"
            await bot.send_message(
                chat_id,
                "🚫 You have been banned for repeatedly bypassing the "
                "verification links.")
            for aid in await db.list_admin_ids():
                try:
                    await bot.send_message(
                        aid,
                        f"🚨 User {user_id} auto-banned for 3 consecutive "
                        f"bypass strikes.\nUsername: {uname}\n"
                        f"Last elapsed: {elapsed:.1f}s\n"
                        f"/unban {user_id} to reverse")
                except Exception as exc:
                    log.warning("admin alert to %s failed: %s", aid, exc)
            return
        await bot.send_message(
            chat_id,
            f"⚠️ UNAUTHORIZED ACTION (Strike {strikes}/3)\n\n"
            "You tried to bypass the link to get the files.\n\n"
            "Our server security system flagged this request. If you reach "
            "3 strikes, you will be temporarily/permanently banned.")
        # give them a fresh link to solve properly
        item = await db.get_item_by_file_id(file_id)
        if item:
            settings = await db.get_settings()
            await send_shortener_gate(bot, chat_id, user_id, item, settings)
        return

    await db.mark_token_used(token)
    await db.reset_strikes(user_id)  # consecutive strikes only
    settings = await db.get_settings()
    item = await db.get_item_by_file_id(file_id)
    category = (item or {}).get("category") or await db.resolve_category(file_id)
    hours = int(settings.get("verify_hours") or 6)
    if category:
        # per-category verification (also mirrors into the legacy flag)
        await db.mark_verified(user_id, category, hours)
    else:
        await db.mark_verified(user_id, hours)  # legacy/global form

    if not item:
        await bot.send_message(chat_id, "❌ This file is no longer available.")
        return
    if not await gate_ok(bot, user_id, category):
        await send_force_sub(bot, chat_id, file_id, category)
        return
    await deliver_now(bot, chat_id, user_id, item)


# ── posting logic (shared by the scheduler and /dripnow) ──────
async def _forward_with_tag(bot, post_channel, post_msg_id, main_id, tag,
                            markup=None):
    """FORWARD the just-published post to the Main Posting Channel, then send
    the tag as a REPLY (quote) to it. When the forward itself fails (e.g.
    /protect on) fall back to copy_message so the main channel still gets it."""
    try:
        fwd = await bot.forward_message(
            chat_id=main_id, from_chat_id=post_channel,
            message_id=post_msg_id)
    except Exception as exc:
        log.warning("forward to main failed (%s); copying instead", exc)
        fwd = await bot.copy_message(
            chat_id=main_id, from_chat_id=post_channel,
            message_id=post_msg_id, reply_markup=markup)
    if tag:
        try:
            await bot.send_message(
                main_id, tag, reply_to_message_id=fwd.message_id,
                allow_sending_without_reply=True)
        except Exception as exc:
            log.warning("tag quote failed, sending plain tag: %s", exc)
            try:
                await bot.send_message(main_id, tag)
            except Exception as exc2:
                log.error("tag message failed entirely: %s", exc2)
    return fwd


async def do_post(bot, category=None, _depth=0):
    """Post the next queued cover for ONE category to its Posting Channel,
    then (optionally) forward it to that category's Main channel with its tag.
    Self-heals past deleted DB messages. Cursor-safe via Mongo."""
    if _depth >= 10:
        log.error("do_post: too many dead items in a row; aborting")
        return None
    cat = await db.get_category(category) if category else None
    if category and not cat:
        log.warning("do_post: unknown category %r", category)
        return None
    post_channel = (cat or {}).get("post_channel_id")
    db_channel = (cat or {}).get("db_channel_id")
    if not post_channel or not db_channel:
        # legacy fallback for the very first migrated pipeline
        s = await db.get_settings()
        post_channel = post_channel or s.get("post_channel_id") or config.POST_CHANNEL_ID
        db_channel = db_channel or s.get("db_channel_id") or config.DB_CHANNEL_ID
    if not post_channel or not db_channel:
        log.warning("post/db channel not configured; skipping post.")
        return None

    item = await db.next_unposted(category)
    if not item:
        log.info("do_post: queue empty for %r", category)
        return None

    cover_id = item.get("cover_message_id")
    if not cover_id and item.get("videos"):
        cover_id = item["videos"][0]["db_message_id"]
    if not cover_id:
        await db.mark_posted(item["db_message_id"], category=category)
        return None

    link = f"https://t.me/{config.BOT1_USERNAME}?start=file_{item['file_id']}"
    post_no = await db.count_posted(category) + 1
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"#{post_no} 𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱", url=link)]])
    try:
        # Channel posts are NEVER protected - /protect only applies to the
        # files Bot 2 delivers to users.
        msg = await bot.copy_message(
            chat_id=post_channel, from_chat_id=db_channel,
            message_id=cover_id, reply_markup=markup,
            has_spoiler=True)
    except Exception as exc:
        # Source message was deleted from the DB channel: skip the dead item.
        log.warning("do_post: db message %s gone (%s); skipping %s",
                    item["db_message_id"], exc, item["file_id"])
        await db.mark_posted(item["db_message_id"], category=category)
        return await do_post(bot, category, _depth + 1)
    await db.mark_posted(item["db_message_id"], post_message_id=msg.message_id,
                         category=category)
    log.info("posted %s (db id %s) -> %s [%s]", item["file_id"],
             item["db_message_id"], post_channel, category)
    main_id = (cat or {}).get("post_main_channel_id")
    if main_id:
        try:
            await _forward_with_tag(bot, post_channel, msg.message_id,
                                    main_id, (cat or {}).get("post_tag"), markup)
            log.info("forwarded %s to main channel %s", item["file_id"], main_id)
        except Exception as exc:
            log.warning("main-channel forward failed: %s", exc)
            try:
                admin = (config.ADMIN_IDS or [None])[0]
                if admin:
                    await bot.send_message(
                        admin,
                        "⚠️ Forward to main channel failed for "
                        f"{item['file_id']}: {exc}\n"
                        "(Is the bot admin in the main channel?)")
            except Exception:
                pass
    return item


# ── per-category scheduling (times are IST) ───────────────────
async def schedule_daily(job_queue):
    """(Re)install ONE daily drip job per enabled category, each at that
    category's post_time in IST. Accepts an Application or a JobQueue."""
    job_queue = getattr(job_queue, "job_queue", job_queue)  # Application or JobQueue
    if job_queue is None:
        log.warning("schedule_daily: no job_queue available; skipping")
        return
    for j in list(job_queue.jobs()):
        if j.name and j.name.startswith("daily_post"):
            j.schedule_removal()
    from zoneinfo import ZoneInfo
    ist = ZoneInfo(IST)
    for cat in await db.list_categories(enabled_only=True):
        if not cat.get("schedule_enabled", True):
            log.info("schedule disabled for %s", cat["key"])
            continue
        hh, mm = _hhmm(cat.get("post_time"))
        key = cat["key"]
        job_queue.run_daily(
            _make_daily_cb(key),
            time=datetime.time(hour=hh, minute=mm, tzinfo=ist),
            name=f"daily_post:{key}")
        log.info("daily post [%s] scheduled at %02d:%02d IST", key, hh, mm)


def _make_daily_cb(key):
    async def _cb(context: ContextTypes.DEFAULT_TYPE):
        cat = await db.get_category(key)
        if not cat or not cat.get("enabled", True):
            return
        if cat.get("schedule_paused"):
            log.info("schedule_paused=True for %s; skipping today's post", key)
            return
        await do_post(context.bot, category=key)
    return _cb


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
            await process_verify(context.bot, user.id, user.id, file_id, token,
                                 getattr(user, "username", None))
            return

    await update.message.reply_text(WELCOME)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command list; admins also see the full admin panel list."""
    user = update.effective_user
    text = HELP_USER
    if user and await is_admin(user.id):
        text += HELP_ADMIN
    await update.message.reply_text(text, parse_mode="HTML")


async def on_checksub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    file_id = (query.data or "").split(":", 1)[-1]
    category = await db.resolve_category(file_id)
    if await gate_ok(context.bot, user.id, category):
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
    """Live-index new messages appearing in ANY registered Database Channel.

    Routes by chat_id -> the owning category, so each pipeline indexes only
    its own source channel and never touches another category's queue."""
    msg = update.channel_post
    cat = await db.category_for_db_channel(msg.chat_id)
    if not cat:
        # Back-compat: fall back to the legacy single global DB channel.
        settings = await db.get_settings()
        if not settings.get("db_channel_id") or msg.chat_id != settings.get("db_channel_id"):
            return
        cat_key = None
    else:
        cat_key = cat["key"]
    kind = scanner.classify_from_botapi(msg)
    if not kind:
        return
    await db.ingest_raw({
        "message_id": msg.message_id,
        "kind": kind,
        "caption": msg.caption or "",
    }, category=cat_key)
    await db.rebuild_items(cat_key)


# ── application factory ───────────────────────────────────────
def build_bot1() -> Application:
    app = Application.builder().token(config.BOT1_TOKEN).updater(None).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    for name, func in ADMIN_COMMANDS.items():
        app.add_handler(CommandHandler(name, func))
    app.add_handler(CallbackQueryHandler(on_checksub, pattern=r"^checksub:"))
    # category-management inline actions (post now / pause / resume / delete)
    from bot1_admin import on_category_action
    app.add_handler(CallbackQueryHandler(on_category_action, pattern=r"^cat:"))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    # conversational /addcategory + /editcategory wizard (free-text answers)
    from bot1_admin import wizard_message_handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   wizard_message_handler), group=1)
    return app


# === tz helper retained for admin /setschedule compat ===
_TZ_ALIASES = {
    "IST": "Asia/Kolkata", "INDIA": "Asia/Kolkata", "CHENNAI": "Asia/Kolkata",
    "PKT": "Asia/Karachi", "BST": "Asia/Dhaka", "ICT": "Asia/Bangkok",
    "JST": "Asia/Tokyo", "KST": "Asia/Seoul", "WIB": "Asia/Jakarta",
    "GMT": "Etc/GMT", "UTC": "UTC",
}


def _resolve_tz(name):
    """Return (tzinfo, canonical_label). Falls back to Asia/Kolkata (IST)."""
    from zoneinfo import ZoneInfo
    raw = (name or IST).strip()
    cand = _TZ_ALIASES.get(raw.upper(), raw)
    for c in (cand, cand.replace(" ", "/")):
        try:
            return ZoneInfo(c), c
        except Exception:
            pass
    return ZoneInfo(IST), IST
