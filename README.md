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
3. Every day at `post_time`, a cover (thumbnail + caption + **Download** button)
   is posted to the public **Posting Channel**; the item is flipped to `posted: true`.
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
   /shortenerapi https://vplink.in/api
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
| `/broadcast <message>` | message every known user |
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
| `/shortenerapi <url>` | set the shortener API base |
| `/setverifytime <hours>` | verification validity (default 6) — per category |
| `/settokenttl <minutes>` | Bot 1 → Bot 2 handoff token TTL |
| `/shortenermsg <text>` | landing/overlay heading |
| `/shortenerbotmsg <text>` | DM text prompting the shortener |
| `/verifymsg <text>` | text shown after verification |
| `/shortenerbtn <label> \| <url>` | add a secondary button |
| `/clearshortenerbtns` | remove all secondary buttons |

### Bot 2 — user

| Command | Effect |
|---|---|
| `/start` | welcome / handles `deliver_*` links |
| `/setautodelete <time>` | personal auto-delete override |

Durations accept `30min`, `2hour`, `12hour`, `1day`, `7day`, `never`
(a bare number means minutes).

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
