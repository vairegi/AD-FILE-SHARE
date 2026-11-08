"""Shortener economy, security, rotation and durable reminder tests.
Opt-in real DB: FUN_EARN_REAL_MONGO=1 (ONLY localhost:27019; never MONGO_URI).
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import mongomock_motor
import pytest
import pytest_asyncio
from telegram.error import Forbidden, BadRequest, TimedOut, RetryAfter

import config
import db
import earn_links as el
import handlers
import main
import native_tables
import persona
import runtime
import token_admin as ta

CLOCK = [1900000000.0]
REAL_SAFE = el.safe_api_base


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    await runtime.stop_jobs()
    if os.environ.get('FUN_EARN_REAL_MONGO') == '1':
        from motor.motor_asyncio import AsyncIOMotorClient
        client=AsyncIOMotorClient('mongodb://127.0.0.1:27019',serverSelectionTimeoutMS=3000)
        await client.admin.command('ping')
    else:
        client=mongomock_motor.AsyncMongoMockClient()
    name='fun_earn_sandbox_'+uuid.uuid4().hex
    db.use_test_client(client,name)
    await db.col('users').create_index('user_id',unique=True)
    await db.col('unlocks').create_index([('user_id',1),('collection_id',1)],unique=True)
    await db.col('referrals').create_index('friend_id',unique=True)
    await ta.ensure_indexes()
    await el.ensure_indexes()
    CLOCK[0]=1900000000.0
    monkeypatch.setattr(db,'now_ts',lambda:CLOCK[0])
    monkeypatch.setattr(config,'OWNER_ID',7)
    monkeypatch.setattr(config,'ADMIN_IDS',[7,8])
    monkeypatch.setattr(config,'BOT_TOKEN','111111111:TESTTOKEN_EARN')
    monkeypatch.setattr(config,'BOT_USERNAME','fun_earn_test_bot')
    monkeypatch.setattr(config,'REQUIRED_CHANNEL_IDS',[])
    monkeypatch.setattr(el,'_notify_lock',asyncio.Lock())
    monkeypatch.setattr(el,'safe_api_base',AsyncMock(return_value=True))
    await db.create_user(55,first_name='Senpai')
    await db.col('users').update_one({'user_id':55},{'$set':{'welcomed':True,'tokens':10}})
    await db.update_settings({'earn_enabled':True,'per_verify_tokens':3,'earn_min_seconds':10})
    yield
    await runtime.stop_jobs()
    await client.drop_database(name)
    client.close()


def bot():
    return NS(username='fun_earn_test_bot',send_message=AsyncMock(return_value=NS(message_id=100)),
              _post=AsyncMock(return_value={'message_id':100}))


def update(args=(),uid=7):
    u=NS(id=uid,first_name='Senpai',username='senpai')
    return NS(effective_user=u,effective_chat=NS(id=uid,type='private'),
        message=NS(chat=NS(id=uid,type='private'),message_id=10,delete=AsyncMock(),reply_text=AsyncMock()))


def context(args=(),b=None):
    return NS(args=list(args),bot=b or bot())


def callback(data='earn:solve',uid=55,b=None):
    b=b or bot()
    q=NS(data=data,from_user=NS(id=uid,first_name='Senpai'),answer=AsyncMock(),
         message=NS(chat=NS(id=uid,type='private'),message_id=22),
         get_bot=lambda:b,edit_message_text=AsyncMock())
    return NS(callback_query=q)


async def provider(site='alpha',status='active'):
    await db.col('earn_providers').update_one({'_id':site},{'$set':{
        'site':site,'status':status,'api_base':'https://api.example.test/api','api_key':'private-test-key'}},upsert=True)


async def issued(monkeypatch,b=None):
    async def short(p,d): return f'https://{p["site"]}.example/safe'
    monkeypatch.setattr(el,'shorten',AsyncMock(side_effect=short))
    text,kb=await el.issue(b or bot(),55)
    u=await db.get_user(55)
    token=(u.get('earn') or {}).get('pending')
    a=await db.col('earn_attempts').find_one({'_id':token}) if token else None
    return a,text,kb


async def solved(monkeypatch,b=None):
    a,_,_=await issued(monkeypatch,b)
    assert a is not None and a['state']=='pending'
    CLOCK[0]+=a['min_seconds']
    text,kb=await el.complete(b or bot(),55,a['_id'])
    return a,text,kb


@pytest.mark.parametrize('uid,chat',[(99,'private'),(7,'group'),(7,'supergroup')])
async def test_admin_commands_private_only(uid,chat):
    u=update(uid=uid);u.message.chat.type=chat
    for fn,args in [(el.cmd_reward,['9']),(el.cmd_timer,['3m']),(el.cmd_switch,['off']),
                    (el.cmd_providers,['add','alpha','https://example.test/api','secret'])]:
        await fn(u,context(args))
    s=await db.get_settings()
    assert s['per_verify_tokens']==3 and s['earn_min_seconds']==10 and s['earn_enabled']
    assert await db.col('earn_providers').count_documents({})==0
    assert not u.message.reply_text.called


@pytest.mark.parametrize('value',['-1','1.5','abc','1000001','999999999999999'])
async def test_reward_invalid_no_write(value):
    u=update();await el.cmd_reward(u,context([value]))
    assert (await db.get_settings())['per_verify_tokens']==3
    assert 'Senpai' in u.message.reply_text.call_args.args[0]


@pytest.mark.parametrize('value,seconds',[('150',150),('150s',150),('3m',180),('1h',3600),('10sec',10)])
async def test_timer_accepts_root_style_units(value,seconds):
    u=update();await el.cmd_timer(u,context([value]))
    assert (await db.get_settings())['earn_min_seconds']==seconds
    assert 'No automatic bans' in u.message.reply_text.call_args.args[0]


@pytest.mark.parametrize('value',['0','9s','2h','-10','abc','3601s'])
async def test_timer_invalid_no_write(value):
    await el.cmd_timer(update(),context([value]))
    assert (await db.get_settings())['earn_min_seconds']==10


async def test_config_disable_zero_and_status():
    u=update()
    await el.cmd_reward(u,context(['0']))
    await el.cmd_switch(u,context(['off']))
    await el.cmd_switch(u,context(['status']))
    assert 'OFF' in u.message.reply_text.call_args.args[0]
    assert 'LinkGuard is not connected' in u.message.reply_text.call_args.args[0]
    assert el.settings_values({})==(False,0,150)


async def test_provider_add_list_pause_resume_remove_no_key_echo():
    u=update();b=bot()
    await el.cmd_providers(u,context(['add','ALPHA','https://example.test/api','SUPER_SECRET_KEY'],b))
    row=await db.col('earn_providers').find_one({'_id':'alpha'})
    assert row['api_key']=='SUPER_SECRET_KEY' and row['status']=='active'
    assert u.message.delete.called
    await el.cmd_providers(u,context([]))
    assert 'SUPER_SECRET_KEY' not in '\n'.join(c.args[0] for c in u.message.reply_text.call_args_list)
    await el.cmd_providers(u,context(['pause','alpha']))
    assert not await el.active_providers()
    await el.cmd_providers(u,context(['resume','alpha']))
    assert len(await el.active_providers())==1
    await el.cmd_providers(u,context(['remove','alpha']))
    assert await db.col('earn_providers').count_documents({})==0


@pytest.mark.parametrize('args',[['add'],['add','bad.site','https://x/api','k'],['pause'],
    ['remove','x','extra'],['add','alpha','https://x/api',''],['unknown']])
async def test_invalid_provider_arguments_cannot_add(args):
    await el.cmd_providers(update(),context(args))
    assert not await el.active_providers()


@pytest.mark.parametrize('url',['http://example.com/api','https://127.0.0.1/api',
 'https://localhost/api','https://user:pass@example.com/api','https://example.com:8443/api',
 'https://example.com/api?api=secret','https://example.com/api#x','https://10.0.0.1/api'])
async def test_api_endpoint_security(url,monkeypatch):
    async def addresses(host,port,**kw):
        if host=='127.0.0.1': ip='127.0.0.1'
        elif host=='10.0.0.1': ip='10.0.0.1'
        else: ip='93.184.216.34'
        return [(2,1,6,'',(ip,443))]
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',addresses)
    assert not await REAL_SAFE(url)


async def test_api_public_https_allowed(monkeypatch):
    async def addresses(*a,**kw):return [(2,1,6,'',('93.184.216.34',443))]
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',addresses)
    assert await REAL_SAFE('https://public.example/api')


@pytest.mark.parametrize('payload',[
 'https://short.example/abc',{'shortenedUrl':'https://short.example/abc'},
 {'shortened_url':'https://short.example/abc'},{'url':'https://short.example/abc'}])
async def test_provider_api_plain_json_encoded_destination(payload,monkeypatch):
    requests=[]
    def handle(r):
        requests.append(r)
        return httpx.Response(200,json=payload) if isinstance(payload,dict) else httpx.Response(200,text=payload)
    el._http=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    destination='https://t.me/fun_earn_test_bot?start=earn_abc'
    result=await el.shorten({'api_base':'https://example.test/api','api_key':'KEY'},destination)
    assert result=='https://short.example/abc'
    assert requests[0].url.params['url']==destination and requests[0].url.params['api']=='KEY'


@pytest.mark.parametrize('payload',['no link','https://localhost/x','javascript:alert(1)',
 {'status':'error'},['https://short.example/x']])
async def test_bad_provider_responses_do_not_create_direct_reward(payload):
    def handle(r):
        return httpx.Response(200,json=payload) if not isinstance(payload,str) else httpx.Response(200,text=payload)
    el._http=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    assert await el.shorten({'api_base':'https://example.test/api','api_key':'KEY'},'https://t.me/test?start=x') is None


async def test_direct_destination_response_rejected():
    dest='https://t.me/test_bot?start=earn_x'
    el._http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=dest)))
    assert await el.shorten({'api_base':'https://example.test/api','api_key':'KEY'},dest) is None


async def test_transient_retry_once_no_credentials_logged(caplog):
    calls=[]
    def handle(r):
        calls.append(r)
        if len(calls)==1:return httpx.Response(503)
        return httpx.Response(200,text='https://short.example/x')
    el._http=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    result=await el.shorten({'api_base':'https://example.test/api','api_key':'HIDE_ME'},'https://t.me/test_bot?start=x')
    assert result and len(calls)==2 and 'HIDE_ME' not in caplog.text


async def test_long_retry_after_fails_over_without_sleep():
    calls=[]
    def handle(r):calls.append(r);return httpx.Response(429,headers={'retry-after':'120'})
    el._http=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    assert await el.shorten({'api_base':'https://example.test/api','api_key':'KEY'},'https://t.me/x') is None
    assert len(calls)==1


async def test_disabled_no_provider_unknown_user_do_not_mint(monkeypatch):
    b=bot()
    text,_=await el.issue(b,999)
    assert '/start' in text
    await db.update_settings({'earn_enabled':False})
    assert 'resting' in (await el.issue(b,55))[0]
    await db.update_settings({'earn_enabled':True})
    assert 'rest' in (await el.issue(b,55))[0]
    assert await db.col('earn_attempts').count_documents({})==0


async def test_pending_link_is_stable_for_duplicate_and_concurrent_taps(monkeypatch):
    await provider()
    a,text,kb=await issued(monkeypatch)
    first=a['_id']
    await asyncio.gather(el.issue(bot(),55),el.issue(bot(),55))
    assert await db.col('earn_attempts').count_documents({})==1
    assert el.shorten.await_count==1
    assert (await db.get_user(55))['earn']['pending']==first
    assert '+3 tokens' in text and 'Senpai' in text
    assert any(b.url for row in kb.inline_keyboard for b in row)


async def test_user_bound_random_return_single_use_and_ledger(monkeypatch):
    await provider()
    a,_,_=await issued(monkeypatch)
    CLOCK[0]+=10
    text,_=await el.complete(bot(),999,a['_id'])
    assert 'match' in text
    text,_=await el.complete(bot(),55,'0'*32)
    assert 'match' in text
    results=await asyncio.gather(*(el.complete(bot(),55,a['_id']) for _ in range(8)))
    assert (await db.get_user(55))['tokens']==13
    assert await db.col('transactions').count_documents({'reason':'shortener_reward'})==1
    assert sum('Tokens landed' in text for text,_ in results)==1
    user=await db.get_user(55)
    assert len(user['earn']['history'])==1
    assert user['earn'].get('applied',{})=={}  # no permanent growing per-solve map


async def test_too_early_preserves_link_no_ban_and_timer_from_issuance(monkeypatch):
    await provider()
    async def slow(p,d):CLOCK[0]+=50;return 'https://short.example/x'
    monkeypatch.setattr(el,'shorten',slow)
    await el.issue(bot(),55)
    a=await db.col('earn_attempts').find_one({})
    assert a['issued_at']==a['created_at']+50
    text,_=await el.complete(bot(),55,a['_id'])
    assert 'patience' in text and 'No ban' in text
    assert (await db.get_user(55))['tokens']==10
    assert (await db.col('earn_attempts').find_one({}))['state']=='pending'
    CLOCK[0]+=10
    assert 'Tokens landed' in (await el.complete(bot(),55,a['_id']))[0]


@pytest.mark.parametrize('token',['','bad','a'*31,'a'*33,'a'*32+'_g','../x'])
async def test_malformed_and_legacy_suffixes_cannot_credit(token):
    text,_=await el.complete(bot(),55,token)
    assert 'match' in text
    assert (await db.get_user(55))['tokens']==10


async def test_expiry_and_no_blind_pending_replacement(monkeypatch):
    await provider()
    a,_,_=await issued(monkeypatch)
    CLOCK[0]=a['expires_at']
    assert 'expired' in (await el.complete(bot(),55,a['_id']))[0]
    b,_,_=await issued(monkeypatch)
    assert b['_id']!=a['_id']
    assert 'expired' in (await el.complete(bot(),55,a['_id']))[0]
    assert (await db.get_user(55))['tokens']==10


async def test_rotation_five_each_and_rolling_boundary(monkeypatch):
    await provider('alpha');await provider('beta');await provider('paused','paused')
    sites=[];times=[]
    for _ in range(10):
        a,_,_=await solved(monkeypatch)
        sites.append(a['site']);times.append(CLOCK[0])
    assert sites==['alpha','beta']*5
    assert (await db.get_user(55))['tokens']==40
    before=await db.col('earn_attempts').count_documents({})
    text,_=await el.issue(bot(),55)
    assert 'five rewards' in text
    assert await db.col('earn_attempts').count_documents({})==before
    CLOCK[0]=times[0]+el.DAY-.01
    assert not await el.eligible(await db.get_user(55),CLOCK[0])
    CLOCK[0]=times[0]+el.DAY
    assert [x['site'] for x in await el.eligible(await db.get_user(55),CLOCK[0])]==['alpha']
    a,_,_=await issued(monkeypatch)
    assert a['site']=='alpha'


async def test_round_robin_failure_and_all_outages_never_credit_or_count(monkeypatch):
    await provider('alpha');await provider('beta')
    async def short(p,d):return None if p['site']=='alpha' else 'https://short.example/x'
    monkeypatch.setattr(el,'shorten',short)
    await el.issue(bot(),55)
    a=await db.col('earn_attempts').find_one({'state':'pending'})
    assert a['site']=='beta'
    CLOCK[0]+=10
    await el.complete(bot(),55,a['_id'])
    assert (await db.get_user(55))['tokens']==13
    monkeypatch.setattr(el,'shorten',AsyncMock(return_value=None))
    text,_=await el.issue(bot(),55)
    assert 'No tokens or daily uses were taken' in text
    assert len((await db.get_user(55))['earn']['history'])==1


async def test_remove_and_readd_same_site_cannot_reset_quota(monkeypatch):
    await provider()
    for _ in range(5): await solved(monkeypatch)
    await db.col('earn_providers').delete_one({'_id':'alpha'})
    await provider()
    assert not await el.eligible(await db.get_user(55),CLOCK[0])


async def test_promised_reward_timer_survive_config_pause_and_restart(monkeypatch):
    await provider()
    a,_,_=await issued(monkeypatch)
    await db.update_settings({'earn_enabled':False,'per_verify_tokens':99,'earn_min_seconds':3600})
    await db.col('earn_providers').delete_one({'_id':'alpha'})
    CLOCK[0]+=10
    text,_=await el.complete(bot(),55,a['_id'])
    assert '+3 tokens' in text and (await db.get_user(55))['tokens']==13


async def test_crash_after_wallet_before_ledger_never_recredits(monkeypatch):
    await provider()
    a,_,_=await issued(monkeypatch);CLOCK[0]+=10
    real=db.col
    broken=NS(update_one=AsyncMock(side_effect=RuntimeError('ledger offline')))
    monkeypatch.setattr(db,'col',lambda n:broken if n=='transactions' else real(n))
    with pytest.raises(RuntimeError): await el.complete(bot(),55,a['_id'])
    user=await db.get_user(55)
    assert user['tokens']==13 and a['_id'] in user['earn']['applied']
    due=user['earn']['remind_at']
    monkeypatch.setattr(db,'col',real)
    CLOCK[0]+=100
    await el.recover_batch()
    assert (await db.get_user(55))['tokens']==13
    assert (await db.get_user(55))['earn']['remind_at']==due
    assert await db.col('transactions').count_documents({'reason':'shortener_reward'})==1
    await el._apply(await db.get_user(55),a)  # even a stale pre-credit snapshot cannot repay
    assert (await db.get_user(55))['tokens']==13


async def test_accepted_before_wallet_failure_survives_expiry(monkeypatch):
    await provider()
    a,_,_=await issued(monkeypatch);CLOCK[0]+=10
    original=el._apply
    monkeypatch.setattr(el,'_apply',AsyncMock(side_effect=RuntimeError('down')))
    with pytest.raises(RuntimeError):await el.complete(bot(),55,a['_id'])
    assert (await db.get_user(55))['tokens']==10
    CLOCK[0]+=el.LINK_TTL+1
    monkeypatch.setattr(el,'_apply',original)
    await el.recover_batch()
    assert (await db.get_user(55))['tokens']==13


async def test_spending_and_gift_concurrent_with_earning_preserve_balance(monkeypatch):
    await provider()
    a,_,_=await issued(monkeypatch);CLOCK[0]+=10
    await asyncio.gather(el.complete(bot(),55,a['_id']),db.try_spend_tokens(55,2,'unlock'),
                         db.add_tokens(55,8,'admin_gift'))
    assert (await db.get_user(55))['tokens']==19


async def test_reminder_latest_completion_24h_and_late_shift(monkeypatch):
    await provider();b=bot()
    a,_,_=await solved(monkeypatch,b)
    first=CLOCK[0];due=first+el.DAY
    assert (await db.get_user(55))['earn']['remind_at']==due
    CLOCK[0]=due-1
    assert await el.reminder_batch(b)==0
    CLOCK[0]=due
    assert await el.reminder_batch(b)==1
    first_message=b.send_message.call_args.args[1]
    assert 'Senpai' in first_message
    assert (await db.get_user(55))['earn']['remind_at']==due+el.DAY
    # Solve 4h late; next reminder is based on the SUCCESS, not the old anchor.
    CLOCK[0]=due+4*3600
    await solved(monkeypatch,b)
    last=CLOCK[0]
    assert (await db.get_user(55))['earn']['remind_at']==last+el.DAY
    CLOCK[0]=due+el.DAY
    assert await el.reminder_batch(b)==0
    CLOCK[0]=last+el.DAY
    assert await el.reminder_batch(b)==1
    assert b.send_message.call_args.args[1]!=first_message  # style survives daily solves


async def test_daily_reminders_until_solve_variants_no_duplicate_or_catchup_burst(monkeypatch):
    await provider();b=bot()
    await solved(monkeypatch,b)
    CLOCK[0]+=el.DAY
    texts=[]
    for _ in range(7):
        assert await el.reminder_batch(b)==1
        texts.append(b.send_message.call_args.args[1])
        assert await el.reminder_batch(b)==0
        CLOCK[0]+=el.DAY
    assert len(set(texts))==7
    CLOCK[0]+=3*el.DAY
    assert await el.reminder_batch(b)==1
    assert await el.reminder_batch(b)==0
    assert (await db.get_user(55))['earn']['remind_at']>CLOCK[0]


@pytest.mark.parametrize('exc,state',[(Forbidden('blocked'),'blocked'),
 (BadRequest('chat not found'),'failed'),(TimedOut('timeout'),'uncertain')])
async def test_reminder_delivery_failures_do_not_touch_wallet_or_spam(monkeypatch,exc,state):
    await provider();b=bot()
    await solved(monkeypatch,b)
    CLOCK[0]+=el.DAY
    b.send_message.side_effect=exc
    await el.reminder_batch(b)
    assert (await db.get_user(55))['tokens']==13
    rec=await db.col('earn_notices').find_one({})
    assert rec['state']==state
    calls=b.send_message.call_count
    assert await el.reminder_batch(b)==0
    assert b.send_message.call_count==calls
    if state in ('blocked','failed'):
        assert 'remind_at' not in (await db.get_user(55))['earn']


async def test_reminder_floodwait_respects_delay_and_original_daily_anchor(monkeypatch):
    await provider();b=bot()
    await solved(monkeypatch,b);CLOCK[0]+=el.DAY
    anchor=CLOCK[0]
    b.send_message.side_effect=RetryAfter(120)
    await el.reminder_batch(b)
    next_at=(await db.get_user(55))['earn']['remind_at']
    assert next_at==anchor+121
    before=b.send_message.call_count
    CLOCK[0]+=120
    await el.reminder_batch(b)
    assert b.send_message.call_count==before
    CLOCK[0]=next_at
    b.send_message.side_effect=None
    assert await el.reminder_batch(b)==1
    assert (await db.get_user(55))['earn']['remind_at']==anchor+el.DAY


async def test_restart_mid_reminder_send_marks_uncertain_not_resent(monkeypatch):
    await provider();b=bot()
    await solved(monkeypatch,b);CLOCK[0]+=el.DAY
    u=await db.get_user(55);earn=u['earn']
    rid=f'55:{earn["remind_generation"]}:0'
    await db.col('earn_notices').insert_one({'_id':rid,'state':'sending',
        'scheduled_at':CLOCK[0],'attempts':1})
    assert await el.reminder_batch(b)==0
    assert not b.send_message.called
    assert (await db.col('earn_notices').find_one({'_id':rid}))['state']=='uncertain'
    assert (await db.get_user(55))['earn']['remind_at']==CLOCK[0]+el.DAY


async def test_no_reminders_before_first_solve_or_when_paused(monkeypatch):
    await provider();b=bot()
    assert await el.reminder_batch(b)==0
    await solved(monkeypatch,b);CLOCK[0]+=el.DAY
    await db.update_settings({'earn_enabled':False})
    assert await el.reminder_batch(b)==0
    await db.update_settings({'earn_enabled':True})
    await db.col('earn_providers').update_one({'_id':'alpha'},{'$set':{'status':'paused'}})
    assert await el.reminder_batch(b)==0
    assert not b.send_message.called


async def test_start_route_return_has_no_welcome_bonus_and_callback_can_start(monkeypatch):
    await provider();b=bot()
    monkeypatch.setattr(el,'shorten',AsyncMock(return_value='https://short.example/x'))
    q=callback(b=b)
    await handlers.on_button(q,context(b=b))
    assert q.callback_query.edit_message_text.called
    a=await db.col('earn_attempts').find_one({'state':'pending'})
    CLOCK[0]+=10
    u=update(uid=55)
    await handlers.cmd_start(u,context(['earn_'+a['_id']],b))
    assert (await db.get_user(55))['tokens']==13
    assert 'Tokens landed' in u.message.reply_text.call_args.args[0]
    await handlers.cmd_start(u,context(['earn_'+a['_id']],b))
    assert (await db.get_user(55))['tokens']==13


async def test_command_registration_earn_menu_and_help(monkeypatch):
    app=main.build_bot()
    names={n for group in app.handlers.values() for h in group for n in getattr(h,'commands',[])}
    assert {'solve','setperverifytoken','shortenerapi','shortener','bantimecontrol','users'}<=names
    u=update();await handlers.cmd_admin(u,context())
    assert '/setperverifytoken' in u.message.reply_text.call_args.args[0]
    q=callback('earn')
    await handlers.on_button(q,context())
    kb=q.callback_query.edit_message_text.call_args.kwargs['reply_markup']
    assert any(b.callback_data=='earn:solve' for row in kb.inline_keyboard for b in row)


async def test_native_directory_counts_unique_unlocks_not_restreams():
    for cid in ('a','b','c'):await db.insert_unlock(55,cid)
    assert not await db.insert_unlock(55,'a')
    await db.create_referral(55,100)
    await db.qualify_referral(100)
    await db.create_referral(55,101)
    rich,kb=await ta.table(0,7)
    table=next(b for b in rich['blocks'] if b['type']=='table')
    assert table['is_bordered'] and table['is_striped'] and table['is_compact']
    assert [c['text'] for c in table['cells'][0]]==['#','Name / User ID','Tokens','Friends','Unlocked Posts']
    row=table['cells'][1]
    assert row[2]['text']=='10' and row[3]['text']=='1\n+1 pending' and row[4]['text']=='3'
    assert row[1]['text'][0]=={'type':'url','text':'Senpai','url':'tg://user?id=55'}
    assert all(c['align'] in ('left','center','right') and c['valign'] in ('top','middle','bottom')
               for row in table['cells'] for c in row)


async def test_native_send_edit_failures_are_not_fake_table_fallback():
    b=bot();rich,kb=await ta.table(0,7)
    await native_tables.send(b,7,rich,kb)
    assert b._post.call_args.args[0]=='sendRichMessage'
    payload=b._post.call_args.args[1]
    assert payload['rich_message']==rich and 'text' not in payload
    b._post.side_effect=BadRequest('message is not modified')
    assert await native_tables.edit(b,7,10,rich,kb) is None
    b._post.side_effect=BadRequest('unsupported rich_message')
    with pytest.raises(BadRequest):await native_tables.edit(b,7,10,rich,kb)


async def test_users_token_alias_and_private_callback_owner():
    b=bot();u=update()
    await ta.users_alias(u,context(['token'],b))
    assert b._post.call_args.args[0]=='sendRichMessage'
    await ta.users_alias(u,context(['tokens','1'],b))
    before=b._post.call_count
    q=callback('ut:7:0',uid=8,b=b)
    await handlers.on_button(q,context(b=b))
    assert b._post.call_count==before
    q=callback('ut:7:0',uid=7,b=b)
    await handlers.on_button(q,context(b=b))
    assert b._post.call_args.args[0]=='editMessageText'


async def test_real_ptb_transport_serializes_native_table_with_no_fake_text():
    from telegram import Bot
    from telegram.request import BaseRequest
    class Recording(BaseRequest):
        def __init__(self):self.calls=[]
        @property
        def read_timeout(self):return 5
        async def initialize(self):pass
        async def shutdown(self):pass
        async def do_request(self,url,method,request_data=None,**kwargs):
            params=request_data.parameters if request_data else {}
            api=url.rsplit('/',1)[-1];self.calls.append((api,params))
            result=({'id':123456,'is_bot':True,'first_name':'Offline','username':'fun_earn_test_bot'}
                    if api=='getMe' else
                    {'message_id':1,'date':0,'chat':{'id':7,'type':'private'},'text':'table'})
            return 200,json.dumps({'ok':True,'result':result}).encode()
    request=Recording()
    rich,kb=await ta.table(0,7)
    async with Bot('123456:OFFLINE_TEST_ONLY',request=request) as b:
        await native_tables.send(b,7,rich,kb)
        await native_tables.edit(b,7,1,rich,kb)
    calls=[(api,p) for api,p in request.calls if api!='getMe']
    assert [api for api,p in calls]==['sendRichMessage','editMessageText']
    for _,p in calls:
        assert 'rich_message' in p and 'parse_mode' not in p and 'text' not in p
        raw=p['rich_message']
        if isinstance(raw,str):raw=json.loads(raw)
        assert next(x for x in raw['blocks'] if x['type']=='table')['cells'][1][4]['text']=='0'


async def test_provider_paused_while_api_runs_is_not_given_to_user(monkeypatch):
    await provider('alpha');await provider('beta')
    async def short(p,d):
        if p['site']=='alpha':
            await db.col('earn_providers').update_one({'_id':'alpha'},{'$set':{'status':'paused'}})
        return f'https://{p["site"]}.example/x'
    monkeypatch.setattr(el,'shorten',short)
    await el.issue(bot(),55)
    attempt=await db.col('earn_attempts').find_one({'state':'pending'})
    assert attempt['site']=='beta'
    assert (await db.get_user(55))['tokens']==10


async def test_quotas_and_rotation_are_per_user_not_global(monkeypatch):
    await provider('alpha');await provider('beta')
    await solved(monkeypatch)
    await db.create_user(66,first_name='Other Senpai')
    await db.col('users').update_one({'user_id':66},{'$set':{'welcomed':True}})
    await el.issue(bot(),66)
    u=await db.get_user(66)
    a=await db.col('earn_attempts').find_one({'_id':u['earn']['pending']})
    assert a['site']=='alpha'
    assert (await db.get_user(55))['earn']['last_site']=='alpha'


async def test_native_table_unsupported_endpoint_explained_without_pre_fallback():
    b=bot();b._post.side_effect=BadRequest('unknown method sendRichMessage')
    u=update()
    await ta.users_command(u,context(b=b))
    notice=u.message.reply_text.call_args.args[0]
    assert 'Native rich tables' in notice and '<pre>' not in notice
    assert b._post.call_args.args[0]=='sendRichMessage'


async def test_native_table_missing_message_recreates_rich_message_only():
    b=bot();b._post.side_effect=[BadRequest('message to edit not found'),{'message_id':9}]
    rich,kb=await ta.table(0,7)
    await native_tables.edit(b,7,8,rich,kb)
    assert [c.args[0] for c in b._post.call_args_list]==['editMessageText','sendRichMessage']


async def test_reward_success_reply_failure_cannot_double_pay(monkeypatch):
    await provider()
    a,_,_=await issued(monkeypatch);CLOCK[0]+=10
    u=update(uid=55);u.message.reply_text.side_effect=TimedOut('send uncertain')
    with pytest.raises(TimedOut):
        await el.complete_update(u,context(),a['_id'])
    assert (await db.get_user(55))['tokens']==13
    await el.complete(bot(),55,a['_id'])
    assert (await db.get_user(55))['tokens']==13
    assert await db.col('transactions').count_documents({'reason':'shortener_reward'})==1


async def test_reminder_and_completion_race_preserves_new_due(monkeypatch):
    await provider();b=bot()
    await solved(monkeypatch,b);CLOCK[0]+=el.DAY
    a,_,_=await issued(monkeypatch);CLOCK[0]+=10
    now=CLOCK[0]
    await asyncio.gather(el.reminder_batch(b),el.complete(b,55,a['_id']))
    assert (await db.get_user(55))['earn']['remind_at']==now+el.DAY
    assert (await db.get_user(55))['tokens']==16


async def test_ist_timestamp_and_all_persona_variants():
    at=datetime(2026,11,8,4,30,tzinfo=timezone.utc).timestamp()
    assert '10:00 AM IST' in el.stamp(at)
    assert len({persona.earn_reminder(i,3) for i in range(7)})==7
    for kind in ('disabled','invalid','expired','early','retry','limit','unavailable','already','start','building'):
        assert 'Senpai' in persona.earn_notice(kind,seconds=5)
