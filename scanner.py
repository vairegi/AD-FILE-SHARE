"""Telethon userbot scanner used by /scandb and /rescandb.

The Bot API cannot read a channel's history, so a user account session
(SESSION_USER) is used to walk the Database Channel from oldest to newest.
Everything it sees is written to the `raw` staging collection, after which
db.rebuild_items() groups messages into items (cover -> videos -> srt).
"""
import logging
import asyncio

_scan_lock = asyncio.Lock()

import config
import db

log = logging.getLogger("scanner")


def classify_message(msg):
    """Classify a Telethon message object."""
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "photo", None):
        return "cover"
    doc = getattr(msg, "document", None)
    if doc is not None:
        mime = (getattr(doc, "mime_type", "") or "")
        if mime.startswith("image/"):
            return "cover"
        # .mkv/.avi/.webm/.mp4 uploaded as FILES arrive as documents, not
        # msg.video — without this they were misread as subtitles and dropped.
        if mime.startswith("video/"):
            return "video"
        return "srt"
    if getattr(msg, "animation", None):
        return "video"
    return None


def classify_from_botapi(msg):
    """Classify a python-telegram-bot Message coming from channel_post."""
    if getattr(msg, "video", None) is not None:
        return "video"
    if getattr(msg, "photo", None):
        return "cover"
    doc = getattr(msg, "document", None)
    if doc is not None:
        mime = (doc.mime_type or "")
        if mime.startswith("image/"):
            return "cover"
        if mime.startswith("video/"):
            return "video"
        return "srt"
    if getattr(msg, "animation", None) is not None:
        return "video"
    return None


async def scan_channel(channel_id, category=None, progress=None):
    if _scan_lock.locked():
        raise RuntimeError('A history scan is already running. Wait for it to finish; live posts remain durably staged.')
    async with _scan_lock:
        return await _scan_channel(channel_id, category, progress)


async def _scan_channel(channel_id, category=None, progress=None):
    """Explicit history scan with a frozen high-water mark and 100-row batches.
    No video downloads. Newer live source revisions always win. Reconciliation
    streams one atomic group at a time, never materializes the full history.
    """
    import asyncio
    import root_index
    if not (config.API_ID and config.API_HASH and config.SESSION_USER):
        raise RuntimeError("Telethon credentials are missing. Set API_ID, API_HASH and SESSION_USER.")
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(config.SESSION_USER), config.API_ID, config.API_HASH)
    scanned, high, batch, ranks = 0, 0, [], []
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await asyncio.wait_for(client.is_user_authorized(), timeout=15):
            raise RuntimeError("The Telethon session is not authorized. Regenerate SESSION_USER.")
        latest = await asyncio.wait_for(client.get_messages(channel_id, limit=1), timeout=45)
        target = latest[0].id if latest else 0
        if not target:
            return {"scanned": 0, "items": 0, "genres": 0}
        iterator = client.iter_messages(channel_id, reverse=True, max_id=target + 1).__aiter__()
        while True:
            try:
                msg = await asyncio.wait_for(anext(iterator), timeout=45)
            except StopAsyncIteration:
                break
            kind = classify_message(msg) or 'skip'
            entry = {"message_id": msg.id, "kind": kind, "caption": msg.message or ""}
            if kind == 'cover' and getattr(msg, 'photo', None):
                try:
                    from telethon.utils import pack_bot_file_id
                    entry['file_id'] = pack_bot_file_id(msg.photo)
                except Exception:
                    pass
            date = getattr(msg, 'edit_date', None) or getattr(msg, 'date', None)
            batch.append(entry)
            ranks.append(root_index.source_rank(date.timestamp() if date else 0, live=False))
            high = max(high, msg.id)
            scanned += kind != 'skip'
            if len(batch) == 100:
                await root_index.stage(batch, category, ranks=ranks)
                batch, ranks = [], []
                if progress:
                    await progress(scanned)
        if batch:
            await root_index.stage(batch, category, ranks=ranks)
        # A historical read reaching today's tail does NOT prove the upload
        # of its final cover is finished. Only next-cover or owner seal does.
        async with root_index.category_lock(category):
            await db._db.root_index_state.update_one({'_id': db._norm_key(category)},
                {'$max': {'trusted_through': high}})
        items = await root_index.rebuild(category, end_id=high, include_final=False)
        # Individual committed covers match genres incrementally. Do not load
        # every file caption into RAM for a redundant genre rebuild.
        return {"scanned": scanned, "items": items, "genres": 0}
    finally:
        await client.disconnect()
