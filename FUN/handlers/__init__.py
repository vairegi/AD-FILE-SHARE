"""Bot 3 handlers — navigation, economy, delivery, admin, userbot setup.

All state-changing operations are idempotent / atomic; every failure path is
handled so a button tap never leaves the user stuck. Designed for fast
in-place card editing (edit_message_text) instead of sending new messages.
"""
import html as _html
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import db
import persona

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


def _nav_row(back_cb="home", home_cb="home"):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back", callback_data=back_cb),
        InlineKeyboardButton("🏠 Main Menu", callback_data=home_cb)]])


# ── /start (+ referral deep link ref_<id>) ───────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    doc = await _user(update)

    args = context.args or []
    if not doc.get("welcomed"):
        # First contact — welcome tokens (+ referral bonus if deep-linked)
        bonus = config.WELCOME_TOKENS
        inviter = None
        if args and args[0].startswith("ref_"):
            try:
                inviter = int(args[0][4:])
            except ValueError:
                inviter = None
            if inviter and inviter != u.id:
                created = await db.create_referral(inviter, u.id)
                if created:
                    bonus += config.REFERRAL_FRIEND_BONUS
        await db.add_tokens(u.id, bonus,
                            "welcome_referral" if inviter else "welcome",
                            {"inviter": inviter})
        await db.col("users").update_one(
            {"user_id": u.id}, {"$set": {"welcomed": True}})

    # Optional: user opened a specific collection via deep link open_<cid>
    if args and args[0].startswith("open_"):
        await _send_feed_card(update, context, args[0][5:])
        return

    await update.message.reply_text(
        persona.home(int(doc.get("tokens", 0)) or 0, 0,
                     await db.count_unlocks(u.id),
                     await db.count_bookmarks(u.id)),
        reply_markup=_home_kb())


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
    try:
        if data == "home":
            await _show_home(q)
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
            await _unlock(update, context, q, data[7:], restream=False)
        elif data.startswith("restream:"):
            await _unlock(update, context, q, data[9:], restream=True)
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
    try:
        await q.edit_message_text(text, reply_markup=kb,
                                  parse_mode="HTML")
    except Exception:
        # message content unchanged / message deleted -> fall back to new msg
        await q.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


# ── screens ──────────────────────────────────────────────────────
async def _show_home(q):
    u = q.from_user
    doc = await db.get_user(u.id) or {}
    await _edit(q, persona.home(int(doc.get("tokens", 0)), 0,
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
        for t in tasks if t.get("enabled", True)]
    rows.append([InlineKeyboardButton(
        f"🎁 Refer Friends (+{config.REFERRAL_INVITER_BONUS} Tokens)",
        callback_data="ref")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
    text = persona.earn_home(int(doc.get("tokens", 0))) if rows[:-1] else \
        persona.task_unavailable()
    await _edit(q, text, InlineKeyboardMarkup(rows))


async def _show_profile(q):
    uid = q.from_user.id
    doc = await db.get_user(uid) or {}
    txs = await db.list_transactions(uid, 500)
    earned = sum(t["amount"] for t in txs if t["amount"] > 0)
    spent = -sum(t["amount"] for t in txs if t["amount"] < 0)
    refs = await db.referral_stats(uid)
    await _edit(q, persona.profile(
        uid, int(doc.get("tokens", 0)), 0, await db.count_unlocks(uid),
        await db.count_bookmarks(uid), earned, spent, 0, refs["friends"]),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔓 Unlocked Posts", callback_data="feed:all:0"),
             InlineKeyboardButton("⭐ Saved Posts", callback_data="feed:all:0")],
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
    link = f"https://t.me/{config.BOT_USERNAME}?start=ref_{uid}"
    await _edit(q, persona.referral(link, stats["friends"], stats["earned"]),
                _nav_row("earn"))


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
    await _render_feed_card(q, c, category, idx, total)


async def _send_feed_card(update, context, collection_id):
    c = await db.get_collection(collection_id)
    if not c:
        await update.message.reply_text(persona.feed_empty(),
                                        reply_markup=_home_kb())
        return
    cat = c.get("category") or "all"
    total = await db.count_collections(cat if cat != "all" else None)
    q = type("Q", (), {"from_user": update.effective_user,
                       "message": update.message,
                       "edit_message_text": lambda *a, **k: None,
                       "answer": lambda *a, **k: None})()
    await update.message.reply_text(
        _feed_text(update.effective_user.id, c),
        reply_markup=_feed_kb(c["collection_id"], cat, 0, total,
                              await db.is_unlocked(update.effective_user.id,
                                                   c["collection_id"])))


async def _render_feed_card(q, c, category, idx, total):
    uid = q.from_user.id
    unlocked = await db.is_unlocked(uid, c["collection_id"])
    await db.bump_collection(c["collection_id"], "views")
    await _edit(q, _feed_text(uid, c),
                _feed_kb(c["collection_id"], category, idx, total, unlocked))


def _feed_text(uid, c):
    price = c.get("price") or config.COLLECTION_PRICE
    return persona.feed_caption(
        esc(c.get("title") or "Drop"), c.get("file_count", 0),
        c.get("views", 0), c.get("unlocks", 0), price, 0, False)


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
            if m.status in ("left", "kicked"):
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


async def _unlock(update, context, q, cid, restream):
    uid = q.from_user.id
    c = await db.get_collection(cid)
    if not c:
        await q.answer("🎀 This drop isn't available anymore...", show_alert=True)
        return
    price = c.get("price") or config.COLLECTION_PRICE

    already = await db.is_unlocked(uid, cid)
    if not already:
        if not await _check_membership(context.bot, uid):
            ids = await db.required_channels()
            await _edit(q, persona.join_required(ids),
                        _nav_row("home"))
            return
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
    else:
        price = 0

    await _deliver(update, context, q, c, price)


async def _deliver(update, context, q, c, price_paid):
    uid = q.from_user.id
    files = c.get("files") or []
    delivery_id = f"d{uuid.uuid4().hex[:12]}"
    sent_ids, failed = [], []
    try:
        from services import deliver_collection
        sent_ids, failed = await deliver_collection(
            context.bot, uid, c, files)
    except Exception as exc:
        import logging
        logging.getLogger("fun.delivery").exception("deliver: %s", exc)
        failed = files

    await db.create_delivery({
        "delivery_id": delivery_id, "user_id": uid,
        "collection_id": c["collection_id"],
        "message_ids": sent_ids,
        "auto_delete_at": db.now_ts() + config.AUTO_DELETE_AFTER_SECONDS,
        "delete_status": "pending" if sent_ids else "none",
        "failed_count": len(failed), "at": db.now_ts()})

    doc = await db.get_user(uid) or {}
    if not sent_ids and files:
        # zero-delivery -> full refund
        if price_paid:
            await db.add_tokens(uid, price_paid, "delivery_refund",
                                {"collection_id": c["collection_id"]})
        doc = await db.get_user(uid) or {}
        await _edit(q, persona.delivery_failed_refund(
            price_paid, int(doc.get("tokens", 0))), _nav_row("home"))
        return

    await db.bump_collection(c["collection_id"], "unlocks")
    await _edit(q, persona.stream_complete(
        esc(c.get("title") or "Drop"), len(sent_ids), price_paid),
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
    if not task:
        await q.answer("🎀 Task not available...", show_alert=True)
        return
    last = await db.get_last_task_run(uid, task_id)
    remaining = config.TASK_COOLDOWN_SECONDS - \
        (db.now_ts() - (last or {}).get("at", 0)) if last else 0
    if last and remaining > 0:
        h, m = int(remaining // 3600), int((remaining % 3600) // 60)
        await q.answer(persona.task_cooldown(h, m), show_alert=True)
        return
    # Verification: provider callback (see providers.py) hits /task/complete
    # with a signed token. The button only opens the sponsor link; crediting
    # happens server-side on the verified callback — NOT on button tap.
    await q.answer("🔗 Opening sponsor link — come back after finishing!",
                   show_alert=True)
    await q.message.reply_text(
        f"🎀 Sponsor task: {esc(task['label'])}\n\n"
        f"Complete it, then the provider will confirm and I'll credit "
        f"+{task.get('reward', config.DEFAULT_TASK_REWARD)} tokens "
        f"automatically!\n\n👉 {esc(task.get('url', ''))}",
        parse_mode="HTML")


async def credit_task(user_id: int, task_id: str) -> str:
    """Called by the verified provider callback."""
    settings = await db.get_settings()
    task = next((t for t in (settings.get("tasks") or [])
                 if t.get("task_id") == task_id), None)
    if not task:
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
    today = db.ist_today(0)
    yesterday = db.ist_today(1)
    last = await db.get_last_checkin(user_id)
    if last and last.get("date") == today:
        from datetime import datetime, timedelta
        now_ist = datetime.now(db.IST)
        nxt = (now_ist + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        rem = (nxt - now_ist).total_seconds()
        h, m = int(rem // 3600), int((rem % 3600) // 60)
        await reply(persona.checkin_already(h, m))
        return
    streak = (last.get("streak", 0) + 1
              if last and last.get("date") == yesterday else 1)
    bonus = config.STREAK_BONUS if streak % config.STREAK_PERIOD_DAYS == 0 else 0
    reward = config.DAILY_CHECKIN_REWARD + bonus
    await db.record_checkin(user_id, today, streak, reward)
    balance = await db.add_tokens(user_id, reward, "checkin",
                                  {"streak": streak})
    next_in = config.STREAK_PERIOD_DAYS - (streak % config.STREAK_PERIOD_DAYS)
    await reply(persona.checkin_done(config.DAILY_CHECKIN_REWARD, bonus,
                                     balance, streak, next_in))


# ── referral qualification (called after membership check passes) ─
async def maybe_qualify_referral(bot, friend_id: int, friend_name: str):
    doc = await db.qualify_referral(friend_id)
    if not doc:
        return
    inviter = int(doc["inviter_id"])
    bal = await db.add_tokens(inviter, config.REFERRAL_INVITER_BONUS,
                              "referral", {"friend_id": friend_id})
    try:
        await bot.send_message(inviter, persona.referral_notify(
            esc(friend_name), config.REFERRAL_INVITER_BONUS, bal),
            parse_mode="HTML")
        await db.mark_referral_notified(friend_id)
    except Exception:
        # notification failed — reward already credited (never double-credit)
        import logging
        logging.getLogger("fun.referral").warning("notify failed %s", inviter)


# ── admin ────────────────────────────────────────────────────────
def _is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


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
        "• /pending — review ambiguous groups\n"
        "• /setup_userbot — secure session onboarding\n"
        "• /stats — counts")


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    try:
        cid, cat = context.args[0], context.args[1]
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /addchannel <id> <category>")
        return
    await db.upsert_channel_mapping(int(cid), cat)
    await update.message.reply_text(f"✅ Channel {cid} → category {cat}")


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
    await db.update_settings({"required_channel_ids": [cid]})
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
    """/scan <channel_id> — run the Telethon userbot scanner now."""
    if not _is_admin(update.effective_user.id):
        return
    try:
        cid = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /scan <channel_id>")
        return
    cat = await db.category_for_channel(cid)
    if not cat:
        await update.message.reply_text("🎀 Map this channel first: /addchannel <id> <category>")
        return
    try:
        import main
        client = getattr(main, "_userbot_client", None)
    except Exception:
        client = None
    if not client:
        await update.message.reply_text("🎀 Userbot not connected yet — run /setup_userbot first.")
        return
    import scanner
    r = await scanner.scan_channel(client, cid, cat)
    await update.message.reply_text(
        f"✅ Scan done: {r['collections']} collection(s), "
        f"{r['posts_scanned']} posts, {r['pending_files']} pending file(s).")


async def cmd_setup_userbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setup_userbot — owner-only. Import an existing StringSession securely.
    The session is encrypted (SESSION_ENCRYPTION_KEY in Render env) and stored
    in MongoDB. It is never logged or shown. Owner sends it in a PRIVATE
    follow-up message; we delete that message immediately after saving."""
    if update.effective_user.id != config.OWNER_ID:
        return
    if not config.SESSION_ENCRYPTION_KEY:
        await update.message.reply_text(
            "⚠️ Set SESSION_ENCRYPTION_KEY in Render env first "
            "(a Fernet key). Then re-run /setup_userbot.")
        return
    await db.update_settings({"userbot_setup_waiting": update.effective_user.id})
    await update.message.reply_text(
        "🎀 Send me your Telethon StringSession now (private chat only). "
        "I'll encrypt it, store it, and delete your message right away.")


async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captures the owner session-import message during setup."""
    s = await db.get_settings()
    waiting = s.get("userbot_setup_waiting")
    if not waiting or update.effective_user.id != waiting:
        return
    session = (update.message.text or "").strip()
    try:
        import userbot_setup
        await userbot_setup.save_session(session)
        await update.message.reply_text("✅ Session encrypted & stored. Restarting userbot...")
        try:
            import main
            main._userbot_client = await userbot_setup.build_client()
        except Exception:
            pass
        await db.update_settings({"userbot_setup_waiting": None})
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Save failed: {exc}")
    finally:
        try:
            await update.message.delete()
        except Exception:
            pass


# pending-review placeholder (owner corrects scanner groupings)
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    s = await db.get_settings()
    pend = s.get("pending_groups") or []
    if not pend:
        await update.message.reply_text("🎀 No ambiguous groups — all clean!")
        return
    await update.message.reply_text(
        f"🎀 {len(pend)} group(s) need review:\n" +
        "\n".join(f"• {p.get('desc', '?')}" for p in pend[:20]))
