"""Offline regressions for durable Bot API ingestion. No production connections."""
import asyncio
import io
import json
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import mongomock_motor
import pytest
import pytest_asyncio
from PIL import Image
from telegram import Message, Update, Bot as TelegramBot
from telegram.request import BaseRequest

import config, db, handlers, live_index as live, main, runtime, scanner, rich_cards, covers
from test_repairs import source_message, Source, collection, update, context, Bot

CID = -10012345


def payload(mid, kind='video', caption=None, album=None, cid=CID):
    p = {'message_id': mid, 'date': 1700000000 + mid,
         'chat': {'id': cid, 'type': 'channel', 'title': 'offline-source'}}
    if caption is not None:
        p['caption'] = caption
    if album:
        p['media_group_id'] = str(album)
    f = {'file_id': f'file-{mid}', 'file_unique_id': f'unique-{mid}'}
    if kind == 'photo':
        p['photo'] = [{**f, 'width': 300, 'height': 200}]
    elif kind == 'video':
        p['video'] = {**f, 'width': 640, 'height': 360, 'duration': 9,
                      'thumbnail': {'file_id': f'thumb-{mid}', 'file_unique_id': f't-{mid}',
                                    'width': 300, 'height': 200}}
    elif kind == 'audio':
        p['audio'] = {**f, 'duration': 20}
    elif kind == 'document':
        p['document'] = f
    elif kind == 'animation':
        p['animation'] = {**f, 'duration': 2, 'width': 100, 'height': 100}
    else:
        p['text'] = 'not media'
    return p


async def send(mid, **kwargs):
    return await live.enqueue(Message.de_json(payload(mid, **kwargs), None))


async def process(cid=CID):
    job = await db.col('source_jobs').find_one({'_id': cid})
    assert job
    return await live.process_channel(job)


async def ready():
    await db.col('source_jobs').update_many({}, {'$set': {'ready_at': 0}})


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    await runtime.stop_jobs()
    local_client = None
    if os.environ.get('BOT3_LOCAL_MONGO_TEST') == '1':
        from motor.motor_asyncio import AsyncIOMotorClient
        local_client = AsyncIOMotorClient('mongodb://127.0.0.1:27019', serverSelectionTimeoutMS=3000)
        await local_client.drop_database('botapi_queue_sandbox_only')
        db.use_test_client(local_client, 'botapi_queue_sandbox_only')
    else:
        db.use_test_client(mongomock_motor.AsyncMongoMockClient(), 'botapi_queue_tests')
    await db.col('collections').create_index('collection_id', unique=True)
    await live.ensure_indexes()
    await db.upsert_channel_mapping(CID, 'sparkle')
    monkeypatch.setattr(config, 'OWNER_ID', 7)
    monkeypatch.setattr(config, 'ADMIN_IDS', [7])
    monkeypatch.setattr(config, 'WEBHOOK_SECRET', 'offline-webhook-secret')
    live._last.update(state='starting', last_channel_at=None, last_error_type=None)
    monkeypatch.setattr(scanner, '_prepare_cover', AsyncMock(return_value={'hosted': True, 'ready': True}))
    yield
    await runtime.stop_jobs()
    if local_client:
        await local_client.drop_database('botapi_queue_sandbox_only')
        local_client.close()


async def test_webhook_previously_ignored_channel_posts_now_durable_before_200(monkeypatch):
    fake = NS(bot=Bot(), update_queue=asyncio.Queue())
    monkeypatch.setattr(main, 'app_bot', fake)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://offline.test') as c:
        response = await c.post('/webhook', headers={'X-Telegram-Bot-Api-Secret-Token': config.WEBHOOK_SECRET},
                                json={'update_id': 1, 'channel_post': payload(10, 'photo', 'Cover\nLine2\nLine3')})
    assert response.status_code == 200
    assert fake.update_queue.qsize() == 0  # independent of blocked user deliveries
    assert await db.col('source_posts').count_documents({}) == 1
    assert await db.col('source_jobs').count_documents({}) == 1


async def test_webhook_persistence_failure_returns_503_not_silent_loss(monkeypatch):
    monkeypatch.setattr(main, 'app_bot', NS(bot=Bot(), update_queue=asyncio.Queue()))
    monkeypatch.setattr(live, 'enqueue', AsyncMock(side_effect=RuntimeError('offline failure')))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://offline.test') as c:
        headers = {'X-Telegram-Bot-Api-Secret-Token': config.WEBHOOK_SECRET}
        assert (await c.post('/webhook', headers=headers,
                            json={'update_id': 1, 'channel_post': payload(1)})).status_code == 503


async def test_webhook_unauthorized_never_writes(monkeypatch):
    monkeypatch.setattr(main, 'app_bot', NS(bot=Bot(), update_queue=asyncio.Queue()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://offline.test') as c:
        assert (await c.post('/webhook', json={'update_id': 1, 'channel_post': payload(1)})).status_code == 403
    assert await db.col('source_posts').count_documents({}) == 0


async def test_count_262_to_293_without_any_userbot_or_video_download():
    for n in range(262):
        c = collection(f'c{CID}_{2*n+1}', (2*n+2,))
        c.update(channel_id=CID, category='sparkle')
        c['cover']['message_id'] = 2*n+1
        await db.upsert_collection(c)
    for n in range(31):
        mid = 600 + 2*n
        await send(mid, kind='photo', caption='New cover\nQuality\nDescription')
        await send(mid+1)
    assert runtime.userbot is None
    assert await db.col('source_jobs').count_documents({}) == 1
    count, cover_ids = await process()
    assert count == 62 and len(cover_ids) == 31
    assert await db.count_collections('sparkle') == 293
    assert (await db.get_collection(f'c{CID}_660'))['file_count'] == 1
    assert await db.col('source_jobs').count_documents({}) == 0


async def test_duplicate_delivery_and_replay_do_not_double_files_or_reset_paid_data():
    await send(10, kind='photo', caption='Title\nA\nB')
    await send(11)
    await process()
    key = f'c{CID}_10'
    await db.col('collections').update_one({'collection_id': key}, {'$set': {
        'price': 7, 'views': 123, 'unlocks': 5, 'cover.url': 'https://example.org/image.jpg'}})
    await db.create_user(7)
    await db.add_tokens(7, 21, 'offline')
    await db.insert_unlock(7, key)
    await db.toggle_bookmark(7, key)
    for _ in range(3):
        await send(10, kind='photo', caption='Title\nA\nB')
        await send(11)
        await process()
    doc = await db.get_collection(key)
    assert doc['file_count'] == 1 and len(doc['files']) == 1
    assert (doc['price'], doc['views'], doc['unlocks']) == (7, 123, 5)
    assert doc['cover']['url'] == 'https://example.org/image.jpg'
    assert await db.is_unlocked(7, key)
    assert await db.count_bookmarks(7) == 1
    assert (await db.get_user(7))['tokens'] == 21


async def test_restart_pending_metadata_survives_process_cleanup():
    await send(10, kind='photo', caption='Title\nA\nB')
    await send(11)
    await runtime.stop_jobs()
    await process()
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 1


async def test_append_to_last_userbot_collection_and_preserve_history_cursor():
    c = collection(f'c{CID}_10', (11,))
    c['channel_id'] = CID
    await db.upsert_collection(c)
    await db.col('scan_state').insert_one({'_id': CID, 'cursor': 11, 'last_cover_id': 10})
    await send(12)
    await process()
    assert (await db.get_collection(c['collection_id']))['file_count'] == 2
    assert (await db.col('scan_state').find_one({'_id': CID}))['cursor'] == 11
    assert (await db.col('live_scan_state').find_one({'_id': CID}))['last_message_id'] == 12


async def test_out_of_order_new_cover_reassigns_only_affected_tail():
    await send(10, kind='photo', caption='First\nA\nB')
    await send(11)
    await send(21)
    await process()
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 2
    await send(20, kind='photo', caption='Second\nA\nB')
    await process()
    assert [f['message_id'] for f in (await db.get_collection(f'c{CID}_10'))['files']] == [11]
    assert [f['message_id'] for f in (await db.get_collection(f'c{CID}_20'))['files']] == [21]


async def test_out_of_order_files_sorted_for_telegram_copy_messages():
    await send(10, kind='photo', caption='First\nA\nB')
    await send(12)
    await process()
    await send(11)
    await process()
    assert [f['message_id'] for f in (await db.get_collection(f'c{CID}_10'))['files']] == [11, 12]


async def test_media_before_cover_pending_then_late_cover_recovers():
    await send(11)
    await process()
    assert await db.col('pending_files').count_documents({}) == 1
    await send(10, kind='photo', caption='Late\nA\nB')
    await process()
    assert await db.col('pending_files').count_documents({}) == 0
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 1


async def test_cover_album_all_members_excluded_delivery_album_kept():
    await send(10, kind='photo', caption='Cover\nA\nB', album='cover')
    await send(11, kind='photo', album='cover')
    await send(12, album='delivery')
    await send(13, album='delivery')
    await process()
    d = await db.get_collection(f'c{CID}_10')
    assert d['cover']['album_message_ids'] == [10, 11]
    assert [f['message_id'] for f in d['files']] == [12, 13]


async def test_very_late_captionless_cover_album_member_not_sold_as_delivery():
    await send(10, kind='photo', caption='Cover\nA\nB', album='cover')
    await send(12)
    await process()
    await send(11, kind='photo', album='cover')
    await process()
    d = await db.get_collection(f'c{CID}_10')
    assert set(d['cover']['album_message_ids']) == {10, 11}
    assert [f['message_id'] for f in d['files']] == [12]


@pytest.mark.parametrize('kind', ['photo', 'video'])
async def test_cover_rules_match_historical_classifier(kind):
    await send(10, kind=kind, caption='First\nSecond\nThird')
    await send(11)
    await process()
    d = await db.get_collection(f'c{CID}_10')
    assert d['cover']['media_type'] == kind and d['file_count'] == 1


@pytest.mark.parametrize('kind', ['photo', 'video', 'document', 'animation', 'audio'])
async def test_short_caption_and_captionless_media_are_delivery_not_new_drop(kind):
    await send(10, kind='photo', caption='First\nSecond\nThird')
    await send(11, kind=kind, caption='only two\nlines')
    await process()
    assert await db.count_collections() == 1
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 1


async def test_unmapped_channel_and_plain_text_ignored():
    assert not await send(10, cid=-100999)
    assert not await send(11, kind='text')
    assert await db.col('source_posts').count_documents({}) == 0


async def test_removed_mapping_never_resurrected_by_pending_job():
    await send(10, kind='photo', caption='Title\nA\nB')
    await db.remove_channel_mapping(CID)
    await process()
    assert await db.count_collections() == 0
    assert await db.category_for_channel(CID) is None
    assert await db.col('source_jobs').count_documents({}) == 0


async def test_same_message_ids_isolated_between_channels():
    await db.upsert_channel_mapping(-100999, 'other')
    for cid in (CID, -100999):
        await send(10, kind='photo', caption='Title\nA\nB', cid=cid)
        await send(11, cid=cid)
        await process(cid)
    assert await db.count_collections('sparkle') == 1
    assert await db.count_collections('other') == 1


async def test_partial_enqueue_failure_repaired_by_webhook_retry(monkeypatch):
    original_col = db.col
    jobs = original_col('source_jobs')
    original = jobs.update_one
    monkeypatch.setattr(db, 'col', lambda name: jobs if name == 'source_jobs' else original_col(name))
    monkeypatch.setattr(jobs, 'update_one', AsyncMock(side_effect=RuntimeError('job write failed')))
    with pytest.raises(RuntimeError):
        await send(10, kind='photo', caption='Title\nA\nB')
    assert await db.col('source_posts').count_documents({}) == 1
    monkeypatch.setattr(db.col('source_jobs'), 'update_one', original)
    await send(10, kind='photo', caption='Title\nA\nB')
    await process()
    assert await db.count_collections() == 1


async def test_crash_mid_processing_replay_is_idempotent(monkeypatch):
    await send(10, kind='photo', caption='Title\nA\nB')
    await send(11)
    original_col = db.col
    state = original_col('live_scan_state')
    original = state.update_one
    monkeypatch.setattr(db, 'col', lambda name: state if name == 'live_scan_state' else original_col(name))
    monkeypatch.setattr(state, 'update_one', AsyncMock(side_effect=RuntimeError('simulated crash')))
    with pytest.raises(RuntimeError):
        await process()
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 1
    assert await db.col('source_jobs').count_documents({}) == 1
    monkeypatch.setattr(db.col('live_scan_state'), 'update_one', original)
    await process()
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 1
    assert await db.col('source_jobs').count_documents({}) == 0


async def test_new_arrival_during_scan_retains_job_for_followup(monkeypatch):
    await send(10, kind='photo', caption='Title\nA\nB')
    await send(11)
    original = live._process_unit
    added = False
    async def injecting(*args):
        nonlocal added
        await original(*args)
        if not added:
            added = True
            await send(12)
    monkeypatch.setattr(live, '_process_unit', injecting)
    await process()
    assert await db.col('source_jobs').count_documents({}) == 1
    await process()
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 2


async def test_batch_quiet_window_and_maximum_wait(monkeypatch):
    monkeypatch.setattr(db, 'now_ts', lambda: 100.0)
    await send(10, kind='photo', caption='Title\nA\nB')
    monkeypatch.setattr(db, 'now_ts', lambda: 101.0)
    assert not await live.drain_once(Bot())
    # Continuous burst resets quiet time, but first_pending_at caps deferral.
    for n in range(11, 21):
        monkeypatch.setattr(db, 'now_ts', lambda n=n: float(90+n))
        await send(n)
    monkeypatch.setattr(db, 'now_ts', lambda: 110.0)
    assert await live.drain_once(Bot())
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 10


async def test_poison_channel_retries_without_starving_healthy_source(monkeypatch):
    await db.upsert_channel_mapping(-100999, 'healthy')
    await send(10, kind='photo', caption='Bad\nA\nB')
    await send(10, kind='photo', caption='Good\nA\nB', cid=-100999)
    await ready()
    original = live.process_channel
    async def fail_one(job):
        if job['_id'] == CID:
            raise ValueError('offline failure')
        return await original(job)
    monkeypatch.setattr(live, 'process_channel', fail_one)
    await live.drain_once(Bot())
    job = await db.col('source_jobs').find_one({'_id': CID})
    assert job['last_error_type'] == 'ValueError'
    assert job['ready_at'] > db.now_ts()
    await live.drain_once(Bot())
    assert await db.count_collections('healthy') == 1
    assert await db.col('source_jobs').count_documents({'_id': CID}) == 1


async def test_journal_no_ttl_before_success_and_seven_days_after_success():
    await send(10, kind='photo', caption='Title\nA\nB')
    row = await db.col('source_posts').find_one({})
    assert 'expires_on' not in row
    await process()
    row = await db.col('source_posts').find_one({})
    assert 6*86400 < row['expires_on'].timestamp() - db.now_ts() <= 7*86400
    indexes = await db.col('source_posts').index_information()
    assert indexes['expires_on_1']['expireAfterSeconds'] == 0


async def test_history_catchup_not_skipped_when_live_posts_already_indexed():
    await send(20, kind='photo', caption='Newest\nA\nB')
    await send(21)
    await process()
    src = Source([source_message(10, 'Older\nA\nB', kind='photo'), source_message(11),
                  source_message(20, 'Newest\nA\nB', kind='photo'), source_message(21)])
    await scanner.scan_channel(src, CID, 'sparkle', prepare_covers=False)
    assert await db.count_collections() == 2
    assert (await db.get_collection(f'c{CID}_20'))['file_count'] == 1


async def test_live_and_historical_writer_share_channel_lock():
    from locks import channel_lock
    await send(10, kind='photo', caption='Title\nA\nB')
    async with channel_lock(CID):
        task = asyncio.create_task(process())
        await asyncio.sleep(0.01)
        assert not task.done()
    await task


async def test_new_photos_have_immediate_preview_without_host_or_userbot():
    await send(10, kind='photo', caption='Title\nA\nB')
    await process()
    d = await db.get_collection(f'c{CID}_10')
    assert await rich_cards._cached_photo(d['cover']) == ('file-10', None)
    fake = NS(send_photo=AsyncMock(return_value=NS(photo=[])))
    await rich_cards.show_cover_card(fake, 7, 'offline card', None, d)
    fake.send_photo.assert_awaited_once()
    assert fake.send_photo.call_args.kwargs['photo'] == 'file-10'


async def test_video_thumbnail_downloaded_as_small_image_never_resent_id_or_video():
    await send(10, kind='video', caption='Title\nA\nB')
    await process()
    d = await db.get_collection(f'c{CID}_10')
    assert await rich_cards._cached_photo(d['cover']) == (None, None)
    buf = io.BytesIO()
    Image.new('RGB', (40, 30), 'blue').save(buf, 'JPEG')
    class Remote:
        file_size = len(buf.getvalue())
        async def download_to_memory(self, out):
            out.write(buf.getvalue())
    bot = NS(get_file=AsyncMock(return_value=Remote()))
    data = await covers.download_image(live.BotCoverClient(bot), CID, 10)
    assert Image.open(io.BytesIO(data)).size == (40, 30)
    bot.get_file.assert_awaited_once_with('thumb-10')


async def test_cover_adapter_rejects_oversized_download_before_fetch():
    remote = NS(file_size=covers.MAX_BYTES+1, download_to_memory=AsyncMock())
    adapter = live.BotCoverClient(NS(get_file=AsyncMock(return_value=remote)))
    with pytest.raises(ValueError):
        await adapter.download_media('thumb', io.BytesIO())
    remote.download_to_memory.assert_not_awaited()


async def test_no_userbot_logs_paused_once_but_live_queue_still_works(monkeypatch, caplog):
    import userbot_setup
    monkeypatch.setattr(userbot_setup, 'build_client', AsyncMock(return_value=None))
    with caplog.at_level('WARNING'):
        await runtime.poll_once()
        await runtime.poll_once()
    assert sum('Historical catch-up paused' in r.message for r in caplog.records) == 1
    await send(10, kind='photo', caption='Title\nA\nB')
    await ready()
    assert await live.drain_once(Bot())
    assert await db.count_collections() == 1


async def test_admin_only_status_and_health_no_credentials(monkeypatch):
    u = update(9)
    await handlers.cmd_index_status(u, context())
    assert not u.message.replies
    await db.update_settings({'userbot_credentials': 'PRIVATE_NOT_FOR_STATUS'})
    u = update(7)
    await handlers.cmd_index_status(u, context())
    assert 'durable automatic queue' in u.message.replies[-1][0]
    assert 'PRIVATE_NOT_FOR_STATUS' not in u.message.replies[-1][0]
    h = await main.health()
    assert h['version'] == 'channel-post-queue-v1'
    assert 'PRIVATE_NOT_FOR_STATUS' not in json.dumps(h)


async def test_real_lifespan_registers_channel_updates_starts_worker_and_stops(monkeypatch):
    import userbot_setup
    class App:
        def __init__(self):
            self.bot = Bot()
            self.bot.set_webhook = AsyncMock()
            self.bot.set_my_commands = AsyncMock()
            self.initialize = AsyncMock()
            self.start = AsyncMock()
            self.stop = AsyncMock()
            self.shutdown = AsyncMock()
    fake = App()
    monkeypatch.setattr(config, 'RENDER_EXTERNAL_URL', 'https://offline.test')
    monkeypatch.setattr(main, 'build_bot', lambda: fake)
    monkeypatch.setattr(userbot_setup, 'build_client', AsyncMock(return_value=None))
    async with main.lifespan(main.app):
        await asyncio.sleep(0.01)
        call = fake.bot.set_webhook.call_args.kwargs
        assert 'channel_post' in call['allowed_updates']
        assert call['drop_pending_updates'] is False
        assert live.snapshot()['state'] == 'running'
        assert len(runtime._background) == 3
    assert not runtime._tasks
    assert live.snapshot()['state'] == 'stopped'


async def test_real_sdk_webhook_and_photo_serialization_offline():
    class Recording(BaseRequest):
        def __init__(self): self.calls = []
        @property
        def read_timeout(self): return 5
        async def initialize(self): pass
        async def shutdown(self): pass
        async def do_request(self, url, method, request_data=None, **kwargs):
            params = request_data.parameters if request_data else {}
            api = url.rsplit('/', 1)[-1]
            self.calls.append((api, params))
            if api == 'getMe':
                result = {'id': 1, 'is_bot': True, 'first_name': 'Offline', 'username': 'offline_bot'}
            elif api == 'setWebhook':
                result = True
            else:
                result = {'message_id': 99, 'date': 1700000000,
                          'chat': {'id': 7, 'type': 'private'}, 'photo': [
                              {'file_id': 'result', 'file_unique_id': 'r', 'width': 300, 'height': 200}]}
            return 200, json.dumps({'ok': True, 'result': result}).encode()
    request = Recording()
    async with TelegramBot('123456:OFFLINE_TEST_ONLY', request=request) as bot:
        await bot.set_webhook('https://offline.test/webhook', allowed_updates=main.WEBHOOK_UPDATES,
                              secret_token='offline_secret', drop_pending_updates=False)
        await send(10, kind='photo', caption='Title\nA\nB')
        await process()
        d = await db.get_collection(f'c{CID}_10')
        await rich_cards.show_cover_card(bot, 7, 'Card', None, d)
    webhook = next(p for api, p in request.calls if api == 'setWebhook')
    photo = next(p for api, p in request.calls if api == 'sendPhoto')
    assert webhook['allowed_updates'] == ['message', 'callback_query', 'channel_post']
    assert webhook['drop_pending_updates'] is False
    assert photo['photo'] == 'file-10'


async def test_earlier_cover_album_member_keeps_purchased_stable_id():
    # The caption-bearing member is delivered first, before the lower-ID member.
    await send(11, kind='photo', caption='Album\nA\nB', album='999')
    await send(12)
    await process()
    stable = f'c{CID}_11'
    await db.insert_unlock(7, stable)
    await db.col('collections').update_one({'collection_id': stable}, {'$set': {'price': 9}})
    await send(10, kind='photo', album='999')
    await process()
    assert await db.count_collections() == 1
    d = await db.get_collection(stable)
    assert d['cover']['message_id'] == 10
    assert set(d['cover']['album_message_ids']) == {10, 11}
    assert d['file_count'] == 1 and d['price'] == 9
    assert await db.is_unlocked(7, stable)
    src = Source([source_message(10, '', kind='photo', gid=999),
                  source_message(11, 'Album\nA\nB', kind='photo', gid=999), source_message(12)])
    await scanner.scan_channel(src, CID, 'sparkle', full=True, prepare_covers=False)
    assert await db.count_collections() == 1
    assert await db.is_unlocked(7, stable)
    assert (await db.get_collection(stable))['price'] == 9


async def test_retried_journal_row_unsets_previous_expiration():
    await send(10, kind='photo', caption='Title\nA\nB')
    await process()
    assert 'expires_on' in await db.col('source_posts').find_one({})
    await send(10, kind='photo', caption='Title\nA\nB')
    assert 'expires_on' not in await db.col('source_posts').find_one({})


async def test_connected_userbot_does_not_duplicate_scan_for_queued_live_burst(monkeypatch):
    from test_autoindex_pricing import LiveSource
    await runtime.replace_userbot(LiveSource([]))
    await send(10, kind='photo', caption='Title\nA\nB')
    monkeypatch.setattr(runtime, 'EVENT_DEBOUNCE_SECONDS', 0)
    schedule = NS(fn=lambda: None)
    from unittest.mock import Mock
    schedule = Mock()
    monkeypatch.setattr(runtime, 'schedule_scan', schedule)
    await runtime._debounced_scan(CID)
    schedule.assert_not_called()


async def test_real_background_queue_indexes_without_owner_command(monkeypatch):
    monkeypatch.setattr(live, 'TICK_SECONDS', 0.001)
    await send(10, kind='photo', caption='Title\nA\nB')
    await send(11)
    await ready()
    runtime.start_jobs(Bot())
    for _ in range(500):
        doc = await db.get_collection(f'c{CID}_10')
        if doc and doc['file_count'] == 1:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail('Persisted channel updates were not processed by the real background worker')
    await runtime.stop_jobs()
    assert not runtime._tasks and not runtime._background


async def test_burst_2000_metadata_updates_coalesces_one_job_no_video_bytes():
    for n in range(1000):
        await send(2*n+10, kind='photo', caption='Burst\nA\nB')
        await send(2*n+11)
    assert await db.col('source_jobs').count_documents({}) == 1
    assert await db.col('source_posts').count_documents({}) == 2000
    row = await db.col('source_posts').find_one({'media_type': 'video'})
    assert 'data' not in row and 'file_id' not in row  # no delivery video cached
    count, ids = await process()
    assert count == 2000 and len(ids) == 1000
    assert await db.count_collections() == 1000
    assert await db.col('collections').count_documents({'file_count': 1}) == 1000


async def test_concurrent_duplicate_webhooks_idempotent_real_database():
    await asyncio.gather(*(send(10, kind='photo', caption='Title\nA\nB') for _ in range(20)))
    await asyncio.gather(*(send(11) for _ in range(20)))
    assert await db.col('source_posts').count_documents({}) == 2
    job = await db.col('source_jobs').find_one({'_id': CID})
    assert job['version'] == 40
    await process()
    assert await db.count_collections() == 1
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 1
