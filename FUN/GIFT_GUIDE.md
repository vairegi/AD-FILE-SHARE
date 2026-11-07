# Funbot — Admin gifts and token directory v1

Prepared 2026-11-07. Base commit: 10e48fc4e264bc6229ed3bcbaf78f0a55cf722a7.
Scope: independent FUN service only. Bots 1/2, root shortener/LinkGuard and
bandwidth fixes are not modified. No production database or Telegram writes
were performed during development or testing.

## Install
The archive preserves repository-relative FUN/ paths. Merge these files into
EXISTING FUN/; do not create FUN/FUN/. Five runtime files must ship together:
- FUN/token_admin.py (NEW; required)
- FUN/handlers.py
- FUN/main.py
- FUN/runtime.py
- FUN/persona.py

The other Python files in the ZIP are regression tests. GIFT_GUIDE.md is this
guide. Upload/commit together, then deploy ONLY the Funbot Render service.
Keep its root directory FUN, existing build command, start command, environment
variables, Mongo database name and ONE Uvicorn worker/instance. No dependency
changes, no secret changes, no manual migrations or rescans are required.
New indexes and gift job/receipt collections are created automatically.
Existing check-in, referral, unlock, broadcast and cover behavior stays intact.

## Commands — admin/owner PRIVATE chat only
/gift 123456789 5
  Creates a durable gift job for that existing started user; adds five tokens
  to their wallet, without deducting tokens from the admin. Requires the user
  to have completed /start (welcomed=True), not just an arbitrary Telegram ID.
  Sends a styled Senpai gift alert with gift amount, current balance, and
  Start Watching / My Balance / Earn Tokens buttons.

/gift all 8
  Shows the count of eligible started users, amount per user and total planned.
  Press Send gifts to confirm, or Cancel to leave balances untouched.
  Confirmation is private, bound to the issuing admin and expires after
  10 minutes. Other admins cannot confirm someone else's preview. Recipient
  eligibility is cut off at preview creation: users starting afterwards are
  not included. Admins who started the bot are included as users too.
  Already-started users with blocked chats still receive wallet credit, but
  Telegram cannot deliver their alert. The summary distinguishes both outcomes.

/gift status
  Latest gift job for the issuing admin: preview/queued/running/finished/cancelled,
  planned users, credited wallets, alerts sent, blocked, failed, skipped and
  uncertain notification counts. Command replay is not a new gift. Sending a
  NEW /gift message intentionally creates a NEW gift and adds more tokens.

/userstoken
/userstoken 2
  Paginated Telegram-native rich table, 10 users per page, sorted by balance
  descending with a stable user-ID tiebreak. Columns: #, Name, Tokens, Friends.
  Friends means QUALIFIED referrals, consistently with the existing Funbot
  profile/reward rules. Pending referrals are listed separately beside the
  tappable profile links and user IDs, so pending invites are not mistaken for
  rewarded friends. All started users remain accessible via Previous/Next.
  Refresh and page navigation re-read balances; no cached balance values.
  Names are shortened only in the aligned table, HTML-escaped, with longer
  tappable names/user IDs below it. This is native HTML text, NOT an image.
  Table navigation is restricted to the issuing admin's private chat.

Commands are listed in /admin. New non-admin users do NOT gain gift privileges.
Amounts must be positive whole numbers, 1..1,000,000,000. Fractional, negative,
zero, malformed, unknown-user and group-chat requests cannot mint tokens.
A conservative maximum balance prevents numeric overflow; skipped wallets
are counted separately. All-user gifts run in bounded background pages with
rate-aware alert sending, without blocking normal bot update processing.
The worker wakes on a gift command/confirmation, with a 30-second bounded
recovery poll when idle. Do not run multiple Funbot processes.

## Reliability and recovery
The wallet increment and per-job credit marker are a SINGLE atomic MongoDB
update. Gifts have durable jobs/checkpoints, per-recipient receipts and a
transaction ledger reason admin_gift (amount, issuing admin, gift job ID).
Retries and restarts reconcile missing ledger/receipt writes without paying
twice, even if the user spent tokens after the initial wallet credit.
DO NOT clear gift_credits from user documents or delete gift_jobs / gift_receipts
or admin_gift ledger rows as a retry mechanism; those records prevent duplicates.
Each gift leaves a small permanent marker on its recipients' user documents.

A failed alert never rolls back wallet credit. Telegram flood waits are deferred
and attempted at most five times. A timeout/disconnect or restart during an
in-flight Telegram send can leave delivery uncertain; ambiguous alerts are NOT
resent automatically because Telegram does not offer exactly-once sendMessage.
Uncertain means the user may or may not have received the notice, NOT that their
wallet credit is uncertain. Credits are recoverable independently. A DB outage
leaves the job queued for later recovery; /gift status remains the audit view.

## Verification
Untouched baseline: 475 Funbot tests passed.
Final full Funbot suite: 533 passed (includes 58 new gift/directory tests).
The 58 new tests also passed against MongoDB 8.0.16 on localhost in uniquely
named disposable databases. No production URI was used by tests.
Two existing lifecycle assertions were updated from 3 to 4 background workers
and now explicitly check the gift coroutine plus shutdown cleanup.

Tests cover private owner/admin permission, forged callbacks, duplicate
commands/confirmation, cancellation/expiry, unknown/unstarted users, audience
cutoff, paginated bulk credit, restart checkpoints, simultaneous spend/credit,
crash after wallet update before ledger write, DB outage recovery, blocked chats,
Telegram timeouts, RetryAfter/deferred backoff/exhaustion, alert styling/buttons,
live directory refresh, pending vs qualified referrals, empty/out-of-range pages,
HTML escaping, Unicode names, command registration, startup/shutdown, immediate
worker wakeup, and REAL python-telegram-bot request serialization offline.

Re-run from FUN/: install existing requirements-test.txt; python -m pytest -q.
For LOCAL real MongoDB only: start a disposable local server on 127.0.0.1:27019,
then FUN_GIFT_REAL_MONGO=1 python -m pytest -q test_token_admin.py.
The real-DB test fixture uses ONLY that hard-coded localhost address and deletes
its uniquely named sandbox database after each test.

NOT performed: live Telegram sends, actual Render deployment, production MongoDB
writes, real mobile-device display verification or guarantees against every
future infrastructure failure. Dependency deprecation warnings are present;
there are no failed tests in the final runs. The final ZIP is extracted over a
clean checkout and both final test runs are repeated before delivery.

## After deployment
Check /health responds normally. In your admin private chat use /userstoken,
then /gift YOUR_OWN_ID 5 and verify the wallet increment, cute alert and buttons.
Use /gift all 8 to inspect the preview before confirming the real distribution.
Never test a mass gift in production unless you actually intend to award it.
