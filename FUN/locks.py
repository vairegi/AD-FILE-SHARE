"""Single-process locks with weak references so idle users don't leak RAM."""
import asyncio
from weakref import WeakValueDictionary
_users = WeakValueDictionary()
_channels = WeakValueDictionary()
_feeds = WeakValueDictionary()

def _get(pool, key):
    lock = pool.get(key)
    if lock is None:
        lock = asyncio.Lock()
        pool[key] = lock
    return lock

def user_lock(uid):
    return _get(_users, int(uid))

def channel_lock(cid):
    return _get(_channels, int(cid))


def feed_lock(uid):
    return _get(_feeds, int(uid))
