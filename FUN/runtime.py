"""Connected userbot and background jobs. One Render process only."""
import asyncio
import logging
import config
import db
log = logging.getLogger("fun.runtime")
userbot = None
bot = None
_scans = {}
_tasks = set()
_background = []


def spawn(coro):
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def replace_userbot(client):
    global userbot
    old = userbot
    # Wait for active channel scans before replacing their client.
    for task in list(_scans.values()):
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
    userbot = client
    if old and old is not client:
        await old.disconnect()


async def disconnect_userbot():
    await replace_userbot(None)


def scan_busy(cid):
    task = _scans.get(int(cid))
    return task is not None and not task.done()


async def _scan(cid, reply=None, full=False):
    try:
        if not userbot:
            raise ValueError("No connected userbot. Use /setup_userbot.")
        category = await db.category_for_channel(cid)
        if not category:
            raise ValueError("Channel is not mapped. Use /addchannel first.")
        import scanner
        result = await scanner.scan_channel(userbot, cid, category, full=full)
        if reply:
            await reply(f"✅ Scan complete, Senpai~! {result['posts_scanned']} media posts; "
                        f"{result['collections']} collections updated; {result['pending_files']} orphan files. "
                        "Use /collections to inspect indexed drops.")
        return result
    except Exception:
        log.error("Channel scan failed for %s (no credentials logged)", cid)
        if reply:
            await reply("⚠️ Scan failed. Check that the userbot account joined the source channel and that its ID is correct. "
                        "Use /userbot_status and /channels. No purchases were removed.")
    finally:
        _scans.pop(int(cid), None)


def schedule_scan(cid, reply=None, full=False):
    cid = int(cid)
    if scan_busy(cid):
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
            log.error("Cleanup tick failed; persisted jobs will retry")
        await asyncio.sleep(20)


async def scan_loop():
    while True:
        try:
            if userbot:
                mappings = await db.list_channel_mappings()
                for mapping in mappings:
                    cid = mapping["channel_id"]
                    if scan_busy(cid):
                        continue
                    # Compare latest id before re-reading the last collection.
                    state = await db.col("scan_state").find_one({"_id": cid}) or {}
                    if not state:
                        continue  # initial historical scan must be explicitly requested
                    latest = await userbot.get_messages(cid, limit=1)
                    if latest and latest[0].id > int(state.get("cursor", 0)):
                        schedule_scan(cid)
        except Exception:
            log.warning("Automatic scan tick failed; will retry")
        await asyncio.sleep(60)


def start_jobs(telegram_bot):
    global bot
    bot = telegram_bot
    _background.extend([spawn(deletion_loop()), spawn(scan_loop())])


async def stop_jobs():
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*list(_tasks), return_exceptions=True)
    _tasks.clear()
    _background.clear()
    _scans.clear()
    await disconnect_userbot()
