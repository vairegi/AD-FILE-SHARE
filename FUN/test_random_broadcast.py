"""Offline Bot 3 shuffle/history and exact-message broadcasting regressions."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
import mongomock_motor
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot as TelegramBot
from telegram.error import BadRequest, Forbidden, TimedOut, RetryAfter
from telegram.request import BaseRequest
import config, db, handlers, feed_sessions, broadcast, runtime, main, chat_setup
from test_repairs import collection, update, context, Q, Bot


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    await runtime.stop_jobs()
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(), 'random_broadcast_tests')
    await db.col('users').create_index('user_id',unique=True)
    monkeypatch.setattr(config,'OWNER_ID',7)
    monkeypatch.setattr(config,'ADMIN_IDS',[7])
    monkeypatch.setattr(config,'REQUIRED_CHANNEL_IDS',[])
    monkeypatch.setattr(broadcast,'_worker_lock',asyncio.Lock())
    monkeypatch.setattr(broadcast,'SEND_GAP',0)
    monkeypatch.setattr(broadcast,'ALBUM_DELAY',0)
    chat_setup._pending.clear()
    broadcast._pending_albums.clear()
    broadcast._jobs.clear()
    yield
    await runtime.stop_jobs()
    chat_setup._pending.clear()
    broadcast._pending_albums.clear()
    broadcast._jobs.clear()


async def seed(n=4,category='movies'):
    docs=[]
    for i in range(n):
        doc=collection(f'{category}_{i}',mids=(100+i,),timestamp=i+1)
        doc['category']=category
        await db.upsert_collection(doc)
        docs.append(doc)
    return docs


def msg(mid=80,text='Message with @mention #tag <literal>',markup=None,gid=None):
    return NS(message_id=mid,chat=NS(id=7,type='private'),text=text,
              reply_markup=markup,media_group_id=gid)


def owner_message(source=None):
    u=update()
    u.message.message_id=81
    u.message.reply_to_message=source
    return u


def sending_bot():
    return NS(copy_message=AsyncMock(return_value=NS(message_id=900)),
              copy_messages=AsyncMock(side_effect=lambda **k:[NS(message_id=1000+i) for i in k['message_ids']]),
              send_message=AsyncMock(return_value=NS(message_id=901)))


async def make_job(bot=None,messages=None):
    bot=bot or sending_bot()
    job_id=await broadcast._preview(bot,7,7,messages or [{'message_id':80,'reply_markup':None}])
    return job_id,bot


async def confirm(job_id,bot,uid=7):
    q=Q(f'bc:send:{job_id}',uid=uid,bot=bot)
    await handlers.on_button(NS(callback_query=q),context(bot))
    task=broadcast._jobs.get(job_id)
    if task: await task
    return await db.col('broadcast_jobs').find_one({'_id':job_id})


async def test_random_next_has_no_repeats_until_all_seen():
    await seed(8)
    state,doc,total=await feed_sessions.start(7)
    seen=[doc['collection_id']]
    for _ in range(7):
        state,doc,_=await feed_sessions.navigate(7,state['session_id'],'next')
        seen.append(doc['collection_id'])
    assert total==8 and len(set(seen))==8
    state,doc,_=await feed_sessions.navigate(7,state['session_id'],'next')
    assert doc['collection_id']!=seen[-1]


async def test_previous_and_forward_replay_real_history():
    docs=await seed(4)
    state,first,_=await feed_sessions.start(7,seed=docs[0]['collection_id'])
    state,second,_=await feed_sessions.navigate(7,state['session_id'],'next')
    state,third,_=await feed_sessions.navigate(7,state['session_id'],'next')
    state,back,_=await feed_sessions.navigate(7,state['session_id'],'prev')
    assert back['collection_id']==second['collection_id']
    state,forward,_=await feed_sessions.navigate(7,state['session_id'],'next')
    assert forward['collection_id']==third['collection_id']
    assert first['collection_id']!=second['collection_id']


async def test_new_start_avoids_immediate_same_collection():
    await seed(2)
    state,a,_=await feed_sessions.start(7)
    _,b,_=await feed_sessions.start(7)
    assert a['collection_id']!=b['collection_id']
    assert await feed_sessions.navigate(7,state['session_id'],'next') is None


async def test_other_users_cannot_reuse_session():
    await seed()
    state,_,_=await feed_sessions.start(7)
    assert await feed_sessions.navigate(9,state['session_id'],'next') is None
    other,_,_=await feed_sessions.start(9)
    assert other['session_id']!=state['session_id']


async def test_category_scoped_and_new_posts_appear_without_rebuilding_history():
    await seed(2,'one'); await seed(2,'two')
    state,doc,total=await feed_sessions.start(7,'one')
    assert total==2 and doc['category']=='one'
    state,_,_=await feed_sessions.navigate(7,state['session_id'],'next')
    extra=collection('new_one');extra['category']='one';await db.upsert_collection(extra)
    state,doc,total=await feed_sessions.navigate(7,state['session_id'],'next')
    assert doc['collection_id']=='new_one' and total==3


async def test_empty_and_one_collection():
    assert await feed_sessions.start(7) is None
    await seed(1)
    state,doc,_=await feed_sessions.start(7)
    for direction in ('prev','next'):
        state,same,_=await feed_sessions.navigate(7,state['session_id'],direction)
        assert same['collection_id']==doc['collection_id']


async def test_disabled_empty_and_deleted_items_are_skipped():
    docs=await seed(4)
    for index,status,count in ((0,'hidden',1),(1,'active',0)):
        await db.col('collections').update_one({'collection_id':docs[index]['collection_id']},
            {'$set':{'status':status,'file_count':count}})
    state,doc,total=await feed_sessions.start(7)
    assert total==2 and doc['collection_id'] in (docs[2]['collection_id'],docs[3]['collection_id'])
    await db.col('collections').delete_one({'collection_id':doc['collection_id']})
    state,other,_=await feed_sessions.navigate(7,state['session_id'],'next')
    assert other['collection_id']!=doc['collection_id']


async def test_history_bounded_and_persistent(monkeypatch):
    await seed(5)
    monkeypatch.setattr(feed_sessions,'HISTORY_LIMIT',4)
    monkeypatch.setattr(feed_sessions,'SEEN_LIMIT',6)
    state,_,_=await feed_sessions.start(7)
    for _ in range(15):
        state,_,_=await feed_sessions.navigate(7,state['session_id'],'next')
    saved=await db.col('feed_sessions').find_one({'_id':7})
    assert len(saved['history'])==4 and len(saved['seen'])<=6
    assert saved['expires_on']
    state,doc,_=await feed_sessions.navigate(7,saved['session_id'],'prev')
    assert doc['collection_id']==saved['history'][-2]


async def test_callback_keyboard_drives_next_and_previous():
    await seed(3)
    q=Q('feed:all:0')
    await handlers.on_button(NS(callback_query=q),context(q.bot))
    saved=await db.col('feed_sessions').find_one({'_id':7})
    sid=saved['session_id']
    markup=q.bot.sent[-1][2]['reply_markup'].to_dict()['inline_keyboard']
    assert markup[0][0]['callback_data']=='noop'
    assert markup[0][2]['callback_data']==f'watch:next:{sid}'
    first=saved['history'][0]
    q.data=f'watch:next:{sid}'
    await handlers.on_button(NS(callback_query=q),context(q.bot))
    saved=await db.col('feed_sessions').find_one({'_id':7})
    assert len(saved['history'])==2
    q.data=f'watch:prev:{sid}'
    await handlers.on_button(NS(callback_query=q),context(q.bot))
    assert (await db.col('feed_sessions').find_one({'_id':7}))['history'][0]==first
    assert (await db.col('feed_sessions').find_one({'_id':7}))['position']==0


async def test_delivery_next_does_not_return_to_first_database_post():
    docs=await seed(3)
    state,_,_=await feed_sessions.start(7,seed=docs[1]['collection_id'])
    sid=await feed_sessions.next_after_delivery(7,docs[1])
    assert sid==state['session_id']
    _,next_doc,_=await feed_sessions.navigate(7,sid,'next')
    assert next_doc['collection_id']!=docs[1]['collection_id']


async def test_disabled_collection_cannot_be_charged():
    doc=(await seed(1))[0]
    await db.col('collections').update_one({'collection_id':doc['collection_id']},{'$set':{'status':'hidden'}})
    await db.create_user(7);await db.add_tokens(7,10,'offline')
    q=Q();await handlers._serialized_unlock(update(),context(q.bot),q,doc['collection_id'],False)
    assert (await db.get_user(7))['tokens']==10
    assert not q.bot.calls


async def test_broadcast_command_no_send_until_confirmation():
    bot=sending_bot();u=owner_message(msg())
    await handlers.cmd_broadcast(u,context(bot))
    assert bot.copy_message.await_count==1  # preview only
    bot.copy_message.assert_awaited_once_with(chat_id=7,from_chat_id=7,message_id=80)
    job=await db.col('broadcast_jobs').find_one({})
    assert job['status']=='draft'


async def test_pending_input_supports_media_without_text():
    bot=sending_bot();u=owner_message()
    await handlers.cmd_broadcast(u,context(bot))
    u.message=msg(text=None)
    await handlers.on_private_message(u,context(bot))
    assert await db.col('broadcast_jobs').count_documents({})==1
    assert await db.col('broadcast_inputs').count_documents({})==0


async def test_formatted_text_and_existing_inline_buttons_copied_not_reconstructed():
    markup=InlineKeyboardMarkup([[InlineKeyboardButton('Open',url='https://example.com'),
                                  InlineKeyboardButton('Menu',callback_data='home')]])
    bot=sending_bot();u=owner_message(msg(markup=markup))
    await handlers.cmd_broadcast(u,context(bot))
    await db.create_user(10)
    job=await db.col('broadcast_jobs').find_one({})
    await confirm(job['_id'],bot)
    call=next(c.kwargs for c in bot.copy_message.call_args_list if c.kwargs['chat_id']==10)
    assert call['from_chat_id']==7 and call['message_id']==80
    assert call['reply_markup'].to_dict()==markup.to_dict()
    assert 'text' not in call and 'caption' not in call and 'parse_mode' not in call


async def test_nonadmin_cannot_prepare_or_confirm():
    bot=sending_bot();u=owner_message(msg());u.effective_user=NS(id=9)
    await handlers.cmd_broadcast(u,context(bot))
    assert not bot.copy_message.await_count
    jid,_=await make_job(bot)
    await confirm(jid,bot,uid=9)
    assert (await db.col('broadcast_jobs').find_one({'_id':jid}))['status']=='draft'


async def test_job_belongs_to_preparing_admin(monkeypatch):
    monkeypatch.setattr(config,'ADMIN_IDS',[7,9])
    jid,bot=await make_job()
    await confirm(jid,bot,uid=9)
    assert (await db.col('broadcast_jobs').find_one({'_id':jid}))['status']=='draft'


async def test_double_confirm_does_not_send_twice():
    await db.create_user(10);await db.create_user(11)
    jid,bot=await make_job()
    job=await confirm(jid,bot)
    assert job['sent']==2
    await confirm(jid,bot)
    targets=[c.kwargs['chat_id'] for c in bot.copy_message.call_args_list]
    assert targets.count(10)==1 and targets.count(11)==1


async def test_large_audience_is_paginated_and_streamed():
    await db.col('users').insert_many([{'user_id':uid} for uid in range(1000,1235)])
    jid,bot=await make_job()
    job=await confirm(jid,bot)
    assert job['sent']==235 and job['status']=='finished'
    assert bot.copy_message.await_count==236  # one preview + 235 recipients


async def test_blocked_and_bad_requests_do_not_stop_broadcast():
    for uid in (10,11,12):await db.create_user(uid)
    jid,bot=await make_job()
    async def copy(**kw):
        if kw['chat_id']==10:raise Forbidden('blocked')
        if kw['chat_id']==11:raise BadRequest('chat not found')
        return NS(message_id=1)
    bot.copy_message.side_effect=copy
    job=await confirm(jid,bot)
    assert job['sent']==1 and job['blocked']==1 and job['failed']==1


async def test_network_uncertainty_not_blindly_retried():
    await db.create_user(10)
    jid,bot=await make_job()
    bot.copy_message.side_effect=TimedOut('unknown acceptance')
    job=await confirm(jid,bot)
    assert job['uncertain']==1
    assert bot.copy_message.await_count==2  # preview + single uncertain send


async def test_rate_limit_honors_full_numeric_and_timedelta_wait(monkeypatch):
    for value in (70,timedelta(seconds=90)):
        error=RetryAfter(value)
        call=AsyncMock(side_effect=[error,NS(message_id=1)])
        sleep=AsyncMock()
        with monkeypatch.context() as mp:
            mp.setenv('PTB_TIMEDELTA','true' if isinstance(value,timedelta) else 'false')
            mp.setattr(broadcast.asyncio,'sleep',sleep)
            await broadcast._retry(call)
        seconds=value.total_seconds() if isinstance(value,timedelta) else value
        sleep.assert_awaited_once_with(seconds+1)
        assert call.await_count==2


async def test_rate_limit_retries_bounded(monkeypatch):
    call=AsyncMock(side_effect=RetryAfter(2))
    monkeypatch.setattr(broadcast.asyncio,'sleep',AsyncMock())
    with pytest.raises(RetryAfter):await broadcast._retry(call)
    assert call.await_count==broadcast.MAX_ATTEMPTS


async def test_unsupported_message_stops_during_preview():
    bot=sending_bot();bot.copy_message.side_effect=BadRequest('cannot copy')
    assert await broadcast._preview(bot,7,7,[{'message_id':1}]) is None
    assert await db.col('broadcast_jobs').count_documents({})==0
    assert 'Nothing was broadcast' in bot.send_message.call_args.args[1]


async def test_complete_album_is_collected_and_copied_as_batch():
    bot=sending_bot();u=owner_message()
    await handlers.cmd_broadcast(u,context(bot))
    for mid in (82,80,81):
        u.message=msg(mid=mid,text=None,gid='album1')
        await handlers.on_private_message(u,context(bot))
    await asyncio.gather(*[a['task'] for a in broadcast._pending_albums.values()])
    args=bot.copy_messages.call_args.kwargs
    assert args['message_ids']==[80,81,82]
    await db.create_user(10)
    job=await db.col('broadcast_jobs').find_one({})
    final=await confirm(job['_id'],bot)
    assert final['sent']==1 and bot.copy_messages.await_count==2


async def test_reply_to_one_album_item_requires_safe_full_album_workflow():
    bot=sending_bot();u=owner_message(msg(gid='g'))
    await handlers.cmd_broadcast(u,context(bot))
    assert not bot.copy_message.await_count and not bot.copy_messages.await_count
    assert 'WHOLE album' in u.message.replies[-1][0]


async def test_partial_album_result_is_uncertain_not_retried():
    bot=sending_bot()
    source=[{'message_id':1},{'message_id':2}]
    jid,_=await make_job(bot,source)
    await db.create_user(10)
    bot.copy_messages.side_effect=None;bot.copy_messages.return_value=[NS(message_id=1)]
    job=await confirm(jid,bot)
    assert job['uncertain']==1 and bot.copy_messages.await_count==2


async def test_cancel_draft_and_expired_draft_cannot_send():
    jid,bot=await make_job()
    q=Q(f'bc:cancel:{jid}',bot=bot)
    await handlers.on_button(NS(callback_query=q),context(bot))
    await confirm(jid,bot)
    assert (await db.col('broadcast_jobs').find_one({'_id':jid}))['status']=='cancelled'
    jid,_=await make_job(bot)
    await db.col('broadcast_jobs').update_one({'_id':jid},{'$set':{'draft_expires_at':0}})
    await confirm(jid,bot)
    assert (await db.col('broadcast_jobs').find_one({'_id':jid}))['status']=='draft'


async def test_cancel_active_job_and_pending_input():
    await db.create_user(10);await db.create_user(11)
    jid,bot=await make_job()
    entered=asyncio.Event()
    async def wait(**kw):
        entered.set();await asyncio.Event().wait()
    bot.copy_message.side_effect=wait
    q=Q(f'bc:send:{jid}',bot=bot)
    await handlers.on_button(NS(callback_query=q),context(bot))
    await asyncio.wait_for(entered.wait(),1)
    await handlers.cmd_broadcast(owner_message(),context(bot,args=['cancel']))
    task=broadcast._jobs.get(jid)
    if task:await task
    job=await db.col('broadcast_jobs').find_one({'_id':jid})
    assert job['status']=='cancelled' and job['uncertain']==1


async def test_restart_marks_active_jobs_interrupted_without_replay():
    await db.col('broadcast_jobs').insert_many([
        {'_id':'a','status':'running','owner_id':7},
        {'_id':'b','status':'queued','owner_id':7},
        {'_id':'c','status':'finished','owner_id':7}])
    await broadcast.recover_after_restart()
    assert await db.col('broadcast_jobs').count_documents({'status':'interrupted'})==2
    assert (await db.col('broadcast_jobs').find_one({'_id':'c'}))['status']=='finished'
    assert not broadcast._jobs


async def test_userbot_credentials_never_become_broadcast_content():
    bot=sending_bot();u=owner_message()
    chat_setup._pending[7]={'expires_at':10**20,'step':'api_id'}
    await handlers.cmd_broadcast(u,context(bot))
    assert await db.col('broadcast_inputs').count_documents({})==0
    assert 'Credentials' in u.message.replies[-1][0]


async def test_expired_input_is_not_captured_and_status_command_works():
    bot=sending_bot();u=owner_message()
    await db.col('broadcast_inputs').insert_one({'_id':7,'expires_at':0})
    u.message.message_id=88
    assert await broadcast.consume(u,context(bot))
    assert not bot.copy_message.await_count
    await handlers.cmd_broadcast(u,context(bot,args=['status']))
    assert 'No broadcasts' in u.message.replies[-1][0]


async def test_cancel_queued_job_is_persisted_before_worker_starts():
    jid,bot=await make_job()
    await db.col('broadcast_jobs').update_one({'_id':jid},{'$set':{'status':'queued'}})
    await handlers.cmd_broadcast(owner_message(),context(bot,args=['cancel']))
    job=await db.col('broadcast_jobs').find_one({'_id':jid})
    assert job['status']=='cancelled'
    await broadcast._run(bot,jid)
    assert bot.copy_message.await_count==1  # preview only


async def test_nonadmin_private_message_never_becomes_broadcast():
    u=owner_message();u.effective_user=NS(id=999)
    await db.col('broadcast_inputs').insert_one({'_id':999,'expires_at':db.now_ts()+600})
    bot=sending_bot()
    assert not await broadcast.consume(u,context(bot))
    bot.copy_message.assert_not_awaited()


async def test_concurrent_next_clicks_keep_valid_history():
    await seed(5)
    state,_,_=await feed_sessions.start(7)
    await asyncio.gather(*[feed_sessions.navigate(7,state['session_id'],'next') for _ in range(3)])
    saved=await db.col('feed_sessions').find_one({'_id':7})
    assert len(saved['history'])==4 and len(set(saved['history']))==4
    assert saved['position']==3


async def test_previous_skips_deleted_history_entries():
    await seed(5)
    state,first,_=await feed_sessions.start(7)
    state,second,_=await feed_sessions.navigate(7,state['session_id'],'next')
    state,_,_=await feed_sessions.navigate(7,state['session_id'],'next')
    await db.col('collections').delete_one({'collection_id':second['collection_id']})
    _,back,_=await feed_sessions.navigate(7,state['session_id'],'prev')
    assert back['collection_id']==first['collection_id']


async def test_album_inline_keyboard_restored_without_recopied_media():
    bot=sending_bot()
    bot.edit_message_reply_markup=AsyncMock()
    markup=InlineKeyboardMarkup([[InlineKeyboardButton('Visit',url='https://example.com')]])
    complete=await broadcast._copy(bot,10,7,[
        {'message_id':2,'reply_markup':markup.to_dict()}, {'message_id':1}])
    assert complete
    bot.copy_messages.assert_awaited_once()
    args=bot.edit_message_reply_markup.call_args.kwargs
    assert args['message_id']==1002 and args['reply_markup'].to_dict()==markup.to_dict()
    bot.edit_message_reply_markup.side_effect=BadRequest('cannot attach buttons')
    complete=await broadcast._copy(bot,10,7,[
        {'message_id':2,'reply_markup':markup.to_dict()}, {'message_id':1}])
    assert not complete and bot.copy_messages.await_count==2


async def test_cancel_command_acknowledges_broadcast_input_not_setup():
    bot=sending_bot();u=owner_message()
    await handlers.cmd_broadcast(u,context(bot))
    await handlers.cmd_cancel(u,context(bot))
    assert await db.col('broadcast_inputs').count_documents({})==0
    assert 'Broadcast message input cancelled' in u.message.replies[-1][0]
    bot.copy_message.assert_not_awaited()


class CaptureRequest(BaseRequest):
    def __init__(self):self.calls=[]
    @property
    def read_timeout(self):return 5
    async def initialize(self):pass
    async def shutdown(self):pass
    async def do_request(self,url,method,request_data=None,**kwargs):
        name=url.rsplit('/',1)[-1]
        params=request_data.json_parameters if request_data else {}
        self.calls.append((name,params))
        result={'id':123,'is_bot':True,'first_name':'OfflineBot'} if name=='getMe' else {'message_id':55}
        return 200,json.dumps({'ok':True,'result':result}).encode()


async def test_real_ptb_serialization_of_copy_and_keyboard():
    transport=CaptureRequest()
    bot=TelegramBot('123:OFFLINE_TEST_TOKEN',request=transport)
    async with bot:
        markup=InlineKeyboardMarkup([[InlineKeyboardButton('Visit',url='https://example.com')]])
        await broadcast._copy(bot,10,7,[{'message_id':80,'reply_markup':markup.to_dict()}])
    name,params=transport.calls[-1]
    assert name=='copyMessage'
    assert params['chat_id']=='10' and params['from_chat_id']=='7' and params['message_id']=='80'
    assert json.loads(params['reply_markup'])==markup.to_dict()
    assert 'text' not in params and 'parse_mode' not in params


def test_broadcast_registered_and_all_private(monkeypatch):
    monkeypatch.setattr(config,'BOT_TOKEN','123456:FAKE_TEST_TOKEN_NOT_REAL')
    app=main.build_bot()
    registered={name for h in app.handlers[0] if hasattr(h,'commands') for name in h.commands}
    assert 'broadcast' in registered
