"""Offline rich HTML/fallback probes; synthetic URLs, no real network.
The field grammar is verified against the official Bot API, not a fake
block schema copied from the implementation. Run python rich_verify_test.py.
"""
import asyncio
import json
from types import SimpleNamespace as NS
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
import rich_cards


class RecBot:
    def __init__(self, rich_error=None, photo_error=None):
        self.rich_error=rich_error;self.photo_error=photo_error;self.calls=[]
    async def _post(self, endpoint, data=None, **kw):
        self.calls.append((endpoint,data))
        if self.rich_error:raise self.rich_error
        return {"message_id":42}
    async def send_photo(self,**kw):
        self.calls.append(("sendPhoto",kw))
        if self.photo_error:raise self.photo_error
        return NS(message_id=43,photo=[])
    async def edit_message_media(self,**kw):
        self.calls.append(("editMessageMedia",kw))
        if self.photo_error:raise self.photo_error
        return NS(message_id=42,photo=[])
    async def send_message(self,*args,**kw):
        self.calls.append(("sendMessage",kw));return NS(message_id=44)
    async def edit_message_text(self,*args,**kw):
        self.calls.append(("editMessageText",kw));return NS(message_id=42)
    async def delete_message(self,**kw):self.calls.append(("deleteMessage",kw))


KB=InlineKeyboardMarkup([[InlineKeyboardButton("Unlock",callback_data="unlock:1",api_kwargs={"style":"danger"})]])


async def main():
    b=RecBot();await rich_cards.show_cover_card(b,1,"<b>Text</b>",KB,{"cover":{"url":"https://graph.org/file/x.jpg","urls":[]}},"slideshow")
    payload=json.loads(b.calls[0][1]["rich_message"])
    assert set(payload)=={"html"} and "<tg-slideshow>" in payload["html"]
    print("PASS documented InputRichMessage.html slideshow")
    b=RecBot();await rich_cards.show_cover_card(b,1,"Text",KB,{"cover":{"url":"https://graph.org/file/a.jpg","urls":["https://graph.org/file/b.jpg"]}},"collage")
    payload=json.loads(b.calls[0][1]["rich_message"])
    assert "<tg-collage>" in payload["html"] and payload["html"].count("<img ")==2
    print("PASS multi-image collage HTML")
    b=RecBot(rich_error=BadRequest("unsupported"))
    await rich_cards.show_cover_card(b,1,"Text",KB,{"cover":{"url":"https://graph.org/file/x.jpg","urls":[]}},"slideshow",edit_message_id=42)
    assert any(c[0]=="editMessageMedia" for c in b.calls) and all(c[0]!="deleteMessage" for c in b.calls)
    print("PASS explicit rejection -> in-place photo")
    b=RecBot(rich_error=BadRequest("unsupported"),photo_error=BadRequest("invalid image"))
    await rich_cards.show_cover_card(b,1,"Text",KB,{"cover":{"url":"https://graph.org/file/x.jpg","urls":[]}},"slideshow")
    assert any(c[0]=="sendMessage" for c in b.calls)
    print("PASS unavailable image -> explicit text fallback")
    b=RecBot();await rich_cards.show_cover_card(b,1,"Text",KB,{"cover":{}},"collage")
    assert b.calls[0][0]=="sendMessage"
    print("PASS no prepared media -> navigable text")
    b=RecBot();await rich_cards.show_cover_card(b,1,"Text",KB,{"cover":{"url":"https://graph.org/file/x.jpg","urls":[]}},"slideshow")
    markup=b.calls[0][1].get("reply_markup") or {}
    assert markup["inline_keyboard"][0][0]["style"]=="danger"
    assert any("<tg-slideshow>" in json.loads(c[1]["rich_message"])["html"] for c in b.calls)
    print("PASS native colored keyboard retained")
    print("6/6 rich probes passed")


if __name__=="__main__":asyncio.run(main())
