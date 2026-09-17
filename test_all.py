"""End-to-end functional test harness for the two-bot project.

Runs every handler/command path with a fake Telegram bot (no real API calls)
against a REAL MongoDB database. Exits non-zero if any check fails.
"""
import asyncio
import os
import sys
import types

# ── environment (must be set BEFORE importing config) ─────────
os.environ.update({
    "BOT1_TOKEN": "111111111:TESTTOKENBOT1",
    "BOT2_TOKEN": "222222222:TESTTOKENBOT2",
    "BOT1_USERNAME": "gatebot_test",
    "BOT2_USERNAME": "deliverybot_test",
    "MONGO_DB_NAME": "video_bots_dev_test",
    "ADMIN_IDS": "999",
    "SHORTENER_API_KEY": "test-api-key",
    "RENDER_EXTERNAL_URL": "",   # skip webhook registration in tests
})
# MONGO_URI comes from the real environment.

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import db      # noqa: E402
import utils   # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra if not cond else ''}")


# ── fakes ─────────────────────────────────────────────────────
class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeUpdate:
    def __init__(self, uid=999, text=""):
        self.message = FakeMessage(text)
        self.effective_message = self.message
        self.effective_user = FakeUser(uid)
        self.callback_query = None


class FakeBot:
    def __init__(self):
        self.sent = []          # (chat_id, text, kwargs)
        self.copied = []        # (chat_id, from_chat_id, message_id)
        self.copy_kwargs = []   # kwargs passed to copy_message
        self.forwarded = []     # (chat_id, from_chat_id, message_id)
        self.deleted = []
        self.membership = True  # get_chat_member result control

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return types.SimpleNamespace(message_id=9000 + len(self.sent))

    async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
        self.copied.append((chat_id, from_chat_id, message_id))
        self.copy_kwargs.append(kw)
        return types.SimpleNamespace(message_id=8000 + len(self.copied))

    async def forward_message(self, chat_id, from_chat_id, message_id, **kw):
        self.forwarded.append((chat_id, from_chat_id, message_id))
        return types.SimpleNamespace(message_id=7000 + len(self.forwarded))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def get_chat(self, channel_id):
        return types.SimpleNamespace(username="forcechannel")

    async def get_chat_member(self, chat_id, user_id):
        status = "member" if self.membership else "left"
        return types.SimpleNamespace(status=status)

    async def export_chat_invite_link(self, channel_id):
        return "https://t.me/+inviteXYZ"


class FakeContext:
    def __init__(self, bot=None, args=None, application=None):
        self.bot = bot or FakeBot()
        self.args = args or []
        self.application = application or types.SimpleNamespace(job_queue=None)


class FakeQuery:
    def __init__(self, uid, data):
        self.from_user = FakeUser(uid)
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)


async def main():
    # ── 1. utils ──────────────────────────────────────────────
    check("parse 5min", utils.parse_duration("5min") == 300)
    check("parse 2hour", utils.parse_duration("2hour") == 7200)
    check("parse 12hour", utils.parse_duration("12hour") == 43200)
    check("parse 7day", utils.parse_duration("7day") == 604800)
    check("parse bare 30 -> 30min", utils.parse_duration("30") == 1800)
    check("parse never -> 0", utils.parse_duration("never") == 0)
    check("parse junk -> None", utils.parse_duration("banana") is None)
    check("human 3700", "hour" in utils.human_duration(3700))

    # ── 2. grouping logic (pure) ──────────────────────────────
    raw = [
        {"message_id": 10, "kind": "cover", "caption": "Cover A"},
        {"message_id": 11, "kind": "video", "caption": "SD"},
        {"message_id": 12, "kind": "video", "caption": "HD"},
        {"message_id": 13, "kind": "srt", "caption": "subs"},
        {"message_id": 20, "kind": "cover", "caption": "Cover B"},
        {"message_id": 21, "kind": "video", "caption": ""},
    ]
    items = db.group_items(raw)
    check("group: 2 items", len(items) == 2)
    check("group: item1 has 2 videos + 1 srt",
          len(items[0]["videos"]) == 2 and len(items[0]["srts"]) == 1)
    check("group: item2 has 1 video, 0 srt",
          len(items[1]["videos"]) == 1 and len(items[1]["srts"]) == 0)
    check("group: oldest-first by db_message_id",
          items[0]["db_message_id"] < items[1]["db_message_id"])

    # ── 3. MongoDB layer (real connection) ────────────────────
    await db.connect()
    await db.ping()
    check("mongo: connect + ping", True)

    s = await db.get_settings()
    check("settings defaults", s["shortener_enabled"] is False
          and s["verify_hours"] == 6 and s["token_ttl_minutes"] == 10)

    await db.update_settings({"verify_hours": 8, "token_ttl_minutes": 7,
                              "auto_delete_minutes": 60})
    s = await db.get_settings()
    check("settings update", s["verify_hours"] == 8 and s["token_ttl_minutes"] == 7)

    await db.touch_user(999)
    u = await db.get_user(999)
    check("user touch", u and u["user_id"] == 999)

    until = await db.mark_verified(999, 1)
    u = await db.get_user(999)
    check("mark_verified", u["verified_until"] > db.now() and abs(u["verified_until"] - until) < 2)

    await db.set_banned(555, True)
    check("ban", (await db.get_user(555))["banned"] is True)
    await db.set_banned(555, False)
    check("unban", (await db.get_user(555))["banned"] is False)

    await db.set_user_autodelete(999, 5)
    check("user autodelete override", (await db.get_user(999))["auto_delete_override"] == 5)

    # files queue
    for mid in (21, 20):  # insert out of order on purpose
        await db.upsert_item({"file_id": f"f{mid}", "db_message_id": mid,
                              "cover_message_id": mid, "caption": f"C{mid}",
                              "videos": [{"db_message_id": mid + 1, "caption": ""}],
                              "srts": []})
    nxt = await db.next_unposted()
    check("queue oldest-first", nxt["db_message_id"] == 20)
    await db.mark_posted(20, 12345)
    nxt = await db.next_unposted()
    check("queue advances after mark_posted", nxt["db_message_id"] == 21)
    # re-upsert must PRESERVE posted status
    await db.upsert_item({"file_id": "f20", "db_message_id": 20,
                          "cover_message_id": 20, "caption": "C20-edit",
                          "videos": [{"db_message_id": 21, "caption": ""}], "srts": []})
    f20 = await db._db.files.find_one({"db_message_id": 20})
    check("re-scan preserves posted flag", f20["posted"] is True and f20["caption"] == "C20-edit")

    # tokens
    tok = await db.create_token(999, "f20", 10, kind="deliver")
    t = await db.get_token(tok)
    check("token create", t["used"] is False and t["user_id"] == 999)
    await db.mark_token_used(tok)
    check("token burn", (await db.get_token(tok))["used"] is True)

    # join requests / admins
    await db.record_join_request(777)
    check("join request recorded", await db.has_join_request(777))
    check("no join request", not await db.has_join_request(778))
    await db.add_db_admin(888)
    check("db admin", await db.is_db_admin(888))
    check("env admin", await utils.is_admin(999))
    check("non-admin", not await utils.is_admin(12345))

    # deletions
    await db.add_deletion(999, [1, 2], db.now() - 1)  # already due
    due = await db.due_deletions()
    check("due deletions", len(due) >= 1)
    for d in due:
        await db.remove_deletion(d["_id"])
    check("deletions removed", len(await db.due_deletions()) == 0)

    # ── 4. application factories ──────────────────────────────
    import bot1
    import bot2
    from telegram.ext import CommandHandler

    app1 = bot1.build_bot1()
    app2 = bot2.build_bot2()
    cmds1 = {c for h in app1.handlers[0] if isinstance(h, CommandHandler)
             for c in h.commands}
    cmds2 = {c for h in app2.handlers[0] if isinstance(h, CommandHandler)
             for c in h.commands}
    expected_admin = {"shortener", "shortenerapi", "setverifytime", "settokenttl",
                      "shortenermsg", "shortenerbotmsg", "verifymsg", "shortenerbtn",
                      "clearshortenerbtns", "protect", "broadcast", "stats", "ban",
                      "unban", "addadmin", "setforcesub", "setautodelete",
                      "setpostchannel", "setdbchannel", "setposttime", "dripnow",
                      "rescandb", "scandb", "setschedule", "schedule",
                      "pauseposting", "resumeposting", "queueinfo", "queue_reset",
                      "setpostmainchannel", "setposttag"}
    check("bot1 registers /start", "start" in cmds1)
    check("bot1 registers all admin commands", expected_admin <= cmds1,
          f"missing={expected_admin - cmds1}")
    check("bot2 registers /start + /setautodelete", {"start", "setautodelete"} <= cmds2)

    # ── 5. Bot 1 gate flow ────────────────────────────────────
    await db.update_settings({"force_sub_channel_id": None,
                              "shortener_enabled": False})
    fb = FakeBot()

    # unknown file
    await bot1.process_file(fb, 999, 999, "nope")
    check("gate: unknown file rejected", "no longer available" in fb.sent[-1][1])

    # shortener OFF -> straight to deliver link
    fb.sent.clear()
    await bot1.process_file(fb, 999, 999, "f21")
    joined = str(fb.sent[-1])
    check("gate: shortener off -> Get File link",
          "deliverybot_test" in joined and "deliver_f21_" in joined)

    # shortener ON -> verify link wrapped by (mocked) shortener
    await db.update_settings({"shortener_enabled": True})
    import shortener
    orig_shorten = shortener.shorten
    captured = {}

    async def fake_shorten(url):
        captured["url"] = url
        return "https://vplink.in/AbCdEf"

    shortener.shorten = fake_shorten
    fb.sent.clear()
    await bot1.process_file(fb, 999, 999, "f21")
    shortener.shorten = orig_shorten
    joined = str(fb.sent[-1])
    check("gate: shortener on -> vplink button", "vplink.in/AbCdEf" in joined)
    check("gate: verify deep link was the shortener target",
          "gatebot_test?start=verify_f21_" in captured.get("url", ""))
    vtok = captured["url"].split("verify_f21_")[1]
    vdoc = await db.get_token(vtok)
    check("gate: verify token stored, kind=verify, bound to user",
          vdoc and vdoc["kind"] == "verify" and vdoc["user_id"] == 999
          and vdoc["file_id"] == "f21")

    # force-sub blocks when not a member
    await db.update_settings({"force_sub_channel_id": -100111})
    fb2 = FakeBot(); fb2.membership = False
    await bot1.process_file(fb2, 4242, 4242, "f21")
    joined = str(fb2.sent[-1])
    check("gate: non-member gets Join+Check buttons",
          "Join Channel" in joined and "checksub:f21" in joined)

    # pending join request satisfies the gate
    await db.record_join_request(4243)
    fb3 = FakeBot(); fb3.membership = False
    await bot1.process_file(fb3, 4243, 4243, "f21")
    check("gate: pending join request passes",
          "Join Channel" not in str(fb3.sent[-1]))

    # ── 6. verify return flow ─────────────────────────────────
    fb.sent.clear()
    await bot1.process_verify(fb, 999, 999, "f21", "deadbeef")
    check("verify: bad token rejected", "expired" in fb.sent[-1][1])

    await bot1.process_verify(fb, 555, 555, "f21", vtok)  # wrong user
    check("verify: other user rejected", "another user" in fb.sent[-1][1])
    check("verify: token NOT burned on wrong user",
          (await db.get_token(vtok))["used"] is False)

    fb.sent.clear()
    await db.update_settings({"force_sub_channel_id": None})
    await db._db.tokens.update_one({"token": vtok},
                                   {"$set": {"created_at": db.now() - 200}})
    await bot1.process_verify(fb, 999, 999, "f21", vtok)
    joined = str(fb.sent[-1])
    check("verify: success -> deliver link issued", "deliver_f21_" in joined)
    check("verify: token burned after success", (await db.get_token(vtok))["used"] is True)
    check("verify: user marked verified",
          (await db.get_user(999))["verified_until"] > db.now())
    dtok = joined.split("deliver_f21_")[1].split("'")[0].split('"')[0]
    ddoc = await db.get_token(dtok)
    check("verify: deliver token bound to same user+file",
          ddoc and ddoc["user_id"] == 999 and ddoc["file_id"] == "f21"
          and ddoc["kind"] == "deliver")

    # ── 7. Bot 2 delivery flow ────────────────────────────────
    fb.sent.clear()
    await bot2.process_delivery(fb, 999, 999, "f21", "badtoken")
    check("deliver: bad token rejected", "tap Download again" in fb.sent[-1][1])

    await bot2.process_delivery(fb, 555, 555, "f21", dtok)
    check("deliver: wrong user rejected", "not issued for you" in fb.sent[-1][1])
    check("deliver: token intact on wrong user",
          (await db.get_token(dtok))["used"] is False)

    fb.sent.clear(); fb.copied.clear()
    await db.update_settings({"db_channel_id": -100999, "auto_delete_minutes": 30})
    await bot2.process_delivery(fb, 999, 999, "f21", dtok)
    check("deliver: copy_message used (server-side)", len(fb.copied) == 1
          and fb.copied[0][1] == -100999)
    check("deliver: token burned", (await db.get_token(dtok))["used"] is True)
    check("deliver: auto-delete note shown", "auto-deleted" in fb.sent[-1][1])
    due = await db.due_deletions()
    check("deliver: deletion queued (not yet due)", len(due) == 0)

    # multi-version item -> chooser
    await db.upsert_item({"file_id": "fmulti", "db_message_id": 50,
                          "cover_message_id": 50, "caption": "multi",
                          "videos": [{"db_message_id": 51, "caption": "SD"},
                                     {"db_message_id": 52, "caption": "HD"}],
                          "srts": [{"db_message_id": 53, "caption": "subs"}]})
    mtok = await db.create_token(999, "fmulti", 10, kind="deliver")
    fb.sent.clear()
    await bot2.process_delivery(fb, 999, 999, "fmulti", mtok)
    joined = str(fb.sent[-1])
    check("deliver: multi-version shows chooser",
          "dl:fmulti:0:999" in joined and "dl:fmulti:1:999" in joined)

    # chooser callback delivers chosen version + srt
    fb.copied.clear(); fb.sent.clear()
    q = FakeQuery(999, "dl:fmulti:1:999")
    upd = FakeUpdate(999); upd.callback_query = q
    await bot2.on_download(upd, FakeContext(bot=fb))
    check("chooser: HD copied", fb.copied and fb.copied[0][2] == 52)
    check("chooser: srt copied too", len(fb.copied) == 2 and fb.copied[1][2] == 53)

    q2 = FakeQuery(555, "dl:fmulti:0:999")  # other user taps
    upd2 = FakeUpdate(555); upd2.callback_query = q2
    sent_before = len(fb.sent)
    await bot2.on_download(upd2, FakeContext(bot=fb))
    check("chooser: wrong user blocked", q2.answers and "not issued for you" in q2.answers[0][0]
          and len(fb.sent) == sent_before)

    # user /setautodelete override
    upd = FakeUpdate(999)
    await bot2.setautodelete(upd, FakeContext(args=["12hour"]))
    check("bot2 user override 12hour",
          (await db.get_user(999))["auto_delete_override"] == 720)
    upd = FakeUpdate(999)
    await bot2.setautodelete(upd, FakeContext(args=["never"]))
    check("bot2 user override never -> 0",
          (await db.get_user(999))["auto_delete_override"] == 0)

    # deletion sweeper
    await db.add_deletion(999, [111, 222], db.now() - 5)
    fb.deleted.clear()
    await bot2.sweep_deletions(FakeContext(bot=fb))
    check("sweeper deletes due messages", set(fb.deleted) == {(999, 111), (999, 222)})
    check("sweeper clears queue", len(await db.due_deletions()) == 0)

    # ── 8. admin commands (every one) ─────────────────────────
    import bot1_admin as adm

    async def run(cmd, args=None, text=None, uid=999, bot=None):
        upd = FakeUpdate(uid)
        if text is not None:
            upd.message.text = text
        ctx = FakeContext(bot=bot or FakeBot(), args=args or [])
        await cmd(upd, ctx)
        return upd.message.replies[-1] if upd.message.replies else ""

    r = await run(adm.cmd_shortener, ["on"]);            check("/shortener on", "enabled" in r)
    r = await run(adm.cmd_shortener, ["status"]);        check("/shortener status", "Shortener gate" in r)
    r = await run(adm.cmd_shortener, ["off"]);           check("/shortener off", "disabled" in r)
    r = await run(adm.cmd_shortenerapi, ["https://vplink.in/api"]); check("/shortenerapi", "vplink.in/api" in r)
    r = await run(adm.cmd_setverifytime, ["6"]);         check("/setverifytime", "6 hours" in r)
    r = await run(adm.cmd_settokenttl, ["15"]);          check("/settokenttl", "15 minutes" in r)
    r = await run(adm.cmd_shortenermsg, text="/shortenermsg HEADING"); check("/shortenermsg", "updated" in r)
    check("  -> heading stored", (await db.get_settings())["shortener_msg"] == "HEADING")
    r = await run(adm.cmd_shortenerbotmsg, text="/shortenerbotmsg BODY"); check("/shortenerbotmsg", "updated" in r)
    r = await run(adm.cmd_verifymsg, text="/verifymsg WELCOME"); check("/verifymsg", "updated" in r)
    r = await run(adm.cmd_shortenerbtn, text="/shortenerbtn My Site | https://example.com")
    check("/shortenerbtn", "Added button" in r)
    check("  -> button stored", (await db.get_settings())["shortener_buttons"][0]["label"] == "My Site")
    r = await run(adm.cmd_clearshortenerbtns);           check("/clearshortenerbtns", "removed" in r)
    r = await run(adm.cmd_ban, ["4242"]);                check("/ban", "Banned" in r)
    r = await run(adm.cmd_unban, ["4242"]);              check("/unban", "Unbanned" in r)
    r = await run(adm.cmd_addadmin, ["31337"]);          check("/addadmin", "now an admin" in r)
    r = await run(adm.cmd_setforcesub, ["-100555"]);     check("/setforcesub", "Force-subscribe channel set" in r)
    r = await run(adm.cmd_setforcesub, ["off"]);         check("/setforcesub off", "disabled" in r)
    r = await run(adm.cmd_setautodelete, ["7day"]);      check("/setautodelete 7day", "7 days" in r)
    check("  -> stored as minutes", (await db.get_settings())["auto_delete_minutes"] == 10080)
    r = await run(adm.cmd_setpostchannel, ["-100777"]);  check("/setpostchannel", "-100777" in r)
    r = await run(adm.cmd_setdbchannel, ["-100888"]);    check("/setdbchannel", "-100888" in r)
    r = await run(adm.cmd_setposttime, ["20:30"]);       check("/setposttime", "20:30" in r)
    r = await run(adm.cmd_stats);                        check("/stats", "Statistics" in r and "Users" in r)
    r = await run(adm.cmd_dripnow);                      check("/dripnow", "Posted item" in r or "Nothing posted" in r)
    r = await run(adm.cmd_scandb, ["-100888"]);          check("/scandb (no creds -> clean error)",
                                                                "Scan failed" in r)
    r = await run(adm.cmd_rescandb);                     check("/rescandb (no creds -> clean error)",
                                                                "Scan failed" in r)
    # broadcast
    bbot = FakeBot()
    r = await run(adm.cmd_broadcast, text="/broadcast hello all", bot=bbot)
    check("/broadcast summary", "Done" in r and "Sent" in r)
    check("/broadcast reached users", len(bbot.sent) >= 1)

    # non-admin blocked
    upd = FakeUpdate(uid=12345)
    await adm.cmd_stats(upd, FakeContext())
    check("admin guard blocks non-admin", "only by admins" in upd.message.replies[-1])

    # /protect command
    r = await run(adm.cmd_protect, ["on"])
    check("/protect on", "enabled" in r
          and (await db.get_settings())["protect_content"] is True)
    r = await run(adm.cmd_protect, ["off"])
    check("/protect off", "disabled" in r
          and (await db.get_settings())["protect_content"] is False)
    r = await run(adm.cmd_protect, [])
    check("/protect status", "OFF" in r)
    upd = FakeUpdate(uid=12345)
    await adm.cmd_protect(upd, FakeContext(args=["on"]))
    check("/protect blocked for non-admin", "only by admins" in upd.message.replies[-1]
          and (await db.get_settings())["protect_content"] is False)

    # /stats shows every connected channel with an embedded link
    await db.update_settings({"db_channel_id": -100111, "post_channel_id": -100222,
                              "post_main_channel_id": -100333,
                              "force_sub_channel_id": -100444})
    r = await run(adm.cmd_stats)
    check("/stats lists all channels",
          "Database:" in r and "Post Channel:" in r
          and "Main Posting Channel:" in r and "Force-Sub Channel:" in r)
    check("/stats embeds channel invite links", "https://t.me/forcechannel" in r)
    check("/stats shows protect state", "Content protection" in r)

    # ── 9. /start handlers ────────────────────────────────────
    fb = FakeBot()
    upd = FakeUpdate(999)
    await bot1.start(upd, FakeContext(bot=fb, args=[]))
    check("bot1 /start welcome", "Welcome" in upd.message.replies[-1])

    upd = FakeUpdate(999)
    await bot1.start(upd, FakeContext(bot=fb, args=["file_f21"]))
    check("bot1 /start file_ deep link routes", len(fb.sent) >= 1)

    upd = FakeUpdate(999)
    await bot2.start(upd, FakeContext(bot=fb, args=[]))
    check("bot2 /start welcome", "delivers your files" in upd.message.replies[-1])

    # banned user blocked at /start
    await db.set_banned(6001, True)
    upd = FakeUpdate(6001)
    await bot1.start(upd, FakeContext(bot=FakeBot(), args=["file_f21"]))
    check("bot1 banned user blocked", "banned" in upd.message.replies[-1])
    await db.set_banned(6001, False)

    # ── 10. shortener module parsing ──────────────────────────
    class FakeResp:
        def __init__(self, text):
            self.text = text
        def raise_for_status(self):
            pass
        def json(self):
            import json
            return json.loads(self.text)

    class FakeClient:
        payload = "https://vplink.in/xyz123"
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, params=None):
            return FakeResp(self.payload)

    import httpx as _httpx
    orig_client = shortener.httpx.AsyncClient
    shortener.httpx.AsyncClient = lambda *a, **k: FakeClient()
    r = await shortener.shorten("https://t.me/x?start=y")
    check("shortener text mode", r == "https://vplink.in/xyz123")
    FakeClient.payload = '{"status":"success","shortenedUrl":"\\"https://vplink.in/jso\\""}'
    r = await shortener.shorten("https://t.me/x?start=y")
    check("shortener JSON mode", r == "https://vplink.in/jso")
    FakeClient.payload = '{"status":"error","message":"bad key"}'
    r = await shortener.shorten("https://t.me/x?start=y")
    check("shortener error mode -> None", r is None)
    shortener.httpx.AsyncClient = orig_client

    # ── 11. FastAPI app + routes (import main; no lifespan) ───
    import main as m
    routes = {r.path for r in m.app.routes}
    check("fastapi routes", {"/health", "/bot1/webhook", "/bot2/webhook"} <= routes)

    # ── 11b. posting flow: protect flag + main-channel tag ─────
    # deterministic queue state: mark everything posted, then add fresh items
    await db._db.files.update_many({"posted": False}, {"$set": {"posted": True}})
    await db.update_settings({"post_channel_id": -100222, "db_channel_id": -100111,
                              "post_main_channel_id": None, "post_tag": None,
                              "protect_content": True, "queue_cursor": None})
    await db.clear_queue_cursor()
    await db.upsert_item({"file_id": "f100", "db_message_id": 100,
                          "cover_message_id": 100, "caption": "cap100",
                          "videos": [{"db_message_id": 101, "caption": ""}],
                          "srts": []})
    fb = FakeBot()
    item = await bot1.do_post(fb)
    check("do_post posts oldest unposted item", item and item["file_id"] == "f100")
    check("channel posts stay unprotected even when /protect is on",
          fb.copied and fb.copied[0] == (-100222, -100111, 100)
          and fb.copy_kwargs[0].get("protect_content") in (None, False))
    _btn = fb.copy_kwargs[0].get("reply_markup")
    _bt = _btn.inline_keyboard[0][0].text if _btn else ""
    check("download button shows the post number",
          _bt.startswith("#") and "𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱" in _bt)

    await db.update_settings({"protect_content": False})
    await db.upsert_item({"file_id": "f110", "db_message_id": 110,
                          "cover_message_id": 110, "caption": "cap110",
                          "videos": [{"db_message_id": 111, "caption": ""}],
                          "srts": []})
    fb = FakeBot()
    item = await bot1.do_post(fb)
    check("channel posts unprotected when /protect off",
          item and item["file_id"] == "f110"
          and fb.copy_kwargs[0].get("protect_content") in (None, False))

    # main-channel forward: tag sent first, then the just-published post
    await db.upsert_item({"file_id": "f120", "db_message_id": 120,
                          "cover_message_id": 120, "caption": "cap120",
                          "videos": [{"db_message_id": 121, "caption": ""}],
                          "srts": []})
    await db.update_settings({"post_main_channel_id": -100333,
                              "post_tag": "#NewDrop"})
    fb = FakeBot()
    item = await bot1.do_post(fb)
    check("do_post forwards the POST-channel post to main (real forward)",
          (-100333, -100222, 8001) in fb.forwarded)
    check("do_post sends tag as a QUOTE-REPLY to the forwarded post",
          any(cid == -100333 and "#NewDrop" in txt
              and kw.get("reply_to_message_id") == 7001
              for cid, txt, kw in fb.sent))

    # tag message fails -> tag must still be embedded as the forward's caption
    class TagFailBot(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            if chat_id == -100333:
                raise RuntimeError("no send rights in main channel")
            return await super().send_message(chat_id, text, **kw)

    await db.upsert_item({"file_id": "f130", "db_message_id": 130,
                          "cover_message_id": 130, "caption": "cap130",
                          "videos": [{"db_message_id": 131, "caption": ""}],
                          "srts": []})
    tf = TagFailBot()
    item = await bot1.do_post(tf)
    check("do_post tag failure -> forward still happens, no crash",
          bool(item) and (-100333, -100222, 8001) in tf.forwarded)

    # /queueinfo shows queued items with embedded channel links (HTML)
    await db.upsert_item({"file_id": "f140", "db_message_id": 140,
                          "cover_message_id": 140, "caption": "My Cool Video",
                          "videos": [{"db_message_id": 141, "caption": ""}],
                          "srts": []})
    upd = FakeUpdate()
    await adm.cmd_queueinfo(upd, FakeContext())
    qreply = upd.message.replies[-1] if upd.message.replies else ""
    check("/queueinfo shows queued items", "Queue info" in qreply
          and "My Cool Video" in qreply)
    check("/queueinfo embeds links to the queued posts",
          'href="https://t.me/c/111/140"' in qreply)
    check("/queueinfo shows global post numbers", "#" in qreply)

    # /queue_reset rewinds: re-queue everything from the requested post
    await db.queue_reset_to_position(2)
    cur2 = (await db.queue_summary(1))["cursor"]
    nxt = await db.next_unposted()
    check("/queue_reset rewinds so the requested post is next",
          nxt is not None and cur2 is not None
          and nxt["db_message_id"] == cur2)

    # self-heal: item whose DB message was deleted is skipped, queue moves on
    await db.upsert_item({"file_id": "f150", "db_message_id": 150,
                          "cover_message_id": 150, "caption": "dead item",
                          "videos": [{"db_message_id": 151, "caption": ""}],
                          "srts": []})
    await db.upsert_item({"file_id": "f160", "db_message_id": 160,
                          "cover_message_id": 160, "caption": "alive item",
                          "videos": [{"db_message_id": 161, "caption": ""}],
                          "srts": []})
    await db._db.files.update_many({}, {"$set": {"posted": True}})
    await db._db.files.update_many({"db_message_id": {"$in": [150, 160]}},
                                   {"$set": {"posted": False}})
    await db.clear_queue_cursor()
    class DeadMsgBot(FakeBot):
        async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
            if message_id == 150:
                raise RuntimeError("message to copy not found")
            return await super().copy_message(chat_id, from_chat_id,
                                              message_id, **kw)
    await db.update_settings({"post_main_channel_id": None, "post_tag": None})
    dbot = DeadMsgBot()
    healed = await bot1.do_post(dbot)
    check("queue heals itself past a deleted DB post",
          healed is not None and healed["file_id"] == "f160")

    # ── 11c. anti-bypass strikes + auto-ban ───────────────────
    await db.upsert_item({"file_id": "f170", "db_message_id": 170,
                          "cover_message_id": 170, "caption": "strike item",
                          "videos": [{"db_message_id": 171, "caption": ""}],
                          "srts": []})
    fb = FakeBot()
    t1 = await db.create_token(7777, "f170", 60, kind="verify")
    await bot1.process_verify(fb, 7777, 7777, "f170", t1, "Noob7")
    check("bypass: strike 1 warning issued",
          any("Strike 1/3" in txt for _, txt, _ in fb.sent))
    check("bypass: bypassed token burned",
          (await db.get_token(t1))["used"] is True)
    t2 = await db.create_token(7777, "f170", 60, kind="verify")
    await bot1.process_verify(fb, 7777, 7777, "f170", t2, "Noob7")
    check("bypass: strike 2 warning issued",
          any("Strike 2/3" in txt for _, txt, _ in fb.sent))
    t3 = await db.create_token(7777, "f170", 60, kind="verify")
    await bot1.process_verify(fb, 7777, 7777, "f170", t3, "Noob7")
    check("bypass: 3rd strike auto-bans user",
          (await db.get_user(7777))["banned"] is True)
    check("bypass: admin alerted (username + elapsed + unban hint)",
          any(cid == 999 and "auto-banned" in txt and "@Noob7" in txt
              and "/unban 7777" in txt for cid, txt, _ in fb.sent))

    # banned users cannot get files from Bot 2 either
    fb2 = FakeBot()
    dtk = await db.create_token(7777, "f170", 10, kind="deliver")
    await bot2.process_delivery(fb2, 7777, 7777, "f170", dtk)
    check("banned user blocked in Bot 2", "banned" in fb2.sent[-1][1])

    # /unban resets the strike counter
    await db.set_banned(7777, False)
    check("unban resets strikes",
          int((await db.get_user(7777)).get("strikes") or 0) == 0)

    # a legitimate slow solve passes and is not flagged
    t4 = await db.create_token(8888, "f170", 60, kind="verify")
    await db._db.tokens.update_one({"token": t4},
                                   {"$set": {"created_at": db.now() - 200}})
    fb.sent.clear()
    await bot1.process_verify(fb, 8888, 8888, "f170", t4)
    check("legit slow verify passes", "deliver_f170_" in str(fb.sent[-1]))

    # ── 11d. /rescandb drops deleted posts (queue renumbers) ──
    await db.ingest_raw({"message_id": 900, "kind": "cover", "caption": "a"})
    await db.ingest_raw({"message_id": 901, "kind": "video", "caption": ""})
    await db.ingest_raw({"message_id": 910, "kind": "cover", "caption": "b"})
    await db.ingest_raw({"message_id": 911, "kind": "video", "caption": ""})
    await db.rebuild_items()
    await db._db.raw.delete_one({"message_id": 900})
    await db._db.raw.delete_one({"message_id": 901})
    await db.rebuild_items()
    check("rescan drops deleted DB posts from the queue",
          await db.get_item_by_file_id("f900") is None
          and await db.get_item_by_file_id("f910") is not None)

    # ── cleanup ───────────────────────────────────────────────
    await db._client.drop_database("video_bots_dev_test")
    await db.close()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{'='*50}\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed.")
    if failed:
        print("FAILED:", *failed, sep="\n  - ")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
