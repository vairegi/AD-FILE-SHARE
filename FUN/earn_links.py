"""Funbot short-link tokens. Temporary TIMER/RETURN verification, NOT an ad receipt.

Single FUN process (as the existing app requires). Wallet, rolling history,
rotation and reminder scheduling change in ONE compare-and-swap user write.
A durable attempt is saved before any API request. Exactly one active link per
user, no credit on provider outage. Recovery reconciles ledger after crashes.
"""
import asyncio
import ipaddress
import logging
import re
import secrets
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx
from pymongo import ReturnDocument
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

import config
import db
import persona
from locks import user_lock
from ui import button, bot_username, edit_card
from token_admin import private_admin

log = logging.getLogger('fun.earn_links')
DAY = 86400
LIMIT = 5
LINK_TTL = 7200
DEFAULT_MIN = 150
MAX_REWARD = 1_000_000
MAX_BALANCE = 9_000_000_000_000_000
POLL = 30
BATCH = 50
IST = timezone(timedelta(hours=5, minutes=30))
_notify_lock = asyncio.Lock()
_http = None


async def close_http():
    global _http
    client, _http = _http, None
    if client is not None:
        await client.aclose()


async def ensure_indexes():
    await db.col('earn_attempts').create_index([('state', 1), ('credited_at', 1)])
    await db.col('earn_attempts').create_index([('user_id', 1), ('created_at', -1)])
    await db.col('earn_providers').create_index('site', unique=True)
    await db.col('users').create_index('earn.remind_at')
    # Claimed reminders are durable: a post-send crash is marked uncertain,
    # never automatically retried as if Telegram had not received it.
    await db.col('earn_notices').create_index([('state', 1), ('retry_at', 1)])


def markup(url=None):
    rows = []
    if url:
        rows.append([button('🔗 Open Shortener, Senpai~', url=url, style='success')])
    rows.append([button('🪙 Solve & Earn', 'earn:solve', style='success'),
                 button('👛 My Balance', 'profile')])
    rows.append([button('🏠 Main Menu', 'home')])
    return InlineKeyboardMarkup(rows)


def stamp(at):
    return datetime.fromtimestamp(at, IST).strftime('%d %b, %I:%M %p IST')


def settings_values(settings):
    return (bool(settings.get('earn_enabled', False)),
            max(0, int(settings.get('per_verify_tokens', 0))),
            max(10, min(3600, int(settings.get('earn_min_seconds', DEFAULT_MIN)))))


def history(user, now):
    return [h for h in (user.get('earn', {}).get('history') or [])
            if float(h.get('at', 0)) > now - DAY]


async def active_providers():
    return await db.col('earn_providers').find({'status': 'active'}).sort('site', 1).limit(100).to_list(100)


async def eligible(user, now, providers=None):
    providers = await active_providers() if providers is None else providers
    events = history(user, now)
    result = []
    for p in providers:
        times = [h['at'] for h in events if h['site'] == p['site']]
        if len(times) < LIMIT:
            result.append(p)
    return result


async def availability(user, now):
    providers = await active_providers()
    events = history(user, now)
    candidates = []
    for p in providers:
        times = sorted(h['at'] for h in events if h['site'] == p['site'])
        if len(times) < LIMIT:
            return now
        candidates.append(times[0] + DAY)
    return min(candidates) if candidates else None


async def safe_api_base(value):
    """Admin-configured HTTPS only, no credentials/query, local/private targets.
    Redirects are never followed by the HTTP client. DNS is checked before each
    request (not a blanket guarantee against malicious DNS rebinding).
    """
    try:
        p = urlsplit(value)
        if (p.scheme != 'https' or not p.hostname or p.username or p.password or
                p.query or p.fragment or p.port not in (None, 443)):
            return False
        if p.hostname.lower() == 'localhost' or p.hostname.lower().endswith(('.localhost', '.local')):
            return False
        loop = asyncio.get_running_loop()
        entries = await loop.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
        return bool(entries) and all(ipaddress.ip_address(x[4][0]).is_global for x in entries)
    except (ValueError, OSError):
        return False


def clean_url(value):
    if not isinstance(value, str):
        return None
    value = value.strip().strip('"').strip("'")
    try:
        p = urlsplit(value)
        if (p.scheme in ('http', 'https') and p.hostname and not p.username and not p.password
                and not any(x.isspace() for x in value)):
            # Do not give clients local targets returned by a bad provider.
            try:
                if not ipaddress.ip_address(p.hostname).is_global:
                    return None
            except ValueError:
                if p.hostname.lower() == 'localhost' or p.hostname.lower().endswith(('.localhost', '.local')):
                    return None
            return value
    except ValueError:
        pass
    return None


async def shorten(provider, destination):
    global _http
    if not await safe_api_base(provider['api_base']):
        return None
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(15), follow_redirects=False,
                                  limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
    for attempt in range(2):
        try:
            r = await _http.get(provider['api_base'], params={
                'api': provider['api_key'], 'url': destination, 'format': 'text'})
            if r.status_code in (429, 500, 502, 503, 504):
                # Never retry earlier than a provider's numeric Retry-After;
                # long/unknown waits fail over instead of blocking the bot.
                raw = r.headers.get('retry-after')
                if raw:
                    try:
                        delay = float(raw)
                    except ValueError:
                        return None
                    if delay > 3:
                        return None
                else:
                    delay = .5
                if attempt == 0:
                    await asyncio.sleep(max(0, delay))
                    continue
                return None
            if r.status_code != 200 or len(r.content) > 65536:
                return None
            result = clean_url(r.text)
            if result:
                return result if result != destination else None
            try:
                data = r.json()
                if not isinstance(data, dict):
                    return None
                for key in ('shortenedUrl', 'shortened_url', 'short', 'url', 'link'):
                    candidate = clean_url(data.get(key))
                    if candidate and candidate != destination:
                        return candidate
            except ValueError:
                pass
            return None
        except (httpx.TransportError, asyncio.TimeoutError):
            if attempt == 0:
                await asyncio.sleep(.5)
                continue
        except Exception as exc:
            log.warning('Shortener request failed error=%s', type(exc).__name__)
            return None
    return None


async def config_command(update, context, kind):
    if not private_admin(update):
        return
    args = list(context.args or [])
    s = await db.get_settings()
    enabled, reward, minimum = settings_values(s)
    text = None
    if kind == 'reward':
        if not args:
            text = f'🎀 Senpai, each accepted return earns {reward} tokens.\n/setperverifytoken <1..{MAX_REWARD}> · 0 pauses new rewards.'
        elif len(args) == 1 and re.fullmatch(r'[0-9]{1,7}', args[0]) and int(args[0]) <= MAX_REWARD:
            await db.update_settings({'per_verify_tokens': int(args[0])})
            text = f'🎁 All set, Senpai~! New links earn {int(args[0])} tokens each. Existing links keep their promised reward.'
        else:
            text = '🎀 Usage: /setperverifytoken <0..1000000> — whole tokens only, Senpai~'
    elif kind == 'timer':
        if not args:
            text = f'⏱ Senpai, the minimum wait is {minimum}s.\n/bantimecontrol 150s · 3m · 1h\nToo-fast returns wait and retry — NO bans. A timer is not proof of ad completion.'
        else:
            m = re.fullmatch(r'([0-9]{1,4})(s|sec|m|min|h)?', ''.join(args).lower())
            seconds = int(m[1]) * {'s':1,'sec':1,'m':60,'min':60,'h':3600,None:1}[m[2]] if m else 0
            if not 10 <= seconds <= 3600:
                text = '🎀 Senpai, try /bantimecontrol 150s or 3m (10s–3600s). No automatic bans.'
            else:
                await db.update_settings({'earn_min_seconds': seconds})
                text = f'⏱ New links now wait at least {seconds}s, Senpai~! Existing links keep their original timer. No automatic bans.'
    elif kind == 'switch':
        if len(args) == 1 and args[0].lower() in ('on', 'off'):
            await db.update_settings({'earn_enabled': args[0].lower() == 'on'})
            text = '🎀 Solve & Earn is now ' + ('ON, Senpai~!' if args[0].lower() == 'on' else 'OFF, Senpai. Existing valid links can still finish.')
        elif not args or args == ['status']:
            active = len(await active_providers())
            text = (f'🎀 Senpai’s shortener rewards\nStatus: {"ON" if enabled else "OFF"}\n'
                    f'Reward: {reward} tokens · Wait: {minimum}s\nActive providers: {active}\n'
                    'Limit: 5 rewarded returns per provider / rolling 24h.\n'
                    '/shortener on|off|status · /shortenerapi\n'
                    'Temporary return-link/timer checks only — LinkGuard is not connected.')
        else:
            text = '🎀 Usage: /shortener on|off|status, Senpai~'
    await update.message.reply_text(text)


async def cmd_reward(update, context):
    return await config_command(update, context, 'reward')


async def cmd_timer(update, context):
    return await config_command(update, context, 'timer')


async def cmd_switch(update, context):
    return await config_command(update, context, 'switch')


async def cmd_providers(update, context):
    if not private_admin(update):
        return
    args = list(context.args or [])
    action = args[0].lower() if args else 'list'
    collection = db.col('earn_providers')
    if action == 'add':
        if (len(args) != 4 or not re.fullmatch(r'[a-z0-9_]{1,32}', args[1].lower())
                or not await safe_api_base(args[2]) or not 1 <= len(args[3]) <= 1024):
            await update.message.reply_text('🎀 Senpai, use /shortenerapi add <site> <https://provider/api> <key>. Only public HTTPS API addresses are allowed.')
            return
        site = args[1].lower()
        existing = await collection.find_one({'_id':site})
        if existing:
            await update.message.reply_text('🎀 That provider is already saved, Senpai~ Remove it first to replace the key. Daily usage is kept.')
            return
        if await collection.count_documents({}) >= 100:
            await update.message.reply_text('🎀 The shelf holds 100 providers, Senpai~ Remove an old one first.')
            return
        # Never echo the API key; best-effort remove the command containing it.
        try:
            await update.message.delete()
        except Exception:
            pass
        await collection.update_one({'_id':site}, {'$setOnInsert': {
            'site':site, 'api_base':args[2].rstrip('/'), 'api_key':args[3],
            'status':'active', 'created_at':db.now_ts()}}, upsert=True)
        await update.message.reply_text(f'🌸 Saved {site}, Senpai~! Its key stays hidden. Set /setperverifytoken then /shortener on. Connection is checked when creating a link.')
        return
    if action in ('pause', 'resume', 'remove', 'delete') and len(args) == 2:
        site = args[1].lower()
        if action in ('remove', 'delete'):
            result = await collection.delete_one({'_id':site})
            ok = bool(result.deleted_count)
        else:
            result = await collection.update_one({'_id':site}, {'$set': {'status':'paused' if action == 'pause' else 'active'}})
            ok = bool(result.matched_count)
        await update.message.reply_text(f'🎀 {site}: {action} done, Senpai~ Daily usage is kept.' if ok else '🎀 I could not find that provider, Senpai. Try /shortenerapi.')
        return
    if action not in ('list','status') or len(args) > 1:
        await update.message.reply_text('🎀 Senpai’s shortener controls\n/shortenerapi add <site> <api_base> <key>\n/shortenerapi pause <site>\n/shortenerapi resume <site>\n/shortenerapi remove <site>\n/shortenerapi — list the rotation')
        return
    rows = await collection.find({}).sort('site',1).limit(100).to_list(100)
    lines = ['🎀 Senpai’s Shortener Shelf', 'Each user rotates through eligible ACTIVE providers.']
    for i, row in enumerate(rows, 1):
        lines.append(f'{i}. {row["site"]} · {row["status"]} · {row["api_base"]} · Key: hidden')
    if not rows:
        lines.append('🌸 No providers yet. /shortenerapi add <site> <api_base> <key>')
    # Bounded Telegram messages even with 100 provider entries.
    for start in range(0, len(lines), 15):
        await update.message.reply_text('\n'.join(lines[start:start+15]))


async def _pending(user, now):
    token = (user.get('earn') or {}).get('pending')
    if not token:
        return None
    attempt = await db.col('earn_attempts').find_one({'_id':token})
    if attempt and (attempt['state'] == 'accepted' or (attempt.get('expires_at', 0) > now and attempt['state'] in ('building','pending'))):
        return attempt
    return None


async def issue(bot, uid):
    async with user_lock(uid):
        user = await db.get_user(uid)
        if not user or not user.get('welcomed'):
            return persona.earn_notice('start'), markup()
        now = db.now_ts()
        old = await _pending(user, now)
        if old:
            if old['state'] == 'pending':
                return persona.earn_link(old['site'], old['reward'], old['min_seconds'], stamp(old['expires_at'])), markup(old['short_url'])
            if old['state'] == 'building' and now - old['created_at'] < 90:
                return persona.earn_notice('building'), markup()
            if old['state'] == 'accepted':
                result = await _apply(user, old)
                return persona.earn_notice('recovered' if result in ('credited','already') else 'retry'), markup()
        s = await db.get_settings()
        enabled, reward, minimum = settings_values(s)
        if not enabled or reward <= 0:
            return persona.earn_notice('disabled'), markup()
        providers = await active_providers()
        if not providers:
            return persona.earn_notice('unavailable'), markup()
        choices = await eligible(user, now, providers)
        if not choices:
            next_at = await availability(user, now)
            return persona.earn_notice('limit', when=stamp(next_at)), markup()
        # Continue alphabetically after the last SUCCESS, not after failed
        # calls/taps. Quota is tied to the site name; remove/re-add cannot reset it.
        last = (user.get('earn') or {}).get('last_site', '')
        ordered = [p for p in choices if p['site'] > last] + [p for p in choices if p['site'] <= last]
        username = await bot_username(bot)
        for provider in ordered:
            token = secrets.token_hex(16)  # 32 chars, Telegram start <=64 bytes
            attempt = {'_id':token, 'user_id':uid, 'site':provider['site'],
                'reward':reward, 'min_seconds':minimum, 'created_at':now,
                'expires_at':now + LINK_TTL, 'state':'building'}
            await db.col('earn_attempts').insert_one(attempt)
            await db.col('users').update_one({'user_id':uid}, {'$set':{'earn.pending':token}})
            destination = f'https://t.me/{username}?start=earn_{token}'
            short = await shorten(provider, destination)
            if not short:
                await db.col('earn_attempts').update_one({'_id':token}, {'$set':{'state':'failed'}})
                continue
            # Provider may have been paused/removed while the HTTP call ran.
            active = await db.col('earn_providers').find_one({'_id':provider['site'], 'status':'active'})
            if not active:
                await db.col('earn_attempts').update_one({'_id':token}, {'$set':{'state':'failed'}})
                continue
            issued_at = db.now_ts()
            await db.col('earn_attempts').update_one({'_id':token, 'state':'building'},
                {'$set':{'state':'pending', 'short_url':short, 'issued_at':issued_at,
                         'expires_at':issued_at + LINK_TTL}})
            return persona.earn_link(provider['site'], reward, minimum, stamp(issued_at + LINK_TTL)), markup(short)
        # No bypass/direct reward if all APIs fail; no rolling usage consumed.
        await db.col('users').update_one({'user_id':uid}, {'$unset':{'earn.pending':''}})
        return persona.earn_notice('unavailable'), markup()


async def cmd_earn(update, context):
    if getattr(update.effective_chat, 'type', None) != 'private':
        return
    text, kb = await issue(context.bot, update.effective_user.id)
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=kb)


async def solve_callback(update, context):
    q = update.callback_query
    text, kb = await issue(context.bot, q.from_user.id)
    await edit_card(q, text, kb)


async def reconcile(attempt):
    """Wallet marker proves credit until ledger + final attempt state exist.
    Clean it only AFTER those durable writes; normal wallets retain no growing
    permanent set of per-solve markers. A cleanup flag closes the last gap.
    """
    uid, token = attempt['user_id'], attempt['_id']
    current = await db.col('earn_attempts').find_one({'_id':token})
    doc = await db.get_user(uid)
    has_marker = bool(doc and token in (doc.get('earn') or {}).get('applied', {}))
    if not has_marker and (not current or current['state'] != 'credited'):
        return False
    if has_marker:
        at = attempt['credited_at']
        await db.col('transactions').update_one({'_id':f'earn:{token}'}, {'$setOnInsert': {
            'user_id':uid, 'amount':attempt['reward'], 'reason':'shortener_reward', 'at':at,
            'meta':{'site':attempt['site'], 'attempt':token, 'verification':'temporary_timer_return'}}}, upsert=True)
        await db.col('earn_attempts').update_one({'_id':token},
            {'$set':{'state':'credited', 'cleanup_pending':True}})
    await db.col('users').update_one({'user_id':uid}, {'$unset':{f'earn.applied.{token}':''}})
    await db.col('earn_attempts').update_one({'_id':token}, {'$set':{'cleanup_pending':False}})
    return True


async def _apply(user, attempt):
    uid, token = attempt['user_id'], attempt['_id']
    current = await db.col('earn_attempts').find_one({'_id':token})
    if current and current['state'] == 'credited':
        await reconcile(current)
        return 'already'
    if token in (user.get('earn') or {}).get('applied', {}):
        await reconcile(attempt)
        return 'already'
    # accepted_at was fixed before a crash; its rolling usage and due timestamp
    # must not move because the ledger was unavailable after the wallet write.
    at = attempt['credited_at']
    events = history(user, db.now_ts())
    if sum(h['site'] == attempt['site'] for h in events) >= LIMIT:
        await db.col('earn_attempts').update_one({'_id':token}, {'$set':{'state':'quota_denied'}})
        return 'limit'
    events.append({'site':attempt['site'], 'at':at})
    earn = user.get('earn') or {}
    version = int(earn.get('version', 0))
    ver_query = {'earn.version':version} if 'version' in earn else {'earn.version':{'$exists':False}}
    result = await db.col('users').update_one({'user_id':uid, 'welcomed':True,
        f'earn.applied.{token}':{'$exists':False}, 'tokens':{'$lte':MAX_BALANCE - attempt['reward']},
        **ver_query}, {'$inc':{'tokens':attempt['reward'], 'earn.version':1}, '$set':{
        f'earn.applied.{token}':at, 'earn.history':events, 'earn.last_site':attempt['site'],
        'earn.last_completion':at, 'earn.remind_at':at + DAY, 'earn.reminder_seq':0,
        'earn.remind_generation':token}, '$unset':{'earn.pending':''}})
    if not result.modified_count:
        return 'retry'
    await reconcile(attempt)
    return 'credited'


async def complete(bot, uid, token):
    async with user_lock(uid):
        if not re.fullmatch(r'[a-f0-9]{32}', token or ''):
            return persona.earn_notice('invalid'), markup()
        attempt = await db.col('earn_attempts').find_one({'_id':token, 'user_id':uid})
        user = await db.get_user(uid)
        if not attempt or not user or not user.get('welcomed'):
            return persona.earn_notice('invalid'), markup()
        now = db.now_ts()
        if attempt['state'] == 'credited':
            return persona.earn_notice('already'), markup()
        if attempt['state'] == 'accepted':
            state = await _apply(user, attempt)
        else:
            if attempt['state'] != 'pending' or attempt['expires_at'] <= now:
                return persona.earn_notice('expired'), markup()
            # Only the CURRENT active attempt for this user may be returned.
            if (user.get('earn') or {}).get('pending') != token:
                return persona.earn_notice('expired'), markup()
            wait = attempt['issued_at'] + attempt['min_seconds'] - now
            if wait > 0:
                return persona.earn_notice('early', seconds=int(wait) + 1), markup()
            # Pending promises survive provider pause/removal and reward edits;
            # state/user/quota/timer still enforced before wallet credit.
            accepted = await db.col('earn_attempts').find_one_and_update(
                {'_id':token, 'state':'pending'}, {'$set':{'state':'accepted','credited_at':now}},
                return_document=ReturnDocument.AFTER)
            if not accepted:
                return persona.earn_notice('retry'), markup()
            attempt = accepted
            state = await _apply(user, attempt)
        if state in ('credited','already'):
            balance = int((await db.get_user(uid) or {}).get('tokens', 0))
            return persona.earn_success(attempt['reward'], balance, stamp(attempt['credited_at'] + DAY)), markup()
        return persona.earn_notice(state if state == 'limit' else 'retry'), markup()


async def complete_update(update, context, token):
    try:
        text, kb = await complete(context.bot, update.effective_user.id, token)
    except Exception as exc:
        log.warning('Reward completion deferred error=%s', type(exc).__name__)
        text, kb = persona.earn_notice('retry'), markup()
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=kb)


async def recover_batch():
    # No accepted attempt is automatically credited without its wallet marker.
    # Safely resume the atomic credit under the same per-user lock as returns.
    attempts = await db.col('earn_attempts').find({'$or':[{'state':'accepted'},
        {'state':'credited','cleanup_pending':True}]}).limit(BATCH).to_list(BATCH)
    for a in attempts:
        async with user_lock(a['user_id']):
            u = await db.get_user(a['user_id'])
            if u:
                await _apply(u, a)


async def reminder_batch(bot):
    async with _notify_lock:
        now = db.now_ts()
        s = await db.get_settings()
        enabled, reward, _ = settings_values(s)
        if not enabled or not reward or not await active_providers():
            return 0
        rows = await db.col('users').find({'welcomed':True,
            'earn.remind_at':{'$lte':now}}).sort('earn.remind_at',1).limit(BATCH).to_list(BATCH)
        sent = 0
        for user in rows:
            uid = user['user_id']
            # Lock prevents a completion from racing a due reminder. A successful
            # completion always resets the next due time, never overwritten here.
            async with user_lock(uid):
                user = await db.get_user(uid)
                earn = (user or {}).get('earn') or {}
                due = earn.get('remind_at')
                if due is None or due > now:
                    continue
                ready_at = await availability(user, now)
                if ready_at is None:
                    continue
                if ready_at > now:
                    await db.col('users').update_one({'user_id':uid, 'earn.remind_at':due},
                        {'$set':{'earn.remind_at':ready_at}})
                    continue
                generation = earn['remind_generation']
                seq = int(earn.get('reminder_seq', 0))
                rid = f'{uid}:{generation}:{seq}'
                rec = await db.col('earn_notices').find_one({'_id':rid})
                if rec and rec.get('retry_at', 0) > now:
                    # Move to retry_at to prevent a deferred row monopolizing pages.
                    await db.col('users').update_one({'user_id':uid, 'earn.remind_generation':generation},
                        {'$set':{'earn.remind_at':rec['retry_at']}})
                    continue
                if rec and rec['state'] in ('sent','uncertain','sending','failed','blocked'):
                    state = 'uncertain' if rec['state'] == 'sending' else rec['state']
                    await db.col('earn_notices').update_one({'_id':rid}, {'$set':{'state':state}})
                    await _advance_notice(uid, generation, seq, rec.get('scheduled_at', due), state, now)
                    continue
                await db.col('earn_notices').update_one({'_id':rid}, {'$setOnInsert': {
                    'user_id':uid, 'generation':generation, 'seq':seq,
                    'state':'pending','attempts':0,'scheduled_at':due}}, upsert=True)
                rec = await db.col('earn_notices').find_one_and_update(
                    {'_id':rid,'state':'pending'}, {'$set':{'state':'sending'},'$inc':{'attempts':1}},
                    return_document=ReturnDocument.AFTER)
                if not rec:
                    continue
                state = 'sent'
                try:
                    await bot.send_message(uid, persona.earn_reminder(int(earn.get('reminder_style', 0)), reward), parse_mode='HTML',
                                           reply_markup=markup())
                    sent += 1
                except RetryAfter as exc:
                    if rec['attempts'] < 5:
                        delay = exc.retry_after
                        delay = delay.total_seconds() if hasattr(delay,'total_seconds') else float(delay)
                        retry_at = now + max(1, delay) + 1
                        await db.col('earn_notices').update_one({'_id':rid},
                            {'$set':{'state':'pending','retry_at':retry_at}})
                        await db.col('users').update_one({'user_id':uid, 'earn.remind_generation':generation},
                            {'$set':{'earn.remind_at':retry_at}})
                        continue
                    state = 'failed'
                except Forbidden:
                    state = 'blocked'
                except BadRequest:
                    state = 'failed'
                except NetworkError:
                    state = 'uncertain'
                except Exception:
                    state = 'uncertain'
                await db.col('earn_notices').update_one({'_id':rid}, {'$set':{'state':state,'at':now}})
                await _advance_notice(uid, generation, seq, rec.get('scheduled_at', due), state, now)
            await asyncio.sleep(.08)
        return sent


async def _advance_notice(uid, generation, seq, due, state, now):
    # Keep the intended daily anchor, skip missed days after downtime (no burst).
    next_due = due + (int(max(0, now - due) // DAY) + 1) * DAY
    change = {'$inc':{'earn.reminder_seq':1,'earn.reminder_style':1}, '$set':{'earn.remind_at':next_due}}
    if state in ('blocked','failed'):
        change = {'$inc':{'earn.reminder_seq':1,'earn.reminder_style':1}, '$unset':{'earn.remind_at':''}}
    await db.col('users').update_one({'user_id':uid, 'earn.remind_generation':generation,
                                    'earn.reminder_seq':seq}, change)


async def loop(bot):
    while True:
        try:
            await recover_batch()
            await reminder_batch(bot)
        except Exception as exc:
            log.warning('Shortener reward worker deferred error=%s', type(exc).__name__)
        await asyncio.sleep(POLL)
