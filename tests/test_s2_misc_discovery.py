"""S2 discovery consumes full observations but need not create all-misc cards."""
import asyncio
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.pages import add_task_page as page
from src.rename.input_directory_discovery import discover_sources
from tests.test_content_choice_contract import records
from tests import test_content_policy_contract as policy_fixture
from tests import test_input_directory_discovery_contract as discovery_fixture


class MiscDiscoveryContracts(unittest.IsolatedAsyncioTestCase):
    flow = discovery_fixture.InputDirectoryDiscoveryContracts.flow
    configure = discovery_fixture.InputDirectoryDiscoveryContracts.configure
    admit_ui = discovery_fixture.InputDirectoryDiscoveryContracts.admit_ui

    def known_groups(self, root, source):
        payload = next(source.glob('*S01E01*.mkv'))
        incoming = root/'incoming'
        self.watchable = set()
        for season in range(1,6):
            folder = incoming/f'Season {season}'
            folder.mkdir(parents=True)
            video = folder/f'Ordinary Show S{season:02d}E01.mkv'
            shutil.copyfile(payload, video)
            subtitle = video.with_suffix('.srt')
            subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\ntext\n', encoding='utf-8')
            self.watchable.update((video,subtitle))
        for name, data in (('Featurettes/Show [Trailer].mkv', payload.read_bytes()),
                           ('Scans/cover.jpg',b'scan'),('CDs/track.flac',b'disc'),('README.txt',b'doc')):
            path = incoming/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(data)
        self.original_files = {p:p.read_bytes() for p in incoming.rglob('*') if p.is_file()}
        return incoming

    async def test_real_add_task_skips_only_pure_misc_groups_and_keeps_five_seasons(self):
        async with self.flow(configure_source=self.known_groups, configure_worker=self.configure,
                             admit=self.admit_ui) as flow:
            self.assertEqual(5,len(self.actual_ids))
            assigned=[]
            for key in self.actual_ids:
                task=flow.manager._read(key)
                self.assertFalse(task.get('source_group_pending'))
                self.assertEqual('need_confirm',task['workflow_status'],task.get('error'))
                assigned.extend(Path(r['source_path']) for r in task['source_group_observation']['identities'])
                self.assertEqual(task['task_source_scope']['source_identities'],task['source_group_observation']['identities'])
                plan=flow.store.load(key)
                self.assertFalse(flow.service.load_or_create(plan).approved_entry_ids)
                self.assertTrue(all(not e.target_path.exists() for e in plan.entries))
            self.assertEqual(len(assigned),len(set(assigned)))
            self.assertEqual(self.watchable,set(assigned))
            self.assertEqual({str(p.parent) for p in self.watchable},set(self.provider_calls))
            self.assertEqual(self.original_files,{p:p.read_bytes() for p in self.original_files})

    async def test_all_misc_ui_creates_nothing_and_explains_no_task_needed(self):
        def only_misc(root,source):
            incoming=root/'incoming'
            (incoming/'Scans').mkdir(parents=True)
            (incoming/'Scans/cover.jpg').write_bytes(b'scan')
            (incoming/'README.txt').write_bytes(b'doc')
            return incoming
        notices=[]
        async def admit(manager, source, task_id):
            self.before_admission=records(manager.task_root.parent)
            await self.admit_ui(manager,source,task_id)
        with patch.object(page,'notify',lambda message,**_kw:notices.append(message)):
            async with self.flow(configure_source=only_misc,configure_worker=self.configure,
                                 admit=admit) as flow:
                self.assertEqual([],self.actual_ids)
                self.assertEqual([],self.provider_calls)
                self.assertFalse(list(flow.store.root.glob('*.json')))
                self.assertEqual(self.before_admission,records(flow.root))
                self.assertIn('无需创建任务',' '.join(notices))

    async def test_mixed_unknown_companions_and_fonts_keep_complete_group(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            source=root/'sources'/'Featurettes'
            source.mkdir(parents=True)
            for name in ('Show [CM].mkv','Unknown OVA.mkv','Unknown.srt','Unknown.mka','Fonts.zip'):
                (source/name).write_bytes(b'disposable source; never opened as archive')
            fixture=policy_fixture.ContentPolicyContracts('runTest')
            manager=fixture.manager(root)
            try:
                discovery=discover_sources((source.parent,))
                batch=manager.prepare_discovered_batch(discovery,is_anime=None,is_movie=None,source='ui')
                self.assertEqual(1,len(batch['jobs']))
                self.assertEqual(set(source.iterdir()),{Path(r.source_path) for r in batch['jobs'][0].source_group.identities})
            finally:
                await manager.stop()

    async def test_drift_or_truncated_batch_cannot_skip_unknown_sources(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            source=root/'sources'
            source.mkdir()
            (source/'Unknown.mkv').write_bytes(b'video')
            (source/'README.txt').write_bytes(b'doc')
            fixture=policy_fixture.ContentPolicyContracts('runTest')
            manager=fixture.manager(root)
            try:
                discovery=discover_sources((source,))
                batch=manager.prepare_discovered_batch(discovery,is_anime=None,is_movie=None,source='ui')
                before=records(root)
                with self.assertRaises((ValueError,RuntimeError)):
                    await manager.enqueue_batch(**{**batch,'jobs':(),'task_requests':()})
                self.assertEqual(before,records(root))
                (source/'New OVA.mkv').write_bytes(b'new source')
                before=records(root)
                with self.assertRaisesRegex(ValueError,'变化'):
                    await manager.enqueue_batch(**batch)
                self.assertEqual(before,records(root))
            finally:
                await manager.stop()

    async def test_direct_video_and_selected_disc_root_are_not_guessed_from_bare_words(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            fixture=policy_fixture.ContentPolicyContracts('runTest')
            manager=fixture.manager(root)
            try:
                for relative in ('Trailer/Trailer.mkv','CDs/track.flac','Scans/cover.jpg'):
                    path=root/relative
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(b'unknown explicit-root content')
                    for source in (path,path.parent):
                        discovery=discover_sources((source,))
                        batch=manager.prepare_discovered_batch(discovery,is_anime=None,is_movie=None,source='ui')
                        self.assertEqual(1,len(batch['jobs']))
            finally:
                await manager.stop()

    async def test_disconnected_group_selector_cannot_prepare_or_publish(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            source=root/'sources'
            source.mkdir()
            (source/'Unknown.mkv').write_bytes(b'video')
            fixture=policy_fixture.ContentPolicyContracts('runTest')
            manager=fixture.manager(root)
            try:
                discovery=discover_sources((source,))
                batch=manager.prepare_discovered_batch(discovery,is_anime=None,is_movie=None,source='ui')
                before=records(root)
                with patch('src.rename.input_directory_discovery.discovery_task_groups',return_value=None):
                    with self.assertRaises((TypeError,ValueError)):
                        manager.prepare_discovered_batch(discovery,is_anime=None,is_movie=None,source='ui')
                    with self.assertRaises((ValueError,RuntimeError)):
                        await manager.enqueue_batch(**batch)
                self.assertEqual(before,records(root))
            finally:
                await manager.stop()


if __name__=='__main__':
    unittest.main()
