# FUN Bot 3 v1.5.0 — direct-file hosts + Catbox (2026-10-18)

All files remain directly inside FUN/. Bots 1–2 are not changed.
No articles, page titles, createPage calls, automatic Telegraph accounts or
Render-served images are used. Only the small prepared cover JPEG is uploaded.

## Verified facts, not another claim that the remote host is fixed
Live tests from this sandbox using a harmless 320x180 synthetic JPEG returned:
- https://telegra.ph/upload: HTTP 400, "Unknown error"
- https://graph.org/upload: HTTP 400, "Unknown error"
- https://catbox.moe/user/api.php: HTTP 412, "Invalid uploader"
The Catbox request was also tried with requests instead of httpx: same 412.
These results do NOT establish the reason for each service's refusal, nor
prove uploads from your Render account will get the same result. They DO mean
we cannot honestly claim a working external URL was obtained in the live test.
A timeout, 400, 412 and a 429 are different errors; do not call them all flood
limits. Telegraph account tokens are not an established fix for /upload.
The official Telegraph API does not document /upload authentication.

## Correct direct-file API requests
Telegraph/Graph: POST https://telegra.ph/upload then https://graph.org/upload
with multipart field file=(cover.jpg, JPEG bytes, image/jpeg). Responses are
JSON arrays such as [{"src":"/file/...jpg"}]. Join src to the host URL.
Posting to the Graph homepage, https://graph.org, is not the upload API.

Catbox: POST https://catbox.moe/user/api.php with form reqtype=fileupload and
multipart field fileToUpload=(cover.jpg, JPEG bytes, image/jpeg). The response
is a PLAIN-TEXT direct URL, e.g. https://files.catbox.moe/abc123.jpg — not JSON.
Posting to the Catbox homepage is not the upload API. Anonymous is the default;
the userhash field is omitted, as the official API specifies. If you already
have a Catbox account, optional Render secret CATBOX_USERHASH may be supplied.
It is not guaranteed to fix a provider's refusal. Never paste it into logs.

The clean asynchronous implementation is in cover_hosts.py (httpx-compatible
client). covers.py handles MongoDB image caching and orchestrates the adapters.
No image upload/download occurs on a normal Next/Previous tap with a cached URL.

## Behaviour changes
- Host order: telegra.ph -> graph.org -> Catbox. A rejection/rate limit on one
  host does not stop the next independent host being tried.
- Correct Catbox URLs are accepted by the Telegram preview renderer; merely
  uploading to Catbox without fixing the old domain filter would not work.
- Returned URLs are checked for expected scheme/domain/path and stored in Mongo.
  A plain-text error or HTML page returned with HTTP 200 is NOT a hosted URL.
- Successful URLs are reused on rescans/retries (even force=True); old cached
  JPEGs are used for pending hosting retries instead of re-downloading sources.
- Provider-level cooldowns prevent 162 requests to an endpoint known to reject
  the request. When all hosts refuse, the job pauses with clear per-host errors.
- Rate retries use an INNER loop, retrying the same collection before advancing.
- Metadata is read before upload waits; a long job does not leave a MongoDB
  cursor idle through host cooldowns (which can abort after the first 101 rows).
- Job totals separate attempted, actually hosted, cached-photo-only, failures,
  and not attempted. Zero hosted links is explicitly reported as a failure,
  not as successful link-preview preparation.
- No Render cover routes are enabled; the old link_previews.py is an external-
  URL-only compatibility shim so uploading this release overwrites stale code.
- /covermode was incorrectly registered to a token command in the old release;
  command routing is fixed.

## Feed behaviour
With a valid direct URL: editMessageText + HTML invisible-link anchor and
LinkPreviewOptions(url, prefer_large_media=True, show_above_text=True).
If the current card is an OLD PHOTO, it must be converted into a TEXT message
once: send the link-preview card successfully, then delete the old photo.
Continuing to edit that photo with editMessageMedia would keep it a photo.
Subsequent text-card navigation edits in place. Telegram's edited marker can
appear; live client preview generation and sub-0.5-second speed are not promised.
If every public host rejects the upload, the existing cached photo/file_id
fallback remains available. A photo fallback is clearly NOT a link preview.
The previous plain Previous/Unlock/Next labels and compact caption are preserved.

## Deploy
1. Upload ALL files from this ZIP's FUN/ into the repository's FUN/ and redeploy.
2. No DB reset, new required variable, rescan, or session re-import is needed.
3. Run /retry_covers, then /cover_status. Existing cached covers can be uploaded
   without a connected userbot; only missing source images require the userbot.
4. Catbox links ready > 0 or Telegraph links ready > 0 confirms direct URLs
   were actually accepted/stored. A cached-image count does not confirm hosting.
5. Send /start -> Start Watching; normal text cards update in place.
6. If Catbox also returns HTTP 412 Invalid uploader from Render, there is no
   valid public URL to preview. Do NOT repeatedly retry 162 uploads or assume
   a token/format change fixes it. Check the provider/account or use a host
   that accepts uploads from your network. This release cannot force acceptance.

All three are public third-party hosts. Running /retry_covers publishes cover
JPEGs to them, not the full delivery videos. Only upload material you may share,
and comply with the hosts' policies. Anonymous uploads may not be manageable
or deletable later; the bot does not guarantee their lifetime or confidentiality.

## Test results
python -m pytest -q  (after pip install -r requirements-test.txt)
python test_legacy.py
TEST_RESULTS.txt contains the exact final sandbox and live-probe results.
Mocks verify integration logic, not actual third-party acceptance or Telegram
client rendering. No production DB, account session or user media was used.

References:
https://catbox.moe/tools.php
https://telegra.ph/api
https://core.telegram.org/bots/api#linkpreviewoptions

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

    python -m pytest -q
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
This release changes cover hosting and preview integration; unrelated limitations remain.
The old release incorrectly described every feature as fully wired:
- The pipeline has regression tests for 162 cached/no-hosted-URL rows and real
  PTB callback binding/request serialization, not just permissive fake q.bot
  objects. These are offline tests, not a claim that external hosts or every
  Telegram client were live-tested. Source access and host acceptance must
  still be confirmed after deployment with /cover_status.
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

## Credential safety after sharing a MongoDB URI
Never commit a live URI/password. Rotate any password shared in a support
conversation, update MONGO_URI in Render, and then repeat /setup_userbot:
the simplified userbot credential encryption derives its key from MONGO_URI.
Changing the URI therefore requires a fresh import of your existing session.
Do not send the replacement password to support. This ZIP has no live URI,
Render log, userbot session, account API hash or production media.
