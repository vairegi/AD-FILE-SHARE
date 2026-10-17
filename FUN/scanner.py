"""Streaming source scanner. Stable cover-derived IDs; album-aware boundaries.
Incremental scans re-read the last cover so appended files stay attached.
No source messages, purchases, bookmarks or owner price overrides are deleted.
"""
import logging
from typing import Optional
import db
from classifier import classify
from locks import channel_lock
log = logging.getLogger("fun.scanner")


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


async def scan_channel(client, channel_id: int, category: str,
                       limit: Optional[int] = None, full=False):
    async with channel_lock(channel_id):
        state = await db.col("scan_state").find_one({"_id": int(channel_id)}) or {}
        start = 0 if full else int(state.get("last_cover_id") or 0)
        # Migrate a channel previously indexed by v1 without a last-cover cursor.
        if not full and not start:
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
                current_id = doc["collection_id"]
                existing = await db.get_collection(current_id)
                if existing:
                    # Preserve all file links, counters and owner overrides.
                    current_files = {f["message_id"] for f in existing.get("files", [])}
                    await db.col("collections").update_one({"collection_id": current_id},
                        {"$set": {"category": category, "title": doc["title"]}})
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

        async for message in client.iter_messages(int(channel_id),
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
            "category": category, "posts_scanned": scanned}}, upsert=True)
        return {"collections": len(touched), "pending_files": pending_count,
                "posts_scanned": scanned}
