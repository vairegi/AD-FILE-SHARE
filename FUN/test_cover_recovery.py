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
async def test_real_ptb_callback_get_bot_and_exact_rich_html_request():
    await db.upsert_collection({**collection(),"cover":{"message_id":10,"url_status":"ok",
        "url":"https://telegra.ph/file/cover.jpg"}})
    request=RecordingRequest()
    async with TelegramBot("123456:FAKE_TEST_TOKEN_NOT_REAL",request=request) as bot:
        incoming=Update.de_json({"update_id":1,"callback_query":{
            "id":"test_query","from":{"id":7,"is_bot":False,"first_name":"Alice"},
            "chat_instance":"test","data":"feed:all:0","message":{
                "message_id":42,"date":0,"chat":{"id":7,"type":"private"},"text":"menu"}}},bot)
        q=incoming.callback_query
        assert not hasattr(q,"bot") and q.get_bot() is bot
        await handlers.on_button(incoming,NS(bot=bot))
    edits=[params for endpoint,params,files in request.calls if endpoint=="editMessageText"]
    assert edits,"The real CallbackQuery must enter the media path"
    payload=edits[0]["rich_message"]
    if isinstance(payload,str):payload=json.loads(payload)
    assert set(payload)=={"html"}
    assert '<tg-slideshow><img src="https://telegra.ph/file/cover.jpg"/></tg-slideshow>' in payload["html"]
    keyboard=edits[0]["reply_markup"]
    if isinstance(keyboard,str):keyboard=json.loads(keyboard)
    assert keyboard["inline_keyboard"][0][1]["style"]=="danger"


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
    payload=rich_cards._rich_payload("<b>Title</b>",await covers.preview_items(saved),"collage")
    assert payload["html"].count("<img ")==2 and "<tg-collage>" in payload["html"]
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
async def test_local_fallback_then_reuses_telegram_file_id_without_binary_upload():
    key="-100:10"
    await db.col("cover_assets").insert_one({"_id":key,"data":Binary(jpeg())})
    previews=[{"key":key,"url":None,"file_id":None}]
    bot=PhotoBot()
    await rich_cards.show_cover_card(bot,7,"<b>Text</b>",None,previews,"slideshow",edit_message_id=42)
    assert bot.calls[0][0]=="edit_photo" and "delete" not in bot.calls
    asset=await db.col("cover_assets").find_one({"_id":key})
    assert asset["telegram_file_id"]=="CACHED_TELEGRAM_FILE"
    previews[0]["file_id"]=asset["telegram_file_id"]
    bot.calls.clear()
    await rich_cards.show_cover_card(bot,7,"Text",None,previews,"slideshow",edit_message_id=42)
    assert bot.calls[0][1]["media"].media=="CACHED_TELEGRAM_FILE"


@pytest.mark.asyncio
async def test_rich_reject_falls_back_in_place_and_timeout_never_blind_retries():
    previews=[{"url":"https://graph.org/file/cover.jpg"}]
    bot=PhotoBot()
    await rich_cards.show_cover_card(bot,7,"text",None,previews,"collage",edit_message_id=42)
    assert bot.calls[0]=="rich" and bot.calls[1][0]=="edit_photo" and "delete" not in bot.calls
    bot=PhotoBot(network_error=True)
    with pytest.raises(TimedOut):
        await rich_cards.show_cover_card(bot,7,"text",None,previews,"collage",edit_message_id=42)
    assert bot.calls==["rich"]


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
async def test_real_ptb_cached_image_fallback_serializes_multipart():
    key="-100:10"
    await db.col("cover_assets").insert_one({"_id":key,"data":Binary(jpeg())})
    request=RecordingRequest()
    async with TelegramBot("123456:FAKE_TEST_TOKEN_NOT_REAL",request=request) as bot:
        await rich_cards.show_cover_card(bot,7,"<b>Preview</b>",None,
            [{"key":key,"url":None,"file_id":None}],"slideshow",edit_message_id=42)
    edits=[(params,files) for endpoint,params,files in request.calls if endpoint=="editMessageMedia"]
    assert len(edits)==1 and edits[0][1], "Cached image must become a real multipart file upload"
    media=edits[0][0]["media"]
    if isinstance(media,str):media=json.loads(media)
    assert media["type"]=="photo" and media["media"].startswith("attach://")
