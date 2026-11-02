"""Durable root webhook inbox + four fixed processing workers.
Bot1 channel posts bypass generic dispatch and stage compact metadata before
HTTP 200. All other updates are persisted before acknowledgement, with
per-bot/update-id idempotence. No per-request detached processing tasks.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time
import uuid
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
import db
import root_index

log = logging.getLogger('root.dispatch')
WORKERS_PER_BOT = 2
INGRESS_LIMIT = 8
MAX_BODY_BYTES = 256 * 1024
PROCESS_TIMEOUT = 3600  # preserve paced broadcasts and explicit history scans
LEASE_SECONDS = PROCESS_TIMEOUT + 60
KEEP_COMPLETED_HOURS = 48  # compact receipts outlive Telegram's retry window
IDLE_POLL_SECONDS = 30.0  # recovery only; persisted local ingress wakes immediately
RECOVERY_SECONDS = 30.0
ERROR_SECONDS = 5.0
_events = {}
_event_loop = None
_recovery_at = {}
_counters = {'claims': 0, 'empty_claims': 0, 'recoveries': 0}
_tasks = []
_slots = None
_loop = None
_owner = uuid.uuid4().hex
_active = 0
_peak_active = 0


def ingress_slots():
    global _slots, _loop
    loop = asyncio.get_running_loop()
    if _loop is not loop:
        _slots, _loop = asyncio.Semaphore(INGRESS_LIMIT), loop
    return _slots


def worker_event(name, worker_id):
    """One bounded wake flag per fixed worker; never an in-memory payload queue."""
    global _event_loop
    loop = asyncio.get_running_loop()
    if _event_loop is not loop:
        _events.clear()
        _recovery_at.clear()
        _event_loop = loop
    return _events.setdefault((name, worker_id), asyncio.Event())


def wake(name):
    for (bot_name, _), event in tuple(_events.items()):
        if bot_name == name:
            event.set()


async def recover_expired(name):
    """Rate-limited per bot, shared by its two workers. Never replay side effects."""
    current = time.monotonic()
    if current < _recovery_at.get(name, 0):
        return
    # Reserve before await so simultaneous workers cannot duplicate the sweep.
    _recovery_at[name] = current + RECOVERY_SECONDS
    try:
        _counters['recoveries'] += 1
        await db._db.root_update_inbox.update_many(
            {'bot': name, 'state': 'running', 'lease_until': {'$lte': db.now()}},
            {'$set': {'state': 'review', 'last_error': 'interrupted_handler'}})
    except BaseException:
        _recovery_at.pop(name, None)
        raise


async def ensure_indexes():
    await db._db.root_update_inbox.create_index([('bot', 1), ('state', 1), ('ready_at', 1)])
    await db._db.root_update_inbox.create_index('expires_at', expireAfterSeconds=0)


async def enqueue(bot_name, payload):
    ident = f"{bot_name}:{int(payload['update_id'])}"
    # Retained completion receipts prevent retry replays of side effects.
    await db._db.root_update_inbox.update_one({'_id': ident}, {'$setOnInsert': {
        'bot': bot_name, 'payload': payload, 'state': 'pending',
        'ready_at': db.now(), 'attempts': 0}}, upsert=True)
    # Set only AFTER the write succeeds. Startup/fallback polling covers a
    # crash between persistence and this process-local latency optimization.
    wake(bot_name)


async def drain_once(name, application):
    global _active, _peak_active
    now = db.now()
    ticket = uuid.uuid4().hex
    await recover_expired(name)
    _counters['claims'] += 1
    doc = await db._db.root_update_inbox.find_one_and_update(
        {'bot': name, 'ready_at': {'$lte': now}, 'state': 'pending'},
        {'$set': {'state': 'running', 'owner': _owner, 'ticket': ticket,
                  'lease_until': now + LEASE_SECONDS}, '$inc': {'attempts': 1}},
        sort=[('ready_at', 1)], return_document=ReturnDocument.AFTER)
    if not doc:
        _counters['empty_claims'] += 1
        return False
    _active += 1
    _peak_active = max(_peak_active, _active)
    q = {'_id': doc['_id'], 'ticket': ticket}
    try:
        from telegram import Update
        update = Update.de_json(doc['payload'], application.bot)
        # Startup-recovered accepted channel updates also use the staging path.
        if name == 'bot1' and (update.channel_post or update.edited_channel_post):
            await root_index.stage_message(update.channel_post or update.edited_channel_post, update.update_id)
        else:
            await asyncio.wait_for(application.process_update(update), timeout=PROCESS_TIMEOUT)
        await db._db.root_update_inbox.update_one(q, {'$set': {
            'state': 'done', 'completed_at': db.now(),
            'expires_at': datetime.now(timezone.utc) + timedelta(hours=KEEP_COMPLETED_HOURS)},
            '$unset': {'payload': '', 'lease_until': ''}})
    except asyncio.CancelledError:
        await db._db.root_update_inbox.update_one(q, {'$set': {'state': 'review', 'last_error': 'interrupted_handler'},
                                                      '$unset': {'lease_until': ''}})
        raise
    except Exception as exc:
        # Handlers may have sent messages/spent tokens before an exception.
        # Never automatically replay uncertain side effects. Channel staging
        # is idempotent and safe to retry; other errors need admin review.
        is_channel = name == 'bot1' and bool(doc['payload'].get('channel_post') or doc['payload'].get('edited_channel_post'))
        await db._db.root_update_inbox.update_one(q, {'$set': {
            'state': 'pending' if is_channel else 'review',
            'ready_at': db.now() + 15, 'last_error': type(exc).__name__}, '$unset': {'lease_until': ''}})
        log.warning('Webhook worker error bot=%s kind=%s; credentials not logged', name, type(exc).__name__)
    finally:
        _active -= 1
    return True


async def loop(name, application, worker_id=0):
    event = worker_event(name, worker_id)
    while True:
        # Clear BEFORE querying. An enqueue during/after an empty query stays
        # signalled, so it cannot be lost in the query-to-wait race.
        event.clear()
        delay = IDLE_POLL_SECONDS
        try:
            found = await drain_once(name, application)
        except Exception as exc:
            found = False
            delay = ERROR_SECONDS
            log.warning('Inbox retry bot=%s error=%s', name, type(exc).__name__)
        if not found:
            try:
                await asyncio.wait_for(event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass


async def metrics_loop():
    import psutil
    while True:
        try:
            queued = await db._db.root_update_inbox.count_documents({'state': {'$in': ['pending', 'running', 'review']}})
            jobs = await db._db.root_index_jobs.count_documents({})
            log.info('Root resources rss_mib=%.1f active_updates=%s pending_updates=%s index_categories=%s '
                'inbox_claims_total=%s inbox_empty_total=%s inbox_recoveries_total=%s '
                'index_passes_total=%s index_waits_total=%s',
                psutil.Process().memory_info().rss / (1024 * 1024), _active, queued, jobs,
                _counters['claims'], _counters['empty_claims'], _counters['recoveries'],
                root_index.snapshot()['passes'], root_index.snapshot()['waits'])
        except Exception as exc:
            log.warning('Resource metrics unavailable error=%s', type(exc).__name__)
        await asyncio.sleep(60)


def start(bot1, bot2):
    if _tasks:
        return
    for name, app in (('bot1', bot1), ('bot2', bot2)):
        for i in range(WORKERS_PER_BOT):
            _tasks.append(asyncio.create_task(loop(name, app, i), name=f'root-{name}-worker-{i}'))
    _tasks.append(asyncio.create_task(metrics_loop(), name='root-resources'))


async def stop():
    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    _events.clear()
    _recovery_at.clear()
