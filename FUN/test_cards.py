"""v1.4.0 feed renderer contract: Telegraph link previews edited in place,
editMessageMedia fallback with one-time upload + file_id reuse. Fully offline."""
import asyncio
import io

import mongomock_motor
import pytest
import pytest_asyncio
from PIL import Image
from bson import Binary
from types import SimpleNamespace as NS
from telegram.error import BadRequest, TimedOut

import db
import rich_cards

TG_URL = "https://telegra.ph/file/abc123.jpg"


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (16, 12), (90, 120, 150)).save(out, "JPEG")
    return out.getvalue()


@pytest_asyncio.fixture(autouse=True)
async def isolated():
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(), "cards")
    yield


class CardBot:
    """Only the methods the renderer may call; anything else is an AttributeError."""

    def __init__(self, edit_text_error=None, edit_media_error=None,
                 send_error=None, delete_error=None):
        self.calls = []
        self.edit_text_error = edit_text_error
        self.edit_media_error = edit_media_error
        self.send_error = send_error
        self.delete_error = delete_error

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.calls.append(("editMessageText",
                           {"chat_id": chat_id, "message_id": message_id,
                            "text": text, **kw}))
        if self.edit_text_error:
            raise self.edit_text_error
        return NS(message_id=message_id)

    async def edit_message_media(self, chat_id, message_id, media, reply_markup=None):
        self.calls.append(("editMessageMedia",
                           {"chat_id": chat_id, "message_id": message_id,
                            "media": media, "reply_markup": reply_markup}))
        if self.edit_media_error:
            raise self.edit_media_error
        return NS(message_id=message_id, photo=[NS(file_id="FID_1")])

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("sendMessage", {"chat_id": chat_id, "text": text, **kw}))
        if self.send_error:
            raise self.send_error
        return NS(message_id=500)

    async def send_photo(self, chat_id, photo, **kw):
        self.calls.append(("sendPhoto", {"chat_id": chat_id, "photo": photo, **kw}))
        if self.send_error:
            raise self.send_error
        return NS(message_id=501, photo=[NS(file_id="FID_2")])

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))
        if self.delete_error:
            raise self.delete_error


async def test_hosted_cover_edits_text_in_place_with_link_preview():
    bot = CardBot()
    await rich_cards.show_cover_card(bot, 7, "<b>T</b>", None,
                                     {"cover": {"url": TG_URL}}, edit_message_id=42)
    assert [e for e, _ in bot.calls] == ["editMessageText"]
    kw = bot.calls[0][1]
    assert kw["message_id"] == 42 and kw["parse_mode"] == "HTML"
    assert kw["text"].startswith('<a href="' + TG_URL + '">⁣</a>')
    opts = kw["link_preview_options"].to_dict()
    assert opts == {"url": TG_URL, "is_disabled": False,
                    "prefer_large_media": True, "show_above_text": True}


async def test_first_card_sends_message_with_preview():
    bot = CardBot()
    result = await rich_cards.show_cover_card(bot, 7, "t", None, {"cover": {"url": TG_URL}})
    assert bot.calls[0][0] == "sendMessage" and result.message_id == 500


async def test_not_modified_is_ignored_silently():
    bot = CardBot(edit_text_error=BadRequest("Bad Request: message is not modified"))
    result = await rich_cards.show_cover_card(bot, 7, "t", None,
                                              {"cover": {"url": TG_URL}}, edit_message_id=42)
    assert result is None and len(bot.calls) == 1


async def test_current_media_message_with_url_converts_via_media_edit_and_caches_file_id():
    bot = CardBot(edit_text_error=BadRequest("Bad Request: there is no text in the message to edit"))
    key = "-100:10"
    await db.col("cover_assets").insert_one({"_id": key, "data": Binary(jpeg())})
    await rich_cards.show_cover_card(bot, 7, "cap", None,
                                     {"cover": {"url": TG_URL, "asset_keys": [key]}},
                                     edit_message_id=42)
    assert [e for e, _ in bot.calls] == ["editMessageText", "editMessageMedia"]
    media = bot.calls[1][1]["media"]
    assert media.media == TG_URL and media.caption == "cap"  # URL, zero upload
    assert (await db.col("cover_assets").find_one({"_id": key}))["telegram_file_id"] == "FID_1"


async def test_no_host_cached_file_id_media_edit_without_upload():
    key = "-100:10"
    await db.col("cover_assets").insert_one(
        {"_id": key, "data": Binary(jpeg()), "telegram_file_id": "CACHED"})
    bot = CardBot()
    await rich_cards.show_cover_card(bot, 7, "cap", None,
                                     {"cover": {"asset_keys": [key]}}, edit_message_id=42)
    media = bot.calls[0][1]["media"]
    assert media.media == "CACHED"


async def test_no_host_uploads_bytes_once_then_reuses_file_id():
    key = "-100:10"
    await db.col("cover_assets").insert_one({"_id": key, "data": Binary(jpeg())})
    bot = CardBot()
    coll = {"cover": {"asset_keys": [key]}}
    await rich_cards.show_cover_card(bot, 7, "a", None, coll, edit_message_id=42)
    from telegram import InputFile
    first = bot.calls[0][1]["media"].media
    assert isinstance(first, InputFile) and first.attach_uri  # one-time multipart upload
    assert (await db.col("cover_assets").find_one({"_id": key}))["telegram_file_id"] == "FID_1"
    await rich_cards.show_cover_card(bot, 7, "b", None, coll, edit_message_id=42)
    assert bot.calls[1][1]["media"].media == "FID_1"
    assert [e for e, _ in bot.calls] == ["editMessageMedia", "editMessageMedia"]


async def test_media_edit_on_text_message_replaces_card_send_first_then_delete():
    bot = CardBot(edit_media_error=BadRequest("Bad Request: there is no media in the message to edit"))
    key = "-100:10"
    await db.col("cover_assets").insert_one(
        {"_id": key, "data": Binary(jpeg()), "telegram_file_id": "CACHED"})
    result = await rich_cards.show_cover_card(bot, 7, "cap", None,
                                              {"cover": {"asset_keys": [key]}}, edit_message_id=42)
    assert [e for e, _ in bot.calls] == ["editMessageMedia", "sendPhoto", "deleteMessage"]
    assert result.message_id == 501
    assert bot.calls[-1][1]["message_id"] == 42


async def test_replace_send_failure_keeps_old_card_and_raises():
    bot = CardBot(edit_media_error=BadRequest("no media in the message to edit"),
                  send_error=TimedOut())
    key = "-100:10"
    await db.col("cover_assets").insert_one(
        {"_id": key, "data": Binary(jpeg()), "telegram_file_id": "C"})
    with pytest.raises(TimedOut):
        await rich_cards.show_cover_card(bot, 7, "cap", None,
                                         {"cover": {"asset_keys": [key]}}, edit_message_id=42)
    assert [e for e, _ in bot.calls] == ["editMessageMedia", "sendPhoto"]


async def test_rapid_taps_serialized_all_edits_applied_in_order():
    bot = CardBot()
    coll = {"cover": {"url": TG_URL}}
    await asyncio.gather(*[
        rich_cards.show_cover_card(bot, 7, f"t{i}", None, coll, edit_message_id=42)
        for i in range(5)])
    assert [e for e, _ in bot.calls] == ["editMessageText"] * 5
    assert [c[1]["text"].split("\n", 1)[1] for c in bot.calls] == [f"t{i}" for i in range(5)]


async def test_no_cover_edits_text_with_warning_and_disabled_preview():
    bot = CardBot()
    await rich_cards.show_cover_card(bot, 7, "t", None, {"cover": {}}, edit_message_id=42)
    kw = bot.calls[0][1]
    assert kw["link_preview_options"].is_disabled
    assert "Cover preview unavailable" in kw["text"]


async def test_graph_org_url_also_accepted_as_hosted_cover():
    bot = CardBot()
    await rich_cards.show_cover_card(
        bot, 7, "t", None,
        {"cover": {"url": "https://graph.org/file/xyz.jpg"}}, edit_message_id=42)
    assert bot.calls[0][1]["link_preview_options"].url == "https://graph.org/file/xyz.jpg"


async def test_non_host_url_is_not_used_as_preview():
    bot = CardBot()
    await rich_cards.show_cover_card(
        bot, 7, "t", None,
        {"cover": {"url": "https://example.com/legacy-preview/deadbeef"}}, edit_message_id=42)
    # Render-hosted legacy URLs are ignored -> warning text path, no preview
    assert bot.calls[0][1]["link_preview_options"].is_disabled
