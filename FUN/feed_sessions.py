"""Mongo-backed randomized browsing with bounded, per-user history.
No video bytes or full collection documents are cached in user sessions.
"""
from datetime import datetime, timedelta, timezone
import uuid
import db
from locks import feed_lock

HISTORY_LIMIT = 200
SEEN_LIMIT = 2000
SESSION_DAYS = 30


def _eligible(category):
    query = {'status': 'active', 'file_count': {'$gt': 0}}
    if category not in ('all', '', None):
        query['category'] = category
    return query


async def _collection(cid, category):
    return await db.col('collections').find_one({**_eligible(category), 'collection_id': cid})


async def _random(category, excluded):
    query = _eligible(category)
    if excluded:
        query['collection_id'] = {'$nin': list(excluded)}
    # Filter/project before sampling so delivery-file arrays never accumulate
    # in a Python list. MongoDB selects one random collection ID.
    result = await db.col('collections').aggregate([
        {'$match': query}, {'$project': {'collection_id': 1}}, {'$sample': {'size': 1}}
    ]).to_list(length=1)
    return await _collection(result[0]['collection_id'], category) if result else None


async def _save(doc):
    doc['updated_at'] = db.now_ts()
    doc['expires_on'] = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    await db.col('feed_sessions').replace_one({'_id': doc['_id']}, doc, upsert=True)


async def start(user_id, category='all', seed=None):
    category = category or 'all'
    async with feed_lock(user_id):
        previous = await db.col('feed_sessions').find_one({'_id': int(user_id)})
        current = None
        if previous and previous.get('history'):
            current = previous['history'][previous.get('position', 0)]
        doc = await _collection(seed, category) if seed else None
        if doc is None:
            doc = await _random(category, [current] if current else [])
        if doc is None:
            doc = await _random(category, [])
        if doc is None:
            return None
        state = {'_id': int(user_id), 'session_id': uuid.uuid4().hex[:16],
                 'category': category, 'history': [doc['collection_id']],
                 'position': 0, 'seen': [doc['collection_id']]}
        await _save(state)
        return state, doc, await db.col('collections').count_documents(_eligible(category))


async def navigate(user_id, session_id, direction):
    if direction not in ('prev', 'next'):
        return None
    async with feed_lock(user_id):
        state = await db.col('feed_sessions').find_one({'_id': int(user_id), 'session_id': session_id})
        if not state:
            return None  # old/another user's buttons must not change this session
        category = state['category']
        history = state['history']
        position = int(state['position'])
        candidate = position - 1 if direction == 'prev' else position + 1
        step = -1 if direction == 'prev' else 1
        while 0 <= candidate < len(history):
            doc = await _collection(history[candidate], category)
            if doc:
                state['position'] = candidate
                await _save(state)
                return state, doc, await db.col('collections').count_documents(_eligible(category))
            candidate += step  # gracefully skip removed/disabled history entries
        if direction == 'prev':
            doc = await _collection(history[position], category)
            if doc:
                return state, doc, await db.col('collections').count_documents(_eligible(category))
            return None
        seen = state.get('seen', [])
        doc = await _random(category, seen)
        if doc is None:
            # A new shuffle cycle: still avoid repeating the current collection.
            doc = await _random(category, [history[position]])
            if doc is not None:
                seen = []
        if doc is None:
            doc = await _collection(history[position], category)  # one-drop category
            if doc is None:
                return None
        history = history[:position + 1] + [doc['collection_id']]
        state['history'] = history[-HISTORY_LIMIT:]
        state['position'] = len(state['history']) - 1
        state['seen'] = (seen + [doc['collection_id']])[-SEEN_LIMIT:]
        await _save(state)
        return state, doc, await db.col('collections').count_documents(_eligible(category))


async def next_after_delivery(user_id, collection):
    """Continue the real browsing session, not the first DB row after delivery."""
    state = await db.col('feed_sessions').find_one({'_id': int(user_id)})
    if state and state.get('history') and state['history'][state['position']] == collection['collection_id']:
        return state['session_id']
    result = await start(user_id, collection.get('category') or 'all', seed=collection['collection_id'])
    return result[0]['session_id'] if result else None
