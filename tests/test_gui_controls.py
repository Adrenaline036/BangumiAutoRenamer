"""按钮与配置的事故形状验证；只创建隔离GUI元素。"""
import ast
import re
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, AsyncMock
from nicegui import ui, Client
from nicegui.page import page
from src.pages import config_page as config, work_affiliation_view as affiliation
from src.pages import plan_review_page
from src.element.color import MAINC


def button_callback(button):
    callbacks=[]
    with patch('nicegui.elements.button.handle_event',side_effect=lambda callback,_:callbacks.append(callback)):
        next(e for e in button._event_listeners.values() if e.type=='click').handler(None)
    assert len(callbacks)==1
    return callbacks[0]


class GuiControls(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client=Client(page('/isolated-controls'),request=None)
        self.addCleanup(self.client.delete)

    def render_affiliation(self,pending):
        owner=NS(plan=NS(work_identity=NS(status='confirmed')),task_decision=NS(),task_data={})
        with self.client, patch.object(plan_review_page,'is_fresh_unexecuted_task',return_value=True), patch.object(affiliation,'pending_from_plan',return_value=pending):
            affiliation.render(owner,ui=ui,notify=lambda *a,**k:None)
        return owner,[e for e in self.client.elements.values() if isinstance(e,ui.button)]

    async def test_pending_has_one_complete_independent_entry(self):
        owner,buttons=self.render_affiliation(NS(to_dict=lambda:{'recommendations':[{'title':'示例','root':'/isolated/library'}]}))
        independent=[b for b in buttons if b.text in ('作为独立作品整理','作为另一部作品整理')]
        self.assertEqual(1,len(independent),'疑似重复提示仍出现两个独立入口')
        merge=next(b for b in buttons if b.text=='归入已有作品')
        self.assertEqual(merge._props.get('color'),independent[0]._props.get('color'))
        self.assertIn('bar-review-action',independent[0].classes)
        callback=button_callback(independent[0])
        with patch.object(affiliation,'independent',new_callable=AsyncMock) as complete:
            await callback()
            self.assertIs(owner,complete.await_args.args[0])

    async def test_normal_independent_entry_remains(self):
        _,buttons=self.render_affiliation(None)
        self.assertEqual(['作为独立作品整理','归入已有作品'],[b.text for b in buttons])

    def config_dialog(self):
        values=dict(api_key='',anime_path='/isolated/Anime',anime_movie_path='/isolated/AnimeMovies',movie_path='/isolated/Movies',bangumi_path='/isolated/TV',bangumi_enabled=True,bangumi_token='',bangumi_user_agent='BAR/example',mode='链接',docker_mnt='/isolated',log_level='DEBUG')
        with self.client,patch.object(config.cm,'get_config',side_effect=values.get):dialog=config.ConfigPage()
        return dialog

    async def test_config_labels_placeholder_grid_and_appended_browse(self):
        dialog=self.config_dialog();elements=list(dialog.descendants())
        labels=[getattr(e,'text','') for e in elements]
        for text in ('Bangumi 动画检索','Bangumi Token','Bangumi UA'):self.assertIn(text,labels)
        token=next((e for e in elements if isinstance(e,ui.input) and e._props.get('placeholder')=='可选'),None)
        self.assertIsNotNone(token,'Token没有空值占位')
        self.assertEqual('password',token._props.get('type'))
        browse=[e for e in elements if isinstance(e,ui.button) and e._props.get('icon') in ('folder_open','o_folder_open')]
        self.assertEqual(4,len(browse))
        self.assertTrue(all(b.parent_slot.name=='append' and isinstance(b.parent_slot.parent,ui.input) for b in browse))
        keys=('anime_path','anime_movie_path','movie_path','bangumi_path')
        expected={getattr(dialog.config,k):k for k in keys}
        self.assertEqual(set(expected),{b.parent_slot.parent.value for b in browse})
        self.assertEqual(4,len({b.parent_slot.parent.id for b in browse}))
        for button in browse:
            callback=button_callback(button)
            with patch.object(dialog,'pick',new_callable=AsyncMock) as pick:
                await callback(None)
                pick.assert_awaited_once_with(key=expected[button.parent_slot.parent.value])

    async def test_toggle_spread_with_icons_and_original_red(self):
        dialog=self.config_dialog();toggles=[e for e in dialog.descendants() if isinstance(e,ui.toggle)]
        self.assertEqual(3,len(toggles))
        for toggle in toggles:
            self.assertTrue(toggle._props.get('spread'),'分段选项未铺满')
            self.assertEqual(MAINC,toggle._props['toggle-color'])
            self.assertTrue(all(o.get('icon','').startswith('o_') for o in toggle._props['options']))
        mode=next(t for t in toggles if '链接' in t._values)
        mode.set_value('复制');self.assertEqual('复制',dialog.config.mode)
        self.assertEqual(11,len(config.BASIC_CONFIG_KEYS))

    def test_all_button_factories_have_icon(self):
        missing=[]
        for path in Path('src').rglob('*.py'):
            tree=ast.parse(path.read_bytes())
            parents={child:parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for n in ast.walk(tree):
                if not isinstance(n,ast.Call):continue
                f=n.func
                is_button=isinstance(f,ast.Attribute) and f.attr=='button' and isinstance(f.value,ast.Name) and f.value.id in ('ui','ui_api')
                is_button=is_button or isinstance(f,ast.Name) and f.id=='RedButton'
                icon=next((k.value for k in n.keywords if k.arg=='icon'),None)
                if isinstance(f,ast.Name) and f.id=='RedButton' and len(n.args)>2:icon=n.args[2]
                valid=icon is not None and (not isinstance(icon,ast.Constant) or isinstance(icon.value,str) and bool(icon.value.strip()))
                if is_button and not valid:
                    chain=n
                    while chain in parents and isinstance(parents[chain],(ast.Call,ast.Attribute)):chain=parents[chain]
                    if not re.search(r'icon=[a-z_]+',ast.unparse(chain)):missing.append(f'{path}:{n.lineno}')
        self.assertFalse(missing,'缺少图标: '+', '.join(missing))
