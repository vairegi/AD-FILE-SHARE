"""Telethon userbot scanner used by /scandb and /rescandb.

The Bot API cannot read a channel's history, so a user account session
(SESSION_USER) is used to walk the Database Channel from oldest to newest.
Everything it sees is written to the `raw` staging collection, after which
db.rebuild_items() groups messages into items (cover -> videos -> srt).
"""
import logging

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
        return "cover" if mime.startswith("image/") else "srt"
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
        return "cover" if mime.startswith("image/") else "srt"
    if getattr(msg, "animation", None) is not None:
        return "video"
    return None


async def scan_channel(channel_id, category=None, progress=None):
    """Scan a channel's full history into the raw staging collection,
    tagged to a single category so pipelines never mix."""
    if not (config.API_ID and config.API_HASH and config.SESSION_USER):
        raise RuntimeError(
            "Telethon credentials are missing. Set API_ID, API_HASH and SESSION_USER."
        )

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(config.SESSION_USER),
                            config.API_ID, config.API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "The Telethon session is not authorized. Regenerate SESSION_USER."
            )
        scanned = 0
        async for msg in client.iter_messages(channel_id, reverse=True):
            kind = classify_message(msg)
            if not kind:
                continue
            await db.ingest_raw({
                "message_id": msg.id,
                "kind": kind,
                "caption": msg.message or "",
            }, category=category)
            scanned += 1
            if progress and scanned % 200 == 0:
                await progress(scanned)
        items = await db.rebuild_items(category)
        return {"scanned": scanned, "items": items}
    finally:
        await client.disconnect()
