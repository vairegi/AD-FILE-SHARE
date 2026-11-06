/**
 * test_worker_return_proof.mjs — runs the REAL worker.js against a local
 * node:sqlite database (D1 API shim). No Cloudflare, no network, Turnstile
 * mocked. Verifies the v1.3 paste-back defence and the normal human flow.
 *
 *   node --experimental-sqlite test_worker_return_proof.mjs
 */
import { DatabaseSync } from "node:sqlite";
import worker from "./worker.js";

const HOST = "linkguard.test";
const UA = "Mozilla/5.0 (Linux; Android 14) Chrome/155 Mobile Safari/537.36";
const IP = "152.58.129.25";
const SHORT_HOST = "vplink.in";

const db = new DatabaseSync(":memory:");
db.exec(`
CREATE TABLE slugs (slug TEXT PRIMARY KEY, destination TEXT, status TEXT,
  created_at INTEGER, expires_at INTEGER, honeypot INTEGER, grant_slug TEXT);
CREATE TABLE sessions (id TEXT PRIMARY KEY, slug TEXT, ip TEXT, ua TEXT, nonce TEXT,
  created_at INTEGER, turnstile_ok INTEGER, state TEXT);
CREATE TABLE claims (token TEXT PRIMARY KEY, slug TEXT, session_id TEXT, ip TEXT,
  ua TEXT, expires_at INTEGER, used INTEGER);
CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, event TEXT, data TEXT);
CREATE TABLE decoys (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, active INTEGER);
CREATE TABLE rate (id TEXT PRIMARY KEY, count INTEGER);
CREATE TABLE finish2_grants (slug TEXT PRIMARY KEY, final_destination TEXT,
  session_id TEXT, ref_hosts TEXT, expires_at INTEGER, used INTEGER, created_at INTEGER);
CREATE TABLE ref_hosts (host TEXT PRIMARY KEY);
`);

class Stmt {
  constructor(db, sql) { this.db = db; this.sql = sql; this.args = []; }
  bind(...a) { const s = new Stmt(this.db, this.sql); s.args = a; return s; }
  async first() { return this.db.prepare(this.sql).get(...this.args) ?? null; }
  async all() { return { results: this.db.prepare(this.sql).all(...this.args) }; }
  async run() {
    const r = this.db.prepare(this.sql).run(...this.args);
    return { meta: { changes: Number(r.changes || 0) } };
  }
}

const D1 = {
  prepare: (sql) => new Stmt(db, sql),
  async batch(stmts) { const out = []; for (const s of stmts) out.push(await s.run()); return out; },
};

const env = {
  LINKGUARD_DB: D1,
  WORKER_HOSTNAME: HOST,
  HMAC_SECRET: "test-hmac-secret-0123456789",
  ADMIN_API_KEY: "test-admin-key",
  TURNSTILE_SECRET: "test-turnstile",
  TURNSTILE_SITEKEY: "test-sitekey",
  LANDING_WAIT_SECONDS: "1",
  DECOY_COUNT: "0",
  GRANT_TTL_MINUTES: "60",
  RETURN_POLICY: "nav_only",
  ALLOW_EMPTY_REFERER: "true",   // the production setting, on purpose
};

globalThis.fetch = async (url) => {
  const u = String(url);
  if (u.includes("challenges.cloudflare.com"))
    return new Response(JSON.stringify({ success: true, hostname: HOST }),
      { status: 200, headers: { "content-type": "application/json" } });
  throw new Error("external network disabled: " + u);
};

const results = [];
function check(name, cond, detail = "") {
  results.push({ name, pass: !!cond, detail });
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}

const base = `https://${HOST}`;
const ctx = {};

function req(path, { method = "GET", headers = {}, body = null } = {}) {
  return new Request(base + path, {
    method, body,
    headers: { "user-agent": UA, "cf-connecting-ip": IP, ...headers },
  });
}

async function admin(path, payload) {
  const r = await worker.fetch(req(path, {
    method: "POST",
    headers: { "x-admin-key": "test-admin-key", "content-type": "application/json" },
    body: JSON.stringify(payload),
  }), env, { waitUntil() {} });
  return { status: r.status, json: await r.json() };
}

/* ── 1. mint the post: public slug + its exit grant ─────────────── */
const finalDest = "https://t.me/deliverybot_test?start=deliver_jav_f385_7f7976de";
const grant = await admin("/api/admin/mint2", {
  final_destination: finalDest, ref_hosts: [SHORT_HOST],
});
check("mint2 returns finish2_url", grant.status === 200 && !!grant.json.finish2_url,
  grant.json.finish2_url ? "" : JSON.stringify(grant.json));
ctx.grantSlug = grant.json.slug;
ctx.finish2Url = grant.json.finish2_url;
const finish2Path = new URL(ctx.finish2Url).pathname + new URL(ctx.finish2Url).search;
const f2 = (mint) => {
  const u = new URL(mint.finish2_url);
  return u.pathname + u.search;
};

const shortDest = `https://${SHORT_HOST}/aq60`;
const pub = await admin("/api/admin/mint", { destination: shortDest, grant_slug: ctx.grantSlug });
ctx.pubSlug = pub.json.slug;
check("mint links public slug to grant", pub.status === 200 && !!ctx.pubSlug);

/* ── 2. Door 2: landing sets the lg_s session cookie ───────────── */
async function openLanding() {
  const r = await worker.fetch(req("/" + ctx.pubSlug), env, { waitUntil() {} });
  const html = await r.text();
  const cookie = (r.headers.get("set-cookie") || "").split(";")[0];
  const sid = cookie.split("=")[1];
  const nonce = (html.match(/NONCE\s*=\s*["']([a-f0-9]+)["']/) ||
                 html.match(/nonce\s*=\s*["']([a-f0-9]+)["']/) ||
                 html.match(/"nonce"\s*:\s*"([a-f0-9]+)"/) || [])[1];
  return { status: r.status, cookie, sid, nonce, html };
}

const land = await openLanding();
check("landing served with lg_s cookie", land.status === 200 && !!land.sid && !!land.nonce,
  "sid=" + String(land.sid).slice(0, 8));

/* ── 3. Door 1 proof + claim + departure for the shortener ─────── */
async function claim(landing) {
  const r = await worker.fetch(req("/api/claim", {
    method: "POST",
    headers: { cookie: landing.cookie, "content-type": "application/json" },
    body: JSON.stringify({
      slug: ctx.pubSlug, session_id: landing.sid, cf_token: "turnstile-ok",
      nonce: landing.nonce,
    }),
  }), env, { waitUntil() {} });
  return { status: r.status, json: await r.json() };
}

const cl = await claim(land);
check("claim mints a Door-3 token", cl.status === 200 && !!cl.json.token,
  JSON.stringify(cl.json).slice(0, 80));

const entry = await worker.fetch(req("/finish?t=" + encodeURIComponent(cl.json.token), {
  headers: { referer: `${base}/${ctx.pubSlug}` },
}), env, { waitUntil() {} });
check("legit /finish redirects to the shortener", entry.status === 302 &&
  entry.headers.get("location") === shortDest, String(entry.status));

/* ── 4. THE BYPASS: paste the returned /finish2 URL into the same browser ── */
const pasted = await worker.fetch(req(finish2Path, {
  headers: {
    cookie: land.cookie,
    "sec-fetch-site": "none",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
  },
}), env, { waitUntil() {} });
check("BYPASS blocked: pasted /finish2 in the verified browser -> 403",
  pasted.status === 403, `status ${pasted.status} (v1.2 returned 302)`);

const pasted2 = await worker.fetch(req(finish2Path, {
  headers: { cookie: land.cookie, "sec-fetch-site": "none" },
}), env, { waitUntil() {} });
check("bypass stays blocked on retry", pasted2.status === 403, `status ${pasted2.status}`);

const botFetch = await worker.fetch(req(finish2Path, {
  headers: { cookie: land.cookie, "user-agent": "python-httpx/0.27" },
}), env, { waitUntil() {} });
check("bypass bot fetching directly (no Sec-Fetch, no Referer) -> 403",
  botFetch.status === 403, `status ${botFetch.status}`);

const spoofed = await worker.fetch(req(finish2Path, {
  headers: { cookie: land.cookie, referer: "https://evil.example/x", "sec-fetch-site": "none" },
}), env, { waitUntil() {} });
check("paste with unapproved Referer -> 403", spoofed.status === 403, `status ${spoofed.status}`);

await admin("/api/admin/observe", { on: true, minutes: 10 });
const observedPaste = await worker.fetch(req(finish2Path, {
  headers: { cookie: land.cookie, referer: "https://evil.example/x", "sec-fetch-site": "none" },
}), env, { waitUntil() {} });
check("observe-mode cannot rescue a pasted URL", observedPaste.status === 403,
  `status ${observedPaste.status}`);
await admin("/api/admin/observe", { on: false, minutes: 0 });

/* ── 5. Release status must report NOT released while blocked ───── */
async function releaseStatus(slug) {
  return (await admin("/api/admin/release_status", { slug })).json;
}
let rs = await releaseStatus(ctx.grantSlug);
check("release_status: blocked attempt is not 'released'",
  rs.released === false, `released=${rs.released} reason=${rs.reason}`);

/* ── 6. The REAL human path: shortener redirects back cross-site ── */
const legit = await worker.fetch(req(finish2Path, {
  headers: {
    cookie: land.cookie,
    referer: `https://${SHORT_HOST}/continue`,
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
  },
}), env, { waitUntil() {} });
check("legit shortener return -> 302 to the Telegram deep link",
  legit.status === 302 && legit.headers.get("location") === finalDest,
  `status ${legit.status}`);
check("legit return is marked no-store",
  (legit.headers.get("cache-control") || "").includes("no-store"));

rs = await releaseStatus(ctx.grantSlug);
check("release_status: legit completion reports released=true",
  rs.released === true && rs.release_site === "cross-site",
  `released=${rs.released} site=${rs.release_site}`);

const replay = await worker.fetch(req(finish2Path, {
  headers: {
    cookie: land.cookie, referer: `https://${SHORT_HOST}/continue`,
    "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
  },
}), env, { waitUntil() {} });
check("grant is single-use (replay -> 403)", replay.status === 403, `status ${replay.status}`);

/* ── 7. Referer stripped by policy still works in nav_only ─────── */
const stripped = await admin("/api/admin/mint2", {
  final_destination: finalDest, ref_hosts: [SHORT_HOST],
});
const pub2 = await admin("/api/admin/mint", { destination: shortDest, grant_slug: stripped.json.slug });
ctx.pubSlug = pub2.json.slug;
const land2 = await openLanding();
const cl2 = await claim(land2);
await worker.fetch(req("/finish?t=" + encodeURIComponent(cl2.json.token), {
  headers: { referer: `${base}/${ctx.pubSlug}` },
}), env, { waitUntil() {} });
const s2 = new URL(stripped.json.finish2_url);
const noRef = await worker.fetch(req(s2.pathname + s2.search, {
  headers: {
    cookie: land2.cookie,
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
  },
}), env, { waitUntil() {} });
check("real human with Referer stripped by policy still passes (nav_only)",
  noRef.status === 302, `status ${noRef.status}`);

/* ── 8. strict policy rejects a missing Referer ─────────────────── */
const strict = (await admin("/api/admin/mint2", {
  final_destination: finalDest, ref_hosts: [SHORT_HOST],
})).json;
const strictSlug = strict.slug;
const pub3 = await admin("/api/admin/mint", { destination: shortDest, grant_slug: strictSlug });
ctx.pubSlug = pub3.json.slug;
const land3 = await openLanding();
const cl3 = await claim(land3);
await worker.fetch(req("/finish?t=" + encodeURIComponent(cl3.json.token), {
  headers: { referer: `${base}/${ctx.pubSlug}` },
}), env, { waitUntil() {} });
const strictNoRef = await worker.fetch(req(f2(strict), {
  headers: {
    cookie: land3.cookie,
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
  },
}), { ...env, RETURN_POLICY: "strict" }, { waitUntil() {} });
check("strict policy still demands an approved Referer",
  strictNoRef.status === 403, `status ${strictNoRef.status}`);

/* ── 9. Never departed -> cannot redeem ────────────────────────── */
const p4 = await admin("/api/admin/mint2", { final_destination: finalDest, ref_hosts: [SHORT_HOST] });
const p4pub = await admin("/api/admin/mint", { destination: shortDest, grant_slug: p4.json.slug });
ctx.pubSlug = p4pub.json.slug;
const land4 = await openLanding();
await claim(land4);
const noDepart = await worker.fetch(req(f2(p4.json), {
  headers: {
    cookie: land4.cookie, referer: `https://${SHORT_HOST}/continue`,
    "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
  },
}), env, { waitUntil() {} });
check("grant that never reached the shortener cannot be redeemed",
  noDepart.status === 403, `status ${noDepart.status}`);
const rs4 = await releaseStatus(p4.json.slug);
check("release_status: undeparted grant is not released",
  rs4.released === false, `reason=${rs4.reason}`);

/* ── 10. Foreign browser / foreign UA still refused ─────────────── */
const p5 = await admin("/api/admin/mint2", { final_destination: finalDest, ref_hosts: [SHORT_HOST] });
const p5pub = await admin("/api/admin/mint", { destination: shortDest, grant_slug: p5.json.slug });
ctx.pubSlug = p5pub.json.slug;
const land5 = await openLanding();
const cl5 = await claim(land5);
await worker.fetch(req("/finish?t=" + encodeURIComponent(cl5.json.token), {
  headers: { referer: `${base}/${ctx.pubSlug}` },
}), env, { waitUntil() {} });
const niceHeaders = {
  referer: `https://${SHORT_HOST}/continue`,
  "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
};
const noCookie = await worker.fetch(req(f2(p5.json), {
  headers: niceHeaders,
}), env, { waitUntil() {} });
check("no session cookie -> 403", noCookie.status === 403, `status ${noCookie.status}`);
const badUa = await worker.fetch(req(f2(p5.json), {
  headers: { ...niceHeaders, cookie: land5.cookie, "user-agent": "curl/8.0" },
}), env, { waitUntil() {} });
check("UA mismatch -> 403", badUa.status === 403, `status ${badUa.status}`);

/* ── 11. Concurrent redemption: exactly one wins ───────────────── */
const p6 = await admin("/api/admin/mint2", { final_destination: finalDest, ref_hosts: [SHORT_HOST] });
const p6pub = await admin("/api/admin/mint", { destination: shortDest, grant_slug: p6.json.slug });
ctx.pubSlug = p6pub.json.slug;
const land6 = await openLanding();
const cl6 = await claim(land6);
await worker.fetch(req("/finish?t=" + encodeURIComponent(cl6.json.token), {
  headers: { referer: `${base}/${ctx.pubSlug}` },
}), env, { waitUntil() {} });
const race = await Promise.all([1, 2, 3].map(() => worker.fetch(
  req(f2(p6.json), { headers: { ...niceHeaders, cookie: land6.cookie } }),
  env, { waitUntil() {} })));
const wins = race.filter(r => r.status === 302).length;
if (wins !== 1) {
  const bodies = await Promise.all(race.map(r => r.clone().text()));
  console.log("RACE DEBUG bodies: " + bodies.map(b => b.slice(0, 160)).join(" ||| "));
  const dep = await D1.prepare("SELECT used, departed_at, released_at FROM finish2_grants WHERE slug = ?")
    .bind(p6.json.slug).first();
  console.log("RACE DEBUG grant row: " + JSON.stringify(dep));
  const lg = await D1.prepare("SELECT event, data FROM logs WHERE event LIKE 'finish2%' ORDER BY id DESC LIMIT 8").all();
  console.log("RACE DEBUG logs: " + JSON.stringify(lg.results));
}
check("atomic single use: exactly one 302 across 3 concurrent redemptions",
  wins === 1, `statuses ${race.map(r => r.status).join(",")}`);

/* ── 12. health + unknown routes unchanged ─────────────────────── */
const health = await worker.fetch(req("/api/health"), env, { waitUntil() {} });
check("/api/health still 200", health.status === 200);
const unknown = await admin("/api/admin/nope", {});
check("unknown admin route still 404", unknown.status === 404);

const failed = results.filter(r => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} worker checks passed`);
if (failed.length) {
  console.log("FAILED:\n" + failed.map(f => " - " + f.name + " :: " + f.detail).join("\n"));
  process.exit(1);
}
