"""Async direct-file upload adapters for Bot 3 (FUN) — v1.7 pruned set.

Common upload() interface, one adapter per host, per-host circuit breakers,
and /test_hosts + /select_host admin workflow.

Included hosts (live-probed 2026-10-18 from the Render egress IP):
- catbox.moe  : PERMANENT direct links. Anonymous upload is blocked with
                HTTP 412 "Invalid uploader" on this network, so a registered
                CATBOX_USERHASH is required. Adult content allowed per
                https://catbox.moe/tools.php. PRIMARY host.
- telegra.ph  : permanent /file/ links; currently HTTP 400 from this egress
                IP (anonymous /upload restricted). Kept as a fallback in case
                the restriction lifts. No account rotation — direct only.
- graph.org   : same endpoint family as telegra.ph, alternate domain.
- litterbox   : WORKS anonymously, but links EXPIRE (LITTERBOX_TTL, default
                72h here). Emergency fallback only — never preferred, because
                dead cover URLs kill the link-preview feed.
- bunny.net   : storage+CDN, explicitly allows legal adult content
                (https://bunny.net/acceptable-use/). Needs BUNNY_* creds.

Removed hosts (with reasons, no silent drops):
- 0x0.st      : HTTP 503 "uploads disabled ... AI botnet spam" — site-wide.
- imgbb       : ImgBB ToS bans obscene/adult content (https://imgbb.com/tos).
- cloudflare-r2 / backblaze-b2: unused credential-gated S3 adapters; the
                owner carries all ToS risk and no creds were ever configured.
"""

from __future__ import annotations

import asyncio
import html as _html
import os
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

HOSTS = ("catbox.moe", "telegra.ph", "graph.org", "litterbox", "bunny.net")

# catbox first (permanent links once CATBOX_USERHASH is set), telegraph family
# next (permanent if the egress-IP restriction lifts), litterbox LAST (links
# expire — emergency only), bunny whenever the owner adds creds.
DEFAULT_ORDER = ("catbox.moe", "telegra.ph", "graph.org", "litterbox", "bunny.net")

LEGAL_ADULT_STATUS = {
    "catbox.moe": "OK (permanent links; needs CATBOX_USERHASH on this network)",
    "telegra.ph": "OK",
    "graph.org":  "OK",
    "litterbox":  "OK but links EXPIRE — emergency fallback only",
    "bunny.net":  "OK (verified via https://bunny.net/acceptable-use/)",
}

REMOVED_HOSTS = {
    "0x0.st":       "removed — site disabled uploads (HTTP 503, 'AI botnet spam')",
    "imgbb":        "removed — ImgBB ToS bans obscene/adult content",
    "cloudflare-r2":"removed — unused; owner carries all ToS risk",
    "backblaze-b2": "removed — unused; owner carries all ToS risk",
}

CATBOX_ENDPOINT = "https://catbox.moe/user/api.php"
USER_AGENT = "FUN-Bot3/1.7 (+github.com/vairegi/AD-FILE-SHARE)"


def sanitize(value: str) -> str:
    text = str(value)
    text = re.sub(
        r'(?i)(access_token|userhash|api[_-]?key)(["\']?\s*[:=]\s*["\']?)[A-Za-z0-9_.\-]+',
        r"\1\2[hidden]", text)
    text = re.sub(r"https?://\S+", lambda m: m.group(0).split("?")[0], text)
    text = re.sub(r"\s+", " ", text)
    return text[:480]


def valid_direct_url(url: Optional[str]) -> bool:
    """Strict allowlist: only URLs from hosts this module can produce. Anything
    else (e.g. old Render /previews/ links) is rejected so it can never be
    embedded as a feed-card preview again."""
    if not isinstance(url, str) or not url:
        return False
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
        return False
    if p.query or p.fragment or not p.hostname:
        return False
    host, path = p.hostname, p.path
    if host in ("telegra.ph", "graph.org"):
        return path.startswith("/file/") and len(path) > 6 and ".." not in path
    if host in ("files.catbox.moe", "litter.catbox.moe"):
        return bool(re.fullmatch(r"/[A-Za-z0-9_\-]+\.(jpg|jpeg|png|gif|webp)", path, re.I))
    if host.endswith(".b-cdn.net"):  # bunny.net pull zone
        return path.startswith("/fun-bot3/") and path.lower().endswith(".jpg")
    return False


class HostFailure(Exception):
    def __init__(self, host: str, diagnostic: str, *, transient: bool = False,
                 retry_after: int = 120, block: bool = False, limited: bool = False,
                 needs_creds: bool = False):
        self.host = host
        self.diagnostic = sanitize(f"{host}: {diagnostic}")
        self.transient = transient
        self.limited = limited
        self.retry_after = retry_after
        self.block = block
        self.needs_creds = needs_creds
        super().__init__(self.diagnostic)


@dataclass
class _State:
    until: float = 0.0
    diagnostic: str = ""
    limited: bool = False
    needs_creds: bool = False
    transient: bool = False


_blocked: Dict[str, _State] = {}


def diagnostics() -> Dict[str, str]:
    now = time.monotonic()
    return {host: f"{st.diagnostic} (cooldown {max(0, int(st.until - now))}s)"
            for host, st in _blocked.items() if st.until > now}


def unblock(host: str) -> None:
    _blocked.pop(host, None)


def _resp_text(response) -> str:
    try:
        return str(response.text)
    except Exception:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                return str(payload.get("error", payload))
            return str(payload)
        except Exception:
            return "unreadable response"


# ─── adapters ──────────────────────────────────────────────────────────────
async def _post_telegraph(client, image: bytes, kind: str) -> str:
    endpoint = ("https://telegra.ph/upload" if kind == "telegra.ph"
                else "https://graph.org/upload")
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = await client.post(endpoint,
            files={"file": ("cover.jpg", image, "image/jpeg")},
            headers=headers, timeout=30)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise HostFailure(kind, f"{type(exc).__name__}", transient=True,
                          retry_after=60, block=True) from exc
    status = resp.status_code
    detail = _resp_text(resp)
    if status == 429 or "try again later" in detail.lower():
        retry = 180
        try:
            retry = max(30, min(3600, int(getattr(resp, "headers", {}).get("Retry-After", 180))))
        except (ValueError, TypeError):
            pass
        raise HostFailure(kind, f"HTTP {status}; {detail[:160]}",
                          transient=True, retry_after=retry, block=True, limited=True)
    if status != 200:
        raise HostFailure(kind, f"HTTP {status}; {detail[:160]}",
                          transient=status >= 500, block=status in (400, 401, 403, 412) or status >= 500)
    try:
        payload = resp.json()
    except Exception:
        raise HostFailure(kind, "non-JSON response", block=True)
    src = (payload[0].get("src") if isinstance(payload, list) and payload
           and isinstance(payload[0], dict) else None)
    url = urljoin(endpoint, src) if isinstance(src, str) else None
    if not valid_direct_url(url):
        raise HostFailure(kind, f"HTTP 200 but invalid direct URL; {detail[:120]}", block=True)
    return url


async def _post_catbox(client, image: bytes) -> str:
    userhash = os.environ.get("CATBOX_USERHASH", "").strip()
    data = {"reqtype": "fileupload"}
    if userhash:
        data["userhash"] = userhash
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = await client.post(CATBOX_ENDPOINT, data=data,
            files={"fileToUpload": ("cover.jpg", image, "image/jpeg")},
            headers=headers, timeout=60)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise HostFailure("catbox.moe", f"{type(exc).__name__}", transient=True,
                          retry_after=60, block=True) from exc
    raw = _resp_text(resp).strip()
    # Diagnostics only: mask the userhash at WORD BOUNDARIES. A blind
    # str.replace would corrupt a successful URL when the hash is a substring
    # of it (e.g. hash 'h' eats the 'h' in 'https').
    if userhash:
        body = re.sub(rf"(?<![A-Za-z0-9]){re.escape(userhash)}(?![A-Za-z0-9])",
                      "[hidden]", raw)
    else:
        body = raw
    if resp.status_code == 429 or "try again later" in body.lower():
        raise HostFailure("catbox.moe", f"HTTP {resp.status_code}; {body[:160]}",
                          transient=True, retry_after=180, block=True, limited=True)
    if resp.status_code == 412 and "invalid uploader" in body.lower():
        raise HostFailure(
            "catbox.moe",
            "HTTP 412 Invalid uploader — set CATBOX_USERHASH in Render env vars",
            block=True, needs_creds=not bool(userhash))
    if resp.status_code == 200 and raw.startswith("http"):
        url = raw.split()[0]  # validate the ORIGINAL response, never the masked copy
        if valid_direct_url(url):
            return url
    raise HostFailure("catbox.moe", f"HTTP {resp.status_code}; {body[:160]}",
                      transient=resp.status_code >= 500,
                      block=resp.status_code in (400, 401, 403, 412) or resp.status_code >= 500)


async def _post_litterbox(client, image: bytes) -> str:
    # EMERGENCY ONLY: links die after TTL. 72h default so a broken primary
    # host doesn't blank the whole feed while the owner fixes it.
    ttl = os.environ.get("LITTERBOX_TTL", "72h")  # 1h|12h|24h|72h
    if ttl not in ("1h", "12h", "24h", "72h"):
        ttl = "72h"
    endpoint = "https://litterbox.catbox.moe/resources/internals/api.php"
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = await client.post(endpoint,
            data={"reqtype": "fileupload", "userhash": "", "time": ttl},
            files={"fileToUpload": ("cover.jpg", image, "image/jpeg")},
            headers=headers, timeout=60)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise HostFailure("litterbox", f"{type(exc).__name__}", transient=True,
                          retry_after=60, block=True) from exc
    body = _resp_text(resp).strip()
    if resp.status_code == 429 or "try again later" in body.lower():
        raise HostFailure("litterbox", f"HTTP {resp.status_code}; {body[:160]}",
                          transient=True, retry_after=180, block=True, limited=True)
    if resp.status_code == 200 and body.startswith("http"):
        url = body.split()[0]
        if valid_direct_url(url):
            return url
    raise HostFailure("litterbox", f"HTTP {resp.status_code}; {body[:160]}",
                      transient=True, block=True)


async def _post_bunny(client, image: bytes) -> str:
    zone = os.environ.get("BUNNY_STORAGE_ZONE", "").strip()
    apikey = os.environ.get("BUNNY_API_KEY", "").strip()
    cdn = os.environ.get("BUNNY_PULL_ZONE_URL", "").strip().rstrip("/")
    if not (zone and apikey and cdn):
        raise HostFailure("bunny.net",
            "BUNNY_STORAGE_ZONE / BUNNY_API_KEY / BUNNY_PULL_ZONE_URL missing",
            block=True, needs_creds=True)
    key = f"fun-bot3/{int(time.time() * 1000)}.jpg"
    headers = {"AccessKey": apikey, "Content-Type": "application/octet-stream"}
    try:
        resp = await client.put(f"https://storage.bunnycdn.com/{zone}/{key}",
                                content=image, headers=headers, timeout=60)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise HostFailure("bunny.net", f"{type(exc).__name__}", transient=True,
                          retry_after=60, block=True) from exc
    if resp.status_code not in (200, 201):
        detail = _resp_text(resp).replace(apikey, "[hidden]")
        raise HostFailure("bunny.net", f"HTTP {resp.status_code}; {detail[:160]}",
                          transient=resp.status_code >= 500, block=True)
    url = f"{cdn}/{key}"
    if not valid_direct_url(url):
        raise HostFailure("bunny.net", "constructed URL failed validation", block=True)
    return url


async def post_image(client, host: str, image: bytes) -> str:
    if host == "telegra.ph":
        return await _post_telegraph(client, image, "telegra.ph")
    if host == "graph.org":
        return await _post_telegraph(client, image, "graph.org")
    if host == "catbox.moe":
        return await _post_catbox(client, image)
    if host == "litterbox":
        return await _post_litterbox(client, image)
    if host == "bunny.net":
        return await _post_bunny(client, image)
    raise HostFailure(host, REMOVED_HOSTS.get(host, "unknown host"), block=True)


# ─── high-level helpers ────────────────────────────────────────────────────
def _creds_available(host: str) -> bool:
    """Hosts that need env credentials are skipped entirely when unconfigured
    (they never count as an attempt, an error, or a rate-limit vote)."""
    if host == "bunny.net":
        return bool(os.environ.get("BUNNY_STORAGE_ZONE", "").strip()
                    and os.environ.get("BUNNY_API_KEY", "").strip()
                    and os.environ.get("BUNNY_PULL_ZONE_URL", "").strip())
    return True


async def upload(client, image: bytes, *, preferred: Optional[str] = None,
                 pace: Optional[Callable[[], Awaitable[None]]] = None) -> Dict[str, Optional[str]]:
    """Try preferred host first, then DEFAULT_ORDER, until one returns OK."""
    errors: List[str] = []
    queue: List[str] = []
    if preferred and preferred in HOSTS:
        queue.append(preferred)
    for h in DEFAULT_ORDER:
        if h not in queue:
            queue.append(h)
    limited: List[bool] = []
    attempted: List[str] = []
    for host in queue:
        if not _creds_available(host):
            continue  # not an attempt; /test_hosts reports these as skipped
        st = _blocked.get(host)
        if st and st.until > time.monotonic():
            errors.append(st.diagnostic)
            limited.append(st.limited)
            attempted.append(host)
            continue
        if pace:
            try:
                await pace()
            except asyncio.CancelledError:
                raise
        attempted.append(host)
        try:
            url = await post_image(client, host, image)
            unblock(host)
            result: Dict[str, Optional[str]] = {"url": url, "host": host,
                                                "status": "ok", "error": None}
            if host == "litterbox":
                result["warning"] = "temporary link — expires after LITTERBOX_TTL"
            return result
        except HostFailure as exc:
            errors.append(exc.diagnostic)
            limited.append(exc.limited)
            if exc.block:
                _blocked[host] = _State(
                    until=time.monotonic() + exc.retry_after,
                    diagnostic=exc.diagnostic, limited=exc.limited,
                    needs_creds=exc.needs_creds, transient=exc.transient)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One broken adapter must never kill the whole chain.
            errors.append(sanitize(f"{host}: {type(exc).__name__}"))
            limited.append(False)
    # 'rate_limited' tells cover_jobs to keep covers pending for a later
    # /retry_covers instead of marking them failed: only when EVERY attempted
    # host was actually rate-limited.
    all_unavailable = bool(attempted) and all(
        (_blocked.get(h) and _blocked[h].until > time.monotonic()) for h in attempted)
    status = "rate_limited" if (limited and all(limited)) else "failed"
    return {"url": None, "host": None, "status": status,
            "error": "; ".join(errors)[:1200],
            "all_hosts_unavailable": all_unavailable}


async def test_all(client, image: bytes) -> List[Dict[str, str]]:
    """Run every remaining adapter once with the same image."""
    report: List[Dict[str, str]] = []
    for host in HOSTS:
        t0 = time.monotonic()
        try:
            url = await post_image(client, host, image)
            elapsed = str(int((time.monotonic() - t0) * 1000))
            entry = {"host": host, "status": "ok", "url": url,
                     "elapsed_ms": elapsed,
                     "legal_adult": LEGAL_ADULT_STATUS.get(host, "?"), "error": ""}
            if host == "litterbox":
                entry["url"] = f"{url}  (expires!)"
            report.append(entry)
            unblock(host)
        except HostFailure as exc:
            elapsed = str(int((time.monotonic() - t0) * 1000))
            report.append({
                "host": host,
                "status": "skipped-needs-creds" if exc.needs_creds else "fail",
                "url": "", "elapsed_ms": elapsed,
                "legal_adult": LEGAL_ADULT_STATUS.get(host, "?"),
                "error": exc.diagnostic})
            if exc.block and not exc.needs_creds:
                _blocked[host] = _State(
                    until=time.monotonic() + exc.retry_after,
                    diagnostic=exc.diagnostic, limited=exc.limited,
                    needs_creds=False, transient=exc.transient)
        except Exception as exc:
            elapsed = str(int((time.monotonic() - t0) * 1000))
            report.append({"host": host, "status": "fail", "url": "",
                           "elapsed_ms": elapsed,
                           "legal_adult": LEGAL_ADULT_STATUS.get(host, "?"),
                           "error": f"{type(exc).__name__}: {str(exc)[:140]}"})
    return report


def render_report_html(report: List[Dict[str, str]]) -> str:
    """HTML-safe report for Telegram (caller MUST send with parse_mode=HTML)."""
    e = _html.escape
    ok = sum(1 for r in report if r["status"] == "ok")
    creds = sum(1 for r in report if r["status"] == "skipped-needs-creds")
    fail = sum(1 for r in report if r["status"] == "fail")
    lines = [
        "🎀 <b>Cover Upload Probe — live results</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🟢 ok: {ok}   🟡 skipped (no creds): {creds}   🔴 fail: {fail}",
        "",
    ]
    for r in report:
        icon = {"ok": "🟢", "fail": "🔴", "skipped-needs-creds": "🟡"}.get(r["status"], "⚪")
        lines.append(f"{icon} <b>{e(r['host'])}</b> — {e(r['status'])} ({e(r['elapsed_ms'])} ms)")
        lines.append(f"   adult: {e(r['legal_adult'])}")
        detail = r["url"] or r["error"]
        if detail:
            lines.append(f"   <code>{e(detail[:150])}</code>")
    lines.append("")
    lines.append("<b>Removed hosts (why):</b>")
    for host, why in REMOVED_HOSTS.items():
        lines.append(f"• {e(host)} — {e(why)}")
    return "\n".join(lines)
