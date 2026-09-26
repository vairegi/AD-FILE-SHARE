# Telegram Video Bots — single-service, two-bot deployment

A production-ready Python project hosting **two Telegram bots inside ONE process**
so they fit comfortably on a single Render free-tier Web Service (512 MB).

* **Bot 1 — Gate / Link bot**: drip-posts daily covers to a public channel,
  runs the force-subscribe gate, the monetised shortener verification gate,
  and carries the full admin panel.
* **Bot 2 — File Delivery bot**: validates a short-lived single-use token and
  delivers the video by **server-side copy** from the private Database Channel.

```
Telegram ──► /bot1/webhook ──┐
Telegram ──► /bot2/webhook ──┤  single FastAPI + uvicorn process
                             └─► shared MongoDB Atlas cluster
```

---

## 1. The user flow in one picture

1. Admin uploads videos to the private **Database Channel**
   (`cover → 1-2 videos → optional .srt` posts).
2. `/scandb <channel_id>` (Telethon userbot) indexes the whole channel into
   MongoDB, grouped into items, oldest first, `posted: false`.
3. Every day at `post_time`, a cover (thumbnail + caption + a green full-width
   **Download** button, plus any `/addbutton` extras) is posted to the public
   **Posting Channel**; the item is flipped to `posted: true`.
4. User taps **Download** → Bot 1 DM (`/start file_<id>`).
5. **Gate 1 — force-subscribe**: not a member → *Join Channel* + *I've Joined ✅*.
6. **Gate 2 — shortener**: sends a VPLinks-wrapped `verify_…` deep link back to Bot 1.
7. User finishes the shortener, lands back, is marked verified **for one file only**,
   and receives a **Get File** button → Bot 2.
8. Bot 2 validates the token (single-use, TTL, bound to that user) and **copies**
   the video from the Database Channel to the user.
9. The delivered message is queued for **auto-deletion** after the configured window.

---

## 2. Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app: lifespan starts/stops both bots, registers webhooks, `/health`, both webhook routes |
| `bot1.py` | Bot 1 handlers, posting queue, gates, deep links |
| `bot1_admin.py` | All Bot 1 admin commands (each guarded by `@admin_only`) |
| `bot2.py` | Bot 2 handlers, token validation, delivery + auto-delete queue |
| `db.py` | Motor/MongoDB layer and all indexes |
| `scanner.py` | Telethon channel-history scanner (`/scandb`, `/rescandb`) |
| `shortener.py` | VPLinks/GPLinks API client (`?api=KEY&url=…&format=text`) |
| `config.py` | Environment-variable loading (no hardcoded secrets) |
| `utils.py` | Flexible duration parser, `human_duration`, `@admin_only` |
| `requirements.txt` | Dependencies |
| `runtime.txt` | Pins Python 3.11.9 for Render |
| `.env.example` | Copy to `.env` for local runs |

---

## 3. Environment variables (Render dashboard)

| Variable | Bot 1 | Bot 2 | Notes |
|---|:---:|:---:|---|
| `BOT1_TOKEN` | ✅ | — | from @BotFather |
| `BOT2_TOKEN` | — | ✅ | from @BotFather |
| `BOT1_USERNAME` | ✅ | ✅ | no `@`, used for deep links |
| `BOT2_USERNAME` | ✅ | ✅ | no `@`, used for deep links |
| `MONGO_URI` | ✅ | ✅ | same Atlas cluster for both |
| `MONGO_DB_NAME` | ✅ | ✅ | e.g. `video_bots` |
| `ADMIN_IDS` | ✅ | — | comma-separated Telegram user IDs |
| `DB_CHANNEL_ID` | ✅ | ✅ | DB channel is used by both — set it on **both** services |
| `POST_CHANNEL_ID` | ✅ | — | optional seed; changeable later |
| `FORCE_SUB_CHANNEL_ID` | ✅ | — | optional seed; changeable later |
| `RENDER_EXTERNAL_URL` | ✅ | ✅ | e.g. `https://my-bots.onrender.com` |
| `BOT1_WEBHOOK_SECRET` / `BOT2_WEBHOOK_SECRET` | ✅ | ✅ | any random string |
| `SHORTENER_API_KEY` | ✅ | — | your VPLinks API token |
| `API_ID` / `API_HASH` / `SESSION_USER` | ✅ | — | only needed for `/scandb` |

> Both bots share one MongoDB database — **every variable must be set on the
> single Render service**. (If you ever split into two services, set `DB_CHANNEL_ID`,
> `MONGO_URI` and the usernames on both.)

---

## 4. Zip the project and push to a new GitHub repo

```bash
# from the parent of the project folder
zip -r telegram-video-bots.zip tgbots

# create an empty repo on github.com first (no README, no .gitignore),
# then:
cd tgbots
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

`.gitignore` already excludes `.env`, `__pycache__/` and the session files, so
**no secrets are committed**.

---

## 5. Deploy on Render (free tier, manual — no render.yaml)

Free-tier instances cannot use Blueprints, so create the service by hand:

1. Render dashboard → **New +** → **Web Service**.
2. Connect the GitHub repo you just pushed.
3. Settings:
   * **Runtime**: `Python 3`
   * **Environment → Python Version**: `3.11.9` (or add env var `PYTHON_VERSION=3.11.9`).
     Without this, Render deploys on its newest Python (3.14) — the `runtime.txt`
     file alone is not always honored on current Render.
   * **Build Command**: `pip install -r requirements.txt`
   * **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   * **Instance Type**: Free
   * **Health Check Path**: `/health`
4. Add every variable from the table above under **Environment**.
5. Deploy. When it goes live, copy the service URL into `RENDER_EXTERNAL_URL`
   and redeploy — the app registers the webhooks itself on startup.

> Free instances sleep after ~15 min of inactivity and cold-start in ~30-60 s.
> Telegram retries failed webhook deliveries, so no updates are lost, but the
> first message after a sleep can take a little while.

### Setting the webhooks manually (optional)

The app sets them automatically, but you can force it:

```bash
curl "https://api.telegram.org/bot<BOT1_TOKEN>/setWebhook" \
  -d "url=https://<service>.onrender.com/bot1/webhook" \
  -d "secret_token=<BOT1_WEBHOOK_SECRET>"

curl "https://api.telegram.org/bot<BOT2_TOKEN>/setWebhook" \
  -d "url=https://<service>.onrender.com/bot2/webhook" \
  -d "secret_token=<BOT2_WEBHOOK_SECRET>"
```

---

## 6. One-time setup checklist

1. Create both bots with @BotFather; note tokens and usernames.
2. Create the **Database Channel** and the **Posting Channel**; add **both bots
   as admins** of the Database Channel (needed for `copy_message`).
3. If using force-subscribe, add **Bot 1 as an admin** of that channel and turn on
   **join request approval** so pending requests can be captured.
4. Fill `.env` (local) or the Render environment, deploy.
5. In Bot 1 as an admin, create your first pipeline (categories replace the
   old single-pipeline commands):
   ```
   /addcategory jav Jav           (guided wizard: DB channel -> posting
                                   channel -> main channel -> tag -> time)
   /setforcesub <channel_id>      (or /setforcesub off)
   /shortenerapi add vplink https://vplink.in/api YOUR_KEY
   /shortener on
   /setverifytime 6
   /setautodelete 7day jav
   ```
   The wizard offers a **Scan now** button to index the DB channel
   (the existing ~200-300 videos) right after setup.
6. Check `/categories` and `/stats`, then `/dripnow jav` to post the first cover.
7. Adding another category later (Anime, Movies, …) is one command,
   zero redeploy: `/addcategory anime Anime` — then answer the wizard.

### Generating `SESSION_USER` (Telethon)

Run once **locally** (not on Render):

```bash
pip install telethon
python - <<'PY'
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
api_id = int(input("API_ID: "))
api_hash = input("API_HASH: ")
with TelegramClient(StringSession(), api_id, api_hash) as c:
    print("\nSESSION_USER =\n", c.session.save())
PY
```

Paste the printed string into the `SESSION_USER` variable.

---

## 7. Command reference

### Bot 1 — admin

**Pipelines (categories) — the core of v2**

| Command | Effect |
|---|---|
| `/addcategory <key> <label>` | guided wizard: DB channel -> Posting channel -> Main channel -> tag -> daily time (IST) |
| `/categories` | full dashboard of every pipeline (channels, queue, posted counts, settings) with Post-now / Pause / Edit / Delete buttons |
| `/editcategory <key>` | re-run the wizard to change a pipeline's settings |
| `/delcategory <key>` | remove a pipeline (purge data optional) |
| `/use <key>` | set the ACTIVE pipeline for the scoped commands below |

**Scoped commands** — act on the active pipeline (`/use`), or append a key
(e.g. `/dripnow anime`). With only ONE pipeline configured they target it
automatically.

| Command | Effect |
|---|---|
| `/broadcast` (reply) · `/broadcast <text>` | asks for a delete timer (`2h`, `30m`, `1h 2m`, `never`), then broadcasts; copies auto-delete from every user when due |
| `/stats` | users, verified, banned + per-pipeline breakdown |
| `/ban <id>` · `/unban <id>` | toggle a ban |
| `/addadmin <id>` | promote an admin without redeploying |
| `/setforcesub <id \| off> [category]` | global default; per-category override with a trailing key |
| `/setautodelete <time> [category]` | per-category auto-delete timer |
| `/setpostchannel <id> [category]` | destination posting channel |
| `/setdbchannel <id> [category]` | source Database Channel |
| `/setpostmainchannel <id \| off> [category]` | main-channel forwarding |
| `/setposttag <text \| off> [category]` | tag line above each main forward |
| `/setposttime <HH:MM> [category]` | daily post time (**IST**) |
| `/setschedule <HH:MM> [category]` | set time (IST) + enable |
| `/schedule on\|off [category]` | enable/disable daily posting |
| `/pauseposting` · `/resumeposting` | pause/resume one pipeline |
| `/dripnow [category]` | post the next queued item immediately |
| `/queueinfo [category]` | next 10 queued posts with links |
| `/queue_reset N [category]` | rewind the queue to post number N |
| `/protect on\|off [category]` | per-category content protection |
| `/rescandb [category]` | re-index the pipeline's DB channel (keeps `posted` flags) |
| `/scandb <channel_id> [category]` | point a pipeline at a DB channel + index it |

**Shortener gate (global)**

| Command | Effect |
|---|---|
| `/shortener on\|off\|status` | toggle or inspect the gate |
| `/shortenerapi` | rotation dashboard — `add`/`pause`/`resume`/`remove` shorteners |
| `/setverifytime <hours>` | verification validity (default 6) — per category |
| `/settokenttl <minutes>` | Bot 1 → Bot 2 handoff token TTL |
| `/shortenermsg <text>` | landing/overlay heading |
| `/shortenerbotmsg <text>` | DM text prompting the shortener |
| `/verifymsg <text>` | text shown after verification |
| `/shortenerbtn <label> \| <url>` | add a secondary button |
| `/clearshortenerbtns` | remove all secondary buttons |

**Channel posts & captions (global, v4.0)**

| Command | Effect |
|---|---|
| `/addbutton <label> \| <link> [| green\|blue\|red]` | extra button under every new channel post (omit the color for the default transparent look) |
| `/buttons` · `/removebutton <n>` · `/clearbuttons` | list / remove one / remove all extra buttons |
| `/addcovercaption <text\|off>` | text appended after every posted cover caption |
| `/addfilecaption <text\|off>` | text appended after every delivered file caption |
| `/addsticker` · `/removesticker` | sticker posted to the MAIN channel after each post (skipped when no main channel) |

**Genres & browse menu (v4.4)**

| Command | Effect |
|---|---|
| `/addgenre <pipeline> <genre>` | register a genre under a pipeline; runs a ONE-TIME caption scan in the background (result stored in MongoDB — never rescanned) |
| `/delgenre <pipeline> <genre>` | remove a genre and its stored match list |
| `/genres [pipeline]` | list genres with matched/posted counts (all pipelines when omitted) |
| `/onbrowse` · `/offbrowse` | enable/disable the `/browse` menu globally |

### Bot 1 — user

| Command | Effect |
|---|---|
| `/start` | welcome (carries a 📂 Browse button) / handles `file_*` and `verify_*` deep links |
| `/browse` | multi-level menu: category → genre → posted items; works in DMs and groups; results deep-link into the gated Download flow |
| `/help` | command list |

### Bot 2 — user

| Command | Effect |
|---|---|
| `/start` | welcome / handles `deliver_*` links |
| `/setautodelete <time>` | personal auto-delete override |

Durations accept `30min`, `2hour`, `12hour`, `1day`, `7day`, `never`
(a bare number means minutes). Compound forms like `1h 2m` also work (v4.0).

---

## 8. Notes & limitations

* **Large files are fine.** Delivery uses `copy_message`, which Telegram performs
  server-side — the bot never downloads the file, so the 20 MB Bot API download
  limit and the source-channel identity are both irrelevant.
* **One verification = one file.** The `verify_` token is single-use and bound to
  the user ID, so a forwarded link is refused by Bot 2.
* **Queue position lives in MongoDB**, so restarts and redeploys never repeat or
  skip an item.
* **`/scandb` needs Telethon credentials.** Without `API_ID`/`API_HASH`/`SESSION_USER`
  the rest of the bots still run; only the history scan is unavailable.
* **The force-sub gate fails open** if `get_chat_member` errors (e.g. Bot 1 is not
  an admin of that channel) so real users are never locked out — the failure is
  logged. `get_chat_member` is the only check performed, as required.

---

## 9. Legal notice

This system is intended **only** for content the operator owns or is licensed to
distribute. Deploy it with media you hold the rights to.

## v2.4 — Strict per-post verification + admin shortener bypass (2026-09-19)

- **Strict per-post verification**: the shortener gate is shown on EVERY Download tap. Solving the link for one post never unlocks another post — or the same post again. `users.verified` / `verified_until` are now STATS ONLY (they feed `/stats` + `/categories` counts) and never skip the gate. Applies automatically to all current AND future categories/channels.
- **Admin bypass**: admins (env `ADMIN_IDS` + `/addadmin`) receive the file instantly without the shortener. Force-sub still applies to admins.

## v2.5 — Auto-delete reaches every pipeline + /withfilemessages (2026-09-19)

- **Fixed the "1 hour" bug**: /setautodelete now applies the timer GLOBALLY **and** to every existing pipeline. Previously a pipeline's own older auto_delete_minutes silently overrode the global value, so a 7-day setting still showed "1 hour".
- **New /withfilemessages [time] <text>** (Bot 2, admin-only): sets your own post-delivery notice. An optional leading time (7day / 1hour / 30min / never) also sets the auto-delete timer for all files; `{N Duration}` inside the text is replaced with the real time left before deletion.

## v2.7 — /addcategory wizard crash fix (2026-09-19)

- **Root cause of the silent freeze at step 5/5**: the wizard seeded `data` with
  `"key"` and `_wizard_finish` passed it again via `**data`, crashing with
  `TypeError: create_category() got multiple values for argument 'key'`. Fixed —
  both reserved keys are popped before the DB call.
- The wizard can no longer die silently: finish/step errors are caught, logged,
  and reported to the admin ("Nothing was saved — please retry"), and the
  session is reset instead of leaving the bot unresponsive.
- Step 4 prompt now explains what the "tag line" is (caption above each
  main-channel forward; optional).

## v2.8 — /addcategory "Scan now" fix (2026-09-19)

- **DuplicateKeyError on scan**: the `raw` collection had a legacy unique index on `message_id` alone; a second pipeline's scan crashed because every DB channel restarts message_id at 1. db.connect() now drops the stale index at startup; the compound (category, message_id) unique index is the correct key.
- **AttributeError 'NoneType' reply_text**: "Scan now" is a button callback (update.message is None) — the scan error handler crashed while reporting the failure. _run_scan now replies via context.bot.send_message and works from commands and callbacks alike.

## v2.9 — Complete stale-index sweep + callback-safe scan (2026-09-19)

- v2.8 dropped the legacy `raw.message_id` unique index; the SAME legacy pattern also existed on `files.db_message_id` (plus a leftover `files.posted_1`), which crashed the scan one step later with another DuplicateKeyError. db.connect() now drops ALL of them at startup. Adding any future pipeline scans cleanly.
- `_run_scan` replies via context.bot.send_message so "Scan now" (a button callback) always reports success/failure instead of freezing.
- `_wizard_finish` pops the reserved `key` field before `**data` (the TypeError that silently killed /addcategory) and reports failures to the admin.

## v3.0 — Multi-version posts deliver everything (2026-09-19)

- Bot 2 no longer asks "🎬 Choose which version you want:". When a post has multiple videos in the DB channel, ALL versions are delivered in one go. Subtitle files attach once (with the first video); the auto-delete notice is posted once, after the last file. Old "dl:" buttons already sent to users still work (they deliver that single version).

## v3.1 — MKV/AVI/WebM delivery fix (2026-09-19)

- Videos uploaded as Telegram **documents** (`.mkv`, `.avi`, `.webm`, and `.mp4`
  sent as files) were misclassified as subtitles, so items had no videos and
  users got "This file is no longer available." Both classifiers (userbot scan +
  live channel_post) now treat any `video/*` mime-type document as a video.
- After deploying, re-scan the affected pipeline(s) (`/rescandb` or Scan now) so
  already-uploaded MKV files are indexed correctly. New uploads work immediately.

---

## v3.2 (2026-09-20) — multi-shortener round-robin + instant anti-bypass ban

**Multi-shortener rotation.** `/shortenerapi` is now a management dashboard
for MULTIPLE shorteners instead of one global key:

- `/shortenerapi` — lists every saved shortener: site name, masked key,
  API base, 🟢 Active / ⏸ Paused.
- `/shortenerapi add <site> <api_base> <api_key>` — add to the rotation
  (validates the URL, rejects duplicate names, then runs a live self-test).
  Example: `/shortenerapi add gplink https://gplinks.in/api YOURKEY`
- `/shortenerapi pause <site>` — stays in the list but is SKIPPED in rotation.
- `/shortenerapi resume <site>` — back into rotation.
- `/shortenerapi remove <site>` — deleted from the database completely.
- The old single-key syntax (bare key / `url` / `clearkey`) was REMOVED.

How rotation works: every Download tap advances THAT USER'S own round-robin
cursor (stored on the user doc) and picks from the ACTIVE shorteners only —
paused entries are filtered out before the pick, so they can never serve a
link. Fail-open: if EVERY shortener is paused, the user gets the file
directly (same as today's disabled-gate behavior; logged as a warning).
Each verify token records which shortener served it (`shortener_site`).

Storage: new `shorteners` MongoDB collection (unique index on `site`). On
first startup the legacy single key (settings.shortener_api_key, else the
SHORTENER_API_KEY env var) is migrated in as `vplink` — no key needs to be
re-entered, and the old settings fields stay untouched as a safety net.

**Instant anti-bypass ban.** The verify timing gate no longer gives 3
strikes: ONE too-fast attempt burns the token and bans the user immediately.
The admin alert (username, elapsed seconds, /unban hint) is unchanged.

---

## v3.3 (2026-09-20) — hotfix: shortener re-add after remove

**Bug:** after `/shortenerapi remove <site>` removed the LAST entry, the
v3.2 migration (which runs whenever the collection is empty) re-seeded
`vplink` from the legacy/env key on the next process start — so re-adding
the same name with a different base failed with "already exists".
**Fix:** migration now runs exactly ONCE (guard flag
`settings.shorteners_migrated`); an empty rotation stays empty forever.
The SHORTENER_API_KEY env var is no longer read at runtime — the
`shorteners` collection (managed only via /shortenerapi) is the single
source of truth. `add` now also tells you the existing entry's base when
a name is taken, and how to replace it.

---

## v3.4 (2026-09-20) — multiple force-sub channels + shortener note

**Force-sub now supports MULTIPLE channels.** `/setforcesub <channel_id>`
APPENDS to the required list instead of replacing the previous one (global
default), `/setforcesub <channel_id> <category>` appends per-category, and
`/setforcesub off` clears the list. Users must be members of ALL required
channels; the gate message shows one Join button per channel. Backward
compatible: the old single `force_sub_channel_id` value is honoured until
you set the new list.

**Shortener self-test clarification:** the ⚠️ on adding `vplink` was NOT a
bot bug — the base `https://vplinks.com/api` has no working TLS (connection
fails), while `https://vplink.in/api` works with the same key. Always use
the exact API base from your provider dashboard.

---

## v3.5 (2026-09-20) — force-sub gate bypass fix + admin tooling

**CRITICAL FIX — Gate 1 bypass.** `gate_ok` accepted ANY join request ever
recorded (`has_join_request(user_id)`, unscoped), so a user who once tapped
"request to join" on any channel skipped force-sub forever. Join requests
are now stored per-channel (`{user_id, chat_id}`) and a request only passes
the channel it was made for. Note: pre-v3.5 request records have no channel
id and no longer grant a pass — affected users simply re-request once.

**/setforcesub auto join-request link.** Setting a channel now calls
`create_chat_invite_link(creates_join_request=True)` and stores the link;
the gate's Join button uses it, so every gated user lands in the channel's
pending-requests list (backup audience if a channel is ever banned).

**New admin commands:** `/forcesublist` (dashboard: global + per-category
channels with their invite links), `/forcesubremove <channel_id|category|global>`,
`/banmessage <text>` (or reply to a message, or `reset`), `/banlist` — each
entry formatted `1 - @username Elapsed: 46.7s \`/unban 8416709177\`` with the
command in inline code for one-tap copy.

---

## v3.6 (2026-09-20) — hotfix: custom ban message at ban moment + forcesub link backfill

**Fix 1 — `/banmessage` now applies at the ban moment.** The anti-bypass
instant-ban in bot1.py sent a hardcoded message; it now uses the custom
`ban_message` (falling back to the default). The custom text is used in ALL
ban spots: instant-ban, /start in both bots, Bot 2 delivery + buttons.

**Fix 2 — `/forcesublist` backfills join-request links.** Channels set
before v3.5 had no stored invite link, so the dashboard showed "(no
join-request link stored)". The dashboard now creates the join-request link
on the fly (create_chat_invite_link, creates_join_request=True), stores it,
and displays it — run /forcesublist once after deploying and every channel
gets its link. The gate button picks it up automatically.

**Cosmetic:** /forcesublist and /forcesubremove moved from "General" to the
"Access & content" section of the /admin help list.

---

## v3.7 (2026-09-22) — /banlist as a native rich-message table

`/banlist` now renders as a real table using Bot API 10.1+ Rich Messages
(`sendRichMessage` -> `InputRichBlockTable`, `is_compact: true`): columns
# / User / Detail / Tap-to-copy, with the `/unban <id>` cell in code style
so one tap copies only the command. Long lists (70-80+) are split into
45-row table pages automatically (rich-message limits: 32k chars / 500
blocks / 20 columns). If Telegram rejects the rich payload on any client
or rollout stage, the bot automatically falls back to paged inline-code
text messages (40 rows each) — the list can never fail to display.

---

## v3.8 (2026-09-22) — rich-table fix + /addsticker

**Fix — rich banlist table.** Telegram rejected the v3.7 payload
("can't find field cells"): `InputRichBlockTable` takes `cells` of
`InputRichText` ({text, entities}), not block rows. Corrected. The text
fallback also moved from Markdown to HTML so @usernames can never break
parsing on long lists (the "byte offset 1460" crash).

**New — /addsticker.** Admin sends `/addsticker`, then forwards/sends any
sticker; the bot saves it and posts it immediately after every channel
post in every pipeline (a tap-able tag sticker under each post).
`/removesticker` turns it off. Non-admin stickers are ignored; the intake
auto-aborts if the next message isn't a sticker.

---

## v3.9 (2026-09-22) — /addsticker fix + rich /shortenermsg

**Fix — /addsticker never captured the sticker.** The intake handler was
registered in handler group 0 with a catch-all filter, so forwarded
stickers were silently swallowed and never saved. It now lives in its own
later group with a stickers-only filter — sticker captured, confirmed, and
posted after every channel post.

**Enhancement — /shortenermsg accepts rich input.** Plain text, Markdown
styles (bold/italic/underline/strike/spoiler), ```pre blocks```, inline
`code`, links, quotes and blockquotes all survive: message entities are
converted to HTML and stored (shortener_msg_html). Reply to any formatted
message with /shortenermsg to copy its rich text. The gate sends the
heading with parse_mode=HTML when a rich version exists; the plain-text
fallback path is unchanged.

---

## v4.0 (2026-09-25) — colored post buttons, sticker→main channel, caption commands, timed broadcasts

**Colored channel-post buttons (Bot API 9.4 `InlineKeyboardButton.style`).**
The default `#N 𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱` button is now always GREEN (`style=success`)
and full-width. New global commands:

- `/addbutton <label> | <link> [| green|blue|red]` — add an extra button
  under the Download button of EVERY new channel post (all pipelines).
  Omit the color for the default transparent look.
- `/buttons` — list extras · `/removebutton <n>` — remove one ·
  `/clearbuttons` — remove all.

Layout: the Download row stays full-width; extras sit TWO per row (a single
extra takes the whole row). Colors ride through PTB's `api_kwargs`
passthrough because python-telegram-bot 21.x predates the Bot API 9.4
`style` field. If Telegram ever rejects a styled post, the bot automatically
reposts the same message with unstyled buttons — a post is never lost.
Main-channel forwards carry the same buttons (colorless on a real forward —
Telegram forwards cannot carry a custom markup).

**Fix — /addsticker destination.** The post sticker now goes to the
pipeline's MAIN posting channel (`post_main_channel_id`) instead of the
base posting channel; pipelines without a main channel skip the sticker.
(This also removes a latent NameError — the old line referenced an
undefined `settings` variable.)

**New — /addcovercaption · /addfilecaption (global).** Extra text APPENDED
after the original caption: cover captions apply to the posted cover photo,
file captions to every file Bot 2 delivers. `<cmd> off` clears it.

**Timed /broadcast.** `/broadcast` (reply mode AND inline text) no longer
sends immediately: the bot asks "After how many hours or minutes should
this broadcast message be deleted from users?", accepts `2h`, `30m`,
`1h 2m` or `never` (utils.parse_duration now understands compound
durations), then broadcasts. Every delivered copy is queued in the existing
`deletions` collection tagged `bot: "bot1"` — only the sending bot can
delete its messages — and removed by a new Bot-1 sweeper job
(restart-safe, non-blocking). `never` keeps the broadcast forever;
`/cancel` aborts a pending broadcast.

---

### v4.0.1 (2026-09-25) — /addbutton multi-button + reply fixes

- `/addbutton` now accepts MULTIPLE buttons in one command — a color word
  ends a button, so
  `/addbutton 💸𝗣𝗿𝗲𝗺𝗶𝘂𝗺💸 | https://t.me/NSFW_Universe/6 | red 🦋𝐁𝐀𝐂𝐊𝐔𝐏🦋 | https://t.me/NSFW_Universe | blue`
  adds both as half-width buttons on one row. (Was wrongly rejecting the
  second label as an "unknown color".)
- Confirmation replies no longer crash with Telegram's
  `unsupported start tag "n"` 400 error; the whole reply is escaped and
  link previews are suppressed.
- `/addbutton`, `/addcovercaption`, `/addfilecaption` no longer crash on
  edited messages (`update.message` can be None — they now use
  `update.effective_message`).

---

## v4.1 (2026-09-25) — shortener failover + outage no-ban

**Failover across providers.** `shortener.shorten` now tries EVERY active
shortener (per-user round-robin start, e.g. vplink then arolink) and uses
the first one that answers — when one provider's server is down the other
takes over automatically.

**Outage = direct delivery, never a ban.** If NO provider can serve (all
down, all paused, or none configured), the gate skips verification entirely
and hands the user the Get File button directly — previously users received
the raw deep link, "solved" it in seconds, and were auto-banned by the
sub-150s anti-bypass rule. Every outage delivers an admin alert naming the
failed provider(s) (check the `/shortenerapi` dashboard).

**Ban rule is now outage-aware.** Each verify token records whether a live
shortener served it (`shortener_state`: `active`/`down`). The instant
sub-150s ban applies ONLY to `active` tokens — tokens issued during an
outage can never ban a user. Zero-tolerance banning for genuine bypass
scripts is unchanged.

---

## v4.2 (2026-09-25) — broadcast flood-limit fix

**Fix — /broadcast silently dropped most users.** The loop sent one
forwardMessage every ~50 ms and counted every Telegram `429 Too Many
Requests` as a permanent failure. On back-to-back broadcasts the Render log
showed 1,059 calls with zero 200 OK, and "Sent" collapsed 349 → 129.
Users were NOT blocking the bot — Telegram was rate-limiting it.

The broadcast loop is now paced (~3 messages/sec, under Telegram's global
limit), honors the `retry_after` value from every 429, and retries the same
user up to 3 times — so every reachable user eventually receives the
message. Only users who still fail after all retries (they blocked the bot
or deactivated) count as failures. Before starting, the bot now tells the
admin the approximate duration; the webhook already answers Telegram
instantly, so the bot stays fully responsive during a long broadcast.

---

## v4.3 (2026-09-25) — /verified_users daily report

**New — `/verified_users` (admin).** A native compact rich-message table
(`InputRichBlockTable`, `is_compact=True` — same proven shape as `/banlist`)
of everyone who verified SINCE MIDNIGHT IST, one row per user:

| # | Name | Elapsed | Category | Link Type | Count |

- **Live capture**: `process_verify` records every successful verification
  (name, username, elapsed seconds, pipeline category, serving shortener).
- **Same user = one row**: repeat verifications bump `Count`, show the
  LATEST elapsed time, and append new categories / link types.
- **Link Type**: the provider that served their link (`vplink`, `arolink`);
  files delivered during a shortener outage (v4.1 failsafe) appear as
  `Direct (outage)`.
- **Daily session, IST**: rows are keyed by IST date; the report always
  reads "today", so midnight starts fresh automatically. A daily 00:00 IST
  job deletes anything older than today+yesterday.
- **`/verified_users yesterday`** shows the previous day's table.
- Falls back to a paged monospace table if Telegram rejects the rich
  payload — the report can never fail to display.

---

## v4.4 (2026-09-26) — dynamic genre browse menu (rich messages)

**New — `/browse` multi-level menu (users).** One inline message that edits
itself in place (ephemeral view): every ENABLED pipeline → its genres →
posted items (10 per page, Prev/Next). Works in DMs and groups; new
pipelines appear automatically. Result buttons deep-link into the existing
`/start file_…` flow, so force-sub, the shortener gate and the Bot 2 token
handoff apply exactly like channel posts — the menu can never bypass
monetization. `/start` also carries a 📂 Browse button.

**New — genre registry (admin).** `/addgenre <pipeline> <genre>` runs a
ONE-TIME background caption scan and persists the matches in MongoDB
(`genres` collection) — the database is never re-scanned for that genre.
New uploads are matched incrementally at index time (in-memory regex per
genre, zero extra queries), and `/rescandb` refreshes all genre lists of
the pipeline once. Matching is format-proof: `#Romance! 💕`, bold, mono,
quotes and emoji all count (captions are stored as plain text), and
whole-phrase matching keeps `romance` from matching `romancer`.
`/delgenre` and `/genres` manage the registry.

**New — `/onbrowse` / `/offbrowse` (admin).** Global switch for the menu
(channel-post Download buttons are unaffected).

Safety: banned users are rejected at every menu level; stale taps on
deleted pipelines/genres re-render the parent menu instead of erroring;
callback data stays under Telegram's 64-byte limit by construction
(genre slugs are capped at 24 chars).

---

## v4.5 (2026-09-26) — /browse becomes true RICH messages + EPHEMERAL views (Bot API 10.3)

**Fix — the v4.4 menu used a standard InlineKeyboardMarkup under a plain
message.** The owner asked for Telegram's Rich Messages with embedded
buttons and ephemeral views; v4.5 implements exactly that.

**Rich messages.** Every /browse level (categories → genres → posted items)
is now an `InputRichMessage` sent via `sendRichMessage` and edited via
`editMessageText(rich_message=…)`: a `heading` block, a `paragraph` block,
and buttons EMBEDDED as `InputRichBlockButtons` rows of `RichMessageButton`
(`callback_data` for navigation, `url` + `style: success` for item deep
links). No inline keyboard markup is attached anywhere in the menu.

**Ephemeral views (groups).** The first /browse render in a group/supergroup
sends with `ephemeral_message_parameters.receiver_user_id` — visible only to
the user who opened it. Taps inside it arrive carrying
`Message.ephemeral_message_id` (read from PTB `api_kwargs`, since PTB has no
rich/ephemeral models) and are edited in place via `editEphemeralMessageText`
(chat_id + receiver_user_id + ephemeral_message_id), per the Bot API 10.3
rule that callbacks FROM ephemeral messages must not use
`replace_callback_query_message`.

**Private chats.** Ephemeral views are a group-only feature per the docs, so
DMs render the same rich message normally (edited with `editMessageText`).

**No library upgrade.** python-telegram-bot ships no rich/ephemeral helpers
(verified against the latest release), so all calls use `bot._post(...)` —
the raw-API path already proven in production by /banlist and
/verified_users. If Telegram rejects the rich call, the user gets a clear
"update your Telegram app" notice (per owner decision: no inline-keyboard
fallback).

Everything else from v4.4 is unchanged: genre registry, one-time backfill
scan persisted in MongoDB, lazy incremental matching, posted-only results,
/onbrowse /offbrowse, ban checks, 64-byte callback budget, deep links into
the gated Download flow.


### v4.5.1 (2026-09-26) — hotfix: rich-text node format rejected by the API

**Fix — every /browse attempt fell into the error path.** v4.5 built rich
text as `{"type": "plain", "text": ...}`; Telegram's Bot API rejects that
shape, so `sendRichMessage`/`editMessageText` returned Bad Request and the
handler wrongly blamed the client ("app too old"). Rich-text nodes now use
the proven production shape `{"text": ..., "entities": [...]}` — the exact
format the live `/banlist` and `/verified_users` rich tables send. The error
handler now also shows the real API error text instead of a generic notice,
so any future failure is diagnosable from a screenshot.
