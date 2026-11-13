"""Component regression for the reported movie entry; not human acceptance."""
from types import SimpleNamespace
import inspect
import unittest
from unittest.mock import patch

from nicegui.client import Client
from nicegui.page import page
from src.pages import plan_review_page as review
from src.pages import asset_choice_forms as forms


class MoviePrimaryEntry(unittest.TestCase):
    def render(self, kind='movie_continuous', *, initial=True, selection=True, entity=None):
        client = Client(page('/component-movie-entry'), request=None)
        self.addCleanup(client.delete)
        metadata = {'mapping_source': 'inventory_review_draft' if initial else 'inventory_smart_completion'}
        if selection:
            metadata['content_selection'] = {'version_groups': []}
        if not initial:
            metadata['movie_version_selection'] = {'groups': []}
        primary = SimpleNamespace(namespace='bangumi' if entity == 'subject' else 'tmdb', entity_type=entity or ('tv' if kind == 'tv_episodes' else 'movie'))
        dialog = SimpleNamespace(plan=SimpleNamespace(metadata=metadata,
            work_identity=SimpleNamespace(status="confirmed", primary_provider=primary)), task_decision=None,
            _selected_movie_primary_sources=lambda: [], _complete_inventory_draft=lambda *a: None)
        group = SimpleNamespace(group_id='movie-1', kind=kind)
        with client:
            review.PlanReviewDialog._render_completion_choice(dialog, group,
                selected_kind=None if initial else kind, video_options={})
            forms.render_movie_choices(dialog, group, kind, {}, [], ui=review.ui,
                edit_versions=lambda *args: None)
        return client, dialog, group

    def test_first_movie_exposes_primary_selection_not_tv_action(self):
        client, _, _ = self.render()
        texts = [getattr(e, 'text', '') for e in client.elements.values()]
        self.assertIn('选择电影正片', texts)
        self.assertNotIn('按季和集智能整理', texts)
        self.assertNotIn('编辑电影版本标签与保留范围', texts)
        self.assertNotIn('整理方式', texts)

    def test_later_movie_retains_version_editor_but_not_tv_action(self):
        client, _, _ = self.render(initial=False)
        texts = [getattr(e, 'text', '') for e in client.elements.values()]
        self.assertIn('选择电影正片', texts)
        self.assertNotIn('编辑电影版本标签与保留范围', texts)
        self.assertNotIn('电影正片版本编辑', texts)
        self.assertNotIn('按季和集智能整理', texts)

    def test_tv_retains_tv_action_without_movie_selector(self):
        client, _, _ = self.render('tv_episodes')
        texts = [getattr(e, 'text', '') for e in client.elements.values()]
        self.assertIn('生成剧集整理计划', texts)
        self.assertNotIn('按季和集智能整理', texts)
        self.assertNotIn('选择电影正片', texts)
        self.assertNotIn('编辑电影版本标签与保留范围', texts)

    def test_primary_button_reuses_existing_editor_callback(self):
        client, dialog, group = self.render()
        calls = []
        # Capture the callback passed to NiceGUI, without any durable operation.
        from nicegui import ui
        with client, patch.object(ui, 'button', wraps=ui.button) as buttons:
            forms.render_movie_choices(dialog, group, 'movie_continuous', {}, [],
                ui=ui, edit_versions=lambda owner, key: calls.append((owner, key)))
        button = next(c for c in buttons.call_args_list if c.args[0] == '选择电影正片')
        self.assertFalse(calls)
        button.kwargs['on_click']()
        self.assertEqual([(dialog, 'movie-1')], calls)

    def test_layout_switch_code_is_deleted_not_hidden(self):
        self.assertFalse(hasattr(review.PlanReviewDialog, '_toggle_movie_segments'))
        body = inspect.getsource(forms.render_movie_choices)
        self.assertNotIn('更改整理方式', body)
        self.assertNotIn('ui.expansion', body)
        self.assertNotIn('ui.checkbox', body)

    def test_bangumi_tv_keeps_generation_not_movie_choice(self):
        client, _, _ = self.render('tv_episodes', entity='subject')
        texts = [getattr(e, 'text', '') for e in client.elements.values()]
        self.assertIn('生成剧集整理计划', texts)
        self.assertNotIn('选择电影正片', texts)

    def test_completed_tv_can_regenerate_after_library_choice(self):
        client, _, _ = self.render('tv_episodes', initial=False)
        texts = [getattr(e, 'text', '') for e in client.elements.values()]
        self.assertIn('重新生成剧集整理计划', texts)
        self.assertNotIn('按季和集智能整理', texts)
        self.assertNotIn('整理方式', texts)


if __name__ == '__main__':
    unittest.main()
