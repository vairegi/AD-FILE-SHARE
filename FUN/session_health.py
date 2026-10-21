"""Safe, persisted fatal-session diagnosis; never store/log exception messages.
A broken Telegram auth key cannot be repaired by retrying it. Only a successfully
validated owner import clears this pause; the independent Bot API queue is untouched.
"""
import asyncio
import db

FATAL_NAMES = frozenset({'AuthKeyDuplicatedError', 'AuthKeyUnregisteredError',
                         'SessionRevokedError', 'SessionExpiredError',
                         'UserDeactivatedError', 'UserDeactivatedBanError'})


def fatal(exc):
    return type(exc).__name__ in FATAL_NAMES


def recovery(error='AuthKeyDuplicatedError'):
    if error == 'AuthKeyDuplicatedError':
        return ('⚠️ Telegram invalidated this userbot session: AuthKeyDuplicatedError.\n'
                'The same authorization key was used from different IP addresses. '
                'This key cannot be repaired by retrying.\n\n'
                'Stop other scripts/services using this session, including your laptop. '
                'Keep one Funbot instance and one worker. Generate a NEW dedicated '
                'Telethon StringSession on your trusted computer; exit that script '
                'before importing it through /setup_userbot. Do not resend the old string.\n\n'
                'Historical catch-up is paused. New channel-post indexing continues '
                'without the userbot. No purchases or balances were removed.')
    return ('⚠️ Telegram rejected this saved userbot authorization. Historical catch-up '
            'is paused; repeated retries are disabled. Generate a new dedicated '
            'Telethon StringSession on your trusted computer and import it through '
            '/setup_userbot. New channel-post indexing continues without the userbot. '
            'No purchases or balances were removed.')


async def blocked():
    state = (await db.get_settings()).get('userbot_health') or {}
    return state if state.get('state') == 'reauth_required' else None


async def safe_disconnect(client):
    if client is None:
        return
    try:
        await asyncio.wait_for(client.disconnect(), timeout=5)
    except Exception:
        pass


async def stored_recovery():
    state = await blocked()
    return recovery(state.get('error_type')) if state else 'No connected userbot. Use /setup_userbot first.'
