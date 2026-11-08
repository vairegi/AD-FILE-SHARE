# Funbot — Shortener Tokens, Daily Reminders and REAL Rich Tables v1
Prepared 2026-11-08; base commit 7da79b8e1be44d9ed1dd730ca92cfbd936a133fc.

## Scope and deployment
Only the independent FUN service changes. Bots 1/2, root MongoDB, root
shortener settings, LinkGuard/worker.js and existing gift balances are untouched.
No production DB records were changed and no live Telegram messages were sent.

Merge the ZIP's FUN/ into the EXISTING FUN/ directory. Never create FUN/FUN/.
Commit all runtime replacements/new files together and deploy ONLY Funbot's
Render service. Keep root directory FUN, all existing environment variables,
requirements and the single-process start command. ONE worker/instance only.
Do not deploy any of these files at repository root or into Bots 1/2.

Required runtime files:
- earn_links.py (NEW)
- native_tables.py (NEW)
- handlers.py
- main.py
- runtime.py
- persona.py
- token_admin.py
The other Python files are regression tests. No new dependencies or manual
migrations are required. New collections/indexes are initialized on startup.
The existing gift, check-in, referral, delivery, cover and broadcast code remains.

## First-time setup — private admin/owner chat with FUNBOT
Shortener rewards are OFF with reward 0 by default, so nothing starts minting
before the owner chooses the payout and configures providers.

/shortenerapi add vplink https://vplink.in/api YOUR_KEY
/setperverifytoken 3
/bantimecontrol 150s
/shortener on

Use YOUR real shortener key only in your private admin chat; never put it in
GitHub or this ZIP. Best-effort the bot removes the command containing the key
and never echoes it in status. API keys remain in Funbot's MongoDB settings
collection earn_providers, not in the source. Keys are NOT copied from Bots 1/2.
Provider API addresses must be public HTTPS URLs without embedded credentials,
query strings or fragments (the bot supplies api/url/format parameters).
A provider's base URL must match its VPLinks/GPLinks-style link-creation API.

/shortenerapi                  — list the rotation, with keys hidden
/shortenerapi list             — same dashboard
/shortenerapi add SITE HTTPS_API_BASE KEY
/shortenerapi pause SITE       — skip new links; keep saved settings
/shortenerapi resume SITE      — resume new links
/shortenerapi remove SITE      — remove from the rotation
/shortener on|off|status       — enable, pause NEW links, inspect configuration
/setperverifytoken N           — reward for NEW links (0 pauses new rewards)
/setperverifytoken             — show reward
/bantimecontrol 150s           — minimum wait, root-style syntax
/bantimecontrol 3m             — minutes also supported (10s..3600s)
/bantimecontrol                — show current minimum; NO AUTO-BANS

Existing valid pending links KEEP their promised reward and timer, even if an
admin changes those settings, disables new earning or pauses/removes a provider.
Configuration changes do not silently invalidate a user's already-completed
shortener work. A pending link expires two hours after issuance. The minimum
wait starts at issuance, AFTER the provider request, not before HTTP delays.

## User flow and limits
Open Earn Tokens -> Solve & Earn, or /solve.
The screen shows the selected provider, promised reward, minimum wait and expiry.
Open the shortener, complete its steps, then follow its final Telegram link.
There is no manual Claim button that can mint tokens without a valid return.

The return is a cryptographically random, 32-hex-character, user-bound, expiring,
one-time attempt. Another Telegram account cannot redeem it. Replays cannot pay
again. A return before the minimum wait is told to wait/retry, with NO ban and no
balance deduction. Reopen the final Telegram link after the displayed wait.

Exactly FIVE rewarded returns per provider in ANY rolling 24 hours per user:
- One provider -> up to five rewards in its rolling window.
- Two providers -> up to ten, five each, alternating eligible providers.
- Successful returns advance the per-user rotation alphabetically through active
  eligible provider names. Exhausted/paused providers are skipped.
- Other users have independent quotas/rotation.
- No midnight reset. Each completion stops occupying a slot exactly 24h later.
- Clicks, failed API calls, early returns and expired attempts do NOT consume uses.
- One pending link per user: repeated taps reopen the SAME unfinished link.
- If a provider cannot create a link, try the next eligible provider.
- If ALL fail, withhold the reward and invite a later retry. Never expose a
  direct-reward fallback just because a shortener is down.
- Removing and re-adding the SAME site name does not reset its rolling history.
  Renaming a site counts as a new provider: keep names stable.
- Safety bounds: at most 100 configured provider names, reward 0..1,000,000.

## Important agreed security limit
This release intentionally does NOT connect LinkGuard or authenticated provider
completion callbacks. Its checks prove that a valid link returned to the right
Telegram user after a minimum wait, NOT that the ad network counted a paid view.
Someone who extracts the final link and waits may still obtain the reward. The
owner explicitly accepted this temporary limitation. It is NOT bypass-proof.
No automatic bans, root `_g` trust shortcut, or automatic direct-credit fallback
were copied from Bots 1/2. Future LinkGuard integration should preserve the same
atomic payout logic and add stronger server-side completion evidence.

## Reminder timing and persona
After each successful accepted return at time T, the next reminder is T+24h.
Example: completion 10:00 AM IST -> next reminder 10:00 AM IST next day.
If the user instead completes at 2:00 PM, subsequent reminders reset to 2:00 PM.
Every success resets the schedule, including extra solves on the same day.

If no new success occurs, remind every 24h on that anchor until one does.
Seven different cute Senpai reminder messages rotate. The variant counter is
NOT reset by a successful solve, so daily completers don't get the same text
every day. All notices include styled Solve & Earn, My Balance and Main Menu
buttons; link, success, wait, quota, expired and failure screens use the same
persona. The footer explains the five-per-provider rolling rule.

Reminders start ONLY after the user's first rewarded return; existing users
are not mass-messaged on deployment. When rewards are disabled, payout is zero,
or no providers are active, reminders pause. Wallet credits never depend on
reminder delivery. Blocked/invalid chats stop scheduled alerts until another
successful solve creates a fresh schedule. Network-uncertain sends are not
blindly resent. Flood waits are respected with up to five attempts. Missed days
during downtime produce one due reminder, not a backlog burst.

The background worker checks indexed due records every 30 seconds in bounded
pages, with short gaps between messages. Delivery is approximate: up to the poll
interval plus queue/network delays while the service is awake. No code can send
while a Render service is asleep/down; for punctual reminders keep Funbot running
on an always-on service. Telegram delivery itself is not exactly-once.

## /userstoken — actual native table, not an image or monospace text
/userstoken [page]
/users token [page]             — alias; /users tokens also works

The directory now sends sendRichMessage with InputRichMessage.blocks and a real
InputRichBlockTable, with bordered, striped, compact rows and native linked names.
Columns: # | Name / User ID | Tokens | Friends | Unlocked Posts.
Friends = qualified referrals, with pending invites shown separately.
Unlocked Posts = unique collection unlocks in the user's vault (including owned
collections later removed from the feed), NOT number of re-streams or files sent.
Balances/referrals/unlocks are queried fresh for each paginated 10-user page.
Previous/Next/Refresh stays restricted to the issuing private-chat admin.
User names are literal RichText strings; they cannot inject HTML/table blocks.

Source schema checked in the official documentation:
https://core.telegram.org/bots/api#inputrichblocktable
https://core.telegram.org/bots/api#richblocktablecell
https://core.telegram.org/bots/api#sendrichmessage
https://core.telegram.org/bots/api#editmessagetext

The pinned python-telegram-bot 22.8 lacks typed native table methods. The small
native_tables bridge uses that SAME SDK's authenticated pooled request transport
with documented Bot API JSON fields; it does not upgrade unrelated delivery
code or manually assemble token-bearing URLs. Requires the modern public Bot API
(or an up-to-date self-hosted server); use an updated Telegram app. Compact
spacing is a Bot API 10.3 field; native input table blocks were added in 10.2.
If an endpoint rejects rich tables, the bot explains the problem rather than
silently substituting the old fake table. Real rendering on your phone must be
checked after deployment; offline tests cannot prove a client's visual layout.

## Database safety and recovery
New data: earn_providers (provider keys/settings), earn_attempts (attempt receipts),
earn_notices (reminder delivery states), earn.* fields on existing user wallets,
and deterministic transactions with reason shortener_reward.

A SINGLE conditional MongoDB wallet update increments tokens, records rolling
usage, advances rotation, sets the next reminder and creates a temporary credit
marker. Each attempt has a durable state plus a deterministic ledger ID.
A restart/DB outage between credit and ledger writes is reconciled without paying
twice. Markers are removed only AFTER durable completion/ledger writes, keeping
normal user documents from accumulating an unbounded map of solved links.
Old wallet credits are never changed when a late ledger recovery runs.
Concurrent gifting/spending/earning uses increments rather than overwriting the
balance. Gift commands and their independent recovery state are unchanged.

Do NOT delete earn_attempts, transaction rows, earn.history, earn.version, or
in-progress earn.applied markers to retry an operation. Those protect quotas and
prevent duplicate minting. No production reset, rescan or queue purge is needed.
Attempt and ledger audit receipts intentionally have no TTL cleanup in v1.

## Sandbox test results
Untouched base: 533 Funbot tests passed.
Final full suite: 620 tests passed (533 existing plus 87 new).
Focused earning/gift/directory suite: 145 tests passed against real MongoDB 8.0.16
on localhost in unique, disposable databases; also passed with mocked MongoDB.
The final ZIP is extracted on a clean checkout and these tests are rerun there.

Coverage includes: admin permissions/arguments, keys not echoed, provider add,
pause/resume/remove, public-HTTPS endpoint checks, text/JSON API parsing, URL
encoding, transient retry and Retry-After, failure fallback with NO credit,
per-user rotation, five-per-site quota and EXACT rolling boundary, stable pending
links, wrong-user/forged/early/expired/replayed returns, legacy suffix rejection,
crash after wallet-before-ledger, acceptance recovery after expiry, stale state
replay, concurrent gifts/spending/earnings, success-notice timeout, exact 24h
reminder math/late shift, repeated daily alerts and seven styles, duplicate sends,
reminder/completion races, flood waits, blocked chats, ambiguous-send restart,
actual native table fields and unique unlock counts, alias/ownership/pagination,
unsupported-server errors and REAL PTB request serialization offline.

Two existing lifecycle counts changed from four to five background workers.
Existing table-specific tests now inspect native rich blocks instead of <pre>
HTML, while retaining pagination, permission, Unicode and gift coverage.
Dependency deprecation warnings remain; no final tests fail.

Re-run from FUN/: install requirements-test.txt; python -m pytest -q.
Real LOCAL DB: run disposable MongoDB at 127.0.0.1:27019, then
FUN_EARN_REAL_MONGO=1 FUN_GIFT_REAL_MONGO=1 python -m pytest -q test_earn_links.py test_token_admin.py
These fixtures use ONLY localhost and never use a production MONGO_URI for tests.

Not performed: live paid-shortener API calls with real keys, live Telegram sends,
actual paid-ad completion, real Render deployment, or real-phone table rendering.
Do not interpret offline passing tests as a guarantee that a provider credits
revenue or that an arbitrary client/server version supports rich messages.

## Post-deployment smoke check
1. Open /admin, /shortenerapi and /userstoken in your private admin chat.
2. Configure one provider, reward and timer, enable /shortener on.
3. In a test wallet, /solve -> finish -> return: exactly N tokens added once.
4. Reopen the same return: no second credit. Try an early return: wait, no ban.
5. Add a second provider and check the rotation; don't mint artificial production
   rewards just for testing unless you intend those credits.
6. Confirm the directory is a REAL native table with unlocked-post counts.
7. Check reminder behavior with the documented 24h schedule on an awake service.
