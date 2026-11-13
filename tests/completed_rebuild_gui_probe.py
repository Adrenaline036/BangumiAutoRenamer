"""Isolated real NiceGUI client, configured services and registered events."""
import os
import sys
from pathlib import Path


def main():
    root=Path(sys.argv[1]); task_id=sys.argv[2]
    os.environ['BAR_DATA_DIR']=str(root)
    os.environ['BAR_UI_TEST_MODE']='1'
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    import asyncio
    import hashlib
    from nicegui.client import Client
    from nicegui.page import page
    from src.pages import data_table_page as dashboard
    from src.pages.completed_rebuild_view import configured_service
    from src.main_page import main_page

    def snapshot():
        return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and p.suffix != '.lock'}

    async def check():
        from nicegui import core
        core.loop = asyncio.get_running_loop()
        client=Client(page('/rebuild-probe'),request=None)
        with client:
            main_page()
            before=snapshot()
            service=configured_service(); old=service.plans.load(task_id)
            # No original-media integrity audit: a foreign target, not changed
            # source bytes, is the real unsafe retry requiring a manual exit.
            source = old.entries[0].source_path
            target = old.entries[0].target_path
            target.unlink()
            target.write_bytes(b'foreign target')
            unsafe_before = snapshot()
            await dashboard._retry_task(task_id)
            notifications=[message for message in client.outbox.messages if message[1]=='notify']
            assert notifications and notifications[-1][2]['type']=='negative', notifications
            assert notifications[-1][2]['message']
            assert snapshot()==unsafe_before, 'unsafe retry must not write'
            target.unlink()
            os.link(source, target)
            before=snapshot()
            old.entries[0].target_path.unlink()
            before=snapshot()
            buttons=[e for e in client.elements.values() if getattr(e,'text',None)=='重建']
            assert buttons, 'completed card must expose rebuild'
            assert all(e._props.get('color')=='blue-7' for e in buttons)
            button=buttons[0]
            button._handle_event({'listener_id':next(iter(button._event_listeners)),'args':{}})
            # Async event runs the real registered callback, including io_bound.
            for _ in range(100):
                if any(getattr(e,'text',None)=='生成待批准计划' for e in client.elements.values()): break
                await asyncio.sleep(.05)
            assert any(getattr(e,'text',None)=='生成待批准计划' for e in client.elements.values())
            assert snapshot()==before, 'opening rebuild must remain read-only'
            assert service.plans.fingerprint(old)==service.plans.fingerprint(service.plans.load(task_id))
            assert not old.entries[0].target_path.exists()
            for e in list(client.elements.values()):
                if type(e).__name__=='Dialog' and e.value: e.close()
        client.delete()
        print('REBUILD_GUI_PASS')
    asyncio.run(check())


if __name__=='__main__': main()
