"""Async direct-file upload adapters. No articles/accounts/page creation.
Telegraph -> Graph -> Catbox, with per-provider circuit breakers.
A rejected provider never prevents trying the next independent provider.
"""
import asyncio
import re
import time
from urllib.parse import urljoin, urlsplit

CATBOX_ENDPOINT = 'https://catbox.moe/user/api.php'
USER_AGENT = 'FUN-Bot3/1.5 (+https://github.com/vairegi/AD-FILE-SHARE)'
HOSTS = ('telegra.ph', 'graph.org', 'catbox.moe')
_blocked = {}  # name -> {until, diagnostic, transient}; no credentials or files


def sanitize(value):
    text = str(value)
    text = re.sub(r'(?i)(access_token|userhash|api_key)([\"\x27]?\s*[:=]\s*[\"\x27]?)[A-Za-z0-9_-]+', r'\1\2[hidden]', text)
    text = re.sub(r'https?://[^\s;]+', lambda m: m.group(0).split('?')[0], text)
    return ' '.join(text.split())[:350]


def valid_direct_url(url):
    if not isinstance(url, str):
        return False
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or p.query or p.fragment:
            return False
        if p.hostname in ('telegra.ph', 'graph.org'):
            return p.path.startswith('/file/') and len(p.path) > 6 and '..' not in p.path
        if p.hostname == 'files.catbox.moe':
            return bool(re.fullmatch(r'/[A-Za-z0-9_-]+\.(jpg|jpeg|png|gif|webp)', p.path, re.I))
    except ValueError:
        pass
    return False


class HostFailure(Exception):
    def __init__(self, diagnostic, *, transient=False, retry_after=600, block=False, limited=False):
        self.diagnostic = sanitize(diagnostic)
        self.transient = transient
        self.limited = limited
        self.retry_after = retry_after
        self.block = block
        super().__init__(self.diagnostic)


def _body(response):
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return str(payload.get('error', payload))
        if isinstance(payload, str):
            return payload
        return str(payload)
    except Exception:
        return str(getattr(response, 'text', 'non-JSON response'))


async def post_image(client, host, endpoint, image, *, userhash=''):
    """One attempt. Return a strictly validated direct image URL or HostFailure."""
    kwargs = dict(files={'file': ('cover.jpg', image, 'image/jpeg')}, timeout=30)
    if host == 'catbox.moe':
        data = {'reqtype': 'fileupload'}
        if userhash:
            data['userhash'] = userhash
        kwargs.update(data=data, files={'fileToUpload': ('cover.jpg', image, 'image/jpeg')},
                      headers={'User-Agent': USER_AGENT})
    try:
        response = await client.post(endpoint, **kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise HostFailure(f'{host}: {type(exc).__name__}', transient=True, retry_after=60, block=True) from exc
    status = response.status_code
    detail = _body(response)
    if userhash:
        detail = detail.replace(userhash, '[hidden]')
    if status == 429 or 'try again later' in detail.lower():
        retry = 180
        try:
            retry = max(30, min(3600, int(getattr(response, 'headers', {}).get('Retry-After', 180))))
        except (ValueError, TypeError):
            pass
        raise HostFailure(f'{host}: HTTP {status}; {detail}', transient=True, retry_after=retry, block=True, limited=True)
    if status != 200:
        raise HostFailure(f'{host}: HTTP {status}; {detail}',
            transient=status >= 500, block=status in (400, 401, 403, 412) or status >= 500)
    if host == 'catbox.moe':
        url = str(getattr(response, 'text', '')).strip()
    else:
        try:
            payload = response.json()
            src = payload[0].get('src') if isinstance(payload, list) and payload and isinstance(payload[0], dict) else None
            url = urljoin(endpoint, src) if isinstance(src, str) else None
        except Exception:
            url = None
    expected_domain = 'files.catbox.moe' if host == 'catbox.moe' else host
    if not valid_direct_url(url) or urlsplit(url).hostname != expected_domain:
        raise HostFailure(f'{host}: HTTP 200 but no valid direct image URL; {detail}', block=True)
    return url


def diagnostics():
    now = time.monotonic()
    return {host: f"{item['diagnostic']} (cooldown {max(0, int(item['until']-now))}s)"
            for host, item in _blocked.items() if item['until'] > now}


async def upload(client, image, *, primary, mirror, userhash='', pace=None):
    errors, limited = [], []
    endpoints = [('telegra.ph', primary), ('graph.org', mirror), ('catbox.moe', CATBOX_ENDPOINT)]
    for host, endpoint in endpoints:
        if endpoint.rstrip('/') in ('https://telegra.ph', 'https://graph.org'):
            endpoint = endpoint.rstrip('/') + '/upload'
        old = _blocked.get(host)
        if old and old['until'] > time.monotonic():
            errors.append(old['diagnostic']);limited.append(old.get('limited', False))
            continue
        try:
            if pace:
                await pace()
            url = await post_image(client, host, endpoint, image, userhash=userhash)
            _blocked.pop(host, None)
            return {'url': url, 'host': host, 'status': 'ok', 'error': None}
        except HostFailure as exc:
            errors.append(exc.diagnostic);limited.append(exc.limited)
            if exc.block:
                _blocked[host] = {'until': time.monotonic() + exc.retry_after,
                                 'diagnostic': exc.diagnostic, 'transient': exc.transient, 'limited': exc.limited}
    unavailable = all(x in diagnostics() for x in HOSTS)
    return {'url': None, 'host': None, 'status': 'rate_limited' if limited and all(limited) else 'failed',
            'error': '; '.join(errors)[:1000], 'all_hosts_unavailable': unavailable}
