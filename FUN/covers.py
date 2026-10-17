"""Durable cover pipeline. Download once, Telegraph -> graph.org, cache.
A local preview asset is also stored in MongoDB so mirror downtime doesn't
mean a text-only feed. Each small image is a separate document, not a giant
collection document. Only explicit preparation/retry downloads source media.
"""
import asyncio
import hashlib
import io
import logging
import time
from weakref import WeakValueDictionary
from urllib.parse import urljoin, urlparse
from bson import Binary
import config
import db
log = logging.getLogger("fun.covers")
MAX_BYTES = 5 * 1024 * 1024
MAX_PREVIEWS = 10
_locks = WeakValueDictionary()


def asset_key(channel_id, message_id):
    return f"{int(channel_id)}:{int(message_id)}"


def _lock(key):
    result = _locks.get(key)
    if result is None:
        result = asyncio.Lock(); _locks[key] = result
    return result


def _http_url(value):
    if not isinstance(value, str): return False
    parsed = urlparse(value)
    return parsed.scheme in ("https", "http") and bool(parsed.hostname) and not parsed.username


async def _try_upload(client, url, file_bytes):
    try:
        response = await client.post(url, files={"file": ("cover.jpg", file_bytes, "image/jpeg")}, timeout=12)
        if response.status_code != 200:
            return None
        payload = response.json()
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            src = payload[0].get("src")
            result = urljoin(url, src) if isinstance(src, str) else None
            if result and _http_url(result):
                return result
    except Exception:
        pass
    return None


async def _validate(client, url):
    try:
        response = await client.head(url, timeout=10, follow_redirects=True)
        if response.status_code == 405:
            response = await client.get(url, headers={"Range": "bytes=0-0"}, timeout=10, follow_redirects=True)
        return response.status_code in (200, 206) and response.headers.get("content-type", "").lower().startswith("image/")
    except Exception:
        return False


async def host_cover(client, file_bytes):
    for name, endpoint in (("telegra.ph", config.COVER_PRIMARY_HOST), ("graph.org", config.COVER_FALLBACK_HOST)):
        url = await _try_upload(client, endpoint, file_bytes)
        if url and await _validate(client, url):
            return {"url": url, "host": name, "status": "ok"}
    return {"url": None, "host": None, "status": "failed"}


async def prepare_cover_for_collection(collection, client, download_bytes):
    """Compatibility helper. Runtime uses prepare_collection below."""
    cover = collection.get("cover") or {}
    if cover.get("url_status") == "ok" and cover.get("url"):
        return collection
    try:
        data = await download_bytes(cover.get("message_id"))
        if data:
            result = await host_cover(client, data)
            cover.update(url=result["url"], url_host=result["host"], url_status=result["status"])
        else:
            cover["url_status"] = "failed"
    except Exception:
        cover["url_status"] = "failed"
    collection["cover"] = cover
    return collection


def normalize_image(data):
    from PIL import Image, ImageOps
    if not data or len(data) > MAX_BYTES:
        raise ValueError("cover_image_missing_or_too_large")
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 25_000_000:
            raise ValueError("cover_dimensions_too_large")
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1920,1920))
        output = io.BytesIO(); image.save(output, format="JPEG", quality=85, optimize=True)
        return output.getvalue()


async def download_image(userbot, channel_id, mid):
    """Photos: media. Video covers: still thumbnail, NOT the full video."""
    message = await asyncio.wait_for(userbot.get_messages(int(channel_id), ids=int(mid)), timeout=25)
    if not message:
        raise ValueError("source_message_missing")
    output = io.BytesIO()
    if getattr(message, "photo", None):
        await asyncio.wait_for(userbot.download_media(message.photo, file=output), timeout=40)
    elif getattr(message, "video", None):
        await asyncio.wait_for(userbot.download_media(message, thumb=-1, file=output), timeout=40)
    else:
        raise ValueError("source_has_no_preview_media")
    return normalize_image(output.getvalue())


async def prepare_collection(userbot, collection, http, force=False):
    """Host each cover image and cache URLs. If bytes are already cached and
    the URL is missing, re-upload from the cache (no source re-download)."""
    """Prepare all cover-album images with persistent per-source caching.
Successful links are never re-downloaded or uploaded just because of a tap
or rescan. A failed mirror can still leave a usable Telegram-upload preview.
"""
    cid = collection["collection_id"]
    original = collection.get("cover") or {}
    if not force and original.get("url_status") == "ok" and original.get("url"):
        return {"hosted": True, "ready": True, "cached": True}
    ids = original.get("album_message_ids") or [original.get("message_id")]
    ids = list(dict.fromkeys(x for x in ids if x))[:MAX_PREVIEWS]
    keys, urls, errors = [], [], []
    for mid in ids:
        key = asset_key(collection["channel_id"], mid)
        async with _lock(key):
            asset = await db.col("cover_assets").find_one({"_id": key}) or {}
            data = bytes(asset["data"]) if asset.get("data") else None
            if not data:
                try:
                    data = await download_image(userbot, collection["channel_id"], mid)
                    await db.col("cover_assets").update_one({"_id": key}, {"$set": {
                        "data": Binary(data), "mime": "image/jpeg", "sha256": hashlib.sha256(data).hexdigest(),
                        "channel_id": collection["channel_id"], "source_message_id": mid,
                        "cached_at": db.now_ts()}}, upsert=True)
                except Exception as exc:
                    # Persist an actionable category, never raw RPC errors/credentials.
                    errors.append("source_preview_unavailable:" + type(exc).__name__)
                    continue
            keys.append(key)
            if asset.get("url_status") == "ok" and asset.get("url"):
                urls.append(asset["url"])
                continue
            # Scan skips known failed attempts; an explicit retry may re-attempt
            # the mirrors using cached bytes (without another source download).
            result = await host_cover(http, data)
            await db.col("cover_assets").update_one({"_id": key}, {"$set": {
                "url": result["url"], "url_host": result["host"], "url_status": result["status"],
                "last_attempt_at": db.now_ts()}}, upsert=True)
            if result["url"]:
                urls.append(result["url"])
    fields = {"cover.asset_keys": keys, "cover.urls": urls,
        "cover.url": urls[0] if urls else None,
        "cover.url_host": urlparse(urls[0]).hostname if urls else None,
        "cover.url_status": "ok" if urls else "failed",
        "cover.preview_status": "ready" if keys or urls else "failed",
        "cover.last_error": ";".join(errors) if errors else (None if urls else "mirrors_unavailable_using_cached_image"),
        "cover.last_attempt_at": db.now_ts()}
    await db.col("collections").update_one({"collection_id": cid}, {"$set": fields})
    return {"hosted": bool(urls), "ready": bool(keys or urls), "cached": False}


async def preview_items(collection):
    """Read metadata only. Do NOT load binary images per navigation tap."""
    cover = collection.get("cover") or {}
    keys = cover.get("asset_keys") or []
    items = []
    for key in keys[:MAX_PREVIEWS]:
        asset = await db.col("cover_assets").find_one({"_id":key}, {"data":0})
        if asset:
            items.append({"key":key,"url":asset.get("url"), "file_id":asset.get("telegram_file_id")})
    if not items:
        for url in cover.get("urls") or ([cover["url"]] if cover.get("url") else []):
            if _http_url(url): items.append({"key":None,"url":url,"file_id":None})
    return items


async def needs_preparation_count():
    # Includes pending, absent, failed, and malformed "ok but no URL" rows.
    return await db.col("collections").count_documents({"$or": [
        {"cover.url_status":{"$ne":"ok"}}, {"cover.url":None}, {"cover.url":""}]})
