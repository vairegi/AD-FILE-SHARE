"""Admin-only, preview-confirmed broadcasts using Telegram server-side copies.
Formatting/entities/captions are never parsed/reconstructed. Source inline
keyboards are passed explicitly. Existing albums retain grouping. Unsupported
Telegram message types fail during preview, before any recipient receives them.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import uuid
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
import config
import db
import runtime
from ui import button

INPUT_TTL = 600
ALBUM_DELAY = 1.2
SEND_GAP = 0.1
MAX_ATTEMPTS = 5
_pending_albums = {}
_jobs = {}
_worker_lock = asyncio.Lock()


def is_admin(uid):
    return uid == config.OWNER_ID or uid in config.ADMIN_IDS


def private_admin(update):
    user = getattr(update, 'effective_user', None)
    message = getattr(update, 'message', None)
    chat = getattr(message, 'chat', None)
    return bool(user and message and getattr(chat, 'type', None) == 'private' and is_admin(user.id))


async def _retry(call):
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await call()
        except RetryAfter as exc:
            if attempt + 1 == MAX_ATTEMPTS:
                raise
            wait = exc.retry_after
            seconds = wait.total_seconds() if isinstance(wait, timedelta) else float(wait)
            await asyncio.sleep(max(0, seconds) + 1)
    raise RuntimeError('Unreachable broadcast retry state')


async def _copy(bot, target, source_chat, messages):
    if len(messages) > 1:
        ids = sorted({int(m['message_id']) for m in messages})
        result = await _retry(lambda: bot.copy_messages(chat_id=target,
            from_chat_id=source_chat, message_ids=ids))
        if len(result) != len(ids):
            return False  # partial/uncertain; never blindly resend an album
        by_id = {int(m['message_id']): m for m in messages}
        for original_id, copied in zip(ids, result):
            markup = by_id[original_id].get('reply_markup')
            if markup:
                try:
                    keyboard = InlineKeyboardMarkup.de_json(markup, bot)
                    await _retry(lambda: bot.edit_message_reply_markup(
                        chat_id=target, message_id=copied.message_id, reply_markup=keyboard))
                except Exception:
                    return False  # media sent, but buttons failed; never resend media
                await asyncio.sleep(SEND_GAP)
        return True
    source = messages[0]
    kwargs = {'chat_id': target, 'from_chat_id': source_chat,
              'message_id': int(source['message_id'])}
    if source.get('reply_markup'):
        kwargs['reply_markup'] = InlineKeyboardMarkup.de_json(source['reply_markup'], bot)
    await _retry(lambda: bot.copy_message(**kwargs))
    return True


def _source(message):
    markup = getattr(message, 'reply_markup', None)
    return {'message_id': int(message.message_id),
            'reply_markup': markup.to_dict() if markup else None}


async def cancel_input(uid):
    removed = await db.col('broadcast_inputs').delete_one({'_id': int(uid)})
    album = _pending_albums.pop(int(uid), None)
    if album and not album['task'].done():
        album['task'].cancel()
    return bool(removed.deleted_count or album)


async def command(update, context):
    if not private_admin(update):
        return
    uid = update.effective_user.id
    args = list(context.args or [])
    if args and args[0].lower() == 'status':
        return await status(update.message, uid)
    if args and args[0].lower() == 'cancel':
        await cancel_input(uid)
        job = await db.col('broadcast_jobs').find_one({'owner_id': uid,
            'status': {'$in': ['queued', 'running']}}, sort=[('created_at', -1)])
        if job:
            # A task cancelled before its coroutine starts cannot run finally.
            # Persist queued cancellation here, not only inside the worker.
            await db.col('broadcast_jobs').update_one({'_id': job['_id']},
                {'$set': {'cancel_requested': True}})
            await db.col('broadcast_jobs').update_one({'_id': job['_id'], 'status': 'queued'},
                {'$set': {'status': 'cancelled', 'finished_at': db.now_ts()}})
            task = _jobs.get(job['_id'])
            if task and not task.done():
                task.cancel()
        await update.message.reply_text('Broadcast input/job cancellation requested. Already delivered copies cannot be recalled.')
        return
    if args:
        await update.message.reply_text('Use /broadcast in reply to the message, or send /broadcast and then your message. /broadcast status · /broadcast cancel')
        return
    import chat_setup
    chat_setup._purge()
    if uid in chat_setup._pending:
        await update.message.reply_text('Finish or /cancel userbot setup before preparing a broadcast. Credentials must never become broadcast content.')
        return
    await cancel_input(uid)
    source = getattr(update.message, 'reply_to_message', None)
    if source:
        if getattr(source, 'media_group_id', None):
            await update.message.reply_text('To broadcast the WHOLE album: send /broadcast, then forward/send all album items together. Replying to one item cannot retrieve the rest.')
            return
        await _preview(context.bot, uid, source.chat.id, [_source(source)])
        return
    await db.col('broadcast_inputs').replace_one({'_id': uid}, {'_id': uid,
        'expires_at': db.now_ts() + INPUT_TTL,
        'expires_on': datetime.now(timezone.utc) + timedelta(seconds=INPUT_TTL)}, upsert=True)
    await update.message.reply_text('Send or forward the message/album to broadcast. Text, formatting, captions, tags and existing inline buttons are copied as-is where Telegram permits. You will see a preview and must confirm. /cancel stops message input.')


async def consume(update, context):
    if not private_admin(update):
        return False
    uid = update.effective_user.id
    pending = await db.col('broadcast_inputs').find_one({'_id': uid})
    if not pending:
        return False
    if pending['expires_at'] < db.now_ts():
        await cancel_input(uid)
        await update.message.reply_text('Broadcast input expired. Send /broadcast again.')
        return True
    message = update.message
    gid = getattr(message, 'media_group_id', None)
    if gid:
        album = _pending_albums.get(uid)
        if album:
            if album['group'] == gid:
                album['messages'][message.message_id] = _source(message)
                album['last_item_at'] = asyncio.get_running_loop().time()
            else:
                await update.message.reply_text('Finish the current album preview first, or /cancel and start again.')
            return True
        album = {'group': gid, 'chat': message.chat.id,
                 'last_item_at': asyncio.get_running_loop().time(),
                 'messages': {message.message_id: _source(message)}}
        _pending_albums[uid] = album
        album['task'] = runtime.spawn(_finish_album(context.bot, uid, album))
        return True
    await cancel_input(uid)
    await _preview(context.bot, uid, message.chat.id, [_source(message)])
    return True


async def _finish_album(bot, uid, album):
    try:
        # Wait for a quiet interval AFTER the final item, not the first item.
        while True:
            remaining = ALBUM_DELAY - (asyncio.get_running_loop().time() - album['last_item_at'])
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        await db.col('broadcast_inputs').delete_one({'_id': uid})
        await _preview(bot, uid, album['chat'], list(album['messages'].values()))
    finally:
        if _pending_albums.get(uid) is album:
            _pending_albums.pop(uid, None)


async def _preview(bot, uid, source_chat, messages):
    # Preview MUST succeed before creating a confirmable draft.
    try:
        complete = await _copy(bot, uid, source_chat, messages)
        if not complete:
            raise ValueError('Incomplete album copy')
    except Exception as exc:
        await bot.send_message(uid, '⚠️ Telegram could not copy this message/album (' + type(exc).__name__ + '). Nothing was broadcast. Service messages, protected content and some special message types cannot be copied.')
        return None
    total = await db.col('users').count_documents({})
    job_id = uuid.uuid4().hex[:16]
    await db.col('broadcast_jobs').insert_one({'_id': job_id, 'owner_id': uid,
        'source_chat': source_chat, 'messages': messages, 'status': 'draft',
        'created_at': db.now_ts(), 'draft_expires_at': db.now_ts() + INPUT_TTL,
        'total': total, 'sent': 0, 'failed': 0, 'blocked': 0, 'uncertain': 0})
    markup = InlineKeyboardMarkup([[
        button('✅ Send to all users', callback_data=f'bc:send:{job_id}', style='success'),
        button('❌ Cancel', callback_data=f'bc:cancel:{job_id}', style='danger')]])
    await bot.send_message(uid, f'Preview above. Send a copy to up to {total} Bot 3 users? Nothing is sent until you confirm.', reply_markup=markup)
    return job_id


async def callback(update, context):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        return
    try:
        _, action, job_id = (q.data or '').split(':')
    except ValueError:
        return
    if action not in ('send', 'cancel'):
        return
    job = await db.col('broadcast_jobs').find_one({'_id': job_id, 'owner_id': q.from_user.id})
    if not job:
        await context.bot.send_message(q.from_user.id, 'That broadcast belongs to another admin or no longer exists.')
        return
    if action == 'cancel':
        changed = await db.col('broadcast_jobs').update_one({'_id': job_id, 'status': 'draft'},
            {'$set': {'status': 'cancelled'}})
        await context.bot.send_message(q.from_user.id, 'Broadcast cancelled.' if changed.modified_count else 'Already confirmed/finished. Use /broadcast cancel for an active job.')
        return
    if job.get('draft_expires_at', 0) < db.now_ts():
        await context.bot.send_message(q.from_user.id, 'Broadcast preview expired. Prepare a new /broadcast.')
        return
    # Atomic confirmation makes double taps incapable of launching two jobs.
    result = await db.col('broadcast_jobs').update_one({'_id': job_id, 'status': 'draft'},
        {'$set': {'status': 'queued'}})
    if not result.modified_count:
        await context.bot.send_message(q.from_user.id, 'This broadcast was already confirmed or cancelled. /broadcast status')
        return
    task = runtime.spawn(_run(context.bot, job_id))
    _jobs[job_id] = task
    await context.bot.send_message(q.from_user.id, 'Broadcast queued. You can keep using the bot. /broadcast status · /broadcast cancel')


async def _run(bot, job_id):
    current_uid = None
    try:
        async with _worker_lock:
            job = await db.col('broadcast_jobs').find_one({'_id': job_id})
            if not job or job.get('status') != 'queued':
                return
            if job.get('cancel_requested'):
                await db.col('broadcast_jobs').update_one({'_id': job_id}, {'$set': {'status': 'cancelled'}})
                return
            # Snapshot only the highest recipient ID, then page; never load
            # every user (or hold a Mongo cursor across network/flood waits).
            last_user = await db.col('users').find_one({}, {'user_id': 1}, sort=[('user_id', -1)])
            cutoff = int(last_user['user_id']) if last_user else 0
            total = await db.col('users').count_documents({'user_id': {'$lte': cutoff}})
            claim = await db.col('broadcast_jobs').update_one(
                {'_id': job_id, 'status': 'queued', 'cancel_requested': {'$ne': True}},
                {'$set': {'status': 'running', 'total': total}})
            if not claim.modified_count:
                return
            after = -1
            while True:
                rows = await db.col('users').find({'user_id': {'$gt': after, '$lte': cutoff}},
                    {'user_id': 1}).sort('user_id', 1).limit(100).to_list(length=100)
                if not rows:
                    break
                for row in rows:
                    state = await db.col('broadcast_jobs').find_one({'_id': job_id})
                    if state.get('cancel_requested'):
                        raise asyncio.CancelledError()
                    current_uid = int(row['user_id'])
                    field = 'sent'
                    try:
                        complete = await _copy(bot, current_uid, job['source_chat'], job['messages'])
                        if not complete:
                            field = 'uncertain'
                    except Forbidden:
                        field = 'blocked'
                    except BadRequest:
                        field = 'failed'
                    except NetworkError:
                        # May have reached Telegram. Blind retries duplicate messages.
                        field = 'uncertain'
                    except Exception:
                        field = 'failed'
                    after = current_uid
                    current_uid = None
                    await db.col('broadcast_jobs').update_one({'_id': job_id}, {'$inc': {field: 1}})
                    await asyncio.sleep(SEND_GAP)
            await db.col('broadcast_jobs').update_one({'_id': job_id},
                {'$set': {'status': 'finished', 'finished_at': db.now_ts()}})
    except asyncio.CancelledError:
        increments = {'uncertain': 1} if current_uid is not None else {}
        changes = {'$set': {'status': 'cancelled', 'finished_at': db.now_ts()}}
        if increments:
            changes['$inc'] = increments
        await db.col('broadcast_jobs').update_one({'_id': job_id}, changes)
    except Exception as exc:
        await db.col('broadcast_jobs').update_one({'_id': job_id},
            {'$set': {'status': 'interrupted', 'last_error_type': type(exc).__name__}})
    finally:
        _jobs.pop(job_id, None)
        job = await db.col('broadcast_jobs').find_one({'_id': job_id})
        if job:
            try:
                await _retry(lambda: bot.send_message(job['owner_id'], _summary(job)))
            except Exception:
                pass


def _summary(job):
    return (f"📣 Broadcast {job['status']}\n"
            f"Recipients: {job.get('total', 0)}\nSent: {job.get('sent', 0)}\n"
            f"Blocked/unreachable: {job.get('blocked', 0)}\nFailed: {job.get('failed', 0)}\n"
            f"Uncertain/partial: {job.get('uncertain', 0)}\n"
            'Uncertain copies are not retried automatically to avoid duplicates.')


async def status(message, uid):
    job = await db.col('broadcast_jobs').find_one({'owner_id': uid}, sort=[('created_at', -1)])
    await message.reply_text(_summary(job) if job else 'No broadcasts prepared yet. Use /broadcast.')


async def recover_after_restart():
    # Never replay an interrupted send automatically: last delivery may have
    # succeeded just before the process stopped. Stored summaries remain visible.
    await db.col('broadcast_jobs').update_many({'status': {'$in': ['queued', 'running']}},
        {'$set': {'status': 'interrupted', 'last_error_type': 'ServiceRestart'}})
