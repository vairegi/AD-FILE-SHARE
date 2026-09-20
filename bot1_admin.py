"""Bot 1 admin panel — every command is guarded by @admin_only.

ADMIN_IDS (env) plus any user promoted with /addadmin can run these.

Multi-category (v2.0)
---------------------
Pipeline/category settings live in the `categories` collection and are managed
entirely through the bot:
  /addcategory <key> <label>   — guided wizard: DB channel -> posting channel
                                 -> main channel -> tag -> daily time (IST)
  /categories                  — one-screen dashboard of every pipeline
  /editcategory <key>          — wizard to change any field later
  /delcategory <key>           — remove a pipeline (with confirmation)
  /use <key>                   — set the ACTIVE pipeline for scoped commands

Scoped commands (act on the active category, or take a trailing key):
  /dripnow /rescandb /scandb /queueinfo /queue_reset /pauseposting
  /resumeposting /schedule /setposttime /setschedule /protect
  /setautodelete /setdbchannel /setpostchannel /setpostmainchannel /setposttag

Global (unaffected by /use):
  /shortener* /verifymsg /setverifytime /settokenttl /broadcast /stats
  /ban /unban /addadmin /setforcesub (supports a trailing category key for
  per-category overrides)

NOTE: do_post / schedule_daily are imported lazily inside the commands that
need them — importing them here at module level creates a circular import
(bot1 -> bot1_admin -> bot1) that crashes the process on startup.
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import db
import scanner
from utils import admin_only, human_duration, parse_duration
from telegram.helpers import escape_markdown

log = logging.getLogger("bot1.admin")


# ── small shared helpers ──────────────────────────────────────
def _h(text) -> str:
    """Escape a string for HTML parse_mode."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _parse_hhmm(text):
    try:
        hh, mm = text.split(":")[:2]
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    except Exception:
        pass
    return None


def _parse_channel_id(text):
    t = (text or "").strip()
    return int(t) if t.lstrip("-").isdigit() else None


async def _channel_info(bot, channel_id) -> str:
    """Channel title with its invite link embedded (HTML), or 'not set'."""
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


async def _resolve_active(update, args, start=0):
    """Resolve the target category for a scoped command.

    Priority: trailing argument (a category key) -> the admin's active
    category (set via /use). Returns (key, remaining_args)."""
    args = list(args or [])
    if len(args) > start:
        maybe = args[-1].lower()
        if await db.get_category(maybe):
            return maybe, args[:-1]
    uid = update.effective_user.id if update.effective_user else None
    active = await db.get_active_category(uid) if uid else None
    return active, args


async def _auto_key(update, key):
    """Single-category auto-select: with exactly one pipeline configured,
    scoped commands work with no /use needed. Returns a key or None."""
    if key:
        return key
    cats = await db.list_categories()
    if len(cats) == 1:
        return cats[0]["key"]
    return None


async def _require_active(update, key):
    """Reply with guidance when no category could be resolved. Returns bool."""
    if key:
        return True
    cats = await db.list_categories()
    if not cats:
        await update.message.reply_text(
            "No pipelines exist yet. Create one with /addcategory <key> <label>.")
    else:
        await update.message.reply_text(
            "Multiple pipelines exist. Pick one with /use <key> first "
            "(or append the key to this command). See /categories.")
    return False


# ══════════════════════════════════════════════════════════════
#  CATEGORY MANAGEMENT + WIZARD
# ══════════════════════════════════════════════════════════════
# In-memory wizard sessions: {admin_user_id: {"mode", "step", "key", "data"}}.
# Ephemeral by design (a redeploy just restarts the wizard) — only a 2-minute
# setup conversation. Durable state lives in Mongo.
_WIZARD = {}


def _p_db_channel(t):
    cid = _parse_channel_id(t)
    return ("db_channel_id", cid) if cid is not None else (None, "Send a numeric channel id (e.g. -1001234567890).")


def _p_post_channel(t):
    cid = _parse_channel_id(t)
    return ("post_channel_id", cid) if cid is not None else (None, "Send a numeric channel id (e.g. -1001234567890).")


def _p_main_channel(t):
    if t.strip().lower() in ("skip", "-", "none", "off"):
        return ("post_main_channel_id", None)
    cid = _parse_channel_id(t)
    return ("post_main_channel_id", cid) if cid is not None else (None, "Send a numeric channel id, or 'skip'.")


def _p_tag(t):
    if t.strip().lower() in ("skip", "-", "none", "off"):
        return ("post_tag", None)
    return ("post_tag", t.strip())


def _p_time(t):
    if t.strip().lower() in ("skip", "-", "none"):
        return ("post_time", "18:00")
    v = _parse_hhmm(t)
    return ("post_time", v) if v else (None, "Send the time as HH:MM (24h, IST), e.g. 21:30 — or 'skip'.")


# Ordered ADD-wizard steps: (step_name, prompt, parser)
_WIZARD_STEPS = [
    ("db_channel",   "1/5 · Send the <b>Database Channel</b> id (where raw files are uploaded).", _p_db_channel),
    ("post_channel", "2/5 · Send the <b>Posting Channel</b> id (where covers are drip-posted).", _p_post_channel),
    ("main_channel", "3/5 · Send the <b>Main Posting Channel</b> id, or <code>skip</code>.", _p_main_channel),
    ("tag",          "4/5 · Send the <b>tag line</b> — a caption shown above each forward in the Main Channel (e.g. a hashtag or @handle). Optional: <code>skip</code>.", _p_tag),
    ("time",         "5/5 · Send the <b>daily post time</b> as HH:MM (IST), or <code>skip</code> for 18:00.", _p_time),
]


def _wizard_cancel_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="cat:wizcancel")]])


@admin_only
async def cmd_addcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addcategory <key> <label…> — start the guided pipeline setup wizard."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /addcategory <key> <label>\nExample: /addcategory hanime HAnime")
        return
    key = args[0].lower()
    label = " ".join(args[1:]).strip() or key
    if not key.replace("_", "").isalnum():
        await update.message.reply_text(
            "❌ Key must be letters/numbers/underscore only (no spaces).")
        return
    if await db.get_category(key):
        await update.message.reply_text(
            f"❌ A pipeline named '{key}' already exists. Use /editcategory {key}.")
        return
    uid = update.effective_user.id
    _WIZARD[uid] = {"mode": "add", "step": 0, "key": key,
                    "data": {"key": key, "label": label}}
    await update.message.reply_text(
        f"🧙 Setting up pipeline <b>{_h(label)}</b> (<code>{key}</code>)\n\n"
        + _WIZARD_STEPS[0][1],
        parse_mode="HTML", reply_markup=_wizard_cancel_kb())


@admin_only
async def cmd_editcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/editcategory <key> — re-run the wizard to change a pipeline's settings."""
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /editcategory <key>")
        return
    key = args[0].lower()
    cat = await db.get_category(key)
    if not cat:
        await update.message.reply_text(f"❌ No pipeline named '{key}'. See /categories.")
        return
    uid = update.effective_user.id
    _WIZARD[uid] = {"mode": "edit", "step": 0, "key": key, "data": {}}
    await update.message.reply_text(
        f"✏️ Editing pipeline <b>{_h(cat.get('label') or key)}</b> (<code>{key}</code>)\n"
        "Current values are kept if you press skip where offered.\n\n"
        + _WIZARD_STEPS[0][1],
        parse_mode="HTML", reply_markup=_wizard_cancel_kb())


async def _wizard_finish(update: Update, context, session):
    key, data = session["key"], session["data"]
    try:
        if session["mode"] == "add":
            # pop BOTH reserved keys so **data can never collide with the
            # positional `key` arg (the crash that silently killed the wizard).
            label = data.pop("label", key)
            data.pop("key", None)
            await db.create_category(key, label, **data)
            msg = f"✅ Pipeline <b>{_h(key)}</b> created and LIVE."
        else:
            await db.update_category(key, data)
            msg = f"✅ Pipeline <b>{_h(key)}</b> updated."
    except Exception as exc:
        # NEVER die silently: report the failure so the admin sees what happened
        # instead of the bot just "stopping responding".
        log.exception("wizard finish failed for %s", key)
        await update.message.reply_text(
            f"❌ Setup failed: {exc!r}\nNothing was saved — please retry.")
        return
    # (re)build schedules so the new/edited category gets its daily IST job now
    try:
        from bot1 import schedule_daily
        await schedule_daily(context.application.job_queue)
    except Exception as exc:
        log.warning("reschedule after category save failed: %s", exc)
    await update.message.reply_text(msg, parse_mode="HTML")
    await _send_category_card(update, context, key)
    cat = await db.get_category(key)
    if cat and cat.get("db_channel_id"):
        await update.message.reply_text(
            "Index its Database Channel now?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 Scan now", callback_data=f"cat:scan:{key}"),
                InlineKeyboardButton("Later", callback_data="cat:noop"),
            ]]))


async def wizard_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Free-text handler for in-progress /addcategory + /editcategory wizards.
    Registered at group=1 so it never shadows commands. Ignores non-admins and
    users with no active wizard session."""
    user = update.effective_user
    if not user:
        return
    session = _WIZARD.get(user.id)
    if not session:
        return
    from utils import is_admin
    if not await is_admin(user.id):
        return
    text = (update.message.text or "").strip()
    if text.lower() in ("/cancel", "cancel"):
        _WIZARD.pop(user.id, None)
        await update.message.reply_text("✖️ Wizard cancelled.")
        return
    step_idx = session["step"]
    _, prompt, parser = _WIZARD_STEPS[step_idx]
    field, value = parser(text)
    if field is None:
        await update.message.reply_text(f"⚠️ {value}\n\n{prompt}", parse_mode="HTML")
        return
    session["data"][field] = value
    session["step"] += 1
    try:
        if session["step"] < len(_WIZARD_STEPS):
            await update.message.reply_text(_WIZARD_STEPS[session["step"]][1],
                                            parse_mode="HTML",
                                            reply_markup=_wizard_cancel_kb())
        else:
            _WIZARD.pop(user.id, None)
            await _wizard_finish(update, context, session)
    except Exception as exc:
        log.exception("wizard step %s crashed", session["step"])
        _WIZARD.pop(user.id, None)
        await update.message.reply_text(
            f"❌ Something went wrong: {exc!r}\nWizard reset — start over with /addcategory.")


@admin_only
async def cmd_delcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delcategory <key> — remove a pipeline (asks for confirmation)."""
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /delcategory <key>")
        return
    key = args[0].lower()
    cat = await db.get_category(key)
    if not cat:
        await update.message.reply_text(f"❌ No pipeline named '{key}'.")
        return
    await update.message.reply_text(
        f"⚠️ Delete pipeline <b>{_h(cat.get('label') or key)}</b>?\n\n"
        "Choose whether to also purge its queued/indexed data:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Delete + purge data", callback_data=f"cat:delpurge:{key}")],
            [InlineKeyboardButton("🗑 Delete (keep data)", callback_data=f"cat:delkeep:{key}")],
            [InlineKeyboardButton("✖️ Cancel", callback_data="cat:noop")],
        ]))


@admin_only
async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/use <key> — set the active pipeline for the scoped admin commands."""
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /use <key>  (see /categories)")
        return
    key = args[0].lower()
    cat = await db.get_category(key)
    if not cat:
        await update.message.reply_text(f"❌ No pipeline named '{key}'. See /categories.")
        return
    await db.set_active_category(update.effective_user.id, key)
    await update.message.reply_text(
        f"✅ Active pipeline set to <b>{_h(cat.get('label') or key)}</b>.\n"
        "Scoped commands (/dripnow, /queueinfo, /rescandb, …) now target it.",
        parse_mode="HTML")


async def _send_category_card(update, context, key):
    """The per-category dashboard card with inline action buttons."""
    bot = context.bot
    cat = await db.get_category(key)
    if not cat:
        await update.message.reply_text(f"❌ Pipeline '{key}' no longer exists.")
        return
    total = await db.count_files(key)
    posted = await db.count_posted(key)
    remaining = await db.count_pending(key)
    verified = await db.count_verified(key)
    sm = await db.queue_summary(key, 1)
    nxt = sm["items"][0] if sm["items"] else None

    db_ch = await _channel_info(bot, cat.get("db_channel_id"))
    post_ch = await _channel_info(bot, cat.get("post_channel_id"))
    main_ch = await _channel_info(bot, cat.get("post_main_channel_id"))
    fsub = cat.get("force_sub_channel_id")
    fsub_txt = (await _channel_info(bot, fsub)) if fsub else "global default"

    status = "⏸ paused" if cat.get("schedule_paused") else (
        "✅ active" if cat.get("enabled", True) and cat.get("schedule_enabled", True)
        else "⛔ disabled")
    ad = int(cat.get("auto_delete_minutes") or 0)
    lines = [
        f"📂 <b>{_h(cat.get('label') or key)}</b> (<code>{key}</code>) — {status}",
        f"   🗄 DB Channel: {db_ch}",
        f"   📣 Posting: {post_ch}",
        f"   🏠 Main: {main_ch}"
        + (f" · tag: {_h(cat['post_tag'])}" if cat.get("post_tag") else ""),
        f"   🕒 Schedule: {_h(cat.get('post_time') or '18:00')} IST daily",
        f"   📦 Queue: {total} total · {posted} posted · {remaining} remaining",
        f"   ▶️ Next: #{posted + 1}"
        + (f" — {_h((nxt.get('caption') or nxt.get('file_id') or '')[:40])}" if nxt else " (queue empty)"),
        f"   🔗 Force-sub: {fsub_txt}",
        f"   🛡 Protect: {'ON' if cat.get('protect_content') else 'OFF'}"
        f"  ·  ⏳ Auto-delete: {human_duration(ad * 60)}",
        f"   ✅ Verified users: {verified}",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Post now", callback_data=f"cat:post:{key}"),
         InlineKeyboardButton("⏸ Pause" if not cat.get("schedule_paused") else "▶️ Resume",
                              callback_data=f"cat:togglepause:{key}")],
        [InlineKeyboardButton("✏️ Edit", callback_data=f"cat:edit:{key}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"cat:del:{key}")],
    ])
    await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                    reply_markup=kb, disable_web_page_preview=True)


@admin_only
async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/categories — full dashboard: every pipeline with all its details."""
    cats = await db.list_categories()
    if not cats:
        await update.message.reply_text(
            "No pipelines configured yet.\nCreate one with /addcategory <key> <label>.")
        return
    active = await db.get_active_category(update.effective_user.id)
    await update.message.reply_text(
        f"🗂 <b>{len(cats)} pipeline(s)</b>"
        + (f" — active: <code>{_h(active)}</code>" if active else ""),
        parse_mode="HTML")
    for cat in cats:
        await _send_category_card(update, context, cat["key"])


@admin_only
async def on_category_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline buttons on the category dashboard cards (cat:<action>:<key>)."""
    query = update.callback_query
    data = query.data or ""
    await query.answer()
    if data == "cat:noop":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if data == "cat:wizcancel":
        _WIZARD.pop(query.from_user.id, None)
        try:
            await query.edit_message_text("✖️ Wizard cancelled.")
        except Exception:
            pass
        return
    try:
        _, action, key = data.split(":", 2)
    except ValueError:
        return
    key = key.lower()
    cat = await db.get_category(key)
    if not cat:
        try:
            await query.edit_message_text(f"❌ Pipeline '{key}' no longer exists.")
        except Exception:
            pass
        return

    if action == "post":
        from bot1 import do_post
        item = await do_post(context.bot, category=key)
        await context.bot.send_message(
            query.from_user.id,
            f"✅ [{key}] Posted {item['file_id']}." if item
            else f"ℹ️ [{key}] Nothing posted (queue empty or channels not set).")
    elif action == "togglepause":
        new_state = not cat.get("schedule_paused")
        await db.update_category(key, {"schedule_paused": new_state})
        await context.bot.send_message(
            query.from_user.id,
            f"{'⏸️ Paused' if new_state else '▶️ Resumed'} [{key}].")
    elif action == "edit":
        _WIZARD[query.from_user.id] = {"mode": "edit", "step": 0, "key": key, "data": {}}
        await context.bot.send_message(
            query.from_user.id,
            f"✏️ Editing <b>{_h(cat.get('label') or key)}</b>\n\n" + _WIZARD_STEPS[0][1],
            parse_mode="HTML", reply_markup=_wizard_cancel_kb())
    elif action == "del":
        await context.bot.send_message(
            query.from_user.id, f"⚠️ Delete pipeline <b>{_h(key)}</b>?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Delete + purge data", callback_data=f"cat:delpurge:{key}")],
                [InlineKeyboardButton("🗑 Delete (keep data)", callback_data=f"cat:delkeep:{key}")],
                [InlineKeyboardButton("✖️ Cancel", callback_data="cat:noop")],
            ]))
    elif action in ("delpurge", "delkeep"):
        await db.delete_category(key, purge_data=(action == "delpurge"))
        try:
            from bot1 import schedule_daily
            await schedule_daily(context.application.job_queue)
        except Exception as exc:
            log.warning("reschedule after delete failed: %s", exc)
        await context.bot.send_message(
            query.from_user.id,
            f"🗑 Pipeline '{key}' deleted"
            + (" and its data purged." if action == "delpurge" else " (data kept)."))
    elif action == "scan":
        await context.bot.send_message(query.from_user.id,
                                       f"🔍 Scanning DB channel for [{key}]…")
        await _run_scan(update, context, cat.get("db_channel_id"), key)


# ══════════════════════════════════════════════════════════════
#  SHORTENER GATE (global)
# ══════════════════════════════════════════════════════════════
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
            f"API key: {_mask_key(settings.get('shortener_api_key'))} (database)\n"
            f"Shorteners in rotation: {len(await db.list_shorteners())} "
            f"(manage with /shortenerapi)\n"
            f"Verify validity: {settings.get('verify_hours')} h\n"
            f"Token TTL: {settings.get('token_ttl_minutes')} min\n"
            f"Extra buttons: {len(settings.get('shortener_buttons') or [])}"
        )
        return
    enabled = args[0].lower() == "on"
    await db.update_settings({"shortener_enabled": enabled})
    await update.message.reply_text(
        f"✅ Shortener gate {'enabled' if enabled else 'disabled'}.")


def _mask_key(key):
    """Mask an API key for display: first4...last4 (handles short keys)."""
    key = (key or "").strip()
    if len(key) <= 8:
        return "•••" if key else "(not set)"
    return f"{key[:4]}...{key[-4:]}"


@admin_only
async def cmd_shortenerapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/shortenerapi — multi-shortener round-robin dashboard (v3.2).

      /shortenerapi                              -> list all shorteners
      /shortenerapi add <site> <api_base> <key>  -> add to the rotation
      /shortenerapi pause <site>                 -> skip it in rotation
      /shortenerapi resume <site>                -> back into rotation
      /shortenerapi remove <site>                -> delete it completely
    Paused shorteners stay listed but are NEVER used for user links.
    The old single-key syntax (bare key / url / clearkey) was removed."""
    import shortener as _sh
    args = [x for x in (context.args or []) if x]
    sub = args[0].lower() if args else ""

    if sub == "add":
        if len(args) < 4 or not args[2].startswith(("http://", "https://")):
            await update.message.reply_text(
                "Usage: /shortenerapi add <site> <api_base> <api_key>\n"
                "Example: /shortenerapi add gplink https://gplinks.in/api abc123key")
            return
        try:
            doc = await db.add_shortener(args[1], args[2], args[3])
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid site name — letters, numbers and _ only.")
            return
        if not doc:
            await update.message.reply_text(
                f"⚠️ '{args[1].lower()}' already exists. "
                "Remove it first or pick another name.")
            return
        short = await _sh.test_key(doc["api_base"], doc["api_key"])
        live = "✅ key verified live (shortener accepted it)." if short else (
            "⚠️ saved, but the live self-test did not return a link — check the key.")
        await update.message.reply_text(
            f"✅ Shortener '{doc['site']}' added to the rotation.\n"
            f"Base: {doc['api_base']}\nKey: {_mask_key(doc['api_key'])}\n"
            f"Live self-test: {live}")
        return

    if sub in ("pause", "resume"):
        if len(args) < 2:
            await update.message.reply_text(f"Usage: /shortenerapi {sub} <site>")
            return
        target = "paused" if sub == "pause" else "active"
        if not await db.set_shortener_status(args[1], target):
            await update.message.reply_text(
                f"❌ No shortener named '{args[1].lower()}'.")
            return
        icon = "⏸" if sub == "pause" else "🟢"
        extra = (" It stays in the list but is skipped in the rotation."
                 if sub == "pause" else " It is back in the rotation.")
        await update.message.reply_text(
            f"{icon} '{args[1].lower()}' is now {target}.{extra}")
        return

    if sub in ("remove", "delete"):
        if len(args) < 2:
            await update.message.reply_text("Usage: /shortenerapi remove <site>")
            return
        if not await db.remove_shortener(args[1]):
            await update.message.reply_text(
                f"❌ No shortener named '{args[1].lower()}'.")
            return
        await update.message.reply_text(
            f"🗑 '{args[1].lower()}' removed from the database and rotation.")
        return

    if sub and sub not in ("status", "list"):
        await update.message.reply_text(
            "Unknown action. Use:\n"
            "/shortenerapi — dashboard\n"
            "/shortenerapi add <site> <api_base> <api_key>\n"
            "/shortenerapi pause <site> · resume <site> · remove <site>")
        return

    rows = await db.list_shorteners()
    if not rows:
        await update.message.reply_text(
            "🔗 No shorteners configured yet.\n"
            "Add one: /shortenerapi add <site> <api_base> <api_key>\n"
            "Example: /shortenerapi add vplink https://vplink.in/api YOURKEY")
        return
    lines = ["🔗 Shortener rotation dashboard\n"]
    for i, r in enumerate(rows, 1):
        icon = "🟢" if r.get("status") == "active" else "⏸"
        state = "Active" if r.get("status") == "active" else "Paused"
        lines.append(f"{i}. {icon} {r['site']} — {state}")
        lines.append(f"   Base: {r['api_base']}")
        lines.append(f"   Key: {_mask_key(r.get('api_key'))}")
    act = sum(1 for r in rows if r.get("status") == "active")
    lines.append(f"\n{len(rows)} total · {act} active · {len(rows) - act} paused")
    lines.append("Users rotate per-person through the ACTIVE ones only.")
    lines.append("Manage: add · pause · resume · remove")
    await update.message.reply_text("\n".join(lines))


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


# ══════════════════════════════════════════════════════════════
#  GENERAL (global)
# ══════════════════════════════════════════════════════════════
@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply mode: FORWARD the replied-to message to every user — a real
    forward keeps the 'Forwarded from @channel' tag, quote blocks and media
    exactly as they are. Inline mode (/broadcast <text>): copy the command
    message itself so its own formatting (links, bold, quotes) is kept."""
    msg = update.message
    target = msg.reply_to_message
    if target is None and not msg.text.partition(" ")[2].strip():
        await update.message.reply_text(
            "Usage: reply to any message with /broadcast to forward it to "
            "all users (channel tag & quotes kept) — or /broadcast <text>.")
        return
    ids = await db.all_user_ids()
    sent = failed = 0
    await update.message.reply_text(f"📣 Broadcasting to {len(ids)} users…")
    for uid in ids:
        try:
            if target is not None:
                await context.bot.forward_message(
                    chat_id=uid, from_chat_id=msg.chat_id,
                    message_id=target.message_id)
            else:
                await context.bot.copy_message(
                    chat_id=uid, from_chat_id=msg.chat_id,
                    message_id=msg.message_id)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"✅ Done. Sent: {sent} · Failed: {failed}")


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global overview + a per-pipeline breakdown."""
    settings = await db.get_settings()
    users = await db.count_users()
    verified = await db.count_verified()
    banned = await db.count_banned()
    fsub_ch = await _channel_info(context.bot, settings.get("force_sub_channel_id"))

    lines = [
        "📊 <b>Bot Statistics</b>\n",
        f"👤 Users: {users}",
        f"✅ Verified (any pipeline): {verified}",
        f"🚫 Banned: {banned}\n",
        "⚙️ <b>Global</b>",
        f"Shortener gate: {'ON' if settings.get('shortener_enabled') else 'OFF'}",
        f"Force-Sub (default): {fsub_ch}",
        f"Verify validity: {settings.get('verify_hours')} h"
        f" · Token TTL: {settings.get('token_ttl_minutes')} min\n",
        "🗂 <b>Pipelines</b>",
    ]
    cats = await db.list_categories()
    if not cats:
        lines.append("  none yet — /addcategory")
    for cat in cats:
        k = cat["key"]
        total = await db.count_files(k)
        posted = await db.count_posted(k)
        pending = await db.count_pending(k)
        status = "⏸" if cat.get("schedule_paused") else ("✅" if cat.get("enabled", True) else "⛔")
        lines.append(
            f"  {status} <b>{_h(cat.get('label') or k)}</b> — "
            f"{posted} posted / {pending} queued / {total} total"
            f" · {cat.get('post_time') or '18:00'} IST")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                    disable_web_page_preview=True)


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
    """/setforcesub <channel_id|off> [category]
    Global default when no category is given; per-category override otherwise."""
    args = list(context.args or [])
    if not args:
        await update.message.reply_text("Usage: /setforcesub <channel_id | off> [category]")
        return
    # optional trailing category key -> per-category override
    target_key = None
    if len(args) > 1 and await db.get_category(args[-1].lower()):
        target_key = args.pop(-1).lower()
    val = args[0]
    if val.lower() == "off":
        if target_key:
            await db.update_category(target_key, {"force_sub_channel_id": None})
            await update.message.reply_text(
                f"✅ Force-sub override cleared for [{target_key}] (uses global default).")
        else:
            await db.update_settings({"force_sub_channel_id": None})
            await update.message.reply_text("✅ Force-subscribe disabled (global).")
        return
    channel_id = _parse_channel_id(val)
    if channel_id is None:
        await update.message.reply_text("Provide a numeric channel id or 'off'.")
        return
    if target_key:
        await db.update_category(target_key, {"force_sub_channel_id": channel_id})
        scope = f"[{target_key}]"
    else:
        await db.update_settings({"force_sub_channel_id": channel_id})
        scope = "global default"
    try:
        me = await context.bot.get_chat_member(channel_id, context.bot.id)
        note = f"Bot status in channel: {me.status}"
    except Exception as exc:
        note = f"⚠️ Could not verify bot membership there: {exc}"
    await update.message.reply_text(f"✅ Force-subscribe channel set ({scope}).\n{note}")


# ══════════════════════════════════════════════════════════════
#  PER-CATEGORY SCOPED COMMANDS
# ══════════════════════════════════════════════════════════════
@admin_only
async def cmd_protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/protect on|off [category] — per-category content protection."""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    if not args or args[0].lower() not in ("on", "off"):
        cat = await db.get_category(key) if key else {}
        await update.message.reply_text(
            "Usage: /protect on | off [category]\n"
            f"Protection for [{key}] is currently "
            f"{'ON' if (cat or {}).get('protect_content') else 'OFF'}.")
        return
    enabled = args[0].lower() == "on"
    await db.update_category(key, {"protect_content": enabled})
    await update.message.reply_text(
        f"✅ [{key}] Content protection {'enabled' if enabled else 'disabled'}.\n"
        + ("Users can no longer forward or save delivered files."
           if enabled else "Users can forward and save delivered files again."))


@admin_only
async def cmd_setautodelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setautodelete <time> [category] — per-category auto-delete timer."""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    if not args:
        await update.message.reply_text(
            "Usage: /setautodelete <time> [category]\n"
            "Examples: 30min · 2hour · 1day · 7day · never")
        return
    seconds = parse_duration(args[0])
    if seconds is None:
        await update.message.reply_text("Could not parse that duration. Try 30min / 2hour / 7day.")
        return
    await db.update_category(key, {"auto_delete_minutes": seconds // 60})
    await update.message.reply_text(
        f"✅ [{key}] Auto-delete set to {human_duration(seconds)}.")


async def _set_category_channel(update, context, key, field, cid, label):
    await db.update_category(key, {field: cid})
    note = ""
    try:
        me = await context.bot.get_chat_member(cid, context.bot.id)
        note = f" Bot status: {me.status}."
        if me.status not in ("administrator", "creator"):
            note += " ⚠️ Bot is NOT admin there."
    except Exception as exc:
        note = f" ⚠️ Could not verify channel: {exc}"
    await update.message.reply_text(f"✅ [{key}] {label} set to {cid}.{note}")


@admin_only
async def cmd_setdbchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setdbchannel <channel_id> [category]"""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    cid = _parse_channel_id(args[0]) if args else None
    if cid is None:
        await update.message.reply_text("Usage: /setdbchannel <channel_id> [category]")
        return
    await _set_category_channel(
        update, context, key, "db_channel_id", cid, "Database channel")
    await update.message.reply_text(
        f"✅ [{key}] Database channel set to {cid}. Run /rescandb {key} to index it.")


@admin_only
async def cmd_setpostchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpostchannel <channel_id> [category]"""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    cid = _parse_channel_id(args[0]) if args else None
    if cid is None:
        await update.message.reply_text("Usage: /setpostchannel <channel_id> [category]")
        return
    await _set_category_channel(update, context, key, "post_channel_id", cid,
                                "Posting channel")


@admin_only
async def cmd_setpostmainchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setpostmainchannel <channel_id|off> [category]"""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    if not args:
        await update.message.reply_text("Usage: /setpostmainchannel <channel_id|off> [category]")
        return
    if args[0].lower() == "off":
        await db.update_category(key, {"post_main_channel_id": None})
        await update.message.reply_text(f"✅ [{key}] Main-channel forwarding disabled.")
        return
    cid = _parse_channel_id(args[0])
    if cid is None:
        await update.message.reply_text("❌ channel_id must be a number.")
        return
    await _set_category_channel(update, context, key, "post_main_channel_id", cid,
                                "Main channel")


@admin_only
async def cmd_setposttag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setposttag <text|off> [category]"""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    if not args:
        await update.message.reply_text("Usage: /setposttag <text|off> [category]")
        return
    tag = " ".join(args)
    if tag.lower() == "off":
        await db.update_category(key, {"post_tag": None})
        await update.message.reply_text(f"✅ [{key}] Post tag cleared.")
        return
    await db.update_category(key, {"post_tag": tag})
    await update.message.reply_text(f"✅ [{key}] Post tag set to: {tag}")


@admin_only
async def cmd_setposttime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setposttime HH:MM [category] — daily post time, always IST."""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    t = _parse_hhmm(args[0]) if args else None
    if not t:
        await update.message.reply_text("Usage: /setposttime HH:MM [category]  (IST)")
        return
    await db.update_category(key, {"post_time": t})
    from bot1 import schedule_daily
    await schedule_daily(context.application.job_queue)
    await update.message.reply_text(f"✅ [{key}] Daily post time set to {t} IST.")


@admin_only
async def cmd_setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setschedule HH:MM [category] — set time (IST) and enable the pipeline."""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    t = _parse_hhmm(args[0]) if args else None
    if not t:
        await update.message.reply_text("Usage: /setschedule HH:MM [category]  (IST)")
        return
    await db.update_category(key, {
        "post_time": t, "schedule_enabled": True, "schedule_paused": False})
    from bot1 import schedule_daily
    await schedule_daily(context.application.job_queue)
    await update.message.reply_text(f"✅ [{key}] Daily post scheduled at {t} IST.")


@admin_only
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/schedule on|off [category]"""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    if not args or args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /schedule on|off [category]")
        return
    on = args[0].lower() == "on"
    await db.update_category(key, {"schedule_enabled": on, "schedule_paused": False})
    from bot1 import schedule_daily
    await schedule_daily(context.application.job_queue)
    await update.message.reply_text(
        f"✅ [{key}] Daily posting enabled." if on
        else f"⏸️ [{key}] Daily posting disabled.")


@admin_only
async def cmd_pauseposting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    await db.update_category(key, {"schedule_paused": True})
    await update.message.reply_text(f"⏸️ [{key}] Posting paused.")


@admin_only
async def cmd_resumeposting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    await db.update_category(key, {"schedule_paused": False})
    await update.message.reply_text(f"▶️ [{key}] Posting resumed.")


@admin_only
async def cmd_dripnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    await update.message.reply_text(f"⏳ [{key}] Posting the next queued item…")
    from bot1 import do_post
    item = await do_post(context.bot, category=key)
    if item:
        await update.message.reply_text(f"✅ [{key}] Posted item {item['file_id']}.")
    else:
        await update.message.reply_text(
            f"Nothing posted for [{key}] (queue empty or channels not configured).")


@admin_only
async def cmd_queueinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    sm = await db.queue_summary(key, 10)
    items = sm["items"]
    if not items:
        await update.message.reply_text(f"[{key}] Queue is empty.")
        return
    cat = await db.get_category(key)
    db_ch = (cat or {}).get("db_channel_id")
    base = f"https://t.me/c/{str(db_ch)[4:]}" if db_ch and str(db_ch).startswith("-100") else None
    lines = [f"📋 <b>Queue info [{_h(key)}]</b>",
             f"Position: #{sm['position']} — Remaining: {sm['remaining']}", ""]
    posted_total = await db.count_posted(key)
    for i, it in enumerate(items, 1):
        caption = (it.get("caption") or "").strip().split("\n")[0][:60]
        label = _h(caption or str(it.get("file_id") or it["db_message_id"]))
        if base:
            label = f'<a href="{base}/{it["db_message_id"]}">{label}</a>'
        lines.append(f"#{posted_total + i} · {label}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                    disable_web_page_preview=True)


@admin_only
async def cmd_queue_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/queue_reset N [category]"""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    try:
        n = int(args[0])
    except Exception:
        await update.message.reply_text("Usage: /queue_reset N [category]")
        return
    res = await db.queue_reset_to_position(n, key)
    if not res:
        await update.message.reply_text("❌ Invalid queue position.")
        return
    posted = await db.count_posted(key)
    pending = await db.count_pending(key)
    await update.message.reply_text(
        f"✅ [{key}] Queue reset. Cursor set to db_message_id={res['db_message_id']}\n"
        f"Posted={posted} - Queued={pending}")


# ── scanning (userbot) ────────────────────────────────────────
async def _run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id, category=None):
    """Run the userbot scan. Works from BOTH a command (update.message set) and
    a button callback (update.message is None) — always reply somewhere."""
    chat = getattr(update, "effective_chat", None)
    chat_id = chat.id if chat else update.effective_user.id

    async def _say(text):
        try:
            await context.bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            pass

    async def progress(n):
        await _say(f"…scanned {n} messages")
    try:
        result = await scanner.scan_channel(channel_id, category=category, progress=progress)
    except Exception as exc:
        log.exception("scan failed for category %s", category)
        await _say(f"❌ Scan failed: {exc}")
        return
    await _say(
        f"✅ [{category or 'legacy'}] Scan complete. Messages indexed: {result['scanned']} · "
        f"Items in queue: {result['items']}")


@admin_only
async def cmd_rescandb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    cat = await db.get_category(key) if key else None
    channel = (cat or {}).get("db_channel_id")
    if not channel:
        await update.message.reply_text(
            f"No database channel set for [{key}]. Set it via /editcategory {key} first.")
        return
    await update.message.reply_text(f"🔍 Re-indexing [{key}] database channel…")
    await _run_scan(update, context, channel, key)


@admin_only
async def cmd_scandb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/scandb <db_channel_id> [category] — point a pipeline at a DB channel + index it."""
    args = list(context.args or [])
    key, args = await _resolve_active(update, args)
    key = await _auto_key(update, key)
    if not await _require_active(update, key):
        return
    cid = _parse_channel_id(args[0]) if args else None
    if cid is None:
        await update.message.reply_text("Usage: /scandb <db_channel_id> [category]")
        return
    await db.update_category(key, {"db_channel_id": cid})
    await update.message.reply_text(f"🔍 [{key}] Scanning the database channel…")
    await _run_scan(update, context, cid, key)


@admin_only
async def cmd_renamecategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/renamecategory <key> <new label> — cosmetic rename only.

    The category KEY (used in file ids and Download links) never changes, so
    renaming the display label never breaks existing posts."""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /renamecategory <key> <new label>\n"
            "Only the display name changes — existing links keep working.")
        return
    key = args[0].lower()
    label = " ".join(args[1:]).strip()
    cat = await db.get_category(key)
    if not cat:
        await update.message.reply_text(f"❌ No pipeline named '{key}'.")
        return
    await db.update_category(key, {"label": label})
    await update.message.reply_text(
        f"✅ Pipeline '{key}' renamed to <b>{_h(label)}</b>.\n"
        "Existing Download buttons keep working (the link key is unchanged).",
        parse_mode="HTML")


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
    "setschedule": cmd_setschedule,
    "schedule": cmd_schedule,
    "pauseposting": cmd_pauseposting,
    "resumeposting": cmd_resumeposting,
    "queueinfo": cmd_queueinfo,
    "queue_reset": cmd_queue_reset,
    "setpostmainchannel": cmd_setpostmainchannel,
    "setposttag": cmd_setposttag,
    # multi-category management
    "addcategory": cmd_addcategory,
    "editcategory": cmd_editcategory,
    "delcategory": cmd_delcategory,
    "categories": cmd_categories,
    "use": cmd_use,
    "renamecategory": cmd_renamecategory,
}
