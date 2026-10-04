================================================================================
v52 - LIVE WORKER SESSIONS (/addworker) + RICH-TABLE /stats
================================================================================
Files changed: db.py, session_manager.py, bot.py, botapi.py, richboard.py.

/addworker <session string> adds a scraping worker LIVE — validated, stored in
Mongo (config.extra_sessions), attached to the rotation with no redeploy. Bare
/addworker lists all workers ([env] / [bot#k]); /removeworker <bot#> removes a
bot-added one. /stats now renders a compact Bot API rich table (one row per
worker, emoji membership matrix) with a chunked plain-text fallback.

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

# Telegram MTProto Userbot — Post/File Scraper

Scrapes a target channel post-by-post: clicks **Download** → @Fubuki_xRobot
**Short link** → sends link to a **bypass group** → clicks the tagged **Open link**
→ Fubuki final link → @Rias_Gremory_Robot → forwards **cover post + video(s) + .srt**
to your database channel. Progress/config/stats live in MongoDB.

## Deploy (Render free web service)
1. Push this repo to GitHub.
2. Render → New → Web Service → connect repo (or use render.yaml blueprint).
3. Env vars: `API_ID`, `API_HASH`, `STRING_SESSION`, `MONGO_URI`, plus
   `BOT_TOKEN` (from @BotFather) and `ADMIN_USER_ID` (your numeric id from
   @userinfobot) to enable the **control bot with a tappable / command menu**.
4. Free instances sleep — ping `https://<app>.onrender.com/health` every ~10 min
   with an uptime monitor (UptimeRobot etc.).

## Two ways to control it

**A) Control bot (recommended):** open a chat with YOUR bot (the BOT_TOKEN one).
Tap `/` — a menu lists every command. Tap `/target`, `/bypass` or `/adddb`
with NO argument: the bot asks for the id, you send it, it validates and
saves it. Add all three one by one, then tap `/start`.

**B) Userbot commands:** type these in any chat (Saved Messages recommended):

| Command | Action |
|---|---|
| `/target <id>` | set target channel |
| `/bypass <id-or-@bot>` | set bypass bot **#1** (the pool's first entry) — group id OR bot @username (e.g. `@dex_fekkyeww_bot`) |
| `/addbypass` | **v39** add another bypass bot to the pool (wizard or `/addbypass @bot`) |
| `/removebypass <n>` | **v39** remove pool bot #n (see `/bypasslist`) |
| `/bypasslist` | **v39** show the bypass pool + domain rules |
| `/domainbypass` | **v39** map a short-link domain to ONE bypass bot (wizard, or `/domainbypass babylinks.in @BypassBot_A`) — those links go ONLY to that bot; `aerolinks.*` wildcards work |
| `/deldomain <n>` | **v39** remove domain rule #n |
| `/altbypass <id-or-@bot>` | last-resort fallback — tried after every pool bot fails; both failing DMs the admin the post link |

`/targets` shows each target's DB channel as a tappable invite link (minted by the userbot, which is admin there, and cached).
| `/linkbutton [text]` | list LINK_BOT button labels, or add one — matched immediately, **no restart** |

Each target's LINK_BOT is discovered per-post from the Download button's own `t.me/<bot>?start=...` URL, and MEDIA_BOT is discovered from LINK_BOT's final reply — so different target channels can use completely different bot pairs with no config.
| `/removelinkbutton <n>` | remove custom label #n (see `/linkbutton` for numbers) |
| `/adddb <id>` | set database channel |
| `/lastpost [n]` | show the newest real post in target n (default 1) |
| `/start` | start scraping |
| `/pause [n]` | pause — bare = ALL targets; `/pause 2` = only target 2, others keep scraping. Persisted in MongoDB (crash-safe) |
| `/resume [n]` | bare = resume everything; `/resume 2` = resume only target 2 from its saved message id |
| `/status` `/current` | live stage & current post |
| `/progress` | posts done, **last scraped post**, resume point, media sent, failures + reasons |
| `/ping` | check the bot is alive (works in control bot AND userbot) |
| `/checkram` |
| `/stats` | **v40** list every connected userbot: acc number + name + @username + id |
| `/invite [n] <link>` | **v40** userbot n (or ALL if no n) joins a channel (public @name or private t.me/+ link); each confirms the channel it joined |
| `/leave [n] <id|link>` | **v40** userbot n (or ALL) leaves a channel |
| `/avoid "txt"` | **v40** GLOBAL DB2 strip — removed from every target's DB2 caption (per-target /avoidtext still works on top) |
| `/replaceword "old" "new"` | **v40** GLOBAL DB2 replace — rewrites text in every DB2 caption before the avoid strip (empty "new" deletes) | **v39.1** show RAM usage — process MB + container used/limit (Render 512 MB cap) |
| `/skip` `/stop` | skip current post / stop |
| `/replace <ch> "old" "new"` | userbot edits every post containing `old` in that channel, replacing all occurrences |
| `/replace "old" "new"` | BOT edits every DB2-mirror post containing `old` (2-arg form) |
| `/deletetext <ch> "text"` | userbot removes `text` from every matching post in that channel |
| `/massdlt <chat> <start_link> <end_link>` | userbot deletes every message between the two message links (inclusive) — chunked + paced, flood-safe |
| `/massdlt_status` `/massdlt_stop` | watch / stop the mass-delete |
| `/forward <target> <source> <start_link> <end_link>` | userbot copies a message range into another channel — by reference, no "Forwarded from" tag, zero download |
| `/forward_status` `/forward_stop` `/forward_resume` | watch / stop / resume a forward (cursor saved in MongoDB, survives crashes) |
| `/add <channel> @bot1 [@bot2 …]` | userbot adds the bot(s) to the channel as ADMIN with as many rights as the userbot itself has |
| `/addadmin [user id]` | owner adds a bot admin (full control-bot access); bare = list owner + admins |
| `/removeadmin <user id>` | owner removes a bot admin |
| `/setdb2 <n> <id\|off>` | set/clear a target's DB2 clean-mirror channel |
| `/avoidtext [n] ["text"]` | DB2: list or add a credit string to strip from mirrored captions |
| `/removeavoid <n> <#>` | remove an avoid string |
| `/targatelinkmode <n> button\|caption [text]` | **v42** change how target n's download link is found — inline Download button, or a hidden hyperlink behind the exact caption trigger text |
| `/keepimages on\|off` | **v42** GLOBAL switch (default ON): OFF = every image the media bot sends is discarded; only videos/.srt/text reach the DB |
| `/scan4duplicates <channel_id>` | **v43.3** find duplicate posts in a DB2 channel — scans **cover-post (photo) captions only** (video/.srt captions ignored), compares ONLY the story description (hashtag lines like `#uncensored #recommended`, the `Episode/Subtitle/Censorship/Rating/Network:` block and the `edited` tail stripped first), first-10-words fuzzy match (≥75%, difflib), clustered report with `t.me/c/…` links, chunked output; **read by the USERBOT** (bots are blocked from channel history — the userbot must be a member, use `/invite`); runs async without blocking the scraper |

## v42 updates
- **Caption download links:** the `/target` wizard now asks (after DB/DB2) "Is the download link in a Button or in the Caption?". Caption mode asks for the EXACT text holding the hidden hyperlink and stores mode + trigger per target in MongoDB. Scraping that target extracts the embedded URL (`MessageEntityTextUrl`, exact Unicode-folded match — 𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 𝗛𝗲𝗿𝗲 == "Download Here") instead of looking for a Download button: a `t.me/<bot>?start=…` caption link continues through the normal LINK_BOT → Short-link → bypass chain, while any other URL is treated as the short link itself and goes straight to bypass. Change the mode later with `/targatelinkmode` (progress is kept).
- **Restricted targets** ("Restrict saving content" / `noforwards`): the cover post can't be copied by reference, so the userbot downloads the cover image, re-uploads it to the DB channel as a NEW message with the exact original caption/buttons, and deletes the temp file immediately after (on success AND on failure). Videos/.srt collected from the media bot are unaffected, and the fresh DB cover mirrors to DB2 normally.
- **`/targets` board:** button TTL raised from 150s to 30 minutes, and sending a NEW board instantly expires the chat's previous board — buttons on older scrolled-up boards show the "board expired" popup and change nothing.

## DB2 clean mirror (bot-powered)
Each target can have a second **DB2** channel (set in the `/target` wizard — it
now asks for it after the DB channel — or later with `/setdb2 <n> <id>`). When
the userbot posts new content into a target's DB channel, the **control BOT**
automatically re-posts it into DB2 with a **cleaned caption**: every
`/avoidtext` string is removed, plus ALL links (`t.me/…`, `http(s)://…`) and
`@username` mentions are auto-stripped, and embedded-link formatting is
dropped (captions are re-sent as plain text). Media, spoiler flags and
buttons are preserved; posts arrive as fresh bot posts with no forward tag.
**Requirement:** the BOT must be admin in both the DB channel (to see its
posts) and DB2 (to post). Because DB2 posts are the bot's own, they can be
re-edited flood-free with `/replace "old" "new"` (2 args = every DB2 channel,
edited by the BOT; the 3-arg `/replace <ch> "old" "new"` form still uses the
userbot on any channel).

## MTProto bulk jobs (mass delete / forward / add-bot)
All three run as paced background jobs, exactly like the bulk editor: one
action every few seconds (`MASS_DELETE_DELAY` / `FORWARD_DELAY`, ~3s default,
tunable via Render env), deletions go out in chunks of `MASS_DELETE_CHUNK`
(default 100 ids per call) so even a 2000-message range is deleted
piece-by-piece with rests — Telegram never sees a burst. FloodWait errors
are slept through in place and the same work retried (up to
`BULK_MAX_FLOOD`, default 900s). While any job runs, the scraper
auto-pauses (Telegram's flood bucket is account-wide) and auto-resumes
after; a summary is DM'd to the admin when a run finishes. `/forward`
persists its cursor in MongoDB after every message, so `/forward_stop`, a
crash, or a redeploy can be picked up with `/forward_resume`. `/add` needs
the userbot to already be an admin with add-admins permission in that
channel; it grants each bot as many admin rights as the userbot itself has there (group-only rights are skipped in channels).

`/targets` and `/progress` show each channel's **title as a tappable link** instead of a bare id: private targets link to their last scraped post (`t.me/c/…` — works for any member, no invite link needed), DB channels use their cached invite link (falling back to the plain title).

Extra admins: `/addadmin <user id>` (owner only) gives another Telegram user FULL control-bot access — every command, like the owner. Bare `/addadmin` lists owner + admins; `/removeadmin <user id>` revokes one. The list lives in MongoDB, so it survives restarts/redeploys.

Bulk edits are **Telegram-safe paced**: after scanning, a background worker edits ONE message every `BULK_EDIT_DELAY` seconds (default 2.5s — tune via Render env), sleeps through FloodWait errors in place and retries the same message (up to `BULK_MAX_FLOOD`, default 900s), and posts live progress into the status message every 10 edits. The control bot stays responsive during long runs. 500-message run at default pacing ≈ 21 min. During a bulk edit the scraper auto-pauses (the flood bucket is account-wide — scraper sends share the same limit as edits) and auto-resumes when the run finishes; pacing is jittered (+0–1.5s random).

`/help` and the tappable menu are generated from the same command list, so every command above appears in both. Telegram caches the "/" menu — if it looks stale after a redeploy, close/reopen the bot chat.

## Flood-wait protection (v3)
If Telegram returns FloodWaitError at login or mid-scrape, the process now
SLEEPS in place for the required seconds instead of crashing — so Render never
enters a crash-restart loop and Telegram's flood timer is never refreshed.

## Crash resilience
Progress (last processed message id) is written to MongoDB after EVERY post.
If Render crashes or restarts, the bot resumes from that exact point —
nothing is scraped twice.

## Copy-mode delivery (v17)
The cover post and all media arrive in the DB channel as FRESH posts with NO
"Forwarded from" tag — the userbot re-sends each message's media by its
Telegram file reference (send_file with msg.media), so Telegram copies the
file server-to-server. Nothing is downloaded to disk or RAM (no temp files,
stays flat on the 512MB free tier even for 700MB+ videos), and every file
keeps its original format: playable video with thumbnail/duration/filename,
spoiler flag, caption and buttons all preserved. Every send is VERIFIED:
Telegram can silently accept a send whose file reference is dead (message
created with no media — nothing appears in the channel); the bot detects
this, deletes the empty message, refetches the source for a fresh
reference and retries. Everything the media bot sends is mirrored —
videos, stickers, photos AND text notes (like its deletion warnings).
Media collection waits for the actual VIDEO to arrive before its
quiet-timer can end collection — the media bot posts stickers instantly
but uploads videos slowly, and a post with ZERO videos is failed +
retried, never archived as sticker-only.

IDs: use the numeric id (e.g. `-1001234567890`) or @username.
The account must be a member of the target channel, bypass group (with
post permission), and admin (post rights) in the DB channel.

## Parallel multi-userbot scraping (v39)
With 2+ sessions (`STRING_SESSION`, `STRING_SESSION2`, …) the scraper no longer rotates sequentially — it runs all available accounts **in parallel on the current target's pending posts** (account 1 → post #1, account 2 → post #2, …). A single remaining post goes to any one free account. Delivery into the DB channel is **serialized per DB channel**: one post's complete bundle (cover FIRST, then all its media) always lands before the next bundle starts, so posts never interleave (DB2 inherits the same order). Progress advances only past **contiguously finished** posts, so an out-of-order finish or a flood-parked account never makes the scraper skip a post. When a target is fully caught up the scraper moves to the next resumed target and fans its posts across all accounts the same way. A single account still uses the original sequential path. `/pause` lets in-flight posts finish; `/skip` aborts all in-flight posts.

## Bypass routing (v39)
Each captured short link's **domain** picks its bypass bot: a `/domainbypass` rule sends it ONLY to that bot, while any other link tries each pool bot (`/bypass` + `/addbypass`) in order, then `/altbypass` as the last resort. Every bot gets **2 attempts**; a 2nd failure fires an instant ⚠️ **Bypass Failure Alert** to the owner + all `/addadmin` admins (failed link, bypass bot, userbot, target channel, post msg id), and the post is skipped per the normal error policy — no crash, no stuck loop.

## Multi-session later
`session_manager.py` already round-robins — add more StringSessions to scale.

## Duplicate protection (v45)

Before a post's download chain even starts, the scraper checks its COVER caption against a fingerprint index built from the target's **DB2** channel (DB2 captions are the clean, credit-stripped copies):

* **Fingerprint** — the story paragraph only (everything before the first `➪ Episode:-` metadata bullet; metadata-keyword lines also cut when the bullet is absent), NFKD-folded so 𝗯𝗼𝗹𝗱/𝘪𝘵𝘢𝘭𝘪𝘤/mono/fancy fonts collapse to plain text, URLs and @mentions stripped, tokens <3 chars and pure numbers dropped.
* **Match** — exact fingerprint hit, else rapidfuzz `token_set_ratio >= 90`. A match SKIPS the whole post (no Download click, no bypass, no DB/DB2 bundle) and advances progress like a normal skip.
* **One scan, ever** — built automatically in the background when you /target (or /setdb2) a channel with DB2, cached in MongoDB + RAM. /pause, /resume and restarts NEVER re-scan. `/dupescan <n>` forces a rebuild (e.g. after mass-editing DB2 captions). Bare `/dupescan` lists per-target index sizes.
* **Auto-grows** — every successfully scraped cover is added to the index, so a repost of a NEW post is caught later with zero DB2 re-scanning.
* **Per-target** — targets sharing one DB2 each load their own copy of the same fingerprints, so a cover already in the shared DB2 is skipped for BOTH targets.
* **Notifications** — skips are DM'd in batches of 10 per target, plus a mandatory flush when that target is paused (bare /pause flushes all); /status shows the per-target dupe counter.

### Duplicate protection — v45.1 updates

* **Shared DB2 = shared index** — targets pointing at the SAME DB2 channel share one fingerprint set: scanning once indexes every sibling, every auto-added fingerprint propagates to all of them, and `/dupescan 7` when target 8 shares the DB2 answers "shares the existing index — no rescan". A forced `/dupescan <n>` rebuild refreshes the whole DB2 group.
* **Pause/resume reporting** — `/pause <n>` appends that target's pending skip details (post ids + % match) to its reply; `/resume <n>` reports the target's session dupe total and clears any pending batch; bare `/pause` still DMs every target's batch.
* **Build note** — requirements pins `rapidfuzz>=3.14.6,<4`, which publishes cp314 wheels: Render's default Python 3.14 builds natively (remove the `PYTHON_VERSION=3.11.9` override).
