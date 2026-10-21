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
    from persona import share_text
    share = "https://t.me/share/url?" + urlencode({
        "url": link, "text": share_text()})
    return link, share


async def _edit_card(q, text, kb):
    """Keep native photo fallback navigable as well as text/rich cards."""
    from telegram.error import BadRequest
    import html as html_module
    message=getattr(q,"message",None)
    plain=html_module.unescape(re.sub(r"<[^>]*>","",text))
    photo=any(bool(getattr(message, k, None)) for k in ("photo", "video", "animation", "document"))
    if photo and len(plain.encode("utf-16-le"))//2 <= 1000:
        try:
            return await q.edit_message_caption(caption=text,reply_markup=kb,parse_mode="HTML")
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():return None
            raise
    if photo:
        # Caption is too long for a native photo. Create the replacement first;
        # never delete the only visible card before another request succeeds.
        replacement=await message.reply_text(text,reply_markup=kb,parse_mode="HTML")
        try:await message.delete()
        except Exception:pass
        return replacement
    try:
        return await q.edit_message_text(text,reply_markup=kb,parse_mode="HTML",disable_web_page_preview=True)
    except BadRequest as exc:
        error=str(exc).lower()
        if "message is not modified" in error:return None
        if "message to edit not found" in error or "message can't be edited" in error:
            return await message.reply_text(text,reply_markup=kb,parse_mode="HTML")
        raise


def h(value):
    return html.escape(str(value), quote=True)


async def edit_card(q, text, kb):
    result = await _edit_card(q, text, kb)
    message = getattr(q, 'message', None)
    chat = getattr(message, 'chat', None)
    if chat is not None and callable(getattr(q, 'get_bot', None)):
        import cover_sources
        await cover_sources.clear_companion(q.get_bot(), chat.id,
                                            getattr(message, 'message_id', None))
    return result
