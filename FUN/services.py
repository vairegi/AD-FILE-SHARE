"""Delivery service: server-side copy from DB channel in album batches.

copyMessages preserves album grouping and original captions; Telegram caps
albums at 10 items per request. Large videos are never downloaded through
Render — they are copied server-side by file/message reference.
"""
import asyncio
import logging
from typing import List, Tuple, Dict, Any

import config
import db

log = logging.getLogger("fun.services")

# in-process per-user delivery serializer (single-process Render service)
_locks: Dict[int, asyncio.Lock] = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    if user_id not in _locks:
        _locks[user_id] = asyncio.Lock()
    return _locks[user_id]


async def deliver_collection(bot, user_id: int, collection: dict,
                             files: List[Dict[str, Any]]) -> Tuple[List[int], List[Dict[str, Any]]]:
    """Copy deliverable files (covers already excluded by the classifier) in
    batches of <= ALBUM_BATCH_SIZE. Returns (sent_message_ids, failed_files).
    Serialized per user so two taps can never interleave deliveries."""
    sent: List[int] = []
    failed: List[Dict[str, Any]] = []
    channel = collection.get("channel_id")
    async with _lock_for(user_id):
        ids = [f.get("message_id") for f in files if f.get("message_id")]
        for i in range(0, len(ids), config.ALBUM_BATCH_SIZE):
            batch = ids[i:i + config.ALBUM_BATCH_SIZE]
            try:
                if len(batch) == 1:
                    m = await bot.copy_message(
                        chat_id=user_id, from_chat_id=channel,
                        message_id=batch[0])
                    sent.append(m.message_id)
                else:
                    msgs = await bot.copy_messages(
                        chat_id=user_id, from_chat_id=channel,
                        message_ids=batch)
                    sent.extend(m.message_id for m in msgs)
            except Exception as exc:
                log.warning("batch %s..%s failed: %s", batch[0], batch[-1], exc)
                # per-file fallback so one bad message doesn't kill the batch
                for mid in batch:
                    try:
                        m = await bot.copy_message(
                            chat_id=user_id, from_chat_id=channel,
                            message_id=mid)
                        sent.append(m.message_id)
                    except Exception as exc2:
                        log.warning("file %s failed: %s", mid, exc2)
                        failed.append({"message_id": mid})
    return sent, failed


async def run_deletion_sweeper(bot):
    """Periodic job: delete delivered files whose 10-minute window expired.
    Persists in MongoDB so restarts/spin-downs don't lose the schedule."""
    due = await db.due_deletions()
    for d in due:
        ok = True
        for mid in d.get("message_ids") or []:
            try:
                await bot.delete_message(chat_id=d["user_id"], message_id=mid)
            except Exception as exc:
                log.info("delete %s/%s: %s", d["user_id"], mid, exc)
                # already deleted / too old -> treat as done, don't retry forever
        await db.mark_delete_status(
            d["delivery_id"], "done" if ok else "partial")
