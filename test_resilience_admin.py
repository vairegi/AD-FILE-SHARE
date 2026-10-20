"""Offline tests for DB-only admin listing and resilient outbound requests.
No production credentials, Telegram calls, or provider network calls.
Run: python3 -m unittest test_resilience_admin -v
"""
import asyncio
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from mongomock_motor import AsyncMongoMockClient
import config
import db
import bot1_admin as admin
import shortener
import linkguard
import outbound

class ConfigTests(unittest.TestCase):
    def test_accepted_separators_and_no_concatenation(self):
        for value in ('111,222', '111 222', '111\n222', '111;222', '[111,222]', '["111", "222"]'):
            self.assertEqual(config._int_list(value), [111, 222], value)
        self.assertEqual(config._int_list('111, junk, 111, 222'), [111,222])

class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_db, self.old_env = db._db, config.ADMIN_IDS
        self.mongo = AsyncMongoMockClient()
        db._db = self.mongo['offline_admin_test']
        config.ADMIN_IDS = [111]
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.update = SimpleNamespace(message=self.message, effective_message=self.message,
            effective_chat=SimpleNamespace(id=1), effective_user=SimpleNamespace(id=111))
        self.bot = SimpleNamespace(_post=AsyncMock(return_value={}), get_chat=AsyncMock(
            return_value=SimpleNamespace(full_name='Resolved Title', username='resolved')))
        self.context = SimpleNamespace(bot=self.bot, args=[])

    async def asyncTearDown(self):
        db._db, config.ADMIN_IDS = self.old_db, self.old_env
        self.mongo.close()

    async def seed(self, uid=222):
        await db.add_db_admin(uid)
        await db._db.users.update_one({'user_id':uid}, {'$set':{'name':'A & <B> 😄'}}, upsert=True)

    async def test_env_overlap_hidden_in_database_and_authority_retained(self):
        await self.seed(111)
        await self.seed(222)
        self.assertEqual([r['user_id'] for r in await db.list_admins_full()], [222])
        self.assertTrue(await db.is_db_admin(111))
        from utils import is_admin
        self.assertTrue(await is_admin(111))

    async def test_renderer_defense_even_if_db_helper_returns_env_rows(self):
        rows = [{'user_id':111,'source':'DB','name':'Hidden'},
                {'user_id':333,'source':'ENV','name':'Hidden too'},
                {'user_id':222,'source':'DB','name':'Visible','added_at':0}]
        with patch.object(db,'list_admins_full',AsyncMock(return_value=rows)):
            await admin._send_admin_table(self.update,self.context)
        cells=self.bot._post.call_args.kwargs['data']['rich_message']['blocks'][2]['cells']
        self.assertEqual(len(cells),2)
        self.assertEqual(cells[1][1]['text']['url'],'tg://user?id=222')
        self.assertEqual(cells[1][1]['text']['text'],'Visible')

    async def test_profile_title_and_safe_html_fallback(self):
        await self.seed()
        self.bot._post.side_effect=RuntimeError('simulated rich rejection')
        await admin._send_admin_table(self.update,self.context)
        text=self.message.reply_text.call_args.args[0]
        self.assertIn('<a href="tg://user?id=222">A &amp; &lt;B&gt; 😄</a>',text)
        self.assertIn('DB only',text)
        self.assertNotIn('111',text)
        self.assertNotIn('<pre>',text)

    async def test_empty_list_and_permissions(self):
        await admin.cmd_addadmin(self.update,self.context)
        self.assertIn('No bot-added admins',self.message.reply_text.call_args.args[0])
        self.update.effective_user=SimpleNamespace(id=999)
        await admin.cmd_addadmin(self.update,self.context)
        self.assertIn('only by admins',self.message.reply_text.call_args.args[0])

    async def test_add_and_remove_unchanged(self):
        self.context.args=['222']
        await admin.cmd_addadmin(self.update,self.context)
        self.assertTrue(await db.is_db_admin(222))
        await admin.cmd_radmin(self.update,self.context)
        self.assertFalse(await db.is_db_admin(222))
        self.context.args=['111']
        await admin.cmd_radmin(self.update,self.context)
        self.assertIn('ADMIN_IDS',self.message.reply_text.call_args.args[0])

    async def test_pagination_keeps_all_db_admins(self):
        for uid in range(1000,1031):
            await self.seed(uid)
        await admin._send_admin_table(self.update,self.context)
        sizes=[len(c.kwargs['data']['rich_message']['blocks'][2]['cells'])-1
               for c in self.bot._post.call_args_list]
        self.assertEqual(sizes,[15,15,1])

class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await outbound.close()
        self.fake_key='OFFLINE_SECRET_KEY'
        self.destination='https://telegram.example/start?token=PRIVATE_DESTINATION'

    async def asyncTearDown(self):
        await outbound.close()

    async def with_transport(self, handler):
        outbound._client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        outbound._loop=asyncio.get_running_loop()
        outbound._slots=asyncio.Semaphore(outbound.MAX_IN_FLIGHT)

    async def test_timeout_retried_and_recovers_no_empty_error(self):
        requests=[]
        def handler(request):
            requests.append(request)
            if len(requests)==1:
                raise httpx.ReadTimeout('',request=request)
            return httpx.Response(200,text='https://provider.example/short')
        await self.with_transport(handler)
        with patch.object(shortener.asyncio,'sleep',AsyncMock()), self.assertLogs('shortener',level='WARNING') as logs:
            result=await shortener._shorten_with('https://provider.example/api',self.fake_key,self.destination)
        self.assertEqual(result,'https://provider.example/short')
        self.assertEqual(len(requests),2)
        self.assertIn('ReadTimeout','\n'.join(logs.output))
        self.assertNotIn(self.fake_key,'\n'.join(logs.output))
        self.assertNotIn('PRIVATE_DESTINATION','\n'.join(logs.output))

    async def test_read_connect_and_protocol_failures_have_bounded_attempts(self):
        for error in (httpx.ConnectTimeout,httpx.ReadTimeout,httpx.RemoteProtocolError):
            count=[]
            def handler(request):
                count.append(1)
                raise error('',request=request)
            await outbound.close()
            await self.with_transport(handler)
            with patch.object(shortener.asyncio,'sleep',AsyncMock()):
                result=await shortener._shorten_with('https://provider.example/api',self.fake_key,self.destination)
            self.assertIsNone(result)
            self.assertEqual(len(count),2)

    async def test_503_retry_recovers(self):
        calls=[]
        def handler(request):
            calls.append(1)
            return httpx.Response(503 if len(calls)==1 else 200,
                                  text='busy' if len(calls)==1 else 'https://provider.example/s')
        await self.with_transport(handler)
        with patch.object(shortener.asyncio,'sleep',AsyncMock()):
            self.assertEqual(await shortener._shorten_with('https://provider.example/api','k','https://dest.example'),
                             'https://provider.example/s')
        self.assertEqual(len(calls),2)

    async def test_auth_error_not_retried(self):
        calls=[]
        def handler(request):
            calls.append(1); return httpx.Response(401,text='unauthorized')
        await self.with_transport(handler)
        self.assertIsNone(await shortener._shorten_with('https://provider.example/api','k','https://dest.example'))
        self.assertEqual(len(calls),1)

    async def test_rate_limit_retry_after_respected(self):
        calls=[]
        def handler(request):
            calls.append(1)
            if len(calls)==1:
                return httpx.Response(429,headers={'Retry-After':'3'})
            return httpx.Response(200,text='https://provider.example/s')
        await self.with_transport(handler)
        with patch.object(shortener.asyncio,'sleep',AsyncMock()) as sleep:
            self.assertEqual(await shortener._shorten_with('https://provider.example/api','k','https://dest.example'),
                             'https://provider.example/s')
            sleep.assert_awaited_once_with(3.0)

    async def test_long_retry_after_skips_without_early_retry(self):
        calls=[]
        def handler(request):
            calls.append(1); return httpx.Response(429,headers={'Retry-After':'120'})
        await self.with_transport(handler)
        with patch.object(shortener.asyncio,'sleep',AsyncMock()) as sleep:
            self.assertIsNone(await shortener._shorten_with('https://provider.example/api','k','https://dest.example'))
            sleep.assert_not_awaited()
        self.assertEqual(len(calls),1)

    async def test_text_and_json_response_variants(self):
        variants=[b'https://provider.example/s', b'"https://provider.example/s"',
            b'{"status":"success","shortenedUrl":"https://provider.example/s"}',
            b'{"data":{"short_url":"https://provider.example/s"}}']
        for payload in variants:
            await outbound.close()
            await self.with_transport(lambda request: httpx.Response(200,content=payload))
            self.assertEqual(await shortener._shorten_with('https://provider.example/api','k','https://dest.example'),
                             'https://provider.example/s')

    async def test_200_html_errors_and_input_echo_not_success(self):
        for payload in ('<html>challenge</html>', '{"status":"error","message":"bad key"}',
                        'https://dest.example', 'https://provider.example/s garbage'):
            await outbound.close()
            await self.with_transport(lambda request: httpx.Response(200,text=payload))
            self.assertIsNone(await shortener._shorten_with('https://provider.example/api','k','https://dest.example'))

    async def test_round_robin_failover_and_result_contract_preserved(self):
        entries=[{'site':'a','api_base':'https://a.example/api','api_key':'a'},
                 {'site':'b','api_base':'https://b.example/api','api_key':'b'}]
        fake=AsyncMock(side_effect=[None,'https://b.example/s'])
        with patch.object(db,'active_shorteners',AsyncMock(return_value=entries)), \
             patch.object(db,'bump_rr_cursor',AsyncMock(return_value=0)), \
             patch.object(shortener,'_shorten_with',fake):
            self.assertEqual(await shortener.shorten('https://dest.example',with_status=True),
                             ('https://b.example/s','active','b',['a']))
        with patch.object(db,'active_shorteners',AsyncMock(return_value=entries)), \
             patch.object(db,'bump_rr_cursor',AsyncMock(return_value=0)), \
             patch.object(shortener,'_shorten_with',AsyncMock(return_value=None)):
            self.assertEqual(await shortener.shorten('https://dest.example',with_status=True),
                             (None,'down',None,['a','b']))

    async def test_shared_client_reused_and_closed(self):
        await self.with_transport(lambda request: httpx.Response(200,text='ok'))
        first=outbound.client()
        await outbound.request('GET','https://a.example')
        await outbound.request('GET','https://b.example')
        self.assertIs(first,outbound.client())
        await outbound.close()
        self.assertTrue(first.is_closed)
        self.assertIsNone(outbound._client)

    async def test_maximum_inflight_is_bounded(self):
        active=peak=0
        release=asyncio.Event()
        async def handler(request):
            nonlocal active,peak
            active+=1; peak=max(peak,active)
            await release.wait()
            active-=1
            return httpx.Response(200,text='ok')
        await self.with_transport(handler)
        tasks=[asyncio.create_task(outbound.request('GET','https://provider.example')) for _ in range(20)]
        for _ in range(100):
            await asyncio.sleep(0)
            if peak==outbound.MAX_IN_FLIGHT: break
        self.assertEqual(peak,outbound.MAX_IN_FLIGHT)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(peak,outbound.MAX_IN_FLIGHT)

    async def test_worker_timeout_retried_once(self):
        calls=[]
        def handler(request):
            calls.append(request)
            if len(calls)==1: raise httpx.ConnectTimeout('',request=request)
            return httpx.Response(200,json={'slug':'test','finish2_url':'https://worker.example/finish2?s=test'})
        await self.with_transport(handler)
        settings={'linkguard_base':'https://worker.example','linkguard_key':'offline'}
        with patch.object(db,'get_settings',AsyncMock(return_value=settings)), \
             patch.object(linkguard,'shortener_hosts',AsyncMock(return_value=[])), \
             patch.object(linkguard.asyncio,'sleep',AsyncMock()):
            result=await linkguard.mint_return_grant('https://dest.example')
        self.assertEqual(result['slug'],'test')
        self.assertEqual(len(calls),2)

    async def test_request_deadline_is_bounded(self):
        async def handler(request):
            await asyncio.Event().wait()
        await self.with_transport(handler)
        with patch.object(outbound,'REQUEST_DEADLINE',0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await outbound.request('GET','https://provider.example')

if __name__=='__main__':
    unittest.main(verbosity=2)
