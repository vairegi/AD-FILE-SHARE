"""botapi.py — control BOT (BOT_TOKEN from BotFather) with a tappable menu.
v13: per-target DB channels. /target wizard now asks for the channel id AND
then its DB channel; /setdb adjusts a target's DB later; /reset and /goto are
per-target; all wizards accept inline args (/adddb -100…); /help lists every
command. Only ADMIN_USER_ID can use it."""
from telethon import TelegramClient, events
from telethon.sessions import MemorySession
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from config import (API_ID, API_HASH, BOT_TOKEN, ADMIN_USER_ID, BTN_SHORT_LINK,
                    BULK_EDIT_DELAY, BULK_MAX_FLOOD, BULK_PROGRESS_EVERY)
from telethon.errors import FloodWaitError
import asyncio
import difflib
import logging
import re
import shlex
import time
import random
import db as DB
import mtprotomgr as MTM
import dedup  # v45/v46: DB2 duplicate-skip index + auto-index + alerts
import richboard
from flow import state
from telethon.tl.types import Channel, Chat, User

bot = None  # created lazily inside start() (Py3.14 has no loop at import time)

_CMDS = [
    # v40: regrouped for the tappable menu — related commands sit together
    ("help",     "Show all commands"),
    ("ping",     "Check the bot is alive"),
    ("stats",    "v52: rich worker/membership table (compact Bot API table)"),
    ("addworker",  "v52: add a scraping worker live: /addworker <session string> (bare = list all)"),
    ("removeworker", "v52: remove a bot-added worker: /removeworker <bot#>"),
    ("checkram", "Show RAM usage (process + container, Render 512MB cap)"),
    ("target",   "Add target channel + its DB channel (wizard)"),
    ("targets",  "Live charge-sheet board (table + buttons) — plain list: /targets_text"),
    ("targets_text", "Plain markdown targets list (fallback)"),
    ("deltarget","Remove a target channel"),
    ("setdb",    "Change a target's DB channel"),
    ("adddb",    "Set the fallback DB channel"),
    ("bypass",   "Set bypass bot #1 (the pool's first entry)"),
    ("addbypass","Add another bypass bot to the pool (wizard)"),
    ("removebypass","Remove a pool bypass bot: /removebypass <n>"),
    ("bypasslist","List the bypass pool + domain rules"),
    ("domainbypass","Map a short-link domain to one bypass bot (wizard)"),
    ("deldomain","Remove a domain rule: /deldomain <n>"),
    ("altbypass","Last-resort fallback bypass — tried after every pool bot fails"),
    ("linkbutton", "List or add LINK_BOT button labels (no restart)"),
    ("targatelinkmode", "v42: target link mode: /targatelinkmode <n> button|caption [text]"),
    ("keepimages", "v42: collect media-bot images too: /keepimages on|off (global)"),
    ("removelinkbutton", "Remove a LINK_BOT button label by number"),
    ("goto",     "Set a target's start message (/goto <n> <msg> or link)"),
    ("reset",    "Reset a target's progress to post 1"),
    ("lastpost", "Newest post in a target channel"),
    ("scan4duplicates", "v43.3: find duplicate COVER posts in a DB2 channel (story-only fuzzy match)"),
    ("dupescan", "v45: rebuild a target's DB2 duplicate-skip index: /dupescan <n> (v46 auto-indexes new DB2 covers)"),
    ("start",    "Start scraping"),
    ("pause",    "Pause all (bare or /pause all), or one: /pause 2"),
    ("resume",   "Resume all (bare or /resume all), or one: /resume 2"),
    ("status",   "Live stage & config"),
    ("current",  "Current post & stage"),
    ("progress", "Stats, last post, failure reasons"),
    ("skip",     "Skip current post"),
    ("stop",     "Stop the scraper"),
    ("cancel",   "Cancel an active wizard prompt"),
    ("replace",  "Edit posts: /replace <ch> \"old\" \"new\" (userbot) — 2 args = DB2 (bot)"),
    ("deletetext","Remove text from channel posts: /deletetext <ch> \"text\""),
    ("massdlt",  "Delete a message range: /massdlt <chat> <start_link> <end_link>"),
    ("massdlt_status", "Mass-delete progress"),
    ("massdlt_stop", "Stop the mass-delete"),
    ("forward",  "Copy range to a channel: /forward <target> <source> <start> <end>"),
    ("forward_status", "Forward progress"),
    ("forward_stop", "Stop the forward (resumable)"),
    ("forward_resume", "Resume a stopped/interrupted forward"),
    ("add",      "Add bot(s) as admin: /add <channel> @bot1 @bot2 …"),
    ("addadmin", "Add a bot admin (bare /addadmin lists them)"),
    ("removeadmin", "Remove a bot admin: /removeadmin <user id>"),
    ("setdb2",   "Set a target's DB2 clean-mirror channel: /setdb2 <n> <id|off>"),
    ("avoidtext","DB2: strip a credit string: /avoidtext <n> \"multi word text\" (bare = list)"),
    ("removeavoid", "Remove an avoid string: /removeavoid <n> <#>"),
    ("checkdm",   "Auto admin pipeline: /checkdm on|off (userbot watches @richmining DMs)"),
    ("invite",   "Userbot joins a channel: /invite [n] <link> (no n = ALL)"),
    ("leave",    "Userbot leaves a channel: /leave [n] <channel id|link>"),
    ("avoid",    "DB2 GLOBAL strip text: /avoid \"txt\" (bare = list)"),
    ("removegavoid", "Remove a global avoid: /removegavoid <#>"),
    ("replaceword",  "DB2 GLOBAL replace: /replaceword \"old\" \"new\" (bare = list)"),
    ("removereplace", "Remove a replace rule: /removereplace <#>"),
]

_pending = {}  # user_id -> (kind, extra)
_sm = [None]   # v40: SessionManager, set by register()
_BG_TASKS = []  # v46: keep-alive refs for background tasks (DB2 tail-scan scheduler)
log_mirror = logging.getLogger("db2mirror")

# v39: validate a bypass endpoint (GROUP id or BOT @username) via the
# userbot — shared by /bypass, /altbypass, /addbypass and /domainbypass.
async def _validate_bypass_endpoint(scrape_client, ev, v):
    """Returns (ok, is_bot). Replies with the problem and returns (False,
    None) when the userbot can't resolve it or it isn't a group/bot."""
    try:
        ent = await scrape_client.get_entity(v)
    except Exception as e:
        await ev.reply(f"⚠️ Can't access that chat with the userbot account: {e}")
        return False, None
    is_group = _entity_ok(ent, "group")
    is_bot = isinstance(ent, User) and getattr(ent, "bot", False)
    if not (is_group or is_bot):
        await ev.reply(
            f"⚠️ {v} isn't a group and isn't a bot — bypass must be one of those.\n"
            "For a bot, send its @username (e.g. @dex_fekkyeww_bot). "
            "For a group, paste any message link from the group "
            "(https://t.me/c/1234567890/12) or its -100… id.")
        return False, None
    return True, is_bot


async def _admin(uid):
    """The owner (ADMIN_USER_ID) always passes; ids added via /addadmin
    (Mongo-backed) also get full access."""
    if ADMIN_USER_ID == 0 or uid == ADMIN_USER_ID:
        return True
    return uid in await DB.get_admins()


async def _menu():
    await bot(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(), lang_code="",
        commands=[BotCommand(c, d) for c, d in _CMDS]))


def _parse_id(raw):
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError:
        return raw.lstrip("@")


C_LINK = re.compile(r"(?:https?://)?t\.me/c/(\d+)(?:/\d+)?")


def _parse_chat_id(raw):
    """Numeric id, @username, or a t.me/c/<channel>[/<msg>] link -> chat id."""
    raw = raw.strip()
    m = C_LINK.search(raw)
    if m:
        return int("-100" + m.group(1))
    return _parse_id(raw)


def _not_a_channel_msg(v, kind="channel"):
    return (f"⚠️ {v} resolves to a USER/BOT, not a {kind} — nothing can be posted there.\n"
            "Easiest fix: copy ANY message link from the target chat "
            "(looks like https://t.me/c/1234567890/12) and paste that link instead of the id.")


def _entity_ok(ent, need):
    """need='channel' -> Channel only; need='group' -> Channel or Chat."""
    if need == "channel":
        return isinstance(ent, Channel)
    return isinstance(ent, (Channel, Chat))


async def _db_invite_link(client, db_id):
    """Tappable invite link for a DB channel, minted via the userbot (it is
    admin there) and cached in Mongo — no new link on every /targets call."""
    if not db_id or client is None:
        return None
    key = f"db_link_{db_id}"
    cached = await DB.get_config(key)
    if cached:
        return cached
    try:
        inv = await client(ExportChatInviteRequest(db_id))
    except Exception:
        return None  # no permission / private without invite rights -> plain id
    link = getattr(inv, "link", None)
    if link:
        await DB.set_config(key, link)
    return link


_TITLE_CACHE = {}  # chat_id -> title (titles rarely change)


async def _chat_title(client, chat_id):
    """Resolve a channel/group title via the userbot (it's a member there).
    Cached in-memory so repeat /targets calls don't spam get_entity."""
    if chat_id is None:
        return None
    if chat_id in _TITLE_CACHE:
        return _TITLE_CACHE[chat_id]
    title = None
    if client is not None:
        try:
            ent = await client.get_entity(chat_id)
            title = getattr(ent, "title", None) or getattr(ent, "first_name", None)
        except Exception:
            title = None
    if title:
        _TITLE_CACHE[chat_id] = title
    return title



# ---------------- v43.2: story-only caption extraction for /scan4duplicates ----------------
# Captions share boilerplate that makes UNRELATED posts look alike: hashtag
# lines (#uncensored #recommended #new), metadata lines (▸ Episode:- 1,
# ▸ Subtitle:- English, ▸ Censorship: #Uncensored, ▸ Rating:- ...,
# 18+ Network: @…) and Telegram's "edited <date>" tail. The duplicate scan
# must compare ONLY the story description, so those lines are stripped BEFORE
# the first 10 words are taken.
_NONSTORY_LINE_RE = re.compile(
    r"(?:^|\b)(?:episode|subtitles?|censor(?:ship|ed)?|rating|network|audio|"
    r"quality|resolution|size|duration|genre|genres|studio|release|language|"
    r"source|seed|leech)\s*[:\-]", re.I)
_EDITED_TAIL_RE = re.compile(r"edited\s+\w+\s+\d+.*$", re.I)


def _story_snippet(text, n_words=10):
    """Caption -> normalized first-10-words of the STORY lines only.
    Drops: metadata lines (keyword + ':' anywhere, bullet or not), lines that
    are only hashtags, emoji-only lines, and the 'edited' tail. Words are
    lowercased with punctuation stripped, ready for SequenceMatcher."""
    if not text:
        return ""
    text = _EDITED_TAIL_RE.sub("", text)
    keep = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if _NONSTORY_LINE_RE.search(ln):
            continue
        toks = ln.split()
        if toks and all(t.startswith("#") for t in toks):
            continue                      # hashtag-only line
        words = re.findall(r"\w+", ln.lower())
        if not words:
            continue                      # emoji/punctuation-only line
        keep.append(" ".join(words))
    return " ".join(" ".join(keep).split()[:n_words])


def _c_link(chat_id, msg_id=None):
    """t.me/c/<internal>/<msg> — opens a PRIVATE channel for any member, no
    invite link needed. Needs a message id to be tappable, so callers pass
    the last scraped post when they have one."""
    if not msg_id:
        return None
    s = str(chat_id)
    if s.startswith("-100"):
        s = s[4:]
    return f"https://t.me/c/{s}/{msg_id}"


async def _chat_md(client, chat_id, msg_id=None, invite=None):
    """'[title](link)' markdown for a chat. Link priority: given invite link,
    then t.me/c/<id>/<msg_id>. Falls back to the plain title, then raw id."""
    title = await _chat_title(client, chat_id)
    label = (title or str(chat_id)).replace("[", "(").replace("]", ")").replace("\n", " ")
    link = invite or _c_link(chat_id, msg_id)
    return f"[{label}]({link})" if link else label


async def _pause_all_targets():
    """v36: pause the WHOLE scraper PERSISTENTLY and visibly.

    Bare /pause and /pause all used to set only the in-memory state.paused flag.
    If a post was already in flight the pass kept going, /targets showed no pause
    marker, and a restart cleared the flag — so it looked like "it didn't pause".
    We now ALSO flip every target's paused flag in MongoDB (identical to the proven
    /pause <n> path). The scrape loop's active-target filter then skips them all
    within seconds, /targets shows the pause marker, and the pause survives restarts.

    Returns (newly_paused_count, total_targets).
    """
    state.paused = True
    state.user_paused = True          # MANUAL pause: bulk-job auto-resume must not clear it
    targets = await DB.get_targets()
    flipped = 0
    for t in targets:
        if not t.get("paused"):
            await DB.set_target_paused(t["id"], True)
            flipped += 1
        state.paused_ids.add(t["id"])
    state.reset_gen += 1; state._last_scan = None   # drop any in-flight pass
    # v45: flush every pending duplicate-skip batch so no skip is lost on pause
    for _t in targets:
        try:
            await dedup.flush_skips(_t["id"], reason="all targets paused")
        except Exception:
            pass
    return flipped, len(targets)


async def _list_targets_text(client=None):
    targets = await DB.get_targets()
    if not targets:
        return None
    lines = ["🎯 Target channels:"]
    for i, t in enumerate(targets):
        prog = await DB.get_progress(t["id"])
        last = await DB.get_last_post(t["id"])
        # private targets have no invite link — embed the LAST SCRAPED post
        # link instead (works for any member of the channel)
        t_md = await _chat_md(client, t["id"], msg_id=last)
        if t["db_id"]:
            link = await _db_invite_link(client, t["db_id"])
            db_txt = await _chat_md(client, t["db_id"], invite=link)
        else:
            db_txt = "(fallback /adddb)"
        if t.get("db2_id"):
            # v37 FIX: embed the DB2 link too — same invite-link logic as DB
            link2 = await _db_invite_link(client, t["db2_id"])
            db_txt += " → DB2 " + await _chat_md(client, t["db2_id"], invite=link2)
        flag = " ⏸" if t.get("paused") else ""
        lines.append(f"  {i+1}. {t_md} → {db_txt} — resume at msg {prog}{flag}")
    return "\n".join(lines)


async def _target_link(client, tid, last_post):
    """v38: ALWAYS give a target a tappable link. Preferred: an invite link
    minted by the userbot (it is admin in the target channels) and cached in
    Mongo — works even before the first post is scraped. Fallback: the
    t.me/c/<id>/<last_post> trick once scraping has started."""
    inv = await _db_invite_link(client, tid)
    return inv or _c_link(tid, last_post)


async def _build_board_rows(client):
    """v37: gather the per-target data the rich charge-sheet board needs.
    Resolves titles/links once; targets link to their last scraped post (the
    t.me/c/… trick works for private channels), DB/DB2 use cached invite links."""
    targets = await DB.get_targets()
    rows = []
    for i, t in enumerate(targets):
        last = await DB.get_last_post(t["id"])
        rows.append({
            "n": i + 1,
            "id": t["id"],
            "title": await _chat_title(client, t["id"]) or str(t["id"]),
            "t_link": await _target_link(client, t["id"], last),
            "db_title": (await _chat_title(client, t["db_id"])
                         if t.get("db_id") else None),
            "db_link": (await _db_invite_link(client, t["db_id"])
                        if t.get("db_id") else None),
            "db2_title": (await _chat_title(client, t["db2_id"])
                          if t.get("db2_id") else None),
            "db2_link": (await _db_invite_link(client, t["db2_id"])
                         if t.get("db2_id") else None),
            "resume": await DB.get_progress(t["id"]),
            "paused": bool(t.get("paused")),
        })
    return rows


async def _add_target_with_db(scrape_client, ev, tid, dbid, db2=None,
                              link_mode="button", link_trigger=None):
    """v42: link_mode 'button' (default) or 'caption' + the exact trigger text
    that holds the hidden caption hyperlink."""
    try:
        ent_t = await scrape_client.get_entity(tid)
    except Exception as e:
        await ev.reply(f"⚠️ Can't access that target with the userbot account: {e}")
        return
    if not _entity_ok(ent_t, "channel"):
        await ev.reply(_not_a_channel_msg(tid, "channel"))
        return
    try:
        ent_d = await scrape_client.get_entity(dbid)
    except Exception as e:
        await ev.reply(f"⚠️ Can't access that DB channel with the userbot account: {e}")
        return
    if not _entity_ok(ent_d, "channel"):
        await ev.reply(_not_a_channel_msg(dbid, "channel"))
        return
    await DB.add_target(tid, dbid, link_mode, link_trigger)
    if db2 is not None:
        try:
            ent2 = await scrape_client.get_entity(db2)
            if not _entity_ok(ent2, "channel"):
                await ev.reply(_not_a_channel_msg(db2, "channel"))
                return
        except Exception as e:
            await ev.reply(f"⚠️ Can't access that DB2 channel: {e}")
            return
        await DB.set_target_db2(tid, db2)
    await ev.reply(f"✅ Target saved:\n  {tid} → DB {dbid}"
                   + (f" → DB2 {db2} (bot clean-mirror ON — make sure the BOT is admin in DB and DB2)"
                      if db2 is not None else "")
                   + (f"\n  Link mode: CAPTION — trigger text: `{link_trigger}`"
                      if link_mode == "caption"
                      else f"\n  Link mode: BUTTON — button text: `{link_trigger or 'Download'}`")
                   + "\n\n/targets to view all, /start to scrape.")
    # v45.1: a sibling target already scanned the SAME DB2? share its index
    # instantly — no second scan. Otherwise build once, in the background.
    if db2 is not None:
        shared = await dedup.share_db2(tid, db2)
        if shared:
            await ev.reply(f"🧬 DB2 duplicate-index shared from target {shared} "
                           f"(same DB2 — no rescan needed).")
        else:
            async def _scan_done(res, _tid=tid):
                try:
                    await ev.reply(f"🧬 DB2 duplicate-index ready for target {_tid}: "
                                   f"{res.get('count', 0)} cover fingerprint(s) — reposts "
                                   f"will be auto-skipped (≥90% match).")
                except Exception:
                    pass
            dedup.scan_db2_bg(scrape_client, tid, db2, done_cb=_scan_done)


def register(scrape_client, sm=None):
    # v40: sm = the SessionManager (gives /stats, /invite, /leave every account).
    _sm[0] = sm
    # ---------- info ----------
    @bot.on(events.NewMessage(pattern=r"^/help$"))
    async def help_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        d = dict(_CMDS)
        def L(name):
            return f"/{name} — {d.get(name, '')}"
        sections = [
            # v40: regrouped — mirror the order things are used in
            # v43.3: regrouped in workflow order
            ("🎯 TARGETS & CHANNELS", ["target", "targatelinkmode", "targets",
                "targets_text", "setdb", "setdb2", "adddb", "deltarget"]),
            ("🔐 BYPASS SETUP", ["bypass", "addbypass", "removebypass",
                "bypasslist", "domainbypass", "deldomain", "altbypass"]),
            ("🧼 CAPTION CLEANING (DB2)", ["keepimages", "avoid", "avoidtext",
                "removeavoid", "removegavoid", "replaceword", "removereplace"]),
            ("🔘 LINK-BOT BUTTONS", ["linkbutton", "removelinkbutton"]),
            ("▶️ SCRAPING CONTROL", ["start", "pause", "resume", "skip",
                "stop", "goto", "reset"]),
            ("📊 MONITOR & SCAN", ["status", "current", "progress", "lastpost",
                "scan4duplicates", "dupescan", "checkram"]),
            ("🛠️ BULK JOBS", ["replace", "deletetext", "massdlt",
                "massdlt_status", "massdlt_stop", "forward", "forward_status",
                "forward_stop", "forward_resume"]),
            ("👥 USERBOTS & ADMINS", ["stats", "addworker", "removeworker",
                "invite", "leave", "add", "checkdm", "addadmin", "removeadmin"]),
            ("ℹ️ MISC", ["help", "ping", "cancel"]),
        ]
        lines = ["📖 COMMANDS"]
        shown = set()
        for head, names in sections:
            lines.append(f"\n{head}")
            for n in names:
                if n in d:
                    lines.append(L(n))
                    shown.add(n)
        rest = [c for c, _ in _CMDS if c not in shown]
        if rest:
            lines.append("\nOTHER")
            lines += [L(n) for n in rest]
        lines.append("\nTips: /target walks you through channel + its DB channel. "
                     "/goto accepts a message link (auto-picks the right target). "
                     "Bare /pause pauses EVERYTHING (also /pause all); /pause 2 pauses "
                     "ONLY target 2 (see /targets for numbers). Bare /resume resumes "
                     "everything; /resume 2 resumes one target. "
                     "When LINK_BOT renames its button: /linkbutton <new text> — "
                     "active instantly, no restart. "
                     "Caught-up channels re-scan for new posts every 30s.")
        await ev.reply("\n".join(lines))

    # ---------- v39.1: /checkram — RAM usage ----------
    @bot.on(events.NewMessage(pattern=r"^/checkram$"))
    async def checkram_cmd(ev):
        """Process + container RAM, so you can watch Render's 512MB free cap."""
        if not await _admin(ev.sender_id):
            return
        import os as _os
        rss_mb = None
        try:
            with open("/proc/self/status") as f:
                for ln in f:
                    if ln.startswith("VmRSS:"):
                        rss_mb = int(ln.split()[1]) / 1024.0
                        break
        except Exception:
            pass
        used_mb = limit_mb = None
        try:
            if _os.path.exists("/sys/fs/cgroup/memory.current"):           # cgroup v2
                with open("/sys/fs/cgroup/memory.current") as f:
                    used_mb = int(f.read()) / (1024 * 1024)
                with open("/sys/fs/cgroup/memory.max") as f:
                    raw = f.read().strip()
                limit_mb = None if raw == "max" else int(raw) / (1024 * 1024)
            elif _os.path.exists("/sys/fs/cgroup/memory/memory.usage_in_bytes"):  # cgroup v1
                with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as f:
                    used_mb = int(f.read()) / (1024 * 1024)
                with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
                    limit_mb = int(f.read()) / (1024 * 1024)
        except Exception:
            pass
        lines = ["\U0001F9E0 **RAM usage**"]
        if rss_mb is not None:
            lines.append(f"• This process (userbot + control bot): **{rss_mb:.0f} MB**")
        if used_mb is not None:
            if limit_mb and limit_mb < 1000000:   # absurd = host value, not the container cap
                lines.append(f"• Container: **{used_mb:.0f} / {limit_mb:.0f} MB** ({used_mb / limit_mb * 100:.0f}%)")
            else:
                lines.append(f"• Container: **{used_mb:.0f} MB** used")
        lines.append("• Render free tier kills the dyno at **512 MB** — keep it under ~480 MB")
        await ev.reply("\n".join(lines))

    @bot.on(events.NewMessage(pattern=r"^/ping$"))
    async def ping_cmd(ev):
        if await _admin(ev.sender_id):
            await ev.reply(f"🏓 Pong — control bot + scraper alive.\n"
                           f"📍 Stage: {state.stage} | Running: {state.running} | Paused: {state.paused}")

    # ---------- bulk channel text editing (userbot-powered) ----------
    @bot.on(events.NewMessage(pattern=r"^/replace(?:\s+([\s\S]+))?$"))
    async def replace_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        try:
            parts = shlex.split(arg)
        except ValueError:
            parts = []
        if len(parts) == 2:
            # DB2 form — the BOT edits its own clean-mirror posts (no userbot
            # flood hassle): /replace "old" "new"
            old, new = parts
            await _db2_bulk_edit(ev, old, new)
            return
        if len(parts) != 3:
            await ev.reply('Usage: /replace <channel id> "target text" "replacement text"\n'
                           'The USERBOT edits that channel (needs edit rights).\n'
                           'Or: /replace "old" "new" — the BOT edits every DB2 mirror channel.\n'
                           'Quotes are needed when texts contain spaces.')
            return
        channel, old, new = parts
        await _bulk_edit(scrape_client, ev, _parse_chat_id(channel), old, new)

    @bot.on(events.NewMessage(pattern=r"^/deletetext(?:\s+([\s\S]+))?$"))
    async def deletetext_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        try:
            parts = shlex.split(arg)
        except ValueError:
            parts = []
        if len(parts) != 2:
            await ev.reply('Usage: /deletetext <channel id> "text to delete"\n'
                           'Quotes are needed when the text contains spaces. '
                           'The USERBOT edits the posts (must have edit rights).')
            return
        channel, old = parts
        await _bulk_edit(scrape_client, ev, _parse_chat_id(channel), old, "")

    # ---------- MTProto bulk jobs: mass delete / forward / add-bot-admin ----------
    @bot.on(events.NewMessage(pattern=r"^/massdlt(?:\s+([\s\S]+))?$"))
    async def massdlt_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        try:
            parts = shlex.split(arg)
        except ValueError:
            parts = []
        if len(parts) != 3:
            await ev.reply("Usage: /massdlt <chat_id> <start_link> <end_link>\n"
                           "Deletes every message between the two links (inclusive). "
                           "Chunked + paced (Telegram-safe) so a 2000-message range "
                           "is deleted piece-by-piece, never flooded.")
            return
        chat, s, e = parts
        await MTM.massdlt_start(scrape_client, ev, _parse_chat_id(chat), s, e)

    @bot.on(events.NewMessage(pattern=r"^/massdlt_status$"))
    async def massdlt_status_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        txt = MTM.massdlt_job.progress_text()
        await ev.reply(txt or "ℹ️ No mass-delete has run yet.")

    @bot.on(events.NewMessage(pattern=r"^/massdlt_stop$"))
    async def massdlt_stop_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        if MTM.massdlt_job.status == "running":
            MTM.massdlt_job.stop = True
            MTM.massdlt_job.status = "stopping"
            await ev.reply("🛑 Stopping mass-delete — it stops after the current chunk. "
                           "/massdlt_status to confirm.")
        else:
            await ev.reply("ℹ️ No mass-delete is running.")

    @bot.on(events.NewMessage(pattern=r"^/forward(?:\s+([\s\S]+))?$"))
    async def forward_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        try:
            parts = shlex.split(arg)
        except ValueError:
            parts = []
        if len(parts) != 4:
            await ev.reply("Usage: /forward <target_channel> <source_channel> <start_link> <end_link>\n"
                           "Copies every message in the range to the target channel — "
                           "by reference, NO 'Forwarded from' tag, paced to avoid floods. "
                           "Stops/crashes are resumable with /forward_resume.")
            return
        tgt, src, s, e = parts
        # v44: keep the raw source key for link validation, resolve the real
        # id separately — feeding _parse_chat_id(src) into _resolve_range made
        # Telegram resolve a t.me/c/<digits>/<msg> link as a PEER id and the
        # "links don't belong to the given chat_id" guard killed the run.
        src_raw = src.strip()
        src_id = _parse_chat_id(src)
        m = re.search(r"(?:https?://)?t\.me/c/(\d+)(?:/\d+)?", src_raw)
        if m:
            src_id = int("-100" + m.group(1))
        await MTM.forward_start(scrape_client, ev, _parse_chat_id(tgt),
                                src_id, s, e, sm=sm)  # v44: rotation pool

    @bot.on(events.NewMessage(pattern=r"^/forward_status$"))
    async def forward_status_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        txt = MTM.forward_job.progress_text()
        if txt is None:
            saved = await DB.get_config("fwd_job")
            if saved and saved.get("ids") and saved.get("pos", 0) < len(saved["ids"]):
                txt = (f"⏸ forward — stopped/interrupted\n"
                       f"   {saved.get('source')} → {saved.get('target')}\n"
                       f"   {saved.get('pos', 0)}/{len(saved['ids'])} done — "
                       f"/forward_resume to continue.")
            else:
                txt = "ℹ️ No forward has run yet."
        await ev.reply(txt)

    @bot.on(events.NewMessage(pattern=r"^/forward_stop$"))
    async def forward_stop_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        if MTM.forward_job.status == "running":
            MTM.forward_job.stop = True
            MTM.forward_job.status = "stopping"
            await ev.reply("🛑 Stopping forward — cursor saved in MongoDB; "
                           "/forward_resume continues from the exact message.")
        else:
            await ev.reply("ℹ️ No forward is running.")

    @bot.on(events.NewMessage(pattern=r"^/forward_resume$"))
    async def forward_resume_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        await MTM.forward_resume(scrape_client, ev, sm=sm)  # v44

    @bot.on(events.NewMessage(pattern=r"^/add(?:\s+([\s\S]+))?$"))
    async def add_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        try:
            parts = shlex.split(arg)
        except ValueError:
            parts = []
        if len(parts) < 2:
            await ev.reply("Usage: /add <channel id> @bot1 [@bot2 @bot3 …]\n"
                           "Adds each bot to the channel as ADMIN with ALL permissions. "
                           "The USERBOT does it — it must already be an admin there "
                           "with add-admins permission.")
            return
        channel, bots = parts[0], parts[1:]
        await MTM.add_bots(scrape_client, ev, _parse_chat_id(channel), bots)

    # ---------- DB2 clean mirror (BOT copies DB -> DB2, credits stripped) ----------
    # v48 #1 - @mentions are no longer DELETED: every handle becomes
    # MENTION_REPLACEMENT so the credit becomes yours. Plain "@user",
    # markdown-wrapped **@user** / __@user__ / `@user`, unicode-styled handles
    # (math-bold "@\U0001D63C\U0001D63F\U0001D692\U0001D62D...", monospace, small
    # caps) and fullwidth "\uff20user" are all caught: the STYLING is folded to
    # ASCII first (NFKD + combining marks + small caps + Greek/Cyrillic
    # homoglyphs) and the \w class then matches whatever remains.
    # v48 #2 - /avoid used a plain str.replace, so an entry survived whenever the
    # caption differed in SPACING / LETTER CASE / ".." vs "...", or showed a
    # STYLED handle. Each entry now gets a word-key AND a separator-blind key,
    # matched against whole lines (a pasted "12. text" also matches a bare
    # "text" line, with any list number), then a flexible inline pass clears the
    # rest. Lines left with no letter at all (bare "1.", "\u2501\u2501\u2501"
    # rules, emoji-only residue) are dropped.
    import unicodedata as _ud
    MENTION_REPLACEMENT = "@NSFW_Universe"
    _URL_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+|https?://\S+")
    _MENTION_RE = re.compile(r"[@\uff20\ufe6b](?:\w*[^\W_])?")
    # the caption goes to DB2 as PLAIN text so they were literal characters.
    # markdown wrappers around a handle (**@user** / __@user__ / `@user`) are
    # dropped as well - the caption reaches DB2 as PLAIN text, so those
    # characters were literal. Built from re.escape() to stay readable.
    # markdown wrappers around a handle (**@user** / __@user__ / `@user`) are
    # dropped as well - the caption reaches DB2 as PLAIN text, so those
    # characters were literal. Built from re.escape() to stay readable.
    _MENT_WRAP_RE = re.compile("([*_`]{1,3})" + re.escape(MENTION_REPLACEMENT) + "([*_`]{1,3})")
    _LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
    _MARKS_RE = re.compile(r"[\u0300-\u036f\u200b-\u200f\ufeff]")
    _PUNCT_RE = re.compile(r"[\W_]+", re.UNICODE)
    _NUMBULLET_RE = re.compile(r"^\s*\d{1,3}[.)]\s+(.+)$", re.S)
    _DOTS_RE = re.compile(r"\.{2,}|\u2026")
    _SMALLCAP = str.maketrans({"\u01c8": "j", "\u01cb": "j", "\u01dd": "e", "\u01f2": "z", "\u0237": "j", "\u1d00": "a", "\u1d03": "b", "\u1d04": "c", "\u1d05": "d", "\u1d07": "e", "\u1d08": "e", "\u1d09": "i", "\u1d0a": "j", "\u1d0b": "k", "\u1d0d": "m", "\u1d0e": "n", "\u1d0f": "o", "\u1d10": "o", "\u1d11": "o", "\u1d12": "o", "\u1d16": "o", "\u1d17": "o", "\u1d18": "p", "\u1d19": "r", "\u1d1a": "r", "\u1d1b": "t", "\u1d1c": "u", "\u1d1d": "u", "\u1d1e": "u", "\u1d1f": "m", "\u1d20": "v", "\u1d21": "w", "\u1d22": "z", "\u1d43": "a", "\u1d44": "a", "\u1d47": "b", "\u1d48": "d", "\u1d49": "e", "\u1d4b": "e", "\u1d4c": "e", "\u1d4d": "g", "\u1d4e": "i", "\u1d4f": "k", "\u1d50": "m", "\u1d52": "o", "\u1d53": "o", "\u1d54": "o", "\u1d55": "o", "\u1d56": "p", "\u1d57": "t", "\u1d58": "u", "\u1d59": "u", "\u1d5a": "m", "\u1d5b": "v", "\u1d62": "i", "\u1d63": "r", "\u1d64": "u", "\u1d65": "v", "\u1d77": "g", "\u1d79": "g", "\ua730": "f", "\ua731": "s", "\ua747": "l", "\ua763": "z", "\ua77a": "d", "\ua77c": "f", "\ua77f": "g", "\ua781": "l", "\ua783": "r", "\ua785": "s", "\ua787": "t", "\ua7ae": "i", "\ua7af": "q", "\ua7bb": "a", "\ua7bd": "i", "\ua7bf": "u", "\ua7c1": "o", "\ua7c3": "w", "\ua7d1": "g", "\ua7d7": "s", "\ua7d9": "s", "\ua7f6": "h", "\ua7fa": "m"})
    _HOMO = str.maketrans({"\u0391": "a", "\u0392": "b", "\u0395": "e", "\u0396": "z", "\u0397": "h", "\u0399": "i", "\u039a": "k", "\u039c": "m", "\u039d": "n", "\u039f": "o", "\u03a1": "p", "\u03a4": "t", "\u03a5": "y", "\u03a7": "x", "\u03b1": "a", "\u03b2": "b", "\u03b3": "y", "\u03b4": "d", "\u03b5": "e", "\u03b6": "z", "\u03b7": "n", "\u03b8": "o", "\u03b9": "i", "\u03ba": "k", "\u03bb": "l", "\u03bc": "u", "\u03bd": "v", "\u03be": "x", "\u03bf": "o", "\u03c0": "n", "\u03c1": "p", "\u03c2": "s", "\u03c3": "o", "\u03c4": "t", "\u03c5": "u", "\u03c6": "f", "\u03c7": "x", "\u03c8": "y", "\u03c9": "w", "\u03ca": "i", "\u03cb": "u", "\u0430": "a", "\u0432": "b", "\u0435": "e", "\u043a": "k", "\u043c": "m", "\u043d": "h", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0442": "t", "\u0443": "y", "\u0445": "x", "\u0455": "s", "\u0456": "i", "\uff41": "a"})

    def _fold(s):
        """NFKD-fold to plain ASCII: styled letters, small caps and Greek/Cyrillic
        look-alikes (monospace \U0001D698 -> Greek omicron) all collapse."""
        return _MARKS_RE.sub("", _ud.normalize("NFKD", s or "")).translate(_SMALLCAP).translate(_HOMO)

    def _cmp_key(s):
        """Word key for avoid matching: case, emoji, punctuation, list numbers and
        lining rules are all irrelevant - only the words remain."""
        s = _fold(s).lower()
        s = re.sub(r"^\s*\d{1,3}[.)]\s+", "", s)
        s = _DOTS_RE.sub(" ", s)
        s = _PUNCT_RE.sub(" ", s)
        return " ".join(s.split())

    def _sq_key(s):
        """Separator-blind key: '_' '-' '.' and spaces all vanish, so
        'adult_horizon' == 'adult horizon' == 'AdultHorizon'."""
        s = _fold(s).lower()
        s = re.sub(r"^\s*\d{1,3}[.)]\s+", "", s)
        return re.sub(r"[\W_]+", "", s)

    def _flex_avoid(a):
        """Whitespace/case/dot-tolerant regex source for one /avoid string, so
        'Due TO COPYRIGHT ISSUES..' also matches 'Due To COPYRIGHT ISSUES...'."""
        toks = [x for x in re.split(r"\s+", _fold(a).strip()) if x]
        if not toks:
            return None
        parts = []
        for tok in toks:
            esc = re.escape(tok)
            flexed = _DOTS_RE.sub(lambda _m: "[.\u2026]{0,5}", esc)
            if not flexed or flexed == "[.\u2026]{0,5}":
                flexed = esc                        # pure-punctuation token: exact
            parts.append(flexed)
        return r"\s*".join(parts)

    def _swap_mentions(t):
        """Every @handle becomes MENTION_REPLACEMENT - never dropped."""
        t = _MENTION_RE.sub(MENTION_REPLACEMENT, t)
        return _MENT_WRAP_RE.sub(MENTION_REPLACEMENT, t)

    def _strip_avoids(t, avoids):
        """Remove /avoid strings: whole-line match first (word key, then
        separator-blind key), then the flexible inline pass for text embedded
        inside a longer line."""
        if not avoids:
            return t
        variants = []
        for a in avoids:
            a = (a or "").strip()
            if not a:
                continue
            variants.append(a)
            m_num = _NUMBULLET_RE.match(a)          # a pasted "12. text"
            if m_num:
                variants.append(m_num.group(1).strip())
        keys = [k for k in (_cmp_key(v) for v in variants) if k]
        sqs = [q for q in (_sq_key(v) for v in variants) if q]
        if keys or sqs:
            keep = []
            for ln in t.splitlines():
                nl, nq = _cmp_key(ln), _sq_key(ln)
                hit = False
                for k in keys:                      # word-key pass
                    if nl and (nl == k or (k in nl and len(k) >= 0.5 * len(nl))):
                        hit = True
                        break
                if not hit:                         # separator-blind pass
                    for q in sqs:
                        if nq and (nq == q or (q in nq and len(q) >= 0.5 * len(nq))):
                            hit = True
                            break
                if not hit:
                    keep.append(ln)
            t = "\n".join(keep)
        for v in variants:                          # inline leftovers
            if v in t:
                t = t.replace(v, " ")
            pat = _flex_avoid(v)
            if pat:
                t = re.sub(pat, " ", t, flags=re.I | re.UNICODE)
        return re.sub(r"[ \t]{2,}", " ", t)

    def _clean_caption(text, avoids):
        """Strip /avoid strings, strip URLs, rewrite every @mention to
        MENTION_REPLACEMENT, drop lines with no letters at all, tidy whitespace.
        Sent as plain text, so embedded-link formatting dies with the entities."""
        t = _strip_avoids(text or "", avoids)
        t = _URL_RE.sub("", t)
        t = _swap_mentions(t)
        kept = []
        for ln in t.splitlines():
            if ln.strip() and not _LETTER_RE.search(ln):
                continue        # bare "1." / "\u2501\u2501\u2501" rule / emoji-only
            kept.append(ln)
        t = "\n".join(kept)
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()
    _DB2_CACHE = {"ts": 0.0, "map": {}}

    async def _db2_map():
        """db_channel_id -> target dict, for every target with a DB2 set.
        Cached 30s so normal chats cost no Mongo query."""
        if time.time() - _DB2_CACHE["ts"] > 30:
            m = {}
            for t in await DB.get_targets():
                if t.get("db2_id") and t.get("db_id"):
                    m[t["db_id"]] = t
            _DB2_CACHE["ts"] = time.time()
            _DB2_CACHE["map"] = m
        return _DB2_CACHE["map"]

    async def _copy_to_db2(m, db2, avoids, target_id=None):
        """Copy ONE DB message into DB2 as a fresh bot post (no forward tag),
        caption cleaned. FloodWait is slept through in place."""
        # v40: GLOBAL DB2 rules — /replaceword rewrites first, then _clean_caption
        # strips global /avoid strings AND the per-target /avoidtext ones.
        _txt = m.message or ""
        for _p in await DB.get_replace_words():
            if _p.get("old"):
                _txt = _txt.replace(_p["old"], _p.get("new", ""))
        cap = _clean_caption(_txt, (await DB.get_global_avoids()) + list(avoids))
        spoiler = bool(getattr(getattr(m, "media", None), "spoiler", False))
        for _ in range(3):
            try:
                sent = None
                if getattr(m, "media", None) is not None:
                    sent = await bot.send_file(db2, m.media, caption=cap,
                                               buttons=m.buttons, spoiler=spoiler)
                elif cap:
                    sent = await bot.send_message(db2, cap)
                # v46: index this cover the instant it lands in DB2, using the
                # CLEANED caption (exactly what DB2 now shows), so the stored
                # fingerprint == the DB2 fingerprint — a future repost is gated
                # without ever re-scanning DB2.
                if target_id and getattr(m, "photo", None) is not None:
                    _fp = dedup.fingerprint_cover(cap)
                    if _fp:
                        await dedup.remember(target_id, _fp,
                                             getattr(sent, "id", 0), db2_id=db2)
                return True
            except FloodWaitError as fe:
                await asyncio.sleep(min(getattr(fe, "seconds", 60), BULK_MAX_FLOOD) + 5)
            except Exception as e:
                log_mirror.warning("DB2 copy failed: %s", e)
                return False
        return False

    _DB2_LOCKS = {}  # db chat_id -> asyncio.Lock (order preservation)

    @bot.on(events.NewMessage())
    async def db2_mirror(ev):
        """Auto-mirror: userbot posts to DB -> bot re-posts to DB2 cleaned.
        Requires the BOT to be admin in BOTH DB (to receive channel posts) and
        DB2 (to post). Serialized per DB channel: Telethon dispatches channel
        events concurrently, so without this lock a slow/flooded cover copy
        could finish AFTER the next message's copy — DB2 got videos before the
        cover. The lock guarantees DB2 order == DB order (cover first)."""
        t = (await _db2_map()).get(ev.chat_id)
        if not t:
            return
        lock = _DB2_LOCKS.setdefault(ev.chat_id, asyncio.Lock())
        async with lock:
            await _copy_to_db2(ev.message, t["db2_id"], t.get("avoid") or [],
                               target_id=t["id"])

    async def _db2_bulk_edit(ev, old, new):
        """BOT edits every message containing `old` in every DB2 channel —
        these are the bot's OWN posts, so editing is allowed. Paced + flood-safe."""
        targets = [t for t in await DB.get_targets() if t.get("db2_id")]
        if not targets:
            await ev.reply("No DB2 channels set — add one with /setdb2 <n> <id>.")
            return
        status = await ev.reply(f"🔍 Scanning {len(targets)} DB2 channel(s) for \"{old}\"…")
        edited = failed = floods = 0
        for t in targets:
            db2 = t["db2_id"]
            matches = []
            try:
                async for m in bot.iter_messages(db2):
                    if old in (m.message or ""):
                        matches.append(m)
            except Exception as e:
                await ev.reply(f"⚠️ Can't read DB2 {db2} (is the bot an admin there?): {e}")
                continue
            if not matches:
                continue
            await ev.reply(f"DB2 {db2}: editing {len(matches)} message(s)…")
            for m in matches:
                new_txt = (m.message or "").replace(old, new).strip()
                while True:
                    try:
                        await bot.edit_message(db2, m.id, text=new_txt, buttons=m.buttons)
                        edited += 1
                        break
                    except FloodWaitError as fe:
                        secs = getattr(fe, "seconds", 60)
                        if secs <= BULK_MAX_FLOOD:
                            floods += 1
                            await asyncio.sleep(secs + 5)
                            continue
                        failed += 1
                        break
                    except Exception:
                        failed += 1
                        break
                if edited and edited % BULK_PROGRESS_EVERY == 0:
                    try:
                        await status.edit(f"⏳ {edited} edited, {failed} failed…")
                    except Exception:
                        pass
                await asyncio.sleep(BULK_EDIT_DELAY + random.uniform(0, 1.5))
        await status.edit(f"✅ DB2 edit done — {edited} message(s) updated"
                          + (f", {failed} failed" if failed else "")
                          + (f", {floods} flood wait(s) slept" if floods else ""))

    @bot.on(events.NewMessage(pattern=r"^/setdb2(?:\s+([\s\S]+))?$"))
    async def setdb2_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        try:
            parts = shlex.split(arg)
        except ValueError:
            parts = []
        targets = await DB.get_targets()
        if len(parts) != 2:
            cur = [f"  {i+1}. {t['id']} → DB2 {t.get('db2_id') or '(off)'}"
                   for i, t in enumerate(targets)]
            await ev.reply("Usage: /setdb2 <target #> <db2 channel id | off>\n"
                           "The BOT re-posts everything from that target's DB into DB2 "
                           "with credits/links stripped. Bot must be admin in both.\n"
                           + ("\n".join(cur) if cur else "No targets yet."))
            return
        n_raw, db2_raw = parts
        t = None
        if n_raw.isdigit() and 1 <= int(n_raw) <= len(targets):
            t = targets[int(n_raw) - 1]
        else:
            for cand in targets:
                if str(cand["id"]) == n_raw:
                    t = cand
                    break
        if not t:
            await ev.reply("⚠️ Unknown target — see /targets for numbers.")
            return
        if db2_raw.lower() in ("off", "none", "0", "-"):
            await DB.set_target_db2(t["id"], None)
            await ev.reply(f"🧼 DB2 mirror OFF for target {t['id']}.")
            return
        db2 = _parse_chat_id(db2_raw)
        try:
            ent = await scrape_client.get_entity(db2)
        except Exception as e:
            await ev.reply(f"⚠️ Can't access that DB2 channel: {e}")
            return
        if not _entity_ok(ent, "channel"):
            await ev.reply(_not_a_channel_msg(db2, "channel"))
            return
        await DB.set_target_db2(t["id"], db2)
        await ev.reply(f"✅ Target {t['id']} now mirrors DB → DB2 {db2} (credits stripped).\n"
                       "Make sure the BOT is admin in the DB channel AND in DB2.\n"
                       "Add credit strings to strip with /avoidtext.")
        # v45.1: sibling with the same DB2 already indexed? share it, else scan
        if not await dedup.share_db2(t["id"], db2):
            dedup.scan_db2_bg(scrape_client, t["id"], db2)

    @bot.on(events.NewMessage(pattern=r"^/avoidtext(?:\s+([\s\S]+))?$"))
    async def avoidtext_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        try:
            parts = shlex.split(arg)
        except ValueError:
            parts = []
        targets = await DB.get_targets()
        if not parts:
            lines = ["🧼 DB2 avoid-strings (stripped from mirrored captions, "
                     "on top of auto-stripped @mentions and links):"]
            for i, t in enumerate(targets):
                av = t.get("avoid") or []
                lines.append(f"  {i+1}. {t['id']}: " + (", ".join(f'\"{a}\"' for a in av) if av else "(none)"))
            lines.append("\n/avoidtext <target #> \"text\" to add — /removeavoid <target #> <#> to remove.")
            await ev.reply("\n".join(lines))
            return
        if len(parts) == 1:
            if not (parts[0].isdigit() and 1 <= int(parts[0]) <= len(targets)):
                await ev.reply("Usage: /avoidtext <target #> \"text to strip\" — quotes when it has spaces.")
                return
            t = targets[int(parts[0]) - 1]
            av = t.get("avoid") or []
            await ev.reply(f"🧼 Avoid-strings for target {int(parts[0])} ({t['id']}):\n"
                           + ("\n".join(f"  {j+1}. \"{a}\"" for j, a in enumerate(av)) if av else "  (none)")
                           + "\n\n/avoidtext " + parts[0] + " \"text\" to add, /removeavoid " + parts[0] + " <#> to remove.")
            return
        # v30: multi-word avoid strings — after the target number, EVERYTHING
        # (quoted or not) is the string to strip, so both of these work:
        #   /avoidtext 1 "how are you"   and   /avoidtext 1 how are you
        n_raw, text = parts[0], " ".join(parts[1:]).strip()
        if not (n_raw.isdigit() and 1 <= int(n_raw) <= len(targets)):
            await ev.reply("⚠️ Unknown target number — see /targets.")
            return
        t = targets[int(n_raw) - 1]
        av, added = await DB.add_avoid(t["id"], text)
        if av is None:
            await ev.reply("⚠️ Target not found.")
        elif added:
            await ev.reply(f"✅ Target {n_raw}: will strip \"{text}\" from DB2 captions "
                           f"({len(av)} avoid-string(s) now).")
        else:
            await ev.reply("ℹ️ That string is already on the avoid list.")

    @bot.on(events.NewMessage(pattern=r"^/removeavoid(?:\s+(\d+))?$"))
    async def removeavoid_cmd(ev):
        """v41: /removeavoid N — N is the entry number in the /avoidtext list
        (per-target avoids numbered top to bottom). Removes that exact string."""
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        targets = await DB.get_targets()
        # build the same numbered list /avoidtext shows: per-target avoids in order
        flat = []  # [(target_id, string_index_1based, text)]
        for t in targets:
            for si, s in enumerate(await DB.get_avoids(t["id"])):
                flat.append((t["id"], si + 1, s))
        if not arg.isdigit() or not (1 <= int(arg) <= len(flat)):
            listing = "\n".join(f"  {i+1}. {tid} » \"{s}\"" for i, (tid, _, s) in enumerate(flat)) or "  (none)"
            await ev.reply(f"Usage: /removeavoid <#> — the number from this list:\n{listing}")
            return
        tid, si, _ = flat[int(arg) - 1]
        res = await DB.remove_avoid(tid, si)
        if res is None:
            await ev.reply("⚠️ That string is already gone — /avoidtext to see the current list.")
        else:
            _, removed = res
            await ev.reply(f"🗑 Removed avoid #{arg}: \"{removed}\" (target {tid}).")

    # ---------- /checkdm: userbot DM pipeline (@richmining -> auto admin) ----------
    @bot.on(events.NewMessage(pattern=r"^/checkdm(?:\s+(\S+))?$"))
    async def checkdm_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip().lower()
        if arg in ("on", "off"):
            await DB.set_config("checkdm_enabled", arg == "on")
            if arg == "on":
                await ev.reply("🔗 checkdm ON — the userbot now watches its DM with "
                               "@richmining. Send a channel invite link (public "
                               "t.me/name or private t.me/+hash) and it will: join → "
                               "wait until it's promoted to admin → add @lifesimplerbot "
                               "as admin with the same rights the userbot has → leave → "
                               "reply DONE ✅. Then it's ready for the next link.")
            else:
                await ev.reply("🔗 checkdm OFF — invite links from @richmining are ignored.")
            return
        cur = bool(await DB.get_config("checkdm_enabled"))
        await ev.reply(f"🔗 checkdm is {'ON ✅' if cur else 'OFF ❌'}.\n"
                       "Usage: /checkdm on  |  /checkdm off")

    # ---------- extra admins (owner-only management, Mongo-backed) ----------
    def _owner(ev):
        return ADMIN_USER_ID == 0 or ev.sender_id == ADMIN_USER_ID

    @bot.on(events.NewMessage(pattern=r"^/addadmin(?:\s+(\S+))?$"))
    async def addadmin_cmd(ev):
        # v39: gate on _admin (any existing admin), NOT _owner. _owner only
        # passes for ev.sender_id == ADMIN_USER_ID, so messaging from a second
        # account silently did nothing ('no response'). Any admin may add more.
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        if not arg:
            admins = await DB.get_admins()
            lines = [f"👑 Owner: `{ADMIN_USER_ID}`", "🛡 Admins (full bot access):"]
            lines += ([f"  {i+1}. `{a}`" for i, a in enumerate(admins)]
                      if admins else ["  (none yet)"])
            lines.append("\n/addadmin <user id> to add one — they can use EVERY "
                         "command like the owner. Get an id from @userinfobot.")
            await ev.reply("\n".join(lines))
            return
        try:
            uid = int(arg)
        except ValueError:
            await ev.reply("⚠️ /addadmin needs the NUMERIC Telegram user id "
                           "(e.g. /addadmin 123456789) — @userinfobot gives it.")
            return
        if ADMIN_USER_ID and uid == ADMIN_USER_ID:
            await ev.reply("ℹ️ That's the owner already.")
            return
        admins = await DB.add_admin(uid)
        await ev.reply(f"✅ `{uid}` is now an admin — full bot access granted. "
                       f"({len(admins)} admin(s) total)")

    @bot.on(events.NewMessage(pattern=r"^/removeadmin(?:\s+(\S+))?$"))
    async def removeadmin_cmd(ev):
        # v39: same _admin gate as /addadmin (was _owner -> silent no-op for
        # non-owner admins).
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        if not arg:
            await ev.reply("Usage: /removeadmin <user id> — bare /addadmin lists current admins.")
            return
        try:
            uid = int(arg)
        except ValueError:
            await ev.reply("⚠️ numeric user id only.")
            return
        admins, removed = await DB.remove_admin(uid)
        await ev.reply(f"🗑 `{uid}` removed — they can no longer use the bot."
                       if removed else "ℹ️ That id isn't in the admin list.")

    # ---------- LINK_BOT button labels (Mongo-backed, no restart) ----------
    @bot.on(events.NewMessage(pattern=r"^/linkbutton(?:\s+(.+))?$"))
    async def linkbutton_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        if arg:
            # strip surrounding quotes if the owner wrapped the label
            if len(arg) > 1 and arg[0] in '"\'' and arg[-1] == arg[0]:
                arg = arg[1:-1].strip()
            if not arg:
                await ev.reply("⚠️ Empty label. Usage: /linkbutton <button text>")
                return
            buttons, added = await DB.add_link_button(arg)
            if added:
                await ev.reply(f"✅ Button label added: {arg}\n"
                               f"LINK_BOT link button now matches: "
                               f"{BTN_SHORT_LINK} (built-in) + {len(buttons)} custom label(s). "
                               f"Active immediately — /linkbutton to list, /removelinkbutton <n> to remove.")
            else:
                await ev.reply(f"⚠️ '{arg}' is already in the list (see /linkbutton).")
            return
        buttons = await DB.get_link_buttons()
        lines = [f"🔗 LINK_BOT button labels (built-in: '{BTN_SHORT_LINK}' — always active):"]
        if buttons:
            lines += [f"  {i+1}. {b}" for i, b in enumerate(buttons)]
            lines.append("\n/linkbutton <text> to add, /removelinkbutton <n> to remove.")
        else:
            lines.append("  (no custom labels yet)")
            lines.append("\nIf LINK_BOT renamed its button: /linkbutton <exact new text> — "
                         "the userbot matches it on the next post, no restart needed.")
        await ev.reply("\n".join(lines))

    @bot.on(events.NewMessage(pattern=r"^/removelinkbutton(?:\s+(\d+))?$"))
    async def removelinkbutton_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        n = ev.pattern_match.group(1)
        if not n:
            await ev.reply("Usage: /removelinkbutton <number> — see /linkbutton for the numbered list.")
            return
        r = await DB.remove_link_button(int(n))
        if r is None:
            buttons = await DB.get_link_buttons()
            await ev.reply(f"⚠️ Number must be 1-{len(buttons)} (see /linkbutton).")
            return
        buttons, removed = r
        await ev.reply(f"🗑 Removed label {n}: {removed}\n"
                       f"{len(buttons)} custom label(s) left (built-in '{BTN_SHORT_LINK}' is always active).")

    # ---------- v52: /addworker /removeworker — runtime worker sessions ----------
    @bot.on(events.NewMessage(pattern=r"^/addworker(?:\s+([\s\S]+))?$"))
    async def addworker_cmd(ev):
        """v52: /addworker <session string> adds a scraping worker LIVE — the
        StringSession is validated (logged in), stored in Mongo
        (config.extra_sessions — survives redeploys) and attached to the running
        pool INSTANTLY, no restart. Bare /addworker lists every worker tagged
        [env] / [bot#k] — env workers come from Render env vars, bot workers
        were added here."""
        if not await _admin(ev.sender_id):
            return
        mgr = _sm[0]
        if mgr is None:
            await ev.reply("Session manager unavailable.")
            return
        sess = (ev.pattern_match.group(1) or "").strip()

        if not sess:
            # ---- bare /addworker -> list every worker + its source ----
            clients = mgr.all()
            sources = mgr.sources()
            extra = await DB.get_extra_sessions()
            lines = [f"👷 WORKERS — {len(clients)} loaded "
                     f"({sources.count('env')} env, {sources.count('bot')} bot-added)"]
            bot_k = 0
            for i, c in enumerate(clients):
                src = sources[i] if i < len(sources) else "env"
                if src == "bot":
                    bot_k += 1
                    tag_src = f"bot#{bot_k}"
                else:
                    tag_src = "env"
                try:
                    if not c.is_connected():
                        lines.append(f"{i + 1}. ⚪ NOT CONNECTED [{tag_src}]")
                        continue
                    me = await c.get_me()
                    tag = f"@{me.username}" if getattr(me, "username", None) else str(me.id)
                    fname = ((getattr(me, "first_name", "") or "") + " " +
                             (getattr(me, "last_name", "") or "")).strip()
                    lines.append(f"{i + 1}. {fname or '?'} {tag} · id `{me.id}` [{tag_src}]")
                except Exception as e:
                    lines.append(f"{i + 1}. 🔴 error — {type(e).__name__} [{tag_src}]")
            lines.append("")
            lines.append("• env workers = Render env vars (STRING_SESSION, "
                         "STRING_SESSION2, …) — change them on Render.")
            lines.append("• /addworker <session string> — add a worker live "
                         "(stored in Mongo, survives redeploys).")
            lines.append(f"• /removeworker <bot#> — remove a bot-added worker "
                         f"({len(extra)} stored, {bot_k} live now).")
            await ev.reply("\n".join(lines))
            return

        # ---- /addworker <session string> ----
        try:
            from telethon.sessions import StringSession as _SS
            _SS(sess)                      # fast malformed-string check
        except Exception:
            await ev.reply("⚠️ That doesn't look like a Telethon StringSession — "
                           "generate one with gen_session.py and paste the whole string.")
            return
        if any(getattr(getattr(c, "session", None), "save", None) and c.session.save() == sess
               for c in mgr.all()):
            await ev.reply("ℹ️ That exact session is already a live worker.")
            return
        stored = await DB.get_extra_sessions()
        if any(x.get("session") == sess for x in stored):
            await ev.reply("ℹ️ That session is already stored (config.extra_sessions) — "
                           "it loads on the next restart.")
            return
        status = await ev.reply("⏳ Logging the new worker in…")
        try:
            c = await mgr.add_session(sess, source="bot")
        except Exception as e:
            await status.edit(f"❌ Login failed — session NOT added.\n"
                              f"`{type(e).__name__}: {str(e)[:200]}`\n"
                              "Check the string is complete and the account isn't "
                              "flood-limited, then retry.")
            return
        try:
            me = await c.get_me()
            my_id = me.id
            tag = f"@{me.username}" if getattr(me, "username", None) else str(me.id)
            fname = ((getattr(me, "first_name", "") or "") + " " +
                     (getattr(me, "last_name", "") or "")).strip()
        except Exception:
            my_id, tag, fname = None, "?", "?"
        # same account as an existing worker? -> undo, keep the original
        for oc in mgr.all():
            if oc is c:
                continue
            try:
                ome = await oc.get_me()
            except Exception:
                continue
            if my_id is not None and getattr(ome, "id", None) == my_id:
                await mgr.remove_session(c)
                await status.edit(f"ℹ️ That account is ALREADY a worker "
                                  f"({tag}) — duplicate not added.")
                return
        await DB.add_extra_session(sess, note=f"{tag} {fname}".strip())
        try:
            import checkdm
            checkdm.register(c)            # same DM watcher as the other workers
        except Exception as e:
            logging.getLogger("botapi").warning(
                "checkdm register on new worker failed: %s", e)
        n = mgr.count()
        await status.edit(
            f"✅ Worker {n} added — **{fname or '?'}** {tag} · id `{my_id}` [bot]\n"
            f"Stored in Mongo — it re-attaches automatically after every redeploy.\n"
            f"The account is LIVE now and joins the scraping rotation.\n"
            f"Next: /invite {n} <channel> so it can read your targets — /stats to verify.")

    @bot.on(events.NewMessage(pattern=r"^/removeworker(?:\s+(\d+))?$"))
    async def removeworker_cmd(ev):
        """v52: /removeworker <bot#> removes a BOT-ADDED worker (the [bot#k]
        numbering shown by bare /addworker): detaches it from the live pool and
        deletes it from Mongo so it never re-attaches. env workers can only be
        removed on Render."""
        if not await _admin(ev.sender_id):
            return
        mgr = _sm[0]
        if mgr is None:
            await ev.reply("Session manager unavailable.")
            return
        arg = ev.pattern_match.group(1)
        if not arg:
            await ev.reply("Usage: /removeworker <bot#> — the [bot#k] numbering "
                           "shown by bare /addworker.")
            return
        r = await DB.remove_extra_session(int(arg))
        if not r:
            extra = await DB.get_extra_sessions()
            await ev.reply(f"⚠️ No bot-added worker #{arg} — "
                           f"{len(extra)} stored. Bare /addworker lists them.")
            return
        extra, removed = r
        # detach the live client carrying the same session string, if present
        live = None
        for c in mgr.all():
            try:
                if (getattr(c, "source", "env") == "bot"
                        and c.session.save() == removed.get("session")):
                    live = c
                    break
            except Exception:
                pass
        detached = False
        if live is not None:
            detached = await mgr.remove_session(live)
        await ev.reply(
            f"🗑 Removed bot-added worker #{arg} ({removed.get('note') or 'no note'}).\n"
            + ("Live worker detached — it leaves the rotation now."
               if detached else
               "No matching live worker found (or it's the last one) — "
               "it simply won't load on the next boot.")
            + f"\n{len(extra)} bot-added worker(s) left.")

    # ---------- v40: /stats, /invite, /leave, /avoid, /replaceword ----------
    @bot.on(events.NewMessage(pattern=r"^/stats$"))
    async def stats_cmd(ev):
        """v41: per-userbot connection + profile + MEMBERSHIP matrix — which
        account is in which target channel, and its role in each DB / DB2 —
        plus the control bot's own DB/DB2 rights. A '❌ target' row is
        exactly why a resume silently does nothing — join it with /invite."""
        if not await _admin(ev.sender_id):
            return
        mgr = _sm[0]
        if mgr is None:
            await ev.reply("Session manager unavailable.")
            return
        targets = await DB.get_targets()

        async def _role(client, cid):
            """(emoji, label) for the client's membership in chat cid."""
            try:
                ent = await client.get_entity(cid)
            except Exception:
                return "\u274C", "no access"
            try:
                p = await client.get_permissions(ent, "me")
                if getattr(p, "is_admin", False) or getattr(p, "is_creator", False):
                    return "\U0001F451", "admin"
                return "\u2705", "member"
            except Exception:
                try:
                    await client.get_messages(ent, limit=1)
                    return "\u2705", "member"
                except Exception:
                    return "\u274C", "NOT a member"

        status = await ev.reply("⏳ Building /stats — checking every account against every channel…")
        # ---- v52: compact RICH TABLE (Bot API InputRichBlockTable) ----
        # one row per worker; cells hold only emojis keyed by target number, so
        # the table stays narrow no matter how many targets exist.
        role_cache = {}

        async def _cached_em(client, cid):
            """_role emoji cached per (client, chat) — a DB/DB2 shared by
            several targets is checked once, not once per target."""
            key = (id(client), cid)
            if key not in role_cache:
                role_cache[key] = (await _role(client, cid))[0]
            return role_cache[key]

        legend = []
        for j, t in enumerate(targets):
            ttl = await _chat_title(scrape_client, t["id"]) or str(t["id"])
            legend.append(f"{j + 1}·{ttl}")

        tbl_rows = [[("#", None), ("Worker", None), ("Src", None),
                     ("Targets", None), ("DB", None), ("DB2", None)]]
        # control-bot row first (it posts the DB2 clean mirror)
        me_b = await bot.get_me()
        bot_name = f"@{me_b.username}" if getattr(me_b, "username", None) else "control bot"
        bot_db = [await _cached_em(bot, t.get("db_id")) if t.get("db_id") else "·"
                  for t in targets]
        bot_db2 = [await _cached_em(bot, t.get("db2_id")) if t.get("db2_id") else "·"
                   for t in targets]
        tbl_rows.append([("🤖", None), (bot_name, None), ("bot", None),
                         ("·" * len(targets) if targets else "—", None),
                         (" ".join(bot_db) or "—", None),
                         (" ".join(bot_db2) or "—", None)])
        # ---- one compact row per userbot worker ----
        for i, c in enumerate(mgr.all()):
            src = getattr(c, "source", "env")
            try:
                if not c.is_connected():
                    tbl_rows.append([(str(i + 1), None), ("⚪ offline", None), (src, None),
                                     ("—", None), ("—", None), ("—", None)])
                    continue
                me = await c.get_me()
                label = (f"@{me.username}" if getattr(me, "username", None) else
                         (((getattr(me, "first_name", "") or "") + " " +
                           (getattr(me, "last_name", "") or "")).strip() or str(me.id)))
                t_cells = [await _cached_em(c, t["id"]) for t in targets]
                d_cells = [await _cached_em(c, t.get("db_id")) if t.get("db_id") else "·"
                           for t in targets]
                d2_cells = [await _cached_em(c, t.get("db2_id")) if t.get("db2_id") else "·"
                            for t in targets]
                tbl_rows.append([(str(i + 1), None), (label, None), (src, None),
                                 (" ".join(t_cells) or "—", None),
                                 (" ".join(d_cells) or "—", None),
                                 (" ".join(d2_cells) or "—", None)])
            except Exception as e:
                tbl_rows.append([(str(i + 1), None), (f"🔴 {type(e).__name__}", None), (src, None),
                                 ("—", None), ("—", None), ("—", None)])

        legend_txt = " · ".join(legend)
        if len(legend_txt) > 600:
            legend_txt = legend_txt[:600].rsplit(" · ", 1)[0] + " …"
        footer = ("✅ member · 👑 admin · ❌ no access — fix ❌ targets with "
                  "/invite, ❌ DB/DB2 with /add\n" + (legend_txt or "no targets"))
        try:
            await status.delete()
        except Exception:
            pass
        sent = await richboard.send_table(ev.chat_id, "📊 WORKERS — live matrix",
                                          tbl_rows, footer=footer)
        if not sent:
            # plain-text fallback (chunked — stays sendable at any size)
            lines = [f"📊 WORKERS — {mgr.count()} worker(s) × {len(targets)} target(s)"]
            for row in tbl_rows[1:]:
                lines.append(" | ".join((cell[0] or "—") for cell in row))
            lines += ["", footer]
            txt = "\n".join(lines)
            for k in range(0, len(txt), 3800):
                await ev.reply(txt[k:k + 3800])

    def _parse_invite(ev):
        """'/invite 2 <link>' -> (2, link); '/invite <link>' -> (None, link).
        Returns (n_or_None, target_raw) or None when the args are wrong."""
        parts = (ev.pattern_match.group(1) or "").strip().split()
        if not parts:
            return None
        if len(parts) >= 2 and parts[0].isdigit():
            return int(parts[0]), parts[1]
        return None, parts[0]

    async def _join_one(ev, idx, raw):
        mgr = _sm[0]
        c = mgr.all()[idx]
        name = f"acc{idx + 1}/{mgr.count()}"
        from telethon.tl.functions.messages import ImportChatInviteRequest
        from telethon.tl.functions.channels import JoinChannelRequest
        raw = raw.strip()
        try:
            m = re.search(r"(?:t\.me/)?(?:\+|joinchat/)([A-Za-z0-9_\-]+)$", raw)
            if m:                                   # private invite link
                await c(ImportChatInviteRequest(m.group(1)))
                ent = None
            else:                                   # @username / t.me/name / id
                ent = await c.get_entity(_parse_chat_id(raw))
                await c(JoinChannelRequest(ent))
            if ent is None:                         # resolve title after a hash join
                try:
                    d = await c.get_dialogs(limit=1)
                    ent = d[0].entity if d else None
                except Exception:
                    ent = None
            title = getattr(ent, "title", None) or raw
            me = await c.get_me()
            tag = f"@{me.username}" if getattr(me, "username", None) else str(me.id)
            await ev.reply(f"✅ {name} ({tag}) joined **{title}**")
        except Exception as e:
            msg = str(e)
            if "USER_ALREADY_PARTICIPANT" in msg:
                await ev.reply(f"ℹ️ {name} is already a member of {raw}")
            else:
                await ev.reply(f"⚠️ {name} failed to join {raw}: {type(e).__name__} {msg[:150]}")

    @bot.on(events.NewMessage(pattern=r"^/invite(?:\s+([\s\S]+))?$"))
    async def invite_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        mgr = _sm[0]
        parsed = _parse_invite(ev)
        if mgr is None or not parsed:
            await ev.reply("Usage: /invite [account#] <channel link or @username>\n"
                           "• /invite 2 https://t.me/+abc… — only userbot 2 joins\n"
                           "• /invite @somechannel — EVERY userbot joins")
            return
        n, raw = parsed
        if n is not None and not (1 <= n <= mgr.count()):
            await ev.reply(f"⚠️ No account #{n} — {mgr.count()} loaded. /stats to see them.")
            return
        idxs = [n - 1] if n is not None else list(range(mgr.count()))
        for idx in idxs:
            await _join_one(ev, idx, raw)
            await asyncio.sleep(3)          # paced — Telegram-safe

    @bot.on(events.NewMessage(pattern=r"^/leave(?:\s+([\s\S]+))?$"))
    async def leave_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        mgr = _sm[0]
        parsed = _parse_invite(ev)
        if mgr is None or not parsed:
            await ev.reply("Usage: /leave [account#] <channel id or link>\n"
                           "• /leave 1 -1001234567890 — only userbot 1 leaves\n"
                           "• /leave -1001234567890 — EVERY userbot leaves")
            return
        n, raw = parsed
        if n is not None and not (1 <= n <= mgr.count()):
            await ev.reply(f"⚠️ No account #{n} — {mgr.count()} loaded.")
            return
        from telethon.tl.functions.channels import LeaveChannelRequest
        idxs = [n - 1] if n is not None else list(range(mgr.count()))
        for idx in idxs:
            c = mgr.all()[idx]
            name = f"acc{idx + 1}/{mgr.count()}"
            try:
                ent = await c.get_entity(_parse_chat_id(raw))
                title = getattr(ent, "title", None) or raw
                await c(LeaveChannelRequest(ent))
                await ev.reply(f"🚪 {name} left **{title}**")
            except Exception as e:
                await ev.reply(f"⚠️ {name} couldn't leave {raw}: {type(e).__name__} {str(e)[:150]}")
            await asyncio.sleep(2)

    @bot.on(events.NewMessage(pattern=r"^/avoid(?:\s+([\s\S]+))?$"))
    async def avoid_cmd(ev):
        """v40: GLOBAL DB2 strip — applies to every target's DB2 mirror."""
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip().strip('"').strip()
        if not arg:
            av = await DB.get_global_avoids()
            listing = "\n".join(f"  {i+1}. `{t}`" for i, t in enumerate(av)) or "  (none)"
            await ev.reply(f"🧼 GLOBAL avoid strings (stripped from ALL DB2 captions):\n{listing}\n\n"
                           "Add: /avoid \"text to strip\" · Remove: /removegavoid <#>\n"
                           "Per-target strips still work via /avoidtext.")
            return
        av, added = await DB.add_global_avoid(arg)
        await ev.reply((f"✅ Global avoid added — `{arg}` is now stripped from every DB2 caption. "
                        f"({len(av)} global rule(s))") if added else
                       f"ℹ️ `{arg}` is already a global avoid.")

    @bot.on(events.NewMessage(pattern=r"^/removegavoid(?:\s+(\d+))?$"))
    async def removegavoid_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        res = await DB.remove_global_avoid(int(arg)) if arg.isdigit() else None
        if res is None:
            await ev.reply("Usage: /removegavoid <#> — bare /avoid lists them with numbers.")
            return
        av, removed = res
        await ev.reply(f"🗑 Removed global avoid `{removed}` — {len(av)} left.")

    @bot.on(events.NewMessage(pattern=r"^/replaceword(?:\s+([\s\S]+))?$"))
    async def replaceword_cmd(ev):
        """v40: GLOBAL DB2 word/phrase replace — runs BEFORE the avoid strip."""
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        if not arg:
            pairs = await DB.get_replace_words()
            listing = "\n".join(f"  {i+1}. `{p['old']}` → `{p['new']}`"
                                for i, p in enumerate(pairs)) or "  (none)"
            await ev.reply(f"🔁 GLOBAL replace rules (applied to ALL DB2 captions):\n{listing}\n\n"
                           "Add: /replaceword \"old text\" \"new text\" · Remove: /removereplace <#>")
            return
        try:
            parts = [p for p in shlex.split(arg)]
        except ValueError:
            parts = []
        if len(parts) != 2:
            await ev.reply("Usage: /replaceword \"old text\" \"new text\" — quote both. "
                           "Use \"\" as the new text to delete the word instead.")
            return
        old_w, new_w = parts
        pairs = await DB.add_replace_word(old_w, new_w)
        await ev.reply(f"✅ Global replace saved: `{old_w}` → `{new_w or '(deleted)'}` "
                       f"— applied to every DB2 caption before the avoid strip. "
                       f"({len(pairs)} rule(s))")

    @bot.on(events.NewMessage(pattern=r"^/removereplace(?:\s+(\d+))?$"))
    async def removereplace_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        res = await DB.remove_replace_word(int(arg)) if arg.isdigit() else None
        if res is None:
            await ev.reply("Usage: /removereplace <#> — bare /replaceword lists them with numbers.")
            return
        pairs, removed = res
        await ev.reply(f"🗑 Removed replace rule `{removed['old']}` → `{removed['new']}` "
                       f"— {len(pairs)} left.")

    # ---------- wizards (all accept inline args too) ----------
    @bot.on(events.NewMessage(pattern=r"^/target(?:\s+(.+))?$"))
    async def target_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        if arg:
            _pending[ev.sender_id] = ("target_db", _parse_chat_id(arg))
            await ev.reply(f"Target: **{_parse_chat_id(arg)}**\nNow send the DB channel id for THIS target "
                           f"(where its videos+srt go).\nCancel: /cancel")
            return
        _pending[ev.sender_id] = ("target_id", None)
        await ev.reply("Send me the TARGET channel id (numeric like -100… or @username).\nCancel: /cancel")

    # ---------- v39: multi-bypass pool + domain routing wizard ----------
    @bot.on(events.NewMessage(pattern=r"^/addbypass(?:\s+(.+))?$"))
    async def addbypass_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        if arg:
            await _do_addbypass(ev, arg)
            return
        _pending[ev.sender_id] = ("addbypass", None)
        await ev.reply("Send me the bypass bot @username (e.g. @BypassBot_A) or a "
                       "GROUP id (-100… / a t.me/c/ link) to ADD to the bypass pool.\n"
                       "Pool bots are tried in order for any short link with no domain "
                       "rule. See the pool with /bypasslist.\nCancel: /cancel")

    async def _do_addbypass(ev, raw):
        v = _parse_chat_id(raw)
        ok, is_bot = await _validate_bypass_endpoint(scrape_client, ev, v)
        if not ok:
            return
        pool, added = await DB.add_bypass(v)
        if added:
            await ev.reply(f"✅ Added {v} ({'bot' if is_bot else 'group'}) to the bypass "
                           f"pool — {len(pool)} endpoint(s) total. /bypasslist to view.")
        else:
            await ev.reply(f"ℹ️ {v} is already in the bypass pool (#{pool.index(v)+1}).")

    @bot.on(events.NewMessage(pattern=r"^/removebypass(?:\s+(\d+))?$"))
    async def removebypass_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        pool = await DB.get_bypass_pool()
        if not pool:
            await ev.reply("The bypass pool is empty — add one with /bypass or /addbypass.")
            return
        if not arg.isdigit():
            listing = "\n".join(f"  {i+1}. `{p}`" for i, p in enumerate(pool))
            await ev.reply(f"Usage: /removebypass <number>\nPool:\n{listing}")
            return
        res = await DB.remove_bypass(int(arg))
        if res is None:
            await ev.reply(f"⚠️ No pool endpoint #{arg} — see /bypasslist.")
            return
        pool2, removed = res
        await ev.reply(f"🗑 Removed {removed} from the pool — {len(pool2)} endpoint(s) left.")

    @bot.on(events.NewMessage(pattern=r"^/bypasslist$"))
    async def bypasslist_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        cfg = await DB.get_config()
        pool = await DB.get_bypass_pool()
        rules = await DB.get_bypass_domains()
        lines = ["🔁 **Bypass pool** (tried in order for links with no domain rule):"]
        lines += ([f"  {i+1}. `{p}`" for i, p in enumerate(pool)] or ["  (empty)"])
        lines.append(f"• Last-resort fallback (/altbypass): `{cfg.get('alt_bypass_id')}`")
        lines.append("🌐 **Domain rules** (these links go ONLY to their bot):")
        lines += ([f"  {i+1}. `{r['domain']}` → `@{r['endpoint']}`"
                   for i, r in enumerate(rules)] or ["  (none)"])
        lines.append("\n/addbypass · /removebypass <n> · /domainbypass · /deldomain <n>")
        await ev.reply("\n".join(lines))

    @bot.on(events.NewMessage(pattern=r"^/domainbypass(?:\s+([\s\S]+))?$"))
    async def domainbypass_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        if arg:
            parts = arg.split()
            if len(parts) == 2:
                await _do_domainbypass_save(ev, parts[0], parts[1])
            else:
                await ev.reply("Usage: /domainbypass <domain> <@bot or group id> — "
                               "e.g. /domainbypass babylinks.in @BypassBot_A\n"
                               "or bare /domainbypass for the wizard.")
            return
        _pending[ev.sender_id] = ("domainbypass_domain", None)
        await ev.reply("Step 1/2 — send the short-link DOMAIN to route "
                       "(e.g. `babylinks.in`, or a wildcard like `aerolinks.*`; "
                       "pasting a full link works too).\nCancel: /cancel")

    async def _do_domainbypass_save(ev, domain_raw, endpoint_raw):
        from scraper import norm_domain
        domain = norm_domain(domain_raw)
        if not domain:
            await ev.reply(f"⚠️ Couldn't read a domain from `{domain_raw}` — send e.g. "
                           "`babylinks.in` or `aerolinks.*`.")
            return
        v = _parse_chat_id(endpoint_raw)
        ok, is_bot = await _validate_bypass_endpoint(scrape_client, ev, v)
        if not ok:
            return
        await DB.add_bypass_domain(domain, v)
        await ev.reply(f"✅ Domain rule saved: `{domain}` → `{v}` "
                       f"({'bot' if is_bot else 'group'}).\n"
                       "Links on that domain go ONLY to that endpoint. /bypasslist to view.")

    @bot.on(events.NewMessage(pattern=r"^/deldomain(?:\s+(\d+))?$"))
    async def deldomain_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        rules = await DB.get_bypass_domains()
        if not rules:
            await ev.reply("No domain rules set — add one with /domainbypass.")
            return
        if not arg.isdigit():
            listing = "\n".join(f"  {i+1}. `{r['domain']}` → `@{r['endpoint']}`"
                                for i, r in enumerate(rules))
            await ev.reply(f"Usage: /deldomain <number>\nRules:\n{listing}")
            return
        res = await DB.remove_bypass_domain(int(arg))
        if res is None:
            await ev.reply(f"⚠️ No domain rule #{arg} — see /bypasslist.")
            return
        rules2, removed = res
        await ev.reply(f"🗑 Removed rule `{removed['domain']}` → `@{removed['endpoint']}` "
                       f"— {len(rules2)} rule(s) left.")

    @bot.on(events.NewMessage(pattern=r"^/(bypass|adddb|altbypass)(?:\s+(.+))?$"))
    async def simple_wizard(ev):
        if not await _admin(ev.sender_id):
            return
        cmd = ev.pattern_match.group(1)
        arg = (ev.pattern_match.group(2) or "").strip()
        field = {"bypass": "bypass_id", "adddb": "db_id",
                 "altbypass": "alt_bypass_id"}[cmd]
        if cmd == "bypass" and not arg:
            # v39: /bypass manages pool slot #1 — show the pool in the prompt
            pool = await DB.get_bypass_pool()
            if pool:
                listing = "\n".join(f"  {i+1}. `{p}`" for i, p in enumerate(pool))
                await ev.reply(f"Current bypass pool:\n{listing}\n")
        if arg:
            await _save_simple(ev, field, _parse_chat_id(arg))
            return
        _pending[ev.sender_id] = (field, None)
        if field == "bypass_id":
            await ev.reply(
                "Send me bypass bot #1 — a BOT @username (e.g. @dex_fekkyeww_bot) "
                "or a GROUP id (like -100…). This REPLACES pool slot #1; add more "
                "bots with /addbypass.\n"
                "Bot endpoints reply in DM with the bypassed link in text — no "
                "'Open link' button needed.\nCancel: /cancel")
        elif field == "alt_bypass_id":
            await ev.reply(
                "Send me the ALT bypass endpoint (GROUP id or BOT @username).\n"
                "Used ONLY when the primary /bypass doesn't return a link — "
                "the second session (STRING_SESSION2) talks to it when it's the "
                "active account. Both fail = you get a DM alert with the post link.\n"
                "Cancel: /cancel")
        else:
            await ev.reply("Send me the fallback DB channel id (numeric like -100… or @username).\nCancel: /cancel")

    async def _save_simple(ev, field, v):
        try:
            ent = await scrape_client.get_entity(v)
        except Exception as e:
            await ev.reply(f"⚠️ Can't access that chat with the userbot account: {e}")
            return
        if field == "db_id" and not _entity_ok(ent, "channel"):
            await ev.reply(_not_a_channel_msg(v, "channel"))
            return
        if field in ("bypass_id", "alt_bypass_id"):
            # bypass endpoints can be a GROUP (Channel/Chat) OR a BOT (User with bot=True)
            is_group = _entity_ok(ent, "group")
            is_bot = isinstance(ent, User) and getattr(ent, "bot", False)
            if not (is_group or is_bot):
                await ev.reply(
                    f"⚠️ {v} isn't a group and isn't a bot — bypass must be one of those.\n"
                    "For a bot, send its @username (e.g. @dex_fekkyeww_bot). "
                    "For a group, paste any message link from the group "
                    "(https://t.me/c/1234567890/12) or its -100… id.")
                return
            kind = "bot" if is_bot else "group"
            if field == "bypass_id":
                # v39: /bypass manages pool slot #1 — replace it (or insert
                # when the pool is empty), keeping any /addbypass extras.
                pool = await DB.get_bypass_pool()
                if pool:
                    pool[0] = v
                else:
                    pool = [v]
                await DB.set_config("bypass_pool", pool)
                await DB.set_config("bypass_id", pool[0])
                await ev.reply(f"✅ Bypass pool slot #1 = {v} ({kind}) — "
                               f"{len(pool)} pool endpoint(s) total. "
                               "More bots: /addbypass. View: /bypasslist.")
                return
            label = "ALT bypass"
            await DB.set_config(field, v)
            await ev.reply(f"✅ Saved {label} = {v} ({kind}). "
                           + ("No 'Open link' button needed — the bypassed t.me link is read from the reply text."
                              if is_bot else
                              "The tagger's 'Open link' button reply is expected.")
                           + ("" if field == "bypass_id" else
                              " Used automatically if the primary /bypass fails."))
            return
        await DB.set_config(field, v)
        await ev.reply(f"✅ Saved {field} = {v}")

    @bot.on(events.NewMessage(pattern=r"^/deltarget(?:\s+(.+))?$"))
    async def deltarget_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        targets = await DB.get_targets()
        if not targets:
            await ev.reply("No target channels set. Add one with /target")
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        if arg:
            await _do_remove(ev, arg)
            return
        _pending[ev.sender_id] = ("deltarget", None)
        listing = await _list_targets_text(scrape_client)
        await ev.reply(f"{listing}\n\nSend the id (or list number) to REMOVE.\n"
                       "Progress is kept — re-adding later resumes where it left off.\nCancel: /cancel")

    async def _do_remove(ev, raw):
        targets = await DB.get_targets()
        if raw.isdigit() and 1 <= int(raw) <= len(targets):
            v = targets[int(raw) - 1]["id"]
        else:
            v = _parse_id(raw)
        if v not in [t["id"] for t in targets]:
            await ev.reply(f"⚠️ {v} is not in your targets list.")
            return
        remaining = await DB.remove_target(v)
        await ev.reply(f"🗑 Removed {v} (its progress is kept).\n"
                       f"Targets left: {[t['id'] for t in remaining] or 'none'}")

    @bot.on(events.NewMessage(pattern=r"^/setdb(?:\s+(.+))?$"))
    async def setdb_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        targets = await DB.get_targets()
        if not targets:
            await ev.reply("No target channels. Add one with /target")
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        if arg:
            await _do_setdb(ev, arg)
            return
        _pending[ev.sender_id] = ("setdb", None)
        listing = await _list_targets_text(scrape_client)
        await ev.reply(f"{listing}\n\nSend: <number> <db_id>  (e.g.  2 -100999888777)\nCancel: /cancel")

    async def _do_setdb(ev, arg):
        parts = arg.split()
        targets = await DB.get_targets()
        if len(parts) != 2 or not parts[0].isdigit() or not (1 <= int(parts[0]) <= len(targets)):
            await ev.reply("⚠️ Format: <target number> <db_id> — e.g.  2 -100999888777")
            return
        t = targets[int(parts[0]) - 1]
        dbid = _parse_chat_id(parts[1])
        try:
            ent = await scrape_client.get_entity(dbid)
        except Exception as e:
            await ev.reply(f"⚠️ Can't access that DB channel with the userbot account: {e}")
            return
        if not _entity_ok(ent, "channel"):
            await ev.reply(_not_a_channel_msg(dbid, "channel"))
            return
        await DB.set_target_db(t["id"], dbid)
        await ev.reply(f"✅ Target {t['id']} now posts to DB {dbid}")

    # ---------- v42: per-target link mode (button vs caption hyperlink) ----------
    @bot.on(events.NewMessage(pattern=r"^/targatelinkmode(?:\s+([\s\S]+))?$"))
    async def targatelinkmode_cmd(ev):
        """Change how a target's download link is found, without re-adding it:
        /targatelinkmode <n> button [button text] | /targatelinkmode <n> caption <trigger text>
        v51: button mode takes an optional custom button text (Join / Watch Now ...)."""
        if not await _admin(ev.sender_id):
            return
        targets = await DB.get_targets()
        if not targets:
            await ev.reply("No target channels. Add one with /target")
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        parts = arg.split(None, 2)
        if (len(parts) < 2 or not parts[0].isdigit()
                or not (1 <= int(parts[0]) <= len(targets))
                or parts[1].lower() not in ("button", "caption")
                or (parts[1].lower() == "caption" and (len(parts) < 3 or not parts[2].strip()))):
            lines = ["Usage:\n  /targatelinkmode <n> button [button text]"
                     "\n  /targatelinkmode <n> caption <trigger text>\n"]
            for i, t in enumerate(targets):
                mode = t.get("link_mode") or "button"
                lines.append(f"  {i+1}. `{t['id']}` — mode: **{mode}**"
                             + (f", trigger: `{t.get('link_trigger')}`"
                                if mode == "caption" else ""))
            await ev.reply("\n".join(lines))
            return
        t = targets[int(parts[0]) - 1]
        mode = parts[1].lower()
        # v51: button mode also accepts a custom button text
        trigger = parts[2].strip() if (mode in ("caption", "button") and len(parts) > 2) else None
        await DB.set_target_link_mode(t["id"], mode, trigger)
        await ev.reply(f"✅ Target {int(parts[0])} link mode = **{mode.upper()}**"
                       + (f" — trigger text: `{trigger}`" if (mode == "caption" and trigger)
                          else f" — button text: `{trigger}`" if (mode == "button" and trigger)
                          else ""))

    # ---------- v42: media-bot image collection switch ----------
    @bot.on(events.NewMessage(pattern=r"^/keepimages(?:\s+(\S+))?$"))
    async def keepimages_cmd(ev):
        """GLOBAL switch: OFF = images from the media bot are discarded before
        DB delivery (videos/.srt/text are never touched). Default ON."""
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip().lower()
        if arg in ("on", "off"):
            await DB.set_keep_images(arg == "on")
            await ev.reply("✅ Images from the media bot will now be "
                           + ("COLLECTED and forwarded to the DB (like videos)."
                              if arg == "on" else
                              "DISCARDED — only videos/.srt/text are forwarded to the DB."))
            return
        cur = await DB.get_keep_images()
        await ev.reply(f"🖼 keepimages is currently **{'ON' if cur else 'OFF'}** "
                       "(global, all targets).\nUsage: /keepimages on|off")

    # ---------- v43: DB2 duplicate scanner ----------
    @bot.on(events.NewMessage(pattern=r"^/scan4duplicates(?:\s+(\S+))?$"))
    async def scan4duplicates_cmd(ev):
        """Scan a DB2 channel for duplicate COVER-POST captions.
        v43.1: history is read by the USERBOT (bot accounts are blocked from
        GetHistoryRequest by Telegram, even as admins).
        v43.2: only the STORY paragraph is compared — metadata lines
        (Episode/Subtitle/Censorship/Rating/Network...), hashtag lines
        (#uncensored #recommended) and the 'edited' tail are stripped first.
        v43.3: only COVER POSTS (photo messages) are scanned — video/.srt
        captions are ignored, because the cover is the post's identity and
        identical videos carry near-identical captions anyway.
        Compares the first 10 words (lowercased, punctuation stripped) of every
        text/caption with difflib.SequenceMatcher — pairs >= 75% similar are
        flagged, clustered (A~B and B~C merge into one group), and reported as
        t.me/c/ links in chunks that respect Telegram's length limit. Fully
        async: yields every 100 fetched messages and every 50 compare rounds
        so the scraper loop is never blocked."""
        if not await _admin(ev.sender_id):
            return
        arg = (ev.pattern_match.group(1) or "").strip()
        if not arg:
            await ev.reply("Usage: /scan4duplicates <channel_id> — the DB2 channel to scan "
                           "(numeric id, @username, or a t.me/c/… message link).")
            return
        cid = _parse_chat_id(arg)
        # v43.1: scan via the USERBOT, not the control bot — bot accounts are
        # BLOCKED from GetHistoryRequest even as channel admins ("The API access
        # for bot users is restricted"), so bot.iter_messages always aborts at 0
        # messages. The userbot only needs channel MEMBERSHIP (join with /invite).
        try:
            ent = await scrape_client.get_entity(cid)
        except Exception as e:
            await ev.reply(f"⚠️ Can't access {cid} — the USERBOT must be a member "
                           f"of that channel (join it with /invite).\n`{e}`")
            return
        title = getattr(ent, "title", None) or str(cid)
        status = await ev.reply(f"🔍 Scanning **{title}** for duplicate captions…")
        snippets = []  # (msg_id, normalized 10-word snippet)
        count = 0
        try:
            async for m in scrape_client.iter_messages(ent):  # v43.1: userbot reads history
                count += 1
                if count % 100 == 0:
                    await asyncio.sleep(0.1)  # never starve the event loop
                # v43.3: only COVER POSTS count — skip video and .srt
                # captions entirely; the photo post is the post's identity
                if not m.photo:
                    continue
                # v43.2: compare ONLY the story description — shared hashtags
                # and the Episode/Subtitle/Censorship/Rating/Network block must
                # never make two different posts match
                words = _story_snippet(m.message or "").split()
                if len(words) < 3:   # tiny snippets false-positive on everything
                    continue
                snippets.append((m.id, " ".join(words)))
        except Exception as e:
            try:
                await status.edit(f"⚠️ Scan aborted after {count} message(s): `{e}`")
            except Exception:
                pass
            return
        # pairwise fuzzy compare — quick_ratio pre-filters keep the O(n²) cheap;
        # matching pairs merge into clusters via union-find so a triple duplicate
        # reports as ONE group with 3 links, not two overlapping pairs
        parent = list(range(len(snippets)))

        def _find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(len(snippets)):
            si = snippets[i][1]
            for j in range(i + 1, len(snippets)):
                sm = difflib.SequenceMatcher(None, si, snippets[j][1])
                if sm.real_quick_ratio() < 0.75 or sm.quick_ratio() < 0.75:
                    continue
                if sm.ratio() >= 0.75:
                    ri, rj = _find(i), _find(j)
                    if ri != rj:
                        parent[ri] = rj
            if i % 50 == 0:
                await asyncio.sleep(0)  # yield during the O(n²) pass
        clusters = {}
        for idx in range(len(snippets)):
            clusters.setdefault(_find(idx), []).append(idx)
        groups = [sorted(g, key=lambda x: snippets[x][0])
                  for g in clusters.values() if len(g) > 1]
        if not groups:
            try:
                await status.edit(f"✅ **{title}**: scanned {count} message(s) "
                                  f"({len(snippets)} with captions) — no duplicates found.")
            except Exception:
                pass
            return
        lines = [f"🔍 **Duplicates Found in {title}:**",
                 f"(scanned {count} messages — {len(groups)} duplicate group(s))\n"]
        for g in sorted(groups, key=lambda g: snippets[g[0]][0]):
            disp = snippets[g[0]][1]
            disp = disp[:80] + ("…" if len(disp) > 80 else "")
            lines.append(f'📝 Matching Text: *"{disp}"*')
            for k, idx in enumerate(g):
                lines.append(f"🔗 Link {k + 1}: {_c_link(cid, snippets[idx][0])}")
            lines.append("")
        # chunk the report to respect Telegram's ~4096-char message limit
        chunks, cur = [], ""
        for ln in "\n".join(lines).split("\n"):
            if cur and len(cur) + len(ln) + 1 > 3500:
                chunks.append(cur)
                cur = ln
            else:
                cur = (cur + "\n" + ln) if cur else ln
        if cur:
            chunks.append(cur)
        try:
            await status.edit(chunks[0])
        except Exception:
            await ev.reply(chunks[0])
        for c in chunks[1:]:
            await ev.reply(c)
            await asyncio.sleep(0.5)

    # ---------- v45/v45.1: DB2 duplicate-skip index ----------
    @bot.on(events.NewMessage(pattern=r"^/dupescan(?:\s+(\d+))?$"))
    async def dupescan_cmd(ev):
        """(Re)build a target's DB2 duplicate-skip index. v45.1: targets
        sharing the SAME DB2 share one index — if a sibling is already
        indexed, this target just copies it (no rescan); a forced rebuild
        also refreshes every sibling."""
        if not await _admin(ev.sender_id):
            return
        targets = await DB.get_targets()
        arg = ev.pattern_match.group(1)
        if not targets:
            await ev.reply("No targets set — /target first.")
            return
        if not (arg and arg.isdigit() and 1 <= int(arg) <= len(targets)):
            cur = []
            for i, t in enumerate(targets):
                doc = await dedup.get_cover_fp(t["id"])
                nfp = len((doc or {}).get("fingerprints") or [])
                cur.append(f"  {i+1}. {t['id']} → DB2 {t.get('db2_id') or '(off)'}"
                           f" — {nfp} fingerprint(s)")
            await ev.reply("Usage: /dupescan <target #> — rebuild its DB2 duplicate index.\n"
                           + "\n".join(cur))
            return
        t = targets[int(arg) - 1]
        if not t.get("db2_id"):
            await ev.reply(f"⚠️ Target {arg} has no DB2 — set one with /setdb2.")
            return
        try:
            await scrape_client.get_entity(t["db2_id"])
        except Exception as e:
            await ev.reply(f"⚠️ Can't access DB2 — the USERBOT must be a member "
                           f"(join it with /invite).\n`{e}`")
            return
        siblings = [s for s in await dedup.db2_owners(t["db2_id"]) if s != t["id"]]
        shared = await dedup.share_db2(t["id"], t["db2_id"])
        if shared:
            nfp = len(dedup._FPS.get(t["id"]) or [])
            await ev.reply(f"🧬 Target {t['id']} now shares the existing DB2 index "
                           f"from target {shared} — {nfp} fingerprint(s), no rescan.\n"
                           f"(Sibling targets on this DB2: {siblings or 'none'})")
            return
        dedup.invalidate(t["id"])
        for sib in siblings:
            dedup.invalidate(sib)
        status = await ev.reply(f"🔍 Scanning DB2 {t['db2_id']} for cover-post fingerprints…"
                                + (f"\n(Will also index sibling target(s): {siblings})"
                                   if siblings else ""))
        try:
            res = await dedup.scan_db2(scrape_client, t["id"], t["db2_id"])
            await status.edit(f"✅ Duplicate index rebuilt for target {t['id']}: "
                              f"{res.get('count', 0)} cover fingerprint(s) from "
                              f"{res.get('scanned', 0)} message(s) in "
                              f"{res.get('seconds', '?')}s.\n"
                              + (f"Sibling target(s) synced too: {res.get('synced')}\n"
                                 if res.get("synced") else "")
                              + "Scraping now skips any target cover matching ≥90%.")
        except Exception as e:
            await status.edit(f"⚠️ DB2 scan failed: `{e}` — scraping continues ungated.")

    # ---------- v45: DB2 duplicate-skip index ----------
    @bot.on(events.NewMessage(pattern=r"^/dupescan(?:\s+(\d+))?$"))
    async def dupescan_cmd(ev):
        """Manually (re)build a target's duplicate-skip index from its DB2
        cover posts. Normally NOT needed — /target and /setdb2 trigger it
        automatically, and every scraped cover is added afterwards. Use this
        to FORCE a rebuild (e.g. after mass-editing DB2 captions)."""
        if not await _admin(ev.sender_id):
            return
        targets = await DB.get_targets()
        arg = ev.pattern_match.group(1)
        if not targets:
            await ev.reply("No targets set — /target first.")
            return
        if not (arg and arg.isdigit() and 1 <= int(arg) <= len(targets)):
            cur = []
            for i, t in enumerate(targets):
                doc = await dedup.get_cover_fp(t["id"])
                n = len((doc or {}).get("fingerprints") or [])
                cur.append(f"  {i+1}. {t['id']} → DB2 {t.get('db2_id') or '(off)'}"
                           f" — {n} fingerprint(s)")
            await ev.reply("Usage: /dupescan <target #> — rebuild its DB2 duplicate index.\n"
                           + "\n".join(cur))
            return
        t = targets[int(arg) - 1]
        if not t.get("db2_id"):
            await ev.reply(f"⚠️ Target {arg} has no DB2 — set one with /setdb2.")
            return
        try:
            await scrape_client.get_entity(t["db2_id"])
        except Exception as e:
            await ev.reply(f"⚠️ Can't access DB2 — the USERBOT must be a member "
                           f"(join it with /invite).\n`{e}`")
            return
        dedup.invalidate(t["id"])  # force a fresh rebuild
        status = await ev.reply(f"🔍 Scanning DB2 {t['db2_id']} for cover-post fingerprints…")
        try:
            res = await dedup.scan_db2(scrape_client, t["id"], t["db2_id"])
            await status.edit(f"✅ Duplicate index rebuilt for target {t['id']}: "
                              f"{res.get('count', 0)} cover fingerprint(s) from "
                              f"{res.get('scanned', 0)} message(s) in "
                              f"{res.get('seconds', '?')}s.\n"
                              "Scraping now skips any target cover matching ≥90%.")
        except Exception as e:
            await status.edit(f"⚠️ DB2 scan failed: `{e}` — scraping continues ungated.")

    @bot.on(events.NewMessage(pattern=r"^/cancel$"))
    async def cancel(ev):
        _pending.pop(ev.sender_id, None)
        await ev.reply("Cancelled.")

    @bot.on(events.NewMessage())
    async def wizard_answer(ev):
        item = _pending.get(ev.sender_id)
        if not item or not await _admin(ev.sender_id) or ev.raw_text.startswith("/"):
            return  # a new /command cancels the pending wizard instead of being eaten
        kind, extra = item
        if kind == "target_id":
            tid = _parse_chat_id(ev.raw_text)
            try:
                ent = await scrape_client.get_entity(tid)
            except Exception as e:
                await ev.reply(f"⚠️ Can't access that channel with the userbot account: {e}")
                return
            if not _entity_ok(ent, "channel"):
                await ev.reply(_not_a_channel_msg(tid, "channel"))
                return
            _pending[ev.sender_id] = ("target_db", tid)
            await ev.reply(f"Target: **{tid}**\nNow send the DB channel id for THIS target "
                           f"(where its videos+srt go).\nCancel: /cancel")
            return
        if kind == "target_db":
            dbid = _parse_chat_id(ev.raw_text)
            _pending[ev.sender_id] = ("target_db2", (extra, dbid))
            await ev.reply(f"DB: **{dbid}**\nOptional: send the DB2 (clean mirror) channel id for "
                           "this target — the BOT will re-post everything from DB into DB2 "
                           "with credits/links stripped. Send `skip` to leave it off.\n"
                           "Cancel: /cancel")
            return
        if kind == "target_db2":
            tid, dbid = extra
            raw = ev.raw_text.strip().lower()
            db2 = None if raw in ("skip", "-", "none", "off", "0") else _parse_chat_id(ev.raw_text)
            # v42: next wizard step — WHERE does the download link live?
            _pending[ev.sender_id] = ("target_mode", (tid, dbid, db2))
            await ev.reply("Is the download link in a **Button** or in the **Caption**?\n"
                           "• `button` — the post has an inline Download button (default)\n"
                           "• `caption` — the URL is a hidden hyperlink behind a text in the caption\n"
                           "Cancel: /cancel")
            return
        if kind == "target_mode":  # v42
            tid, dbid, db2 = extra
            raw = ev.raw_text.strip().lower()
            if raw.startswith("c"):
                _pending[ev.sender_id] = ("target_trigger", (tid, dbid, db2))
                await ev.reply("Caption mode — send the EXACT text that holds the hidden "
                               "hyperlink (e.g. `Download Here`). It is matched exactly "
                               "(fancy Unicode fonts like 𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 are folded, so plain "
                               "text is fine).\nCancel: /cancel")
            else:
                _pending[ev.sender_id] = ("target_btntext", (tid, dbid, db2))
                await ev.reply("Button mode — send the TEXT on the link button "
                               "(e.g. `Download`, `Join`, `Watch Now`). Send `skip` "
                               "to use the default Download. Matched after Unicode-fold, "
                               "so styled labels (𝗝𝗼𝗶𝗻) are fine.\nCancel: /cancel")
            return
        if kind == "target_btntext":  # v51: custom link-button text
            _pending.pop(ev.sender_id, None)
            tid, dbid, db2 = extra
            bt = ev.raw_text.strip()
            bt = None if bt.lower() in ("skip", "-", "none", "default", "") else bt
            await _add_target_with_db(scrape_client, ev, tid, dbid, db2,
                                      "button", bt)
            return
        if kind == "target_trigger":  # v42
            _pending.pop(ev.sender_id, None)
            tid, dbid, db2 = extra
            trigger = ev.raw_text.strip()
            if not trigger:
                await ev.reply("⚠️ Trigger text can't be empty — run /target again.")
                return
            await _add_target_with_db(scrape_client, ev, tid, dbid, db2,
                                      "caption", trigger)
            return
        if kind == "deltarget":
            _pending.pop(ev.sender_id, None)
            await _do_remove(ev, ev.raw_text.strip())
            return
        if kind == "setdb":
            _pending.pop(ev.sender_id, None)
            await _do_setdb(ev, ev.raw_text.strip())
            return
        if kind == "addbypass":  # v39: pool add via wizard
            _pending.pop(ev.sender_id, None)
            await _do_addbypass(ev, ev.raw_text.strip())
            return
        if kind == "domainbypass_domain":  # v39: step 1 — the domain
            from scraper import norm_domain
            domain = norm_domain(ev.raw_text)
            if not domain:
                await ev.reply(f"⚠️ Couldn't read a domain from `{ev.raw_text.strip()}` — "
                               "send e.g. `babylinks.in` or `aerolinks.*`.\nCancel: /cancel")
                return
            _pending[ev.sender_id] = ("domainbypass_endpoint", domain)
            await ev.reply(f"Step 2/2 — domain `{domain}`: now send the bypass bot "
                           "@username (or group id) that should handle ONLY links on "
                           "this domain.\nCancel: /cancel")
            return
        if kind == "domainbypass_endpoint":  # v39: step 2 — the bot
            _pending.pop(ev.sender_id, None)
            await _do_domainbypass_save(ev, extra, ev.raw_text.strip())
            return
        # simple fields: bypass_id / db_id
        _pending.pop(ev.sender_id, None)
        await _save_simple(ev, kind, _parse_chat_id(ev.raw_text))

    # ---------- view ----------
    @bot.on(events.NewMessage(pattern=r"^/targets(_text)?$"))
    async def targets_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        plain = ev.pattern_match.group(1) == "_text"
        targets = await DB.get_targets()
        if not targets:
            await ev.reply("No target channels. Add one with /target")
            return
        if not plain:
            # v37: try the Bot API rich charge sheet (table + coloured buttons);
            # falls back to the markdown listing when the Bot API server
            # doesn't support rich messages yet.
            rows = await _build_board_rows(scrape_client)
            if await richboard.send_targets_board(ev.chat_id, rows):
                return
        listing = await _list_targets_text(scrape_client)
        await ev.reply(listing + "\n\n/setdb to change a DB, /deltarget to remove, "
                                 "/pause <n> //resume <n> to pause/resume one target.")

    @bot.on(events.CallbackQuery(pattern=rb"^tglp:"))
    async def board_cb(ev):
        """v37: charge-sheet board buttons (Pause/Resume toggle, Refresh)."""
        if not await _admin(ev.sender_id):
            await ev.answer("⛔ Admins only", alert=True)
            return
        action, n = richboard.parse_callback(ev.data.decode())
        # v39.2: the tapped board's message id travels inside the callback
        # query — pass it through so Refresh/toggle EDITS that exact message
        # even after a Render restart wiped the in-memory board map (the old
        # getattr(ev, 'message_id') is always None on a CallbackQuery).
        _tap_mid = (getattr(getattr(ev, "query", None), "msg_id", None)
                    or getattr(getattr(ev, "original_update", None), "msg_id", None))
        # v38: boards expire after BOARD_TTL — late taps get a popup instead
        # of acting on a stale board (saves server resources too)
        if richboard.board_expired(ev.chat_id, _tap_mid):
            await ev.answer("This board has expired. Run /targets to open a fresh board.",
                            alert=True)
            return
        if action == "refresh":
            rows = await _build_board_rows(scrape_client)
            await richboard.refresh_board(ev.chat_id, rows, msg_id=_tap_mid)  # edits in place
            await ev.answer("🔄 Board refreshed")
            return
        if action == "toggle":
            targets = await DB.get_targets()
            if not (1 <= n <= len(targets)):
                await ev.answer("⚠️ That target no longer exists", alert=True)
                return
            t = targets[n - 1]
            if t.get("paused"):
                await DB.set_target_paused(t["id"], False)
                state.paused_ids.discard(t["id"])
                state.paused = False; state.user_paused = False
                state.abort = False; state.started = True
                state.reset_gen += 1; state._last_scan = None
                await ev.answer(f"▶️ Target {n} resumed")
            else:
                await DB.set_target_paused(t["id"], True)
                state.paused_ids.add(t["id"])
                state.reset_gen += 1; state._last_scan = None
                await ev.answer(f"⏸ Target {n} paused")
            rows = await _build_board_rows(scrape_client)
            await richboard.refresh_board(ev.chat_id, rows, msg_id=_tap_mid)  # edits in place — ▶️ Resume N flips to ⏸ Pause N on the SAME message
            return
        await ev.answer()

    @bot.on(events.NewMessage(pattern=r"^/lastpost(?:\s+(\d+))?$"))
    async def lastpost(ev):
        if not await _admin(ev.sender_id):
            return
        targets = await DB.get_targets()
        if not targets:
            await ev.reply("Set a target first: /target")
            return
        n = ev.pattern_match.group(1)
        idx = int(n) - 1 if n and n.isdigit() and 1 <= int(n) <= len(targets) else 0
        tid = targets[idx]["id"]
        from scraper import is_post, find_button
        from config import BTN_DOWNLOAD
        total = 0
        newest = None
        async for m0 in scrape_client.iter_messages(tid, limit=500):
            total += 1
            if newest is None and is_post(m0):
                newest = m0
        resume_at = await DB.get_progress(tid)
        if newest:
            m = newest
            btn = find_button(m, BTN_DOWNLOAD)
            cap = (m.message or "")[:200].replace("\n", " ")
            await ev.reply(f"📄 Target {idx+1}: {tid}\n• messages scanned: {total}"
                           + f"\n• newest post msg id: {m.id}"
                           + "\n• date: " + m.date.strftime("%Y-%m-%d %H:%M UTC")
                           + "\n• caption: " + cap
                           + "\n• Download button: " + ("yes — " + btn[2].text if btn else "NO")
                           + f"\n• current resume point: {resume_at}"
                           + "\n\nTip: /reset to scrape from post 1, /goto to start elsewhere.")
            return
        await ev.reply(f"Scanned {total} messages in {tid} — none look like posts "
                       "(media+caption+Download button).")

    # ---------- control ----------
    @bot.on(events.NewMessage(pattern=r"^/start$"))
    async def start_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        targets = await DB.get_targets()
        cfg = await DB.get_config()
        missing = []
        if not targets:
            missing.append("target (use /target)")
        if not await DB.get_bypass_pool():
            missing.append("bypass bot (/bypass or /addbypass)")
        if not cfg.get("db_id") and not all(t.get("db_id") for t in targets):
            missing.append("db (per-target via /setdb or fallback via /adddb)")
        if missing:
            await ev.reply("⚠️ Cannot start — missing: " + ", ".join(missing))
            return
        state.abort = False; state.paused = False; state.started = True
        await ev.reply("▶️ Scraper started. It scans each target from its saved point.\n"
                       "Check /progress anytime.")

    @bot.on(events.NewMessage(pattern=r"^/(pause|resume)(?:\s+(\S+))?$"))
    async def pause_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        action = ev.pattern_match.group(1)
        n = ev.pattern_match.group(2)
        if action == "pause":
            if n is None or n.lower() == "all":
                # v36 FIX: global pause is now persistent + visible (see
                # _pause_all_targets). Bare /pause or /pause all both trigger it.
                flipped, total = await _pause_all_targets()
                await ev.reply("⏸ Paused ALL targets"
                               + (f" ({flipped} newly paused)" if flipped else " (already paused)")
                               + ". Progress is saved in MongoDB — safe even if Render "
                               "crashes. /resume all to continue.")
                return
            if not n.isdigit():
                await ev.reply("Usage: /pause <target #> or /pause all — see /targets for numbers.")
                return
            targets = await DB.get_targets()
            if not (1 <= int(n) <= len(targets)):
                await ev.reply((await _list_targets_text(scrape_client) or "No targets set.") +
                               f"\n\n⚠️ Target number must be 1-{len(targets)} (see /targets).")
                return
            t = targets[int(n) - 1]
            if t.get("paused"):
                await ev.reply(f"⏸ Target {n} ({t['id']}) is already paused.")
                return
            await DB.set_target_paused(t["id"], True)
            state.paused_ids.add(t["id"])          # loop sees it within seconds
            state.reset_gen += 1; state._last_scan = None  # drop the current pass
            # v45.1: pending duplicate-skip details go INLINE in the pause reply
            _sk = dedup.consume_skips(t["id"])
            _extra = ("\n\n" + dedup.format_batch(t["id"], _sk,
                       f"pending at pause — {len(_sk)} skip(s)")) if _sk else ""
            await ev.reply(f"⏸ Target {n} ({t['id']}) paused — other targets keep scraping.\n"
                           f"/resume {n} to resume this one." + _extra)
            return
        # ---- /resume ----
        if n is not None and n.lower() != "all" and not n.isdigit():
            await ev.reply("Usage: /resume <target #>, /resume all, or bare /resume (everything).")
            return
        if n is None or n.lower() == "all":
            # v35: bare /resume resumes EVERYTHING (same as /resume all):
            # global flag + manual-pause flag + all per-target flags
            state.paused = False; state.user_paused = False
            state.abort = False; state.started = True
            targets = await DB.get_targets()
            unpaused = 0
            for t in targets:
                if t.get("paused"):
                    await DB.set_target_paused(t["id"], False)
                    unpaused += 1
            state.paused_ids.clear()
            state.reset_gen += 1; state._last_scan = None
            msg = "▶️ Resumed from saved progress."
            if unpaused:
                msg += f" ({unpaused} individually-paused target(s) resumed too.)"
            await ev.reply(msg)
            return
        if not n.isdigit():
            await ev.reply("Usage: /resume <target #> or /resume all — see /targets for numbers.")
            return
        targets = await DB.get_targets()
        if not (1 <= int(n) <= len(targets)):
            await ev.reply((await _list_targets_text(scrape_client) or "No targets set.") +
                           f"\n\n⚠️ Target number must be 1-{len(targets)} (see /targets).")
            return
        t = targets[int(n) - 1]
        await DB.set_target_paused(t["id"], False)
        state.paused_ids.discard(t["id"])
        state.paused = False; state.user_paused = False
        state.abort = False; state.started = True
        state.reset_gen += 1; state._last_scan = None
        rp = await DB.get_progress(t["id"])
        # v45.1: report this target's dupe-skip summary on resume
        _sk_total = dedup._count(t["id"])
        _pend = dedup.consume_skips(t["id"])
        _pend_str = ("\n" + dedup.format_batch(t["id"], _pend,
                     f"cleared on resume — {len(_pend)} pending skip(s)")) if _pend else ""
        _summary = f"\n🧬 Duplicates skipped so far (this session): {_sk_total}" if _sk_total else ""
        await ev.reply(f"▶️ Target {n} ({t['id']}) resumed — continues from message id {rp}."
                       + _summary + _pend_str)

    @bot.on(events.NewMessage(pattern=r"^/(status|current)$"))
    async def status_cmd(ev):
        if await _admin(ev.sender_id):
            cfg = await DB.get_config()
            listing = await _list_targets_text(scrape_client) or "  (none)"
            ind = sorted(str(i + 1) for i, t in enumerate(await DB.get_targets()) if t.get("paused"))
            await ev.reply(f"🤖 Running: {state.running} | Paused: {state.paused}"
                           + (f" | Individually paused targets: {', '.join(ind)}" if ind else "") + "\n"
                           f"📍 Stage: {state.stage}\n📄 Current post: {state.current_post}\n"
                           f"{listing}\n🔁 Bypass: {cfg.get('bypass_id')}\n"
                           f"🗄 Fallback DB: {cfg.get('db_id')}"
                           + dedup.status_lines())   # v45: per-target dupe counters

    @bot.on(events.NewMessage(pattern=r"^/progress$"))
    async def progress_cmd(ev):
        if await _admin(ev.sender_id):
            from bot import fmt_progress
            await ev.reply(await fmt_progress(scrape_client))

    @bot.on(events.NewMessage(pattern=r"^/skip$"))
    async def skip_cmd(ev):
        if await _admin(ev.sender_id):
            state.abort = True
            await ev.reply("⏭ Skipping current post…")

    @bot.on(events.NewMessage(pattern=r"^/stop$"))
    async def stop_cmd(ev):
        if await _admin(ev.sender_id):
            state.abort = True; state.running = False; state.started = False
            await ev.reply("🛑 Stopped. Progress saved — /resume or /start continues from the same post.")

    # ---------- per-target reset & goto ----------
    @bot.on(events.NewMessage(pattern=r"^/reset(?:\s+(\d+))?$"))
    async def reset_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        targets = await DB.get_targets()
        if not targets:
            await ev.reply("Set a target first: /target")
            return
        n = ev.pattern_match.group(1)
        if n and n.isdigit() and 1 <= int(n) <= len(targets):
            idx = int(n) - 1
        elif len(targets) == 1:
            idx = 0
        else:
            await ev.reply((await _list_targets_text()) +
                           "\n\nWhich target? Send /reset <number>  (e.g. /reset 2)")
            return
        tid = targets[idx]["id"]
        await DB.reset_progress(tid)
        state.reset_gen += 1
        state._last_scan = None
        await ev.reply(f"♻️ Progress reset for target {idx+1} ({tid}).\n"
                       "Next scan starts from POST 1. /start (or /resume) to begin.")

    @bot.on(events.NewMessage(pattern=r"^/goto\b"))
    async def goto_cmd(ev):
        if not await _admin(ev.sender_id):
            return
        targets = await DB.get_targets()
        if not targets:
            await ev.reply("Set a target first: /target")
            return
        parts = ev.raw_text.split(maxsplit=2)
        from scraper import parse_private_link
        tid = mid = None
        if len(parts) == 2:
            arg = parts[1]
            if arg.lstrip("-").isdigit():
                if len(targets) > 1:
                    listing = await _list_targets_text(scrape_client)
                    await ev.reply(f"{listing}\n\nMultiple targets — pick one:\n"
                                   f"/goto <number> <msg_id>  (e.g. /goto 2 120)\n"
                                   f"or use a message link: /goto https://t.me/c/<channel>/<msg>")
                    return
                tid, mid = targets[0]["id"], int(arg)
            else:
                cid, mid = parse_private_link(arg)
                if not mid:
                    await ev.reply("Couldn't parse that. Send /goto <msg_id>, /goto <n> <msg_id>, "
                                   "or /goto https://t.me/c/<channel>/<msg>")
                    return
                ids = [t["id"] for t in targets]
                if cid not in ids:
                    await ev.reply(f"⚠️ That link's channel ({cid}) is not in your targets. "
                                   "Add it with /target first.")
                    return
                tid = cid
        elif len(parts) == 3 and parts[1].isdigit():
            n = int(parts[1])
            if not (1 <= n <= len(targets)):
                await ev.reply(f"⚠️ Target number must be 1-{len(targets)}.")
                return
            arg = parts[2]
            tid = targets[n - 1]["id"]
            if arg.lstrip("-").isdigit():
                mid = int(arg)
            else:
                cid, mid = parse_private_link(arg)
                if not mid or cid != tid:
                    await ev.reply("⚠️ That link doesn't belong to the chosen target.")
                    return
        else:
            await ev.reply("Usage: /goto <msg_id> | /goto <target#> <msg_id> | /goto <message link>")
            return
        await DB.set_progress(tid, mid - 1)  # loop uses min_id=last_id -> starts AT mid
        state.reset_gen += 1
        state._last_scan = None
        await ev.reply(f"📌 Target {tid} will resume from message {mid}. /start or /resume to go.")


async def _bulk_edit(scrape_client, ev, channel, old, new):
    """/replace + /deletetext core — v25: TELEGRAM-SAFE PACING.
    Phase 1: scan (server-side search) and COLLECT all matching messages.
    Phase 2: a BACKGROUND worker edits ONE message every BULK_EDIT_DELAY
    seconds (default 2.5s) so Telegram's edit rate limit is never hit.
    FloodWaitError is slept through IN-PLACE and the SAME message retried
    (waits beyond BULK_MAX_FLOOD count as failures instead of parking
    forever). Live progress is edited into the status message every
    BULK_PROGRESS_EVERY edits. Returns the worker task (handlers ignore it)
    so the control bot stays responsive during long runs."""
    try:
        ent = await scrape_client.get_entity(channel)
    except Exception as e:
        await ev.reply(f"⚠️ Can't access that channel with the userbot account: {e}")
        return None
    title = getattr(ent, "title", str(channel))
    status = await ev.reply(f"🔍 Scanning **{title}** for messages containing: {old}…")
    matches = []
    try:
        async for m in scrape_client.iter_messages(ent, search=old):
            txt = m.message or ""
            if old not in txt:
                continue
            matches.append((m, txt))
    except Exception as e:
        try:
            await status.edit(f"⚠️ Scan failed ({e}).")
        except Exception:
            pass
        return None
    if not matches:
        await status.edit(f"✅ Nothing found in **{title}** containing: {old}")
        return None
    eta_min = len(matches) * BULK_EDIT_DELAY / 60
    await status.edit(
        f"📋 Found {len(matches)} message(s) in **{title}**.\n"
        f"Editing 1 every {BULK_EDIT_DELAY}s (Telegram-safe pacing) — ETA ~{eta_min:.1f} min.\n"
        "Flood waits are slept through automatically. Progress updates follow.\n"
        "\u23f8 Scraper auto-pauses while edits run (shared rate limit) and resumes after.")

    async def _worker():
        # v25.1: pause the scraper during bulk edits — Telegram's flood bucket
        # is ACCOUNT-WIDE; the scraper's sends share it (that caused the 219s
        # flood on the 99-message run while post 358 was being delivered).
        was_scraping = state.started and not state.paused
        if was_scraping:
            state.paused = True
        edited = failed = floods = 0
        last_err = None
        started = time.time()
        for m, txt in matches:
            new_txt = txt.replace(old, new)
            if not new:
                # /deletetext: tidy leftover double spaces / blank lines
                new_txt = re.sub(r"[ \t]{2,}", " ", new_txt)
                new_txt = re.sub(r"\n{3,}", "\n\n", new_txt).strip()
            if new_txt == txt:
                continue
            while True:
                try:
                    if not new_txt.strip():
                        # nothing left after the edit: Telegram forbids EMPTY text
                        # messages (MESSAGE_EMPTY). Media posts keep the file with
                        # an empty caption (legal); TEXT-ONLY posts are DELETED.
                        if getattr(m, "media", None) is not None:
                            await scrape_client.edit_message(ent, m, "", parse_mode=None)
                        else:
                            await scrape_client.delete_messages(ent, m)
                    else:
                        await scrape_client.edit_message(ent, m, new_txt, parse_mode=None)
                    edited += 1
                    break
                except FloodWaitError as fe:
                    secs = getattr(fe, "seconds", 60) or 60
                    if secs <= BULK_MAX_FLOOD:
                        floods += 1
                        try:
                            await status.edit(
                                f"⏳ Flood wait {secs}s from Telegram — sleeping it off "
                                f"(edited {edited}/{len(matches)} so far)…")
                        except Exception:
                            pass
                        await asyncio.sleep(secs + 5)
                        continue  # retry the SAME message after the wait
                    failed += 1
                    last_err = fe
                    break
                except Exception as e:
                    failed += 1
                    last_err = e
                    break
            await asyncio.sleep(BULK_EDIT_DELAY + random.uniform(0, 1.5))  # pacing + jitter — human-like, never a fixed burst pattern
            if edited and edited % BULK_PROGRESS_EVERY == 0:
                try:
                    await status.edit(f"⏳ Progress: {edited}/{len(matches)} edited"
                                      + (f", {failed} failed" if failed else "")
                                      + (f", {floods} flood wait(s) handled" if floods else ""))
                except Exception:
                    pass
        verb = "edited" if new else "cleaned"
        mins = (time.time() - started) / 60
        summary = (f"✅ Done — **{title}**: {verb} {edited}/{len(matches)} in {mins:.1f} min"
                   + (f", {floods} flood wait(s) slept through" if floods else ""))
        if failed:
            summary += f", {failed} failed (last error: {last_err})"
        try:
            await status.edit(summary)
        except Exception:
            pass
        if was_scraping:
            state.paused = False
            try:
                await ev.reply("▶️ Scraper resumed — bulk edit finished.")
            except Exception:
                pass

    return asyncio.ensure_future(_worker())


DEDUP_TAIL_INTERVAL = 1800  # v46: auto tail-scan every DB2 every 30 min


async def _dedup_tail_scheduler(scrape_client):
    """v46: index NEW DB2 covers automatically so /dupescan is never needed.
    One tail-scan per unique DB2 per cycle (siblings sharing a DB2 aren't
    scanned twice). Fully defensive — a failure logs and the loop continues."""
    await asyncio.sleep(15)               # let startup settle
    while True:
        try:
            seen = set()
            for t in await DB.get_targets():
                db2 = t.get("db2_id")
                if not db2 or db2 in seen:
                    continue
                seen.add(db2)
                res = await dedup.tail_scan_db2(scrape_client, db2)
                n = res.get("added") or 0
                if n:
                    logging.getLogger("botapi").info(
                        "dedup tail-scan DB2 %s: +%d fingerprint(s)", db2, n)
                    await dedup.notify_tail_added(db2, n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("botapi").exception(
                "dedup tail-scan scheduler error (continuing)")
        await asyncio.sleep(DEDUP_TAIL_INTERVAL)


async def start(scrape_client, sm=None):
    global bot
    bot = TelegramClient(MemorySession(), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    register(scrape_client, sm)
    await _menu()
    # v46: DB2 auto-index — startup tail-scan then every 30 min (manual DB2
    # posts get gated automatically; /dupescan is no longer required).
    _BG_TASKS.append(asyncio.ensure_future(_dedup_tail_scheduler(scrape_client)))
    return bot
