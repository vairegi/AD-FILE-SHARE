"""Public, opaque cover URLs backed by the existing Mongo cache.
Only prepared JPEG previews are exposed. No sessions, source IDs or delivery media.
Navigation reads metadata only; Telegram fetches preview bytes separately.
"""
import html
import re
import secrets
from urllib.parse import urlsplit
import config
import db
import covers

TOKEN_RE = re.compile(r"[a-f0-9]{48}\Z")

def public_base():
    value = config.RENDER_EXTERNAL_URL.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return value

def valid_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in ("https", "http") and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False

async def resolve_url(collection):
    cover = collection.get("cover") or {}
    for url in [cover.get("url"), *(cover.get("urls") or [])]:
        if valid_url(url):
            return url
    base = public_base()
    if not base:
        return None
    cached = cover.get("link_preview_url")
    if isinstance(cached, str) and cached.startswith(base + "/previews/") and TOKEN_RE.fullmatch(cached.rsplit("/", 1)[-1]):
        return cached
    keys = cover.get("asset_keys") or []
    if not keys and collection.get("channel_id") and cover.get("message_id"):
        keys = [covers.asset_key(collection["channel_id"], cover["message_id"])]
    for key in keys:
        # Exclude data: navigation must not transfer JPEGs from Mongo to Python.
        async with covers._lock("public:" + key):
            asset = await db.col("cover_assets").find_one(
                {"_id": key, "data": {"$exists": True}}, {"data": 0})
            if not asset:
                continue
            token = asset.get("public_token")
            if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
                token = secrets.token_hex(24)
                await db.col("cover_assets").update_one({"_id": key}, {"$set": {"public_token": token}})
        url = base + "/previews/" + token
        if collection.get("collection_id"):
            await db.col("collections").update_one({"collection_id": collection["collection_id"]},
                {"$set": {"cover.link_preview_url": url, "cover.link_preview_host": "render"}})
        cover["link_preview_url"] = url
        return url
    return None

async def backfill_urls():
    """Metadata-only upgrade, no mirror calls, uploads or source downloads."""
    prepared = 0
    async for collection in db.col("collections").find({}, {"cover": 1, "channel_id": 1, "collection_id": 1}):
        if await resolve_url(collection):
            prepared += 1
    return prepared

async def preview_asset(token, include_data=False):
    if not TOKEN_RE.fullmatch(token):
        return None
    projection = {"data": 1, "sha256": 1} if include_data else {"_id": 1}
    return await db.col("cover_assets").find_one({"public_token": token, "data": {"$exists": True}}, projection)

def preview_html(token):
    base = public_base()
    if not base:
        return None
    image = html.escape(base + "/previews/" + token + ".jpg", quote=True)
    page = html.escape(base + "/previews/" + token, quote=True)
    return ('<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex,nofollow,noarchive">'
        '<meta property="og:type" content="website">'
        '<meta property="og:title" content="Cover preview">'
        '<meta property="og:description" content=" ">'
        '<meta property="og:url" content="' + page + '">'
        '<meta property="og:image" content="' + image + '">'
        '<meta property="og:image:type" content="image/jpeg">'
        '<meta name="twitter:card" content="summary_large_image">'
        '<meta name="referrer" content="no-referrer">'
        '<title>Cover preview</title></head><body>'
        '<img src="' + image + '" alt="Cover preview"></body></html>')
