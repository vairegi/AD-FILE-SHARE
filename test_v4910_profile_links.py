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
    rich_rows = []
    for cell_ in name_cells:
        rich = cell_.get("text")
        if not isinstance(rich, dict) or rich.get("type") not in ("url", "text_mention"):
            ok_all_linked = False
            continue
        rich_rows.append(rich)
    check("every Name cell is a documented rich link (url/text_mention), never tg://",
          ok_all_linked and not any("tg://" in str(r) for r in rich_rows))
    check("normal row label is profile TITLE 'Ananya Singh' (not @username/id)",
          any(r["text"] == "Ananya Singh" and r["url"] == "https://t.me/ananya"
              for r in rich_rows))
    check("legacy row backfilled from users collection -> title 'Ravi Kumar'",
          any(r["text"] == "Ravi Kumar" for r in rich_rows))

# ── FALLBACK PATH (rich rejected) ─────────────────────────────────────
    bot2_ = FakeBot(rich_fails=True)
    upd2 = FakeUpdate()
    await bot1_admin.cmd_verified_users(upd2, FakeCtx(bot2_, []))
    html_all = "\n".join(t for t, kw in upd2.message.replies)
    check("fallback emits HTML pages", upd2.message.replies)
    check("fallback name 1 is a t.me profile link with the TITLE",
          '<a href="https://t.me/ananya">Ananya Singh</a>' in html_all)
    check("fallback name 2 is a real profile URL (its username was backfilled)",
          '<a href="https://t.me/ravi_k">Ravi Kumar</a>' in html_all
          and "tg://user" not in html_all)
    check("fallback no longer prints a bare user id",
          "880001" not in html_all and "880002" not in html_all)

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
