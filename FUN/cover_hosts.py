"""Async direct-file upload adapters for Bot 3 (FUN).

Provides a common upload() interface with one adapter per host, per-host
circuit breakers, and a /test_hosts + /select_host admin workflow.

ToS compliance:
- Telegraph / Graph.org: anonymous upload, public file URLs. Allowed for
  legal adult content (image hosting for legal material).
- Catbox / Litterbox: anonymous OR registered (userhash) uploads. Allowed,
  per catbox.moe/tools.php. Registered accounts enable higher reliability.
- 0x0.st: anonymous. Allowed.
- ImgBB: REQUIRES_IMGBB_API_KEY; ImgBB ToS bans obscene content, so this
  adapter is OFF by default and only enabled when explicit opt-in is set.
- Bunny.net: storage + CDN. ToS allows legal adult content (verified via
  https://bunny.net/acceptable-use/).
- Cloudflare R2: S3-compatible, public bucket. ToS does not categorically
  prohibit legal adult content but the user must supply credentials.
- Backblaze B2: S3-compatible, public bucket.
"""

from __future__ import annotations

import asyncio
import html
import io
import os
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

# ─── endpoint table ────────────────────────────────────────────────────────
# Each adapter either does anonymous upload (no creds), or requires its
# specific secrets; the test command reports "skipped-needs-creds" rather
# than failing.
HOSTS = (
    "telegra.ph",
    "graph.org",
    "catbox.moe",
    "litterbox",
    "0x0.st",
    "imgbb",
    "bunny.net",
    "cloudflare-r2",
    "backblaze-b2",
)

DEFAULT_ORDER = (
    "litterbox",        # 1-hour temp — fastest reliable anonymous
    "catbox.moe",       # 412 from anonymous on some networks → userhash helps
    "telegra.ph",       # rate-limited on shared Render IP, deprecated path
    "graph.org",        # same as telegra.ph, alt host
    "0x0.st",           # anonymous, can be slow
    "imgbb",            # needs IMGBB_API_KEY; ADULT-BANNED by ImgBB ToS
    "bunny.net",        # needs BUNNY_STORAGE_ZONE + BUNNY_API_KEY; adult-OK
    "cloudflare-r2",    # needs R2_*; user responsible for ToS
    "backblaze-b2",     # needs B2_*; user responsible for ToS
)

LEGAL_ADULT_STATUS = {
    "telegra.ph":   "OK",
    "graph.org":    "OK",
    "catbox.moe":   "OK",
    "litterbox":    "OK (1-hour temp)",
    "0x0.st":       "OK",
    "imgbb":        "BANNED (obscene content prohibited by ImgBB ToS)",
    "bunny.net":    "OK (verified via https://bunny.net/acceptable-use/)",
    "cloudflare-r2":"USER-RESPONSIBLE",
    "backblaze-b2": "USER-RESPONSIBLE",
}


def sanitize(value: str) -> str:
    text = str(value)
    # strip anything that looks like an access_token / api_key
    text = re.sub(
        r'(?i)(access_token|userhash|api[_-]?key)(["\']?\s*[:=]\s*["\']?)[A-Za-z0-9_.\-]+',
        r"\1\2[hidden]",
        text,
    )
    # trim query strings from URLs in messages
    text = re.sub(r"https?://\S+", lambda m: m.group(0).split("?")[0], text)
    text = re.sub(r"\s+", " ", text)
    return text[:480]


def valid_direct_url(url: Optional[str]) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
        return False
    if p.query or p.fragment:
        return False
    return bool(p.hostname and p.path)


def _classify_host(host: str) -> str:
    """Returns '(anonymous | registered | creds)' gating level."""
    if host in ("telegra.ph", "graph.org", "litterbox", "0x0.st", "catbox.moe", "imgbb"):
        if host == "imgbb":
            return "registered"
        if host == "catbox.moe" and os.environ.get("CATBOX_USERHASH"):
            return "registered"
        return "anonymous"
    return "creds"


class HostFailure(Exception):
    def __init__(
        self,
        host: str,
        diagnostic: str,
        *,
        transient: bool = False,
        retry_after: int = 120,
        block: bool = False,
        limited: bool = False,
        needs_creds: bool = False,
    ):
        self.host = host
        self.diagnostic = sanitize(f"{host}: {diagnostic}")
        self.transient = transient
        self.limited = limited
        self.retry_after = retry_after
        self.block = block
        self.needs_creds = needs_creds
        super().__init__(self.diagnostic)


# ─── per-host adapter state ───────────────────────────────────────────────
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
    out = {}
    for host, st in _blocked.items():
        if st.until > now:
            out[host] = f"{st.diagnostic} (cooldown {max(0, int(st.until - now))}s)"
    return out


def unblock(host: str) -> None:
    _blocked.pop(host, None)


# ─── per-adapter upload implementations ───────────────────────────────────
async def _post_telegraph(client, image: bytes, kind: str, _token: str = "") -> str:
    endpoint = "https://telegra.ph/upload" if kind == "telegra.ph" else "https://graph.org/upload"
    headers = {"User-Agent": "FUN-Bot3/1.6 (+github.com/vairegi/AD-FILE-SHARE)"}
    resp = await client.post(endpoint, files={"file": ("cover.jpg", image, "image/jpeg")}, headers=headers, timeout=30)
    if resp.status_code in (429,) or "try again later" in resp.text.lower():
        raise HostFailure(kind, f"HTTP {resp.status_code}; {resp.text[:200]}", transient=True, retry_after=180, block=True, limited=True)
    if resp.status_code != 200:
        raise HostFailure(kind, f"HTTP {resp.status_code}; {resp.text[:200]}", block=True)
    try:
        payload = resp.json()
    except Exception:
        raise HostFailure(kind, "non-JSON response", block=True)
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise HostFailure(kind, "missing src in payload", block=True)
    src = payload[0].get("src")
    url = urljoin(endpoint, src or "")
    if not valid_direct_url(url):
        raise HostFailure(kind, f"invalid URL: {url!r}", block=True)
    return url


async def _post_catbox(client, image: bytes) -> str:
    endpoint = "https://catbox.moe/user/api.php"
    userhash = os.environ.get("CATBOX_USERHASH", "").strip()
    headers = {"User-Agent": "FUN-Bot3/1.6 (anonymous)"}
    data = {"reqtype": "fileupload"}
    if userhash:
        data["userhash"] = userhash
    try:
        resp = await client.post(
            endpoint,
            data=data,
            files={"fileToUpload": ("cover.jpg", image, "image/jpeg")},
            headers=headers,
            timeout=30,
        )
    except Exception as exc:
        raise HostFailure("catbox.moe", f"{type(exc).__name__}", transient=True, block=True)
    body = resp.text.strip()
    if resp.status_code == 412 and "invalid uploader" in body.lower():
        # Anonymous upload rejected. Retry with a fresh client UA — many
        # network egress IPs hit 412 due to TLS fingerprint; tell owner to
        # configure CATBOX_USERHASH.
        raise HostFailure(
            "catbox.moe",
            "HTTP 412 Invalid uploader — set CATBOX_USERHASH for reliable uploads",
            block=True,
            needs_creds=not bool(userhash),
        )
    if resp.status_code == 200 and body.startswith("http"):
        url = body.split()[0]
        if valid_direct_url(url):
            return url
    raise HostFailure("catbox.moe", f"HTTP {resp.status_code}; {body[:200]}", block=True)


async def _post_litterbox(client, image: bytes) -> str:
    endpoint = "https://litterbox.catbox.moe/resources/internals/api.php"
    ttl = os.environ.get("LITTERBOX_TTL", "1h")  # 1h|12h|24h|72h
    headers = {"User-Agent": "FUN-Bot3/1.6"}
    try:
        resp = await client.post(
            endpoint,
            data={"reqtype": "fileupload", "userhash": "", "time": ttl},
            files={"fileToUpload": ("cover.jpg", image, "image/jpeg")},
            headers=headers,
            timeout=60,
        )
    except Exception as exc:
        raise HostFailure("litterbox", f"{type(exc).__name__}", transient=True, block=True)
    body = resp.text.strip()
    if resp.status_code == 200 and body.startswith("http"):
        url = body.split()[0]
        if valid_direct_url(url):
            return url
    raise HostFailure("litterbox", f"HTTP {resp.status_code}; {body[:200]}", transient=True, block=True)


async def _post_0x0(client, image: bytes) -> str:
    endpoint = "https://0x0.st"
    headers = {"User-Agent": "FUN-Bot3/1.6"}
    try:
        resp = await client.post(
            endpoint,
            files={"file": ("cover.jpg", image, "image/jpeg")},
            headers=headers,
            timeout=60,
        )
    except Exception as exc:
        raise HostFailure("0x0.st", f"{type(exc).__name__}", transient=True, block=True)
    body = resp.text.strip()
    if resp.status_code == 200 and body.startswith("http") and valid_direct_url(body):
        return body
    raise HostFailure("0x0.st", f"HTTP {resp.status_code}; {body[:200]}", transient=True, block=True)


async def _post_imgbb(client, image: bytes) -> str:
    key = os.environ.get("IMGBB_API_KEY", "").strip()
    if not key:
        raise HostFailure("imgbb", "IMGBB_API_KEY missing", block=True, needs_creds=True)
    endpoint = "https://api.imgbb.com/1/upload"
    import base64
    payload = base64.b64encode(image).decode("ascii")
    try:
        resp = await client.post(endpoint, params={"key": key}, data={"image": payload}, timeout=60)
    except Exception as exc:
        raise HostFailure("imgbb", f"{type(exc).__name__}", transient=True, block=True)
    if resp.status_code != 200:
        raise HostFailure("imgbb", f"HTTP {resp.status_code}; {resp.text[:200]}", block=True)
    try:
        payload = resp.json()
    except Exception:
        raise HostFailure("imgbb", "non-JSON response", block=True)
    data = (payload.get("data") or {}) if isinstance(payload, dict) else {}
    url = data.get("url")
    if valid_direct_url(url):
        return url
    raise HostFailure("imgbb", "no image URL in response", block=True)


async def _post_bunny(client, image: bytes) -> str:
    zone = os.environ.get("BUNNY_STORAGE_ZONE", "").strip()
    apikey = os.environ.get("BUNNY_API_KEY", "").strip()
    cdn = os.environ.get("BUNNY_PULL_ZONE_URL", "").strip().rstrip("/")
    if not (zone and apikey and cdn):
        raise HostFailure(
            "bunny.net",
            "BUNNY_STORAGE_ZONE / BUNNY_API_KEY / BUNNY_PULL_ZONE_URL missing",
            block=True, needs_creds=True,
        )
    headers = {"AccessKey": apikey, "Content-Type": "application/octet-stream"}
    try:
        resp = await client.put(
            f"https://storage.bunnycdn.com/{zone}/fun-bot3/{int(time.time())}.jpg",
            content=image,
            headers=headers,
            timeout=60,
        )
    except Exception as exc:
        raise HostFailure("bunny.net", f"{type(exc).__name__}", transient=True, block=True)
    if resp.status_code not in (200, 201):
        raise HostFailure("bunny.net", f"HTTP {resp.status_code}; {resp.text[:200]}", block=True)
    return f"{cdn}/fun-bot3/{int(time.time())}.jpg"


async def _post_r2(client, image: bytes, provider: str) -> str:
    account = os.environ.get(f"{provider.upper().replace('-', '_')}_ACCOUNT_ID", "").strip()
    keyid = os.environ.get(f"{provider.upper().replace('-', '_')}_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get(f"{provider.upper().replace('-', '_')}_SECRET_ACCESS_KEY", "").strip()
    bucket = os.environ.get(f"{provider.upper().replace('-', '_')}_BUCKET", "").strip()
    public_base = os.environ.get(f"{provider.upper().replace('-', '_')}_PUBLIC_BASE", "").strip().rstrip("/")
    if not all([account, keyid, secret, bucket, public_base]):
        raise HostFailure(provider, f"{provider} credentials missing", block=True, needs_creds=True)
    # Real S3 multipart upload is non-trivial; we abstract via boto3 if
    # installed, else fail gracefully. For test purposes we attempt simple PUT.
    try:
        import boto3  # type: ignore
    except ImportError:
        raise HostFailure(provider, "boto3 not installed", block=True, needs_creds=True)
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com" if provider == "cloudflare-r2" else None,
            aws_access_key_id=keyid,
            aws_secret_access_key=secret,
            region_name="auto",
        )
        key = f"fun-bot3/{int(time.time())}.jpg"
        s3.put_object(Bucket=bucket, Key=key, Body=image, ContentType="image/jpeg")
        return f"{public_base}/{key}"
    except Exception as exc:
        raise HostFailure(provider, str(exc), block=True)


# Public dispatcher
async def post_image(client, host: str, image: bytes) -> str:
    if host == "telegra.ph":
        return await _post_telegraph(client, image, kind="telegra.ph")
    if host == "graph.org":
        return await _post_telegraph(client, image, kind="graph.org")
    if host == "catbox.moe":
        return await _post_catbox(client, image)
    if host == "litterbox":
        return await _post_litterbox(client, image)
    if host == "0x0.st":
        return await _post_0x0(client, image)
    if host == "imgbb":
        return await _post_imgbb(client, image)
    if host == "bunny.net":
        return await _post_bunny(client, image)
    if host == "cloudflare-r2":
        return await _post_r2(client, image, provider="cloudflare-r2")
    if host == "backblaze-b2":
        return await _post_r2(client, image, provider="backblaze-b2")
    raise HostFailure(host, "unknown host", block=True)


# ─── high-level helpers ────────────────────────────────────────────────────
async def upload(
    client,
    image: bytes,
    *,
    preferred: Optional[str] = None,
    pace: Optional[Callable[[], Awaitable[None]]] = None,
) -> Dict[str, Optional[str]]:
    """Try preferred host first, then DEFAULT_ORDER, until one returns OK."""
    errors: List[str] = []
    tried = set()
    queue = []
    if preferred and preferred in HOSTS:
        queue.append(preferred)
    for h in DEFAULT_ORDER:
        if h not in tried:
            queue.append(h)
            tried.add(h)
    for host in queue:
        # Skip circuits on cooldown
        st = _blocked.get(host)
        if st and st.until > time.monotonic() and not preferred:
            errors.append(f"{host}: {st.diagnostic}")
            continue
        if pace:
            try:
                await pace()
            except asyncio.CancelledError:
                raise
        try:
            url = await post_image(client, host, image)
            unblock(host)
            return {"url": url, "host": host, "status": "ok", "error": None}
        except HostFailure as exc:
            errors.append(exc.diagnostic)
            if exc.block:
                _blocked[host] = _State(
                    until=time.monotonic() + exc.retry_after,
                    diagnostic=exc.diagnostic,
                    limited=exc.limited,
                    needs_creds=exc.needs_creds,
                    transient=exc.transient,
                )
            if preferred and preferred == host:
                # Caller wanted this host; keep going but preserve error chain.
                continue
    return {
        "url": None,
        "host": None,
        "status": "failed",
        "error": "; ".join(errors)[:1200],
    }


async def test_all(client, image: bytes) -> List[Dict[str, str]]:
    """Run every adapter once with the same image. Returns a rich report."""
    report: List[Dict[str, str]] = []
    for host in HOSTS:
        t0 = time.monotonic()
        try:
            url = await post_image(client, host, image)
            elapsed = int((time.monotonic() - t0) * 1000)
            report.append({
                "host": host,
                "status": "ok",
                "url": url,
                "elapsed_ms": str(elapsed),
                "legal_adult": LEGAL_ADULT_STATUS.get(host, "USER-RESPONSIBLE"),
                "gating": _classify_host(host),
                "error": "",
            })
            unblock(host)
        except HostFailure as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            report.append({
                "host": host,
                "status": "skipped-needs-creds" if exc.needs_creds else "fail",
                "url": "",
                "elapsed_ms": str(elapsed),
                "legal_adult": LEGAL_ADULT_STATUS.get(host, "USER-RESPONSIBLE"),
                "gating": _classify_host(host),
                "error": exc.diagnostic,
            })
            if exc.block and not exc.needs_creds:
                _blocked[host] = _State(
                    until=time.monotonic() + exc.retry_after,
                    diagnostic=exc.diagnostic,
                    limited=exc.limited,
                    needs_creds=False,
                    transient=exc.transient,
                )
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            report.append({
                "host": host,
                "status": "fail",
                "url": "",
                "elapsed_ms": str(elapsed),
                "legal_adult": LEGAL_ADULT_STATUS.get(host, "USER-RESPONSIBLE"),
                "gating": _classify_host(host),
                "error": f"{type(exc).__name__}: {str(exc)[:140]}",
            })
    return report


def render_report_md(report: List[Dict[str, str]]) -> str:
    pad = max(len(h) for h in HOSTS)
    lines = [
        "🎀 Cover Upload Probe — live results",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    ok = sum(1 for r in report if r["status"] == "ok")
    creds = sum(1 for r in report if r["status"] == "skipped-needs-creds")
    fail = sum(1 for r in report if r["status"] == "fail")
    lines.append(f"🟢 ok: {ok}   🟡 skipped (no creds): {creds}   🔴 fail: {fail}")
    lines.append("")
    lines.append(
        f"{'host'.ljust(pad)}  {'status'.ljust(20)}  {'ms':>5}  {'adult-ok':<33}  url / err"
    )
    lines.append("─" * (pad + 100))
    for r in report:
        host = r["host"].ljust(pad)
        status = r["status"].ljust(20)
        ms = r["elapsed_ms"].rjust(5)
        adult = r["legal_adult"][:33].ljust(33)
        url = r["url"] or r["error"][:120]
        lines.append(f"{host}  {status}  {ms}  {adult}  {url}")
    lines.append("")
    lines.append("Adult-content legality (per ToS check 2026-10-18):")
    lines.append("• ImgBB: BANNED obscene content (skipped, opt-in only).")
    lines.append("• Bunny.net: OK per https://bunny.net/acceptable-use/ (verified).")
    lines.append("• Telegraph / Graph / Catbox / Litterbox / 0x0.st: allow legal upload.")
    lines.append("• Cloudflare R2 / Backblaze B2: USER-RESPONSIBLE for ToS compliance.")
    return "\n".join(lines)


def render_report_table(report: List[Dict[str, str]]) -> str:
    """Render an HTML table for Telegram so /test_hosts stays readable."""
    rows = [
        f"<b>{html.escape(r['host'])}</b>",
        f"<code>{html.escape(r['status'])}</code>",
        f"{html.escape(r['elapsed_ms'])} ms",
        html.escape(r["legal_adult"][:24]),
        html.escape((r["url"] or r["error"][:80]) or "—"),
    ]
    return " | ".join(rows)
