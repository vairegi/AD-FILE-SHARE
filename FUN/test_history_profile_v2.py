"""Offline regressions for durable Previous/Next and counted profile buttons."""
import asyncio
from types import SimpleNamespace as NS
import pytest
import pytest_asyncio
import mongomock_motor
import config, db, handlers, feed_sessions, runtime
from test_repairs import collection, Q, update, context


@pytest_asyncio.fixture(autouse=True)
async def isolated(monkeypatch):
    await runtime.stop_jobs()
    db.use_test_client(mongomock_motor.AsyncMongoMockClient(),'history_profile_v2')
    await db.col('collections').create_index('collection_id',unique=True)
    await db.col('feed_history').create_index([('user_id',1),('seq',1)],unique=True)
    await db.col('unlocks').create_index([('user_id',1),('collection_id',1)],unique=True)
    await db.col('bookmarks').create_index([('user_id',1),('collection_id',1)],unique=True)
    monkeypatch.setattr(config,'OWNER_ID',7)
    monkeypatch.setattr(config,'ADMIN_IDS',[7])
    monkeypatch.setattr(config,'REQUIRED_CHANNEL_IDS',[])
    yield
    await runtime.stop_jobs()


async def docs(n=8,category='movies'):
    result=[]
    for i in range(n):
        doc=collection(f'{category}_{i}',mids=(1000+i,))
        doc['category']=category
        await db.upsert_collection(doc)
        result.append(doc)
    return result


async def open_seed(doc,uid=7):
    return await feed_sessions.start(uid,doc['category'],seed=doc['collection_id'])


async def test_repeated_start_does_not_erase_previous_posts():
    items=await docs(6)
    result=None
    for doc in items:
        result=await open_seed(doc)
    state,current,_=result
    assert current['collection_id']==items[-1]['collection_id']
    for expected in reversed(items[:-1]):
        state,previous,_=await feed_sessions.navigate(7,state['session_id'],'prev')
        assert previous['collection_id']==expected['collection_id']
    assert await db.col('feed_history').count_documents({'user_id':7})==6


async def test_repeated_start_watching_random_calls_keep_entire_trail():
    await docs(8)
    viewed=[]
    for _ in range(6):
        state,doc,_=await feed_sessions.start(7)
        viewed.append(doc['collection_id'])
    assert len(set(viewed))==6
    for expected in reversed(viewed[:-1]):
        state,doc,_=await feed_sessions.navigate(7,state['session_id'],'prev')
        assert doc['collection_id']==expected


async def test_unlocking_old_card_preserves_the_full_chain():
    items=await docs(5)
    for doc in items:
        state,_,_=await open_seed(doc)
    sid=await feed_sessions.next_after_delivery(7,items[1])
    assert sid==state['session_id']
    state,doc,_=await feed_sessions.navigate(7,sid,'prev')
    assert doc['collection_id']==items[-1]['collection_id']
    state,doc,_=await feed_sessions.navigate(7,sid,'prev')
    assert doc['collection_id']==items[-2]['collection_id']


async def test_restream_current_card_does_not_duplicate_or_reset_history():
    items=await docs(3)
    for doc in items:
        state,_,_=await open_seed(doc)
    for _ in range(3):
        assert await feed_sessions.next_after_delivery(7,items[-1])==state['session_id']
    assert await db.col('feed_history').count_documents({'user_id':7})==3
    _,previous,_=await feed_sessions.navigate(7,state['session_id'],'prev')
    assert previous['collection_id']==items[-2]['collection_id']


async def test_history_window_never_deletes_full_history(monkeypatch):
    items=await docs(9)
    monkeypatch.setattr(feed_sessions,'HISTORY_LIMIT',3)
    for doc in items:
        state,_,_=await open_seed(doc)
    assert len(state['history'])==3
    assert await db.col('feed_history').count_documents({'user_id':7})==9
    for expected in reversed(items[:-1]):
        state,current,_=await feed_sessions.navigate(7,state['session_id'],'prev')
        assert current['collection_id']==expected['collection_id']
        assert len(state['history'])<=3
    assert state['has_previous'] is False and state['has_forward'] is True
    for expected in items[1:]:
        state,current,_=await feed_sessions.navigate(7,state['session_id'],'next')
        assert current['collection_id']==expected['collection_id']


async def test_previous_can_go_beyond_two_hundred_records():
    items=await docs(215)
    await db.col('feed_history').insert_many([
        {'_id':f'7:{i+1}','user_id':7,'seq':i+1,'collection_id':doc['collection_id'],'category':'movies'}
        for i,doc in enumerate(items)])
    sid=await feed_sessions.next_after_delivery(7,items[-1])
    # Cross the old 200-entry retention boundary without losing anything.
    for _ in range(205):
        state,current,_=await feed_sessions.navigate(7,sid,'prev')
    assert current['collection_id']==items[9]['collection_id']
    assert await db.col('feed_history').count_documents({'user_id':7})==215


async def test_legacy_array_migrates_idempotently_and_keeps_callback_id():
    items=await docs(5)
    await db.col('feed_sessions').insert_one({'_id':7,'session_id':'legacy',
        'category':'movies','history':[c['collection_id'] for c in items],
        'position':4,'seen':[]})
    state,doc,_=await feed_sessions.navigate(7,'legacy','prev')
    assert state['schema']==2 and doc['collection_id']==items[3]['collection_id']
    await feed_sessions.next_after_delivery(7,doc)
    assert await db.col('feed_history').count_documents({'user_id':7})==5


async def test_session_expiry_restart_does_not_delete_persistent_history():
    items=await docs(4)
    for doc in items:await open_seed(doc)
    await db.col('feed_sessions').delete_one({'_id':7})
    sid=await feed_sessions.next_after_delivery(7,items[-1])
    state,doc,_=await feed_sessions.navigate(7,sid,'prev')
    assert doc['collection_id']==items[-2]['collection_id']
    assert await db.col('feed_history').count_documents({'user_id':7})==4


async def test_category_changes_preserve_actual_viewing_trail():
    one=await docs(2,'one');two=await docs(2,'two')
    await open_seed(one[0]);state,_,_=await open_seed(two[0])
    state,back,_=await feed_sessions.navigate(7,state['session_id'],'prev')
    assert back['collection_id']==one[0]['collection_id'] and state['category']=='one'
    state,forward,_=await feed_sessions.navigate(7,state['session_id'],'next')
    assert forward['collection_id']==two[0]['collection_id'] and state['category']=='two'


async def test_new_view_after_back_keeps_forward_trail_and_last_visible_post():
    items=await docs(5)
    for doc in items[:3]:state,_,_=await open_seed(doc)
    state,_,_=await feed_sessions.navigate(7,state['session_id'],'prev')
    state,_,_=await open_seed(items[3])
    state,back,_=await feed_sessions.navigate(7,state['session_id'],'prev')
    assert back['collection_id']==items[1]['collection_id']
    state,back,_=await feed_sessions.navigate(7,state['session_id'],'prev')
    assert back['collection_id']==items[2]['collection_id']
    assert await db.col('feed_history').count_documents({'collection_id':items[2]['collection_id']})==1


async def test_more_than_a_page_of_deleted_entries_are_skipped():
    items=await docs(65)
    await db.col('feed_history').insert_many([
        {'_id':f'7:{i+1}','user_id':7,'seq':i+1,'collection_id':doc['collection_id'],'category':'movies'}
        for i,doc in enumerate(items)])
    await db.col('collections').delete_many({'collection_id':{'$in':[d['collection_id'] for d in items[1:-1]]}})
    sid=await feed_sessions.next_after_delivery(7,items[-1])
    _,back,_=await feed_sessions.navigate(7,sid,'prev')
    assert back['collection_id']==items[0]['collection_id']


async def test_no_shuffle_button_and_previous_works_outside_ui_window():
    kb=handlers._feed_kb('x','movies',0,1,False,session={
        'session_id':'test','position':0,'history':['x'],
        'has_previous':True,'has_forward':True})
    rows=kb.to_dict()['inline_keyboard']
    assert rows[0][0]['callback_data']=='watch:prev:test'
    assert rows[0][2]['callback_data']=='watch:next:test'
    assert all('Shuffle' not in b['text'] for row in rows for b in row)
    assert len(rows)==2


async def test_unlocked_and_saved_library_views_join_the_same_history():
    items=await docs(3)
    await open_seed(items[0])
    await db.insert_unlock(7,items[1]['collection_id'])
    await db.toggle_bookmark(7,items[2]['collection_id'])
    await handlers._show_library(Q(),'unlocks',0)
    await handlers._show_library(Q(),'bookmarks',0)
    state=await db.col('feed_sessions').find_one({'_id':7})
    _,back,_=await feed_sessions.navigate(7,state['session_id'],'prev')
    assert back['collection_id']==items[1]['collection_id']


async def test_shared_link_view_does_not_erase_history():
    items=await docs(3)
    await open_seed(items[0]);await open_seed(items[1])
    u=update();c=context()
    await handlers._send_feed_card(u,c,items[2]['collection_id'])
    state=await db.col('feed_sessions').find_one({'_id':7})
    _,back,_=await feed_sessions.navigate(7,state['session_id'],'prev')
    assert back['collection_id']==items[1]['collection_id']


async def test_histories_are_private_and_old_cards_do_not_change_current_state():
    items=await docs(3)
    old,_,_=await open_seed(items[0])
    new,_,_=await open_seed(items[1])
    assert await feed_sessions.navigate(9,new['session_id'],'prev') is None
    assert await feed_sessions.navigate(7,old['session_id'],'prev') is None
    state=await db.col('feed_sessions').find_one({'_id':7})
    assert state['cursor']==2


async def test_concurrent_navigation_does_not_lose_records():
    await docs(8)
    state,_,_=await feed_sessions.start(7)
    await asyncio.gather(*[feed_sessions.navigate(7,state['session_id'],'next') for _ in range(4)])
    records=await db.col('feed_history').find({'user_id':7}).sort('seq',1).to_list(length=10)
    assert len(records)==5 and [r['seq'] for r in records]==[1,2,3,4,5]
    assert len({r['collection_id'] for r in records})==5


@pytest.mark.parametrize('unlocked,saved',[(0,0),(1,0),(10,3),(4,1)])
async def test_profile_buttons_use_exact_counts_and_kaomoji(unlocked,saved):
    for i in range(unlocked):await db.insert_unlock(7,f'unlocked_{i}')
    for i in range(saved):await db.toggle_bookmark(7,f'saved_{i}')
    q=Q();await handlers._show_profile(q)
    rows=q.message.edits[-1][1]['reply_markup'].to_dict()['inline_keyboard']
    assert rows[0][0]['text']==f'(⌕) Unlocked Posts ({unlocked})'
    assert rows[0][1]['text']==f'⭐ Saved Posts ({saved})'
    assert rows[0][0]['callback_data']=='library:unlocks:0'
    assert rows[0][1]['callback_data']=='library:bookmarks:0'
    assert '🔓' not in rows[0][0]['text']


async def test_profile_counts_refresh_after_bookmark_remove_and_unlock():
    await db.toggle_bookmark(7,'one')
    q=Q();await handlers._show_profile(q)
    assert q.message.edits[-1][1]['reply_markup'].inline_keyboard[0][1].text=='⭐ Saved Posts (1)'
    await db.toggle_bookmark(7,'one');await db.insert_unlock(7,'one')
    await handlers._show_profile(q)
    row=q.message.edits[-1][1]['reply_markup'].inline_keyboard[0]
    assert row[0].text=='(⌕) Unlocked Posts (1)' and row[1].text=='⭐ Saved Posts (0)'


async def test_profile_lifetime_totals_include_more_than_500_transactions():
    await db.col('transactions').insert_many([
        {'user_id':7,'amount':1,'at':i} for i in range(600)] +
        [{'user_id':7,'amount':-2,'at':601+i} for i in range(25)] +
        [{'user_id':99,'amount':9999,'at':9999}])
    assert await db.transaction_totals(7)==(600,50)
    q=Q();await handlers._show_profile(q)
    text=q.message.edits[-1][0]
    assert '600' in text and '50' in text and '9999' not in text


async def test_profile_no_transactions_has_zero_totals():
    assert await db.transaction_totals(7)==(0,0)


async def test_repeated_next_in_one_drop_category_creates_no_fake_history():
    item=(await docs(1))[0]
    state,_,_=await open_seed(item)
    for _ in range(5):await feed_sessions.navigate(7,state['session_id'],'next')
    assert await db.col('feed_history').count_documents({'user_id':7})==1
