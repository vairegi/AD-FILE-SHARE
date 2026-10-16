"""v4.9.10 dedicated test — /verified_users profile-linked TITLES.

Checks:
 1. Rich table: every Name cell is a text_link entity to tg://user?id=<id>,
    label = profile TITLE (stored full name) — never username, never bare id.
 2. Legacy row with NO stored name gets backfilled from the users collection
    (profile title, still linked) instead of rendering a bare numeric ID.
 3. HTML fallback: every name is <a href="tg://user?id=...">TITLE</a>.
 4. Zero-verification path still renders the 📭 message (regression guard).
"""
import os, sys, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB_NAME", "video_bots_dev_test")
os.environ.setdefault("BOT1_TOKEN", "1:x"); os.environ.setdefault("BOT2_TOKEN", "2:x")
os.environ.setdefault("BOT1_USERNAME", "b1"); os.environ.setdefault("BOT2_USERNAME", "b2")
os.environ.setdefault("ADMIN_IDS", "1"); os.environ.setdefault("DB_CHANNEL_ID", "-100")
os.environ.setdefault("POST_CHANNEL_ID", "-100"); os.environ.setdefault("FORCE_SUB_CHANNEL_ID", "-100")
os.environ.setdefault("RENDER_EXTERNAL_URL", "")

import mongomock_motor
import db, bot1_admin

results = []
def check(name, ok):
    results.append((name, bool(ok)))
    print(("PASS" if ok else "FAIL"), "-", name)


class FakeMsg:
    def __init__(self, text="/verified_users"):
        self.text = text
        self.replies = []          # list of (text, kwargs)
    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))
        return None


class FakeChat:
    id = 555


class FakeUser:
    id = 1


class FakeUpdate:
    def __init__(self, text="/verified_users"):
        self.message = FakeMsg(text)
        self.effective_message = self.message
        self.effective_chat = FakeChat()
        self.effective_user = FakeUser()


class FakeBot:
    def __init__(self, rich_fails=False):
        self.rich_fails = rich_fails
        self.rich_payloads = []
    async def _post(self, method, data=None, **kw):
        if method == "sendRichMessage":
            if self.rich_fails:
                raise RuntimeError("rich rejected")
            self.rich_payloads.append(data)
            return {}
        return {}


class FakeCtx:
    def __init__(self, bot, args=None):
        self.bot = bot
        self.args = args or []


async def main():
    db._client = mongomock_motor.AsyncMongoMockClient()
    db._db = db._client[os.environ["MONGO_DB_NAME"]]

    day = db.ist_today()

    # users collection: user 880002 has a profile title but their log row
    # will be a LEGACY row (no name/username stored).
    await db._db.users.insert_one({"user_id": 880002, "name": "Ravi Kumar",
                                   "username": "ravi_k", "first_name": "Ravi"})

    # user 880001: normal row WITH stored profile title + username
    await db.record_verification(880001, name="Ananya Singh", username="ananya",
                                 elapsed=12.0, category="jav", link_type="arolinks.com")
    await db.record_verification(880001, name="Ananya Singh", username="ananya",
                                 elapsed=30.0, category="anime", link_type="arolinks.com")
    # user 880002: LEGACY-style row (no name, no username) — premium delivery
    await db.record_verification(880002, name=None, username=None,
                                 elapsed=0.0, category="jav", link_type="👑 premium")

    # ── RICH PATH ──────────────────────────────────────────────────────────
    bot = FakeBot()
    upd = FakeUpdate()
    await bot1_admin.cmd_verified_users(upd, FakeCtx(bot, []))

    check("rich path sent header + table(s)",
          bot.rich_payloads and upd.message.replies)
    name_cells = []
    for pl in bot.rich_payloads:
        for row in pl["rich_message"]["blocks"][0]["cells"][1:]:
            name_cells.append(row[1])
    check("rich table has exactly 2 user rows", len(name_cells) == 2)

    ok_all_linked = True
    labels = {}
    for cell_ in name_cells:
        text = cell_.get("text", "")
        ents = cell_.get("entities") or []
        ent = ents[0] if ents else {}
        if ent.get("type") != "text_link" or not str(ent.get("url", "")).startswith("tg://user?id="):
            ok_all_linked = False
        uid = str(ent.get("url", "")).rsplit("=", 1)[-1]
        labels[uid] = text
    check("every Name cell is a text_link to tg://user?id=", ok_all_linked)
    check("normal row label is profile TITLE 'Ananya Singh' (not @username/id)",
          labels.get("880001") == "Ananya Singh")
    check("legacy row backfilled from users collection -> title 'Ravi Kumar'",
          labels.get("880002") == "Ravi Kumar")

    # ── FALLBACK PATH (rich rejected) ─────────────────────────────────────
    bot2_ = FakeBot(rich_fails=True)
    upd2 = FakeUpdate()
    await bot1_admin.cmd_verified_users(upd2, FakeCtx(bot2_, []))
    html_all = "\n".join(t for t, kw in upd2.message.replies)
    check("fallback emits HTML pages", upd2.message.replies)
    check("fallback name 1 is an HTML profile link with the TITLE",
          '<a href="tg://user?id=880001">' in html_all and "Ananya Singh" in html_all)
    check("fallback name 2 (legacy) is an HTML profile link with backfilled TITLE",
          '<a href="tg://user?id=880002">' in html_all and "Ravi Kumar" in html_all)
    check("fallback shows no bare '880001' outside its link",
          html_all.count("880001") == 1 and "tg://user?id=880001" in html_all)

    # ── ZERO-VERIFICATION DAY (regression guard) ──────────────────────────
    db2 = mongomock_motor.AsyncMongoMockClient()
    db._db = db2[os.environ["MONGO_DB_NAME"]]
    bot3 = FakeBot()
    upd3 = FakeUpdate()
    await bot1_admin.cmd_verified_users(upd3, FakeCtx(bot3, []))
    check("empty day -> 📭 message, no crash",
          any("No verifications recorded" in t for t, _ in upd3.message.replies))

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


asyncio.run(main())
