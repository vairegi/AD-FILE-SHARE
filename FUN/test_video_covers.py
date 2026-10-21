"""Video cover regressions. Telegram requests are recorded, never sent.
Optional real MongoDB is ONLY 127.0.0.1:27019 in a sandbox-only database.
"""
import asyncio
import html
import json
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import mongomock_motor
import pytest
import pytest_asyncio
from telegram import Bot, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut
from telegram.request import BaseRequest
import classifier, config, cover_sources, covers, cover_jobs, db, handlers, live_index, main, rich_cards, scanner, ui
from test_cards import CardBot
from test_live_index import payload
from test_repairs import Source, source_message, update, context

CHAT=-10012345
CAPTION='Example <title> & details\nSecond line\nThird line'
MARKUP=InlineKeyboardMarkup([[InlineKeyboardButton('Next', callback_data='watch:next:s1'),
                             InlineKeyboardButton('Unlock', callback_data='unlock:c-10012345_10')]])


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    client=None
    if os.getenv('BOT3_LOCAL_MONGO_TEST') == '1':
        from motor.motor_asyncio import AsyncIOMotorClient
        client=AsyncIOMotorClient('mongodb://127.0.0.1:27019', serverSelectionTimeoutMS=3000)
        await client.drop_database('video_cover_sandbox_only')
        db.use_test_client(client,'video_cover_sandbox_only')
    else:
        db.use_test_client(mongomock_motor.AsyncMongoMockClient(),'video_cover_tests')
    await db.col('collections').create_index('collection_id',unique=True)
    await live_index.ensure_indexes()
    await db.upsert_channel_mapping(CHAT,'sample')
    monkeypatch.setattr(config,'ADMIN_IDS',[7])
    monkeypatch.setattr(config,'OWNER_ID',7)
    yield
    if client:
        await client.drop_database('video_cover_sandbox_only')
        client.close()


def row(kind='video',caption=CAPTION,mid=10):
    return {'collection_id':f'c{CHAT}_{mid}','channel_id':CHAT,'title':'Example',
            'category':'sample','cover':{'media_type':kind,'message_id':mid,'caption':caption,
                                      'url_status':'failed'},'files':[], 'file_count':0}


class CopyBot(CardBot):
    def __init__(self,copy_error=None,**kw):
        super().__init__(**kw)
        self.copy_error=copy_error
        self.next_id=800
    async def copy_message(self,**kw):
        self.calls.append(('copyMessage',kw))
        if self.copy_error:
            raise self.copy_error
        self.next_id+=1
        return NS(message_id=self.next_id)


@pytest.mark.parametrize('kind',['video','animation','document_video'])
async def test_native_cover_legacy_row_uses_source_copy_not_photo_or_upload(kind):
    c=row(kind)
    c['cover']['url']='https://files.catbox.moe/legacy.jpg'
    await db.upsert_collection(c)
    bot=CopyBot()
    await rich_cards.show_cover_card(bot,7,'<b>Cost: 7</b>',MARKUP,c,edit_message_id=42)
    assert [name for name,_ in bot.calls]==['copyMessage','deleteMessage']
    kw=bot.calls[0][1]
    assert (kw['from_chat_id'],kw['message_id'])==(CHAT,10)
    assert kw['reply_markup'] is MARKUP and kw['protect_content'] is True
    assert html.escape(CAPTION) in kw['caption'] and '<b>Cost: 7</b>' in kw['caption']
    saved=await db.get_collection(c['collection_id'])
    assert saved['cover']['source_chat_id']==CHAT
    assert saved['cover']['source_message_id']==10


@pytest.mark.parametrize('kind',['photo','video','animation','document_video'])
async def test_classifier_stores_source_and_excludes_cover_from_delivery(kind):
    posts=[{'message_id':10,'media_type':kind,'caption':CAPTION},
           {'message_id':11,'media_type':'video','caption':''}]
    result=classifier.classify(posts,CHAT,'sample')
    c=result['collections'][0]
    assert c['cover']['kind']==kind
    assert c['cover']['source_chat_id']==CHAT and c['cover']['source_message_id']==10
    assert c['file_count']==1 and c['files'][0]['message_id']==11


@pytest.mark.parametrize('kind',['video','animation','document_video'])
async def test_short_caption_video_remains_delivery(kind):
    result=classifier.classify([{'message_id':10,'media_type':kind,'caption':'Only\ntwo'}],CHAT,'sample')
    assert result['collections']==[] and result['pending'][0]['message_id']==10


async def test_mixed_album_selects_video_member_but_stable_id_remains_first_member():
    c=classifier.classify([
        {'message_id':10,'media_type':'photo','caption':CAPTION,'grouped_id':'g'},
        {'message_id':11,'media_type':'video','caption':'','grouped_id':'g'}],CHAT,'sample')['collections'][0]
    assert c['collection_id']==f'c{CHAT}_10'
    assert c['cover']['source_message_id']==11 and c['cover']['message_id']==10
    bot=CopyBot()
    await rich_cards.show_cover_card(bot,7,'Stats',MARKUP,c)
    assert bot.calls[0][1]['message_id']==11
    assert html.escape(CAPTION) in bot.calls[0][1]['caption']


async def test_native_preparation_skips_all_downloads_and_catbox(monkeypatch):
    c=row()
    await db.upsert_collection(c)
    fetch=AsyncMock(side_effect=AssertionError('video bytes must not be fetched'))
    host=AsyncMock(side_effect=AssertionError('video must not reach image hosts'))
    monkeypatch.setattr(covers,'host_cover',host)
    assert await covers._prepare(c,None,fetch)=={'hosted':False,'ready':True,'native':True}
    fetch.assert_not_called()
    host.assert_not_called()
    assert await covers.needs_preparation_count()==0
    assert await db.col('collections').count_documents(cover_jobs.QUERY)==0


async def test_image_catbox_path_remains_same_link_preview():
    c=row('photo')
    c['cover'].update(url='https://files.catbox.moe/photo.jpg',url_status='ok')
    bot=CopyBot()
    await rich_cards.show_cover_card(bot,7,'Stats',MARKUP,c,edit_message_id=42)
    assert [n for n,_ in bot.calls]==['editMessageText']
    assert bot.calls[0][1]['link_preview_options'].url==c['cover']['url']


async def test_source_backfill_idempotent_and_preserves_owner_economy():
    c=row()
    c.update(price=9,views=33,unlocks=4,files=[{'message_id':15}],file_count=1)
    await db.upsert_collection(c)
    await db.col('users').insert_one({'user_id':7,'tokens':122})
    await db.col('unlocks').insert_one({'user_id':7,'collection_id':c['collection_id']})
    assert (await cover_sources.backfill())=={'checked':1,'updated':1,'unresolved':0}
    assert (await cover_sources.backfill())['updated']==0
    saved=await db.get_collection(c['collection_id'])
    for key in ('price','views','unlocks','files','file_count'):
        assert saved[key]==c[key]
    assert (await db.col('users').find_one({'user_id':7}))['tokens']==122
    assert await db.col('unlocks').count_documents({})==1


async def test_missing_top_level_chat_can_be_derived_without_scan():
    c=row(); c.pop('channel_id')
    assert cover_sources.resolve(c)==('video',CHAT,10)
    await db.upsert_collection(c)
    assert (await cover_sources.backfill())['updated']==1


async def test_unresolvable_source_reported_not_invented():
    c={'collection_id':'legacy-unparseable','cover':{'media_type':'video'}}
    await db.upsert_collection(c)
    assert (await cover_sources.backfill())['unresolved']==1
    bot=CopyBot()
    await rich_cards.show_cover_card(bot,7,'Stats',MARKUP,c)
    assert [n for n,_ in bot.calls]==['sendMessage']
    assert 'source is missing' in bot.calls[0][1]['text']


async def test_long_caption_is_copied_verbatim_separate_controls_cleanup():
    c=row(caption='😃'*500+'\nsecond\nthird')
    bot=CopyBot()
    result=await rich_cards.show_cover_card(bot,7,'Statistics: balance 122',MARKUP,c,edit_message_id=42)
    assert [n for n,_ in bot.calls]==['copyMessage','sendMessage','deleteMessage']
    assert 'caption' not in bot.calls[0][1]  # Telegram preserves source caption/entities verbatim
    assert bot.calls[1][1]['reply_markup'] is MARKUP
    assert await db.col('cover_companions').count_documents({})==1
    photo=row('photo'); photo['cover']['url']='https://files.catbox.moe/a.jpg'
    await rich_cards.show_cover_card(bot,7,'New',MARKUP,photo,edit_message_id=result.message_id)
    assert ('deleteMessage',{'chat_id':7,'message_id':801}) in bot.calls
    assert await db.col('cover_companions').count_documents({})==0


async def test_companion_cleanup_on_home_or_categories():
    bot=CopyBot()
    await db.col('cover_companions').insert_one({'_id':'7:42','cover_message_id':99})
    q=NS(message=NS(chat=NS(id=7),message_id=42,photo=None),
         get_bot=lambda:bot, edit_message_text=AsyncMock(return_value=NS(message_id=42)))
    await ui.edit_card(q,'Main Menu',MARKUP)
    q.edit_message_text.assert_awaited_once()
    assert bot.calls==[('deleteMessage',{'chat_id':7,'message_id':99})]


@pytest.mark.parametrize('media',['video','animation','document'])
async def test_main_menu_and_categories_callbacks_support_native_media(media):
    message=NS(photo=None,video=None,animation=None,document=None)
    setattr(message,media,True)
    q=NS(message=message,edit_message_caption=AsyncMock(return_value=NS(message_id=42)))
    await ui.edit_card(q,'Main Menu',MARKUP)
    q.edit_message_caption.assert_awaited_once()


async def test_copy_timeout_never_deletes_previous_card_or_retries_upload():
    bot=CopyBot(copy_error=TimedOut())
    with pytest.raises(TimedOut):
        await rich_cards.show_cover_card(bot,7,'Stats',MARKUP,row(),edit_message_id=42)
    assert [n for n,_ in bot.calls]==['copyMessage']


async def test_deleted_source_fallback_retains_controls_no_reupload():
    bot=CopyBot(copy_error=BadRequest('message to copy not found'))
    await rich_cards.show_cover_card(bot,7,'Stats',MARKUP,row(),edit_message_id=42)
    assert [n for n,_ in bot.calls]==['copyMessage','sendMessage','deleteMessage']
    assert 'source deleted' in bot.calls[1][1]['text']
    assert bot.calls[1][1]['reply_markup'] is MARKUP


async def test_failed_replacement_keeps_old_card():
    bot=CopyBot(copy_error=BadRequest('message to copy not found'),send_error=TimedOut())
    with pytest.raises(TimedOut):
        await rich_cards.show_cover_card(bot,7,'Stats',MARKUP,row(),edit_message_id=42)
    assert [n for n,_ in bot.calls]==['copyMessage','sendMessage']


async def test_protect_off_honoured():
    await db.update_settings({'protect_content':False})
    bot=CopyBot()
    await rich_cards.show_cover_card(bot,7,'Stats',MARKUP,row())
    assert bot.calls[0][1]['protect_content'] is False


@pytest.mark.parametrize('kind',['animation','document_video'])
async def test_real_botapi_metadata_detects_animation_before_document(kind):
    p=payload(10,kind='animation' if kind=='animation' else 'document',caption=CAPTION)
    if kind=='animation':
        p['document']={'file_id':'also_doc','file_unique_id':'docid','mime_type':'video/mp4'}
    else:
        p['document']['mime_type']='video/mp4'
    post=live_index.post_metadata(Message.de_json(p,None))
    assert post['media_type']==kind
    assert classifier.classify([post],CHAT,'sample')['collections']


async def test_history_metadata_detects_video_document_and_animation():
    base=dict(id=10,photo=None,video=None,gif=None,message=CAPTION,date=None,grouped_id=None)
    m=NS(**base,document=NS(mime_type='video/mp4'))
    assert scanner._post_from_message(m)['media_type']=='document_video'
    m.gif=True
    assert scanner._post_from_message(m)['media_type']=='animation'


async def test_mixed_live_queue_dry_run_no_userbot():
    for mid,kind in [(10,'photo'),(20,'video'),(30,'animation'),(40,'document_video')]:
        p=payload(mid,kind='document' if kind=='document_video' else kind,caption=CAPTION)
        if kind=='document_video':p['document']['mime_type']='video/mp4'
        await live_index.enqueue(Message.de_json(p,None))
        await live_index.enqueue(Message.de_json(payload(mid+1,kind='video'),None))
    job=await db.col('source_jobs').find_one({'_id':CHAT})
    processed,_=await live_index.process_channel(job)
    assert processed==8
    docs=await db.col('collections').find({}).sort('cover.message_id',1).to_list(None)
    assert len(docs)==4 and all(c['file_count']==1 for c in docs)
    bot=CopyBot()
    for c in docs:
        if c['cover']['media_type']=='photo':
            c['cover']['url']='https://files.catbox.moe/sample.jpg'
        await rich_cards.show_cover_card(bot,7,'Cost / balance',MARKUP,c)
    counts={name:sum(n==name for n,_ in bot.calls) for name in ('copyMessage','sendMessage','sendPhoto')}
    assert counts=={'copyMessage':3,'sendMessage':1,'sendPhoto':0}
    print(f'MIXED DRY RUN: metadata={processed}; collections={len(docs)}; delivery_files=4; calls={counts}; video_downloads=0')


async def test_history_scan_replay_adds_refs_and_preserves_price():
    c=row(); c['price']=9
    await db.upsert_collection(c)
    source=Source([source_message(10,caption=CAPTION,kind='video'),source_message(11,caption='',kind='video')])
    await scanner.scan_channel(source,CHAT,'sample',full=True,prepare_covers=False)
    saved=await db.get_collection(c['collection_id'])
    assert saved['cover']['kind']=='video' and saved['cover']['source_message_id']==10
    assert saved['price']==9 and saved['file_count']==1


async def test_repair_command_is_admin_only_and_registered(monkeypatch):
    await db.upsert_collection(row())
    outsider=update(uid=88)
    await handlers.cmd_repair_video_covers(outsider,context())
    assert 'source_chat_id' not in (await db.get_collection(row()['collection_id']))['cover']
    owner=update(uid=7)
    await handlers.cmd_repair_video_covers(owner,context())
    assert 'No Telegram history scan' in owner.message.replies[-1][0]
    monkeypatch.setattr(config,'BOT_TOKEN','123456:offline-test-token')
    app=main.build_bot()
    assert any('repair_video_covers' in getattr(h,'commands',()) for h in app.handlers[0])


async def test_real_ptb_serializes_copy_request_without_upload():
    class Recording(BaseRequest):
        def __init__(self):self.calls=[]
        @property
        def read_timeout(self):return 5
        async def initialize(self):pass
        async def shutdown(self):pass
        async def do_request(self,url,method,request_data=None,**kwargs):
            name=url.rsplit('/',1)[-1]
            self.calls.append((name,request_data))
            result={'id':1,'is_bot':True,'first_name':'Offline','username':'offline_bot'} if name=='getMe' else {'message_id':555}
            return 200,json.dumps({'ok':True,'result':result}).encode()
    req=Recording()
    async with Bot('123456:offline-token',request=req) as bot:
        await rich_cards.show_cover_card(bot,7,'<b>Stats</b>',MARKUP,row())
    name,data=req.calls[-1]
    assert name=='copyMessage' and not data.multipart_data
    assert data.parameters['from_chat_id']==CHAT
    assert data.parameters['message_id']==10
    assert 'watch:next:s1' in data.json_parameters['reply_markup']


async def test_video_to_unprepared_photo_sends_warning_card_before_delete():
    bot=CopyBot(edit_text_error=BadRequest('there is no text in the message to edit'))
    await rich_cards.show_cover_card(bot,7,'Stats',MARKUP,row('photo'),edit_message_id=801)
    assert [n for n,_ in bot.calls]==['editMessageText','sendMessage','deleteMessage']
    assert 'Cover preview unavailable' in bot.calls[1][1]['text']


async def test_actual_watch_next_previous_and_home_flow():
    import feed_sessions
    for mid,kind in [(10,'video'),(20,'photo')]:
        c=row(kind,mid=mid)
        c.update(status='active',files=[{'message_id':mid+1}],file_count=1)
        if kind=='photo':c['cover']['url']='https://files.catbox.moe/photo.jpg'
        await db.upsert_collection(c)
    await db.col('users').insert_one({'user_id':7,'tokens':122})
    bot=CopyBot()
    q=NS(from_user=NS(id=7),message=NS(message_id=42,chat=NS(id=7)),get_bot=lambda:bot)
    state,c,total=await feed_sessions.start(7,'sample',seed=f'c{CHAT}_10')
    await handlers._render_feed_card(q,c,'sample',0,total,session=state)
    assert bot.calls[0][0]=='copyMessage'
    buttons=bot.calls[0][1]['reply_markup'].to_dict()['inline_keyboard']
    callbacks=[b.get('callback_data') for row_buttons in buttons for b in row_buttons]
    assert f'watch:next:{state["session_id"]}' in callbacks
    assert f'unlock:c{CHAT}_10' in callbacks
    q.message.message_id=801
    bot.edit_text_error=BadRequest('there is no text in the message to edit')
    await handlers._show_random_step(q,state['session_id'],'next')
    assert any(n=='sendMessage' and 'photo.jpg' in kw.get('text','') for n,kw in bot.calls)
    q.message.message_id=500
    bot.edit_text_error=None
    await handlers._show_random_step(q,state['session_id'],'prev')
    assert sum(n=='copyMessage' for n,_ in bot.calls)==2
    assert await db.col('feed_history').count_documents({'user_id':7})==2


async def test_long_caption_copy_cleanup_if_control_send_fails():
    bot=CopyBot(send_error=TimedOut())
    with pytest.raises(TimedOut):
        await rich_cards.show_cover_card(bot,7,'Long statistics and balance 122',MARKUP,
            row(caption='😃'*500+'\nsecond\nthird'),edit_message_id=42)
    assert [n for n,_ in bot.calls]==['copyMessage','sendMessage','deleteMessage']
    assert bot.calls[-1][1]['message_id']==801
    assert not any(n=='deleteMessage' and kw['message_id']==42 for n,kw in bot.calls)
