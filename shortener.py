"""Shortener integration (VPLinks / GPLinks style API).

v3.2: multi-shortener round-robin. Every Download tap picks the next ACTIVE
shortener for THAT user (per-user cursor in MongoDB); paused shorteners are
skipped entirely. If every shortener is paused, None is returned and the
caller fails open (delivers the file directly). If the shorteners collection
is completely empty (fresh deploy, pre-migration), the legacy single-key
settings (DB field, then env var) are used so nothing breaks.

Request : GET <base>?api=<KEY>&url=<url-encoded link>&format=text
Response: the raw short link as plain text (or JSON when format is omitted).
"""
import logging

import httpx

import config
import db

log = logging.getLogger("shortener")


async def _shorten_with(base: str, key: str, long_url: str):
    """Perform the actual API call against one provider. URL or None."""
    params = {"api": key, "url": long_url, "format": "text"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(base, params=params)
            resp.raise_for_status()
            text = (resp.text or "").strip().strip('"')
            if text.startswith("http"):
                return text
            # Some providers ignore format=text and return JSON.
            try:
                data = resp.json()
                if isinstance(data, dict):
                    if data.get("status") == "error":
                        log.warning("Shortener error: %s", data.get("message"))
                        return None
                    value = data.get("shortenedUrl") or data.get("shortened_url")
                    if value:
                        return str(value).strip('"')
            except Exception:
                pass
    except Exception as exc:
        log.warning("Shortener request failed: %s", exc)
    return None


async def test_key(base: str, key: str):
    """Live self-test used by '/shortenerapi add': returns a URL or None."""
    return await _shorten_with(base, key, "https://example.com/self-test")


async def shorten(long_url: str, user_id=None):
    """Return a shortened URL string, or None when no shortener can serve.

    Rotation: the user's own round-robin cursor advances on every call and
    only ACTIVE shorteners take part (paused ones are filtered out before the
    modulo, so they are never selected). Fail-open: when every shortener is
    paused this returns None and the gate delivers the file directly.
    """
    entry = await db.next_shortener(user_id or 0)
    if not entry:
        if await db.count_shorteners():
            log.warning("All shorteners are paused; cannot shorten (fail-open upstream).")
            return None
        # Legacy fallback: no shorteners collection entries at all (fresh
        # deploy before migration, or migration found no key anywhere).
        settings = await db.get_settings()
        key = (settings.get("shortener_api_key") or "").strip()
        if not key:
            log.warning("No shortener API key set (DB or env); cannot shorten.")
            return None
        entry = {"api_base": (settings.get("shortener_api_base")
                              or "https://vplink.in/api").strip(),
                 "api_key": key, "site": "legacy"}
    return await _shorten_with(entry["api_base"], entry["api_key"], long_url)
