"""MongoDB (Motor) data-access layer shared by Bot 1 and Bot 2.

Multi-category architecture (v2.0)
----------------------------------
The bot runs ANY number of independent content pipelines (categories). Each
category maps ONE Database Channel -> ONE Posting Channel -> (optionally) ONE
Main Posting Channel. Categories are created/edited at runtime through Bot 1
admin commands (zero redeploy) and stored in the `categories` collection.

Collections
-----------
users         : { user_id, verified: {category_key: until}, verified_until
                  (legacy/global) — STATS ONLY since v2.4 (strict per-post
                  verification: these timestamps feed /stats + /categories
                  counts but NEVER grant file access), banned, strikes,
                  auto_delete_override, joined_at }
categories    : { key, label, enabled, db_channel_id, post_channel_id,
                  post_main_channel_id, post_tag, post_time (HH:MM, IST),
                  schedule_enabled, schedule_paused, queue_cursor,
                  force_sub_channel_id (None = use global default),
                  protect_content, auto_delete_minutes, created_at, updated_at }
files         : { file_id (category-prefixed, globally unique), category,
                  db_message_id, cover_message_id, caption, videos[], srts[],
                  posted, created_at, posted_at, post_message_id }
tokens        : { token, user_id, file_id, kind, expires_at, used, created_at }
settings      : { _id: 'global', ... }   single doc (global-only knobs)
raw           : { category, message_id, kind, caption }  staging for the scan
join_requests : { user_id, at }
admins        : { user_id, at }
admin_state   : { user_id, active_category }
deletions     : { chat_id, message_ids[], delete_at }

file_id format
--------------
New items are addressed as "{category}_f{db_message_id}" (e.g. "jav_f123").
Items created before the upgrade keep their legacy "f{db_message_id}" form;
get_item_by_file_id() transparently falls back so old Download buttons in
already-published channel posts keep working forever.
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
    "shortener_api_key": None,   # set via /shortenerapi (DB-first, env fallback)
    "verify_hours": 6,
    "shortener_msg": "🔓 Verification required",
    "shortenerbot_msg": (
        "🔒 To download this file you must complete a quick verification.\n\n"
        "Tap the button below, finish the shortener step, and you'll come "
        "straight back here automatically."
    ),
    "verify_msg": "✅ Verification complete! Tap below to get your file.",
    "shortener_buttons": [],
    "force_sub_channel_id": config.FORCE_SUB_CHANNEL_ID,   # global default
    # legacy single-pipeline knobs, kept only as migration seeds:
    "auto_delete_minutes": 15,
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
    "with_file_message": None,   # custom post-delivery notice (/withfilemessages)
}

# Fields a category document always carries (defaults for /addcategory).
DEFAULT_CATEGORY = {
    "enabled": True,
    "db_channel_id": None,
    "post_channel_id": None,
    "post_main_channel_id": None,
    "post_tag": None,
    "post_time": "18:00",                 # always interpreted as IST
    "schedule_enabled": True,
    "schedule_paused": False,
    "queue_cursor": None,
    "force_sub_channel_id": None,         # None -> use the global default
    "protect_content": False,
    "auto_delete_minutes": 15,
    "with_file_message": None,   # per-pipeline override of the delivery notice
}

CATEGORY_EDITABLE = {
    "db_channel_id", "post_channel_id", "post_main_channel_id", "post_tag",
    "post_time", "force_sub_channel_id", "protect_content",
    "auto_delete_minutes", "with_file_message", "label", "enabled",
}


def now() -> float:
    return time.time()


def _norm_key(key) -> str:
    """Category keys are case-insensitive identifiers (stored lowercase)."""
    return str(key or "").strip().lower()


async def connect():
    """Open the shared connection, ensure indexes/settings, run migration."""
    global _client, _db
    _client = AsyncIOMotorClient(config.MONGO_URI)
    _db = _client[config.MONGO_DB_NAME]
    await _db.settings.update_one(
        {"_id": "global"}, {"$setOnInsert": DEFAULT_SETTINGS}, upsert=True
    )
    await _db.users.create_index("user_id", unique=True)
    await _db.files.create_index([("category", 1), ("db_message_id", 1)], unique=True)
    await _db.files.create_index("file_id", unique=True, sparse=True)
    await _db.files.create_index([("category", 1), ("posted", 1)])
    await _db.tokens.create_index("token", unique=True)
    # Drop the legacy single-field unique index on raw.message_id — it collides
    # across categories (every pipeline's DB channel restarts message_id at 1),
    # which crashed the scan with DuplicateKeyError. The compound (category,
    # message_id) index below is the correct unique key.
    try:
        await _db.raw.drop_index("message_id_1")
    except Exception:
        pass  # index already absent
    # Drop ALL legacy single-field unique indexes that predate multi-category —
    # they collide across pipelines because every channel restarts message_id /
    # db_message_id at 1 (this exact bug crashed the scan twice: first on
    # raw.message_id, then on files.db_message_id). Self-healing on startup.
    for coll, idx in (("raw", "message_id_1"),
                      ("files", "db_message_id_1"),
                      ("files", "posted_1")):
        try:
            await _db[coll].drop_index(idx)
        except Exception:
            pass  # index already absent
    await _db.raw.create_index([("category", 1), ("message_id", 1)], unique=True)
    await _db.categories.create_index("key", unique=True)
    await _db.categories.create_index("db_channel_id")
    await _db.join_requests.create_index("user_id", unique=True)
    await _db.admins.create_index("user_id", unique=True)
    await _db.deletions.create_index("delete_at")
    await _db.admin_state.create_index("user_id", unique=True)
    await migrate_to_categories()
    return _db


async def close():
    if _client:
        _client.close()


async def ping():
    await _client.admin.command("ping")


# ── migration: single pipeline -> categories ──────────────────
async def migrate_to_categories():
    """One-time upgrade: if no categories exist but legacy single-pipeline
    settings do, create the first category from them and tag legacy data.

    Existing files/raw docs keep their legacy `f{N}` file_id form (the whole
    lookup chain understands it); they are only tagged with the category key.
    Safe to run on every startup — it is a no-op once categories exist."""
    if await _db.categories.count_documents({}):
        return False
    s = await _db.settings.find_one({"_id": "global"}) or {}
    if not (s.get("db_channel_id") or config.DB_CHANNEL_ID):
        return False  # nothing configured yet -> fresh install, wizard flow
    key = _norm_key(config.MIGRATION_CATEGORY_KEY) or "manga"
    doc = dict(DEFAULT_CATEGORY)
    doc.update({
        "key": key,
        "label": key.capitalize(),
        "db_channel_id": s.get("db_channel_id") or config.DB_CHANNEL_ID,
        "post_channel_id": s.get("post_channel_id") or config.POST_CHANNEL_ID,
        "post_main_channel_id": s.get("post_main_channel_id"),
        "post_tag": s.get("post_tag"),
        "post_time": s.get("post_time") or "18:00",
        "schedule_enabled": bool(s.get("schedule_enabled", True)),
        "schedule_paused": bool(s.get("schedule_paused", False)),
        "queue_cursor": s.get("queue_cursor"),
        "protect_content": bool(s.get("protect_content", False)),
        "auto_delete_minutes": int(s.get("auto_delete_minutes") or 15),
        "created_at": now(),
        "updated_at": now(),
    })
    await _db.categories.insert_one(doc)
    # Tag untagged legacy data (missing OR null category) with this category.
    untagged = {"$or": [{"category": {"$exists": False}}, {"category": None}]}
    await _db.files.update_many(untagged, {"$set": {"category": key}})
    await _db.raw.update_many(untagged, {"$set": {"category": key}})
    return True


# ── settings (global knobs) ───────────────────────────────────
async def get_settings():
    """Global settings, always merged over DEFAULT_SETTINGS so every key is
    present even if the stored doc predates a setting."""
    doc = await _db.settings.find_one({"_id": "global"})
    merged = dict(DEFAULT_SETTINGS)
    if doc:
        merged.update(doc)
    return merged


async def update_settings(fields: dict):
    fields.pop("_id", None)
    await _db.settings.update_one({"_id": "global"}, {"$set": fields}, upsert=True)


# ── categories (the dynamic registry) ─────────────────────────
async def create_category(key, label=None, **fields):
    """Insert a new pipeline. Returns the doc, or None if key already exists."""
    key = _norm_key(key)
    if not key or not key.replace("_", "").isalnum():
        raise ValueError("invalid category key")
    if await _db.categories.find_one({"key": key}):
        return None
    doc = dict(DEFAULT_CATEGORY)
    doc.update({k: v for k, v in fields.items() if k in DEFAULT_CATEGORY})
    doc["key"] = key
    doc["label"] = (label or key).strip() or key
    doc["created_at"] = now()
    doc["updated_at"] = now()
    await _db.categories.insert_one(doc)
    return doc


async def update_category(key, fields: dict):
    fields = {k: v for k, v in fields.items()
              if k in CATEGORY_EDITABLE
              or k in ("schedule_enabled", "schedule_paused", "queue_cursor")}
    fields.pop("key", None)
    fields["updated_at"] = now()
    await _db.categories.update_one({"key": _norm_key(key)}, {"$set": fields})


async def get_category(key):
    return await _db.categories.find_one({"key": _norm_key(key)})


async def list_categories(enabled_only=False):
    q = {"enabled": True} if enabled_only else {}
    cur = _db.categories.find(q).sort("created_at", 1)
    return [d async for d in cur]


async def delete_category(key, purge_data=False):
    """Remove a pipeline. With purge_data=True also drops its files/raw."""
    key = _norm_key(key)
    res = await _db.categories.delete_one({"key": key})
    if purge_data:
        await _db.files.delete_many({"category": key})
        await _db.raw.delete_many({"category": key})
    return res.deleted_count


async def category_for_db_channel(channel_id):
    """Resolve which pipeline owns a given Database Channel id."""
    try:
        channel_id = int(channel_id)
    except (TypeError, ValueError):
        return None
    return await _db.categories.find_one({"db_channel_id": channel_id})


async def count_categories():
    return await _db.categories.count_documents({})


# ── admin UI state (active category per admin, survives restarts) ──
async def set_active_category(user_id, key):
    await _db.admin_state.update_one(
        {"user_id": user_id},
        {"$set": {"active_category": _norm_key(key)}}, upsert=True)


async def get_active_category(user_id):
    doc = await _db.admin_state.find_one({"user_id": user_id})
    key = (doc or {}).get("active_category")
    if key and await _db.categories.find_one({"key": key}):
        return key
    return None


# ── users ─────────────────────────────────────────────────────
async def touch_user(user_id):
    await _db.users.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"joined_at": now(), "banned": False,
                          "auto_delete_override": None, "verified": {}}},
        upsert=True,
    )


async def get_user(user_id):
    return await _db.users.find_one({"user_id": user_id})


async def mark_verified(user_id, category, hours=None):
    """Mark a user verified for ONE category (per-category verification).

    Backward-compatible signature: may be called the NEW way
    ``mark_verified(user_id, category, hours)`` or the LEGACY way
    ``mark_verified(user_id, hours)``. The legacy form writes the global
    ``verified_until`` flag (pre-upgrade behaviour); the new form writes the
    per-category flag and mirrors it to the legacy flag for compatibility."""
    if hours is None:                     # legacy call: (user_id, hours)
        hours = category
        category = None
    until = now() + hours * 3600
    if category:
        await _db.users.update_one(
            {"user_id": user_id},
            {"$set": {f"verified.{_norm_key(category)}": until},
             "$max": {"verified_any_until": until},
             "$setOnInsert": {"joined_at": now(), "banned": False,
                              "auto_delete_override": None}},
            upsert=True,
        )
    else:
        await _db.users.update_one(
            {"user_id": user_id},
            {"$set": {"verified_until": until},
             "$max": {"verified_any_until": until},
             "$setOnInsert": {"joined_at": now(), "banned": False,
                              "auto_delete_override": None}},
            upsert=True,
        )
    return until


async def is_verified(user_id, category) -> bool:
    """True when the user holds an unexpired verification for this category.

    STATS/DIAGNOSTICS ONLY (v2.4+): strict per-post verification means this
    must NEVER be used to skip the shortener gate — doing so was the
    'verified globally' bug. Kept for admin stats and future features.

    Legacy users verified before the upgrade carry a bare ``verified_until``;
    that is honoured for any category so nobody is forced to re-verify."""
    doc = await get_user(user_id)
    if not doc:
        return False
    t = now()
    until = (doc.get("verified") or {}).get(_norm_key(category))
    if until and until > t:
        return True
    legacy = doc.get("verified_until")
    return bool(legacy and legacy > t)


async def add_strike(user_id) -> int:
    """Increment and return the bypass-strike counter for a user (global)."""
    doc = await _db.users.find_one_and_update(
        {"user_id": user_id},
        {"$inc": {"strikes": 1},
         "$setOnInsert": {"joined_at": now()}},
        upsert=True, return_document=True)
    return int((doc or {}).get("strikes") or 1)


async def reset_strikes(user_id):
    await _db.users.update_one({"user_id": user_id},
                               {"$set": {"strikes": 0}})


async def set_banned(user_id, banned: bool):
    await _db.users.update_one(
        {"user_id": user_id},
        {"$set": {"banned": banned, **({"strikes": 0} if not banned else {})},
         "$setOnInsert": {"joined_at": now()}},
        upsert=True,
    )


async def set_user_autodelete(user_id, minutes):
    await _db.users.update_one(
        {"user_id": user_id},
        {"$set": {"auto_delete_override": minutes},
         "$setOnInsert": {"joined_at": now(), "banned": False}},
        upsert=True,
    )


async def count_users():
    return await _db.users.count_documents({})


async def count_verified(category=None):
    """Verified-user count, globally (any category) or for one category."""
    t = now()
    if category:
        return await _db.users.count_documents(
            {f"verified.{_norm_key(category)}": {"$gt": t}})
    return await _db.users.count_documents(
        {"$or": [{"verified_any_until": {"$gt": t}},
                 {"verified_until": {"$gt": t}}]})


async def count_banned():
    return await _db.users.count_documents({"banned": True})


async def all_user_ids():
    cur = _db.users.find({}, {"user_id": 1})
    return [d["user_id"] async for d in cur]


# ── files / posting queue (category-scoped) ───────────────────
def make_file_id(category, db_message_id) -> str:
    return f"{_norm_key(category)}_f{int(db_message_id)}"


async def upsert_item(item: dict):
    """Insert/refresh an item while PRESERVING its posted status."""
    set_fields = {k: v for k, v in item.items() if k not in ("posted", "created_at")}
    await _db.files.update_one(
        {"category": item.get("category"),
         "db_message_id": item["db_message_id"]},
        {"$set": set_fields,
         "$setOnInsert": {"posted": False, "created_at": now()}},
        upsert=True,
    )


async def _cursor_of(category):
    if not category:
        return None
    cat = await get_category(category)
    return (cat or {}).get("queue_cursor")


async def next_unposted(category=None):
    """Lowest db_message_id that has not been posted yet (oldest first)."""
    q = {"posted": False}
    if category:
        q["category"] = _norm_key(category)
        cur = await _cursor_of(category)
        if cur:
            q["db_message_id"] = {"$gte": cur}
    return await _db.files.find_one(q, sort=[("db_message_id", 1)])


async def next_n_queued(category=None, n=10):
    q = {"posted": False}
    if category:
        q["category"] = _norm_key(category)
        cur = await _cursor_of(category)
        if cur:
            q["db_message_id"] = {"$gte": cur}
    return await _db.files.find(q).sort("db_message_id", 1).limit(int(n)).to_list(int(n))


async def mark_posted(db_message_id, post_message_id=None, category=None):
    q = {"db_message_id": db_message_id}
    if category:
        q["category"] = _norm_key(category)
    await _db.files.update_one(
        q,
        {"$set": {"posted": True, "posted_at": now(), "post_message_id": post_message_id}},
    )


async def get_item_by_file_id(file_id):
    """Look up by file_id, tolerating BOTH id styles in either direction.

    Items are stored with category-prefixed ids ("jav_f123"). Buttons posted
    before the upgrade carry legacy bare ids ("f123"). Resolve by trying, in
    order: as-is -> add each known category prefix -> strip a category prefix.
    Every old and new button keeps working against migrated data."""
    s = str(file_id or "").strip()
    if not s:
        return None
    # 1. exact match (new-style ids, or legacy ids on legacy docs)
    item = await _db.files.find_one({"file_id": s})
    if item:
        return item
    # 2. legacy bare id -> try each category prefix (f123 -> jav_f123)
    if s.startswith("f") and "_f" not in s and s[1:].isdigit():
        suffix = s[1:]
        async for cat in _db.categories.find({}, {"key": 1}):
            item = await _db.files.find_one(
                {"file_id": f"{cat['key']}_f{suffix}"})
            if item:
                return item
    # 3. prefixed id -> strip to legacy (jav_f123 -> f123)
    if not s.startswith("f") and "_f" in s:
        legacy = "f" + s.rsplit("_f", 1)[-1]
        return await _db.files.find_one({"file_id": legacy})
    return None


async def resolve_category(file_id):
    """Best-effort category key for a file_id (doc, then id prefix)."""
    item = await get_item_by_file_id(file_id)
    if item and item.get("category"):
        return item["category"]
    s = str(file_id or "")
    if "_f" in s and not s.startswith("f"):
        return s.rsplit("_f", 1)[0]
    return None


async def count_files(category=None):
    q = {"category": _norm_key(category)} if category else {}
    return await _db.files.count_documents(q)


async def count_posted(category=None):
    q = {"posted": True}
    if category:
        q["category"] = _norm_key(category)
    return await _db.files.count_documents(q)


async def count_pending(category=None):
    q = {"posted": False}
    if category:
        q["category"] = _norm_key(category)
    return await _db.files.count_documents(q)


# ── raw staging + grouping (category-scoped) ──────────────────
async def ingest_raw(entry: dict, category=None):
    cat = _norm_key(category or entry.get("category"))
    await _db.raw.update_one(
        {"category": cat, "message_id": entry["message_id"]},
        {"$set": {**entry, "category": cat}}, upsert=True
    )


async def all_raw(category=None):
    q = {"category": _norm_key(category)} if category else {}
    cur = _db.raw.find(q).sort("message_id", 1)
    return [d async for d in cur]


def group_items(raw: list, category=None):
    """Group raw messages into items.

    DB-channel layout: cover post (photo) -> 1-2 videos -> optional .srt.
    Every 'cover' starts a new item; videos/srts attach to the current item.
    Items are ordered by message id, which is naturally sequential.
    """
    cat = _norm_key(category)
    items, current = [], None
    for m in raw:
        kind = m.get("kind")
        if kind == "cover":
            current = {"db_message_id": m["message_id"],
                       "cover_message_id": m["message_id"],
                       "cover_file_id": m.get("file_id"),
                       "caption": m.get("caption") or "",
                       "videos": [], "srts": []}
            items.append(current)
        elif kind == "video":
            if current is None:
                current = {"db_message_id": m["message_id"],
                           "cover_message_id": None,
                           "cover_file_id": None,
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
    if cat:
        for it in items:
            it["category"] = cat
    return items


async def rebuild_items(category=None):
    """Re-derive the files collection from raw staging (keeps posted flags).

    Scoped per category so rescanning one pipeline never disturbs another.
    Self-healing: items whose source messages are no longer in the DB channel
    are REMOVED, so the queue renumbers itself on the next /rescandb."""
    cat = _norm_key(category)
    items = group_items(await all_raw(cat), cat)
    valid_ids = {it["db_message_id"] for it in items}
    base = {"category": cat} if cat else {}
    if valid_ids:
        await _db.files.delete_many({**base, "db_message_id": {"$nin": list(valid_ids)}})
    else:
        await _db.files.delete_many(base)
    for it in items:
        await upsert_item({
            "file_id": make_file_id(cat, it["db_message_id"]) if cat
                       else f"f{it['db_message_id']}",
            "category": cat or it.get("category"),
            "db_message_id": it["db_message_id"],
            "cover_message_id": it["cover_message_id"],
            "cover_file_id": it.get("cover_file_id"),
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


async def list_admin_ids():
    """ENV admins plus every admin promoted via /addadmin."""
    ids = list(config.ADMIN_IDS)
    async for doc in _db.admins.find({}):
        if doc["user_id"] not in ids:
            ids.append(doc["user_id"])
    return ids


# ── auto-delete queue (restart-safe) ──────────────────────────
async def add_deletion(chat_id, message_ids, delete_at):
    await _db.deletions.insert_one({
        "chat_id": chat_id, "message_ids": list(message_ids), "delete_at": delete_at
    })


async def due_deletions():
    cur = _db.deletions.find({"delete_at": {"$lte": now()}})
    return [d async for d in cur]


async def remove_deletion(doc_id):
    await _db.deletions.delete_one({"_id": doc_id})


# ── persisted per-category queue state ────────────────────────
async def queue_summary(category=None, n=10):
    cat = _norm_key(category)
    cur = await _cursor_of(cat) if cat else None
    less = 0
    base = {"category": cat} if cat else {}
    if cur:
        less = await _db.files.count_documents(
            {**base, "posted": False, "db_message_id": {"$lt": cur}})
    items = await next_n_queued(cat, n)
    remaining = await _db.files.count_documents(
        {**base, "posted": False, **({"db_message_id": {"$gte": cur}} if cur else {})})
    return {
        "cursor": cur,
        "position": (less + 1) if cur else 1,
        "remaining": remaining,
        "items": items,
    }


async def queue_reset_to_position(n, category=None):
    """Move cursor to the Nth item (1-indexed over ALL items, db_message_id ASC).
    Flips every earlier still-unposted item to posted. Returns target or None.
    Scoped per category so each pipeline's queue is independent."""
    n = int(n)
    if n < 1:
        return None
    base = {"category": _norm_key(category)} if category else {}
    order = await _db.files.find(base).sort("db_message_id", 1).to_list(None)
    if n > len(order):
        return None
    target = order[n - 1]
    tid = target["db_message_id"]
    await _db.files.update_many(
        {**base, "db_message_id": {"$lt": tid}, "posted": False},
        {"$set": {"posted": True, "posted_at": now()}})
    # Rewind: everything from position N onward becomes unposted again.
    await _db.files.update_many(
        {**base, "db_message_id": {"$gte": tid}},
        {"$set": {"posted": False},
         "$unset": {"posted_at": "", "post_message_id": ""}})
    if category:
        await _db.categories.update_one(
            {"key": _norm_key(category)},
            {"$set": {"queue_cursor": tid, "updated_at": now()}})
    else:
        await _db.settings.update_one(
            {"_id": "global"}, {"$set": {"queue_cursor": tid}}, upsert=True)
    return target


async def clear_queue_cursor(category=None):
    if category:
        await _db.categories.update_one(
            {"key": _norm_key(category)},
            {"$set": {"queue_cursor": None, "updated_at": now()}})
    else:
        await _db.settings.update_one(
            {"_id": "global"}, {"$unset": {"queue_cursor": ""}})
