"""Bounded, pooled HTTP for paid shorteners and LinkGuard (not Telegram).

One client per service event loop; no user links/keys are logged here.
Call close() during application shutdown.
"""
import asyncio
import httpx

MAX_IN_FLIGHT = 8
REQUEST_DEADLINE = 45.0
TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=5.0)
_client = None
_loop = None
_slots = None


def client():
    global _client, _loop, _slots
    loop = asyncio.get_running_loop()
    # The real service has one loop. The loop check also isolates offline
    # tests that use separate asyncio.run() calls.
    if _client is None or _client.is_closed or _loop is not loop:
        _client = httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=True,
            limits=httpx.Limits(max_connections=MAX_IN_FLIGHT,
                               max_keepalive_connections=MAX_IN_FLIGHT,
                               keepalive_expiry=20.0))
        _loop = loop
        _slots = asyncio.Semaphore(MAX_IN_FLIGHT)
    return _client


async def request(method, url, **kwargs):
    pool = client()
    # Include queueing in the deadline: an overloaded queue cannot wait forever.
    async def perform():
        async with _slots:
            return await pool.request(method, url, **kwargs)
    return await asyncio.wait_for(perform(), timeout=REQUEST_DEADLINE)


async def close():
    global _client, _loop, _slots
    if _client is not None:
        await _client.aclose()
    _client = _loop = _slots = None
