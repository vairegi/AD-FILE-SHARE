"""Offline regression suite: real PTB serialization, ASGI routes, Mongo mock,
Telethon session parsing and mocked client authorization. Never contacts a
real bot/channel/provider or uses production credentials.
Run: pytest -q test_repairs.py
"""
import asyncio
import html
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from urllib.parse import urlparse, parse_qs
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
import mongomock_motor
from cryptography.fernet import Fernet, InvalidToken
from telethon.sessions import StringSession
from telethon.crypto import AuthKey
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut
import config, db, ui, persona, handlers, scanner, services, userbot_setup as setup, runtime, main, providers, covers


class Msg:
    def __init__(self, text="/start", chat_type="private"):
        self.text = text
        self.chat = NS(type=chat_type, id=7)
        self.replies, self.edits = [], []
        self.deleted = False
    async def reply_text(self, text, **kw):
        self.replies.append((text, kw)); return self
    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw)); return self
    async def delete(self):
        self.deleted = True


class Bot:
    username = "Relax_Time_Test_Bot"
    def __init__(self, member="member", error=None):
        self.member = member; self.error = error; self.calls=[]; self.sent=[]; self.deleted=[]
    async def get_me(self):
        return NS(username=self.username)
    async def get_chat_member(self, cid, uid):
        return NS(status=self.member, is_member=False)
    async def get_chat(self, cid):
        return NS(username="example_channel", invite_link=None)
    async def send_message(self, uid=None, text=None, **kw):
        uid = uid if uid is not None else kw.pop("chat_id")
        self.sent.append((uid, text, kw)); return NS(message_id=500)
    async def copy_message(self, **kw):
        self.calls.append(("single", kw))
        if self.error: raise self.error
        return NS(message_id=1000 + kw["message_id"])
    async def copy_messages(self, **kw):
        self.calls.append(("batch", kw))
        if self.error: raise self.error
        return [NS(message_id=1000+m) for m in kw["message_ids"]]
    async def delete_message(self, **kw):
        self.deleted.append(kw["message_id"])


class Q:
    def __init__(self, data="home", uid=7, bot=None):
        self.data=data; self.from_user=NS(id=uid, first_name="Alice & Bob", username="alice")
        self.message=Msg(); self.bot=bot or Bot(); self.answers=[]
    def get_bot(self): return self.bot
    async def answer(self, text=None, **kw): self.answers.append((text,kw))
    async def edit_message_text(self, text, **kw):
        return await self.message.edit_message_text(text, **kw)


def update(uid=7, text="/start"):
    return NS(effective_user=NS(id=uid,first_name="Alice <A>", username="alice"), message=Msg(text))


def context(bot=None, args=None):
    return NS(bot=bot or Bot(), args=args or [])


def collection(cid="c-100_10", mids=(11,12), timestamp=1):
    return {"collection_id":cid,"channel_id":-100,"category":"movies", "title":"Sample & title <safe>",
        "cover":{"message_id":10,"album_message_ids":[10],"url_status":"pending"},
        "files":[{"message_id":m,"media_type":"video","caption":None} for m in mids],
        "file_count":len(mids),"price":None,"status":"active","posted_at":timestamp,"views":0,"unlocks":0}


@pytest_asyncio.fixture(autouse=True)
async def database(monkeypatch):
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(), "repair_tests")
    await db.col("users").create_index("user_id", unique=True)
    await db.col("unlocks").create_index([("user_id",1),("collection_id",1)], unique=True)
    await db.col("referrals").create_index("friend_id", unique=True)
    await db.col("bookmarks").create_index([("user_id",1),("collection_id",1)], unique=True)
    monkeypatch.setattr(config,"OWNER_ID",7)
    monkeypatch.setattr(config,"ADMIN_IDS",[7])
    monkeypatch.setattr(config,"REQUIRED_CHANNEL_IDS",[])
    monkeypatch.setattr(config,"BOT_USERNAME","")
    monkeypatch.setattr(config,"RENDER_EXTERNAL_URL","https://example.onrender.com")
    monkeypatch.setattr(config,"SESSION_ENCRYPTION_KEY",Fernet.generate_key().decode())
    runtime.userbot=None
    yield
    await runtime.stop_jobs()


@pytest.mark.parametrize("data,color", [("feed:all:0","danger"),("unlock:c1","danger"),
    ("cats","success"),("earn","primary"),("profile","primary"),("help","primary"),
    ("restream:c1","success"),("task:t1","success"),("home","primary"),("save:c1","primary")])
def test_actual_button_style_field(data,color):
    encoded=ui.button("Tap",data).to_dict()
    assert encoded["style"]==color
    assert encoded["callback_data"]==data


def test_home_keyboard_exact_color_rows():
    rows=handlers._home_kb().to_dict()["inline_keyboard"]
    assert [len(r) for r in rows]==[1,2,2]
    assert [[b["style"] for b in r] for r in rows]==[["danger"],["success","primary"],["primary","primary"]]


@pytest.mark.parametrize("bad",["x"*65,"🎀"*17,""])
def test_callback_length_rejected(bad):
    with pytest.raises(ValueError): ui.button("test",bad)


def test_html_home_exact_emphasis_and_escaping(monkeypatch):
    monkeypatch.setattr(config,"BOT_NAME","Cute <Hub> & Friends")
    text=persona.home(5,1,6,3)
    assert "<b>🎀 Uwahh~! Welcome to Cute &lt;Hub&gt; &amp; Friends" in text
    assert "<b>Token Balance:</b> <code>5 Tokens</code>" in text
    assert "<b>Daily Streak:</b> <code>1 Days</code>" in text
    assert "<i>🌸 E-Ehehe" in text and text.endswith("</i>")
    assert "<b><code>" not in text


@pytest.mark.parametrize("name,args",[
    ("categories",(5,)),("feed_empty",()),("feed_caption",("<sample>",2,4,1,2,5,False)),
    ("insufficient",(2,0)),("stream_complete",("<sample>",2,2)),
    ("delivery_failed_refund",(2,5)),("help_card",()),("profile",(7,5,1,2,3,5,2,1,0)),
    ("earn_home",(5,)),("task_unavailable",()),("task_cooldown",(2,3)),
    ("checkin_done",(3,1,9,5,5)),("checkin_already",(7,8)),
    ("referral",("https://t.me/testbot?start=ref_7",2,10)),
    ("referral_notify",("<friend>",5,10)),("join_required",([-100],))])
def test_all_persona_cards_have_valid_balanced_tags(name,args):
    from html.parser import HTMLParser
    class CheckHTML(HTMLParser):
        def __init__(self): super().__init__(); self.stack=[]
        def handle_starttag(self, tag, attrs):
            assert tag in ("b","i","code","a","blockquote")
            assert not (tag=="code" and any(x in ("b","i","a") for x in self.stack))
            self.stack.append(tag)
        def handle_endtag(self, tag): assert self.stack.pop()==tag
    text=getattr(persona,name)(*args)
    parser=CheckHTML();parser.feed(text); assert not parser.stack
    assert text.startswith("<b>")
    assert "<sample>" not in text and "<friend>" not in text


@pytest.mark.asyncio
async def test_start_shows_post_credit_balance_and_html():
    u=update(); await handlers.cmd_start(u,context())
    text,kw=u.message.replies[-1]
    assert "<code>5 Tokens</code>" in text and kw["parse_mode"]=="HTML"
    await handlers.cmd_start(u,context())
    assert (await db.get_user(7))["tokens"]==5


@pytest.mark.asyncio
async def test_concurrent_welcome_and_checkins_no_duplicate():
    await asyncio.gather(*(handlers.cmd_start(update(),context()) for _ in range(10)))
    assert (await db.get_user(7))["tokens"]==5
    replies=[]
    async def reply(text, **kw): replies.append((text,kw))
    await asyncio.gather(*(handlers.do_checkin(7,reply) for _ in range(10)))
    assert (await db.get_user(7))["tokens"]==8
    assert sum("Complete" in t for t,k in replies)==1
    assert all(k["parse_mode"]=="HTML" for t,k in replies)


@pytest.mark.asyncio
@pytest.mark.parametrize("previous,expected",[(0,1),(4,5),(9,10)])
async def test_streak_bonus_days(monkeypatch,previous,expected):
    await db.create_user(7)
    if previous:
        await db.record_checkin(7,db.ist_today(1),previous,3)
    u=update();await handlers.cmd_checkin(u,context())
    assert (await db.get_last_checkin(7))["streak"]==expected
    assert (await db.get_user(7))["tokens"]==(4 if previous else 3)
    await handlers._show_home(Q())


@pytest.mark.asyncio
async def test_missed_day_streak_resets():
    await db.create_user(7);await db.record_checkin(7,db.ist_today(2),4,3)
    await handlers.cmd_checkin(update(),context())
    assert (await db.get_last_checkin(7))["streak"]==1


@pytest.mark.asyncio
async def test_referral_username_auto_and_share_url(monkeypatch):
    monkeypatch.setattr(config,"BOT_USERNAME","")
    q=Q("ref");await handlers._show_referral(q)
    text,kw=q.message.edits[-1]
    assert "https://t.me/Relax_Time_Test_Bot?start=ref_7" in text
    assert "https://t.me/?" not in text
    share=kw["reply_markup"].to_dict()["inline_keyboard"][0][0]
    assert share["text"]=="📤 Share on Telegram" and share["style"]=="success"
    params=parse_qs(urlparse(share["url"]).query)
    assert params["url"]==["https://t.me/Relax_Time_Test_Bot?start=ref_7"]
    assert params["text"]==["Stream exclusive media now!"]


@pytest.mark.asyncio
async def test_username_fetch_fallback_and_missing(monkeypatch):
    class NoCache:
        async def get_me(self): return NS(username="Fetched_Test_Bot")
    assert await ui.bot_username(NoCache())=="Fetched_Test_Bot"
    monkeypatch.setattr(config,"BOT_USERNAME","@Fallback_Test_Bot")
    assert await ui.bot_username(None)=="Fallback_Test_Bot"
    monkeypatch.setattr(config,"BOT_USERNAME","")
    with pytest.raises(RuntimeError): await ui.bot_username(None)


@pytest.mark.asyncio
async def test_referral_qualified_after_membership_and_notification():
    await db.create_user(8)
    await db.update_settings({"required_channel_ids":[-100]})
    u=update();b=Bot(member="left")
    await handlers.cmd_start(u,context(b,["ref_8"]))
    assert (await db.get_user(7))["tokens"]==8
    assert (await db.get_user(8))["tokens"]==0
    b.member="member"
    q=Q("checkjoin",bot=b)
    await handlers.on_button(NS(callback_query=q),context(b))
    assert (await db.get_user(8))["tokens"]==5
    assert any(uid==8 and "Yatta" in text for uid,text,kw in b.sent)
    await handlers.on_button(NS(callback_query=q),context(b))
    assert (await db.get_user(8))["tokens"]==5


@pytest.mark.asyncio
async def test_broken_edit_does_not_spawn_on_not_modified():
    q=Q();q.edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified"))
    await ui.edit_card(q,persona.home(5,0,0,0),handlers._home_kb())
    assert not q.message.replies
    q.edit_message_text=AsyncMock(side_effect=BadRequest("Message to edit not found"))
    await ui.edit_card(q,persona.home(5,0,0,0),handlers._home_kb())
    assert len(q.message.replies)==1


@pytest.mark.asyncio
async def test_edit_network_error_is_not_hidden_by_new_message():
    q=Q();q.edit_message_text=AsyncMock(side_effect=TimedOut())
    with pytest.raises(TimedOut): await ui.edit_card(q,"text",None)
    assert not q.message.replies


@pytest.mark.asyncio
@pytest.mark.parametrize("data",["home","cats","earn","profile","help","ref","feed:all:0","library:unlocks:0","library:bookmarks:0","checkin","noop"])
async def test_each_navigation_screen_uses_html_and_colors(data):
    await handlers.cmd_start(update(),context())
    q=Q(data);await handlers.on_button(NS(callback_query=q),context(q.bot))
    if data=="noop": return
    assert q.message.edits, f"{data} didn't edit the existing message"
    text,kw=q.message.edits[-1]
    assert kw["parse_mode"]=="HTML"
    assert "<b>" in text
    if kw["reply_markup"]:
        for row in kw["reply_markup"].to_dict()["inline_keyboard"]:
            assert all(b["style"] in ("primary","success","danger") for b in row)


@pytest.mark.asyncio
async def test_feed_balance_and_real_vault_bookmarks():
    await handlers.cmd_start(update(),context())
    a=collection("a",timestamp=1);b=collection("b",timestamp=2)
    await db.upsert_collection(a);await db.upsert_collection(b)
    await db.insert_unlock(7,"a");await db.toggle_bookmark(7,"b")
    qa=Q();await handlers._show_library(qa,"unlocks",0)
    assert "Already in your Vault" in (qa.bot.sent[-1][1], qa.bot.sent[-1][2])[0]
    assert "5 Tokens" in (qa.bot.sent[-1][1], qa.bot.sent[-1][2])[0]
    assert "restream:a" in (qa.bot.sent[-1][1], qa.bot.sent[-1][2])[1]["reply_markup"].to_json()
    qb=Q();await handlers._show_library(qb,"bookmarks",0)
    assert "unlock:b" in (qb.bot.sent[-1][1], qb.bot.sent[-1][2])[1]["reply_markup"].to_json()


@pytest.mark.asyncio
async def test_unlock_charge_free_replay_and_join_gate():
    await handlers.cmd_start(update(),context());await db.upsert_collection(collection())
    q=Q("unlock:c-100_10");await handlers.on_button(NS(callback_query=q),context(q.bot))
    assert (await db.get_user(7))["tokens"]==3
    assert await db.is_unlocked(7,"c-100_10")
    assert "Delivered <code>2</code>" in q.message.edits[-1][0]
    await db.update_settings({"required_channel_ids":[-100]})
    q.bot.member="left";q.data="restream:c-100_10";before=len(q.bot.calls)
    await handlers.on_button(NS(callback_query=q),context(q.bot))
    assert len(q.bot.calls)==before
    assert "Join Channel" in q.message.edits[-1][1]["reply_markup"].to_json()
    assert await db.is_unlocked(7,"c-100_10")


@pytest.mark.asyncio
@pytest.mark.parametrize("mids",[(),(11,12)])
async def test_no_charge_empty_or_insufficient(mids):
    await db.create_user(7);await db.upsert_collection(collection(mids=mids))
    q=Q("unlock:c-100_10");await handlers.on_button(NS(callback_query=q),context(q.bot))
    assert (await db.get_user(7))["tokens"]==0
    assert not await db.is_unlocked(7,"c-100_10")
    assert not q.bot.calls


@pytest.mark.asyncio
async def test_explicit_failure_refunds_but_uncertain_timeout_does_not():
    await handlers.cmd_start(update(),context());await db.upsert_collection(collection())
    q=Q("unlock:c-100_10",bot=Bot(error=BadRequest("message not found")))
    await handlers.on_button(NS(callback_query=q),context(q.bot))
    assert (await db.get_user(7))["tokens"]==5
    assert not await db.is_unlocked(7,"c-100_10")
    q=Q("unlock:c-100_10",bot=Bot(error=TimedOut()))
    await handlers.on_button(NS(callback_query=q),context(q.bot))
    assert (await db.get_user(7))["tokens"]==3
    assert len(q.bot.calls)==1  # never blind-resend after timeout
    assert "Delivery incomplete" in q.message.edits[-1][0]


@pytest.mark.asyncio
async def test_deletion_retries_transient_errors():
    await db.create_delivery({"delivery_id":"d","user_id":7,"message_ids":[1,2],
        "auto_delete_at":0,"delete_status":"pending"})
    b=Bot()
    async def delete(**kw):
        if kw["message_id"]==2: raise TimedOut()
    b.delete_message=delete
    await services.run_deletion_sweeper(b)
    doc=await db.get_delivery("d")
    assert doc["message_ids"]==[2] and doc["delete_status"]=="pending"
    b.delete_message=AsyncMock()
    await services.run_deletion_sweeper(b)
    assert (await db.get_delivery("d"))["delete_status"]=="done"


@pytest.mark.asyncio
@pytest.mark.parametrize("status",["member","administrator","creator","restricted","left","kicked"])
async def test_membership_status_handling(status):
    await db.update_settings({"required_channel_ids":[-100]})
    assert await handlers._check_membership(Bot(member=status),7)==(status in ("member","administrator","creator"))


@pytest.mark.parametrize("value",["", "password", "0"*32,"not_a_fernet_key"])
def test_invalid_key_rejected_before_requesting_session(monkeypatch,value):
    monkeypatch.setattr(config,"SESSION_ENCRYPTION_KEY",value)
    assert "Fernet" in setup.configuration_error()


def session_string():
    s=StringSession();s.set_dc(2,"149.154.167.51",443);s.auth_key=AuthKey(b"x"*256)
    return s.save()


def test_real_session_parser_and_encryption_roundtrip():
    assert setup.configuration_error() is None
    credentials=setup.validate_credentials("12345","a"*32,session_string())
    blob=setup.encrypt_session(json.dumps(credentials))
    assert credentials==json.loads(setup.decrypt_session(blob))
    assert session_string() not in blob


@pytest.mark.parametrize("api_id,api_hash,session",[("no","a"*32,"bad"),(0,"a"*32,"bad"),(1,"hash","bad"),(1,"a"*32,"bad")])
def test_invalid_api_or_session_rejected(api_id,api_hash,session):
    with pytest.raises(ValueError): setup.validate_credentials(api_id,api_hash,session)


class FakeTelethon:
    instances=[]
    authorized=True
    def __init__(self,*args): self.args=args;self.disconnected=False;self.instances.append(self)
    async def connect(self): pass
    async def is_user_authorized(self): return self.authorized
    async def disconnect(self): self.disconnected=True


@pytest.mark.asyncio
async def test_secure_import_stores_only_encrypted_credentials_once():
    token=await setup.issue_setup_token()
    raw=await db.col("setup_tokens").find_one({})
    assert token not in json.dumps(raw)
    client=await setup.import_credentials(token,12345,"a"*32,session_string(),FakeTelethon)
    saved=await db.get_settings()
    assert "userbot_credentials" in saved
    assert session_string() not in json.dumps(saved)
    assert (await setup.load_credentials())["api_id"]==12345
    assert await setup.validate_setup_token(token) is None
    with pytest.raises(ValueError): await setup.import_credentials(token,12345,"a"*32,session_string(),FakeTelethon)
    await client.disconnect()


@pytest.mark.asyncio
async def test_expired_setup_link_and_second_link_invalidates_first():
    first=await setup.issue_setup_token();second=await setup.issue_setup_token()
    assert await setup.validate_setup_token(first) is None
    assert await setup.validate_setup_token(second)
    await db.col("setup_tokens").update_many({}, {"$set":{"expires_at":0}})
    assert await setup.validate_setup_token(second) is None


@pytest.mark.asyncio
async def test_unauthorized_session_does_not_overwrite_or_consume(monkeypatch):
    token=await setup.issue_setup_token()
    monkeypatch.setattr(FakeTelethon,"authorized",False)
    with pytest.raises(ValueError): await setup.import_credentials(token,1,"b"*32,session_string(),FakeTelethon)
    assert not (await db.get_settings()).get("userbot_credentials")
    assert await setup.validate_setup_token(token)
    assert FakeTelethon.instances[-1].disconnected


@pytest.mark.asyncio
async def test_setup_command_ignores_old_key_and_is_owner_only(monkeypatch):
    monkeypatch.setattr(config,"SESSION_ENCRYPTION_KEY","bad")
    u=update();await handlers.cmd_setup_userbot(u,context())
    assert "Step 1/3" in u.message.replies[-1][0]
    assert "Fernet.generate_key" not in u.message.replies[-1][0]
    other=update(9);await handlers.cmd_setup_userbot(other,context())
    assert not other.message.replies


@pytest.mark.asyncio
async def test_setup_command_no_webpage_or_arbitrary_session_capture():
    import chat_setup
    u=update();await handlers.cmd_setup_userbot(u,context())
    assert "reply_markup" not in u.message.replies[-1][1]
    assert "API ID" in u.message.replies[-1][0]
    await handlers.cmd_cancel(u,context())
    await db.update_settings({"userbot_setup_waiting":7})
    u=update(text="a random message or session")
    await handlers.on_private_text(u,context())
    assert not (await db.get_settings()).get("userbot_session")
    assert not u.message.deleted


@pytest.mark.asyncio
async def test_channel_admin_validation_mapping_and_instructions():
    u=update();await handlers.cmd_addchannel(u,context(args=["-100123","movies"]))
    assert await db.category_for_channel(-100123)=="movies"
    assert "/scan -100123" in u.message.replies[-1][0]
    bad=update();await handlers.cmd_addchannel(bad,context(args=["not-a-number","movies"]))
    assert "Usage" in bad.message.replies[-1][0]
    other=update(9);await handlers.cmd_addchannel(other,context(args=["-100222","bad"]))
    assert await db.category_for_channel(-100222) is None
    await handlers.cmd_channels(u,context());assert "movies" in u.message.replies[-1][0]
    await handlers.cmd_removechannel(u,context(args=["-100123"]))
    assert await db.category_for_channel(-100123) is None


def source_message(mid,caption=None,kind="video",gid=None,duration=9):
    return NS(id=mid,message=caption,photo=object() if kind=="photo" else None,
        video=NS(attributes=[NS(duration=duration)]) if kind=="video" else None,
        document=None,grouped_id=gid,date=datetime.fromtimestamp(mid,timezone.utc))


class Source:
    def __init__(self,posts): self.posts=posts;self.kwargs=[]
    async def iter_messages(self,cid,**kw):
        self.kwargs.append(kw)
        for p in self.posts:
            if p.id>kw.get("min_id",0): yield p


@pytest.mark.asyncio
async def test_scanner_initial_incremental_full_no_orphans_or_duplicate_files():
    source=Source([source_message(1,"Cover\nQuality\nAudio",kind="photo"),source_message(2)])
    result=await scanner.scan_channel(source,-100,"movies")
    assert result["collections"]==1
    cid="c-100_1"
    assert (await db.get_collection(cid))["file_count"]==1
    await db.insert_unlock(7,cid)
    await db.col("collections").update_one({"collection_id":cid},{"$set":{"price":4,"views":10}})
    source.posts.append(source_message(3))
    await scanner.scan_channel(source,-100,"movies")
    d=await db.get_collection(cid)
    assert d["file_count"]==2 and [f["message_id"] for f in d["files"]]==[2,3]
    assert d["price"]==4 and d["views"]==10 and await db.is_unlocked(7,cid)
    await scanner.scan_channel(source,-100,"movies",full=True)
    assert (await db.get_collection(cid))["file_count"]==2
    assert await db.col("pending_files").count_documents({})==0


@pytest.mark.asyncio
async def test_scanner_album_and_short_video_rules_and_pending():
    source=Source([source_message(1),source_message(2,"Preview\n720p\nAudio",gid=42,duration=20),
        source_message(3,gid=42),source_message(4,duration=9)])
    result=await scanner.scan_channel(source,-100,"movies")
    doc=await db.get_collection("c-100_2")
    assert doc["cover"]["album_message_ids"]==[2,3]
    assert doc["file_count"]==1 and doc["files"][0]["message_id"]==4
    assert result["pending_files"]==1
    u=update();await handlers.cmd_pending(u,context())
    assert "message 1" in u.message.replies[-1][0]


@pytest.mark.asyncio
async def test_scan_command_missing_userbot_and_unknown_channel():
    u=update();await handlers.cmd_scan(u,context(args=["-100"]))
    assert "Map it first" in u.message.replies[-1][0]
    await db.upsert_channel_mapping(-100,"movies")
    await handlers.cmd_scan(u,context(args=["-100"]))
    assert "/setup_userbot" in u.message.replies[-1][0]


@pytest.mark.asyncio
async def test_runtime_manual_background_scan():
    await db.upsert_channel_mapping(-100,"movies")
    runtime.userbot=Source([source_message(1,"a\nb\nc",kind="photo"),source_message(2)])
    u=update();await handlers.cmd_scan(u,context(args=["-100"]))
    assert "background" in u.message.replies[-1][0]
    await asyncio.gather(*list(runtime._tasks))
    assert await db.get_collection("c-100_1")
    runtime.userbot=None


@pytest.mark.asyncio
async def test_application_health_setup_and_webhook_guards(monkeypatch):
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url="https://example.onrender.com") as c:
        for path in ("/","/health"):
            assert (await c.get(path)).status_code==200
            assert (await c.head(path)).status_code==200
        page=await c.get("/setup/userbot")
        assert page.status_code==200 and "no-store" in page.headers["cache-control"]
        assert "location.hash" in page.text and "history.replaceState" in page.text
        assert (await c.post("/webhook",json={})).status_code==403
        assert (await c.post("/setup/userbot",json={"token":"bad"})).status_code==403
        assert (await c.post("/setup/userbot",json={},headers={"Origin":"https://evil.invalid"})).status_code==403


@pytest.mark.asyncio
async def test_setup_http_validates_and_connects(monkeypatch):
    import httpx
    token=await setup.issue_setup_token()
    async def connect(credentials):
        return await setup.connect_credentials(credentials,FakeTelethon)
    original=setup.connect_credentials
    async def fake_connect(credentials,client_factory=None):
        return await original(credentials,FakeTelethon)
    monkeypatch.setattr(setup,"connect_credentials",fake_connect)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url=config.RENDER_EXTERNAL_URL) as c:
        result=await c.post("/setup/userbot",json={"token":token,"api_id":1,"api_hash":"a"*32,"session":session_string()})
        assert result.status_code==200,result.text
        assert runtime.userbot
        replay=await c.post("/setup/userbot",json={"token":token})
        assert replay.status_code==403


def test_all_registered_commands_private_and_no_token_at_import(monkeypatch):
    monkeypatch.setattr(config,"BOT_TOKEN","123456:FAKE_TEST_TOKEN_NOT_REAL")
    application=main.build_bot()
    commands=set()
    for handler in application.handlers[0]:
        if hasattr(handler,"commands"):
            commands.update(handler.commands)
    assert {"setup_userbot","userbot_status","scan","addchannel","collections","start","checkin","botstatus"}<=commands
    assert main.app_bot is None


@pytest.mark.asyncio
async def test_status_and_admin_commands_without_exposing_credentials():
    await db.update_settings({"userbot_credentials":"DO_NOT_SHOW"})
    for fn in (handlers.cmd_admin,handlers.cmd_userbot_status,handlers.cmd_channels,handlers.cmd_stats,handlers.cmd_pending,handlers.cmd_collections):
        u=update();await fn(u,context())
        assert u.message.replies
        assert "DO_NOT_SHOW" not in u.message.replies[-1][0]
    u=update();await handlers.cmd_botstatus(u,context())
    assert "Relax_Time_Test_Bot" in u.message.replies[-1][0]
    await handlers.cmd_remove_userbot(u,context())
    assert not (await db.get_settings()).get("userbot_credentials")


@pytest.mark.asyncio
async def test_starting_background_jobs_and_shutdown(monkeypatch):
    called=[]
    async def sweep(bot): called.append(True)
    monkeypatch.setattr(services,"run_deletion_sweeper",sweep)
    runtime.start_jobs(Bot())
    await asyncio.sleep(0.01)
    assert called and len(runtime._background)==2
    await runtime.stop_jobs()
    assert not runtime._tasks and not runtime._background


@pytest.mark.asyncio
async def test_real_ptb_request_serialization_without_network():
    from telegram import Bot as TelegramBot
    from telegram.request import BaseRequest
    class Recording(BaseRequest):
        def __init__(self): self.recorded=[]
        @property
        def read_timeout(self): return 5
        async def initialize(self): pass
        async def shutdown(self): pass
        async def do_request(self,url,method,request_data=None,**kwargs):
            params=request_data.parameters if request_data else {}
            self.recorded.append((url.rsplit("/",1)[-1],params))
            if url.endswith("getMe"):
                result={"id":123456,"is_bot":True,"first_name":"Test","username":"Relax_Time_Test_Bot"}
            else:
                result={"message_id":1,"date":0,"chat":{"id":7,"type":"private"},"text":"test"}
            return 200,json.dumps({"ok":True,"result":result}).encode()
    request=Recording()
    async with TelegramBot("123456:FAKE_TEST_TOKEN_NOT_REAL",request=request) as b:
        await b.send_message(7,persona.home(5,0,0,0),parse_mode="HTML",reply_markup=handlers._home_kb())
    method,params=request.recorded[-1]
    assert method=="sendMessage" and params["parse_mode"]=="HTML"
    keyboard=params["reply_markup"]
    if isinstance(keyboard,str): keyboard=json.loads(keyboard)
    assert keyboard["inline_keyboard"][0][0]["style"]=="danger"
    assert keyboard["inline_keyboard"][1][0]["style"]=="success"
    assert "<code>5 Tokens</code>" in params["text"]


@pytest.mark.asyncio
async def test_admin_required_channels_and_price_validation():
    u=update();await handlers.cmd_reqchannel(u,context(args=["-1001","https://t.me/+testInvite"]))
    await handlers.cmd_reqchannel(u,context(args=["-1002"]))
    assert await db.required_channels()==[-1001,-1002]
    assert (await db.get_settings())["required_channel_links"]["-1001"]=="https://t.me/+testInvite"
    await handlers.cmd_reqchannel(u,context(args=["off"]))
    assert await db.required_channels()==[]
    await db.upsert_collection(collection("a"))
    await handlers.cmd_setprice(u,context(args=["a","0"]))
    assert (await db.get_collection("a"))["price"]==0
    await handlers.cmd_setprice(u,context(args=["a","-3"]))
    assert (await db.get_collection("a"))["price"]==0
    await handlers.cmd_setprice(u,context(args=["missing","2"]))
    assert "not found" in u.message.replies[-1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fn",["cmd_addchannel","cmd_removechannel","cmd_reqchannel","cmd_addtask","cmd_setprice","cmd_scan"])
async def test_each_admin_missing_args_does_not_crash(fn):
    u=update();await getattr(handlers,fn)(u,context())
    assert u.message.replies and "Usage" in u.message.replies[-1][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fn",["cmd_admin","cmd_channels","cmd_reqchannel","cmd_addtask","cmd_setprice","cmd_scan","cmd_pending","cmd_collections","cmd_stats","cmd_botstatus","cmd_userbot_status","cmd_remove_userbot"])
async def test_nonadmin_denied_each_command(fn):
    u=update(99);await getattr(handlers,fn)(u,context(args=["-100","movies"]))
    assert not u.message.replies


@pytest.mark.asyncio
async def test_task_signatures_callbacks_and_cooldown(monkeypatch):
    import time
    u=update();await handlers.cmd_addtask(u,context(args=["t1","Sponsor","https://example.com","5"]))
    settings=await db.get_settings();settings["tasks"][0]["secret"]="FAKE_TEST_SECRET"
    await db.update_settings({"tasks":settings["tasks"]})
    await db.create_user(7)
    ts=int(time.time());sig=providers.make_signature("FAKE_TEST_SECRET",7,"t1",ts)
    assert await providers.verify_callback(7,"t1",ts,"bad")=="bad_signature"
    assert await providers.verify_callback(7,"missing",ts,sig)=="unknown_task"
    assert await providers.verify_callback(7,"t1",ts-3600,sig)=="expired"
    assert await providers.verify_callback(7,"t1",ts,sig)=="ok"
    assert await providers.verify_callback(7,"t1",ts,sig)=="cooldown"
    assert (await db.get_user(7))["tokens"]==5
    q=Q("task:t1");await handlers.on_button(NS(callback_query=q),context(q.bot))
    assert any("Task Cooling" in text for text,kw in q.message.edits)


@pytest.mark.asyncio
async def test_http_signed_task_and_webhook_dispatch(monkeypatch):
    import httpx,time
    monkeypatch.setattr(config,"WEBHOOK_SECRET","test_webhook_secret")
    fake=NS(bot=Bot(),update_queue=asyncio.Queue())
    monkeypatch.setattr(main,"app_bot",fake)
    await db.create_user(7)
    await db.update_settings({"tasks":[{"task_id":"t","secret":"FAKE_TEST_SECRET","reward":5}]})
    ts=int(time.time());sig=providers.make_signature("FAKE_TEST_SECRET",7,"t",ts)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url=config.RENDER_EXTERNAL_URL) as c:
        response=await c.post("/webhook",headers={"X-Telegram-Bot-Api-Secret-Token":"test_webhook_secret"},json={"update_id":1})
        assert response.status_code==200 and fake.update_queue.qsize()==1
        assert (await c.post("/task/complete")).status_code==400
        result=await c.post("/task/complete",params={"user_id":7,"task_id":"t","ts":ts,"sig":sig})
        assert result.json()["status"]=="ok"
        assert fake.bot.sent


@pytest.mark.asyncio
async def test_application_lifespan_jobs_identity_and_shutdown(monkeypatch):
    monkeypatch.setattr(config,"WEBHOOK_SECRET","test_webhook_secret")
    class Application:
        def __init__(self):
            self.bot=Bot();self.bot.set_webhook=AsyncMock();self.bot.set_my_commands=AsyncMock()
            self.initialize=AsyncMock();self.start=AsyncMock();self.stop=AsyncMock();self.shutdown=AsyncMock()
    app=Application()
    monkeypatch.setattr(main,"build_bot",lambda:app)
    monkeypatch.setattr(setup,"build_client",AsyncMock(return_value=None))
    async with main.lifespan(main.app):
        assert config.BOT_USERNAME=="Relax_Time_Test_Bot"
        assert runtime._background
        assert app.bot.set_webhook.called
    assert app.stop.called and app.shutdown.called


@pytest.mark.asyncio
async def test_cover_host_primary_fallback_and_preparation():
    import covers
    class MirrorHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, files=None, timeout=None, **kw):
            if "telegra.ph" in url: raise RuntimeError("down")
            return NS(status_code=200, json=lambda: [{"src": "/file/g.jpg"}])
        async def head(self, url, **kw):
            return NS(status_code=200, headers={"content-type": "image/jpeg"})
    async with MirrorHTTP() as http:
        r = await covers.host_cover(http, b"img")
    assert r["status"] == "ok" and r["host"] == "graph.org"
    assert r["url"] == "https://graph.org/file/g.jpg"
    class AllDown:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw): raise RuntimeError("down")
    async with AllDown() as http:
        r = await covers.host_cover(http, b"img")
    assert r["status"] == "failed" and r["error"]


@pytest.mark.asyncio
async def test_saved_toggle_and_stale_callbacks_and_deeplink():
    await handlers.cmd_start(update(),context())
    await db.upsert_collection(collection("a"))
    for expected in (1,0):
        q=Q("save:a");await handlers.on_button(NS(callback_query=q),context(q.bot))
        assert await db.count_bookmarks(7)==expected
    q=Q("unlock:missing");await handlers.on_button(NS(callback_query=q),context(q.bot))
    assert any("isn't available" in (text or "") for text,kw in q.answers)
    u=update();ctx=context(args=["open_a"]);await handlers.cmd_start(u,ctx)
    assert u.message.replies or ctx.bot.sent, "deep-link open must render a card"
    if u.message.replies:
        assert u.message.replies[-1][1].get("parse_mode")=="HTML"


@pytest.mark.asyncio
async def test_userbot_legacy_load_and_remove(monkeypatch):
    import os
    await setup.save_session(session_string())
    monkeypatch.setenv("API_ID","1234");monkeypatch.setenv("API_HASH","a"*32)
    assert await setup.load_session()==session_string()
    await setup.remove_session()
    assert await setup.load_session() is None


@pytest.mark.asyncio
async def test_scanner_document_text_only_and_empty_source():
    doc=source_message(1,kind="document");doc.document=object()
    assert scanner._post_from_message(doc)["media_type"]=="document"
    text=source_message(2,kind="text");assert scanner._post_from_message(text) is None
    assert (await scanner.scan_channel(Source([]),-100,"movies"))["collections"]==0


@pytest.mark.asyncio
async def test_cleanup_terminal_error_and_copy_count_mismatch():
    await db.create_delivery({"delivery_id":"d","user_id":7,"message_ids":[1],"auto_delete_at":0,"delete_status":"pending"})
    b=Bot();b.delete_message=AsyncMock(side_effect=BadRequest("message to delete not found"))
    await services.run_deletion_sweeper(b)
    assert (await db.get_delivery("d"))["delete_status"]=="done"
    b=Bot();b.copy_messages=AsyncMock(return_value=[NS(message_id=1001)])
    sent,failed=await services.deliver_collection(b,7,collection(),collection()["files"])
    assert len(sent)==1 and failed[0]["uncertain"]
