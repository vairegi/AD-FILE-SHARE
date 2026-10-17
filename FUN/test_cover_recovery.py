"""Regression for the actual incident: 162 pending rows, zero failed rows,
no URLs; old retry matched none and real PTB callbacks had no q.bot.
No production database credentials, documents, sessions or media in tests.
"""
import asyncio
import io
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
import mongomock_motor
from PIL import Image
from bson import Binary
from telegram import Bot as TelegramBot, Update, InlineKeyboardMarkup
from telegram.request import BaseRequest
from telegram.error import BadRequest, TimedOut
import config, db, covers, cover_jobs, scanner, handlers, rich_cards, runtime, ui
from test_repairs import update, context, collection, source_message, Q
from test_covers_feed import FakeHTTP, ScanClient


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(),"recovery_tests")
    monkeypatch.setattr(config,"OWNER_ID",7);monkeypatch.setattr(config,"ADMIN_IDS",[7])
    runtime.userbot=None;cover_jobs._task=None
    yield
    await runtime.stop_jobs()
    cover_jobs._task=None


def jpeg():
    out=io.BytesIO();Image.new("RGB",(10,10),(130,150,170)).save(out,"JPEG");return out.getvalue()


@pytest.mark.asyncio
async def test_exact_162_pending_production_state_backfills_in_background(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx,"AsyncClient",FakeHTTP)
    for i in range(162):
        await db.upsert_collection({"collection_id":f"c{i}","channel_id":-100,
            "cover":{"message_id":i+1,"url_status":"pending","url":None},"status":"active"})
    await db.create_user(7);await db.add_tokens(7,11,"test")
    await db.insert_unlock(7,"c0")
    assert await db.col("collections").count_documents({"cover.url_status":"failed"})==0
    async def prepare(userbot,doc,http,force=False):
        assert force
        await db.col("collections").update_one({"collection_id":doc["collection_id"]},
            {"$set":{"cover.url_status":"ok","cover.url":"https://telegra.ph/file/test.jpg"}})
        return {"hosted":True,"ready":True}
    monkeypatch.setattr(covers,"prepare_collection",prepare)
    runtime.userbot=NS(disconnect=AsyncMock())
    u=update();await handlers.cmd_retry_covers(u,context())
    assert "162 covers" in u.message.replies[0][0]
    assert "background" in u.message.replies[0][0]
    assert cover_jobs.busy()
    await cover_jobs._task
    assert (await db.col("cover_jobs").find_one({"_id":"current"}))["processed"]==162
    assert await db.col("collections").count_documents({"cover.url_status":"ok"})==162
    assert (await db.get_user(7))["tokens"]==11
    assert await db.is_unlocked(7,"c0")


@pytest.mark.asyncio
async def test_retry_selects_pending_missing_failed_and_bad_ok_but_skips_cached(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx,"AsyncClient",FakeHTTP)
    docs=[("pending",{"url_status":"pending"}), ("missing",{}),
          ("failed",{"url_status":"failed"}), ("bad_ok",{"url_status":"ok","url":None}),
          ("good",{"url_status":"ok","url":"https://telegra.ph/file/good.jpg"})]
    for name,cover in docs:
        await db.upsert_collection({"collection_id":name,"channel_id":-100,"cover":cover})
    seen=[]
    async def prepare(userbot,doc,http,force=False):
        seen.append(doc["collection_id"]);return {"hosted":False,"ready":False}
    monkeypatch.setattr(covers,"prepare_collection",prepare)
    runtime.userbot=NS(disconnect=AsyncMock())
    await handlers.cmd_retry_covers(update(),context());await cover_jobs._task
    assert set(seen)=={"pending","missing","failed","bad_ok"}


@pytest.mark.asyncio
async def test_retry_denied_nonadmin_and_no_connected_userbot():
    other=update(99);await handlers.cmd_retry_covers(other,context())
    assert not other.message.replies
    u=update();await handlers.cmd_retry_covers(u,context())
    assert "/setup_userbot" in u.message.replies[0][0]


@pytest.mark.asyncio
async def test_retry_no_work_message_is_factually_accurate():
    runtime.userbot=NS(disconnect=AsyncMock())
    await db.upsert_collection({"collection_id":"ok","cover":{"url_status":"ok","url":"https://graph.org/file/x.jpg"}})
    u=update();await handlers.cmd_retry_covers(u,context())
    assert "already have cached hosted cover URLs" in u.message.replies[0][0]


class RecordingRequest(BaseRequest):
    def __init__(self):self.calls=[]
    @property
    def read_timeout(self):return 5
    async def initialize(self):pass
    async def shutdown(self):pass
    async def do_request(self,url,method,request_data=None,**kwargs):
        endpoint=url.rsplit("/",1)[-1]
        params=request_data.parameters if request_data else {}
        self.calls.append((endpoint,params,request_data.multipart_data if request_data else None))
        if endpoint=="getMe":result={"id":123456,"is_bot":True,"first_name":"test","username":"Test_Recovery_Bot"}
        elif endpoint=="answerCallbackQuery":result=True
        else:result={"message_id":42,"date":0,"chat":{"id":7,"type":"private"},"text":"preview"}
        return 200,json.dumps({"ok":True,"result":result}).encode()


@pytest.mark.asyncio
async def test_real_ptb_callback_get_bot_uses_sendmessage_not_edit():
    await db.upsert_collection({**collection(),"cover":{"url":"https://telegra.ph/file/cover.jpg"}})
    request=RecordingRequest()
    async with TelegramBot("123456:FAKE_TEST_TOKEN_NOT_REAL",request=request) as bot:
        incoming=Update.de_json({"update_id":1,"callback_query":{
            "id":"test_query","from":{"id":7,"is_bot":False,"first_name":"Alice"},
            "chat_instance":"test","data":"feed:all:0","message":{
                "message_id":40,"date":0,"chat":{"id":7,"type":"private"},"text":"menu"}}},bot)
        assert not hasattr(incoming.callback_query,"bot") and incoming.callback_query.get_bot() is bot
        await handlers.on_button(incoming,NS(bot=bot))
    sends=[params for endpoint,params,files in request.calls if endpoint=="sendMessage"]
    assert len(sends)==1 and sends[0]["parse_mode"]=="HTML"
    opts=sends[0]["link_preview_options"]
    if isinstance(opts,str):opts=json.loads(opts)
    assert opts["show_above_text"] and opts["prefer_large_media"]
    assert opts["url"]=="https://telegra.ph/file/cover.jpg"
    keyboard=sends[0]["reply_markup"]
    if isinstance(keyboard,str):keyboard=json.loads(keyboard)
    assert keyboard["inline_keyboard"][0][1]["style"]=="danger"
    assert not any(e.startswith("edit") or e in ("sendPhoto","sendRichMessage") for e,_,_ in request.calls)
    assert [e for e,_,_ in request.calls][-2:]==["sendMessage","deleteMessage"]


@pytest.mark.asyncio
async def test_multiple_album_covers_downloaded_once_and_hosted_in_order(monkeypatch):
    doc={**collection(),"cover":{"message_id":10,"album_message_ids":[10,11],"url_status":"pending"}}
    await db.upsert_collection(doc)
    client=ScanClient([source_message(10,kind="photo"),source_message(11,kind="photo")])
    http=FakeHTTP()
    assert (await covers.prepare_collection(client,doc,http))["ready"]
    saved=await db.get_collection(doc["collection_id"])
    assert len(saved["cover"]["asset_keys"])==2 and len(saved["cover"]["urls"])==2
    assert len(client.downloads)==2 and len(http.posted)==2
    items = await covers.preview_items(saved)
    urls = [i["url"] for i in items if i.get("url")]
    import link_previews
    assert len(urls)==2 and await link_previews.resolve_url(saved)==urls[0]
    await covers.prepare_collection(client,saved,http,force=True)
    assert len(client.downloads)==2 and len(http.posted)==2


@pytest.mark.asyncio
async def test_both_hosts_down_caches_local_image_no_repeat_download_on_retry():
    class Down(FakeHTTP):
        async def post(self,*args,**kwargs):self.posted.append(args[0]);raise TimedOut()
    doc={**collection(),"cover":{"message_id":10,"album_message_ids":[10],"url_status":"pending"}}
    await db.upsert_collection(doc)
    client=ScanClient([source_message(10,kind="photo")]);http=Down()
    result=await covers.prepare_collection(client,doc,http)
    assert result["ready"] and not result["hosted"]
    saved=await db.get_collection(doc["collection_id"])
    assert saved["cover"]["preview_status"]=="ready" and saved["cover"]["url_status"]=="failed"
    assert await db.col("cover_assets").count_documents({"data":{"$exists":True}})==1
    assert len(client.downloads)==1 and len(http.posted)==2
    await covers.prepare_collection(client,saved,http,force=True)
    assert len(client.downloads)==1 and len(http.posted)==4


@pytest.mark.asyncio
async def test_video_cover_uses_thumbnail_never_full_video():
    class Video(ScanClient):
        async def download_media(self,media,file=None,thumb=None):
            assert thumb==-1
            file.write(jpeg());self.downloads.append(media)
    client=Video([source_message(10,kind="video",duration=20)])
    data=await covers.download_image(client,-100,10)
    assert data.startswith(b"\xff\xd8") and len(client.downloads)==1


@pytest.mark.asyncio
async def test_invalid_image_and_missing_source_have_explicit_diagnostics():
    with pytest.raises(Exception):covers.normalize_image(b"not an image")
    doc={**collection(),"cover":{"message_id":10,"url_status":"pending"}}
    await db.upsert_collection(doc)
    client=NS(get_messages=AsyncMock(return_value=None))
    result=await covers.prepare_collection(client,doc,FakeHTTP())
    assert not result["ready"]
    saved=await db.get_collection(doc["collection_id"])
    assert "source_preview_unavailable" in saved["cover"]["last_error"]


class PhotoBot:
    def __init__(self,network_error=False):self.calls=[];self.network_error=network_error
    async def _post(self,*args,**kwargs):
        self.calls.append("rich")
        if self.network_error:raise TimedOut()
        raise BadRequest("rich format unsupported")
    async def edit_message_media(self,**kwargs):
        self.calls.append(("edit_photo",kwargs));return NS(message_id=42,photo=[NS(file_id="CACHED_TELEGRAM_FILE")])
    async def send_photo(self,**kwargs):
        self.calls.append(("send_photo",kwargs));return NS(message_id=43,photo=[NS(file_id="CACHED_TELEGRAM_FILE")])
    async def delete_message(self,**kwargs):self.calls.append("delete")


@pytest.mark.asyncio
async def test_cached_image_has_public_preview_url_not_telegram_upload(monkeypatch):
    import link_previews
    monkeypatch.setattr(config,"RENDER_EXTERNAL_URL","https://example.onrender.com")
    key="-100:10"
    await db.col("cover_assets").insert_one({"_id":key,"data":Binary(jpeg())})
    coll={"cover":{"asset_keys":[key]}}
    url=await link_previews.resolve_url(coll)
    assert url.startswith("https://example.onrender.com/previews/")
    assert await link_previews.resolve_url(coll)==url
    asset=await db.col("cover_assets").find_one({"_id":key})
    assert "public_token" in asset and "telegram_file_id" not in asset


@pytest.mark.asyncio
async def test_send_timeout_keeps_old_card_and_does_not_try_other_endpoints():
    from unittest.mock import AsyncMock
    bot=NS(send_message=AsyncMock(side_effect=TimedOut()),delete_message=AsyncMock())
    with pytest.raises(TimedOut):
        await rich_cards.show_cover_card(bot,7,"text",None,
            {"cover":{"url":"https://graph.org/file/cover.jpg"}},edit_message_id=42)
    bot.send_message.assert_awaited_once()
    bot.delete_message.assert_not_called()


@pytest.mark.asyncio
async def test_photo_navigation_edits_caption_for_home_and_never_deletes_first():
    q=Q();q.message.photo=[NS(file_id="cached")]
    q.edit_message_caption=AsyncMock()
    await ui.edit_card(q,"<b>Main Menu</b>",None)
    q.edit_message_caption.assert_awaited_once()
    q.message.delete=AsyncMock()
    await ui.edit_card(q,"<b>"+"x"*1300+"</b>",None)
    assert q.message.replies and q.message.delete.called


@pytest.mark.asyncio
async def test_cover_status_reports_exact_incident_without_reading_user_credentials():
    for i in range(162):
        await db.upsert_collection({"collection_id":str(i),"cover":{"url_status":"pending"}})
    status=await cover_jobs.statistics()
    assert status["Collections"]==162 and status["Pending"]==162
    assert status["Hosted URL ready"]==0 and status["Worker running"]=="no"
    u=update();await handlers.cmd_cover_status(u,context())
    assert "Pending: 162" in u.message.replies[-1][0]


@pytest.mark.asyncio
async def test_head_405_image_validation_uses_range_get():
    client=NS(head=AsyncMock(return_value=NS(status_code=405,headers={})),
        get=AsyncMock(return_value=NS(status_code=206,headers={"content-type":"image/jpeg"})))
    assert await covers._validate(client,"https://graph.org/file/image.jpg")
    assert client.get.call_args.kwargs["headers"]=={"Range":"bytes=0-0"}


@pytest.mark.asyncio
async def test_real_ptb_cached_cover_sends_no_multipart(monkeypatch):
    monkeypatch.setattr(config,"RENDER_EXTERNAL_URL","https://example.onrender.com")
    key="-100:10"
    await db.col("cover_assets").insert_one({"_id":key,"data":Binary(jpeg())})
    request=RecordingRequest()
    async with TelegramBot("123456:FAKE_TEST_TOKEN_NOT_REAL",request=request) as bot:
        await rich_cards.show_cover_card(bot,7,"<b>Preview</b>",None,
            {"cover":{"asset_keys":[key]}},edit_message_id=40)
    sends=[(params,files) for endpoint,params,files in request.calls if endpoint=="sendMessage"]
    assert len(sends)==1 and not sends[0][1]
    assert not any(e in ("editMessageMedia","sendPhoto","sendRichMessage") for e,_,_ in request.calls)
    assert "/previews/" in sends[0][0]["text"]
