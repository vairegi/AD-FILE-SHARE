/**
 * LinkGuard v1.2 — three-door link protection on Cloudflare Workers.
 *
 * v1.2 (observe-mode + host-learning): paid shorteners exit through
 * ad-network intermediate hosts, so a static Referer allowlist can
 * false-positive legit users ("bypass detected" after a REAL solve).
 * New admin routes let the owner observe the true exit hosts and
 * approve them:
 *   POST /api/admin/observe        {on:true, minutes:N}  learn mode
 *   POST /api/admin/observed_hosts {}                    list learned
 *   POST /api/admin/ref_hosts_add  {host:"x.com"}        approve one
 *   POST /api/admin/observed_clear {}                    clear learned
 * While observing, an unknown Referer host is RECORDED and allowed
 * through to the cookie/UA/grant checks (which still protect the
 * link) instead of being denied on the spot.
 *
 * v1.1 (anti-bypass): the paid shortener's destination is no longer the raw
 * Telegram deep link. It is /finish2?s=<grant_slug>&t=<grant>, which releases
 * the real deep link ONLY to the original verified browser session:
 *   lg_s cookie + matching UA + Referer from an allowed shortener host +
 *   HMAC grant, single-use, 60-minute TTL.
 * A copied shortener URL is worthless: burned on first use and bound to the
 * cookie/UA of the session that solved Turnstile.
 *
 * Door 1: Turnstile proof-of-humanity (siteverify, hostname-checked).
 * Door 2: landing page — server-side session + lg_s cookie + nonce, 15 s
 *         wait, Get Link button, muted 300 ms flash, decoy fan-out.
 * Door 3: /finish  (entry to shortener) — Referer allowlist, HMAC, single-use,
 *         ~90 s TTL, UA binding (IP drift logged, allowed).
 *         /finish2 (exit from shortener) — see handleFinish2.
 *
 * Storage: D1 (LINKGUARD_DB) — slugs, sessions, claims, logs, decoys, rate,
 *          finish2_grants, ref_hosts.
 * Secrets: HMAC_SECRET, TURNSTILE_SECRET, ADMIN_API_KEY, TG_BOT_TOKEN.
 * Vars:    TURNSTILE_SITEKEY, WORKER_HOSTNAME, TG_ADMIN_IDS, TOKEN_TTL_SECONDS,
 *          LANDING_WAIT_SECONDS, DECOY_COUNT, GRANT_TTL_MINUTES,
 *          ALLOW_EMPTY_REFERER ("true" only if your shortener strips Referer).
 */

const JSON_CT = { "content-type": "application/json; charset=utf-8" };
const NOSTORE = { "cache-control": "no-store, no-cache, must-revalidate" };

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    try {
      if (path === "/api/health") return json({ ok: true, ts: Date.now() });
      if (path === "/api/claim" && request.method === "POST")
        return await handleClaim(request, env);
      if (path === "/finish" && request.method === "GET")
        return await handleFinish(request, env, url, ctx);
      if (path === "/finish2" && request.method === "GET")
        return await handleFinish2(request, env, url);
      if (path.startsWith("/api/admin/"))
        return await handleAdmin(request, env, path);
      if (request.method === "GET" && path.length > 1)
        return await handleLanding(request, env, path.slice(1));
      return text("Not found", 404);
    } catch (err) {
      await logEvent(env, "error", { msg: String(err), path });
      return text("Internal error", 500);
    }
  },
};

/* ───────────────────────────── Door 2: landing ─────────────────────────── */

async function handleLanding(request, env, slug) {
  if (!/^[a-z0-9]{4,32}$/.test(slug)) return text("Bad link", 400);
  const row = await env.LINKGUARD_DB.prepare(
    "SELECT * FROM slugs WHERE slug = ?"
  ).bind(slug).first();
  if (!row) return text("Link not found", 404);
  if (row.status !== "active") return text("Link revoked", 410);

  const ip = request.headers.get("cf-connecting-ip") || "0.0.0.0";
  const ua = request.headers.get("user-agent") || "";

  if (row.honeypot) {
    await logEvent(env, "honeypot_hit", { slug, ip, ua });
    await alertAdmins(env, `🍯 Honeypot hit\nslug: ${slug}\nip: ${ip}\nua: ${ua}`);
    return text("Link not found", 404);
  }

  if (!(await rateOk(env, `land:${ip}`, 30, 60))) {
    await logEvent(env, "rate_limited", { slug, ip, stage: "landing" });
    return text("Slow down", 429);
  }

  // v1.1: if this post's exit grant is already burned, say so up front
  // instead of sending the user through a doomed loop.
  try {
    if (row.grant_slug) {
      const g = await env.LINKGUARD_DB.prepare(
        "SELECT used FROM finish2_grants WHERE slug = ?").bind(row.grant_slug).first();
      if (g && g.used) return usedPage();
    }
  } catch { /* pre-migration schema: skip */ }

  // v1.1: REUSE a live session if this browser already has one for this slug
  // (retry loop from a /finish2 failure, or a page reload).
  const cookieSid = cookieVal(request, "lg_s");
  let sessionId = null, nonce = null, skipTurnstile = false;
  if (cookieSid) {
    const s = await env.LINKGUARD_DB.prepare(
      "SELECT * FROM sessions WHERE id = ? AND slug = ?"
    ).bind(cookieSid, slug).first();
    if (s && s.ua === ua &&
        Date.now() - s.created_at < 90 * 60 * 1000 &&
        ["challenged", "out_to_shortener"].includes(s.state)) {
      sessionId = s.id;
      nonce = s.nonce;
      skipTurnstile = true;
      if (s.state === "out_to_shortener") {
        // Already went out once; force a fresh claim cycle so a new Door-3
        // token gets minted on click.
        try {
          await env.LINKGUARD_DB.prepare(
            "UPDATE sessions SET state = 'challenged', turnstile_ok = 0 WHERE id = ?"
          ).bind(sessionId).run();
        } catch { /* pre-migration */ }
      }
    }
  }

  if (!sessionId) {
    sessionId = crypto.randomUUID();
    nonce = crypto.randomUUID().replace(/-/g, "");
    try {
      await env.LINKGUARD_DB.prepare(
        `INSERT INTO sessions (id, slug, ip, ua, nonce, created_at, turnstile_ok, state)
         VALUES (?, ?, ?, ?, ?, ?, 0, 'served')`
      ).bind(sessionId, slug, ip, ua, nonce, Date.now()).run();
    } catch {
      await env.LINKGUARD_DB.prepare(
        `INSERT INTO sessions (id, slug, ip, ua, nonce, created_at, turnstile_ok)
         VALUES (?, ?, ?, ?, ?, ?, 0)`
      ).bind(sessionId, slug, ip, ua, nonce, Date.now()).run();
    }
  }
  await logEvent(env, "landing_served", { slug, sessionId, ip, resumed: skipTurnstile });

  const resp = html(landingPage(env, slug, sessionId, nonce, waitSec(env), skipTurnstile));
  resp.headers.append("set-cookie",
    `lg_s=${sessionId}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=5400`);
  return resp;
}

function waitSec(env) {
  return parseInt(env.LANDING_WAIT_SECONDS || "15", 10);
}

function landingPage(env, slug, sessionId, nonce, wait, skipTurnstile) {
  const sitekey = env.TURNSTILE_SITEKEY || "";
  const turnstileHtml = skipTurnstile
    ? `<p class="ok">✅ Already verified — tap below when the timer ends.</p>`
    : `<div class="cf-turnstile" data-sitekey="${esc(sitekey)}" data-callback="tsPassed"
       data-theme="dark"></div>`;
  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="origin">
<title>Your link is almost ready</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<style>
  :root{color-scheme:dark}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0f172a;color:#e2e8f0;font-family:system-ui,Segoe UI,Roboto,sans-serif}
  .card{width:min(92vw,420px);background:#1e293b;border:1px solid #334155;border-radius:16px;
        padding:28px;text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.4)}
  h1{font-size:1.15rem;margin:0 0 6px}
  p.sub{color:#94a3b8;font-size:.85rem;margin:0 0 20px}
  p.ok{color:#4ade80;font-size:.9rem;margin:14px 0}
  .cf-turnstile{display:flex;justify-content:center;margin:14px 0}
  #getlink{display:none;width:100%;padding:14px;border:0;border-radius:10px;cursor:pointer;
        background:#22c55e;color:#052e16;font-weight:700;font-size:1rem}
  #getlink:disabled{background:#334155;color:#64748b;cursor:not-allowed}
  #timer{font-variant-numeric:tabular-nums;color:#94a3b8;font-size:.8rem;margin-top:10px;min-height:1.2em}
  #flash{display:none;margin-top:10px;font-size:10px;color:#475569;word-break:break-all}
  .err{color:#f87171;font-size:.85rem;margin-top:12px;display:none}
</style></head>
<body><div class="card">
  <h1>🔒 Verify you are human</h1>
  <p class="sub">This protects the link from bots and scrapers.</p>
  ${turnstileHtml}
  <button id="getlink" disabled>Get Link</button>
  <div id="timer"></div>
  <div id="flash"></div>
  <div class="err" id="err"></div>
</div>
<script>
const SLUG=${JSON.stringify(slug)},SID=${JSON.stringify(sessionId)},
      NONCE=${JSON.stringify(nonce)},WAIT=${wait},SKIP_TS=${skipTurnstile ? "true" : "false"};
let tsToken=null,countdown=null;
function showErr(m){const e=document.getElementById('err');e.textContent=m;e.style.display='block';}
function armButton(){
  const b=document.getElementById('getlink');b.style.display='block';
  let left=WAIT;const t=document.getElementById('timer');
  t.textContent='Link unlocks in '+left+'s…';
  countdown=setInterval(()=>{left--;
    if(left<=0){clearInterval(countdown);t.textContent='';b.disabled=false;b.textContent='🔗 Get Link';}
    else t.textContent='Link unlocks in '+left+'s…';},1000);
}
window.tsPassed=function(token){tsToken=token;armButton();};
if(SKIP_TS){tsToken='RESUMED';armButton();}
document.getElementById('getlink').addEventListener('click',async()=>{
  const b=document.getElementById('getlink');b.disabled=true;b.textContent='Preparing…';
  try{
    const r=await fetch('/api/claim',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({slug:SLUG,session_id:SID,cf_token:tsToken,nonce:NONCE})});
    const d=await r.json();
    if(!r.ok){showErr(d.reason||'Verification failed — reload the page.');b.textContent='Retry';b.disabled=false;return;}
    // Muted flash: the /finish URL appears for 300ms in 10px dim gray. It is
    // single-use and 90s-lived, so displaying it is harmless — the tab leaves
    // immediately and a redirect-follower records the decoys, not this.
    const flash=document.getElementById('flash');
    flash.style.display='block';flash.textContent=d.finish_url;
    setTimeout(()=>{window.location.assign(d.finish_url);},300);
  }catch(e){showErr('Network error — try again.');b.disabled=false;b.textContent='Retry';}
});
</script></body></html>`;
}

/* ─────────────────────── Door 2→3: POST /api/claim ─────────────────────── */

async function handleClaim(request, env) {
  let body;
  try { body = await request.json(); }
  catch { return json({ reason: "bad_json" }, 400); }
  const { slug, session_id, cf_token, nonce } = body || {};
  if (!slug || !session_id || !cf_token || !nonce)
    return json({ reason: "missing_fields" }, 400);

  const ip = request.headers.get("cf-connecting-ip") || "0.0.0.0";
  const ua = request.headers.get("user-agent") || "";

  if (!(await rateOk(env, `claim:${ip}`, 10, 60)))
    return deny(env, slug, session_id, ip, "rate_limited", 429);

  const sess = await env.LINKGUARD_DB.prepare(
    "SELECT * FROM sessions WHERE id = ? AND slug = ?"
  ).bind(session_id, slug).first();
  if (!sess) return deny(env, slug, session_id, ip, "no_session", 403);
  if (sess.turnstile_ok) return deny(env, slug, session_id, ip, "session_used", 403);
  if (Date.now() - sess.created_at > 90 * 60 * 1000)
    return deny(env, slug, session_id, ip, "session_expired", 403);
  if (sess.nonce !== nonce) return deny(env, slug, session_id, ip, "bad_nonce", 403);
  if (sess.ua !== ua)
    return deny(env, slug, session_id, ip, "binding_mismatch", 403, true);
  if (sess.ip !== ip)
    await logEvent(env, "ip_drift", { slug, sessionId: session_id, from: sess.ip, to: ip, stage: "claim" });

  // Door 1 proof. 'RESUMED' = this browser already solved Turnstile in this
  // session and is re-claiming after a /finish2 retry loop.
  if (cf_token !== "RESUMED") {
    const ts = await fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        secret: env.TURNSTILE_SECRET, response: cf_token, remoteip: ip,
      }),
    }).then(r => r.json()).catch(() => null);
    if (!ts || !ts.success)
      return deny(env, slug, session_id, ip, "turnstile_failed", 403);
    if (ts.hostname && env.WORKER_HOSTNAME && ts.hostname !== env.WORKER_HOSTNAME)
      return deny(env, slug, session_id, ip, "turnstile_hostname", 403);
  } else if (sess.state !== "challenged") {
    return deny(env, slug, session_id, ip, "turnstile_failed", 403);
  }

  try {
    await env.LINKGUARD_DB.prepare(
      "UPDATE sessions SET turnstile_ok = 1, state = 'out_to_shortener' WHERE id = ?"
    ).bind(session_id).run();
  } catch {
    await env.LINKGUARD_DB.prepare(
      "UPDATE sessions SET turnstile_ok = 1 WHERE id = ?"
    ).bind(session_id).run();
  }

  // v1.1: bind this post's exit grant to the session that just proved human.
  // finish2 will later require grant.session_id === the cookie's session.
  try {
    const srow = await env.LINKGUARD_DB.prepare(
      "SELECT grant_slug FROM slugs WHERE slug = ?").bind(slug).first();
    if (srow && srow.grant_slug) {
      await env.LINKGUARD_DB.prepare(
        "UPDATE finish2_grants SET session_id = ? WHERE slug = ? AND used = 0"
      ).bind(session_id, srow.grant_slug).run();
    }
  } catch { /* pre-migration schema: finish2 unavailable anyway */ }

  const ttl = Math.min(Math.max(parseInt(env.TOKEN_TTL_SECONDS || "90", 10), 30), 300);
  const exp = Math.floor(Date.now() / 1000) + ttl;
  const rnd = crypto.randomUUID().replace(/-/g, "");
  const payload = `${slug}.${session_id}.${exp}.${rnd}`;
  const sig = await hmac(env.HMAC_SECRET, payload);
  const token = b64url(payload) + "." + sig;

  await env.LINKGUARD_DB.prepare(
    `INSERT INTO claims (token, slug, session_id, ip, ua, expires_at, used)
     VALUES (?, ?, ?, ?, ?, ?, 0)`
  ).bind(token, slug, session_id, ip, ua, exp).run();
  await logEvent(env, "token_minted", { slug, sessionId: session_id, ip, exp });

  const host = env.WORKER_HOSTNAME || new URL(request.url).host;
  return json({ token, expires_in: ttl, finish_url: `https://${host}/finish?t=${encodeURIComponent(token)}` });
}

/* ──────────────── Door 3 (entry): GET /finish?t= ──────────────── */

async function handleFinish(request, env, url, ctx) {
  const token = url.searchParams.get("t") || "";
  const ip = request.headers.get("cf-connecting-ip") || "0.0.0.0";
  const ua = request.headers.get("user-agent") || "";
  const host = env.WORKER_HOSTNAME || url.host;

  const ref = request.headers.get("referer") || "";
  if (!ref.startsWith(`https://${host}/`))
    return denyFinish(env, token, ip, "bad_referer", ref);

  const dot = token.lastIndexOf(".");
  if (dot < 1) return denyFinish(env, token, ip, "malformed");
  const payload = atobUrl(token.slice(0, dot));
  const sig = token.slice(dot + 1);
  const expect = await hmac(env.HMAC_SECRET, payload);
  if (!timingSafeEq(sig, expect)) return denyFinish(env, token, ip, "forged");

  const [slug, sessionId, expStr, rnd] = payload.split(".");
  const exp = parseInt(expStr, 10);
  if (!slug || !sessionId || !exp || !rnd) return denyFinish(env, token, ip, "malformed");
  if (Math.floor(Date.now() / 1000) > exp) return denyFinish(env, token, ip, "expired");

  const claim = await env.LINKGUARD_DB.prepare(
    "SELECT * FROM claims WHERE token = ?"
  ).bind(token).first();
  if (!claim) return denyFinish(env, token, ip, "unknown_token");
  if (claim.used) return denyFinish(env, token, ip, "reused");
  if (claim.ua !== ua) {
    await logEvent(env, "binding_mismatch", { slug, sessionId, finishUa: ua });
    await alertAdmins(env, `⚠️ Token UA mismatch\nslug: ${slug}\nclaim ua: ${claim.ua}\nfinish ua: ${ua}`);
    return denyFinish(env, token, ip, "binding_mismatch", null, true);
  }
  if (claim.ip !== ip)
    await logEvent(env, "ip_drift", { slug, sessionId, from: claim.ip, to: ip, stage: "finish" });

  const slugRow = await env.LINKGUARD_DB.prepare(
    "SELECT destination, status FROM slugs WHERE slug = ?"
  ).bind(slug).first();
  if (!slugRow || slugRow.status !== "active")
    return denyFinish(env, token, ip, "slug_revoked");

  await env.LINKGUARD_DB.prepare("UPDATE claims SET used = 1 WHERE token = ?").bind(token).run();
  await logEvent(env, "claim_ok", { slug, sessionId, ip });

  ctx.waitUntil(fireDecoys(env, slug, ip));

  return new Response(null, {
    status: 302,
    headers: { location: slugRow.destination, ...NOSTORE },
  });
}

async function fireDecoys(env, slug, ip) {
  try {
    const n = parseInt(env.DECOY_COUNT || "3", 10);
    const { results } = await env.LINKGUARD_DB.prepare(
      "SELECT url FROM decoys ORDER BY RANDOM() LIMIT ?"
    ).bind(n).all();
    await Promise.allSettled((results || []).map(r =>
      fetch(r.url, { headers: { "user-agent": "Mozilla/5.0 (LinkGuard decoy)" } })
        .then(x => x.arrayBuffer()).catch(() => null)));
    await logEvent(env, "decoys_fired", { slug, ip, count: (results || []).length });
  } catch { /* decoys are best-effort */ }
}

/* ────────── Door 3 (exit): GET /finish2?s=<grant>&t=<token> ────────── */

async function handleFinish2(request, env, url) {
  const slug = url.searchParams.get("s") || "";
  const grant = url.searchParams.get("t") || "";
  const ip = request.headers.get("cf-connecting-ip") || "0.0.0.0";
  const ua = request.headers.get("user-agent") || "";

  const fail = async (reason, extra) => {
    await logEvent(env, "finish2_denied", { slug, ip, reason, extra });
    return retryPage(env, slug, reason);
  };

  // 1. Session cookie — the "same browser" proof. No cookie = not our human.
  const cookieSid = cookieVal(request, "lg_s");
  if (!cookieSid) return fail("no_cookie");
  const sess = await env.LINKGUARD_DB.prepare(
    "SELECT * FROM sessions WHERE id = ?"
  ).bind(cookieSid).first();
  if (!sess) return fail("no_session");
  if (!["out_to_shortener", "completed"].includes(sess.state))
    return fail("not_through_door1");

  // 2. User-Agent binding (IP drift allowed for legit mobile users, logged).
  if (sess.ua !== ua) {
    await alertAdmins(env, `⚠️ finish2 UA mismatch\nslug: ${slug}\nsess ua: ${sess.ua}\nreq ua: ${ua}`);
    return fail("ua_mismatch");
  }
  if (sess.ip !== ip)
    await logEvent(env, "ip_drift", { slug, sessionId: sess.id, from: sess.ip, to: ip, stage: "finish2" });

  // 3. Referer allowlist — must arrive FROM a known shortener host.
  const ref = request.headers.get("referer") || "";
  const refHost = hostOf(ref);
  const allowed = await allowedRefHosts(env, slug);
  const emptyOk = String(env.ALLOW_EMPTY_REFERER || "false").toLowerCase() === "true";
  if (!refHost) {
    if (!emptyOk) return fail("bad_shortener_referer", "(empty)");
  } else   if (!allowed.some(h => refHost === h || refHost.endsWith("." + h))) {
    // v1.2: while observe-mode is on, record the real exit host and
    // let the request continue to the cookie/UA/grant checks instead
    // of false-denying a legit user who solved the shortener.
    if (refHost && await observeActive(env)) {
      await recordObservedHost(env, refHost);
      await logEvent(env, "finish2_observed_ref", { slug, refHost });
    } else {
      return fail("bad_shortener_referer", refHost);
    }
  }

  // 4. HMAC + expiry on the grant token itself.
  const dot = grant.lastIndexOf(".");
  if (dot < 1) return fail("malformed");
  const payload = atobUrl(grant.slice(0, dot));
  const sig = grant.slice(dot + 1);
  const expect = await hmac(env.HMAC_SECRET, payload);
  if (!timingSafeEq(sig, expect)) return fail("forged");
  const [gSlug, gSid, expStr, rnd] = payload.split(".");
  const exp = parseInt(expStr, 10);
  if (!gSlug || !gSid || !exp || !rnd) return fail("malformed");
  if (gSlug !== slug) return fail("slug_mismatch");
  if (Math.floor(Date.now() / 1000) > exp) return fail("expired");

  // 5. Grant must exist, be unused, and belong to THIS session.
  const row = await env.LINKGUARD_DB.prepare(
    "SELECT * FROM finish2_grants WHERE slug = ?"
  ).bind(slug).first();
  if (!row) return fail("unknown_grant");
  if (row.used) return fail("reused");
  if (row.session_id !== sess.id) return fail("wrong_session");

  // 6. Burn BEFORE revealing the deep link.
  await env.LINKGUARD_DB.prepare(
    "UPDATE finish2_grants SET used = 1 WHERE slug = ?").bind(slug).run();
  try {
    await env.LINKGUARD_DB.prepare(
      "UPDATE sessions SET state = 'completed' WHERE id = ?").bind(sess.id).run();
  } catch { /* pre-migration */ }
  await logEvent(env, "finish2_ok", { slug, sessionId: sess.id, ip });

  return new Response(null, {
    status: 302,
    headers: { location: row.final_destination, ...NOSTORE },
  });
}

/* Friendly retry page — tells bypassers to start over, with a fresh link
   back to THIS post's public landing slug (reverse-looked-up from the grant). */
async function retryPage(env, slug, reason) {
  const host = env.WORKER_HOSTNAME || "";
  const hints = {
    no_cookie: "Your browser session was not found (cookies may be blocked).",
    no_session: "Your session expired or never started.",
    not_through_door1: "You skipped the human-verification step.",
    ua_mismatch: "This link is locked to the browser that solved the verification.",
    bad_shortener_referer: "You did not arrive through the official shortener flow.",
    forged: "This link is invalid.",
    expired: "This link expired. Links are only valid for a short time.",
    reused: "This link was already used. Links work exactly once.",
    unknown_grant: "This link is invalid.",
    wrong_session: "This link belongs to a different session.",
    slug_mismatch: "This link is invalid.",
    malformed: "This link is invalid.",
  };
  const hint = hints[reason] || "Verification failed.";
  let btn = "";
  try {
    const pub = await env.LINKGUARD_DB.prepare(
      "SELECT slug FROM slugs WHERE grant_slug = ? AND status = 'active'"
    ).bind(slug).first();
    if (pub) btn = `<a class="btn" href="https://${esc(host)}/${esc(pub.slug)}">🔁 Start verification again</a>`;
  } catch { /* pre-migration */ }
  return html(`<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Verification incomplete</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif}
.card{width:min(92vw,420px);background:#1e293b;border:1px solid #334155;border-radius:16px;
padding:28px;text-align:center}
h1{font-size:1.1rem}p{color:#94a3b8;font-size:.9rem}
a.btn{display:inline-block;margin-top:16px;padding:13px 26px;border-radius:10px;
background:#22c55e;color:#052e16;font-weight:700;text-decoration:none}</style></head>
<body><div class="card">
<h1>⚠️ Bypass detected — verification incomplete</h1>
<p>${esc(hint)}</p>
<p>To get your file, start the verification again from the beginning.
Do not skip steps or reuse old links — each link works exactly once.</p>
${btn}
</div></body></html>`, 403);
}

function usedPage() {
  return html(`<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link already used</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#e2e8f0;font-family:system-ui,sans-serif;text-align:center}
.card{width:min(92vw,420px);background:#1e293b;border:1px solid #334155;border-radius:16px;padding:28px}
p{color:#94a3b8}</style></head>
<body><div class="card"><h1>✅ This link was already used</h1>
<p>Each link works exactly once. Tap <b>Download</b> again in Telegram to get a fresh link.</p>
</div></body></html>`, 410);
}

async function allowedRefHosts(env, slug) {
  // Per-grant list wins (pushed by the bot from its live /shortenerapi
  // registry at mint time); fall back to the global ref_hosts table.
  try {
    const g = await env.LINKGUARD_DB.prepare(
      "SELECT ref_hosts FROM finish2_grants WHERE slug = ?").bind(slug).first();
    if (g && g.ref_hosts) {
      const arr = JSON.parse(g.ref_hosts);
      if (Array.isArray(arr) && arr.length) return arr;
    }
  } catch { /* fall through */ }
  try {
    const { results } = await env.LINKGUARD_DB.prepare(
      "SELECT host FROM ref_hosts").all();
    return (results || []).map(r => r.host);
  } catch { return []; }
}

/* ─────────────────────────────── Admin API ─────────────────────────────── */

async function handleAdmin(request, env, path) {
  const key = request.headers.get("x-admin-key") || "";
  if (!env.ADMIN_API_KEY || !timingSafeEq(key, env.ADMIN_API_KEY))
    return json({ error: "unauthorized" }, 401);

  if (path === "/api/admin/mint" && request.method === "POST") {
    const { destination, ttl_days, honeypot, grant_slug } = await request.json();
    if (!destination || !/^https?:\/\//.test(destination))
      return json({ error: "bad_destination" }, 400);
    const slug = await newSlug(env);
    const exp = ttl_days ? Date.now() + ttl_days * 86400e3 : null;
    try {
      await env.LINKGUARD_DB.prepare(
        `INSERT INTO slugs (slug, destination, status, created_at, expires_at, honeypot, grant_slug)
         VALUES (?, ?, 'active', ?, ?, ?, ?)`
      ).bind(slug, destination, Date.now(), exp, honeypot ? 1 : 0, grant_slug || null).run();
    } catch {
      await env.LINKGUARD_DB.prepare(
        `INSERT INTO slugs (slug, destination, status, created_at, expires_at, honeypot)
         VALUES (?, ?, 'active', ?, ?, ?)`
      ).bind(slug, destination, Date.now(), exp, honeypot ? 1 : 0).run();
    }
    const host = env.WORKER_HOSTNAME || "";
    await logEvent(env, "slug_minted", { slug, honeypot: !!honeypot, grant: !!grant_slug });
    return json({ slug, url: `https://${host}/${slug}`, expires_at: exp });
  }

  // v1.1: mint the exit grant. The shortener points at finish2_url, NOT the
  // final Telegram deep link. final_destination is stored server-side and is
  // only revealed after every /finish2 check passes. session_id starts empty
  // and is bound to the session that solves Turnstile (see handleClaim).
  if (path === "/api/admin/mint2" && request.method === "POST") {
    const { final_destination, ref_hosts } = await request.json();
    if (!final_destination || !/^https?:\/\//.test(final_destination))
      return json({ error: "bad_destination" }, 400);
    const slug = await newSlug(env);
    const ttlMin = parseInt(env.GRANT_TTL_MINUTES || "60", 10);
    const exp = Math.floor(Date.now() / 1000) + ttlMin * 60;
    const rnd = crypto.randomUUID().replace(/-/g, "");
    const payload = `${slug}.pending.${exp}.${rnd}`;
    const sig = await hmac(env.HMAC_SECRET, payload);
    const grant = b64url(payload) + "." + sig;
    const hosts = Array.isArray(ref_hosts) && ref_hosts.length
      ? JSON.stringify(ref_hosts) : null;
    await env.LINKGUARD_DB.prepare(
      `INSERT INTO finish2_grants (slug, final_destination, session_id, ref_hosts, expires_at, used, created_at)
       VALUES (?, ?, '', ?, ?, 0, ?)`
    ).bind(slug, final_destination, hosts, exp, Date.now()).run();
    const host = env.WORKER_HOSTNAME || "";
    await logEvent(env, "grant_minted", { slug, ttlMin });
    return json({ slug, finish2_url: `https://${host}/finish2?s=${slug}&t=${encodeURIComponent(grant)}`, expires_at: exp });
  }

  // v1.1: the bot pushes its live shortener-domain list here (fallback
  // allowlist when a grant carries no per-grant list).
  if (path === "/api/admin/ref_hosts" && request.method === "POST") {
    const { hosts } = await request.json();
    if (!Array.isArray(hosts)) return json({ error: "bad_hosts" }, 400);
    await env.LINKGUARD_DB.prepare("DELETE FROM ref_hosts").run();
    if (hosts.length) {
      const stmt = env.LINKGUARD_DB.prepare("INSERT INTO ref_hosts (host) VALUES (?)");
      await env.LINKGUARD_DB.batch(hosts.map(h => stmt.bind(String(h))));
    }
    await logEvent(env, "ref_hosts_updated", { count: hosts.length });
    return json({ ok: true, count: hosts.length });
  }

  if (path === "/api/admin/revoke" && request.method === "POST") {
    const { slug } = await request.json();
    const r = await env.LINKGUARD_DB.prepare(
      "UPDATE slugs SET status = 'revoked' WHERE slug = ?"
    ).bind(slug || "").run();
    await logEvent(env, "slug_revoked", { slug });
    return json({ ok: r.meta.changes > 0 });
  }

  if (path === "/api/admin/decoys" && request.method === "POST") {
    const { urls } = await request.json();
    if (!Array.isArray(urls) || !urls.length) return json({ error: "bad_urls" }, 400);
    const stmt = env.LINKGUARD_DB.prepare(
      "INSERT INTO decoys (url, active) VALUES (?, 1)");
    await env.LINKGUARD_DB.batch(urls.map(u => stmt.bind(String(u))));
    return json({ ok: true, added: urls.length });
  }

  if (path === "/api/admin/logs" && request.method === "GET") {
    const { results } = await env.LINKGUARD_DB.prepare(
      "SELECT * FROM logs ORDER BY id DESC LIMIT 50").all();
    return json({ logs: results });
  }

    // v1.2 — observe-mode + host-learning
  if (path === "/api/admin/observe" && request.method === "POST") {
    const body = await request.json().catch(() => ({}));
    const on = body.on !== false;
    const minutes = on ? Math.min(Math.max(Number(body.minutes) || 60, 1), 720) : 0;
    await ensureV12Tables(env);
    const until = String(Date.now() + minutes * 60000);
    await env.LINKGUARD_DB.prepare(
      "INSERT INTO lg_state (key, value) VALUES ('observe_until', ?) " +
      "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    ).bind(until).run();
    await logEvent(env, "observe_mode", { on, minutes });
    return json({ ok: true, observe_on: on, minutes });
  }
  if (path === "/api/admin/observed_hosts" && request.method === "POST") {
    await ensureV12Tables(env);
    const rows = await env.LINKGUARD_DB.prepare(
      "SELECT host, first_seen, last_seen, hits FROM ref_observed ORDER BY last_seen DESC LIMIT 50").all();
    const ob = await env.LINKGUARD_DB.prepare(
      "SELECT value FROM lg_state WHERE key = 'observe_until'").first();
    return json({ ok: true, observe_on: !!(ob && Number(ob.value) > Date.now()),
                  hosts: rows.results || [] });
  }
  if (path === "/api/admin/ref_hosts_add" && request.method === "POST") {
    const body = await request.json().catch(() => ({}));
    let host = String(body.host || "").trim().toLowerCase().replace(/^\*\./, "");
    host = hostOf(host) || host;               // tolerate a pasted full URL
    if (!host || !/^[a-z0-9.-]+\.[a-z]{2,}$/.test(host))
      return json({ error: "bad_host" }, 400);
    try {
      // schema-tolerant insert: mirror an existing row's shape if there is one
      const sample = await env.LINKGUARD_DB.prepare("SELECT * FROM ref_hosts LIMIT 1").all();
      const row0 = (sample.results || [])[0] || null;
      if (row0) {
        const keys = Object.keys(row0);
        const hostCol = keys.find(k => /host/i.test(k)) || keys[0];
        const vals = keys.map(k =>
          k === hostCol ? host :
          k.toLowerCase() === "id" ? null : row0[k]);
        await env.LINKGUARD_DB.prepare(
          `INSERT OR IGNORE INTO ref_hosts (${keys.join(",")}) VALUES (${keys.map(() => "?").join(",")})`
        ).bind(...vals).run();
      } else {
        await env.LINKGUARD_DB.prepare(
          "INSERT OR IGNORE INTO ref_hosts (host) VALUES (?)").bind(host).run();
      }
      await logEvent(env, "ref_host_added", { host });
      return json({ ok: true, added: host });
    } catch (e) {
      return json({ error: "insert_failed", detail: String(e) }, 500);
    }
  }
  if (path === "/api/admin/observed_clear" && request.method === "POST") {
    await ensureV12Tables(env);
    await env.LINKGUARD_DB.prepare("DELETE FROM ref_observed").run();
    return json({ ok: true });
  }
  return json({ error: "unknown_admin_route" }, 404);
}

async function newSlug(env) {
  const abc = "abcdefghjkmnpqrstuvwxyz23456789";
  for (let i = 0; i < 10; i++) {
    let s = "";
    const bytes = crypto.getRandomValues(new Uint8Array(8));
    for (const b of bytes) s += abc[b % abc.length];
    const hit = await env.LINKGUARD_DB.prepare(
      "SELECT 1 FROM slugs WHERE slug = ?").bind(s).first();
    if (!hit) return s;
  }
  throw new Error("slug_space_exhausted");
}

/* ─────────────────────────────── Helpers ───────────────────────────────── */

async function hmac(secret, msg) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(msg));
  return [...new Uint8Array(mac)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function b64url(s) { return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
function atobUrl(s) { return atob(s.replace(/-/g, "+").replace(/_/g, "/")); }

function timingSafeEq(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

function cookieVal(request, name) {
  const c = request.headers.get("cookie") || "";
  for (const part of c.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return v.join("=");
  }
  return null;
}

function hostOf(url) {
  try { return new URL(url).hostname.toLowerCase(); } catch { return ""; }
}

async function rateOk(env, key, limit, windowSec) {
  const bucket = Math.floor(Date.now() / (windowSec * 1000));
  const id = `${key}:${bucket}`;
  await env.LINKGUARD_DB.prepare(
    `INSERT INTO rate (id, count) VALUES (?, 1)
     ON CONFLICT(id) DO UPDATE SET count = count + 1`
  ).bind(id).run();
  const row = await env.LINKGUARD_DB.prepare(
    "SELECT count FROM rate WHERE id = ?").bind(id).first();
  return (row?.count || 0) <= limit;
}

async function logEvent(env, event, data) {
  try {
    await env.LINKGUARD_DB.prepare(
      "INSERT INTO logs (ts, event, data) VALUES (?, ?, ?)"
    ).bind(Date.now(), event, JSON.stringify(data || {})).run();
  } catch { /* logging must never break the flow */ }
}

async function alertAdmins(env, message) {
  if (!env.TG_BOT_TOKEN || !env.TG_ADMIN_IDS) return;
  const ids = String(env.TG_ADMIN_IDS).split(",").map(s => s.trim()).filter(Boolean);
  await Promise.allSettled(ids.map(id =>
    fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: id, text: message }),
    })));
}

/* ─────────────────── v1.2: observe-mode helpers ─────────────────── */

let _v12TablesReady = false;
async function ensureV12Tables(env) {
  if (_v12TablesReady) return;
  await env.LINKGUARD_DB.prepare(
    "CREATE TABLE IF NOT EXISTS lg_state (key TEXT PRIMARY KEY, value TEXT)").run();
  await env.LINKGUARD_DB.prepare(
    "CREATE TABLE IF NOT EXISTS ref_observed (host TEXT PRIMARY KEY, first_seen INTEGER, last_seen INTEGER, hits INTEGER)").run();
  _v12TablesReady = true;
}

async function observeActive(env) {
  await ensureV12Tables(env);
  const row = await env.LINKGUARD_DB.prepare(
    "SELECT value FROM lg_state WHERE key = 'observe_until'").first();
  return !!(row && Number(row.value) > Date.now());
}

async function recordObservedHost(env, host) {
  const now = Date.now();
  await env.LINKGUARD_DB.prepare(
    "INSERT INTO ref_observed (host, first_seen, last_seen, hits) VALUES (?, ?, ?, 1) " +
    "ON CONFLICT(host) DO UPDATE SET last_seen = excluded.last_seen, hits = hits + 1"
  ).bind(host, now, now).run();
}

async function deny(env, slug, sessionId, ip, reason, status = 403, alert = false) {
  await logEvent(env, "claim_denied", { slug, sessionId, ip, reason });
  if (alert) await alertAdmins(env, `🚫 Claim denied (${reason})\nslug: ${slug}\nip: ${ip}`);
  return json({ reason }, status, NOSTORE);
}

async function denyFinish(env, token, ip, reason, extra = null, alert = false) {
  await logEvent(env, "finish_denied", { ip, reason, extra });
  if (alert) await alertAdmins(env, `🚫 Finish denied (${reason})\nip: ${ip}`);
  return json({ error: "forbidden", reason }, 403, NOSTORE);
}

function json(obj, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), { status, headers: { ...JSON_CT, ...extraHeaders } });
}
function text(t, status = 200) {
  return new Response(t, { status, headers: { "content-type": "text/plain; charset=utf-8" } });
}
function html(h, status = 200) {
  return new Response(h, { status, headers: { "content-type": "text/html; charset=utf-8", ...NOSTORE } });
}
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;"); }
