"""视频归属可独立保存，未完成字幕仍阻止批准。"""
import unittest
from src.rename.plan_review import entry_id
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from tests import test_partition_content_contract as fixtures

def package(root, source, tag='zh-sc'):
    video = source / 'Ordinary Show [SP01].mkv'
    video.write_bytes(next(source.glob('*S01E01*.mkv')).read_bytes())
    video.with_suffix('.' + tag + '.ass').write_text('[Script Info]\nTitle: Sample\n', encoding='utf-8')
    return source

class VideoSubtitleReadiness(unittest.IsolatedAsyncioTestCase):
    flow = fixtures.PartitionContentContracts.flow
    current = fixtures.PartitionContentContracts.current
    selected = fixtures.PartitionContentContracts.selected

    async def check_special(self, tag):
        async with self.flow(automatic=False, configure_source=lambda r,s:package(r,s,tag)) as flow:
            plan, state = await self.selected(flow)
            video = next(e for e in plan.entries if e.media_kind == 'video' and '[SP01]' in e.source_path.name)
            subtitle = next(e for e in plan.entries if e.subtitle is not None)
            revised = flow.service.revise_tv_content(plan.task_id, PlanStore.fingerprint(plan),
                expected_revision=state.revision, mode='special', edit=dict(entry_ids=[entry_id(video)],
                    action='unmatched_extra', provider_episode_id=None))
            saved = flow.service.decision_state_store.load(plan.task_id)
            self.assertEqual('unmatched_extra', saved.provider_special_decisions[entry_id(video)]['action'])
            self.assertEqual(plan.subtitle_override_rules, revised.subtitle_override_rules)
            current_sub = next(e for e in revised.entries if e.source_path == subtitle.source_path)
            self.assertEqual(subtitle.subtitle, current_sub.subtitle)
            self.assertEqual(PlanStore.fingerprint(revised), PlanStore.fingerprint(flow.store.load(plan.task_id)))
            final_video = next(e for e in revised.entries if e.source_path == video.source_path)
            self.assertEqual('disc_extra', final_video.role)
            self.assertEqual('Extras', final_video.target_path.parent.name)
            if tag == 'zh-sc':
                self.assertEqual(subtitle.target_path, current_sub.target_path)
                self.assertIsNone(current_sub.subtitle.primary_language)
                self.assertIn('subtitle_parse_unresolved', {i.code for i in PlanVerifier().verify(revised).issues})
                with self.assertRaisesRegex((ValueError, RuntimeError), 'subtitle_parse_unresolved'):
                    flow.service.approve_entries(plan.task_id, PlanStore.fingerprint(revised), [entry_id(e) for e in revised.entries])
                self.assertFalse(flow.service.load_or_create(revised).approved_entry_ids)
            else:
                final_video = next(e for e in revised.entries if e.source_path == video.source_path)
                self.assertTrue(current_sub.target_path.name.startswith(final_video.target_path.stem + '.en.'))

    async def test_unknown_subtitle_does_not_block_special_save(self):
        await self.check_special('zh-sc')

    async def test_known_subtitle_still_follows_saved_video(self):
        await self.check_special('en')
