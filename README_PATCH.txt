================================================================================
v52 - LIVE WORKER SESSIONS (/addworker) + RICH-TABLE /stats (Render bot)
================================================================================
Files changed: db.py, session_manager.py, bot.py, botapi.py, richboard.py
(README.md, README_PATCH.txt updated). Deploy: replace those five files in the
repo root and redeploy Render. No data migration — bot-added sessions live in
the existing config doc (extra_sessions), created on first /addworker.

WHY: adding a scraping account meant editing Render env vars + a redeploy; and
/stats printed one giant plain-text list that errors out once targets grow.

WHAT CHANGED:
  * /addworker <session string> — validates the StringSession (logs it in),
    stores it in Mongo (config.extra_sessions — survives redeploys) and
    attaches it to the live rotation INSTANTLY, no restart. Duplicate strings
    and duplicate accounts are refused; a bad string never touches the pool.
    The checkdm DM watcher is registered on the new worker too.
  * Bare /addworker — lists EVERY worker (profile, id) tagged [env] / [bot#k],
    plus stored-vs-live counts.
  * /removeworker <bot#> — detaches a bot-added worker from the live pool and
    deletes it from Mongo. env workers are still managed on Render.
  * bot.py startup re-attaches stored bot-added sessions after the env ones; a
    stored session that fails login is SKIPPED loudly, never fatal.
  * /stats — now a compact Bot API rich table (InputRichBlockTable via
    richboard.py): one row per worker, emoji matrix (# | Worker | Src |
    Targets | DB | DB2), control-bot row first, numbered target titles in the
    footer. Falls back to a chunked plain-text grid if rich messages are
    rejected. ✅ member · 👑 admin · ❌ no access.

================================================================================
v51 - CUSTOM LINK-BUTTON TEXT PER TARGET (Render bot)
================================================================================
Files changed: scraper.py, flow.py, db.py, botapi.py. Deploy: replace all four
in the repo root and redeploy Render. No data migration — targets without a
custom text behave exactly as before (Download button).

WHY: button mode hard-assumed the post's link button says "Download". Channels
whose button says "Join" / "Watch Now" were never detected as posts at all.

WHAT CHANGED:
  * /target wizard: after you pick Button mode, the bot now asks for the button
    TEXT (e.g. Download / Join / Watch Now). Send `skip` to keep the default
    Download. Unicode-styled labels are folded, plain text works.
  * /targatelinkmode <n> button <text> — set/change it any time without
    re-adding the target. Caption mode unchanged.
  * The custom text is stored in the target's existing link_trigger field
    (db.py), used by post detection (scraper.is_post / why_not_post) AND by the
    actual click + link-bot peek (flow._follow_button / _peek_link_bot), so the
    whole chain follows the button the channel really uses.

================================================================================
v50 - DUPLICATE-SKIP LINK FIX + ALERT CLEANUP (Render bot)
================================================================================
Files changed: dedup.py, bot.py  (README_PATCH.txt updated). Deploy: replace
both files in the repo root and redeploy Render.
AFTER DEPLOY: run /dupescan once per DB2 channel (5 total). 342 of 477 stored
fingerprints are still the OLD v46 format, which v49's exact-match gate can
never hit — this is why real duplicates were being processed again. One
/dupescan per DB2 rewrites them in v49 format and restores the correct DB2
message ids. It only touches the fingerprint index, never your channels.

FIX 1 - skip alerts now link to the REAL DB2 post (dedup.py)
  WHY IT WAS WRONG: note_scraped() fed the TARGET channel's message id into
  the fingerprint->message-id map, and remember() blindly overwrote whatever
  DB2 id was already stored (by the DB2 mirror hook / DB2 scan). A later
  scrape of the same cover on any sibling target stamped e.g. "389" (a target
  msg id); the alert glued it onto the DB2 channel id -> t.me/c/<db2>/389,
  pointing at an UNRELATED DB2 post. Detection itself was correct (exact
  fingerprint match); only the reported link was wrong.
  * note_scraped() no longer passes a message id (target ids are not DB2 ids).
  * remember() is first-wins for the msg id and never stores a 0.
  * "(not recorded)" alerts now tell you to run /dupescan to rebuild.

FIX 2 - no more double DM on skipped dupes (bot.py, parallel path)
  find_possible_dup ran even when the exact gate had already skipped the post,
  so every "DUPLICATE SKIPPED" DM was followed by a confusing "POSSIBLE
  DUPLICATE" DM for the same post. The check now lives inside the else-branch:
  it only fires when the post was NOT an exact duplicate.

FIX 3 - possible-dup noise floor raised 60% -> 80% (dedup.py)
  Unrelated captions share generic words and were tripping the 60% floor.

================================================================================
v48 - CAPTION REWRITE + WORKING /avoid (Render bot)
================================================================================
Files changed: botapi.py  (README.md, README_PATCH.txt updated). No other file
touched. Deploy: replace botapi.py in the repo root and redeploy Render.

FIX 1 - every @username in a DB->DB2 caption becomes @NSFW_Universe
  * _clean_caption no longer DELETES handles; _swap_mentions rewrites them.
  * Catches plain "@user", markdown-wrapped **@user** / __@user__ / `@user`,
    unicode-styled handles (math-sans-bold @𝘼𝘿... "adult",
    small caps, circled letters), fullwidth "＠user" and "@user_bot".
  * Styling is folded away first (NFKD + combining-mark strip), so a styled
    handle folds to its ASCII letters before the swap.
  * A caption already containing @NSFW_Universe is left untouched (no doubling).
  * URLs (t.me / telegram.me / http) are still stripped exactly as before.

FIX 2 - /avoid and /avoidtext now actually strip text out of captions
  WHY IT FAILED: the old code did `t.replace(a, "")` - a byte-exact match. Your
  entries ("Bagairat 1 And 2 Full Movie...", "You😂 naughty 😈 And
  Something", "💗Channel -@AdultX_Horizon", "Due TO COPYRIGHT ISSUES..")
  never matched the live caption because the CAPTION differed in SPACING, LETTER
  CASE, ".." vs "...", or used a unicode-STYLED handle instead of ASCII.
  NOTE: text sent as its OWN message was already dropped; only caption text
  survived - exactly the symptom reported.

  NEW MATCHING (two passes per avoid string):
   1) whole-line pass - line key = NFKD-fold, lowercase, strip leading list
      number, drop all punctuation/emoji; an entry matches a line when the key
      equals the line key OR covers >=50% of it. So "12. text" also matches a
      bare "text" line, ANY list number matches, and "Due TO COPYRIGHT ISSUES.."
      matches "...ISSUES..." in the caption.
   2) inline pass - whitespace- and dot-flexible regex (re.I) over the line,
      using the folded entry, so embedded occurrences are removed too.
  LEFT-OVER DEBRIS: after the strip, any line with no LETTER left at all is
  removed - bare "1." / "8." list numbers, "┏━━━" rule lines
  and emoji-only residue can no longer reappear in DB2. Normal lines such as
  "1080p Sub MKV | Episode 3" are never touched.

TESTS RUN IN SANDBOX (real _clean_caption extracted from the patched file):
  20/20 checks pass - 6 mention cases (plain, styled, bold, mono, fullwidth,
  styled+underscore), 8 /avoid cases (exact, case+dots, multiline, numbered
  paste, styled-vs-ASCII, dash+renumbered, rule dust, handle-only), 6 end-to-end
  checks (realistic DB2 caption, idempotency, no over-strip, all-avoided -> empty,
  long caption preserved). All 14 repo modules compile.
================================================================================

================================================================================
v46 — DUPLICATE-DETECTION HARDENING (Render bot)
================================================================================
Files changed: dedup.py, botapi.py (README.md, README_PATCH.txt updated).

WHY: identical DB2 cover captions sometimes skipped, sometimes not. Root cause
was index FRESHNESS, not the fuzzy matcher:
  1) _FPS loaded from Mongo once per process and never refreshed — a fingerprint
     pushed by the LAPTOP script was invisible to the long-running Render bot
     until its next restart.
  2) note_scraped()'s Mongo push failed silently -> RAM-only fingerprint, lost
     on restart.
  3) note_scraped() fingerprinted the RAW target caption, not the CLEANED
     caption that actually lands in DB2 (/avoid, /replaceword, link strip).

NEW IN v46
  * Mongo->RAM refresh (REFRESH_TTL = 300s): ensure_index() re-merges Mongo every
    5 min WITHOUT dropping what RAM already holds, so laptop-script fingerprints
    are honoured live. Fixes the intermittent skip.
  * DB2 AUTO-INDEX — no more /dupescan needed:
      - db2_mirror hook: every cover the bot mirrors into DB2 is fingerprinted
        from its CLEANED caption the instant it lands (stored fp == DB2 fp).
      - tail_scan_db2(): incremental, cursor-based scan (db2_cover_index
        .last_indexed_msg_id) that indexes ONLY new DB2 covers — idempotent, so
        covers posted into DB2 BY HAND get auto-indexed within 30 min.
      - startup tail-scan + every 30 min (DEDUP_TAIL_INTERVAL, sibling-aware:
        one scan per unique DB2 per cycle).
  * ADMIN ALERTS: an instant DM on EVERY duplicate skip (flood-wait-guarded,
    serialized), PLUS a per-batch summary every 10 (empty batch = no DM, so
    /pause won't send "0 skipped").
  * remember() retries its Mongo push and logs a loud warning on failure.
  * All new background work is wrapped in try/except so one error never kills
    the bot; single-process / Render-safe.

ALL PREVIOUS BEHAVIOUR IS UNCHANGED (exact-or->=90 fuzzy match, per-DB2 sibling
sharing, cover-only scanning, auto-grow, /dupescan, /pause|/resume skip detail).
TESTING: py_compile PASS on all 14 files; 20-assertion mocked suite PASS
(tail-scan add + idempotent + cursor, external-push refresh, sibling propagation,
instant alert x10 + single batch summary + empty-batch no-op, no-double-index).
================================================================================


================================================================================
README_PATCH — v42: caption download links + /keepimages + restricted-content
cover workaround + /targets board TTL/invalidation
================================================================================

DRAG onto the repo root: bot.py, flow.py, scraper.py, db.py, botapi.py,
richboard.py, forwarder.py, README.md, README_PATCH.txt
(everything else unchanged — forwarder.py IS in this zip because the
restricted-cover workaround lives in send_cover).

1) EMBEDDED CAPTION LINK SUPPORT. The /target wizard now asks (after DB and
   DB2): "Is the download link in a 'Button' or in the 'Caption'?" Caption
   mode asks for the EXACT text holding the hidden hyperlink (e.g. "Download
   Here") and stores link_mode + link_trigger per target in MongoDB
   (db.get_targets auto-migrates old docs to mode 'button'). Detection and
   scraping are mode-aware:
   - scraper.is_post(msg, link_mode, link_trigger) — caption mode = media +
     caption + the trigger text carrying a MessageEntityTextUrl (no buttons
     needed); button mode is byte-identical behavior to before.
   - scraper.find_caption_url() matches the entity's covered text EXACTLY
     after the same NFKD Unicode fold the button matcher uses, so
     '𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 𝗛𝗲𝗿𝗲' == 'Download Here'.
   - flow.process_post caption path: a t.me/<bot>?start=… caption link runs
     the SAME deep-link -> Short-link -> bypass chain as a Download button;
     any other URL is treated as the short link itself and jumps straight
     into _bypass_chain (link_bot may be None there — the bypass result
     resolves it; the group path's base_f moved AFTER the Open-link click
     for exactly this reason).
   - NEW COMMAND /targatelinkmode <n> button | caption <trigger text> —
     change an existing target's mode without re-adding it (progress kept).

2) /keepimages on|off — GLOBAL Mongo-backed switch (default ON = old
   behavior). OFF: flow's classification discards EVERY image the media bot
   sends (photos AND image-mime documents, with or without caption) before
   the DB bundle is built. Videos, .srt and text notes are never touched;
   the video-armed quiet timer in _collect_media is unchanged.

3) RESTRICTED TARGETS ("Restrict saving content" / msg.noforwards).
   forwarder.send_cover detects the flag and, instead of a by-reference
   copy (which Telegram rejects), downloads the cover to a temp file,
   uploads it to the DB channel as a NEW message with the exact original
   caption/buttons/spoiler, then DELETES the temp file in a finally block —
   success or failure — so Render's disk never fills. Media from the bypass
   bot is unaffected (it isn't restricted), and the fresh DB cover post
   mirrors to DB2 normally.

4) /targets BOARD LIFECYCLE. BOARD_TTL 150s -> 1800s (30 min). mark_board
   now ZEROES the previous board's timestamp when a new board is sent for
   the same chat (deleting it would fall through to the live-board check
   and wrongly allow stale taps), so buttons on an older scrolled-up board
   answer "This board has expired" and change nothing. In-place edits
   (Refresh/toggle) pass the same message id and are never invalidated.

TESTING: py_compile PASS on all 7 changed .py files. Mocked sandbox suite
(55+ assertions): caption URL extraction (exact/fancy-font/wrong-trigger/
no-entity), mode-aware is_post/why_not_post, is_image_msg, /keepimages
on/off classification, caption-mode process_post end-to-end (deep-link AND
direct-short-link forms) with a mocked client, button-mode process_post
regression, restricted cover download->upload->temp-deleted (plus deletion
on upload failure, and the normal non-restricted path never downloading),
board TTL=1800 + previous-board invalidation + in-place edit survival, db
target migration/add/set_target_link_mode/keep_images defaults, and botapi
handler registration + admin-gate + wizard-step wiring. NO live Telegram
calls were made — validate one real caption-mode post on a test target
after deploy.
================================================================================

================================================================================
README_PATCH — v41: /stats v2 + /removeavoid N + replace-wins guarantee
================================================================================

DRAG onto the repo root: botapi.py, README_PATCH.txt (everything else unchanged).

1) /stats v2 — per userbot: connection state, name/@username/id, then a full
   membership matrix: ✅ member / ❌ NOT a member / 👑 admin for EVERY target
   channel and every DB + DB2 channel, plus the CONTROL BOT's own DB/DB2
   rights. A ❌ on a target is exactly why a resume silently does nothing —
   join it with /invite.

2) /removeavoid N — N is the entry number in the /avoidtext list (per-target
   avoids numbered top to bottom). Bare /removeavoid shows the numbered list.

3) /replaceword always wins over /avoid and /avoidtext — replacement rules
   run BEFORE any strip in the DB2 mirror pipeline (order verified in code).

TESTING: py_compile PASS on botapi.py; role-check and numbering logic match
patterns already proven in v38–v40.
================================================================================

================================================================================
README_PATCH — v45: DB2 duplicate-skip gate (fingerprint index + /dupescan)
================================================================================

DRAG onto the repo root: dedup.py (NEW), bot.py, botapi.py, requirements.txt,
README.md, README_PATCH.txt (everything else unchanged).

1) NEW MODULE dedup.py — fingerprint_cover() (story-only head up to the
   first ➪ metadata bullet, NFKD font fold so bold/quote/mono/fancy captions
   match their clean DB2 twins, URL/@mention strip, >=3-char tokens), Mongo
   collections cover_fp (per-target index, $slice-capped at 20000) and
   db2_cover_index (scan watermark), rapidfuzz token_set_ratio >= 90 matching
   with an exact-equality fast path, batched skip DMs (10 per target or on
   /pause), auto-grow after every successful scrape. The gate NEVER raises —
   a fingerprint error means "scrape normally", never a blocked post.
2) GATE in bot.py — both the sequential loop and the v39 parallel dispatcher
   call dedup.already_indexed() right after is_post(); a duplicate cover
   short-circuits the ENTIRE chain (no Download click, no bypass, no DB/DB2
   bundle) and advances progress exactly like a normal non-post skip.
3) ONE-TIME AUTO SCAN — the /target wizard and /setdb2 fire
   dedup.scan_db2_bg(): DB2 history is read ONCE by the USERBOT (bot
   accounts are blocked from GetHistoryRequest), cover posts only (m.photo
   gate — same rule as /scan4duplicates), fingerprints stored in Mongo and
   cached in RAM. /pause, /resume, /goto and restarts NEVER re-scan.
   /dupescan <n> forces a rebuild.
4) requirements.txt — adds rapidfuzz (dedup falls back to difflib if the
   wheel is ever missing).

TESTING: py_compile PASS on all 6 files. Mocked sandbox suite: fingerprint
cut at ➪ / metadata-keyword fallback / fancy-font NFKD fold / URL+mention
strip / empty-caption pass-through, exact + fuzzy >=90 matching,
below-threshold pass-through, $ne-dedup push, cover-only scan filtering
(photos in, videos/.srt/empty captions out), cached rescan no-op,
batch-of-10 DM + on-pause flush, gate-never-raises. NO live Telegram calls —
validate one real duplicate post on a test target after deploy.
================================================================================

================================================================================
README_PATCH — v45.1: Python-3.14 rapidfuzz pin + shared-DB2 index sync +
pause/resume skip details
================================================================================

DRAG onto the repo root: dedup.py, bot.py, botapi.py, requirements.txt,
README.md, README_PATCH.txt (everything else unchanged).

1) BUILD FIX — rapidfuzz pinned to >=3.14.6,<4 (cp314 manylinux wheels are
   published on PyPI from 3.14.6; 3.10.1 had none, forcing a failing
   scikit-build-core source build on Python 3.14). Remove the Render
   PYTHON_VERSION=3.11.9 override after deploy.
2) SHARED DB2 — targets pointing at the same DB2 channel share ONE
   fingerprint index: dedup.share_db2() copies a sibling's index (RAM +
   Mongo, marked 'shared_from') instead of rescanning; scan_db2() syncs
   every sibling on completion; remember() propagates every auto-added
   fingerprint to all siblings. /dupescan shares when a sibling index
   exists; a forced rebuild invalidates siblings so one rebuild refreshes
   the whole DB2 group. /target and /setdb2 share first, scan second.
3) PAUSE/RESUME DETAILS — /pause <n> appends the target's pending skip
   batch (post ids + % match) inline via dedup.consume_skips()/
   format_batch(); /resume <n> reports the target's session dupe total
   and clears any pending batch. Bare /pause still DMs every target's batch.

TESTING: py_compile PASS on all 13 files; 30-assertion mocked suite
(fingerprint cut/fold/strip, exact + fuzzy >=90, below-threshold pass,
$ne dedup, cover-only scan filtering, sibling share + scan sync + remember
propagation, cached rescan no-op, batch-of-10 DM, pause consume-vs-flush,
gate-never-raises on Mongo failure). NO live Telegram calls.
================================================================================
