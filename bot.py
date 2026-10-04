import os
"""bot.py — entry point: session, commands, health server, main scrape loop.
v3: FloodWait-safe startup — on FloodWaitError the process SLEEPS IN-PLACE
(instead of crashing), so Render never enters a crash-restart loop that keeps
refreshing Telegram's flood timer. Clean task shutdown (no 'coroutine never
awaited' warnings). Py3.14-compatible event loop creation."""
import asyncio, logging, sys, time
from aiohttp import web
from telethon.errors import FloodWaitError
from config import POSTS_PER_ACCOUNT
from session_manager import SessionManager
from scraper import is_post, why_not_post
from flow import process_post, state, Abort
import commands, db as DB
import botapi
import dedup  # v45: DB2 duplicate-skip gate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tgscraper")

MAX_AUTH_RETRIES = 3


async def guarded(factory, name):
    """Await factory() with FloodWait protection. factory must return a FRESH
    coroutine each attempt (coroutines cannot be re-awaited). On FloodWait we
    sleep e.seconds INSIDE the process — Render sees a live process, so no
    restart loop and no re-auth spam against Telegram's servers."""
    for attempt in range(1, MAX_AUTH_RETRIES + 1):
        try:
            return await factory()
        except FloodWaitError as e:
            wait = e.seconds + 5
            log.warning("%s: FloodWaitError — sleeping %ds in-process (attempt %d/%d)",
                        name, wait, attempt, MAX_AUTH_RETRIES)
            if attempt == MAX_AUTH_RETRIES:
                log.error("%s: still flood-limited after %d attempts — parking "
                          "15 min then exiting cleanly (Render restarts AFTER "
                          "Telegram's timer expires, no loop).", name, attempt)
                await asyncio.sleep(900)
                sys.exit(0)
            await asyncio.sleep(wait)


def _fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


async def fmt_progress(client=None):
    s = await DB.get_stats()
    targets = await DB.get_targets()
    tlines = []
    for i, t in enumerate(targets):
        lp = await DB.get_last_post(t["id"])
        rp = await DB.get_progress(t["id"])
        flag = " ⏸PAUSED" if t.get("paused") else ""
        if client is not None:
            # titled + linked: private targets link to their last scraped post
            t_md = await botapi._chat_md(client, t["id"], msg_id=lp)
            inv = (await botapi._db_invite_link(client, t["db_id"])
                   if t.get("db_id") else None)
            db_md = (await botapi._chat_md(client, t["db_id"], invite=inv)
                     if t.get("db_id") else "(fallback)")
            tlines.append(f"  {i+1}. {t_md} → DB {db_md} "
                          f"(resume {rp}, last scraped {lp}){flag}")
        else:
            tlines.append(f"  {i+1}. {t['id']} → DB {t.get('db_id') or '(fallback)'} "
                          f"(resume {rp}, last scraped {lp}){flag}")
    tid = targets[0]["id"] if targets else None
    last = await DB.get_progress(tid) if tid else 0
    last_post = await DB.get_last_post(tid) if tid else None
    fails = await DB.get_failures(5)
    lines = [
        "\U0001F4CA PROGRESS",
        f"\U0001F4CD Stage: {state.stage}",
        f"\U0001F4C4 Current post: {state.current_post}",
        f"\u2705 Last scraped post: {last_post}",
        f"\u23ED Next resume point (msg id): {last}",
        f"\U0001F4C1 Posts scraped: {s.get('posts_done', 0)}",
        f"\U0001F3AC Videos sent: {s.get('videos_sent', 0)}",
        f"\U0001F4AC SRT sent: {s.get('srt_sent', 0)}",
        f"\u274C Failures: {s.get('failures', 0)}",
    ]
    if getattr(state, "workers", None):  # v39: live per-worker stages
        lines.append("👷 Parallel workers:")
        for w, stg in state.workers.items():
            lines.append(f"  • {w}: {stg}")
    if tlines:
        lines.append("🎯 Targets:")
        lines.extend(tlines)
    if fails:
        lines.append("— Recent failures —")
        for f in fails:
            lines.append(f"\u2022 post {f['post_id']} @ {f['stage']}: {f['reason']} ({_fmt_ts(f['ts'])})")
    return "\n".join(lines)


# v39: account index -> unix ts until which that account is flood-parked
# (its post stays unresolved and is retried next pass on a rested account).
_FLOOD_COOLDOWNS = {}


async def _parallel_one(client, cfg, msg, idx, name, resolved):
    """v39: process ONE post on ONE account inside a parallel wave.
    Completed/failed/skipped posts go into `resolved` (progress advances past
    them). A FloodWait does NOT resolve the post — the watermark stops at it
    and the next pass retries it, while the flooded account rests."""
    try:
        state.workers[name] = f"post {msg.id}: starting"
        # v39.2: refetch the post with THIS account's client before touching it.
        # The dispatcher's reader (acc1) fetched these message objects, and a
        # photo/document's access_hash + file_reference are ACCOUNT-BOUND —
        # acc2 re-sending acc1's media object gets MediaEmptyError ('media
        # object is invalid or the current account may not be able to send
        # it'). That's exactly how posts 39/41 died on acc2 while 38/40/42 on
        # acc1 delivered fine. A one-call refetch gives this worker its OWN
        # valid reference. In v38 the same account read+processed, so the bug
        # is specific to the shared-reader parallel path.
        fresh = await client.get_messages(cfg["target_id"], ids=msg.id)
        if not fresh:
            raise RuntimeError(f"post {msg.id} could not be re-read by this account")
        msg = fresh
        await process_post(client, cfg, msg, worker_name=name)
        resolved.add(msg.id)
        # v45: auto-grow — this cover is now archived in DB2, so a future
        # repost is gated WITHOUT re-scanning DB2
        await dedup.note_scraped(cfg["target_id"], msg.message or "", msg.id,
                                 db2_id=cfg.get("db2_id"))  # v45.1 sibling sync
        log.info("post %s done (%s, parallel)", msg.id, name)
    except Abort:
        await DB.add_failure(msg.id, state.workers.get(name, "?"), "skipped by user")
        resolved.add(msg.id)
    except FloodWaitError as e:
        _FLOOD_COOLDOWNS[idx] = time.time() + e.seconds
        log.warning("FloodWait %ds on %s — account parked; post %s retried next pass",
                    e.seconds, name, msg.id)
    except Exception as e:
        log.exception("post %s failed (%s: %s) — if 'ChannelPrivate'/'not a member', "
                      "that userbot account must JOIN the target channel first",
                      msg.id, type(e).__name__, e)
        await DB.add_failure(msg.id, state.workers.get(name, "?"), e)
        resolved.add(msg.id)
    finally:
        state.workers.pop(name, None)


async def _parallel_pass(sm, target, cfg, last_id, pass_gen):
    """v39: PARALLEL multi-userbot dispatcher for ONE target at a time.
    All available accounts share this target's pending posts — account 1
    takes post #1, account 2 takes post #2, etc., simultaneously (a single
    remaining post goes to any one free account). Delivery to the DB channel
    is serialized per DB channel by forwarder's lock, so one post's complete
    bundle (cover FIRST, then all media) always lands before the next bundle
    starts — posts never interleave. Progress advances only to the highest
    CONTIGUOUS resolved message id, so an out-of-order finish or a parked
    account can never make the watermark skip a post. When this target is
    caught up, the next loop pass moves to the next resumed target and its
    posts are fanned out across all accounts the same way.
    /pause lets in-flight posts finish but dispatches no new wave; /skip
    aborts every in-flight post (each is recorded as 'skipped by user')."""
    reader = sm.all()[0]
    collected = []  # [(msg_id, is_post, msg)] ascending — for the watermark walk
    pending = []
    # v39.1: fail LOUDLY when the reader account can't access the channel —
    # before this, a non-member account just made the pass die in the generic
    # 'scrape loop error' handler with no hint about the real cause
    try:
        await reader.get_messages(target, limit=1)
    except Exception as e:
        log.error("parallel: account 1 cannot READ target %s — %s: %s. "
                  "Every userbot must be a MEMBER of the target channel. Skipping pass.",
                  target, type(e).__name__, e)
        return
    async for msg in reader.iter_messages(target, min_id=last_id, reverse=True):
        if state.abort:
            state.abort = False
            return
        if state.reset_gen != pass_gen or target in state.paused_ids:
            return
        if not is_post(msg, cfg.get("link_mode", "button"),  # v42
                       cfg.get("link_trigger")):
            collected.append((msg.id, False, None))
        else:
            # v45: DB2 duplicate gate — a cover already archived in DB2
            # skips the WHOLE chain (Download click, bypass, DB bundle +
            # DB2 mirror). Marked non-post so the watermark advances.
            _dup = await dedup.already_indexed(target, msg.message or "")
            if _dup:
                logging.info("DUP SKIP: msg %s already in DB2 (100%% exact match) -- skipped", msg.id)
                await dedup.record_skip(target, msg.id, 100.0, _dup[0], _dup[2] if len(_dup) > 2 else None)
                # v46.2: a dup-SKIP is 'handled' too — bump last_post so the
                try: await DB.set_last_post(target, msg.id)
                except Exception: pass
                collected.append((msg.id, False, None))
            else:
                log.info("POST FOUND: msg %s — queued for a parallel worker", msg.id)
                collected.append((msg.id, True, msg))
                pending.append(msg)

                # v50: partial / near match -> alert admin, still process.
                # Moved INSIDE the else: v49 ran this even after an exact-match
                # skip, so every skipped dupe fired a second, confusing
                # "POSSIBLE DUPLICATE" DM on top of the "DUPLICATE SKIPPED" DM.
                _p = await dedup.find_possible_dup(target, msg.message or "")
                if _p:
                    mfp, sc, mmid = _p
                    logging.info("POSSIBLE DUPE: msg %s near %s in DB2 (%.1f%%) -- processing", msg.id, mmid, sc)
                    await dedup.record_possible_duplicate(target, msg.id, sc, mfp, mmid)
        if len(collected) >= 500 or len(pending) >= 200:
            break  # bound one pass; the next pass continues from the watermark
    if not pending:
        # v39.1: SAY it when a resumed target simply has nothing new — the
        # #1 'resumed but nothing happened' mystery is a channel whose resume
        # point is already at the newest message (nothing to scrape)
        # v46.1: even with nothing to scrape, persist the watermark so the
        # /targets board shows the LIVE cursor (dup-skips advance the saved
        # position instead of leaving it stale).
        if collected:
            _new_wm = max(m for m, _p, _m2 in collected)
            if _new_wm > last_id:
                try:
                    await DB.set_progress(target, _new_wm)
                    log.info("parallel: target %s watermark advanced %s -> %s "
                             "(no new posts — skips persisted)", target, last_id, _new_wm)
                except Exception as e:
                    log.warning("parallel: could not persist watermark for %s (%s)",
                                target, e)
        else:
            log.info("parallel: target %s caught up — scanned 0 msg(s) from id %s, no new posts",
                     target, last_id)
        return
    log.info("parallel: %d pending post(s) on target %s across %d account(s)",
             len(pending), target, sm.count())
    wm = last_id
    resolved = set()
    i = 0
    while i < len(pending):
        if state.abort:
            state.abort = False
            break
        if state.reset_gen != pass_gen or target in state.paused_ids:
            break
        while state.paused and not state.abort:
            await asyncio.sleep(2)  # pause = finish in-flight, dispatch nothing new
        if state.abort:
            state.abort = False
            break
        now = time.time()
        free = [idx for idx in range(sm.count())
                if _FLOOD_COOLDOWNS.get(idx, 0) <= now]
        if not free:
            wait = max(5, min(_FLOOD_COOLDOWNS.values()) - now)
            log.warning("parallel: every account flood-parked — sleeping %ds", wait)
            await asyncio.sleep(wait)
            continue
        batch = pending[i:i + len(free)]
        # v39.1: log every wave so the Render log shows parallel activity
        log.info("parallel wave: posts %s -> accounts %s",
                 [m.id for m in batch], [f"acc{idx + 1}" for idx in free[:len(batch)]])
        tasks = []
        for k, msg in enumerate(batch):
            idx = free[k]
            tasks.append(asyncio.ensure_future(_parallel_one(
                sm.all()[idx], cfg, msg, idx, f"acc{idx + 1}/{sm.count()}", resolved)))
        await asyncio.gather(*tasks, return_exceptions=True)
        if state.abort:
            state.abort = False  # /skip aborted the whole wave — already recorded
        i += len(batch)
        # watermark: advance over every contiguously-resolved message
        for mid, is_p, _ in collected:
            if mid <= wm:
                continue
            if is_p and mid not in resolved:
                break
            wm = mid
        await DB.set_progress(target, wm)


async def health(_):
    return web.json_response({"ok": True, "stage": state.stage,
                              "running": state.running, "post": state.current_post})


async def start_health_server(port):
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("health server on :%s", port)


async def scrape_loop(sm):
    """Scan target channel oldest->newest, processing real posts.
    Progress is persisted to MongoDB after EVERY message — a Render crash or
    redeploy resumes from the exact same post. Rotates accounts every
    POSTS_PER_ACCOUNT posts (and immediately on FloodWait): the next account
    continues from the same Mongo progress, so the handoff is seamless."""
    posts_on_account = 0
    while True:
        client = sm.current()
        await asyncio.sleep(3)
        if not state.started or state.paused:
            state.running = False
            continue
        cfg = await DB.get_config()
        targets = await DB.get_targets()
        # per-target pause flags live in Mongo (survive restarts) — mirror them
        # into memory every pass so /pause <n> /resume <n> work exactly like
        # /goto's reset_gen pattern
        state.paused_ids = {t["id"] for t in targets if t.get("paused")}
        missing = []
        if not cfg.get("bypass_id"):
            missing.append("bypass_id")
        if not targets:
            missing.append("target (use /target)")
        elif not cfg.get("db_id") and not all(t.get("db_id") for t in targets):
            missing.append("db (set per-target with /setdb or a fallback with /adddb)")
        if missing:
            state.running = False
            if state.stage != "waiting config":
                log.warning("started but config missing: %s — set via control bot", missing)
                state.stage = "waiting config"
            continue
        if not state.running:
            log.info("scraper ACTIVE — targets=%s bypass=%s", targets, cfg["bypass_id"])
        state.running = True
        # rotate through ACTIVE targets only: individually paused channels are
        # skipped entirely, the rest keep scraping
        active = [t for t in targets if not t.get("paused")]
        if not active:
            state.running = False
            if state.stage != "all targets paused":
                log.info("every target is individually paused — waiting for /resume <n>")
                state.stage = "all targets paused"
            await asyncio.sleep(10)
            continue
        # rotate through targets: pick the one with the oldest progress
        tprog = [(t, await DB.get_progress(t["id"])) for t in active]
        tsel, last_id = min(tprog, key=lambda x: x[1])
        target = tsel["id"]
        cfg = dict(cfg)
        cfg["target_id"] = target
        cfg["db_id"] = tsel.get("db_id") or cfg.get("db_id")  # per-target DB wins
        # v42: per-target download-link discovery — 'button' (default) or
        # 'caption' (hidden hyperlink behind cfg["link_trigger"] text)
        cfg["link_mode"] = tsel.get("link_mode") or "button"
        cfg["link_trigger"] = tsel.get("link_trigger")
        cfg["db2_id"] = tsel.get("db2_id")  # v45.1: for dedup sibling sync
        pass_gen = state.reset_gen
        if len(targets) > 1:
            log.info("multi-target: %d channels, working on %s -> DB %s", len(targets), target, cfg["db_id"])
        if getattr(state, "_last_scan", None) != (target, last_id):
            log.info("scanning target %s from message id %s (oldest -> newest)", target, last_id)
            state._last_scan = (target, last_id)
        try:
            if sm.count() > 1:
                # v39: parallel dispatcher — all accounts share this target's
                # pending posts. The sequential loop below is untouched and
                # still runs exactly as before for a single account.
                await _parallel_pass(sm, target, cfg, last_id, pass_gen)
                if state.stage != "watching for new posts":
                    log.info("scan pass complete (caught up to latest message); watching for new posts")
                    state.stage = "watching for new posts"
                await asyncio.sleep(30)  # poll for new posts
                continue
            async for msg in client.iter_messages(target, min_id=last_id, reverse=True):
                while state.paused and not state.abort:
                    await asyncio.sleep(2)
                if state.abort:
                    state.abort = False
                    break
                if state.reset_gen != pass_gen:
                    # /reset, /goto, or /pause|/resume <n> ran mid-pass — drop
                    # this pass NOW so the next pass re-reads fresh state
                    log.info("state changed mid-pass (reset/goto/pause) — restarting pass")
                    break
                if target in state.paused_ids:
                    # /pause <n> hit the channel currently being scraped —
                    # abandon this pass and move to a still-active target
                    log.info("target %s paused via /pause <n> — moving to next target", target)
                    break
                if not is_post(msg, cfg.get("link_mode", "button"),  # v42
                               cfg.get("link_trigger")):
                    log.info("skip msg %s (%s)", msg.id,
                             why_not_post(msg, cfg.get("link_mode", "button"),
                                          cfg.get("link_trigger")))
                    await DB.set_progress(target, msg.id)
                    continue
                # v45: DB2 duplicate gate (sequential path) — same as the
                # parallel gate above; progress advances like a normal skip
                _dup = await dedup.already_indexed(target, msg.message or "")
                if _dup:
                    logging.info("DUP SKIP: msg %s already in DB2 (100%% exact match) -- skipped", msg.id)
                    await dedup.record_skip(target, msg.id, 100.0, _dup[0], _dup[2] if len(_dup) > 2 else None)
                    # v46.2: keep the board title link live on skips too
                    try: await DB.set_last_post(target, msg.id)
                    except Exception: pass
                    await DB.set_progress(target, msg.id)
                    continue

                # v49: partial / near match -> alert admin, still process.
                _p = await dedup.find_possible_dup(target, msg.message or "")
                if _p:
                    mfp, sc, mmid = _p
                    logging.info("POSSIBLE DUPE: msg %s near %s in DB2 (%.1f%%) -- processing", msg.id, mmid, sc)
                    await dedup.record_possible_duplicate(target, msg.id, sc, mfp, mmid)

                log.info("POST FOUND: msg %s — starting download flow", msg.id)
                state.current_post = msg.id
                try:
                    await process_post(client, cfg, msg)
                    # v33: revert to saving msg.id. The v30 id-1 trick fixed the
                    # pause-skip, but because the completed post is fully processed
                    # at this point, resuming re-scrapes it -> DUPLICATE (reported:
                    # pause at 149, resume -> 149 scraped again). The original pause
                    # skip came from the in-flight race: /pause waited for the
                    # current post, yet the OLD progress was read before it
                    # finished. The scan now re-reads progress on every pass (it
                    # already restarts on reset_gen bump when /pause runs), so the
                    # correct id is used. iter_messages(min_id=id) is exclusive, so
                    # the completed post is not reprocessed and the next post is
                    # not skipped.
                    await dedup.note_scraped(target, msg.message or "", msg.id,
                                             db2_id=tsel.get("db2_id"))  # v45.1 sibling sync
                    await DB.set_progress(target, msg.id)
                    posts_on_account += 1
                    log.info("post %s done (%s, %d/%d on this account)", msg.id,
                             sm.current_name(), posts_on_account, POSTS_PER_ACCOUNT)
                    if sm.count() > 1 and posts_on_account >= POSTS_PER_ACCOUNT:
                        old = sm.current_name()
                        sm.rotate()
                        posts_on_account = 0
                        client = sm.current()
                        log.info("ROTATE: %s rested -> now scraping with %s (continues from msg %s)",
                                 old, sm.current_name(), msg.id)
                except Abort:
                    await DB.add_failure(msg.id, state.stage, "skipped by user")
                    await DB.set_progress(target, msg.id)
                    state.abort = False
                except FloodWaitError as e:
                    # Rate limit on this account: rotate to the next (rested)
                    # account immediately — it continues from the same progress.
                    if sm.count() > 1:
                        old = sm.current_name()
                        sm.rotate()
                        posts_on_account = 0
                        log.warning("FloodWait %ds on %s — ROTATING to %s (post %s will be retried there)",
                                    e.seconds, old, sm.current_name(), msg.id)
                        break  # abandon this pass (its generator belongs to the
                               # flooded account); next pass uses the new account
                               # and resumes from saved progress -> same post retried
                    # single account: sleep it out / park if huge
                    from config import FLOOD_MAX_WAIT, FLOOD_PARK
                    if e.seconds > FLOOD_MAX_WAIT:
                        state.paused = True
                        log.warning("FloodWait %ds on post %s exceeds cap %ds — PAUSING scraper for %ds "
                                    "(it auto-resumes after; progress already saved)", e.seconds, msg.id,
                                    FLOOD_MAX_WAIT, FLOOD_PARK)
                        await asyncio.sleep(FLOOD_PARK)
                        state.paused = False
                    else:
                        log.warning("scrape FloodWait %ds on post %s — sleeping", e.seconds, msg.id)
                        await asyncio.sleep(e.seconds + 5)
                    continue
                except Exception as e:
                    log.exception("post %s failed", msg.id)
                    await DB.add_failure(msg.id, state.stage, e)
                    await DB.set_progress(target, msg.id)
                state.current_post = None
                state.stage = "idle"
                from config import POST_DELAY
                await asyncio.sleep(POST_DELAY)  # pacing: one post at a time, ban-safe
            if state.stage != "watching for new posts":
                log.info("scan pass complete (caught up to latest message); watching for new posts")
                state.stage = "watching for new posts"
            await asyncio.sleep(30)  # poll for new posts
        except FloodWaitError as e:
            log.warning("scrape loop FloodWait %ds — sleeping in-process", e.seconds)
            await asyncio.sleep(e.seconds + 5)
        except Exception:
            log.exception("scrape loop error")
            await asyncio.sleep(15)


async def main():
    from config import PORT, BOT_TOKEN
    sm = SessionManager()
    client = await guarded(lambda: sm.start(), "userbot login")
    me = await client.get_me()
    state.scrape_client = client  # v39: userbot fallback for admin alert DMs
    log.info("logged in as %s (%s) — %d env account(s) loaded (%s)",
             me.first_name, me.id, sm.count(),
             "PARALLEL multi-userbot scraping" if sm.count() > 1
             else "single account")
    # v52: re-attach sessions previously added via /addworker (Mongo-backed).
    # A stored session that fails to log in is SKIPPED loudly — it never
    # blocks startup or the env sessions.
    try:
        extra = await DB.get_extra_sessions()
    except Exception as e:
        log.warning("v52: could not read extra sessions from Mongo: %s", e)
        extra = []
    for x in extra:
        try:
            xc = await sm.add_session(x["session"], source="bot")
            xme = await xc.get_me()
            log.info("v52: re-attached bot-added worker @%s (%s)",
                     getattr(xme, "username", "?"), xme.id)
        except Exception as e:
            log.warning("v52: a bot-added session failed to log in and was "
                        "SKIPPED: %s", e)
    # NOTE: userbot command handlers are DISABLED (bot-only replies).
    # commands.register(client)  <- uncomment to re-enable Saved-Messages commands
    # v34: /checkdm pipeline — every userbot session watches @richmining's DM
    # for invite links (join -> wait for admin -> add @lifesimplerbot -> leave
    # -> reply DONE). Gated by the checkdm_enabled flag in MongoDB.
    import checkdm
    for _c in sm.all():
        checkdm.register(_c)
    log.info("checkdm watcher registered on %d session(s)", sm.count())
    await start_health_server(PORT)
    tasks = [
        asyncio.ensure_future(scrape_loop(sm)),
    ]
    tasks += [asyncio.ensure_future(c.run_until_disconnected()) for c in sm.all()]
    if BOT_TOKEN:
        ctl = await guarded(lambda: botapi.start(client, sm), "control bot login")  # v40: sm for /stats /invite /leave
        me_b = await ctl.get_me()
        log.info("control bot @%s online (command menu registered)", me_b.username)
        tasks.append(asyncio.ensure_future(ctl.run_until_disconnected()))
    else:
        log.info("BOT_TOKEN not set — control bot/menu disabled, userbot commands still work")
    try:
        await asyncio.gather(*tasks)
    finally:
        # clean shutdown: cancel + await every task so nothing is 'never awaited'
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def run():
    loop = asyncio.new_event_loop()          # Py3.14-compatible loop creation
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    except FloodWaitError as e:
        # last-resort guard: sleep out the timer, then exit 0 — Render's restart
        # happens AFTER the flood window, so no restart loop
        secs = e.seconds + 5
        log.warning("FloodWaitError escaped main — sleeping %ds then clean exit", secs)
        time.sleep(secs)
        sys.exit(0)
    except Exception:
        log.exception("fatal error")
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    run()

# ---- v49: admin DM sender (real Telethon) + ADMIN_USER_ID wiring -------
async def _v49_send_admin_dm(uid, text):
    global bot
    if bot is None: return
    try:
        await bot.send_message(int(uid), text, parse_mode='md')
    except Exception as _e:
        try:
            from telethon.errors import FloodWaitError as _fw
            if isinstance(_e, _fw):
                await asyncio.sleep(min(getattr(_e,'seconds',60), 60)+5)
                await bot.send_message(int(uid), text, parse_mode='md')
        except Exception: pass

dedup._send_admin_dm = _v49_send_admin_dm
dedup.set_admin_ids([int(x) for x in str(__import__('os').environ.get('ADMIN_USER_ID','0')).split(',') if x.strip()])
