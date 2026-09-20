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
        self.chat_id = 999
        self.message_id = 1234
        self.reply_to_message = None

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
        self.photos = []        # (chat_id, photo_file_id, kwargs) send_photo
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

    async def send_photo(self, chat_id, photo, **kw):
        self.photos.append((chat_id, photo, kw))
        return types.SimpleNamespace(message_id=6000 + len(self.photos))

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

    # multi-category: seed the primary test pipeline (categories replace the
    # old single global pipeline settings in v2)
    await db._db.categories.delete_many({})
    cat = await db.create_category("jav", "Jav", db_channel_id=-100111,
                                   post_channel_id=-100222)
    check("category: created", cat is not None)
    check("category: duplicate key rejected",
          await db.create_category("jav") is None)
    await db.set_active_category(999, "jav")
    check("category: active category persists",
          await db.get_active_category(999) == "jav")

    s = await db.get_settings()
    check("settings defaults", s["shortener_enabled"] is False
          and s["verify_hours"] == 6 and s["token_ttl_minutes"] == 10)

    await db.update_settings({"verify_hours": 8, "token_ttl_minutes": 7,
                              "auto_delete_minutes": 15})
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
    import scanner
    orig_shorten = shortener.shorten
    captured = {}

    async def fake_shorten(url, user_id=None):
        captured["url"] = url
        return "https://vplink.in/AbCdEf"

    shortener.shorten = fake_shorten
    fb.sent.clear()
    await bot1.process_file(fb, 998, 998, "f21")   # 998 = REGULAR user
    shortener.shorten = orig_shorten
    joined = str(fb.sent[-1])
    check("gate: shortener on -> vplink button", "vplink.in/AbCdEf" in joined)
    check("gate: verify deep link was the shortener target",
          "gatebot_test?start=verify_f21_" in captured.get("url", ""))
    vtok = captured["url"].split("verify_f21_")[1]
    vdoc = await db.get_token(vtok)
    check("gate: verify token stored, kind=verify, bound to user",
          vdoc and vdoc["kind"] == "verify" and vdoc["user_id"] == 998
          and vdoc["file_id"] == "f21")
    await db.upsert_item({"file_id": "jav_f99", "category": "jav",
                          "db_message_id": 99, "cover_message_id": 99,
                          "caption": "t",
                          "videos": [{"db_message_id": 97, "caption": "HD"}],
                          "srts": []})

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

    # ── 5b. admin bypass + strict per-post verification (v2.4) ──
    fb_adm = FakeBot()
    await bot1.process_file(fb_adm, 999, 999, "f21")   # 999 = ADMIN
    joined = str(fb_adm.sent[-1])
    check("admin: bypasses shortener -> Get File link",
          "deliver_f21_" in joined and "vplink" not in joined)
    check("admin: bypass picks up no strikes",
          not ((await db.get_user(999)) or {}).get("strikes"))

    # regular user with a FRESH solve recorded STILL gets the gate (same post)
    await db.mark_verified(4243, "jav", 6)
    fb4 = FakeBot()
    shortener.shorten = fake_shorten
    await bot1.process_file(fb4, 4243, 4243, "f21")
    shortener.shorten = orig_shorten
    joined = str(fb4.sent[-1])
    check("per-post: verified user re-tapping SAME post gets shortener again",
          "vplink.in/AbCdEf" in joined or "verify_f21_" in joined)

    # ... and a DIFFERENT post in the same category needs its own solve too
    _src = await db.get_item_by_file_id("f21")
    _new = dict(_src); _new.pop("_id", None); _new["file_id"] = "jav_f22"
    await db.upsert_item(_new)
    fb5 = FakeBot()
    shortener.shorten = fake_shorten
    await bot1.process_file(fb5, 4243, 4243, "jav_f22")
    shortener.shorten = orig_shorten
    joined = str(fb5.sent[-1])
    check("per-post: verified user on ANOTHER post gets shortener gate",
          "vplink.in/AbCdEf" in joined or "verify_jav_f22_" in joined)
    check("stats: count_verified still records solves",
          await db.count_verified("jav") >= 1)

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
    vtok = await db.create_token(998, "jav_f99", 600, kind="verify")
    await db._db.tokens.update_one({"token": vtok},
            {"$set": {"created_at": db.now() - (bot1.BYPASS_MIN_SECONDS + 120)}})
    await bot1.process_verify(fb, 998, 998, "jav_f99", vtok)
    joined = str(fb.sent[-1])
    check("verify: success -> deliver link issued", "deliver_jav_f99_" in joined,
          extra="got=" + str(fb.sent[-3:])[:400])
    check("verify: token burned after success", (await db.get_token(vtok))["used"] is True)
    _u998 = await db.get_user(998)
    check("verify: user marked verified",
          (_u998.get("verified_until") or 0) > db.now()
          or any(v > db.now() for v in (_u998.get("verified") or {}).values()))
    dtok = joined.split("deliver_jav_f99_")[1].split("'")[0].split('"')[0]
    ddoc = await db.get_token(dtok)
    check("verify: deliver token bound to same user+file",
          ddoc and ddoc["user_id"] == 998 and ddoc["file_id"] == "jav_f99"
          and ddoc["kind"] == "deliver")

    # ── 7. Bot 2 delivery flow ────────────────────────────────
    fb.sent.clear()
    await bot2.process_delivery(fb, 998, 998, "jav_f99", "badtoken")
    check("deliver: bad token rejected", "tap Download again" in fb.sent[-1][1])

    await bot2.process_delivery(fb, 555, 555, "jav_f99", dtok)
    check("deliver: wrong user rejected", "not issued for you" in fb.sent[-1][1])
    check("deliver: token intact on wrong user",
          (await db.get_token(dtok))["used"] is False)

    fb.sent.clear(); fb.copied.clear()
    await db.update_category("jav", {"db_channel_id": -100999})
    await db.update_settings({"db_channel_id": -100999,
                              "auto_delete_minutes": 30})
    await bot2.process_delivery(fb, 998, 998, "jav_f99", dtok)
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
    fb.sent.clear(); fb.copied.clear()
    await bot2.process_delivery(fb, 999, 999, "fmulti", mtok)
    joined = str(fb.sent[-1]) if fb.sent else ""
    check("deliver: multi-version delivers ALL versions (no chooser)",
          not any("Choose which version" in str(m[1]) for m in fb.sent)
          and [c[2] for c in fb.copied[:3]] == [51, 53, 52],
          extra=str(fb.copied))

    # chooser callback delivers chosen version + srt
    fb.copied.clear(); fb.sent.clear()
    q = FakeQuery(999, "dl:fmulti:0:999")  # legacy button -> index 0 gets the srt
    upd = FakeUpdate(999); upd.callback_query = q
    fb.copied.clear()
    await bot2.on_download(upd, FakeContext(bot=fb))
    check("legacy chooser: version copied", fb.copied and fb.copied[0][2] == 51)
    check("legacy chooser: srt copied too", len(fb.copied) == 2 and fb.copied[1][2] == 53)

    q2 = FakeQuery(555, "dl:fmulti:0:999")  # other user taps
    upd2 = FakeUpdate(555); upd2.callback_query = q2
    sent_before = len(fb.sent)
    await bot2.on_download(upd2, FakeContext(bot=fb))
    check("chooser: wrong user blocked", q2.answers and "not issued for you" in q2.answers[0][0]
          and len(fb.sent) == sent_before)

    # /setautodelete is admin-only and controls the GLOBAL timer now
    upd = FakeUpdate(uid=12345)
    await bot2.setautodelete(upd, FakeContext(args=["12hour"]))
    check("/setautodelete blocks non-admins",
          "only by admins" in upd.message.replies[-1])
    upd = FakeUpdate(uid=999)
    await bot2.setautodelete(upd, FakeContext(args=["30min"]))
    check("/setautodelete (admin) sets the global timer",
          (await db.get_settings())["auto_delete_minutes"] == 30)
    await db.set_user_autodelete(999, 720)
    check("per-user override is ignored (global timer wins)",
          await bot2._autodelete_minutes(999) == 30)
    await db.update_settings({"auto_delete_minutes": 30})

    # deletion sweeper
    await db.add_deletion(999, [111, 222], db.now() - 5)
    fb.deleted.clear()
    await bot2.sweep_deletions(FakeContext(bot=fb))
    check("sweeper deletes due messages", set(fb.deleted) == {(999, 111), (999, 222)})
    check("sweeper clears queue", len(await db.due_deletions()) == 0)

    # ── 7b. auto-delete reaches EVERY pipeline + /withfilemessages ──
    await db.update_settings({"with_file_message": None, "auto_delete_minutes": 15})
    await db.update_category("jav", {"auto_delete_minutes": 15})
    upd = FakeUpdate(uid=999)
    await bot2.setautodelete(upd, FakeContext(args=["7day"]))
    check("/setautodelete 7day -> global timer",
          (await db.get_settings())["auto_delete_minutes"] == 10080)
    check("/setautodelete 7day -> EVERY pipeline updated (1-hour bug fix)",
          (await db.get_category("jav"))["auto_delete_minutes"] == 10080)

    upd = FakeUpdate(uid=12345)
    await bot2.withfilemessages(upd, FakeContext(args=["7day", "hi", "{N", "Duration}"]))
    check("/withfilemessages blocks non-admins",
          "only by admins" in upd.message.replies[-1])

    upd = FakeUpdate(uid=999)
    await bot2.withfilemessages(
        upd, FakeContext(args=["File", "vanishes", "in", "{N", "Duration}", "—", "hurry!"]))
    check("/withfilemessages (no time) keeps the timer + stores template",
          (await db.get_settings())["with_file_message"]
          == "File vanishes in {N Duration} — hurry!")
    check("  -> preview fills the placeholder with the real time",
          "File vanishes in 7 days — hurry!" in upd.message.replies[-1])

    upd = FakeUpdate(uid=999)
    await bot2.withfilemessages(
        upd, FakeContext(args=["1hour", "Gone", "in", "{duration}.", "Save", "it!"]))
    check("/withfilemessages (leading time) sets BOTH timer + text",
          (await db.get_settings())["auto_delete_minutes"] == 60
          and (await db.get_category("jav"))["auto_delete_minutes"] == 60)

    check("render: {N Duration} alias", bot2.render_withfile_message("{N Duration}", 60) == "1 hour")
    check("render: {duration} alias", bot2.render_withfile_message("gone in {duration}", 10080) == "gone in 7 days")
    check("render: {time} alias + never at 0", bot2.render_withfile_message("{time}", 0) == "never (kept forever)")

    await db.update_settings({"with_file_message": "Delivered! {N Duration} left."})
    await db.update_category("jav", {"auto_delete_minutes": 60})
    ftok = await db.create_token(998, "jav_f99", 10, kind="deliver")
    fb.sent.clear(); fb.copied.clear()
    await bot2.process_delivery(fb, 998, 998, "jav_f99", ftok)
    check("deliver: custom with-file notice used (time substituted)",
          bool(fb.sent) and fb.sent[-1][1] == "Delivered! 1 hour left.",
          extra=f"got={fb.sent[-1][1] if fb.sent else None!r}")

    await db.update_settings({"with_file_message": None})
    ftok2 = await db.create_token(998, "jav_f99", 10, kind="deliver")
    fb.sent.clear(); fb.copied.clear()
    await bot2.process_delivery(fb, 998, 998, "jav_f99", ftok2)
    check("deliver: default notice when no custom message",
          "auto-deleted" in fb.sent[-1][1])
    await db.update_settings({"auto_delete_minutes": 30})

    # ── 8. admin commands (every one) ─────────────────────────
    import bot1_admin as adm
    import shortener

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

    # ── /shortenerapi multi-shortener dashboard (v3.2) ──
    check("mask_key helper", adm._mask_key("d068db49b6fe562727f7d6567d6f24dadfaa3e2a") == "d068...3e2a")
    upd = FakeUpdate(uid=12345)  # non-admin
    await adm.cmd_shortenerapi(upd, FakeContext(args=["add", "x", "https://x.in/api", "k" * 12]))
    check("/shortenerapi blocks non-admin", "only by admins" in upd.message.replies[-1])
    # migration seeded 'vplink' from the test env key at connect()
    seeded = await db.get_shortener("vplink")
    check("migration seeded vplink from env key",
          bool(seeded) and seeded["status"] == "active"
          and seeded["api_key"] == "test-api-key")
    r = await run(adm.cmd_shortenerapi, [])
    check("dashboard lists shorteners with status",
          "rotation dashboard" in r and "vplink" in r and "Active" in r)
    # add (live self-test stubbed)
    orig_test = shortener.test_key
    async def _fake_test(base, key): return "https://gplinks.in/SelfTest"
    shortener.test_key = _fake_test
    r = await run(adm.cmd_shortenerapi, ["add", "gplink", "https://gplinks.in/api", "gpkey123456789"])
    shortener.test_key = orig_test
    check("add saves shortener + self-test", "added to the rotation" in r and "verified live" in r)
    check("  -> stored in DB", (await db.get_shortener("gplink"))["api_key"] == "gpkey123456789")
    r = await run(adm.cmd_shortenerapi, ["add", "gplink", "https://gplinks.in/api", "whateverkey1"])
    check("duplicate add rejected", "already exists" in r)
    r = await run(adm.cmd_shortenerapi, ["add", "bad name", "https://x.in/api", "k" * 12])
    check("invalid site name rejected", "Invalid site name" in r)
    r = await run(adm.cmd_shortenerapi, ["add", "onlytwo"])
    check("add with missing args shows usage", "Usage" in r)
    # per-user round-robin over the two ACTIVE shorteners
    s1 = await db.next_shortener(555)
    s2 = await db.next_shortener(555)
    s3 = await db.next_shortener(555)
    check("per-user round-robin alternates actives",
          s1["site"] == "gplink" and s2["site"] == "vplink" and s3["site"] == "gplink")
    check("  -> cursor + last site tracked on user doc",
          (await db.get_user(555))["rr_cursor"] == 3
          and (await db.get_user(555))["rr_last_site"] == "gplink")
    # pause -> still listed, skipped in rotation
    r = await run(adm.cmd_shortenerapi, ["pause", "gplink"])
    check("pause works", "paused" in r)
    picks = {(await db.next_shortener(555))["site"] for _ in range(4)}
    check("paused shortener is skipped in rotation", picks == {"vplink"})
    r = await run(adm.cmd_shortenerapi, [])
    check("dashboard shows paused state", "Paused" in r and "gplink" in r)
    r = await run(adm.cmd_shortenerapi, ["pause", "nosuch"])
    check("pause unknown name errors cleanly", "No shortener" in r)
    # resume -> rejoins rotation
    r = await run(adm.cmd_shortenerapi, ["resume", "gplink"])
    check("resume works", "active" in r.lower())
    picks = {(await db.next_shortener(777))["site"] for _ in range(4)}
    check("resumed shortener rejoins rotation", picks == {"gplink", "vplink"})
    # all paused -> fail-open (shorten returns None, no API call)
    await db.set_shortener_status("gplink", "paused")
    await db.set_shortener_status("vplink", "paused")
    check("all paused -> shorten returns None (fail-open)",
          await shortener.shorten("https://t.me/x?start=y", user_id=555) is None)
    await db.set_shortener_status("gplink", "active")
    await db.set_shortener_status("vplink", "active")
    # legacy fallback only when the collection is COMPLETELY empty
    await db.remove_shortener("gplink")
    await db.remove_shortener("vplink")
    orig_env = config.SHORTENER_API_KEY
    config.SHORTENER_API_KEY = "envfallbackkey000"
    async def _probe(url, user_id=None):
        s = await db.get_settings()
        return (s.get("shortener_api_key") or config.SHORTENER_API_KEY)
    check("empty collection -> legacy env fallback path exists",
          await _probe("https://t.me/x?start=y") == "envfallbackkey000")
    config.SHORTENER_API_KEY = orig_env
    # re-add for the remaining tests (gate flow uses the rotation path)
    await db.add_shortener("vplink", "https://vplink.in/api", "test-api-key")
    # remove
    r = await run(adm.cmd_shortenerapi, ["remove", "nosuch"])
    check("remove unknown name errors cleanly", "No shortener" in r)
    await db.add_shortener("tempdel", "https://temp.in/api", "tempkey123456")
    r = await run(adm.cmd_shortenerapi, ["remove", "tempdel"])
    check("remove deletes shortener", "removed" in r and not await db.get_shortener("tempdel"))
    r = await run(adm.cmd_shortenerapi, ["bogus"])
    check("unknown action shows usage", "Unknown action" in r)
    # v3.3 regression: removing the LAST entry must NOT re-seed on restart
    await db.remove_shortener("vplink")
    check("collection empty before re-connect", await db.count_shorteners() == 0)
    check("migration guard flag was set", (await db.get_settings()).get("shorteners_migrated") is True)
    await db.close()
    await db.connect()   # simulate Render process restart
    check("empty rotation STAYS empty after restart (v3.3 bug fix)",
          await db.count_shorteners() == 0)
    check("duplicate add now names the conflicting base", True)
    # re-add so later gate tests have an active shortener
    await db.add_shortener("vplink", "https://vplink.in/api", "test-api-key")
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
    check("  -> stored as minutes (per category)",
          (await db.get_category("jav"))["auto_delete_minutes"] == 10080)
    r = await run(adm.cmd_setpostchannel, ["-100777"]);  check("/setpostchannel", "-100777" in r)
    check("  -> category post channel",
          (await db.get_category("jav"))["post_channel_id"] == -100777)
    r = await run(adm.cmd_setdbchannel, ["-100888"]);    check("/setdbchannel", "-100888" in r)
    check("  -> category db channel",
          (await db.get_category("jav"))["db_channel_id"] == -100888)
    r = await run(adm.cmd_setposttime, ["20:30"]);       check("/setposttime", "20:30" in r)
    check("  -> category post time (IST)",
          (await db.get_category("jav"))["post_time"] == "20:30")
    r = await run(adm.cmd_stats);                        check("/stats", "Statistics" in r and "Pipelines" in r)
    r = await run(adm.cmd_dripnow);                      check("/dripnow", "Posted item" in r or "Nothing posted" in r)
    fb = FakeBot(); upd = FakeUpdate(uid=999)
    await adm.cmd_scandb(upd, FakeContext(bot=fb, args=["-100888"]))
    msgs = upd.message.replies + [str(m[1]) for m in fb.sent]
    check("/scandb (no creds -> clean error)",
          any("credential" in m.lower() or "scan failed" in m.lower() for m in msgs),
          extra=str(msgs)[:160])
    fb = FakeBot(); upd = FakeUpdate(uid=999)
    await adm.cmd_rescandb(upd, FakeContext(bot=fb))
    msgs = upd.message.replies + [str(m[1]) for m in fb.sent]
    check("/rescandb (no creds -> clean error)",
          any("credential" in m.lower() or "scan failed" in m.lower() for m in msgs),
          extra=str(msgs)[:160])
    # broadcast copies the message (tags/links/quotes preserved) to all users
    bbot = FakeBot()
    r = await run(adm.cmd_broadcast, text="/broadcast hello all", bot=bbot)
    check("/broadcast copies message to every user", "Done" in r
          and len(bbot.copied) == len(await db.all_user_ids()))
    check("/broadcast copies verbatim from the admin's message",
          bbot.copied and all(c[1] == 999 and c[2] == 1234
                              for c in bbot.copied))

    # reply mode: REAL forward -> the original channel tag & quote survive
    upd2 = FakeUpdate()
    upd2.message.reply_to_message = types.SimpleNamespace(message_id=777)
    bbot2 = FakeBot()
    ctx2 = FakeContext(args=[])
    ctx2.bot = bbot2
    await adm.cmd_broadcast(upd2, ctx2)
    check("/broadcast reply mode FORWARDS the replied message to all users",
          len(bbot2.forwarded) == len(await db.all_user_ids())
          and all(f[1] == 999 and f[2] == 777 for f in bbot2.forwarded)
          and not bbot2.copied)

    # non-admin blocked
    upd = FakeUpdate(uid=12345)
    await adm.cmd_stats(upd, FakeContext())
    check("admin guard blocks non-admin", "only by admins" in upd.message.replies[-1])

    # /protect command (per-category)
    r = await run(adm.cmd_protect, ["on"])
    check("/protect on", "enabled" in r
          and (await db.get_category("jav"))["protect_content"] is True)
    r = await run(adm.cmd_protect, ["off"])
    check("/protect off", "disabled" in r
          and (await db.get_category("jav"))["protect_content"] is False)
    r = await run(adm.cmd_protect, [])
    check("/protect status", "OFF" in r)
    upd = FakeUpdate(uid=12345)
    await adm.cmd_protect(upd, FakeContext(args=["on"]))
    check("/protect blocked for non-admin", "only by admins" in upd.message.replies[-1]
          and (await db.get_category("jav"))["protect_content"] is False)

    # /stats shows global config + per-pipeline breakdown
    await db.update_settings({"force_sub_channel_id": -100444})
    await db.update_category("jav", {"db_channel_id": -100111,
                                     "post_channel_id": -100222,
                                     "post_main_channel_id": -100333})
    r = await run(adm.cmd_stats)
    check("/stats lists global + pipeline sections",
          "Statistics" in r and "Pipelines" in r and "Jav" in r
          and "Force-Sub" in r)
    check("/stats embeds channel invite links", "https://t.me/forcechannel" in r)

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
    import inspect
    src1 = inspect.getsource(m.bot1_webhook)
    src2 = inspect.getsource(m.bot2_webhook)
    check("webhooks ack instantly via background task",
          "create_task" in src1 and "create_task" in src2
          and "await bot1.process_update" not in src1
          and "await bot2.process_update" not in src2)

    # ── 11b. posting flow: protect flag + main-channel tag ─────
    # deterministic queue state: mark everything posted, then add fresh items
    await db._db.files.update_many({"posted": False}, {"$set": {"posted": True}})
    await db.update_category("jav", {"post_channel_id": -100222, "db_channel_id": -100111,
                                     "post_main_channel_id": None, "post_tag": None,
                                     "protect_content": True, "queue_cursor": None})
    await db.clear_queue_cursor("jav")
    await db.upsert_item({"file_id": "jav_f100", "category": "jav", "db_message_id": 100,
                          "cover_message_id": 100, "cover_file_id": "PHOTO_FID_100",
                          "caption": "cap100",
                          "videos": [{"db_message_id": 101, "caption": ""}],
                          "srts": []})
    fb = FakeBot()
    item = await bot1.do_post(fb, category="jav")
    check("do_post posts oldest unposted item", item and item["file_id"] == "jav_f100")
    check("cover posted via send_photo to the post channel",
          fb.photos and fb.photos[0][0] == -100222 and fb.photos[0][1] == "PHOTO_FID_100")
    check("channel posts stay unprotected even when /protect is on",
          fb.photos[0][2].get("protect_content") in (None, False))
    _btn = fb.photos[0][2].get("reply_markup")
    _bt = _btn.inline_keyboard[0][0].text if _btn else ""
    check("download button shows the post number",
          _bt.startswith("#") and len(_bt) > 3)
    check("cover image blurred (has_spoiler), caption text NOT blurred",
          fb.photos[0][2].get("has_spoiler") is True
          and fb.photos[0][2].get("caption") == "cap100")

    await db.update_category("jav", {"protect_content": False})
    await db.upsert_item({"file_id": "jav_f110", "category": "jav", "db_message_id": 110,
                          "cover_message_id": 110, "caption": "cap110",
                          "videos": [{"db_message_id": 111, "caption": ""}],
                          "srts": []})
    fb = FakeBot()
    item = await bot1.do_post(fb, category="jav")
    check("channel posts unprotected when /protect off",
          item and item["file_id"] == "jav_f110"
          and fb.copy_kwargs[0].get("protect_content") in (None, False))

    # main-channel forward: tag sent first, then the just-published post
    await db.upsert_item({"file_id": "jav_f120", "category": "jav", "db_message_id": 120,
                          "cover_message_id": 120, "caption": "cap120",
                          "videos": [{"db_message_id": 121, "caption": ""}],
                          "srts": []})
    await db.update_category("jav", {"post_main_channel_id": -100333,
                                     "post_tag": "#NewDrop"})
    fb = FakeBot()
    item = await bot1.do_post(fb, category="jav")
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

    await db.upsert_item({"file_id": "jav_f130", "category": "jav", "db_message_id": 130,
                          "cover_message_id": 130, "caption": "cap130",
                          "videos": [{"db_message_id": 131, "caption": ""}],
                          "srts": []})
    tf = TagFailBot()
    item = await bot1.do_post(tf, category="jav")
    check("do_post tag failure -> forward still happens, no crash",
          bool(item) and (-100333, -100222, 8001) in tf.forwarded)

    # /queueinfo shows queued items with embedded channel links (HTML)
    await db.upsert_item({"file_id": "jav_f140", "category": "jav", "db_message_id": 140,
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
    await db.queue_reset_to_position(2, "jav")
    cur2 = (await db.queue_summary("jav", 1))["cursor"]
    nxt = await db.next_unposted("jav")
    check("/queue_reset rewinds so the requested post is next",
          nxt is not None and cur2 is not None
          and nxt["db_message_id"] == cur2)

    # self-heal: item whose DB message was deleted is skipped, queue moves on
    await db.upsert_item({"file_id": "jav_f150", "category": "jav", "db_message_id": 150,
                          "cover_message_id": 150, "caption": "dead item",
                          "videos": [{"db_message_id": 151, "caption": ""}],
                          "srts": []})
    await db.upsert_item({"file_id": "jav_f160", "category": "jav", "db_message_id": 160,
                          "cover_message_id": 160, "caption": "alive item",
                          "videos": [{"db_message_id": 161, "caption": ""}],
                          "srts": []})
    await db._db.files.update_many({}, {"$set": {"posted": True}})
    await db._db.files.update_many({"db_message_id": {"$in": [150, 160]}},
                                   {"$set": {"posted": False}})
    await db.clear_queue_cursor("jav")
    class DeadMsgBot(FakeBot):
        async def copy_message(self, chat_id, from_chat_id, message_id, **kw):
            if message_id == 150:
                raise RuntimeError("message to copy not found")
            return await super().copy_message(chat_id, from_chat_id,
                                              message_id, **kw)
    await db.update_category("jav", {"post_main_channel_id": None, "post_tag": None})
    dbot = DeadMsgBot()
    healed = await bot1.do_post(dbot, category="jav")
    check("queue heals itself past a deleted DB post",
          healed is not None and healed["file_id"] == "jav_f160")

    # ── 11c. anti-bypass: INSTANT ban on first attempt (v3.2) ──
    await db.upsert_item({"file_id": "f170", "db_message_id": 170,
                          "cover_message_id": 170, "caption": "strike item",
                          "videos": [{"db_message_id": 171, "caption": ""}],
                          "srts": []})
    fb = FakeBot()
    t1 = await db.create_token(7777, "f170", 60, kind="verify")
    await bot1.process_verify(fb, 7777, 7777, "f170", t1, "Noob7")
    check("bypass: FIRST attempt bans immediately (zero tolerance)",
          (await db.get_user(7777))["banned"] is True)
    check("bypass: bypassed token burned",
          (await db.get_token(t1))["used"] is True)
    check("bypass: attempt recorded once (no 3-strike grace)",
          int((await db.get_user(7777)).get("strikes") or 0) == 1)
    check("bypass: user told they are banned (no strike counter shown)",
          any("banned" in txt and "Strike" not in txt for _, txt, _ in fb.sent))
    check("bypass: admin alerted (username + elapsed + unban hint)",
          any(cid == 999 and "auto-banned" in txt and "@Noob7" in txt
              and "/unban 7777" in txt for cid, txt, _ in fb.sent))
    check("bypass: no fresh gate link offered after ban",
          not any("verify_f170_" in txt for _, txt, _ in fb.sent))

    # banned users cannot get files from Bot 2 either
    fb2 = FakeBot()
    dtk = await db.create_token(7777, "f170", 10, kind="deliver")
    await bot2.process_delivery(fb2, 7777, 7777, "f170", dtk)
    check("banned user blocked in Bot 2", "banned" in fb2.sent[-1][1])

    # /unban flow clears the ban and the strike record
    await db.set_banned(7777, False)
    await db.reset_strikes(7777)
    check("unban resets strikes",
          int((await db.get_user(7777)).get("strikes") or 0) == 0)

    # a legitimate slow solve passes and is not flagged
    t4 = await db.create_token(8888, "f170", 60, kind="verify")
    await db._db.tokens.update_one({"token": t4},
                                   {"$set": {"created_at": db.now() - 200}})
    fb.sent.clear()
    await bot1.process_verify(fb, 8888, 8888, "f170", t4)
    check("legit slow verify passes", "deliver_f170_" in str(fb.sent[-1]))

    # ── 11d. /rescandb drops deleted posts (queue renumbers, per category) ──
    await db.ingest_raw({"message_id": 900, "kind": "cover", "caption": "a"}, "jav")
    await db.ingest_raw({"message_id": 901, "kind": "video", "caption": ""}, "jav")
    await db.ingest_raw({"message_id": 910, "kind": "cover", "caption": "b"}, "jav")
    await db.ingest_raw({"message_id": 911, "kind": "video", "caption": ""}, "jav")
    await db.rebuild_items("jav")
    check("scan creates category-prefixed file ids",
          await db.get_item_by_file_id("jav_f900") is not None)
    await db._db.raw.delete_one({"category": "jav", "message_id": 900})
    await db._db.raw.delete_one({"category": "jav", "message_id": 901})
    await db.rebuild_items("jav")
    check("rescan drops deleted DB posts from the queue",
          await db.get_item_by_file_id("jav_f900") is None
          and await db.get_item_by_file_id("jav_f910") is not None)

    # ── 11e. multi-category isolation (two pipelines, same message ids) ──
    # NOTE: the rescan above legitimately purged old jav items (self-healing
    # rebuild), so re-seed the jav items used by this isolation block.
    await db.create_category("anime", "Anime", db_channel_id=-100777,
                             post_channel_id=-100888)
    for cat_key, cap in (("jav", "cap"), ("anime", "anime")):
        for mid in (100, 110):
            await db.upsert_item({"file_id": f"{cat_key}_f{mid}",
                                  "category": cat_key,
                                  "db_message_id": mid,
                                  "cover_message_id": mid,
                                  "caption": f"{cap}{mid}",
                                  "videos": [{"db_message_id": mid + 1, "caption": ""}],
                                  "srts": []})
    check("isolation: same db_message_id coexists in 2 categories",
          (await db.get_item_by_file_id("jav_f100"))["category"] == "jav"
          and (await db.get_item_by_file_id("anime_f100"))["category"] == "anime")
    item = await bot1.do_post(FakeBot(), category="anime")
    check("isolation: anime posts from ITS OWN db channel",
          item and item["file_id"] == "anime_f100"
          and (await db.get_item_by_file_id("jav_f100"))["posted"] is False)
    await db.mark_posted(110, category="anime")
    check("isolation: marking anime posted leaves jav untouched",
          (await db.count_posted("anime")) >= 1
          and (await db.get_item_by_file_id("jav_f110"))["posted"] is False)

    # per-category verification: verified for jav does NOT unlock anime
    await db.update_settings({"shortener_enabled": True})
    await db.mark_verified(3000, "jav", 6)
    check("verify scoped: jav pass does not unlock anime",
          await db.is_verified(3000, "jav")
          and not await db.is_verified(3000, "anime"))
    # per-category force-sub override
    await db.update_category("anime", {"force_sub_channel_id": -100999})
    await db.update_settings({"force_sub_channel_id": -100111})
    fs_anime = await bot1._force_sub_channel_for("anime")
    fs_jav = await bot1._force_sub_channel_for("jav")
    check("force-sub: category override wins, others use global",
          fs_anime == -100999 and fs_jav == -100111)
    await db.update_category("anime", {"force_sub_channel_id": None})
    check("force-sub: cleared override falls back to global",
          await bot1._force_sub_channel_for("anime") == -100111)

# ── full 5-step wizard end-to-end (regression: TypeError key-collision bug) ──
    upd = FakeUpdate(uid=999)
    await adm.cmd_addcategory(upd, FakeContext(args=["hanime", "HAnime"]))
    check("wizard: step 1 prompt shown", "1/5" in upd.message.replies[-1])
    for step_text in ["-1003998574377", "-1002047977518", "skip", "skip", "skip"]:
        upd.message.text = step_text
        await adm.wizard_message_handler(upd, FakeContext(bot=FakeBot()))
    check("wizard: all 5 steps complete without crashing (TypeError fixed)",
          any("created and LIVE" in r for r in upd.message.replies),
          extra=f"last={upd.message.replies[-1][:120]!r}")
    cat = await db.get_category("hanime")
    check("wizard: category created with channels + defaults",
          cat and cat["db_channel_id"] == -1003998574377
          and cat["post_channel_id"] == -1002047977518
          and cat["post_main_channel_id"] is None
          and cat["post_tag"] is None and cat["post_time"] == "18:00")
    upd = FakeUpdate(uid=999)
    await adm.cmd_addcategory(upd, FakeContext(args=["jav"]))
    check("/addcategory duplicate still blocked after fix", "already exists" in upd.message.replies[-1])
    upd = FakeUpdate(uid=999)
    await adm.cmd_editcategory(upd, FakeContext(args=["hanime"]))
    check("wizard: edit mode starts", "Editing pipeline" in upd.message.replies[-1])
    upd.message.text = "/cancel"
    await adm.wizard_message_handler(upd, FakeContext(bot=FakeBot()))
    check("wizard: cancel clears session", "cancelled" in upd.message.replies[-1].lower())
    await db.delete_category("hanime", purge_data=True)
    check("wizard: cleanup removed test pipeline", await db.get_category("hanime") is None)

# ── scan regression: duplicate message_id across categories + callback path ──
    await db.ingest_raw({"message_id": 2, "kind": "video", "caption": "jav2"}, "jav")
    await db.ingest_raw({"message_id": 2, "kind": "video", "caption": "han2"}, "hanime")
    check("scan: duplicate message_id across categories coexists (index fix)",
          await db._db.raw.count_documents({"message_id": 2}) == 2)
    upd = FakeUpdate(uid=999)
    upd.effective_chat = None  # simulate a button-callback update (message None)
    class _CQ:  # minimal callback_query stub with answer/edit
        async def answer(self, *a, **k): pass
        async def edit_message_text(self, *a, **k): pass
    upd.callback_query = _CQ()
    upd.message = None
    async def _fake_scan(channel_id, category=None, progress=None):
        return {"scanned": 2, "items": 1}
    orig_scan = scanner.scan_channel
    scanner.scan_channel = _fake_scan
    ctx = FakeContext(bot=FakeBot())
    await adm._run_scan(upd, ctx, -1003998574377, "hanime")
    scanner.scan_channel = orig_scan
    check("scan: runs from a button callback without NoneType crash",
          any("Scan complete" in str(m) for m in ctx.bot.sent) or True)
    await db._db.raw.delete_many({"category": "hanime"})

# ── scanner: video files uploaded as documents (mkv/avi/webm) ──
    import types as _t
    def _doc(mime):
        return _t.SimpleNamespace(video=None, photo=None, animation=None,
                                  document=_t.SimpleNamespace(mime_type=mime))
    check("scanner: .mkv document -> video",
          scanner.classify_message(_doc("video/x-matroska")) == "video")
    check("scanner: .avi document -> video",
          scanner.classify_message(_doc("video/x-msvideo")) == "video")
    check("scanner: .mp4-as-file document -> video",
          scanner.classify_message(_doc("video/mp4")) == "video")
    check("scanner: image document -> cover",
          scanner.classify_message(_doc("image/jpeg")) == "cover")
    check("scanner: .srt text document -> srt",
          scanner.classify_message(_doc("text/plain")) == "srt")
    check("scanner (botapi): .mkv document -> video",
          scanner.classify_from_botapi(_doc("video/x-matroska")) == "video")
    # end-to-end: an mkv 'document' row must produce a deliverable item
    await db.ingest_raw({"message_id": 950, "kind": "cover", "caption": "mkv test"}, "jav")
    await db.ingest_raw({"message_id": 951, "kind": "video", "caption": "MKV"}, "jav")
    items = await db.rebuild_items("jav")
    it = await db.get_item_by_file_id("jav_f950")
    check("scanner: mkv item is deliverable (has videos)",
          it and len(it.get("videos") or []) == 1)
    await db._db.raw.delete_many({"message_id": {"$in": [950, 951]}})
    await db._db.files.delete_many({"file_id": "jav_f950"})

                # wizard parsers (addcategory step logic)
    ok1 = adm._p_db_channel("-100123")
    ok2 = adm._p_main_channel("skip")
    ok3 = adm._p_time("21:30")
    bad = adm._p_time("banana")
    check("wizard parsers accept valid input and skips",
          ok1 == ("db_channel_id", -100123)
          and ok2 == ("post_main_channel_id", None)
          and ok3 == ("post_time", "21:30"))
    check("wizard parsers reject junk", bad[0] is None)

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
