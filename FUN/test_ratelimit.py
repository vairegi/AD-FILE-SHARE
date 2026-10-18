"""v1.4.1: rate-limit handling, pacing, token sanitization, token reset."""
import asyncio, io, time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import mongomock_motor, pytest, pytest_asyncio
from PIL import Image
from bson import Binary
import config, db, covers, cover_jobs, handlers, runtime
from test_repairs import update, context


def jpeg():
    out = io.BytesIO(); Image.new("RGB",(8,8),(1,2,3)).save(out,"JPEG"); return out.getvalue()

@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(), "rl_tests")
    monkeypatch.setattr(config, "OWNER_ID", 7); monkeypatch.setattr(config, "ADMIN_IDS", [7])
    covers._last_upload_at = 0.0
    runtime.userbot = None; cover_jobs._task = None
    yield
    await runtime.stop_jobs(); cover_jobs._task = None

class RateLimitHTTP:
    def __init__(self): self.posts = 0
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, *a, **k): raise RuntimeError("no token api")
    async def post(self, url, **kw):
        self.posts += 1
        return NS(status_code=200, json=lambda: {"error": "Try again later"})

async def test_try_again_later_detected_as_rate_limited_and_stops():
    http = RateLimitHTTP()
    r = await covers.host_cover(http, jpeg(), token="secret_tok")
    assert r["status"] == "rate_limited"
    # v1.7 pruned set: catbox + telegraph + graph + litterbox each tried once
    # (bunny.net skipped — no creds), then stop.
    assert http.posts == 4
    assert "secret_tok" not in r["error"] and "access_token=[hidden]" not in r["error"]

async def test_http_429_detected():
    class H:
        async def post(self, url, **kw): return NS(status_code=429, json=lambda: {})
    r = await covers.host_cover(H(), jpeg())
    assert r["status"] == "rate_limited"

async def test_errors_sanitize_tokens():
    class H:
        async def post(self, url, **kw):
            return NS(status_code=500, json=lambda: {"error": "bad access_token=abc123XYZ_-token"})
    r = await covers.host_cover(H(), jpeg())
    assert r["status"] == "failed" and "abc123XYZ" not in r["error"]

async def test_pacing_spaces_uploads(monkeypatch):
    monkeypatch.setattr(covers, "MIN_UPLOAD_INTERVAL", 2.0)
    covers._last_upload_at = 0.0
    # Single-host order so the count asserts pacing between UPLOADS, not
    # between adapters of the multi-host chain.
    import cover_hosts
    monkeypatch.setattr(cover_hosts, "DEFAULT_ORDER", ("telegra.ph",))
    slept = []
    real_sleep = asyncio.sleep
    async def spy(t): slept.append(t); await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", spy)
    class H:
        def __init__(self): self.n = 0
        async def post(self, url, **kw):
            self.n += 1
            return NS(status_code=200, text="", json=lambda: [{"src": "/file/x.jpg"}])
    h = H()
    await covers.host_cover(h, jpeg()); await covers.host_cover(h, jpeg())
    assert h.n == 2 and len(slept) == 1 and 0 < slept[0] <= covers.MIN_UPLOAD_INTERVAL

async def test_rate_limited_cover_stays_pending_for_later_retry(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: RateLimitHTTP())
    key = covers.asset_key(-100, 10)
    await db.col("cover_assets").insert_one({"_id": key, "data": Binary(jpeg())})
    coll = {"collection_id": "c-100_10", "channel_id": -100,
            "cover": {"message_id": 10, "asset_keys": [key], "url_status": "failed"}}
    await db.upsert_collection(coll)
    async with RateLimitHTTP() as http:
        result = await covers.prepare_collection(None, coll, http, force=True)
    assert result["rate_limited"] is True
    doc = await db.get_collection("c-100_10")
    assert doc["cover"]["url_status"] == "pending"  # retryable, not marked failed

async def test_job_pauses_cleanly_on_persistent_rate_limit(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    for i in range(12):
        await db.upsert_collection({"collection_id": f"c{i}", "channel_id": -100,
            "cover": {"message_id": i, "url_status": "pending"}})
    async def prepare(u, d, h, force=False): return {"hosted": False, "ready": True, "rate_limited": True}
    monkeypatch.setattr(covers, "prepare_collection", prepare)
    runtime.userbot = NS(disconnect=AsyncMock())
    u = update(); await handlers.cmd_retry_covers(u, context())
    await cover_jobs._task
    state = await db.col("cover_jobs").find_one({"_id": "current"})
    assert state["state"] == "rate_limited_paused"
    assert state["processed"] == 0 and state["hosted"] == 0  # same cover was not consumed

async def test_telegraph_token_reset_removes_leaked_token():
    await db.update_settings({"telegraph_token": "leaked_tok"})
    u = update()
    await handlers.cmd_telegraph_token(u, context(args=["reset"]))
    assert "removed" in u.message.replies[-1][0]
    assert not (await db.get_settings()).get("telegraph_token")
