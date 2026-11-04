"""Root-only force-sub UI helpers. No background polls or cached membership.
Membership policy stays in each bot's _is_member: inconclusive checks fail
open as before, and channel-scoped recorded join requests satisfy the gate.
Only public channel display metadata is cached, bounded and per bot identity.
"""
import asyncio
from collections import OrderedDict
import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
import db

log = logging.getLogger('root.force_sub')
TITLE_TTL = 600.0
TITLE_ERROR_TTL = 30.0
MAX_TITLE_CACHE = 256
_metadata = OrderedDict()
_metadata_loop = None


async def missing_channels(bot, user_id, category=None, *, member_check):
    """Return only unsatisfied requirements in configured order, without dupes.
    No positive/negative membership cache: a newly added channel or a user
    leaving a channel is reflected on the very next download/check-again.
    """
    missing, seen = [], set()
    for channel in await db.force_sub_channels(category):
        if not channel:
            continue
        channel = int(channel)
        if channel in seen:
            continue
        seen.add(channel)
        member = await member_check(bot, channel, user_id)
        if member is True or member is None:
            continue
        if await db.has_join_request(user_id, channel):
            continue
        missing.append(channel)
    return missing


def _cache_key(bot, channel_id):
    # A cached title is harmless, but keep each bot's permissions/metadata
    # isolated. Use its public numeric identity; never store its token.
    try:
        ident = int(bot.id)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        ident = id(bot)
    return ident, int(channel_id)


async def channel_info(bot, channel_id, *, refresh=False):
    global _metadata_loop
    loop = asyncio.get_running_loop()
    if _metadata_loop is not loop:
        _metadata.clear()
        _metadata_loop = loop
    key, now = _cache_key(bot, channel_id), time.monotonic()
    cached = _metadata.get(key)
    if cached and not refresh and cached['until'] > now:
        _metadata.move_to_end(key)
        return dict(cached)
    fallback = f'Channel {int(channel_id)}'
    title, username, ttl = fallback, None, TITLE_ERROR_TTL
    try:
        chat = await bot.get_chat(int(channel_id))
        candidate = getattr(chat, 'title', None)
        if isinstance(candidate, str) and candidate.strip():
            title = ' '.join(candidate.split())[:256]
            ttl = TITLE_TTL
        candidate = getattr(chat, 'username', None)
        if isinstance(candidate, str) and candidate.strip():
            username = candidate.strip().lstrip('@')
    except Exception as exc:
        # A failed getChat cannot bypass membership or invent an invite URL.
        log.warning('Channel title unavailable channel=%s error=%s', channel_id, type(exc).__name__)
        if cached and cached['title'] != fallback:
            title, username = cached['title'], cached['username']
    info = {'title': title, 'username': username, 'until': now + ttl}
    _metadata[key] = info
    _metadata.move_to_end(key)
    while len(_metadata) > MAX_TITLE_CACHE:
        _metadata.popitem(last=False)
    return dict(info)


async def channel_title(bot, channel_id, *, refresh=False):
    return (await channel_info(bot, channel_id, refresh=refresh))['title']


def button_title(title):
    label = '📢 ' + title
    return label if len(label) <= 60 else label[:59] + '…'


async def prompt(bot, chat_id, user_id, callback_data, category=None, *,
                 member_check, missing=None, query=None):
    """Render/refresh only unsatisfied channels; never deliver or mint tokens.
    Pass a freshly computed snapshot from the caller to avoid rechecking every
    channel just to draw the initial screen. Callback refreshes compute anew.
    """
    if missing is None:
        missing = await missing_channels(bot, user_id, category, member_check=member_check)
    rows, unavailable = [], []
    for channel in dict.fromkeys(missing):
        info = await channel_info(bot, channel)
        url = await db.force_sub_link(channel, category)
        if not url and info['username']:
            url = 'https://t.me/' + info['username']
        if not url:
            try:
                url = await bot.export_chat_invite_link(channel)
            except Exception as exc:
                log.warning('Channel invite unavailable channel=%s error=%s', channel, type(exc).__name__)
        if url:
            rows.append([InlineKeyboardButton(button_title(info['title']), url=url)])
        else:
            unavailable.append(info['title'])
    rows.append([InlineKeyboardButton("✅ I've Joined", callback_data=callback_data)])
    if missing:
        count = len(missing)
        noun = 'channel' if count == 1 else 'channels'
        text = (f'🔒 <b>Join {count} {noun} to continue</b>\n\n'
                'Only channels you still need to join are shown.\n'
                'Already joined or requested channels are hidden.\n\n'
                'Tap a channel below, join or request access, then tap '
                '<b>✅ I\'ve Joined</b>.')
        if unavailable:
            text += ('\n\n⚠️ Invite unavailable for: ' +
                     ', '.join(html.escape(t) for t in unavailable) +
                     '. Please contact an admin; access is still required.')
    else:
        # Membership may change between a legacy caller's check and display.
        text = '✅ <b>All required channels are satisfied.</b>\nTap <b>I\'ve Joined</b> to continue.'
    kwargs = {'reply_markup': InlineKeyboardMarkup(rows), 'parse_mode': 'HTML',
              'disable_web_page_preview': True}
    if query is not None:
        try:
            await query.edit_message_text(text, **kwargs)
            return
        except BadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return
        except Exception:
            pass
        # Deleted/old/non-editable prompt: send the fresh filtered screen.
    await bot.send_message(chat_id, text, **kwargs)
