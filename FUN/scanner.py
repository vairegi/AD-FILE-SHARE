"""Telethon userbot scanner for Bot 3.

Reads a mapped database channel's history (oldest first), groups albums,
runs the cover classifier, and upserts collections into MongoDB. Re-scans
are incremental (state cursor) and never erase existing unlocks/bookmarks —
collection_ids are deterministic (channel_id + cover message_id).
"""
import logging
from typing import Optional, List, Dict, Any

import config
import db
from classifier import classify

log = logging.getLogger("fun.scanner")


def _post_from_message(m) -> Optional[Dict[str, Any]]:
    media_type = None
    duration = None
    if getattr(m, "photo", None):
        media_type = "photo"
    elif getattr(m, "video", None):
        media_type = "video"
        duration = getattr(m.video, "duration", None)
    elif getattr(m, "document", None):
        media_type = "document"
    if media_type is None and not (getattr(m, "message", None) or "").strip():
        return None
    return {
        "message_id": m.id,
        "grouped_id": getattr(m, "grouped_id", None),
        "media_type": media_type,
        "caption": getattr(m, "message", None),
        "duration": duration,
        "date_ts": m.date.timestamp() if getattr(m, "date", None) else 0,
    }


async def scan_channel(client, channel_id: int, category: str,
                       limit: Optional[int] = None) -> Dict[str, int]:
    """Full/incremental scan. `client` is a connected Telethon client."""
    state = (await db.get_settings()).get("scan_state", {})
    cursor = state.get(str(channel_id), 0)

    posts: List[Dict[str, Any]] = []
    async for m in client.iter_messages(int(channel_id), min_id=cursor,
                                        limit=limit, reverse=True):
        p = _post_from_message(m)
        if p:
            posts.append(p)

    result = classify(posts, channel_id, category)
    for c in result["collections"]:
        existing = await db.get_collection(c["collection_id"])
        if existing:
            # incremental: merge new files only, preserve views/unlocks/price
            known = {f["message_id"] for f in existing.get("files", [])}
            new_files = [f for f in c["files"] if f["message_id"] not in known]
            if new_files:
                await db.col("collections").update_one(
                    {"collection_id": c["collection_id"]},
                    {"$push": {"files": {"$each": new_files}},
                     "$inc": {"file_count": len(new_files)}})
        else:
            await db.upsert_collection(c)

    if result["pending"]:
        await db.update_settings(
            {"pending_groups": [
                {"desc": f"channel {channel_id}: {len(result['pending'])} "
                         f"orphan file(s) before first cover",
                 "channel_id": channel_id}]})

    if posts:
        state[str(channel_id)] = max(p["message_id"] for p in posts)
        await db.update_settings({"scan_state": state})

    return {"collections": len(result["collections"]),
            "pending_files": len(result["pending"]),
            "posts_scanned": len(posts)}
