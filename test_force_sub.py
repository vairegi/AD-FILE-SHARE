"""Root force-sub UX regressions; no production credentials/network.
Reuses the existing test_root_index.py isolated mock/local Mongo fixture.
ROOT_LOCAL_MONGO_TEST=1 uses localhost:27019 sandbox database only.
"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from telegram.error import BadRequest
from test_root_index import isolate
import bot1, bot2, bot1_admin as admin, config, db, force_sub


@pytest.fixture(autouse=True)
def clear_titles():
    force_sub._metadata.clear()
    yield
    force_sub._metadata.clear()


def fake_bot(statuses=None, titles=None, bot_id=11):
    statuses = statuses or {}
    titles = titles or {}
    async def member(chat_id, user_id):
        result = statuses.get(int(chat_id), 'left')
        if isinstance(result, Exception): raise result
        if isinstance(result, tuple): return NS(status='restricted', is_member=result[1])
        return NS(status=result)
    async def chat(cid):
        result = titles.get(int(cid), f'Title {cid}')
        if isinstance(result, Exception): raise result
        return NS(title=result, username=None)
    return NS(id=bot_id, get_chat_member=AsyncMock(side_effect=member),
        get_chat=AsyncMock(side_effect=chat), send_message=AsyncMock(),
        export_chat_invite_link=AsyncMock(return_value='https://t.me/+fallback'),
        create_chat_invite_link=AsyncMock(return_value=NS(invite_link='https://t.me/+created')))


async def seed(channels, category=None, links=True):
    fields = {'force_sub_channel_ids':channels, 'force_sub_channel_id':None,
        'force_sub_links': {str(c):f'https://t.me/+invite{abs(c)}' for c in channels} if links else {}}
    if category:
        await db._db.categories.update_one({'key':category},{'$set':fields},upsert=True)
    else:
        await db.update_settings(fields)


async def prompt(module, bot, uid=7, category=None, **kwargs):
    if module is bot1:
        await module.send_force_sub(bot, uid, 'demo_f1', category, **kwargs)
    else:
        await module.send_force_sub(bot, uid, category, **kwargs)
    return bot.send_message.call_args


def url_buttons(call):
    return [b for row in call.kwargs['reply_markup'].inline_keyboard for b in row if b.url]


def query(user=7, data='checksub:demo_f1'):
    return NS(from_user=NS(id=user), data=data,
        answer=AsyncMock(), edit_message_text=AsyncMock())


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_only_new_fourth_channel_after_user_joined_first_three(module):
    await seed([-101,-102,-103,-104])
    bot=fake_bot({-101:'member',-102:'administrator',-103:'creator'}, {-104:'New Updates ✨'})
    call=await prompt(module,bot)
    buttons=url_buttons(call)
    assert len(buttons)==1 and buttons[0].text=='📢 New Updates ✨'
    assert buttons[0].url.endswith('invite104')
    assert 'Join 1 channel' in call.args[1]
    assert bot.get_chat.await_count==1


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_recorded_request_hidden_but_unrelated_channel_still_required(module):
    await seed([-101,-102,-103])
    await db.record_join_request(7,-101)
    await db.record_join_request(8,-102)
    await db.record_join_request(7,-999)
    await db.record_join_request(7,None)
    call=await prompt(module,fake_bot())
    assert [b.url for b in url_buttons(call)]==['https://t.me/+invite102','https://t.me/+invite103']


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_restricted_member_kept_distinct_from_restricted_nonmember(module):
    await seed([-101,-102])
    bot=fake_bot({-101:('restricted',True),-102:('restricted',False)})
    assert await module.missing_force_sub(bot,7)==[-102]


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_empty_requirements_pass_gate_and_no_join_buttons(module):
    await seed([])
    bot=fake_bot()
    assert await module.gate_ok(bot,7)
    assert not url_buttons(await prompt(module,bot))
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_member_check_error_preserves_existing_fail_open_policy(module):
    await seed([-101,-102])
    bot=fake_bot({-101:RuntimeError('not admin')})
    assert await module.missing_force_sub(bot,7)==[-102]
    assert [b.url for b in url_buttons(await prompt(module,bot))]==['https://t.me/+invite102']


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_duplicate_required_ids_do_not_duplicate_buttons(module):
    await seed([-101,-101,-102,-102])
    bot=fake_bot()
    call=await prompt(module,bot)
    assert len(url_buttons(call))==2 and bot.get_chat_member.await_count==2


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_category_override_not_combined_with_global_channels(module):
    await seed([-101,-102]); await seed([-201,-202],category='demo')
    call=await prompt(module,fake_bot({-201:'member'}),category='demo')
    assert [b.url for b in url_buttons(call)]==['https://t.me/+invite202']


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_missing_category_override_falls_back_to_global(module):
    await seed([-101,-102])
    await db._db.categories.update_one({'key':'demo'},{'$set':{'force_sub_channel_ids':None}})
    assert await module.missing_force_sub(fake_bot({-101:'member'}),7,'demo')==[-102]


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_membership_not_cached_after_user_leaves(module):
    await seed([-101])
    bot=fake_bot({-101:'member'})
    assert await module.gate_ok(bot,7)
    bot.get_chat_member.side_effect=None
    bot.get_chat_member.return_value=NS(status='left')
    assert not await module.gate_ok(bot,7)


async def test_title_cache_reused_but_membership_stays_live():
    await seed([-101])
    bot=fake_bot(titles={-101:'Cool Channel'})
    await prompt(bot1,bot); await prompt(bot1,bot)
    assert bot.get_chat.await_count==1 and bot.get_chat_member.await_count==2


async def test_title_cache_isolated_between_bots_and_bounded(monkeypatch):
    monkeypatch.setattr(force_sub,'MAX_TITLE_CACHE',2)
    a=fake_bot(titles={-101:'First Bot'},bot_id=11)
    b=fake_bot(titles={-101:'Second Bot'},bot_id=12)
    assert await force_sub.channel_title(a,-101)=='First Bot'
    assert await force_sub.channel_title(b,-101)=='Second Bot'
    await force_sub.channel_title(a,-102)
    assert len(force_sub._metadata)==2
    await force_sub.channel_title(a,-101)
    assert a.get_chat.await_count==3


async def test_cache_expiry_refreshes_renamed_channel(monkeypatch):
    now=[100.]
    monkeypatch.setattr(force_sub.time,'monotonic',lambda:now[0])
    bot=fake_bot(titles={-101:'Old'})
    assert await force_sub.channel_title(bot,-101)=='Old'
    bot.get_chat.side_effect=None;bot.get_chat.return_value=NS(title='New',username=None)
    now[0]=100+force_sub.TITLE_TTL+1
    assert await force_sub.channel_title(bot,-101)=='New'


async def test_explicit_title_refresh_used_by_admin_reflects_rename():
    bot=fake_bot(titles={-101:'Old'})
    await force_sub.channel_title(bot,-101)
    bot.get_chat.side_effect=None;bot.get_chat.return_value=NS(title='New',username=None)
    assert await force_sub.channel_title(bot,-101,refresh=True)=='New'


async def test_title_failure_uses_safe_id_fallback_without_removing_gate():
    await seed([-101])
    bot=fake_bot(titles={-101:RuntimeError('no access')})
    call=await prompt(bot1,bot)
    assert url_buttons(call)[0].text=='📢 Channel -101'
    assert not await bot1.gate_ok(bot,7)


async def test_failed_title_refresh_keeps_known_title():
    bot=fake_bot(titles={-101:'Known'})
    await force_sub.channel_title(bot,-101)
    bot.get_chat.side_effect=RuntimeError('temporary failure')
    assert await force_sub.channel_title(bot,-101,refresh=True)=='Known'


async def test_private_channel_stored_request_link_preferred_and_not_exported():
    await seed([-101])
    bot=fake_bot()
    assert url_buttons(await prompt(bot2,bot))[0].url=='https://t.me/+invite101'
    bot.export_chat_invite_link.assert_not_awaited()


async def test_public_channel_link_fallback_reuses_title_lookup():
    await seed([-101],links=False)
    bot=fake_bot();bot.get_chat.side_effect=None
    bot.get_chat.return_value=NS(title='Public Title',username='public_handle')
    buttons=url_buttons(await prompt(bot1,bot))
    assert buttons[0].url=='https://t.me/public_handle'
    assert bot.get_chat.await_count==1
    bot.export_chat_invite_link.assert_not_awaited()


async def test_missing_link_does_not_silently_allow_access():
    await seed([-101],links=False)
    bot=fake_bot(titles={-101:'A & <B>'})
    bot.export_chat_invite_link.side_effect=RuntimeError('permission denied')
    call=await prompt(bot2,bot)
    assert not url_buttons(call)
    assert 'Invite unavailable' in call.args[1] and 'A &amp; &lt;B&gt;' in call.args[1]
    assert not await bot2.gate_ok(bot,7)


async def test_long_unicode_title_single_button_bounded_plain_text():
    await seed([-101])
    bot=fake_bot(titles={-101:'🔥<b>&\n'*60})
    call=await prompt(bot1,bot)
    label=url_buttons(call)[0].text
    assert len(label)<=60 and label.endswith('…') and '\n' not in label
    assert call.kwargs['parse_mode']=='HTML'


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_supplied_membership_snapshot_does_not_repeat_checks(module):
    await seed([-101])
    bot=fake_bot()
    await prompt(module,bot,missing=[-101])
    bot.get_chat_member.assert_not_awaited()


async def test_bot1_real_download_gate_single_snapshot_and_callback(monkeypatch):
    await seed([-101,-102])
    await db._db.files.insert_one({'file_id':'demo_f1','category':'demo','db_message_id':1})
    bot=fake_bot({-101:'member'})
    deliver=AsyncMock();monkeypatch.setattr(bot1,'deliver_now',deliver)
    await bot1.process_file(bot,7,7,'demo_f1')
    assert bot.get_chat_member.await_count==2
    call=bot.send_message.call_args
    assert len(url_buttons(call))==1
    assert call.kwargs['reply_markup'].inline_keyboard[-1][0].callback_data=='checksub:demo_f1'
    deliver.assert_not_awaited()


async def test_bot1_partial_join_callback_edits_prompt_without_delivering(monkeypatch):
    await seed([-101,-102,-103]);await db.record_join_request(7,-102)
    bot=fake_bot({-101:'member'})
    q=query();process=AsyncMock();monkeypatch.setattr(bot1,'process_file',process)
    await bot1.on_checksub(NS(callback_query=q),NS(bot=bot))
    call=q.edit_message_text.call_args
    assert [b.url for b in url_buttons(call)]==['https://t.me/+invite103']
    process.assert_not_awaited()
    assert q.answer.call_args.kwargs['show_alert'] is True


async def test_bot1_all_joined_callback_resumes_same_file(monkeypatch):
    await seed([-101]);bot=fake_bot({-101:'member'});q=query()
    resume=AsyncMock();monkeypatch.setattr(bot1,'process_file',resume)
    await bot1.on_checksub(NS(callback_query=q),NS(bot=bot))
    resume.assert_awaited_once_with(bot,7,7,'demo_f1')


async def test_deleted_prompt_refresh_sends_new_filtered_message():
    await seed([-101]);bot=fake_bot();q=query()
    q.edit_message_text.side_effect=BadRequest('Message to edit not found')
    await bot1.send_force_sub(bot,7,'demo_f1',query=q)
    assert len(url_buttons(bot.send_message.call_args))==1


async def test_unchanged_prompt_edit_does_not_spam_new_message():
    await seed([-101]);bot=fake_bot();q=query()
    q.edit_message_text.side_effect=BadRequest('Message is not modified')
    await bot1.send_force_sub(bot,7,'demo_f1',query=q)
    bot.send_message.assert_not_awaited()


async def test_bot2_start_preserves_pending_delivery_and_original_token_unused(monkeypatch):
    await seed([-101,-102])
    await db._db.files.insert_one({'file_id':'demo_f1','category':'demo','db_message_id':1})
    bot=fake_bot({-101:'member'});deliver=AsyncMock()
    monkeypatch.setattr(bot2,'process_delivery',deliver)
    update=NS(effective_user=NS(id=7),message=NS(reply_text=AsyncMock()))
    await bot2.start(update,NS(bot=bot,args=['deliver_demo_f1_original']))
    assert (await db.get_pending_delivery(7))['file_id']=='demo_f1'
    assert bot.get_chat_member.await_count==2
    assert [b.url for b in url_buttons(bot.send_message.call_args)]==['https://t.me/+invite102']
    assert bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard[-1][0].callback_data=='sub2:g'
    deliver.assert_not_awaited()


async def test_bot2_partial_join_callback_keeps_pending_file_and_refreshes(monkeypatch):
    await seed([-101,-102]);await db.set_pending_delivery(7,'demo_f1')
    await db._db.files.insert_one({'file_id':'demo_f1','category':'demo','db_message_id':1})
    bot=fake_bot({-101:'member'});q=query(data='sub2:g')
    resume=AsyncMock();monkeypatch.setattr(bot2,'resume_pending_delivery',resume)
    await bot2.on_checksub2(NS(callback_query=q),NS(bot=bot))
    assert (await db.get_pending_delivery(7))['file_id']=='demo_f1'
    assert [b.url for b in url_buttons(q.edit_message_text.call_args)]==['https://t.me/+invite102']
    resume.assert_not_awaited()


async def test_bot2_all_satisfied_callback_resumes_pending_file_with_existing_flow(monkeypatch):
    await seed([-101]);await db.record_join_request(7,-101)
    await db.set_pending_delivery(7,'demo_f1')
    await db._db.files.insert_one({'file_id':'demo_f1','category':'demo','db_message_id':1})
    bot=fake_bot();q=query(data='sub2:g');resume=AsyncMock()
    monkeypatch.setattr(bot2,'resume_pending_delivery',resume)
    await bot2.on_checksub2(NS(callback_query=q),NS(bot=bot))
    assert await db.get_pending_delivery(7) is None
    resume.assert_awaited_once_with(bot,7,7,'demo_f1')


async def test_bot2_bare_start_filters_and_no_pending_file_created():
    await seed([-101,-102]);bot=fake_bot({-101:'member'})
    update=NS(effective_user=NS(id=7),message=NS(reply_text=AsyncMock()))
    await bot2.start(update,NS(bot=bot,args=[]))
    assert [b.url for b in url_buttons(bot.send_message.call_args)]==['https://t.me/+invite102']
    assert await db.get_pending_delivery(7) is None


async def test_actual_join_request_handlers_store_channel_scoped_records():
    for module,channel in [(bot1,-101),(bot2,-102)]:
        await module.on_join_request(NS(chat_join_request=NS(from_user=NS(id=7),chat=NS(id=channel))),NS())
    assert await db.has_join_request(7,-101) and await db.has_join_request(7,-102)
    assert not await db.has_join_request(7,-103)


def admin_update(uid=999):
    message=NS(reply_text=AsyncMock())
    return NS(effective_user=NS(id=uid),message=message,effective_message=message)


async def test_admin_list_shows_titles_ids_and_both_scopes_with_html_escaping():
    await seed([-101]);await seed([-201],category='demo')
    bot=fake_bot(titles={-101:'News & <Updates>',-201:'Anime 日本語 🎬'})
    update=admin_update()
    await admin.cmd_forcesublist(update,NS(bot=bot,args=[]))
    text='\n'.join(c.args[0] for c in update.message.reply_text.call_args_list)
    assert 'News &amp; &lt;Updates&gt;' in text and 'Anime 日本語 🎬' in text
    assert 'Global' in text and '[demo]' in text
    assert '<code>-101</code>' in text and '<code>-201</code>' in text
    assert '/forcesubremove' in text
    assert all(c.kwargs['parse_mode']=='HTML' for c in update.message.reply_text.call_args_list)
    bot.create_chat_invite_link.assert_not_awaited()


async def test_admin_list_backfills_missing_request_link_in_database():
    await seed([-101],links=False)
    bot=fake_bot();update=admin_update()
    await admin.cmd_forcesublist(update,NS(bot=bot,args=[]))
    assert await db.force_sub_link(-101)=='https://t.me/+created'
    assert bot.create_chat_invite_link.call_args.kwargs['creates_join_request'] is True


async def test_admin_list_does_not_crash_if_metadata_and_invite_are_unavailable():
    await seed([-101],links=False)
    bot=fake_bot(titles={-101:RuntimeError('no access')})
    bot.create_chat_invite_link.side_effect=RuntimeError('secret must not appear')
    update=admin_update();await admin.cmd_forcesublist(update,NS(bot=bot,args=[]))
    text=update.message.reply_text.call_args.args[0]
    assert 'Channel -101' in text and 'Unavailable' in text
    assert 'secret must not appear' not in text


async def test_admin_title_is_refreshed_not_permanently_cached():
    await seed([-101]);bot=fake_bot(titles={-101:'Old'})
    await force_sub.channel_title(bot,-101)
    bot.get_chat.side_effect=None;bot.get_chat.return_value=NS(title='Renamed',username=None)
    update=admin_update();await admin.cmd_forcesublist(update,NS(bot=bot,args=[]))
    assert 'Renamed' in update.message.reply_text.call_args.args[0]


async def test_admin_list_paginates_whole_entries_under_telegram_limit():
    channels=list(range(-101,-131,-1));await seed(channels)
    bot=fake_bot(titles={c:'🔥<&'*85 for c in channels});update=admin_update()
    await admin.cmd_forcesublist(update,NS(bot=bot,args=[]))
    pages=update.message.reply_text.call_args_list
    assert len(pages)>1
    all_text='\n'.join(c.args[0] for c in pages)
    for c in pages:
        assert len(c.args[0].encode('utf-16-le'))//2<=3800
        assert c.args[0].count('<b>')==c.args[0].count('</b>')
        assert c.args[0].count('<a ')==c.args[0].count('</a>')
    for cid in channels: assert f'<code>{cid}</code>' in all_text


@pytest.mark.parametrize('module',[bot1,bot2])
async def test_newly_added_channel_detected_after_previous_gate_pass(module):
    await seed([-101,-102,-103])
    bot=fake_bot({-101:'member',-102:'member',-103:'member'})
    assert await module.gate_ok(bot,7)
    await seed([-101,-102,-103,-104])
    assert await module.missing_force_sub(bot,7)==[-104]
    call=await prompt(module,bot)
    assert [b.url for b in url_buttons(call)]==['https://t.me/+invite104']


async def test_real_bot2_join_callback_validates_new_token_copies_and_marks_used(monkeypatch):
    await seed([-101])
    await db._db.files.insert_one({'file_id':'demo_f1','category':'demo',
        'db_message_id':1,'videos':[{'db_message_id':10,'caption':'Original'}], 'srts':[]})
    bot=fake_bot();bot.copy_message=AsyncMock(return_value=NS(message_id=500))
    for name,value in [('_db_channel_for',-900),('_autodelete_minutes',0),
                       ('_protect_flag',True),('_withfile_template',None)]:
        monkeypatch.setattr(bot2,name,AsyncMock(return_value=value))
    original=await db.create_token(7,'demo_f1',10,kind='deliver')
    update=NS(effective_user=NS(id=7),message=NS(reply_text=AsyncMock()))
    await bot2.start(update,NS(bot=bot,args=[f'deliver_demo_f1_{original}']))
    assert (await db.get_pending_delivery(7))['file_id']=='demo_f1'
    bot.copy_message.assert_not_awaited()
    await db.record_join_request(7,-101)
    await bot2.on_checksub2(NS(callback_query=query(data='sub2:g')),NS(bot=bot))
    assert await db.get_pending_delivery(7) is None
    assert (await db.get_token(original))['used'] is False
    assert await db._db.tokens.count_documents({'user_id':7,'kind':'deliver','used':True})==1
    bot.copy_message.assert_awaited_once()
    assert bot.copy_message.call_args.kwargs['from_chat_id']==-900
    assert bot.copy_message.call_args.kwargs['message_id']==10


async def test_bot1_postverification_gate_uses_filtered_list_before_handoff(monkeypatch):
    await seed([-101,-102])
    await db._db.files.insert_one({'file_id':'demo_f1','category':'demo','db_message_id':1})
    token=await db.create_token(7,'demo_f1',10,kind='verify')
    bot=fake_bot({-101:'member'});handoff=AsyncMock()
    monkeypatch.setattr(bot1,'deliver_now',handoff)
    await bot1.process_verify(bot,7,7,'demo_f1',token,via_gate=True)
    assert [b.url for b in url_buttons(bot.send_message.call_args)]==['https://t.me/+invite102']
    assert (await db.get_token(token))['used'] is False
    handoff.assert_not_awaited()


async def test_nonadmin_cannot_access_title_list():
    await seed([-101]);bot=fake_bot();update=admin_update(uid=123)
    await admin.cmd_forcesublist(update,NS(bot=bot,args=[]))
    bot.get_chat.assert_not_awaited()
    assert 'admin' in update.message.reply_text.call_args.args[0].lower()
