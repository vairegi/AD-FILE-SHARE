"""Bot 1 — the Link / Gate bot (multi-category).

Responsibilities
----------------
* One independent drip-posting pipeline PER CATEGORY (each with its own DB
  channel -> Posting channel -> optional Main channel, time, pause state).
* /dripnow manual posting (per category).
* Force-subscribe gate (global default, optional per-category override).
* Shortener verification gate (STRICT PER-POST since v2.4: every Download tap
  requires its own shortener solve — no global/per-category skip; admins bypass).
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


async def _notify_admin(bot, text):
    """DM every admin a failure report (never raises)."""
    for aid in await db.list_admin_ids():
        try:
            await bot.send_message(aid, text)
        except Exception as exc:
            log.warning("admin alert to %s failed: %s", aid, exc)


def _is_gone(exc) -> bool:
    """True only when the source message was genuinely deleted/absent.
    Anything else (bad kwarg, permissions, flood) must NOT be treated as a
    deletable item, or a single bug would silently mark the whole queue posted."""
    msg = str(exc).lower()
    return ("message to copy not found" in msg
            or "message not found" in msg
            or "message_id_invalid" in msg
            or "message identifier is not specified" in msg)


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
    "4. Tap 📥 Get File — the delivery bot sends it\n\n"
    "Or tap 📂 <b>Browse</b> (/browse) to explore by category &amp; genre."
)

HELP_ADMIN = (
    "\n\n🛠 <b>Admin commands</b>\n"
    "\n<b>▸ Pipelines (categories)</b>\n"
    "/addcategory &lt;key&gt; &lt;label&gt; — guided setup wizard\n"
    "/categories — dashboard of every pipeline\n"
    "/editcategory &lt;key&gt; — edit a pipeline\n"
    "/renamecategory &lt;key&gt; &lt;label&gt; — rename display name (links keep working)\n"
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
    "/forcesublist · /forcesubremove &lt;channel_id|category&gt;\n"
    "\n<b>▸ Shortener gate</b> (global)\n"
    "/shortener on|off|status · /shortenerapi add|pause|resume|remove\n"
    "/setverifytime &lt;hours&gt; · /settokenttl &lt;minutes&gt;\n"
    "/shortenermsg · /shortenerbotmsg · /verifymsg\n"
    "/shortenerbtn &lt;label&gt; | &lt;url&gt; · /clearshortenerbtns\n"
    "\n<b>▸ General</b>\n"
    "/broadcast &lt;message&gt; — copy to all users\n"
    "/stats — overview + per-pipeline breakdown\n"
    "/ban &lt;user_id&gt; · /unban &lt;user_id&gt;\n"
    "/banlist · /banmessage &lt;text|reply|reset&gt;\n"
    "/addsticker · /removesticker — sticker after every post (main channel)\n"
    "/addbutton &lt;label&gt; | &lt;link&gt; [| green|blue|red] — extra button under every post\n"
    "/buttons · /removebutton &lt;n&gt; · /clearbuttons — manage extra buttons\n"
    "/addcovercaption &lt;text|off&gt; — appended to every posted cover caption\n"
    "/addfilecaption &lt;text|off&gt; — appended to every delivered file caption\n"
    "/addadmin &lt;user_id&gt; — promote an admin\n"
    "/verified_users [yesterday] — today's verifications as a table"
    "\n\n<b>▸ Genres &amp; browse menu</b> (v4.4)\n"
    "/addgenre &lt;pipeline&gt; &lt;genre&gt; — add a genre (one-time caption scan, then auto)\n"
    "/delgenre &lt;pipeline&gt; &lt;genre&gt; · /genres [pipeline] — manage genres\n"
    "/onbrowse · /offbrowse — enable/disable the /browse menu (users: /browse)\n"
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
    channels = await db.force_sub_channels(category)
    for channel in channels or ([channel] if channel else []):
        if not channel:
            continue
        member = await _is_member(bot, channel, user_id)
        if member is True:
            continue
        if member is None:
            # check itself failed (bot not admin etc.) — fail open, logged in _is_member
            continue
        # v3.5: join request must be for THIS channel (was: any request passed
        # every channel -> users with an old request skipped Gate 1 forever)
        if await db.has_join_request(user_id, channel):
            continue
        return False   # missing at least one required channel
    return True
    if member is None:
        # Misconfiguration (bot not admin / wrong id) — fail open so real users
        # are not locked out. Log it so the admin can fix the setup.
        log.warning("Force-sub check inconclusive; failing open for user %s", user_id)
        return True
    return False


async def send_force_sub(bot, chat_id, file_id, category=None):
    channel = await _force_sub_channel_for(category)
    rows = []
    for ch in await db.force_sub_channels(category):
        # prefer the stored join-request invite link (v3.5), else best-effort
        u = await db.force_sub_link(ch, category) or await _channel_link(bot, ch)
        if u:
            rows.append([InlineKeyboardButton("📢 Join Channel", url=u)])
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
    short, state, site, failures = await shortener.shorten(
        deep, user_id=user_id, with_status=True)
    # v4.1: record on the token whether a LIVE shortener served it — the
    # anti-bypass ban in process_verify only applies to 'active' tokens.
    await db.set_token_shortener(token, site, state=state)
    if state == "down":
        # No provider could serve (vplink/arolink down, all paused, or none
        # configured): hand the file over directly, NEVER ban the user for
        # our outage, and alert the admins which provider(s) failed.
        await db.mark_token_used(token)
        log.warning("shorteners down %s — direct delivery to user %s",
                    failures, user_id)
        for aid in await db.list_admin_ids():
            try:
                await bot.send_message(
                    aid,
                    "⚠️ Shortener outage: no provider could shorten a link "
                    f"(failed: {', '.join(failures) or 'none active'}).\n"
                    f"User {user_id} got the file directly — no verification, "
                    "no ban. Check the /shortenerapi dashboard.")
            except Exception as exc:
                log.warning("admin outage alert to %s failed: %s", aid, exc)
        await deliver_now(bot, chat_id, user_id, item)
        return

    rows = [[InlineKeyboardButton("🔓 Verify & Download", url=short or deep)]]
    for b in settings.get("shortener_buttons") or []:
        rows.append([InlineKeyboardButton(b["label"], url=b["url"])])

    body = settings.get("shortenerbot_msg") or ""
    _html = settings.get("shortener_msg_html")
    if _html:
        # v3.9: rich heading (set via /shortenermsg with entities / reply)
        _text = f"{_html}\n\n{body}" if body else _html
        _pm = "HTML"
    else:
        heading = settings.get("shortener_msg") or "Verification required"
        _text = f"{heading}\n\n{body}"
        _pm = None
    await bot.send_message(
        chat_id, _text,
        reply_markup=InlineKeyboardMarkup(rows),
        disable_web_page_preview=True,
        parse_mode=_pm,
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
    # Admin bypass (v2.4): admins receive the file immediately, no shortener.
    # Only the shortener step is skipped — force-sub (above) still applies.
    if await is_admin(user_id):
        await deliver_now(bot, chat_id, user_id, item)
        return
    settings = await db.get_settings()
    if not settings.get("shortener_enabled"):
        await deliver_now(bot, chat_id, user_id, item)
        return
    # STRICT per-post verification (v2.4): solving the shortener for one post
    # NEVER grants access to another post (or to this same post again). Every
    # Download tap shows the shortener gate; there is no global/per-category
    # skip. Applies automatically to all current and future categories.
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
    # v4.1: only treat a sub-150s return as a bypass when the token's link was
    # served by a LIVE shortener. Tokens issued while every provider was down
    # ('down') reached the user as a direct link — instant return is expected
    # and must NEVER ban them.
    if (elapsed < BYPASS_MIN_SECONDS
            and doc.get("shortener_state", "active") != "down"):
        await db.mark_token_used(token)  # burn the bypassed link
        # v3.2: ZERO TOLERANCE — a single too-fast attempt bans instantly.
        # No 3-strike grace period anymore.
        await db.add_strike(user_id)   # keep a record of the attempt
        await db.set_banned(user_id, True)
        await db.mark_ban_info(user_id, username, elapsed)
        uname = f"@{username}" if username else "(no username)"
        _bm = (await db.get_settings()).get("ban_message") or \
            "🚫 You have been banned for bypassing the verification link."
        await bot.send_message(chat_id, _bm)
        for aid in await db.list_admin_ids():
            try:
                await bot.send_message(
                    aid,
                    f"🚨 User {user_id} auto-banned for bypassing the "
                    f"verification link (instant ban, no warnings).\n"
                    f"Username: {uname}\n"
                    f"Elapsed: {elapsed:.1f}s\n"
                    f"/unban {user_id} to reverse")
            except Exception as exc:
                log.warning("admin alert to %s failed: %s", aid, exc)
        return

    await db.mark_token_used(token)
    await db.reset_strikes(user_id)  # consecutive strikes only
    # v4.3: record this successful verification for /verified_users.
    # Link type: the shortener that served the token, or the v4.1 outage
    # bypass labelled "Direct (outage)".
    _lt = doc.get("shortener_site") or (
        "Direct (outage)" if doc.get("shortener_state") == "down" else None)
    try:
        _u = await bot.get_chat(user_id)
        _name = getattr(_u, "full_name", None) or getattr(_u, "first_name", None)
    except Exception:
        _name = None
    _item0 = await db.get_item_by_file_id(file_id)
    _cat0 = (_item0 or {}).get("category") or await db.resolve_category(file_id)
    try:
        await db.record_verification(
            user_id, name=_name, username=username, elapsed=elapsed,
            category=_cat0, link_type=_lt)
    except Exception as exc:
        log.warning("verification log failed (never blocks delivery): %s", exc)
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
def _btn(text, url, color=None):
    """One inline button. `color` maps to the Bot API 9.4 button `style`
    (success=green / primary=blue / danger=red). PTB 21.x has no `style`
    field yet, so it rides through api_kwargs (merged verbatim into the
    outgoing payload by TelegramObject.to_dict). color=None keeps the
    default transparent look (no style key at all)."""
    ak = {}
    if color:
        ak["style"] = color
    return InlineKeyboardButton(text, url=url, api_kwargs=ak)


def build_post_markup(link, post_no, extra_buttons):
    """Channel-post keyboard (v4.0): row 1 is always the GREEN, full-width
    Download button; /addbutton extras follow TWO per row (a single extra
    takes the whole row). Pure -> unit-tested in test_all.py."""
    rows = [[_btn(f"#{post_no} 𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱", link, "success")]]
    extras = list(extra_buttons or [])
    for i in range(0, len(extras), 2):
        rows.append([_btn(b["label"], b["url"], b.get("color"))
                     for b in extras[i:i + 2]])
    return InlineKeyboardMarkup(rows)


def _build_styled_markup(markup, clean=False):
    """Copy of a markup whose buttons carry their style via api_kwargs —
    passed as a RAW reply_markup dict through api_kwargs on send. With
    clean=True every style is stripped (fallback when Telegram rejects the
    styled payload: the post itself must never be lost)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            b.text, url=b.url,
            api_kwargs={} if clean else dict(getattr(b, "api_kwargs", None) or {}))
         for b in row]
        for row in markup.inline_keyboard])


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


async def _post_cover(bot, post_channel, db_channel, item, markup, styled=False):
    """Post the cover image BLURRED (has_spoiler) so only the IMAGE is hidden;
    the caption text stays normal.

    v4.0: the /addcovercaption extra is APPENDED after the original caption.
    With styled=True the reply markup is rebuilt with Bot API 9.4 button
    styles (green Download etc.) and sent as a raw dict through api_kwargs.

    copy_message cannot apply a spoiler, so when the cover's Bot API file_id
    was captured at scan/index time we re-send the photo with
    send_photo(has_spoiler=True). Falls back to copy_message (no spoiler, full
    caption preserved) when the cover is not a photo, its file_id is unknown
    (items scanned before this upgrade), or the caption exceeds the photo
    caption limit — so posting NEVER breaks."""
    cover_id = item.get("cover_message_id")
    if not cover_id and item.get("videos"):
        cover_id = item["videos"][0]["db_message_id"]
    caption = (item.get("caption") or "").strip()
    _cc_extra = (await db.get_settings()).get("cover_caption_extra")
    if _cc_extra:
        caption = (caption + "\n" + str(_cc_extra).strip()).strip()
    photo_fid = item.get("cover_file_id")
    api_kwargs = ({"reply_markup": _build_styled_markup(markup).to_dict()}
                  if styled else None)
    if photo_fid and len(caption) <= 1024:
        kwargs = {"caption": caption or None, "reply_markup": markup,
                  "has_spoiler": True}
        if api_kwargs:
            kwargs["api_kwargs"] = api_kwargs
        return await bot.send_photo(chat_id=post_channel, photo=photo_fid,
                                    **kwargs)
    kwargs = {"reply_markup": markup}
    if api_kwargs:
        kwargs["api_kwargs"] = api_kwargs
    return await bot.copy_message(
        chat_id=post_channel, from_chat_id=db_channel,
        message_id=cover_id, **kwargs)


async def do_post(bot, category=None, _depth=0):
    """Post the next queued cover for ONE category to its Posting Channel,
    then (optionally) forward it to that category's Main channel with its tag.
    Self-heals ONLY past genuinely-deleted DB messages. Any other error aborts
    the run and reports to the admin. Cursor-safe via Mongo."""
    if _depth >= 10:
        log.error("do_post: too many dead items in a row; aborting")
        await _notify_admin(
            bot,
            f"⚠️ [{category}] Daily post aborted: 10 deleted/empty items in a "
            f"row. The queue stopped here. Run /queueinfo {category} to inspect "
            "and /queue_reset N " + str(category) + " to rewind if needed.")
        return None
    cat = await db.get_category(category) if category else None
    if category and not cat:
        log.warning("do_post: unknown category %r", category)
        await _notify_admin(bot, f"⚠️ Post failed: unknown pipeline '{category}'.")
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
        await _notify_admin(
            bot, f"⚠️ [{category}] Daily post skipped: Posting or Database "
                 f"channel is not configured. Set them via /editcategory {category}.")
        return None

    item = await db.next_unposted(category)
    if not item:
        log.info("do_post: queue empty for %r", category)
        await _notify_admin(
            bot, f"ℹ️ [{category}] Daily post: queue is empty (nothing to post). "
                 f"Add files to the DB channel or /rescandb {category}.")
        return None

    cover_id = item.get("cover_message_id")
    if not cover_id and item.get("videos"):
        cover_id = item["videos"][0]["db_message_id"]
    if not cover_id:
        await db.mark_posted(item["db_message_id"], category=category)
        return None

    link = f"https://t.me/{config.BOT1_USERNAME}?start=file_{item['file_id']}"
    post_no = await db.count_posted(category) + 1
    # v4.0: green full-width Download button + any global /addbutton extras.
    settings = await db.get_settings()
    markup = build_post_markup(link, post_no, settings.get("post_buttons") or [])
    try:
        # Channel posts are NEVER protected - /protect only applies to the
        # files Bot 2 delivers to users. styled=True attaches the button
        # colors via a raw reply_markup passthrough (api_kwargs).
        msg = await _post_cover(bot, post_channel, db_channel, item, markup,
                                styled=True)
    except Exception as exc:
        if _is_gone(exc):
            # Source message genuinely deleted: skip the dead item, keep going.
            log.warning("do_post: db message %s gone (%s); skipping %s",
                        item["db_message_id"], exc, item["file_id"])
            await db.mark_posted(item["db_message_id"], category=category)
            return await do_post(bot, category, _depth + 1)
        # v4.0: if the styled (colored-button) payload itself was rejected,
        # retry ONCE with the same buttons but no styles — the daily post
        # must never be lost over a cosmetic feature.
        try:
            log.warning("do_post: retrying %s without button styles",
                        item["file_id"])
            msg = await _post_cover(bot, post_channel, db_channel, item,
                                    markup, styled=False)
        except Exception:
            # Any OTHER error: do NOT mark posted. Abort and report the admin.
            log.error("do_post: failed to post %s: %s", item["file_id"], exc)
            await _notify_admin(
                bot,
                f"⚠️ [{category}] Daily post FAILED on {item['file_id']}\n"
                f"Error: {exc}\n"
                "Nothing was posted and the queue was NOT advanced. Fix the "
                f"cause, then /dripnow {category}.")
            return None
    await db.mark_posted(item["db_message_id"], post_message_id=getattr(msg, "message_id", None),
                         category=category)
    # v4.0: the saved sticker goes to the pipeline's MAIN posting channel
    # (used to go to the base posting channel — and that old line referenced
    # an undefined `settings`, a latent NameError). No main channel -> skip.
    _sid = settings.get("post_sticker_id")
    _main_ch = (cat or {}).get("post_main_channel_id")
    if _sid and _main_ch:
        try:
            await bot.send_sticker(chat_id=_main_ch, sticker=_sid)
        except Exception as exc:
            log.warning("post sticker to main channel failed: %s", exc)

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
            await _notify_admin(
                bot,
                f"⚠️ [{category}] Forward to main channel failed for "
                f"{item['file_id']}: {exc}\n(Is the bot admin in the main channel?)")
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


# ── v4.0: timed-/broadcast deletion sweeper (Bot 1's own messages) ──
async def sweep_broadcast_deletions(context: ContextTypes.DEFAULT_TYPE):
    """Delete broadcast copies whose auto-delete timer has elapsed. Scoped to
    entries tagged bot='bot1' — only the sending bot can delete a message."""
    for entry in await db.due_deletions(bot="bot1"):
        for message_id in entry.get("message_ids", []):
            try:
                await context.bot.delete_message(entry["chat_id"], message_id)
            except Exception:
                pass
        await db.remove_deletion(entry["_id"])


async def reset_verification_logs(context):
    """v4.3: midnight-IST hygiene — drop logs older than today+yesterday.
    The report reads today's date key only, so the new day starts fresh by
    design; this job just keeps the collection clean."""
    await db.purge_old_verifications()


def schedule_broadcast_sweeper(application):
    """Install the repeating broadcast-deletion sweeper on Bot 1's job queue."""
    jq = getattr(application, "job_queue", application)  # Application or JobQueue
    if jq is None:
        return
    for job in jq.get_jobs_by_name("broadcast_sweep"):
        job.schedule_removal()
    jq.run_repeating(sweep_broadcast_deletions, interval=60, first=20,
                     name="broadcast_sweep")
    # v4.3: daily reset of the verified-users log at 00:00 IST
    from zoneinfo import ZoneInfo
    import datetime as _dt
    for job in jq.get_jobs_by_name("verified_reset"):
        job.schedule_removal()
    jq.run_daily(reset_verification_logs,
                 time=_dt.time(hour=0, minute=0,
                               tzinfo=ZoneInfo("Asia/Kolkata")),
                 name="verified_reset")


# ── command / update handlers ─────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.touch_user(user.id)
    record = await db.get_user(user.id)
    if record and record.get("banned"):
        _bm = (await db.get_settings()).get("ban_message") or \
            "🚫 You are banned from using this bot."
        await update.message.reply_text(_bm)
        return

    args = context.args or []
    if args:
        payload = args[0]
        if payload.startswith("file_"):
            await process_file(context.bot, user.id, user.id, payload[5:])
            return
        if payload.startswith("verify_"):
            rest = payload[len("verify_"):]
            # rpartition: file_id may itself contain '_' (category-prefixed ids
            # like "jav_f30"); the token is always the final '_' segment.
            file_id, _, token = rest.rpartition("_")
            await process_verify(context.bot, user.id, user.id, file_id, token,
                                 getattr(user, "username", None))
            return

    # v4.4: the plain /start welcome carries a direct entry into the genre
    # browse menu (categories -> genres -> posted items). Banned users and
    # the gate flows above returned long before this point.
    await update.message.reply_text(WELCOME, reply_markup=_menu_markup(
        [[InlineKeyboardButton("\U0001F4C2 Browse collection",
                               callback_data="menu:home")]]))


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


# ══════════════════════════════════════════════════════════════
#  BROWSE MENU — Bot API 10.3 RICH messages + EPHEMERAL views (v4.5)
# ══════════════════════════════════════════════════════════════
# The menu is a genuine RICH MESSAGE: InputRichMessage.blocks with buttons
# EMBEDDED as InputRichBlockButtons / RichMessageButton blocks — never an
# InlineKeyboardMarkup under a plain text message.
#
# * GROUPS: the menu is an EPHEMERAL message (sendRichMessage with
#   ephemeral_message_parameters.receiver_user_id) — visible only to the user
#   who opened it. Follow-up taps arrive carrying Message.ephemeral_message_id
#   and edit it in place via editEphemeralMessageText (the docs require
#   replace_callback_query_message=False for callbacks FROM ephemeral
#   messages — Bot API 10.3).
# * PRIVATE CHATS: ephemeral views do not exist there (docs: ephemeral =
#   group messages visible to one user), so the menu is a normal rich
#   message edited with editMessageText(rich_message=...).
# * python-telegram-bot ships NO rich/ephemeral helpers (verified against the
#   latest release), so every call goes through bot._post — the same raw-API
#   path already proven in production by /banlist and /verified_users.
# * Level-3 item buttons are URL deep links into the existing /start file_
#   flow: force-sub, shortener and the Bot 2 token handoff apply exactly
#   like channel posts — the menu can never bypass monetization or the gates.
BROWSE_PAGE_SIZE = 10
BROWSE_DISABLED_MSG = (
    "🚫 Browsing is currently disabled by the admin.\n"
    "Use the Download buttons under channel posts meanwhile.")


def _menu_markup(rows):
    """Plain inline keyboard — only used for the /start welcome entry button."""
    return InlineKeyboardMarkup(rows)


def _rt(text):
    """RichText PLAIN node = a bare JSON string. Per the Bot API docs, RichText
    'can be either a String for plain text, an Array of RichText, or any of
    the [typed] classes' — there is NO plain-text object. v4.5 used
    {"type": "plain"} and v4.5.1 used {"text", "entities"} (that shape is only
    valid for table CELLS, which is why /banlist works); both objects were
    rejected with 'can't find field "type"'. A string is the correct leaf."""
    return str(text)


def _rbtn(label, callback_data=None, url=None, style=None):
    """RichMessageButton: text + exactly ONE action field (docs: url, or
    callback_data of 1-64 bytes). style: danger/success/primary/link."""
    b = {"text": _rt(label)}
    if style:
        b["style"] = style
    if url:
        b["url"] = url
    elif callback_data is not None:
        b["callback_data"] = callback_data
    return b


def _btn_row(*buttons):
    """One InputRichBlockButtons row (docs: 1-8 buttons shown in one row)."""
    return {"type": "buttons", "buttons": list(buttons)}


def _heading(text, size=3):
    """InputRichBlockSectionHeading (size 1 largest .. 6 smallest)."""
    return {"type": "heading", "text": _rt(text), "size": size}


def _para(text):
    """InputRichBlockParagraph."""
    return {"type": "paragraph", "text": _rt(text)}


def _rich(blocks):
    """InputRichMessage payload ({'blocks': [...]})."""
    return {"blocks": blocks}


def _menu_home_payload(cats):
    """Level 0: one embedded button row per ENABLED pipeline (a new pipeline
    appears automatically — pure DB read, nothing hardcoded)."""
    blocks = [_heading("📚 Browse the collection", 2)]
    if not cats:
        blocks.append(_para("No categories are available yet."))
        return _rich(blocks)
    blocks.append(_para("Pick a category:"))
    for c in cats:
        blocks.append(_btn_row(_rbtn(
            f"📁 {(c.get('label') or c['key']).strip()}",
            callback_data=f"menu:cat:{c['key']}")))
    return _rich(blocks)


def _menu_genres_payload(cat, genres):
    """Level 1: the category's genres (one embedded row each) + Back."""
    key, label = cat["key"], (cat.get("label") or cat["key"]).strip()
    blocks = [_heading(f"📁 {label}", 3)]
    if genres:
        blocks.append(_para("Pick a genre:"))
        for g in genres:
            blocks.append(_btn_row(_rbtn(
                f"🏴 {g['genre'].title()} ({len(g.get('matched') or [])})",
                callback_data=f"menu:g:{key}:{g['genre']}")))
    else:
        blocks.append(_para("No genres here yet — check back soon."))
    blocks.append(_btn_row(_rbtn("◀️ Back", callback_data="menu:home")))
    return _rich(blocks)


def _menu_results_payload(cat, genre, items, page, page_size=BROWSE_PAGE_SIZE):
    """Level 3: posted items as embedded URL buttons (deep links into the
    gated /start flow), paginated. Every callback_data stays under Telegram's
    64-byte limit by construction (genre slugs capped at db.GENRE_MAX_LEN)."""
    key, label = cat["key"], (cat.get("label") or cat["key"]).strip()
    g = genre["genre"]
    total = len(items)
    pages = max(1, -(-total // page_size))
    page = max(0, min(page, pages - 1))
    blocks = [_heading(f"🏴 {label} · {g.title()}", 3),
              _para(f"{total} posted item{'s' if total != 1 else ''}"
                    + (f" — page {page + 1}/{pages}" if pages > 1 else ""))]
    if not items:
        blocks.append(_btn_row(_rbtn("📭 Nothing posted here yet",
                                     callback_data="menu:noop")))
    for i, it in enumerate(items[page * page_size:(page + 1) * page_size]):
        title = ((it.get("caption") or "").strip().split("\n")[0]
                 or it.get("file_id") or "item")[:40]
        blocks.append(_btn_row(_rbtn(
            f"{page * page_size + i + 1}. {title}",
            url=(f"https://t.me/{config.BOT1_USERNAME}"
                 f"?start=file_{it['file_id']}"),
            style="success")))
    nav = []
    if page > 0:
        nav.append(_rbtn("◀️ Prev",
                         callback_data=f"menu:p:{key}:{g}:{page - 1}"))
    if page < pages - 1:
        nav.append(_rbtn("Next ▶️",
                         callback_data=f"menu:p:{key}:{g}:{page + 1}"))
    if nav:
        blocks.append(_btn_row(*nav))
    blocks.append(_btn_row(_rbtn("◀️ Genres", callback_data=f"menu:cat:{key}"),
                           _rbtn("🏠 Home", callback_data="menu:home")))
    return _rich(blocks)


def _eph_id(message):
    """ephemeral_message_id is not in PTB's Message model (Bot API 10.2+);
    PTB keeps unknown fields in api_kwargs — read it from either place."""
    eid = getattr(message, "ephemeral_message_id", None)
    if eid is None:
        eid = (getattr(message, "api_kwargs", None) or {}).get(
            "ephemeral_message_id")
    return eid


async def _browse_render(target, context, level, key=None, genre=None,
                         page=0):
    """Render one menu level as a RICH message. `target` is a Message from
    /browse (level='message') or a callback Query (level='callback').
    Stale taps (pipeline/genre deleted meanwhile) re-render the parent level
    instead of erroring. Returns False when nothing should be sent (banned
    user / browse disabled — the user was already told)."""
    user = target.from_user
    uid = user.id if user else None
    if uid is not None:
        rec = await db.get_user(uid)
        if rec and rec.get("banned"):
            _bm = (await db.get_settings()).get("ban_message") or \
                "🚫 You are banned from using this bot."
            if level == "callback":
                await target.answer(_bm, show_alert=True)
            else:
                await target.reply_text(_bm)
            return False
        await db.touch_user(uid)
    if not (await db.get_settings()).get("browse_enabled", True):
        if level == "callback":
            await target.answer("Browsing is currently disabled.",
                                show_alert=True)
        else:
            await target.reply_text(BROWSE_DISABLED_MSG)
        return False

    if key is None:                                   # level 0: categories
        payload = _menu_home_payload(
            await db.list_categories(enabled_only=True))
    elif genre is None:                               # level 1: genres
        cat = await db.get_category(key)
        if not cat or not cat.get("enabled", True):
            payload = _menu_home_payload(
                await db.list_categories(enabled_only=True))
        else:
            payload = _menu_genres_payload(cat, await db.list_genres(key))
    else:                                             # level 3: results
        cat = await db.get_category(key)
        gdoc = await db.get_genre(key, genre)
        if not cat or not cat.get("enabled", True):
            payload = _menu_home_payload(
                await db.list_categories(enabled_only=True))
        elif not gdoc:
            payload = _menu_genres_payload(cat, await db.list_genres(key))
        else:
            items = await db.items_by_genre(key, genre, posted_only=True)
            payload = _menu_results_payload(cat, gdoc, items, page)

    if level == "callback":
        await target.answer()
        msg = target.message
        chat_id = getattr(msg, "chat_id", None) or msg.chat.id
        eid = _eph_id(msg)
        if eid is not None:
            # tap INSIDE an ephemeral menu (groups) -> edit it in place
            method = "editEphemeralMessageText"
            data = {"chat_id": chat_id, "receiver_user_id": uid,
                    "ephemeral_message_id": eid, "rich_message": payload}
        else:
            method = "editMessageText"
            data = {"chat_id": chat_id, "message_id": msg.message_id,
                    "rich_message": payload}
    else:
        chat_id = getattr(target, "chat_id", None) or target.chat.id
        method = "sendRichMessage"
        data = {"chat_id": chat_id, "rich_message": payload}
        ctype = getattr(getattr(target, "chat", None), "type", None)
        if ctype in ("group", "supergroup") and uid is not None:
            # first render in a group -> ephemeral: only this user sees it
            data["ephemeral_message_parameters"] = {"receiver_user_id": uid}
    try:
        await context.bot._post(method, data=data)
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return True
        err = str(exc)[:160]
        log.exception("rich browse menu failed (%s): %s", method, err)
        if level == "callback":
            try:
                await target.answer(f"Rich menu error: {err[:150]}",
                                    show_alert=True)
            except Exception:
                pass
        else:
            await target.reply_text(
                f"⚠️ Rich menu failed to load.\nError: {err}\n"
                "(Please screenshot this and send it to the admin.)")
    return True


async def browse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/browse — opens the genre menu as a rich message. Works in DMs and
    groups alike; in groups it is EPHEMERAL (visible only to the sender),
    and the results deep-link into a Bot 1 DM so the gates run privately."""
    msg = update.effective_message
    if msg:
        await _browse_render(msg, context, "message")


async def on_browse_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback router for the browse menu (menu:home / menu:cat:<key> /
    menu:g:<key>:<genre> / menu:p:<key>:<genre>:<page> / menu:noop)."""
    query = update.callback_query
    data = query.data or ""
    if data == "menu:noop":
        await query.answer()
        return
    parts = data.split(":")
    try:
        if data == "menu:home" or len(parts) < 3:
            await _browse_render(query, context, "callback")
        elif parts[1] == "cat":
            await _browse_render(query, context, "callback", key=parts[2])
        elif parts[1] == "g" and len(parts) >= 4:
            await _browse_render(query, context, "callback",
                                 key=parts[2], genre=parts[3], page=0)
        elif parts[1] == "p" and len(parts) >= 5:
            await _browse_render(query, context, "callback",
                                 key=parts[2], genre=parts[3],
                                 page=int(parts[4]))
        else:
            await query.answer()
    except Exception:
        log.exception("browse menu callback failed: %s", data)
        try:
            await query.answer("Something went wrong — try /browse again.",
                               show_alert=True)
        except Exception:
            pass


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record pending join requests so they also satisfy the force-sub gate."""
    req = update.chat_join_request
    await db.record_join_request(req.from_user.id, req.chat.id)
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
    entry = {"message_id": msg.message_id, "kind": kind,
             "caption": msg.caption or ""}
    # capture the Bot API file_id of cover photos so do_post can blur them
    if kind == "cover" and msg.photo:
        entry["file_id"] = msg.photo[-1].file_id
    await db.ingest_raw(entry, category=cat_key)
    await db.rebuild_items(cat_key)
    if kind == "cover" and cat_key:
        # v4.4: lazy incremental genre matching — regex the fresh caption
        # against this pipeline's genres (in-memory, microseconds); matched
        # genres store the new file_id so the DB is NEVER re-scanned.
        try:
            await db.match_item_genres(cat_key,
                                       db.make_file_id(cat_key, msg.message_id),
                                       entry["caption"])
        except Exception as exc:
            log.warning("genre match for new item failed (non-fatal): %s", exc)


# ── application factory ───────────────────────────────────────
def build_bot1() -> Application:
    app = Application.builder().token(config.BOT1_TOKEN).updater(None).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    for name, func in ADMIN_COMMANDS.items():
        app.add_handler(CommandHandler(name, func))
    app.add_handler(CallbackQueryHandler(on_checksub, pattern=r"^checksub:"))
    # v4.4: /browse command + genre menu callbacks (registered BEFORE the
    # catch-all text handlers; own 'menu:' namespace, never collides)
    app.add_handler(CommandHandler("browse", browse_cmd))
    app.add_handler(CallbackQueryHandler(on_browse_menu, pattern=r"^menu:"))
    # category-management inline actions (post now / pause / resume / delete)
    from bot1_admin import on_category_action
    app.add_handler(CallbackQueryHandler(on_category_action, pattern=r"^cat:"))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    from bot1_admin import sticker_intake
    from telegram.ext import MessageHandler, filters as _flt
    # v3.9 FIX: group=1 and STICKER-only filter. As group-0 filters.ALL it ran
    # AFTER nothing (same group as catch-all text below) and, worse, swallowed
    # every non-command message silently — the forwarded sticker never got a
    # reply and was never saved. Now: dedicated later group, stickers only.
    app.add_handler(MessageHandler(_flt.Sticker.ALL, sticker_intake), group=1)
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    # conversational /addcategory + /editcategory wizard (free-text answers)
    from bot1_admin import wizard_message_handler
    # v4.0: captures the admin's time answer after /broadcast (runs BEFORE the
    # wizard handler; both are keyed by user id and never active together)
    from bot1_admin import broadcast_pending_reply
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   broadcast_pending_reply), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   wizard_message_handler), group=1)
    schedule_broadcast_sweeper(app)
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
