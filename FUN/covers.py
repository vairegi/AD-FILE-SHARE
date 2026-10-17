"""Cover-host utility: telegra.ph then graph.org. Retained for integration.
This helper alone does not implement a live Telegram preview pipeline.
URLs must return 200 with an image Content-Type before being accepted.
"""
import logging
from typing import Optional, Dict, Any

import config

log = logging.getLogger("fun.covers")


async def _try_upload(client, url: str, file_bytes: bytes) -> Optional[str]:
    try:
        resp = await client.post(url, files={"file": ("cover.jpg", file_bytes, "image/jpeg")}, timeout=30)
        if resp.status_code != 200:
            return None
        data = resp.json()
        # telegraph upload returns [{"src": "/file/xxx.jpg"}]
        if isinstance(data, list) and data and data[0].get("src"):
            base = "https://telegra.ph" if "telegra.ph" in url else "https://graph.org"
            return base + data[0]["src"]
    except Exception as exc:
        log.info("cover upload to %s failed: %s", url, exc)
    return None


async def _validate(client, url: str) -> bool:
    try:
        r = await client.head(url, timeout=15, follow_redirects=True)
        ctype = r.headers.get("content-type", "")
        return r.status_code == 200 and ctype.lower().startswith("image/")
    except Exception:
        return False


async def host_cover(client, file_bytes: bytes) -> Dict[str, Any]:
    """Upload to primary host, then fallback host. Returns
    {"url": ..., "host": "telegra.ph"|"graph.org"|None, "status": "ok"|"failed"}."""
    for name, endpoint in (("telegra.ph", config.COVER_PRIMARY_HOST),
                           ("graph.org", config.COVER_FALLBACK_HOST)):
        url = await _try_upload(client, endpoint, file_bytes)
        if url and await _validate(client, url):
            return {"url": url, "host": name, "status": "ok"}
    return {"url": None, "host": None, "status": "failed"}


async def prepare_cover_for_collection(collection: dict, client,
                                       download_bytes) -> dict:
    """Fill collection['cover'] URL fields. download_bytes(message_id) ->
    bytes for the cover's image/thumbnail. On total failure we keep
    url_status='failed' so a future renderer can choose a fallback."""
    cover = collection.get("cover") or {}
    if cover.get("url_status") == "ok" and cover.get("url"):
        return collection  # already prepared — never re-upload per tap
    try:
        data = await download_bytes(cover.get("message_id"))
        if data:
            result = await host_cover(client, data)
            cover.update({"url": result["url"], "url_host": result["host"],
                          "url_status": result["status"]})
        else:
            cover["url_status"] = "failed"
    except Exception as exc:
        log.warning("cover prep failed for %s: %s",
                    collection.get("collection_id"), exc)
        cover["url_status"] = "failed"
    collection["cover"] = cover
    return collection
