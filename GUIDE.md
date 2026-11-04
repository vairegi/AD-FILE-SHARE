# Root Bots 1 + 2 — Smart Force-Sub Update v1

Prepared 2026-11-04 against latest repository commit
4e956d73310905199f425fd27c5e953affbd6c75. Root service only; FUN is unchanged.

## Install this update

Extract this ZIP and upload its files to the repository ROOT (not FUN):

- bot1.py, bot2.py, bot1_admin.py: actual replacement source files.
- force_sub.py: REQUIRED new shared helper; upload with the bot files.
- test_force_sub.py: 50 focused regressions.
- test_all.py: existing legacy harness with only four gate-UI assertions
  adapted to inspect join/check buttons instead of the removed generic text.
- GUIDE.md: this maintained guide.

Commit the four runtime files together so Render never deploys a partial set.
Deploy only the root Bots 1 + 2 service. Existing environment variables,
requirements, build/start commands and one-worker deployment remain unchanged.
No database migration, re-adding channels, URI change, rescan or reset is needed.
The already-installed bandwidth fix remains: main.py, root_dispatch.py,
root_index.py, db.py, requirements.txt and all FUN files are unchanged.
Nothing was pushed/deployed automatically or written to production MongoDB.

## New behavior

If a user already joined three required channels and an admin adds a fourth,
only that fourth channel appears on the next download. A recorded join request
for the SAME user and SAME channel also satisfies the gate, as before. Another
user's request, another channel's request or an unscoped legacy request cannot
satisfy a requirement for a specific channel.

Both bots now display actual channel titles on join buttons instead of six
identical Join Channel labels. The prompt shows the remaining count. Tapping
I've Joined rechecks requirements and edits the old prompt down to the remaining
channels. Once satisfied, Bot 1 resumes its existing file/shortener flow and
Bot 2 resumes its existing pending delivery. Tokens and captions are unchanged.

/forcesublist now shows channel TITLE, numeric ID, global/category scope and
clickable invite. IDs remain so /forcesubremove works as before. Long lists are
split into complete HTML-safe pages. Missing legacy request links are still
created/saved by this admin command as before; existing stored links are kept.

Titles use Telegram getChat. For reliable checks and metadata, configure BOTH
bots with appropriate channel permissions/admin access. If a title lookup fails,
a previous known title is reused or a safe Channel <id> fallback is shown;
this never substitutes a guessed title or a guessed invite URL. If an invite
cannot be resolved, the prompt explains the problem without treating that
requirement as satisfied.

Only channel display metadata is cached: at most 256 entries, separately keyed
by bot identity, with a ten-minute success TTL / short failure retry. Admin list
refreshes titles explicitly. A renamed channel can therefore take up to ten
minutes to change in user prompts unless the admin refreshes the list first.
Membership is NEVER cached: newly added requirements and users leaving channels
are checked on each new download/check-again. There is no background polling,
new scheduler or new production dependency. Each initial gate passes one fresh
missing-channel snapshot to its renderer to avoid duplicate membership calls.

Existing policy is deliberately preserved: an inconclusive Telegram membership
check fails open and is logged. This release does not make that policy stricter.
The existing database stores recorded requests, not a live Telegram request
lifecycle; declined/stale request reconciliation is not introduced here.

The obsolete, shadowed first force-sub block in bot2.py was removed. The active
later definitions were updated; broadcast and active delivery bodies are kept.

## Verification for this update

Sandbox Python 3.12 and isolated real MongoDB 8.0.16 (localhost:27019 only).
Mocked Telegram API calls; no live Telegram or provider requests were exercised.
No production database write or Render deployment was performed.

- 50 new force-sub tests pass on both mocked and real local MongoDB.
- Combined focused regression suite: 202 passed.
- Real local database suite: 131 passed (root indexing, bandwidth and force-sub).
- Standalone profile-title harness: 10/10 passed; ASGI route harness: PASS.
- The exact final ZIP is extracted over a clean base checkout and retested;
  compilation, archive integrity and byte-identical source checks are verified.
- The legacy harness remains 433/440, with the SAME seven pre-existing failures
  as the untouched base commit; the names are retained in the historical
  bandwidth section below. No legacy functional failure was hidden or repaired.
  Four obsolete label-only gate checks were legitimately updated to inspect
  required URL buttons and their existing callbacks instead of generic wording.

Coverage includes the three-joined-plus-new-fourth scenario, channel-scoped
requests, restricted membership, category overrides/global fallback, live
rechecks after leaving, title cache limits/renaming/failure, private request
links, missing invite handling, partial-join prompt refresh, deleted/unchanged
prompts, admin permissions, HTML safety, pagination, actual database persistence,
and a full Bot 2 pending -> request -> fresh-token -> copy -> consume path with
mocked Telegram copying and REAL sandbox database writes. The original gated
handoff token remains unused while the new resume token is validated/consumed.

For maintainers, use the existing root test dependencies plus pytest,
pytest-asyncio and mongomock-motor, then run (sandbox only):

    MONGO_URI='' python -m pytest -q --asyncio-mode=auto test_root_index.py test_bandwidth.py test_force_sub.py test_delivery_hotfix.py test_resilience_admin.py test_linkguard.py

With isolated local MongoDB on port 27019:

    ROOT_LOCAL_MONGO_TEST=1 MONGO_URI='' python -m pytest -q --asyncio-mode=auto test_root_index.py test_bandwidth.py test_force_sub.py

Standalone legacy scripts are run separately, not imported into pytest. Never
point them at production: their fixed test databases may be dropped.

After deploying, check one existing download in each bot, join/request one of
several missing channels, then tap I've Joined: that channel should disappear
while the others remain. Confirm an already-subscribed user sees only a newly
added channel and /forcesublist displays titles. Existing older prompt messages
refresh on their next check-again; they are not edited in bulk at deployment.
Sandbox tests cannot guarantee perfect production Telegram permissions or API
behavior. Keep the existing bandwidth monitoring instructions below.

Rollback this update only: restore bot1.py, bot2.py and bot1_admin.py together
from commit 4e956d7 and redeploy root only. The unused force_sub.py can remain or
be removed after rollback. Do NOT revert the bandwidth files or delete data.

---

# Historical bandwidth fix v1 — already installed

The following records the PREVIOUS release. Its installation/rollback filenames
are NOT the instructions for the current smart force-sub ZIP above.

Prepared 2026-11-02 against repository commit
585aa8f0a30ce7c68494acbbf10d103ff0e34491.
This is a root-service-only replacement package. FUN is unchanged.

## Install through GitHub

1. Extract this ZIP. Upload main.py, root_dispatch.py, root_index.py,
   test_bandwidth.py and this GUIDE.md to the repository ROOT, replacing
   matching filenames. Do not put them inside FUN or inside another folder.
2. Commit the three runtime replacements together, not as separate deployed
   partial versions. Existing requirements.txt, bot1.py, bot2.py and db.py stay.
3. Deploy only the Render service hosting root Bots 1 + 2. Keep its root
   directory at repository root, its existing environment variables, build
   command and uvicorn main:app start command. No new environment variable or
   production dependency is required. Keep ONE instance / ONE Uvicorn worker.
4. Prefer a quiet deployment window; pause the source importer briefly, wait
   for the prior process to exit and the new service to become healthy, then
   resume. Already-running uncertain commands remain review-only, not replayed.
5. Confirm the startup log contains root-bandwidth-v1 (event wake + bounded
   recovery). GET and HEAD /health both return 200 when the database is healthy;
   database failure still returns 503.

No database reset, rescan, queue purge, URI change or indiscriminate /flushindex
is required. Do not delete raw staging or waiting jobs. Uploading this archive
has not deployed anything automatically.

## Diagnosis and changes

The November screenshot reports 1.73 GB monthly Service-Initiated traffic and
53.31 MB in the selected hourly point. The full November log has 1,650 lines
from 10:25:18 to 11:43:03 UTC: 70 successful Bot 1 and 45 successful Bot 2
webhooks, two recorded file deliveries, 1,090 successful health GETs, six
health HEAD 405s, and five index categories in all 78 resource samples.
Normal root file delivery uses Telegram-side copies, not video reuploads.
These observations support persistent background traffic; the logs do not
provide per-destination byte attribution or prove that every billed byte came
from MongoDB.

- root_dispatch.py keeps four fixed workers and durable-before-ack receipts.
  Workers wake locally AFTER a successful inbox write, then drain available
  work immediately. Empty workers wait up to 30 seconds instead of 0.25s;
  this fallback catches missed local signals, delayed retries and updates
  persisted by an overlapping old process. Normal local webhooks DO NOT wait
  30 seconds. Expired-running recovery is shared/rate-limited per bot to once
  per 30 seconds instead of every claim. Review-only handling of uncertain
  side effects, deduplication, lease duration and processing limits are kept.
- root_index.py wakes on durable source notifications. It sleeps to a durable
  quiet/max-wait deadline, with a 30-second recovery backstop instead of
  one-second idle polling. Open/missing/oversized blocks are parked with a
  five-minute safety recheck. This is NOT a completion timeout: they remain
  unpublished. New source metadata, caption edits or explicit owner sealing
  reactivate work immediately. Error retry remains 15 seconds. Ingestion's
  eight-second maximum wait cannot override error or parked-source backoff.
  A stale processing/error generation cannot delay a fresh notification.
- main.py adds HEAD /health support and a recognizable startup version line.
- test_bandwidth.py adds 30 isolated regressions, reusing test_root_index.py's
  existing sandbox fixture; that existing file is already in the repository.
- A small first_pending_at index is created automatically for job scheduling.
  There is no destructive data migration and no published-flag/token change.

Normal scheduled posting, forced membership, shortener gates, delivery tokens,
Telegram-side copies, caption behavior and deletion schedules are not edited.
Existing memory bounds, item fences, one heavy indexer, category locks,
metadata-only staging and independent FUN runtime are retained.

## Local measurement (not a Render billing prediction)

Identical 60-second A/B runs used real isolated MongoDB 8.0.16 on localhost,
five unfinished cover groups, four inbox workers, the indexer and metrics
worker, no user activity and unchanged production source versus this fix.

| Measurement | Before | Fixed |
| --- | ---: | ---: |
| Database commands | 2,478 | 94 |
| Serialized outgoing command BSON bytes | 804,316 | 24,657 |
| Pending raw metadata retained | 10 | 10 |
| Incomplete groups published | 0 | 0 |

That is about 96.2% fewer commands and 96.9% less outgoing command BSON in
this specific idle test, including initial processing of the five groups.
BSON was measured by the MongoDB command listener. It excludes TLS/TCP
framing, replies, heartbeats and other services/HTTP traffic. Atlas latency,
replica topology and real ingestion differ; no monthly GB saving is promised.
Full Render service-initiated traffic has NOT been measured after deployment.

## Tests and limits

Sandbox interpreter: Python 3.12; actual Render Python version was not changed.
Real-database tests use only localhost:27019 and a fixed disposable sandbox
name. No production write, Telegram delivery, provider purchase, live
broadcast or Render deployment was performed for this fix.

Focused root regressions: 152 passed (51 existing root-index/webhook tests,
30 new bandwidth tests, plus 71 existing delivery/admin/HTTP/LinkGuard tests).
Real local MongoDB run: 81 passed (51 existing root-index tests + 30 new tests).
Standalone profile-title harness: 10/10 checks passed.
Standalone ASGI route harness: PASS (health, webhook persistence and secrets).
Compilation, ZIP CRC integrity, exact extraction/file-byte comparisons and
FUN unchanged checks are also performed before delivery.

The legacy test_all.py harness is NOT completely green: 433/440 checks pass,
with the exact same seven failures on untouched commit 585aa8f and this fix:

- deliver: multi-version delivers ALL versions (no chooser)
- shortener text mode
- shortener JSON mode
- wizard parsers accept valid input and skips
- gate outage: admins alerted which providers failed
- v4.8.3: 3 videos + srt delivered per-file, back-to-back
- v4.8.3: 'I've Joined' resumes delivery per-file

Those failures predate this change; this bandwidth package does not claim to
repair them. Targeted delivery tests pass separately. Do not combine the
standalone scripts into pytest collection: some execute and exit at import.
Mock runs produce deprecation warnings from mongomock; these are not failures.

For a maintainer reproducing sandbox tests (not required to deploy), install
root requirements plus pytest, pytest-asyncio and mongomock-motor, then run:

    MONGO_URI='' python -m pytest -q --asyncio-mode=auto test_root_index.py test_bandwidth.py test_delivery_hotfix.py test_resilience_admin.py test_linkguard.py

With an isolated local MongoDB listening on 127.0.0.1:27019 only:

    ROOT_LOCAL_MONGO_TEST=1 MONGO_URI='' python -m pytest -q --asyncio-mode=auto test_root_index.py test_bandwidth.py

Never point a test harness at production. Test collection may create/drop its
fixed sandbox database. Sandbox success cannot guarantee perfect production
behavior or verify Telegram/provider compatibility without live checks.

## Verify after deployment

First check /health and the root-bandwidth-v1 startup marker. Smoke-test an
existing Download link through Bot 1 to Bot 2, ordinary gate completion,
caption/copy behavior and deletion. Add a new cover group and then its next
cover: the first should index; the current final cover should stay waiting.
A waiting index status is expected and does NOT imply a stuck publishing job.

The existing once-per-minute Root resources log now includes cumulative,
process-local counters: inbox_claims_total, inbox_empty_total,
inbox_recoveries_total, index_passes_total and index_waits_total. Compare their
differences between lines, not their totals; they reset on process restart.
They contain no message bodies, tokens, URIs or credentials. The existing
pending_updates count includes review records, so one review record is not
necessarily an actively queued update. Do not reset it merely for a zero.

In an idle settled service, these counters should rise slowly instead of
thousands of times per minute. Source bursts legitimately raise them. A
five-minute safety recheck of a waiting group is intentional; it never seals
that group.

Compare two COMPLETE hourly Render graph windows after deployment, preferably
with similar activity. Do not compare an hour containing a deployment overlap
or a large import against a quiet hour. The 1.73 GB monthly total will NOT go
back down: verify that subsequent hourly increments fall. If they do not,
collect matching root logs and investigate Atlas command rates plus actual
outbound traffic. Health probes/Telegram requests, source metadata writes,
explicit history scans and external API calls still have real costs.

## Rollback

Restore main.py, root_dispatch.py and root_index.py together from base commit
585aa8f0a30ce7c68494acbbf10d103ff0e34491 and redeploy the root service only.
Do not delete database collections. Added job fields/index are additive; old
code can read them, but rollback restores its aggressive polling behavior.
Rollback may bring the original bandwidth cost back. FUN needs no rollback.
