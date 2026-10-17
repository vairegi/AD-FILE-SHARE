"""Async MongoDB (Motor) data layer for Bot 3. Independent of Bots 1-2."""
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import config

_db = None
_client = None

IST = timezone(timedelta(hours=5, minutes=30))


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def ist_today(offset_days: int = 0) -> str:
    d = datetime.now(IST).date() - timedelta(days=offset_days)
    return d.isoformat()


async def connect():
    global _db, _client
    if _db is not None:
        return _db
    if not config.MONGO_URI:
        raise RuntimeError("MONGO_URI not configured")
    import motor.motor_asyncio as motor
    _client = motor.AsyncIOMotorClient(config.MONGO_URI)
    _db = _client[config.MONGO_DB_NAME]
    await _db.users.create_index("user_id", unique=True)
    await _db.collections.create_index([("category", 1), ("posted_at", -1)])
    await _db.collections.create_index("status")
    await _db.unlocks.create_index(
        [("user_id", 1), ("collection_id", 1)], unique=True)
    await _db.bookmarks.create_index(
        [("user_id", 1), ("collection_id", 1)], unique=True)
    await _db.transactions.create_index([("user_id", 1), ("at", -1)])
    await _db.task_runs.create_index([("user_id", 1), ("task_id", 1), ("at", -1)])
    await _db.checkins.create_index([("user_id", 1), ("date", -1)])
    await _db.referrals.create_index("friend_id", unique=True)
    await _db.referrals.create_index("inviter_id")
    await _db.deliveries.create_index("auto_delete_at")
    return _db


def use_test_client(client, db_name: str):
    """Tests inject a mongomock-motor client here (sync, no network)."""
    global _db, _client
    _client = client
    _db = client[db_name]


def col(name: str):
    if _db is None:
        raise RuntimeError("db not connected")
    return _db[name]


# ── users / tokens ───────────────────────────────────────────────
async def get_user(user_id: int) -> Optional[dict]:
    return await col("users").find_one({"user_id": user_id})


async def create_user(user_id: int, username=None, first_name=None) -> dict:
    doc = {"user_id": user_id, "username": username, "first_name": first_name,
           "tokens": 0, "created_at": now_ts(), "welcomed": False}
    try:
        await col("users").insert_one(doc)
        return doc
    except Exception:
        return (await get_user(user_id)) or doc


async def add_tokens(user_id: int, amount: int, reason: str,
                     meta: Optional[dict] = None) -> int:
    await col("users").update_one({"user_id": user_id},
                                  {"$inc": {"tokens": int(amount)}},
                                  upsert=True)
    await col("transactions").insert_one(
        {"user_id": user_id, "amount": int(amount), "reason": reason,
         "meta": meta or {}, "at": now_ts()})
    u = await get_user(user_id)
    return int((u or {}).get("tokens", 0))


async def try_spend_tokens(user_id: int, amount: int, reason: str,
                           meta: Optional[dict] = None) -> bool:
    """Atomic check-and-debit. False if balance too low."""
    from pymongo import ReturnDocument
    doc = await col("users").find_one_and_update(
        {"user_id": user_id, "tokens": {"$gte": int(amount)}},
        {"$inc": {"tokens": -int(amount)}},
        return_document=ReturnDocument.AFTER)
    if not doc:
        return False
    await col("transactions").insert_one(
        {"user_id": user_id, "amount": -int(amount), "reason": reason,
         "meta": meta or {}, "at": now_ts()})
    return True


async def list_transactions(user_id: int, limit: int = 50) -> List[dict]:
    cur = col("transactions").find({"user_id": user_id}).sort("at", -1).limit(limit)
    return [d async for d in cur]


# ── unlocks / bookmarks (vault) ─────────────────────────────────
async def insert_unlock(user_id: int, collection_id: str) -> bool:
    """True if this call created the unlock (unique). False if existed."""
    try:
        await col("unlocks").insert_one(
            {"user_id": user_id, "collection_id": collection_id,
             "at": now_ts()})
        return True
    except Exception:
        return False


async def is_unlocked(user_id: int, collection_id: str) -> bool:
    return bool(await col("unlocks").find_one(
        {"user_id": user_id, "collection_id": collection_id}))


async def list_unlocks(user_id: int) -> List[dict]:
    cur = col("unlocks").find({"user_id": user_id}).sort("at", -1)
    return [d async for d in cur]


async def count_unlocks(user_id: int) -> int:
    return await col("unlocks").count_documents({"user_id": user_id})


async def toggle_bookmark(user_id: int, collection_id: str) -> bool:
    """Returns True if bookmark was added, False if removed."""
    if await col("bookmarks").find_one(
            {"user_id": user_id, "collection_id": collection_id}):
        await col("bookmarks").delete_one(
            {"user_id": user_id, "collection_id": collection_id})
        return False
    try:
        await col("bookmarks").insert_one(
            {"user_id": user_id, "collection_id": collection_id,
             "at": now_ts()})
        return True
    except Exception:
        return True


async def count_bookmarks(user_id: int) -> int:
    return await col("bookmarks").count_documents({"user_id": user_id})


# ── collections (scanner output) ────────────────────────────────
async def upsert_collection(doc: dict) -> None:
    cid = doc["collection_id"]
    await col("collections").update_one(
        {"collection_id": cid}, {"$set": doc}, upsert=True)


async def get_collection(collection_id: str) -> Optional[dict]:
    return await col("collections").find_one({"collection_id": collection_id})


async def list_collections(category: Optional[str] = None,
                           status: str = "active", skip: int = 0,
                           limit: int = 1) -> List[dict]:
    q: Dict[str, Any] = {"status": status}
    if category:
        q["category"] = category
    cur = (col("collections").find(q)
           .sort("posted_at", -1).skip(skip).limit(limit))
    return [d async for d in cur]


async def count_collections(category: Optional[str] = None,
                            status: str = "active") -> int:
    q: Dict[str, Any] = {"status": status}
    if category:
        q["category"] = category
    return await col("collections").count_documents(q)


async def list_categories() -> List[str]:
    return await col("collections").distinct("category", {"status": "active"})


async def bump_collection(collection_id: str, field: str, amount: int = 1):
    await col("collections").update_one(
        {"collection_id": collection_id}, {"$inc": {field: amount}})


# ── checkins (IST calendar days) ────────────────────────────────
async def get_last_checkin(user_id: int) -> Optional[dict]:
    return await col("checkins").find_one(
        {"user_id": user_id}, sort=[("date", -1)])


async def record_checkin(user_id: int, date: str, streak: int, reward: int):
    await col("checkins").insert_one(
        {"user_id": user_id, "date": date, "streak": streak,
         "reward": reward, "at": now_ts()})


# ── sponsor tasks ────────────────────────────────────────────────
async def get_last_task_run(user_id: int, task_id: str) -> Optional[dict]:
    return await col("task_runs").find_one(
        {"user_id": user_id, "task_id": task_id}, sort=[("at", -1)])


async def record_task_run(user_id: int, task_id: str, reward: int):
    await col("task_runs").insert_one(
        {"user_id": user_id, "task_id": task_id, "reward": reward,
         "at": now_ts()})


# ── referrals ────────────────────────────────────────────────────
async def create_referral(inviter_id: int, friend_id: int) -> bool:
    if inviter_id == friend_id:
        return False
    try:
        await col("referrals").insert_one(
            {"inviter_id": inviter_id, "friend_id": friend_id,
             "status": "pending", "notified": False, "at": now_ts()})
        return True
    except Exception:
        return False


async def get_referral(friend_id: int) -> Optional[dict]:
    return await col("referrals").find_one({"friend_id": friend_id})


async def qualify_referral(friend_id: int) -> Optional[dict]:
    """pending -> qualified exactly once. Returns the doc if THIS call
    qualified it (caller credits + notifies), else None."""
    from pymongo import ReturnDocument
    return await col("referrals").find_one_and_update(
        {"friend_id": friend_id, "status": "pending"},
        {"$set": {"status": "qualified", "qualified_at": now_ts()}},
        return_document=ReturnDocument.AFTER)


async def mark_referral_notified(friend_id: int):
    await col("referrals").update_one(
        {"friend_id": friend_id}, {"$set": {"notified": True}})


async def referral_stats(inviter_id: int) -> Dict[str, int]:
    total = await col("referrals").count_documents({"inviter_id": inviter_id})
    qualified = await col("referrals").count_documents(
        {"inviter_id": inviter_id, "status": "qualified"})
    return {"friends": qualified, "earned": qualified * config.REFERRAL_INVITER_BONUS,
            "pending": total - qualified}


# ── deliveries + auto-delete queue ───────────────────────────────
async def create_delivery(doc: dict) -> str:
    await col("deliveries").insert_one(doc)
    return doc["delivery_id"]


async def get_delivery(delivery_id: str) -> Optional[dict]:
    return await col("deliveries").find_one({"delivery_id": delivery_id})


async def update_delivery(delivery_id: str, fields: dict):
    await col("deliveries").update_one(
        {"delivery_id": delivery_id}, {"$set": fields})


async def due_deletions(now: Optional[float] = None) -> List[dict]:
    cur = col("deliveries").find(
        {"auto_delete_at": {"$lte": now if now is not None else now_ts()},
         "delete_status": "pending"})
    return [d async for d in cur]


async def mark_delete_status(delivery_id: str, status: str):
    await col("deliveries").update_one(
        {"delivery_id": delivery_id}, {"$set": {"delete_status": status}})


# ── settings / channel mappings ──────────────────────────────────
async def get_settings() -> dict:
    return (await col("settings").find_one({"_id": "main"})) or {}


async def update_settings(fields: dict):
    await col("settings").update_one({"_id": "main"}, {"$set": fields},
                                     upsert=True)


async def required_channels() -> List[int]:
    s = await get_settings()
    ids = s.get("required_channel_ids")
    if ids is not None:
        return [int(x) for x in ids]
    return list(config.REQUIRED_CHANNEL_IDS)


async def upsert_channel_mapping(channel_id: int, category: str):
    await col("channel_map").update_one(
        {"channel_id": int(channel_id)},
        {"$set": {"channel_id": int(channel_id), "category": category}},
        upsert=True)


async def remove_channel_mapping(channel_id: int):
    await col("channel_map").delete_one({"channel_id": int(channel_id)})


async def list_channel_mappings() -> List[dict]:
    cur = col("channel_map").find({}).sort("category", 1)
    return [d async for d in cur]


async def category_for_channel(channel_id: int) -> Optional[str]:
    d = await col("channel_map").find_one({"channel_id": int(channel_id)})
    return d.get("category") if d else None
