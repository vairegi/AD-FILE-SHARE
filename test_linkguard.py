"""Offline test suite for LinkGuard v1.1 (Worker logic mirrored in Python) +
linkguard.py bot client. No Cloudflare, no Telegram, no Mongo needed
(mongomock for db, httpx MockTransport for HTTP)."""
import asyncio
import base64
import hashlib
import hmac as _hmac
import json
import os
import re
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


# ── Python mirror of the Worker's token / claim logic (Door 3 entry) ───────
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
    if not re.fullmatch(r"[A-Za-z0-9\-_]+", b64):
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
    """Mirror of handleFinish decision order (v1.1: UA-bound, IP drift ok)."""
    if not referer.startswith(f"https://{HOST}/"):
        return 403, "bad_referer"
    if token_state == "unknown":
        return 403, "unknown_token"
    if token_state == "used":
        return 403, "reused"
    if claim_ua != finish_ua:
        return 403, "binding_mismatch"
    return 302, "ok"


# ── Python mirror of handleFinish2 (Door 3 exit, v1.1) ──────────────────────
def check_finish2(has_cookie, sess_state, sess_ua, req_ua, sess_ip, req_ip,
                  ref_host, allowed, grant_state, grant_session, sess_id,
                  now, exp, allow_empty_referer=False):
    if not has_cookie:
        return 403, "no_cookie"
    if sess_state is None:
        return 403, "no_session"
    if sess_state not in ("out_to_shortener", "completed"):
        return 403, "not_through_door1"
    if sess_ua != req_ua:
        return 403, "ua_mismatch"
    # sess_ip vs req_ip: drift allowed (logged only)
    if not ref_host:
        if not allow_empty_referer:
            return 403, "bad_shortener_referer"
    elif ref_host not in allowed:
        return 403, "bad_shortener_referer"
    if grant_state == "malformed":
        return 403, "malformed"
    if grant_state == "forged":
        return 403, "forged"
    if now > exp:
        return 403, "expired"
    if grant_state == "unknown":
        return 403, "unknown_grant"
    if grant_state == "used":
        return 403, "reused"
    if grant_session != sess_id:
        return 403, "wrong_session"
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
    """Door 3 entry: the curl 403 matrix + the one 302 flow."""

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

    def test_wrong_ua_403(self):
        self.assertEqual(self._finish(finish_ua="curl/8")[1], "binding_mismatch")

    def test_ip_drift_allowed_302(self):
        self.assertEqual(self._finish(finish_ip="9.9.9.9"), (302, "ok"))

    def test_legit_browser_302(self):
        self.assertEqual(self._finish(), (302, "ok"))


class TestFinish2Matrix(unittest.TestCase):
    """Door 3 EXIT (v1.1): the bypass-killer matrix."""

    def _f2(self, **kw):
        d = dict(has_cookie=True, sess_state="out_to_shortener",
                 sess_ua="UA", req_ua="UA", sess_ip="1.1.1.1", req_ip="1.1.1.1",
                 ref_host="vplink.in", allowed=["vplink.in", "arolinks.com"],
                 grant_state="fresh", grant_session="S1", sess_id="S1",
                 now=1000, exp=4600)
        d.update(kw)
        return check_finish2(**d)

    def test_happy_path_302(self):
        self.assertEqual(self._f2(), (302, "ok"))

    def test_no_cookie_403(self):
        # The exact reported bypass: copied vplink URL opened elsewhere.
        self.assertEqual(self._f2(has_cookie=False)[1], "no_cookie")

    def test_not_through_door1_403(self):
        self.assertEqual(self._f2(sess_state="served")[1], "not_through_door1")

    def test_wrong_ua_403(self):
        self.assertEqual(self._f2(req_ua="curl/8.0")[1], "ua_mismatch")

    def test_ip_drift_allowed(self):
        self.assertEqual(self._f2(req_ip="9.9.9.9"), (302, "ok"))

    def test_bad_shortener_referer_403(self):
        self.assertEqual(self._f2(ref_host="evil.example")[1],
                         "bad_shortener_referer")

    def test_empty_referer_403_by_default(self):
        self.assertEqual(self._f2(ref_host="")[1], "bad_shortener_referer")

    def test_empty_referer_allowed_when_configured(self):
        self.assertEqual(self._f2(ref_host="", allow_empty_referer=True),
                         (302, "ok"))

    def test_arolinks_referer_ok(self):
        self.assertEqual(self._f2(ref_host="arolinks.com"), (302, "ok"))

    def test_forged_grant_403(self):
        self.assertEqual(self._f2(grant_state="forged")[1], "forged")

    def test_expired_grant_403(self):
        self.assertEqual(self._f2(now=9999)[1], "expired")

    def test_reused_grant_403(self):
        # Waiting 150s then replaying the copied link hits this.
        self.assertEqual(self._f2(grant_state="used")[1], "reused")

    def test_wrong_session_403(self):
        # Another verified browser cannot claim this grant.
        self.assertEqual(self._f2(sess_id="S2")[1], "wrong_session")


class TestNicktrick(unittest.TestCase):
    """Hosting a same-host forwarder page must NOT help an attacker."""

    def test_forwarder_cannot_mint_without_session_nonce(self):
        payload = "abc123.attacker-session.9999999999.rnd"
        sig = _hmac.new(b"wrong-key", payload.encode(), hashlib.sha256).hexdigest()
        b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        self.assertEqual(verify_token(f"{b64}.{sig}", 0), "forged")

    def test_forwarder_replay_gets_reused(self):
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
        return patch.object(db, "get_settings", new=lambda: _aret(self.settings))

    def _mock_client(self, handler):
        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient
        return patch.object(httpx, "AsyncClient",
                            lambda **kw: real(transport=transport))

    def test_protect_mints_and_sends_key(self):
        captured = {}

        def handler(request: httpx.Request):
            captured["key"] = request.headers.get("x-admin-key")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "slug": "abc12345",
                "url": "https://linkguard.test.workers.dev/abc12345"})

        async def run():
            with self._mock_client(handler):
                return await linkguard.protect("https://t.me/bot?start=verify_f_t")

        with self._patch():
            url = asyncio.run(run())
        self.assertEqual(url, "https://linkguard.test.workers.dev/abc12345")
        self.assertEqual(captured["key"], "admin-key-123")
        self.assertEqual(captured["body"]["destination"],
                         "https://t.me/bot?start=verify_f_t")
        self.assertNotIn("grant_slug", captured["body"])

    def test_protect_passes_grant_slug(self):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"slug": "p1", "url": "https://x/p1"})

        async def run():
            with self._mock_client(handler):
                return await linkguard.protect("https://x", grant_slug="gg1")

        with self._patch():
            asyncio.run(run())
        self.assertEqual(captured["body"].get("grant_slug"), "gg1")

    def test_mint_return_grant(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "slug": "gg1",
                "finish2_url": "https://linkguard.test.workers.dev/finish2?s=gg1&t=tok"})

        async def run():
            with self._mock_client(handler):
                return await linkguard.mint_return_grant(
                    "https://t.me/bot?start=verify_f_t")

        with self._patch():
            d = asyncio.run(run())
        self.assertTrue(captured["url"].endswith("/api/admin/mint2"))
        self.assertEqual(captured["body"]["final_destination"],
                         "https://t.me/bot?start=verify_f_t")
        self.assertIn("ref_hosts", captured["body"])
        self.assertEqual(d["slug"], "gg1")

    def test_mint_return_grant_fails_open(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        async def run():
            with self._mock_client(handler):
                return await linkguard.mint_return_grant("https://x")

        with self._patch():
            self.assertIsNone(asyncio.run(run()))

    def test_push_ref_hosts(self):
        def handler(request):
            return httpx.Response(200, json={"ok": True, "count": 0})

        async def run():
            with self._mock_client(handler):
                return await linkguard.push_ref_hosts()

        with self._patch():
            self.assertTrue(asyncio.run(run()))

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
            with self._mock_client(handler):
                return await linkguard.protect("https://x")

        with self._patch():
            self.assertIsNone(asyncio.run(run()))

    def test_revoke_ok(self):
        def handler(request):
            return httpx.Response(200, json={"ok": True})

        async def run():
            with self._mock_client(handler):
                return await linkguard.revoke("abc12345")

        with self._patch():
            self.assertTrue(asyncio.run(run()))


async def _aret(v):
    return v


if __name__ == "__main__":
    unittest.main(verbosity=2)
