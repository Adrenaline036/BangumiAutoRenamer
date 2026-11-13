"""AI退役兼容合同；不读取真实配置，不导入产品单例。"""
from __future__ import annotations
import ast
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time
import uuid
from types import SimpleNamespace as NS
from types import ModuleType
from unittest.mock import patch
from urllib.parse import urlparse
from collections.abc import Mapping
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_stage1_manual_input_contract import source_objects, ROOT

OLD_KEYS = {'ai_provider', 'ai_api_key', 'ai_base_url', 'ai_model', 'ai_temperature',
            'gemini_api_key', 'gemini_base_url', 'gemini_model', 'gemini_temperature',
            'ai_enabled', 'ai_confidence_threshold', 'openai_output_format', 'ai_auto_save'}
DISK_ROOT = None


class VersionStorage:
    """实际文件层只新增不可变版本；不替产品验证操作系统原子覆盖。"""
    def __init__(self):
        self.content = None
        self.current_path = None

    def write(self, content):
        if DISK_ROOT is not None:
            self.current_path = DISK_ROOT / (uuid.uuid4().hex + '.json')
            with self.current_path.open('xb') as handle:
                handle.write(content)
        self.content = content

    def exists(self):
        return self.content is not None

    def read_bytes(self):
        return self.current_path.read_bytes() if self.current_path else self.content

    def read_text(self, encoding='utf-8'):
        return self.read_bytes().decode(encoding)

    def open(self, mode='r', encoding='utf-8'):
        if mode != 'r':
            raise AssertionError('读取夹具不接受写入')
        return io.StringIO(self.read_text(encoding))


def queue_objects(names):
    text = (ROOT / 'src/task_queue.py').read_text(encoding='utf-8-sig')
    tree = ast.parse(text)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TaskQueueManager')
    cls.body = [n for n in cls.body if getattr(n, 'name', None) in names]
    task = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TaskJob')
    ns = {'__name__': __name__, '__package__': '_retirement', 'dataclass': dataclass, 'Path': Path,
          'uuid': uuid, 'PlanExecutionError': ValueError, 'BatchTaskSourceRequest': NS}
    exec(compile(ast.Module(body=[task, cls], type_ignores=[]), str(ROOT / 'src/task_queue.py'),
                 'exec', flags=__import__('__future__').annotations.compiler_flag), ns)
    return ns


def config_namespace(memory):
    path = ROOT / 'src/config/config_manager.py'
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    names = {'CONFIG_DEFAULT', 'ConfigManager'}
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'RETIRED_AI_CONFIG_KEYS' for t in n.targets):
            names.add('RETIRED_AI_CONFIG_KEYS')
    return source_objects('src/config/config_manager.py', names,
                          {'contextmanager': __import__('contextlib').contextmanager, 'json': json, 'urlparse': urlparse,
                           'open': lambda *a, **k: io.StringIO(memory['text'])})


class RetirementConfig(unittest.TestCase):
    def test_page_save_filters_passive_keys_and_log_preserves_old_storage(self):
        old = {key: 'synthetic-retired-value' for key in OLD_KEYS}
        old.update(ai_base_url='https://synthetic.invalid', gemini_base_url='https://synthetic.invalid')
        memory = {'text': json.dumps(old)}
        config = config_namespace(memory)
        cm = config['ConfigManager'].__new__(config['ConfigManager'])
        cm._config_lock = nullcontext
        cm.config_path = NS(exists=lambda: True)
        cm._write_unlocked = lambda value: memory.update(text=json.dumps(value))
        cm.update_config()
        logged, notices, saved, closed = [], [], [], []
        original_save = cm.set_configs
        def save(values):
            saved.append(dict(values))
            return original_save(values)
        cm.set_configs = save
        ns = source_objects('src/pages/config_page.py', {'BASIC_CONFIG_KEYS', 'ConfigPage'},
            {'ui': NS(dialog=object, notify=lambda *a, **k: notices.append(a)), 'cm': cm,
             'logger': NS(info=logged.append, error=logged.append), 'is_ui_test_mode': lambda: False,
             'SimpleNamespace': NS, 'CN_MAP': {}, 'Path': Path,
             'PureWindowsPath': __import__('pathlib').PureWindowsPath,
             'PurePosixPath': __import__('pathlib').PurePosixPath,
             'first_reparse_component': lambda path: None, 'update_log_level_from_config': lambda: None})
        page = ns['ConfigPage'].__new__(ns['ConfigPage'])
        page.config = NS(**{**{key: cm.get_config(key) for key in ns['BASIC_CONFIG_KEYS']}, **cm.config})
        page.config.movie_path = str(ROOT)
        page.close = lambda: closed.append(True)
        page._handle_ok()
        self.assertEqual(closed, [True], notices)
        self.assertEqual(set(saved[0]), set(ns['BASIC_CONFIG_KEYS']))
        for key in OLD_KEYS:
            self.assertEqual(json.loads(memory['text'])[key], old[key])
            self.assertNotIn(key, '\n'.join(logged))
        self.assertNotIn('synthetic-retired-value', '\n'.join(logged))

    def test_new_defaults_do_not_generate_ai_settings(self):
        ns = config_namespace({'text': '{}'})
        self.assertFalse(OLD_KEYS & ns['CONFIG_DEFAULT'].keys())
        self.assertFalse(OLD_KEYS & ns['ConfigManager']._normalized({}).keys())

    def test_old_configuration_survives_load_edit_save_reload_but_is_inactive(self):
        old = {key: 'synthetic-history-' + key for key in OLD_KEYS}
        old.update(ai_enabled=True, ai_auto_save=False, movie_path='/media/Movies')
        memory = {'text': json.dumps(old)}
        ns = config_namespace(memory)
        obj = ns['ConfigManager'].__new__(ns['ConfigManager'])
        obj._config_lock = nullcontext
        obj.config_path = NS(exists=lambda: True)
        obj._write_unlocked = lambda value: memory.update(text=json.dumps(value))
        obj.update_config()
        for key in OLD_KEYS:
            self.assertEqual(obj.config[key], old[key])
            self.assertEqual(obj.get_config(key), '')
            self.assertFalse(obj.set_config(key, 'changed'))
        self.assertTrue(obj.set_configs({'movie_path': '/manual/Movies'}))
        obj.write_config()
        obj.config = {}
        obj.update_config()
        for key in OLD_KEYS:
            self.assertEqual(obj.config[key], old[key])
            self.assertEqual(json.loads(memory['text'])[key], old[key])
        self.assertEqual(obj.config['movie_path'], '/manual/Movies')


class LegacyTask(unittest.TestCase):
    def test_process_record_expressions_preserve_compatibility_value_on_reopen(self):
        tree = ast.parse((ROOT / 'src/rename/process.py').read_text('utf-8-sig'))
        writes = [value for n in ast.walk(tree) if isinstance(n, ast.Dict)
                  for key, value in zip(n.keys, n.values)
                  if isinstance(key, ast.Constant) and key.value == 'use_ai']
        self.assertEqual(len(writes), 3)
        publisher = source_objects('src/utils/utils.py', {'_serialize_task_record'}, {'json': json})
        for expression in writes:
            for value in (True, False, None):
                for anime in (True, False):
                    with self.subTest(line=expression.lineno, value=value, anime=anime):
                        stored = eval(compile(ast.Expression(expression), 'process-record-expression', 'eval'),
                                      {'use_ai': value, 'is_anime': anime, 'ai_allowed': False})
                        cell = VersionStorage()
                        cell.write(publisher['_serialize_task_record']({'use_ai': stored}))
                        self.assertIs(json.loads(cell.read_bytes())['use_ai'], value)

    def test_resume_job_uses_original_origin_value_instead_of_top_level_record(self):
        from src.task_queue import TaskJob
        tree = ast.parse((ROOT / 'src/task_queue.py').read_text('utf-8-sig'))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name == '_resume_default_activation_locked')
        assignment = next(n for n in method.body if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'job' for t in n.targets))
        expression = compile(ast.Expression(assignment.value), 'resume-job-fields', 'eval')
        for value in (True, False, None):
            with self.subTest(value=value):
                origin = NS(path=str(ROOT), is_anime=False, is_movie=True, custom_name=None,
                            requested_season=None, use_ai=value, source='ui')
                current = {'use_ai': 'different-top-level-value', 'task_source_scope': None}
                job = eval(expression, dict(TaskJob=TaskJob, Path=Path, origin=origin,
                    task_id='task', expected_fingerprint='fingerprint', state=NS(revision=1),
                    TaskSourceScope=NS(from_mapping=lambda value: value), current=current))
                self.assertIs(job.use_ai, value)
                self.assertIs(job.recognition_origin, origin)
                self.assertEqual(current['use_ai'], 'different-top-level-value')

    def test_removed_control_none_cannot_overwrite_old_true(self):
        ns = source_objects('src/rename/recognition_queue_admission.py', {'search_name', '_name', '_workspace_data'},
                            {'Path': Path, 'PlanExecutionError': ValueError})
        for authority in (False, True):
            with self.subTest(authority=authority):
                current = dict(path=str(ROOT), is_anime=False, is_movie=True, name='Example', season_id=None, use_ai=True)
                request = dict(path=str(ROOT), preview_only=False, is_anime=False, is_movie=True,
                               custom_name='Example', custom_season_id=None, use_ai=None)
                result = ns['_workspace_data'](current, request, NS(media_domain='movie'), authority)
                self.assertIs(result['use_ai'], True)

    def test_old_values_survive_whole_record_publication_and_reopen(self):
        ns = source_objects('src/rename/recognition_queue_admission.py', {'search_name', '_name', '_workspace_data'},
                            {'Path': Path, 'PlanExecutionError': ValueError})
        for authority in (False, True):
            for value in (True, False, None, 'missing'):
                with self.subTest(authority=authority, value=value, disk=DISK_ROOT is not None):
                    cell = VersionStorage()
                    publish = source_objects('src/utils/utils.py',
                        {'_serialize_task_record', 'publish_task_and_accept_at', 'read_task_at', 'read_task_file', 'TaskAcceptanceUncertain'},
                        {'__name__': 'src.utils.utils', '__package__': 'src.utils',
                         'Path': Path, 'json': json, 'time': time, 'task_record_lock': lambda *a: nullcontext(),
                         'safe_task_path': lambda *a: cell,
                         '_atomic_replace_task_bytes': lambda path, content: cell.write(content)})
                    current = dict(uuid='old-task', path=str(ROOT), is_anime=False, is_movie=True, name='Example', season_id=None)
                    if value != 'missing':
                        current['use_ai'] = value
                    original = publish['_serialize_task_record'](current)
                    cell.write(original)
                    request = dict(path=str(ROOT), preview_only=False, is_anime=False, is_movie=True,
                                   custom_name='Example', custom_season_id=None)
                    result = ns['_workspace_data'](current, request, NS(media_domain='movie'), authority)
                    self.assertEqual(result.get('use_ai'), current.get('use_ai'))
                    accepted = []
                    publish['publish_task_and_accept_at'](ROOT, 'old-task', previous=original, data=result,
                        before_publish=lambda: None, accept=lambda: accepted.append(True))
                    self.assertEqual(accepted, [True])
                    self.assertEqual(json.loads(cell.read_bytes()).get('use_ai'), current.get('use_ai'))
                    self.assertEqual(publish['read_task_at'](ROOT, 'old-task').get('use_ai'), current.get('use_ai'))

    def test_workspace_no_ai_request_preserves_old_values_and_authority(self):
        ns = source_objects('src/rename/recognition_queue_admission.py', {'search_name', '_name', '_workspace_data'},
                            {'Path': Path, 'PlanExecutionError': ValueError})
        for authority in (False, True):
            for value in (True, False, None, 'missing'):
                with self.subTest(authority=authority, value=value):
                    current = dict(path=str(ROOT), is_anime=False, is_movie=True, name='Example', season_id=None)
                    if value != 'missing':
                        current['use_ai'] = value
                    request = dict(path=str(ROOT), preview_only=False, is_anime=False, is_movie=True,
                                   custom_name='Example', custom_season_id=None)
                    result = ns['_workspace_data'](current, request, NS(media_domain='movie'), authority)
                    self.assertEqual(result.get('use_ai'), current.get('use_ai'))
                    reopened = json.loads(json.dumps(result))
                    self.assertEqual(reopened.get('use_ai'), current.get('use_ai'))
                    self.assertEqual(current.get('use_ai'), None if value == 'missing' else value)

    def test_workspace_explicit_name_keeps_legacy_ai_value(self):
        ns = source_objects('src/rename/recognition_queue_admission.py', {'search_name', '_name', '_workspace_data'},
                            {'Path': Path, 'PlanExecutionError': ValueError})
        current = dict(path=str(ROOT), is_anime=False, is_movie=True, name='Example', season_id=None, use_ai=True)
        request = dict(path=str(ROOT), preview_only=False, is_anime=False, is_movie=True,
                       custom_name='Other', custom_season_id=None, use_ai=True)
        result = ns['_workspace_data'](current, request, NS(media_domain='movie'), True)
        self.assertEqual(('Other', 'Other'), (result['name'], result['search_name']))
        self.assertIs(result['use_ai'], True)
        self.assertEqual('Example', current['name'])

    def test_origin_v1_old_values_and_fingerprint_are_unchanged(self):
        ns = source_objects('src/rename/automatic_recognition.py', {'_HASH', '_digest', 'AutomaticRecognitionOrigin'},
                            {'__name__': __name__, 'Mapping': Mapping, 'dataclass': dataclass, 'asdict': asdict,
                             'hashlib': hashlib, 'Path': Path, 're': re,
                             'encode': lambda v: json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'))})
        cls = ns['AutomaticRecognitionOrigin']
        for value in (True, False, None):
            with self.subTest(value=value):
                origin = cls('task', 'job', 'batch', 'a'*64, '', '', -1, str(ROOT), False, True, None, value, None, 'ui')
                raw = origin.to_dict()
                reopened = cls.from_mapping(json.loads(json.dumps(raw)))
                self.assertEqual(reopened.to_dict(), raw)
                self.assertEqual(reopened.fingerprint, origin.fingerprint)
                raw.pop('use_ai')
                with self.assertRaises(ValueError):
                    cls.from_mapping(raw)


class RetiredPaths(unittest.TestCase):
    def test_new_prepared_jobs_have_none_compatibility_value(self):
        ns = queue_objects({'prepare_input_batch', 'prepare_discovered_batch'})
        manager = ns['TaskQueueManager']()
        module = ModuleType('_retirement.rename.input_directory_discovery')
        module.collect_source = lambda path: NS(path=ROOT, input_scope=(str(ROOT),), scope_kind='parent_directory', identities=())
        with patch.dict(sys.modules, {module.__name__: module}):
            batch = manager.prepare_input_batch((ROOT,), is_anime=False, is_movie=True, source='ui', preview_only=True)
        self.assertIsNone(batch['jobs'][0].use_ai)
        group = NS(path=ROOT, input_scope=(str(ROOT),), scope_kind='parent_directory', identities=(), reason=None)
        module = ModuleType('_retirement.rename.input_directory_discovery')
        module.discovery_task_groups = lambda d: (group,)
        with patch.dict(sys.modules, {module.__name__: module}):
            batch = manager.prepare_discovered_batch(NS(), is_anime=False, is_movie=True, source='ui')
        self.assertIsNone(batch['jobs'][0].use_ai)

    def test_dedicated_modules_and_tests_are_absent(self):
        for relative in ('src/ai', 'src/rename/ai_processor.py', 'tests/test_ai_gate.py',
                         'tests/test_ai_recognition.py', 'tests/test_task_ai_policy.py', 'tests/README_AI_Testing.md'):
            with self.subTest(path=relative):
                self.assertFalse((ROOT / relative).exists())

    def test_new_public_entrypoints_have_no_ai_parameter(self):
        tree = ast.parse((ROOT / 'src/task_queue.py').read_text(encoding='utf-8-sig'))
        names = {'prepare_input_batch', 'prepare_discovered_batch', 'enqueue_batch', 'reenqueue_workspace'}
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names:
                with self.subTest(method=n.name):
                    self.assertNotIn('use_ai', [a.arg for a in n.args.args + n.args.kwonlyargs])

    def test_gui_has_no_ai_controls_or_feature_claims(self):
        pages = [ROOT / 'src/pages' / name for name in (
            'config_page.py', 'add_task_page.py', 'data_table_page.py',
            'plan_review_page.py', 'recognition_recovery_view.py')]
        for path in pages + [ROOT / 'src/main_page.py']:
            filename = path.name
            with self.subTest(page=filename):
                text = path.read_text(encoding='utf-8-sig')
                vocabulary = OLD_KEYS | {'use_ai', 'openai', 'gemini', 'unified_ai_tester',
                    'ai_processor', 'AIClient', 'AIProcessor', 'AI识别', 'AI辅助', 'AI 辅助', 'AI 可选', 'BAR_FORCE_DISABLE_AI'}
                for token in vocabulary:
                    self.assertFalse(token in text, filename + ': ' + token)

    def test_no_ai_dependency_or_storage_generator(self):
        for filename in ('pyproject.toml', 'requirements.txt', 'requirements_docker.txt'):
            with self.subTest(file=filename):
                text = (ROOT / filename).read_text(encoding='utf-8-sig')
                self.assertIsNone(re.search(r'(?im)^\s*["\']?(?:openai|google-genai|google|hachoir)(?:[>=<~!\[])', text), filename)
        self.assertNotIn('AI_ANALYSIS_PATH', (ROOT / 'src/utils/path.py').read_text(encoding='utf-8-sig'))

    def test_process_has_rules_only_and_no_ai_switch(self):
        text = (ROOT / 'src/rename/process.py').read_text(encoding='utf-8-sig')
        self.assertIsNone(re.search(r'AIProcessor|AIAnalysisResult|ai_processor|ai_allowed|task_ai_policy', text))
        self.assertIn('self._process_traditional(', text)


if __name__ == '__main__':
    if '--disk-root' in sys.argv:
        index = sys.argv.index('--disk-root')
        DISK_ROOT = Path(sys.argv[index + 1]).resolve()
        allowed = (ROOT / 'local/development/evidence/stage2-ai-20261009-081019').resolve()
        if not DISK_ROOT.is_relative_to(allowed) or DISK_ROOT == allowed:
            raise ValueError('文件验证只允许本轮隔离目录')
        DISK_ROOT.mkdir(parents=True, exist_ok=True)
        del sys.argv[index:index + 2]
    unittest.main()
