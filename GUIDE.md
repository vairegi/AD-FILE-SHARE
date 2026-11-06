# LinkGuard v1.3 — Return Proof (closes the shortener bypass)

Prepared 2026-11-06. Patch base: repo commit `0f29868`.
Scope: Cloudflare Worker (`worker.js`) + Bots 1&2 root service (`linkguard.py`, `bot1.py`, `db.py`).
Funbot / `FUN/` is **not** touched. No new Python dependency, no new env var required.

---

## 1. What was actually happening

The reported bypass worked like this:

1. User taps **Download** in the posting channel → Bot 1 mints a verify token, a
   LinkGuard landing slug, and a signed Door-3 **exit grant**.
2. The landing page solves Turnstile in the user's real browser and the shortener
   opens. The browser keeps the `lg_s` session cookie.
3. Instead of finishing the shortener, the user sends the `vplink.in` link to a
   bypass bot. The bot returns our own
   `linkguard.fubukisecure.workers.dev/finish2?s=…&t=…` URL.
4. The user **pastes that URL into the same browser** (the one holding the cookie,
   same User-Agent) — and v1.2 released the Telegram deep link, because every
   v1.2 check was satisfied:
   - `lg_s` cookie present ✔
   - User-Agent matched ✔
   - grant authentic, unused, bound to that session ✔
   - **Referer empty — and `ALLOW_EMPTY_REFERER=true` let it through** ✔

The Cloudflare events confirm it: at 08:47:24 IST the Chrome request to
`/finish2` carried a cookie, **no Referer**, and `Sec-Fetch-Site: none`, and the
Worker answered **302**. The later Edge request without the cookie got **403** —
so the browser binding was working; it just was not proving the shortener was
completed *by a real navigation*.

**Key insight:** a cookie can be kept, and a User-Agent can be matched, by a URL
that was pasted. What a paste cannot fake is the *navigation shape* of the
request. No page script can forge `Sec-Fetch-*`; only the browser writes them.

---

## 2. What this patch changes

### Worker (`worker.js`) — v1.3

1. **Navigation proof on `/finish2`** (`navProof()`):
   - `Sec-Fetch-Site: none` → **denied** (`pasted_url`). This is the exact
     signature of a URL typed or pasted into the address bar.
   - `Sec-Fetch-Mode` / `Sec-Fetch-Dest` must be `navigate` / `document`.
   - A **present but unapproved** Referer is always denied.
   - A browser that sends **no** `Sec-Fetch-*` at all needs an approved Referer.
   - `ALLOW_EMPTY_REFERER` no longer opens a hole: in `RETURN_POLICY=nav_only`
     it is only honoured in `legacy` mode.
2. **Departure proof**: `/finish` (the hop to the shortener) now stamps
   `finish2_grants.departed_at`. A grant that never departed cannot be redeemed
   (`no_departure`).
3. **Atomic single use**: the burn is now
   `UPDATE … SET used = 1 … WHERE slug = ? AND used = 0` and the row count is
   checked, so two concurrent redemptions can no longer both win.
4. **Release record**: a verified redemption stores `released_at`, `released_ip`,
   `release_site`.
5. **New admin route** `POST /api/admin/release_status {slug}` →
   `{ok, released, reason, released_at, release_site, departed_at, policy}`.
   This is what lets the bot verify Door 3 *completed*.
6. Schema is migrated in place (`ensureV13Tables`) with `ALTER TABLE … ADD COLUMN`
   guarded by try/catch, so an existing D1 database keeps working.
7. Observe-mode can still **learn** an unknown exit host, but can no longer turn a
   pasted URL into a pass.
7. Retry page gained real hints for the new refusal reasons.

### Bot side (`linkguard.py`, `bot1.py`, `db.py`)

- `db.set_token_grant(token, grant_slug)` — the verify token now remembers its
  exit grant.
- `bot1.send_shortener_gate` persists that grant when it mints one.
- `linkguard.release_status(slug)` — asks the Worker; returns `None` when the
  Worker cannot be asked (unconfigured, transport error, 5xx, non-`ok` payload).
- `linkguard.return_proof_verdict(...)` — pure policy:
  | grant on token | Worker answer | verdict |
  |---|---|---|
  | no (pre-v1.3 link) | — | `allow` (old timing-gate rule only) |
  | yes | `released: true` | `allow` |
  | yes | `released: false` | `deny` |
  | yes | no answer | `retry` |
- `bot1._return_proof(...)` runs inside `process_verify` for **every non-`_g`
  verify link**, before anything is burned, marked verified or delivered:
  - `allow` → normal flow.
  - `deny` → token burned, **file withheld**, user told the shortener step was
    not completed in this browser, admins alerted. **No automatic ban** and no
    strike: a wrong denial must never punish a paying user.
  - `retry` → token **not** burned, nothing delivered, user asked to tap Download
    again, admins alerted. Our outage never costs the user their file.
- `_g` (force-sub resume) links skip the proof, exactly as they already skipped
  the timing gate.

### Explicitly not changed

- Posting, captions, delivery, purchases, force-sub, Funbot.
- The 100 s anti-bypass timing gate stays as a second, independent layer.

---

## 3. Deploy

### A. Worker
1. Cloudflare dashboard → Workers → `linkguard` → **Edit code**.
2. Paste the new `worker.js`. Deploy.
3. Keep every existing secret/var. `RETURN_POLICY` is **optional**:
   - unset → `nav_only` (recommended, this is the default),
   - `strict` → also requires a Referer on every return,
   - `legacy` → old v1.2 behaviour (do not use; it re-opens the hole).
4. Keep `ALLOW_EMPTY_REFERER=true` — it is now inert in `nav_only`.

No D1 migration step is needed: the Worker adds the columns itself on the first
`/finish` or `/finish2`.

### B. Bots 1 + 2 (Render root service)
Upload to the **repository root** (not `FUN/`): `bot1.py`, `db.py`,
`linkguard.py`. Commit together, then deploy only the Bots 1+2 service.
`/health` must stay 200. No env var change.

### C. Verify after deploy (2 minutes)
- Open a fresh Download link, complete the shortener normally → file delivered.
- Cloudflare logs should show `finish2_ok` with `release_site: "cross-site"`.
- Paste a `/finish2` URL into the address bar → `finish2_nav_denied` with
  `reason: "pasted_url"`, and the bot withholds the file.
- Admin alert text: “Verification NOT confirmed — file withheld”.

---

## 4. Test evidence (all run in the sandbox, offline)

| Suite | What it is | Result |
|---|---|---|
| `test_worker_return_proof.mjs` | the **real** `worker.js` under Node against a local `node:sqlite` D1 shim, Turnstile mocked, no network | **24/24** |
| `test_return_proof.py` | Bot-side gate on the real patched modules (httpx MockTransport + mongomock) | **19/19** |
| `test_linkguard.py` | legacy Python mirror of the Worker, updated to teach the v1.3 rule | **38/38** |

Worker checks covered: mint → landing cookie → claim → `/finish` departure →
normal cross-site return (302 to the deep link) → `release_status` reports
`released: true`; the reported paste-back (403, was 302 in v1.2); bypass bot
fetching directly with no headers (403); unapproved Referer (403); observe-mode
cannot rescue a paste (403); Referer stripped by policy still passes in
`nav_only` (302); `strict` still demands a Referer (403); grant that never
departed (403); missing cookie (403); UA mismatch (403); replay (403); three
concurrent redemptions → exactly one 302; `/api/health` and unknown admin routes
unchanged.

Bot checks covered: released → silent allow; not released → file withheld, token
burned, **no ban, no strike**; unreachable Worker → retry, token preserved; Worker
exception → retry; Worker disabled while a grant exists → retry, never a silent
allow; legacy token without a grant → allow with **no** needless Worker call;
`process_verify` wiring (proof runs for real links, skipped for `_g`); grant slug
persisted on the token.

**Not tested here:** a real Cloudflare Worker deployment, real D1, real
Turnstile, live Telegram delivery, and a real Render deploy. Those need the
deploy steps above; the sandbox cannot reach them.

---

## 5. Honest limits

- `Sec-Fetch-*` is sent by current Chrome/Edge/Firefox/Safari. A very old browser
  sends none and then needs an approved Referer — that is the `missing_nav_headers`
  hint telling the user to update the browser. Real mobile traffic in the
  captured events all sent these headers.
- A determined attacker running a **real headless browser** that follows the
  shortener's own redirect is indistinguishable from a human by header shape.
  This patch stops the *reported* bypass (paste-back of a bot-minted URL) and
  stops a leaked deep link from being honoured; it does not claim to defeat a
  full browser-automation farm.
- Residual gap worth closing next: require a server-side receipt from the
  shortener itself (a postback/signed callback per completion). `vplink.in`
  publishes no such API, so nothing here pretends to have it.
