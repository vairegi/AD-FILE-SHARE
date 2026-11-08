"""Private-admin gifts and token directory. Independent FUN service only.

Mongo user credit + per-job marker is ONE atomic update. Durable jobs and
receipts let a restart reconcile ledger/checkpoints without paying twice.
Telegram delivery is not exactly-once: ambiguous sends are recorded, not
blindly repeated. Credits do not depend on notification delivery.
"""
import asyncio
import html
import logging
import math
import re
import unicodedata

from pymongo import ReturnDocument
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

import config
import db
import persona
from ui import button, edit_card

log = logging.getLogger('fun.token_admin')
PAGE_SIZE = 10
BATCH_SIZE = 50
SEND_GAP = .08
POLL_SECONDS = 30
MAX_AMOUNT = 1_000_000_000
MAX_BALANCE = 9_000_000_000_000_000  # conservative int64 headroom
CONFIRM_SECONDS = 600
_worker_lock = asyncio.Lock()
_wake = None


def wake():
    if _wake is not None:
        _wake.set()


class Deferred(Exception):
    def __init__(self, seconds):
        self.seconds = max(1, float(seconds))


def admin(uid):
    return bool(uid and (uid == config.OWNER_ID or uid in config.ADMIN_IDS))


def private_admin(update):
    user = getattr(update, 'effective_user', None)
    msg = getattr(update, 'message', None)
    return bool(user and msg and admin(user.id) and
                getattr(getattr(msg, 'chat', None), 'type', None) == 'private')


async def ensure_indexes():
    await db.col('gift_jobs').create_index([('status', 1), ('ready_at', 1)])
    await db.col('gift_receipts').create_index([('job_id', 1), ('user_id', 1)], unique=True)
    await db.col('users').create_index([('welcomed', 1), ('tokens', -1), ('user_id', 1)])


def audience(job):
    if job['target'] != 'all':
        return {'user_id': int(job['target']), 'welcomed': True}
    cutoff = job['created_at']
    # started_at is stamped by the new /start flow; legacy started users use
    # their original creation time. Do not include newcomers during a long run.
    return {'welcomed': True, 'user_id': {'$gt': 0, '$lte': job['max_uid']},
            '$or': [{'started_at': {'$lte': cutoff}},
                    {'started_at': {'$exists': False}, 'created_at': {'$lte': cutoff}}]}


async def command(update, context):
    if not private_admin(update):
        return
    args = list(context.args or [])
    if args == ['status']:
        job = await db.col('gift_jobs').find_one({'owner_id': update.effective_user.id},
                                               sort=[('created_at', -1)])
        return await update.message.reply_text(await summary(job) if job else '🎁 No gifts yet.')
    if (len(args) != 2 or not re.fullmatch(r'[1-9][0-9]{0,9}', args[1]) or
            not 1 <= int(args[1]) <= MAX_AMOUNT or
            not (args[0].lower() == 'all' or re.fullmatch(r'[1-9][0-9]{0,15}', args[0]))):
        await update.message.reply_text(
            '🎁 Gift tokens\n/gift <user_id> <amount>\n/gift all <amount>\n/gift status\n'
            f'Amount must be a whole number from 1 to {MAX_AMOUNT:,}.')
        return
    target = 'all' if args[0].lower() == 'all' else int(args[0])
    owner = update.effective_user.id
    # A redelivered Telegram command must never create another credit job.
    job_id = f'g{update.message.chat.id}_{update.message.message_id}'
    existing = await db.col('gift_jobs').find_one({'_id': job_id})
    if existing:
        await update.message.reply_text(await summary(existing))
        return
    if target != 'all':
        user = await db.get_user(target)
        if not user or not user.get('welcomed'):
            await update.message.reply_text('🎀 That user must /start this bot first. No tokens were added.')
            return
    top = await db.col('users').find_one({'welcomed': True, 'user_id': {'$gt': 0}},
                                       {'user_id': 1}, sort=[('user_id', -1)])
    job = {'_id': job_id, 'owner_id': owner, 'target': target, 'amount': int(args[1]),
           'created_at': db.now_ts(), 'max_uid': int((top or {}).get('user_id', 0)),
           'after_uid': 0, 'status': 'preview' if target == 'all' else 'queued', 'ready_at': 0}
    job['total'] = await db.col('users').count_documents(audience(job))
    if not job['total']:
        await update.message.reply_text('🎀 No users who have started the bot were found. Nothing changed.')
        return
    result = await db.col('gift_jobs').update_one({'_id': job_id}, {'$setOnInsert': job}, upsert=True)
    if result.upserted_id is None:
        await update.message.reply_text('This gift is already recorded. /gift status')
        return
    if target == 'all':
        await update.message.reply_text(
            f'🎁 <b>A little surprise for everyone, Senpai~!</b>\n\n'
            f'👥 Started users: <b>{job["total"]:,}</b>\n'
            f'🪙 Gift each: <b>+{job["amount"]:,} tokens</b>\n'
            f'🎀 Total planned: <b>{job["total"] * job["amount"]:,} tokens</b>\n\n'
            'Confirm to credit wallets and send a cute gift alert.\n'
            'Users who blocked the bot still receive wallet credit; their alert cannot be delivered.',
            parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[
                button('🎁 Send gifts', f'gift:confirm:{job_id}', style='success'),
                button('✖ Cancel', f'gift:cancel:{job_id}', style='danger')]]))
    else:
        wake()  # durable job exists before the worker is signalled
        await update.message.reply_text(f'🎁 Gift queued: +{job["amount"]:,} tokens → {target}. /gift status')
    # Background worker picks up queued jobs; private bot updates remain fast.


async def callback(update, context):
    q = update.callback_query
    if (not admin(q.from_user.id) or
            getattr(getattr(q.message, 'chat', None), 'type', None) != 'private'):
        return
    parts = (q.data or '').split(':')
    if len(parts) != 3 or parts[1] not in ('confirm', 'cancel'):
        return
    _, action, jid = parts
    job = await db.col('gift_jobs').find_one({'_id': jid, 'owner_id': q.from_user.id})
    if not job:
        return
    expired = db.now_ts() > job['created_at'] + CONFIRM_SECONDS
    state = 'cancelled' if action == 'cancel' or expired else 'queued'
    claimed = await db.col('gift_jobs').update_one({'_id': jid, 'owner_id': q.from_user.id,
        'status': 'preview'}, {'$set': {'status': state, 'ready_at': 0}})
    if not claimed.modified_count:
        await context.bot.send_message(q.from_user.id, 'This gift was already confirmed or cancelled. /gift status')
        return
    text = ('⌛ Gift preview expired. Send /gift again.' if expired else
            '🎁 Gifts queued, Senpai~! Wallets and alerts are processed in the background. /gift status'
            if state == 'queued' else '✖ Gift cancelled. No tokens were added.')
    if state == 'queued':
        wake()
    await edit_card(q, text, None)


async def credit(job, uid):
    """Crash-safe credit, ledger, receipt reconciliation; never upserts a user."""
    jid = job['_id']
    rid = f'{jid}:{uid}'
    marker = f'gift_credits.{jid}'
    receipt = await db.col('gift_receipts').find_one({'_id': rid})
    if receipt and receipt.get('credited'):
        return receipt
    doc = await db.col('users').find_one_and_update(
        {'user_id': uid, 'welcomed': True, marker: {'$exists': False},
         '$or': [{'tokens': {'$lte': MAX_BALANCE - job['amount']}},
                 {'tokens': {'$exists': False}}]},
        {'$inc': {'tokens': job['amount']}, '$set': {marker: job['amount']}},
        return_document=ReturnDocument.AFTER)
    if not doc:
        doc = await db.get_user(uid)
        if not doc or jid not in (doc.get('gift_credits') or {}):
            await db.col('gift_receipts').update_one({'_id': rid}, {'$setOnInsert': {
                'job_id': jid, 'user_id': uid, 'credited': False,
                'notify': 'skipped', 'reason': 'ineligible_or_balance_limit'}}, upsert=True)
            return await db.col('gift_receipts').find_one({'_id': rid})
    # Deterministic _id closes the crash gap between wallet write and ledger.
    await db.col('transactions').update_one({'_id': rid}, {'$setOnInsert': {
        'user_id': uid, 'amount': job['amount'], 'reason': 'admin_gift',
        'meta': {'admin_id': job['owner_id'], 'gift_job': jid}, 'at': job['created_at']}}, upsert=True)
    await db.col('gift_receipts').update_one({'_id': rid}, {'$setOnInsert': {
        'job_id': jid, 'user_id': uid, 'credited': True,
        'balance_after_credit': int(doc.get('tokens', 0)), 'notify': 'pending', 'attempts': 0}}, upsert=True)
    return await db.col('gift_receipts').find_one({'_id': rid})


async def notify(bot, job, receipt):
    if not receipt.get('credited') or receipt.get('notify') != 'pending':
        return
    rid = receipt['_id']
    claimed = await db.col('gift_receipts').find_one_and_update(
        {'_id': rid, 'notify': 'pending'}, {'$set': {'notify': 'sending'}, '$inc': {'attempts': 1}},
        return_document=ReturnDocument.AFTER)
    if not claimed:
        return
    state = 'sent'
    try:
        user = await db.get_user(receipt['user_id']) or {}
        await bot.send_message(receipt['user_id'],
            persona.gift_notify(job['amount'], int(user.get('tokens', 0))), parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [button('🎬 Start Watching', 'feed:all:0', style='success')],
                [button('👛 My Balance', 'profile'), button('🎁 Earn Tokens', 'earn')]]))
    except RetryAfter as exc:
        if claimed['attempts'] < 5:
            await db.col('gift_receipts').update_one({'_id': rid}, {'$set': {'notify': 'pending'}})
            delay = exc.retry_after
            raise Deferred((delay.total_seconds() if hasattr(delay, 'total_seconds') else float(delay)) + 1)
        state = 'failed'
    except Forbidden:
        state = 'blocked'
    except BadRequest:
        state = 'failed'
    except NetworkError:
        state = 'uncertain'  # Telegram may already have delivered it.
    except Exception:
        state = 'uncertain'
    await db.col('gift_receipts').update_one({'_id': rid}, {'$set': {'notify': state}})


async def process_job(bot, job):
    """Bounded recipient pages, stable cursor; a crash replays safely."""
    jid = job['_id']
    await db.col('gift_jobs').update_one({'_id': jid}, {'$set': {'status': 'running'}})
    after = job.get('after_uid', 0)
    while True:
        query = audience(job)
        query['user_id'] = ({'$gt': after, '$lte': job['max_uid']} if job['target'] == 'all'
                            else int(job['target']))
        if job['target'] != 'all' and after >= int(job['target']):
            break
        rows = await db.col('users').find(query, {'user_id': 1}).sort('user_id', 1).limit(BATCH_SIZE).to_list(BATCH_SIZE)
        if not rows:
            break
        for row in rows:
            uid = int(row['user_id'])
            receipt = await credit(job, uid)
            # A previous process may have died after sending but before saving
            # the response. It is safe to re-credit, NOT safe to re-send.
            if receipt.get('notify') == 'sending':
                await db.col('gift_receipts').update_one({'_id': receipt['_id']},
                    {'$set': {'notify': 'uncertain'}})
            else:
                await notify(bot, job, receipt)
            after = uid
            await db.col('gift_jobs').update_one({'_id': jid}, {'$set': {'after_uid': after}})
            await asyncio.sleep(SEND_GAP)
    await db.col('gift_jobs').update_one({'_id': jid},
        {'$set': {'status': 'finished', 'finished_at': db.now_ts()}})
    done = await db.col('gift_jobs').find_one({'_id': jid})
    try:
        await bot.send_message(job['owner_id'], await summary(done))
    except Exception:
        pass  # /gift status is durable even if this summary cannot be delivered.


async def run_once(bot):
    async with _worker_lock:
        job = await db.col('gift_jobs').find_one({'status': {'$in': ['queued', 'running']},
            'ready_at': {'$lte': db.now_ts()}}, sort=[('created_at', 1)])
        if not job:
            return False
        try:
            await process_job(bot, job)
        except Deferred as exc:
            await db.col('gift_jobs').update_one({'_id': job['_id']},
                {'$set': {'status': 'queued', 'ready_at': db.now_ts() + exc.seconds}})
        except Exception as exc:
            log.warning('Gift job paused error=%s', type(exc).__name__)
            await db.col('gift_jobs').update_one({'_id': job['_id']},
                {'$set': {'status': 'queued', 'ready_at': db.now_ts() + POLL_SECONDS,
                          'last_error_type': type(exc).__name__}})
        return True


async def loop(bot):
    global _wake
    event = _wake = asyncio.Event()
    try:
        while True:
            event.clear()  # before DB reads: no lost command/confirmation wakeup
            try:
                worked = await run_once(bot)
            except Exception as exc:
                log.warning('Gift worker unavailable error=%s', type(exc).__name__)
                worked = False
            try:
                await asyncio.wait_for(event.wait(), timeout=1 if worked else POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        if _wake is event:
            _wake = None


async def summary(job):
    groups = await db.col('gift_receipts').aggregate([
        {'$match': {'job_id': job['_id']}},
        {'$group': {'_id': '$notify', 'n': {'$sum': 1},
                    'credited': {'$sum': {'$cond': ['$credited', 1, 0]}}}}]).to_list(None)
    states = {g['_id']: g['n'] for g in groups}
    credited = sum(g['credited'] for g in groups)
    return (f'🎁 Gift {job["status"]}\n'
            f'Gift: +{job["amount"]:,} tokens each\nPlanned users: {job["total"]:,}\n'
            f'Wallets credited: {credited:,}\nAlerts sent: {states.get("sent", 0):,}\n'
            f'Blocked: {states.get("blocked", 0):,} · Failed: {states.get("failed", 0):,}\n'
            f'Uncertain alerts: {states.get("uncertain", 0):,}\n'
            f'Skipped: {states.get("skipped", 0):,}\n'
            'Blocked/failed alerts do not reverse wallet credits.\n'
            'Uncertain alerts are not resent automatically. /gift status')


def short_name(user, width=12):
    name = user.get('first_name') or user.get('username') or str(user['user_id'])
    name = ''.join(c if not unicodedata.category(c).startswith('C') else ' ' for c in str(name))
    name = ' '.join(name.split())
    out, size = '', 0
    for char in name:
        n = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if size + n > width - 1:
            return out + '…' + ' ' * max(0, width - size - 1)
        out += char
        size += n
    return out + ' ' * max(0, width - size)


async def table(page, owner):
    import native_tables as native
    total = await db.col('users').count_documents({'welcomed': True})
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(int(page), pages - 1))
    rows = await db.col('users').find({'welcomed': True},
        {'user_id': 1, 'first_name': 1, 'username': 1, 'tokens': 1}).sort(
        [('tokens', -1), ('user_id', 1)]).skip(page * PAGE_SIZE).limit(PAGE_SIZE).to_list(PAGE_SIZE)
    ids = [r['user_id'] for r in rows]
    stats = await db.col('referrals').aggregate([
        {'$match': {'inviter_id': {'$in': ids}}},
        {'$group': {'_id': '$inviter_id', 'friends': {'$sum': {'$cond': [
            {'$eq': ['$status', 'qualified']}, 1, 0]}}, 'total': {'$sum': 1}}}]).to_list(PAGE_SIZE)
    refs = {r['_id']: r for r in stats}
    unlocks = await db.col('unlocks').aggregate([
        {'$match': {'user_id': {'$in': ids}}},
        {'$group': {'_id': '$user_id', 'n': {'$sum': 1}}}]).to_list(PAGE_SIZE)
    counts = {r['_id']: r['n'] for r in unlocks}
    cells = [[native.cell(label, header=True) for label in
              ('#', 'Name / User ID', 'Tokens', 'Friends', 'Unlocked Posts')]]
    for index, user in enumerate(rows, page * PAGE_SIZE + 1):
        ref = refs.get(user['user_id'], {'friends': 0, 'total': 0})
        raw = str(user.get('first_name') or user.get('username') or 'User')
        name = ' '.join(''.join(c if not unicodedata.category(c).startswith('C') else ' '
                               for c in raw).split())[:60]
        linked = [{'type':'url', 'text':name, 'url':f'tg://user?id={user["user_id"]}'},
                  f'\n{user["user_id"]}']
        pending = ref['total'] - ref['friends']
        friends = str(ref['friends']) + (f'\n+{pending} pending' if pending else '')
        cells.append([native.cell(str(index), align='right'), native.cell(linked),
            native.cell(str(int(user.get('tokens',0))), align='right'),
            native.cell(friends, align='right'),
            native.cell(str(counts.get(user['user_id'],0)), align='right')])
    blocks = [native.paragraph({'type':'bold', 'text':'🪙 Senpai’s Token Directory'}),
              native.paragraph(f'👥 {total:,} started users · Page {page + 1}/{pages}'),
              {'type':'table', 'cells':cells, 'is_bordered':True, 'is_striped':True,
               'is_compact':True, 'caption':'🎀 Live wallets, friends and unlocked drops'}]
    if not rows:
        blocks.append(native.paragraph('🎀 No users yet — waiting for the first /start, Senpai~!'))
    blocks.append(native.paragraph('🌸 Highest balances first. Friends = qualified referrals; pending invites are separate. Unlocked Posts = unique purchases in the user’s vault, not repeat re-streams. Tap a name to open their profile~'))
    buttons = []
    if page:
        buttons.append(button('◀ Previous', f'ut:{owner}:{page - 1}'))
    buttons.append(button('🔄 Refresh', f'ut:{owner}:{page}'))
    if page + 1 < pages:
        buttons.append(button('Next ▶', f'ut:{owner}:{page + 1}'))
    return {'blocks':blocks, 'skip_entity_detection':True}, InlineKeyboardMarkup([buttons])


async def users_command(update, context):
    if not private_admin(update):
        return
    args = list(context.args or [])
    if len(args) > 1 or (args and not re.fullmatch(r'[1-9][0-9]{0,6}', args[0])):
        await update.message.reply_text('🎀 Usage: /userstoken [page], Senpai~')
        return
    import native_tables
    rich, kb = await table(int(args[0]) - 1 if args else 0, update.effective_user.id)
    try:
        await native_tables.send(context.bot, update.message.chat.id, rich, kb)
    except BadRequest:
        await update.message.reply_text('🎀 Native rich tables are unavailable on this Bot API endpoint, Senpai. This screen needs Bot API 10.2+; use an up-to-date Telegram app and Bot API server. I will not replace it with a fake text table.')


async def users_alias(update, context):
    if not private_admin(update):
        return
    args = list(context.args or [])
    if not args or args[0].lower() not in ('token', 'tokens'):
        await update.message.reply_text('🎀 Use /users token [page] or /userstoken, Senpai~')
        return
    from types import SimpleNamespace
    return await users_command(update, SimpleNamespace(args=args[1:], bot=context.bot))


async def users_callback(update, context):
    q = update.callback_query
    if (not admin(q.from_user.id) or
            getattr(getattr(q.message, 'chat', None), 'type', None) != 'private'):
        return
    parts = (q.data or '').split(':')
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return
    if int(parts[1]) != q.from_user.id or len(parts[2]) > 7:
        return
    import native_tables
    rich, kb = await table(int(parts[2]), q.from_user.id)
    try:
        await native_tables.edit(context.bot, q.message.chat.id, q.message.message_id, rich, kb)
    except BadRequest:
        await context.bot.send_message(q.from_user.id, '🎀 This native table could not be refreshed, Senpai. Try /userstoken again. No wallet data was changed.')
