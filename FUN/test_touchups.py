from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from urllib.parse import urlparse, parse_qs
import pytest
import pytest_asyncio
import mongomock_motor
from telegram.error import BadRequest
import db, config, handlers, services, persona, ui
from test_repairs import Bot, update, context, collection

@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(), 'touchups')
    monkeypatch.setattr(config, 'ADMIN_IDS', [7])
    monkeypatch.setattr(config, 'OWNER_ID', 7)

@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [None, True, False])
@pytest.mark.parametrize('count', [1, 3])
async def test_protection_single_and_album(enabled, count):
    if enabled is not None:
        await db.update_settings({'protect_content': enabled})
    b = Bot(); c = collection(mids=tuple(range(10,10+count)))
    sent, failed = await services.deliver_collection(b,7,c,c['files'])
    assert len(sent)==count and not failed
    assert all(kw['protect_content'] is (True if enabled is None else enabled) for _,kw in b.calls)

@pytest.mark.asyncio
async def test_protection_fallback_no_unprotected_retry():
    b=Bot(); b.copy_messages=AsyncMock(side_effect=BadRequest('rejected'))
    c=collection()
    await services.deliver_collection(b,7,c,c['files'])
    assert b.copy_messages.call_args.kwargs['protect_content'] is True
    assert len(b.calls)==2
    assert all(kw['protect_content'] is True for _,kw in b.calls)

@pytest.mark.asyncio
async def test_admin_toggle_status_and_invalid():
    u=update()
    await handlers.cmd_protect(u,context())
    assert 'ON' in u.message.replies[-1][0]
    await handlers.cmd_protect(u,context(args=['off']))
    assert (await db.get_settings())['protect_content'] is False
    await handlers.cmd_protect(u,context(args=['invalid']))
    assert (await db.get_settings())['protect_content'] is False
    await handlers.cmd_protect(u,context(args=['on']))
    assert (await db.get_settings())['protect_content'] is True

@pytest.mark.asyncio
async def test_nonadmin_cannot_disable():
    u=update(uid=99)
    await handlers.cmd_protect(u,context(args=['off']))
    assert 'protect_content' not in await db.get_settings()
    assert not u.message.replies

@pytest.mark.asyncio
async def test_notification_share_uses_inviter_not_friend():
    await db.create_user(8)
    await db.col('referrals').insert_one({'friend_id':7,'inviter_id':8,'qualified':False})
    b=Bot()
    await handlers.maybe_qualify_referral(b,7,'Friend <safe>')
    uid,text,kw=b.sent[-1]
    assert uid==8 and 'Friend &lt;safe&gt;' in text
    button=kw['reply_markup'].to_dict()['inline_keyboard'][0][0]
    params=parse_qs(urlparse(button['url']).query)
    assert params['url'][0].endswith('start=ref_8')
    assert params['text'][0] in persona.SHARE_LINES
    balance=(await db.get_user(8))['tokens']
    await handlers.maybe_qualify_referral(b,7,'Friend')
    assert (await db.get_user(8))['tokens']==balance
    assert len(b.sent)==1

@pytest.mark.asyncio
async def test_all_share_variations_keep_referral_link(monkeypatch):
    for line in persona.SHARE_LINES:
        monkeypatch.setattr(persona.random,'choice',lambda choices:line)
        link,share=await ui.referral_links(Bot(),8936230259)
        p=parse_qs(urlparse(share).query)
        assert p['url']==[link] and p['text']==[line]
        assert link.endswith('start=ref_8936230259')

def test_four_variations_each_preserve_stats(monkeypatch):
    for scene,lines in persona.ALIVE_LINES.items():
        assert len(set(lines))==4
    for idx in range(4):
        monkeypatch.setattr(persona.random,'choice',lambda lines:lines[idx])
        text=persona.home(12,3,4,5)
        assert '12 Tokens' in text and '3 Days' in text and '4 Drops' in text
        assert persona.cute(persona.ALIVE_LINES['home'][idx]) in text
        assert persona.cute(persona.ALIVE_LINES['referral'][idx]) in persona.referral_notify('Friend',5,17)
        assert persona.cute(persona.ALIVE_LINES['delivery'][idx]) in persona.stream_complete('Title',6,2)
        assert persona.cute(persona.ALIVE_LINES['checkin'][idx]) in persona.checkin_done(3,0,12,1,4)
