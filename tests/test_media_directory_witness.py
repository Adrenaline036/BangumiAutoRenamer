"""真实媒体硬链接前的目录创建凭据。"""
import json,os,unittest
from pathlib import Path
from unittest.mock import patch
from tests._media_flow_helpers import approved_tv

class MediaDirectoryWitness(unittest.IsolatedAsyncioTestCase):
    async def test_directory_witnesses_persist_before_first_hardlink(self):
        service,plan,fingerprint=await approved_tv(self)
        targets={str(e.target_path):e for e in plan.entries}
        missing={target:[] for target in targets}
        for target in targets:
            parent=Path(target).parent
            while not parent.exists():
                missing[target].append(parent);parent=parent.parent
        self.assertTrue(any(missing.values()))
        seen=[];link=os.link
        def observe(source,target,*args,**kwargs):
            if str(target) in targets and not seen:
                ledger=json.loads((service.record_root/(plan.task_id+'.ledger.json')).read_text('utf-8'))
                self.assertEqual(fingerprint,ledger['plan_fingerprint'])
                rows={row['path']:row for row in ledger['directory_witnesses']}
                self.assertFalse(Path(target).exists())
                for parent in missing[str(target)]:
                    row=rows[str(parent)];fact=parent.stat()
                    self.assertEqual((fact.st_dev,fact.st_ino),(row['dev'],row['ino']))
                    self.assertEqual(plan.task_id,row['task_id'])
                    self.assertEqual(fingerprint,row['plan_fingerprint'])
                    self.assertTrue(row['created'])
                seen.append(str(target))
            return link(source,target,*args,**kwargs)
        with patch('src.rename.trans.os.link',side_effect=observe),patch('src.rename.trans.cm.get_config',return_value='链接'),patch.dict(os.environ,{'BAR_DRY_RUN':'false'}):
            result=service.execute(plan.task_id,fingerprint)
        self.assertEqual(1,len(seen))
        self.assertEqual('completed',result.ledger_status)
        self.assertTrue(all(os.path.samefile(e.source_path,e.target_path) for e in plan.entries))
