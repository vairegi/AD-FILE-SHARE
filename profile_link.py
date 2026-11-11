"""v4.9.30 — DOCUMENTED profile links for rich-message titles.

`tg://user?id=<id>` is NOT a Bot API rich-text link: Telegram renders it as
dead text in tables/messages. The Bot API 10.1 rich-text types that ARE
documented for this are:

  RichTextUrl         {type:"url", text:RichText, url:String}      (a text with a link)
  RichTextTextMention {type:"text_mention", text:RichText, user:User}
                                                                  (a mention of a
                                                                   Telegram user by
                                                                   their identifier)

So: a username -> RichTextUrl to https://t.me/<username> (a real, tappable
profile URL); no username -> RichTextTextMention (works by user id without a
username). Plain HTML fallbacks use the same https://t.me/ link, or bold text
when the user has no username (an HTML fragment cannot carry a mention).

Docs: https://core.telegram.org/bots/api#richtexturl
      https://core.telegram.org/bots/api#richtexttextmention
      https://core.telegram.org/bots/api#inputrichblocktable
"""
from __future__ import annotations

import re

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


def clean_username(username):
    u = str(username or "").strip().lstrip("@")
    return u if _USERNAME_RE.match(u) else None


def tme_url(username):
    """Public profile URL, or None when the user has no usable username."""
    u = clean_username(username)
    return f"https://t.me/{u}" if u else None


def rich_title(title, user_id, username=None):
    """RichText for a clickable profile title (never tg://user?id)."""
    label = str(title or "").strip() or f"User {user_id}"
    url = tme_url(username)
    if url:
        return {"type": "url", "text": label, "url": url}
    return {"type": "text_mention", "text": label,
            "user": {"id": int(user_id), "is_bot": False, "first_name": label}}


def html_title(title, user_id, username=None, escape=None):
    """HTML fallback for the same title (<a href> or bold without a username)."""
    esc = escape or (lambda s: str(s).replace("&", "&amp;")
                     .replace("<", "&lt;").replace(">", "&gt;"))
    label = str(title or "").strip() or f"User {user_id}"
    url = tme_url(username)
    return f'<a href="{url}">{esc(label)}</a>' if url else f"<b>{esc(label)}</b>"
