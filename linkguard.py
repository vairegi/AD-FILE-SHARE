"""LinkGuard client (v1.0) — bot-side helper for the self-hosted three-door
link protector running on Cloudflare Workers.

The Worker holds the slug -> destination mapping; the bots only ever see the
short URL. All calls are signed with the shared ADMIN_API_KEY. Fail-open: on
any error the functions return None/False so callers fall back to the normal
paid-shortener path (mirroring shortener.py's 'down' semantics — never punish
the user for our outage).
"""
import logging

import httpx

import db

log = logging.getLogger("linkguard")

_TIMEOUT = httpx.Timeout(15.0)


async def _cfg():
    """(base, key) from DB settings, or (None, None) if not configured."""
    s = await db.get_settings()
    base = (s.get("linkguard_base") or "").strip().rstrip("/")
    key = (s.get("linkguard_key") or "").strip()
    return (base, key) if (base and key) else (None, None)


async def enabled() -> bool:
    s = await db.get_settings()
    return bool(s.get("linkguard_enabled"))


async def mint(destination: str, ttl_days=None, honeypot: bool = False):
    """Create a protected slug for `destination`. Returns the short URL or None."""
    base, key = await _cfg()
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{base}/api/admin/mint",
                headers={"x-admin-key": key},
                json={"destination": destination,
                      "ttl_days": ttl_days, "honeypot": honeypot},
            )
            r.raise_for_status()
            return (r.json() or {}).get("url")
    except Exception as exc:
        log.warning("linkguard mint failed: %s", exc)
        return None


async def revoke(slug: str) -> bool:
    base, key = await _cfg()
    if not base:
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{base}/api/admin/revoke",
                                  headers={"x-admin-key": key},
                                  json={"slug": slug})
            return bool(r.json().get("ok"))
    except Exception as exc:
        log.warning("linkguard revoke failed: %s", exc)
        return False


async def add_decoys(urls) -> bool:
    base, key = await _cfg()
    if not base or not urls:
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{base}/api/admin/decoys",
                                  headers={"x-admin-key": key},
                                  json={"urls": list(urls)})
            return bool(r.json().get("ok"))
    except Exception as exc:
        log.warning("linkguard decoys failed: %s", exc)
        return False


async def health() -> bool:
    base, _ = await _cfg()
    if not base:
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
            r = await client.get(f"{base}/api/health")
            return r.status_code == 200 and (r.json() or {}).get("ok") is True
    except Exception:
        return False


async def protect(long_url: str):
    """Mint a LinkGuard short URL for long_url, or None (fail-open)."""
    if not await enabled():
        return None
    return await mint(long_url)
