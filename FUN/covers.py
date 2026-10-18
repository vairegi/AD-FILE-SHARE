"""Cover preparation: download the small cover image once (userbot), upload it
to Telegraph (telegra.ph first, graph.org mirror), cache the direct file URL in
MongoDB. Navigation never re-uploads. If every host fails, the cached JPEG is
sent to Telegram once via editMessageMedia and its file_id is reused.
"""
import asyncio
import hashlib
import io
import re
import logging
from urllib.parse import urljoin
from weakref import WeakValueDictionary

import httpx

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


class UploadError(RuntimeError):
    pass


class RateLimited(UploadError):
    """Telegraph answered 429 / 'Try again later' — stop hammering, wait."""


_last_upload_at = 0.0
MIN_UPLOAD_INTERVAL = 2.0  # seconds between upload attempts (anti flood-limit)


async def _pace():
    global _last_upload_at
    now = asyncio.get_event_loop().time()
    wait = MIN_UPLOAD_INTERVAL - (now - _last_upload_at)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_upload_at = asyncio.get_event_loop().time()


def _sanitize(msg):
    """Never leak tokens/URLs-with-credentials into stored errors or chats."""
    msg = re.sub(r'access_token=[A-Za-z0-9_-]+', 'access_token=[hidden]', str(msg))
    msg = re.sub(r'"access_token"\s*:\s*"[^"]+"', '"access_token": "[hidden]"', msg)
    return msg[:300]


async def ensure_token(client):
    """Telegraph API token via api.telegra.ph/createAccount; cached in settings.
    Anonymous uploads were restricted by Telegraph; the token is the fix.
    """
    settings = await db.get_settings()
    token = settings.get("telegraph_token")
    if token:
        return token
    try:
        get = getattr(client, "get", None)
        if get is None:
            raise RuntimeError("no_get_method")
        r = await get(
            "https://api.telegra.ph/createAccount",
            params={"short_name": config.TELEGRAPH_SHORT_NAME,
                    "author_name": config.TELEGRAPH_AUTHOR},
            timeout=15)
        payload = r.json()
        token = (payload.get("result") or {}).get("access_token")
        if r.status_code == 200 and payload.get("ok") and token:
            await db.update_settings({"telegraph_token": token})
            log.info("Telegraph account created; token cached in settings")
            return token
        log.info("Telegraph createAccount rejected: HTTP %s %.120s",
                 r.status_code, payload)
    except Exception as exc:
        log.info("Telegraph createAccount unavailable: %s", type(exc).__name__)
    return None


async def _try_upload(client, url, file_bytes, token=None):
    kwargs = {"files": {"file": ("cover.jpg", file_bytes, "image/jpeg")}, "timeout": 20}
    if token:
        kwargs["data"] = {"access_token": token}
    await _pace()
    try:
        r = await client.post(url, **kwargs)
    except Exception as exc:
        raise UploadError(f"{url}: {type(exc).__name__}") from exc
    try:
        payload = r.json()
    except Exception:
        payload = None
    err_text = str((payload or {}).get("error", "") if isinstance(payload, dict) else "")
    if r.status_code == 429 or "try again later" in err_text.lower():
        raise RateLimited(f"{url.split('?')[0]}: rate limited (try again later)")
    if r.status_code != 200:
        # 400/403/5xx: NOT a dead end — caller tries the next attempt/host.
        raise UploadError(f"{url}: HTTP {r.status_code} {_sanitize(str(payload))[:80]}")
    if isinstance(payload, dict) and payload.get("error"):
        raise UploadError(f"{url}: {_sanitize(payload.get('error'))}")
    if isinstance(payload, list) and payload and payload[0].get("src"):
        direct = urljoin(url, payload[0]["src"])
        if _http_url(direct):
            return direct
    raise UploadError(f"{url}: unexpected response {str(payload)[:80]}")


async def _validate(client, url):
    """Advisory only: upload response is authoritative; Telegram fetches the URL
    itself, so a blocked HEAD from Render must not discard a working link."""
    try:
        r = await client.head(url, timeout=10, follow_redirects=True)
        ok = r.status_code in (200, 206) and \
            "image" in (r.headers.get("content-type") or "")
        if not ok and r.status_code == 405:
            r = await client.get(url, timeout=10,
                                 headers={"Range": "bytes=0-0"})
            ok = r.status_code in (200, 206) and \
                "image" in (r.headers.get("content-type") or "")
        if not ok:
            log.info("cover URL HEAD check inconclusive (kept anyway): %s", url)
        return ok
    except Exception:
        return False


async def host_cover(client, file_bytes, token=None):
    """telegra.ph (token form, then anonymous) -> graph.org mirror. Paced.
    Returns {"url", "host", "status", "error"}; status may be "rate_limited".
    Never raises. Errors are sanitized (no tokens).
    """
    errors = []
    for host, base in (("telegra.ph", config.COVER_PRIMARY_HOST),
                       ("graph.org", config.COVER_FALLBACK_HOST)):
        # /upload accepts anonymous multipart only; an access_token form field
        # makes Telegraph return HTTP 400. Token belongs to api.telegra.ph/*.
        attempts = [(base, None)]
        for url, tok in attempts:
            try:
                direct = await _try_upload(client, url, file_bytes, token=tok)
                return {"url": direct, "host": host, "status": "ok", "error": None}
            except RateLimited as exc:
                errors.append(str(exc))
                return {"url": None, "host": None, "status": "rate_limited",
                        "error": "; ".join(_sanitize(e) for e in errors)}
            except UploadError as exc:
                errors.append(str(exc))
            except Exception as exc:
                errors.append(f"{url}: {type(exc).__name__}")
    return {"url": None, "host": None, "status": "failed",
            "error": "; ".join(_sanitize(e) for e in errors) or "mirrors_unavailable"}


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
    cover = collection.get("cover") or {}
    if not force and cover.get("url_status") == "ok" and cover.get("url"):
        return {"hosted": True, "ready": True, "cached": True}
    token = await ensure_token(http)
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
    hosted = await host_cover(http, data, token=token)
    update = {"cover.asset_keys": keys, "cover.last_attempt_at": db.now_ts()}
    if hosted["status"] == "rate_limited":
        # Stay pending so a later /retry_covers (after cooldown) picks it up.
        update.update({"cover.url_status": "pending",
                       "cover.last_error": hosted["error"]})
        await db.col("collections").update_one(
            {"collection_id": collection["collection_id"]}, {"$set": update})
        return {"hosted": False, "ready": True, "rate_limited": True}
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
    return {"hosted": hosted["status"] == "ok", "ready": True}


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
        {"$or": [{"cover.url_status": {"$ne": "ok"}},
                 {"cover.url": None}, {"cover.url": ""}]})
