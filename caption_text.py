"""v4.9.30 — BOLD CAPTIONS (Bot API `caption_entities`).

Channel posts used to be plain copies/forwards, so their captions rendered
exactly as stored (never bold) and a forwarded caption could not be changed
at all. Both paths now send an explicit caption + `caption_entities`, which
the Bot API documents for `sendPhoto`/`sendVideo` (*"A JSON-serialized list of
special entities that appear in the caption, which can be specified instead of
parse_mode"*) and for `copyMessage` (*"caption_entities — A JSON-serialized
list of special entities that appear in the new caption"*).

Entity offsets/lengths are counted in UTF-16 code units (the same unit
Bot API uses for MessageEntity offset/length), so emoji and any other
astral-plane character count as 2 — never Python `len()`.
"""
from __future__ import annotations

import html as _html
import re

CAPTION_LIMIT = 1024          # Bot API: 0-1024 characters after entity parsing
_PHOTO_CAPTION_LIMIT = 1024


def utf16_len(text) -> int:
    """Length of `text` in UTF-16 code units (what MessageEntity counts)."""
    return len(str(text or "").encode("utf-16-le")) // 2


def _chars(chunks) -> int:
    """Plain character count of what has been emitted so far."""
    return sum(len(c) for c in chunks)


def _clean(txt: str) -> str:
    # Telegram rejects entities on empty text; collapse the newlines the
    # stored extras can produce ("a\n\n\nb" -> "a\nb") so offsets stay honest.
    return re.sub(r"\n{3,}", "\n\n", str(txt or "")).strip()


def bold_entity(text) -> list:
    """ONE bold entity spanning the ENTIRE caption (the requested look)."""
    n = utf16_len(text)
    return [{"type": "bold", "offset": 0, "length": n}] if n else []


def bold_caption(text) -> tuple:
    """(caption_text, entities) — whole caption bold, nothing else changed."""
    txt = _clean(text)
    return txt, bold_entity(txt)


_TAGS = {
    "b": "bold", "strong": "bold", "i": "italic", "em": "italic",
    "u": "underline", "ins": "underline", "s": "strike", "strike": "strike",
    "del": "strike", "code": "code", "pre": "pre",
    "tg-spoiler": "spoiler", "blockquote": "blockquote",
}
_TOKEN = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9-]+)([^>]*)>", re.S)
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


def html_to_entities(markup) -> tuple:
    """Minimal HTML (parse_mode=HTML subset) -> (plain_text, entities).

    Supports <b> <strong> <i> <em> <u> <s> <strike> <del> <code> <pre>
    <tg-spoiler> <blockquote> <a href="..."> and <br>. Unknown tags are
    dropped, HTML entities are decoded, and every offset is UTF-16 based.
    """
    src = str(markup or "")
    out, spans, stack, pos = [], [], [], 0

    for m in _TOKEN.finditer(src):
        out.append(_html.unescape(src[pos:m.start()]))
        pos = m.end()
        closing, tag, attrs = m.group(1) == "/", m.group(2).lower(), m.group(3)
        if tag == "br":
            out.append("\n")
            continue
        if tag == "a":
            if closing:
                if stack and stack[-1][0] == "text_link":
                    kind, start, url = stack.pop()
                    spans.append((kind, start, _chars(out), url))
            else:
                hm = _HREF.search(attrs or "")
                stack.append(("text_link", _chars(out), hm.group(1) if hm else None))
            continue
        kind = _TAGS.get(tag)
        if not kind:
            continue                       # unknown/bare tag -> no formatting
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == kind:
                    _, start, _u = stack.pop(i)
                    spans.append((kind, start, _chars(out), None))
                    break
        else:
            stack.append((kind, _chars(out), None))
    out.append(_html.unescape(src[pos:]))
    for kind, start, _u in stack:          # unclosed tags close at the end
        spans.append((kind, start, _chars(out), None))

    plain = _clean("".join(out))
    # char index -> UTF-16 offset over the FINAL (cleaned) text
    char_to_u16, u16 = [], 0
    for ch in plain:
        char_to_u16.append(u16)
        u16 += utf16_len(ch)
    char_to_u16.append(u16)

    entities = []
    for kind, start, end, url in spans:
        s_i = min(max(start, 0), len(plain))
        e_i = min(max(end, 0), len(plain))
        if e_i <= s_i:
            continue
        ent = {"type": kind, "offset": char_to_u16[s_i],
               "length": char_to_u16[e_i] - char_to_u16[s_i]}
        if kind == "text_link":
            if not url:
                continue
            ent["url"] = url
        entities.append(ent)
    return plain, entities


def bold_caption_html(markup) -> tuple:
    """HTML caption (+ /addcovercaption extra) -> plain caption whose OWN
    formatting is kept as entities and where the WHOLE caption is bold."""
    plain, ents = html_to_entities(markup)
    ents = _merge([dict(e) for e in ents] + bold_entity(plain))
    return plain, ents


def _merge(entities) -> list:
    """Drop duplicates/empty spans and order them the way Telegram expects."""
    seen, out = set(), []
    for e in entities:
        key = (e.get("type"), e.get("offset"), e.get("length"), e.get("url"))
        if key in seen or not e.get("length"):
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda e: (e["offset"], -e["length"]))
    return out


def fits_caption(text) -> bool:
    return utf16_len(text) <= _PHOTO_CAPTION_LIMIT


def truncate(text, limit=_PHOTO_CAPTION_LIMIT) -> str:
    """Cut to `limit` UTF-16 units (Telegram counts units, not chars)."""
    txt = str(text or "")
    if utf16_len(txt) <= limit:
        return txt
    budget, out = limit - 1, []
    for ch in txt:                                   # never split a surrogate pair
        if utf16_len(ch) + budget < 0:
            return "".join(out) + "…"
        budget -= utf16_len(ch)
        if utf16_len("".join(out + [ch])) > limit - 1:
            break
        out.append(ch)
    return "".join(out) + "…"


def to_message_entities(entities) -> list:
    """Raw dicts -> python-telegram-bot MessageEntity objects (typed payload)."""
    from telegram import MessageEntity
    out = []
    for e in entities or []:
        kw = {"type": e.get("type"), "offset": int(e.get("offset") or 0),
              "length": int(e.get("length") or 0)}
        if e.get("url"):
            kw["url"] = e["url"]
        try:
            out.append(MessageEntity(**kw))
        except Exception:                            # unknown type -> drop
            continue
    return out
