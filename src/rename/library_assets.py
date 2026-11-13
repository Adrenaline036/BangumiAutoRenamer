"""Work-facing asset inventory; task IDs remain an internal audit reference."""
from dataclasses import dataclass
import json
from pathlib import Path

from .completed_rebuild import CompletedRebuildService
from .path_safety import first_reparse_component
from .replacement_completion import has_completed_media


@dataclass(frozen=True)
class LibraryAsset:
    task_id: str
    title: str
    season: int | None
    source: str
    target: str
    state: str
    detail: str
    version: str = ''


def version_label(plan):
    """Display only the already frozen version authority, not path guesses."""
    versions = {}
    for entry in plan.entries:
        asset = entry.presentation_asset
        if asset is not None and asset.asset_role.kind == 'content_version' and asset.version_descriptor is not None:
            descriptor = asset.version_descriptor
            if descriptor.version_ref:
                versions[descriptor.version_ref] = descriptor.display_label or descriptor.version_ref
    return ' / '.join(versions[key] for key in sorted(versions))


class LibraryAssets:
    def __init__(self, rebuild: CompletedRebuildService, *, review_service=None):
        self.rebuild = rebuild
        self.review_service = review_service

    def list_assets(self):
        """Read-only projection: missing never becomes logical retirement."""
        rows = []
        for path in sorted(self.rebuild.tasks.glob('*.json')):
            try:
                task = json.loads(path.read_text('utf-8'))
                plan = self.rebuild.plans.load(path.stem)
                if not has_completed_media(task) and not plan.metadata.get('completed_rebuild_observation'):
                    continue
                from .completed_source_reentry import membership
                if membership(self.rebuild, plan).state != "current":
                    continue
                missing = sum(not entry.target_path.is_file() for entry in plan.entries)
                state = "missing" if missing else "present"
                detail = (f"发现 {missing} 个目标缺失；点击重建后完整核查" if missing
                          else "目标存在；尚未核查内容，点击重建后完整核查")
                rows.append(LibraryAsset(path.stem, plan.work_identity.title if plan.work_identity else '未确认作品',
                    plan.metadata.get('season_number'), str(task.get('path') or ''),
                    str(plan.metadata.get('canonical_work_root') or ''), state, detail, version_label(plan)))
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                # An unreadable history card cannot veto another work's action.
                continue
        return tuple(sorted(rows, key=lambda r: (r.title, r.season if r.season is not None else -1, r.version, r.source)))

    def completed_source(self, request):
        return self.completed_sources((request,)).get(request.task_id)

    def completed_sources(self, requests):
        """同一批次共用历史观察；命中后仍核对当前计划和冻结来源。"""
        from .task_batch_scope import TaskSourceScope, source_identity_set_fingerprint
        from .plan_execution import PlanExecutionService
        wanted = {}
        for request in requests:
            key = (tuple(request.input_scope), source_identity_set_fingerprint(request.source_identities))
            wanted.setdefault(key, []).append(request)
        matches = {r.task_id: [] for r in requests}
        for path in self.rebuild.tasks.glob('*.json'):
            try:
                task = json.loads(path.read_text('utf-8'))
                if not has_completed_media(task):
                    continue
                scope = TaskSourceScope.from_mapping(task.get('task_source_scope'))
                selected = wanted.get(((str(Path(str(task.get('path') or '')).resolve()),),
                                      scope.source_identity_set_fingerprint))
                if not selected:
                    continue
                _task, plan, _state = self.rebuild._current(path.stem)
                PlanExecutionService._validate_frozen_source_identities(plan)
                if any(first_reparse_component(Path(p)) is not None for p in selected[0].input_scope):
                    raise ValueError('来源路径包含重解析节点')
                for request in selected:
                    matches[request.task_id].append(path.stem)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                continue
        if any(len(items) > 1 for items in matches.values()):
            raise ValueError('同一来源对应多个完成资产，请核查来源占用')
        return {key: items[0] for key, items in matches.items() if items}

    def repair(self, task_id):
        from .completed_source_reentry import membership, generate_reentry
        plan = self.rebuild.plans.load(task_id)
        member = membership(self.rebuild, plan)
        if member.state == "superseded":
            return generate_reentry(self.rebuild, self.review_service, task_id)
        if member.state != "current":
            raise ValueError("原资产没有当前登记或明确接替历史，请核查作品登记")
        view = self.rebuild.inspect(task_id)
        if view.state == 'missing':
            return self.rebuild.generate(task_id, expected_fingerprint=view.fingerprint)
        if view.state == 'complete':
            return self.rebuild.plans.load(task_id)
        raise ValueError(view.detail)

    def inspect_admission(self, task_id):
        """Read-only routing before any action in a mixed admission batch."""
        from .completed_rebuild import RebuildSnapshot
        from .completed_source_reentry import membership, inspect_reentry
        try:
            plan = self.rebuild.plans.load(task_id)
            member = membership(self.rebuild, plan)
            if member.state == "superseded":
                inspect_reentry(self.rebuild, self.review_service, task_id)
                return RebuildSnapshot("choice", "已被接替的来源可生成新的待选择计划，不执行媒体操作")
            if member.state == "current":
                return self.rebuild.inspect(task_id)
            raise ValueError("原资产没有当前登记或明确接替历史，请核查作品登记")
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            return RebuildSnapshot("blocked", str(exc))
