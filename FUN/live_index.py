"""Durable Bot API channel-post ingestion, independent of the user session.
Only compact metadata enters Mongo; one worker streams one channel at a time.
Historical scan_state is deliberately NOT advanced by this partial-history path.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import db
from classifier import classify
from locks import channel_lock

log = logging.getLogger('fun.live_index')
QUIET_SECONDS = 3
MAX_BATCH_WAIT = 10
TICK_SECONDS = 2
RETRY_SECONDS = 30
JOURNAL_DAYS = 7
_last = {'state': 'starting', 'last_channel_at': None, 'last_error_type': None}


async def ensure_indexes():
    await db.col('source_posts').create_index([('channel_id', 1), ('message_id', 1)], unique=True)
    await db.col('source_posts').create_index('expires_on', expireAfterSeconds=0)
    await db.col('source_jobs').create_index('ready_at')
    await db.col('collections').create_index([('channel_id', 1), ('cover.message_id', 1)])
    await db.col('collections').create_index([('channel_id', 1), ('files.message_id', 1)])
    await db.col('collections').create_index([('channel_id', 1), ('cover.grouped_id', 1)])
    await db.col('source_posts').create_index([('channel_id', 1), ('grouped_id', 1), ('message_id', 1)])
    await db.col('pending_files').create_index([('channel_id', 1), ('file.message_id', 1)])


def post_metadata(message):
    """Keep bot-bound photo/thumbnail references; never download delivery videos."""
    kind, media = None, None
    if message.photo:
        kind, media = 'photo', message.photo[-1]
    elif message.video:
        kind, media = 'video', message.video
    elif message.document:
        kind, media = 'document', message.document
    elif message.animation:
        kind, media = 'animation', message.animation
    elif message.audio:
        kind, media = 'audio', message.audio
    thumb = getattr(media, 'thumbnail', None)
    duration = getattr(media, 'duration', None)
    if hasattr(duration, 'total_seconds'):
        duration = duration.total_seconds()
    return {'message_id': message.message_id, 'grouped_id': message.media_group_id,
            'media_type': kind, 'caption': message.caption or '',
            'duration': duration, 'date_ts': message.date.timestamp(),
            'photo_file_id': media.file_id if kind == 'photo' else None,
            'thumbnail_file_id': thumb.file_id if thumb else None}


async def enqueue(message):
    """Called before webhook 200. Failure propagates so Telegram can retry."""
    cid = int(message.chat_id)
    if not await db.category_for_channel(cid):
        return False
    post = post_metadata(message)
    if not post['media_type']:
        return False
    mid, now = int(post['message_id']), db.now_ts()
    await db.col('source_posts').update_one({'_id': f'{cid}:{mid}'},
        {'$setOnInsert': {'channel_id': cid, **post}, '$unset': {'expires_on': ''}}, upsert=True)
    # Retried webhook after journal write/job-write failure still repairs the job.
    await db.col('source_jobs').update_one({'_id': cid}, {
        '$min': {'dirty_from': mid}, '$inc': {'version': 1},
        '$set': {'ready_at': now + QUIET_SECONDS, 'last_received_at': now},
        '$setOnInsert': {'first_pending_at': now}}, upsert=True)
    _last['last_channel_at'] = now
    return True


async def _remove_files(cid, message_ids, except_collection=None):
    """Reconcile late album members/late covers without touching paid metadata."""
    query = {'channel_id': cid, 'files.message_id': {'$in': message_ids}}
    if except_collection:
        query['collection_id'] = {'$ne': except_collection}
    ids = set(message_ids)
    async for doc in db.col('collections').find(query):
        files = [f for f in doc.get('files', []) if f['message_id'] not in ids]
        await db.col('collections').update_one({'collection_id': doc['collection_id']},
            {'$set': {'files': files, 'file_count': len(files)}})


async def _process_unit(cid, category, unit, covers_to_prepare):
    result = classify(unit, cid, category)
    ids = [p['message_id'] for p in unit]
    if result['collections']:
        doc = result['collections'][0]
        from scanner import existing_cover
        existing = await existing_cover(doc)
        key = existing['collection_id'] if existing else doc['collection_id']
        doc['collection_id'] = key
        doc['ingest_source'] = 'bot_api'
        await db.col('collections').update_one({'collection_id': key},
            {'$setOnInsert': doc}, upsert=True)
        # Do not overwrite owner price/status/counters, cached hosts, or old files.
        fields = {'category': category, 'title': doc['title'],
                  'cover.caption': doc['cover']['caption'],
                  'cover.grouped_id': doc['cover'].get('grouped_id')}
        first = unit[0]
        if first.get('photo_file_id'):
            fields['cover.telegram_file_id'] = first['photo_file_id']
        if first.get('thumbnail_file_id'):
            fields['cover.thumbnail_file_id'] = first['thumbnail_file_id']
        await db.col('collections').update_one({'collection_id': key}, {
            '$set': fields, '$min': {'cover.message_id': doc['cover']['message_id']},
            '$addToSet': {'cover.album_message_ids': {'$each': ids}}})
        await _remove_files(cid, ids)
        await db.col('pending_files').delete_many({'channel_id': cid, 'file.message_id': {'$in': ids}})
        covers_to_prepare.add(key)
        return
    # Query by source message ID, not timestamp. Handles delayed/out-of-order
    # webhook delivery and appends to legacy userbot-indexed collections too.
    target = await db.col('collections').find_one(
        {'channel_id': cid, 'cover.message_id': {'$lte': ids[0]}},
        sort=[('cover.message_id', -1)])
    if target:
        key = target['collection_id']
        cover_ids = set((target.get('cover') or {}).get('album_message_ids') or [])
        files = [f for f in result['pending'] if f['message_id'] not in cover_ids]
        await _remove_files(cid, [f['message_id'] for f in files], except_collection=key)
        existing = {f['message_id'] for f in target.get('files', [])}
        new = [f for f in files if f['message_id'] not in existing]
        if new:
            merged = sorted(target.get('files', []) + new, key=lambda f: f['message_id'])
            await db.col('collections').update_one({'collection_id': key}, {
                '$set': {'files': merged, 'file_count': len(merged)}})
        await db.col('pending_files').delete_many({'channel_id': cid, 'file.message_id': {'$in': ids}})
    else:
        for f in result['pending']:
            await db.col('pending_files').update_one({'_id': f"{cid}:{f['message_id']}"},
                {'$set': {'channel_id': cid, 'file': f,
                          'reason': 'media before first identifiable cover'}}, upsert=True)


async def process_channel(job):
    """Replay only the affected tail; atomic per-document writes are retry-safe.
    Journal expiry begins only AFTER successful processing. Pending rows have no TTL.
    """
    cid = int(job['_id'])
    covers_to_prepare = set()
    async with channel_lock(cid):
        category = await db.category_for_channel(cid)
        if not category:
            # Explicit removal wins. Never resurrect a removed source mapping.
            await db.col('source_jobs').delete_one({'_id': cid, 'version': job['version']})
            await db.col('source_posts').delete_many({'channel_id': cid})
            return 0, covers_to_prepare
        floor = int(job['dirty_from'])
        row = await db.col('source_posts').find_one({'channel_id': cid, 'message_id': floor})
        if row and row.get('grouped_id'):
            first = await db.col('source_posts').find_one(
                {'channel_id': cid, 'grouped_id': row['grouped_id']}, sort=[('message_id', 1)])
            floor = min(floor, first['message_id'])
        previous = await db.col('collections').find_one(
            {'channel_id': cid, 'cover.message_id': {'$lte': floor}}, sort=[('cover.message_id', -1)])
        if previous:
            floor = min(floor, previous['cover']['message_id'])
        # Freeze the high-water mark; arrivals during this pass retain a new job
        # version and are processed in the next pass (including lower-ID arrivals).
        newest = await db.col('source_posts').find_one({'channel_id': cid}, sort=[('message_id', -1)])
        ceiling = newest['message_id'] if newest else floor
        query = {'channel_id': cid, 'message_id': {'$gte': floor, '$lte': ceiling}}
        unit, key, processed = [], None, 0
        cursor = db.col('source_posts').find(query).sort('message_id', 1).batch_size(100)
        async for post in cursor:
            next_key = ('album', post['grouped_id']) if post.get('grouped_id') else ('single', post['message_id'])
            if unit and next_key != key:
                await _process_unit(cid, category, unit, covers_to_prepare)
                unit = []
                # Give interactive handlers time during a large metadata catch-up.
                await asyncio.sleep(0)
            unit.append(post)
            key = next_key
            processed += 1
        if unit:
            await _process_unit(cid, category, unit, covers_to_prepare)
        expiry = datetime.now(timezone.utc) + timedelta(days=JOURNAL_DAYS)
        await db.col('source_posts').update_many(query, {'$set': {'expires_on': expiry}})
        await db.col('live_scan_state').update_one({'_id': cid}, {'$set': {
            'last_indexed_at': db.now_ts(), 'last_message_id': ceiling,
            'posts_processed': processed, 'last_error_type': None}}, upsert=True)
        await db.col('source_jobs').delete_one({'_id': cid, 'version': job['version']})
    return processed, covers_to_prepare


class BotCoverClient:
    """Adapter for the EXISTING separate cover worker. Small images only."""
    def __init__(self, bot):
        self.bot = bot

    async def get_messages(self, cid, ids):
        doc = await db.col('collections').find_one({'channel_id': int(cid), 'cover.message_id': int(ids)})
        cover = (doc or {}).get('cover') or {}
        fid = cover.get('telegram_file_id') or cover.get('thumbnail_file_id')
        return SimpleNamespace(photo=fid, media=fid) if fid else None

    async def download_media(self, media, file, **kwargs):
        import covers
        remote = await self.bot.get_file(media)
        if remote.file_size and remote.file_size > covers.MAX_BYTES:
            raise ValueError('Cover image too large')
        await remote.download_to_memory(out=file)
        if file.tell() > covers.MAX_BYTES:
            raise ValueError('Cover image too large')


async def drain_once(telegram_bot):
    now = db.now_ts()
    job = await db.col('source_jobs').find_one({'$or': [
        {'ready_at': {'$lte': now}},
        {'first_pending_at': {'$lte': now - MAX_BATCH_WAIT}, 'retry_at': {'$exists': False}}]},
        sort=[('ready_at', 1)])
    if not job:
        return False
    try:
        count, ids = await process_channel(job)
        if ids:
            import runtime
            # Reuse the independent, paced cover worker. Photos are immediately
            # viewable using this bot's own file_id even if hosts/session fail.
            runtime._queue_covers(BotCoverClient(telegram_bot), ids)
        _last.update(state='running', last_error_type=None)
        log.info('Live source indexed channel=%s metadata=%s', job['_id'], count)
    except Exception as exc:
        error = type(exc).__name__
        _last.update(state='retrying', last_error_type=error)
        await db.col('source_jobs').update_one({'_id': job['_id']}, {'$set': {
            'ready_at': now + RETRY_SECONDS, 'retry_at': now + RETRY_SECONDS,
            'last_error_type': error}}, upsert=False)
        log.warning('Live indexing retained for retry channel=%s error=%s', job['_id'], error)
    return True


async def loop(telegram_bot):
    _last['state'] = 'running'
    log.info('Channel-post queue running; userbot is only required for historical catch-up.')
    while True:
        try:
            await drain_once(telegram_bot)
        except Exception as exc:
            _last.update(state='retrying', last_error_type=type(exc).__name__)
            log.warning('Live queue tick failed error=%s; persisted jobs retained', type(exc).__name__)
        await asyncio.sleep(TICK_SECONDS)


def snapshot():
    return dict(_last)


async def status_text():
    import runtime
    pending = await db.col('source_jobs').count_documents({})
    client = runtime.userbot
    connected = bool(client) and (not callable(getattr(client, 'is_connected', None)) or client.is_connected())
    history = 'connected (see /userbot_status for authorization)' if connected else 'paused — use /setup_userbot for historical catch-up'
    recent = await db.col('live_scan_state').find({}).sort('last_indexed_at', -1).limit(5).to_list(5)
    progress = ''.join(f"\nChannel {d['_id']}: last indexed message {d['last_message_id']}" for d in recent)
    return (f"🎀 Source indexing\nNew channel posts: durable automatic queue\n"
            f"Worker: {_last['state']}\nPending channels: {pending}\n"
            f"History reader: {history}{progress}\n"
            "New covers need 3+ caption lines. Refresh Categories after indexing.\n"
            "Previously missed posts need the userbot once; /scan is not needed for each new post.")
