"""Link-preview-only contract; no production accounts/DB/media required.
Real PTB serialization, ASGI OG/image endpoints, caching, failed sends,
delete ordering, stale clicks, and the 162 cached/no-hosted-URL incident.
"""
import asyncio
import io
import json
import hashlib
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import httpx
import pytest
import pytest_asyncio
import mongomock_motor
from PIL import Image
from bson import Binary
from telegram import Bot, InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut
from telegram.request import BaseRequest
import config
import db
import main
import rich_cards
import link_previews
import ui


def jpeg():
    out=io.BytesIO();Image.new('RGB',(32,24),(100,120,140)).save(out,'JPEG');return out.getvalue()

@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(),'link_preview_tests')
    monkeypatch.setattr(config,'RENDER_EXTERNAL_URL','https://example.onrender.com')
    await db.col('cover_assets').create_index('public_token',unique=True,sparse=True)
    rich_cards._retired.clear()
    yield
    rich_cards._retired.clear()

async def seed(index=10):
    key=f'-100:{index}';data=jpeg()
    await db.col('cover_assets').insert_one({'_id':key,'data':Binary(data),'mime':'image/jpeg',
        'sha256':hashlib.sha256(data).hexdigest(),'source_message_id':index,'channel_id':-100})
    c={'collection_id':f'c{index}','channel_id':-100,
       'cover':{'url_status':'failed','url':None,'asset_keys':[key]}}
    await db.col('collections').insert_one(c)
    return c

class StrictBot:
    """Only sendMessage/deleteMessage exist: any media/edit call fails immediately."""
    def __init__(self,send_error=None,delete_error=None):
        self.calls=[];self.send_error=send_error;self.delete_error=delete_error;self.next_id=101
    async def send_message(self,**kw):
        self.calls.append(('sendMessage',kw))
        if self.send_error:raise self.send_error
        result=NS(message_id=self.next_id);self.next_id+=1;return result
    async def delete_message(self,**kw):
        self.calls.append(('deleteMessage',kw))
        if self.delete_error:raise self.delete_error

@pytest.mark.parametrize('url',['https://telegra.ph/file/x.jpg','https://graph.org/file/x.jpg'])
async def test_hosted_link_text_and_exact_preview_options(url):
    bot=StrictBot();kb=InlineKeyboardMarkup([[ui.button('Unlock',callback_data='unlock:c')]])
    await rich_cards.show_cover_card(bot,7,'<b>Title</b>\n<i>Text</i>',kb,{'cover':{'url':url}},edit_message_id=42)
    assert [e for e,_ in bot.calls]==['sendMessage','deleteMessage']
    sent=bot.calls[0][1]
    assert sent['text']==f'<a href="{url}">\u2063</a>\n<b>Title</b>\n<i>Text</i>'
    assert sent['parse_mode']=='HTML'
    assert sent['link_preview_options'].to_dict()=={'url':url,'is_disabled':False,
        'prefer_large_media':True,'show_above_text':True}
    assert sent['reply_markup'].to_dict()['inline_keyboard'][0][0]['style']=='danger'
    assert bot.calls[-1][1]=={'chat_id':7,'message_id':42}

async def test_no_old_message_no_delete():
    bot=StrictBot();await rich_cards.show_cover_card(bot,7,'text',None,{'cover':{'url':'https://graph.org/file/a.jpg'}})
    assert len(bot.calls)==1

async def test_cached_cover_uses_stable_url_without_loading_binary_on_navigation(monkeypatch):
    c=await seed();assets=db.col('cover_assets');original=assets.find_one;projections=[]
    async def audited(*args,**kwargs):
        projection=args[1] if len(args)>1 else kwargs.get('projection')
        assert projection and projection.get('data')==0
        projections.append(projection);return await original(*args,**kwargs)
    monkeypatch.setattr(assets,'find_one',audited)
    original_col=db.col
    monkeypatch.setattr(db,'col',lambda name: assets if name=='cover_assets' else original_col(name))
    url=await link_previews.resolve_url(c)
    assert url.startswith('https://example.onrender.com/previews/')
    assert len(url.rsplit('/',1)[1])==48
    assert await link_previews.resolve_url(c)==url
    c2=await db.get_collection('c10')
    assert await link_previews.resolve_url(c2)==url
    assert len(projections)==1
    assert c2['cover']['link_preview_host']=='render'

async def test_all_162_cached_images_get_urls_without_mirrors_or_userbot(monkeypatch):
    # The resolver has no upload/download APIs; any accidental call is a failure.
    import covers
    monkeypatch.setattr(covers,'host_cover',AsyncMock(side_effect=AssertionError('no mirror uploads')))
    monkeypatch.setattr(covers,'download_image',AsyncMock(side_effect=AssertionError('no source downloads')))
    for i in range(162):await seed(i+1)
    assert await link_previews.backfill_urls()==162
    assert await db.col('collections').count_documents({'cover.link_preview_host':'render'})==162
    first=[a['public_token'] async for a in db.col('cover_assets').find({})]
    assert len(set(first))==162
    assert await link_previews.backfill_urls()==162
    assert first==[a['public_token'] async for a in db.col('cover_assets').find({})]
    assert await db.col('collections').count_documents({'cover.url_status':'failed'})==162  # truth: mirrors still failed

@pytest.mark.parametrize('error',[TimedOut(),BadRequest('message rejected')])
async def test_send_failure_never_deletes_old_or_tries_media(error):
    bot=StrictBot(send_error=error)
    with pytest.raises(type(error)):
        await rich_cards.show_cover_card(bot,7,'text',None,{'cover':{'url':'https://graph.org/file/a.jpg'}},edit_message_id=42)
    assert [e for e,_ in bot.calls]==['sendMessage']

async def test_delete_failure_preserves_new_card():
    bot=StrictBot(delete_error=BadRequest('cannot delete'))
    result=await rich_cards.show_cover_card(bot,7,'text',None,{'cover':{'url':'https://graph.org/file/a.jpg'}},edit_message_id=42)
    assert result.message_id==101
    assert len(bot.calls)==2

async def test_duplicate_rapid_taps_do_not_send_two_replacements():
    bot=StrictBot();c={'cover':{'url':'https://graph.org/file/a.jpg'}}
    results=await asyncio.gather(*[rich_cards.show_cover_card(bot,7,'text',None,c,edit_message_id=42) for _ in range(5)])
    assert [e for e,_ in bot.calls]==['sendMessage','deleteMessage']
    assert len([r for r in results if r is not None])==1

async def test_next_previous_chain_each_send_then_delete_and_reuses_url():
    bot=StrictBot();a=await seed(10);b=await seed(11)
    await rich_cards.show_cover_card(bot,7,'a',None,a,edit_message_id=40)
    await rich_cards.show_cover_card(bot,7,'b',None,b,edit_message_id=101)
    await rich_cards.show_cover_card(bot,7,'a',None,a,edit_message_id=102)
    assert [e for e,_ in bot.calls]==['sendMessage','deleteMessage']*3
    assert bot.calls[0][1]['link_preview_options'].url==bot.calls[4][1]['link_preview_options'].url
    assert [kw['message_id'] for e,kw in bot.calls if e=='deleteMessage']==[40,101,102]

async def test_missing_cover_is_explicit_text_not_photo():
    bot=StrictBot();await rich_cards.show_cover_card(bot,7,'text',None,{'cover':{}})
    kw=bot.calls[0][1]
    assert 'Cover preview unavailable' in kw['text'] and kw['link_preview_options'].is_disabled

@pytest.mark.parametrize('base',['','http://example.com','https://user:pass@example.com','https://example.com/?token=secret'])
async def test_bad_public_base_does_not_publish_link(monkeypatch,base):
    monkeypatch.setattr(config,'RENDER_EXTERNAL_URL',base)
    assert await link_previews.resolve_url(await seed()) is None

async def test_served_og_html_and_image_are_real_and_do_not_expose_source_fields():
    c=await seed();url=await link_previews.resolve_url(c);path=url.split('.com',1)[1]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='https://example.onrender.com') as client:
        response=await client.get(path)
        assert response.status_code==200
        assert '<meta property="og:image"' in response.text
        assert url+'.jpg' in response.text
        assert 'source_message_id' not in response.text and '-100:10' not in response.text
        image=await client.get(path+'.jpg')
        assert image.status_code==200 and image.content==jpeg()
        assert image.headers['content-type']=='image/jpeg' and 'public' in image.headers['cache-control']
        Image.open(io.BytesIO(image.content)).verify()
        head=await client.head(path+'.jpg')
        assert head.status_code==200 and head.content==b''
        assert head.headers['content-length']==str(len(image.content))
        assert (await client.head(path)).status_code==200
        assert (await client.get('/previews/'+'a'*48)).status_code==404
        assert (await client.get('/previews/not-a-token.jpg')).status_code==404
        assert (await client.get('/previews/-100:10')).status_code==404
        assert (await client.get('/previews/'+url.rsplit('/',1)[1]+'/other')).status_code==404

async def test_deleting_asset_revokes_public_url():
    c=await seed();url=await link_previews.resolve_url(c)
    await db.col('cover_assets').delete_many({})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='https://example.onrender.com') as client:
        assert (await client.get(url)).status_code==404
        assert (await client.get(url+'.jpg')).status_code==404

class Recording(BaseRequest):
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
        if endpoint=='getMe':result={'id':123456,'is_bot':True,'first_name':'Test','username':'Link_Test_Bot'}
        elif endpoint=='deleteMessage':result=True
        else:result={'message_id':101,'date':0,'chat':{'id':7,'type':'private'},'text':'preview'}
        return 200,json.dumps({'ok':True,'result':result}).encode()

async def test_real_ptb_wire_request_has_no_image_bytes_no_richmessage_no_edits():
    c=await seed();request=Recording()
    async with Bot('123456:FAKE_TEST_TOKEN_NOT_REAL',request=request) as bot:
        await rich_cards.show_cover_card(bot,7,'<b>Title</b>',None,c,edit_message_id=42)
    assert [e for e,_,_ in request.calls]==['getMe','sendMessage','deleteMessage']
    assert all(not files for _,_,files in request.calls)
    _,params,_=request.calls[1]
    opts=params['link_preview_options']
    if isinstance(opts,str):opts=json.loads(opts)
    assert opts['show_above_text'] and opts['prefer_large_media'] and not opts['is_disabled']
    assert '/previews/' in opts['url'] and '\u2063' in params['text']
    assert 'rich_message' not in params and 'photo' not in params and 'media' not in params
