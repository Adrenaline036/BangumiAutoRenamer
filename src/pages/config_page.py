
from src.component.dialog_frame import dialog_card
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Sequence
from types import SimpleNamespace

from nicegui import ui

from ..logger import logger, update_log_level_from_config
from ..config.config_manager import CN_MAP, cm
from ..element.red import RedButton, RedToogle
from ..component.local_file_picker import local_file_picker
from ..rename.path_safety import first_reparse_component


BASIC_CONFIG_KEYS = [
    "api_key",
    "anime_path",
    "anime_movie_path",
    "movie_path",
    "bangumi_path",
    "bangumi_enabled",
    "bangumi_token",
    "bangumi_user_agent",
    "mode",
    "docker_mnt",
    "log_level",
]


class ConfigPage(ui.dialog):

    def __init__(self) -> None:
        super().__init__()
        self.config = SimpleNamespace(**{key: cm.get_config(key) for key in BASIC_CONFIG_KEYS})

        _s = "width: min(94vw, 980px); max-width: none; flex-wrap: nowrap; max-height: 88vh;"
        with self, dialog_card().style(_s).classes("flex bar-dialog bar-config-dialog"):
            ui.label("配置").style("font-size: 20px; font-weight: bold")
            ui.separator()

            # 基础配置
            ui.label("基础配置").style(
                "font-size: 16px; font-weight: bold; margin-top: 10px;"
            )
            for cn in BASIC_CONFIG_KEYS:
                self._create_config_row(cn)

            with ui.row(wrap=False).classes(
                "bar-dialog-actions bar-config-actions w-full justify-end"
            ).classes('bar-modal-footer'):
                RedButton("取消", on_click=self.close, icon='o_close').props("outline")
                RedButton(
                    "保存",
                    on_click=self._handle_ok,
                icon='o_save')

    def _create_config_row(self, cn: str):
        icons = {'api_key': 'vpn_key', 'bangumi_enabled': 'language',
                 'bangumi_token': 'vpn_key', 'bangumi_user_agent': 'badge',
                 'mode': 'album', 'docker_mnt': 'folder', 'log_level': 'description'}
        with ui.element('div').classes('bar-config-row w-full'):
            with ui.row(wrap=False).classes('bar-config-label items-center'):
                ui.icon('o_' + icons.get(cn, 'movie')).props('size=20px')
                label = ui.label(CN_MAP.get(cn, cn))
                if cn == 'bangumi_user_agent':
                    label.tooltip('Bangumi User-Agent')
                elif cn == 'bangumi_token':
                    label.tooltip('Bangumi Access Token')
            if cn in {'mode', 'bangumi_enabled', 'log_level'}:
                options = (['链接', '复制', '剪切'] if cn == 'mode' else
                           ['启用', '禁用'] if cn == 'bangumi_enabled' else
                           ['DEBUG', 'INFO', 'WARNING', 'ERROR'])
                value = cm.get_config(cn)
                if cn == 'bangumi_enabled':
                    value = '启用' if value else '禁用'
                RedToogle(options, value=value,
                    on_change=lambda e, c=cn: self._change(c,
                        e.value == '启用' if c == 'bangumi_enabled' else e.value)
                ).classes('bar-config-toggle w-full')
            else:
                input_widget = ui.input(value=cm.get_config(cn),
                    on_change=lambda e, c=cn: self._change(c, e.value)
                ).props('filled dense').classes('bar-config-input w-full').bind_value(self.config, cn)
                if 'api_key' in cn or cn.endswith('_token'):
                    input_widget.props('type=password')
                if cn == 'bangumi_token':
                    input_widget.props('placeholder=可选')
                if cn.endswith('path'):
                    with input_widget.add_slot('append'):
                        ui.button(icon='o_folder_open',
                            on_click=lambda e, c=cn: self.pick(key=c)
                        ).props(f'flat round color=red-7 aria-label="选择{CN_MAP.get(cn, cn)}"'
                        ).tooltip(f'选择{CN_MAP.get(cn, cn)}')

    async def pick(self, *, key: str) -> None:
        result = await local_file_picker('~', multiple=True)
        if not isinstance(result, Sequence) or isinstance(result, (str, bytes, bytearray)) or not result:
            return
        result = result[0]
        if not isinstance(result, str) or not result.strip():
            return
        logger.info(f'[配置] {key} 选择了 {result}')
        self._change(key, result)

    def _change(self, key: str, value: str) -> None:
        setattr(self.config, key, value)

    def _handle_ok(self):
        pending = {key: getattr(self.config, key) for key in BASIC_CONFIG_KEYS}
        for key, value in pending.items():
            if not key.endswith("_path") or value in (None, ""):
                continue
            raw = str(value).strip()
            if not (
                PureWindowsPath(raw).is_absolute()
                or PurePosixPath(raw).is_absolute()
            ):
                ui.notify(
                    f"{CN_MAP.get(key, key)} 路径必须是绝对路径",
                    type="negative",
                )
                return
            local_path = Path(raw)
            if local_path.is_absolute():
                existing = local_path
                while not existing.exists() and existing != existing.parent:
                    existing = existing.parent
                if not existing.exists():
                    ui.notify(
                        f"{CN_MAP.get(key, key)} 路径没有可信的现有父目录",
                        type="negative",
                    )
                    return
                reparse = first_reparse_component(local_path)
                if reparse is not None:
                    ui.notify(
                        f"{CN_MAP.get(key, key)} 路径包含重解析点：{reparse}",
                        type="negative",
                    )
                    return


        # 整页配置只提交一次事务；任一字段无效时不会留下部分保存。
        if not cm.set_configs(pending):
            ui.notify("配置保存失败：包含未知字段", type="negative")
            return
        config_show = {key: cm.get_config(key) for key in BASIC_CONFIG_KEYS}
        for key in config_show.keys():
            if "api_key" in key or key.endswith("_token"):
                config_show[key] = len(str(config_show[key])) * "*"

        logger.info('[配置] 配置已修改为： {}'.format(config_show))
        ui.notify("配置保存成功", type="positive")
        # 更新运行时日志级别
        try:
            update_log_level_from_config()
            logger.info(f"[配置] 运行时日志级别已更新为 {cm.get_config('log_level')}")
        except Exception as e:
            logger.error(f"[配置] 更新运行时日志级别失败: {e}")
        self.close()

async def config_page() -> None:
    await ConfigPage()
