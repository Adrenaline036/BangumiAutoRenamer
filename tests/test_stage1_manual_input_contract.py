"""人工输入与提交边界：执行源码原方法，仅用内存替换外部依赖。"""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass, replace
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
from typing import Sequence
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def source_objects(relative, names, namespace):
    """保留完整原定义和行号，不执行模块顶层单例或有副作用的导入。"""
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    selected = []
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in names:
            selected.append(node)
    if len(selected) != len(names):
        raise AssertionError(f'源码定义缺失：{relative} {names}')
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, str(path), 'exec', flags=__import__('__future__').annotations.compiler_flag), namespace)
    return namespace


class ConfigContract(unittest.TestCase):
    def manager(self, data):
        memory = {'text': json.dumps(data), 'writes': []}
        ns = source_objects('src/config/config_manager.py', {'CONFIG_DEFAULT', 'RETIRED_AI_CONFIG_KEYS', 'ConfigManager'},
                            {'contextmanager': __import__('contextlib').contextmanager,
                             'json': json, 'urlparse': urlparse,
                             'open': lambda *a, **k: io.StringIO(memory['text'])})
        obj = ns['ConfigManager'].__new__(ns['ConfigManager'])
        obj.config_path = NS(exists=lambda: True)
        obj._config_lock = nullcontext

        def write(value):
            memory['text'] = json.dumps(value)
            memory['writes'].append(dict(value))

        obj._write_unlocked = write
        return obj, memory, ns['CONFIG_DEFAULT']

    def test_manual_roots_survive_load_save_reload(self):
        for key in ('docker_mnt', 'bangumi_path', 'movie_path', 'anime_path', 'anime_movie_path'):
            for value in ('/media', '/media/Movies', '/media/中文 电影', '/elsewhere', '', 'Z:/offline'):
                with self.subTest(key=key, value=value):
                    obj, memory, _ = self.manager({key: value})
                    obj.update_config()
                    self.assertEqual(obj.config[key], value)
                    obj.write_config()
                    obj.update_config()
                    self.assertEqual(json.loads(memory['text'])[key], value)
                    self.assertEqual(obj.config[key], value)

    def test_set_configs_preserves_manual_roots(self):
        obj, memory, _ = self.manager({'movie_path': '/media/Movies'})
        self.assertTrue(obj.set_configs({'anime_path': '/media/Anime'}))
        self.assertEqual(json.loads(memory['text'])['movie_path'], '/media/Movies')
        self.assertEqual(obj.config['anime_path'], '/media/Anime')

    def test_defaults_optional_field_control(self):
        obj, _, defaults = self.manager({})
        obj.update_config()
        self.assertEqual(obj.config['docker_mnt'], defaults['docker_mnt'])
        self.assertNotIn('font_archive_enabled', obj.config)
        self.assertFalse(obj.set_config('unknown', 'x'))
        self.assertFalse(obj.set_configs({'font_archive_enabled': True}))
        self.assertNotIn('font_archive_enabled', obj.config)


class Button:
    def __init__(self):
        self.disabled = False

    def props(self, value='', *, remove=''):
        if value == 'disable':
            self.disabled = True
        if remove == 'disable':
            self.disabled = False


class DialogBase:
    def close(self):
        self.was_closed = True


class SubmitContract(unittest.IsolatedAsyncioTestCase):
    def make(self):
        calls, notices = [], []
        ns = source_objects('src/pages/add_task_page.py', {'media_flags', 'AddTaskDialog'},
                            {'ui': NS(dialog=DialogBase), 'Path': Path, 'Sequence': Sequence,
                             'asyncio': asyncio, 'is_ui_test_mode': lambda: False,
                             'notify': lambda text, **kw: notices.append((text, kw))})
        cls = ns['AddTaskDialog']
        dialog = cls.__new__(cls)
        dialog.__dict__.update(path=str(ROOT), input_paths=(), media_type='movie',
                               _submitting=False, _discovering=False, _generation=0, _closed=False,
                               is_deleted=False, submit_button=Button(), was_closed=False)
        dialog.discovery = NS(roots=(ROOT.resolve(),))

        async def io_bound(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        def prepare(observed, **kwargs):
            calls.append(('prepare', kwargs))
            return {'discovery': observed}

        async def enqueue(**kwargs):
            calls.append(('enqueue', kwargs))
            guard = kwargs.get('admission_guard')
            if guard and not guard():
                raise ValueError('输入已改变')
            if kwargs.get('on_admission_started'):
                kwargs['on_admission_started']()
            return ('abcdef123456',)

        ns['run'] = NS(io_bound=io_bound)
        ns['task_queue'] = NS(prepare_discovered_batch=prepare, enqueue_batch=enqueue)
        return dialog, ns, calls, notices

    async def test_success_and_double_click_control(self):
        d, ns, calls, _ = self.make()
        original = ns['task_queue'].prepare_discovered_batch
        async def io_bound(fn, *args, **kw):
            await d._submit()
            return original(*args, **kw)
        ns['run'].io_bound = io_bound
        await d._submit()
        self.assertTrue(d.was_closed)
        self.assertEqual(len([c for c in calls if c[0] == 'enqueue']), 1)
        self.assertEqual(calls[0][1]['is_movie'], True)

    async def test_each_input_change_during_prepare_rejects_and_restores(self):
        mutations = {'media': lambda d: setattr(d, 'media_type', 'anime'),
                     'path': lambda d: setattr(d, 'path', str(ROOT / 'src')),
                     'generation': lambda d: setattr(d, '_generation', 1),
                     'discovery': lambda d: setattr(d, 'discovery', NS(roots=(ROOT.resolve(),)))}
        for name, mutation in mutations.items():
            with self.subTest(input=name):
                d, ns, calls, _ = self.make()
                async def io_bound(fn, *args, **kw):
                    result = fn(*args, **kw)
                    mutation(d)
                    return result
                ns['run'].io_bound = io_bound
                await d._submit()
                self.assertFalse(any(c[0] == 'enqueue' for c in calls))
                self.assertFalse(d._submitting)
                self.assertFalse(d.submit_button.disabled)

    async def test_media_change_in_final_scan_rejects(self):
        for key, value in (('media_type', 'anime'),):
            with self.subTest(key=key):
                d, ns, _, _ = self.make()
                admitted = []
                async def enqueue(**kw):
                    setattr(d, key, value)
                    if kw['admission_guard']():
                        admitted.append(True)
                        return ('x',)
                    raise ValueError('输入已改变')
                ns['task_queue'].enqueue_batch = enqueue
                await d._submit()
                self.assertEqual(admitted, [])
                self.assertFalse(d.submit_button.disabled)

    async def test_prepare_failure_and_invalid_type_restore(self):
        for invalid_type in (False, True):
            with self.subTest(invalid_type=invalid_type):
                d, ns, _, _ = self.make()
                if invalid_type:
                    d.media_type = 'invalid'
                else:
                    ns['task_queue'].prepare_discovered_batch = lambda *a, **k: (_ for _ in ()).throw(ValueError('准备失败'))
                await d._submit()
                self.assertFalse(d._submitting)
                self.assertFalse(d.submit_button.disabled)
                self.assertFalse(d.was_closed)

    async def test_error_before_admission_allows_retry(self):
        d, ns, _, _ = self.make()
        async def enqueue(**kw):
            raise ValueError('核查失败，尚未接纳')
        ns['task_queue'].enqueue_batch = enqueue
        await d._submit()
        self.assertFalse(d._submitting)
        self.assertFalse(d.submit_button.disabled)

    async def test_error_after_admission_does_not_offer_duplicate(self):
        d, ns, _, notices = self.make()
        count = []
        async def enqueue(**kw):
            count.append(True)
            if kw.get('on_admission_started'):
                kw['on_admission_started']()
            raise OSError('保存结果不确定')
        ns['task_queue'].enqueue_batch = enqueue
        await d._submit()
        await d._submit()
        self.assertEqual(len(count), 1)
        self.assertTrue(d.submit_button.disabled)
        self.assertTrue(any('任务卡' in text for text, _ in notices))
        d.close()
        self.assertTrue(d.was_closed)

    async def test_cancellation_before_admission_restores_and_propagates(self):
        d, ns, _, _ = self.make()
        async def io_bound(*a, **kw):
            raise asyncio.CancelledError()
        ns['run'].io_bound = io_bound
        with self.assertRaises(asyncio.CancelledError):
            await d._submit()
        self.assertFalse(d._submitting)
        self.assertFalse(d.submit_button.disabled)

    async def test_cancellation_after_admission_blocks_duplicate(self):
        d, ns, _, notices = self.make()
        async def enqueue(**kw):
            if kw.get('on_admission_started'):
                kw['on_admission_started']()
            raise asyncio.CancelledError()
        ns['task_queue'].enqueue_batch = enqueue
        with self.assertRaises(asyncio.CancelledError):
            await d._submit()
        self.assertTrue(d.submit_button.disabled)
        self.assertTrue(any('任务卡' in text for text, _ in notices))

    async def test_close_during_prepare_does_not_admit(self):
        d, ns, calls, _ = self.make()
        async def io_bound(fn, *a, **kw):
            value = fn(*a, **kw)
            d.close()
            return value
        ns['run'].io_bound = io_bound
        await d._submit()
        self.assertFalse(any(c[0] == 'enqueue' for c in calls))

    async def test_empty_result_closes_without_repeat(self):
        d, ns, _, notices = self.make()
        async def enqueue(**kw):
            return ()
        ns['task_queue'].enqueue_batch = enqueue
        await d._submit()
        self.assertTrue(d.was_closed)
        self.assertTrue(any('无需创建任务' in text for text, _ in notices))


class QueueError(Exception):
    pass


@dataclass(frozen=True)
class MemoryJob:
    task_id: str
    source_group: object = None
    operation: str = 'rename'
    preview_only: bool = True
    path: object = None
    is_anime: object = False
    is_movie: object = True
    custom_name: object = None
    custom_season_id: object = None
    use_ai: object = None
    provider_selection: object = None
    source: str = 'ui'
    task_source_scope: object = None
    recognition_origin: object = None


class QueueContract(unittest.IsolatedAsyncioTestCase):
    def make(self, repair_ids=(), ids=('one',)):
        events, records = [], {}
        state = NS(valid=True, on_inspect=None, on_repair=None, on_start=None, on_validate=None)
        package = '_bar_stage1_memory'
        modules = {}
        for name in (package, package + '.rename', package + '.rename.input_directory_discovery',
                     package + '.rename.library_assets', package + '.rename.content_policy',
                     package + '.rename.automatic_recognition'):
            modules[name] = ModuleType(name)
        def validate(discovery):
            if state.on_validate:
                state.on_validate()
        modules[package + '.rename.input_directory_discovery'].__dict__.update(
            validate_discovery=validate, discovery_task_groups=lambda d: d.groups, SourceGroup=type('SourceGroup', (), {}))
        class Assets:
            def __init__(self, *a, **kw):
                pass
            def completed_source(self, request):
                return request.task_id if request.task_id in repair_ids else None
            def inspect_admission(self, existing):
                events.append('inspect')
                if state.on_inspect:
                    state.on_inspect()
                return NS(state='complete')
            def repair(self, existing):
                events.append('repair')
                if state.on_repair:
                    state.on_repair()
        modules[package + '.rename.library_assets'].LibraryAssets = Assets
        modules[package + '.rename.content_policy'].policy_for_admission = lambda queue, request: request
        modules[package + '.rename.automatic_recognition'].AutomaticRecognitionOrigin = type('Origin', (), {})
        async def io_bound(fn, *a, **kw):
            await asyncio.sleep(0)
            return fn(*a, **kw)
        ns = {'__name__': package + '.queue', '__package__': package, 'asyncio': asyncio,
              'run': NS(io_bound=io_bound), 'PlanExecutionError': QueueError, 'replace': replace,
              'safe_task_path': lambda *a: NS(is_file=lambda: False), 'utc_now': lambda: 'fixed',
              'restore_task_record_at': lambda *a: events.append('restore')}
        tree = ast.parse((ROOT / 'src/task_queue.py').read_text(encoding='utf-8-sig'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TaskQueueManager')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'enqueue_batch')
        isolated = ast.ClassDef(name='Queue', bases=[], keywords=[], body=[method], decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[isolated], type_ignores=[]))
        exec(compile(module, str(ROOT / 'src/task_queue.py'), 'exec', flags=__import__('__future__').annotations.compiler_flag), ns)
        queue = ns['Queue']()
        queue.__dict__.update(_admission_epoch=0, _stopping=False, _started=True,
                              execution_service=NS(rebuild_service=None, review_service=None),
                              _has_abandon_audit=lambda identifier: False, _pending=deque(),
                              _read=lambda identifier: records.get(identifier), task_root=None,
                              _write=lambda identifier, payload: (events.append('write'), records.update({identifier: payload})),
                              _refresh_queue_positions=lambda: None,
                              _queue=NS(put_nowait=lambda job: events.append('publish')))
        async def start():
            events.append('start')
            if state.on_start:
                state.on_start()
            queue._started = True
        queue.start = start
        scopes = tuple(NS(task_id=identifier, batch_scope_fingerprint='fp', reservation_lineage_id='lineage',
                          reservation_revision=1, to_dict=lambda: {}) for identifier in ids)
        manifest = NS(state='reservations_written', task_source_scopes=scopes, batch_id='batch', revision=1)
        def reserve(**kw):
            events.append('reserve')
            return manifest
        def activate(*a, **kw):
            manifest.state = 'active'
            return manifest
        queue.batch_scope_store = NS(reserve_batch=reserve, mark_tasks_enqueued=lambda *a, **k: manifest,
                                    activate_batch=activate, fail_closed=lambda *a: events.append('fail_closed'))
        requests = tuple(NS(task_id=identifier) for identifier in ids)
        jobs = tuple(MemoryJob(identifier) for identifier in ids)
        kwargs = dict(batch_id='batch', task_requests=requests, jobs=jobs, admission_guard=lambda: state.valid)
        return queue, state, events, records, kwargs, patch.dict(sys.modules, modules)

    async def test_ordinary_admission_control(self):
        q, s, events, records, kw, modules = self.make()
        with modules:
            self.assertEqual(await q.enqueue_batch(**kw), ('one',))
        self.assertEqual(set(records), {'one'})
        self.assertEqual(events, ['reserve', 'write'])

    async def test_rejection_without_discovery_precedes_start_and_reserve(self):
        for started in (False, True):
            with self.subTest(started=started):
                q, s, events, records, kw, modules = self.make()
                q._started = started
                s.valid = False
                with modules, self.assertRaises(QueueError):
                    await q.enqueue_batch(**kw)
                self.assertEqual(events, [])
                self.assertEqual(records, {})

    async def test_input_change_after_inspection_prevents_repair(self):
        q, s, events, records, kw, modules = self.make(repair_ids=('one',))
        s.on_inspect = lambda: setattr(s, 'valid', False)
        with modules, self.assertRaises(QueueError):
            await q.enqueue_batch(**kw)
        self.assertEqual(events, ['inspect'])

    async def test_stop_after_inspection_prevents_repair(self):
        q, s, events, _, kw, modules = self.make(repair_ids=('one',))
        s.on_inspect = lambda: setattr(q, '_admission_epoch', 1)
        with modules, self.assertRaises(QueueError):
            await q.enqueue_batch(**kw)
        self.assertEqual(events, ['inspect'])

    async def test_guard_propagates_to_fresh_recursion_after_repair(self):
        q, s, events, _, kw, modules = self.make(repair_ids=('one',), ids=('one', 'two'))
        s.on_repair = lambda: setattr(s, 'valid', False)
        with modules, self.assertRaises(QueueError):
            await q.enqueue_batch(**kw)
        self.assertEqual(events, ['inspect', 'repair'])

    async def test_notification_precedes_side_effect_region(self):
        for repairs, started in (((), True), ((), False), (('one',), True)):
            with self.subTest(repairs=repairs, started=started):
                q, s, events, _, kw, modules = self.make(repair_ids=repairs)
                q._started = started
                kw['on_admission_started'] = lambda: events.append('notify')
                with modules:
                    await q.enqueue_batch(**kw)
                expected = (['notify', 'start'] if not started else [])
                expected += ['inspect', 'notify', 'repair'] if repairs else ['notify', 'reserve', 'write']
                self.assertEqual(events, expected)

    async def test_notification_precedes_worker_publication(self):
        q, s, events, records, kw, modules = self.make()
        kw['jobs'] = (replace(kw['jobs'][0], operation='await_source_confirmation', preview_only=False),)
        kw['on_admission_started'] = lambda: events.append('notify')
        with modules:
            await q.enqueue_batch(**kw)
        self.assertEqual(events, ['notify', 'reserve', 'write', 'publish'])
        self.assertEqual(set(records), {'one'})

    async def test_mixed_repair_and_fresh_control(self):
        q, s, events, records, kw, modules = self.make(repair_ids=('one',), ids=('one', 'two'))
        kw['on_admission_started'] = lambda: events.append('notify')
        with modules:
            self.assertEqual(await q.enqueue_batch(**kw), ('one', 'two'))
        self.assertEqual(events, ['inspect', 'notify', 'repair', 'notify', 'reserve', 'write'])
        self.assertEqual(set(records), {'two'})

    async def test_stop_during_repair_prevents_fresh_recursion(self):
        q, s, events, records, kw, modules = self.make(repair_ids=('one',), ids=('one', 'two'))
        s.on_repair = lambda: setattr(q, '_admission_epoch', 1)
        with modules, self.assertRaises(QueueError):
            await q.enqueue_batch(**kw)
        self.assertEqual(events, ['inspect', 'repair'])
        self.assertEqual(records, {})

    async def test_change_during_start_prevents_reserve(self):
        q, s, events, records, kw, modules = self.make()
        q._started = False
        s.on_start = lambda: setattr(s, 'valid', False)
        with modules, self.assertRaises(QueueError):
            await q.enqueue_batch(**kw)
        self.assertEqual(events, ['start'])
        self.assertEqual(records, {})

    async def test_dialog_and_queue_original_methods_share_admission_boundary(self):
        for scenario in ('success', 'changed_in_scan', 'write_uncertain'):
            with self.subTest(scenario=scenario):
                d, ns, calls, notices = SubmitContract().make()
                q, s, events, records, kw, modules = self.make()
                d.discovery.groups = (None,)
                batch = {key: value for key, value in kw.items() if key != 'admission_guard'}
                batch['discovery'] = d.discovery
                ns['task_queue'] = NS(prepare_discovered_batch=lambda *a, **k: batch, enqueue_batch=q.enqueue_batch)
                if scenario == 'changed_in_scan':
                    s.on_validate = lambda: setattr(d, 'media_type', 'anime')
                elif scenario == 'write_uncertain':
                    def reserve(**kw):
                        events.append('reserve')
                        raise OSError('接纳结果未知')
                    q.batch_scope_store.reserve_batch = reserve
                with modules:
                    await d._submit()
                    if scenario == 'write_uncertain':
                        await d._submit()
                self.assertFalse(d._submitting)
                if scenario == 'success':
                    self.assertTrue(d.was_closed)
                    self.assertEqual(events, ['reserve', 'write'])
                elif scenario == 'changed_in_scan':
                    self.assertEqual(events, [])
                    self.assertFalse(d.submit_button.disabled)
                else:
                    self.assertEqual(events, ['reserve'])
                    self.assertTrue(d._submission_needs_check)
                    self.assertTrue(d.submit_button.disabled)
                    self.assertTrue(any('任务卡' in text for text, _ in notices))

    async def test_discovery_stop_control(self):
        q, s, events, _, kw, modules = self.make()
        kw['discovery'] = NS(groups=(None,))
        s.on_validate = lambda: setattr(q, '_admission_epoch', 1)
        with modules, self.assertRaises(QueueError):
            await q.enqueue_batch(**kw)
        self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main()
