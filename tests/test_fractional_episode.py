"""小数集分类、整数正片及原任务继续的隔离验证。"""
import asyncio
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from src.rename.episode_inference import build_semantic_context
from src.rename.media_classifier import preclassify_media, LocalRole, PreclassificationKind, MediaPreclassification, MediaClassification
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from tests import test_default_activation_contract as activation


def classified(name, title='Show'):
    return preclassify_media(Path(name), name, 1, build_semantic_context(name, title))


class FractionalEpisodeRules(unittest.TestCase):
    def test_half_episode_survives_semantic_normalization(self):
        for token in ('[6.5]', '[11.5]', '(12.5)', '【11.5】', '［１１．５］'):
            with self.subTest(token=token):
                result = classified(f'[Group] Show {token}[Ma10p_1080p][x265_flac].mkv')
                self.assertEqual(PreclassificationKind.UNKNOWN, result.kind)
                self.assertEqual(LocalRole.UNKNOWN, result.classification.local_role)
                self.assertIsNone(result.classification.episode_number)
                self.assertEqual(1, result.classification.season_scope)

    def test_title_and_technical_decimals_are_not_episode_markers(self):
        for name, title in (
            ('Show Ver1.1a [01][1080p].mkv', 'Show'),
            ('Show [01][AAC5.1][1080p].mkv', 'Show'),
            ('Show [01][DTS 5.1][1080p].mkv', 'Show'),
            ('Show [v1.5][01][1080p].mkv', 'Show'),
            ('Show [11.5] [01][1080p].mkv', 'Show 11.5'),
            ('Show [01][11.5Mbps].mkv', 'Show'),
        ):
            with self.subTest(name=name):
                self.assertEqual(PreclassificationKind.MAIN_CANDIDATE, classified(name, title).kind)

    def test_existing_bonus_markers_and_integer_control(self):
        for token in ('[12β]', '[12b]', '13(OVA)', '[OAD]'):
            with self.subTest(token=token):
                self.assertEqual(LocalRole.EPISODE_LIKE_BONUS, classified('Show '+token+'.mkv').classification.local_role)
        for episode in range(1, 23):
            self.assertEqual(PreclassificationKind.MAIN_CANDIDATE, classified(f'Show [{episode:02}].mkv').kind)

    def test_legacy_process_sub_keeps_half_in_manual_review(self):
        from src.rename.process import Rename
        worker = object.__new__(Rename)
        worker.R = {}
        source = Path('source') / '[Group] Show [11.5][1080p].mkv'
        worker.process_sub('Show', ['[Group] Show '], source, Path('target'), 1,
                           semantic_context=build_semantic_context(source.name, 'Show'))
        self.assertIn('ManualReview', worker.R[source].parts)
        self.assertNotIn('Associated', worker.R[source].parts)



    def test_legacy_production_half_with_season_or_version_is_pending(self):
        from src.rename.process import Rename
        for suffix in ('S02', 'v2'):
            with self.subTest(suffix=suffix):
                worker = object.__new__(Rename)
                worker.R = {}
                source = Path('source') / f'[Group] Show {suffix} [11.5][1080p].mkv'
                worker.process_sub('Show', None, source, Path('target'), 2,
                                   semantic_context=build_semantic_context(source.name, 'Show'))
                self.assertIn('ManualReview', worker.R[source].parts)
                self.assertNotIn('Associated', worker.R[source].parts)


def incident_source(root, original):
    source = root / 'incident'
    source.mkdir()
    payload = next(original.glob('*.mkv'))
    for episode in [*(f'{n:02}' for n in range(1, 23)), '11.5']:
        name = f'[Group] Ordinary Show [{episode}][Ma10p_1080p][x265_flac].mkv'
        video = source / name
        shutil.copyfile(payload, video)
        for language in ('chs', 'cht'):
            video.with_suffix('.'+language+'.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\n字幕\n', encoding='utf-8')
    (source / 'README.txt').write_text('隔离样本', encoding='utf-8')
    return source


class FractionalEpisodeFlow(unittest.IsolatedAsyncioTestCase):
    def assert_normal(self, plan, excluded=()):
        main = [e for e in plan.entries if e.media_kind == 'video' and e.role == 'main_episode']
        self.assertEqual(22 - len(excluded), len(main))
        self.assertEqual(set(range(1, 23)) - set(excluded), {e.episode_id for e in main})
        self.assertTrue(all(e.season_id == 1 for e in main))
        fraction = next(e for e in plan.entries if e.media_kind == 'video' and '[11.5]' in e.source_path.name)
        self.assertEqual('unknown', fraction.role)
        self.assertIsNone(fraction.episode_id)
        self.assertEqual('unknown', fraction.presentation_asset.asset_role.kind)
        self.assertFalse(plan.metadata.get('episode_assignment_conflict_sources'))
        normal_paths = {e.source_path for e in main}
        subtitles = [e for e in plan.entries if e.media_kind == 'subtitle' and e.subtitle and e.subtitle.associated_video_source_path in normal_paths]
        self.assertEqual(44 - 2 * len(excluded), len(subtitles))
        self.assertEqual(44 - 2 * len(excluded), len({e.target_path for e in subtitles}))
        codes = {i.code for i in PlanVerifier().verify(plan).issues}
        self.assertNotIn('episode_assignment_conflict', codes)
        self.assertNotIn('presentation_group_content_missing', codes)
        self.assertFalse(any(e.target_path.exists() for e in plan.entries))

    async def test_initial_queue_keeps_22_main_and_only_fraction_pending(self):
        async with activation.DefaultActivationProducerContracts().flow(configure_source=incident_source) as flow:
            self.assert_normal(flow.store.load(flow.task_id))

    async def test_original_task_revision_preserves_choices_without_rebuild(self):
        def old_classifier(item_path, candidate_name, season_id, extra_context_name):
            if '[11.5]' in candidate_name:
                return MediaPreclassification(PreclassificationKind.MAIN_CANDIDATE,
                    MediaClassification(LocalRole.UNKNOWN, season_id, 'normal_candidate', None, 'Pending', '旧分类事故样本'))
            return preclassify_media(item_path, candidate_name, season_id, extra_context_name)
        legacy = patch('src.rename.plan_review.preclassify_media', side_effect=old_classifier)
        with legacy:
            context = activation.DefaultActivationProducerContracts().flow(configure_source=incident_source)
            flow = await context.__aenter__()
            old = flow.store.load(flow.task_id)
            from tests.test_content_choice_contract import change
            omitted = next(e.source_path for e in old.entries if e.media_kind == 'video' and '[22]' in e.source_path.name)
            old = change(flow.service, old, [omitted])
        try:
            self.assertTrue(old.metadata.get('episode_assignment_conflict_sources'))
            state = flow.service.decision_state_store.load(flow.task_id)
            sources = {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()}
            first = next(e for e in old.entries if e.media_kind == 'video' and '[01]' in e.source_path.name)
            current = flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(old),
                expected_revision=state.revision, mode='role', edit={'entry_id': entry_id(first), 'role': 'main_candidate'})
            self.assert_normal(current, excluded=(22,))
            after = flow.service.decision_state_store.load(flow.task_id)
            self.assertEqual(old.task_id, current.task_id)
            self.assertEqual(state.confirmed_work, after.confirmed_work)
            self.assertEqual(state.season_binding, after.season_binding)
            self.assertEqual(state.initial_source_basis, after.initial_source_basis)
            self.assertEqual(state.content_selection.to_dict()['excluded_sources'], after.content_selection.to_dict()['excluded_sources'])
            self.assertEqual('main_candidate', after.manual_role_decisions[entry_id(first)])
            self.assertEqual(current, flow.store.load(flow.task_id))
            self.assertEqual(sources, {str(p): p.read_bytes() for p in flow.source.rglob('*') if p.is_file()})
            with self.assertRaises((ValueError, RuntimeError)):
                flow.service.revise_tv_content(flow.task_id, PlanStore.fingerprint(old),
                    expected_revision=state.revision, mode='role', edit={'entry_id': entry_id(first), 'role': 'extra'})
            self.assertEqual(current, flow.store.load(flow.task_id))
        finally:
            await context.__aexit__(None, None, None)


class FractionalManualSpecial(unittest.IsolatedAsyncioTestCase):
    async def test_official_choice_survives_subsequent_role_revision(self):
        from tests import test_initial_tv_completion_contract as initial
        from src.rename.provider_special_evidence import official_special_rows
        root, service, _, result, extras, *_ = await initial.InitialTVCompletionContracts.fixture(
            self, anime=False, exceptional=True, companions=True,
            exceptional_name='Ordinary Show [11.5].mkv')
        plan = result.plan
        source = extras['exceptional']
        video = next(e for e in plan.entries if e.source_path == source)
        rows = official_special_rows(plan, entry_id(video))
        self.assertEqual(1, len(rows))
        state = service.decision_state_store.load(plan.task_id)
        selected = service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
            expected_revision=state.revision, mode='special',
            edit={'entry_id': entry_id(video), 'action': 'confirmed_official_special', 'provider_episode_id': '9001'})
        chosen_state = service.decision_state_store.load(plan.task_id)
        chosen_video = next(e for e in selected.entries if e.source_path == source)
        self.assertEqual((0, 1), (chosen_video.season_id, chosen_video.episode_id))
        main = next(e for e in selected.entries if e.media_kind == 'video' and e.source_path != source)
        revised = service.revise_tv_content(plan.task_id, PlanStore.fingerprint(selected),
            expected_revision=chosen_state.revision, mode='role',
            edit={'entry_id': entry_id(main), 'role': 'main_candidate'})
        final = next(e for e in revised.entries if e.source_path == source)
        after = service.decision_state_store.load(plan.task_id)
        self.assertEqual(chosen_state.provider_special_decisions, after.provider_special_decisions)
        self.assertEqual((chosen_video.target_path, 0, 1), (final.target_path, final.season_id, final.episode_id))
        self.assertFalse(any(e.target_path.exists() for e in revised.entries))
