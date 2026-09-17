"""MongoDB (Motor) data-access layer shared by Bot 1 and Bot 2.

Collections
-----------
users         : { user_id, verified_until, banned, auto_delete_override, joined_at }
files         : { file_id, db_message_id, cover_message_id, caption, videos[], srts[],
                  posted, created_at, posted_at, post_message_id }
tokens        : { token, user_id, file_id, kind, expires_at, used, created_at }
settings      : { _id: 'global', ... }          single document
raw           : { message_id, kind, caption }   staging for the DB channel scan
join_requests : { user_id, at }
admins        : { user_id, at }
deletions     : { chat_id, message_ids[], delete_at }
"""
import time
import uuid

from motor.motor_asyncio import AsyncIOMotorClient

import config

_client = None
_db = None

DEFAULT_SETTINGS = {
    "_id": "global",
    "shortener_enabled": False,
    "shortener_api_base": "https://vplink.in/api",
    "verify_hours": 6,
    "shortener_msg": "🔓 Verification required",
    "shortenerbot_msg": (
        "🔒 To download this file you must complete a quick verification.\n\n"
        "Tap the button below, finish the shortener step, and you'll come "
        "straight back here automatically."
    ),
    "verify_msg": "✅ Verification complete! Tap below to get your file.",
    "shortener_buttons": [],
    "force_sub_channel_id": config.FORCE_SUB_CHANNEL_ID,
    "auto_delete_minutes": 60,
    "post_channel_id": config.POST_CHANNEL_ID,
    "db_channel_id": config.DB_CHANNEL_ID,
    "post_time": "18:00",
    "token_ttl_minutes": 10,
    "post_main_channel_id": None,
    "post_tag": None,
    "post_timezone": "Asia/Kolkata",
    "schedule_enabled": True,
    "schedule_paused": False,
    "queue_cursor": None,
    "protect_content": False,
}


def now() -> float:
    return time.time()


async def connect():
    """Open the shared connection and make sure indexes/settings exist."""
    global _client, _db
    _client = AsyncIOMotorClient(config.MONGO_URI)
    _db = _client[config.MONGO_DB_NAME]
    await _db.settings.update_one(
        {"_id": "global"}, {"$setOnInsert": DEFAULT_SETTINGS}, upsert=True
    )
    await _db.users.create_index("user_id", unique=True)
    await _db.files.create_index("db_message_id", unique=True)
    await _db.files.create_index("posted")
    await _db.tokens.create_index("token", unique=True)
    await _db.raw.create_index("message_id", unique=True)
    await _db.join_requests.create_index("user_id", unique=True)
    await _db.admins.create_index("user_id", unique=True)
    await _db.deletions.create_index("delete_at")
    return _db


async def close():
    if _client:
        _client.close()


async def ping():
    await _client.admin.command("ping")


# ── settings ──────────────────────────────────────────────────
async def get_settings():
    doc = await _db.settings.find_one({"_id": "global"})
    return doc or dict(DEFAULT_SETTINGS)


async def update_settings(fields: dict):
    fields.pop("_id", None)
    await _db.settings.update_one({"_id": "global"}, {"$set": fields}, upsert=True)


# ── users ─────────────────────────────────────────────────────
async def touch_user(user_id):
    await _db.users.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"joined_at": now(), "banned": False,
                          "auto_delete_override": None, "verified_until": 0}},
        upsert=True,
    )


async def get_user(user_id):
    return await _db.users.find_one({"user_id": user_id})


async def mark_verified(user_id, hours):
    until = now() + hours * 3600
    await _db.users.update_one(
        {"user_id": user_id},
        {"$set": {"verified_until": until},
         "$setOnInsert": {"joined_at": now(), "banned": False,
                          "auto_delete_override": None}},
        upsert=True,
    )
    return until


async def add_strike(user_id) -> int:
    """Increment and return the bypass-strike counter for a user."""
    doc = await _db.users.find_one_and_update(
        {"user_id": user_id},
        {"$inc": {"strikes": 1},
         "$setOnInsert": {"joined_at": now(), "verified_until": 0}},
        upsert=True, return_document=True)
    return int((doc or {}).get("strikes") or 1)


async def reset_strikes(user_id):
    await _db.users.update_one({"user_id": user_id},
                               {"$set": {"strikes": 0}})


async def set_banned(user_id, banned: bool):
    await _db.users.update_one(
        {"user_id": user_id},
        {"$set": {"banned": banned, **({"strikes": 0} if not banned else {})},
         "$setOnInsert": {"joined_at": now(), "verified_until": 0}},
        upsert=True,
    )


async def set_user_autodelete(user_id, minutes):
    await _db.users.update_one(
        {"user_id": user_id},
        {"$set": {"auto_delete_override": minutes},
         "$setOnInsert": {"joined_at": now(), "banned": False, "verified_until": 0}},
        upsert=True,
    )


async def count_users():
    return await _db.users.count_documents({})


async def count_verified():
    return await _db.users.count_documents({"verified_until": {"$gt": now()}})


async def count_banned():
    return await _db.users.count_documents({"banned": True})


async def all_user_ids():
    cur = _db.users.find({}, {"user_id": 1})
    return [d["user_id"] async for d in cur]


# ── files / posting queue ─────────────────────────────────────
async def upsert_item(item: dict):
    """Insert/refresh an item while PRESERVING its posted status."""
    set_fields = {k: v for k, v in item.items() if k not in ("posted", "created_at")}
    await _db.files.update_one(
        {"db_message_id": item["db_message_id"]},
        {"$set": set_fields,
         "$setOnInsert": {"posted": False, "created_at": now()}},
        upsert=True,
    )


async def next_unposted():
    """Lowest db_message_id that has not been posted yet (oldest first)."""
    return await _db.files.find_one({"posted": False}, sort=[("db_message_id", 1)])


async def mark_posted(db_message_id, post_message_id=None):
    await _db.files.update_one(
        {"db_message_id": db_message_id},
        {"$set": {"posted": True, "posted_at": now(), "post_message_id": post_message_id}},
    )


async def get_item_by_file_id(file_id):
    return await _db.files.find_one({"file_id": file_id})


async def count_files():
    return await _db.files.count_documents({})


async def count_posted():
    return await _db.files.count_documents({"posted": True})


async def count_pending():
    return await _db.files.count_documents({"posted": False})


# ── raw staging + grouping ────────────────────────────────────
async def ingest_raw(entry: dict):
    await _db.raw.update_one(
        {"message_id": entry["message_id"]}, {"$set": entry}, upsert=True
    )


async def all_raw():
    cur = _db.raw.find().sort("message_id", 1)
    return [d async for d in cur]


def group_items(raw: list):
    """Group raw messages into items.

    DB-channel layout: cover post (photo) -> 1-2 videos -> optional .srt.
    Every 'cover' starts a new item; videos/srts attach to the current item.
    Items are ordered by message id, which is naturally sequential.
    """
    items, current = [], None
    for m in raw:
        kind = m.get("kind")
        if kind == "cover":
            current = {"db_message_id": m["message_id"],
                       "cover_message_id": m["message_id"],
                       "caption": m.get("caption") or "",
                       "videos": [], "srts": []}
            items.append(current)
        elif kind == "video":
            if current is None:
                current = {"db_message_id": m["message_id"],
                           "cover_message_id": None,
                           "caption": m.get("caption") or "",
                           "videos": [], "srts": []}
                items.append(current)
            current["videos"].append(
                {"db_message_id": m["message_id"], "caption": m.get("caption") or ""}
            )
        elif kind == "srt" and current is not None:
            current["srts"].append(
                {"db_message_id": m["message_id"], "caption": m.get("caption") or ""}
            )
    return items


async def rebuild_items():
    """Re-derive the files collection from raw staging (keeps posted flags).

    Self-healing: items whose source messages are no longer in the DB channel
    (e.g. a duplicate the owner deleted) are REMOVED, so the queue renumbers
    itself on the next /rescandb or /scandb."""
    items = group_items(await all_raw())
    valid_ids = {it["db_message_id"] for it in items}
    if valid_ids:
        await _db.files.delete_many({"db_message_id": {"$nin": list(valid_ids)}})
    else:
        await _db.files.delete_many({})
    for it in items:
        await upsert_item({
            "file_id": f"f{it['db_message_id']}",
            "db_message_id": it["db_message_id"],
            "cover_message_id": it["cover_message_id"],
            "caption": it["caption"],
            "videos": it["videos"],
            "srts": it["srts"],
        })
    return len(items)


# ── tokens ────────────────────────────────────────────────────
async def create_token(user_id, file_id, ttl_minutes, kind="deliver"):
    token = uuid.uuid4().hex
    await _db.tokens.insert_one({
        "token": token, "user_id": user_id, "file_id": file_id, "kind": kind,
        "expires_at": now() + ttl_minutes * 60, "used": False, "created_at": now(),
    })
    return token


async def get_token(token):
    return await _db.tokens.find_one({"token": token})


async def mark_token_used(token):
    await _db.tokens.update_one(
        {"token": token}, {"$set": {"used": True, "used_at": now()}}
    )


# ── join requests ─────────────────────────────────────────────
async def record_join_request(user_id):
    await _db.join_requests.update_one(
        {"user_id": user_id}, {"$set": {"user_id": user_id, "at": now()}}, upsert=True
    )


async def has_join_request(user_id):
    return await _db.join_requests.find_one({"user_id": user_id}) is not None


# ── admins ────────────────────────────────────────────────────
async def add_db_admin(user_id):
    await _db.admins.update_one(
        {"user_id": user_id}, {"$set": {"user_id": user_id, "at": now()}}, upsert=True
    )


async def is_db_admin(user_id):
    return await _db.admins.find_one({"user_id": user_id}) is not None


# ── auto-delete queue (restart-safe) ──────────────────────────
async def list_admin_ids():
    """ENV admins plus every admin promoted via /addadmin."""
    ids = list(config.ADMIN_IDS)
    async for doc in _db.admins.find({}):
        if doc["user_id"] not in ids:
            ids.append(doc["user_id"])
    return ids


async def add_deletion(chat_id, message_ids, delete_at):
    await _db.deletions.insert_one({
        "chat_id": chat_id, "message_ids": list(message_ids), "delete_at": delete_at
    })


async def due_deletions():
    cur = _db.deletions.find({"delete_at": {"$lte": now()}})
    return [d async for d in cur]


async def remove_deletion(doc_id):
    await _db.deletions.delete_one({"_id": doc_id})


# === v1.3 : persisted schedule / queue state ===

async def next_unposted():
    """Lowest unposted db_message_id, honouring queue_cursor if set."""
    s = await get_settings()
    q = {"posted": False}
    cur = (s or {}).get("queue_cursor")
    if cur:
        q["db_message_id"] = {"$gte": cur}
    return await _db.files.find_one(q, sort=[("db_message_id", 1)])


async def next_n_queued(n=10):
    s = await get_settings()
    q = {"posted": False}
    cur = (s or {}).get("queue_cursor")
    if cur:
        q["db_message_id"] = {"$gte": cur}
    return await _db.files.find(q).sort("db_message_id", 1).limit(int(n)).to_list(int(n))


async def queue_summary(n=10):
    s = await get_settings()
    cur = (s or {}).get("queue_cursor")
    less = 0
    if cur:
        less = await _db.files.count_documents(
            {"posted": False, "db_message_id": {"$lt": cur}})
    items = await next_n_queued(n)
    remaining = await _db.files.count_documents(
        {"posted": False, **({"db_message_id": {"$gte": cur}} if cur else {})})
    return {
        "cursor": cur,
        "position": (less + 1) if cur else 1,
        "remaining": remaining,
        "items": items,
    }


async def queue_reset_to_position(n):
    """Move cursor to the Nth item (1-indexed over ALL items, db_message_id ASC).
    Flips every earlier still-unposted item to posted. Returns target or None."""
    n = int(n)
    if n < 1:
        return None
    order = await _db.files.find({}).sort("db_message_id", 1).to_list(None)
    if n > len(order):
        return None
    target = order[n - 1]
    tid = target["db_message_id"]
    await _db.files.update_many(
        {"db_message_id": {"$lt": tid}, "posted": False},
        {"$set": {"posted": True, "posted_at": now()}})
    # Rewind: everything from position N onward becomes unposted again, so
    # /dripnow and the daily job restart exactly at post number N.
    await _db.files.update_many(
        {"db_message_id": {"$gte": tid}},
        {"$set": {"posted": False},
         "$unset": {"posted_at": "", "post_message_id": ""}})
    await _db.settings.update_one(
        {"_id": "global"}, {"$set": {"queue_cursor": tid}}, upsert=True)
    return target


async def clear_queue_cursor():
    await _db.settings.update_one(
        {"_id": "global"}, {"$unset": {"queue_cursor": ""}})
