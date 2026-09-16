"""Shortener integration (VPLinks / GPLinks style API).

Request : GET <base>?api=<KEY>&url=<url-encoded link>&format=text
Response: the raw short link as plain text (or JSON when format is omitted).
"""
import logging

import httpx

import config
import db

log = logging.getLogger("shortener")


async def shorten(long_url: str):
    """Return a shortened URL string, or None when the API fails."""
    settings = await db.get_settings()
    base = (settings.get("shortener_api_base") or "https://vplink.in/api").strip()
    key = config.SHORTENER_API_KEY
    if not key:
        log.warning("SHORTENER_API_KEY is not set; cannot shorten.")
        return None

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
