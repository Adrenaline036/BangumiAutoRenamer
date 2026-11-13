"""保持现有交互与展示：原方法的取消与单条读取故障合同。"""
from __future__ import annotations

import ast
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from types import MethodType, SimpleNamespace as NS
import unittest

ROOT = next(p for p in Path(__file__).resolve().parents if (p/'src/pages/config_page.py').exists())


def original(relative, name, namespace):
    tree = ast.parse((ROOT/relative).read_text('utf-8-sig'))
    nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    assert len(nodes) == 1, name
    node = nodes[0]
    assert not node.decorator_list
    compile_flags = __import__('__future__').annotations.compiler_flag
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT/relative), 'exec', flags=compile_flags), namespace)
    return namespace[name]


class PickerContract(unittest.IsolatedAsyncioTestCase):
    async def choose(self, value, error=None):
        calls, logs = [], []
        async def picker(*args, **kwargs):
            calls.append((args, kwargs))
            if error:
                raise error
            return value
        namespace = {'local_file_picker':picker, 'Sequence':Sequence, 'logger':NS(info=logs.append)}
        pick = original('src/pages/config_page.py', 'pick', namespace)
        change = original('src/pages/config_page.py', '_change', namespace)
        obj = NS(config=NS(movie_path='Z:/原目录', anime_path='Z:/保留'))
        obj._change = MethodType(change, obj)
        try:
            await pick(obj, key='movie_path')
        finally:
            self.assertEqual(calls, [(('~',), {'multiple':True})])
            self.assertEqual(obj.config.anime_path, 'Z:/保留')
        return obj.config.movie_path, logs

    async def test_cancel_none_preserves_edit_value(self):
        self.assertEqual((await self.choose(None))[0], 'Z:/原目录')

    async def test_empty_sequence_preserves_edit_value(self):
        for value in ([], ()):
            with self.subTest(value=value):
                self.assertEqual((await self.choose(value))[0], 'Z:/原目录')

    async def test_scalar_strings_and_bytes_are_not_path_lists(self):
        for value in ('D:/中文 完整目录', b'D:/path', bytearray(b'D:/path')):
            with self.subTest(value=value):
                self.assertEqual((await self.choose(value))[0], 'Z:/原目录')

    async def test_invalid_first_value_preserves_edit_value(self):
        for value in ([None], [''], ['  '], [1], [b'path']):
            with self.subTest(value=value):
                self.assertEqual((await self.choose(value))[0], 'Z:/原目录')

    async def test_normal_selection_keeps_full_first_path_and_log(self):
        for value in (['D:/中文 完整目录'], ['D:/中文 完整目录', 'D:/second'], ('D:/中文 完整目录',), ['D:/完整目录 ']):
            with self.subTest(value=value):
                actual, logs = await self.choose(value)
                self.assertEqual(actual, value[0])
                self.assertEqual(logs, [f'[配置] movie_path 选择了 {value[0]}'])

    async def test_picker_error_propagates(self):
        with self.assertRaisesRegex(RuntimeError, 'picker failure'):
            await self.choose(None, RuntimeError('picker failure'))


class Entry:
    def __init__(self, stem, mtime, payload=None, stat_error=None, read_error=None):
        self.stem, self.name = stem, stem+'.json'
        self.mtime, self.payload = mtime, payload
        self.stat_error, self.read_error = stat_error, read_error
        self.stat_calls = 0
    def stat(self):
        self.stat_calls += 1
        if self.stat_error:
            raise self.stat_error
        return NS(st_mtime=self.mtime)


class Root:
    def __init__(self, entries, exists=True):
        self.entries, self.present = entries, exists
    def exists(self):
        return self.present
    def glob(self, pattern):
        assert pattern == '*.json'
        return iter(self.entries)


class RowsContract(unittest.TestCase):
    def load(self, entries, *, canonical=False, exists=True):
        root = Root(entries, exists)
        reads, logs = [], []
        def read(entry):
            reads.append(entry.stem)
            if entry.read_error:
                if isinstance(entry.read_error, FileNotFoundError):
                    return None
                raise entry.read_error
            return deepcopy(entry.payload)
        def isolated_read(entry):
            self.assertFalse(canonical)
            return read(entry)
        def canonical_read(name):
            self.assertTrue(canonical)
            return read(next(e for e in entries if e.stem == name))
        namespace = {'TASK_PATH':root if canonical else object(), 'logger':NS(error=logs.append),
            'read_task_file':isolated_read, 'get_task':canonical_read}
        fn = original('src/pages/data_table_page.py', 'load_task_rows', namespace)
        return fn(root), reads, logs

    def test_missing_root_is_empty(self):
        self.assertEqual(self.load([], exists=False), ([], [], []))

    def test_empty_directory_is_empty(self):
        self.assertEqual(self.load([]), ([], [], []))

    def test_disappeared_stat_does_not_hide_other_rows(self):
        entries=[Entry('old',1,{'name':'旧任务'}),Entry('gone',3,stat_error=FileNotFoundError('gone')),Entry('new',2,{'name':'新任务'})]
        rows, reads, logs = self.load(entries)
        self.assertEqual([r['uuid'] for r in rows], ['new','old'])
        self.assertEqual(reads, ['new','old'])
        self.assertEqual(logs, [])

    def test_stat_permission_error_cannot_publish_a_partial_snapshot(self):
        with self.assertRaises(PermissionError):
            self.load([Entry('denied',2,stat_error=PermissionError('denied')),Entry('ok',1,{'name':'ok'})])

    def test_read_permission_error_cannot_publish_a_partial_snapshot(self):
        with self.assertRaises(PermissionError):
            self.load([Entry('denied',2,read_error=PermissionError('denied')),Entry('ok',1,{'name':'ok'})])

    def test_non_io_stat_error_is_not_hidden(self):
        with self.assertRaisesRegex(RuntimeError,'programming error'):
            self.load([Entry('bad',1,stat_error=RuntimeError('programming error'))])

    def test_sorting_and_equal_time_order_are_unchanged(self):
        entries=[Entry('a',1,{'name':'a'}),Entry('b',3,{'name':'b'}),Entry('c',3,{'name':'c'}),Entry('d',2,{'name':'d'})]
        rows, reads, logs = self.load(entries)
        self.assertEqual([r['uuid'] for r in rows],['b','c','d','a'])
        self.assertEqual(reads,['b','c','d','a'])
        self.assertEqual(logs,[])
        self.assertTrue(all(e.stat_calls==1 for e in entries))

    def test_bad_read_empty_and_wrong_uuid_remain_skipped(self):
        entries=[Entry('read-gone',4,read_error=FileNotFoundError('gone')),Entry('empty',3,{}),
            Entry('wrong',2,{'uuid':'other'}),Entry('ok',1,{'name':'ok'})]
        rows, reads, logs = self.load(entries)
        self.assertEqual([r['uuid'] for r in rows],['ok'])
        self.assertEqual(reads,['read-gone','empty','wrong','ok'])
        self.assertEqual(len(logs),1)

    def test_normal_row_projection_and_source_records_are_unchanged(self):
        payload={'uuid':'one','name':'正常','operation_history':[], 'projection_receipt_fingerprint':'abc','custom':{'nested':1}}
        before=deepcopy(payload)
        rows, _, _ = self.load([Entry('one',1,payload)])
        expected={**payload,'path':'未记录','season_id':None,'is_anime':None,'is_movie':None,
            'workflow_status':'completed','operation_history_state':'current','available_recovery_actions':[],
            'current_authority_usable':True}
        self.assertEqual(rows,[expected])
        self.assertEqual(payload,before)

    def test_legacy_projection_and_error_status_are_unchanged(self):
        rows, _, _ = self.load([Entry('legacy',1,{'error':'known'})])
        self.assertEqual(rows[0]['workflow_status'],'failed')
        self.assertEqual(rows[0]['operation_history_state'],'legacy_unknown')
        self.assertEqual(rows[0]['available_recovery_actions'],['replan_unapproved','safe_abandon'])
        self.assertFalse(rows[0]['current_authority_usable'])

    def test_canonical_root_uses_existing_get_task_route(self):
        rows, reads, logs = self.load([Entry('one',1,{'name':'normal'})],canonical=True)
        self.assertEqual([r['uuid'] for r in rows],['one'])
        self.assertEqual(reads,['one'])
        self.assertEqual(logs,[])


if __name__ == '__main__':
    unittest.main()
