"""Rich Message (sendRichMessage / editMessageText rich_message) payload
builders for Bot 3.

Rich cards give us the collage (InputRichBlockCollage) and slideshow
(InputRichBlockSlideshow) previews plus in-card styled buttons
(InputRichBlockButtons, Bot API 10.3). We ALWAYS try the rich payload first
and fall back to a classic HTML text card + inline keyboard when the client/
server rejects it — so users on older clients still get a working bot.
"""
import json
import logging
from typing import List, Dict, Any, Optional

log = logging.getLogger("fun.rich")


def cover_blocks(collection: dict, mode: str = "collage") -> List[dict]:
    """Build the preview block. mode: 'collage' | 'slideshow'. Uses hosted
    URLs (telegraph/graph.org). If url_status != 'ok', the caller should use
    the Telegram-hosted fallback instead."""
    cover = collection.get("cover") or {}
    url = cover.get("url")
    if not url or cover.get("url_status") != "ok":
        return []
    photo = {"type": "photo", "photo": url}
    if mode == "slideshow":
        return [{"type": "slideshow", "items": [photo]}]
    return [{"type": "collage", "items": [photo]}]


def buttons_block(rows: List[List[Dict[str, str]]]) -> dict:
    """In-card buttons. Each button: {"text", "callback_data"|"url",
    "style": primary|success|danger}."""
    return {"type": "buttons",
            "rows": [[{"text": b["text"],
                       "callback_data": b.get("callback_data"),
                       "url": b.get("url"),
                       "style": b.get("style", "primary")}
                      for b in row] for row in rows]}


def rich_card_payload(text: str, blocks: Optional[List[dict]] = None) -> dict:
    """InputRichMessage as explicit blocks: heading + paragraph + media +
    buttons. Caller sends via raw Bot API call (_post 'sendRichMessage')."""
    out = {"blocks": [{"type": "paragraph", "text": text}]}
    if blocks:
        out["blocks"] = blocks + out["blocks"]
    return out


async def send_rich(bot, chat_id: int, payload: dict) -> bool:
    """Raw sendRichMessage (python-telegram-bot may not ship rich helpers).
    Returns True if Telegram accepted it, False otherwise (caller falls back
    to a normal HTML message)."""
    try:
        await bot._post("sendRichMessage",
                        data={"chat_id": chat_id,
                              "rich_message": json.dumps(payload)})
        return True
    except Exception as exc:
        log.info("rich send rejected, falling back: %s", exc)
        return False
