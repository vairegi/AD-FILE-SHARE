"""Cover pipeline tests: scan-time download+host+cache, feed cover rendering,
mode toggle, /help. Offline — fake userbot/http/bot only."""
import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
import mongomock_motor
import config, db, handlers, scanner, covers
from test_repairs import update, context, Q, Bot, collection, source_message, Source


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(), "covers_feed")
    for idx in (("user_id",),):
        await db.col("users").create_index("user_id", unique=True)
    await db.col("collections").create_index("collection_id", unique=True)
    await db.col("unlocks").create_index([("user_id",1),("collection_id",1)], unique=True)
    await db.col("bookmarks").create_index([("user_id",1),("collection_id",1)], unique=True)
    monkeypatch.setattr(config, "OWNER_ID", 7)
    monkeypatch.setattr(config, "ADMIN_IDS", [7])
    yield


class FakeHTTP:
    """telegra.ph works, returns hosted image."""
    def __init__(self, primary_fails=False):
        self.primary_fails = primary_fails
        self.posted = []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, files=None, timeout=None, **kw):
        self.posted.append(url)
        if self.primary_fails and "telegra.ph" in url:
            raise RuntimeError("down")
        host = "graph.org" if "graph.org" in url else "telegra.ph"
        return NS(status_code=200, json=lambda: [{"src": "/file/cover.jpg"}])
    async def head(self, url, timeout=None, follow_redirects=None):
        return NS(status_code=200, headers={"content-type": "image/jpeg"})


class ScanClient(Source):
    def __init__(self, posts):
        super().__init__(posts)
        self.downloads = []
    async def get_messages(self, cid, ids=None, limit=None):
        if ids is not None:
            return next(p for p in self.posts if p.id == ids)
        return self.posts[-1:] if limit == 1 else self.posts
    async def download_media(self, media, file=None, thumb=None):
        self.downloads.append(media)
        import io
        from PIL import Image
        image=io.BytesIO(); Image.new("RGB",(8,8),(120,140,160)).save(image,"JPEG")
        file.write(image.getvalue())
        return file


class RichBot(Bot):
    def __init__(self):
        super().__init__()
        self.posts = []
    async def _post(self, endpoint, data=None, **kw):
        self.posts.append((endpoint, data))
        return {}
    async def send_photo(self, chat_id, photo, **kw):
        self.calls.append(("url_photo", photo)); return NS(message_id=43, photo=[])
    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.posts.append(("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                               "text": text, **kw}))
        return NS(message_id=message_id)
    async def edit_message_media(self, **kw):
        self.calls.append(("edit_url_photo", kw)); return NS(message_id=42, photo=[])
    async def delete_message(self, **kw): pass


def _seed_cover_collection(url_status="pending", url=None):
    c = collection("c-100_10")
    c["cover"].update({"url_status": url_status, "url": url,
                       "url_host": "telegra.ph" if url else None})
    return c


@pytest.mark.asyncio
async def test_scan_hosts_and_caches_cover_url(monkeypatch):
    monkeypatch.setattr(scanner.httpx, "AsyncClient", FakeHTTP) if hasattr(scanner, "httpx") else None
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeHTTP)
    posts = [source_message(10, "Cover\nQuality\nAudio", kind="photo"),
             source_message(11), source_message(12)]
    client = ScanClient(posts)
    result = await scanner.scan_channel(client, -100, "movies")
    doc = await db.get_collection("c-100_10")
    assert doc["cover"]["url_status"] == "ok"
    assert doc["cover"]["url"].startswith("https://telegra.ph/file/")
    assert doc["cover"]["url_host"] == "telegra.ph"
    assert result["covers_ready"] == 1
    check_dl = len(client.downloads) >= 1
    assert check_dl


@pytest.mark.asyncio
async def test_scan_falls_back_to_graph_org(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: FakeHTTP(primary_fails=True))
    posts = [source_message(10, "Cover\nQ\nA", kind="photo"), source_message(11)]
    client = ScanClient(posts)
    await scanner.scan_channel(client, -100, "movies")
    doc = await db.get_collection("c-100_10")
    assert doc["cover"]["url_host"] == "graph.org"
    assert "graph.org" in doc["cover"]["url"]


@pytest.mark.asyncio
async def test_rescan_does_not_reupload_cached_cover(monkeypatch):
    import httpx
    fake = FakeHTTP()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: fake)
    posts = [source_message(10, "Cover\nQ\nA", kind="photo"), source_message(11)]
    client = ScanClient(posts)
    await scanner.scan_channel(client, -100, "movies")
    posts.append(source_message(12))
    before = len(fake.posted)
    downloads=len(client.downloads)
    assert before==1 and downloads==1
    await scanner.scan_channel(client, -100, "movies")
    assert len(fake.posted) == before  # cached — no re-upload for same cover
    assert len(client.downloads)==downloads


@pytest.mark.asyncio
async def test_failed_upload_keeps_pending_and_no_url(monkeypatch):
    import httpx
    class AllFail(FakeHTTP):
        async def post(self, url, files=None, timeout=None, **kw):
            raise RuntimeError("down")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: AllFail())
    posts = [source_message(10, "Cover\nQ\nA", kind="photo"), source_message(11)]
    await scanner.scan_channel(ScanClient(posts), -100, "movies")
    doc = await db.get_collection("c-100_10")
    assert doc["cover"]["url_status"] == "failed"
    assert doc["cover"]["url"] is None


@pytest.mark.asyncio
async def test_feed_edits_card_in_place_with_telegraph_link_preview():
    await db.upsert_collection(_seed_cover_collection("ok", "https://telegra.ph/file/x.jpg"))
    q = Q("feed:all:0", bot=RichBot()); q.message.message_id = 42
    await handlers.on_button(NS(callback_query=q), context(q.bot))
    assert not q.bot.sent and not q.bot.calls
    endpoint, kw = q.bot.posts[-1]
    assert endpoint == "editMessageText" and kw["message_id"] == 42
    assert '<a href="https://telegra.ph/file/x.jpg">\u2063</a>' in kw["text"]
    assert kw["link_preview_options"].to_dict() == {
        "url": "https://telegra.ph/file/x.jpg", "is_disabled": False,
        "prefer_large_media": True, "show_above_text": True}


@pytest.mark.asyncio
async def test_legacy_collage_setting_still_edits_in_place():
    await db.update_settings({"cover_mode": "collage"})
    await db.upsert_collection(_seed_cover_collection("ok", "https://graph.org/file/x.jpg"))
    q = Q("feed:all:0", bot=RichBot()); q.message.message_id = 42
    await handlers.on_button(NS(callback_query=q), context(q.bot))
    endpoint, kw = q.bot.posts[-1]
    assert endpoint == "editMessageText"
    assert kw["link_preview_options"].url == "https://graph.org/file/x.jpg"


@pytest.mark.asyncio
async def test_feed_text_fallback_when_no_cover():
    c = _seed_cover_collection("pending", None); c["cover"].pop("asset_keys", None)
    await db.upsert_collection(c)
    q = Q("feed:all:0", bot=RichBot()); q.message.message_id = 42
    await handlers.on_button(NS(callback_query=q), context(q.bot))
    endpoint, kw = q.bot.posts[-1]
    assert endpoint == "editMessageText"
    assert kw["link_preview_options"].is_disabled
    assert "Cover preview unavailable" in kw["text"]


@pytest.mark.asyncio
async def test_covermode_explains_inplace_telegraph_mode_and_checks_owner():
    u = update(); await handlers.cmd_covermode(u, context(args=["slideshow"]))
    assert "edited in place" in u.message.replies[-1][0]
    assert "disabled" in u.message.replies[-1][0]
    other = update(uid=9); await handlers.cmd_covermode(other, context())
    assert not other.message.replies


@pytest.mark.asyncio
async def test_help_command_registered_and_renders(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "123456:FAKE_TEST_TOKEN_NOT_REAL")
    import main
    app = main.build_bot()
    commands = set()
    for h in app.handlers[0]:
        if hasattr(h, "commands"):
            commands.update(h.commands)
    assert "help" in commands and "covermode" in commands
    u = update()
    await handlers.cmd_help(u, context())
    text, kw = u.message.replies[-1]
    assert "Cute Assistant" in text and kw["parse_mode"] == "HTML"
    kb = kw["reply_markup"].to_dict()["inline_keyboard"]
    assert kb[0][0]["style"] == "danger"  # Start Watching red
    assert kb[1][1]["style"] == "primary"  # Main Menu blue
