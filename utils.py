"""Small shared helpers: flexible duration parsing + admin guard."""
import functools
import logging
import re
import time

import config
import db

log = logging.getLogger("utils")

# Human-friendly time units -> seconds
_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def parse_duration(text: str):
    """Parse flexible durations into SECONDS.

    Accepts: '5min', '2hour', '12hour', '7day', '1day', '30m', '90s',
             'never'/'0' (-> 0 = no auto-delete).
    A bare number is interpreted as MINUTES ('30' -> 1800).
    Returns None when the string cannot be understood.
    """
    if text is None:
        return None
    t = str(text).strip().lower().replace(" ", "")
    if not t:
        return None
    if t in ("0", "off", "never", "none"):
        return 0
    if t.isdigit():
        return int(t) * 60  # bare number = minutes
    m = re.fullmatch(r"(\d+)([a-z]+)", t)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    if unit not in _UNITS:
        return None
    return value * _UNITS[unit]


def human_duration(seconds: int) -> str:
    """Turn a seconds count into a readable string like '2 hours 5 minutes'."""
    if not seconds or seconds <= 0:
        return "never (kept forever)"
    parts = []
    for label, size in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        if seconds >= size:
            n, seconds = divmod(seconds, size)
            parts.append(f"{n} {label}{'s' if n != 1 else ''}")
    return " ".join(parts[:2])


def now() -> float:
    return time.time()


async def is_admin(user_id: int) -> bool:
    """Admin if listed in ADMIN_IDS env OR promoted via /addadmin."""
    if user_id in config.ADMIN_IDS:
        return True
    try:
        return await db.is_db_admin(user_id)
    except Exception:  # never let a db hiccup crash a handler
        return False


def admin_only(func):
    """Decorator: silently reject non-admins on admin commands."""
    @functools.wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        user = update.effective_user
        if user and await is_admin(user.id):
            return await func(update, context, *args, **kwargs)
        message = update.effective_message
        if message:
            await message.reply_text(
                "⛔ This command can be used only by admins.")
        return None

    return wrapper
