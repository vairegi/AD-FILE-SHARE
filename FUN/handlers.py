"""Bot 3 handlers — navigation, economy, delivery, admin, userbot setup.

All state-changing operations are idempotent / atomic; every failure path is
handled so a button tap never leaves the user stuck. Designed for fast
in-place card editing (edit_message_text) instead of sending new messages.
"""
import html as _html
import time
import uuid

from telegram import InlineKeyboardMarkup, Update
from ui import button as InlineKeyboardButton, edit_card, referral_links
import asyncio
from locks import user_lock
from telegram.ext import ContextTypes

import config
import db
import persona
import covers

# ── small helpers ───────────────────────────────────────────────
def esc(t) -> str:
    return _html.escape(str(t))


async def _user(update) -> dict:
    u = update.effective_user
    doc = await db.get_user(u.id)
    if doc is None:
        doc = await db.create_user(u.id, username=u.username,
                                   first_name=u.first_name)
    return doc


async def _cover_mode() -> str:
    s = await db.get_settings()
    return s.get("cover_mode") or config.COVER_MODE


def _cover_url(c) -> str:
    cover = c.get("cover") or {}
    if cover.get("url_status") == "ok" and cover.get("url"):
        return cover["url"]
    return ""


def _nav_row(back_cb="home", home_cb="home"):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data=back_cb),
        InlineKeyboardButton("🏠 Main Menu", callback_data=home_cb)]])


# ── /start (+ referral deep link ref_<id>) ───────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with user_lock(update.effective_user.id):
        await _start(update, context)


async def _start(update, context):
    u = update.effective_user
    doc = await _user(update)

    args = context.args or []
    if not doc.get("welcomed"):
        inviter = None
        if args and args[0].startswith("ref_"):
            try:
                candidate = int(args[0][4:])
                if candidate != u.id and await db.get_user(candidate):
                    inviter = candidate
            except ValueError:
                pass
        if inviter:
            await db.create_referral(inviter, u.id)
        referral = await db.get_referral(u.id)
        bonus = config.WELCOME_TOKENS + (config.REFERRAL_FRIEND_BONUS if referral else 0)
        # Balance and welcomed flag are updated in ONE atomic MongoDB write.
        awarded = await db.col("users").update_one(
            {"user_id": u.id, "welcomed": {"$ne": True}},
            {"$set": {"welcomed": True}, "$inc": {"tokens": bonus}})
        if awarded.modified_count:
            await db.col("transactions").insert_one({"user_id": u.id,
                "amount": bonus, "reason": "welcome_referral" if referral else "welcome",
                "meta": {"inviter": inviter}, "at": db.now_ts()})

    # Optional: user opened a specific collection via deep link open_<cid>
    if args and args[0].startswith("open_"):
        await _send_feed_card(update, context, args[0][5:])
        return

    doc = await db.get_user(u.id) or {}
    await update.message.reply_text(
        persona.home(int(doc.get("tokens", 0)), await _streak(u.id),
                     await db.count_unlocks(u.id), await db.count_bookmarks(u.id)),
        parse_mode="HTML", reply_markup=_home_kb())
    if await _check_membership(context.bot, u.id):
        await maybe_qualify_referral(context.bot, u.id, u.first_name or "your friend")


def _home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Start Watching",
                              callback_data="feed:all:0")],
        [InlineKeyboardButton("🗂 Categories", callback_data="cats"),
         InlineKeyboardButton("👛 Earn Tokens", callback_data="earn")],
        [InlineKeyboardButton("🌸 Profile", callback_data="profile"),
         InlineKeyboardButton("📖 Help Guide", callback_data="help")],
    ])


# ── central callback router ──────────────────────────────────────
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()  # fast ack — the UI spinner stops immediately
    data = q.data or ""
    if getattr(getattr(q.message, "chat", None), "type", "private") != "private":
        return
    try:
        if data == "noop":
            return
        elif data == "checkjoin":
            if await _check_membership(context.bot, q.from_user.id):
                await maybe_qualify_referral(context.bot, q.from_user.id, q.from_user.first_name)
                await _show_home(q)
            else:
                await _join_screen(q, context.bot, "home")
        elif data == "checkin":
            async def reply(text, **kwargs):
                await _edit(q, text, _nav_row("earn"))
            await do_checkin(q.from_user.id, reply)
        elif data.startswith("library:"):
            _, kind, index = data.split(":")
            await _show_library(q, kind, int(index))
        elif data == "home":
            await _show_home(q)
        elif data == "covermode":
            cur = await _cover_mode()
            new = "collage" if cur == "slideshow" else "slideshow"
            await db.update_settings({"cover_mode": new})
            await q.answer(f"🎀 Preview mode: {new}", show_alert=True)
        elif data == "cats":
            await _show_categories(q)
        elif data == "earn":
            await _show_earn(q)
        elif data == "profile":
            await _show_profile(q)
        elif data == "help":
            await _show_help(q)
        elif data == "ref":
            await _show_referral(q)
        elif data.startswith("feed:"):
            _, cat, idx = data.split(":")
            await _show_feed(q, cat, int(idx))
        elif data.startswith("open:"):
            await _open_collection(update, q, data[5:])
        elif data.startswith("unlock:"):
            await _serialized_unlock(update, context, q, data[7:], restream=False)
        elif data.startswith("restream:"):
            await _serialized_unlock(update, context, q, data[9:], restream=True)
        elif data.startswith("save:"):
            await _save(update, q, data[5:])
        elif data.startswith("task:"):
            await _task(update, q, data[5:])
        else:
            await q.answer("🎀 Hmm? Unknown button...", show_alert=True)
    except Exception as exc:  # never crash the dispatcher
        import logging
        logging.getLogger("fun.handlers").exception("button %s: %s", data, exc)
        try:
            await q.answer("🎀 S-Something went wrong, Senpai! (╥﹏╥)",
                           show_alert=True)
        except Exception:
            pass


async def _edit(q, text, kb):
    return await edit_card(q, text, kb)


async def _streak(uid):
    last = await db.get_last_checkin(uid)
    if last and last.get("date") in (db.ist_today(), db.ist_today(1)):
        return int(last.get("streak", 0))
    return 0


# ── screens ──────────────────────────────────────────────────────
async def _show_home(q):
    u = q.from_user
    doc = await db.get_user(u.id) or {}
    await _edit(q, persona.home(int(doc.get("tokens", 0)), await _streak(u.id),
                                await db.count_unlocks(u.id),
                                await db.count_bookmarks(u.id)), _home_kb())


async def _show_categories(q):
    doc = await db.get_user(q.from_user.id) or {}
    cats = await db.list_categories()
    rows = []
    for c in cats:
        n = await db.count_collections(c)
        rows.append([InlineKeyboardButton(f"{c} ({n})",
                                          callback_data=f"feed:{c}:0")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
    await _edit(q, persona.categories(int(doc.get("tokens", 0))),
                InlineKeyboardMarkup(rows))


async def _show_earn(q):
    doc = await db.get_user(q.from_user.id) or {}
    settings = await db.get_settings()
    tasks = settings.get("tasks") or []
    rows = [[InlineKeyboardButton(
        f"✅ {t['label']} (+{t.get('reward', config.DEFAULT_TASK_REWARD)} Tokens)",
        callback_data=f"task:{t['task_id']}")]
        for t in tasks if t.get("enabled", True) and t.get("secret")]
    rows.append([InlineKeyboardButton(
        f"🎁 Refer Friends (+{config.REFERRAL_INVITER_BONUS} Tokens)",
        callback_data="ref")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
    text = persona.earn_home(int(doc.get("tokens", 0)))
    if not any(t.get("enabled", True) and t.get("secret") for t in tasks):
        text += "\n\n" + persona.task_unavailable()
    rows.insert(0, [InlineKeyboardButton("🎁 Daily Check-In", callback_data="checkin")])
    await _edit(q, text, InlineKeyboardMarkup(rows))


async def _show_profile(q):
    uid = q.from_user.id
    doc = await db.get_user(uid) or {}
    txs = await db.list_transactions(uid, 500)
    earned = sum(t["amount"] for t in txs if t["amount"] > 0)
    spent = -sum(t["amount"] for t in txs if t["amount"] < 0)
    refs = await db.referral_stats(uid)
    await _edit(q, persona.profile(
        uid, int(doc.get("tokens", 0)), await _streak(uid), await db.count_unlocks(uid),
        await db.count_bookmarks(uid), earned, spent,
        await db.col("task_runs").count_documents({"user_id": uid}), refs["friends"]),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔓 Unlocked Posts", callback_data="library:unlocks:0"),
             InlineKeyboardButton("⭐ Saved Posts", callback_data="library:bookmarks:0")],
            [InlineKeyboardButton("👛 Earn Tokens", callback_data="earn"),
             InlineKeyboardButton("🎁 Refer Friends", callback_data="ref")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))


async def _show_help(q):
    await _edit(q, persona.help_card(), InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Start Watching", callback_data="feed:all:0")],
        [InlineKeyboardButton("🌸 Profile", callback_data="profile"),
         InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))


async def _show_referral(q):
    uid = q.from_user.id
    stats = await db.referral_stats(uid)
    try:
        bot = q.get_bot() if hasattr(q, "get_bot") else None
        link, share = await referral_links(bot, uid)
    except RuntimeError:
        await _edit(q, persona.head("🎀 Invitation temporarily unavailable") +
            "The bot username could not be resolved. Owner: check /botstatus.", _nav_row("earn"))
        return
    await _edit(q, persona.referral(link, stats["friends"], stats["earned"]),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Share on Telegram", url=share, style="success")],
            [InlineKeyboardButton("🔙 Back", callback_data="earn"),
             InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))


async def _show_library(q, kind, index):
    if kind not in ("unlocks", "bookmarks"):
        return
    docs = await db.col(kind).find({"user_id": q.from_user.id}).sort("at", -1).to_list(None)
    collections = []
    for d in docs:
        c = await db.get_collection(d["collection_id"])
        if c and c.get("status") == "active":
            collections.append(c)
    if not collections:
        await _edit(q, persona.feed_empty(), _nav_row("profile"))
        return
    index = max(0, min(index, len(collections) - 1))
    c = collections[index]
    uid = q.from_user.id
    unlocked = await db.is_unlocked(uid, c["collection_id"])
    keyboard = _feed_kb(c["collection_id"], "all", 0, 1, unlocked)
    rows = list(keyboard.inline_keyboard)
    rows.insert(0, [InlineKeyboardButton("⬅️ Previous", callback_data=f"library:{kind}:{index-1}" if index else "noop"),
                   InlineKeyboardButton("Next ➡️", callback_data=f"library:{kind}:{index+1}" if index+1<len(collections) else "noop")])
    await _edit(q, await _feed_text(uid, c), InlineKeyboardMarkup(rows))


# ── feed (collage/slideshow browser) ─────────────────────────────
async def _show_feed(q, category, idx):
    cat = None if category in ("all", "") else category
    total = await db.count_collections(cat)
    if total == 0:
        await _edit(q, persona.feed_empty(), _nav_row("cats"))
        return
    idx = max(0, min(idx, total - 1))
    cols = await db.list_collections(cat, skip=idx, limit=1)
    if not cols:
        await _edit(q, persona.feed_empty(), _nav_row("cats"))
        return
    c = cols[0]
    await _render_feed_card(q, c, category, idx, total, context=q.get_bot() if hasattr(q, "get_bot") else getattr(q, "bot", None))


async def _send_feed_card(update, context, collection_id):
    c = await db.get_collection(collection_id)
    if not c:
        await update.message.reply_text(persona.feed_empty(),
                                        reply_markup=_home_kb(), parse_mode="HTML")
        return
    cat = c.get("category") or "all"
    total = await db.count_collections(cat if cat != "all" else None)
    q = type("Q", (), {"from_user": update.effective_user,
                       "message": update.message,
                       "edit_message_text": lambda *a, **k: None,
                       "answer": lambda *a, **k: None})()
    text = await _feed_text(update.effective_user.id, c)
    kb = _feed_kb(c["collection_id"], cat, 0, total,
                  await db.is_unlocked(update.effective_user.id, c["collection_id"]))
    items = await covers.preview_items(c)
    if items:
        import rich_cards
        await rich_cards.show_cover_card(context.bot, update.effective_user.id, text, kb,
                                         items, await _cover_mode(), collection_id=c["collection_id"])
        return
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def _render_feed_card(q, c, category, idx, total, context=None):
    uid = q.from_user.id
    unlocked = await db.is_unlocked(uid, c["collection_id"])
    await db.bump_collection(c["collection_id"], "views")
    text = await _feed_text(uid, c)
    kb = _feed_kb(c["collection_id"], category, idx, total, unlocked)
    bot = context or (q.get_bot() if hasattr(q, "get_bot") else getattr(q, "bot", None))
    items = await covers.preview_items(c)
    if items and bot:
        import rich_cards
        mode = await _cover_mode()
        chat_id = q.message.chat.id if getattr(q.message, "chat", None) else uid
        mid = getattr(q.message, "message_id", None)
        await rich_cards.show_cover_card(bot, chat_id, text, kb, items, mode,
            edit_message_id=mid, collection_id=c["collection_id"])
        return
    await _edit(q, text, kb)


async def _feed_text(uid, c):
    price = config.COLLECTION_PRICE if c.get("price") is None else int(c["price"])
    return persona.feed_caption(
        c.get("title") or "Drop", c.get("file_count", 0),
        c.get("views", 0), c.get("unlocks", 0), price,
        int((await db.get_user(uid) or {}).get("tokens", 0)),
        await db.is_unlocked(uid, c["collection_id"]))


def _feed_kb(cid, category, idx, total, unlocked):
    rows = [[
        InlineKeyboardButton("⬅️ Previous",
                             callback_data=f"feed:{category}:{idx - 1}" if idx > 0 else "noop"),
        InlineKeyboardButton("🔄 Re-Stream" if unlocked else "🔓 Unlock",
                             callback_data=(f"restream:{cid}" if unlocked
                                            else f"unlock:{cid}")),
        InlineKeyboardButton("Next ➡️",
                             callback_data=f"feed:{category}:{idx + 1}" if idx < total - 1 else "noop"),
    ], [
        InlineKeyboardButton("🗂 Categories", callback_data="cats"),
        InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]
    return InlineKeyboardMarkup(rows)


# ── unlock / re-stream / delivery ────────────────────────────────
async def _check_membership(bot, user_id) -> bool:
    ids = await db.required_channels()
    if not ids:
        return True
    for cid in ids:
        try:
            m = await bot.get_chat_member(cid, user_id)
            if m.status in ("left", "kicked") or (m.status == "restricted" and not getattr(m, "is_member", False)):
                return False
        except Exception:
            return False
    return True


async def _open_collection(update, q, cid):
    # "Save" from a delivered card opens the vault entry; simple behavior.
    added = await db.toggle_bookmark(q.from_user.id, cid)
    await q.answer("⭐ Saved to your Bookmarks!" if added
                   else "🗑 Removed from Bookmarks.", show_alert=True)


async def _save(update, q, cid):
    added = await db.toggle_bookmark(q.from_user.id, cid)
    await q.answer("⭐ Saved to your Bookmarks!" if added
                   else "🗑 Removed from Bookmarks.", show_alert=True)


async def _join_screen(q, bot, retry):
    ids = await db.required_channels()
    rows = []
    settings = await db.get_settings()
    for cid in ids:
        url = (settings.get("required_channel_links") or {}).get(str(cid))
        if not url:
            try:
                chat = await bot.get_chat(cid)
                url = f"https://t.me/{chat.username}" if getattr(chat, "username", None) else getattr(chat, "invite_link", None)
            except Exception:
                pass
        if url:
            rows.append([InlineKeyboardButton("📢 Join Channel", url=url, style="success")])
    rows.append([InlineKeyboardButton("✅ I've Joined", callback_data=retry if retry != "home" else "checkjoin")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
    await _edit(q, persona.join_required(ids), InlineKeyboardMarkup(rows))


async def _serialized_unlock(update, context, q, cid, restream):
    async with user_lock(q.from_user.id):
        await _unlock(update, context, q, cid, restream)


async def _unlock(update, context, q, cid, restream):
    uid = q.from_user.id
    c = await db.get_collection(cid)
    if not c:
        await q.answer("🎀 This drop isn't available anymore...", show_alert=True)
        return
    price = config.COLLECTION_PRICE if c.get("price") is None else int(c["price"])

    if not c.get("files"):
        await _edit(q, persona.head("🎀 This drop isn't ready yet, Senpai~!") +
                    "No delivery files are indexed. You have not been charged.", _nav_row("home"))
        return
    if not await _check_membership(context.bot, uid):
        await _join_screen(q, context.bot, f"unlock:{cid}")
        return
    await maybe_qualify_referral(context.bot, uid, q.from_user.first_name or "your friend")
    already = await db.is_unlocked(uid, cid)
    if restream and not already:
        restream = False  # stale/free-replay callbacks must still pay
    if not already:
        doc = await db.get_user(uid) or {}
        if int(doc.get("tokens", 0)) < price:
            await _edit(q, persona.insufficient(price,
                                                int(doc.get("tokens", 0))),
                        InlineKeyboardMarkup([
                            [InlineKeyboardButton("👛 Earn Tokens",
                                                  callback_data="earn")],
                            [InlineKeyboardButton("🏠 Main Menu",
                                                  callback_data="home")]]))
            return
        # Atomic spend THEN record unlock (unlock insert is unique => no
        # double-charge even if tapped twice).
        if not await db.try_spend_tokens(uid, price, "unlock",
                                         {"collection_id": cid}):
            doc = await db.get_user(uid) or {}
            await _edit(q, persona.insufficient(price,
                                                int(doc.get("tokens", 0))),
                        _nav_row("earn"))
            return
        created = await db.insert_unlock(uid, cid)
        if not created:
            # race: another tap already unlocked -> refund this spend
            await db.add_tokens(uid, price, "unlock_refund_race",
                                {"collection_id": cid})
            price = 0
    else:
        price = 0

    await _deliver(update, context, q, c, price)


async def _deliver(update, context, q, c, price_paid):
    uid = q.from_user.id
    files = c.get("files") or []
    delivery_id = f"d{uuid.uuid4().hex[:12]}"
    sent_ids, failed = [], []
    await db.create_delivery({"delivery_id": delivery_id, "user_id": uid,
        "collection_id": c["collection_id"], "message_ids": [], "failed_files": [],
        "auto_delete_at": db.now_ts() + config.AUTO_DELETE_AFTER_SECONDS,
        "delete_status": "none", "status": "sending", "at": db.now_ts()})
    try:
        from services import deliver_collection
        sent_ids, failed = await deliver_collection(
            context.bot, uid, c, files, delivery_id=delivery_id)
    except Exception as exc:
        import logging
        logging.getLogger("fun.delivery").exception("deliver: %s", exc)
        failed = [{**f, "uncertain": True, "reason": "unexpected_delivery_failure"} for f in files]


    doc = await db.get_user(uid) or {}
    if not sent_ids and files and not any(f.get("uncertain") for f in failed):
        # zero-delivery -> full refund
        if price_paid:
            await db.add_tokens(uid, price_paid, "delivery_refund",
                                {"collection_id": c["collection_id"]})
        if price_paid:
            await db.col("unlocks").delete_one({"user_id": uid, "collection_id": c["collection_id"]})
        doc = await db.get_user(uid) or {}
        await _edit(q, persona.delivery_failed_refund(
            price_paid, int(doc.get("tokens", 0))), _nav_row("home"))
        return

    if price_paid and sent_ids:
        await db.bump_collection(c["collection_id"], "unlocks")
    await db.update_delivery(delivery_id, {"failed_files": failed,
        "status": "partial_or_uncertain" if failed else "complete",
        "message_ids": sent_ids, "delete_status": "pending" if sent_ids else "none"})
    completion = persona.stream_complete(c.get("title") or "Drop", len(sent_ids), price_paid)
    if failed:
        completion = (persona.head("⚠️ Delivery incomplete, Senpai") +
            f"Confirmed delivered messages: <code>{len(sent_ids)}</code>. "
            "Some files need owner review; do not repeatedly retry an uncertain delivery.")
    await _edit(q, completion,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("💾 Save",
                                  callback_data=f"save:{c['collection_id']}"),
             InlineKeyboardButton("🔄 Re-Stream",
                                  callback_data=f"restream:{c['collection_id']}")],
            [InlineKeyboardButton("➡️ Next Post",
                                  callback_data=f"feed:{c.get('category') or 'all'}:0"),
             InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))


# ── sponsor tasks ────────────────────────────────────────────────
async def _task(update, q, task_id):
    uid = q.from_user.id
    settings = await db.get_settings()
    task = next((t for t in (settings.get("tasks") or [])
                 if t.get("task_id") == task_id and t.get("enabled", True)),
                None)
    if not task or not task.get("secret"):
        await q.answer("🎀 Task not available...", show_alert=True)
        return
    last = await db.get_last_task_run(uid, task_id)
    remaining = config.TASK_COOLDOWN_SECONDS - \
        (db.now_ts() - (last or {}).get("at", 0)) if last else 0
    if last and remaining > 0:
        h, m = int(remaining // 3600), int((remaining % 3600) // 60)
        await _edit(q, persona.task_cooldown(h, m), _nav_row("earn"))
        return
    # Verification: provider callback (see providers.py) hits /task/complete
    # with a signed token. The button only opens the sponsor link; crediting
    # happens server-side on the verified callback — NOT on button tap.
    await _edit(q, persona.head(f"🎀 Sponsor Task: {task['label']}") +
        persona.stat("🎁", "Reward", f"+{task.get('reward', config.DEFAULT_TASK_REWARD)} tokens") +
        "\n" + persona.cute("🌸 Finish the configured sponsor verification flow, Senpai~! "
            "Only a verified completion can award your tokens."),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open Sponsor Task", url=task.get("url"), style="success")],
                              [InlineKeyboardButton("🔙 Back", callback_data="earn"),
                               InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))


async def credit_task(user_id: int, task_id: str) -> str:
    """Called only by a verified provider callback, serialized per wallet."""
    async with user_lock(user_id):
        return await _credit_task(user_id, task_id)


async def _credit_task(user_id, task_id):
    settings = await db.get_settings()
    task = next((t for t in (settings.get("tasks") or [])
                 if t.get("task_id") == task_id), None)
    if not task or not task.get("enabled", True):
        return "unknown_task"
    last = await db.get_last_task_run(user_id, task_id)
    if last and db.now_ts() - last.get("at", 0) < config.TASK_COOLDOWN_SECONDS:
        return "cooldown"
    reward = int(task.get("reward", config.DEFAULT_TASK_REWARD))
    await db.record_task_run(user_id, task_id, reward)
    await db.add_tokens(user_id, reward, "task", {"task_id": task_id})
    return "ok"


# ── /checkin ─────────────────────────────────────────────────────
async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_checkin(update.effective_user.id, update.message.reply_text)


async def do_checkin(user_id: int, reply):
    async with user_lock(user_id):
        await _do_checkin(user_id, reply)


async def _do_checkin(user_id, reply):
    today = db.ist_today(0)
    yesterday = db.ist_today(1)
    user = await db.get_user(user_id)
    if not user:
        user = await db.create_user(user_id)
    last = await db.get_last_checkin(user_id)
    if user.get("checkin_date") and (not last or user["checkin_date"] > last.get("date", "")):
        last = {"date": user["checkin_date"], "streak": user.get("streak", 0)}
    if user.get("checkin_date") == today or (last and last.get("date") == today):
        from datetime import datetime, timedelta
        now_ist = datetime.now(db.IST)
        nxt = (now_ist + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        rem = (nxt - now_ist).total_seconds()
        h, m = int(rem // 3600), int((rem % 3600) // 60)
        await reply(persona.checkin_already(h, m), parse_mode="HTML")
        return
    streak = (last.get("streak", 0) + 1
              if last and last.get("date") == yesterday else 1)
    bonus = config.STREAK_BONUS if streak % config.STREAK_PERIOD_DAYS == 0 else 0
    reward = config.DAILY_CHECKIN_REWARD + bonus
    granted = await db.col("users").update_one(
        {"user_id": user_id, "checkin_date": {"$ne": today}},
        {"$set": {"checkin_date": today, "streak": streak}, "$inc": {"tokens": reward}})
    if not granted.modified_count:
        return
    await db.record_checkin(user_id, today, streak, reward)
    await db.col("transactions").insert_one({"user_id": user_id, "amount": reward,
        "reason": "checkin", "meta": {"streak": streak}, "at": db.now_ts()})
    balance = int((await db.get_user(user_id) or {}).get("tokens", 0))
    next_in = config.STREAK_PERIOD_DAYS - (streak % config.STREAK_PERIOD_DAYS)
    await reply(persona.checkin_done(config.DAILY_CHECKIN_REWARD, bonus,
                                     balance, streak, next_in), parse_mode="HTML")


# ── referral qualification (called after membership check passes) ─
async def maybe_qualify_referral(bot, friend_id: int, friend_name: str):
    doc = await db.get_referral(friend_id)
    if not doc:
        return
    inviter = int(doc["inviter_id"])
    if inviter == friend_id:
        return
    # Legacy credits are recognized so an upgrade doesn't re-pay old referrals.
    legacy = await db.col("transactions").find_one({"user_id": inviter,
        "reason": "referral", "meta.friend_id": friend_id})
    if legacy:
        await db.col("users").update_one({"user_id": inviter}, {"$addToSet": {"paid_referrals": friend_id}})
    credited = await db.col("users").update_one(
        {"user_id": inviter, "paid_referrals": {"$ne": friend_id}},
        {"$inc": {"tokens": config.REFERRAL_INVITER_BONUS},
         "$addToSet": {"paid_referrals": friend_id}})
    if credited.modified_count:
        await db.col("transactions").insert_one({"user_id": inviter,
            "amount": config.REFERRAL_INVITER_BONUS, "reason": "referral",
            "meta": {"friend_id": friend_id}, "at": db.now_ts()})
    await db.qualify_referral(friend_id)
    # Notification retries never repeat the atomic balance credit.
    doc = await db.get_referral(friend_id)
    if not doc.get("notified"):
        balance = int((await db.get_user(inviter) or {}).get("tokens", 0))
        try:
            await bot.send_message(inviter, persona.referral_notify(friend_name,
                config.REFERRAL_INVITER_BONUS, balance), parse_mode="HTML")
            await db.mark_referral_notified(friend_id)
        except Exception:
            pass


# ── admin ────────────────────────────────────────────────────────
def _is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(persona.help_card(), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Start Watching", callback_data="feed:all:0")],
            [InlineKeyboardButton("🌸 Profile", callback_data="profile"),
             InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))


async def cmd_covermode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    arg = (context.args or [""])[0].lower()
    if arg not in ("slideshow", "collage"):
        cur = await _cover_mode()
        await update.message.reply_text(
            f"🎀 Preview mode: {cur}\n\n/covermode slideshow — swipeable photos\n"
            "/covermode collage — combined grid preview")
        return
    await db.update_settings({"cover_mode": arg})
    await update.message.reply_text(f"✅ Preview mode set to {arg}.")


async def cmd_retry_covers(update, context):
    if not _is_admin(update.effective_user.id):
        return
    import runtime, cover_jobs
    if not runtime.userbot:
        await update.message.reply_text("🎀 Connect the userbot first: /setup_userbot")
        return
    if cover_jobs.busy():
        await update.message.reply_text("⏳ Cover preparation is already running. Use /cover_status for progress.")
        return
    count = await covers.needs_preparation_count()
    if not count:
        await update.message.reply_text("✅ All collections already have cached hosted cover URLs. Use /cover_status for details.")
        return
    status_message = await update.message.reply_text(
        f"🎀 Found {count} covers needing preparation (pending, missing or failed). Starting in the background…")
    cover_jobs.start(runtime.userbot, status_message, update.message.reply_text)


async def cmd_cover_status(update, context):
    if not _is_admin(update.effective_user.id):
        return
    import cover_jobs
    stats = await cover_jobs.statistics()
    await update.message.reply_text(
        "🎀 Cover status\n" + "\n".join(f"{key}: {value}" for key,value in stats.items()))


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "🎀 Owner Panel:\n"
        "• /addchannel <channel_id> <category>\n"
        "• /removechannel <channel_id>\n"
        "• /channels — list mappings\n"
        "• /reqchannel <channel_id|off> — required join\n"
        "• /addtask <task_id> <label> <url> [reward]\n"
        "• /setprice <collection_id> <tokens>\n"
        "• /scan <channel_id> — run scanner now\n"
        "• /covermode slideshow|collage — cover preview style\n"
        "• /retry_covers — prepare pending/missing/failed covers\n"
        "• /cover_status — hosted/local preview counts and progress\n"
        "• /pending — inspect orphan files\n"
        "• /collections — latest indexed IDs and counts\n"
        "• /setup_userbot — API ID → API hash → session in chat\n"
        "• /userbot_status — configuration and connection\n"
        "• /remove_userbot — disconnect and remove saved session\n"
        "• /cancel — cancel userbot setup\n"
        "• /botstatus — detected username\n"
        "• /stats — counts")


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    try:
        cid, cat = int(context.args[0]), context.args[1]
        if cid >= 0 or not cat or len(f"feed:{cat}:99999".encode()) > 64 or ":" in cat:
            raise ValueError("Invalid channel/category")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /addchannel <id> <category>")
        return
    await db.upsert_channel_mapping(int(cid), cat)
    await update.message.reply_text(f"✅ Channel {cid} → category {cat}\nNext: /scan {cid}\nThe userbot account must be a member; add Bot 3 to the source channel so it can copy files.")


async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    try:
        cid = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /removechannel <id>")
        return
    await db.remove_channel_mapping(cid)
    await update.message.reply_text(f"✅ Channel {cid} mapping removed.")


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    maps = await db.list_channel_mappings()
    text = "\n".join(f"• {m['channel_id']} → {m['category']}" for m in maps) \
        or "No channel mappings yet."
    await update.message.reply_text(text)


async def cmd_reqchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    arg = (context.args or [""])[0]
    if arg.lower() == "off":
        await db.update_settings({"required_channel_ids": []})
        await update.message.reply_text("✅ Required-join disabled.")
        return
    try:
        cid = int(arg)
    except ValueError:
        await update.message.reply_text("Usage: /reqchannel <id|off>")
        return
    current = await db.required_channels()
    if cid not in current:
        current.append(cid)
    settings = await db.get_settings()
    links = settings.get("required_channel_links") or {}
    if len(context.args) > 1 and context.args[1].startswith("https://t.me/"):
        links[str(cid)] = context.args[1]
    await db.update_settings({"required_channel_ids": current, "required_channel_links": links})
    await update.message.reply_text(f"✅ Users must join {cid} before unlock.")


async def cmd_addtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    try:
        tid, label, url = context.args[0], context.args[1], context.args[2]
        reward = int(context.args[3]) if len(context.args) > 3 \
            else config.DEFAULT_TASK_REWARD
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: /addtask <task_id> <label> <url> [reward]")
        return
    s = await db.get_settings()
    tasks = [t for t in (s.get("tasks") or []) if t.get("task_id") != tid]
    tasks.append({"task_id": tid, "label": label, "url": url,
                  "reward": reward, "enabled": True})
    await db.update_settings({"tasks": tasks})
    await update.message.reply_text(f"✅ Task {tid} saved (+{reward}).")


async def cmd_setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    try:
        cid, price = context.args[0], int(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /setprice <collection_id> <n>")
        return
    if price < 0:
        await update.message.reply_text("Price must be zero or greater.")
        return
    if not await db.get_collection(cid):
        await update.message.reply_text("Collection not found.")
        return
    await db.col("collections").update_one(
        {"collection_id": cid}, {"$set": {"price": price}})
    await update.message.reply_text(f"✅ {cid} price → {price} tokens.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    n_users = await db.col("users").count_documents({})
    n_cols = await db.count_collections()
    n_unlocks = await db.col("unlocks").count_documents({})
    await update.message.reply_text(
        f"🎀 Stats:\n👥 Users: {n_users}\n🗂 Collections: {n_cols}\n"
        f"🔓 Unlocks: {n_unlocks}")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    try:
        cid = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /scan -1001234567890 [full]")
        return
    if not await db.category_for_channel(cid):
        await update.message.reply_text("🎀 Map it first: /addchannel -1001234567890 category")
        return
    import runtime
    if not runtime.userbot:
        await update.message.reply_text("🎀 Userbot isn't connected. Run /setup_userbot then /userbot_status.")
        return
    if runtime.scan_busy(cid):
        await update.message.reply_text("⏳ This channel is already being scanned.")
        return
    runtime.schedule_scan(cid, update.message.reply_text, full="full" in (context.args or []))
    await update.message.reply_text("🎀 Scan started in the background, Senpai~! I'll report the result here.")


async def cmd_setup_userbot(update, context):
    import chat_setup
    await chat_setup.begin(update, context)


async def cmd_userbot_status(update, context):
    import chat_setup
    await chat_setup.status(update, context)


async def cmd_remove_userbot(update, context):
    import chat_setup
    await chat_setup.remove(update, context)


async def cmd_cancel(update, context):
    import chat_setup
    await chat_setup.cancel(update, context)


async def cmd_botstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    from ui import bot_username
    name = await bot_username(context.bot)
    await update.message.reply_text(f"🎀 Bot username: @{name}\nReferral links use this identity automatically.\nUse /admin for setup instructions.")


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import chat_setup
    if await chat_setup.consume(update, context):
        return
    # Outside an explicit owner-private setup, never capture arbitrary input.
    if (update.message.text or "").strip().lower() in ("/check-in", "/check in"):
        await cmd_checkin(update, context)


# pending-review placeholder (owner corrects scanner groupings)
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    s = await db.get_settings()
    pend = list(s.get("pending_groups") or [])
    pending = await db.col("pending_files").find({}).limit(20).to_list(None)
    pend.extend({"desc": f"Channel {d['channel_id']} message {d['file']['message_id']}: {d['reason']}"} for d in pending)
    if not pend:
        await update.message.reply_text("🎀 No ambiguous groups — all clean!")
        return
    await update.message.reply_text(
        f"🎀 {len(pend)} group(s) need review:\n" +
        "\n".join(f"• {p.get('desc', '?')}" for p in pend[:20]))


async def cmd_collections(update, context):
    if not _is_admin(update.effective_user.id):
        return
    docs = await db.col("collections").find({}).sort("posted_at", -1).limit(20).to_list(None)
    text = "🎀 Indexed collections (latest 20)\n" + "\n".join(
        f"{d['collection_id']} | {d.get('category')} | {d.get('file_count', 0)} delivery files" for d in docs)
    await update.message.reply_text(text if docs else "No collections indexed yet. Use /addchannel then /scan.", parse_mode=None)
