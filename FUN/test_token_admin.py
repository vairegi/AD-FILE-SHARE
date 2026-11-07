"""Gift + token-directory tests. Opt-in real LOCAL MongoDB only:
FUN_GIFT_REAL_MONGO=1 python -m pytest -q test_token_admin.py
Never reads production URI; database is uniquely named and deleted afterwards.
"""
import asyncio
import html
import os
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from telegram.error import Forbidden, BadRequest, TimedOut, RetryAfter
import mongomock_motor
import config
import db
import handlers
import main
import persona
import runtime
import token_admin as ta


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    await runtime.stop_jobs()
    if os.environ.get('FUN_GIFT_REAL_MONGO') == '1':
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient('mongodb://127.0.0.1:27019', serverSelectionTimeoutMS=3000)
        await client.admin.command('ping')
    else:
        client = mongomock_motor.AsyncMongoMockClient()
    name = 'fun_gift_sandbox_' + uuid.uuid4().hex
    db.use_test_client(client, name)
    await db.col('users').create_index('user_id', unique=True)
    await db.col('referrals').create_index('friend_id', unique=True)
    await ta.ensure_indexes()
    monkeypatch.setattr(config, 'OWNER_ID', 7)
    monkeypatch.setattr(config, 'ADMIN_IDS', [7, 8])
    monkeypatch.setattr(config, 'BOT_TOKEN', '111111111:TESTTOKEN_FUN_GIFT')
    monkeypatch.setattr(config, 'REQUIRED_CHANNEL_IDS', [])
    monkeypatch.setattr(ta, '_worker_lock', asyncio.Lock())
    monkeypatch.setattr(ta, 'SEND_GAP', 0)
    yield
    await runtime.stop_jobs()
    await client.drop_database(name)
    client.close()


def bot():
    return NS(send_message=AsyncMock(return_value=NS(message_id=91)))


def update(uid=7, mid=10, kind='private'):
    u = NS(id=uid, first_name='Senpai', username='senpai')
    msg = NS(chat=NS(id=uid, type=kind), message_id=mid,
             reply_text=AsyncMock(return_value=NS(message_id=mid + 1)))
    return NS(effective_user=u, message=msg)


def context(args=(), client=None):
    return NS(args=list(args), bot=client or bot())


def callback(data, uid=7, kind='private'):
    q = NS(data=data, from_user=NS(id=uid),
           message=NS(chat=NS(id=uid, type=kind), message_id=19),
           answer=AsyncMock(), edit_message_text=AsyncMock())
    return NS(callback_query=q)


async def user(uid, tokens=10, welcomed=True, name='User'):
    await db.create_user(uid, first_name=name)
    await db.col('users').update_one({'user_id': uid}, {'$set': {
        'tokens': tokens, 'welcomed': welcomed}})


async def job(args=('55', '5'), mid=10, client=None):
    u = update(mid=mid)
    ctx = context(args, client)
    await ta.command(u, ctx)
    j = await db.col('gift_jobs').find_one({'_id': f'g7_{mid}'})
    return j, u, ctx


async def confirm(j, client, uid=7, action='confirm'):
    u = callback(f'gift:{action}:{j["_id"]}', uid)
    await handlers.on_button(u, context(client=client))
    return u


@pytest.mark.parametrize('args', [[], ['55'], ['55','0'], ['55','-1'], ['55','1.5'],
    ['55','abc'], ['-55','5'], ['0','5'], ['@user','5'], ['all','1000000001'],
    ['55','5','extra'], ['55','9'*40], ['55','1e3']])
async def test_bad_arguments_do_not_mutate(args):
    await user(55)
    j, u, ctx = await job(args)
    assert j is None
    assert (await db.get_user(55))['tokens'] == 10
    assert await db.col('transactions').count_documents({}) == 0
    assert u.message.reply_text.called


@pytest.mark.parametrize('uid,kind', [(99,'private'), (7,'group'), (7,'supergroup')])
async def test_private_admin_only_commands(uid, kind):
    u = update(uid=uid, kind=kind)
    await ta.command(u, context(['55','5']))
    await ta.users_command(u, context())
    assert await db.col('gift_jobs').count_documents({}) == 0
    assert not u.message.reply_text.called


async def test_owner_allowed_even_if_admin_list_does_not_include_owner(monkeypatch):
    monkeypatch.setattr(config,'ADMIN_IDS',[])
    await user(55)
    j, _, _ = await job()
    assert j['status'] == 'queued'


@pytest.mark.parametrize('existing', [False, True])
async def test_unknown_or_not_started_user_is_not_created_or_paid(existing):
    if existing:
        await user(55, welcomed=False)
    j, u, _ = await job()
    assert j is None and '/start' in u.message.reply_text.call_args.args[0]
    assert await db.col('transactions').count_documents({}) == 0
    assert (await db.get_user(55))['tokens'] == 10 if existing else await db.get_user(55) is None


async def test_single_gift_credit_ledger_alert_and_buttons():
    await user(55)
    b = bot()
    j, _, _ = await job(client=b)
    assert await ta.run_once(b)
    assert (await db.get_user(55))['tokens'] == 15
    ledger = await db.col('transactions').find_one({'reason':'admin_gift'})
    assert ledger['amount']==5 and ledger['meta']['admin_id']==7
    notice = [c for c in b.send_message.call_args_list if c.args[0] == 55]
    assert len(notice)==1 and '+5 tokens' in notice[0].args[1]
    assert '15 Tokens' in notice[0].args[1]
    buttons = [x.callback_data for row in notice[0].kwargs['reply_markup'].inline_keyboard for x in row]
    assert buttons==['feed:all:0','profile','earn']
    assert all('style' in x.to_dict() for row in notice[0].kwargs['reply_markup'].inline_keyboard for x in row)
    assert (await db.col('gift_jobs').find_one({'_id':j['_id']}))['status']=='finished'


async def test_same_update_duplicate_and_replay_never_double_credit():
    await user(55)
    b=bot()
    j, _, _=await job(client=b)
    await ta.command(update(), context(['55','5'],b))
    await asyncio.gather(ta.run_once(b),ta.run_once(b))
    await ta.credit(j,55)
    assert (await db.get_user(55))['tokens']==15
    assert await db.col('transactions').count_documents({'reason':'admin_gift'})==1
    assert await db.col('gift_receipts').count_documents({})==1
    assert len([c for c in b.send_message.call_args_list if c.args[0]==55])==1


async def test_distinct_gifts_stack_without_affecting_welcome_or_referrals():
    await user(55)
    b=bot()
    await job(mid=10,client=b)
    await job(mid=11,client=b)
    await ta.run_once(b)
    await ta.run_once(b)
    assert (await db.get_user(55))['tokens']==20
    assert await db.col('transactions').count_documents({'reason':'admin_gift'})==2
    assert await db.col('referrals').count_documents({})==0


async def test_all_preview_cancel_and_duplicate_confirm():
    for uid in (10,20,30): await user(uid)
    b=bot()
    j,u,_=await job(('all','8'),client=b)
    assert j['total']==3 and j['status']=='preview'
    assert not await ta.run_once(b)
    assert '24 tokens' in u.message.reply_text.call_args.args[0].replace('<b>','').replace('</b>','')
    await confirm(j,b,action='cancel')
    await confirm(j,b)
    assert not await ta.run_once(b)
    assert (await db.get_user(10))['tokens']==10


async def test_all_only_started_at_preview_excludes_newcomers(monkeypatch):
    for uid in (10,20,30): await user(uid)
    await user(22,welcomed=False)
    b=bot()
    j,_,_=await job(('all','8'),client=b)
    await user(15)  # lower ID but registered after cutoff
    await user(50)
    await db.col('users').update_one({'user_id':22},{'$set':{
        'welcomed':True,'started_at':db.now_ts()+1}})
    await confirm(j,b)
    await confirm(j,b)
    await ta.run_once(b)
    for uid in (10,20,30): assert (await db.get_user(uid))['tokens']==18
    for uid in (15,22,50): assert (await db.get_user(uid))['tokens']==10
    assert await db.col('transactions').count_documents({'reason':'admin_gift'})==3


async def test_other_admin_or_non_admin_cannot_confirm_someones_all_gift():
    await user(55)
    b=bot()
    j,_,_=await job(('all','8'),client=b)
    await confirm(j,b,uid=8)
    await confirm(j,b,uid=99)
    q=callback(f'gift:confirm:{j["_id"]}',7,'group')
    await handlers.on_button(q,context(client=b))
    assert (await db.col('gift_jobs').find_one({'_id':j['_id']}))['status']=='preview'
    assert (await db.get_user(55))['tokens']==10


async def test_expired_preview_cannot_credit():
    await user(55)
    b=bot()
    j,_,_=await job(('all','8'),client=b)
    await db.col('gift_jobs').update_one({'_id':j['_id']},{'$set':{'created_at':0}})
    await confirm(j,b)
    assert not await ta.run_once(b)
    assert (await db.get_user(55))['tokens']==10


async def test_empty_audience_and_maximum_amount():
    j,u,_=await job(('all','8'))
    assert j is None
    await user(55)
    b=bot()
    await job(('55',str(ta.MAX_AMOUNT)),client=b)
    await ta.run_once(b)
    assert (await db.get_user(55))['tokens']==10+ta.MAX_AMOUNT


@pytest.mark.parametrize('exc,expected', [(Forbidden('blocked'),'blocked'),
    (BadRequest('chat not found'),'failed'), (TimedOut('timeout'),'uncertain')])
async def test_notification_failure_does_not_undo_credit_or_repeat(exc,expected):
    await user(55)
    b=bot()
    b.send_message.side_effect=exc
    j,_,_=await job(client=b)
    await ta.run_once(b)
    assert (await db.get_user(55))['tokens']==15
    r=await db.col('gift_receipts').find_one({'job_id':j['_id']})
    assert r['notify']==expected
    before=b.send_message.call_count
    await ta.process_job(b,j)  # stale checkpoint/restart
    assert (await db.get_user(55))['tokens']==15
    calls=[c for c in b.send_message.call_args_list if c.args[0]==55]
    assert len(calls)==1


async def test_retry_after_defers_without_repaying_and_continues_after_wait():
    await user(55)
    b=bot()
    attempts=0
    async def send(uid,*args,**kw):
        nonlocal attempts
        if uid==55:
            attempts+=1
            if attempts==1: raise RetryAfter(2)
        return NS(message_id=1)
    b.send_message.side_effect=send
    j,_,_=await job(client=b)
    await ta.run_once(b)
    saved=await db.col('gift_jobs').find_one({'_id':j['_id']})
    assert saved['status']=='queued' and saved['ready_at']>db.now_ts()
    assert not await ta.run_once(b)
    assert (await db.get_user(55))['tokens']==15
    await db.col('gift_jobs').update_one({'_id':j['_id']},{'$set':{'ready_at':0}})
    await ta.run_once(b)
    assert (await db.get_user(55))['tokens']==15 and attempts==2
    assert await db.col('transactions').count_documents({'reason':'admin_gift'})==1


async def test_retry_after_exhaustion_becomes_failed_not_infinite():
    await user(55)
    b=bot()
    async def send(uid,*args,**kw):
        if uid==55: raise RetryAfter(1)
    b.send_message.side_effect=send
    j,_,_=await job(client=b)
    for _ in range(5):
        await ta.run_once(b)
        await db.col('gift_jobs').update_one({'_id':j['_id']},{'$set':{'ready_at':0}})
    r=await db.col('gift_receipts').find_one({'job_id':j['_id']})
    assert r['attempts']==5 and r['notify']=='failed'
    assert (await db.get_user(55))['tokens']==15


async def test_crash_after_wallet_before_ledger_reconciles(monkeypatch):
    await user(55)
    b=bot()
    j,_,_=await job(client=b)
    real=db.col
    broken=NS(update_one=AsyncMock(side_effect=RuntimeError('simulated ledger outage')))
    monkeypatch.setattr(db,'col',lambda name:broken if name=='transactions' else real(name))
    with pytest.raises(RuntimeError): await ta.credit(j,55)
    assert (await db.get_user(55))['tokens']==15
    monkeypatch.setattr(db,'col',real)
    await ta.run_once(b)
    assert (await db.get_user(55))['tokens']==15
    assert await db.col('transactions').count_documents({'reason':'admin_gift'})==1
    assert (await db.col('gift_receipts').find_one({'job_id':j['_id']}))['notify']=='sent'


async def test_db_outage_does_not_finish_job_and_next_attempt_resumes(monkeypatch):
    await user(55)
    b=bot()
    j,_,_=await job(client=b)
    original=ta.credit
    monkeypatch.setattr(ta,'credit',AsyncMock(side_effect=RuntimeError('db unavailable')))
    await ta.run_once(b)
    saved=await db.col('gift_jobs').find_one({'_id':j['_id']})
    assert saved['status']=='queued' and saved['last_error_type']=='RuntimeError'
    assert (await db.get_user(55))['tokens']==10
    monkeypatch.setattr(ta,'credit',original)
    await db.col('gift_jobs').update_one({'_id':j['_id']},{'$set':{'ready_at':0}})
    await ta.run_once(b)
    assert (await db.get_user(55))['tokens']==15


async def test_restart_with_pending_alert_delivers_once():
    await user(55)
    b=bot()
    j,_,_=await job(client=b)
    await ta.credit(j,55)  # credit saved, service stopped before send
    await db.col('gift_jobs').update_one({'_id':j['_id']},{'$set':{'status':'running'}})
    await ta.run_once(b)
    assert (await db.get_user(55))['tokens']==15
    assert len([c for c in b.send_message.call_args_list if c.args[0]==55])==1


async def test_restart_mid_send_marks_uncertain_and_does_not_resend():
    await user(55)
    b=bot()
    j,_,_=await job(client=b)
    r=await ta.credit(j,55)
    await db.col('gift_receipts').update_one({'_id':r['_id']},{'$set':{'notify':'sending'}})
    await db.col('gift_jobs').update_one({'_id':j['_id']},{'$set':{'status':'running'}})
    await ta.run_once(b)
    assert len([c for c in b.send_message.call_args_list if c.args[0]==55])==0
    assert (await db.col('gift_receipts').find_one({'_id':r['_id']}))['notify']=='uncertain'
    assert (await db.get_user(55))['tokens']==15


async def test_deleted_user_or_balance_limit_skipped_no_phantom():
    await user(55)
    b=bot()
    j,_,_=await job(client=b)
    await db.col('users').delete_one({'user_id':55})
    r=await ta.credit(j,55)
    assert r['notify']=='skipped' and not r['credited']
    assert await db.get_user(55) is None
    await user(66,ta.MAX_BALANCE)
    r=await ta.credit(j,66)
    assert not r['credited'] and (await db.get_user(66))['tokens']==ta.MAX_BALANCE


async def test_batched_all_restart_checkpoint_and_second_job(monkeypatch):
    monkeypatch.setattr(ta,'BATCH_SIZE',2)
    b=bot()
    for uid in range(11,18): await user(uid)
    j,_,_=await job(('all','8'),client=b)
    await confirm(j,b)
    # Reconstruct a restart after first recipient has been credited+notified.
    r=await ta.credit(j,11)
    await ta.notify(b,j,r)
    await db.col('gift_jobs').update_one({'_id':j['_id']},
        {'$set':{'status':'running','after_uid':11}})
    await ta.run_once(b)
    assert await db.col('transactions').count_documents({'reason':'admin_gift'})==7
    for uid in range(11,18): assert (await db.get_user(uid))['tokens']==18
    assert len([c for c in b.send_message.call_args_list if c.args[0] in range(11,18)])==7


async def test_token_table_sort_pagination_referrals_names_and_links():
    for uid in range(1,24): await user(uid,tokens=uid,name=f'User {uid}')
    await user(99,tokens=9999,welcomed=False)
    await db.create_referral(23,101)
    await db.qualify_referral(101)
    await db.create_referral(23,102)
    text,kb=await ta.table(0,7)
    assert '23 started users' in text and 'Page 1/3' in text
    assert text.index('User 23')<text.index('User 22')
    assert 'User 14' in text and 'User 13' not in text
    assert '1 pending' in text and 'tg://user?id=23' in text
    assert '<pre>' in text and '<b>' in text
    assert 'qualified referrals' in text
    assert not any('Previous' in b.text for row in kb.inline_keyboard for b in row)
    assert any(b.callback_data=='ut:7:1' for row in kb.inline_keyboard for b in row)
    last,last_kb=await ta.table(2,7)
    assert 'Page 3/3' in last and 'User 1' in last
    assert not any('Next' in b.text for row in last_kb.inline_keyboard for b in row)
    for row in kb.inline_keyboard:
        assert all(len(b.callback_data.encode())<=64 for b in row)


async def test_table_empty_and_out_of_range_page():
    text,kb=await ta.table(99,7)
    assert 'No users yet' in text and 'Page 1/1' in text
    await user(55)
    text,_=await ta.table(99999,7)
    assert 'Page 1/1' in text


async def test_table_html_escape_long_unicode_no_html_injection():
    name='<b>&"😀界\n' * 30
    for uid in range(1,11): await user(uid,name=name)
    text,kb=await ta.table(0,7)
    assert '&lt;b&gt;' in text
    assert len(text)<4096
    assert '<b>&' not in text
    assert '\n' not in ta.short_name({'first_name':name,'user_id':1})
    assert len(ta.short_name({'first_name':name,'user_id':1}))<=12


@pytest.mark.parametrize('args', [['0'],['-1'],['a'],['1','2']])
async def test_token_command_invalid_pages(args):
    u=update()
    await ta.users_command(u,context(args))
    assert 'Usage' in u.message.reply_text.call_args.args[0]


async def test_directory_command_callback_owner_refresh_and_live_balances():
    await user(55)
    u=update()
    await ta.users_command(u,context())
    assert '<pre>' in u.message.reply_text.call_args.args[0]
    await db.col('users').update_one({'user_id':55},{'$inc':{'tokens':8}})
    q=callback('ut:7:0')
    await handlers.on_button(q,context())
    assert '18' in q.callback_query.edit_message_text.call_args.args[0]
    for uid in (8,99):
        forbidden=callback('ut:7:0',uid)
        await handlers.on_button(forbidden,context())
        assert not forbidden.callback_query.edit_message_text.called


@pytest.mark.parametrize('data', ['gift:evil:x','gift:confirm','ut:x:y','ut:7:-1','ut:7:999999999999'])
async def test_invalid_callbacks_no_mutations(data):
    q=callback(data)
    await handlers.on_button(q,context())
    assert await db.col('gift_jobs').count_documents({})==0
    assert not q.callback_query.edit_message_text.called


async def test_status_preserves_correct_wallet_vs_alert_counts():
    await user(55)
    b=bot()
    b.send_message.side_effect=Forbidden('blocked')
    j,_,_=await job(client=b)
    await ta.run_once(b)
    u=update(mid=11)
    await ta.command(u,context(['status']))
    text=u.message.reply_text.call_args.args[0]
    assert 'Wallets credited: 1' in text and 'Alerts sent: 0' in text and 'Blocked: 1' in text


async def test_command_registration_and_help():
    app=main.build_bot()
    commands={name for group in app.handlers.values() for handler in group
              for name in getattr(handler,'commands',[])}
    assert {'gift','userstoken'}<=commands
    u=update()
    await handlers.cmd_admin(u,context())
    text=u.message.reply_text.call_args.args[0]
    assert '/gift' in text and '/userstoken' in text


async def test_gift_alert_buttons_route_to_existing_screens(monkeypatch):
    called=[]
    async def record(*a,**kw): called.append(True)
    monkeypatch.setattr(handlers,'_show_profile',record)
    monkeypatch.setattr(handlers,'_show_earn',record)
    monkeypatch.setattr(handlers,'_show_feed',record)
    for data in ('profile','earn','feed:all:0'):
        await handlers.on_button(callback(data,55),context())
    assert len(called)==3
    assert '+8 tokens' in persona.gift_notify(8,28) and '28 Tokens' in persona.gift_notify(8,28)


async def test_gift_worker_hook_starts_and_stops_cleanly(monkeypatch):
    entered=asyncio.Event()
    async def fake_loop(bot):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(ta,'loop',fake_loop)
    runtime.start_jobs(bot())
    await asyncio.wait_for(entered.wait(),2)
    await runtime.stop_jobs()
    assert not runtime._tasks


async def test_idle_worker_wakes_immediately_for_new_gift():
    await user(55)
    b = bot()
    task = asyncio.create_task(ta.loop(b))
    try:
        for _ in range(20):
            if ta._wake is not None:
                break
            await asyncio.sleep(.01)
        await job(client=b)
        async def wait_for_credit():
            while (await db.get_user(55))['tokens'] != 15:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait_for_credit(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert ta._wake is None


async def test_concurrent_spend_and_gift_do_not_lose_balance_updates():
    await user(55, 20)
    b = bot()
    j, _, _ = await job(client=b)
    gift, spent = await asyncio.gather(ta.credit(j, 55),
                                     db.try_spend_tokens(55, 7, 'unlock'))
    assert gift['credited'] and spent
    assert (await db.get_user(55))['tokens'] == 18
    await ta.credit(j, 55)
    assert (await db.get_user(55))['tokens'] == 18


async def test_real_ptb_serializes_gift_and_table_offline():
    import json
    from telegram import Bot
    from telegram.request import BaseRequest
    class Recording(BaseRequest):
        def __init__(self): self.calls = []
        @property
        def read_timeout(self): return 5
        async def initialize(self): pass
        async def shutdown(self): pass
        async def do_request(self, url, method, request_data=None, **kwargs):
            params = request_data.parameters if request_data else {}
            api = url.rsplit('/', 1)[-1]
            self.calls.append((api, params))
            result = ({'id':123456, 'is_bot':True, 'first_name':'Test', 'username':'fun_test_bot'}
                      if api == 'getMe' else
                      {'message_id':1, 'date':0, 'chat':{'id':55, 'type':'private'}, 'text':'test'})
            return 200, json.dumps({'ok':True, 'result':result}).encode()
    request = Recording()
    await user(55)
    j, _, _ = await job()
    async with Bot('123456:OFFLINE_TEST_ONLY', request=request) as b:
        receipt = await ta.credit(j, 55)
        await ta.notify(b, j, receipt)
        text, kb = await ta.table(0, 7)
        await b.send_message(7, text, parse_mode='HTML', reply_markup=kb)
    sends = [params for api, params in request.calls if api == 'sendMessage']
    assert len(sends) == 2
    assert '+5 tokens' in sends[0]['text'] and sends[0]['parse_mode'] == 'HTML'
    assert '<pre>' in sends[1]['text'] and 'tg://user?id=55' in sends[1]['text']
    for params in sends:
        kb = params['reply_markup']
        if isinstance(kb, str): kb = json.loads(kb)
        assert all('style' in b for row in kb['inline_keyboard'] for b in row)
