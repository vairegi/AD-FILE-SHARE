"""Tests for the pluggable multi-host uploader (cover_hosts.py) — v1.7 set.

No network access: every adapter is verified against a mock httpx client with
canned responses matching the live-probe results from 2026-10-18.
"""
import io
import os
import pytest

import cover_hosts as ch


def _jpeg_bytes() -> bytes:
    try:
        from PIL import Image  # type: ignore
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), (10, 20, 30)).save(buf, "JPEG", quality=70)
        return buf.getvalue()
    except Exception:
        return b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ch._blocked.clear()
    for k in ("CATBOX_USERHASH", "BUNNY_STORAGE_ZONE", "BUNNY_API_KEY",
              "BUNNY_PULL_ZONE_URL", "LITTERBOX_TTL"):
        monkeypatch.delenv(k, raising=False)
    yield
    ch._blocked.clear()


class TestValidUrl:
    def test_accepts_https_no_query(self):
        assert ch.valid_direct_url("https://telegra.ph/file/abcd1234.jpg")

    def test_rejects_http_query_fragment_userinfo(self):
        assert not ch.valid_direct_url("http://telegra.ph/file/x.jpg")
        assert not ch.valid_direct_url("https://telegra.ph/file/x.jpg?q=1")
        assert not ch.valid_direct_url("https://telegra.ph/file/x.jpg#f")
        assert not ch.valid_direct_url("https://u:p@telegra.ph/file/x.jpg")
        assert not ch.valid_direct_url("")
        assert not ch.valid_direct_url(None)


class _Resp:
    def __init__(self, status, text="", json_obj=None, headers=None):
        self.status_code = status
        self.text = text
        self._json = json_obj
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _Recording:
    def __init__(self, routes):
        self._routes = list(routes)
        self.calls = []

    async def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        for prefix, resp in self._routes:
            if url.startswith(prefix):
                return resp
        raise AssertionError(f"unexpected POST {url}")

    async def put(self, url, **kw):
        self.calls.append(("put", url, kw))
        for prefix, resp in self._routes:
            if url.startswith(prefix):
                return resp
        raise AssertionError(f"unexpected PUT {url}")


# ─── adapter success paths ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_telegraph_ok():
    client = _Recording([("https://telegra.ph",
        _Resp(200, json_obj=[{"src": "/file/abcdef0123.jpg"}]))])
    url = await ch.post_image(client, "telegra.ph", _jpeg_bytes())
    assert url == "https://telegra.ph/file/abcdef0123.jpg"


@pytest.mark.asyncio
async def test_graph_ok():
    client = _Recording([("https://graph.org",
        _Resp(200, json_obj=[{"src": "/file/abcdef0123.jpg"}]))])
    url = await ch.post_image(client, "graph.org", _jpeg_bytes())
    assert url == "https://graph.org/file/abcdef0123.jpg"


@pytest.mark.asyncio
async def test_telegraph_400_blocked_then_graph_fallback():
    client = _Recording([
        ("https://telegra.ph", _Resp(400, '"Unknown error"')),
        ("https://graph.org", _Resp(200, json_obj=[{"src": "/file/zz.jpg"}])),
    ])
    res = await ch.upload(client, _jpeg_bytes(), preferred="telegra.ph")
    assert res["status"] == "ok" and res["host"] == "graph.org"


@pytest.mark.asyncio
async def test_catbox_with_userhash_ok():
    os.environ["CATBOX_USERHASH"] = "hash123"
    client = _Recording([("https://catbox.moe",
        _Resp(200, "https://files.catbox.moe/abc.jpg"))])
    url = await ch.post_image(client, "catbox.moe", _jpeg_bytes())
    assert url == "https://files.catbox.moe/abc.jpg"
    # userhash actually sent
    _, _, kw = client.calls[0]
    assert kw["data"]["userhash"] == "hash123"


@pytest.mark.asyncio
async def test_catbox_412_marks_needs_creds_and_sanitizes():
    client = _Recording([("https://catbox.moe",
        _Resp(412, "Invalid uploader"))])
    with pytest.raises(ch.HostFailure) as ei:
        await ch.post_image(client, "catbox.moe", _jpeg_bytes())
    assert ei.value.needs_creds is True
    assert "CATBOX_USERHASH" in ei.value.diagnostic


@pytest.mark.asyncio
async def test_litterbox_ok_and_default_ttl_72h():
    client = _Recording([("https://litterbox.catbox.moe",
        _Resp(200, "https://litter.catbox.moe/xyz.jpg"))])
    url = await ch.post_image(client, "litterbox", _jpeg_bytes())
    assert url == "https://litter.catbox.moe/xyz.jpg"
    _, _, kw = client.calls[0]
    assert kw["data"]["time"] == "72h"


@pytest.mark.asyncio
async def test_litterbox_marks_temporary_in_upload_result():
    client = _Recording([("https://litterbox.catbox.moe",
        _Resp(200, "https://litter.catbox.moe/xyz.jpg"))])
    res = await ch.upload(client, _jpeg_bytes(), preferred="litterbox")
    assert res["status"] == "ok" and res["host"] == "litterbox"
    assert "expires" in (res.get("warning") or "")


@pytest.mark.asyncio
async def test_bunny_needs_creds():
    with pytest.raises(ch.HostFailure) as ei:
        await ch.post_image(_Recording([]), "bunny.net", _jpeg_bytes())
    assert ei.value.needs_creds is True


@pytest.mark.asyncio
async def test_removed_hosts_rejected():
    for host in ("0x0.st", "imgbb", "cloudflare-r2", "backblaze-b2"):
        with pytest.raises(ch.HostFailure) as ei:
            await ch.post_image(_Recording([]), host, _jpeg_bytes())
        assert "removed" in ei.value.diagnostic


# ─── ordering / fallback ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_upload_prefers_catbox_by_default():
    client = _Recording([("https://catbox.moe",
        _Resp(200, "https://files.catbox.moe/first.jpg"))])
    os.environ["CATBOX_USERHASH"] = "h"
    res = await ch.upload(client, _jpeg_bytes())
    assert res["host"] == "catbox.moe"


@pytest.mark.asyncio
async def test_upload_all_fail_reports_errors():
    routes = [
        ("https://catbox.moe", _Resp(412, "Invalid uploader")),
        ("https://telegra.ph", _Resp(400, '"Unknown error"')),
        ("https://graph.org", _Resp(400, '"Unknown error"')),
        ("https://litterbox.catbox.moe", _Resp(500, "boom")),
    ]
    client = _Recording(routes)
    res = await ch.upload(client, _jpeg_bytes())
    assert res["status"] == "failed" and res["url"] is None
    assert "catbox.moe" in res["error"] and "telegra.ph" in res["error"]


@pytest.mark.asyncio
async def test_circuit_breaker_skips_blocked_host():
    routes = [("https://telegra.ph", _Resp(429, "Try again later"))]
    client = _Recording(routes)
    res1 = await ch.upload(client, _jpeg_bytes(), preferred="telegra.ph")
    assert res1["status"] == "failed"
    calls_after_first = len(client.calls)
    res2 = await ch.upload(client, _jpeg_bytes(), preferred="telegra.ph")
    assert res2["status"] == "failed"
    assert len(client.calls) == calls_after_first  # blocked → not retried


# ─── test_all() + report ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_test_all_covers_exactly_the_pruned_hosts(monkeypatch):
    routes = [
        ("https://catbox.moe", _Resp(200, "https://files.catbox.moe/a.jpg")),
        ("https://telegra.ph", _Resp(400, '"Unknown error"')),
        ("https://graph.org", _Resp(400, '"Unknown error"')),
        ("https://litterbox.catbox.moe", _Resp(200, "https://litter.catbox.moe/b.jpg")),
    ]
    os.environ["CATBOX_USERHASH"] = "h"
    client = _Recording(routes)
    report = await ch.test_all(client, _jpeg_bytes())
    by_host = {r["host"]: r for r in report}
    assert set(by_host) == set(ch.HOSTS)  # exactly the 5 kept hosts
    assert by_host["catbox.moe"]["status"] == "ok"
    assert by_host["litterbox"]["status"] == "ok"
    assert "expires" in by_host["litterbox"]["url"]
    assert by_host["telegra.ph"]["status"] == "fail"
    assert by_host["bunny.net"]["status"] == "skipped-needs-creds"
    # removed hosts are NOT in the probe set
    assert "0x0.st" not in by_host and "imgbb" not in by_host


def test_render_report_html_escapes_and_mentions_removals():
    report = [
        {"host": "catbox.moe", "status": "ok", "url": "https://files.catbox.moe/<x>.jpg",
         "elapsed_ms": "312", "legal_adult": "OK", "error": ""},
        {"host": "telegra.ph", "status": "fail", "url": "",
         "elapsed_ms": "99", "legal_adult": "OK", "error": 'HTTP 400; <b>"Unknown"</b>'},
    ]
    text = ch.render_report_html(report)
    assert "catbox.moe" in text and "telegra.ph" in text
    assert "&lt;b&gt;" in text  # error text escaped — safe for parse_mode=HTML
    assert "0x0.st" in text and "imgbb" in text  # removal reasons listed
    assert "ok: 1" in text and "fail: 1" in text
