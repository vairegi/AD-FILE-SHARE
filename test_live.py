"""Route-level webhook test via httpx ASGITransport (no sockets needed).
Exercises /health, /bot1/webhook, /bot2/webhook with correct + wrong secrets —
the exact code path Telegram hits on Render.
"""
import asyncio
import os
import sys

os.environ.update({
    "BOT1_TOKEN": "111111111:TESTTOKENBOT1",
    "BOT2_TOKEN": "222222222:TESTTOKENBOT2",
    "BOT1_USERNAME": "gatebot_test",
    "BOT2_USERNAME": "deliverybot_test",
    "MONGO_DB_NAME": "video_bots_live_test",
    "ADMIN_IDS": "999",
    "BOT1_WEBHOOK_SECRET": "s1",
    "BOT2_WEBHOOK_SECRET": "s2",
    "RENDER_EXTERNAL_URL": "",
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402


async def run():
    import main as m
    import db

    class FakeTg:
        """Answers any bot.* call with a harmless no-op (offline test)."""
        defaults = type("D", (), {"tzinfo": None})()  # needed by Update.de_json

        def __getattr__(self, name):
            async def _any(*a, **k):
                if name == "send_message":
                    txt = a[1] if len(a) > 1 else k.get("text", "")
                    print(f"   [bot reply] {str(txt)[:60]!r}")
                    return type("M", (), {"message_id": 1})()
                return None
            return _any

    # swap in offline bots BEFORE initialize so no real Telegram call is made
    m.bot1.bot = FakeTg()
    m.bot2.bot = FakeTg()
    await db.connect()
    await m.bot1.initialize(); await m.bot1.start()
    await m.bot2.initialize(); await m.bot2.start()

    ok = True
    transport = httpx.ASGITransport(app=m.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/health")
        print("health:", r.status_code, r.json())
        ok &= r.status_code == 200 and r.json().get("database") is True

        payload = {"update_id": 1,
                   "message": {"message_id": 1,
                               "from": {"id": 999, "is_bot": False, "first_name": "T"},
                               "chat": {"id": 999, "type": "private"},
                               "date": 1, "text": "/start"}}

        r = await c.post("/bot1/webhook", json=payload,
                         headers={"X-Telegram-Bot-Api-Secret-Token": "s1"})
        print("bot1 webhook correct secret:", r.status_code)
        ok &= r.status_code == 200

        r = await c.post("/bot1/webhook", json=payload,
                         headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
        print("bot1 webhook wrong secret:", r.status_code)
        ok &= r.status_code == 403

        r = await c.post("/bot2/webhook", json=payload,
                         headers={"X-Telegram-Bot-Api-Secret-Token": "s2"})
        print("bot2 webhook correct secret:", r.status_code)
        ok &= r.status_code == 200

        r = await c.post("/bot2/webhook", json=payload)  # no header at all
        print("bot2 webhook missing secret:", r.status_code)
        ok &= r.status_code == 403

    await db._client.drop_database("video_bots_live_test")
    await db.close()
    print("ASGI ROUTE TEST:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(run())
