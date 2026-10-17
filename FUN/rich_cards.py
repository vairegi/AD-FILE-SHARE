"""Verified rich HTML -> safe in-place photo fallback. Never delete first.
Uses the official InputRichMessage.html field; no invented block schemas.
Feed reads cached URLs/Telegram file_ids, not the source userbot. Where both
mirror hosts fail, cached MongoDB image bytes are uploaded once and their
Telegram file_id is persisted for later taps.
"""
import html
import json
import re
import logging
from typing import Optional
from telegram import InputFile, InputMediaPhoto
from telegram.error import BadRequest
import db
log = logging.getLogger("fun.rich_cards")


def _items(value):
    if isinstance(value,str): return [{"url":value,"key":None,"file_id":None}]
    return value or []


def _rich_payload(text, previews, mode, markup=None):
    urls = [p.get("url") for p in _items(previews) if p.get("url")]
    tag = "tg-collage" if mode == "collage" else "tg-slideshow"
    media = "".join('<img src="'+html.escape(url,quote=True)+'"/>' for url in urls)
    return {"html": f"<{tag}>{media}</{tag}><p>" + text.replace("\n","<br>") + "</p>"}


def _caption(text):
    plain = html.unescape(re.sub(r"<[^>]*>","",text))
    return text if len(plain.encode("utf-16-le"))//2 <= 1000 else html.escape(plain[:900]+"…")


def _message_id(message):
    return message.get("message_id") if isinstance(message,dict) else getattr(message,"message_id",None)


async def _save_file_id(message, key):
    if not key: return
    photos = message.get("photo",[]) if isinstance(message,dict) else getattr(message,"photo",None)
    if photos:
        last = photos[-1]
        fid = last.get("file_id") if isinstance(last,dict) else getattr(last,"file_id",None)
        if isinstance(fid,str):
            await db.col("cover_assets").update_one({"_id":key},{"$set":{"telegram_file_id":fid}})


async def _photo_source(preview):
    if preview.get("file_id"): return preview["file_id"]
    # Prefer a known local image over an unreliable URL after rich rejection.
    if preview.get("key"):
        asset = await db.col("cover_assets").find_one({"_id":preview["key"]})
        if asset and asset.get("data"):
            return InputFile(bytes(asset["data"]),filename="cover.jpg",attach=True)
    return preview.get("url")


async def show_cover_card(bot, chat_id, text, markup, cover_url, mode,
                          edit_message_id=None, collection_id=None):
    previews = _items(cover_url)
    urls = [p for p in previews if p.get("url")]
    if urls:
        data={"chat_id":chat_id,"rich_message":json.dumps(_rich_payload(text,urls,mode)),
              "reply_markup":markup.to_dict() if markup else None}
        endpoint="sendRichMessage"
        if edit_message_id:
            endpoint="editMessageText"; data["message_id"]=edit_message_id
        try:
            return await bot._post(endpoint,data=data)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower(): return None
            log.info("Rich preview rejected; using cached photo fallback (%s)",type(exc).__name__)
            # Only explicit Telegram rejections are safe to fall back from.
            # A timeout could mean the first request was accepted.
    for preview in previews:
        source = await _photo_source(preview)
        if source is None: continue
        try:
            if edit_message_id:
                result = await bot.edit_message_media(chat_id=chat_id,message_id=edit_message_id,
                    media=InputMediaPhoto(source,caption=_caption(text),parse_mode="HTML"),reply_markup=markup)
            else:
                result = await bot.send_photo(chat_id=chat_id,photo=source,caption=_caption(text),
                                              parse_mode="HTML",reply_markup=markup)
            await _save_file_id(result,preview.get("key"))
            return result
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower(): return None
            # Try a new photo only if the original cannot be converted/edited.
            try:
                result = await bot.send_photo(chat_id=chat_id,photo=source,caption=_caption(text),
                    parse_mode="HTML",reply_markup=markup)
                await _save_file_id(result,preview.get("key"))
                if edit_message_id and _message_id(result):
                    try:
                        await bot.delete_message(chat_id=chat_id,message_id=edit_message_id)
                    except Exception:
                        pass
                return result
            except BadRequest:
                continue
    # Do not silently call this a working image. Keep navigation working and
    # tell the user/owner exactly that there is no prepared usable image.
    if previews:
        text += "\n\n<i>⚠️ Preview unavailable. Owner: /cover_status.</i>"
    if edit_message_id:
        try:
            return await bot.edit_message_text(text,chat_id=chat_id,message_id=edit_message_id,
                                               reply_markup=markup,parse_mode="HTML")
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower(): return None
    return await bot.send_message(chat_id,text,reply_markup=markup,parse_mode="HTML")
