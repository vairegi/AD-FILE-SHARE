"""Root bandwidth regressions. Never uses production credentials.
Run: pytest -q --asyncio-mode=auto test_root_index.py test_bandwidth.py
Optional real MongoDB: ROOT_LOCAL_MONGO_TEST=1, localhost:27019 only.
Reuses the isolated fixture and helpers from the existing root-index suite.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import httpx
import pytest
from telegram import Bot
from test_root_index import isolate, post, send, drain, items
import config, db, main, root_dispatch as dispatch, root_index as idx


def payload(n=1):
    return {'update_id': n, 'message': {'message_id': n, 'date': 1,
        'from': {'id': 7, 'first_name': 'Offline', 'is_bot': False},
        'chat': {'id': 7, 'type': 'private'}, 'text': '/help'}}


def application(process=None):
    return NS(bot=Bot('123456:offline'), process_update=process or AsyncMock())


async def until(predicate, timeout=2):
    async def wait():
        while not await predicate():
            await asyncio.sleep(.005)
    await asyncio.wait_for(wait(), timeout)


async def test_idle_workers_query_once_then_sleep_not_quarter_second(monkeypatch):
    poll = AsyncMock(return_value=False)
    monkeypatch.setattr(dispatch, 'drain_once', poll)
    monkeypatch.setattr(dispatch, 'IDLE_POLL_SECONDS', 10)
    dispatch.start(application(), application())
    await asyncio.sleep(.35)
    assert poll.await_count == 4
    assert len(dispatch._events) == 4


async def test_recovery_sweep_shared_and_throttled_between_workers(monkeypatch):
    update = AsyncMock()
    monkeypatch.setattr(db, '_db', NS(root_update_inbox=NS(update_many=update)))
    await asyncio.gather(*(dispatch.recover_expired('bot1') for _ in range(20)))
    assert update.await_count == 1
    await dispatch.recover_expired('bot2')
    assert update.await_count == 2
    dispatch._recovery_at['bot1'] = 0
    await dispatch.recover_expired('bot1')
    assert update.await_count == 3


async def test_failed_recovery_can_retry_without_waiting_full_interval(monkeypatch):
    update = AsyncMock(side_effect=RuntimeError('offline'))
    monkeypatch.setattr(db, '_db', NS(root_update_inbox=NS(update_many=update)))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await dispatch.recover_expired('bot1')
    assert update.await_count == 2
    assert 'bot1' not in dispatch._recovery_at


async def test_enqueue_wakes_only_after_successful_persistence(monkeypatch):
    event = dispatch.worker_event('bot2', 0)
    async def save(*a, **kw):
        assert not event.is_set()
    update = AsyncMock(side_effect=save)
    monkeypatch.setattr(db, '_db', NS(root_update_inbox=NS(update_one=update)))
    await dispatch.enqueue('bot2', payload())
    assert event.is_set()
    event.clear()
    update.side_effect = RuntimeError('write failed')
    with pytest.raises(RuntimeError):
        await dispatch.enqueue('bot2', payload(2))
    assert not event.is_set()


async def test_new_webhook_wakes_sleeping_workers_without_idle_delay(monkeypatch):
    monkeypatch.setattr(dispatch, 'IDLE_POLL_SECONDS', 10)
    app = application()
    dispatch.start(application(), app)
    await asyncio.sleep(.04)
    await dispatch.enqueue('bot2', payload())
    async def done(): return app.process_update.await_count == 1
    await until(done, .5)
    await asyncio.sleep(.03)
    assert (await db._db.root_update_inbox.find_one({'_id': 'bot2:1'}))['state'] == 'done'


async def test_no_lost_wakeup_between_empty_query_and_wait(monkeypatch):
    calls = 0
    entered, finish, processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def claim(name, app):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await finish.wait()
            return False
        processed.set()
        return False
    monkeypatch.setattr(dispatch, 'drain_once', claim)
    monkeypatch.setattr(dispatch, 'IDLE_POLL_SECONDS', 10)
    task = asyncio.create_task(dispatch.loop('bot2', application(), 0))
    try:
        await asyncio.wait_for(entered.wait(), .5)
        dispatch.wake('bot2')
        finish.set()
        await asyncio.wait_for(processed.wait(), .5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_restart_recovers_persisted_before_wake_update():
    await dispatch.enqueue('bot2', payload())
    await dispatch.stop()
    app = application()
    dispatch.start(application(), app)
    async def done(): return app.process_update.await_count == 1
    await until(done)


async def test_external_pending_update_is_found_by_fallback_poll(monkeypatch):
    monkeypatch.setattr(dispatch, 'IDLE_POLL_SECONDS', .04)
    app = application()
    dispatch.start(application(), app)
    await asyncio.sleep(.025)
    await db._db.root_update_inbox.insert_one({'_id': 'bot2:5', 'bot': 'bot2',
        'state': 'pending', 'ready_at': 0, 'attempts': 0, 'payload': payload(5)})
    async def done(): return app.process_update.await_count == 1
    await until(done, .5)


async def test_busy_workers_keep_fixed_concurrency_and_no_duplicate_receipts():
    running = peak = 0
    gate = asyncio.Event()
    async def process(update):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await gate.wait()
        running -= 1
    app = application(AsyncMock(side_effect=process))
    for n in range(12): await dispatch.enqueue('bot2', payload(n))
    dispatch.start(application(), app)
    async def two(): return running == 2
    await until(two)
    for n in range(12): await dispatch.enqueue('bot2', payload(n))
    gate.set()
    async def all_done():
        return await db._db.root_update_inbox.count_documents({'state': 'done'}) == 12
    await until(all_done)
    assert peak == 2 and app.process_update.await_count == 12


async def test_cancelled_handler_is_review_not_replayed_after_restart():
    started = asyncio.Event()
    async def process(update):
        started.set()
        await asyncio.Event().wait()
    app = application(AsyncMock(side_effect=process))
    await dispatch.enqueue('bot2', payload())
    dispatch.start(application(), app)
    await asyncio.wait_for(started.wait(), 1)
    await dispatch.stop()
    doc = await db._db.root_update_inbox.find_one({'_id': 'bot2:1'})
    assert doc['state'] == 'review'
    next_app = application()
    assert not await dispatch.drain_once('bot2', next_app)
    next_app.process_update.assert_not_awaited()


async def test_worker_database_error_uses_bounded_backoff(monkeypatch):
    claim = AsyncMock(side_effect=RuntimeError('offline'))
    monkeypatch.setattr(dispatch, 'drain_once', claim)
    monkeypatch.setattr(dispatch, 'ERROR_SECONDS', .1)
    task = asyncio.create_task(dispatch.loop('bot1', application(), 0))
    try:
        await asyncio.sleep(.055)
        assert claim.await_count == 1
        await asyncio.sleep(.07)
        assert claim.await_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_open_cover_parked_and_eight_second_debounce_cannot_bypass(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(db, 'now', lambda: now[0])
    await send(post(1, 'cover'), post(2))
    assert await drain() == 0
    job = await db._db.root_index_jobs.find_one({'_id': 'demo'})
    assert job['waiting_for_source'] and job['ready_at'] == 400
    before = idx.snapshot()['passes']
    now[0] = 109
    for _ in range(5): assert await idx.drain_once() == 0
    assert idx.snapshot()['passes'] == before
    assert not await items()
    assert await db._db.raw.count_documents({'index_pending': True}) == 2


async def test_parked_cover_sparse_recheck_never_seals_or_publishes(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(db, 'now', lambda: now[0])
    await send(post(1, 'cover'), post(2)); await drain()
    now[0] = 401
    assert await idx.drain_once() == 0
    assert not await items()
    job = await db._db.root_index_jobs.find_one({'_id': 'demo'})
    assert job['ready_at'] == 701


async def test_new_cover_reactivates_parked_group():
    await send(post(1, 'cover'), post(2)); await drain()
    await send(post(3, 'cover'))
    job = await db._db.root_index_jobs.find_one({'_id': 'demo'})
    assert 'waiting_for_source' not in job and 'retry_at' not in job
    assert await drain() == 1
    assert [d['db_message_id'] for d in await items()] == [1]


async def test_explicit_seal_reactivates_without_waiting_five_minutes():
    await send(post(1, 'cover'), post(2)); await drain()
    assert await idx.seal('demo') == 2
    assert await drain() == 1
    assert [d['db_message_id'] for d in await items()] == [1]


async def test_error_backoff_not_bypassed_by_eight_second_debounce(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(db, 'now', lambda: now[0])
    await send(post(1, 'cover'), post(2), post(3, 'cover'))
    real = idx.process_job
    process = AsyncMock(side_effect=RuntimeError('write failed'))
    monkeypatch.setattr(idx, 'process_job', process)
    await drain()
    now[0] = 109
    assert await idx.drain_once() == 0 and process.await_count == 1
    now[0] = 116
    monkeypatch.setattr(idx, 'process_job', real)
    assert await idx.drain_once() == 1


async def test_new_notify_clears_error_backoff(monkeypatch):
    await send(post(1, 'cover'), post(2), post(3, 'cover'))
    real = idx.process_job
    monkeypatch.setattr(idx, 'process_job', AsyncMock(side_effect=RuntimeError('offline')))
    await drain()
    await send(post(4), post(5, 'cover'))
    assert 'retry_at' not in (await db._db.root_index_jobs.find_one({'_id': 'demo'}))
    monkeypatch.setattr(idx, 'process_job', real)
    assert await drain() == 2


async def test_continuous_ingress_still_gets_max_wait_processing(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(db, 'now', lambda: now[0])
    await send(post(1, 'cover'), post(2), post(3, 'cover'))
    now[0] = 109
    await idx.notify('demo', 1, await idx.sequence())
    assert (await db._db.root_index_jobs.find_one({'_id': 'demo'}))['ready_at'] == 111
    assert await idx.drain_once() == 1


async def test_stale_open_pass_cannot_park_new_source_generation(monkeypatch):
    await send(post(1, 'cover'), post(2))
    job = await db._db.root_index_jobs.find_one({'_id': 'demo'})
    await send(post(3, 'cover'))
    async with idx.lease() as fence:
        await idx.process_job(job, fence)
    latest = await db._db.root_index_jobs.find_one({'_id': 'demo'})
    assert latest['seq'] > job['seq']
    assert 'waiting_for_source' not in latest
    assert await drain() == 1


async def test_stale_error_cannot_delay_new_source_generation(monkeypatch):
    await send(post(1, 'cover'), post(2))
    async def failed(job, fence):
        await send(post(3, 'cover'))
        raise RuntimeError('old generation')
    real = idx.process_job
    monkeypatch.setattr(idx, 'process_job', failed)
    await drain()
    latest = await db._db.root_index_jobs.find_one({'_id': 'demo'})
    assert 'retry_at' not in latest
    monkeypatch.setattr(idx, 'process_job', real)
    assert await drain() == 1


async def test_indexer_idle_loop_sleeps_without_one_second_poll(monkeypatch):
    poll = AsyncMock(return_value=0)
    monkeypatch.setattr(idx, 'drain_once', poll)
    idx.start()
    await asyncio.sleep(.15)
    assert poll.await_count == 1
    assert poll.call_args.kwargs['recover'] is True


async def test_notification_wakes_indexer_not_only_fallback_timer(monkeypatch):
    monkeypatch.setattr(idx, 'QUIET_SECONDS', .01)
    monkeypatch.setattr(idx, 'TICK_SECONDS', .005)
    idx.start()
    await asyncio.sleep(.025)
    await send(post(1, 'cover'), post(2), post(3, 'cover'))
    async def one(): return len(await items()) == 1
    await until(one, .5)


async def test_periodic_recovery_finds_raw_job_write_gap(monkeypatch):
    monkeypatch.setattr(idx, 'IDLE_POLL_SECONDS', .04)
    monkeypatch.setattr(idx, 'RECOVERY_SECONDS', .04)
    monkeypatch.setattr(idx, 'QUIET_SECONDS', .005)
    monkeypatch.setattr(idx, 'TICK_SECONDS', .005)
    idx.start()
    await asyncio.sleep(.02)
    notify = idx.notify
    monkeypatch.setattr(idx, 'notify', AsyncMock(side_effect=RuntimeError('job write failed')))
    with pytest.raises(RuntimeError):
        await send(post(1, 'cover'), post(2), post(3, 'cover'))
    monkeypatch.setattr(idx, 'notify', notify)
    async def one(): return len(await items()) == 1
    await until(one, 1)


async def test_indexer_next_delay_respects_retry_not_old_first_pending(monkeypatch):
    monkeypatch.setattr(db, 'now', lambda: 100.0)
    await db._db.root_index_jobs.insert_one({'_id': 'demo', 'ready_at': 115,
        'retry_at': 115, 'first_pending_at': 0})
    assert await idx.next_delay() == 15


async def test_indexer_next_delay_accounts_for_ingress_max_wait(monkeypatch):
    monkeypatch.setattr(db, 'now', lambda: 100.0)
    await db._db.root_index_jobs.insert_one({'_id': 'demo', 'ready_at': 115,
        'first_pending_at': 95})
    assert await idx.next_delay() == 3


async def test_parked_state_survives_restart_and_new_arrival():
    await send(post(1, 'cover'), post(2)); await drain()
    await idx.stop()
    assert (await db._db.root_index_jobs.find_one({'_id': 'demo'}))['waiting_for_source']
    await send(post(3, 'cover'))
    assert await drain() == 1


async def test_head_health_succeeds_without_405_and_db_failure_stays_503(monkeypatch):
    ping = AsyncMock()
    monkeypatch.setattr(db, 'ping', ping)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as c:
        r = await c.head('/health')
        assert r.status_code == 200 and r.content == b''
        ping.side_effect = RuntimeError('offline')
        assert (await c.head('/health')).status_code == 503
    assert ping.await_count == 2


async def test_no_lost_index_wakeup_between_query_and_sleep(monkeypatch):
    entered, finish, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0
    async def poll(**kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await finish.wait()
        else:
            second.set()
        return 0
    monkeypatch.setattr(idx, 'drain_once', poll)
    monkeypatch.setattr(idx, 'next_delay', AsyncMock(return_value=10))
    idx.start()
    await asyncio.wait_for(entered.wait(), 1)
    idx.wake_event().set()
    finish.set()
    await asyncio.wait_for(second.wait(), .5)


async def test_200_webhooks_durable_burst_preserves_fixed_worker_limit(monkeypatch):
    app = application()
    monkeypatch.setattr(main, 'bot2', app)
    dispatch.start(application(), app)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as c:
        slots = asyncio.Semaphore(8)
        async def submit(n):
            async with slots:
                for attempt in range(10):
                    r = await c.post('/bot2/webhook', json=payload(n),
                        headers={'X-Telegram-Bot-Api-Secret-Token': 'two'})
                    if r.status_code == 200:
                        return
                    assert r.status_code == 503
                    await asyncio.sleep(.01)
                raise AssertionError('Webhook retry budget exhausted')
        await asyncio.gather(*(submit(n) for n in range(200)))
    async def complete():
        return await db._db.root_update_inbox.count_documents({'state': 'done'}) == 200
    await until(complete, 5)
    assert app.process_update.await_count == 200
    assert len(dispatch._tasks) == 5 and len(dispatch._events) == 4


async def test_counters_do_not_query_or_disclose_payloads():
    snap = idx.snapshot()
    assert set(snap) == {'passes', 'waits'}
    snap['passes'] = -999
    assert idx.snapshot()['passes'] != -999
