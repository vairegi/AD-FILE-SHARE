# FUN Bot 3 — v1.1.0 repair release (2026-10-17)

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

## Fixes in this release
- Real Bot API button styles: Start Watching/Unlock red (danger), Categories/
  Re-Stream/join/share green (success), other navigation blue (primary).
  Colors are fields in the serialized keyboard, not text or emoji tricks.
- Selective HTML: bold heading and labels; monospace numbers/values; italic
  cute narration, with escaped dynamic titles/user names. Button text itself
  is plain Telegram button text; HTML formatting applies to message cards.
- Referral identity resolves automatically from getMe; never emits t.me/?start.
  Share on Telegram uses the official URL-encoded t.me/share/url flow with
  “Stream exclusive media now!” as accompanying text.
- Validate Fernet keys BEFORE asking for a session. API ID/hash and session
  are imported via an owner-only, expiring, single-use HTTPS form. They are
  encrypted together in MongoDB. The separate Fernet key stays in Render.
- Setup checks account authorization before replacing saved credentials.
  No bot-chat login codes/passwords/session capture. Setup bearer is in the
  URL fragment, not HTTP query logs. Failed connections do not replace data.
- Background /scan, persisted incremental scanner state, album-aware cover
  boundaries, stable collection IDs, no duplicate appended files on rescan.
- Registered automatic cleanup and incremental polling jobs. Expired deletion
  jobs survive restarts; transient deletion failures retry instead of vanishing.
- Fixed stale balances/streaks, separate Vault/Bookmarks screens, re-stream
  membership checks, duplicate welcome/check-in/referral credits, and unsafe
  blind retries after uncertain Telegram delivery timeouts.
- Suppress HTTP client URL logs that can expose the bot token. GET/HEAD / and
  /health return 200 (the previous log showed HEAD / returning 404).

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
- SESSION_ENCRYPTION_KEY: a valid Fernet key generated as below.

Optional:
- BOT_NAME: display branding; set it to “Relax Time” if that is your chosen name.
- BOT_USERNAME: optional fallback, without @. getMe normally supplies it.
- API_ID/API_HASH: only needed to load a legacy v1 session. New secure setup
  stores them encrypted in MongoDB, so these two env vars are not required.
- REQUIRED_CHANNEL_IDS: optional initial membership-gate IDs; /reqchannel
  stores your subsequent changes in MongoDB.

Keep the process at ONE worker. Do not deploy a second service with the same
bot token or connect two live processes using the same Telethon session.

## 1. Fix the Fernet error
A Fernet key is NOT an API hash, bot token or arbitrary password. It encodes
32 random bytes in URL-safe base64 (normally 44 characters, often ending =).
On YOUR trusted computer, with cryptography installed, generate a fresh key:

    python setup_tools.py key

Or:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Copy the complete output into Render's SESSION_ENCRYPTION_KEY, no surrounding
quotes. Save/redeploy. Do not share that output. Keep a private backup.
Do not change a valid key after credentials have been stored: the old key is
needed to decrypt them. If the old key is lost, re-import a new session.
For WEBHOOK_SECRET, python setup_tools.py webhook-secret produces a secret.

## 2. Connect the userbot
1. Open /setup_userbot in your private chat with Bot 3 as OWNER_ID.
2. If the encryption key is invalid, the bot explains how to fix it first.
3. Open “Open Secure Setup”. The owner-only link expires after ten minutes;
   a newly requested setup link invalidates the previous link.
4. Enter API ID and API hash from https://my.telegram.org and an EXISTING
   authorized Telethon StringSession for your dedicated user account.
5. The page validates, connects and encrypts the credentials in MongoDB.
6. Run /userbot_status. Connected should say yes.

If you need to create a StringSession, install Telethon on your trusted
computer and run python setup_tools.py session. Its prompts run locally,
not in the bot. Import its secret output only on YOUR setup page. This tool
has no need for your BotFather token. Telegram Devices can revoke the session.
/remove_userbot disconnects and removes the stored credentials; it does not
revoke the account session at Telegram. Revoke in Devices when appropriate.

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

    pytest -q test_repairs.py
    python test_legacy.py

These run offline against an in-memory MongoDB mock and mocked external
services. They exercise real PTB serialization of style fields, HTML card
structure, referral/share URL encoding, setup validation/encryption and
single-use authorization, FastAPI routes, job registration, scans and
economy/delivery failure paths. They do NOT establish live Telegram client
rendering, real Atlas latency, real userbot login success, endpoint uptime,
or sponsor-provider completion. No production credentials were used.

## Existing scope limitations, not hidden by this repair
This repair fixes menus, onboarding, source scanning and referral sharing.
The old release incorrectly described every feature as fully wired:
- The previous rich preview builder and cover-upload helpers were not called
  by the feed. This release does not claim the live collage/slideshow preview
  pipeline is complete; cover-host helper tests alone cannot prove that.
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
