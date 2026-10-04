"""richboard.py — Bot API rich-message "charge sheet" board (v38).

Telethon (MTProto) can't render tables or colored inline buttons — those are
Bot API features (rich messages: sendRichMessage, Bot API 10.1+; button style:
danger/success/primary, Bot API 9.4+). This module is a thin aiohttp client on
top of the existing BOT_TOKEN that only sends / edits messages — it never polls,
so the Telethon control bot keeps receiving updates (incl. the board's button
taps via events.CallbackQuery) exactly as before.

Board features (v38):
  * 2-column button grid — Pause/Resume buttons pair up side-by-side; an odd
    last button goes full-width on its own row; Refresh is always last row.
  * In-place refresh — tapping a button EDITS the same board message
    (editMessageText with rich_message) instead of cluttering the chat.
  * Board expiry — buttons stop working after BOARD_TTL seconds; a late tap
    gets a popup "This board has expired. Run /targets to open a fresh board."
    v42: TTL raised to 30 minutes, and generating a NEW board instantly
    invalidates the chat's PREVIOUS board — a tap on an older, scrolled-up
    board answers with the expired popup and changes nothing (no stale state).
  * Linked cells everywhere — target titles link to their last scraped post
    (t.me/c/…), DB/DB2 use cached invite links.

rows = [{
    "n": 1, "id": -100…, "title": "Channel title",
    "t_link": "https://t.me/c/<id>/<last>",      # or None
    "db_title": "DB title", "db_link": "…",      # or None
    "db2_title": "DB2 title", "db2_link": "…",   # or None
    "resume": 372, "paused": False,
}, ...]
"""
import asyncio
import logging
import time

import aiohttp

from config import BOT_TOKEN

log = logging.getLogger("richboard")

_API = "https://api.telegram.org/bot{tok}/{method}"
_TIMEOUT = aiohttp.ClientTimeout(total=20)

BOARD_TTL = 1800         # v42: seconds before board buttons expire (30 min)

CB_PREFIX = "tglp:"          # callback namespace for this module
CB_REFRESH = CB_PREFIX + "refresh"
CB_TOGGLE = CB_PREFIX + "t:"  # + <n>

# message_id of the board we currently control per chat (one live board/chat)
_BOARDS = {}            # chat_id -> {"msg_id": int, "ts": float}
_BOARD_TS = {}          # msg_id -> monotonic ts (per-message expiry, capped)


# ---------------------------------------------------------------------------
# low-level Bot API call (send/edit only; never long-polls)
# ---------------------------------------------------------------------------
async def api_call(method, payload):
    """POST one Bot API method. Returns (ok: bool, result_or_error)."""
    url = _API.format(tok=BOT_TOKEN, method=method)
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as sess:
            async with sess.post(url, json=payload) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:
        log.warning("Bot API %s failed to send: %s", method, e)
        return False, str(e)
    if not data.get("ok"):
        log.warning("Bot API %s rejected: %s", method, data.get("description"))
        return False, data.get("description", "unknown error")
    return True, data.get("result")


# ---------------------------------------------------------------------------
# rich text helpers (RichText can be a plain string, an array, or typed objs)
# ---------------------------------------------------------------------------
def _rt_text(title, url=None):
    """Linked rich-text cell content: RichTextUrl or plain string."""
    title = (title or "—").replace("\n", " ").strip() or "—"
    if url:
        return {"type": "url", "text": title, "url": url}
    return title


def _cell(title, url=None, header=False, align="left"):
    c = {"align": align}
    if title is not None or url is not None:
        c["text"] = _rt_text(title, url)
    if header:
        c["is_header"] = True
    return c


# ---------------------------------------------------------------------------
# v52: GENERIC compact table — reused by /stats (worker/membership matrix).
# cells = list of rows; each row = list of (title, url_or_None) tuples.
# Row 0 is rendered as the header. Kept separate from the targets board below
# so /stats gets a compact table without the board's pause/resume keyboard.
# ---------------------------------------------------------------------------
def _table_cells(rows):
    """rows: [[(title, url), ...], ...] -> Bot API table cell matrix (row 0 header)."""
    out = []
    for ri, row in enumerate(rows):
        out.append([_cell(t, u, header=(ri == 0)) for (t, u) in row])
    return out


def build_table_payload(chat_id, heading, rows, footer=None):
    """sendRichMessage payload: one heading + one compact bordered table.
    is_compact squeezes padding so a wide matrix (many target columns) still
    fits on a phone screen."""
    blocks = [{"type": "heading", "size": 3, "text": heading},
              {"type": "table", "is_bordered": True, "is_striped": True,
               "is_compact": True, "cells": _table_cells(rows)}]
    if footer:
        blocks.append({"type": "footer", "text": footer})
    return {"chat_id": chat_id,
            "rich_message": {"blocks": blocks}}


async def send_table(chat_id, heading, rows, footer=None, retries=2):
    """Send a standalone compact table message. Returns True on success,
    False so the caller can fall back to a plain-text listing."""
    if not BOT_TOKEN or not rows:
        return False
    payload = build_table_payload(chat_id, heading, rows, footer)
    for attempt in range(retries + 1):
        ok, res = await api_call("sendRichMessage", payload)
        if ok:
            return True
        desc = str(res).lower()
        if any(k in desc for k in ("rich", "unsupported", "can't parse",
                                   "chat not found", "forbidden", "not found",
                                   "bad request")):
            log.warning("rich table unsupported (%s) — markdown fallback", desc)
            return False
        await asyncio.sleep(1.5 * (attempt + 1))
    return False


# ---------------------------------------------------------------------------
# board construction
# ---------------------------------------------------------------------------
def _grid_keyboard(rows):
    """2-column Pause/Resume grid. An odd leftover goes full-width on its own
    row; Refresh always occupies the final full-width row."""
    btns = []
    for r in rows:
        if r.get("paused"):
            btns.append({"text": f"▶️ Resume {r['n']}", "style": "success",
                         "callback_data": f"{CB_TOGGLE}{r['n']}"})
        else:
            btns.append({"text": f"⏸ Pause {r['n']}", "style": "danger",
                         "callback_data": f"{CB_TOGGLE}{r['n']}"})
    grid = [btns[i:i + 2] for i in range(0, len(btns), 2)]  # pairs; odd->1-wide
    grid.append([{"text": "🔄 Refresh", "style": "primary",
                  "callback_data": CB_REFRESH}])
    return grid


def build_targets_payload(chat_id, rows):
    """Full sendRichMessage payload for the targets charge sheet."""
    header = [
        _cell("N", header=True, align="center"),
        _cell("Target", header=True),
        _cell("DB", header=True),
        _cell("DB2", header=True),
        _cell("Resume", header=True, align="right"),
    ]
    cells = [header]
    for r in rows:
        flag = "⏸ " if r.get("paused") else ""
        cells.append([
            _cell(str(r["n"]), align="center"),
            _cell(flag + (r.get("title") or str(r.get("id", "?"))), r.get("t_link")),
            _cell(r.get("db_title") or "(fallback /adddb)", r.get("db_link")),
            _cell(r.get("db2_title") or "—", r.get("db2_link")),
            _cell(str(r.get("resume", 0)), align="right"),
        ])
    return {
        "chat_id": chat_id,
        "rich_message": {
            "blocks": [
                {"type": "heading", "size": 3, "text": "🎯 TARGETS — live board"},
                {"type": "table", "is_bordered": True, "is_striped": True,
                 "is_compact": True, "cells": cells},
                {"type": "footer",
                 "text": "DB = storage · DB2 = clean mirror · tap a button to "
                         "pause/resume · /targets_text for the plain list"},
            ],
        },
        "reply_markup": {"inline_keyboard": _grid_keyboard(rows)},
    }


# ---------------------------------------------------------------------------
# send / in-place edit
# ---------------------------------------------------------------------------
async def send_targets_board(chat_id, rows, retries=2):
    """Send a fresh targets board and remember its message_id for later edits.
    Returns True on success, False to fall back to the markdown listing."""
    if not BOT_TOKEN or not rows:
        return False
    payload = build_targets_payload(chat_id, rows)
    for attempt in range(retries + 1):
        ok, res = await api_call("sendRichMessage", payload)
        if ok:
            msg_id = res.get("message_id") if isinstance(res, dict) else None
            mark_board(chat_id, msg_id)
            return True
        desc = str(res).lower()
        if any(k in desc for k in ("rich", "unsupported", "can't parse",
                                   "chat not found", "forbidden", "not found",
                                   "bad request")):
            log.warning("rich board unsupported (%s) — markdown fallback", desc)
            return False
        await asyncio.sleep(1.5 * (attempt + 1))
    return False


async def edit_targets_board(chat_id, message_id, rows):
    """Edit an existing board in place (Refresh / toggle). Falls back to a new
    message if the edit fails (message gone, too old, etc)."""
    payload = build_targets_payload(chat_id, rows)
    payload["message_id"] = message_id
    ok, res = await api_call("editMessageText", payload)
    if ok:
        mark_board(chat_id, message_id)
        return True
    log.warning("editMessageText failed (%s) — sending fresh board", res)
    return await send_targets_board(chat_id, rows)


async def refresh_board(chat_id, rows, msg_id=None):
    """Edit the board in place, else send a new one.
    v39.2: prefer the msg_id that rides inside the button callback — the
    in-memory _BOARDS map dies on every Render restart, and without the
    tapped id the board was re-SENT as a new message instead of edited
    (duplicate boards after each restart)."""
    mid = msg_id or ((_BOARDS.get(chat_id) or {}).get("msg_id"))
    if mid:
        return await edit_targets_board(chat_id, mid, rows)
    return await send_targets_board(chat_id, rows)


# ---------------------------------------------------------------------------
# expiry + callback decode
# ---------------------------------------------------------------------------
def board_expired(chat_id, message_id=None, ts=None):
    """True when the tapped board is older than BOARD_TTL. Checks the specific
    message's send time first, then the tracked live board for the chat."""
    if ts is not None:
        return (time.monotonic() - ts) > BOARD_TTL
    if message_id is not None and message_id in _BOARD_TS:
        return (time.monotonic() - _BOARD_TS[message_id]) > BOARD_TTL
    live = _BOARDS.get(chat_id)
    if not live:
        return False                          # unknown board -> don't block
    return (time.monotonic() - live["ts"]) > BOARD_TTL


def mark_board(chat_id, message_id):
    """Record the live board id/timestamp (used for in-place edits + expiry).
    v42: generating a NEW board INVALIDATES the chat's previous board — its
    timestamp is zeroed (NOT deleted: deleting would make board_expired fall
    through to the live-board check and wrongly allow the stale tap), so
    buttons on an older, scrolled-up board get the expired popup and do
    nothing. In-place edits (Refresh/toggle) pass the SAME message id and are
    never invalidated."""
    ts = time.monotonic()
    prev = _BOARDS.get(chat_id)
    if (prev and prev.get("msg_id") is not None
            and message_id is not None and prev["msg_id"] != message_id):
        _BOARD_TS[prev["msg_id"]] = 0.0      # previous board -> instantly expired
        log.info("board invalidated: chat %s old msg %s (new board %s)",
                 chat_id, prev["msg_id"], message_id)
    _BOARDS[chat_id] = {"msg_id": message_id, "ts": ts}
    if message_id is not None:
        _BOARD_TS[message_id] = ts
        if len(_BOARD_TS) > 200:              # cap memory
            for k in list(_BOARD_TS)[:100]:
                _BOARD_TS.pop(k, None)


def parse_callback(data):
    """Decode a board button tap. Returns ('toggle', n) | ('refresh', None) |
    (None, None) if it's not ours."""
    if not data or not data.startswith(CB_PREFIX):
        return None, None
    rest = data[len(CB_PREFIX):]
    if rest == "refresh":
        return "refresh", None
    if rest.startswith("t:") and rest[2:].isdigit():
        return "toggle", int(rest[2:])
    return None, None
