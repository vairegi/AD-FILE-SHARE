# AD-FILE-SHARE · BOLD CAPTIONS + CLICKABLE PROFILE TITLES (v4.9.30)

Root service only (Bots 1 + 2). `FUN/` (Funbot) is untouched.

## Fix ZIP contents (copy to the REPOSITORY ROOT — never into FUN/)
    bot1.py                        modified  bold channel captions
    bot1_admin.py                  modified  documented profile links
    caption_text.py                NEW       caption -> entities helper
    profile_link.py                NEW       rich/HTML profile title helper
    test_v4911_bold_captions.py    NEW       11 regression tests
    test_all.py                    updated assertions
    test_resilience_admin.py       updated assertions
    test_v4910_profile_links.py    updated assertions
    GUIDE.md                       this file

Commit them together, then redeploy ONLY the Bots 1 + 2 Render service.
No new environment variables, no dependency changes, no DB migration.

## 1. Bold captions on channel posts
Every cover post is now sent with an explicit `caption` plus
`caption_entities`: ONE `bold` entity covering the entire caption.

* Photo path        -> `sendPhoto(caption=..., caption_entities=...)`
* Non-photo path   -> `copyMessage(caption=..., caption_entities=...)`
  (a forwarded message cannot be re-captioned; copyMessage can)
* Offsets/lengths are UTF-16 code units, so emoji (e.g. 🌸) count as 2 —
  `len()` would send a wrong length and Telegram would reject the entity.
* The Download button/inline keyboard is passed through unchanged.
* Formatting already stored by /addcovercaption is preserved: the HTML is
  converted to entities (bold, italic, code, links, quotes) instead of being
  flattened into visible tags.
* Captions longer than 1024 units are trimmed before sending, so a long
  caption can never fail a scheduled post.

Docs: https://core.telegram.org/bots/api#sendphoto (caption_entities)
      https://core.telegram.org/bots/api#copymessage (caption + caption_entities)
      https://core.telegram.org/bots/api#messageentity (offset/length units)

## 2. Clickable profile titles in /verified_users
`tg://user?id=<id>` is NOT a Bot API rich-text link type — Telegram renders it
as dead text. Titles now use only documented types:

* user HAS a username -> RichTextUrl `{type:"url", text, url:"https://t.me/<username>"}`
* user has NO username -> RichTextTextMention `{type:"text_mention", text, user:{id,...}}`
* HTML fallback -> `<a href="https://t.me/<username>">Title</a>`, or `<b>Title</b>`
  when there is no username (an HTML fragment cannot carry a mention)

The same helper is used by /verified_users, /verified_users <id>,
/listpremiumuser and the bot-added-admin table, so no surface still emits
`tg://user?id=`.

Docs: https://core.telegram.org/bots/api#richtexturl
      https://core.telegram.org/bots/api#richtexttextmention
      https://core.telegram.org/bots/api#inputrichblocktable
      https://core.telegram.org/bots/api-changelog (Rich Messages: Bot API 10.1)

## Tests (all run offline, Telegram mocked, MongoDB mocked)
    pip install -r requirements.txt mongomock-motor pytest pytest-asyncio
    python -m pytest -q test_v4911_bold_captions.py     # 11 passed
    MONGO_URI='' python test_v4910_profile_links.py     # 10/10 checks
    MONGO_URI='' python -m unittest test_resilience_admin  # 20 OK
    MONGO_URI='' python test_delivery_hotfix.py         # 15 OK

`test_all.py`, `test_bandwidth.py`, `test_root_index.py`, `test_force_sub.py`,
`test_return_proof.py` and `test_linkguard.py` need a live MongoDB; their
failure set is byte-identical before and after this change (pre-existing
harness issues, no new failures).

## Not tested here
Real Telegram delivery and the Render deployment. Verify in production by
posting one item with /dripnow <category> and running /verified_users.
