"""v4.9.30 regression suite — BOLD channel captions + documented profile links.

Every Telegram call is mocked; the payloads asserted here are the exact
kwargs python-telegram-bot hands to `_post` (and, in the last test, the exact
JSON body a real Bot object serialises for `sendPhoto`).

Bot API references used:
  * copyMessage.caption_entities — "A JSON-serialized list of special
    entities that appear in the new caption"
    https://core.telegram.org/bots/api#copymessage
  * sendPhoto.caption_entities — "A JSON-serialized list of special entities
    that appear in the caption"
    https://core.telegram.org/bots/api#sendphoto
  * MessageEntity offset/length are UTF-16 code units
    https://core.telegram.org/bots/api#messageentity
  * RichTextUrl {type:"url", text, url} and
    RichTextTextMention {type:"text_mention", text, user}
    https://core.telegram.org/bots/api#richtexturl
    https://core.telegram.org/bots/api#richtexttextmention
"""
import asyncio
import json
import os
import sys

import pytest

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB_NAME", "video_bots_dev_test")
os.environ.setdefault("BOT1_TOKEN", "1:x")
os.environ.setdefault("BOT2_TOKEN", "2:x")
os.environ.setdefault("BOT1_USERNAME", "gatebot_test")
os.environ.setdefault("BOT2_USERNAME", "deliverybot_test")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("DB_CHANNEL_ID", "-100")
os.environ.setdefault("POST_CHANNEL_ID", "-100")
os.environ.setdefault("FORCE_SUB_CHANNEL_ID", "-100")
os.environ.setdefault("RENDER_EXTERNAL_URL", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mongomock_motor
import bot1
import bot1_admin
import caption_text
import db
import profile_link


# ── fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture()
def mock_db():
    db._client = mongomock_motor.AsyncMongoMockClient()
    db._db = db._client[os.environ["MONGO_DB_NAME"]]
    return db


class Recorder:
    """Fake Bot: records the EXACT kwargs of every send call."""

    def __init__(self):
        self.photos = []
        self.copies = []
        self.rich = []
        self.texts = []

    async def send_photo(self, **kw):
        self.photos.append(kw)
        return type("M", (), {"message_id": 900})()

    async def copy_message(self, **kw):
        self.copies.append(kw)
        return type("M", (), {"message_id": 901})()

    async def send_message(self, *a, **kw):
        self.texts.append((a, kw))
        return type("M", (), {"message_id": 902})()

    async def _post(self, method, data=None, **kw):
        if method == "sendRichMessage":
            self.rich.append(data)
        return {}


class Msg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


class Update:
    def __init__(self):
        self.message = Msg()
        self.effective_message = self.message
        self.effective_chat = type("C", (), {"id": 555})()
        self.effective_user = type("U", (), {"id": 1})()


class Ctx:
    def __init__(self, bot, args=None):
        self.bot = bot
        self.args = args or []


ITEM = {"db_message_id": 10, "cover_message_id": 10, "cover_file_id": "FID_COVER",
        "caption": "Anime 🌸 東京", "videos": [], "srts": [], "file_id": "f1"}


# ── 1. UTF-16 measurement ────────────────────────────────────────────────
def test_utf16_length_counts_astral_chars_as_two():
    assert caption_text.utf16_len("abc") == 3
    assert caption_text.utf16_len("🌸") == 2
    assert caption_text.utf16_len("Anime 🌸 東京") == 11          # 2+1+2+1+2
    assert caption_text.utf16_len("") == 0


def test_bold_caption_spans_the_whole_text():
    txt, ents = caption_text.bold_caption("Anime 🌸 東京")
    assert txt == "Anime 🌸 東京"
    assert ents == [{"type": "bold", "offset": 0, "length": 11}]


def test_bold_caption_keeps_stored_html_formatting_as_entities():
    txt, ents = caption_text.bold_caption_html(
        "Note <b>bold</b> &amp; <a href=\"https://t.me/x\">link</a>")
    assert txt == "Note bold & link"                    # tags gone, entity decoded
    assert {"type": "bold", "offset": 0, "length": 16} in ents
    assert {"type": "bold", "offset": 5, "length": 4} in ents
    assert {"type": "text_link", "offset": 12, "length": 4,
            "url": "https://t.me/x"} in ents


# ── 2. posting path: sendPhoto carries a bold caption ────────────────────
@pytest.mark.asyncio
async def test_cover_post_sends_bold_caption_entities_and_keeps_buttons(mock_db):
    bot = Recorder()
    markup = bot1.build_post_markup("https://t.me/gatebot_test?start=file_f1", 7, [])
    await bot1._post_cover(bot, -100, -100, dict(ITEM), markup, styled=False)

    kw = bot.photos[0]
    assert kw["caption"] == "Anime 🌸 東京"
    (ent,) = kw["caption_entities"]
    assert (ent.type, ent.offset, ent.length) == ("bold", 0, 11)
    assert kw["reply_markup"] is markup                     # buttons preserved
    assert kw["has_spoiler"] is True
    assert "parse_mode" not in kw                            # entities, not markup


@pytest.mark.asyncio
async def test_copy_path_also_carries_caption_entities(mock_db):
    bot = Recorder()
    markup = bot1.build_post_markup("https://t.me/x?start=file_f1", 1, [])
    item = dict(ITEM, cover_file_id=None)
    await bot1._post_cover(bot, -100, -100, item, markup, styled=False)

    kw = bot.copies[0]
    assert kw["from_chat_id"] == -100 and kw["message_id"] == ITEM["cover_message_id"]
    assert kw["caption"] == "Anime 🌸 東京"
    assert (kw["caption_entities"][0].type, kw["caption_entities"][0].length) == ("bold", 11)
    assert kw["reply_markup"] is markup


@pytest.mark.asyncio
async def test_overlong_caption_is_truncated_to_the_1024_unit_limit(mock_db):
    bot = Recorder()
    markup = bot1.build_post_markup("https://t.me/x?start=file_f1", 1, [])
    await bot1._post_cover(bot, -100, -100,
                           dict(ITEM, caption="🌸" * 900), markup, styled=False)
    kw = bot.photos[0]
    assert caption_text.utf16_len(kw["caption"]) <= 1024
    (ent,) = kw["caption_entities"]
    assert ent.length == caption_text.utf16_len(kw["caption"])
    assert ent.offset == 0


@pytest.mark.asyncio
async def test_plain_caption_without_html_extra_is_still_bold(mock_db):
    bot = Recorder()
    markup = bot1.build_post_markup("https://t.me/x?start=file_f1", 1, [])
    await bot1._post_cover(bot, -100, -100,
                           dict(ITEM, caption="Plain title"), markup, styled=False)
    kw = bot.photos[0]
    assert kw["caption"] == "Plain title"
    assert (kw["caption_entities"][0].offset, kw["caption_entities"][0].length) == (0, 11)


# ── 3. the REAL serialised request body ──────────────────────────────────
@pytest.mark.asyncio
async def test_real_bot_serialises_caption_entities_exactly():
    """The REAL python-telegram-bot request body for a bold emoji caption."""
    import datetime as _dt
    from telegram import Bot, Chat, Message

    ents = caption_text.to_message_entities(
        caption_text.bold_caption("Anime 🌸 東京")[1])

    # sendPhoto -> returns a Message
    got = {}

    async def fake_photo(*a, **kw):
        got["method"] = kw.get("endpoint")
        got["data"] = dict(kw.get("data") or {})
        # raw JSON exactly like the real HTTP layer would return
        return {"message_id": 5, "date": int(_dt.datetime.now().timestamp()),
                "chat": {"id": -100, "type": "channel"}}

    b = Bot("123456:TESTTOKEN")
    b._do_post = fake_photo
    await b.send_photo(chat_id=-100, photo="FID", caption="Anime 🌸 東京",
                       caption_entities=ents)
    assert got["method"] == "sendPhoto"
    body = [e.to_dict() if hasattr(e, "to_dict") else e
            for e in got["data"]["caption_entities"]]
    assert json.loads(json.dumps(body)) == [
        {"type": "bold", "offset": 0, "length": 11}]      # UTF-16 units
    assert got["data"]["caption"] == "Anime 🌸 東京"
    assert "parse_mode" not in got["data"]

    # copyMessage -> returns a MessageId, same entity numbers
    got2 = {}

    async def fake_copy(*a, **kw):
        got2["method"] = kw.get("endpoint")
        got2["data"] = dict(kw.get("data") or {})
        return {"message_id": 7}

    b2 = Bot("123456:TESTTOKEN")
    b2._do_post = fake_copy
    await b2.copy_message(chat_id=-100, from_chat_id=-100, message_id=5,
                          caption="Anime 🌸 東京", caption_entities=ents)
    assert got2["method"] == "copyMessage"
    assert json.loads(json.dumps(
        [e.to_dict() for e in got2["data"]["caption_entities"]])) == body


# ── 4. /verified_users: documented profile links, no tg:// ───────────────
@pytest.mark.asyncio
async def test_verified_table_links_profiles_with_documented_types(mock_db):
    await db.record_verification(880001, name="Ananya Singh", username="ananya",
                                elapsed=12.0, category="jav", link_type="arolink")
    await db.record_verification(880002, name="No Username", username=None,
                                elapsed=0.0, category="jav", link_type="👑 premium")
    bot = Recorder()
    upd = Update()
    await bot1_admin.cmd_verified_users(upd, Ctx(bot))

    assert bot.rich, "rich table expected"
    payload = bot.rich[0]
    blob = json.dumps(payload)
    assert "tg://user" not in blob
    cells = payload["rich_message"]["blocks"][0]["cells"]
    assert cells[0][1]["text"] == "Name"                     # header intact
    rows = {c[1]["text"]["text"]: c[1]["text"] for c in cells[1:]}
    assert rows["Ananya Singh"] == {"type": "url", "text": "Ananya Singh",
                                    "url": "https://t.me/ananya"}
    no_user = rows["No Username"]
    assert no_user["type"] == "text_mention"
    assert no_user["user"]["id"] == 880002


@pytest.mark.asyncio
async def test_verified_users_html_fallback_links_tme_or_bold(mock_db):
    await db.record_verification(880003, name="A & B 🌸", username="ab_user",
                                elapsed=5.0, category="jav", link_type="arolink")
    await db.record_verification(880004, name="No Name", username=None,
                                elapsed=0.0, category="jav", link_type="arolink")
    bot = Recorder()

    async def boom(method, data=None, **kw):
        raise RuntimeError("rich rejected")

    bot._post = boom
    upd = Update()
    await bot1_admin.cmd_verified_users(upd, Ctx(bot))
    html = "\n".join(t for t, _ in upd.message.replies)
    assert '<a href="https://t.me/ab_user">A &amp; B 🌸</a>' in html
    assert "<b>No Name</b>" in html
    assert "tg://user" not in html


# ── 5. helper contract ───────────────────────────────────────────────────
def test_profile_link_helper():
    assert profile_link.tme_url("ada_x") == "https://t.me/ada_x"
    assert profile_link.tme_url("@ada_x") == "https://t.me/ada_x"
    assert profile_link.tme_url("") is None
    assert profile_link.tme_url("bad name") is None
    assert profile_link.rich_title("Ada", 7, "ada_x")["type"] == "url"
    assert profile_link.rich_title("Ada", 7)["type"] == "text_mention"
    assert profile_link.rich_title("Ada", 7)["user"]["id"] == 7
