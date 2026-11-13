"""Display admitted task filenames without a Plan, Provider, or filesystem scan."""
from pathlib import Path
from types import SimpleNamespace

from nicegui import ui

from ..rename.task_batch_scope import TaskSourceScope, TaskBatchScopeError
from ..rename.source_basis import root_for_task
from .plan_file_list import PlanFileListMixin


def source_file_rows(task):
    scope = TaskSourceScope.from_mapping(task.get('task_source_scope'))
    root = Path(root_for_task(task, scope))
    if scope.task_id != task.get('uuid') or not root.is_absolute() or '..' in root.parts:
        raise ValueError('本任务来源范围无效')
    rows = []
    for identity in scope.source_identities:
        path = Path(identity.source_path)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('本任务来源路径无效')
        relative = path.relative_to(root)
        rows.append(dict(source=identity.source_path, name=path.name,
                         file=path.name if path == root else str(relative)))
    if not rows or len({row['source'] for row in rows}) != len(rows):
        raise ValueError('本任务来源清单为空或重复')
    return sorted(rows, key=lambda row: row['file'].casefold())


class SourceFileList(PlanFileListMixin):
    """UI-only projection of admitted paths, not a placeholder RenamePlan."""

    def __init__(self, rows):
        self.plan = self.decision = None
        self.snapshot = SimpleNamespace(rows=[
            {**row, 'id': index + 1, 'entry_id': row['source']}
            for index, row in enumerate(rows)
        ])
        self.query = self.language_filter = self.flag_filter = ''
        self.review_only = self.subtitle_only = self.unresolved_only = False
        self.conflict_only = self.unknown_only = False
        self.view_mode = 'all'
        self.page = 1
        self.selected_ids = set()
        with ui.column().classes('w-full') as self.container:
            self._render_toolbar()
            self.entries_container = ui.column().classes('bar-review-entries w-full gap-2')
            self._render_entries()


def render_task_source_files(task):
    try:
        rows = source_file_rows(task)
    except (TaskBatchScopeError, ValueError, TypeError, KeyError):
        ui.label('无法展示本任务文件清单：已登记的来源范围缺失或无效。请核查任务来源，必要时结束任务后重新添加。').classes('bar-help text-warning')
        return None
    return SourceFileList(rows)
