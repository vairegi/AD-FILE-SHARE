"""ROOT Bots 1/2 regression tests: durable staged, group-aware, bounded indexing.
No production credentials. Real-DB opt-in ONLY uses localhost:27019 and a
fixed disposable root_index_sandbox_tests database.
Run: pytest -q --asyncio-mode=auto test_root_index.py
"""
import os
os.environ['MONGO_URI'] = ''
os.environ.setdefault('BOT1_TOKEN', '111111111:offline-root-one')
os.environ.setdefault('BOT2_TOKEN', '222222222:offline-root-two')
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import json
import httpx
import pytest
import pytest_asyncio
import mongomock_motor
from telegram import Bot, Message
import config, db, root_index as idx, root_dispatch as dispatch, main, bot1_admin as admin


@pytest_asyncio.fixture(autouse=True)
async def isolate(monkeypatch):
    await idx.stop(); await dispatch.stop()
    old_db, old_client = db._db, db._client
    client = None
    if os.getenv('ROOT_LOCAL_MONGO_TEST') == '1':
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient('mongodb://127.0.0.1:27019', serverSelectionTimeoutMS=3000)
        await client.drop_database('root_index_sandbox_tests')
        db._db = client['root_index_sandbox_tests']
    else:
        # mongomock's bulk builder predates PyMongo's optional sort argument.
        # Adapt only that mock signature; real-Mongo runs use the actual driver.
        import inspect
        from mongomock.collection import BulkOperationBuilder
        original = BulkOperationBuilder.add_update
        if 'sort' not in inspect.signature(original).parameters:
            def compatible(self, *args, **kwargs):
                kwargs.pop('sort', None)
                return original(self, *args, **kwargs)
            monkeypatch.setattr(BulkOperationBuilder, 'add_update', compatible)
        db._db = mongomock_motor.AsyncMongoMockClient()['root_index_test']
    db._client = client
    await db._db.raw.create_index([('category', 1), ('message_id', 1)], unique=True)
    await db._db.files.create_index([('category', 1), ('db_message_id', 1)], unique=True)
    await db._db.files.create_index('file_id', unique=True, sparse=True)
    await idx.ensure_indexes(); await dispatch.ensure_indexes()
    await db._db.categories.insert_many([
        {'key':'demo','enabled':True,'db_channel_id':-100},
        {'key':'other','enabled':True,'db_channel_id':-200}])
    monkeypatch.setattr(config, 'ADMIN_IDS', [999])
    monkeypatch.setattr(config, 'BOT1_WEBHOOK_SECRET', 'one')
    monkeypatch.setattr(config, 'BOT2_WEBHOOK_SECRET', 'two')
    yield
    await dispatch.stop(); await idx.stop()
    if client:
        await client.drop_database('root_index_sandbox_tests'); client.close()
    db._db, db._client = old_db, old_client


def post(mid, kind='video', caption='', **kw):
    return dict(message_id=mid,kind=kind,caption=caption,**kw)


async def send(*entries, cat='demo', ranks=None):
    return await idx.stage(list(entries), cat, ranks=ranks)


async def drain():
    job = await db._db.root_index_jobs.find_one({}, sort=[('ready_at', 1)])
    if job:
        await db._db.root_index_jobs.update_one({'_id':job['_id']}, {'$set':{'ready_at':0}})
    return await idx.drain_once()


async def items(cat='demo'):
    return await db._db.files.find({'category':cat}).sort('db_message_id',1).to_list(None)


async def test_open_group_is_never_published_on_timer():
    await send(post(1,'cover','C1'),post(2))
    for _ in range(5):assert await drain()==0
    assert await items()==[]
    assert await db._db.raw.count_documents({'index_pending':True})==2


async def test_timer_mid_cover5_commits_only_cover1_through4():
    data=[]
    for n in range(5):data += [post(1+n*3,'cover',f'C{n+1}'),post(2+n*3),post(3+n*3)]
    # Cover 5 has received one video, but its second video is still uploading.
    await send(*data[:-1])
    assert await drain()==4
    assert [d['db_message_id'] for d in await items()]==[1,4,7,10]
    assert await db._db.files.find_one({'db_message_id':13}) is None
    await send(post(15),post(16,'cover','C6'))
    await drain()
    fifth=await db._db.files.find_one({'db_message_id':13})
    assert [v['db_message_id'] for v in fifth['videos']]==[14,15]


async def test_atomic_group_has_all_videos_and_subtitles():
    await send(post(1,'cover','Caption\nall lines'),post(2),post(3),post(4,'srt'),post(5,'cover','Next'))
    await drain()
    d=(await items())[0]
    assert d['caption']=='Caption\nall lines'
    assert [v['db_message_id'] for v in d['videos']]==[2,3]
    assert d['srts'][0]['db_message_id']==4 and d['index_complete']


async def test_next_cover_does_not_hide_missing_delayed_media():
    await send(post(1,'cover'),post(2),post(4,'cover'))
    assert await drain()==0
    await send(post(3))
    assert await drain()==1
    assert len((await items())[0]['videos'])==2


async def test_text_message_ids_are_accounted_for_without_becoming_media():
    await send(post(1,'cover'),post(2),post(3,'skip','text'),post(4,'cover'))
    assert await drain()==1
    assert len((await items())[0]['videos'])==1


async def test_flush_confirms_only_snapshot_and_newer_arrivals_remain_open():
    await send(post(1,'cover'),post(2))
    assert await idx.seal('demo')==2
    assert await drain()==1
    await send(post(3))
    await drain()
    d=(await items())[0]
    assert [v['db_message_id'] for v in d['videos']]==[2]
    assert not d['index_complete']
    await send(post(4,'cover'))
    await drain()
    assert [v['db_message_id'] for v in (await items())[0]['videos']]==[2,3]


async def test_explicit_flush_can_confirm_a_known_deleted_source_gap():
    await send(post(1,'cover'),post(3))
    await drain(); assert not await items()
    await idx.seal('demo'); await drain()
    assert (await items())[0]['videos'][0]['db_message_id']==3


async def test_duplicate_replays_are_idempotent():
    entries=[post(1,'cover'),post(2),post(3,'cover')]
    for _ in range(20):await send(*entries)
    await drain()
    assert len(await items())==1
    assert await db._db.raw.count_documents({})==3


async def test_stale_source_event_never_overwrites_new_caption():
    await send(post(1,'cover','new'),ranks=[idx.source_rank(10,20,True)])
    await send(post(1,'cover','old'),ranks=[idx.source_rank(9,100,True)])
    assert (await db._db.raw.find_one({'message_id':1}))['caption']=='new'


async def test_old_history_scan_cannot_clobber_new_live_edit():
    await send(post(1,'cover','live'),ranks=[idx.source_rank(10,1,True)])
    await send(post(1,'cover','scan'),ranks=[idx.source_rank(10,live=False)])
    assert (await db._db.raw.find_one({'message_id':1}))['caption']=='live'


async def test_older_fence_never_overwrites_newer_committed_block():
    one=db.group_items([post(1,'cover','old'),post(2)],'demo')[0]
    two=db.group_items([post(1,'cover','new'),post(2),post(3)],'demo')[0]
    assert await idx.commit(two,'demo',100)
    assert not await idx.commit(one,'demo',99)
    saved=(await items())[0]
    assert saved['caption']=='new' and len(saved['videos'])==2


async def test_reconciliation_never_deletes_unrepresented_items_or_published_state():
    await db.upsert_item({'category':'demo','file_id':'legacy_id','db_message_id':90,'videos':[]})
    await db.mark_posted(90,123,category='demo')
    await send(post(1,'cover'),post(2))
    await db.rebuild_items('demo')
    d=await db._db.files.find_one({'db_message_id':90})
    assert d['file_id']=='legacy_id' and d['posted'] and d['post_message_id']==123


async def test_preserves_stable_legacy_id_posted_metadata_tokens_and_users():
    await db._db.files.insert_one({'category':'demo','file_id':'f1','db_message_id':1,
        'posted':True,'post_message_id':55,'created_at':12,'custom':'keep'})
    await db._db.tokens.insert_one({'token':'private-test','used':False})
    await db._db.users.insert_one({'user_id':7,'premium_until':999999})
    await send(post(1,'cover'),post(2),post(3,'cover'));await drain()
    d=(await items())[0]
    assert (d['file_id'],d['posted'],d['post_message_id'],d['created_at'],d['custom'])==('f1',True,55,12,'keep')
    assert (await db._db.tokens.find_one({}))['used'] is False
    assert (await db._db.users.find_one({}))['premium_until']==999999


async def test_categories_do_not_mix():
    await send(post(1,'cover','A'),post(2),post(3,'cover'))
    await send(post(1,'cover','B'),post(2),post(3,'cover'),cat='other')
    await drain();await drain()
    assert (await items('demo'))[0]['caption']=='A'
    assert (await items('other'))[0]['caption']=='B'


async def test_per_pass_block_limit_and_global_single_heavy_worker(monkeypatch):
    monkeypatch.setattr(idx,'MAX_BLOCKS_PER_PASS',2)
    await send(*[post(i,'cover' if i%2 else 'video') for i in range(1,12)])
    assert await drain()==2
    await drain();await drain()
    assert len(await items())==5
    active=peak=0
    async def job(n):
        nonlocal active,peak
        async with idx.heavy_slot():
            active+=1;peak=max(peak,active);await asyncio.sleep(.001);active-=1
    await asyncio.gather(*(job(i) for i in range(30)))
    assert peak==1


async def test_memory_safety_group_cap_never_writes_partial(monkeypatch):
    monkeypatch.setattr(idx,'MAX_GROUP_MESSAGES',3)
    await send(post(1,'cover'),post(2),post(3),post(4),post(5,'cover'))
    await drain()
    assert not await items()
    assert 'too large' in (await db._db.root_index_state.find_one({'_id':'demo'}))['waiting_reason']


async def test_restart_keeps_staging_and_jobs():
    await send(post(1,'cover'),post(2),post(3,'cover'))
    await idx.stop()
    await drain()
    assert len(await items())==1


async def test_raw_job_write_gap_repaired_after_failure(monkeypatch):
    real=idx.notify
    monkeypatch.setattr(idx,'notify',AsyncMock(side_effect=RuntimeError('database fail')))
    with pytest.raises(RuntimeError):await send(post(1,'cover'),post(2),post(3,'cover'))
    assert await db._db.raw.count_documents({})==3
    monkeypatch.setattr(idx,'notify',real)
    await idx.recover_notification();await drain()
    assert len(await items())==1


async def test_write_failure_keeps_pending_whole_block(monkeypatch):
    await send(post(1,'cover'),post(2),post(3,'cover'))
    real=idx.commit
    monkeypatch.setattr(idx,'commit',AsyncMock(side_effect=RuntimeError('write failed')))
    await drain();assert not await items()
    assert await db._db.raw.count_documents({'index_pending':True})==3
    monkeypatch.setattr(idx,'commit',real);await drain()
    assert len(await items())==1


async def test_new_arrival_during_pass_is_retained_for_next_generation(monkeypatch):
    await send(post(1,'cover'),post(2),post(3,'cover'))
    job=await db._db.root_index_jobs.find_one({'_id':'demo'})
    await send(post(4),post(5,'cover'))
    async with idx.lease() as fence:
        await idx.process_job(job,fence)
    await drain()
    assert [d['db_message_id'] for d in await items()]==[1,3]


async def test_bootstrap_only_legacy_tail_and_no_destructive_reset():
    await db._db.raw.insert_many([dict(category='demo',**post(i,'cover' if i%2 else 'video')) for i in range(1,8)])
    await db._db.files.insert_many([{'category':'demo','db_message_id':i,'file_id':f'f{i}',
        'posted':i==1,'videos':[{'db_message_id':i+1}]} for i in (1,3,5)])
    await idx.bootstrap()
    job=await db._db.root_index_jobs.find_one({'_id':'demo'})
    assert job['dirty_from']==5
    await drain()
    assert (await db._db.files.find_one({'db_message_id':1}))['posted']
    assert await db._db.files.count_documents({})==3


async def test_unpublished_legacy_open_group_not_in_posting_queue():
    await db._db.files.insert_one({'category':'demo','file_id':'demo_f1','db_message_id':1,'posted':False})
    await send(post(1,'cover'),post(2));await drain()
    assert await db.next_unposted('demo') is None
    await send(post(3,'cover'));await drain()
    assert (await db.next_unposted('demo'))['db_message_id']==1


async def test_status_reports_open_group_and_admin_seal():
    await send(post(1,'cover'),post(2));await drain()
    assert 'open cover' in await idx.status('demo')
    u=NS(effective_user=NS(id=999),message=NS(reply_text=AsyncMock()))
    await admin.cmd_flushindex(u,NS(args=['demo']))
    assert 'Owner-confirmed' in u.message.reply_text.call_args.args[0]
    await drain();assert len(await items())==1


def payload(mid=1, kind='video', cid=-100, update_id=None):
    message={'message_id':mid,'date':100,'chat':{'id':cid,'type':'channel'},'caption':'caption'}
    if kind=='cover':message['photo']=[{'file_id':'PHOTO','file_unique_id':'UNIQ','width':10,'height':10}]
    elif kind=='video':message['video']={'file_id':'VID','file_unique_id':'VIDU','width':10,'height':10,'duration':20}
    else:message['text']='non-media'
    return {'update_id':mid if update_id is None else update_id,'channel_post':message}


@pytest.mark.parametrize('kind',['cover','video','skip'])
async def test_channel_webhook_persists_before_ack(kind):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as c:
        r=await c.post('/bot1/webhook',json=payload(kind=kind),headers={'X-Telegram-Bot-Api-Secret-Token':'one'})
    assert r.status_code==200
    assert (await db._db.raw.find_one({}))['kind']==kind
    assert not await items()


async def test_webhook_failure_returns_retry_not_false_success(monkeypatch):
    monkeypatch.setattr(idx,'stage_message',AsyncMock(side_effect=RuntimeError('storage unavailable')))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as c:
        r=await c.post('/bot1/webhook',json=payload(),headers={'X-Telegram-Bot-Api-Secret-Token':'one'})
    assert r.status_code==503


async def test_bad_secret_and_oversized_or_malformed_request():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as c:
        assert (await c.post('/bot1/webhook',json=payload())).status_code==403
        headers={'X-Telegram-Bot-Api-Secret-Token':'one'}
        assert (await c.post('/bot1/webhook',content='not-json',headers=headers)).status_code==400
        assert (await c.post('/bot1/webhook',content='x'*(dispatch.MAX_BODY_BYTES+1),headers=headers)).status_code==413


async def test_ingress_full_returns_503_without_accepting():
    slots=dispatch.ingress_slots()
    for _ in range(dispatch.INGRESS_LIMIT):await slots.acquire()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as c:
            r=await c.post('/bot1/webhook',json=payload(),headers={'X-Telegram-Bot-Api-Secret-Token':'one'})
        assert r.status_code==503 and await db._db.raw.count_documents({})==0
    finally:
        for _ in range(dispatch.INGRESS_LIMIT):slots.release()


async def test_generic_update_is_durable_and_deduplicated():
    p={'update_id':44,'message':{'message_id':1,'date':1,'from':{'id':7,'first_name':'Test','is_bot':False},
       'chat':{'id':7,'type':'private'},'text':'/help'}}
    await dispatch.enqueue('bot2',p);await dispatch.enqueue('bot2',p)
    app=NS(bot=Bot('123456:offline'),process_update=AsyncMock())
    assert await dispatch.drain_once('bot2',app)
    assert not await dispatch.drain_once('bot2',app)
    app.process_update.assert_awaited_once()
    d=await db._db.root_update_inbox.find_one({})
    assert d['state']=='done' and 'payload' not in d and 'expires_at' in d


async def test_uncertain_interrupted_command_is_not_blindly_replayed():
    await db._db.root_update_inbox.insert_one({'_id':'bot1:99','bot':'bot1','state':'running',
        'ready_at':0,'lease_until':0,'payload':{'update_id':99}})
    app=NS(process_update=AsyncMock())
    assert not await dispatch.drain_once('bot1',app)
    assert (await db._db.root_update_inbox.find_one({}))['state']=='review'
    app.process_update.assert_not_awaited()


async def test_background_dispatch_has_fixed_worker_count(monkeypatch):
    # Do not run handlers: empty persisted queue.
    app=NS(bot=Bot('123456:offline'),process_update=AsyncMock())
    dispatch.start(app,app);dispatch.start(app,app)
    assert len(dispatch._tasks)==dispatch.WORKERS_PER_BOT*2+1
    await asyncio.sleep(.02);await dispatch.stop()
    assert not dispatch._tasks


async def test_health_db_failure_is_503_and_root_probe_is_valid(monkeypatch):
    monkeypatch.setattr(db,'ping',AsyncMock(side_effect=RuntimeError('offline')))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as c:
        assert (await c.get('/health')).status_code==503
        assert (await c.head('/')).status_code==200


async def test_explicit_rebuild_streams_not_all_raw(monkeypatch):
    await send(post(1,'cover'),post(2),post(3,'cover'),post(4))
    monkeypatch.setattr(db,'all_raw',AsyncMock(side_effect=AssertionError('full materialization forbidden')))
    assert await db.rebuild_items('demo')==2


async def test_large_history_new_live_block_touches_only_tail(monkeypatch):
    # 5,000 existing metadata rows; live path must never call full rebuild.
    legacy=[dict(category='demo',**post(i,'cover' if i%2 else 'video')) for i in range(1,5001)]
    await db._db.raw.insert_many(legacy)
    await idx.ensure_state('demo')
    monkeypatch.setattr(db,'rebuild_items',AsyncMock(side_effect=AssertionError('live rebuild forbidden')))
    await send(post(5001,'cover'),post(5002),post(5003,'cover'))
    await drain()
    assert [d['db_message_id'] for d in await items()]==[5001]


async def test_global_lease_prevents_second_owner_and_recovers_expiry():
    await db._db.root_index_clock.insert_one({'_id':'lease','owner':'other-instance','until':db.now()+100})
    async with idx.lease() as fence:assert fence is None
    await db._db.root_index_clock.update_one({'_id':'lease'},{'$set':{'until':0}})
    async with idx.lease() as fence:assert isinstance(fence,int)


async def test_delayed_cover_before_leading_media_never_creates_orphan_item():
    await send(post(10),post(11),post(12,'cover'))
    await drain();assert not await items()
    await send(post(9,'cover','Delayed header'))
    await drain()
    assert [d['db_message_id'] for d in await items()]==[9]
    assert len((await items())[0]['videos'])==2


async def test_newer_posted_record_survives_stale_block_attempt():
    await db._db.files.insert_one({'category':'demo','db_message_id':1,'file_id':'demo_f1',
        '_index_fence':200,'index_complete':True,'posted':False,'videos':[{'db_message_id':2}]})
    await idx.block('demo',1,'stale',199)
    assert (await items())[0]['index_complete'] is True


async def test_lost_lease_refuses_to_publish():
    await send(post(1,'cover'),post(2),post(3,'cover'))
    async with idx.lease() as fence:
        await db._db.root_index_clock.update_one({'_id':'lease'},{'$set':{'owner':'new-owner','until':db.now()+100}})
        with pytest.raises(RuntimeError):
            await idx.process_block('demo',1,1000,fence)
    assert not await items()


async def test_new_edit_rebuilds_whole_closed_block_without_duplicate_videos():
    await send(post(1,'cover','Old'),post(2),post(3,'cover'));await drain()
    await send(post(1,'cover','New'),ranks=[idx.source_rank(20,200,True)])
    await drain()
    assert len(await items())==1
    assert (await items())[0]['caption']=='New'
    assert len((await items())[0]['videos'])==1


async def test_private_webhook_queues_without_executing_handler(monkeypatch):
    process=AsyncMock()
    monkeypatch.setattr(main,'bot2',NS(bot=main.bot2.bot,process_update=process))
    p={'update_id':100,'message':{'message_id':1,'date':1,'chat':{'id':7,'type':'private'},'text':'/help'}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as c:
        r=await c.post('/bot2/webhook',json=p,headers={'X-Telegram-Bot-Api-Secret-Token':'two'})
    assert r.status_code==200 and await db._db.root_update_inbox.count_documents({'state':'pending'})==1
    process.assert_not_awaited()


async def test_unknown_channel_is_ignored_without_job_or_raw_metadata():
    message=Message.de_json(payload(cid=-999)['channel_post'],None)
    assert not await idx.stage_message(message,1)
    assert await db._db.raw.count_documents({})==0


async def test_edited_channel_webhook_rejects_delayed_old_revision():
    original=payload(mid=1,kind='cover',update_id=10)
    edited=payload(mid=1,kind='cover',update_id=20)
    edited['edited_channel_post']=edited.pop('channel_post')
    edited['edited_channel_post'].update(caption='Edited',edit_date=200)
    headers={'X-Telegram-Bot-Api-Secret-Token':'one'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as c:
        assert (await c.post('/bot1/webhook',json=edited,headers=headers)).status_code==200
        assert (await c.post('/bot1/webhook',json=original,headers=headers)).status_code==200
    assert (await db._db.raw.find_one({}))['caption']=='Edited'


async def test_status_and_flush_commands_enforce_admin_gate():
    u=NS(effective_user=NS(id=123),message=NS(reply_text=AsyncMock()))
    u.effective_message=u.message
    await admin.cmd_flushindex(u,NS(args=['demo']))
    assert not await db._db.root_index_state.find_one({})
    assert 'only by admins' in u.message.reply_text.call_args.args[0]


async def test_scanner_freezes_history_and_leaves_last_cover_unpublished(monkeypatch):
    import scanner,telethon,telethon.sessions
    msgs=[NS(id=i,message=f'm{i}',photo=True if i in (1,4) else None,
        video=True if i in (2,5) else None,document=None,animation=None,
        date=datetime.fromtimestamp(100,timezone.utc),edit_date=None) for i in range(1,6)]
    class Client:
        async def connect(self):pass
        async def disconnect(self):pass
        async def is_user_authorized(self):return True
        async def get_messages(self,channel,limit):return [NS(id=5)]
        def iter_messages(self,channel,reverse,max_id):
            assert reverse and max_id==6
            async def iterator():
                for m in msgs:yield m
            return iterator()
    monkeypatch.setattr(config,'API_ID',1)
    monkeypatch.setattr(config,'API_HASH','dummy')
    monkeypatch.setattr(config,'SESSION_USER','dummy')
    monkeypatch.setattr(telethon,'TelegramClient',lambda *a,**kw:Client())
    monkeypatch.setattr(telethon.sessions,'StringSession',lambda s:None)
    result=await scanner.scan_channel(-100,'demo')
    assert result['scanned']==4 and result['items']==1
    assert [d['db_message_id'] for d in await items()]==[1]
    assert await db._db.raw.count_documents({})==5
    await idx.seal('demo');await drain()
    assert [d['db_message_id'] for d in await items()]==[1,4]


async def test_stage_batch_bound_is_enforced():
    with pytest.raises(ValueError):
        await idx.stage([post(i) for i in range(101)],'demo')


async def test_worker_shutdown_cancels_and_restart_recovers_open_work(monkeypatch):
    monkeypatch.setattr(idx,'TICK_SECONDS',.005)
    await send(post(1,'cover'),post(2),post(3,'cover'))
    await db._db.root_index_jobs.update_one({'_id':'demo'},{'$set':{'ready_at':0}})
    idx.start();idx.start()
    for _ in range(100):
        if len(await items())==1:break
        await asyncio.sleep(.005)
    await idx.stop()
    assert len(await items())==1 and idx._worker is None


async def test_main_lifespan_starts_and_stops_fixed_workers(monkeypatch):
    fake=NS(bot=NS(set_webhook=AsyncMock()),initialize=AsyncMock(),start=AsyncMock(),
        stop=AsyncMock(),shutdown=AsyncMock(),job_queue=None)
    monkeypatch.setattr(main,'bot1',fake);monkeypatch.setattr(main,'bot2',fake)
    monkeypatch.setattr(config,'RENDER_EXTERNAL_URL','')
    monkeypatch.setattr(db,'connect',AsyncMock());monkeypatch.setattr(db,'close',AsyncMock())
    monkeypatch.setattr(main,'schedule_daily',AsyncMock());monkeypatch.setattr(main,'schedule_broadcast_sweeper',lambda app:None)
    monkeypatch.setattr(main,'schedule_sweeper',lambda app:None);monkeypatch.setattr(main,'_set_commands',AsyncMock())
    async with main.lifespan(main.app):
        assert idx._worker is not None and len(dispatch._tasks)==5
    assert not dispatch._tasks and idx._worker is None
    assert fake.stop.await_count==2 and fake.shutdown.await_count==2


async def test_caption_edit_updates_only_its_genre_membership():
    await db._db.genres.insert_many([
        {'category':'demo','genre':'action','matched':[],'scanned':True},
        {'category':'demo','genre':'romance','matched':[],'scanned':True}])
    await send(post(1,'cover','#Action'),post(2),post(3,'cover'));await drain()
    assert 'demo_f1' in (await db._db.genres.find_one({'genre':'action'}))['matched']
    await send(post(1,'cover','#Romance'),ranks=[idx.source_rank(100,1,True)])
    await drain()
    assert 'demo_f1' not in (await db._db.genres.find_one({'genre':'action'}))['matched']
    assert 'demo_f1' in (await db._db.genres.find_one({'genre':'romance'}))['matched']


async def test_scans_cannot_overlap_the_same_shared_session():
    import scanner
    await scanner._scan_lock.acquire()
    try:
        with pytest.raises(RuntimeError,match='already running'):
            await scanner.scan_channel(-100,'demo')
    finally:scanner._scan_lock.release()


async def test_final_group_completes_only_after_deferred_second_chunk():
    await send(post(1,'cover'),post(2),post(3,'srt'),post(4,'cover'),post(5))
    await drain()
    assert len(await items())==1
    # A long timer expiration still cannot finalize cover 4.
    for _ in range(3):await drain()
    assert await db._db.files.find_one({'db_message_id':4}) is None
    await send(post(6),post(7,'srt'),post(8,'cover'))
    await drain()
    second=await db._db.files.find_one({'db_message_id':4})
    assert [v['db_message_id'] for v in second['videos']]==[5,6]
    assert second['srts'][0]['db_message_id']==7
