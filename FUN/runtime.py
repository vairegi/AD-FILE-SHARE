"""Bot 3 source monitoring: live events plus independent polling catch-up.
One Render process only. Source indexing never waits for cover-host uploads.
"""
import asyncio
import logging
import config
import db

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
    try:
        await asyncio.sleep(EVENT_DEBOUNCE_SECONDS)
        if userbot and await db.category_for_channel(cid):
            schedule_scan(cid)
    finally:
        _debounces.pop(cid, None)


async def replace_userbot(client):
    global userbot
    old = userbot
    if old is client:
        return
    _unbind_events()
    for task in list(_scans.values()):
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
    userbot = client
    _entity_cache.clear()
    _bind_events(client)
    if old and old is not client:
        await old.disconnect()


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


async def _scan(cid, reply=None, full=False):
    try:
        while True:
            _rescan_requested.discard(int(cid))
            client = userbot
            if not client:
                raise ValueError('No connected userbot. Use /setup_userbot.')
            category = await db.category_for_channel(cid)
            if not category:
                raise ValueError('Channel is not mapped. Use /addchannel first.')
            import scanner
            source = await resolve_channel(client, cid)
            result = await scanner.scan_channel(client, cid, category, full=full,
                                                source=source, prepare_covers=False)
            _queue_covers(client, result.get('cover_collection_ids', []))
            log.info('Source indexed channel=%s media=%s collections_updated=%s',
                     cid, result['posts_scanned'], result['collections'])
            if reply:
                await reply(f"✅ Scan complete, Senpai~! {result['posts_scanned']} media posts; "
                            f"{result['collections']} collections updated; {result['pending_files']} orphan files. "
                            'Covers prepare separately. Reopen Categories to refresh its count.')
                reply = None
            if int(cid) not in _rescan_requested:
                return result
            full = False
    except Exception as exc:
        log.error('Channel scan failed channel=%s error=%s (no credentials logged)',
                  cid, type(exc).__name__)
        await db.col('scan_state').update_one({'_id': int(cid)}, {'$set': {
            'last_error_type': type(exc).__name__, 'last_error_at': db.now_ts()}}, upsert=True)
        if reply:
            await reply('⚠️ Scan failed. Use /userbot_status and /channels; check the source access and logged error type. No purchases were removed.')
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


async def deletion_loop():
    import services
    while True:
        try:
            await services.run_deletion_sweeper(bot)
        except Exception:
            log.error('Cleanup tick failed; persisted jobs will retry')
        await asyncio.sleep(20)


async def _ensure_userbot():
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


async def poll_once():
    """One failing/missing channel cannot starve all later mappings."""
    if not await _ensure_userbot():
        return
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
            source = await resolve_channel(userbot, cid)
            latest = await asyncio.wait_for(userbot.get_messages(source, limit=1), timeout=30)
            if latest and (not state.get('cursor') or latest[0].id > int(state.get('cursor', 0))):
                schedule_scan(cid)
        except Exception as exc:
            log.warning('Auto-scan check failed channel=%s error=%s; continuing other channels',
                        cid, type(exc).__name__)
            _entity_cache.pop((id(userbot), cid), None)


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
    _background.extend([spawn(deletion_loop()), spawn(scan_loop())])


async def stop_jobs():
    global _cover_task
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
