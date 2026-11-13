"""执行记录摘要与操作许可分离的纯内存合同。"""
from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import dataclass
import io
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Mapping, Tuple
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'src/rename/execution_recovery.py'


def namespace():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    names = {'ExecutionRecoveryError', 'ExecutionRecoverySnapshot',
             'load_execution_recovery', '_task_path', '_ledger_path',
             '_validate_completed_hardlink_ledger', 'abandon_catalog_update_recovery'}
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names]
    assert len(nodes) == len(names)
    store = ast.parse((ROOT / 'src/rename/plan_store.py').read_text(encoding='utf-8-sig'))
    store_class = next(n for n in store.body if isinstance(n, ast.ClassDef) and n.name == 'PlanStore')
    store_class.body = [n for n in store_class.body if isinstance(n, ast.FunctionDef) and n.name in {'__init__', 'path_for'}]
    assert len(store_class.body) == 2
    store_error = next(n for n in store.body if isinstance(n, ast.ClassDef) and n.name == 'PlanStoreError')
    safe_id = next(n for n in store.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_SAFE_TASK_ID' for t in n.targets))
    ns = dict(globals(), AssetCollisionError=RuntimeError)
    module = ast.Module(body=[store_error, safe_id, store_class, *nodes], type_ignores=[])
    exec(compile(module, str(SOURCE), 'exec'), ns)
    return ns


def payload(version=1, **overrides):
    value = {'schema_version': version, 'task_id': 'sample-task',
             'plan_fingerprint': 'a' * 64, 'mode': 'hardlink', 'dry_run': False,
             'status': 'completed', 'entries': [{'action': 'create', 'status': 'created'}]}
    value.update(overrides)
    return value


class ExecutionSummaryContract(unittest.TestCase):
    def setUp(self):
        self.ns = namespace()

    def read(self, value=None, *, raw=None, exists=True, error=None):
        text = json.dumps(value, ensure_ascii=False) if raw is None else raw
        before = deepcopy(value)
        expected = Path('synthetic-records') / 'sample-task.ledger.json'
        seen = []

        def read_only(path, mode, *, encoding):
            self.assertEqual(path, expected)
            self.assertEqual((mode, encoding), ('r', 'utf-8'))
            seen.append(path)
            if error:
                raise error
            return io.StringIO(text)

        self.ns['open'] = read_only
        with patch.object(Path, 'exists', autospec=True, side_effect=lambda path: exists if path == expected else self.fail('读取了其他路径')):
            result = self.ns['load_execution_recovery']('sample-task', Path('synthetic-records'))
        self.assertEqual(value, before)
        self.assertEqual(seen, [expected] if exists else [])
        self.assertEqual(result.ledger_path, expected)
        return result

    def test_v1_summary_control(self):
        result = self.read(payload(1, status='rolled_back', entries=[{'status': 'rolled_back'}, {'status': 'failed'}]))
        self.assertEqual((result.ledger_status, result.entry_count, result.error), ('rolled_back', 2, ''))
        self.assertEqual(result.entry_statuses, (('failed', 1), ('rolled_back', 1)))

    def test_v2_reuse_summary(self):
        result = self.read(payload(2, entries=[{'action': 'reuse_existing', 'status': 'reused_identical_asset'}, {'action': 'create', 'status': 'created'}]))
        self.assertEqual((result.ledger_status, result.entry_count, result.plan_fingerprint, result.error), ('completed', 2, 'a' * 64, ''))
        self.assertEqual(result.entry_statuses, (('created', 1), ('reused_identical_asset', 1)))

    def test_v3_replacement_statuses(self):
        for status in ('planned', 'running', 'completed', 'failed', 'rolled_back'):
            with self.subTest(status=status):
                value = payload(3, status=status, replacement_journal='untrusted/external.json', entries=[{'action': 'replace', 'status': status}, {'action': 'reuse_existing', 'status': 'reused_identical_asset'}])
                result = self.read(value)
                self.assertEqual((result.ledger_status, result.entry_count, result.error), (status, 2, ''))
                self.assertEqual(dict(result.entry_statuses), {status: 1, 'reused_identical_asset': 1})

    def test_missing_record(self):
        result = self.read(exists=False)
        self.assertFalse(result.ledger_present)
        self.assertEqual(result.ledger_status, 'missing')
        self.assertIn('不存在', result.error)

    def test_invalid_json(self):
        result = self.read(raw='{')
        self.assertEqual(result.ledger_status, 'untrusted')
        self.assertIn('无法信任', result.error)

    def test_non_object(self):
        for value in (None, [], 'record'):
            with self.subTest(value=value):
                self.assertIn('不是对象', self.read(value).error)

    def test_wrong_task(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.assertIn('与任务不匹配', self.read(payload(version, task_id='another-task')).error)

    def test_broken_entries(self):
        for version in (1, 2, 3):
            for entries in (None, {}, [None], ['broken']):
                with self.subTest(version=version, entries=entries):
                    self.assertEqual(self.read(payload(version, entries=entries)).ledger_status, 'untrusted')

    def test_unknown_versions_rejected(self):
        for version in (None, 0, 4, '2', [], {}):
            with self.subTest(version=version):
                result = self.read(payload(version))
                self.assertEqual((result.ledger_status, result.entry_count), ('untrusted', 0))
                self.assertIn('版本不受支持', result.error)

    def test_stored_paths_never_redirect_reading(self):
        result = self.read(payload(1, ledger_path='outside/private.json', record_root='elsewhere'))
        self.assertEqual(result.error, '')

    def test_unsafe_task_ids_rejected_before_reading(self):
        self.ns['open'] = lambda *a, **kw: self.fail('非法任务触发文件读取')
        for task_id in ('', '.', '..', '../other', 'a/b', 'a\\b'):
            with self.subTest(task_id=task_id), self.assertRaises(self.ns['ExecutionRecoveryError']):
                self.ns['load_execution_recovery'](task_id, Path('synthetic-records'))

    def test_optional_defaults_preserved(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                result = self.read(payload(version, status=None, plan_fingerprint=42, entries=[{}, {'status': ''}, {'status': 'custom'}, {'status': 'custom'}]))
                self.assertEqual((result.ledger_status, result.plan_fingerprint, result.error), ('unknown', '', ''))
                self.assertEqual(result.entry_statuses, (('custom', 2), ('unknown', 2)))

    def test_read_error_preserved(self):
        result = self.read(payload(), error=PermissionError('denied'))
        self.assertEqual(result.ledger_status, 'untrusted')
        self.assertIn('denied', result.error)

    def plan_and_ledger(self, version=1):
        class Media:
            def __init__(self, name): self.name = name
            def __str__(self): return self.name
            def is_file(self): return True
            def stat(self): return SimpleNamespace(st_dev=1, st_ino=2, st_nlink=2)
        source, target = Media('source.mkv'), Media('target.mkv')
        plan = SimpleNamespace(task_id='sample-task', metadata={}, entries=[SimpleNamespace(source_path=source, target_path=target, collision_resolution=None)])
        ledger = payload(version, entries=[{'source': str(source), 'target': str(target), 'action': 'create', 'status': 'created', 'transfer_type': 'hardlink'}])
        return plan, ledger

    def validate(self, plan, ledger):
        return self.ns['_validate_completed_hardlink_ledger'](plan=plan, ledger=ledger, fingerprint='a' * 64)

    def test_v3_display_does_not_grant_recovery(self):
        plan, ledger = self.plan_and_ledger(3)
        self.assertEqual(self.read(ledger).error, '')
        with self.assertRaisesRegex(ValueError, 'schema3不能脱离类型化换源操作'):
            self.validate(plan, ledger)

    def test_v2_action_mismatch_still_rejected(self):
        plan, ledger = self.plan_and_ledger(2)
        plan.entries[0].collision_resolution = object()
        self.ns['validate_reuse_resolution'] = lambda *args: self.fail('错误action不应进入复用验证')
        self.assertEqual(self.read(ledger).error, '')
        with self.assertRaisesRegex(ValueError, '缺少同内容复用结果'):
            self.validate(plan, ledger)

    def test_fingerprint_guard_independent(self):
        plan, ledger = self.plan_and_ledger(1)
        ledger['plan_fingerprint'] = 'b' * 64
        self.assertEqual(self.read(ledger).plan_fingerprint, 'b' * 64)
        with self.assertRaisesRegex(ValueError, '当前整理版本不匹配'):
            self.validate(plan, ledger)

    def test_abandon_keeps_existing_version_gate_before_any_write(self):
        self.ns['_SHA256'] = re.compile(r'^[0-9a-f]{64}$')
        self.ns['_abandon_audit_path'] = lambda *args: Path('audit/sample.json')
        def forbidden(*args, **kwargs):
            self.fail('版本不被操作接受时不能产生写入')
        self.ns['_exclusive_atomic_json_write'] = forbidden
        self.ns['mutate_task_at'] = forbidden
        plan_store = SimpleNamespace(load=lambda task: object(), fingerprint=lambda plan: 'a' * 64,
                                     delete_current=forbidden)
        for version in (2, 3):
            with self.subTest(version=version):
                task = {'uuid': 'sample-task', 'workflow_status': 'failed', 'execution_status': 'catalog_update_pending'}
                reads = {Path('tasks/sample-task.json'): json.dumps(task),
                         Path('records/sample-task.ledger.json'): json.dumps(payload(version))}
                def read_only(path, *, encoding):
                    self.assertEqual(encoding, 'utf-8')
                    self.assertIn(path, reads)
                    return reads[path]
                with patch.object(Path, 'exists', return_value=False), patch.object(Path, 'read_text', autospec=True, side_effect=read_only), patch.object(Path, 'unlink', side_effect=forbidden):
                    with self.assertRaisesRegex(self.ns['ExecutionRecoveryError'], '当前任务不匹配'):
                        self.ns['abandon_catalog_update_recovery']('sample-task', task_root=Path('tasks'), record_root=Path('records'),
                            plan_store=plan_store, review_root=Path('reviews'), decision_state_root=Path('decisions'),
                            audit_root=Path('audit'), expected_plan_fingerprint='a' * 64)

    def test_hardlink_validator_control(self):
        for version in (1, 2):
            with self.subTest(version=version):
                plan, ledger = self.plan_and_ledger(version)
                self.assertIsNone(self.validate(plan, ledger))
                plan.entries[0].target_path.stat = lambda: SimpleNamespace(st_dev=1, st_ino=99, st_nlink=2)
                with self.assertRaisesRegex(ValueError, 'hardlink'):
                    self.validate(plan, ledger)

    def test_gui_uses_summary_only_for_display_and_abandon_fingerprint(self):
        tree = ast.parse((ROOT / 'src/pages/data_table_page.py').read_text(encoding='utf-8-sig'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_open_execution_recovery')
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'load_execution_recovery']
        self.assertEqual(len(calls), 1)
        self.assertEqual([ast.unparse(arg) for arg in calls[0].args], ['task_id', 'RECORD_PATH'])
        for branch in (n for n in ast.walk(node) if isinstance(n, ast.If)):
            if any(isinstance(n, ast.Name) and n.id == 'snapshot' for n in ast.walk(branch.test)):
                self.assertFalse(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'button' for n in ast.walk(branch)))
        # 放弃确认只将指纹传给现有独立验证入口，不由摘要授权。
        text = ast.unparse(node)
        self.assertIn('snapshot.plan_fingerprint', text)
        self.assertIn('task_data.get', text)


if __name__ == '__main__':
    unittest.main()
