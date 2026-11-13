"""Movie issue buttons use real temporary Decision/Plan transactions, no media execution."""
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.pages import plan_review_page as page
from src.rename.decision_state import DecisionCommandService
from src.rename.inventory_plan import build_inventory_review_draft
from src.rename.movie_assets import inventory_movie_assets
from src.rename.plan_review import PlanReviewService, entry_id
from src.rename.plan_store import PlanStore
from src.rename.movie_version_selection import MovieVersionSelection, validate_selection
from tests.projection_test_support import save_plan_fixture
from tests.test_isolated_sample_acceptance import routing_snapshot_for


def movie_dialog(root, *, segments=False, with_font=False, with_picture=False, dry_run=True, exclude_extras=False):
    """同步用例通过独立事件循环建立当前入队和人工选择状态。"""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from tests._media_flow_helpers import MediaFlow
    from src.rename.initial_movie_completion import RootSnapshotSource
    from src.rename.target_routing import TargetRootSnapshot,ROOT_KEYS
    async def create():
        source=root/'source';source.mkdir()
        videos=[source/name for name in ('Movie [1080p].mkv','Movie [2160p].mkv','Movie 予告編_SAMPLE.mkv','Unused [720p].mkv')]
        for video in videos:
            video.write_bytes(b'isolated media')
            video.with_suffix('.mka').write_bytes(b'isolated audio')
            video.with_suffix('.sc.ass').write_bytes(b'[Script Info]\nScriptType: v4.00+\n')
        if with_font:(source/'[Fonts].zip').write_bytes(b'excluded archive')
        if with_picture:(source/'Cover.jpg').write_bytes(b'excluded picture')
        helper=MediaFlow('runTest')
        result=await helper._run_provider_flow(root,source,'movie-role')
        service=PlanReviewService(result.plan_store,root/'reviews',root/'work_catalog',
            decision_state_root=root/'decisions',task_root=root/'tasks',record_root=root/'records',
            provider_link_catalog_root=root/'provider_links')
        service.root_snapshot_source=RootSnapshotSource(lambda:TargetRootSnapshot.capture({key:root/'library'/key for key in ROOT_KEYS}))
        from tests.test_content_choice_contract import change
        plan=result.plan
        if not exclude_extras:plan=change(service,plan,[videos[2]],action='restore')
        groups=[dict(sources=list(map(str,videos[:2])),label='完整分段')] if segments else [dict(sources=[str(v)],label=label) for v,label in zip(videos[:2],('1080p - Airota','4K - 院线'))]
        intent=service.prepare_initial_movie_completion(plan.task_id,PlanStore.fingerprint(plan),groups=groups,
            excluded_sources=list(map(str,videos[2:] if exclude_extras else videos[3:])),reason='人工选择电影版本和范围')
        plan=service.commit_initial_movie_completion(intent)
        dialog=SimpleNamespace(plan=plan,review_service=service,
            task_decision=service.decision_state_store.load(plan.task_id),
            decision_command_service=DecisionCommandService(service.decision_state_store,service.plan_store))
        dialog._adopt_revision=lambda revised:setattr(dialog,'plan',revised)
        return dialog,videos
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda:asyncio.run(create())).result()






def change_content(dialog, sources, *, action='exclude'):
    from tests.test_content_choice_contract import change
    dialog.plan=change(dialog.review_service,dialog.plan,sources,action=action)
    dialog.task_decision=dialog.review_service.decision_state_store.load(dialog.plan.task_id)


class MovieRoleEntry(unittest.TestCase):
    def test_current_command_preserves_versions_and_paired_media(self):
        from tests.test_inventory_reuse import source_access
        for segments in (False,True):
            with self.subTest(segments=segments),tempfile.TemporaryDirectory() as raw:
                root=Path(raw);dialog,videos=movie_dialog(root,segments=segments)
                before=dialog.plan
                selection=before.metadata['movie_version_selection']
                paired={videos[2],videos[2].with_suffix('.mka'),videos[2].with_suffix('.sc.ass')}
                with source_access(before.source_root) as reads:
                    change_content(dialog,[videos[2]])
                    self.assertTrue(paired <= set(dialog.plan.ignored_sources))
                    change_content(dialog,[videos[2]],action='restore')
                self.assertEqual([],reads)
                self.assertEqual(selection,dialog.plan.metadata['movie_version_selection'])
                self.assertTrue(paired <= {v.source_path for v in dialog.plan.entries})
                self.assertEqual(before.execution_policy,dialog.plan.execution_policy)
                self.assertEqual(before.ignored_sources,dialog.plan.ignored_sources)
                validate_selection(dialog.plan)
                saved=dialog.review_service.plan_store.load(before.task_id)
                self.assertEqual(PlanStore.fingerprint(dialog.plan),PlanStore.fingerprint(saved))

    def test_selected_primary_cannot_be_silently_reclassified_as_extra(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);dialog,videos=movie_dialog(root)
            before=PlanStore.fingerprint(dialog.plan)
            key=entry_id(next(v for v in dialog.plan.entries if v.source_path==videos[0]))
            with self.assertRaises((ValueError,RuntimeError)):
                dialog.review_service.recomplete_inventory_with_role_decisions(dialog.plan.task_id,before,
                    decisions={key:'extra'},reason='必须通过当前版本选择修改正片')
            self.assertEqual(before,PlanStore.fingerprint(dialog.review_service.plan_store.load(dialog.plan.task_id)))

    def test_invalid_stale_and_disconnected_commands_leave_records(self):
        from tests.test_content_choice_contract import records
        from src.rename import content_choice
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);dialog,videos=movie_dialog(root)
            service=dialog.review_service;plan=dialog.plan
            key=entry_id(next(v for v in plan.entries if v.source_path==videos[2]))
            before=records(root)
            for fingerprint,ids in (('stale',[key]),(PlanStore.fingerprint(plan),['missing'])):
                with self.assertRaises((ValueError,RuntimeError)):
                    service.prepare_content_change(plan.task_id,fingerprint,selected_entry_ids=ids,action='exclude',reason='人工调整')
                self.assertEqual(before,records(root))
            preview=service.prepare_content_change(plan.task_id,PlanStore.fingerprint(plan),selected_entry_ids=[key],action='exclude',reason='人工调整')
            with patch.object(content_choice,'_draft',side_effect=ValueError('disconnected')):
                with self.assertRaises((ValueError,RuntimeError)):
                    service.commit_content_change(preview)
            self.assertEqual(before,records(root))
            change_content(dialog,[videos[0].with_suffix('.mka')])
            before=records(root)
            with self.assertRaises((ValueError,RuntimeError)):
                service.commit_content_change(preview)
            self.assertEqual(before,records(root))


class Node:
    def __init__(self, value=None): self.value = value
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def classes(self, *_): return self
    def props(self, *_): return self
    def tooltip(self, *_): return self


class ChoiceUI:
    def __init__(self): self.expanded = False; self.controls = []
    def column(self): return Node()
    def row(self): return Node()
    def label(self, text): return Node()
    def number(self, text, **kwargs): return Node(kwargs.get('value'))
    def expansion(self, text, **kwargs):
        owner = self
        class Expansion(Node):
            def __enter__(self): owner.expanded = True; return self
            def __exit__(self, *_): owner.expanded = False
        self.controls.append((text, False))
        return Expansion()
    def button(self, text, **kwargs): self.controls.append((text, self.expanded)); return Node()
    def checkbox(self, text, **kwargs): self.controls.append((text, self.expanded)); return Node()


class RoleChoiceUI(unittest.TestCase):
    def test_frozen_tv_has_no_hidden_movie_type_switch(self):
        for selected in ('tv_episodes', 'movie_continuous', None):
            with self.subTest(selected=selected):
                fake = ChoiceUI()
                dialog = SimpleNamespace(plan=SimpleNamespace(metadata={}, work_identity=SimpleNamespace(status='confirmed',
                    primary_provider=SimpleNamespace(namespace='tmdb',entity_type='tv'))),
                    task_decision=SimpleNamespace(season_binding={'season_number': 1, 'decision_source': 'provider'}),
                    _selected_movie_primary_sources=lambda: [], _complete_inventory_draft=lambda *a: None,
                    _complete_movie_with_primary=lambda *a: None,
                    _render_movie_primary_picker=lambda *a: fake.controls.append(('电影正片选择', fake.expanded)))
                with patch.multiple(page, ui=fake):
                    page.PlanReviewDialog._render_completion_choice(dialog,
                        SimpleNamespace(kind='tv_episodes', group_id='tv-episodes'), selected_kind=selected, video_options={})
                self.assertIn(('生成剧集整理计划', False), fake.controls)
                movie = [(label, hidden) for label, hidden in fake.controls if '电影' in label]
                self.assertFalse(movie)
                self.assertNotIn(('更改整理方式', False), fake.controls)

    def test_duplicate_role_issue_hidden_per_entry_only_and_keeps_raw_verifier(self):
        from src.pages.asset_choice_forms import visible_plan_issues
        exceptional = dict(code='provider_special_eligibility_unavailable', source='clip.mkv', message='special unavailable')
        duplicate = dict(code='presentation_asset_role_unknown', source='clip.mkv', message='unknown')
        other = dict(code='presentation_asset_role_unknown', source='other.mkv', message='unknown')
        rows = [dict(entry_id='clip', source='clip.mkv', issue_codes=[exceptional['code'], duplicate['code']]),
                dict(entry_id='other', source='other.mkv', issue_codes=[other['code']])]
        plan = SimpleNamespace(metadata={})
        issues = [exceptional, duplicate, other]
        visible = visible_plan_issues(plan, issues, rows=rows)
        self.assertEqual([exceptional, other], visible)
        self.assertEqual([exceptional, duplicate, other], issues)

    def test_real_renderers_route_special_and_other_unknown_to_separate_actions(self):
        from tests.test_r24_astra_decision_chain_contract import EventUi
        from tests.legacy_exceptional_ui_fixture import render_exceptional_episode_repair
        class Buttons(EventUi):
            def button(self, label, **kwargs):
                node = super().button(label, **kwargs)
                node.callback = kwargs.get('on_click')
                return node
        entries = [SimpleNamespace(source_path=Path('C:/Fixture') / name, media_kind='video') for name in ('clip.mkv', 'other.mkv')]
        clip, other = map(entry_id, entries)
        rows = [dict(entry_id=clip, source=str(entries[0].source_path), issue_codes=[
            'provider_special_eligibility_unavailable', 'presentation_asset_role_unknown']),
            dict(entry_id=other, source=str(entries[1].source_path), issue_codes=['presentation_asset_role_unknown'])]
        calls = []
        dialog = SimpleNamespace(plan=SimpleNamespace(metadata={}, entries=entries, ignored_sources=()), snapshot=SimpleNamespace(rows=rows),
            _rebuild_inventory_role=lambda *args: calls.append(('role', args)),
            _apply_exceptional_special_inline=lambda *args: calls.append(('special', args)))
        fake = Buttons()
        with patch.multiple(page, ui=fake):
            page.PlanReviewDialog._render_asset_role_repair(dialog)
        self.assertEqual({other}, set(fake.selects[0].options))
        fake.selects[0].value = other
        fake.buttons['设为特典并重建'].callback()
        render_exceptional_episode_repair(dialog, ui_api=fake, entry_identifier=entry_id,
            file_name=lambda p: p.name, render_partition_action=lambda cb: None)
        self.assertEqual({clip}, set(fake.selects[1].options))
        self.assertTrue(fake.buttons['保留为附加内容'].enabled)
        fake.buttons['保留为附加内容'].callback()
        self.assertEqual([('role', (other, 'extra')), ('special', ([clip], 'unmatched_extra', None))], calls)
        self.assertEqual(2, len(rows[0]['issue_codes']))


if __name__ == '__main__':
    unittest.main()
