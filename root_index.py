"""Durable, bounded, cover-boundary indexing for ROOT Bots 1/2 only.
No live full-history rebuild. Raw metadata is the journal. One heavy worker;
source staging and each complete-block commit share a category lock. A Mongo
lease plus monotonic item fences protects overlapping service instances.
A timeout NEVER declares an open cover complete. Missing live message IDs
block publication; an explicit owner seal can confirm the final snapshot.
"""
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary
from bson import BSON
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, BulkWriteError
import db

log = logging.getLogger('root.index')
QUIET_SECONDS = 2.0
MAX_WAIT_SECONDS = 8.0
TICK_SECONDS = 1.0
RETRY_SECONDS = 15.0
LEASE_SECONDS = 60.0
MAX_GROUP_MESSAGES = 10000
MAX_GROUP_BYTES = 8 * 1024 * 1024
MAX_BLOCKS_PER_PASS = 25
PASS_SECONDS = 5.0
_locks = WeakValueDictionary()
_slots = None
_loop = None
_worker = None
_owner = uuid.uuid4().hex


def category_lock(category):
    key = db._norm_key(category)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def heavy_slot():
    global _slots, _loop
    loop = asyncio.get_running_loop()
    if _loop is not loop:
        _slots, _loop = asyncio.Semaphore(1), loop
    return _slots


async def ensure_indexes():
    await db._db.raw.create_index([('category', 1), ('kind', 1), ('message_id', 1)])
    await db._db.raw.create_index([('category', 1), ('index_pending', 1), ('message_id', 1)])
    await db._db.raw.create_index([('_job_notified', 1), ('_ingest_seq', 1)])
    await db._db.root_index_jobs.create_index('ready_at')


async def sequence():
    doc = await db._db.root_index_clock.find_one_and_update({'_id': 'clock'},
        {'$inc': {'seq': 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    return doc['seq']


async def ensure_state(category):
    cat = db._norm_key(category)
    state = await db._db.root_index_state.find_one({'_id': cat})
    if state:
        return state
    tail = await db._db.raw.find_one({'category': cat}, sort=[('message_id', -1)])
    # Legacy raw never stored text/service messages, so its ID gaps are trusted.
    # This does NOT seal the last cover, nor alter any existing file record.
    state = {'_id': cat, 'trusted_through': (tail or {}).get('message_id', 0),
             'sealed_through': 0, 'created_at': db.now()}
    await db._db.root_index_state.update_one({'_id': cat}, {'$setOnInsert': state}, upsert=True)
    return await db._db.root_index_state.find_one({'_id': cat})


async def notify(category, mid, seq):
    cat, now = db._norm_key(category), db.now()
    await db._db.root_index_jobs.update_one({'_id': cat}, {
        '$min': {'dirty_from': int(mid)}, '$max': {'seq': seq},
        '$set': {'ready_at': now + QUIET_SECONDS, 'last_received_at': now},
        '$setOnInsert': {'first_pending_at': now}}, upsert=True)
    await db._db.raw.update_many({'category': cat, '_ingest_seq': {'$lte': seq},
                                  '_job_notified': False}, {'$set': {'_job_notified': True}})


def source_rank(epoch=0, update_id=0, live=False):
    return f'{max(0,int(epoch)):020}:{1 if live else 0}:{max(0,int(update_id)):020}'


async def stage(entries, category, *, ranks=None):
    """At most 100 metadata rows per call. Persist-before-ack, retry-idempotent.
    Source revisions reject delayed events/scans. Pending raw repairs the
    raw-write/job-write crash gap. No video bytes or user payloads here.
    """
    from pymongo import UpdateOne
    cat = db._norm_key(category)
    if not entries:
        return 0
    if len(entries) > 100:
        raise ValueError('stage batches must contain at most 100 rows')
    async with category_lock(cat):
        await ensure_state(cat)
        seq = await sequence()
        operations = []
        for i, entry in enumerate(entries):
            rank = ranks[i] if ranks is not None else source_rank()
            mid = int(entry['message_id'])
            q = {'category': cat, 'message_id': mid, '$or': [
                {'_source_rank': {'$exists': False}}, {'_source_rank': {'$lt': rank}}]}
            fields = {k: entry[k] for k in ('message_id', 'kind', 'caption', 'file_id') if k in entry}
            fields.update(category=cat, _source_rank=rank, _ingest_seq=seq,
                          index_pending=True, _job_notified=False)
            operations.append(UpdateOne(q, {'$set': fields}, upsert=True))
        try:
            result = await db._db.raw.bulk_write(operations, ordered=False)
            changed = result.modified_count + result.upserted_count
        except BulkWriteError as exc:
            details = exc.details
            if details.get('writeConcernErrors') or any(e.get('code') != 11000 for e in details.get('writeErrors', [])):
                raise
            # Unique (category,message_id) + guarded upsert: duplicate means an
            # equal/newer source revision already exists; never overwrite it.
            changed = details.get('nModified', 0) + details.get('nUpserted', 0)
        # Even a duplicate retry repairs a failed job notification.
        pending = await db._db.raw.find_one({'category': cat, 'index_pending': True}, sort=[('message_id', 1)])
        if pending:
            await notify(cat, pending['message_id'], max(seq, pending.get('_ingest_seq', 0)))
        return changed


async def stage_message(message, update_id=0):
    import scanner
    cat = await db.category_for_db_channel(message.chat_id)
    if cat:
        category = cat['key']
    else:
        settings = await db.get_settings()
        if not settings.get('db_channel_id') or int(message.chat_id) != int(settings['db_channel_id']):
            return False
        category = ''
    kind = scanner.classify_from_botapi(message) or 'skip'
    entry = {'message_id': message.message_id, 'kind': kind, 'caption': message.caption or ''}
    if kind == 'cover' and message.photo:
        entry['file_id'] = message.photo[-1].file_id
    date = getattr(message, 'edit_date', None) or getattr(message, 'date', None)
    epoch = date.timestamp() if date else 0
    await stage([entry], category, ranks=[source_rank(epoch, update_id, True)])
    return True


async def bootstrap():
    """One-time tail-only recovery of legacy staging. Never reset posted flags."""
    async for cat in db._db.categories.find({}, {'key': 1}):
        key = db._norm_key(cat['key'])
        async with category_lock(key):
            state = await ensure_state(key)
            if state.get('bootstrapped'):
                continue
            last = await db._db.files.find_one({'category': key}, {'db_message_id': 1}, sort=[('db_message_id', -1)])
            floor = (last or {}).get('db_message_id', 0)
            seq = await sequence()
            tail_cover = await db._db.raw.find_one({'category': key, 'kind': 'cover'}, {'message_id': 1}, sort=[('message_id', -1)])
            if tail_cover and tail_cover['message_id'] > state.get('sealed_through', 0):
                await block(key, tail_cover['message_id'], 'legacy tail awaiting next cover or owner confirmation', seq)
            await db._db.raw.update_many({'category': key, 'message_id': {'$gte': floor},
                                         'index_pending': {'$ne': True}},
                {'$set': {'index_pending': True, '_job_notified': False, '_ingest_seq': seq}})
            first = await db._db.raw.find_one({'category': key, 'index_pending': True}, sort=[('message_id', 1)])
            if first:
                await notify(key, first['message_id'], seq)
            await db._db.root_index_state.update_one({'_id': key}, {'$set': {'bootstrapped': True}})


async def recover_notification():
    row = await db._db.raw.find_one({'_job_notified': False}, sort=[('_ingest_seq', 1)])
    if row:
        await notify(row['category'], row['message_id'], row.get('_ingest_seq', 0))


@asynccontextmanager
async def lease():
    """A cross-instance lease, with item-level fencing for delayed old writes."""
    async with heavy_slot():
        now = db.now()
        try:
            token = await db._db.root_index_clock.find_one_and_update(
                {'_id': 'lease', '$or': [{'until': {'$lte': now}}, {'until': {'$exists': False}}]},
                {'$set': {'owner': _owner, 'until': now + LEASE_SECONDS}},
                upsert=True, return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            token = None
        if token is None:
            yield None
            return
        fence = await sequence()
        try:
            yield fence
        finally:
            await db._db.root_index_clock.update_one({'_id': 'lease', 'owner': _owner},
                {'$set': {'until': 0}})


async def renew():
    result = await db._db.root_index_clock.update_one(
        {'_id': 'lease', 'owner': _owner, 'until': {'$gt': db.now()}},
        {'$set': {'until': db.now() + LEASE_SECONDS}})
    if not result.matched_count:
        raise RuntimeError('index lease lost; retained source will retry')


async def commit(item, category, fence):
    """One atomic whole-block write. No delete-many reconciliation. Legacy IDs,
    posted flags, counters, tokens and all unrelated fields remain untouched.
    """
    cat = db._norm_key(category)
    fields = {k: item[k] for k in ('cover_message_id', 'cover_file_id', 'caption', 'videos', 'srts')}
    fields.update(_index_fence=fence, index_complete=True, indexed_at=db.now())
    q = {'category': cat, 'db_message_id': item['db_message_id'], '$or': [
        {'_index_fence': {'$exists': False}}, {'_index_fence': {'$lte': fence}}]}
    try:
        result = await db._db.files.update_one(q, {'$set': fields, '$setOnInsert': {
            'file_id': db.make_file_id(cat, item['db_message_id']) if cat else f"f{item['db_message_id']}",
            'posted': False, 'created_at': db.now()}}, upsert=True)
    except DuplicateKeyError:
        return False  # a newer committed generation wins, never retry stale write
    if cat and item.get('cover_message_id'):
        saved = await db._db.files.find_one({'category': cat, 'db_message_id': item['db_message_id']}, {'file_id': 1})
        file_id = (saved or {}).get('file_id', db.make_file_id(cat, item['db_message_id']))
        # Refresh only THIS item's genre membership, including removals after
        # caption edits. Never materialize all file captions/match arrays.
        async for genre in db._db.genres.find({'category': cat, 'scanned': True}, {'genre': 1}):
            await renew()
            if not await db._db.files.find_one({'category': cat, 'db_message_id': item['db_message_id'], '_index_fence': fence}, {'_id': 1}):
                break
            operation = '$addToSet' if db._genre_in_caption(genre['genre'], item.get('caption') or '') else '$pull'
            await db._db.genres.update_one({'_id': genre['_id']}, {operation: {'matched': file_id}})
    return bool(result.matched_count or result.upserted_id)


async def block(category, cover_id, reason, fence):
    # Existing published downloads remain usable. Unpublished legacy partials
    # are excluded from daily posting until a complete version can be committed.
    await db._db.files.update_one({'category': category, 'db_message_id': cover_id,
        'posted': False, '$or': [{'_index_fence': {'$exists': False}}, {'_index_fence': {'$lte': fence}}]},
        {'$set': {'index_complete': False, '_index_fence': fence}})
    await db._db.root_index_state.update_one({'_id': category}, {'$set': {
        'waiting_cover': cover_id, 'waiting_reason': reason, 'last_checked_at': db.now()}})


async def process_block(category, start, job_seq, fence, *, trusted=0, sealed=0, ceiling=None):
    """Stream only one cover block. IDs must be contiguous for new live data.
    Missing/late source rows hold the block even after its next cover arrived.
    """
    cat = db._norm_key(category)
    async with category_lock(cat):
        await renew()
        first = await db._db.raw.find_one({'category': cat, 'kind': 'cover', 'message_id': {'$lte': start}}, sort=[('message_id', -1)])
        if not first:
            first = await db._db.raw.find_one({'category': cat, 'message_id': {'$lte': start}}, sort=[('message_id', 1)])
        if not first:
            return None, False
        lower = first['message_id']
        if first['kind'] != 'cover' and max(trusted, sealed) < lower:
            await block(cat, lower, 'leading media: awaiting its cover or explicit owner confirmation', fence)
            return None, False
        upper_query = {'$gt': lower}
        if ceiling is not None:
            upper_query['$lte'] = ceiling
        next_cover = await db._db.raw.find_one({'category': cat, 'kind': 'cover', 'message_id': upper_query}, sort=[('message_id', 1)])
        upper = next_cover['message_id'] - 1 if next_cover else sealed
        if not next_cover and upper < max(lower, start):
            await block(cat, lower, 'open cover: waiting for next cover or /flushindex', fence)
            return None, False
        rows, size, count, expected, gap, newer = [], 0, 0, lower, False, False
        cursor = db._db.raw.find({'category': cat, 'message_id': {'$gte': lower, '$lte': upper}}).sort('message_id', 1).batch_size(100)
        async for row in cursor:
            mid = row['message_id']
            if mid != expected and mid - 1 > trusted:
                gap = True
            expected = mid + 1
            newer = newer or row.get('_ingest_seq', 0) > job_seq
            size += len(BSON.encode(row)); count += 1
            if count > MAX_GROUP_MESSAGES or size > MAX_GROUP_BYTES:
                await block(cat, lower, 'block too large; owner review required (no partial write)', fence)
                return None, False
            if row['kind'] != 'skip':
                rows.append(row)
        if expected <= upper and upper > trusted:
            gap = True
        if gap or newer:
            await block(cat, lower, 'missing source IDs or newer arrivals: waiting safely', fence)
            return None, False
        groups = db.group_items(rows, cat)
        if len(groups) > 1:
            raise RuntimeError('unexpected cover boundary; block retained')
        if groups:
            await renew()
            if not await commit(groups[0], cat, fence):
                return None, False
        await db._db.raw.update_many({'category': cat, 'message_id': {'$gte': lower, '$lte': upper},
            '$or': [{'_ingest_seq': {'$lte': job_seq}}, {'_ingest_seq': {'$exists': False}}]},
            {'$set': {'index_pending': False, '_job_notified': True}})
        return (next_cover['message_id'] if next_cover else None), True


async def process_job(job, fence):
    cat, started = job['_id'], time.monotonic()
    state = await ensure_state(cat)
    current, blocks, waiting = job['dirty_from'], 0, False
    trusted = max(state.get('trusted_through', 0), state.get('sealed_through', 0))
    while current is not None and blocks < MAX_BLOCKS_PER_PASS:
        current, completed = await process_block(cat, current, job.get('seq', 0), fence,
            trusted=trusted, sealed=state.get('sealed_through', 0))
        if not completed:
            waiting = True
            break
        blocks += 1
        if time.monotonic() - started >= PASS_SECONDS:
            break
        await asyncio.sleep(0)
    async with category_lock(cat):
        pending = await db._db.raw.find_one({'category': cat, 'index_pending': True}, sort=[('message_id', 1)])
        latest = await db._db.root_index_jobs.find_one({'_id': cat})
        if latest:
            q = {'_id': cat, 'seq': latest.get('seq', 0)}
            if pending:
                await db._db.root_index_jobs.update_one(q, {'$set': {
                    'dirty_from': pending['message_id'], 'ready_at': db.now() + (RETRY_SECONDS if waiting else TICK_SECONDS),
                    'first_pending_at': db.now(), 'last_blocks': blocks}})
            else:
                await db._db.root_index_jobs.delete_one(q)
                await db._db.root_index_state.update_one({'_id': cat}, {'$set': {
                    'waiting_cover': None, 'waiting_reason': 'ready', 'last_checked_at': db.now()}})
    if blocks:
        log.info('Indexed closed blocks category=%s blocks=%s', cat, blocks)
    return blocks


async def drain_once():
    await recover_notification()
    now = db.now()
    job = await db._db.root_index_jobs.find_one({'$or': [
        {'ready_at': {'$lte': now}}, {'first_pending_at': {'$lte': now - MAX_WAIT_SECONDS}}]}, sort=[('ready_at', 1)])
    if not job:
        return 0
    async with lease() as fence:
        if fence is None:
            return 0
        try:
            return await process_job(job, fence)
        except Exception as exc:
            log.warning('Index retry category=%s error=%s; source retained', job['_id'], type(exc).__name__)
            await db._db.root_index_jobs.update_one({'_id': job['_id']}, {'$set': {
                'ready_at': db.now() + RETRY_SECONDS, 'first_pending_at': db.now(), 'last_error': type(exc).__name__}})
            return 0


async def seal(category):
    cat = db._norm_key(category)
    async with category_lock(cat):
        await ensure_state(cat)
        last = await db._db.raw.find_one({'category': cat}, sort=[('message_id', -1)])
        if not last:
            return 0
        high = last['message_id']
        await db._db.root_index_state.update_one({'_id': cat}, {'$max': {'sealed_through': high}})
        seq = await sequence()
        first = await db._db.raw.find_one({'category': cat, 'index_pending': True}, sort=[('message_id', 1)])
        floor = first['message_id'] if first else high
        await db._db.raw.update_many({'category': cat, 'message_id': {'$gte': floor}}, {'$set': {'index_pending': True}})
        await notify(cat, floor, seq)
        return high


async def rebuild(category=None, end_id=None, include_final=True):
    """Explicit metadata maintenance, streaming block-by-block, no destructive
    pruning. An explicit caller confirms its existing snapshot, including the
    final block. Live channel handlers MUST use stage_message, not this API.
    """
    cat = db._norm_key(category)
    async with category_lock(cat):
        await ensure_state(cat)
        last = await db._db.raw.find_one({'category': cat}, sort=[('message_id', -1)])
        if not last:
            return 0
        high = min(last['message_id'], end_id) if end_id is not None else last['message_id']
        seq = await sequence()
    total, current = 0, 0
    async with lease() as fence:
        if fence is None:
            raise RuntimeError('Indexer busy; retry maintenance later')
        first = await db._db.raw.find_one({'category': cat}, sort=[('message_id', 1)])
        current = first['message_id']
        while current is not None and current <= high:
            current, completed = await process_block(cat, current, seq, fence, trusted=high, sealed=high if include_final else 0, ceiling=high)
            if not completed:
                break
            total += 1
            await asyncio.sleep(0)
    return total


async def status(category=None):
    query = {'_id': db._norm_key(category)} if category is not None else {}
    states = await db._db.root_index_state.find(query).limit(30).to_list(30)
    lines = ['Source index: one bounded worker; latest open cover is staged, not published.']
    for state in states:
        cat = state['_id']
        job = await db._db.root_index_jobs.find_one({'_id': cat})
        pending = await db._db.raw.count_documents({'category': cat, 'index_pending': True})
        lines.append(f"[{cat or 'legacy'}] pending metadata={pending}; "
            f"waiting cover={state.get('waiting_cover', 'none')}; {state.get('waiting_reason', 'ready')}; "
            f"job={'queued' if job else 'idle'}")
    return '\n'.join(lines)


async def worker_loop():
    while True:
        try:
            await drain_once()
        except Exception as exc:
            log.warning('Index loop retry error=%s', type(exc).__name__)
        await asyncio.sleep(TICK_SECONDS)


def start():
    global _worker
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(worker_loop(), name='root-index-single-worker')


async def stop():
    global _worker
    if _worker:
        _worker.cancel()
        await asyncio.gather(_worker, return_exceptions=True)
        _worker = None
