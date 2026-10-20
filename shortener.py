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
import asyncio
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

import config
import db
import outbound

log = logging.getLogger("shortener")

_trace = ContextVar("shortener_request_id", default="standalone")


def _warn(message, *args):
    log.warning("request=%s " + message, _trace.get(), *args)


MAX_ATTEMPTS = 2
MAX_RETRY_DELAY = 8.0


def _http_url(value):
    if not isinstance(value, str):
        return None
    value = value.strip().strip('"').strip("'")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme in ("http", "https") and parsed.hostname
                and not parsed.username and not parsed.password
                and not any(ch.isspace() for ch in value)):
            return value
    except ValueError:
        pass
    return None


def _retry_delay(response, attempt):
    """Respect Retry-After. A long wait skips this provider, never retries early."""
    raw = response.headers.get("retry-after")
    if raw:
        try:
            seconds = float(raw)
        except ValueError:
            try:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = (date - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 0.5 * (attempt + 1)
        if seconds > MAX_RETRY_DELAY:
            return None
        return max(seconds, 0.0)
    return 0.5 * (attempt + 1)


async def _shorten_with(base: str, key: str, long_url: str):
    """Same URL-or-None API, now with pooled HTTP and one transient retry.

    Diagnostics identify host + exception CLASS, never credentials, the
    destination, exception text (which can contain the key), or response body.
    Provider API errors / auth failures do not get pointless retries.
    """
    try:
        host = urlsplit(base).hostname or "invalid-host"
    except ValueError:
        host = "invalid-host"
    params = {"api": key, "url": long_url, "format": "text"}
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = await outbound.request("GET", base, params=params)
        except (httpx.TransportError, asyncio.TimeoutError) as exc:
            _warn("shortener host=%s attempt=%s/%s error=%s",
                        host, attempt + 1, MAX_ATTEMPTS, type(exc).__name__)
            if attempt + 1 < MAX_ATTEMPTS:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return None
        except Exception as exc:
            _warn("shortener host=%s error=%s (non-retryable)",
                        host, type(exc).__name__)
            return None

        status = resp.status_code
        if status == 429 or status in (408, 500, 502, 503, 504):
            _warn("shortener host=%s attempt=%s/%s HTTP=%s",
                        host, attempt + 1, MAX_ATTEMPTS, status)
            delay = _retry_delay(resp, attempt)
            if attempt + 1 < MAX_ATTEMPTS and delay is not None:
                await asyncio.sleep(delay)
                continue
            return None
        if not 200 <= status < 300:
            _warn("shortener host=%s HTTP=%s (non-retryable)", host, status)
            return None

        try:
            data = resp.json()
        except (ValueError, TypeError):
            data = None
        value = None
        if isinstance(data, dict):
            if str(data.get("status", "")).lower() in ("error", "failed", "fail"):
                _warn("shortener host=%s error=ProviderAPIError HTTP=%s", host, status)
                return None
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            for field in ("shortenedUrl", "shortened_url", "shortUrl", "short_url", "shorturl"):
                value = _http_url(payload.get(field))
                if value:
                    break
        elif isinstance(data, str):
            value = _http_url(data)
        else:
            value = _http_url(resp.text)
        if value and value != long_url:
            return value
        # Never treat HTTP 200 alone (HTML/challenge/error body) as success.
        _warn("shortener host=%s error=UnexpectedResponse HTTP=%s content_type=%s",
                    host, status, resp.headers.get("content-type", "unknown").split(";")[0])
        return None
    return None


async def test_key(base: str, key: str):
    """Live self-test used by '/shortenerapi add': returns a URL or None."""
    return await _shorten_with(base, key, "https://example.com/self-test")


async def _shorten_all(long_url: str, user_id=None, with_status=False):
    """Shorten via the ACTIVE providers WITH FAILOVER (v4.1).

    Every active shortener is tried in per-user round-robin order; the first
    success wins. with_status=True returns (url, state, site, failures):
      state "active" — a live provider served the link (ban rules apply);
      state "down"   — NO provider could serve (all paused, none configured,
                       or every API call failed). Callers must fail open and
                       MUST NOT punish the user for an outage.
    Plain calls return just the URL (or None) — unchanged for old callers.
    """
    entries = await db.active_shorteners()
    if not entries:
        if await db.count_shorteners():
            _warn("All shorteners paused; nothing can serve.")
            result = (None, "down", None, [])
            return result if with_status else None
        # Legacy single-key fallback (fresh deploy before migration).
        settings = await db.get_settings()
        key = (settings.get("shortener_api_key") or "").strip()
        if not key:
            _warn("No shortener API key set (DB or env).")
            result = (None, "down", None, [])
            return result if with_status else None
        entries = [{"api_base": (settings.get("shortener_api_base")
                                 or "https://vplink.in/api").strip(),
                    "api_key": key, "site": "legacy"}]
    start = (await db.bump_rr_cursor(user_id or 0)) % len(entries)
    ordered = entries[start:] + entries[:start]
    failures = []
    for entry in ordered:
        url = await _shorten_with(entry["api_base"], entry["api_key"], long_url)
        if url:
            result = (url, "active", entry.get("site"), failures)
            return result if with_status else url
        failures.append(entry.get("site"))
    _warn("Link generation failed for this request; providers exhausted=%s (not proof of a global outage)", failures)
    result = (None, "down", None, failures)
    return result if with_status else None


async def shorten(long_url: str, user_id=None, with_status=False):
    """Original result contract, with a non-sensitive per-request log ID.
    Concurrent downloads can now be separated without logging user links.
    """
    marker = _trace.set(uuid.uuid4().hex[:12])
    try:
        return await _shorten_all(long_url, user_id=user_id, with_status=with_status)
    finally:
        _trace.reset(marker)
