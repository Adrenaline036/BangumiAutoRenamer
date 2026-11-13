"""原Webhook函数、真实表单解析和内存ASGI路由的输入合同。"""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
import httpx
import starlette.formparsers

ROOT = Path(__file__).resolve().parents[1]


def isolated_app():
    tree = ast.parse((ROOT / 'src/web.py').read_text(encoding='utf-8-sig'))
    selected = [node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in {'_form_bool', '_send_task'}
                or isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id in {'ANI_TAG', 'MOVIE_TAG'}
                        for target in node.targets)]
    function_names = {node.name for node in selected if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    private_calls = {node.func.id for item in selected for node in ast.walk(item)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.startswith('_')}
    assert private_calls <= function_names, f'未提取被调用的私有函数：{private_calls - function_names}'
    app = FastAPI()
    state = SimpleNamespace(prepared=[], accepted=[], paths=[], exists=True, prepare_error=None, enqueue_error=None)

    class MemoryPath:
        def __init__(self, value):
            state.paths.append(value)
            self.value = value

        def __str__(self):
            return self.value

        def exists(self):
            return state.exists

    def prepare(paths, **kwargs):
        if state.prepare_error:
            raise state.prepare_error
        state.prepared.append((paths, kwargs))
        return {'batch_id': 'memory-batch', 'jobs': paths, 'task_requests': ('memory-request',)}

    async def enqueue(**kwargs):
        if state.enqueue_error:
            raise state.enqueue_error
        assert kwargs == {'batch_id': 'memory-batch', 'jobs': state.prepared[-1][0], 'task_requests': ('memory-request',)}
        state.accepted.append(kwargs)
        return ['memory-task', 'memory-task-2']

    namespace = {'Request': Request, 'TaskModel': dict, 'cast': cast, 'app': app, 'Path': MemoryPath, 'asyncio': asyncio,
                 'logger': SimpleNamespace(info=lambda *_: None, error=lambda *_: None),
                 'task_queue': SimpleNamespace(prepare_input_batch=prepare, enqueue_batch=enqueue)}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(ROOT / 'src/web.py'), 'exec'), namespace)
    return app, state


class WebhookInputContract(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, fields, *, exists=True, multipart=False, file_part=False, failure=None):
        app, state = isolated_app()
        state.exists = exists
        if failure:
            state.prepare_error = failure
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://memory.invalid') as client:
            if file_part:
                response = await client.post('/sendTask', files={'path': ('x.mkv', b'\x00')})
            elif multipart:
                response = await client.post('/sendTask', files={key: (None, value) for key, value in fields.items()})
            else:
                response = await client.post('/sendTask', data=fields)
        return response, state

    def accepted(self, response, state, anime=False, movie=None, path='D:/source'):
        self.assertEqual(200, response.status_code)
        self.assertEqual({'code': 202, 'data': '任务已加入队列，可在 WebUI 查看排队与执行状态。',
                          'task_id': 'memory-task', 'status': 'queued'}, response.json())
        self.assertEqual([path], state.paths)
        self.assertEqual(1, len(state.prepared))
        self.assertEqual(1, len(state.accepted))
        paths, kwargs = state.prepared[0]
        self.assertEqual(path, str(paths[0]))
        self.assertIs(kwargs['is_anime'], anime)
        self.assertIs(kwargs['is_movie'], movie)
        self.assertEqual({'is_anime': anime, 'is_movie': movie, 'source': 'webhook', 'preview_only': False}, kwargs)

    async def test_ascii_normal_control_and_response(self):
        self.accepted(*(await self.invoke({'path': 'D:/source'})))

    async def test_urlencoded_unicode_path_is_unchanged(self):
        for path in ['D:/中文 来源/第01话.mkv', 'D:/café + 100%/作品😀', 'D:/source with trailing space ']:
            with self.subTest(path=path):
                self.accepted(*(await self.invoke({'path': path})), path=path)

    async def test_multipart_unicode_field_is_unchanged(self):
        path = 'D:/繁體測試/中文+路径'
        self.accepted(*(await self.invoke({'path': path}, multipart=True)), path=path)

    async def test_missing_or_empty_path_uses_existing_error(self):
        for fields in [{}, {'path': ''}]:
            with self.subTest(fields=fields):
                response, state = await self.invoke(fields)
                self.assertEqual(200, response.status_code)
                self.assertEqual({'code': 400, 'data': '路径为空！'}, response.json())
                self.assertEqual([], state.paths)
                self.assertEqual([], state.prepared)
                self.assertEqual([], state.accepted)

    async def test_missing_path_precedes_skip(self):
        response, state = await self.invoke({'no_process': 'true', 'tag': 'no_process'})
        self.assertEqual({'code': 400, 'data': '路径为空！'}, response.json())
        self.assertEqual([], state.accepted)

    async def test_file_part_path_is_rejected_without_disk_or_admission(self):
        spools = []
        constructor = starlette.formparsers.SpooledTemporaryFile

        def memory_spool(*args, **kwargs):
            spool = constructor(*args, **kwargs)
            spools.append(spool)
            return spool

        with patch('starlette.formparsers.SpooledTemporaryFile', side_effect=memory_spool), \
             patch('tempfile.TemporaryFile', side_effect=AssertionError('禁止落盘rollover')):
            try:
                response, state = await self.invoke({}, file_part=True)
                self.assertEqual(200, response.status_code)
                self.assertEqual({'code': 400, 'data': '路径为空！'}, response.json())
                self.assertEqual([], state.paths)
                self.assertEqual([], state.prepared)
                self.assertEqual([], state.accepted)
            finally:
                self.assertEqual(1, len(spools))
                for spool in spools:
                    self.assertFalse(spool._rolled)
                    spool.close()

    async def test_nonexistent_path_keeps_response(self):
        response, state = await self.invoke({'path': 'D:/source'}, exists=False)
        self.assertEqual(200, response.status_code)
        self.assertEqual({'code': 404, 'data': '路径D:/source不存在！'}, response.json())
        self.assertEqual([], state.prepared)
        self.assertEqual([], state.accepted)

    async def test_no_process_false_tokens_enqueue(self):
        for value in ['false', '0', 'no', 'off', ' FALSE ', 'Off']:
            with self.subTest(value=value):
                self.accepted(*(await self.invoke({'path': 'D:/source', 'no_process': value})))

    async def test_no_process_true_tokens_skip(self):
        for value in ['true', '1', 'yes', 'on', ' TRUE ']:
            with self.subTest(value=value):
                response, state = await self.invoke({'path': 'D:/source', 'no_process': value}, exists=False)
                self.assertEqual(200, response.status_code)
                self.assertEqual({'code': 202, 'data': 'D:/source忽略, 不处理！'}, response.json())
                self.assertEqual([], state.paths)
                self.assertEqual([], state.accepted)

    async def test_skip_tag_retains_veto_even_with_false_field(self):
        response, state = await self.invoke({'path': 'D:/source', 'no_process': 'false', 'tag': 'anime, NO_PROCESS '})
        self.assertEqual({'code': 202, 'data': 'D:/source忽略, 不处理！'}, response.json())
        self.assertEqual([], state.accepted)

    async def test_unknown_skip_value_keeps_legacy_skip(self):
        response, state = await self.invoke({'path': 'D:/source', 'no_process': 'legacy-value'})
        self.assertEqual(202, response.json()['code'])
        self.assertIn('忽略', response.json()['data'])
        self.assertEqual([], state.accepted)

    async def test_explicit_false_anime_overrides_tag(self):
        for value in ['false', '0', 'no', 'off', ' FALSE ']:
            with self.subTest(value=value):
                self.accepted(*(await self.invoke({'path': 'D:/source', 'is_anime': value, 'tag': 'anime'})), anime=False)

    async def test_explicit_true_anime_is_preserved(self):
        for value in ['true', '1', 'yes', 'on', ' TRUE ']:
            with self.subTest(value=value):
                self.accepted(*(await self.invoke({'path': 'D:/source', 'is_anime': value})), anime=True)

    async def test_missing_empty_anime_uses_tags(self):
        for tag in ['anime', ' Anime ', '动漫', '动画']:
            for extra in [{}, {'is_anime': ''}]:
                with self.subTest(tag=tag, extra=extra):
                    self.accepted(*(await self.invoke({'path': 'D:/source', 'tag': tag, **extra})), anime=True)

    async def test_unknown_anime_value_keeps_auto(self):
        self.accepted(*(await self.invoke({'path': 'D:/source', 'is_anime': 'auto', 'tag': 'anime'})), anime=None)

    async def test_movie_tags_keep_existing_flags(self):
        for tag in ['movie', '电影', '剧场', '剧场版']:
            with self.subTest(tag=tag):
                self.accepted(*(await self.invoke({'path': 'D:/source', 'tag': tag})), movie=True)

    async def test_combined_tags_and_manual_anime_keep_movie(self):
        self.accepted(*(await self.invoke({'path': 'D:/source', 'tag': 'movie, anime', 'is_anime': 'false'})), movie=True)

    async def test_empty_skip_is_not_a_skip(self):
        self.accepted(*(await self.invoke({'path': 'D:/source', 'no_process': ''})))

    async def test_preparation_failure_propagates_without_enqueue(self):
        with self.assertRaisesRegex(RuntimeError, 'memory preparation failure'):
            await self.invoke({'path': 'D:/source'}, failure=RuntimeError('memory preparation failure'))

    def test_route_and_startup_bindings_and_legacy_disconnect(self):
        tree = ast.parse((ROOT / 'src/web.py').read_text(encoding='utf-8-sig'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_send_task')
        self.assertEqual("app.post('/sendTask')", ast.unparse(node.decorator_list[0]))
        calls = [n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertNotIn('enqueue', calls)
        preparations = [n for n in ast.walk(node) if isinstance(n, ast.Await)
            and isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == 'asyncio.to_thread'
            and n.value.args and ast.unparse(n.value.args[0]) == 'task_queue.prepare_input_batch']
        self.assertEqual(1, len(preparations))
        self.assertNotIn('prepare_input_batch', calls)
        self.assertEqual(1, calls.count('enqueue_batch'))
        self.assertIn('app.on_startup(task_queue.start)', ast.unparse(tree))
        self.assertIn('app.on_shutdown(task_queue.stop)', ast.unparse(tree))


if __name__ == '__main__':
    unittest.main()
