"""Offline test suite for LinkGuard (Worker logic mirrored in Python) +
linkguard.py bot client. No Cloudflare, no Telegram, no Mongo needed
(mongomock for db, httpx MockTransport for HTTP)."""
import asyncio
import base64
import hashlib
import hmac as _hmac
import importlib
import json
import os
import sys
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

import mongomock
import httpx

import config
import db
import linkguard

SECRET = "ab" * 32
HOST = "linkguard.test.workers.dev"


# ── Python mirror of the Worker's token / claim logic (Door 2+3 core) ──────
def mint_token(slug, session_id, exp, rnd):
    payload = f"{slug}.{session_id}.{exp}.{rnd}"
    sig = _hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{b64}.{sig}"


def verify_token(token, now):
    dot = token.rfind(".")
    if dot < 1:
        return "malformed"
    b64, sig = token[:dot], token[dot + 1:]
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9\-_]+", b64):
        return "malformed"
    try:
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
    except Exception:
        return "malformed"
    expect = _hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(sig, expect):
        return "forged"
    parts = payload.split(".")
    if len(parts) != 4:
        return "malformed"
    if now > int(parts[2]):
        return "expired"
    return "ok"


def check_finish(referer, token_state, claim_ip, finish_ip, claim_ua, finish_ua):
    """Mirror of handleFinish decision order."""
    if not referer.startswith(f"https://{HOST}/"):
        return 403, "bad_referer"
    if token_state == "unknown":
        return 403, "unknown_token"
    if token_state == "used":
        return 403, "reused"
    if claim_ip != finish_ip or claim_ua != finish_ua:
        return 403, "binding_mismatch"
    return 302, "ok"


class TestTokenCrypto(unittest.TestCase):
    def test_valid_token_passes(self):
        exp = int(time.time()) + 90
        t = mint_token("abc123", str(uuid.uuid4()), exp, "x" * 32)
        self.assertEqual(verify_token(t, int(time.time())), "ok")

    def test_forged_signature_rejected(self):
        exp = int(time.time()) + 90
        t = mint_token("abc123", "s", exp, "r")
        bad = t[:-2] + ("00" if not t.endswith("00") else "11")
        self.assertEqual(verify_token(bad, int(time.time())), "forged")

    def test_expired_token_rejected(self):
        exp = int(time.time()) + 5
        t = mint_token("abc123", "s", exp, "r")
        self.assertEqual(verify_token(t, int(time.time()) + 10), "expired")

    def test_malformed_rejected(self):
        self.assertEqual(verify_token("not-a-token", 0), "malformed")
        self.assertEqual(verify_token("!!!.!!!", 0), "malformed")

    def test_tampered_payload_rejected(self):
        exp = int(time.time()) + 90
        t = mint_token("abc123", "s", exp, "r")
        b64, sig = t.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode()
        forged_payload = payload.replace("abc123", "zzz999")
        fb64 = base64.urlsafe_b64encode(forged_payload.encode()).decode().rstrip("=")
        self.assertEqual(verify_token(f"{fb64}.{sig}", int(time.time())), "forged")


class TestFinishMatrix(unittest.TestCase):
    """The curl 403 matrix + the one 302 flow."""

    def _finish(self, **kw):
        d = dict(referer=f"https://{HOST}/abc123", token_state="fresh",
                 claim_ip="1.2.3.4", finish_ip="1.2.3.4",
                 claim_ua="UA", finish_ua="UA")
        d.update(kw)
        return check_finish(**d)

    def test_no_referer_403(self):
        self.assertEqual(self._finish(referer="")[1], "bad_referer")

    def test_foreign_referer_403(self):
        self.assertEqual(
            self._finish(referer="https://evil.example/x")[1], "bad_referer")

    def test_reused_403(self):
        self.assertEqual(self._finish(token_state="used")[1], "reused")

    def test_unknown_403(self):
        self.assertEqual(self._finish(token_state="unknown")[1], "unknown_token")

    def test_wrong_ip_403(self):
        self.assertEqual(self._finish(finish_ip="9.9.9.9")[1], "binding_mismatch")

    def test_wrong_ua_403(self):
        self.assertEqual(self._finish(finish_ua="curl/8")[1], "binding_mismatch")

    def test_legit_browser_302(self):
        self.assertEqual(self._finish(), (302, "ok"))


class TestNicktrick(unittest.TestCase):
    """Hosting a same-host forwarder page must NOT help an attacker."""

    def test_forwarder_cannot_mint_without_session_nonce(self):
        # Attacker who only knows the slug cannot guess session_id+nonce,
        # and without a Turnstile solve claim is denied — tested via crypto:
        # a token with attacker-chosen fields fails HMAC.
        payload = "abc123.attacker-session.9999999999.rnd"
        sig = _hmac.new(b"wrong-key", payload.encode(), hashlib.sha256).hexdigest()
        b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        self.assertEqual(verify_token(f"{b64}.{sig}", 0), "forged")

    def test_forwarder_replay_gets_reused(self):
        # Even if attacker captures a real token, the legit user's claim has
        # already burned it.
        self.assertEqual(
            check_finish(f"https://{HOST}/abc123", "used",
                         "1.1.1.1", "1.1.1.1", "U", "U")[1], "reused")


# ── linkguard.py bot client, with mocked settings + HTTP ────────────────────
class TestBotClient(unittest.TestCase):
    def setUp(self):
        self._client = mongomock.MongoClient()
        db._db = self._client["test_linkguard"]
        self.settings = {
            "linkguard_enabled": True,
            "linkguard_base": "https://linkguard.test.workers.dev",
            "linkguard_key": "admin-key-123",
        }

    def tearDown(self):
        self._client.close()

    def _patch(self):
        return patch.object(db, "get_settings",
                            new=lambda: _aret(self.settings))

    def test_protect_mints_and_sends_key(self):
        captured = {}

        def handler(request: httpx.Request):
            captured["url"] = str(request.url)
            captured["key"] = request.headers.get("x-admin-key")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "slug": "abc12345",
                "url": "https://linkguard.test.workers.dev/abc12345"})

        async def run():
            transport = httpx.MockTransport(handler)
            real = httpx.AsyncClient
            with patch.object(httpx, "AsyncClient",
                              lambda **kw: real(transport=transport)):
                return await linkguard.protect("https://t.me/bot?start=verify_f_t")

        with self._patch():
            url = asyncio.run(run())
        self.assertEqual(url, "https://linkguard.test.workers.dev/abc12345")
        self.assertEqual(captured["key"], "admin-key-123")
        self.assertEqual(captured["body"]["destination"],
                         "https://t.me/bot?start=verify_f_t")

    def test_disabled_returns_none(self):
        self.settings["linkguard_enabled"] = False
        with self._patch():
            self.assertIsNone(asyncio.run(linkguard.protect("https://x")))

    def test_unconfigured_returns_none(self):
        self.settings["linkguard_base"] = ""
        with self._patch():
            self.assertIsNone(asyncio.run(linkguard.protect("https://x")))

    def test_http_error_fails_open(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        async def run():
            transport = httpx.MockTransport(handler)
            real = httpx.AsyncClient
            with patch.object(httpx, "AsyncClient",
                              lambda **kw: real(transport=transport)):
                return await linkguard.protect("https://x")

        with self._patch():
            self.assertIsNone(asyncio.run(run()))  # caller falls back, no crash

    def test_revoke_ok(self):
        def handler(request):
            return httpx.Response(200, json={"ok": True})

        async def run():
            transport = httpx.MockTransport(handler)
            real = httpx.AsyncClient
            with patch.object(httpx, "AsyncClient",
                              lambda **kw: real(transport=transport)):
                return await linkguard.revoke("abc12345")

        with self._patch():
            self.assertTrue(asyncio.run(run()))


async def _aret(v):
    return v


if __name__ == "__main__":
    unittest.main(verbosity=2)
