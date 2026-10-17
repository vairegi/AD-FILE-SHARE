"""Shared Telegram UI helpers. Colors are actual Bot API styles, not emoji."""
import html
import re
from urllib.parse import urlencode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import config


def button(text, callback_data=None, *, url=None, style=None):
    if style is None:
        data = callback_data or ""
        if data.startswith("unlock:") or data == "feed:all:0":
            style = "danger"
        elif (data in ("cats", "checkjoin", "checkin") or
              data.startswith(("restream:", "task:", "library:unlocks"))):
            style = "success"
        else:
            style = "primary"
    if style not in ("primary", "success", "danger"):
        raise ValueError("Unsupported button style")
    if callback_data is not None and not 1 <= len(callback_data.encode()) <= 64:
        raise ValueError("Button callback must be 1–64 bytes")
    # api_kwargs works with older PTB versions too, and serializes to the
    # exact Bot API field. Do not strip styles on network errors.
    return InlineKeyboardButton(text, callback_data=callback_data, url=url,
                                api_kwargs={"style": style})


async def bot_username(bot):
    """Prefer getMe identity, never emit https://t.me/?start=... ."""
    try:
        cached = getattr(bot, "username", None)
    except RuntimeError:  # real PTB bot not initialized yet
        cached = None
    if not cached:
        try:
            me = await bot.get_me()
            cached = getattr(me, "username", None)
        except Exception:
            cached = None
    value = (cached or config.BOT_USERNAME or "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
        raise RuntimeError("Cannot resolve this bot's username. Check getMe/BOT_USERNAME.")
    return value


async def referral_links(bot, user_id):
    username = await bot_username(bot)
    link = f"https://t.me/{username}?start=ref_{int(user_id)}"
    share = "https://t.me/share/url?" + urlencode({
        "url": link, "text": "Stream exclusive media now!"})
    return link, share


async def edit_card(q, text, kb):
    """In-place edits; a redundant tap is not a reason to spam a new card."""
    from telegram.error import BadRequest
    try:
        return await q.edit_message_text(text, reply_markup=kb,
                                         parse_mode="HTML",
                                         disable_web_page_preview=True)
    except BadRequest as exc:
        message = str(exc).lower()
        if "message is not modified" in message:
            return None
        if "message to edit not found" in message or "message can't be edited" in message:
            return await q.message.reply_text(text, reply_markup=kb,
                                               parse_mode="HTML")
        raise


def h(value):
    return html.escape(str(value), quote=True)
