"""Offline probe: does PTB 22.8 serialize the rich payload, and does the
fallback chain pick the right renderer? No network, no real bot."""
import asyncio, json
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import rich_cards


class RecBot:
    def __init__(self, rich_error=None, photo_error=None):
        self.rich_error = rich_error
        self.photo_error = photo_error
        self.calls = []
        self.username = "t"
    async def _post(self, endpoint, data=None, **kw):
        self.calls.append((endpoint, data))
        if self.rich_error:
            raise self.rich_error
        return {}
    async def send_photo(self, **kw):
        self.calls.append(("sendPhoto", kw))
        if self.photo_error:
            raise self.photo_error
    async def send_message(self, *a, **kw):
        self.calls.append(("sendMessage", (a, kw)))
    async def edit_message_text(self, *a, **kw):
        self.calls.append(("editMessageText", (a, kw)))
    async def delete_message(self, **kw):
        self.calls.append(("deleteMessage", kw))


KB = InlineKeyboardMarkup([[InlineKeyboardButton("Unlock", callback_data="u:1")]])


async def main():
    from telegram.error import BadRequest
    ok = True
    # 1. rich slideshow accepted
    b = RecBot()
    await rich_cards.show_cover_card(b, 1, "txt", KB, "https://graph.org/f.jpg", "slideshow")
    ep, data = b.calls[0]
    payload = json.loads(data["rich_message"])
    assert ep == "sendRichMessage" and payload["blocks"][0]["type"] == "slideshow", payload
    assert payload["blocks"][0]["items"][0]["photo"] == "https://graph.org/f.jpg"
    print("PASS rich slideshow payload")
    # 2. collage mode
    b = RecBot()
    await rich_cards.show_cover_card(b, 1, "txt", KB, "https://telegra.ph/f.jpg", "collage")
    payload = json.loads(b.calls[0][1]["rich_message"])
    assert payload["blocks"][0]["type"] == "collage"
    print("PASS collage payload")
    # 3. rich rejected -> photo fallback
    b = RecBot(rich_error=BadRequest("rich not supported"))
    await rich_cards.show_cover_card(b, 1, "txt", KB, "https://graph.org/f.jpg", "slideshow")
    assert any(c[0] == "sendPhoto" for c in b.calls), b.calls
    print("PASS rich->photo fallback")
    # 4. rich + photo fail -> text
    b = RecBot(rich_error=BadRequest("x"), photo_error=BadRequest("x"))
    await rich_cards.show_cover_card(b, 1, "txt", KB, "https://graph.org/f.jpg", "slideshow")
    assert any(c[0] == "sendMessage" for c in b.calls)
    print("PASS photo->text fallback")
    # 5. no cover -> straight text
    b = RecBot()
    await rich_cards.show_cover_card(b, 1, "txt", KB, None, "collage")
    assert b.calls[0][0] == "sendMessage"
    print("PASS no-cover text")
    # 6. buttons block carries style + callback
    b = RecBot()
    await rich_cards.show_cover_card(b, 1, "txt", KB, "https://graph.org/f.jpg", "slideshow")
    btns = json.loads(b.calls[0][1]["rich_message"])["blocks"][-1]
    assert btns["type"] == "buttons" and btns["buttons"][0][0]["callback_data"] == "u:1"
    print("PASS in-card buttons preserved")
    print("ALL rich fallback probes passed")

asyncio.run(main())
