"""同任务人工版本通过现有TV事务保存，未确认重复不能获得执行资格。"""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.decision_state import DecisionState, DecisionStateStore
from tests import test_partition_content_contract as partition


def package(root, source):
    payload = next(source.glob('*S01E01*.mkv')).read_bytes()
    result = root / 'version-source'
    result.mkdir()
    for name in ('Show [01][1440x1080][H264_FLAC].mkv',
                 'Show [01][1920x1080][H264_THDx2].mkv',
                 'Show [02][1440x1080][H264_FLAC].mkv'):
        (result / name).write_bytes(payload)
    (result / 'Show [01][1920x1080][H264_THDx2].en.srt').write_text(
        '1\n00:00:00,000 --> 00:00:01,000\nSample\n', encoding='utf-8')
    return result


def with_special(root, source):
    result = package(root, source)
    (result / 'Show [SP01].mkv').write_bytes(next(result.glob('*.mkv')).read_bytes())
    return result


class EpisodeVersionsContracts(unittest.IsolatedAsyncioTestCase):
    flow = partition.PartitionContentContracts.flow
    selected = partition.PartitionContentContracts.selected
    current = partition.PartitionContentContracts.current

    def rows(self, plan):
        return [dict(entry_id=entry_id(e), episode=1,
                     label='16x9' if '1920x1080' in e.source_path.name else '4x3')
                for e in plan.entries if e.media_kind == 'video' and '[01]' in e.source_path.name]

    def save(self, flow, plan, rows=None, **kwargs):
        state = flow.service.decision_state_store.load(plan.task_id)
        return flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
            expected_revision=state.revision, mode='versions',
            edit=dict(rows=self.rows(plan) if rows is None else rows), **kwargs)

    def unchanged(self, flow, plan, state):
        self.assertEqual(PlanStore.fingerprint(plan), PlanStore.fingerprint(flow.store.load(plan.task_id)))
        self.assertEqual(DecisionStateStore.fingerprint(state),
                         DecisionStateStore.fingerprint(flow.service.decision_state_store.load(plan.task_id)))

    async def test_two_versions_subtitle_and_single_control_survive_reload(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, state = await self.selected(flow)
            self.assertTrue(state.confirmed_work)
            self.assertIn('episode_assignment_conflict', {i.code for i in PlanVerifier().verify(plan).issues})
            revised = self.save(flow, plan)
            self.assertEqual(set(), {i.code for i in PlanVerifier().verify(revised).issues})
            videos = [e for e in revised.entries if e.media_kind == 'video']
            self.assertEqual([1, 1, 2], sorted(e.episode_id for e in videos))
            first = [e for e in videos if e.episode_id == 1]
            self.assertEqual({'4x3', '16x9'}, {e.presentation_asset.version_descriptor.display_label for e in first})
            self.assertEqual(2, len({e.presentation_asset.version_descriptor.version_ref for e in first}))
            self.assertEqual(3, len({e.target_path for e in videos}))
            self.assertTrue(all(f'S01E01 - {e.presentation_asset.version_descriptor.display_label}.mkv' in e.target_path.name for e in first))
            second = next(e for e in videos if e.episode_id == 2)
            self.assertTrue(second.target_path.name.endswith('S01E02.mkv'))
            subtitle = next(e for e in revised.entries if e.subtitle is not None)
            owner = next(e for e in first if '1920x1080' in e.source_path.name)
            self.assertEqual(owner.source_path, subtitle.subtitle.associated_video_source_path)
            self.assertTrue(subtitle.target_path.name.startswith(owner.target_path.stem + '.'))
            self.assertEqual((1, 1), (subtitle.season_id, subtitle.episode_id))
            saved = flow.service.decision_state_store.load(plan.task_id)
            self.assertEqual(2, len(saved.episode_versions))
            self.assertEqual(saved.to_dict(), DecisionState.from_dict(saved.to_dict()).to_dict())
            self.assertEqual(PlanStore.fingerprint(revised), PlanStore.fingerprint(flow.store.load(plan.task_id)))
            self.assertEqual(revised.entries, DecisionStateStore.project_plan(saved, revised).entries)
            self.assertFalse(flow.service.load_or_create(revised).approved_entry_ids)

    async def test_label_edit_keeps_ref_and_stale_page_rejected(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, old = await self.selected(flow)
            revised = self.save(flow, plan)
            refs = {e.source_path:e.presentation_asset.version_descriptor.version_ref for e in revised.entries if e.media_kind == 'video'}
            rows = self.rows(revised)
            rows[0]['label'] = '原始画幅'
            newer = self.save(flow, revised, rows)
            newest_state = flow.service.decision_state_store.load(plan.task_id)
            self.assertEqual(refs, {e.source_path:e.presentation_asset.version_descriptor.version_ref for e in newer.entries if e.media_kind == 'video'})
            with self.assertRaises((ValueError, RuntimeError)):
                flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                    expected_revision=old.revision, mode='versions', edit=dict(rows=rows))
            self.unchanged(flow, newer, newest_state)

    async def test_partial_duplicate_and_label_collision_do_not_save(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, state = await self.selected(flow)
            rows = self.rows(plan)
            for bad in (rows[:1], [rows[0], {**rows[1], 'label': rows[0]['label'].upper()}],
                        [{**rows[0], 'label': 'NUL'}, rows[1]],
                        [{**rows[0], 'entry_id': 'foreign'}, rows[1]],
                        [{**rows[0], 'episode': True}, rows[1]]):
                with self.subTest(bad=bad), self.assertRaises((ValueError, RuntimeError)):
                    self.save(flow, plan, bad)
                self.unchanged(flow, plan, state)

    async def test_tampered_or_missing_selection_blocks_verifier(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, _ = await self.selected(flow)
            revised = self.save(flow, plan)
            from src.rename import episode_versions as versions
            metadata = dict(revised.metadata)
            metadata.pop('episode_versions')
            missing = replace(revised, metadata=metadata)
            self.assertTrue(any(e.presentation_asset and e.presentation_asset.version_descriptor
                                and e.presentation_asset.version_descriptor.identity_policy == 'episode-version.v1'
                                for e in missing.entries))
            self.assertIn('episode_versions_invalid', {i.code for i in PlanVerifier().verify(missing).issues})
            with self.assertRaises(ValueError):
                versions.validate_projection(missing)
            video = next(e for e in revised.entries if e.media_kind == 'video' and e.episode_id == 1)
            changed = replace(video, target_path=video.target_path.with_name('forged.mkv'))
            bad = replace(revised, entries=tuple(changed if e is video else e for e in revised.entries))
            self.assertIn('episode_versions_invalid', {i.code for i in PlanVerifier().verify(bad).issues})
            state = flow.service.decision_state_store.load(plan.task_id)
            with self.assertRaises((ValueError, RuntimeError)):
                versions.project(revised, (replace(state.episode_versions[0], source_basis='0' * 64),))

    async def test_identity_edits_reject_and_season_updates_all_versions(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, _ = await self.selected(flow)
            revised = self.save(flow, plan)
            state = flow.service.decision_state_store.load(plan.task_id)
            chosen = self.rows(revised)[0]['entry_id']
            for mode, edit in (('episode', {'projection_fingerprint':'unrelated'}),
                               ('role', {'entry_id':chosen, 'role':'extra'}),
                               ('range', {'entry_id':chosen, 'season':1, 'start':3, 'end':4, 'naming_mode':'tmdb'})):
                with self.subTest(mode=mode), self.assertRaises((ValueError, RuntimeError)):
                    flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(revised),
                        expected_revision=state.revision, mode=mode, edit=edit)
                self.unchanged(flow, revised, state)
            moved = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(revised),
                expected_revision=state.revision, mode='season', edit={'season_number':2})
            self.assertEqual({2}, {d.season for d in flow.service.decision_state_store.load(plan.task_id).episode_versions})
            self.assertEqual(set(), {i.code for i in PlanVerifier().verify(moved).issues})

    async def test_registered_season_and_replacement_rejected(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, state = await self.selected(flow)
            from src.rename import episode_versions as versions
            with patch.object(flow.service.series_catalog, 'load', return_value=SimpleNamespace(
                    season_bindings=({'season_number':1, 'task_id':'other-task'},))):
                with self.assertRaisesRegex(ValueError, '已登记'):
                    versions.require_new_season(flow.service, plan, state)
                with self.assertRaisesRegex((ValueError, RuntimeError), '已登记'):
                    self.save(flow, plan)
                self.unchanged(flow, plan, state)
            from src.rename.asset_operation import AssetOperation
            replacement = AssetOperation.from_mapping(dict(policy='asset-operation-v1', mode='replace',
                slot_key=f"series:{plan.metadata['series_key']}:season:1", replaces=[], previous_assets=[]))
            replacement_state = replace(state, asset_operation=replacement)
            with self.assertRaisesRegex(ValueError, '换源|资产操作'):
                versions.require_new_season(flow.service, plan, replacement_state)
            versions.require_new_season(flow.service, plan, state)
            accepted = self.save(flow, plan)
            self.assertEqual(2, len(accepted.metadata['episode_versions']))

    async def test_disconnect_projector_does_not_publish(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, state = await self.selected(flow)
            from src.rename import episode_versions as versions
            for broken in (None, 'raise'):
                with patch.object(versions, 'project', side_effect=RuntimeError('disconnected') if broken else None,
                                  return_value=None):
                    with self.assertRaises((ValueError, RuntimeError, TypeError, AttributeError)):
                        self.save(flow, plan)
                self.unchanged(flow, plan, state)

    async def test_clear_selection_returns_conflicts_without_source_changes(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            before = {p:p.read_bytes() for p in flow.source.iterdir() if p.is_file()}
            plan, _ = await self.selected(flow)
            revised = self.save(flow, plan)
            cleared = self.save(flow, revised, [])
            self.assertFalse(flow.service.decision_state_store.load(plan.task_id).episode_versions)
            self.assertIn('episode_assignment_conflict', {i.code for i in PlanVerifier().verify(cleared).issues})
            self.assertEqual(before, {p:p.read_bytes() for p in before})

    async def test_ordinary_single_version_omits_new_field(self):
        async with self.flow(automatic=False) as flow:
            plan, state = await self.selected(flow)
            self.assertNotIn('episode_versions', state.to_dict())
            self.assertNotIn('episode_versions', plan.metadata)
            self.assertEqual(set(), {i.code for i in PlanVerifier().verify(plan).issues})

    async def test_special_and_range_are_not_accepted_as_versions(self):
        async with self.flow(automatic=False, configure_source=with_special) as flow:
            plan, state = await self.selected(flow)
            special = next(e for e in plan.entries if '[SP01]' in e.source_path.name and e.media_kind == 'video')
            with self.assertRaises((ValueError, RuntimeError)):
                self.save(flow, plan, [dict(entry_id=entry_id(special), episode=1, label='SP')])
            self.unchanged(flow, plan, state)
            regular = next(e for e in plan.entries if '[02]' in e.source_path.name and e.media_kind == 'video')
            ranged = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='range', edit=dict(entry_id=entry_id(regular),
                    season=1, start=2, end=3, naming_mode='tmdb'))
            saved = flow.service.decision_state_store.load(plan.task_id)
            with self.assertRaises((ValueError, RuntimeError)):
                self.save(flow, ranged, [dict(entry_id=entry_id(regular), episode=2, label='合并版')])
            self.unchanged(flow, ranged, saved)

    async def test_frozen_contract_rejects_unknown_fields_and_wrong_work(self):
        async with self.flow(automatic=False, configure_source=package) as flow:
            plan, _ = await self.selected(flow)
            revised = self.save(flow, plan)
            from src.rename import episode_versions as versions
            saved = flow.service.decision_state_store.load(plan.task_id)
            decision = saved.episode_versions[0]
            with self.assertRaises(ValueError):
                versions.EpisodeVersionDecision.from_mapping({**decision.to_dict(), 'target_path':'forged'})
            with self.assertRaises(ValueError):
                versions.project(revised, (replace(decision, work_key='bangumi:subject:999'),))
