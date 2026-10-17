"""Link-preview feed: sendMessage only, never sendPhoto or media edits.
The filename is kept for existing imports. This is NOT sendRichMessage.
Send a replacement successfully before deleting the previous bot card.
"""
import html
import logging
from collections import OrderedDict
from telegram import LinkPreviewOptions
from telegram.error import TelegramError
import covers
import link_previews

log = logging.getLogger("fun.link_cards")
_retired = OrderedDict()
MAX_RETIRED = 4096

async def show_cover_card(bot, chat_id, text, markup, collection, mode=None,
                          edit_message_id=None, collection_id=None):
    old_id = edit_message_id  # compatibility argument; no edit API is called
    async with covers._lock("feed:" + str(chat_id)):
        old_key = (chat_id, old_id)
        if old_id and old_key in _retired:
            return None  # rapid repeated taps on an already-replaced card
        url = await link_previews.resolve_url(collection)
        if url:
            # Explicit URL selects the cover even when the caption has other links.
            text = '<a href="' + html.escape(url, quote=True) + '">\u2063</a>\n' + text
            options = LinkPreviewOptions(is_disabled=False, url=url,
                prefer_large_media=True, show_above_text=True)
        else:
            text += ("\n\n<i>⚠️ Cover preview unavailable. Owner: check /cover_status "
                     "and RENDER_EXTERNAL_URL; run /retry_covers if no image is cached.</i>")
            options = LinkPreviewOptions(is_disabled=True)
        # On error/timeout do not delete the existing card, retry with media,
        # or silently repeat a send that Telegram may already have accepted.
        result = await bot.send_message(chat_id=chat_id, text=text,
            parse_mode="HTML", reply_markup=markup, link_preview_options=options)
        new_id = result.get("message_id") if isinstance(result, dict) else getattr(result, "message_id", None)
        if old_id and new_id and old_id != new_id:
            _retired[old_key] = True
            while len(_retired) > MAX_RETIRED:
                _retired.popitem(last=False)
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_id)
            except TelegramError:
                # New card stays usable. Old card might remain if deletion is denied.
                log.info("Old feed card could not be deleted; replacement retained")
        return result
