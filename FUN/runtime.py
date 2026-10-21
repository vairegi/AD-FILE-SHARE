"""Bot 3 source monitoring: live events plus independent polling catch-up.
One Render process only. Source indexing never waits for cover-host uploads.
"""
import asyncio
import logging
import config
import db
import session_health

log = logging.getLogger('fun.runtime')
userbot = None
bot = None
_scans = {}
_tasks = set()
_background = []
_rescan_requested = set()
_entity_cache = {}
_debounces = {}
_cover_pending = {}
_cover_task = None
_event_client = None
_event_callback = None
POLL_SECONDS = 30
EVENT_DEBOUNCE_SECONDS = 2
_history_paused = False
SCAN_BUDGET_SECONDS = 180


def spawn(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def _unbind_events():
    global _event_client, _event_callback
    if _event_client is not None and _event_callback is not None:
        _event_client.remove_event_handler(_event_callback)
    _event_client = _event_callback = None


def _bind_events(client):
    global _event_client, _event_callback
    if client is None or not callable(getattr(client, 'add_event_handler', None)):
        return
    from telethon import events
    _event_callback = _on_source_event
    client.add_event_handler(_event_callback, events.NewMessage())
    client.add_event_handler(_event_callback, events.Album())
    _event_client = client


async def _on_source_event(event):
    """Telethon user-account updates, not Bot API channel-post updates."""
    cid = getattr(event, 'chat_id', None)
    if cid is None or not await db.category_for_channel(cid):
        return
    state = await db.col('scan_state').find_one({'_id': int(cid)})
    if not state:
        # Initial history scan remains explicit for newly mapped channels.
        existing = await db.col('collections').find_one({'channel_id': int(cid)}, {'_id': 1})
        if not existing:
            return
    if scan_busy(cid):
        _rescan_requested.add(int(cid))
        return
    if cid not in _debounces:
        task = spawn(_debounced_scan(int(cid)))
        _debounces[cid] = task


async def _debounced_scan(cid):
    started = db.now_ts()
    try:
        await asyncio.sleep(EVENT_DEBOUNCE_SECONDS)
        if userbot and await db.category_for_channel(cid):
            # Prefer the metadata queue for a live burst. The 30-second poll
            # remains an independent history backstop for missed webhooks.
            pending = await db.col('source_jobs').find_one({'_id': int(cid)}, {'_id': 1})
            recent = await db.col('live_scan_state').find_one({'_id': int(cid)}) or {}
            if pending or recent.get('last_indexed_at', 0) >= started:
                return
            schedule_scan(cid)
    finally:
        _debounces.pop(cid, None)


async def replace_userbot(client):
    global userbot
    old = userbot
    if old is client:
        return
    _unbind_events()
    pending = [task for task in list(_scans.values())
               if task is not asyncio.current_task() and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    userbot = client
    _entity_cache.clear()
    _bind_events(client)
    if old and old is not client:
        for key, queued_client in list(_cover_pending.items()):
            if queued_client is old:
                _cover_pending.pop(key, None)
        await session_health.safe_disconnect(old)


async def disconnect_userbot():
    await replace_userbot(None)


async def resolve_channel(client, cid):
    """Recover missing access hashes after StringSession-based restarts."""
    cid = int(cid)
    key = (id(client), cid)
    if key in _entity_cache:
        return _entity_cache[key]
    resolver = getattr(client, 'get_input_entity', None)
    if not callable(resolver):
        return cid  # offline scanner fixtures also accept numeric IDs
    try:
        entity = await asyncio.wait_for(resolver(cid), timeout=30)
    except ValueError:
        # StringSession does not persist the full entity cache. Find the
        # mapped joined channel in dialogs once, instead of failing every tick.
        entity = None
        async def search_dialogs():
            async for dialog in client.iter_dialogs():
                if int(dialog.id) == cid:
                    return dialog.input_entity
            return None
        entity = await asyncio.wait_for(search_dialogs(), timeout=45)
        if entity is None:
            raise ValueError('Mapped source is not in the user account dialogs') from None
    _entity_cache[key] = entity
    return entity


def scan_busy(cid):
    task = _scans.get(int(cid))
    return task is not None and not task.done()


def _queue_covers(client, ids):
    global _cover_task
    for collection_id in ids:
        _cover_pending[collection_id] = client
    if _cover_pending and (_cover_task is None or _cover_task.done()):
        _cover_task = spawn(_cover_loop())


async def _cover_loop():
    """Single bounded cover worker; it never holds a channel scan lock."""
    global _cover_task
    import scanner
    try:
        while _cover_pending:
            cid = next(iter(_cover_pending))
            client = _cover_pending.pop(cid)
            try:
                doc = await db.get_collection(cid)
                if doc:
                    result = await scanner._prepare_cover(client, doc['channel_id'], doc)
                    if result.get('all_hosts_unavailable') or result.get('rate_limited'):
                        # Persisted cover statuses can be retried via /retry_covers.
                        # Do not spin/upload every pending collection on an outage.
                        _cover_pending.clear()
                        log.warning('Cover worker paused; source indexing continues. Use /retry_covers.')
                        break
            except Exception as exc:
                log.warning('Background cover failed collection=%s error=%s', cid, type(exc).__name__)
    finally:
        _cover_task = None


async def pause_userbot(exc, client=None):
    """Stop retrying invalid auth, without touching the bot-only metadata queue."""
    if client is not None and userbot is not client:
        return  # stale failure from a client the owner already replaced
    previous = await session_health.blocked()
    log.warning('Historical reader paused error=%s; new channel-post queue remains independent',
                type(exc).__name__)
    await db.update_settings({'userbot_health': {
        'state': 'reauth_required', 'error_type': type(exc).__name__, 'at': db.now_ts()}})
    await replace_userbot(None)
    if not previous and bot is not None and config.OWNER_ID:
        try:
            await asyncio.wait_for(bot.send_message(
                config.OWNER_ID, session_health.recovery(type(exc).__name__)), timeout=5)
        except Exception:
            pass


async def _scan(cid, reply=None, full=False):
    passes = 0
    try:
        while True:
            passes += 1
            _rescan_requested.discard(int(cid))
            client = userbot
            if not client:
                raise ValueError('No connected userbot. Use /setup_userbot.')
            category = await db.category_for_channel(cid)
            if not category:
                raise ValueError('Channel is not mapped. Use /addchannel first.')
            import scanner
            await db.col('scan_state').update_one({'_id': int(cid)}, {'$set': {
                'scan_status': 'waiting_for_channel', 'started_at': db.now_ts()}}, upsert=True)
            async def bounded_pass():
                source = await resolve_channel(client, cid)
                end_id = None
                if callable(getattr(client, 'get_messages', None)):
                    latest = await asyncio.wait_for(client.get_messages(source, limit=1), timeout=30)
                    if latest:
                        end_id = latest[0].id
                return await scanner.scan_channel(client, cid, category, full=full,
                                                  source=source, prepare_covers=False, end_id=end_id)
            # Includes source resolution, shared-lock waiting and metadata reads.
            result = await asyncio.wait_for(bounded_pass(), timeout=SCAN_BUDGET_SECONDS)
            _queue_covers(client, result.get('cover_collection_ids', []))
            log.info('Source indexed channel=%s media=%s collections_updated=%s',
                     cid, result['posts_scanned'], result['collections'])
            if reply:
                await reply(f"✅ Scan complete, Senpai~! {result['posts_scanned']} media posts; "
                            f"{result['collections']} collections updated; {result['pending_files']} orphan files. "
                            'Covers prepare separately. Reopen Categories to refresh its count.')
                reply = None
            if int(cid) not in _rescan_requested or passes >= 2:
                return result
            full = False
    except asyncio.CancelledError:
        await db.col('scan_state').update_one({'_id': int(cid)}, {'$set': {
            'scan_status': 'stopped', 'finished_at': db.now_ts()}}, upsert=True)
        if reply:
            try:
                await asyncio.wait_for(reply('⏹ Scan stopped for userbot removal/replacement or service shutdown. '
                    'Saved progress and purchases are preserved; new channel-post indexing is independent.'), timeout=3)
            except Exception:
                pass
        raise
    except Exception as exc:
        log.error('Channel scan failed channel=%s error=%s (no credentials logged)',
                  cid, type(exc).__name__)
        await db.col('scan_state').update_one({'_id': int(cid)}, {'$set': {
            'last_error_type': type(exc).__name__, 'last_error_at': db.now_ts(),
            'scan_status': 'failed', 'finished_at': db.now_ts()}}, upsert=True)
        if session_health.fatal(exc):
            await pause_userbot(exc, locals().get('client'))
        if reply:
            if session_health.fatal(exc):
                await reply(session_health.recovery(type(exc).__name__))
            elif isinstance(exc, TimeoutError):
                await reply('⏳ Scan timed out and released the channel lock. Saved progress is preserved. '
                            'Use /scan_status, then /scan again to resume. Check userbot/source access. '
                            'New channel-post indexing continues; no purchases were removed.')
            else:
                await reply('⚠️ Scan failed (' + type(exc).__name__ + '). Use /userbot_status and /channels; '
                            'check the source access. Saved progress and purchases are preserved.')
    finally:
        _scans.pop(int(cid), None)
        _rescan_requested.discard(int(cid))


def schedule_scan(cid, reply=None, full=False):
    cid = int(cid)
    if scan_busy(cid):
        _rescan_requested.add(cid)
        return _scans[cid]
    task = spawn(_scan(cid, reply, full))
    _scans[cid] = task
    return task


async def scan_status_text(cid):
    cid = int(cid)
    state = await db.col('scan_state').find_one({'_id': cid}) or {}
    active = scan_busy(cid)
    status = state.get('scan_status', 'not started')
    if not active and status in ('running', 'waiting_for_channel'):
        status = 'interrupted — resume with /scan'
    elapsed = max(0, int(db.now_ts() - state.get('started_at', db.now_ts())))
    health = await session_health.blocked()
    reason = health.get('error_type') if health else state.get('last_error_type')
    return (f"🎀 Historical scan · {cid}\nState: {status}\n"
            f"Active worker: {'yes' if active else 'no'}\n"
            f"Elapsed since this pass started: {elapsed}s\n"
            f"Saved cursor: {state.get('cursor', 0)}\n"
            f"Target at pass start: {state.get('target_message_id') or 'not captured'}\n"
            f"Last progress message: {state.get('progress_message_id', 0)}\n"
            f"Media seen this pass: {state.get('progress_posts', 0)}\n"
            f"Error: {reason or 'none'}\n"
            "Use /scan to resume from the saved cover boundary; /index_status checks the independent new-post queue.")


async def deletion_loop():
    import services
    while True:
        try:
            await services.run_deletion_sweeper(bot)
        except Exception:
            log.error('Cleanup tick failed; persisted jobs will retry')
        await asyncio.sleep(20)


async def _ensure_userbot():
    # Startup, polling and an owner command must not construct multiple clients
    # concurrently in this process. This is not a cross-service/session lease.
    from locks import user_lock
    async with user_lock(-19990003):
        if await session_health.blocked():
            return False
        try:
            if userbot is None:
                import userbot_setup
                client = await userbot_setup.build_client()
                if client is None:
                    return False
                await replace_userbot(client)
            check = getattr(userbot, 'is_connected', None)
            if callable(check) and not check():
                await asyncio.wait_for(userbot.connect(), timeout=30)
                if not await asyncio.wait_for(userbot.is_user_authorized(), timeout=15):
                    raise ValueError('User session is no longer authorized')
            return True
        except Exception as exc:
            if session_health.fatal(exc):
                await pause_userbot(exc)
                return False
            raise


async def poll_once():
    """One failing/missing channel cannot starve all later mappings."""
    global _history_paused
    if not await _ensure_userbot():
        if not _history_paused:
            log.warning('Historical catch-up paused: no saved/connected userbot. '
                        'New channel posts use the independent durable queue; '
                        'owner: /setup_userbot then /userbot_status for missed history.')
        _history_paused = True
        return
    if _history_paused:
        log.info('Historical catch-up reader restored')
    _history_paused = False
    mappings = await db.list_channel_mappings()
    for mapping in mappings:
        cid = int(mapping['channel_id'])
        if scan_busy(cid):
            continue
        try:
            state = await db.col('scan_state').find_one({'_id': cid}) or {}
            existing = None
            if not state:
                existing = await db.col('collections').find_one({'channel_id': cid}, {'_id': 1})
                if not existing:
                    continue
            client = userbot
            if client is None:
                break
            source = await resolve_channel(client, cid)
            latest = await asyncio.wait_for(client.get_messages(source, limit=1), timeout=30)
            if latest and (not state.get('cursor') or latest[0].id > int(state.get('cursor', 0))):
                schedule_scan(cid)
        except Exception as exc:
            log.warning('Auto-scan check failed channel=%s error=%s; continuing other channels',
                        cid, type(exc).__name__)
            _entity_cache.pop((id(userbot), cid), None)
            if session_health.fatal(exc):
                await pause_userbot(exc, client)
                break


async def scan_loop():
    while True:
        try:
            await poll_once()
        except Exception as exc:
            log.warning('Automatic scan tick failed error=%s; retrying', type(exc).__name__)
        await asyncio.sleep(POLL_SECONDS)


def start_jobs(telegram_bot):
    global bot
    bot = telegram_bot
    import live_index
    _background.extend([spawn(deletion_loop()), spawn(scan_loop()),
                        spawn(live_index.loop(telegram_bot))])


async def stop_jobs():
    global _cover_task, _history_paused
    _history_paused = False
    import live_index
    live_index._last['state'] = 'stopped'
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*list(_tasks), return_exceptions=True)
    _tasks.clear()
    _background.clear()
    _scans.clear()
    _rescan_requested.clear()
    _debounces.clear()
    _cover_pending.clear()
    _cover_task = None
    await disconnect_userbot()
