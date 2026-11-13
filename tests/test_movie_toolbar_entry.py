"""执行实际GUI渲染函数，使用内存控件树核对位置、刷新与回调；不导入持久化服务。"""
from pathlib import Path
import ast
import copy
import json
from types import SimpleNamespace as NS
from typing import *
import unittest
from functools import partial

CODE = Path(__file__).resolve().parents[1]

class Node:
    def __init__(self, ui, kind, text='', **kw):
        self.ui, self.kind, self.text, self.kw = ui, kind, text, kw
        self.parent = ui.stack[-1] if ui.stack else None
        self.children = []
        self.tokens = set()
        self.visible = True
        if self.parent: self.parent.children.append(self)
        ui.nodes.append(self)
    def __enter__(self): self.ui.stack.append(self); return self
    def __exit__(self, *args): self.ui.stack.pop()
    def classes(self, value): self.tokens.update(value.split()); return self
    def props(self, value): return self
    def tooltip(self, value): return self
    def clear(self):
        for child in list(self.children): child.clear(); self.ui.nodes.remove(child)
        self.children.clear()
    def set_visibility(self, value): self.visible = value

class UI:
    def __init__(self): self.nodes, self.stack = [], []
    def __getattr__(self, kind): return lambda text='', **kw: Node(self, kind, text, **kw)

class Imports(ast.NodeTransformer):
    def visit_ImportFrom(self, node): return None
    def visit_Import(self, node): return None

def functions(file, names, space):
    tree = ast.parse((CODE / file).read_text('utf-8'))
    found = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    found = [Imports().visit(n) for n in found]
    unit = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] + found, type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), str(file), 'exec'), space)
    return {n.name: space[n.name] for n in found}

def page(movie=True, mapping='inventory_review_draft', kind='movie_continuous'):
    ui = UI(); calls = []
    space = dict(ui=ui, Mapping=Mapping, partial=partial, is_ui_test_mode=lambda: True,
                 is_confirmed_movie_plan=lambda plan: plan.movie)
    forms = functions('src/pages/asset_choice_forms.py', {'render_movie_choices', 'is_initial_movie_selection'}, space)
    methods = functions('src/pages/plan_review_page.py', {'_render_completion_workflow', '_render_completion_choice'}, space)
    methods.update(functions('src/pages/plan_file_list.py', {'_refresh_movie_primary_action'}, space))
    # The toolbar's local imports are presentation dependencies only; provide their unchanged names.
    tree = ast.parse((CODE/'src/pages/plan_file_list.py').read_text('utf-8'))
    tool = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_render_plan_tools')
    tool = Imports().visit(tool)
    space.update(open_content_workbench=lambda *a,**kw: None, notify=lambda *a,**kw: None,
                 open_official_special_picker=lambda *a,**kw: None, open_subtitle_editor=lambda *a,**kw: None)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[tool], type_ignores=[])), 'toolbar', 'exec'), space)
    methods['_render_plan_tools'] = space['_render_plan_tools']
    Dialog = type('Dialog', (), methods); space['PlanReviewDialog'] = Dialog
    Dialog._edit_movie_version_groups = lambda self, key: calls.append(key)
    d = Dialog(); group = NS(group_id='existing-group', kind=kind)
    d.plan = NS(movie=movie, metadata={'mapping_source':mapping,'content_selection':{'version_groups':[]}},
        presentation_groups=[group], work_identity=NS(primary_provider=NS(entity_type='movie' if movie else 'tv')),
        entries=[], execution_policy=None)
    d.snapshot = NS(issues=[], work_identity={'status':'confirmed'})
    d.task_decision = None
    d._render_library_choices = lambda: ui.label('保存位置')
    d._video_options = lambda: {}
    d._selected_movie_primary_sources = lambda: []
    d._render_movie_primary_picker = lambda *a: ui.select('旧计划正片选择')
    d._complete_inventory_draft = lambda *a: None
    d.completion_container = ui.column()
    d._render_completion_workflow()
    d._render_plan_tools()
    return d, ui, calls


layout = NS(page=page)

class MovieToolbarEntry(unittest.TestCase):
    def assert_entry(self, dialog, ui, calls):
        tools = next(node for node in ui.nodes if 'bar-plan-tools' in node.tokens)
        self.assertTrue({'调整计划范围', '内容保留与排除', '特别篇 / OVA'} <= {n.text for n in tools.children})
        editors = []
        for node in list(ui.nodes):
            callback = node.kw.get('on_click') if node.kind == 'button' and node.visible else None
            if callback is not None:
                calls.clear()
                callback()
                if calls:
                    self.assertEqual([dialog.plan.presentation_groups[0].group_id], calls)
                    editors.append(node)
        self.assertEqual(1, len(editors))
        self.assertEqual('正片选择与标签编辑', editors[0].text)
        self.assertIs(tools, editors[0].parent)
        self.assertFalse(any(n.kind == 'button' and n.text in {'选择电影正片', '电影正片版本编辑'} for n in ui.nodes))
        return editors[0]

    def test_first_and_completed_movie_have_one_toolbar_editor(self):
        for mapping in ('inventory_review_draft', 'inventory_smart_completion'):
            with self.subTest(mapping=mapping):
                self.assert_entry(*layout.page(mapping=mapping))

    def test_completion_refresh_preserves_editor_and_uses_current_group(self):
        dialog, ui, calls = layout.page()
        button = self.assert_entry(dialog, ui, calls)
        for kind, mapping in [('tv_episodes', 'inventory_smart_completion'), ('movie_continuous', 'inventory_review_draft')]:
            dialog.plan = NS(**{**dialog.plan.__dict__,
                'presentation_groups': [NS(kind=kind, group_id=kind + '-new')],
                'metadata': {**dialog.plan.metadata, 'mapping_source': mapping}})
            dialog._render_completion_workflow()
            self.assertIs(button, self.assert_entry(dialog, ui, calls))

    def test_tv_hides_movie_action_and_movie_restores_it(self):
        dialog, ui, calls = layout.page()
        button = self.assert_entry(dialog, ui, calls)
        for movie in (False, True):
            dialog.plan.movie = movie
            dialog._render_completion_workflow()
            self.assertEqual(movie, button.visible)
        self.assert_entry(dialog, ui, calls)

    def test_flat_save_location_remains(self):
        _, ui, _ = layout.page()
        self.assertFalse(any(n.kind == 'card' for n in ui.nodes))
        self.assertTrue(any(n.text == '保存位置' for n in ui.nodes))

if __name__ == '__main__':
    unittest.main(verbosity=2)
