"""Offline regression tests; no production credentials or external calls."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, patch
import bot2
from telegram.error import RetryAfter, BadRequest

class DeliveryTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.item={'file_id':'demo_f1','category':'demo','videos':[{'db_message_id':1,'caption':'a < b & c'}], 'srts':[]}
        self.token={'kind':'deliver','user_id':42,'file_id':'demo_f1','used':False,'expires_at':10**20}
        self.bot=SimpleNamespace(copy_message=AsyncMock(side_effect=self.copy),send_message=AsyncMock())
        self.count=0
        self.settings={}
        async def mark(token): self.token['used']=True
        pairs=[(bot2,'_db_channel_for',AsyncMock(return_value=-1001)),
               (bot2,'_autodelete_minutes',AsyncMock(return_value=15)),
               (bot2,'_protect_flag',AsyncMock(return_value=True)),
               (bot2,'_withfile_template',AsyncMock(return_value=None)),
               (bot2.db,'get_settings',AsyncMock(return_value=self.settings)),
               (bot2.db,'get_user',AsyncMock(return_value={})),
               (bot2.db,'get_item_by_file_id',AsyncMock(return_value=self.item)),
               (bot2.db,'get_token',AsyncMock(return_value=self.token)),
               (bot2.db,'premium_active',AsyncMock(return_value=False)),
               (bot2.db,'mark_token_used',AsyncMock(side_effect=mark)),
               (bot2.db,'add_deletion',AsyncMock()),
               (bot2.asyncio,'sleep',AsyncMock())]
        for target,name,value in pairs:
            p=patch.object(target,name,value); p.start(); self.addCleanup(p.stop)

    async def copy(self,**kw):
        self.count+=1
        return SimpleNamespace(message_id=1000+self.count)

    async def deliver(self):
        return await bot2.process_delivery(self.bot,42,42,'demo_f1','offline-token')

    def files(self,videos,srts=0):
        self.item['videos']=[{'db_message_id':i+1,'caption':'source'} for i in range(videos)]
        self.item['srts']=[{'db_message_id':videos+i+1} for i in range(srts)]

    async def test_single_video_and_used_token(self):
        self.assertEqual(await self.deliver(),1)
        self.assertTrue(self.token['used'])
        self.bot.copy_message.assert_awaited_once()
        self.assertFalse(hasattr(self.bot,'get_messages'))
        self.assertEqual(bot2.db.add_deletion.call_args.kwargs['bot'],'bot2')

    async def test_two_videos(self):
        self.files(2)
        self.assertEqual(await self.deliver(),2)
        self.assertEqual(self.bot.copy_message.await_count,2)

    async def test_three_videos_and_subtitle(self):
        self.files(3,1)
        self.assertEqual(await self.deliver(),4)
        self.assertEqual([c.kwargs['message_id'] for c in self.bot.copy_message.call_args_list],[1,2,3,4])
        self.assertEqual(bot2.db.add_deletion.await_count,4)
        self.assertEqual(self.bot.send_message.await_count,1)

    async def test_hundred_videos_paced_without_downloading(self):
        self.files(100)
        self.assertEqual(await self.deliver(),100)
        self.assertEqual(self.bot.copy_message.await_count,100)
        self.assertEqual(bot2.asyncio.sleep.await_count,99)

    async def test_real_retryafter_full_wait_not_capped(self):
        self.bot.copy_message.side_effect=[RetryAfter(65),SimpleNamespace(message_id=99)]
        self.assertEqual(await self.deliver(),1)
        bot2.asyncio.sleep.assert_awaited_once_with(66.0)

    async def test_timedelta_retryafter(self):
        error=RetryAfter(2); error.retry_after=timedelta(seconds=90)
        self.bot.copy_message.side_effect=[error,SimpleNamespace(message_id=99)]
        self.assertEqual(await self.deliver(),1)
        bot2.asyncio.sleep.assert_awaited_once_with(91.0)

    async def test_zero_files_keeps_token_reusable(self):
        self.bot.copy_message.side_effect=BadRequest('Chat not found')
        self.assertEqual(await self.deliver(),0)
        self.assertFalse(self.token['used'])
        self.assertNotIn('rate limits',self.bot.send_message.call_args.args[1])
        bot2.db.add_deletion.assert_not_awaited()
        self.bot.copy_message.side_effect=self.copy
        self.assertEqual(await self.deliver(),1)

    async def test_partial_failure_queues_success_and_reports_partial(self):
        self.files(3)
        self.bot.copy_message.side_effect=[SimpleNamespace(message_id=101),BadRequest('Message not found'),SimpleNamespace(message_id=103)]
        self.assertEqual(await self.deliver(),2)
        self.assertEqual(bot2.db.add_deletion.await_count,2)
        self.assertIn('Some files',self.bot.send_message.call_args_list[0].args[1])

    async def test_html_caption_escaped_protection_kept(self):
        self.settings['file_caption_extra_html']='<b>Extra</b>'
        await self.deliver()
        kw=self.bot.copy_message.call_args.kwargs
        self.assertEqual(kw['caption'],'a &lt; b &amp; c\n<b>Extra</b>')
        self.assertEqual(kw['parse_mode'],'HTML')
        self.assertTrue(kw['protect_content'])

    async def test_plain_caption_is_not_parsed_as_html(self):
        self.settings['file_caption_extra']='plain <text>'
        await self.deliver()
        self.assertNotIn('parse_mode',self.bot.copy_message.call_args.kwargs)

    async def test_custom_notice_preserved(self):
        bot2._withfile_template.return_value='Delete after {N Duration}'
        await self.deliver()
        self.assertEqual(self.bot.send_message.call_args.args[1],'Delete after 15 minutes')

    async def test_wrong_user_and_expired_tokens_rejected(self):
        self.token['user_id']=43
        await self.deliver()
        self.bot.copy_message.assert_not_awaited()
        self.token['user_id']=42; self.token['expires_at']=0
        await self.deliver()
        self.bot.copy_message.assert_not_awaited()

    async def test_used_token_not_replayed(self):
        await self.deliver(); await self.deliver()
        self.assertEqual(self.bot.copy_message.await_count,1)

    async def test_simultaneous_same_token_serialized(self):
        await asyncio.gather(self.deliver(),self.deliver())
        self.assertEqual(self.bot.copy_message.await_count,1)
        self.assertFalse(bot2._delivery_token_locks)

    async def test_real_rate_limit_exhaustion_bounded(self):
        self.bot.copy_message.side_effect=RetryAfter(2)
        self.assertEqual(await self.deliver(),0)
        self.assertEqual(self.bot.copy_message.await_count,4)
        self.assertEqual(bot2.asyncio.sleep.await_count,3)
        self.assertFalse(self.token['used'])

if __name__=='__main__': main(verbosity=2)
