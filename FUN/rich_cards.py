"""Feed cover cards: Telegraph link previews, edited IN PLACE.
Primary: editMessageText + LinkPreviewOptions(url=telegra.ph/graph.org cover,
prefer_large_media, show_above_text). No delete+resend flicker.
Fallback (hosts unavailable): editMessageMedia with the cached JPEG - uploaded
once, Telegram file_id reused afterwards. No Render-served cover URLs.
Message-kind changes (text<->media, which Telegram forbids in one edit) are the
only case that replaces a card, and the replacement is sent BEFORE the old
message is deleted.
"""
import html
import logging

from telegram import InputMediaPhoto, LinkPreviewOptions
from telegram.error import BadRequest, TelegramError

import covers
import db
import cover_sources

log = logging.getLogger("fun.cards")


def _hosted_url(cover):
    from cover_hosts import valid_direct_url
    for url in [cover.get("url"), *(cover.get("urls") or [])]:
        if valid_direct_url(url):
            return url
    return None


async def _cached_photo(cover):
    """Returns (source, key): file_id string if already on Telegram, else bytes."""
    # This is a PHOTO file_id received by Bot 3 itself, never another bot's
    # ID or a video thumbnail ID (thumbnails cannot be resent as photos).
    if cover.get("telegram_file_id"):
        return cover["telegram_file_id"], None
    for key in cover.get("asset_keys") or []:
        asset = await db.col("cover_assets").find_one(
            {"_id": key, "data": {"$exists": True}},
            {"data": 1, "telegram_file_id": 1})
        if not asset:
            continue
        if asset.get("telegram_file_id"):
            return asset["telegram_file_id"], key
        return bytes(asset["data"]), key  # raw bytes -> PTB multipart-attaches once
    return None, None


async def _save_file_id(result, key):
    if not key or result is None:
        return
    photos = result.get("photo") if isinstance(result, dict) else getattr(result, "photo", None)
    if photos:
        fid = photos[-1].get("file_id") if isinstance(photos[-1], dict) else getattr(photos[-1], "file_id", None)
        if isinstance(fid, str):
            await db.col("cover_assets").update_one(
                {"_id": key}, {"$set": {"telegram_file_id": fid}})


def _is_not_modified(exc):
    return "message is not modified" in str(exc).lower()


async def _show_photo_card(bot, chat_id, text, markup, collection, mode=None,
                          edit_message_id=None, collection_id=None):
    cover = collection.get("cover") or {}
    if cover_sources.is_native(collection):
        async with covers._lock("feed:" + str(chat_id)):
            return await _copy_video_card(bot, chat_id, text, markup, collection, edit_message_id)
    url = _hosted_url(cover)
    mid = edit_message_id
    async with covers._lock("feed:" + str(chat_id)):
        if url:
            body = '<a href="' + html.escape(url, quote=True) + '">\u2063</a>\n' + text
            options = LinkPreviewOptions(is_disabled=False, url=url,
                                         prefer_large_media=True,
                                         show_above_text=True)
            if mid:
                try:
                    return await bot.edit_message_text(
                        chat_id=chat_id, message_id=mid, text=body,
                        parse_mode="HTML", reply_markup=markup,
                        link_preview_options=options)
                except BadRequest as exc:
                    if _is_not_modified(exc):
                        return None
                    if "no text in the message" in str(exc).lower():
                        result = await bot.send_message(
                            chat_id=chat_id, text=body, parse_mode="HTML",
                            reply_markup=markup, link_preview_options=options)
                        try:
                            await bot.delete_message(chat_id=chat_id, message_id=mid)
                        except TelegramError:
                            log.info("Old photo card retained; new link-preview card sent")
                        return result
                    else:
                        raise
            else:
                return await bot.send_message(
                    chat_id=chat_id, text=body, parse_mode="HTML",
                    reply_markup=markup, link_preview_options=options)
        # media path: cached file_id, one-time byte upload, or hosted URL as media
        source, key = (url, None) if url else await _cached_photo(cover)
        if source:
            media = InputMediaPhoto(media=source, caption=text, parse_mode="HTML")
            if mid:
                try:
                    result = await bot.edit_message_media(
                        chat_id=chat_id, message_id=mid, media=media,
                        reply_markup=markup)
                except BadRequest as exc:
                    if _is_not_modified(exc):
                        return None
                    if "no media" in str(exc).lower() or "wrong type" in str(exc).lower():
                        # text card -> media card: send replacement FIRST
                        result = await bot.send_photo(
                            chat_id=chat_id, photo=source, caption=text,
                            parse_mode="HTML", reply_markup=markup)
                        try:
                            await bot.delete_message(chat_id=chat_id, message_id=mid)
                        except TelegramError:
                            log.info("old feed card could not be deleted; replacement retained")
                    else:
                        raise
            else:
                result = await bot.send_photo(
                    chat_id=chat_id, photo=source, caption=text,
                    parse_mode="HTML", reply_markup=markup)
            # Cache the assigned file_id even for URL-sourced media: speeds up
            # any later editMessageMedia fallback to the cached image.
            await _save_file_id(result, key or (cover.get("asset_keys") or [None])[0])
            return result
        # nothing visual available: explicit text card, preview disabled
        body = (text + "\n\n<i>⚠️ Cover preview unavailable. Owner: run "
                "/retry_covers (needs the userbot) or check /cover_status.</i>")
        options = LinkPreviewOptions(is_disabled=True)
        if mid:
            try:
                return await bot.edit_message_text(
                    chat_id=chat_id, message_id=mid, text=body,
                    parse_mode="HTML", reply_markup=markup,
                    link_preview_options=options)
            except BadRequest as exc:
                if _is_not_modified(exc):
                    return None
                if "no text in the message" not in str(exc).lower():
                    raise
                result = await bot.send_message(
                    chat_id=chat_id, text=body, parse_mode="HTML",
                    reply_markup=markup, link_preview_options=options)
                await _delete_old(bot, chat_id, mid)
                return result
        return await bot.send_message(
            chat_id=chat_id, text=body, parse_mode="HTML",
            reply_markup=markup, link_preview_options=options)


async def _delete_old(bot, chat_id, mid):
    if not mid:
        return
    await cover_sources.clear_companion(bot, chat_id, mid)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=mid)
    except TelegramError:
        log.info("Previous card retained; replacement is available")


async def _copy_video_card(bot, chat_id, text, markup, collection, old_mid):
    """Server-side copy, never getFile/sendVideo/multipart upload.
    Short source captions retain every line and get the usual feed statistics.
    Long captions are copied VERBATIM, with a separate normal control card.
    """
    import re
    await cover_sources.persist(collection)
    _, source_chat, source_mid = cover_sources.resolve(collection)
    original = (collection.get('cover') or {}).get('caption')
    caption = html.escape(original) + "\n\n" + text if original is not None else None
    plain = html.unescape(re.sub(r'<[^>]*>', '', caption or ''))
    separate = caption is None or len(plain.encode('utf-16-le')) // 2 > 1024
    protect = bool((await db.get_settings()).get('protect_content', True))
    if not source_chat or not source_mid:
        result = await bot.send_message(chat_id=chat_id, text=text +
            "\n\n<i>⚠️ Video cover source is missing. Owner: check /repair_video_covers.</i>",
            parse_mode='HTML', reply_markup=markup)
        await _delete_old(bot, chat_id, old_mid)
        return result
    kwargs = dict(chat_id=chat_id, from_chat_id=source_chat,
                  message_id=source_mid, protect_content=protect)
    if not separate:
        kwargs.update(caption=caption, parse_mode='HTML', reply_markup=markup)
    try:
        copied = await bot.copy_message(**kwargs)
    except BadRequest as exc:
        error = str(exc).lower()
        if not any(t in error for t in ('message to copy not found', "message can't be copied",
                                         'message cannot be copied', 'protected content',
                                         'chat not found', 'message_id_invalid')):
            raise
        log.warning('Video cover copy unavailable collection=%s', collection.get('collection_id'))
        result = await bot.send_message(chat_id=chat_id, text=text +
            "\n\n<i>⚠️ Video cover unavailable: source deleted, inaccessible, or copying restricted. "
            "Owner: check the source post and bot access. No re-upload was attempted.</i>",
            parse_mode='HTML', reply_markup=markup)
        await _delete_old(bot, chat_id, old_mid)
        return result
    result = copied
    if separate:
        try:
            result = await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', reply_markup=markup)
            await db.col('cover_companions').update_one(
                {'_id': f'{chat_id}:{result.message_id}'},
                {'$set': {'cover_message_id': copied.message_id}}, upsert=True)
        except Exception:
            # Original control is still intact. Never retry a copy on timeout.
            try:
                await bot.delete_message(chat_id=chat_id, message_id=copied.message_id)
            except TelegramError:
                pass
            raise
    await _delete_old(bot, chat_id, old_mid)
    return result


async def show_cover_card(bot, chat_id, text, markup, collection, mode=None,
                          edit_message_id=None, collection_id=None):
    result = await _show_photo_card(bot, chat_id, text, markup, collection, mode,
                                   edit_message_id, collection_id)
    # A long-caption source copy belongs to the old text control card.
    # Clear it only AFTER the replacement/edit succeeded, never before.
    if not cover_sources.is_native(collection):
        await cover_sources.clear_companion(bot, chat_id, edit_message_id)
    return result
