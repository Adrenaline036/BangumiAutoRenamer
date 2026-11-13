"""显示快照隔离、请求预算与当前持久恢复入口的边界验证。"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
from nicegui import Client
from nicegui.page import page
from src.pages import data_table_page as dashboard
from src.rename.provider_transport import BangumiTransport, ProviderRequestError
from src.rename.provider_search import BangumiProviderClient, resolve_provider_item_url
from src.rename.provider_operation import provider_operation
from src.rename.provider_recovery import can_refresh_selection


def response(status=200, payload=None, retry_after=None):
    result = Mock(status_code=status, headers={'Retry-After': retry_after})
    result.json.return_value = payload if payload is not None else {'id': 1}
    result.raise_for_status.side_effect = requests.HTTPError('private URL token') if status >= 400 else None
    return result


class TransportContracts(unittest.TestCase):
    def transport(self, replies, *, budget=20):
        session = Mock()
        session.get.side_effect = replies
        times, sleeps = [0.0], []
        def sleep(value):
            times[0] += value
            sleeps.append(value)
        transport = BangumiTransport(session, {}, sleeper=sleep, clock=lambda: times[0],
            wall_clock=lambda: datetime(2026, 10, 28, tzinfo=timezone.utc), request_budget=budget)
        return transport, session, times, sleeps

    def test_retry_after_date_seconds_past_invalid_and_budget(self):
        for value, minimum, count in [('3',3,2), ('Wed, 28 Oct 2026 00:00:04 GMT',4,2),
                ('Tue, 27 Oct 2026 00:00:00 GMT',.5,2), ('bad',.5,2), ('30',0,1)]:
            with self.subTest(value=value):
                transport, session, _, sleeps = self.transport([response(429,retry_after=value),response()])
                if count == 1:
                    with self.assertRaises(ProviderRequestError):
                        transport.request('get','/v0/subjects/1',stage='detail')
                else:
                    transport.request('get','/v0/subjects/1',stage='detail')
                self.assertEqual(count, session.get.call_count)
                self.assertGreaterEqual(sum(sleeps),minimum)

    def test_nontransient_errors_and_diagnostics_do_not_expose_exception(self):
        for status, code in [(404,'not_found'),(403,'unauthorized'),(400,'request_failed')]:
            transport, session, _, sleeps = self.transport([response(status)])
            with self.assertRaises(ProviderRequestError) as raised:
                transport.request('get','/v0/subjects/1',stage='detail')
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(code, raised.exception.diagnostic['code'])
            self.assertEqual(1,session.get.call_count)
            self.assertFalse(sleeps)
            self.assertNotIn('private',str(raised.exception)+json.dumps(raised.exception.diagnostic))
        invalid=response()
        invalid.json.side_effect=ValueError('private response body')
        transport, session, _, _=self.transport([invalid])
        with self.assertRaises(ProviderRequestError) as raised:
            transport.request('get','/v0/subjects/1',stage='detail')
        self.assertEqual('invalid_response',raised.exception.diagnostic['code'])
        self.assertEqual(1,session.get.call_count)

    def test_all_three_bangumi_entrypoints_use_same_transport(self):
        session=Mock()
        session.post.side_effect=[requests.ReadTimeout('secret'),response(payload={'data':[]})]
        sleeps=[]
        client=BangumiProviderClient(user_agent='local-test',session=session,sleeper=sleeps.append)
        self.assertEqual([],client.search_animation('test'))
        self.assertEqual(2,session.post.call_count)
        self.assertEqual([.5],sleeps)
        session.get.side_effect=[requests.ReadTimeout(),response(payload={'id':1,'name':'作品','type':2})]
        with patch('src.rename.provider_transport.time.sleep'):
            # 手工链接实际走同一请求函数；替换默认睡眠以免真实等待。
            with patch.object(BangumiTransport, 'request', autospec=True,
                              return_value={'id':1,'name':'作品','type':2}) as request:
                result=resolve_provider_item_url('https://bgm.tv/subject/1',
                    bangumi_user_agent='local-test',session=session)
                self.assertEqual('1',result['provider_id'])
                self.assertEqual('detail',request.call_args.kwargs['stage'])
        session.get.side_effect=[requests.ReadTimeout(),response()]
        self.assertEqual({'id':1},client._get_json('/v0/subjects/1'))
        self.assertEqual(2,session.get.call_count)

    def test_relation_budget_covers_cached_and_uncached_reads_and_no_partial_success(self):
        session=Mock()
        client=BangumiProviderClient(user_agent='local-test',session=session,sleeper=lambda _:None)
        times=[0.0]
        client._transport.clock=lambda:times[0]
        client._transport.relation_budget=2
        def fetch(*args,**kwargs):
            times[0]+=1.1
            return response(payload={'id':1,'name':'作品','platform':'TV'})
        session.get.side_effect=fetch
        with self.assertRaises(ProviderRequestError) as raised:
            client.load_crosswalk_evidence('1')
        self.assertEqual('budget_exhausted',raised.exception.diagnostic['code'])
        self.assertEqual(2,session.get.call_count)
        self.assertIsNone(client._transport.deadline.get())
        session.get.side_effect=None
        session.get.return_value=response()
        @provider_operation
        def cached():
            with client._transport.operation():
                client._get_json('/v0/subjects/1')
                times[0]+=3
                client._get_json('/v0/subjects/1')
        with self.assertRaises(ProviderRequestError):
            cached()

    def test_failures_are_not_cached_success_is_operation_scoped(self):
        session=Mock()
        session.get.side_effect=[response(404),response(),response()]
        client=BangumiProviderClient(user_agent='local-test',session=session,sleeper=lambda _:None)
        @provider_operation
        def work():
            with self.assertRaises(ProviderRequestError):
                client._get_json('/v0/subjects/1')
            client._get_json('/v0/subjects/1')
            client._get_json('/v0/subjects/1')
        work()
        self.assertEqual(2,session.get.call_count)
        client._get_json('/v0/subjects/1')
        self.assertEqual(3,session.get.call_count)

    def test_retry_policy_rejects_stale_or_nonretryable_context(self):
        base=dict(lifecycle='failed',candidate={'provider_id':'1'},retryable=True,
                  failure_code='provider_unavailable',retry_requires_refresh=False)
        self.assertTrue(can_refresh_selection(base))
        for changes in [dict(lifecycle='legacy_stale'),dict(lifecycle='pending'),
                        dict(retryable=False),dict(candidate=None),dict(failure_code='stale_request')]:
            self.assertFalse(can_refresh_selection({**base,**changes}))


class DisplayContracts(unittest.IsolatedAsyncioTestCase):
    async def test_only_visible_card_data_is_retained_and_heavy_changes_do_not_render(self):
        heavy={'uuid':'one','workflow_status':'need_confirm','name':'作品',
               'task_source_scope':{'files':['large']*10000},'source_group_observation':{'huge':'x'*10000}}
        before=deepcopy(heavy)
        projected=dashboard._card_projection(heavy)
        self.assertLess(len(json.dumps(projected)),2000)
        self.assertEqual(before,heavy)
        signature=dashboard.dashboard_signature([heavy])
        heavy['task_source_scope']['files'].append('new')
        self.assertEqual(signature,dashboard.dashboard_signature([heavy]))
        heavy['name']='新作品'
        self.assertNotEqual(signature,dashboard.dashboard_signature([heavy]))

    async def test_paging_differential_updates_order_and_clients_are_independent(self):
        rows=[dict(uuid=f't{i}',workflow_status='need_confirm',name=f'作品{i}') for i in range(45)]
        clients=[Client(page('/independent-'+str(i)),request=None) for i in range(2)]
        for client in clients:
            self.addCleanup(client.delete)
            self.addCleanup(dashboard._dashboard_views.pop,client.id,None)
            with client,patch.object(dashboard,'load_task_rows',return_value=rows), \
                    patch.object(dashboard,'_read_card_decision',return_value=None):
                await dashboard.create_table()
        first=dashboard._dashboard_views[clients[0].id]
        second=dashboard._dashboard_views[clients[1].id]
        section=first.sections['review']
        before=dict(section.cards)
        self.assertEqual(20,len(before))
        with clients[0],patch.object(dashboard,'load_task_rows',return_value=rows), \
                patch.object(dashboard,'_read_card_decision',return_value=None):
            rows[1]=dict(rows[1],name='修改标题')
            await dashboard.refresh_dashboard()
            self.assertIs(before['t0'][1],section.cards['t0'][1])
            self.assertNotEqual(before['t1'][0],section.cards['t1'][0])
            self.assertEqual(before['t1'][0],second.sections['review'].cards['t1'][0])
            section.change_page(1)
            self.assertIn('t20',section.cards)
            self.assertEqual(1,second.sections['review'].page)
            section.change_page(1)
            self.assertEqual(5,len(section.cards))
            rows.pop()
            await dashboard.refresh_dashboard()
            self.assertEqual(4,len(section.cards))

    async def test_loading_and_failure_preserve_content_and_allow_local_navigation(self):
        client=Client(page('/loading-contract'),request=None)
        self.addCleanup(client.delete)
        self.addCleanup(dashboard._dashboard_views.pop,client.id,None)
        rows=[dict(uuid='one',workflow_status='need_confirm',name='作品')]
        with client,patch.object(dashboard,'load_task_rows',return_value=rows), \
                patch.object(dashboard,'_read_card_decision',return_value=None):
            task=dashboard.create_table()
            view=dashboard._dashboard_views[client.id]
            self.assertEqual('正在读取任务列表…',view.status_text.text)
            self.assertTrue(view.progress.visible)
            await task
            old=dict(view.sections['review'].cards)
            with patch.object(dashboard,'_prepare_dashboard',side_effect=PermissionError()),patch.object(dashboard,'notify'):
                await dashboard.refresh_dashboard(force=True)
            self.assertTrue(view.retry.visible)
            self.assertFalse(view.progress.visible)
            self.assertEqual(old,view.sections['review'].cards)
            with patch.object(dashboard,'load_task_rows',side_effect=AssertionError('切页不应读盘')):
                view.sections['review'].change_page(1)
            await dashboard.refresh_dashboard(force=True)
            self.assertFalse(view.status.visible)
