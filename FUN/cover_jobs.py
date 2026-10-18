"""Explicit bounded backfill. Snapshot metadata before network waits so Mongo
cursor idle timeouts cannot end the job at the default first batch (101).
Progress means attempted covers, NOT successful hosted URLs.
"""
import asyncio
import logging
import httpx
import db
import covers
import cover_hosts
import runtime

log = logging.getLogger('fun.cover_jobs')
_task = None
QUERY = {'$or': [{'cover.url_status': {'$ne': 'ok'}}, {'cover.url': None}, {'cover.url': ''}]}
RATE_WAIT_SECONDS = 180
MAX_RATE_RETRIES = 2


def busy():
    return _task is not None and not _task.done()


def start(userbot, message=None, reply=None):
    global _task
    if busy():
        return _task
    _task = runtime.spawn(_run(userbot, message, reply))
    return _task


async def _progress(message, text):
    edit = getattr(message, 'edit_text', None) or getattr(message, 'edit_message_text', None)
    if edit:
        try:
            await edit(text)
        except Exception:
            pass


async def _run(userbot, message, reply):
    state = {'total': 0, 'processed': 0, 'hosted': 0, 'local_ready': 0,
             'failed': 0, 'state': 'running', 'rate_limited_waits': 0}
    try:
        # Metadata only, no JPEGs or delivery-file lists. Exhaust the cursor
        # BEFORE upload timeouts/cooldowns, then retry each item in an inner loop.
        docs = await db.col('collections').find(QUERY,
            {'collection_id': 1, 'cover': 1, 'channel_id': 1}).to_list(length=None)
        state['total'] = len(docs)
        await db.col('cover_jobs').update_one({'_id': 'current'}, {'$set': state}, upsert=True)
        async with httpx.AsyncClient() as http:
            for doc in docs:
                try:
                    retries = 0
                    while True:
                        result = await covers.prepare_collection(userbot, doc, http, force=True)
                        if not result.get('rate_limited'):
                            break
                        if retries >= MAX_RATE_RETRIES:
                            state['state'] = 'rate_limited_paused'
                            break
                        retries += 1
                        state['rate_limited_waits'] += 1
                        await _progress(message, '⏳ All hosts are temporarily unavailable. Waiting, then retrying this SAME cover…')
                        await asyncio.sleep(RATE_WAIT_SECONDS)
                    if state['state'] == 'rate_limited_paused':
                        break
                    state['processed'] += 1
                    if result['hosted']:
                        state['hosted'] += 1
                    elif result.get('ready'):
                        state['local_ready'] += 1
                    else:
                        state['failed'] += 1
                    if result.get('all_hosts_unavailable'):
                        state['state'] = 'hosts_unavailable_paused'
                        break  # do not repeat rejected requests 162 times
                except Exception as exc:
                    state['processed'] += 1
                    state['failed'] += 1
                    diagnostic = 'preparation_error: ' + type(exc).__name__
                    await db.col('collections').update_one({'collection_id': doc['collection_id']},
                        {'$set': {'cover.url_status': 'failed', 'cover.last_error': diagnostic}})
                await db.col('cover_jobs').update_one({'_id': 'current'}, {'$set': state})
                if state['processed'] % 10 == 0:
                    await _progress(message, f"🎀 Attempted {state['processed']}/{state['total']}; hosted {state['hosted']}; cached photo only {state['local_ready']}.")
        if state['state'] == 'running':
            state['state'] = 'complete' if state['hosted'] == state['total'] else 'completed_with_hosting_failures'
        state['remaining'] = state['total'] - state['processed']
        warning = '\n⚠️ No new hosted URLs. Cached photos are NOT link previews.' if state['hosted'] == 0 and state['total'] else ''
        text = (f"🎀 Cover job: {state['state']}\n"
                f"Attempted: {state['processed']}/{state['total']}\n"
                f"Hosted links: {state['hosted']}\n"
                f"Cached photo only (hosting failed): {state['local_ready']}\n"
                f"Source/preparation failures: {state['failed']}\n"
                f"Not attempted: {state['remaining']}" + warning + '\nUse /cover_status for per-host diagnostics.')
        await _progress(message, text)
        if reply and not (getattr(message, 'edit_text', None) or getattr(message, 'edit_message_text', None)):
            await reply(text)
    except asyncio.CancelledError:
        state['state'] = 'interrupted'
        raise
    except Exception as exc:
        state['state'] = 'failed'
        state['job_error'] = type(exc).__name__
        log.warning('Cover preparation interrupted (%s)', type(exc).__name__)
        if reply:
            await reply('⚠️ Cover job stopped. Cached successes are kept. Check /cover_status.')
    finally:
        state['provider_diagnostics'] = cover_hosts.diagnostics()
        await db.col('cover_jobs').update_one({'_id': 'current'}, {'$set': state}, upsert=True)


async def statistics():
    col = db.col('collections')
    ready = {'cover.url_status': 'ok', 'cover.url': {'$type': 'string', '$ne': ''}}
    result = {'Collections': await col.count_documents({}),
              'Hosted URL ready': await col.count_documents(ready),
              'Telegraph links ready': await col.count_documents({'cover.url_status': 'ok', 'cover.url': {'$regex': r'^https://(telegra\.ph|graph\.org)/file/'}}),
              'Catbox links ready': await col.count_documents({'cover.url_status': 'ok', 'cover.url': {'$regex': r'^https://files\.catbox\.moe/'}}),
              'Feed mode': 'in-place direct-image link preview; cached-photo fallback if no URL',
              'Pending': await col.count_documents({'cover.url_status': 'pending'}),
              'No status yet': await col.count_documents({'cover.url_status': {'$exists': False}}),
              'Mirror upload failed': await col.count_documents({'cover.url_status': 'failed'}),
              'Cached cover assets ready': await col.count_documents({'cover.asset_keys.0': {'$exists': True}}),
              'Cached images': await db.col('cover_assets').count_documents({'data': {'$exists': True}}),
              'Worker running': 'yes' if busy() else 'no'}
    state = await db.col('cover_jobs').find_one({'_id': 'current'})
    if state:
        result['Last job'] = f"{state.get('processed',0)}/{state.get('total',0)} attempted; {state.get('hosted',0)} hosted; {state.get('state','unknown')}"
        for host, text in (state.get('provider_diagnostics') or {}).items():
            result[host] = cover_hosts.sanitize(text)
    for host, text in cover_hosts.diagnostics().items():
        result[host] = cover_hosts.sanitize(text)
    last = await col.find_one({'cover.last_error': {'$exists': True, '$nin': [None, '']}},
        {'cover.last_error': 1}, sort=[('cover.last_attempt_at', -1)])
    if last:
        result['Last diagnostic'] = cover_hosts.sanitize((last.get('cover') or {}).get('last_error'))
    return result
