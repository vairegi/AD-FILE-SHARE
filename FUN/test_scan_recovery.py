"""Offline regressions for the observed duplicated-key failure and stalled scans."""
import asyncio
import json
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import mongomock_motor
import pytest
import pytest_asyncio
from telethon.errors import (AuthKeyDuplicatedError, AuthKeyUnregisteredError,
                             SessionRevokedError, SessionExpiredError)
import config, db, runtime, scanner, chat_setup, userbot_setup, handlers, main
import session_health
from test_repairs import source_message, Source, update, context, collection, Bot, session_string
from test_autoindex_pricing import LiveSource, source_drop
from test_live_index import send, ready, CID
import live_index


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    await runtime.stop_jobs()
    local = None
    if os.environ.get('BOT3_LOCAL_MONGO_TEST') == '1':
        from motor.motor_asyncio import AsyncIOMotorClient
        local = AsyncIOMotorClient('mongodb://127.0.0.1:27019', serverSelectionTimeoutMS=3000)
        await local.drop_database('scan_recovery_sandbox_only')
        db.use_test_client(local, 'scan_recovery_sandbox_only')
    else:
        db.use_test_client(mongomock_motor.AsyncMongoMockClient(), 'scan_recovery_tests')
    await db.col('collections').create_index('collection_id', unique=True)
    await db.upsert_channel_mapping(-100, 'movies')
    await live_index.ensure_indexes()
    monkeypatch.setattr(config, 'OWNER_ID', 7)
    monkeypatch.setattr(config, 'ADMIN_IDS', [7])
    monkeypatch.setattr(config, 'MONGO_URI', 'mongodb://offline-fixture-only/unused')
    monkeypatch.setattr(config, 'SESSION_ENCRYPTION_KEY', '')
    monkeypatch.setattr(runtime, 'bot', NS(send_message=AsyncMock()))
    monkeypatch.setattr(scanner, '_prepare_cover', AsyncMock(return_value={'hosted': True, 'ready': True}))
    chat_setup._pending.clear()
    yield
    chat_setup._pending.clear()
    await runtime.stop_jobs()
    if local:
        await local.drop_database('scan_recovery_sandbox_only')
        local.close()


class Broken(LiveSource):
    def __init__(self, error=AuthKeyDuplicatedError):
        super().__init__(source_drop(1))
        self.error = error
    async def iter_messages(self, cid, **kw):
        raise self.error(None)
        yield  # real asynchronous iterator that fails on first source read
    async def get_messages(self, cid, limit=1):
        raise self.error(None)


@pytest.mark.parametrize('error', [AuthKeyDuplicatedError, AuthKeyUnregisteredError,
                                   SessionRevokedError, SessionExpiredError])
async def test_fatal_scan_is_paused_persistently_and_reports_real_recovery(error):
    broken = Broken(error)
    await runtime.replace_userbot(broken)
    reply = AsyncMock()
    await runtime.schedule_scan(-100, reply)
    assert runtime.userbot is None and not broken.connected
    state = await session_health.blocked()
    assert state['error_type'] == error.__name__
    assert 'historical' in reply.call_args.args[0].lower()
    assert 'new' in reply.call_args.args[0].lower()
    assert not runtime.scan_busy(-100)
    assert (await db.col('scan_state').find_one({'_id': -100}))['scan_status'] == 'failed'


async def test_poller_does_not_retry_duplicated_session_every_30_seconds(monkeypatch):
    broken = Broken()
    await runtime.replace_userbot(broken)
    await db.col('scan_state').insert_one({'_id': -100, 'cursor': 1})
    await runtime.poll_once()
    assert await session_health.blocked()
    build = AsyncMock(side_effect=AssertionError('must not reconnect broken key'))
    monkeypatch.setattr(userbot_setup, 'build_client', build)
    for _ in range(4):
        await runtime.poll_once()
    build.assert_not_awaited()
    runtime.bot.send_message.assert_awaited_once()


async def test_failed_startup_build_persists_block_without_looping(monkeypatch):
    build = AsyncMock(side_effect=AuthKeyDuplicatedError(None))
    monkeypatch.setattr(userbot_setup, 'build_client', build)
    assert not await runtime._ensure_userbot()
    assert not await runtime._ensure_userbot()
    build.assert_awaited_once()
    assert await session_health.blocked()


async def test_block_survives_runtime_shutdown_and_prevents_saved_client_build(monkeypatch):
    await db.update_settings({'userbot_health': {'state': 'reauth_required',
                                               'error_type': 'AuthKeyDuplicatedError'}})
    await runtime.stop_jobs()
    connect = AsyncMock(side_effect=AssertionError('not allowed'))
    monkeypatch.setattr(userbot_setup, 'connect_credentials', connect)
    assert await userbot_setup.build_client() is None
    connect.assert_not_awaited()


async def test_new_valid_import_clears_health_pause_only_after_success():
    await db.update_settings({'userbot_health': {'state': 'reauth_required',
                                               'error_type': 'AuthKeyDuplicatedError'}})
    await chat_setup.save_credentials({'api_id': 12345, 'api_hash': 'a'*32,
                                       'session': session_string()})
    assert not await session_health.blocked()
    assert (await userbot_setup.load_credentials())['api_id'] == 12345


async def test_setup_fatal_failure_keeps_old_saved_credentials_and_no_secret_echo(monkeypatch):
    credentials = {'api_id': 12345, 'api_hash': 'a'*32, 'session': session_string()}
    await chat_setup.save_credentials(credentials)
    await chat_setup.begin(update(), context())
    for text in ('12345', 'a'*32):
        await chat_setup.consume(update(text=text), context())
    monkeypatch.setattr(userbot_setup, 'connect_credentials', AsyncMock(side_effect=AuthKeyDuplicatedError(None)))
    u = update(text=session_string())
    await chat_setup.consume(u, context())
    text = u.message.replies[-1][0]
    assert 'AuthKeyDuplicatedError' in text and 'NEW' in text
    assert session_string() not in text and 'a'*32 not in text
    assert await chat_setup.load_credentials() == credentials


async def test_status_real_rpc_rejects_cached_authorized_yes():
    client = LiveSource([])
    client.get_me = AsyncMock(side_effect=AuthKeyDuplicatedError(None))
    await runtime.replace_userbot(client)
    u = update()
    await chat_setup.status(u, context())
    text = u.message.replies[-1][0]
    assert 'Connected: <code>no</code>' in text
    assert 'AuthKeyDuplicatedError' in text
    assert runtime.userbot is None


async def test_manual_scan_blocked_key_returns_instructions_not_generic_scan_start():
    await db.update_settings({'userbot_health': {'state': 'reauth_required',
                                               'error_type': 'AuthKeyDuplicatedError'}})
    u = update()
    await handlers.cmd_scan(u, context(args=['-100']))
    assert 'Do not resend the old string' in u.message.replies[-1][0]
    assert not runtime.scan_busy(-100)


async def test_removal_during_hung_scan_returns_quickly_and_leaves_live_queue_running():
    class Hung(LiveSource):
        async def iter_messages(self, cid, **kw):
            await asyncio.Event().wait()
            yield
    await runtime.replace_userbot(Hung([]))
    task = runtime.schedule_scan(-100)
    for _ in range(100):
        row = await db.col('scan_state').find_one({'_id': -100})
        if row and row.get('scan_status') == 'running': break
        await asyncio.sleep(0.001)
    await asyncio.wait_for(chat_setup.remove(update(), context()), timeout=1)
    assert task.done() and task.cancelled() and runtime.userbot is None
    await db.upsert_channel_mapping(CID, 'sparkle')
    await send(10, kind='photo', caption='Cover\nA\nB')
    await send(11)
    await ready()
    assert await live_index.drain_once(Bot())
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 1


async def test_replacing_userbot_cancels_hung_scan_not_indefinite_wait():
    entered = asyncio.Event()
    class Hung(LiveSource):
        async def iter_messages(self, cid, **kw):
            entered.set()
            await asyncio.Event().wait()
            yield
    old = Hung([])
    await runtime.replace_userbot(old)
    task = runtime.schedule_scan(-100)
    await asyncio.wait_for(entered.wait(), 1)
    new = LiveSource([])
    await asyncio.wait_for(runtime.replace_userbot(new), 1)
    assert runtime.userbot is new and task.cancelled() and not old.connected


async def test_per_read_timeout_releases_shared_channel_lock(monkeypatch):
    from locks import channel_lock
    class Hung(Source):
        async def iter_messages(self, cid, **kw):
            await asyncio.Event().wait()
            yield
    monkeypatch.setattr(scanner, 'READ_TIMEOUT_SECONDS', 0.01)
    with pytest.raises(TimeoutError):
        await scanner.scan_channel(Hung([]), -100, 'movies', prepare_covers=False)
    async with asyncio.timeout(1):
        async with channel_lock(-100):
            pass


async def test_total_scan_budget_bounds_lock_wait_and_reports_timeout(monkeypatch):
    from locks import channel_lock
    runtime.userbot = LiveSource(source_drop(1))
    monkeypatch.setattr(runtime, 'SCAN_BUDGET_SECONDS', 0.01)
    reply = AsyncMock()
    async with channel_lock(-100):
        await asyncio.wait_for(runtime.schedule_scan(-100, reply), 1)
    assert 'timed out' in reply.call_args.args[0]
    assert not runtime.scan_busy(-100)
    assert (await db.col('scan_state').find_one({'_id': -100}))['scan_status'] == 'failed'


async def test_checkpoint_then_disconnect_resumes_without_losing_paid_data(monkeypatch):
    monkeypatch.setattr(scanner, 'CHECKPOINT_UNITS', 1)
    class Interrupted(Source):
        async def iter_messages(self, cid, **kw):
            for p in source_drop(1) + [source_message(3)]:
                yield p
            raise ConnectionError('PRIVATE_MUST_NOT_BE_LOGGED')
    with pytest.raises(ConnectionError):
        await scanner.scan_channel(Interrupted([]), -100, 'movies', prepare_covers=False)
    row = await db.col('scan_state').find_one({'_id': -100})
    assert row['cursor'] == 2 and row['last_cover_id'] == 1
    await db.insert_unlock(7, 'c-100_1')
    await db.col('collections').update_one({'collection_id': 'c-100_1'}, {'$set': {'price': 8}})
    good = Source(source_drop(1) + [source_message(3)] + source_drop(4))
    await scanner.scan_channel(good, -100, 'movies', prepare_covers=False)
    assert good.kwargs[-1]['min_id'] == 0
    assert (await db.get_collection('c-100_1'))['file_count'] == 2
    assert (await db.get_collection('c-100_1'))['price'] == 8
    assert await db.is_unlocked(7, 'c-100_1')
    assert await db.count_collections() == 2


async def test_partial_error_state_does_not_skip_unscanned_live_only_history():
    await db.col('scan_state').insert_one({'_id': -100, 'scan_status': 'failed',
                                          'last_error_type': 'ConnectionError'})
    d = collection('c-100_20', (21,))
    d['cover']['message_id'] = 20
    d['ingest_source'] = 'bot_api'
    await db.upsert_collection(d)
    source = Source(source_drop(1) + source_drop(20))
    await scanner.scan_channel(source, -100, 'movies', prepare_covers=False)
    assert source.kwargs[-1]['min_id'] == 0
    assert await db.get_collection('c-100_1')


async def test_progress_command_includes_actual_state_and_is_admin_only():
    await db.col('scan_state').insert_one({'_id': -100, 'scan_status': 'complete',
                                          'cursor': 123, 'progress_message_id': 123,
                                          'progress_posts': 30})
    u = update()
    await handlers.cmd_scan_status(u, context(args=['-100']))
    assert 'State: complete' in u.message.replies[-1][0]
    assert 'Saved cursor: 123' in u.message.replies[-1][0]
    other = update(9)
    await handlers.cmd_scan_status(other, context(args=['-100']))
    assert not other.message.replies


async def test_stale_running_state_after_restart_is_not_claimed_active():
    await db.col('scan_state').insert_one({'_id': -100, 'scan_status': 'running'})
    text = await runtime.scan_status_text(-100)
    assert 'interrupted' in text and 'Active worker: no' in text


async def test_stale_client_auth_error_cannot_pause_fresh_replacement():
    old, new = Broken(), LiveSource([])
    await runtime.replace_userbot(new)
    await runtime.pause_userbot(AuthKeyDuplicatedError(None), old)
    assert runtime.userbot is new and not await session_health.blocked()


async def test_transient_network_error_does_not_force_new_session(monkeypatch):
    monkeypatch.setattr(userbot_setup, 'build_client', AsyncMock(side_effect=ConnectionError('secret')))
    with pytest.raises(ConnectionError):
        await runtime._ensure_userbot()
    assert not await session_health.blocked()


async def test_simultaneous_restore_attempts_create_only_one_client(monkeypatch):
    client = LiveSource([])
    async def build():
        await asyncio.sleep(0.01)
        return client
    connect = AsyncMock(side_effect=build)
    monkeypatch.setattr(userbot_setup, 'build_client', connect)
    assert all(await asyncio.gather(*(runtime._ensure_userbot() for _ in range(5))))
    connect.assert_awaited_once()


async def test_status_command_registered_private(monkeypatch):
    monkeypatch.setattr(config, 'BOT_TOKEN', '123456:OFFLINE_TEST_ONLY')
    app = main.build_bot()
    assert any('scan_status' in getattr(h, 'commands', ()) for h in app.handlers[0])


async def test_fatal_auth_never_stops_independent_new_post_queue():
    await runtime.replace_userbot(Broken())
    await runtime.schedule_scan(-100)
    assert await session_health.blocked()
    await db.upsert_channel_mapping(CID, 'sparkle')
    await send(10, kind='photo', caption='New\nA\nB')
    await send(11)
    await ready()
    assert await live_index.drain_once(Bot())
    assert (await db.get_collection(f'c{CID}_10'))['file_count'] == 1
    assert 'session' not in json.dumps(await db.col('source_posts').find_one({}), default=str).lower()


async def test_snapshot_end_prevents_chasing_new_posts_during_history_pass():
    source = Source(source_drop(1) + source_drop(3) + source_drop(5))
    await scanner.scan_channel(source, -100, 'movies', end_id=4, prepare_covers=False)
    assert source.kwargs[-1]['max_id'] == 5
    assert await db.count_collections() == 2
    row = await db.col('scan_state').find_one({'_id': -100})
    assert row['cursor'] == 4 and row['target_message_id'] == 4
    assert row['scan_status'] == 'complete'


async def test_history_backfill_sorts_older_files_before_live_appended_media():
    doc = collection('c-100_1', (5, 7))
    doc['cover']['message_id'] = 1
    await db.upsert_collection(doc)
    await scanner.scan_channel(Source([source_message(1, 'Cover\nA\nB', kind='photo')]+
                                     [source_message(i) for i in (2,4,5,7)]),
                               -100, 'movies', full=True, prepare_covers=False)
    result = await db.get_collection('c-100_1')
    assert [f['message_id'] for f in result['files']] == [2,4,5,7]
    assert result['file_count'] == 4


async def test_progress_checkpoint_never_advances_past_incomplete_album(monkeypatch):
    monkeypatch.setattr(scanner, 'CHECKPOINT_UNITS', 1)
    class Interrupted(Source):
        async def iter_messages(self, cid, **kw):
            yield source_message(1, 'Cover\nA\nB', kind='photo')
            yield source_message(2, gid=99)
            yield source_message(3, gid=99)
            raise ConnectionError('offline')
    with pytest.raises(ConnectionError):
        await scanner.scan_channel(Interrupted([]), -100, 'movies', prepare_covers=False)
    row = await db.col('scan_state').find_one({'_id': -100})
    assert row['cursor'] == 1 and row['last_cover_id'] == 1
    await scanner.scan_channel(Source([source_message(1, 'Cover\nA\nB', kind='photo'),
                                       source_message(2,gid=99),source_message(3,gid=99)]),
                               -100, 'movies', prepare_covers=False)
    assert (await db.get_collection('c-100_1'))['file_count'] == 2


async def test_owner_can_resume_completed_scan_and_see_starting_target():
    source = LiveSource(source_drop(1))
    await runtime.replace_userbot(source)
    await runtime.schedule_scan(-100)
    row = await db.col('scan_state').find_one({'_id': -100})
    assert row['target_message_id'] == 2 and row['cursor'] == 2
    source.posts.extend(source_drop(3))
    await runtime.schedule_scan(-100)
    row = await db.col('scan_state').find_one({'_id': -100})
    assert row['target_message_id'] == 4 and row['cursor'] == 4
    assert await db.count_collections() == 2
