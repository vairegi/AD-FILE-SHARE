"""Tests for the pluggable multi-host uploader (cover_hosts.py).

These tests do NOT touch the network — every adapter is verified against a
mock httpx.AsyncClient whose post/put methods return canned responses.
"""

import base64
import io
import os
import pytest

import cover_hosts as ch
from cover_hosts import (
    post_image,
    test_all,
    upload,
    LEGAL_ADULT_STATUS,
    valid_direct_url,
)


def test_all_unused():
    # silence the "imported but unused" warning; this assignment documents intent
    return (post_image, test_all, upload, LEGAL_ADULT_STATUS, valid_direct_url)


def _jpeg_bytes() -> bytes:
    try:
        from PIL import Image  # type: ignore
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), (10, 20, 30)).save(buf, "JPEG", quality=70)
        return buf.getvalue()
    except Exception:
        # Minimal placeholder JPEG bytes
        return b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"


# ─── url validator ────────────────────────────────────────────────────────
class TestValidUrl:
    def test_accepts_https_no_query(self):
        assert valid_direct_url("https://telegra.ph/file/abcd1234.jpg")
    def test_rejects_http(self):
        assert not valid_direct_url("http://telegra.ph/file/abcd1234.jpg")
    def test_rejects_query(self):
        assert not valid_direct_url("https://telegra.ph/file/abcd1234.jpg?x=1")
    def test_rejects_userinfo(self):
        assert not valid_direct_url("https://user:pass@telegra.ph/file/x.jpg")
    def test_rejects_fragment(self):
        assert not valid_direct_url("https://telegra.ph/file/abcd1234.jpg#frag")
    def test_rejects_garbage(self):
        assert not valid_direct_url("not a url")
        assert not valid_direct_url(None)
        assert not valid_direct_url("")


# ─── test_all() with a single recording client ─────────────────────────────
class _Recording:
    def __init__(self, route_table):
        self._routes = list(route_table)
        self.calls = []

    async def post(self, url, **kw):
        from unittest.mock import AsyncMock
        self.calls.append(("post", url, kw))
        # Walk routes until one matches url prefix
        for prefix, response in self._routes:
            if url.startswith(prefix):
                return response
        raise AssertionError(f"unexpected POST to {url}")

    async def put(self, url, **kw):
        self.calls.append(("put", url, kw))
        for prefix, response in self._routes:
            if url.startswith(prefix):
                return response
        raise AssertionError(f"unexpected PUT to {url}")


def _json_resp(status, body, ctype="application/json", headers=None):
    class _Resp:
        def __init__(self):
            self.status_code = status
            self.text = (
                '"OK error"' if ctype == "application/json" and isinstance(body, str)
                else (body if isinstance(body, str) else str(body))
            )
            try:
                import json as _json
                self._json_obj = _json.loads(body) if isinstance(body, str) and ctype.startswith("application/json") else None
            except Exception:
                self._json_obj = None
            self.headers = headers or {}

        def json(self):
            if self._json_obj is None:
                import json as _json
                self._json_obj = _json.loads(self.text)
            return self._json_obj
    return _Resp()


def _txt_resp(status, body, headers=None):
    class _Resp:
        def __init__(self):
            self.status_code = status
            self.text = body
            self._json_obj = None
            self.headers = headers or {}
        def json(self):
            raise ValueError("not json")
    return _Resp()


# ─── each adapter's success path ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_telegraph_ok():
    ch._blocked.clear()
    client = _Recording([
        ("https://telegra.ph",
         _json_resp(200, '[{"src":"/file/abcdef0123abcdef0123abcd.jpg"}]')),
    ])
    url = await post_image(client, "telegra.ph", _jpeg_bytes())
    assert url == "https://telegra.ph/file/abcdef0123abcdef0123abcd.jpg"


@pytest.mark.asyncio
async def test_graph_ok():
    ch._blocked.clear()
    client = _Recording([
        ("https://graph.org",
         _json_resp(200, '[{"src":"/file/abcdef0123abcdef0123abcd.jpg"}]')),
    ])
    url = await post_image(client, "graph.org", _jpeg_bytes())
    assert url == "https://graph.org/file/abcdef0123abcdef0123abcd.jpg"


@pytest.mark.asyncio
async def test_catbox_ok_with_userhash():
    ch._blocked.clear()
    os.environ["CATBOX_USERHASH"] = "secret-hash"
    client = _Recording([
        ("https://catbox.moe",
         _txt_resp(200, "https://files.catbox.moe/abc.jpg")),
    ])
    url = await post_image(client, "catbox.moe", _jpeg_bytes())
    assert url == "https://files.catbox.moe/abc.jpg"


@pytest.mark.asyncio
async def test_catbox_anonymous_412_marks_needs_creds():
    ch._blocked.clear()
    os.environ.pop("CATBOX_USERHASH", None)
    client = _Recording([
        ("https://catbox.moe",
         _txt_resp(412, "Invalid uploader", headers={"Content-Type": "text/html"})),
    ])
    with pytest.raises(ch.HostFailure) as ei:
        await post_image(client, "catbox.moe", _jpeg_bytes())
    assert ei.value.needs_creds is True
    assert "Invalid uploader" in ei.value.diagnostic


@pytest.mark.asyncio
async def test_litterbox_ok():
    ch._blocked.clear()
    client = _Recording([
        ("https://litterbox.catbox.moe",
         _txt_resp(200, "https://litter.catbox.moe/abc123.jpg")),
    ])
    url = await post_image(client, "litterbox", _jpeg_bytes())
    assert url == "https://litter.catbox.moe/abc123.jpg"


@pytest.mark.asyncio
async def test_0x0_ok():
    ch._blocked.clear()
    client = _Recording([
        ("https://0x0.st",
         _txt_resp(200, "https://0x0.st/abc.jpg")),
    ])
    url = await post_image(client, "0x0.st", _jpeg_bytes())
    assert url == "https://0x0.st/abc.jpg"


@pytest.mark.asyncio
async def test_imgbb_needs_key():
    ch._blocked.clear()
    os.environ.pop("IMGBB_API_KEY", None)
    client = _Recording([])
    with pytest.raises(ch.HostFailure) as ei:
        await post_image(client, "imgbb", _jpeg_bytes())
    assert ei.value.needs_creds is True


@pytest.mark.asyncio
async def test_bunny_needs_all_creds():
    ch._blocked.clear()
    for k in ("BUNNY_STORAGE_ZONE", "BUNNY_API_KEY", "BUNNY_PULL_ZONE_URL"):
        os.environ.pop(k, None)
    client = _Recording([])
    with pytest.raises(ch.HostFailure) as ei:
        await post_image(client, "bunny.net", _jpeg_bytes())
    assert ei.value.needs_creds is True


# ─── upload() ordering ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_upload_honors_preferred_when_ok():
    ch._blocked.clear()
    client = _Recording([
        ("https://0x0.st", _txt_resp(200, "https://0x0.st/abc.jpg")),
    ])
    res = await upload(client, _jpeg_bytes(), preferred="0x0.st")
    assert res["status"] == "ok"
    assert res["host"] == "0x0.st"


@pytest.mark.asyncio
async def test_upload_falls_back_when_preferred_fails():
    ch._blocked.clear()
    os.environ.pop("CATBOX_USERHASH", None)
    client = _Recording([
        ("https://catbox.moe", _txt_resp(412, "Invalid uploader")),
        ("https://litterbox.catbox.moe", _txt_resp(200, "https://litter.catbox.moe/abc.jpg")),
    ])
    res = await upload(client, _jpeg_bytes(), preferred="catbox.moe")
    assert res["status"] == "ok"
    assert res["host"] == "litterbox"


@pytest.mark.asyncio
async def test_upload_marks_failed_when_all_fail():
    ch._blocked.clear()
    os.environ.pop("CATBOX_USERHASH", None)
    client = _Recording([
        ("https://telegra.ph", _json_resp(400, '"Unknown error"')),
        ("https://graph.org", _json_resp(400, '"Unknown error"')),
        ("https://catbox.moe", _txt_resp(412, "Invalid uploader")),
        ("https://litterbox.catbox.moe", _txt_resp(500, "Internal")),
        ("https://0x0.st", _txt_resp(502, "Bad Gateway")),
    ])
    res = await upload(client, _jpeg_bytes())
    assert res["status"] == "failed"
    assert res["url"] is None


# ─── test_all() runs every adapter exactly once ───────────────────────────
@pytest.mark.asyncio
async def test_test_all_runs_every_host():
    ch._blocked.clear()
    os.environ.pop("CATBOX_USERHASH", None)
    for k in (
        "IMGBB_API_KEY", "BUNNY_STORAGE_ZONE", "BUNNY_API_KEY",
        "BUNNY_PULL_ZONE_URL",
    ):
        os.environ.pop(k, None)

    routes = [
        ("https://telegra.ph", _json_resp(400, '"Unknown error"')),
        ("https://graph.org", _json_resp(400, '"Unknown error"')),
        ("https://catbox.moe", _txt_resp(412, "Invalid uploader")),
        ("https://litterbox.catbox.moe", _txt_resp(200, "https://litter.catbox.moe/abc.jpg")),
        ("https://0x0.st", _txt_resp(200, "https://0x0.st/abc.jpg")),
        # imgbb / bunny / cloudflare-r2 / backblaze-b2 all need creds
    ]
    client = _Recording(routes)
    report = await test_all(client, _jpeg_bytes())
    by_host = {r["host"]: r for r in report}
    assert set(by_host) == set(ch.HOSTS)
    assert by_host["litterbox"]["status"] == "ok"
    assert by_host["litterbox"]["url"].startswith("https://litter.catbox.moe/")
    assert by_host["0x0.st"]["status"] == "ok"
    # Catbox anonymous 412 is a "needs creds" status — owner must set
    # CATBOX_USERHASH in Render. We treat that as a separate status.
    assert by_host["catbox.moe"]["status"] in ("skipped-needs-creds", "fail")
    assert by_host["catbox.moe"]["error"]
    assert by_host["telegra.ph"]["status"] == "fail"
    assert by_host["imgbb"]["status"] == "skipped-needs-creds"
    assert by_host["bunny.net"]["status"] == "skipped-needs-creds"
    assert by_host["cloudflare-r2"]["status"] == "skipped-needs-creds"
    assert by_host["backblaze-b2"]["status"] == "skipped-needs-creds"
    # adult-ToS info table
    assert "BANNED" in ch.LEGAL_ADULT_STATUS["imgbb"]
    assert "OK" in ch.LEGAL_ADULT_STATUS["bunny.net"]


def test_render_report_md_contains_all_hosts():
    report = [
        {"host": "telegra.ph", "status": "ok", "url": "https://telegra.ph/file/x.jpg",
         "elapsed_ms": "312", "legal_adult": "OK", "gating": "anonymous", "error": ""},
        {"host": "imgbb", "status": "skipped-needs-creds", "url": "",
         "elapsed_ms": "0", "legal_adult": "BANNED", "gating": "registered", "error": "imgbb: IMGBB_API_KEY missing"},
    ]
    text = ch.render_report_md(report)
    assert "telegra.ph" in text
    assert "imgbb" in text
    assert "ok: 1" in text
    assert "skipped (no creds): 1" in text
