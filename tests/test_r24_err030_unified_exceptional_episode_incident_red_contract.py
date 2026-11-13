"""Incident-shaped RED for the unified exceptional-episode review contract.

The test uses real AddTask, queue/worker, persisted Plan/Decision, the actual
review-dialog issue composition and the retired binary-entry rejection. Only
Provider transport and temporary filesystem roots are frozen.
"""

from __future__ import annotations
from tests.queue_fixture import TaskQueueManager as ControlledQueue

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.plan_execution import PlanExecutionError
from src.task_queue import TaskQueueManager
import tests.test_complex_library_special_red_contract as special_fixture


FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "r24_err030_unified_exceptional_episode_incident.json"
    ).read_text("utf-8")
)
class _SG0ExceptionalSearch(special_fixture._SteinsSearch):
    def get_season_info(self, provider_id, season_number):
        if (int(provider_id), int(season_number)) == (78102, 0):
            row = FIXTURE["provider_special"]
            return {
                "season_number": 0,
                "episodes": (
                    {
                        "id": int(row["provider_episode_id"]),
                        "episode_number": int(row["episode_number"]),
                        "name": str(row["title"]),
                        "air_date": str(row["air_date"]),
                        "runtime": int(row["runtime"]),
                    },
                ),
            }
        return super().get_season_info(provider_id, season_number)


def _configured_renamer(root: Path):
    renamer = special_fixture._configured_renamer(root)
    renamer.search = _SG0ExceptionalSearch()
    return renamer


class ERR030UnifiedExceptionalEpisodeIncidentRed(unittest.IsolatedAsyncioTestCase):
    async def _parent_flow(self, root: Path, *, include_audio=False, include_fonts=False):
        package = (
            root
            / "sources"
            / "[VCB-Studio] Steins;Gate 0 [Ma10p_1080p]"
        )
        paths = {}
        for name, relative in FIXTURE["files"].items():
            payload = (
                b"[Script Info]\nScriptType: v4.00+\n"
                if str(relative).endswith(".ass")
                else name.encode("utf-8")
            )
            paths[name] = special_fixture._write(package / relative, payload)
        if include_audio:
            paths["cross_work_audio"] = special_fixture._write(paths["cross_work"].with_suffix(".mka"), b"synthetic audio")
        for episode in range(2, 24):
            name = f"regular_{episode:02d}"
            paths[name] = special_fixture._write(
                package
                / (
                    f"[VCB-Studio] Steins;Gate 0 [{episode:02d}]"
                    "[Ma10p_1080p][x265_flac].mkv"
                ),
                name.encode("ascii"),
            )
        if include_fonts:
            import zipfile
            with zipfile.ZipFile(package / 'Series [Fonts].zip', 'w') as archive:
                archive.writestr('Example.ttf', b'synthetic-font')
        result = await special_fixture.ComplexLibrarySpecialRedContracts._run_via_add_task(
            self,
            root / "flow",
            package,
            FIXTURE["subject_id"],
            renamer_factory=_configured_renamer,
        )
        return result, package, paths

    @staticmethod
    def _manager(root: Path, result) -> TaskQueueManager:
        return ControlledQueue(
            task_root=result.tasks,
            decision_state_root=result.decisions,
            recovery_abandon_audit_root=root / "flow" / "abandon_audit",
            plan_root=result.plans,
            batch_scope_root=root / "flow" / "task_batch_scope",
            test_mode=True,
        )

    @staticmethod
    def _entry_for(plan, source: Path):
        matches = [entry for entry in plan.entries if entry.source_path == source]
        if len(matches) != 1:
            raise AssertionError(f"expected one persisted Plan entry for {source.name}: {matches}")
        return matches[0]

    async def test_real_issue_composition_offers_all_actions_for_overflow_and_special(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _package, paths = await self._parent_flow(root)
            plan = result.plan_store.load(result.task_id)
            verification = PlanVerifier().verify(plan)
            by_source = {
                source: {
                    issue.code
                    for issue in verification.issues
                    if issue.source_path == source
                }
                for source in (paths["overflow"], paths["cross_work"])
            }
            render_issue_actions = vars(
                special_fixture.ComplexLibrarySpecialRedContracts
            )["_render_issue_actions"]
            _dialog, ui, _selected_entry = render_issue_actions(
                self, result, selected_source=paths["cross_work"]
            )

            overflow_id = entry_id(self._entry_for(plan, paths["overflow"]))
            cross_work_id = entry_id(self._entry_for(plan, paths["cross_work"]))
            selector_key_sets = [
                set(options)
                for options, _node in ui.selects
                if isinstance(options, dict)
            ]
            problems = []
            if "provider_regular_episode_overflow_unresolved" not in by_source[paths["overflow"]]:
                problems.append("sanitized E24 incident did not reach the real overflow issue")
            if "provider_special_decision_required" not in by_source[paths["cross_work"]]:
                problems.append("sanitized 23beta incident did not reach the real special issue")
            if not any({overflow_id, cross_work_id}.issubset(keys) for keys in selector_key_sets):
                problems.append("real issue composition has no unified selector for E24 and 23beta")
            for action in FIXTURE["required_actions"]:
                if action not in ui.callbacks:
                    problems.append(f"real issue composition does not expose action: {action}")
            self.assertEqual([], problems)

    async def test_retired_multi_video_partition_preserves_all_sources_and_records(self):
        from tests.test_partition_content_contract import records
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _package, paths = await self._parent_flow(root, include_audio=True)
            plan = result.plan_store.load(result.task_id)
            for subtitle_source, video_source in (
                (paths['overflow_subtitle'], paths['overflow']),
                (paths['cross_work_subtitle'], paths['cross_work']),
            ):
                subtitle = self._entry_for(plan, subtitle_source).subtitle
                self.assertIsNotNone(subtitle)
                self.assertEqual(video_source, subtitle.associated_video_source_path)
            source_before = {p: p.read_bytes() for p in paths.values()}
            def snapshot():
                return {**records(root / 'flow'), **{
                    'reviews/' + str(p.relative_to(root / 'flow' / 'reviews')): p.read_bytes()
                    for p in (root / 'flow' / 'reviews').rglob('*.json')}}
            before = snapshot()
            manager = self._manager(root, result)
            with patch.object(manager.source_partition_store, 'prepare',
                              side_effect=AssertionError('retired publisher reached')):
                with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡.*预览并调整分组'):
                    await manager.partition_task_sources(parent_task_id=result.task_id,
                        expected_plan_fingerprint=PlanStore.fingerprint(plan),
                        selected_entry_ids=tuple(entry_id(self._entry_for(plan, paths[key]))
                                                 for key in ('overflow', 'cross_work')),
                        reason='旧任务也不能调用旧二分发布')
            self.assertEqual(before, snapshot())
            self.assertEqual(source_before, {p: p.read_bytes() for p in source_before})

    async def test_retired_partition_rejects_stale_duplicate_and_all_video_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, _package, paths = await self._parent_flow(root)
            plan = result.plan_store.load(result.task_id)
            manager = self._manager(root, result)
            await manager.start()
            try:
                overflow_id = entry_id(self._entry_for(plan, paths["overflow"]))
                with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡.*预览并调整分组'):
                    await manager.partition_task_sources(
                        parent_task_id=result.task_id,
                        expected_plan_fingerprint="0" * 64,
                        selected_entry_ids=(overflow_id,),
                        reason="stale must fail closed",
                    )
                with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡.*预览并调整分组'):
                    await manager.partition_task_sources(
                        parent_task_id=result.task_id,
                        expected_plan_fingerprint=PlanStore.fingerprint(plan),
                        selected_entry_ids=(overflow_id, overflow_id),
                        reason="duplicate must fail closed",
                    )
                all_video_ids = tuple(
                    entry_id(entry)
                    for entry in plan.entries
                    if entry.media_kind == "video"
                )
                with self.assertRaisesRegex(PlanExecutionError, '旧二分拆卡.*预览并调整分组'):
                    await manager.partition_task_sources(
                        parent_task_id=result.task_id,
                        expected_plan_fingerprint=PlanStore.fingerprint(plan),
                        selected_entry_ids=all_video_ids,
                        reason="parent cannot lose every video",
                    )
                self.assertEqual(
                    PlanStore.fingerprint(plan),
                    PlanStore.fingerprint(result.plan_store.load(result.task_id)),
                )
            finally:
                await manager.stop()


    def test_red_wrapper_and_fixture_are_bounded(self):
        fixture_text = json.dumps(FIXTURE, ensure_ascii=False)
        self.assertNotIn("D:\\Anime", fixture_text)
        self.assertNotIn("C:\\Users", fixture_text)
        self.assertEqual(3, len(FIXTURE["required_actions"]))


if __name__ == "__main__":
    unittest.main()
