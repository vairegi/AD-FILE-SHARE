"""Owner-only userbot session onboarding.

Session string is encrypted (Fernet, key from Render env SESSION_ENCRYPTION_KEY
— NOT stored in MongoDB) and persisted in MongoDB. The session is never logged,
never exported, and never shown in admin lists. On boot we load + connect.
"""
import logging
from typing import Optional

import config
import db

log = logging.getLogger("fun.userbot")


def _fernet():
    if not config.SESSION_ENCRYPTION_KEY:
        raise RuntimeError("SESSION_ENCRYPTION_KEY env var not set")
    from cryptography.fernet import Fernet
    return Fernet(config.SESSION_ENCRYPTION_KEY.encode())


def encrypt_session(session_string: str) -> str:
    return _fernet().encrypt(session_string.encode()).decode()


def decrypt_session(blob: str) -> str:
    return _fernet().decrypt(blob.encode()).decode()


async def save_session(session_string: str) -> None:
    await db.update_settings({"userbot_session": encrypt_session(session_string)})


async def load_session() -> Optional[str]:
    s = await db.get_settings()
    blob = s.get("userbot_session")
    if not blob:
        return None
    try:
        return decrypt_session(blob)
    except Exception as exc:
        log.error("session decrypt failed (wrong key?): %s", exc)
        return None


async def remove_session() -> None:
    await db.update_settings({"userbot_session": None})


async def build_client():
    """Return a connected Telethon client from the stored session, or None."""
    session = await load_session()
    if not session:
        return None
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        api_id = int(__import__("os").environ.get("API_ID", "0"))
        api_hash = __import__("os").environ.get("API_HASH", "")
        client = TelegramClient(StringSession(session), api_id, api_hash)
        await client.connect()
        return client
    except Exception as exc:
        log.error("userbot client connect failed: %s", exc)
        return None
