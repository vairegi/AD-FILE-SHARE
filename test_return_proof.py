"""v1.3 return-proof suite: Worker-confirmed delivery gate for Bots 1+2.

Real checks against the patched modules (linkguard.py, bot1.py, db.py). HTTP is
mocked with httpx.MockTransport; Mongo is mongomock. No network, no Telegram.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("BOT1_TOKEN", "111111111:TESTTOKENBOT1")
os.environ.setdefault("BOT2_TOKEN", "222222222:TESTTOKENBOT2")

import httpx
import mongomock
from mongomock_motor import AsyncMongoMockClient

import bot1
import db
import linkguard

BASE = "https://linkguard.test.workers.dev"


async def _aret(v):
    return v


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return type("M", (), {"message_id": 1})()

    def texts(self):
        return "\n".join(t for _, t in self.sent)


class ClientCase(unittest.TestCase):
    def setUp(self):
        self._client = mongomock.MongoClient()
        db._db = self._client["test_return_proof"]
        self.settings = {
            "linkguard_enabled": True,
            "linkguard_base": BASE,
            "linkguard_key": "admin-key",
        }

    def tearDown(self):
        self._client.close()

    def _patch_settings(self):
        return patch.object(db, "get_settings",
                            new=lambda: _aret(dict(self.settings)))

    def _mock_http(self, handler):
        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient
        return patch.object(httpx, "AsyncClient",
                            lambda **kw: real(transport=transport))

    # ── linkguard.release_status ───────────────────────────────────
    def test_release_status_released(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["key"] = request.headers.get("x-admin-key")
            seen["body"] = request.content
            return httpx.Response(200, json={
                "ok": True, "released": True, "reason": "released",
                "released_at": 1700000000000, "release_site": "cross-site",
                "departed_at": 1699999000000})

        async def run():
            with self._mock_http(handler):
                return await linkguard.release_status("gg1")

        with self._patch_settings():
            d = asyncio.run(run())
        self.assertEqual(seen["path"], "/api/admin/release_status")
        self.assertEqual(seen["key"], "admin-key")
        self.assertIn(b"gg1", seen["body"])
        self.assertTrue(d["released"])
        self.assertEqual(d["release_site"], "cross-site")

    def test_release_status_not_released(self):
        def handler(request):
            return httpx.Response(200, json={
                "ok": True, "released": False, "reason": "awaiting_return"})

        async def run():
            with self._mock_http(handler):
                return await linkguard.release_status("gg1")

        with self._patch_settings():
            d = asyncio.run(run())
        self.assertFalse(d["released"])
        self.assertEqual(d["reason"], "awaiting_return")

    def test_release_status_http_error_is_unknown(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(503, text="down")

        async def run():
            with self._mock_http(handler):
                return await linkguard.release_status("gg1")

        with self._patch_settings():
            self.assertIsNone(asyncio.run(run()))
        self.assertEqual(calls["n"], 2, "worker 5xx is retried exactly once")

    def test_release_status_transport_error_is_unknown(self):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        async def run():
            with self._mock_http(handler):
                return await linkguard.release_status("gg1")

        with self._patch_settings():
            self.assertIsNone(asyncio.run(run()))

    def test_release_status_non_ok_payload_is_unknown(self):
        def handler(request):
            return httpx.Response(200, json={"error": "unauthorized"})

        async def run():
            with self._mock_http(handler):
                return await linkguard.release_status("gg1")

        with self._patch_settings():
            self.assertIsNone(asyncio.run(run()))

    def test_release_status_unconfigured_makes_no_request(self):
        self.settings["linkguard_base"] = ""
        hit = {"n": 0}

        def handler(request):
            hit["n"] += 1
            return httpx.Response(200, json={"ok": True, "released": True})

        async def run():
            with self._mock_http(handler):
                return await linkguard.release_status("gg1")

        with self._patch_settings():
            self.assertIsNone(asyncio.run(run()))
        self.assertEqual(hit["n"], 0)

    def test_release_status_blank_slug_makes_no_request(self):
        hit = {"n": 0}

        def handler(request):
            hit["n"] += 1
            return httpx.Response(200, json={"ok": True, "released": True})

        async def run():
            with self._mock_http(handler):
                return await linkguard.release_status("   ")

        with self._patch_settings():
            self.assertIsNone(asyncio.run(run()))
        self.assertEqual(hit["n"], 0)

    # ── the pure verdict ───────────────────────────────────────────
    def test_verdict_matrix(self):
        V = linkguard.return_proof_verdict
        self.assertEqual(V(None, has_grant=False, worker_configured=True), "allow")
        self.assertEqual(V({"released": True}, has_grant=True, worker_configured=True), "allow")
        self.assertEqual(V({"released": False}, has_grant=True, worker_configured=True), "deny")
        self.assertEqual(V(None, has_grant=True, worker_configured=True), "retry")
        self.assertEqual(V({"released": True}, has_grant=True, worker_configured=False), "retry")
        self.assertEqual(V({"released": False}, has_grant=True, worker_configured=False), "retry")


class GateCase(unittest.TestCase):
    """bot1._return_proof end-to-end with mocked data + Telegram."""

    def setUp(self):
        self._client = mongomock.MongoClient()
        db._db = self._client["test_return_proof_gate"]
        self.bot = FakeBot()
        self.burned = []
        self.banned = []
        self.strikes = []
        self.alerts = []

        async def mark_used(token):
            self.burned.append(token)

        async def set_banned(uid, flag):
            self.banned.append((uid, flag))

        async def add_strike(uid):
            self.strikes.append(uid)

        async def admin_ids():
            return [900]

        self._p = [
            patch.object(db, "mark_token_used", new=mark_used),
            patch.object(db, "set_banned", new=set_banned),
            patch.object(db, "add_strike", new=add_strike),
            patch.object(db, "list_admin_ids", new=admin_ids),
            patch.object(db, "get_settings", new=lambda: _aret({"ban_message": "banned"})),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        self._client.close()

    def _run(self, doc, released=None, enabled=True, raise_exc=None):
        async def status(slug):
            if raise_exc:
                raise raise_exc
            return released

        async def en():
            return enabled

        with patch.object(linkguard, "release_status", new=status), \
             patch.object(linkguard, "enabled", new=en):
            return asyncio.run(bot1._return_proof(self.bot, 555, 777, doc))

    def test_allow_verified_release_sends_nothing(self):
        doc = {"token": "t1", "grant_slug": "gg1"}
        self.assertEqual(self._run(doc, released={"released": True}), "allow")
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.burned, [])
        self.assertEqual(self.banned, [])

    def test_deny_withholds_file_and_never_bans(self):
        doc = {"token": "t2", "grant_slug": "gg2"}
        verdict = self._run(doc, released={"released": False, "reason": "pasted_url"})
        self.assertEqual(verdict, "deny")
        self.assertEqual(self.burned, ["t2"], "the bypassed token is burned")
        self.assertEqual(self.banned, [], "no automatic ban on a failed proof")
        self.assertEqual(self.strikes, [])
        text = self.bot.texts()
        self.assertIn("could not be confirmed", text)
        self.assertIn("do not paste the final link", text)

    def test_retry_is_never_punitive_and_keeps_the_token(self):
        doc = {"token": "t3", "grant_slug": "gg3"}
        verdict = self._run(doc, released=None)
        self.assertEqual(verdict, "retry")
        self.assertEqual(self.burned, [], "unreachable Worker must not burn the link")
        self.assertEqual(self.banned, [])
        self.assertEqual(self.strikes, [])
        self.assertIn("taking longer than usual", self.bot.texts())

    def test_worker_exception_becomes_retry(self):
        doc = {"token": "t4", "grant_slug": "gg4"}
        verdict = self._run(doc, raise_exc=RuntimeError("boom"))
        self.assertEqual(verdict, "retry")
        self.assertEqual(self.burned, [])

    def test_worker_disabled_with_grant_is_retry_not_allow(self):
        doc = {"token": "t5", "grant_slug": "gg5"}
        verdict = self._run(doc, enabled=False, released={"released": True})
        self.assertEqual(verdict, "retry",
                         "a grant-bearing token is never released without confirmation")
        self.assertEqual(self.burned, [])

    def test_legacy_token_without_grant_keeps_timing_gate_only(self):
        doc = {"token": "t6"}  # minted before v1.3
        called = {"n": 0}

        async def status(slug):
            called["n"] += 1
            return None

        async def en():
            return True

        with patch.object(linkguard, "release_status", new=status), \
             patch.object(linkguard, "enabled", new=en):
            verdict = asyncio.run(bot1._return_proof(self.bot, 555, 777, doc))
        self.assertEqual(verdict, "allow")
        self.assertEqual(called["n"], 0, "no needless Worker round-trip")
        self.assertEqual(self.bot.sent, [])


class ProcessVerifyCase(unittest.TestCase):
    """process_verify wiring: proof runs for real links, never for '_g' links."""

    def setUp(self):
        self._client = mongomock.MongoClient()
        db._db = self._client["test_pv"]

    def tearDown(self):
        self._client.close()

    def _patches(self, proof_verdict="deny"):
        calls = {"proof": 0, "delivered": 0, "verified": 0, "burned": 0}

        async def proof(bot, chat_id, user_id, doc):
            calls["proof"] += 1
            return proof_verdict

        async def deliver(bot, chat_id, user_id, item):
            calls["delivered"] += 1

        async def verified(*a, **kw):
            calls["verified"] += 1

        async def burned(token):
            calls["burned"] += 1

        async def no_missing(bot, uid, category=None):
            return []

        ps = [
            patch.object(bot1, "_return_proof", new=proof),
            patch.object(bot1, "deliver_now", new=deliver),
            patch.object(bot1, "missing_force_sub", new=no_missing),
            patch.object(db, "mark_verified", new=verified),
            patch.object(db, "mark_token_used", new=burned),
            patch.object(db, "reset_strikes", new=lambda u: _aret(None)),
            patch.object(db, "get_item_by_file_id",
                         new=lambda fid: _aret({"file_id": fid, "category": "jav"})),
            patch.object(db, "get_settings",
                         new=lambda: _aret({"token_ttl_minutes": 10, "verify_hours": 6})),
            patch.object(bot1, "bypass_min_seconds", new=lambda: _aret(0)),
        ]
        for p in ps:
            p.start()
        return calls, ps

    def _token(self, **extra):
        doc = {"token": "tk", "user_id": 777, "file_id": "jav_f1", "kind": "verify",
               "used": False, "expires_at": db.now() + 600,
               "created_at": db.now() - 200, "shortener_state": "active"}
        doc.update(extra)
        return doc

    def test_real_link_denied_by_proof_delivers_nothing(self):
        calls, ps = self._patches("deny")
        doc = self._token(grant_slug="gg")
        try:
            with patch.object(db, "get_token", new=lambda t: _aret(doc)):
                asyncio.run(bot1.process_verify(FakeBot(), 555, 777, "jav_f1", "tk", "u"))
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(calls["proof"], 1)
        self.assertEqual(calls["delivered"], 0, "no file without Worker confirmation")
        self.assertEqual(calls["verified"], 0, "no verification logged")
        self.assertEqual(calls["burned"], 0, "the deny path owns its own burn")

    def test_gate_link_skips_proof(self):
        calls, ps = self._patches("deny")
        doc = self._token(grant_slug="gg")
        try:
            with patch.object(db, "get_token", new=lambda t: _aret(doc)):
                asyncio.run(bot1.process_verify(FakeBot(), 555, 777, "jav_f1", "tk",
                                                "u", via_gate=True))
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(calls["proof"], 0, "'_g' links already passed Gate 2")
        self.assertEqual(calls["delivered"], 1)
        self.assertEqual(calls["burned"], 0, "gate resume must not burn the token")

    def test_retry_delivers_nothing(self):
        calls, ps = self._patches("retry")
        doc = self._token(grant_slug="gg")
        try:
            with patch.object(db, "get_token", new=lambda t: _aret(doc)):
                asyncio.run(bot1.process_verify(FakeBot(), 555, 777, "jav_f1", "tk", "u"))
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(calls["delivered"], 0)
        self.assertEqual(calls["verified"], 0)


class TokenGrantStorage(unittest.TestCase):
    def setUp(self):
        self._client = AsyncMongoMockClient()
        db._db = self._client["test_token_grant"]

    def tearDown(self):
        self._client.close()

    def test_set_token_grant_persists(self):
        token = asyncio.run(db.create_token(5, "jav_f1", 10, kind="verify"))
        asyncio.run(db.set_token_grant(token, "gg9"))
        doc = asyncio.run(db.get_token(token))
        self.assertEqual(doc["grant_slug"], "gg9")

    def test_set_token_grant_ignores_empty(self):
        token = asyncio.run(db.create_token(5, "jav_f1", 10, kind="verify"))
        asyncio.run(db.set_token_grant(token, ""))
        doc = asyncio.run(db.get_token(token))
        self.assertNotIn("grant_slug", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
