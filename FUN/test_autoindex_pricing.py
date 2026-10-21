"""Bot 3 live-indexing and media-pricing regressions. Completely offline.
No real source accounts, tokens, provider requests or MongoDB credentials.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import mongomock_motor
import config, db, handlers, runtime, scanner, pricing, persona
from test_repairs import update, context, Q, Bot, collection, source_message, Source


class LiveSource(Source):
    def __init__(self, posts):
        super().__init__(posts)
        self.connected = True
        self.registrations = []
    async def get_messages(self, cid, limit=1):
        return self.posts[-limit:][::-1]
    def is_connected(self):
        return self.connected
    async def connect(self):
        self.connected = True
    async def is_user_authorized(self):
        return True
    async def disconnect(self):
        self.connected = False
    def add_event_handler(self, fn, builder):
        self.registrations.append((fn, builder))
    def remove_event_handler(self, fn):
        self.registrations = [(f,b) for f,b in self.registrations if f is not fn]


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    await runtime.stop_jobs()
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(), 'autoindex_price_tests')
    await db.col('collections').create_index('collection_id', unique=True)
    await db.col('users').create_index('user_id', unique=True)
    await db.col('unlocks').create_index([('user_id',1),('collection_id',1)], unique=True)
    monkeypatch.setattr(config,'OWNER_ID',7)
    monkeypatch.setattr(config,'ADMIN_IDS',[7])
    monkeypatch.setattr(config,'REQUIRED_CHANNEL_IDS',[])
    monkeypatch.setattr(scanner,'_prepare_cover',AsyncMock(return_value={'hosted':True,'ready':True}))
    yield
    await runtime.stop_jobs()


def source_drop(cover_id, files=1):
    return [source_message(cover_id,'Cover title\nQuality\nDetails',kind='photo')] + [
        source_message(cover_id+i+1) for i in range(files)]


async def finish_scan(cid=-100):
    task = runtime._scans.get(cid)
    if task:
        await task


@pytest.mark.parametrize('count,expected',[(0,2),(1,2),(18,2),(19,2),(20,5),(21,5),
    (49,5),(50,5),(51,7),(99,7),(100,7),(101,10),(150,10)])
def test_all_price_boundaries(count,expected):
    assert pricing.collection_price({'files':[{'message_id':i+1} for i in range(count)],
                                      'price':None,'views':999999})==expected


@pytest.mark.parametrize('price',[0,1,4,15])
def test_explicit_owner_price_remains(price):
    assert pricing.collection_price({'price':price,'file_count':250})==price


def test_media_not_views_cover_or_stale_count():
    doc={'files':[{'message_id':11},{'message_id':11},{'message_id':12}],
         'file_count':999,'cover':{'album_message_ids':[1,2,3]},'views':10**9}
    assert pricing.media_count(doc)==2
    assert pricing.collection_price(doc)==2


async def test_restart_catchup_from_162_to_183_without_manual_rescan():
    source=LiveSource([m for i in range(162) for m in source_drop(1+2*i)])
    await db.upsert_channel_mapping(-100,'movies')
    await scanner.scan_channel(source,-100,'movies',prepare_covers=False)
    assert await db.count_collections('movies')==162
    # 21 new complete drops arrive while the process is away.
    source.posts.extend([m for i in range(21) for m in source_drop(325+2*i)])
    await runtime.replace_userbot(source)
    await runtime.poll_once()
    await finish_scan()
    assert await db.count_collections('movies')==183
    assert (await db.col('scan_state').find_one({'_id':-100}))['cursor']==366
    # A second tick/refresh neither duplicates collections nor their files.
    await runtime.poll_once()
    assert not runtime.scan_busy(-100)
    assert (await db.get_collection('c-100_325'))['file_count']==1


async def test_new_files_attach_to_last_cover_and_change_tier_automatically():
    source=LiveSource(source_drop(1,18))
    await db.upsert_channel_mapping(-100,'movies')
    await scanner.scan_channel(source,-100,'movies',prepare_covers=False)
    cid='c-100_1'
    assert pricing.collection_price(await db.get_collection(cid))==2
    await db.insert_unlock(7,cid)
    source.posts.extend([source_message(20),source_message(21)])
    runtime.userbot=source
    await runtime.poll_once(); await finish_scan()
    doc=await db.get_collection(cid)
    assert doc['file_count']==20 and pricing.collection_price(doc)==5
    assert await db.is_unlocked(7,cid)
    assert await db.count_collections()==1  # videos do not become 20 fake drops
    await scanner.scan_channel(source,-100,'movies',prepare_covers=False)
    assert (await db.get_collection(cid))['file_count']==20


async def test_broken_first_mapping_does_not_block_later_channels():
    await db.upsert_channel_mapping(-200,'a_broken')
    await db.upsert_channel_mapping(-100,'z_healthy')
    for cid in (-200,-100):
        await db.col('scan_state').insert_one({'_id':cid,'cursor':1})
    source=LiveSource(source_drop(2))
    original=source.get_messages
    async def get(cid,limit=1):
        if cid==-200: raise ValueError('offline inaccessible source')
        return await original(cid,limit)
    source.get_messages=get
    runtime.userbot=source
    await runtime.poll_once(); await finish_scan()
    assert await db.get_collection('c-100_2')


async def test_string_session_missing_entity_recovers_from_dialogs():
    source=LiveSource(source_drop(1))
    entity=object()
    source.get_input_entity=AsyncMock(side_effect=ValueError('missing access hash'))
    async def dialogs():
        yield NS(id=-100,input_entity=entity)
    source.iter_dialogs=dialogs
    assert await runtime.resolve_channel(source,-100) is entity
    assert await runtime.resolve_channel(source,-100) is entity
    source.get_input_entity.assert_awaited_once()


async def test_live_events_debounce_and_ignore_unmapped_sources(monkeypatch):
    source=LiveSource(source_drop(1))
    await db.upsert_channel_mapping(-100,'movies')
    await db.col('scan_state').insert_one({'_id':-100,'cursor':0})
    await runtime.replace_userbot(source)
    assert len(source.registrations)==2
    monkeypatch.setattr(runtime,'EVENT_DEBOUNCE_SECONDS',0)
    await runtime._on_source_event(NS(chat_id=-999))
    assert not runtime._debounces
    await runtime._on_source_event(NS(chat_id=-100))
    await runtime._on_source_event(NS(chat_id=-100))
    assert len(runtime._debounces)==1
    await asyncio.gather(*list(runtime._debounces.values()))
    await finish_scan()
    assert await db.count_collections()==1
    await runtime.disconnect_userbot()
    assert not source.registrations


async def test_post_arrives_during_scan_is_not_lost(monkeypatch):
    source=LiveSource(source_drop(1))
    await db.upsert_channel_mapping(-100,'movies')
    runtime.userbot=source
    original=scanner.scan_channel
    calls=0
    async def scan(*args,**kwargs):
        nonlocal calls
        calls+=1
        result=await original(*args,**kwargs)
        if calls==1:
            source.posts.extend(source_drop(3))
            runtime.schedule_scan(-100)
        return result
    monkeypatch.setattr(scanner,'scan_channel',scan)
    await runtime.schedule_scan(-100)
    assert calls==2 and await db.count_collections()==2


async def test_cover_upload_never_holds_up_source_indexing(monkeypatch):
    source=LiveSource(source_drop(1))
    await db.upsert_channel_mapping(-100,'movies')
    runtime.userbot=source
    blocked=asyncio.Event()
    entered=asyncio.Event()
    async def prepare(*a,**kw):
        entered.set(); await blocked.wait()
        return {'hosted':True,'ready':True}
    monkeypatch.setattr(scanner,'_prepare_cover',prepare)
    await runtime.schedule_scan(-100)
    await asyncio.wait_for(entered.wait(),1)
    assert not runtime.scan_busy(-100)
    source.posts.extend(source_drop(3))
    await runtime.poll_once(); await finish_scan()
    assert await db.count_collections()==2
    blocked.set()
    if runtime._cover_task: await runtime._cover_task


async def test_initial_unscanned_mapping_still_requires_explicit_history_scan():
    source=LiveSource(source_drop(1))
    runtime.userbot=source
    await db.upsert_channel_mapping(-100,'movies')
    await runtime.poll_once()
    assert not runtime.scan_busy(-100) and await db.count_collections()==0


async def test_existing_index_with_missing_scan_state_can_catch_up():
    source=LiveSource(source_drop(1)+source_drop(3))
    await db.upsert_channel_mapping(-100,'movies')
    runtime.userbot=source
    old = collection(cid='c-100_1',mids=(2,))
    old['cover']['message_id'] = 1
    old['cover']['album_message_ids'] = [1]
    await db.upsert_collection(old)
    await runtime.poll_once(); await finish_scan()
    assert await db.get_collection('c-100_3')


async def test_reconnect_disconnected_userbot():
    source=LiveSource([]); source.connected=False
    runtime.userbot=source
    assert await runtime._ensure_userbot()
    assert source.connected


async def test_retry_saved_session_when_initial_startup_connection_failed(monkeypatch):
    import userbot_setup
    source=LiveSource([])
    build=AsyncMock(return_value=source)
    monkeypatch.setattr(userbot_setup,'build_client',build)
    assert await runtime._ensure_userbot()
    assert runtime.userbot is source
    build.assert_awaited_once()


@pytest.mark.parametrize('n,price',[(19,2),(20,5),(50,5),(51,7),(100,7),(101,10)])
async def test_display_matches_actual_charge_and_restream_is_free(n,price):
    doc=collection(mids=tuple(range(11,11+n)))
    await db.upsert_collection(doc)
    await db.create_user(7)
    await db.add_tokens(7,100,'offline_test')
    bot=Bot(); q=Q(uid=7,bot=bot)
    caption=await handlers._feed_text(7,doc)
    assert f'<code>{price} tokens</code>' in caption
    await handlers._serialized_unlock(update(),context(bot),q,doc['collection_id'],False)
    assert (await db.get_user(7))['tokens']==100-price
    await handlers._serialized_unlock(update(),context(bot),q,doc['collection_id'],True)
    assert (await db.get_user(7))['tokens']==100-price


async def test_not_enough_tokens_does_not_deliver_or_spend():
    doc=collection(mids=tuple(range(20)))
    await db.upsert_collection(doc)
    await db.create_user(7); await db.add_tokens(7,4,'offline_test')
    bot=Bot(); q=Q(uid=7,bot=bot)
    await handlers._serialized_unlock(update(),context(bot),q,doc['collection_id'],False)
    assert (await db.get_user(7))['tokens']==4 and not bot.calls
    assert not await db.is_unlocked(7,doc['collection_id'])


async def test_failed_delivery_refunds_tier_amount():
    from telegram.error import BadRequest
    doc=collection(mids=tuple(range(11,31)))
    await db.upsert_collection(doc)
    await db.create_user(7); await db.add_tokens(7,10,'offline_test')
    bot=Bot(error=BadRequest('message to copy not found')); q=Q(uid=7,bot=bot)
    await handlers._serialized_unlock(update(),context(bot),q,doc['collection_id'],False)
    assert (await db.get_user(7))['tokens']==10
    assert not await db.is_unlocked(7,doc['collection_id'])


async def test_setprice_auto_restores_dynamic_tier():
    doc=collection(mids=tuple(range(11,31)))
    doc['price']=9
    await db.upsert_collection(doc)
    await handlers.cmd_setprice(update(),context(args=[doc['collection_id'],'auto']))
    doc=await db.get_collection(doc['collection_id'])
    assert doc['price'] is None and pricing.collection_price(doc)==5


async def test_categories_uses_current_db_count_and_has_refresh():
    await db.upsert_collection(collection())
    q=Q()
    await handlers._show_categories(q)
    markup=q.message.edits[-1][1]['reply_markup'].to_dict()['inline_keyboard']
    assert any(b['text']=='movies (1)' for row in markup for b in row)
    assert any(b['callback_data']=='cats' for row in markup for b in row)
    text=q.message.edits[-1][0]
    assert '20–50' in text and '101+' in text and 'Every drop costs' not in text


def test_help_shows_new_prices():
    text=persona.help_card()
    assert '51–100' in text and '10 tokens' in text and '2 tokens' in text
