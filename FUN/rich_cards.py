"""Feed cover cards. SPEED: hosted URL -> rich card (URL swap only, no upload).

When cover.url is set (telegra.ph / graph.org), the card is a rich message whose
image is the external URL. Next/Previous edits swap the URL — Telegram fetches it
server-side; the bot never re-uploads bytes. That is the fast path your rival uses.

Fallbacks, in order, never leaving a blank card:
  1. hosted URL -> rich tg-slideshow / tg-collage (fast, no upload)
  2. hosted URL -> sendPhoto/editMessageMedia with the SAME url (no upload)
  3. no URL, but cached image -> upload ONCE, then cache telegram file_id
"""
import html, json, re, logging
from typing import Optional
from telegram import InputFile, InputMediaPhoto
from telegram.error import BadRequest
import db
log = logging.getLogger("fun.rich_cards")


def _cover_urls(collection) -> list:
    c = collection.get("cover") or {}
    urls = [u for u in (c.get("urls") or []) if isinstance(u, str) and u.startswith("http")]
    if c.get("url"):
        urls.insert(0, c["url"])
    # de-dup, keep order
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def _rich_payload(text, urls, mode):
    tag = "tg-collage" if mode == "collage" else "tg-slideshow"
    media = "".join('<img src="' + html.escape(u, quote=True) + '"/>' for u in urls)
    return {"html": f"<{tag}>{media}</{tag}><p>" + text.replace("\n", "<br>") + "</p>"}


def _caption(text):
    plain = html.unescape(re.sub(r"<[^>]*>", "", text))
    return text if len(plain.encode("utf-16-le")) // 2 <= 1000 else html.escape(plain[:900] + "…")


async def show_cover_card(bot, chat_id, text, markup, collection, mode,
                          edit_message_id=None, collection_id=None):
    cover = collection.get("cover") or {}
    urls = _cover_urls(collection)

    # FAST PATH: hosted URL -> rich card or URL photo. No byte upload.
    if urls:
        payload = _rich_payload(text, urls, mode)
        data = {"chat_id": chat_id, "rich_message": json.dumps(payload)}
        if markup is not None:
            data["reply_markup"] = markup.to_dict()
        endpoint = "sendRichMessage"
        if edit_message_id:
            endpoint = "editMessageText"; data["message_id"] = edit_message_id
        try:
            return await bot._post(endpoint, data=data)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return None
            log.info("rich card rejected, trying URL photo fallback: %s", type(exc).__name__)
        # URL photo fallback (still no upload — Telegram fetches the URL).
        try:
            if edit_message_id:
                return await bot.edit_message_media(
                    chat_id=chat_id, message_id=edit_message_id,
                    media=InputMediaPhoto(urls[0], caption=_caption(text), parse_mode="HTML"),
                    reply_markup=markup)
            return await bot.send_photo(chat_id=chat_id, photo=urls[0],
                                        caption=_caption(text), parse_mode="HTML",
                                        reply_markup=markup)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return None
            log.info("URL photo fallback failed, trying cached image upload")

    # SLOW PATH (only when no hosted URL): upload cached image ONCE, then reuse file_id.
    keys = (cover.get("asset_keys") or [])
    if keys:
        asset = await db.col("cover_assets").find_one({"_id": keys[0]})
        if asset:
            fid = asset.get("telegram_file_id")
            source = fid or (InputFile(bytes(asset["data"]), filename="cover.jpg", attach=True)
                             if asset.get("data") else None)
            if source is not None:
                try:
                    if edit_message_id:
                        result = await bot.edit_message_media(
                            chat_id=chat_id, message_id=edit_message_id,
                            media=InputMediaPhoto(source, caption=_caption(text), parse_mode="HTML"),
                            reply_markup=markup)
                    else:
                        result = await bot.send_photo(chat_id=chat_id, photo=source,
                                                      caption=_caption(text), parse_mode="HTML",
                                                      reply_markup=markup)
                    if not fid:  # cache file_id so later taps don't re-upload
                        photos = result.get("photo") if isinstance(result, dict) else getattr(result, "photo", None)
                        if photos:
                            last = photos[-1]
                            new_id = last.get("file_id") if isinstance(last, dict) else getattr(last, "file_id", None)
                            if new_id:
                                await db.col("cover_assets").update_one(
                                    {"_id": keys[0]}, {"$set": {"telegram_file_id": new_id}})
                    return result
                except BadRequest as exc:
                    if "message is not modified" in str(exc).lower():
                        return None
                    log.info("cached image upload failed, falling back to text")

    # LAST RESORT: text-only card (never blank).
    note = ("\n\n<i>⚠️ Preview not ready. Owner: /retry_covers then /cover_status.</i>"
            if not urls else "")
    if edit_message_id:
        try:
            return await bot.edit_message_text(text + note, chat_id=chat_id,
                                               message_id=edit_message_id, reply_markup=markup,
                                               parse_mode="HTML")
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return None
        except Exception:
            pass
    return await bot.send_message(chat_id, text + note, reply_markup=markup, parse_mode="HTML")
