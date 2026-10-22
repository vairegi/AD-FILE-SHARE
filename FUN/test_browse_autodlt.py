"""Offline regressions for cross-channel discovery, category scoping and /autodlt."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
import mongomock_motor
import config, db, delivery_policy, feed_sessions, handlers, main, services
from test_repairs import collection, update, context, Q, Bot


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(),'browse_autodlt_test')
    monkeypatch.setattr(config,'ADMIN_IDS',[7])
    monkeypatch.setattr(config,'OWNER_ID',7)
    monkeypatch.setattr(config,'AUTO_DELETE_AFTER_SECONDS',600)
    monkeypatch.setattr(config,'REQUIRED_CHANNEL_IDS',[])
    yield


@pytest.mark.parametrize('raw,seconds', [('1m',60),('15m',900),('20m',1200),('30m',1800),
    ('1h',3600),('2h',7200),('90s',90),('24h',86400),('OFF',0),(' 15M ',900)])
def test_duration_parser(raw,seconds):
    assert delivery_policy.parse_duration(raw)==seconds


@pytest.mark.parametrize('raw',['','10','0m','-1m','1.5m','25h','1 hour','7mins','garbage','999999999h'])
def test_invalid_duration(raw):
    with pytest.raises(ValueError):delivery_policy.parse_duration(raw)


async def seed():
    for cat,channel in [('Manga',-100),('Sparkle',-200)]:
        await db.upsert_channel_mapping(channel,cat)
        for i in range(3):
            c=collection(f'{cat}_{i}',mids=(i+100,))
            c.update(category=cat,channel_id=channel)
            await db.upsert_collection(c)


async def test_global_random_can_select_both_channels():
    await seed()
    channels=set()
    for i in range(60):
        _,doc,_=await feed_sessions.start(i+100,'all')
        channels.add(doc['channel_id'])
    assert channels=={-100,-200}


async def test_next_in_all_mode_does_not_inherit_old_category():
    await seed()
    await feed_sessions.start(7,'Manga')
    state,_,_=await feed_sessions.start(7,'all')
    sources=set()
    for _ in range(18):
        state,doc,_=await feed_sessions.navigate(7,state['session_id'],'next')
        sources.add(doc['channel_id'])
        assert state['category']=='all'
    assert sources=={-100,-200}


@pytest.mark.parametrize('category',['Manga','Sparkle'])
async def test_selected_category_stays_isolated_through_next_previous(category):
    await seed()
    await feed_sessions.start(7,'all',seed='Sparkle_0' if category=='Manga' else 'Manga_0')
    state,_,_=await feed_sessions.start(7,category)
    for direction in ['prev']+['next']*12+['prev']*12:
        state,doc,_=await feed_sessions.navigate(7,state['session_id'],direction)
        assert doc['category']==category and state['category']==category
    assert await db.col('feed_history').count_documents({'user_id':7})>1


async def test_user_start_watching_and_category_callbacks(monkeypatch):
    await seed()
    render=AsyncMock()
    monkeypatch.setattr(handlers,'_render_feed_card',render)
    await handlers.on_button(NS(callback_query=Q('feed:all:0')),context())
    state=await db.col('feed_sessions').find_one({'_id':7})
    assert state['browse_category']=='all'
    await handlers.on_button(NS(callback_query=Q('feed:Manga:0')),context())
    state=await db.col('feed_sessions').find_one({'_id':7})
    assert state['browse_category']=='Manga'
    assert render.await_args.args[1]['category']=='Manga'
    kb=handlers._home_kb().to_dict()
    assert kb['inline_keyboard'][0][0]['callback_data']=='feed:all:0'


async def test_autodlt_admin_gate_and_validation():
    await handlers.cmd_autodlt(update(uid=99),context(args=['15m']))
    assert 'autodlt_seconds' not in await db.get_settings()
    for args in [['bad'],['15m','extra']]:
        u=update()
        await handlers.cmd_autodlt(u,context(args=args))
        assert 'Usage:' in u.message.replies[-1][0]
        assert 'autodlt_seconds' not in await db.get_settings()


async def test_setting_persisted_and_read_from_database_not_environment(monkeypatch):
    u=update()
    await handlers.cmd_autodlt(u,context(args=['15m']))
    assert (await db.get_settings())['autodlt_seconds']==900
    monkeypatch.setattr(config,'AUTO_DELETE_AFTER_SECONDS',123)
    assert await delivery_policy.current_seconds()==900
    assert await delivery_policy.deadline(now=1000)==1900
    status=update()
    await handlers.cmd_autodlt(status,context())
    assert '900 seconds' in status.message.replies[-1][0]


@pytest.mark.parametrize('setting,expected',[(60,1060),(1800,2800),(3600,4600),(0,None)])
async def test_real_delivery_uses_selected_deadline_and_sweeper(monkeypatch,setting,expected):
    await db.update_settings({'autodlt_seconds':setting})
    monkeypatch.setattr(db,'now_ts',lambda:1000)
    c=collection()
    await db.upsert_collection(c)
    await db.col('users').insert_one({'user_id':7,'tokens':122})
    bot=Bot()
    await handlers._deliver(update(),context(bot),Q(bot=bot),c,0)
    delivery=await db.col('deliveries').find_one({})
    assert delivery['auto_delete_at']==expected
    monkeypatch.setattr(db,'now_ts',lambda:999999)
    await services.run_deletion_sweeper(bot)
    assert len(bot.deleted)==(len(c['files']) if setting else 0)


async def test_default_timer_and_existing_delivery_deadlines_unchanged():
    assert await delivery_policy.current_seconds()==600
    await db.create_delivery({'delivery_id':'old','user_id':7,'auto_delete_at':1500,
        'delete_status':'pending','message_ids':[11]})
    await handlers.cmd_autodlt(update(),context(args=['1h']))
    assert (await db.get_delivery('old'))['auto_delete_at']==1500
    assert await delivery_policy.deadline(now=1000)==4600


async def test_autodlt_registered_private_command(monkeypatch):
    monkeypatch.setattr(config,'BOT_TOKEN','123456:offline-test-token')
    app=main.build_bot()
    h=next(h for h in app.handlers[0] if 'autodlt' in getattr(h,'commands',()))
    assert h.callback is handlers.cmd_autodlt
