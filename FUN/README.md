# FUN Bot 3 — v1.2.0 cover previews + /help (2026-10-17)

This folder is an independent service. Bots 1–2 at the repository root are
unchanged. All source, tests and setup tools are directly inside FUN/.
Python creates __pycache__ automatically at runtime; it is not shipped.

## IMPORTANT: upgrading from the previous nested package
GitHub “Add files via upload” does NOT delete old files.
1. Upload the contents of the new FUN folder to the repository's FUN folder.
2. Delete these OLD paths in GitHub before deploying:
   - FUN/handlers/__init__.py (then the empty handlers folder disappears)
   - FUN/rich/cards.py
   - FUN/tests/test_bot3.py
3. Do NOT delete the new FUN/handlers.py or FUN/test_legacy.py.
4. Deploy the latest commit. The app refuses to start if the old handlers
   package still exists, because Python would otherwise import it instead
   of the repaired flat handlers.py.

## v1.2.0: cover previews on Start Watching + /help

**The cover pipeline is now wired in.** When a channel is scanned, each
collection's cover photo (or a short video cover's thumbnail) is downloaded
via the userbot ONCE, uploaded to telegra.ph, falling back to graph.org if
telegra.ph fails. The resulting direct image URL is validated and **cached in
MongoDB**, so browsing never re-downloads or re-uploads a cover.

**Start Watching now shows the cover.** The feed card renders as a rich
message with a swipeable **slideshow** (default) or a combined **collage** of
the cover, using Bot API 10.1+ rich messages. Fallback chain, so the feed is
never blank:
1. Hosted URL + rich card accepted  → slideshow / collage card
2. Rich rejected by the client      → normal photo message (same cached URL)
3. No hosted URL (both hosts down)  → plain text card (old behaviour)

- `/covermode slideshow` (default) — swipeable cover preview.
- `/covermode collage` — combined grid preview.
- `/retry_covers` — re-host covers whose upload failed earlier (e.g. when
  telegra.ph was down). Failures are cached as `failed` so rescanning does
  NOT hammer the hosts; this command resets them to retry.
- `/help` — the Cute Assistant guide card (same as the Help Guide button),
  with Start Watching / Profile / Main Menu buttons.

After scanning, use `/collections` to see indexed drops, then `/start` →
**Start Watching** to browse with covers. Existing collections scanned before
this release have no hosted cover yet — run `/retry_covers` (with the userbot
connected) to host them without re-scanning files.

## v1.1.1: simple owner-chat userbot setup
Send /setup_userbot in YOUR private bot chat. The bot asks for:
1. API ID (numbers).
2. API hash (32 hexadecimal characters).
3. Your existing authorized Telethon user-account StringSession.
It validates and connects BEFORE reporting success. Then /userbot_status
checks both transport connectivity and session authorization. /remove_userbot
disconnects and removes saved credentials; /cancel discards partial setup.

No API_ID, API_HASH or SESSION_ENCRYPTION_KEY Render variables are required.
Those three can be removed after deploying this update. No encryption-key
generation, browser form, phone number, login code or password is needed.
Keep BOT_TOKEN, OWNER_ID, MONGO_URI, WEBHOOK_SECRET and RENDER_EXTERNAL_URL.
This is still a separate FUN service; root Bots 1–2 are not modified.

Setup is owner-only and private-chat-only; expires after ten minutes. Invalid
input stays on the current step. API ID/hash/session messages are deleted on
a best-effort basis; failure produces a warning to delete the message manually.
Bot chats are NOT end-to-end encrypted; deleting messages is not a guarantee
of erasing notifications, screenshots, platform data or previously saved copies.
Use only your own dedicated account, and never send login codes/passwords.

Finished credentials are encrypted in MongoDB. The encryption key is derived
automatically from the existing secret MONGO_URI plus a random salt; no new
key environment variable or plaintext account session is stored in MongoDB.
Changing the exact MONGO_URI string requires re-importing the userbot via
/setup_userbot. Treat Render secrets and MongoDB access as highly sensitive.
A full compromise of the Render service can recover the saved credentials.

The colored keyboards, selective HTML formatting, automatic referral username,
Share on Telegram button, scanners and cleanup improvements from v1.1.0 remain.

## SECURITY: token seen in old Render logs
If your logs include /bot<TOKEN>/getMe or similar URLs, revoke that BOT_TOKEN
in BotFather and replace it in Render. Do not paste tokens, StringSessions,
API hashes, encryption keys or login codes into support chats or GitHub.
No production credentials or Render logs are included in this package.

## Render configuration
Root Directory: FUN
Build Command: pip install -r requirements.txt
Start Command (copy ONLY this command; do NOT prepend “start:”):

    uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1

Required environment variables:
- BOT_TOKEN: the newly rotated BotFather token.
- OWNER_ID: your Telegram numeric user ID.
- ADMIN_IDS: optional comma-separated additional admins; owner is included.
- MONGO_URI: Bot 3's own MongoDB connection string.
- MONGO_DB_NAME: Bot 3's database name (default fun_bot3).
- WEBHOOK_SECRET: a unique random secret, NOT the default fun_secret.
- RENDER_EXTERNAL_URL: your full HTTPS Render address, no trailing slash.
- SESSION_ENCRYPTION_KEY: NOT required for v1.1.1 owner-chat onboarding.

Optional:
- BOT_NAME: display branding; set it to “Relax Time” if that is your chosen name.
- BOT_USERNAME: optional fallback, without @. getMe normally supplies it.
- API_ID/API_HASH: NOT required; enter them one at a time in the owner chat.
- REQUIRED_CHANNEL_IDS: optional initial membership-gate IDs; /reqchannel
  stores your subsequent changes in MongoDB.

Keep the process at ONE worker. Do not deploy a second service with the same
bot token or connect two live processes using the same Telethon session.

## 1. Deploy this update
Upload all files from FUN/ into the repo FUN/ directory. Render Root Directory
remains FUN; Build Command and Start Command stay as shown above. This package
is flat (no nested folders). If upgrading from a very old release, remove the
obsolete handlers/, rich/ and tests/ directories as described above.
Save/deploy, then remove API_ID, API_HASH and SESSION_ENCRYPTION_KEY if present.
Do not remove MONGO_URI or the other required service settings.

## 2. Connect and remove your userbot
In your private bot chat, from the account matching OWNER_ID:

    /setup_userbot

Send API ID when asked, then API hash, then the StringSession you already
generated on your laptop. Wait for the connection result. No need to generate
a new session if the existing one remains valid. Commands:

    /userbot_status
    /cancel
    /remove_userbot

After success, credentials persist across redeploys and reconnect on startup.
Starting /setup_userbot again resets its prompts but keeps an existing saved
userbot until a new connection is validated. A changed MONGO_URI needs re-import.
Removing stored credentials does NOT revoke your Telegram account session
server-side. Use Telegram Settings > Devices to revoke it when desired.

## 3. Add a database channel and scan it
Bot 3 does NOT publish posts into source channels. These are read/copy sources.
- Join the database channel using the dedicated userbot account.
- Add Bot 3 to the same channel (admin is the simplest setup) so the Bot API
  can copy its original files. Userbot access alone is not enough for copying.
- Source channels with protected content can restrict forwarding/copying.
- Use the channel's actual -100... numeric ID (not an invitation URL).

Example (REPLACE the sample ID with your real channel ID):

    /addchannel -1001234567890 movies
    /scan -1001234567890
    /channels
    /collections

Use one-word category keys, such as movies or hindi_movies, without colons.
Map another channel the same way with its own category. /scan runs in the
background and reports when it finishes. /scan <id> full revisits history
without deleting purchases/counters/owner prices. /pending lists files before
a recognizable cover. Do not manually edit MongoDB purchase records.
After the initial scan, the worker checks for new source posts every 60s.
These jobs run only while Render is awake; they resume after a restart.

Cover rule: photo/video with at least THREE non-empty raw caption lines is a
cover. A 7-, 10- or 20-second duration alone does NOT decide. A captionless
9-second video is a delivery file. Source album groups are classified as
one unit. Covers are excluded from unlocked delivery files.

## 4. Required joining, referrals, and sharing
Add Bot 3 as admin in the required subscription channel to reliably check
membership. For a private required channel, supply an existing invite URL:

    /reqchannel -1009876543210 https://t.me/+YOUR_INVITE

Repeat to add more required channels. /reqchannel off disables the gate.
The user gets join buttons and an “I've Joined” recheck. Both paid unlocks
and free re-streams check membership; purchased access is never deleted.
A referred new account gets 8 tokens (5 welcome +3 bonus). The inviter gets
+5 once the referred user passes membership checks. Notification failures
never repeat the balance credit. Old users cannot farm a new joining bonus.
“Share on Telegram” opens Telegram's recipient picker; nothing is sent until
the user chooses a recipient and sends it.

## 5. UptimeRobot
Monitor https://YOUR-SERVICE.onrender.com/health every five minutes.
GET and HEAD are supported. Pinging can reduce idle sleep, but cannot
eliminate free-tier restarts, allowance limits or network outages. Ten-minute
deletion can be late during downtime; remaining jobs retry after startup.

## Economy (unchanged rules)
Welcome 5; collection price 2; /checkin +3 per IST calendar day, +1 on every
fifth consecutive day. A missed day resets the streak. Each paid collection
has permanent vault access and free re-streams. Save is a separate bookmark.
/setprice <collection_id> <n> can override a single collection's price.

## Tests and honest verification boundaries
Install: pip install -r requirements-test.txt
Run:

    pytest -q test_repairs.py test_chat_setup.py test_covers_feed.py
    python3 rich_verify_test.py
    python test_legacy.py

These run offline against an in-memory MongoDB mock and mocked external
services. They exercise real PTB serialization of style fields, HTML card
structure, referral/share URL encoding, setup validation/encryption, the full three-step chat flow,
owner/private checks, cancellation, removal, reconnect and legacy secure-page authorization, FastAPI routes, job registration, scans and
economy/delivery failure paths. They do NOT establish live Telegram client
rendering, real Atlas latency, real userbot login success, endpoint uptime,
or sponsor-provider completion. No production credentials were used.

## Existing scope limitations, not hidden by this repair
This repair fixes menus, onboarding, source scanning and referral sharing.
The old release incorrectly described every feature as fully wired:
- The collage/slideshow cover pipeline IS now wired (scan-time hosting +
  cached URL + rich card render). Offline tests cover payload shape, fallback
  chain, scan-time caching and the /covermode toggle. Live rendering of rich
  slideshow/collage blocks depends on the user's Telegram client version and
  can only be confirmed on a real device after deploy.
- The existing sponsor adapter accepts custom HMAC callbacks. It is NOT a
  working integration with every commercial shortener just by adding a URL.
  Tasks without a configured verification secret stay hidden. Provider-specific
  completion integration still needs implementation/testing with that provider.
- copyMessages preserves source albums; it does NOT turn separately posted
  source messages into new albums. The delivery count is the actual confirmed
  message count. Unknown send outcomes are flagged, not blindly retried.
- Purchase grant/refund spans separate database writes. Single-process
  serialization prevents duplicate taps, but a hard crash between writes
  can still require operator reconciliation; this is not a cross-collection
  MongoDB transaction implementation.

Use the repair as an upgrade for the requested issues, not as proof that
unrelated unfinished features in the previous zip are production-ready.
