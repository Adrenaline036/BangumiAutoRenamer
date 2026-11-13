"""只读隔离合同：输入集合、环境权限、依赖解析和执行拒绝行为。"""
import ast
import fnmatch
import json
from pathlib import Path, PurePosixPath
import shlex
import sys
from types import SimpleNamespace
import unittest

C = Path(__file__).resolve().parents[1]
ENVIRONMENT = {'BAR_UI_PORT', 'BAR_UI_STORAGE_SECRET', 'BAR_DATA_DIR', 'BAR_DRY_RUN', 'TMDB_API_KEY'}
EXTERNAL = set(sys.stdlib_module_names) | {'fcntl', 'msvcrt', 'nicegui', 'fastapi', 'requests',
                                        'structlog', 'jikanpy', 'tmdbsimple', 'xxhash'}

def included(path, rules):
    keep = True
    for raw in rules:
        if not raw or raw.startswith('#'):
            continue
        negate = raw.startswith('!')
        pattern = raw.lstrip('!').strip().removeprefix('./').strip('/')
        # The project's patterns use whole paths, simple globs and directory prefixes.
        if '\\' in pattern:
            raise ValueError('unsupported ignore pattern requires Docker verification')
        parents = [path, *map(str, PurePosixPath(path).parents)]
        if any(fnmatch.fnmatchcase(part, pattern) for part in parents):
            keep = negate
    return keep

def build_inputs(docker, rules, files):
    context = {name for name in files if included(name, rules)}
    copied = set()
    for line in docker.splitlines():
        if not line.strip().upper().startswith('COPY '):
            continue
        body = line.strip()[5:]
        args = json.loads(body) if body.startswith('[') else shlex.split(body)
        if any(arg.startswith('--') for arg in args) or len(args) < 2:
            raise ValueError('unsupported COPY syntax')
        for source in args[:-1]:
            source = source.removeprefix('./').rstrip('/') or '.'
            copied.update(name for name in context if source == '.' or name == source
                          or name.startswith(source + '/') or fnmatch.fnmatchcase(name, source))
    return copied

def bindings(tree):
    known = {'__import__': 'builtins.__import__'}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for item in n.names:
                known[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]
        elif isinstance(n, ast.ImportFrom) and not n.level:
            for item in n.names:
                known[item.asname or item.name] = (n.module or '') + '.' + item.name
    def resolve(node):
        if isinstance(node, ast.Name):
            return known.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return resolve(node.value) + '.' + node.attr
        return ''
    for _ in range(20):
        before = dict(known)
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                value = resolve(n.value)
                if value:
                    for target in n.targets:
                        if isinstance(target, ast.Name):
                            known[target.id] = value
        if before == known:
            break
    return resolve

def roots(tree, package=''):
    resolve = bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split('.')
                if node.level > len(parts):
                    raise AssertionError('relative import escapes production package')
                yield '.'.join(parts[:len(parts)-node.level+1] + ([node.module] if node.module else []))
            else:
                yield node.module or ''
        elif isinstance(node, ast.Call) and resolve(node.func) in {'builtins.__import__', 'importlib.import_module'}:
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                raise AssertionError('dynamic import requires an explicit resolved target')
            yield node.args[0].value

def env_reads(tree):
    resolve = bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and resolve(node.func) in {'os.getenv', 'os.environ.get', 'os.environ.setdefault', 'os.environ.pop'}:
            yield node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else '<dynamic>'
        elif isinstance(node, ast.Subscript) and resolve(node.value) == 'os.environ':
            yield node.slice.value if isinstance(node.slice, ast.Constant) else '<dynamic>'

def guard_namespace(text):
    tree = ast.parse(text)
    nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
    names = {'PlanExecutionError', 'require_approved_execution_policy', '_execute_locked', '_validate_runtime_safety_gate'}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id in {
                'DRY_RUN_PLAN_RETRY_CODE', 'DRY_RUN_PLAN_RETRY_MESSAGE', 'MISSING_EXECUTION_POLICY_CODE'} for x in node.targets):
            nodes.append(node)
    namespace = {'cm': SimpleNamespace(get_config=lambda key: '链接'), 'is_dry_run_enabled': lambda: True}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), '<actual-guard-bodies>', 'exec'), namespace)
    return namespace

class ProductionIsolation(unittest.TestCase):
    def test_all_product_modules_parse(self):
        for path in (C / 'src').rglob('*.py'):
            ast.parse(path.read_text('utf-8-sig'))

    def test_environment_reads_are_production_capabilities(self):
        bad = [(str(p.relative_to(C)), key) for p in (C / 'src').rglob('*.py')
               for key in env_reads(ast.parse(p.read_text('utf-8-sig'))) if key not in ENVIRONMENT]
        self.assertEqual([], bad)

    def test_import_targets_resolve_to_product_or_runtime_dependencies(self):
        bad = []
        for path in (C / 'src').rglob('*.py'):
            for name in roots(ast.parse(path.read_text('utf-8-sig')), '.'.join(path.relative_to(C).parts[:-1])):
                first = name.split('.')[0]
                if first == 'src':
                    target = C.joinpath(*name.split('.'))
                    if not target.with_suffix('.py').is_file() and not target.is_dir():
                        bad.append(name)
                elif first not in EXTERNAL:
                    bad.append(name)
        self.assertEqual([], bad)

    def test_simulation_behavior_lives_outside_product(self):
        found = []
        for path in (C / 'src').rglob('*.py'):
            tree = ast.parse(path.read_text('utf-8-sig'))
            if any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and any(s in n.value for s in ('假执行', '模拟结果', '加入测试队列', '不存在的合成路径'))
                   for n in ast.walk(tree)):
                found.append(str(path.relative_to(C)))
        self.assertEqual([], found)
        self.assertFalse((C / 'src/pages/gui_simulation.py').exists())
        self.assertTrue((C / 'development/gui_simulation.py').is_file())

    def test_future_build_context_excludes_new_development_inputs(self):
        files = {p.relative_to(C).as_posix() for p in C.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
        sentinels = {'development/future.py', 'tests/new_test.py', 'local/evidence/probe.py',
                     'evidence/trace.txt', 'local_dev/preview_controls.py', '.env', 'data/task/x.json'}
        rules = (C / '.dockerignore').read_text('utf8').splitlines()
        copied = build_inputs((C / 'Dockerfile').read_text('utf8'), rules, files | sentinels)
        permitted = {p for p in files if p.startswith('src/') and p != 'src/pages/gui_simulation.py'} | {
            'start.py', 'requirements.txt', 'requirements_docker.txt', 'pyproject.toml', 'pdm.lock', 'LICENSE', 'Dockerfile', '.dockerignore', 'README.md'}
        self.assertEqual([], sorted(copied - permitted))
        self.assertIn('src/start.py', copied)
        self.assertIn('requirements.txt', copied)
        controlled = {'src/start.py', 'development/x.py', 'requirements.txt'}
        for copy in ('COPY . /app', 'COPY ./ /app'):
            self.assertEqual({'src/start.py', 'requirements.txt'},
                build_inputs(copy, ['*', '!src', '!src/**', '!requirements.txt'], controlled))

    def test_imported_product_modules_are_in_actual_build_inputs(self):
        files = {p.relative_to(C).as_posix() for p in C.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
        copied = build_inputs((C/'Dockerfile').read_text('utf8'), (C/'.dockerignore').read_text('utf8').splitlines(), files)
        missing = []
        for path in (C/'src').rglob('*.py'):
            if path.relative_to(C).as_posix() not in copied:
                continue
            for name in roots(ast.parse(path.read_text('utf-8-sig')), '.'.join(path.relative_to(C).parts[:-1])):
                module = name.replace('.', '/') + '.py'
                if name.startswith('src.') and module in files and module not in copied:
                    missing.append((path.relative_to(C).as_posix(), module))
        self.assertEqual([], missing)

    def test_alias_detectors_and_expected_context_controls(self):
        samples = ["import os; env = os.environ; value = env.get('BAR_UAT_MODE')",
            "from os import environ as env; value = env['BAR_UAT_MODE']",
            "from os import getenv as read; value = read('BAR_UAT_MODE')"]
        for sample in samples:
            self.assertEqual(['BAR_UAT_MODE'], list(env_reads(ast.parse(sample))))
        source = "from importlib import import_module as load; other = load; other('local_dev.controls')"
        self.assertIn('local_dev.controls', list(roots(ast.parse(source))))

    def test_safety_guards_reject_actual_invalid_inputs(self):
        ns = guard_namespace((C / 'src/rename/plan_execution.py').read_text('utf8'))
        error = ns['PlanExecutionError']
        for policy in (None, SimpleNamespace(dry_run=True)):
            with self.assertRaises(error):
                ns['require_approved_execution_policy'](policy)
        self.assertIsNone(ns['require_approved_execution_policy'](SimpleNamespace(dry_run=False)))
        with self.assertRaisesRegex(error, '预览任务禁止执行'):
            ns['_execute_locked'](SimpleNamespace(_read_task=lambda _: {'preview_only': True}), 'x', 'fingerprint')
        plan = SimpleNamespace(execution_policy=SimpleNamespace(dry_run=False))
        with self.assertRaisesRegex(error, 'BAR_DRY_RUN'):
            ns['_validate_runtime_safety_gate'](plan)
        ns['is_dry_run_enabled'] = lambda: False
        self.assertIsNone(ns['_validate_runtime_safety_gate'](plan))

if __name__ == '__main__':
    unittest.main(verbosity=2)
