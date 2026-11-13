"""Read-only asset selection and one shared replacement/Catalog CAS predicate.

Catalog + completed Plan/ledger establish *ownership*, not current content health.
The current registered inode may contain damaged bytes: explicit replacement
freezes those bytes for approval and rollback without claiming they are healthy.
Old download sources need not be online. A different inode is foreign, even when
its name or hash resembles the registered asset. No files or Decisions are saved
here; only the Decision command producer may persist the returned observation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from collections.abc import Mapping

from .asset_operation import AssetOperation, plain
from .asset_collision import build_reuse_resolution
from .asset_replacement import (ReplacementEvidence, RetirementEvidence, observe_file,
    observe_replacement, observe_retirement, safe_absolute)
from .completed_history import ledger_for, read_bytes
from .decision_state import DecisionState, DecisionStateStore
from .plan_store import PlanStore
from .projection_authority import validate_plan_projection_receipt
from .season_identity import provider_key


def _digest(value):
    return hashlib.sha256(AssetOperation.encode(value)).hexdigest()


def _version(entry):
    contract = entry.presentation_asset
    if contract is None:
        return None
    if contract.version_descriptor is not None:
        return contract.version_descriptor.version_ref
    return contract.owner_version_ref


def _entry_version(plan, entry):
    version = _version(entry)
    if version is None and entry.subtitle is not None:
        matches = [item for item in plan.entries
                   if item.source_path == entry.subtitle.associated_video_source_path]
        if len(matches) != 1:
            raise ValueError("字幕缺少唯一已裁决的电影版本归属")
        return _version(matches[0])
    return version


class AssetActionService:
    def __init__(self, *, plan_store, decision_store, task_root, record_root,
                 work_catalog, series_catalog):
        self.plans, self.decisions = plan_store, decision_store
        self.tasks, self.records = Path(task_root), Path(record_root)
        self.work, self.series = work_catalog, series_catalog

    def _scope(self, plan):
        root = safe_absolute(str(plan.metadata.get("canonical_work_root") or ""))
        if plan.work_identity is None or plan.work_identity.status != "confirmed":
            raise ValueError("换源必须先确认作品身份")
        if {group.kind for group in plan.presentation_groups} == {"movie_continuous"}:
            versions = {_version(entry) for entry in plan.entries if entry.presentation_asset is not None
                        and entry.presentation_asset.asset_role.kind == "content_version"}
            if len(versions) != 1 or None in versions:
                raise ValueError("换源必须明确选择一个电影版本，不能替换其它版本")
            version = next(iter(versions))
            key = str(plan.metadata.get("catalog_work_key") or plan.metadata.get("proposed_work_key") or "")
            return "work", key, f"work:{key}:version:{version}", root, version
        if {group.kind for group in plan.presentation_groups} == {"tv_episodes"}:
            key, season = str(plan.metadata.get("series_key") or ""), plan.metadata.get("season_number")
            if not key or type(season) is not int or season < 0:
                raise ValueError("换源必须先确认系列与季度")
            if plan.metadata.get('episode_ranges'):
                ordinary_seasons = {e.season_id for e in plan.entries if e.media_kind == 'video'
                    and e.season_id not in {None, 0} and e.presentation_asset is not None
                    and e.presentation_asset.asset_role.kind == 'content_version'}
                if ordinary_seasons and ordinary_seasons != {season}:
                    raise ValueError('本次换源或补齐须与任务季度一致；请先核对连续集数中的季号，原文件和登记保留')
            return "series", key, f"series:{key}:season:{season}", root, season
        raise ValueError("换源只接受单一季度或单一电影版本")

    def _belongs_to_scope(self, plan, scope):
        """A selected action is singular; its historical owner need not be."""
        backend, key, slot, root, selector = scope
        if backend != 'work':
            return self._scope(plan) == scope
        return (plan.work_identity is not None and plan.work_identity.status == 'confirmed'
                and {g.kind for g in plan.presentation_groups} == {'movie_continuous'}
                and safe_absolute(str(plan.metadata.get('canonical_work_root') or '')) == root
                and str(plan.metadata.get('catalog_work_key') or plan.metadata.get('proposed_work_key') or '') == key
                and slot == f'work:{key}:version:{selector}'
                and any(e.presentation_asset is not None
                        and e.presentation_asset.asset_role.kind == 'content_version'
                        and _version(e) == selector for e in plan.entries))

    def _catalog_slot(self, plan):
        backend, key, slot, root, selector = self._scope(plan)
        owners = []
        if backend == "work":
            record = self.work.load(key)
            if Path(record["canonical_root"]) != root or not self.work.compatible_base(record, plan):
                raise ValueError("换源作品或规范根与登记不一致")
            versions = [dict(row) for row in record["versions"] if row.get("version_ref") == selector]
            snapshot = dict(versions=versions, assets=[])
            owners = [(str(row["task_id"]), str(row["ledger_fingerprint"])) for row in versions]
        else:
            match = self.series.load(key)
            if match.canonical_root != root:
                raise ValueError("换源系列规范根与登记不一致")
            bindings = [plain(row) for row in match.season_bindings if row.get("season_number") == selector]
            current_provider = provider_key(plan.work_identity.primary_provider.to_dict())
            if any(provider_key(row.get("provider_identity")) != current_provider for row in bindings):
                raise ValueError("所选季度登记了不同Provider身份")
            snapshot = dict(bindings=bindings)
            for row in bindings:
                task = str(row["task_id"])
                raw = json.loads(read_bytes(self.records / f"{task}.ledger.json"))
                owners.append((task, str(raw.get("plan_fingerprint") or "")))
        owners = sorted(set(owners))
        if backend=='work' and len({task for task, _fp in owners}) > 1:
            raise ValueError("所选槽位存在多代未裁决登记，不能猜测替换对象")
        scope = (backend, key, slot, root, selector)
        active = self._active_slot_entries(scope, owners)
        if backend=='series' and len(owners)>1:
            from .series_asset_scope import validate_active_disjoint
            validate_active_disjoint(self, active)
        if backend == "work":
            for asset in record["assets"]:
                if asset.get("scope") == "shared":
                    continue
                selected = [dict(row) for row in asset.get("associations", ()) if any(
                    row.get("task_id") == item[0] and row.get("source") == item[2]
                    and asset.get("target") == item[3] for item in active)]
                if selected:
                    snapshot["assets"].append({**dict(asset), "associations": selected})
                elif any(row.get("owner_version_ref") == selector for row in asset.get("associations", ())):
                    raise ValueError("电影仍活跃伴随资产缺少当前槽位冻结历史链")
            registered = {(row["task_id"], row["source"], asset["target"])
                          for asset in snapshot["assets"] for row in asset["associations"]}
            if any((task, source, target) not in registered for task, _fp, source, target in active):
                raise ValueError("原Plan条目未登记到所选电影版本")
        snapshot["active_entries"] = active
        owners = sorted({(item[0], item[1]) for item in active})
        return scope, _digest(snapshot), owners, snapshot

    def _active_slot_entries(self, scope, owners):
        """Follow only the current slot's frozen retained references, never tasks.

        A retained row is carried forward directly with its original Plan/ledger
        identity. Current materialized targets win over old reuse references;
        replaced historical targets cannot be resurrected by this traversal.
        """
        backend, key, slot, root, selector = scope
        active = []
        for task, fingerprint in owners:
            current, _ledger, _sha = self._history(task, fingerprint)
            if not self._belongs_to_scope(current, scope):
                raise ValueError("登记原Plan不属于所选作品季度/版本")
            entries = [entry for entry in current.entries if (
                _entry_version(current, entry) == selector if backend == "work" else
                True)]
            current_targets = {str(entry.target_path) for entry in entries}
            active.extend((task, fingerprint, str(entry.source_path), str(entry.target_path)) for entry in entries)
            raw = current.metadata.get("asset_operation")
            operation = AssetOperation.from_mapping(raw).to_dict() if raw is not None else {}
            retained = [row for row in operation.get("previous_assets", ())
                        if row["disposition"] == "retain" and row["target_path"] not in current_targets]
            if backend == "series" and retained:
                payload = json.loads(read_bytes(self.series.root / f"{key}.json"))
                histories = [row for row in payload.get("asset_history", ())
                             if row.get("task_id") == task and row.get("plan_fingerprint") == fingerprint]
                if len(histories) != 1 or any(row["target_path"] not in histories[0].get("retained_targets", ())
                                              for row in retained):
                    raise ValueError("系列保留资产缺少当前季度登记历史链")
            for row in retained:
                if (row["backend"], row["key"], row["slot_key"], safe_absolute(row["canonical_root"])) != scope[:4]:
                    raise ValueError("保留资产历史引用越出当前槽位")
                old, ledger, sha = self._history(row["task_id"], row["plan_fingerprint"])
                pair = (row["source_path"], row["target_path"])
                if not self._belongs_to_scope(old, scope) or sha != row["ledger_sha256"] or pair not in ledger:
                    raise ValueError("保留资产冻结Plan/ledger历史已变化")
                candidates = [entry for entry in old.entries if (str(entry.source_path), str(entry.target_path)) == pair]
                if len(candidates) != 1 or (backend == "work" and _entry_version(old, candidates[0]) != selector):
                    raise ValueError("保留资产不属于当前版本")
                active.append((row["task_id"], row["plan_fingerprint"], *pair))
        if len({item[3] for item in active}) != len(active):
            raise ValueError("所选资产闭包包含重复目标")
        return sorted(active)

    def _history(self, task_id, fingerprint):
        try:
            plan, ledger, raw = ledger_for(self.plans, self.records, task_id, fingerprint)
            validate_plan_projection_receipt(plan)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            raise ValueError("换源缺少可信冻结完成Plan/ledger") from exc
        policy = plan.execution_policy
        if (policy is None or policy.mode != "hardlink" or not policy.preserve_source or policy.dry_run
                or ledger.get("task_id") != task_id or ledger.get("plan_fingerprint") != fingerprint
                or ledger.get("status") != "completed" or ledger.get("mode") != "hardlink"
                or ledger.get("dry_run") is not False):
            raise ValueError("换源原资产缺少对应真实完成记录")
        entries = ledger.get("entries")
        if not isinstance(entries, list) or len(entries) != len(plan.entries):
            raise ValueError("换源完成记录与原Plan条目不一致")
        index = {(row.get("source"), row.get("target")): row for row in entries if isinstance(row, Mapping)}
        if len(index) != len(entries) or set(index) != {(str(e.source_path), str(e.target_path)) for e in plan.entries}:
            raise ValueError("换源完成记录的精确路径集合不一致")
        return plan, index, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _registered_identity(row):
        reused = row.get("status") == "reused_identical_asset" and row.get("transfer_type") == "reuse_existing"
        created = row.get("status") == "created" and row.get("transfer_type") == "hardlink"
        if not (reused or created):
            raise ValueError("换源原条目不是可信完成媒体")
        device, inode = row.get("target_device"), row.get("target_inode")
        if type(device) is not int or device < 0 or type(inode) is not int or inode <= 0:
            raise ValueError("换源原目标缺少物理身份，不能仅凭同名覆盖")
        if created and (device, inode) != (row.get("source_device"), row.get("source_inode")):
            raise ValueError("原硬链接完成身份矛盾")
        return device, inode

    def _previous(self, plan, *, expected=None, completed=False, with_snapshot=False):
        scope, snapshot_fp, owners, snapshot = self._catalog_slot(plan)
        backend, key, slot, root, selector = scope
        rows = []
        new_targets = {str(entry.target_path): entry for entry in plan.entries}
        frozen = {row["target_path"]: row for row in expected or ()}
        for task_id, fingerprint in owners:
            old, ledger_rows, ledger_sha = self._history(task_id, fingerprint)
            if not self._belongs_to_scope(old, scope):
                raise ValueError("登记原Plan不属于所选作品季度/版本")
            for entry in old.entries:
                if (task_id, fingerprint, str(entry.source_path), str(entry.target_path)) not in snapshot["active_entries"]:
                    continue
                if backend == "work" and _entry_version(old, entry) != selector:
                    continue
                target = safe_absolute(entry.target_path)
                target.relative_to(root)
                ledger_row = ledger_rows[(str(entry.source_path), str(target))]
                device, inode = self._registered_identity(ledger_row)
                prior = frozen.get(str(target))
                if prior is not None and prior.get("target_evidence") is not None:
                    evidence = prior["target_evidence"]
                    if (evidence.get("device"), evidence.get("inode")) != (device, inode):
                        raise ValueError("冻结旧目标不属于登记完成实体")
                current = observe_file(target) if target.exists() else None
                if current is not None:
                    if completed and prior and prior["target_evidence"] is None and str(target) in new_targets:
                        new_source = observe_file(new_targets[str(target)].source_path)
                        if current != new_source:
                            raise ValueError("原缺失目标尚未物化为本次来源的硬链接")
                        current_dict = None
                    elif completed and prior and prior["disposition"] == "replace":
                        # Completed mode still binds the old closure, but actual
                        # new targets are checked against ReplacementEvidence.
                        current_dict = prior["target_evidence"]
                    else:
                        if (current.device, current.inode) != (device, inode):
                            raise ValueError("所选目标已变成不同实体，属于未知外来文件")
                        current_dict = asdict(current)
                else:
                    current_dict = prior['target_evidence'] if completed and prior and prior['disposition'] == 'retire' else None
                disposition = "replace" if str(target) in new_targets and current is not None else "retain"
                if prior is not None:
                    disposition = prior["disposition"]
                rows.append(dict(backend=backend, key=key, slot_key=slot, canonical_root=str(root),
                                 slot_fingerprint=snapshot_fp, task_id=task_id, plan_fingerprint=fingerprint,
                                 ledger_sha256=ledger_sha, source_path=str(entry.source_path), target_path=str(target),
                                 target_evidence=current_dict, disposition=disposition))
        if len({row["target_path"] for row in rows}) != len(rows):
            raise ValueError("所选资产闭包包含重复目标")
        result = scope, sorted(rows, key=lambda row: row["target_path"])
        return (*result, snapshot) if with_snapshot else result

    @staticmethod
    def _clear_selected_collisions(plan, pairs):
        names = {Path(source).name for source, _target in pairs}
        source_counts = {name: sum(entry.source_path.name == name for entry in plan.entries) for name in names}
        issues = []
        for issue in plan.metadata.get("asset_collision_unresolved", ()):
            if isinstance(issue, str) and issue in names and source_counts[issue] == 1:
                continue
            if (isinstance(issue, Mapping) and issue.get("code") in {"target_exists", "target_conflict", "asset_collision"}
                    and (issue.get("source_path"), issue.get("target_path")) in pairs):
                continue
            issues.append(issue)
        return tuple(issues)

    def prepare_missing(self, plan):
        """冻结本任务缺失目标与旧登记的对应关系。"""
        validate_plan_projection_receipt(plan)
        state = self.decisions.load(plan.task_id)
        self.validate_operation_authority(plan, state)
        if plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(state):
            raise ValueError('补齐前Decision与当前Plan不一致')
        from .series_repair import prepare
        return prepare(self, plan)

    def prepare_replacement(self, plan):
        """Freeze the selected season/version's whole owned asset closure."""
        validate_plan_projection_receipt(plan)
        state = self.decisions.load(plan.task_id)
        if plan.metadata.get("decision_state_fingerprint") != DecisionStateStore.fingerprint(state):
            raise ValueError("换源前Decision与当前Plan不一致")
        scope = self._scope(plan)
        if scope[0]=='series':
            from .season_replacement import prepare
            return prepare(self,plan,scope)
        scope, previous, snapshot_rows = self._previous(plan, with_snapshot=True)
        backend, _key, slot, root, selector = scope
        selected = []
        revised_entries = []
        previous_by_target = {row["target_path"]: row for row in previous}
        for entry in plan.entries:
            safe_absolute(entry.source_path)
            safe_absolute(entry.target_path)
            revised_entries.append(entry)
            if not entry.target_path.exists():
                revised_entries[-1] = replace(entry, collision_resolution=None)
                continue
            if entry.collision_resolution is not None:
                continue
            if backend == "work" and _entry_version(plan, entry) != selector:
                raise ValueError("换源不能覆盖其它版本或共享资产")
            row = previous_by_target.get(str(entry.target_path))
            if row is None:
                raise ValueError("同名目标没有所选槽位的完成登记，不能授权覆盖")
            reusable = build_reuse_resolution(entry.source_path, entry.target_path)
            if reusable is not None:
                revised_entries[-1] = replace(entry, collision_resolution=reusable)
                continue
            selected.append(observe_replacement(entry.source_path, entry.target_path, root))
        selected_targets = {item.target for item in selected}
        new_targets = {str(entry.target_path) for entry in revised_entries}
        retirements = []
        for row in previous:
            if row['target_path'] in selected_targets:
                row['disposition'] = 'replace'
            elif row['target_path'] in new_targets:
                row['disposition'] = 'reuse' if row['target_evidence'] is not None else 'missing'
            elif row['target_evidence'] is None:
                row['disposition'] = 'missing'
            else:
                row['disposition'] = 'retire'
                retirements.append(observe_retirement(Path(row['target_path']), root))
        mode = "replace" if previous else "append"
        value = dict(policy="asset-operation-v2" if previous else "asset-operation-v1", mode=mode, slot_key=slot,
                     replaces=[item.to_dict() for item in selected], previous_assets=previous)
        if previous:
            from .target_routing import TargetRootSnapshot
            from ..utils.utils import safe_task_path
            task = json.loads(read_bytes(safe_task_path(self.tasks, plan.task_id)))
            raw = plan.metadata.get('target_root_snapshot') or task.get('draft_root_snapshot')
            snapshot = TargetRootSnapshot.from_dict(raw or {})
            expected = plan.metadata.get('target_root_config_fingerprint') or task.get('draft_root_config_fingerprint')
            if expected != snapshot.fingerprint:
                raise ValueError('换源缺少当前冻结库根，请重新生成计划')
            from .replacement_completion import DISCARD_REPLACED_LINKS
            value.update(completion_policy=DISCARD_REPLACED_LINKS, retirements=[row.to_dict() for row in retirements],
                         library_roots=sorted(set(snapshot.to_dict().values())),
                         catalog_before=dict(backend=backend, key=scope[1], slot_key=slot,
                            root=str(root), rows={k: v for k, v in snapshot_rows.items() if k != 'active_entries'}))
        existing_operation = state.asset_operation.to_dict() if state.asset_operation is not None else {}
        if existing_operation.get("movie_scope") is not None:
            value["movie_scope"] = existing_operation["movie_scope"]
        operation = AssetOperation.from_mapping(value)
        retired = [row['target_path'] for row in previous if row['disposition'] in {'replace', 'retire'}]
        resolved_pairs = {(item.source, item.target) for item in selected} | {
            (str(entry.source_path), str(entry.target_path)) for entry in revised_entries
            if entry.collision_resolution is not None}
        draft = replace(plan, entries=tuple(revised_entries), projection_receipt=None, metadata={**plan.metadata,
            "asset_collision_unresolved": self._clear_selected_collisions(plan, resolved_pairs),
            "asset_replacement_retained": (), 'asset_replacement_retired': tuple(retired)})
        return operation, draft

    def validate_operation_authority(self, plan, state):
        """Validate immutable approval inputs without requiring pre-execution targets."""
        from .movie_asset_scope import validate_movie_authority
        validate_movie_authority(plan)
        if not isinstance(state, DecisionState) or state.task_id != plan.task_id:
            raise ValueError("资产操作缺少当前类型化Decision")
        operation = state.asset_operation
        raw = plan.metadata.get("asset_operation")
        if operation is None:
            if raw is not None:
                raise ValueError("裸Plan资产操作没有Decision权威")
            return None
        if raw is None or AssetOperation.from_mapping(raw) != operation:
            raise ValueError("资产操作与Decision不一致")
        validate_plan_projection_receipt(plan)
        if plan.metadata.get("decision_state_fingerprint") != DecisionStateStore.fingerprint(state):
            raise ValueError("资产操作Decision版本不一致")
        if DecisionStateStore.fingerprint(self.decisions.load(plan.task_id)) != DecisionStateStore.fingerprint(state):
            raise ValueError("资产操作已不是当前Decision")
        return operation

    def validate(self, plan, state, *, completed=False):
        """Same typed predicate at approval, execution and locked registration."""
        operation = self.validate_operation_authority(plan, state)
        if operation is None:
            return None
        value = operation.to_dict()
        from .series_repair import is_repair, validate
        if is_repair(value):
            validate(self, plan, value, completed=completed)
            return operation
        if value["mode"] != "replace":
            # Approval and forward execution recheck the same current Catalog
            # dependencies. Immutable authority-only validation (also used by
            # inverse recovery) deliberately does not perform this live check.
            from .movie_asset_scope import validate_append
            validate_append(plan, self.work)
            return operation
        if value['policy']=='asset-operation-v3':
            from .plan_execution import PlanExecutionService
            from .season_replacement import validate
            PlanExecutionService._validate_frozen_source_identities(plan)
            validate(self,plan,value,completed=completed)
            return operation
        if value['policy'] != 'asset-operation-v2':
            raise ValueError('旧换源计划不具备完整退场合同，请重新生成并批准整理计划')
        from .plan_execution import PlanExecutionService
        PlanExecutionService._validate_frozen_source_identities(plan)
        scope, previous, snapshot_rows = self._previous(plan, expected=value["previous_assets"], completed=completed, with_snapshot=True)
        if scope[2] != value["slot_key"] or previous != value["previous_assets"]:
            raise ValueError("换源原资产闭包或槽位代际已变化，请重新生成Plan")
        before = dict(backend=scope[0], key=scope[1], slot_key=scope[2], root=str(scope[3]),
                      rows={k: v for k, v in snapshot_rows.items() if k != 'active_entries'})
        if before != value['catalog_before']:
            raise ValueError('换源登记逆向证据与批准槽位不一致')
        entries = {(str(entry.source_path), str(entry.target_path)) for entry in plan.entries}
        selected = [ReplacementEvidence.from_dict(row) for row in value["replaces"]]
        if {item.target for item in selected} != {row["target_path"] for row in previous if row["disposition"] == "replace"}:
            raise ValueError("换源批准目标与原资产闭包不一致")
        for item in selected:
            if (item.source, item.target) not in entries or item.target_root != str(scope[3]):
                raise ValueError("换源目标不属于当前Plan精确范围")
            previous_row = next(row for row in previous if row["target_path"] == item.target)
            if asdict(item.old_target) != previous_row["target_evidence"]:
                raise ValueError("换源旧目标观察与冻结登记闭包不一致")
            item.validate_current(target_may_be_new=completed)
            if completed and observe_file(item.target) != item.new_source:
                raise ValueError("换源登记前新目标尚未完成")
        retirements = [RetirementEvidence.from_dict(row) for row in value['retirements']]
        if {item.target for item in retirements} != {row['target_path'] for row in previous if row['disposition'] == 'retire'}:
            raise ValueError('退场资产与批准闭包不一致')
        for item in retirements:
            prior = next(row for row in previous if row['target_path'] == item.target)
            if item.target_root != str(scope[3]) or asdict(item.old_target) != prior['target_evidence']:
                raise ValueError('退场资产物理证据与登记闭包不一致')
            item.validate_current(retired=completed)
            if completed and Path(item.target).exists():
                raise ValueError('旧资产尚未完成退场')
        return operation
