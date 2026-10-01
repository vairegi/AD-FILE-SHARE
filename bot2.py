"""Bot 2 — the File Delivery bot.

* Handles /start deliver_<file_id>_<token> deep links from Bot 1.
* Validates the short-lived, single-use, user-bound token.
* Copies the video from the Database Channel straight to the user via
  copy_message (server-side, so large files and the 20 MB download limit
  are both irrelevant, and the source chat is never exposed).
* Queues the delivered messages for auto-deletion (restart-safe queue).

v4.7: Bot 2 enforces its OWN force-subscribe gate (the same /setforcesub
channel list Bot 1 manages — a user must satisfy it in BOTH bots), delivers
every video + subtitle of a post AT ONCE via a single copyMessages batch,
and carries its own /broadcast + /checkram admin commands.
"""
import asyncio
import logging
import re

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


async def _ban_msg():
    """Custom ban message (v3.5 /banmessage), else the default."""
    return (await db.get_settings()).get("ban_message") or \
        "🚫 You are banned from using this bot."

from utils import admin_only, format_ram_report, human_duration, parse_duration

log = logging.getLogger("bot2")


# ══════════════════════════════════════════════════════════════
#  v4.7: FORCE-SUBSCRIBE GATE (Bot 2's own — same list as Bot 1)
# ══════════════════════════════════════════════════════════════
async def _channel_link(bot, channel_id):
    """Best-effort public link for a channel."""
    try:
        chat = await bot.get_chat(channel_id)
        if getattr(chat, "username", None):
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
        log.warning("bot2 get_chat_member failed for %s: %s", channel_id, exc)
        return None


async def gate_ok(bot, user_id, category=None) -> bool:
    """Force-subscribe gate: active member OR a pending join request passes.
    Same channel list Bot 1 enforces (per-category override else the global
    /setforcesub list); a check that itself fails fails OPEN, like Bot 1."""
    for channel in await db.force_sub_channels(category):
        if not channel:
            continue
        member = await _is_member(bot, channel, user_id)
        if member is True:
            continue
        if member is None:
            continue  # misconfiguration — never lock real users out
        if await db.has_join_request(user_id, channel):
            continue  # a join request to THIS channel also passes
        return False   # missing at least one required channel
    return True


async def send_force_sub(bot, chat_id, sub_payload, category=None):
    """Join prompt; '✅ I've Joined' resumes `sub_payload`:
    'f_<file_id>' re-runs the delivery, 'r' re-sends the user's held files."""
    rows = []
    for ch in await db.force_sub_channels(category):
        u = await db.force_sub_link(ch, category) or await _channel_link(bot, ch)
        if u:
            rows.append([InlineKeyboardButton("📢 Join Channel", url=u)])
    rows.append([InlineKeyboardButton("✅ I've Joined",
                                      callback_data=f"sub2:{sub_payload}")])
    await bot.send_message(
        chat_id,
        "🔒 To continue you must join our channel first.\n\n"
        "Tap *Join Channel*, then tap *I've Joined*.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def resume_delivery(bot, chat_id, user_id, file_id):
    """Re-run the full delivery after the gate passes — mints a fresh
    single-use token internally so every validation layer still applies."""
    ttl = int((await db.get_settings()).get("token_ttl_minutes") or 10)
    token = await db.create_token(user_id, file_id, ttl, kind="deliver")
    await process_delivery(bot, chat_id, user_id, file_id, token)


async def resend_held_files(bot, chat_id, user_id):
    """Re-deliver every not-yet-deleted file the user already holds, ALL AT
    ONCE via one copyMessages batch (server-side copy from the own chat)."""
    mids = await db.pending_delivery_message_ids(user_id)
    if not mids:
        await bot.send_message(
            chat_id,
            "✅ Membership confirmed.\nYou have no files waiting — tap "
            "Download in the main bot to get one.")
        return
    await bot.send_message(chat_id,
                           "✅ Membership confirmed — sending your files…")
    try:
        sent = await bot.copy_messages(
            chat_id=chat_id, from_chat_id=chat_id, message_ids=mids[:100])
        delivered = [getattr(m, "message_id", None) for m in sent]
    except Exception as exc:
        log.error("copy_messages resend failed: %s", exc)
        await bot.send_message(
            chat_id,
            "⚠️ Failed to re-send your files. Please tap Download again.")
        return
    minutes = await _autodelete_minutes(user_id)
    if minutes and minutes > 0:
        await db.add_deletion(chat_id, [m for m in delivered if m],
                              db.now() + minutes * 60, bot="bot2")
        note = (f"⏳ These files will be auto-deleted in "
                f"{human_duration(minutes * 60)}.")
    else:
        note = "📌 These files will stay in the chat."
    await bot.send_message(chat_id, note)


async def on_checksub2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'✅ I've Joined' under Bot 2's own force-sub prompt."""
    query = update.callback_query
    user = query.from_user
    payload = (query.data or "").split(":", 1)[-1]
    file_id = payload[2:] if payload.startswith("f_") else None
    category = await db.resolve_category(file_id) if file_id else None
    if await gate_ok(context.bot, user.id, category):
        await query.answer("Thanks for joining!")
        try:
            await query.edit_message_text("✅ Membership confirmed.")
        except Exception:
            pass
        if file_id:
            await resume_delivery(context.bot, user.id, user.id, file_id)
        else:
            await resend_held_files(context.bot, user.id, user.id)
    else:
        await query.answer(
            "You haven't joined yet. Please join, then tap again.",
            show_alert=True)


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record pending join requests so they also satisfy Bot 2's gate."""
    req = update.chat_join_request
    await db.record_join_request(req.from_user.id, req.chat.id)
    log.info("bot2 recorded join request from %s", req.from_user.id)


# ══════════════════════════════════════════════════════════════
#  v4.7: BROADCAST (same UX as Bot 1; audience = Bot 2 starters)
# ══════════════════════════════════════════════════════════════
_BROADCAST_PENDING = {}


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply mode: FORWARD the replied-to message to every Bot 2 user (keeps
    the 'Forwarded from' tag and media as-is). Inline (/broadcast <text>):
    copy the command message itself. The bot first asks for an auto-delete
    timer, THEN sends — paced (~3/sec) with 429 retry_after handling — and
    queues every copy for deletion with Bot 2's own sweeper (bot='bot2')."""
    msg = update.message
    target = msg.reply_to_message
    if target is None and not msg.text.partition(" ")[2].strip():
        await update.message.reply_text(
            "Usage: reply to any message with /broadcast to forward it to "
            "all Bot 2 users — or /broadcast <text>.\n"
            "The bot then asks after how long the broadcast should be "
            "deleted from users.")
        return
    if target is not None:
        _BROADCAST_PENDING[update.effective_user.id] = {
            "mode": "forward", "chat_id": msg.chat_id,
            "message_id": target.message_id}
    else:
        _BROADCAST_PENDING[update.effective_user.id] = {
            "mode": "copy", "chat_id": msg.chat_id,
            "message_id": msg.message_id}
    await update.message.reply_text(
        "⏳ After how many hours or minutes should this broadcast message be "
        "deleted from users?\n\n"
        "Reply with e.g. <code>2h</code>, <code>30m</code>, <code>1h 2m</code> "
        "— or <code>never</code> to keep it forever.\n"
        "Send /cancel to abort.",
        parse_mode="HTML")


async def _run_broadcast(bot, job):
    """Paced (~0.35 s apart, under Telegram's global limit) + 429 retry_after
    honoured per user (up to 3 attempts) — every user eventually receives it."""
    ids = await db.all_user_ids_for("bot2")
    total = len(ids)
    pace = 0.35
    sent = failed = 0
    delivered = {}
    for i, uid in enumerate(ids):
        ok = False
        for attempt in range(3):
            try:
                if job["mode"] == "forward":
                    m = await bot.forward_message(
                        chat_id=uid, from_chat_id=job["chat_id"],
                        message_id=job["message_id"])
                else:
                    m = await bot.copy_message(
                        chat_id=uid, from_chat_id=job["chat_id"],
                        message_id=job["message_id"])
                ok = True
                mid = getattr(m, "message_id", None)
                if mid:
                    delivered.setdefault(uid, []).append(mid)
                break
            except Exception as exc:
                wait = getattr(exc, "retry_after", None)
                if wait is not None:                 # Telegram 429 — honor it
                    log.info("bot2 broadcast 429: retry user %s after %ss",
                             uid, wait)
                    await asyncio.sleep(min(float(wait) + 1.0, 30.0))
                    continue
                log.debug("bot2 broadcast to %s failed permanently: %s",
                          uid, exc)
                break
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(pace)
        if total > 100 and i and i % 100 == 0:
            log.info("bot2 broadcast progress: %d/%d (sent=%d failed=%d)",
                     i, total, sent, failed)
    return sent, failed, delivered


async def broadcast_pending_reply(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE):
    """Free-text handler (group 1) capturing the admin's auto-delete answer.
    Ignores everyone else (no pending broadcast for them)."""
    user = update.effective_user
    if not user:
        return
    job = _BROADCAST_PENDING.get(user.id)
    if not job:
        return
    from utils import is_admin
    if not await is_admin(user.id):
        return
    text = (update.message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        _BROADCAST_PENDING.pop(user.id, None)
        await update.message.reply_text(
            "✖️ Broadcast cancelled — nothing was sent.")
        return
    seconds = parse_duration(text)
    if seconds is None:
        await update.message.reply_text(
            "⚠️ Could not understand that time. Try <code>2h</code>, "
            "<code>30m</code>, <code>1h 2m</code> — or <code>never</code>.",
            parse_mode="HTML")
        return
    _BROADCAST_PENDING.pop(user.id, None)
    _ids_n = len(await db.all_user_ids_for("bot2"))
    await update.message.reply_text(
        f"📣 Broadcasting to {_ids_n} Bot 2 users… "
        f"(~{_ids_n * 0.35 / 60:.0f} min — paced to respect Telegram limits "
        "so EVERY user receives it. You can keep using the bot meanwhile; "
        "I'll report when done.)")
    sent, failed, delivered = await _run_broadcast(context.bot, job)
    if seconds > 0 and delivered:
        delete_at = db.now() + seconds
        for uid, mids in delivered.items():
            await db.add_deletion(uid, mids, delete_at, bot="bot2")
        tail = (f"🗑 Auto-delete scheduled — the broadcast disappears from "
                f"every user in {human_duration(seconds)}.")
    elif seconds > 0:
        tail = "⚠️ Nothing was delivered, so no auto-delete was scheduled."
    else:
        tail = "📌 Kept forever — this broadcast will NOT be auto-deleted."
    await update.message.reply_text(
        f"✅ Done. Sent: {sent} · Failed: {failed}\n{tail}")


@admin_only
async def cmd_checkram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/checkram — RAM of the single process that hosts BOTH bots (that total
    is what Render bills against), plus a per-bot estimate."""
    await update.message.reply_text(format_ram_report(), parse_mode="HTML")

WELCOME = (
    "👋 This bot delivers your files.\n\n"
    "Use the *Get File* button from the main bot to receive a file here.\n\n"
    "Configure how long files stay in this chat with:\n"
    "/setautodelete 5min · 2hour · 12hour · 7day · never\n"
    "/withfilemessages [time] <text> — set your own deletion notice"
)


# ── auto-delete queue ─────────────────────────────────────────
async def sweep_deletions(context: ContextTypes.DEFAULT_TYPE):
    """Delete any delivered messages whose timer has elapsed.
    v4.0: scoped to Bot 2's own messages — broadcasts are sent by Bot 1 and
    can only be deleted by Bot 1 (its own sweeper handles those)."""
    due = await db.due_deletions(bot="bot2")
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
async def send_item(bot, chat_id, item, index, note=True):
    """Deliver one version (video at `index`) + (for the first version) the
    subtitle files. `note=False` skips the trailing auto-delete notice so a
    multi-version delivery posts it only once, after the last file."""
    db_channel = await _db_channel_for(item)
    if not db_channel:
        await bot.send_message(chat_id, "❌ Delivery channel is not configured.")
        return

    # /protect on -> users cannot forward or save the delivered file
    protect = await _protect_flag(item.get("category"))

    video = item["videos"][index]
    # v4.0: the /addfilecaption extra is APPENDED after the file's own caption.
    _fc_extra = (await db.get_settings()).get("file_caption_extra")
    _fc = None
    if _fc_extra:
        _fc = ((video.get("caption") or "").strip()
               + "\n" + _fc_extra.strip()).strip()[:1024]
    try:
        copy_kwargs = {"protect_content": protect}
        if _fc:
            copy_kwargs["caption"] = _fc
        sent = await bot.copy_message(
            chat_id=chat_id, from_chat_id=db_channel,
            message_id=video["db_message_id"],
            **copy_kwargs,
        )
    except Exception as exc:
        log.error("copy_message failed: %s", exc)
        await bot.send_message(
            chat_id,
            "⚠️ Failed to deliver the file. Please go back and tap Download again.",
        )
        return

    delivered = [getattr(sent, "message_id", None)]

    # Attach the subtitle file(s) once, with the FIRST delivered version.
    for srt in (item.get("srts") or []) if index == 0 else []:
        try:
            sent_srt = await bot.copy_message(
                chat_id=chat_id, from_chat_id=db_channel,
                message_id=srt["db_message_id"],
                protect_content=protect,
            )
            delivered.append(getattr(sent_srt, "message_id", None))
        except Exception as exc:
            log.warning("srt copy failed: %s", exc)

    if not note:
        # Queue deletion for this file even mid-batch (restart-safe), but the
        # visible notice is posted only by the final call of the batch.
        minutes = await _autodelete_minutes(chat_id, item.get("category"))
        if minutes and minutes > 0:
            await db.add_deletion(chat_id, [m for m in delivered if m],
                                  db.now() + minutes * 60)
        return
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
            chat_id, await _ban_msg())
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

    # v4.7: deliver EVERYTHING at once — ONE copyMessages batch copies all
    # videos AND the subtitles server-side in a single API call, so the user
    # receives the whole post in one go (order preserved). copyMessages has
    # no per-message caption override, so the /addfilecaption extra travels
    # as ONE follow-up message after the batch instead.
    db_channel = await _db_channel_for(item)
    if not db_channel:
        await bot.send_message(chat_id,
                               "❌ Delivery channel is not configured.")
        return
    protect = await _protect_flag(item.get("category"))
    mids = [v["db_message_id"] for v in item["videos"]]
    mids += [s["db_message_id"] for s in (item.get("srts") or [])]
    try:
        sent = await bot.copy_messages(
            chat_id=chat_id, from_chat_id=db_channel,
            message_ids=mids[:100], protect_content=protect)
        delivered = [getattr(m, "message_id", None) for m in sent]
    except Exception as exc:
        log.error("copy_messages failed: %s", exc)
        await bot.send_message(
            chat_id,
            "⚠️ Failed to deliver the files. Please go back and tap "
            "Download again.")
        return

    _s = await db.get_settings()
    _fc_html = _s.get("file_caption_extra_html")
    _fc_plain = _s.get("file_caption_extra")
    if _fc_html or _fc_plain:
        try:
            await bot.send_message(
                chat_id, str(_fc_html or _fc_plain)[:4096],
                parse_mode="HTML" if _fc_html else None)
        except Exception as exc:
            log.warning("file caption extra failed (%s); sending plain", exc)
            if _fc_plain:
                try:
                    await bot.send_message(chat_id, str(_fc_plain)[:4096])
                except Exception:
                    pass

    minutes = await _autodelete_minutes(chat_id, item.get("category"))
    if minutes and minutes > 0:
        await db.add_deletion(chat_id, [m for m in delivered if m],
                              db.now() + minutes * 60, bot="bot2")
        default_note = (f"⏳ This file will be auto-deleted in "
                        f"{human_duration(minutes * 60)}.")
    else:
        default_note = "📌 This file will stay in the chat."
    template = await _withfile_template(item.get("category"))
    note = (render_withfile_message(template, minutes)
            if template else default_note)
    await bot.send_message(chat_id, note)


# ── handlers ──────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.touch_user(user.id, bot="bot2")   # v4.7: Bot 2's own audience
    record = await db.get_user(user.id)
    if record and record.get("banned"):
        await update.message.reply_text(await _ban_msg())
        return

    args = context.args or []
    if args and args[0].startswith("deliver_"):
        rest = args[0][len("deliver_"):]
        # v4.7: Bot 1's Get File link may carry a SECOND (verify) token for
        # the force-sub resume, marked with a trailing '_g':
        #   deliver_<file_id>_<deliver_token>[_<verify_token>_g]
        _dual = rest.endswith("_g")
        if _dual:
            rest = rest[:-2]
        # rpartition: file_id may itself contain '_' (category-prefixed ids);
        # the token is always the final '_' segment.
        file_id, _, token = rest.rpartition("_")
        if _dual:
            # tail segment is the VERIFY token — the deliver token is the
            # segment before it.
            file_id, _, token = file_id.rpartition("_")
        # v4.7: Bot 2 enforces the force-sub gate ITSELF. The token is NOT
        # burned when the gate fails — '✅ I've Joined' resumes the delivery.
        item = await db.get_item_by_file_id(file_id)
        category = (item or {}).get("category") or \
            await db.resolve_category(file_id)
        if not await gate_ok(context.bot, user.id, category):
            await send_force_sub(context.bot, user.id, f"f_{file_id}",
                                 category)
            return
        await process_delivery(context.bot, user.id, user.id, file_id, token)
        return

    # v4.7: a bare /start is gated too — after joining, every held
    # (not-yet-deleted) file is re-sent ALL AT ONCE.
    if not await gate_ok(context.bot, user.id):
        await send_force_sub(context.bot, user.id, "r")
        return

    await update.message.reply_text(WELCOME)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command list (v4.7, regrouped); admins also see Bot 2's admin panel."""
    from utils import is_admin
    text = (
        "📖 <b>Commands</b>\n\n"
        "/start — Start the bot (re-sends your held files)\n"
        "/help — Show this list\n\n"
        "Files arrive when you tap 📥 Get File in the main bot — every video "
        "and subtitle of the post is delivered together, in one go.")
    if update.effective_user and await is_admin(update.effective_user.id):
        text += (
            "\n\n🛠 <b>Admin commands</b>\n"
            "\n<b>▸ Messaging</b>\n"
            "/broadcast &lt;message&gt; — send to every Bot 2 user (or reply "
            "to a message; asks for an auto-delete timer first)\n"
            "\n<b>▸ Delivery</b>\n"
            "/setautodelete &lt;time&gt; — auto-delete timer for delivered files\n"
            "/withfilemessages [time] &lt;text&gt; — the deletion notice text\n"
            "\n<b>▸ System</b>\n"
            "/checkram — RAM usage: both bots together + per-bot estimate")
    await update.message.reply_text(text, parse_mode="HTML")


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
        await query.answer(await _ban_msg(),
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
    # v4.7: /broadcast (Bot 2 audience) + /checkram
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("checkram", cmd_checkram))
    app.add_handler(CallbackQueryHandler(on_download, pattern=r"^dl:"))
    # v4.7: Bot 2's own force-sub gate — join requests + 'I've Joined' button
    app.add_handler(CallbackQueryHandler(on_checksub2, pattern=r"^sub2:"))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    # v4.7: captures the admin's auto-delete answer after /broadcast
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   broadcast_pending_reply), group=1)
    return app
