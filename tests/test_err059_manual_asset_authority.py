"""Public manual-asset callback, restart, mixed owners and disconnect controls."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import json
from unittest.mock import patch

from src.pages import plan_review_page as page
from src.rename.movie_asset_scope import validate_movie_authority
from src.rename.plan_review import PlanReviewError, PlanReviewService, entry_id
from src.rename.plan_execution import PlanExecutionService, PlanExecutionError
from src.rename.plan_store import PlanStore
from src.rename.decision_state import DecisionStateStore
from tests import _media_flow_helpers as movie_fixture
from tests import test_isolated_sample_acceptance as isolated_fixture


class ManualAssetAuthorityTests(unittest.TestCase):
    def test_gui_version_change_refreezes_and_restart_rejects_old_approval(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            import asyncio
            from tests.test_rc54_work_affiliation import current_movie_pair
            # Use current admission/completion, not the retired raw path rebaser.
            _fixture, base, plan, _second, service = asyncio.run(current_movie_pair(self, root))
            store = service.plan_store
            binding=plan.metadata['catalog_binding_snapshot']
            self.assertGreater(binding['revision'],0)
            before=store.fingerprint(plan)
            service.approve_entries(plan.task_id,before,tuple(entry_id(e) for e in plan.entries))
            state=service.decision_state_store.load(plan.task_id)
            video=next(e for e in plan.entries if e.media_kind=='video')
            adopted=[]
            controls={'presentation_group':video.presentation_asset.presentation_group,
                'presentation_role':'content_version','presentation_subtype':None,'presentation_owner':None,
                'presentation_edition_key':'directors-cut','presentation_edition_label':'导演剪辑版',
                'presentation_resolution':'1080p','presentation_dynamic_range':'SDR',
                'presentation_audio':'FLAC 2.0','presentation_preferred':True,'presentation_reason':'人工确认版本'}
            dialog=SimpleNamespace(plan=plan,review_service=service,selected_ids={entry_id(video)},
                _adopt_revision=adopted.append,**{k:SimpleNamespace(value=v) for k,v in controls.items()})
            with patch.object(page,'notify') as notices:
                asyncio.run(page.PlanReviewDialog._save_presentation_asset(dialog))
            self.assertEqual(1,len(adopted),notices.call_args_list)
            revised=store.load(plan.task_id)
            self.assertEqual(store.fingerprint(revised),store.fingerprint(adopted[0]))
            current=service.decision_state_store.load(plan.task_id)
            self.assertGreater(current.revision,state.revision)
            self.assertEqual(state.asset_operation,current.asset_operation)
            self.assertNotEqual(state.movie_version_selection,current.movie_version_selection)
            self.assertEqual(state.catalog_link_intent,current.catalog_link_intent)
            self.assertEqual(binding,revised.metadata['catalog_binding_snapshot'])
            self.assertEqual(plan.metadata['canonical_work_root'],revised.metadata['canonical_work_root'])
            validate_movie_authority(revised)
            restarted=PlanReviewService(store,root/'reviews',service.work_catalog.root,task_root=root/'tasks',
                decision_state_root=service.decision_state_store.root,
                root_snapshot_source=service.root_snapshot_source)
            with self.assertRaises(PlanReviewError):
                restarted.approve_entries(plan.task_id,before,tuple(entry_id(e) for e in plan.entries))
            restarted.approve_entries(revised.task_id,store.fingerprint(revised),
                tuple(entry_id(e) for e in revised.entries))
            # Disconnect the Decision producer, not a verifier. A newly selected
            # edition cannot retain the preceding edition's operation authority.
            from src.rename import asset_operation
            from src.rename.projection_authority import ProjectionAuthorityError
            previous_plan=store.path_for(revised.task_id).read_bytes()
            previous_state=restarted.decision_state_store.load(revised.task_id)
            with patch.object(asset_operation,'freeze_movie_operation',side_effect=lambda state,*args:replace(state,asset_operation=None)), \
                    self.assertRaisesRegex(ProjectionAuthorityError, '资产操作权威已断开'):
                restarted.revise_presentation_asset(revised.task_id,store.fingerprint(revised),
                    entry_id(next(e for e in revised.entries if e.media_kind=='video')),
                    presentation_group=video.presentation_asset.presentation_group,role_kind='content_version',
                    edition_key='alternate',edition_label='另一剪辑版',resolution='2160p',
                    dynamic_range='SDR',key_audio='FLAC 2.0',preferred=True,reason='断开producer负例')
            self.assertEqual(previous_plan,store.path_for(revised.task_id).read_bytes())
            self.assertEqual(previous_state,restarted.decision_state_store.load(revised.task_id))
            for entry in base.entries:
                self.assertTrue(entry.source_path.samefile(entry.target_path))
            self.assertFalse(any(e.target_path.exists() for e in revised.entries))

    def test_mixed_group_movie_scope_and_partition_are_required(self):
        from tests.test_err060_role_entry import movie_dialog
        from src.rename.rename_plan import PresentationGroup
        from src.rename.plan_verifier import PlanVerifier
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            dialog,_=movie_dialog(root)
            service=dialog.review_service;plan=dialog.plan;store=service.plan_store
            validate_movie_authority(plan)
            operation=service.decision_state_store.load(plan.task_id).asset_operation.to_dict()
            with self.assertRaises(ValueError):
                validate_movie_authority(replace(plan,metadata={**plan.metadata,
                    'asset_operation':{k:v for k,v in operation.items() if k!='movie_scope'}}))
            mixed=replace(plan,presentation_groups=(*plan.presentation_groups,
                PresentationGroup('mixed-tv','tv_episodes','分集剧集',None)))
            self.assertIn('mixed_presentation_requires_partition',{i.code for i in PlanVerifier().verify(mixed).issues})
            # A changed persisted payload cannot reuse the preceding receipt or approval.
            store.path_for(plan.task_id).write_text(json.dumps(mixed.to_dict()),encoding='utf8')
            before=store.path_for(plan.task_id).read_bytes()
            with self.assertRaises((ValueError,RuntimeError)):
                service.approve_entries(plan.task_id,PlanStore.fingerprint(mixed),tuple(map(entry_id,mixed.entries)))
            execution=PlanExecutionService(store,service,task_root=service.task_root,
                record_root=root/'records',catalog_root=service.work_catalog.root,
                decision_state_root=service.decision_state_store.root)
            with self.assertRaises((ValueError,RuntimeError)):
                execution.execute(plan.task_id,PlanStore.fingerprint(mixed))
            self.assertEqual(before,store.path_for(plan.task_id).read_bytes())
            self.assertFalse(any(e.target_path.exists() for e in mixed.entries))

