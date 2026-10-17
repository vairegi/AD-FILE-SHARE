"""Cover preview cards: rich collage/slideshow -> photo -> text fallback.

Uses the current Bot API (rich messages, 10.1+): sendRichMessage /
editMessageText with rich_message. Blocks are RichBlockCollage or
RichBlockSlideshow containing photo blocks whose `photo` is the hosted URL.
Every level has a graceful fallback so the feed NEVER goes blank:
  hosted URL + rich accepted  -> swipeable slideshow / collage card
  hosted URL + rich rejected  -> normal photo message (sendPhoto)
  no hosted URL (upload failed)-> plain text card (previous behaviour)

The hosted URL is cached in MongoDB by the scanner; the feed only READS it,
so navigation never re-uploads or re-fetches the same cover.
"""
import json
import logging
from typing import Optional

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest

log = logging.getLogger("fun.rich_cards")


def _photo_block(url: str) -> dict:
    return {"type": "photo", "photo": url}


def _buttons_block(markup: InlineKeyboardMarkup) -> Optional[dict]:
    """Convert our InlineKeyboardMarkup to a RichBlockButtons block so the
    rich card keeps its coloured buttons in-message."""
    rows = []
    for row in markup.inline_keyboard:
        rb = []
        for b in row:
            raw = getattr(b, "api_kwargs", None) or {}
            entry = {"type": "button", "text": b.text,
                     "style": raw.get("style", "primary")}
            if b.callback_data is not None:
                entry["callback_data"] = b.callback_data
            if b.url:
                entry["url"] = b.url
            rb.append(entry)
        rows.append(rb)
    return {"type": "buttons", "buttons": rows} if rows else None


def _rich_payload(text: str, cover_url: str, mode: str,
                  markup: InlineKeyboardMarkup) -> dict:
    media = (_photo_block(cover_url) if mode == "collage"
             else {"type": "slideshow", "items": [_photo_block(cover_url)]})
    media_block = ({"type": "collage", "items": [media]} if mode == "collage"
                   else media)
    blocks = [media_block, {"type": "paragraph", "text": text}]
    btns = _buttons_block(markup)
    if btns:
        blocks.append(btns)
    return {"blocks": blocks}


async def show_cover_card(bot, chat_id: int, text: str,
                          markup: InlineKeyboardMarkup, cover_url: Optional[str],
                          mode: str, edit_message_id: Optional[int] = None) -> None:
    """Render the feed card with its cover. Falls back down the chain."""
    if cover_url:
        # 1. Rich collage/slideshow card (swipeable / grid preview).
        try:
            payload = _rich_payload(text, cover_url, mode, markup)
            data = {"chat_id": chat_id, "rich_message": json.dumps(payload)}
            if edit_message_id:
                data["message_id"] = edit_message_id
                await bot._post("editMessageText", data=data)
            else:
                await bot._post("sendRichMessage", data=data)
            return
        except BadRequest as exc:
            msg = str(exc).lower()
            if edit_message_id and "message is not modified" in msg:
                return
            log.info("rich cover rejected, falling back to photo: %s", exc)
        except Exception as exc:
            log.info("rich cover failed, falling back to photo: %s", exc)
        # 2. Normal photo message with the hosted URL + inline keyboard.
        try:
            if edit_message_id:
                await bot.delete_message(chat_id=chat_id,
                                         message_id=edit_message_id)
            await bot.send_photo(chat_id=chat_id, photo=cover_url,
                                 caption=text, parse_mode="HTML",
                                 reply_markup=markup)
            return
        except Exception as exc:
            log.info("photo fallback failed, falling back to text: %s", exc)
    # 3. Plain text card (no cover available / everything above failed).
    if edit_message_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id,
                                        message_id=edit_message_id,
                                        reply_markup=markup, parse_mode="HTML")
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=markup,
                           parse_mode="HTML")
