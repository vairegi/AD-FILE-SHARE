"""LinkGuard client (v1.1) — bot-side helper for the self-hosted three-door
link protector running on Cloudflare Workers.

v1.1: adds the Door-3 EXIT grant (mint_return_grant) — the paid shortener's
destination becomes /finish2?s=..&t=.. instead of the raw Telegram deep link,
so a copied shortener URL is worthless outside the original verified browser
session. Also adds push_ref_hosts() to sync the /finish2 Referer allowlist
from the live /shortenerapi registry.

The Worker holds the slug -> destination mapping; the bots only ever see the
short URL. All calls are signed with the shared ADMIN_API_KEY. Fail-open: on
any error the functions return None/False so callers fall back to the normal
paid-shortener path (mirroring shortener.py's 'down' semantics — never punish
the user for our outage).
"""
import logging
from urllib.parse import urlparse

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


async def shortener_hosts():
    """Hostnames of every ACTIVE paid shortener — the /finish2 Referer
    allowlist (v1.1). Derived from the live /shortenerapi registry, so any
    provider added via '/shortenerapi add' is covered automatically."""
    hosts = []
    try:
        for e in await db.active_shorteners():
            h = urlparse((e.get("api_base") or "").strip()).hostname
            if h:
                hosts.append(h.lower())
    except Exception as exc:
        log.warning("shortener_hosts failed: %s", exc)
    return sorted(set(hosts))


async def mint(destination: str, ttl_days=None, honeypot: bool = False,
               grant_slug: str = None):
    """Create a protected slug for `destination`. Returns the short URL or None.
    grant_slug (v1.1) links this public slug to its finish2 exit grant."""
    base, key = await _cfg()
    if not base:
        return None
    payload = {"destination": destination,
               "ttl_days": ttl_days, "honeypot": honeypot}
    if grant_slug:
        payload["grant_slug"] = grant_slug
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{base}/api/admin/mint",
                                  headers={"x-admin-key": key},
                                  json=payload)
            r.raise_for_status()
            return (r.json() or {}).get("url")
    except Exception as exc:
        log.warning("linkguard mint failed: %s", exc)
        return None


async def mint_return_grant(final_destination: str):
    """v1.1: mint the Door-3 EXIT grant for the Telegram deep link.
    Returns {'slug', 'finish2_url', 'expires_at'} or None (fail-open).
    ONLY call this when the front gate is enabled — finish2 requires the
    landing session cookie to exist."""
    base, key = await _cfg()
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{base}/api/admin/mint2",
                headers={"x-admin-key": key},
                json={"final_destination": final_destination,
                      "ref_hosts": await shortener_hosts()},
            )
            r.raise_for_status()
            d = r.json() or {}
            if d.get("finish2_url") and d.get("slug"):
                return d
    except Exception as exc:
        log.warning("linkguard mint2 failed: %s", exc)
    return None


async def push_ref_hosts() -> bool:
    """v1.1: replace the Worker's global shortener-Referer allowlist with the
    current ACTIVE /shortenerapi domains."""
    base, key = await _cfg()
    if not base:
        return False
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{base}/api/admin/ref_hosts",
                                  headers={"x-admin-key": key},
                                  json={"hosts": await shortener_hosts()})
            return bool(r.json().get("ok"))
    except Exception as exc:
        log.warning("linkguard ref_hosts push failed: %s", exc)
        return False


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


async def protect(long_url: str, grant_slug: str = None):
    """Mint a LinkGuard short URL for long_url, or None (fail-open)."""
    if not await enabled():
        return None
    return await mint(long_url, grant_slug=grant_slug)

# ── v1.2: observe-mode + host-learning admin calls ────────────────────
async def _admin_post(path: str, payload=None):
    """POST an admin route on the LinkGuard worker; None on failure."""
    base, key = await _cfg()
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{base}{path}",
                                  headers={"x-admin-key": key},
                                  json=payload or {})
            if r.status_code == 200:
                return r.json()
            log.warning("linkguard admin %s -> HTTP %s: %s", path,
                        r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("linkguard admin %s failed: %s", path, exc)
    return None


async def set_observe(on: bool, minutes: int = 60):
    """Turn observe-mode on/off on the worker (learn shortener exit hosts)."""
    return await _admin_post("/api/admin/observe",
                             {"on": bool(on), "minutes": int(minutes)})


async def observed_hosts():
    """List referer hosts the worker learned while observing."""
    return await _admin_post("/api/admin/observed_hosts")


async def add_ref_host(host: str):
    """Approve one host into the /finish2 Referer allowlist."""
    return await _admin_post("/api/admin/ref_hosts_add", {"host": host})


async def clear_observed():
    return await _admin_post("/api/admin/observed_clear")

