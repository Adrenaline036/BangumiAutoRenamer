"""Operation-local cost boundaries, not real-media acceptance."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import subprocess
from threading import Barrier, Lock
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import tmdbsimple
from src.rename.provider_operation import provider_operation, successful_json, tmdb_info
from src.rename.provider_search import BangumiProviderClient
from src.utils import task_changes
from src.utils.utils import _atomic_replace_task_bytes


class ProviderOperationTests(unittest.TestCase):
    def test_reuse_detaches_mutation_and_next_operation_is_fresh(self):
        fetch = Mock(return_value={'items': [1]})
        @provider_operation
        def operation():
            value = successful_json(('endpoint', 'auth'), fetch)
            value['items'].append(2)
            return successful_json(('endpoint', 'auth'), fetch)
        self.assertEqual({'items': [1]}, operation())
        self.assertEqual(1, fetch.call_count)
        operation()
        self.assertEqual(2, fetch.call_count)
        successful_json(('endpoint', 'auth'), fetch)
        successful_json(('endpoint', 'auth'), fetch)
        self.assertEqual(4, fetch.call_count)

    def test_errors_are_not_cached_and_worker_threads_do_not_share(self):
        fetch = Mock(side_effect=[ValueError('bad JSON'), {'ok': True}])
        @provider_operation
        def operation():
            with self.assertRaises(ValueError):
                successful_json('key', fetch)
            self.assertEqual({'ok': True}, successful_json('key', fetch))
        operation()
        barrier = Barrier(2)
        @provider_operation
        def worker(value):
            barrier.wait(timeout=5)
            return successful_json('same', lambda: value)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual([1, 2], list(pool.map(worker, [1, 2])))

    def test_real_tmdb_client_info_preserves_attributes_and_request_keys(self):
        response = Mock()
        response.json.return_value = {'id': 17, 'name': 'Example', 'genres': []}
        @provider_operation
        def operation():
            first, second = tmdbsimple.TV(17), tmdbsimple.TV(17)
            before = request.call_count
            tmdb_info(first, language='zh-CN')['genres'].append({'id': 16})
            self.assertEqual([], tmdb_info(second, language='zh-CN')['genres'])
            self.assertEqual('Example', second.name)
            self.assertEqual(before + 1, request.call_count)
            tmdb_info(tmdbsimple.TV(17), language='en-US')
            tmdb_info(tmdbsimple.TV(18), language='zh-CN')
            with patch.object(tmdbsimple, 'API_KEY', 'other-disposable-key'):
                tmdb_info(tmdbsimple.TV(17), language='zh-CN')
            self.assertEqual(before + 4, request.call_count)
        with patch.object(tmdbsimple, 'API_KEY', 'disposable-key'), \
                patch.object(tmdbsimple, 'REQUESTS_SESSION', None), \
                patch('requests.request', return_value=response) as request:
            operation()
            operation()

    def test_bangumi_success_only_and_token_separation(self):
        session = Mock()
        session.get.return_value.json.return_value = {'id': 1}
        client = BangumiProviderClient(user_agent='test', session=session)
        @provider_operation
        def operation():
            client._get_json('/subjects/1')
            client._get_json('/subjects/1')
            self.assertEqual(1, session.get.call_count)
            client._token = 'different-disposable-token'
            client._get_json('/subjects/1')
        operation()
        self.assertEqual(2, session.get.call_count)
        session.get.side_effect = requests.ConnectionError('offline')
        @provider_operation
        def errors():
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    client._get_json('/subjects/2')
        errors()
        self.assertEqual(6, session.get.call_count)  # Existing two-attempt policy remains.






class TaskChangeTests(unittest.TestCase):
    def test_no_io_when_quiet_and_each_client_observes_changes(self):
        a, b = Mock(), Mock()
        first, second = task_changes.DashboardRefresh(a), task_changes.DashboardRefresh(b)
        for _ in range(10):
            self.assertFalse(first.poll())
        a.assert_not_called()
        task_changes.mark_changed()
        self.assertTrue(first.poll())
        self.assertTrue(second.poll())
        self.assertFalse(first.poll())
        self.assertFalse(second.poll())
        self.assertEqual(1, a.call_count)
        self.assertEqual(1, b.call_count)

    def test_notification_only_after_successful_durable_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'task.json'
            before = task_changes.revision()
            _atomic_replace_task_bytes(path, b'{}')
            self.assertEqual(before + 1, task_changes.revision())
            with patch('src.utils.utils._replace_task_with_retry', side_effect=OSError('fixture failure')):
                with self.assertRaises(OSError):
                    _atomic_replace_task_bytes(path, b'null')
            self.assertEqual(before + 1, task_changes.revision())
            self.assertEqual(b'{}', path.read_bytes())
