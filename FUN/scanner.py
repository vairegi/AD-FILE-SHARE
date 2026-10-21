"""Streaming source scanner. Stable cover-derived IDs; album-aware boundaries.
Incremental scans re-read the last cover so appended files stay attached.
No source messages, purchases, bookmarks or owner price overrides are deleted.
"""
import logging
from typing import Optional
import db
from classifier import classify
from locks import channel_lock
import covers
log = logging.getLogger("fun.scanner")


async def _download_bytes(client, channel_id, message_id):
    try:
        return await covers.download_image(client, channel_id, message_id)
    except Exception:
        return None


async def _prepare_cover(client, channel_id, doc, force=False):
    import httpx
    async with httpx.AsyncClient() as http:
        return await covers.prepare_collection(client, doc, http, force=force)


def _post_from_message(m):
    media_type = None
    duration = None
    if getattr(m, "photo", None):
        media_type = "photo"
    elif getattr(m, "video", None):
        media_type = "video"
        # Telethon Document attributes carry the duration, not always .video.duration.
        for attr in getattr(m.video, "attributes", []):
            if hasattr(attr, "duration"):
                duration = attr.duration
                break
    elif getattr(m, "document", None):
        media_type = "document"
    if not media_type:
        return None
    return {"message_id": m.id, "grouped_id": getattr(m, "grouped_id", None),
            "media_type": media_type, "caption": getattr(m, "message", None),
            "duration": duration,
            "date_ts": m.date.timestamp() if getattr(m, "date", None) else 0}


async def existing_cover(doc):
    """Keep an existing album's stable ID when an earlier member arrives late."""
    exact = await db.get_collection(doc['collection_id'])
    if exact:
        return exact
    gid = doc['cover'].get('grouped_id')
    if gid:
        return await db.col('collections').find_one({
            'channel_id': doc['channel_id'], 'cover.grouped_id': str(gid)})
    return None


async def scan_channel(client, channel_id: int, category: str,
                       limit: Optional[int] = None, full=False,
                       source=None, prepare_covers=True):
    async with channel_lock(channel_id):
        state = await db.col("scan_state").find_one({"_id": int(channel_id)}) or {}
        start = 0 if full else int(state.get("last_cover_id") or 0)
        # A live-only index is not proof that older history was scanned. Keep
        # legacy migration, but start at zero for the first historical catch-up
        # after Bot API ingestion (live_scan_state never advances scan_state).
        live_only = not state and await db.col("collections").find_one(
            {"channel_id": int(channel_id), "ingest_source": "bot_api"}, {"_id": 1})
        if not full and not start and not live_only:
            last = await db.col("collections").find_one(
                {"channel_id": int(channel_id)}, sort=[("posted_at", -1)])
            if last:
                start = int((last.get("cover") or {}).get("message_id") or 0)
        current_id = None
        current_files = set()
        scanned, touched, pending_count = 0, set(), 0
        unit, group_key = [], None
        last_seen = int(state.get("cursor") or 0)
        last_cover = start

        async def process(group):
            nonlocal current_id, current_files, last_cover, pending_count
            result = classify(group, channel_id, category)
            if result["collections"]:
                doc = result["collections"][0]
                existing = await existing_cover(doc)
                current_id = existing['collection_id'] if existing else doc['collection_id']
                if existing:
                    # Preserve all file links, counters and owner overrides.
                    current_files = {f["message_id"] for f in existing.get("files", [])}
                    await db.col("collections").update_one({"collection_id": current_id},
                        {"$set": {"category": category, "title": doc["title"],
                                  "cover.grouped_id": doc['cover'].get('grouped_id')},
                         "$min": {"cover.message_id": doc['cover']['message_id']},
                         "$addToSet": {"cover.album_message_ids": {"$each": doc['cover']['album_message_ids']}}})
                else:
                    await db.upsert_collection(doc)
                    current_files = set()
                last_cover = int(doc["cover"]["message_id"])
                touched.add(current_id)
            else:
                files = result["pending"]
                if current_id:
                    new = [f for f in files if f["message_id"] not in current_files]
                    if new:
                        await db.col("collections").update_one({"collection_id": current_id},
                            {"$push": {"files": {"$each": new}}, "$inc": {"file_count": len(new)}})
                        current_files.update(f["message_id"] for f in new)
                        touched.add(current_id)
                else:
                    for f in files:
                        await db.col("pending_files").update_one(
                            {"_id": f"{channel_id}:{f['message_id']}"},
                            {"$set": {"channel_id": int(channel_id), "file": f,
                                      "reason": "media before first identifiable cover"}}, upsert=True)
                        pending_count += 1

        async for message in client.iter_messages(source if source is not None else int(channel_id),
                min_id=max(0, start - 1), limit=limit, reverse=True):
            last_seen = max(last_seen, message.id)
            post = _post_from_message(message)
            if not post:
                continue
            scanned += 1
            key = ("album", post["grouped_id"]) if post.get("grouped_id") else ("single", post["message_id"])
            if unit and key != group_key:
                await process(unit)
                unit = []
            unit.append(post)
            group_key = key
        if unit:
            await process(unit)
        await db.col("scan_state").update_one({"_id": int(channel_id)}, {"$set": {
            "cursor": last_seen, "last_cover_id": last_cover, "last_scan_at": db.now_ts(),
            "category": category, "posts_scanned": scanned,
            "last_error_type": None}}, upsert=True)
        # Host covers for every touched collection (downloads + telegra.ph/graph.org
        # upload happen at scan time; results are cached in MongoDB).
        covers_ok = 0
        previews_ready = 0
        for cid in touched if prepare_covers else []:
            doc = await db.get_collection(cid)
            if doc:
                try:
                    doc.setdefault("channel_id", int(channel_id))
                    result = await _prepare_cover(client, channel_id, doc)
                    covers_ok += int(result["hosted"])
                    previews_ready += int(result["ready"])
                except Exception:
                    log.warning("Cover preparation failed for %s", cid)
        return {"collections": len(touched), "pending_files": pending_count,
                "posts_scanned": scanned, "covers_ready": covers_ok, "previews_ready": previews_ready,
                "cover_collection_ids": sorted(touched)}
