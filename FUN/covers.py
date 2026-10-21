"""Cover preparation: download the small cover image once (userbot), upload it
to telegra.ph, graph.org or Catbox, cache the direct file URL in
MongoDB. Navigation never re-uploads. If every host fails, the cached JPEG is
sent to Telegram once via editMessageMedia and its file_id is reused.
"""
import asyncio
import hashlib
import io
import logging
from weakref import WeakValueDictionary

import config
import db

log = logging.getLogger("fun.covers")
MAX_BYTES = 5 * 1024 * 1024
MAX_PREVIEWS = 10
_locks = WeakValueDictionary()


def asset_key(channel_id, message_id):
    return f"{channel_id}:{message_id}"


def _lock(key):
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _http_url(value):
    if not isinstance(value, str):
        return False
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(value)
        return parts.scheme in ("https", "http") and bool(parts.hostname) \
            and not parts.username and not parts.password
    except ValueError:
        return False


_last_upload_at = 0.0
MIN_UPLOAD_INTERVAL = 2.0  # seconds between upload attempts (anti flood-limit)


async def _pace():
    global _last_upload_at
    async with _lock("host-pace"):
        now = asyncio.get_running_loop().time()
        wait = MIN_UPLOAD_INTERVAL - (now - _last_upload_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_upload_at = asyncio.get_running_loop().time()


def _sanitize(msg):
    import cover_hosts
    return cover_hosts.sanitize(msg)


async def ensure_token(client):
    """Legacy compatibility: no account creation. Direct-file hosts need no token."""
    return (await db.get_settings()).get("telegraph_token")


async def host_cover(client, file_bytes, token=None, **legacy_kwargs):
    """Direct files only: asks DB for owner's preferred host, then falls back
    through the multi-host adapter order. Legacy ``token``, ``primary``,
    ``mirror``, ``userhash`` kwargs are accepted for backward-compat with the
    pre-1.6 test suite and silently ignored when the owner did not opt-in."""
    import cover_hosts
    settings = await db.get_settings()
    preferred = settings.get("preferred_host")
    if not preferred:
        for legacy in ("primary", "preferred"):
            value = legacy_kwargs.get(legacy)
            if value and value in cover_hosts.HOSTS:
                preferred = value
                break
    return await cover_hosts.upload(client, file_bytes, preferred=preferred, pace=_pace)


def normalize_image(data):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("cover_image_missing_or_too_large")
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


async def download_image(userbot, channel_id, mid):
    """Small cover bytes only: photo or video THUMBNAIL, never the full video."""
    msg = await userbot.get_messages(channel_id, ids=mid)
    if not msg:
        return None
    photo = getattr(msg, "photo", None)
    media = getattr(msg, "media", None) or photo or getattr(msg, "video", None) \
        or getattr(msg, "document", None)
    if media is None:
        return None
    buf = io.BytesIO()
    if photo:
        await userbot.download_media(media, file=buf)
    elif getattr(msg, "video", None) or getattr(msg, "document", None):
        await userbot.download_media(media, file=buf, thumb=-1)
    else:
        return None
    return normalize_image(buf.getvalue())


async def _prepare(collection, http, fetch_bytes, force=False):
    import cover_sources
    if cover_sources.is_native(collection):
        await cover_sources.persist(collection)
        _, chat, mid = cover_sources.resolve(collection)
        return {"hosted": False, "ready": bool(chat and mid), "native": True}
    cover = collection.get("cover") or {}
    if cover.get("url_status") == "ok" and cover.get("url"):
        return {"hosted": True, "ready": True, "cached": True}
    keys = list(cover.get("asset_keys") or [])
    data = None
    if keys:
        asset = await db.col("cover_assets").find_one(
            {"_id": keys[0], "data": {"$exists": True}}, {"data": 1})
        if asset:
            data = bytes(asset["data"])
    if data is None and fetch_bytes is not None:
        mid = cover.get("message_id")
        cid = collection.get("channel_id")
        # Scanner may call before upserting channel_id; the deterministic
        # collection_id "c<channel_id>_<message_id>" carries both.
        if (not mid or not cid) and str(collection.get("collection_id", "")).count("_") == 1:
            try:
                left, right = str(collection["collection_id"]).split("_")
                cid = cid or int(left[1:])
                mid = mid or int(right)
            except (ValueError, IndexError):
                pass
        if mid and cid:
            key = asset_key(cid, mid)
            async with _lock("dl:" + key):
                try:
                    data = await fetch_bytes(cid, mid)
                except Exception as exc:
                    log.info("cover download failed for %s: %s", key, type(exc).__name__)
                    data = None
                if data:
                    from bson import Binary
                    await db.col("cover_assets").update_one(
                        {"_id": key},
                        {"$set": {"data": Binary(data), "mime": "image/jpeg",
                                  "sha256": hashlib.sha256(data).hexdigest(),
                                  "channel_id": cid, "source_message_id": mid}},
                        upsert=True)
                    keys = [key]
    if not data:
        await db.col("collections").update_one(
            {"collection_id": collection["collection_id"]},
            {"$set": {"cover.url_status": "failed",
                      "cover.last_error": "cover_image_missing_or_source_unreadable",
                      "cover.last_attempt_at": db.now_ts()}})
        return {"hosted": False, "ready": False}
    hosted = await host_cover(http, data)
    update = {"cover.asset_keys": keys, "cover.last_attempt_at": db.now_ts()}
    if hosted["status"] == "rate_limited":
        # Stay pending so a later /retry_covers (after cooldown) picks it up.
        update.update({"cover.url_status": "pending",
                       "cover.last_error": hosted["error"]})
        await db.col("collections").update_one(
            {"collection_id": collection["collection_id"]}, {"$set": update})
        return {"hosted": False, "ready": True, "rate_limited": True,
                "all_hosts_unavailable": hosted.get("all_hosts_unavailable", False)}
    if hosted["status"] == "ok":
        update.update({"cover.url": hosted["url"],
                       "cover.url_host": hosted["host"],
                       "cover.url_status": "ok",
                       "cover.last_error": None})
    else:
        update.update({"cover.url_status": "failed",
                       "cover.last_error": hosted["error"]})
    await db.col("collections").update_one(
        {"collection_id": collection["collection_id"]}, {"$set": update})
    # ready=True: cached JPEG remains usable through the editMessageMedia fallback
    return {"hosted": hosted["status"] == "ok", "ready": True,
            "all_hosts_unavailable": hosted.get("all_hosts_unavailable", False)}


async def prepare_collection(userbot, collection, http, force=False):
    async def fetch(cid, mid):
        return await download_image(userbot, cid, mid)
    return await _prepare(collection, http,
                          fetch if userbot is not None else None, force=force)


async def prepare_cover_for_collection(collection, client, download_bytes):
    """Scanner entry point: caller supplies the per-message downloader."""
    async def fetch(cid, mid):
        return await download_bytes(mid)
    return await _prepare(collection, client, fetch)


async def preview_items(collection):
    cover = collection.get("cover") or {}
    items = []
    if cover.get("url"):
        items.append({"url": cover["url"]})
    for u in cover.get("urls") or []:
        items.append({"url": u})
    for k in cover.get("asset_keys") or []:
        items.append({"key": k})
    return items[:MAX_PREVIEWS]


async def needs_preparation_count():
    return await db.col("collections").count_documents(
        {"$and": [
            {"cover.kind": {"$nin": ["video", "animation", "document_video"]}},
            {"cover.media_type": {"$nin": ["video", "animation", "document_video"]}},
            {"$or": [{"cover.url_status": {"$ne": "ok"}},
                     {"cover.url": None}, {"cover.url": ""}]}]})
