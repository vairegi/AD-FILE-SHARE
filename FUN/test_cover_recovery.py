"""Regression: the 162-pending-cover incident, retried with the token-aware
Telegraph pipeline, plus real PTB wire checks for in-place feed edits.
Offline: in-memory MongoDB, fake HTTP/userbot, fake PTB transport."""
import io, json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest, pytest_asyncio, mongomock_motor
from PIL import Image
from bson import Binary
from telegram import Bot as TelegramBot, Update
from telegram.request import BaseRequest
import config, db, covers, cover_jobs, handlers, runtime
from test_repairs import update, context, collection

TG = "https://telegra.ph/file/tok.jpg"


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(), "recovery_v14")
    monkeypatch.setattr(config, "OWNER_ID", 7)
    monkeypatch.setattr(config, "ADMIN_IDS", [7])
    runtime.userbot = None
    cover_jobs._task = None
    yield
    await runtime.stop_jobs()
    cover_jobs._task = None


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (10, 10), (130, 150, 170)).save(out, "JPEG")
    return out.getvalue()


async def test_exact_162_pending_rows_backfill_in_background(monkeypatch):
    for i in range(162):
        await db.upsert_collection({"collection_id": f"c{i}", "channel_id": -100,
            "cover": {"message_id": i + 1, "url_status": "pending", "url": None},
            "status": "active"})
    await db.create_user(7)
    await db.add_tokens(7, 11, "test")
    await db.insert_unlock(7, "c0")

    async def prepare(userbot, doc, http, force=False):
        assert force
        await db.col("collections").update_one(
            {"collection_id": doc["collection_id"]},
            {"$set": {"cover.url_status": "ok", "cover.url": TG}})
        return {"hosted": True, "ready": True}
    monkeypatch.setattr(covers, "prepare_collection", prepare)
    runtime.userbot = NS(disconnect=AsyncMock())

    u = update()
    await handlers.cmd_retry_covers(u, context())
    assert "162 covers" in u.message.replies[0][0] and "background" in u.message.replies[0][0]
    assert cover_jobs.busy()
    await cover_jobs._task
    assert (await db.col("cover_jobs").find_one({"_id": "current"}))["processed"] == 162
    assert await db.col("collections").count_documents({"cover.url_status": "ok"}) == 162
    assert (await db.get_user(7))["tokens"] == 11
    assert await db.is_unlocked(7, "c0")


async def test_retry_selects_pending_missing_failed_and_bad_ok_but_skips_cached():
    docs = [("pending", {"url_status": "pending"}), ("missing", {}),
            ("failed", {"url_status": "failed"}),
            ("bad_ok", {"url_status": "ok", "url": None}),
            ("good", {"url_status": "ok", "url": TG})]
    for name, cover in docs:
        await db.upsert_collection({"collection_id": name, "channel_id": -100, "cover": cover})
    seen = []

    async def prepare(userbot, doc, http, force=False):
        seen.append(doc["collection_id"])
        return {"hosted": False, "ready": False}
    runtime.userbot = NS(disconnect=AsyncMock())
    import covers as cov
    orig = cov.prepare_collection
    cov.prepare_collection = prepare
    try:
        await handlers.cmd_retry_covers(update(), context())
        await cover_jobs._task
    finally:
        cov.prepare_collection = orig
    assert set(seen) == {"pending", "missing", "failed", "bad_ok"}


class TokenHTTP:
    """createAccount returns a token; upload requires it in form data."""
    def __init__(self):
        self.posts = []
    async def get(self, url, params=None, timeout=None):
        return NS(status_code=200, json=lambda: {"ok": True, "result": {"access_token": "tok123"}})
    async def post(self, url, files=None, timeout=None, data=None, **kw):
        self.posts.append((url, data))
        if not (data or {}).get("access_token") == "tok123":
            return NS(status_code=403, json=lambda: {"error": "ACCESS_TOKEN_INVALID"})
        return NS(status_code=200, json=lambda: [{"src": "/file/ok.jpg"}])


async def test_telegraph_token_created_cached_and_used_for_upload():
    http = TokenHTTP()
    assert await covers.ensure_token(http) == "tok123"
    assert (await db.get_settings())["telegraph_token"] == "tok123"
    assert await covers.ensure_token(http) == "tok123"  # cached, no second createAccount
    r = await covers.host_cover(http, jpeg(), token="tok123")
    assert r["status"] == "ok" and r["url"] == "https://telegra.ph/file/ok.jpg"
    assert any((d or {}).get("access_token") == "tok123" for _, d in http.posts)


async def test_anonymous_403_falls_back_to_token_then_mirror(monkeypatch):
    """Anonymous upload rejected -> same image retried with token on same host."""
    class AnonBlocked:
        def __init__(self): self.posts = []
        async def post(self, url, files=None, timeout=None, data=None, **kw):
            self.posts.append((url, data))
            if "graph.org" in url:
                return NS(status_code=200, json=lambda: [{"src": "/file/mirror.jpg"}])
            if not (data or {}).get("access_token"):
                return NS(status_code=403, json=lambda: {"error": "FORBIDDEN"})
            return NS(status_code=200, json=lambda: [{"src": "/file/tok.jpg"}])
    http = AnonBlocked()
    r = await covers.host_cover(http, jpeg(), token=None)
    # telegra.ph: token absent -> 403; then anonymous attempt also 403; graph.org succeeds
    assert r["status"] == "ok" and r["host"] == "graph.org"
    assert r["url"] == "https://graph.org/file/mirror.jpg"


async def test_all_hosts_down_records_failed_with_diagnostics():
    class Down:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            raise RuntimeError("connection refused")
    async with Down() as http:
        r = await covers.host_cover(http, jpeg(), token=None)
    assert r["status"] == "failed" and "telegra.ph" in r["error"]


async def test_prepare_persists_failed_status_instead_of_silent_pending(monkeypatch):
    import httpx
    class Down:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise RuntimeError("no token api")
        async def post(self, *a, **k): raise RuntimeError("down")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: Down())
    key = covers.asset_key(-100, 10)
    await db.col("cover_assets").insert_one({"_id": key, "data": Binary(jpeg())})
    coll = {"collection_id": "c-100_10", "channel_id": -100,
            "cover": {"message_id": 10, "asset_keys": [key], "url_status": "pending"}}
    await db.upsert_collection(coll)
    async with Down() as http:
        result = await covers.prepare_collection(None, coll, http, force=True)
    assert result["hosted"] is False and result["ready"] is True  # cached JPEG still usable
    doc = await db.get_collection("c-100_10")
    assert doc["cover"]["url_status"] == "failed"
    assert doc["cover"]["last_error"]


async def test_cover_status_reports_telegraph_links_and_pending():
    await db.upsert_collection({"collection_id": "a", "channel_id": -100,
        "cover": {"url_status": "ok", "url": TG}})
    await db.upsert_collection({"collection_id": "b", "channel_id": -100,
        "cover": {"url_status": "pending"}})
    u = update()
    await handlers.cmd_cover_status(u, context())
    text = u.message.replies[-1][0]
    assert "Telegraph links ready: 1" in text and "Pending: 1" in text
    assert "in-place edit" in text


async def test_telegraph_token_admin_command():
    u = update()
    await handlers.cmd_telegraph_token(u, context())
    assert "createAccount" in u.message.replies[-1][0]
    await handlers.cmd_telegraph_token(u, context(args=["abc123def"]))
    assert "saved" in u.message.replies[-1][0]
    assert (await db.get_settings())["telegraph_token"] == "abc123def"
    other = update(uid=9)
    await handlers.cmd_telegraph_token(other, context())
    assert not other.message.replies


class Recording(BaseRequest):
    def __init__(self):
        self.calls = []
    @property
    def read_timeout(self):
        return 5
    async def initialize(self):
        pass
    async def shutdown(self):
        pass
    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        files = request_data.multipart_data if request_data else None
        self.calls.append((endpoint, params, files))
        if endpoint == "getMe":
            result = {"id": 123456, "is_bot": True, "first_name": "T", "username": "link_test_bot"}
        else:
            result = {"message_id": 42, "date": 0, "chat": {"id": 7, "type": "private"}, "text": "x"}
        return 200, json.dumps({"ok": True, "result": result}).encode()


async def test_real_ptb_feed_tap_serializes_editmessagetext_with_link_preview():
    await db.upsert_collection({**collection(), "cover": {"url": TG, "url_status": "ok"}})
    request = Recording()
    async with TelegramBot("123456:FAKE_TEST_TOKEN_NOT_REAL", request=request) as bot:
        incoming = Update.de_json({"update_id": 1, "callback_query": {
            "id": "q1", "from": {"id": 7, "is_bot": False, "first_name": "A"},
            "chat_instance": "ci", "data": "feed:all:0",
            "message": {"message_id": 40, "date": 0,
                        "chat": {"id": 7, "type": "private"}, "text": "menu"}}}, bot)
        await handlers.on_button(incoming, NS(bot=bot))
    endpoints = [e for e, _, _ in request.calls]
    assert "editMessageText" in endpoints
    assert "sendMessage" not in endpoints and "sendPhoto" not in endpoints
    assert "deleteMessage" not in endpoints
    params = next(pr for e, pr, _ in request.calls if e == "editMessageText")
    assert params["message_id"] == 40 and params["parse_mode"] == "HTML"
    opts = params["link_preview_options"]
    if isinstance(opts, str):
        opts = json.loads(opts)
    assert opts["url"] == TG and opts["show_above_text"] and opts["prefer_large_media"]
    assert "⁣" in params["text"]
