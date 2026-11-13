"""将动态界面图标名映射到随应用分发的描边字体。"""

def outlined_icon(name):
    name = str(name or 'close')
    return name if name.startswith('o_') else 'o_' + name


from nicegui import ui


class IconToggle(ui.toggle):
    """只为原生选项补描边图标，值与事件仍使用NiceGUI的映射。"""
    def _update_options(self):
        super()._update_options()
        icons = {'链接': 'link', '复制': 'content_copy', '剪切': 'content_cut',
                 '启用': 'check_circle', '禁用': 'block', 'DEBUG': 'bug_report',
                 'INFO': 'info', 'WARNING': 'warning_amber', 'ERROR': 'error_outline',
                 'all': 'folder', 'issues': 'error_outline'}
        for value, option in zip(self._values, self._props['options']):
            if value in icons:
                option['icon'] = outlined_icon(icons[value])
