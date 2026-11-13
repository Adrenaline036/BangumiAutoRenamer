"""已有人工字幕决定经过实际保存后进入作品更正入口。"""
from contextlib import asynccontextmanager
from unittest.mock import patch
from tests.test_general_work_correction_contract import GeneralWorkCorrectionContracts

class ExistingWorkLifecycle(GeneralWorkCorrectionContracts):
    @asynccontextmanager
    async def owner(self):
        from tests.test_complex_library_special_red_contract import _UiNode
        with patch.object(_UiNode, 'is_deleted', False, create=True):
            async with super().owner() as value:
                yield value
