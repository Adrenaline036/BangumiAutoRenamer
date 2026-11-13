"""字幕批量修订的隔离事故与兼容控制；不执行媒体整理。"""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from src.rename.content_choice import subtitle_edit_view
from src.rename.plan_review import entry_id, apply_subtitle_rule_entries
from src.rename.plan_store import PlanStore
from src.rename.rename_plan import SubtitleOverrideScope
from tests.test_content_choice_contract import records
from tests.test_content_subtitle_lifecycle import movie as migrated


class SubtitleBatchEditor(unittest.TestCase):
    def test_two_file_rules_share_one_revision_without_provider_scope(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service, current, videos = migrated(root)
            self.assertIsNone(current.series_scope)
            view = subtitle_edit_view(current)
            sources = {video.with_suffix('.sc.ass') for video in videos[:2]}
            selected = [entry for entry in view.entries if entry.source_path in sources]
            self.assertEqual(2, len(selected))
            fingerprint = PlanStore.fingerprint(current)
            before = records(root)
            identifiers = [entry_id(e) for e in selected]
            preview = service.adjust_subtitles(current.task_id, fingerprint, identifiers, main_language='ja', _persist=False)
            self.assertEqual(before, records(root))
            result = service.adjust_subtitles(current.task_id, fingerprint, identifiers, main_language='ja')
            self.assertEqual(2, len(result.subtitle_override_rules))
            self.assertEqual(fingerprint, result.revision_parent_fingerprint)
            self.assertEqual(PlanStore.fingerprint(result), PlanStore.fingerprint(service.plan_store.load(current.task_id)))
            self.assertEqual(current.ignored_sources, result.ignored_sources)
            self.assertTrue(all(e.subtitle.primary_language == 'ja'
                for e in subtitle_edit_view(result).entries if e.source_path in sources))

    def test_explicit_chinese_without_script_is_a_valid_manual_choice(self):
        with tempfile.TemporaryDirectory() as raw:
            service, current, videos = migrated(Path(raw))
            entry = next(e for e in subtitle_edit_view(current).entries
                if e.source_path == videos[0].with_suffix('.sc.ass'))
            revised = service.apply_subtitle_override_rule(current.task_id, PlanStore.fingerprint(current),
                entry_id(entry), scope=SubtitleOverrideScope.FILE,
                primary_language='zh', script=None, reason='人工确认中文不区分简繁')
            target = next(e.target_path for e in revised.entries if e.source_path == entry.source_path)
            self.assertTrue(target.name.endswith('.zh.ass'), str(target))

    def test_existing_single_file_override_preserves_other_choices(self):
        with tempfile.TemporaryDirectory() as raw:
            service, current, videos = migrated(Path(raw))
            entry = next(e for e in subtitle_edit_view(current).entries
                if e.source_path == videos[0].with_suffix('.sc.ass'))
            revised = service.apply_subtitle_override_rule(current.task_id, PlanStore.fingerprint(current),
                entry_id(entry), scope=SubtitleOverrideScope.FILE,
                primary_language='ja', script=None, reason='确认日语字幕')
            self.assertEqual(current.ignored_sources, revised.ignored_sources)
            before = {e.source_path: e for e in current.entries if e.source_path != entry.source_path}
            after = {e.source_path: e for e in revised.entries if e.source_path != entry.source_path}
            self.assertEqual(before, after)
