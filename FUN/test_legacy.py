"""Bot 3 sandbox tests — every feature's success + failure + idempotency.

Runs against mongomock (no network). Bot/Telegram calls are faked.
"""
import os, sys, asyncio, time, re, html
from telegram.error import BadRequest

def plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mongomock_motor
import config, db, classifier, persona, handlers, providers, covers

RESULTS = []
def check(name, ok):
    RESULTS.append((name, bool(ok)))
    print(("PASS" if ok else "FAIL"), "-", name)


# ── fakes ────────────────────────────────────────────────────────
class FakeMessage:
    def __init__(self, text="/start"):
        self.text = text
        self.replies = []
        self.edits = []
    async def reply_text(self, text, **kw):
        self.replies.append((plain(text), kw)); return self
    async def edit_message_text(self, text, **kw):
        self.edits.append((plain(text), kw)); return self

class FakeUser:
    def __init__(self, uid, first_name="Senpai", username=None):
        self.id = uid; self.first_name = first_name; self.username = username

class FakeCallbackMessage(FakeMessage):
    pass  # reply_text inherited so _edit() fallback works

class FakeCallback:
    def __init__(self, uid, data):
        self.from_user = FakeUser(uid); self.data = data
        self.message = FakeCallbackMessage(); self.answers = []
    async def edit_message_text(self, text, **kw):
        return await self.message.edit_message_text(text, **kw)
    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append(text)

class FakeUpdate:
    def __init__(self, uid=1, text="/start", args=None):
        self.effective_user = FakeUser(uid)
        self.message = FakeMessage(text)
        self.callback_query = None
        self._args = args or []

class FakeCtx:
    def __init__(self, bot=None, args=None):
        self.bot = bot or FakeBot(); self.args = args or []

class FakeMember:
    status = "member"

class FakeBot:
    def __init__(self, fail_delivery=False, member_status="member"):
        self.sent = []; self.copied = []; self.deleted = []
        self.fail_delivery = fail_delivery
        self.member_status = member_status
    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text)); return FakeMessage()
    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        if self.fail_delivery: raise BadRequest("message to copy not found")
        self.copied.append(message_id)
        return type("M", (), {"message_id": 9000 + message_id})()
    async def copy_messages(self, chat_id, from_chat_id, message_ids, **kw):
        if self.fail_delivery: raise BadRequest("message to copy not found")
        self.copied.extend(message_ids)
        return [type("M", (), {"message_id": 9000 + m})() for m in message_ids]
    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)
    async def get_chat_member(self, cid, uid):
        m = FakeMember(); m.status = self.member_status; return m


async def reset_db():
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(), "t3")


def seed_collection(cid="c-100_10", files=(11, 12, 13), price=None, title="Hot drop"):
    return {"collection_id": cid, "channel_id": -100, "category": "jav",
            "title": title,
            "cover": {"media_type": "photo", "message_id": 10,
                      "caption": "line1\nline2\nline3", "album_message_ids": [10],
                      "duration": None, "url": None, "url_host": None,
                      "url_status": "pending"},
            "files": [{"message_id": m, "media_type": "video", "caption": None} for m in files],
            "file_count": len(files), "posted_at": 1, "price": price,
            "status": "active", "views": 0, "unlocks": 0}


# ── classifier tests ─────────────────────────────────────────────
def test_classifier():
    P = lambda mid, mt, cap, gid=None, dur=None: {
        "message_id": mid, "media_type": mt, "caption": cap,
        "grouped_id": gid, "duration": dur, "date_ts": mid}
    posts = [
        P(10, "photo", "Semxy gf met after long...\n➪ Quality - 720p\n➪ Audio : Hindi\nMust watch"),
        P(11, "video", None, dur=540), P(12, "video", None, dur=9),
        P(13, "photo", "sams happy"),   # short caption -> delivery
        P(20, "video", "Cover two line1\nline2\nline3", dur=20),  # 20s cover OK
        P(21, "video", None, dur=9),
        P(30, "photo", "Album cover a\nb\nc", gid=99),
        P(31, "photo", None, gid=99),   # same album -> part of cover, not delivery
        P(32, "video", None, dur=60),
    ]
    # orphan delivery files before any cover
    posts.insert(0, P(5, "video", None, dur=30))
    r = classifier.classify(posts, -100, "jav")
    check("classifier: 3 collections", len(r["collections"]) == 3)
    c1, c2, c3 = r["collections"]
    check("classifier: cover1 title is first caption line",
          "Semxy gf" in c1["title"])
    check("classifier: cover1 has 3 delivery files (short-caption photo is delivery)",
          c1["file_count"] == 3)
    check("classifier: cover1 excludes cover msg 10",
          all(f["message_id"] != 10 for f in c1["files"]))
    check("classifier: 20s video with long caption IS a cover",
          c2["cover"]["media_type"] == "video" and c2["cover"]["duration"] == 20)
    check("classifier: captionless 9s video is NOT a cover",
          c2["file_count"] == 1 and c2["files"][0]["message_id"] == 21)
    check("classifier: album grouped as one cover unit",
          c3["cover"]["album_message_ids"] == [30, 31] and c3["file_count"] == 1)
    check("classifier: orphan files held as pending",
          len(r["pending"]) == 1 and r["pending"][0]["message_id"] == 5)
    check("classifier: captionless long video NOT cover (duration never decides)",
          classifier.is_cover("video", None) is False)
    check("classifier: 2-line caption NOT cover", classifier.is_cover("photo", "a\nb") is False)
    check("classifier: 3-line caption IS cover", classifier.is_cover("photo", "a\nb\nc") is True)


# ── economy tests ────────────────────────────────────────────────
async def test_start_welcome():
    await reset_db()
    upd = FakeUpdate(uid=1001)
    await handlers.cmd_start(upd, FakeCtx())
    doc = await db.get_user(1001)
    check("start: welcome tokens = 5", doc["tokens"] == 5)
    check("start: cute welcome text", "Uwahh~!" in upd.message.replies[0][0])
    check("start: post-credit balance shown in card", "Token Balance: 5 Tokens" in upd.message.replies[0][0])
    await handlers.cmd_start(upd, FakeCtx())  # second /start
    doc = await db.get_user(1001)
    check("start: no double welcome on repeat /start", doc["tokens"] == 5)


async def test_start_referral():
    await reset_db()
    await db.create_user(2000)  # inviter
    upd = FakeUpdate(uid=2001, args=["ref_2000"])
    await db.update_settings({"required_channel_ids": [-100999]})
    ctx = FakeCtx(bot=FakeBot(member_status="left"), args=["ref_2000"])
    await handlers.cmd_start(upd, ctx)
    doc = await db.get_user(2001)
    check("referral: friend gets 8 (5+3)", doc["tokens"] == 8)
    ref = await db.get_referral(2001)
    check("referral: pending record created", ref and ref["status"] == "pending")
    # inviter NOT paid yet (needs channel-join qualification)
    inv = await db.get_user(2000)
    check("referral: inviter not paid before qualification", inv["tokens"] == 0)
    # qualify -> exactly once + notification
    bot = FakeBot()
    await handlers.maybe_qualify_referral(bot, 2001, "Senpai")
    inv = await db.get_user(2000)
    check("referral: inviter +5 after qualification", inv["tokens"] == 5)
    check("referral: inviter notified", any("Yatta" in t for _, t in bot.sent))
    await handlers.maybe_qualify_referral(bot, 2001, "Senpai")  # again
    inv = await db.get_user(2000)
    check("referral: never double-credits", inv["tokens"] == 5)
    # self-referral blocked
    upd2 = FakeUpdate(uid=2000, args=["ref_2000"])
    await handlers.cmd_start(upd2, FakeCtx(args=["ref_2000"]))
    check("referral: self-referral ignored", (await db.get_referral(2000)) is None)


async def test_checkin():
    await reset_db()
    upd = FakeUpdate(uid=3001)
    await handlers.cmd_checkin(upd, FakeCtx())
    txt = upd.message.replies[0][0]
    check("checkin: complete text", "Daily Check-In Complete" in txt)
    check("checkin: base +3", "Base Reward: +3 tokens" in txt)
    check("checkin: balance 3", "Balance: 3 tokens" in txt)
    check("checkin: streak 1 + next bonus in 4", "Streak: 1 days" in txt and "in 4 day(s)" in txt)
    upd2 = FakeUpdate(uid=3001)
    await handlers.cmd_checkin(upd2, FakeCtx())  # same IST day
    txt2 = upd2.message.replies[0][0]
    check("checkin: same-day repeat shows cooldown", "Already Checked In" in txt2 and "Come back in" in txt2)
    check("checkin: no double credit", (await db.get_user(3001))["tokens"] == 3)


async def test_unlock_flow():
    await reset_db()
    await db.upsert_collection(seed_collection())
    await db.create_user(4001)
    await db.add_tokens(4001, 5, "welcome")
    bot = FakeBot()
    # not a member -> blocked
    bot_blocked = FakeBot(member_status="left")
    await db.update_settings({"required_channel_ids": [-100999]})
    upd = FakeUpdate(uid=4001); q = FakeCallback(4001, "unlock:c-100_10")
    await handlers.on_button(type("U", (), {"callback_query": q,
        "effective_user": q.from_user})(), FakeCtx(bot=bot_blocked))
    check("unlock: non-member blocked with join card",
          any("Join the required channel" in t for t, _ in q.message.edits))
    check("unlock: blocked user not charged", (await db.get_user(4001))["tokens"] == 5)
    # member -> pays 2, gets 3 files, completion card
    upd2 = FakeUpdate(uid=4001); q2 = FakeCallback(4001, "unlock:c-100_10")
    await handlers.on_button(type("U", (), {"callback_query": q2,
        "effective_user": q2.from_user})(), FakeCtx(bot=bot))
    check("unlock: charged exactly 2", (await db.get_user(4001))["tokens"] == 3)
    check("unlock: 3 files copied server-side", sorted(bot.copied) == [11, 12, 13])
    check("unlock: completion card sent",
          any("Stream Complete" in t for t, _ in q2.message.edits))
    check("unlock: delivered count is actual (3)",
          any("Delivered 3 items" in t for t, _ in q2.message.edits))
    check("unlock: vaulted", await db.is_unlocked(4001, "c-100_10"))
    # double tap -> still unlocked, no double charge
    q3 = FakeCallback(4001, "unlock:c-100_10")
    await handlers.on_button(type("U", (), {"callback_query": q3,
        "effective_user": q3.from_user})(), FakeCtx(bot=bot))
    check("unlock: double-tap no double charge", (await db.get_user(4001))["tokens"] == 3)
    # re-stream free
    q4 = FakeCallback(4001, "restream:c-100_10")
    await handlers.on_button(type("U", (), {"callback_query": q4,
        "effective_user": q4.from_user})(), FakeCtx(bot=bot))
    check("restream: free (balance unchanged)", (await db.get_user(4001))["tokens"] == 3)


async def test_unlock_insufficient_and_refund():
    await reset_db()
    await db.upsert_collection(seed_collection(cid="c-100_50"))
    await db.create_user(5001)  # 0 tokens
    q = FakeCallback(5001, "unlock:c-100_50")
    await handlers.on_button(type("U", (), {"callback_query": q,
        "effective_user": q.from_user})(), FakeCtx(bot=FakeBot()))
    check("unlock: insufficient balance card",
          any("not enough tokens" in t for t, _ in q.message.edits))
    check("unlock: no vault entry when unpaid", not await db.is_unlocked(5001, "c-100_50"))
    # zero-delivery -> full refund
    await db.add_tokens(5001, 2, "welcome")
    q2 = FakeCallback(5001, "unlock:c-100_50")
    await handlers.on_button(type("U", (), {"callback_query": q2,
        "effective_user": q2.from_user})(),
        FakeCtx(bot=FakeBot(fail_delivery=True)))
    check("unlock: zero-delivery full refund", (await db.get_user(5001))["tokens"] == 2)
    check("unlock: refund card shown",
          any("refunded" in t.lower() for t, _ in q2.message.edits))


async def test_deletion_queue():
    await reset_db()
    import services
    await db.create_delivery({"delivery_id": "d1", "user_id": 6001,
        "collection_id": "x", "message_ids": [9101, 9102],
        "auto_delete_at": db.now_ts() - 1, "delete_status": "pending",
        "failed_count": 0, "at": db.now_ts()})
    bot = FakeBot()
    await services.run_deletion_sweeper(bot)
    check("deletion: expired files deleted", sorted(bot.deleted) == [9101, 9102])
    d = await db.get_delivery("d1")
    check("deletion: status marked done", d["delete_status"] == "done")


async def test_tasks_and_provider():
    await reset_db()
    await db.create_user(7001)
    await db.update_settings({"tasks": [
        {"task_id": "t1", "label": "Ads Link 1", "url": "https://x",
         "reward": 5, "enabled": True, "secret": "s3cr3t"}]})
    # button shows cooldown-free open; credit only via signed callback
    ok = await handlers.credit_task(7001, "t1")
    check("task: first credit ok", ok == "ok" and (await db.get_user(7001))["tokens"] == 5)
    again = await handlers.credit_task(7001, "t1")
    check("task: 24h cooldown blocks repeat", again == "cooldown")
    check("task: cooldown no double credit", (await db.get_user(7001))["tokens"] == 5)
    ts = int(time.time())
    sig = providers.make_signature("s3cr3t", 7001, "t1", ts)
    check("task: valid signature -> cooldown (already ran)",
          await providers.verify_callback(7001, "t1", ts, sig) == "cooldown")
    check("task: bad signature rejected",
          await providers.verify_callback(7001, "t1", ts, "deadbeef") == "bad_signature")
    check("task: expired signature rejected",
          await providers.verify_callback(7001, "t1", ts - 3600, sig) == "expired")
    check("task: unknown task rejected",
          await providers.verify_callback(7001, "nope", ts, sig) == "unknown_task")


async def test_covers_fallback():
    await reset_db()

    class Resp:
        def __init__(self, code, data=None, ctype="image/jpeg"):
            self.status_code = code; self._d = data; self.headers = {"content-type": ctype}
        def json(self): return self._d

    class FailPrimaryClient:
        def __init__(self): self.calls = []
        async def post(self, url, files=None, timeout=None):
            self.calls.append(url)
            if "telegra.ph" in url: raise RuntimeError("down")
            return Resp(200, [{"src": "/file/abc.jpg"}])
        async def head(self, url, timeout=None, follow_redirects=None):
            return Resp(200)

    r = await covers.host_cover(FailPrimaryClient(), b"jpeg-bytes")
    check("covers: telegraph failure -> graph.org fallback ok",
          r["status"] == "ok" and r["host"] == "graph.org"
          and r["url"] == "https://graph.org/file/abc.jpg")

    class AllFailClient:
        async def post(self, url, files=None, timeout=None): raise RuntimeError("down")
        async def head(self, url, timeout=None, follow_redirects=None): raise RuntimeError("down")

    r2 = await covers.host_cover(AllFailClient(), b"jpeg-bytes")
    check("covers: both mirror hosts down -> external hosting failed (cached Render previews handled separately)",
          r2["status"] == "failed" and r2["url"] is None)


async def test_bookmark_toggle():
    await reset_db()
    await db.upsert_collection(seed_collection(cid="c-100_77"))
    q = FakeCallback(8001, "save:c-100_77")
    await handlers.on_button(type("U", (), {"callback_query": q,
        "effective_user": q.from_user})(), FakeCtx())
    check("save: bookmark added", await db.count_bookmarks(8001) == 1)
    check("save: cute confirm", any("Saved" in (a or "") for a in q.answers))
    q2 = FakeCallback(8001, "save:c-100_77")
    await handlers.on_button(type("U", (), {"callback_query": q2,
        "effective_user": q2.from_user})(), FakeCtx())
    check("save: second tap removes bookmark", await db.count_bookmarks(8001) == 0)


async def main():
    test_classifier()
    for t in (test_start_welcome, test_start_referral, test_checkin,
              test_unlock_flow, test_unlock_insufficient_and_refund,
              test_deletion_queue, test_tasks_and_provider,
              test_covers_fallback, test_bookmark_toggle):
        await t()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:", failed); sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
