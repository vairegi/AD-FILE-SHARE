"""Cover-post classifier for Bot 3's channel scanner.

Owner-confirmed rules (from samples):
  * A PHOTO post whose caption has >= 3 non-empty lines  -> COVER.
  * A VIDEO post whose caption has >= 3 non-empty lines  -> COVER
    (duration does NOT matter — covers can be 7s, 10s or 20s clips).
  * A captionless video (even 9s), or a photo with a 0-2 line caption
    -> DELIVERY file, not a cover.
  * Albums (same grouped_id) are judged as ONE unit using the album's
    first non-empty caption.
  * Delivery files after a cover belong to that cover's collection,
    until the next cover appears.
  * Delivery files seen BEFORE any cover -> held as 'pending' for owner
    review (we can't guess their collection).
  * Covers themselves are EXCLUDED from delivered files.
"""
from typing import List, Dict, Any, Optional

COVER_MIN_CAPTION_LINES = 3
MEDIA_TYPES = ("photo", "video", "document", "animation", "audio")


def nonempty_caption_lines(caption: Optional[str]) -> int:
    if not caption:
        return 0
    return sum(1 for ln in str(caption).splitlines() if ln.strip())


def is_cover(media_type: Optional[str], caption: Optional[str]) -> bool:
    return media_type in ("photo", "video") and \
        nonempty_caption_lines(caption) >= COVER_MIN_CAPTION_LINES


def _album_key(post: Dict[str, Any]):
    gid = post.get("grouped_id")
    return ("album", gid) if gid else ("single", post.get("message_id"))


def group_posts(posts: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Collapse consecutive same-album posts into single units, preserving
    chronological order."""
    units: List[List[Dict[str, Any]]] = []
    seen: Dict[Any, int] = {}
    for p in posts:
        key = _album_key(p)
        if key[0] == "album" and key in seen:
            units[seen[key]].append(p)
        else:
            if key[0] == "album":
                seen[key] = len(units)
            units.append([p])
    return units


def _unit_caption(unit: List[Dict[str, Any]]) -> Optional[str]:
    for p in unit:
        if p.get("caption") and str(p["caption"]).strip():
            return p["caption"]
    return None


def _unit_media_type(unit: List[Dict[str, Any]]) -> Optional[str]:
    return unit[0].get("media_type")


def classify(posts: List[Dict[str, Any]], channel_id: int,
             category: str, id_prefix: str = "c") -> Dict[str, Any]:
    """Turn raw scanned posts (oldest first) into collections.

    Returns {"collections": [...], "pending": [...]} — pending are delivery
    files with no cover yet (owner review)."""
    collections: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for unit in group_posts(posts):
        first = unit[0]
        mtype = _unit_media_type(unit)
        caption = _unit_caption(unit)
        if is_cover(mtype, caption):
            cid = f"{id_prefix}{channel_id}_{first.get('message_id')}"
            current = {
                "collection_id": cid,
                "channel_id": int(channel_id),
                "category": category,
                "title": (caption or "").strip().splitlines()[0][:120],
                "cover": {
                    "media_type": mtype,
                    "message_id": first.get("message_id"),
                    "caption": caption,
                    "album_message_ids": [u.get("message_id") for u in unit],
                    "duration": first.get("duration"),
                    "url": None,
                    "url_host": None,
                    "url_status": "pending",
                },
                "files": [],
                "file_count": 0,
                "posted_at": first.get("date_ts") or 0,
                "price": None,  # None -> config.COLLECTION_PRICE at runtime
                "status": "active",
                "views": 0,
                "unlocks": 0,
            }
            collections.append(current)
        else:
            files = [{"message_id": u.get("message_id"),
                      "media_type": u.get("media_type"),
                      "caption": u.get("caption")}
                     for u in unit if u.get("media_type") in MEDIA_TYPES]
            if current is not None:
                current["files"].extend(files)
            else:
                pending.extend(files)

    for c in collections:
        c["file_count"] = len(c["files"])
    return {"collections": collections, "pending": pending}
