"""dedup.py — v46: DB2-cover fingerprint index + duplicate-skip gate.

Fingerprints ONLY DB2 cover posts (m.photo). Match = exact OR rapidfuzz
token_set_ratio >= 90. One scan per DB2 ever; siblings sharing a DB2 share
the index; every scraped cover auto-grows all siblings.

v46 additions (all backwards-compatible):
  * Mongo->RAM refresh: the running process re-pulls the fingerprints every
    REFRESH_TTL seconds and MERGES them, so a fingerprint pushed by the laptop
    script (or another process) is honoured live — no Render restart needed.
    This is the fix for "sometimes it skips, sometimes it doesn't".
  * tail_scan_db2(): an INCREMENTAL, cursor-based DB2 scan that indexes only
    covers newer than the stored db2_cover_index.last_indexed_msg_id — so new
    DB2 posts (mirrored by the bot OR posted by hand) get gated automatically.
  * Instant admin alert on EVERY skip (flood-wait-guarded) + a per-batch
    summary. Remember() now retries the Mongo push and warns loudly on failure.
"""
import asyncio, logging, re, time, unicodedata
import db as DB

log = logging.getLogger("dedup")

try:
    from rapidfuzz.fuzz import token_set_ratio as _ratio
except Exception:                       # pragma: no cover
    import difflib
    def _ratio(a, b): return difflib.SequenceMatcher(None, a, b).ratio() * 100

try:                                    # telethon may be absent in unit tests
    from telethon.errors import FloodWaitError as _FloodWaitError
except Exception:                       # pragma: no cover
    class _FloodWaitError(Exception):
        pass

DUP_THRESHOLD = 90.0
DUP_BATCH = 10
FP_CAP = 20000
FP_MAX_KEEP = 60000
FP_MIN_TOKENS = 4   # v47: fingerprints shorter than this are unmatchable
                    # (never gated, never indexed) — kills junk-fp collisions

REFRESH_TTL = 300.0        # v46: re-pull Mongo fingerprints into RAM every 5 min
INSTANT_SKIP_ALERT = True  # v46: DM the admin the moment a dupe is skipped

_FPS, _INDEXED, _BUILDING, _LOCKS = {}, set(), {}, {}
_PENDING_SKIPS, _COUNTS, _DB2_OWNERS = {}, {}, {}
_LAST_REFRESH = {}         # v46: target_id -> last Mongo->RAM refresh ts
_DM_LOCK = asyncio.Lock()  # v46: serialize admin DMs (avoid flood bursts)
_DB2_BY_TARGET = {}        # v46.1: target_id -> db2_id cache (1h) for skip links
_FPS_MID = {}              # v46.1: target_id -> {fp: db2_msg_id} for match links


def _chan_link(cid, mid):
    # Telegram private-channel link: t.me/c/<digits-without -100>/<msg_id>
    try:
        d = str(cid)
        d = d[4:] if d.startswith("-100") else d.lstrip("-")
        return f"https://t.me/c/{d}/{mid}"
    except Exception:
        return f"https://t.me/c/0/{mid}"


def _fp_midmap(doc):
    m = {}
    for e in (doc or {}).get("fingerprints") or []:
        if isinstance(e, dict):
            m[e.get("fp")] = e.get("first_seen_msg") or 0
    return m


async def _db2_for(t):
    # v46.1: resolve a target's DB2 id (target doc first, then cover_fp).
    e = _DB2_BY_TARGET.get(t)
    if e and time.time() - e[1] < 3600: return e[0]
    db2 = None
    try:
        for c in await DB.get_targets():
            if c.get("id") == t and c.get("db2_id"):
                db2 = c["db2_id"]; break
        if db2 is None:
            db2 = ((await get_cover_fp(t)) or {}).get("db2_id")
    except Exception:
        pass
    _DB2_BY_TARGET[t] = (db2, time.time())
    return db2


def _lock(t):
    if t not in _LOCKS: _LOCKS[t] = asyncio.Lock()
    return _LOCKS[t]


def _norm(s): return unicodedata.normalize("NFKD", s or "").lower()


_NONSTORY_RE = re.compile(
    r"(?:^|\b)(?:episode|subtitles?|censor(?:ship|ed)?|rating|network|audio|"
    r"quality|resolution|size|duration|genre|genres|studio|release|language|"
    r"status|score|source|seed|leech)[\s.:\u2026]*[:\-]", re.I)


def fingerprint_cover(caption):
    if not caption: return ""
    head = caption
    if "\u27aa" in head:
        head = head.split("\u27aa", 1)[0]
    else:
        kept = []
        for ln in head.splitlines():
            if _NONSTORY_RE.search(ln): break
            kept.append(ln)
        head = "\n".join(kept)
    s = _norm(head)
    s = re.sub("[\u200b-\u200f\ufeff]", "", s)
    s = re.sub(r"(?:https?://)?\S*(?:t\.me|telegram\.me)/\S+|https?://\S+", " ", s)
    s = re.sub(r"@[A-Za-z0-9_]+", " ", s)
    toks = re.findall(r"\w+", s)
    # v47: KEEP pure-digit tokens — dropping them made "episode 1" and
    # "episode 2" collapse into the SAME fingerprint (the false skip reported
    # on HANIME HINDI NETWORK). A fingerprint with fewer than FP_MIN_TOKENS
    # tokens is unmatchable: find_dup ignores it and it is never indexed.
    out = [t for t in toks if len(t) >= 3 or (t.isdigit() and len(t) <= 4)]
    return " ".join(out) if len(out) >= FP_MIN_TOKENS else ""



async def get_cover_fp(t):
    return await DB.db().cover_fp.find_one({"_id": str(t)})


async def init_cover_fp(t, db2, fps, scanned, secs):
    await DB.db().cover_fp.update_one({"_id": str(t)}, {"$set": {
        "target_id": t, "db2_id": db2, "scanned_at": time.time(),
        "scanned_count": scanned, "index_build_time": secs,
        "fingerprints": [{"fp": fp, "first_seen_msg": mid, "hits": [mid],
                          "added_at": time.time()} for fp, mid in fps]}}, upsert=True)


async def add_cover_fp(t, fp, mid):
    # v47: the old upsert-with-$ne raced into E11000 duplicate-key errors and
    # the fingerprint silently lived in RAM only. Now: plain update first; the
    # doc is INSERTED only when it genuinely does not exist.
    if not fp: return False
    entry = {"fp": fp, "first_seen_msg": mid, "hits": [mid],
             "added_at": time.time()}
    res = await DB.db().cover_fp.update_one(
        {"_id": str(t), "fingerprints.fp": {"$ne": fp}},
        {"$push": {"fingerprints": {"$each": [entry],
                                    "$position": 0, "$slice": -FP_CAP}}})
    if getattr(res, "matched_count", 0):
        return bool(res.modified_count)
    # matched 0 -> fp already stored (no-op) OR the doc doesn't exist yet
    try:
        await DB.db().cover_fp.update_one(
            {"_id": str(t)},
            {"$setOnInsert": {"target_id": t, "fingerprints": [entry]}},
            upsert=True)
    except Exception:
        pass  # a concurrent writer inserted first — the fp is stored either way
    return False


async def get_db2_index(db2):
    return await DB.db().db2_cover_index.find_one({"_id": str(db2)})


async def set_db2_index(db2, last_mid):
    await DB.db().db2_cover_index.update_one({"_id": str(db2)},
        {"$set": {"last_indexed_msg_id": last_mid, "last_scan_ts": time.time()}},
        upsert=True)


# ---- v45.1 sibling sync (targets sharing one DB2) ----
async def db2_owners(db2):
    """Every target id whose DB2 = db2 (60s cache)."""
    e = _DB2_OWNERS.get(db2)
    if e and time.time() - e[1] < 60: return list(e[0])
    owners = [t["id"] for t in await DB.get_targets() if t.get("db2_id") == db2]
    _DB2_OWNERS[db2] = (owners, time.time())
    return owners


async def share_db2(target_id, db2_id):
    """Copy a sibling's existing index into this target (RAM + Mongo).
    Returns the sibling id when shared, else None."""
    for sib in await db2_owners(db2_id):
        if sib == target_id: continue
        fps = None
        if sib in _FPS and _FPS[sib]:
            fps = list(_FPS[sib])
        elif sib in _INDEXED:
            doc = await get_cover_fp(sib)
            fps = [e["fp"] for e in (doc or {}).get("fingerprints") or []]
        if fps:
            _FPS[target_id] = fps[:FP_MAX_KEEP]
            _INDEXED.add(target_id)
            _LAST_REFRESH[target_id] = time.time()
            await DB.db().cover_fp.update_one({"_id": str(target_id)},
                {"$set": {"target_id": target_id, "db2_id": db2_id,
                          "scanned_at": time.time(), "shared_from": sib,
                          "fingerprints": [{"fp": fp, "first_seen_msg": 0,
                                            "hits": [], "added_at": time.time()}
                                           for fp in fps]}}, upsert=True)
            return sib
    return None


# ---- matching ----
async def _load_from_mongo(t):
    doc = await get_cover_fp(t)
    fps = [e["fp"] for e in (doc or {}).get("fingerprints") or []]
    _FPS[t] = fps[:FP_MAX_KEEP]
    _FPS_MID[t] = _fp_midmap(doc)
    return bool(fps)


async def _refresh_from_mongo(t):
    """v46: MERGE any fingerprints added to Mongo since the last load into RAM.
    Never drops what we already hold — only unions new ones to the front."""
    doc = await get_cover_fp(t)
    disk = [e["fp"] for e in (doc or {}).get("fingerprints") or []]
    if not disk: return False
    lst = _FPS.setdefault(t, [])
    have = set(lst)
    new = [fp for fp in disk if fp not in have]
    if new:
        lst[:0] = new
        del lst[FP_MAX_KEEP:]
    mm = _FPS_MID.setdefault(t, {})
    for fp, mmid in _fp_midmap(doc).items():
        mm.setdefault(fp, mmid)
    return bool(lst)


async def ensure_index(t):
    """Load the index from Mongo on first use, then keep it fresh: past
    REFRESH_TTL seconds we re-merge Mongo so fingerprints pushed by the laptop
    script / another process are honoured without a restart."""
    if t in _FPS:
        if time.time() - _LAST_REFRESH.get(t, 0) < REFRESH_TTL:
            return bool(_FPS[t])
        async with _lock(t):
            try:
                await _refresh_from_mongo(t)
            except Exception as e:
                log.warning("dedup: refresh from Mongo failed for %s (%s)", t, e)
        _LAST_REFRESH[t] = time.time()
        return bool(_FPS.get(t))
    async with _lock(t):
        if t in _FPS:
            ok = bool(_FPS[t])
        else:
            try:
                ok = await _load_from_mongo(t)
            except Exception as e:
                log.warning("dedup: initial load failed for %s (%s)", t, e)
                ok = False
        _LAST_REFRESH[t] = time.time()
        return ok


def _numset(fp):
    # standalone digit tokens only ("episode 2" -> {2}; "720p" is NOT a number)
    return frozenset(t for t in (fp or "").split() if t.isdigit())


async def find_dup(t, sig):
    # Returns (match_fp, score, match_db2_mid) or None. match_db2_mid is the
    # DB2 message id of the matched cover (from first_seen_msg) used for the
    # tappable 'DB2 SAME POST' link. Old indexes store fp as strings -> None.
    if not sig or not await ensure_index(t): return None
    fps = _FPS.get(t) or []
    mids = _FPS_MID.get(t) or {}
    if sig in fps: return sig, 100.0, mids.get(sig)
    sig_nums = _numset(sig)          # v47: episode/season numbers must agree
    best, best_fp = 0.0, None
    for fp in fps:
        if _numset(fp) != sig_nums:  # "episode 1" can never match "episode 2"
            continue
        r = _ratio(sig, fp)
        if r > best: best, best_fp = r, fp
    return (best_fp, round(best, 1), mids.get(best_fp)) if best >= DUP_THRESHOLD else None


async def remember(t, fp, mid, db2_id=None):
    """Grow this target AND every sibling on the same DB2 (v45.1). v46: the
    Mongo push is retried and logs loudly on failure (a lost push used to mean
    an un-gated repost after the next restart)."""
    if not fp: return
    owners = [t]
    if db2_id is None:
        doc = await get_cover_fp(t)
        db2_id = (doc or {}).get("db2_id")
    if db2_id:
        owners = await db2_owners(db2_id) or [t]
        if t not in owners: owners.append(t)
    for tid in owners:
        async with _lock(tid):
            lst = _FPS.setdefault(tid, [])
            if fp not in lst:
                lst.insert(0, fp); del lst[FP_MAX_KEEP:]
            _FPS_MID.setdefault(tid, {})[fp] = mid
        for attempt in range(2):
            try:
                await add_cover_fp(tid, fp, mid)
                break
            except Exception as e:
                if attempt:
                    log.warning("dedup: Mongo push FAILED for %s (%s) — "
                                "fingerprint kept in RAM only", tid, e)
                else:
                    await asyncio.sleep(0.5)


async def already_indexed(target_id, caption):
    try: return await find_dup(target_id, fingerprint_cover(caption))
    except Exception as e:
        log.warning("dedup: gate error on target %s (%s) — scraping anyway", target_id, e)
        return None


async def note_scraped(target_id, caption, mid, db2_id=None):
    try: await remember(target_id, fingerprint_cover(caption), mid, db2_id=db2_id)
    except Exception as e: log.warning("dedup: remember failed (%s)", e)


# ---- v46: incremental DB2 tail-scan (index brand-new DB2 covers) ----
async def tail_scan_db2(client, db2_id, progress_cb=None):
    """Index ONLY covers newer than the stored cursor (idempotent).
    Returns {status, added, scanned, cursor}. Safe to call repeatedly — a
    cover already indexed is never added twice ($ne guard + RAM set check)."""
    owners = await db2_owners(db2_id)
    if not owners:
        return {"status": "no_owners", "added": 0, "scanned": 0, "cursor": None}
    idx = await get_db2_index(db2_id)
    cursor = (idx or {}).get("last_indexed_msg_id") or 0
    try:
        await ensure_index(owners[0])
    except Exception:
        pass
    new_fps, max_seen, count = [], cursor, 0
    seen_sigs = set()
    async for m in client.iter_messages(db2_id, min_id=cursor, reverse=True):
        count += 1
        if m.id > max_seen: max_seen = m.id
        if not getattr(m, "photo", None): continue
        sig = fingerprint_cover(m.message or "")
        if not sig or sig in seen_sigs: continue
        seen_sigs.add(sig)
        new_fps.append((sig, m.id))
        if progress_cb and count % 100 == 0:
            try: await progress_cb(count, len(new_fps))
            except Exception: pass
    added = 0
    for sig, mid in new_fps:
        if sig in (_FPS.get(owners[0]) or []):
            continue                      # already known — no double index
        try:
            await remember(owners[0], sig, mid, db2_id=db2_id)
            added += 1
        except Exception as e:
            log.warning("dedup: tail-scan remember failed (%s)", e)
    if max_seen > cursor:
        await set_db2_index(db2_id, max_seen)
    log.info("dedup: tail-scan DB2 %s — scanned %d, +%d new fp (cursor %s)",
             db2_id, count, added, max_seen)
    return {"status": "done", "added": added, "scanned": count,
            "cursor": max_seen, "owners": owners}


# ---- batched skip notifications ----
def _count(t): return _COUNTS.get(t, 0)


def status_lines():
    if not _COUNTS: return ""
    out = ["🧬 Duplicates skipped (this session):"]
    for tid, n in sorted(_COUNTS.items()): out.append(f"  • {tid}: {n}")
    return "\n" + "\n".join(out)


def format_batch(target_id, entries, reason, db2_id=None):
    lines = [f"📋 DUPLICATES SKIPPED — target {target_id} ({reason})"]
    for s in entries:
        lines.append(f"• post {s['msg_id']} — {s['score']}% match in DB2")
        if db2_id and s.get("match_mid"):
            lines.append(f"  db2: {_chan_link(db2_id, s['match_mid'])}")
    fp = entries[-1].get("match_fp") if entries else ""
    if fp: lines.append(f"last match: {fp}…")
    lines.append(f"total dupes this session: {_count(target_id)}")
    return "\n".join(lines)


async def _dm_admins(text):
    from flow import state
    from config import ADMIN_USER_ID
    import botapi
    ctl = getattr(botapi, "bot", None)
    async def _one(uid):
        if ctl is not None:
            try: await ctl.send_message(uid, text, parse_mode="md"); return True
            except Exception: pass
        try:
            c = getattr(state, "scrape_client", None)
            if c is not None:
                await c.send_message(uid, text, parse_mode="md"); return True
        except Exception: pass
        return False
    sent = False
    if ADMIN_USER_ID: sent = await _one(ADMIN_USER_ID) or sent
    for a in await DB.get_admins():
        if a != ADMIN_USER_ID: sent = await _one(a) or sent
    if not sent: log.warning("dedup: skip batch could not be delivered: %s", text[:120])


async def _safe_dm(text):
    """v46: flood-wait-guarded, serialized admin DM. Never raises."""
    for attempt in range(2):
        try:
            async with _DM_LOCK:
                await _dm_admins(text)
            return True
        except _FloodWaitError as fe:
            await asyncio.sleep(min(getattr(fe, "seconds", 30), 300) + 1)
        except Exception as e:
            log.warning("dedup: admin DM failed (%s)", e)
            return False
    return False


async def _instant_skip_alert(target_id, msg_id, score, match_fp, match_mid):
    # v46.1: instant DM with tappable target-post AND DB2-match links.
    if not INSTANT_SKIP_ALERT: return
    db2 = await _db2_for(target_id)
    tgt = _chan_link(target_id, msg_id)
    db2l = _chan_link(db2, match_mid) if (db2 and match_mid) else "(not recorded — old index)"
    txt = (f"🔁 *DUPLICATE SKIPPED*\n"
           f"• target: `{target_id}`\n"
           f"• post: {tgt}\n"
           f"• match: {score}% in DB2\n"
           f"• DB2 SAME POST: {db2l}\n"
           f"• fp: `{(match_fp or '')[:80]}`")
    await _safe_dm(txt)


async def notify_tail_added(db2_id, added, owners=None):
    """v46: tell the admin the DB2 auto-index grew (new covers indexed)."""
    txt = (f"🧬 *DB2 auto-index updated*\n"
           f"• DB2: `{db2_id}`\n"
           f"• new cover fingerprint(s) stored: {added}\n"
           f"• future reposts of these covers will be auto-skipped.")
    await _safe_dm(txt)


async def flush_skips(target_id, reason="batch of %d" % DUP_BATCH, db2_id=None):
    q = _PENDING_SKIPS.get(target_id) or []
    if not q: return                       # empty batch -> no DM
    _PENDING_SKIPS[target_id] = []
    try: await _safe_dm(format_batch(target_id, q, reason, db2_id=db2_id))
    except Exception as e: log.warning("dedup: flush_skips DM failed (%s)", e)


def consume_skips(target_id):
    """Take pending skips WITHOUT sending — used by /pause and /resume replies."""
    return _PENDING_SKIPS.pop(target_id, [])


async def _record_skip(target_id, msg_id, score, match_fp, match_mid):
    entry = {"msg_id": msg_id, "score": score,
             "match_fp": (match_fp or "")[:120], "match_mid": match_mid,
             "ts": time.time()}
    _PENDING_SKIPS.setdefault(target_id, []).append(entry)
    _COUNTS[target_id] = _COUNTS.get(target_id, 0) + 1
    if len(_PENDING_SKIPS[target_id]) >= DUP_BATCH:
        db2 = await _db2_for(target_id)
        await flush_skips(target_id, db2_id=db2)


async def record_skip(target_id, msg_id, score, match_fp, match_mid=None):
    try:
        await _instant_skip_alert(target_id, msg_id, score, match_fp, match_mid)
        await _record_skip(target_id, msg_id, score, match_fp, match_mid)
        await DB.incr("dupes_skipped")
        if match_mid:  # v46.1: persist the dup-pair link like the caption fp
            db2 = await _db2_for(target_id)
            await DB.record_dup_pair(target_id, db2, msg_id,
                                     match_mid, score, match_fp)
    except Exception as e: log.warning("dedup: record_skip failed (%s)", e)


def is_indexed(t): return t in _INDEXED
def invalidate(t):
    _INDEXED.discard(t); _FPS.pop(t, None); _LAST_REFRESH.pop(t, None)
    _FPS_MID.pop(t, None); _DB2_BY_TARGET.pop(t, None)


async def scan_db2(client, target_id, db2_id, progress_cb=None):
    """FULL scan; also seeds every sibling target sharing this DB2."""
    if target_id in _INDEXED and _FPS.get(target_id):
        return {"status": "cached", "count": len(_FPS[target_id])}
    tsk = _BUILDING.get(target_id)
    if tsk and not tsk.done(): return {"status": "already_running"}
    siblings = [s for s in await db2_owners(db2_id) if s != target_id]

    async def _run():
        t0 = time.time()
        fps, count, covers, max_seen = [], 0, 0, 0
        async for m in client.iter_messages(db2_id):
            count += 1
            if m.id > max_seen: max_seen = m.id
            if count % 100 == 0:
                await asyncio.sleep(0.1)
                if progress_cb:
                    try: await progress_cb(count, covers)
                    except Exception: pass
            if not m.photo: continue
            sig = fingerprint_cover(m.message or "")
            if not sig: continue
            covers += 1
            fps.append((sig, m.id))
        await init_cover_fp(target_id, db2_id, fps, count, round(time.time() - t0, 1))
        await set_db2_index(db2_id, max_seen or (fps[0][1] if fps else 0))
        async with _lock(target_id):
            _FPS[target_id] = [fp for fp, _ in fps][:FP_MAX_KEEP]
        _FPS_MID[target_id] = {fp: mid for fp, mid in fps}
        _INDEXED.add(target_id); _LAST_REFRESH[target_id] = time.time()
        for sib in siblings:
            async with _lock(sib):
                _FPS[sib] = [fp for fp, _ in fps][:FP_MAX_KEEP]
            _FPS_MID[sib] = {fp: mid for fp, mid in fps}
            _INDEXED.add(sib); _LAST_REFRESH[sib] = time.time()
            await init_cover_fp(sib, db2_id, fps, count, round(time.time() - t0, 1))
        log.info("dedup: target %s indexed — %d fp from DB2 %s (+%d sibling(s))",
                 target_id, len(fps), db2_id, len(siblings))
        return {"status": "done", "count": len(fps), "scanned": count,
                "seconds": round(time.time() - t0, 1), "synced": siblings}

    task = asyncio.ensure_future(_run())
    _BUILDING[target_id] = task
    try: return await task
    finally: _BUILDING.pop(target_id, None)


def scan_db2_bg(client, target_id, db2_id, done_cb=None):
    async def _bg():
        try:
            res = await scan_db2(client, target_id, db2_id)
            if done_cb: await done_cb(res)
        except Exception as e:
            log.exception("dedup: background DB2 scan failed for target %s", target_id)
            try:
                await _safe_dm(f"⚠️ DB2 duplicate-index scan FAILED for target "
                               f"{target_id}: `{e}`\nScraping continues UNGATED — "
                               f"rebuild with /dupescan.")
            except Exception: pass
    asyncio.ensure_future(_bg())
