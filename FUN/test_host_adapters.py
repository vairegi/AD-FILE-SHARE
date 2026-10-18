"""Direct-host adapters: real httpx multipart serialization to MockTransport,
Catbox URL integration, old-photo migration, job pause/progress/cache safety.
No production data, real accounts or uploaded user images.
"""
import asyncio
import io
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import httpx
import pytest
import pytest_asyncio
import mongomock_motor
from bson import Binary
from PIL import Image
from telegram import Bot
from telegram.error import BadRequest, TimedOut
from telegram.request import BaseRequest
import cover_hosts
import covers
import db
import config
import cover_jobs
import rich_cards
import main
import handlers
from test_repairs import update, context
from test_cards import CardBot

URL='https://files.catbox.moe/abc123.jpg'


def jpeg():
    b=io.BytesIO();Image.new('RGB',(8,8),(10,20,30)).save(b,'JPEG');return b.getvalue()

@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(),'host_adapter_tests')
    monkeypatch.setattr(config,'CATBOX_USERHASH','')
    monkeypatch.setattr(config,'OWNER_ID',7);monkeypatch.setattr(config,'ADMIN_IDS',[7])
    cover_jobs._task=None
    yield
    if cover_jobs.busy():
        cover_jobs._task.cancel()
        try:await cover_jobs._task
        except asyncio.CancelledError:pass
    cover_jobs._task=None

async def seed(cid='c1',hosted=False):
    key='-100:'+cid
    await db.col('cover_assets').insert_one({'_id':key,'data':Binary(jpeg())})
    doc={'collection_id':cid,'channel_id':-100,'cover':{'asset_keys':[key],'message_id':1,
        'url_status':'ok' if hosted else 'failed','url':URL if hosted else None}}
    await db.upsert_collection(doc)
    return doc

async def test_exact_catbox_multipart_endpoint_fields_and_text_response():
    calls=[]
    def record(request):
        calls.append(request)
        if request.url.host=='catbox.moe':return httpx.Response(200,text=URL+'\n')
        return httpx.Response(400,json='Unknown error')
    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
        result=await covers.host_cover(client,jpeg())
    assert result['host']=='catbox.moe' and result['url']==URL
    assert [str(r.url) for r in calls]==['https://telegra.ph/upload','https://graph.org/upload','https://catbox.moe/user/api.php']
    body=calls[-1].content
    assert b'name="reqtype"' in body and b'fileupload' in body
    assert b'name="fileToUpload"; filename="cover.jpg"' in body
    assert b'Content-Type: image/jpeg' in body
    assert b'name="userhash"' not in body and b'access_token' not in body

async def test_optional_userhash_sent_only_to_catbox_and_redacted(monkeypatch):
    monkeypatch.setattr(config,'CATBOX_USERHASH','private_hash')
    requests=[]
    def record(request):
        requests.append(request)
        return httpx.Response(412,text='Invalid uploader userhash=private_hash')
    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
        result=await covers.host_cover(client,jpeg())
    assert b'private_hash' in requests[-1].content
    assert all(b'private_hash' not in r.content for r in requests[:-1])
    assert 'private_hash' not in result['error']
    assert 'HTTP 412' in result['error'] and 'Invalid uploader' in result['error']

async def test_rate_limit_on_first_provider_still_tries_graph_then_catbox():
    hosts=[]
    def reply(request):
        hosts.append(request.url.host)
        if request.url.host=='catbox.moe':return httpx.Response(200,text=URL)
        return httpx.Response(429,headers={'Retry-After':'60'},text='Try again later')
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        result=await covers.host_cover(client,jpeg())
    assert result['host']=='catbox.moe' and hosts==list(cover_hosts.HOSTS)
    assert len(cover_hosts.diagnostics())==2

async def test_rejected_hosts_are_not_hit_again_for_every_cover():
    hosts=[]
    def reply(request):
        hosts.append(request.url.host)
        if request.url.host=='catbox.moe':return httpx.Response(200,text=URL)
        return httpx.Response(400,json='Unknown error')
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        for _ in range(4):assert (await covers.host_cover(client,jpeg()))['status']=='ok'
    assert hosts.count('telegra.ph')==1 and hosts.count('graph.org')==1 and hosts.count('catbox.moe')==4

async def test_catbox_http200_error_is_not_saved_as_url():
    def reply(request):return httpx.Response(200,text='Upload failed!')
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        result=await covers.host_cover(client,jpeg())
    assert result['status']=='failed' and result['url'] is None

@pytest.mark.parametrize('url',[
    'https://catbox.moe','https://files.catbox.moe.evil.test/a.jpg',
    'https://user:pass@files.catbox.moe/a.jpg','http://files.catbox.moe/a.jpg',
    'https://files.catbox.moe/a.jpg?secret=1','https://files.catbox.moe/../../secret',
    'https://files.catbox.moe/a.exe','https://files.catbox.moe:444/a.jpg',
    'https://evil.test/cover.jpg',None])
async def test_rejects_non_direct_or_untrusted_urls(url):
    assert not cover_hosts.valid_direct_url(url)

async def test_catbox_plain_url_stored_then_rescan_skips_upload_even_force(monkeypatch):
    doc=await seed();posts=[]
    def reply(request):
        posts.append(request)
        if request.url.host=='catbox.moe':return httpx.Response(200,text=URL)
        return httpx.Response(400,json='Unknown error')
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        result=await covers.prepare_collection(None,doc,client,force=True)
        assert result['hosted']
        saved=await db.get_collection('c1')
        assert saved['cover']['url']==URL and saved['cover']['url_host']=='catbox.moe'
        assert saved['cover']['url_status']=='ok'
        before=len(posts)
        assert (await covers.prepare_collection(None,saved,client,force=True))['cached']
        assert len(posts)==before

async def test_catbox_card_edits_text_preview_no_media_upload():
    bot=CardBot()
    await rich_cards.show_cover_card(bot,7,'<b>Caption</b>',None,{'cover':{'url':URL}},edit_message_id=42)
    assert [e for e,_ in bot.calls]==['editMessageText']
    kw=bot.calls[0][1]
    assert kw['link_preview_options'].url==URL and kw['link_preview_options'].show_above_text
    assert URL in kw['text'] and '\u2063' in kw['text']

async def test_failed_upgrade_from_photo_keeps_original_no_delete():
    bot=CardBot(edit_text_error=BadRequest('there is no text in the message to edit'),send_error=TimedOut())
    with pytest.raises(TimedOut):
        await rich_cards.show_cover_card(bot,7,'x',None,{'cover':{'url':URL}},edit_message_id=42)
    assert [e for e,_ in bot.calls]==['editMessageText','sendMessage']

async def test_162_cached_covers_all_reach_catbox_and_economy_unchanged(monkeypatch):
    for i in range(162):await seed('c'+str(i))
    await db.create_user(7);await db.add_tokens(7,11,'test');await db.insert_unlock(7,'c0')
    factory=httpx.AsyncClient
    calls=[]
    def reply(request):
        calls.append(request.url.host)
        return httpx.Response(200,text=URL) if request.url.host=='catbox.moe' else httpx.Response(400,json='Unknown error')
    monkeypatch.setattr(cover_jobs.httpx,'AsyncClient',lambda:factory(transport=httpx.MockTransport(reply)))
    await cover_jobs._run(None,None,None)
    job=await db.col('cover_jobs').find_one({'_id':'current'})
    assert job['hosted']==162 and job['processed']==162 and job['state']=='complete'
    assert calls.count('telegra.ph')==1 and calls.count('graph.org')==1 and calls.count('catbox.moe')==162
    assert (await db.get_user(7))['tokens']==11 and await db.is_unlocked(7,'c0')
    assert (await cover_jobs.statistics())['Catbox links ready']==162

async def test_job_pauses_after_rejected_host_probe_not_162_false_successes(monkeypatch):
    for i in range(162):await seed('c'+str(i))
    factory=httpx.AsyncClient;calls=[]
    def reply(request):
        calls.append(request)
        return httpx.Response(412,text='Invalid uploader') if request.url.host=='catbox.moe' else httpx.Response(400,json='Unknown error')
    monkeypatch.setattr(cover_jobs.httpx,'AsyncClient',lambda:factory(transport=httpx.MockTransport(reply)))
    texts=[]
    async def save(text):texts.append(text)
    await cover_jobs._run(None,None,save)
    job=await db.col('cover_jobs').find_one({'_id':'current'})
    assert job['state']=='hosts_unavailable_paused' and job['processed']==1 and job['hosted']==0
    assert job['remaining']==161 and len(calls)==3
    assert 'No new hosted URLs' in texts[-1] and 'NOT link previews' in texts[-1]
    assert 'Invalid uploader' in job['provider_diagnostics']['catbox.moe']

async def test_rate_retry_retries_same_cover_before_advancing(monkeypatch):
    await seed('first');await seed('second');ids=[];texts=[]
    async def prepare(userbot,doc,http,force=False):
        ids.append(doc['collection_id'])
        if len(ids)==1:return {'hosted':False,'ready':True,'rate_limited':True}
        return {'hosted':True,'ready':True}
    monkeypatch.setattr(covers,'prepare_collection',prepare)
    monkeypatch.setattr(cover_jobs.asyncio,'sleep',AsyncMock())
    await cover_jobs._run(None,None,None)
    assert ids==['first','first','second']
    assert (await db.col('cover_jobs').find_one({'_id':'current'}))['hosted']==2

async def test_commands_are_registered_to_correct_handlers(monkeypatch):
    monkeypatch.setattr(config,'BOT_TOKEN','123456:FAKE_TEST_TOKEN_NOT_REAL')
    app=main.build_bot()
    mapping={name: handler.callback for group in app.handlers.values() for handler in group if hasattr(handler,'commands') for name in handler.commands}
    assert mapping['covermode'] is handlers.cmd_covermode
    assert mapping['retry_covers'] is handlers.cmd_retry_covers

class Wire(BaseRequest):
    def __init__(self):self.calls=[]
    @property
    def read_timeout(self):return 5
    async def initialize(self):pass
    async def shutdown(self):pass
    async def do_request(self,url,method,request_data=None,**kwargs):
        endpoint=url.rsplit('/',1)[-1]
        params=request_data.parameters if request_data else {}
        files=request_data.multipart_data if request_data else None
        self.calls.append((endpoint,params,files))
        if endpoint=='getMe':result={'id':123456,'is_bot':True,'first_name':'Test','username':'Adapter_Test_Bot'}
        else:result={'message_id':42,'date':0,'chat':{'id':7,'type':'private'},'text':'card'}
        return 200,json.dumps({'ok':True,'result':result}).encode()

async def test_real_ptb_catbox_preview_wire_no_multipart_or_photo_methods():
    wire=Wire()
    async with Bot('123456:FAKE_TEST_TOKEN_NOT_REAL',request=wire) as bot:
        await rich_cards.show_cover_card(bot,7,'<b>Caption</b>',None,{'cover':{'url':URL}},edit_message_id=42)
    assert [e for e,_,_ in wire.calls]==['getMe','editMessageText']
    params=wire.calls[-1][1];options=params['link_preview_options']
    if isinstance(options,str):options=json.loads(options)
    assert options['url']==URL and options['show_above_text'] and options['prefer_large_media']
    assert all(not files for _,_,files in wire.calls)
