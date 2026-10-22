"""Persistent auto-delete policy for NEW file deliveries.
Existing scheduled messages keep their original deadlines; settings never
retroactively destroy delivered files or reactivate disabled deletions.
"""
import re
import config
import db

MAX_SECONDS = 24 * 60 * 60


def parse_duration(value):
    value = value.strip().lower()
    if value == 'off':
        return 0
    match = re.fullmatch(r'([1-9][0-9]{0,5})(s|m|h)', value)
    if not match:
        raise ValueError('Use a duration such as 1m, 15m, 30m, 1h, or off.')
    seconds = int(match[1]) * {'s': 1, 'm': 60, 'h': 3600}[match[2]]
    if not 1 <= seconds <= MAX_SECONDS:
        raise ValueError('Duration must be between 1 second and 24 hours, or off.')
    return seconds


async def current_seconds():
    settings = await db.get_settings()
    value = settings.get('autodlt_seconds', config.AUTO_DELETE_AFTER_SECONDS)
    return max(0, int(value))


async def deadline(now=None):
    seconds = await current_seconds()
    return (db.now_ts() if now is None else now) + seconds if seconds else None


def label(seconds):
    return 'off' if seconds == 0 else f'{seconds} seconds'
