"""Random browsing with a durable, paged viewing trail.

feed_history keeps one small Mongo record per visited position. The session's
history list is just a bounded UI window, never the complete source of truth.
Starting again, opening a shared drop or delivering an older drop does not
discard the rest of the trail. Only collection IDs/metadata are stored.
"""
from datetime import datetime, timedelta, timezone
import uuid
import db
from locks import feed_lock

HISTORY_LIMIT = 200  # bounded UI window, NOT a retention limit
SEEN_LIMIT = 2000
SESSION_DAYS = 30
SCHEMA = 2


def _eligible(category=None):
    query = {'status': 'active', 'file_count': {'$gt': 0}}
    if category not in ('all', '', None):
        query['category'] = category
    return query


async def _collection(cid, category=None):
    return await db.col('collections').find_one({**_eligible(category), 'collection_id': cid})


async def _random(category, excluded):
    query = _eligible(category)
    if excluded:
        query['collection_id'] = {'$nin': list(excluded)}
    result = await db.col('collections').aggregate([
        {'$match': query}, {'$project': {'collection_id': 1}}, {'$sample': {'size': 1}}
    ]).to_list(length=1)
    return await _collection(result[0]['collection_id'], category) if result else None


async def _entry(uid, seq, cid, category, at=None):
    # Deterministic IDs also make an interrupted legacy migration idempotent.
    doc = {'_id': f'{int(uid)}:{int(seq)}', 'user_id': int(uid), 'seq': int(seq),
           'collection_id': cid, 'category': category or 'all',
           'at': db.now_ts() if at is None else at}
    await db.col('feed_history').update_one({'_id': doc['_id']}, {'$setOnInsert': doc}, upsert=True)
    return doc


async def _prepare(uid, state=None):
    state = state or await db.col('feed_sessions').find_one({'_id': int(uid)})
    if state and state.get('schema') == SCHEMA:
        return state
    if state and state.get('history'):
        old_history = list(state['history'])
        for seq, cid in enumerate(old_history, 1):
            await _entry(uid, seq, cid, state.get('category'), state.get('updated_at'))
        position = max(0, min(int(state.get('position', 0)), len(old_history) - 1))
        state.update(schema=SCHEMA, cursor=position + 1)
        return state
    # A TTL-expired session is reconstructible: the history itself has no TTL.
    last = await db.col('feed_history').find_one({'user_id': int(uid)}, sort=[('seq', -1)])
    return {'_id': int(uid), 'schema': SCHEMA, 'session_id': uuid.uuid4().hex[:16],
            'cursor': last['seq'] if last else 0,
            'category': last.get('category', 'all') if last else 'all', 'seen': []}


async def _current(state):
    return await db.col('feed_history').find_one(
        {'user_id': state['_id'], 'seq': int(state.get('cursor', 0))})


async def _save(state):
    uid, cursor = state['_id'], int(state.get('cursor', 0))
    limit = max(1, int(HISTORY_LIMIT))
    before = await db.col('feed_history').find({'user_id': uid, 'seq': {'$lte': cursor}}).sort(
        'seq', -1).limit(limit).to_list(length=limit)
    before.reverse()
    room = limit - len(before)
    after = []
    if room:
        after = await db.col('feed_history').find({'user_id': uid, 'seq': {'$gt': cursor}}).sort(
            'seq', 1).limit(room).to_list(length=room)
    window = before + after
    state['history'] = [row['collection_id'] for row in window]
    state['position'] = max(0, len(before) - 1)
    state['has_previous'] = bool(await db.col('feed_history').find_one(
        {'user_id': uid, 'seq': {'$lt': cursor}}, {'_id': 1}))
    state['has_forward'] = bool(await db.col('feed_history').find_one(
        {'user_id': uid, 'seq': {'$gt': cursor}}, {'_id': 1}))
    state['seen'] = state.get('seen', [])[-max(1, int(SEEN_LIMIT)):]
    state['updated_at'] = db.now_ts()
    state['expires_on'] = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    await db.col('feed_sessions').replace_one({'_id': uid}, state, upsert=True)


async def _append(state, doc, category):
    current = await _current(state)
    if current and current['collection_id'] == doc['collection_id']:
        state['category'] = category
        return  # Unlock/re-stream of the current post must not reset its history.
    last = await db.col('feed_history').find_one({'user_id': state['_id']}, sort=[('seq', -1)])
    seq = last['seq'] if last else 0
    if current and last and current['seq'] != last['seq']:
        # If a new shelf/deep link is opened while viewing an older position,
        # retain both the old forward trail AND the actually last displayed post.
        seq += 1
        await _entry(state['_id'], seq, current['collection_id'], current.get('category'))
    seq += 1
    await _entry(state['_id'], seq, doc['collection_id'], category)
    state['cursor'] = seq
    state['category'] = category
    state['seen'] = (state.get('seen', []) + [doc['collection_id']])[-SEEN_LIMIT:]


async def _pick(state, category, seed=None):
    doc = await _collection(seed, category) if seed else None
    if doc:
        return doc
    current = await _current(state)
    excluded = list(state.get('seen', []))
    if current:
        excluded.append(current['collection_id'])
    doc = await _random(category, excluded)
    if doc is None:
        doc = await _random(category, [current['collection_id']] if current else [])
        if doc:
            state['seen'] = []
    return doc or await _random(category, [])


async def _result(state, doc):
    await _save(state)
    return state, doc, await db.col('collections').count_documents(_eligible(state['category']))


async def start(user_id, category='all', seed=None):
    category = category or 'all'
    async with feed_lock(user_id):
        state = await _prepare(user_id)
        doc = await _pick(state, category, seed)
        if doc is None:
            return None
        state['browse_category'] = category
        await _append(state, doc, category)
        # Reject stale cards, but rotate only the callback identity, not history.
        state['session_id'] = uuid.uuid4().hex[:16]
        return await _result(state, doc)


async def _adjacent(state, direction):
    cursor = int(state.get('cursor', 0))
    scope = state.get('browse_category', state.get('category') or 'all')
    sign, order = ('$lt', -1) if direction == 'prev' else ('$gt', 1)
    while True:
        rows = await db.col('feed_history').find({'user_id': state['_id'],
            'seq': {sign: cursor}}).sort('seq', order).limit(50).to_list(length=50)
        if not rows:
            return None
        # Fetch validity once for a bounded page, rather than one request per
        # obsolete record. Deleted/disabled/empty drops are not re-delivered.
        ids = list({r['collection_id'] for r in rows})
        docs = await db.col('collections').find({**_eligible(scope),
            'collection_id': {'$in': ids}}, {'files': 0}).to_list(length=50)
        valid = {d['collection_id'] for d in docs}
        for row in rows:
            if row['collection_id'] in valid:
                doc = await _collection(row['collection_id'], scope)
                if doc:
                    state['cursor'] = row['seq']
                    state['category'] = scope
                    return doc
        cursor = rows[-1]['seq']


async def navigate(user_id, session_id, direction):
    if direction not in ('prev', 'next'):
        return None
    async with feed_lock(user_id):
        saved = await db.col('feed_sessions').find_one({'_id': int(user_id), 'session_id': session_id})
        if not saved:
            return None
        state = await _prepare(user_id, saved)
        state['category'] = state.get('browse_category', state.get('category') or 'all')
        doc = await _adjacent(state, direction)
        if doc:
            return await _result(state, doc)
        current = await _current(state)
        if direction == 'prev':
            doc = await _collection(current['collection_id']) if current else None
            return await _result(state, doc) if doc else None
        doc = await _pick(state, state.get('category') or 'all')
        if doc is None:
            return None
        await _append(state, doc, state.get('category') or 'all')
        return await _result(state, doc)


async def next_after_delivery(user_id, collection):
    """Record an older delivered post WITHOUT deleting the browsing trail."""
    async with feed_lock(user_id):
        state = await _prepare(user_id)
        category = state.get('category') or collection.get('category') or 'all'
        if category != 'all' and category != collection.get('category'):
            category = collection.get('category') or 'all'
        state['browse_category'] = category
        await _append(state, collection, category)
        await _save(state)
        return state['session_id']


async def remember_view(user_id, collection):
    """Include posts opened from Unlocked/Saved Posts in the viewing trail."""
    return await next_after_delivery(user_id, collection)
