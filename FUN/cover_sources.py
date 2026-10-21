"""Telegram source references for native video covers; no media downloads.
Legacy video rows already contain collection.channel_id + cover.message_id.
Resolve lazily and optionally persist derived fields with backfill().
"""
import re
import db

VIDEO_TYPES = ('video', 'animation', 'document_video')


def media_kind(post):
    kind = post.get('media_type')
    if kind == 'document' and str(post.get('mime_type') or '').lower().startswith('video/'):
        return 'document_video'
    return kind


def source_fields(unit, channel_id):
    video = next((p for p in unit if media_kind(p) in VIDEO_TYPES), None)
    first = video or unit[0]
    return {'source_chat_id': int(channel_id),
            'source_message_id': int(first['message_id']),
            'kind': media_kind(first)}


def resolve(collection):
    cover = collection.get('cover') or {}
    kind = cover.get('kind') or media_kind(cover)
    chat = cover.get('source_chat_id') or collection.get('channel_id')
    mid = cover.get('source_message_id') or cover.get('message_id')
    if not chat or not mid:
        match = re.fullmatch(r'c(-?\d+)_(\d+)', str(collection.get('collection_id', '')))
        if match:
            chat = chat or int(match[1])
            mid = mid or int(match[2])
    try:
        return kind, int(chat), int(mid)
    except (TypeError, ValueError):
        return kind, None, None


def is_native(collection):
    return resolve(collection)[0] in VIDEO_TYPES


async def persist(collection):
    kind, chat, mid = resolve(collection)
    if not chat or not mid or not kind:
        return False
    cover = collection.get('cover') or {}
    fields = {'kind': kind, 'source_chat_id': chat, 'source_message_id': mid}
    changed = {f'cover.{k}': v for k, v in fields.items() if cover.get(k) != v}
    if changed and collection.get('collection_id'):
        await db.col('collections').update_one(
            {'collection_id': collection['collection_id']}, {'$set': changed})
    cover.update(fields)
    collection['cover'] = cover
    return bool(changed)


async def backfill():
    """Streaming metadata-only migration; never contacts Telegram or touches files/economy."""
    counts = {'checked': 0, 'updated': 0, 'unresolved': 0}
    async for doc in db.col('collections').find({}, {'collection_id': 1, 'channel_id': 1, 'cover': 1}).batch_size(100):
        counts['checked'] += 1
        kind, chat, mid = resolve(doc)
        if not chat or not mid or not kind:
            counts['unresolved'] += 1
        elif await persist(doc):
            counts['updated'] += 1
    return counts


async def clear_companion(bot, chat_id, card_id):
    """Remove a long-caption cover associated with a replaced text control card."""
    if not card_id:
        return
    row = await db.col('cover_companions').find_one({'_id': f'{chat_id}:{card_id}'})
    if not row:
        return
    from telegram.error import TelegramError
    try:
        await bot.delete_message(chat_id=chat_id, message_id=row['cover_message_id'])
    except TelegramError:
        return  # keep metadata so a later navigation can retry cleanup
    await db.col('cover_companions').delete_one({'_id': row['_id']})
