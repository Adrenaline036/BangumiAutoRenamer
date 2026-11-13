"""Authoritative per-task decisions, separate from Plans and execution facts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Optional

from .plan_store import PlanStore
from .content_policy import ContentPolicy, parse as parse_content_policy, require_typed
from .rename_plan import ProviderIdentity, RenamePlan, WorkIdentity
from .file_lock import FileLockBusyError
from .catalog_binding import binding_from_decision
from .tv_planning_authority import TVPlanningAuthority
from .rebuild_observation import CompletedRebuildObservation
from .asset_operation import AssetOperation
from .movie_version_selection import MovieVersionSelection
from .content_selection import ContentSelection
from .source_basis import InitialSourceBasis
from .pending_tv_selection import PendingTVSelection

if TYPE_CHECKING:
    from .successor_authority import CohortAuthorityDraft


DECISION_STATE_SCHEMA_VERSION = 5
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_LIBRARY_DOMAINS = {None, "animation", "general"}
_LAYOUT_MODES = {None, "tv_episodes", "movie_versions", "movie_segments"}
_RECOVERY_STATES = {
    None, "healthy", "catalog_missing", "materialization_missing",
    "catalog_record_missing", "catalog_root_missing", "catalog_corrupt",
    "catalog_detach_pending", "evidence_conflict", "source_unavailable",
}


class DecisionStateError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


from .episode_range import EpisodeRangeDecision
from .episode_versions import EpisodeVersionDecision


@dataclass(frozen=True)
class DecisionState:
    task_id: str
    revision: int = 0
    confirmed_work: Mapping[str, object] = field(default_factory=dict)
    provider_evidence: tuple[Mapping[str, object], ...] = ()
    linked_provider_identities: tuple[Mapping[str, object], ...] = ()
    series_binding: Optional[Mapping[str, object]] = None
    season_binding: Optional[Mapping[str, object]] = None
    library_domain: Optional[str] = None
    layout_mode: Optional[str] = None
    manual_role_decisions: Mapping[str, str] = field(default_factory=dict)
    provider_special_decisions: Mapping[str, Mapping[str, object]] = field(
        default_factory=dict
    )
    catalog_link_intent: Optional[Mapping[str, object]] = None
    recovery_state: Optional[str] = None
    execution_failure: Optional[Mapping[str, object]] = None
    provider_link_binding: Optional[Mapping[str, object]] = None
    source_episode_projection: Optional[Mapping[str, object]] = None
    source_episode_projection_decision: Optional[Mapping[str, object]] = None
    provider_subject_boundary_decisions: Mapping[
        str, Mapping[str, object]
    ] = field(default_factory=dict)
    tv_planning_authority: Optional[TVPlanningAuthority] = None
    completed_rebuild_observation: Optional[CompletedRebuildObservation] = None
    asset_operation: Optional[AssetOperation] = None
    movie_version_selection: Optional[MovieVersionSelection] = None
    content_selection: Optional[ContentSelection] = None
    content_policy: ContentPolicy | None = None
    initial_source_basis: InitialSourceBasis | None = None
    pending_tv_selection: PendingTVSelection | None = None
    updated_at: str = ""
    schema_version: int = DECISION_STATE_SCHEMA_VERSION
    episode_ranges: tuple[EpisodeRangeDecision, ...] = ()
    episode_versions: tuple[EpisodeVersionDecision, ...] = ()

    def __post_init__(self) -> None:
        if (type(self.episode_versions) is not tuple or any(type(d) is not EpisodeVersionDecision or d.task_id != self.task_id for d in self.episode_versions)
                or len({d.source_path for d in self.episode_versions}) != len(self.episode_versions)):
            raise DecisionStateError('剧集版本必须使用本任务不可变且来源唯一的决定')
        if type(self.episode_ranges) is not tuple or any(type(d) is not EpisodeRangeDecision for d in self.episode_ranges):
            raise DecisionStateError('连续集数必须使用不可变类型化决定')
        if len({d.source_path for d in self.episode_ranges}) != len(self.episode_ranges):
            raise DecisionStateError('连续集数来源重复')
        if self.pending_tv_selection is not None and type(self.pending_tv_selection) is not PendingTVSelection:
            raise DecisionStateError('季度待确认必须使用类型化依据')
        require_typed(self.content_policy)
        if self.initial_source_basis is not None and type(self.initial_source_basis) is not InitialSourceBasis:
            raise DecisionStateError('来源 basis 必须使用类型化观察')
        if self.content_selection is not None and not isinstance(self.content_selection, ContentSelection):
            raise DecisionStateError('内容选择必须使用类型化冻结输入')
        if self.movie_version_selection is not None and not isinstance(self.movie_version_selection, MovieVersionSelection):
            raise DecisionStateError('电影版本选择必须使用类型化冻结输入')
        if self.asset_operation is not None and not isinstance(self.asset_operation, AssetOperation):
            raise DecisionStateError('资产操作必须使用类型化冻结输入')
        if self.completed_rebuild_observation is not None and not isinstance(self.completed_rebuild_observation, CompletedRebuildObservation):
            raise DecisionStateError('重建必须使用类型化历史观察')
        if not self.task_id or _SAFE_ID.fullmatch(self.task_id) is None:
            raise DecisionStateError("DecisionState task_id 不安全")
        if self.schema_version != DECISION_STATE_SCHEMA_VERSION:
            raise DecisionStateError("DecisionState schema_version 不受支持")
        if self.revision < 0:
            raise DecisionStateError("DecisionState revision 不能为负数")
        if not isinstance(self.linked_provider_identities, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in self.linked_provider_identities
        ):
            raise DecisionStateError("DecisionState 关联资料身份格式无效")
        linked_keys = []
        for item in self.linked_provider_identities:
            key = tuple(
                str(item.get(field) or "").strip()
                for field in ("namespace", "entity_type", "provider_id")
            )
            if not all(key) or str(item.get("status") or "") != "confirmed":
                raise DecisionStateError("DecisionState 关联资料身份不完整")
            linked_keys.append(key)
        if len(linked_keys) != len(set(linked_keys)):
            raise DecisionStateError("DecisionState 关联资料身份重复")
        primary = self.confirmed_work.get("primary_provider")
        primary_key = (
            tuple(
                str(primary.get(field) or "").strip()
                for field in ("namespace", "entity_type", "provider_id")
            )
            if isinstance(primary, Mapping) else None
        )
        if primary_key and all(primary_key) and primary_key in linked_keys:
            raise DecisionStateError("DecisionState 关联资料身份与主身份重复")
        if self.library_domain not in _LIBRARY_DOMAINS:
            raise DecisionStateError("DecisionState 保存位置无效")
        if self.layout_mode not in _LAYOUT_MODES:
            raise DecisionStateError("DecisionState 整理方式无效")
        if self.recovery_state not in _RECOVERY_STATES:
            raise DecisionStateError("DecisionState 恢复状态无效")
        if self.execution_failure is not None:
            if not isinstance(self.execution_failure, Mapping):
                raise DecisionStateError("DecisionState 执行失败记录格式无效")
            code = str(self.execution_failure.get("code") or "").strip()
            backend = str(
                self.execution_failure.get("catalog_backend") or ""
            ).strip()
            key = str(self.execution_failure.get("catalog_key") or "").strip()
            if not code or backend not in {"work", "series"} or not key:
                raise DecisionStateError("DecisionState 执行失败记录不完整")
        if self.provider_link_binding is not None:
            if not isinstance(self.provider_link_binding, Mapping):
                raise DecisionStateError("DecisionState 跨站系列映射格式无效")
            required = ("mapping_id", "revision", "evidence_fingerprint", "status")
            if any(
                self.provider_link_binding.get(name) is None
                or self.provider_link_binding.get(name) == ""
                for name in required
            ):
                raise DecisionStateError("DecisionState 跨站系列映射不完整")
        for name, value in (
            ("source_episode_projection", self.source_episode_projection),
            (
                "source_episode_projection_decision",
                self.source_episode_projection_decision,
            ),
        ):
            if value is not None and not isinstance(value, Mapping):
                raise DecisionStateError(f"DecisionState {name} 格式无效")
        if not isinstance(self.provider_subject_boundary_decisions, Mapping) or not all(
            isinstance(item, Mapping)
            for item in self.provider_subject_boundary_decisions.values()
        ):
            raise DecisionStateError("DecisionState 相邻作品决定格式无效")
        if (
            self.tv_planning_authority is not None
            and not isinstance(self.tv_planning_authority, TVPlanningAuthority)
        ):
            raise DecisionStateError("DecisionState TV 规划权威格式无效")
        if (
            self.tv_planning_authority is not None
            and self.tv_planning_authority.schema_version
            != TVPlanningAuthority.CURRENT_SCHEMA_VERSION
        ):
            raise DecisionStateError("当前 DecisionState 不能引用旧版 TV 规划权威")
        if not isinstance(self.provider_special_decisions, Mapping):
            raise DecisionStateError("DecisionState 特别篇决定格式无效")
        for identifier, decision in self.provider_special_decisions.items():
            if not str(identifier) or not isinstance(decision, Mapping):
                raise DecisionStateError("DecisionState 特别篇决定格式无效")
            action = str(decision.get("action") or "")
            if action in {'confirmed_special_correspondence', 'descriptive_special', 'bangumi_special'}:
                from .special_selection import validate_decision_shape
                try:
                    validate_decision_shape(decision)
                except (ValueError, TypeError, KeyError) as exc:
                    raise DecisionStateError(str(exc)) from exc
                continue
            if action not in {"confirmed_official_special", "unmatched_extra"}:
                raise DecisionStateError("DecisionState 特别篇决定动作无效")
            if action == "confirmed_official_special" and any(
                decision.get(name) in (None, "")
                for name in (
                    "provider_namespace", "provider_entity_type", "provider_id",
                    "season_number", "episode_number", "provider_episode_id",
                )
            ):
                raise DecisionStateError("DecisionState 官方特别篇决定不完整")

    def to_dict(self) -> dict:
        return {
            **({'episode_versions': [d.to_dict() for d in self.episode_versions]} if self.episode_versions else {}),
            **({'episode_ranges': [d.to_dict() for d in self.episode_ranges]} if self.episode_ranges else {}),
            **({'content_policy': self.content_policy.value} if self.content_policy is not None else {}),
            **({'initial_source_basis': self.initial_source_basis.to_dict()} if self.initial_source_basis is not None else {}),
            **({'pending_tv_selection': self.pending_tv_selection.to_dict()} if self.pending_tv_selection is not None else {}),
            **({'asset_operation': self.asset_operation.to_dict()} if self.asset_operation is not None else {}),
            **({'movie_version_selection': self.movie_version_selection.to_dict()} if self.movie_version_selection is not None else {}),
            **({'content_selection': self.content_selection.to_dict()} if self.content_selection is not None else {}),
            **({'completed_rebuild_observation': self.completed_rebuild_observation.to_dict()}
               if self.completed_rebuild_observation is not None else {}),
            "schema_version": self.schema_version,
            "revision": self.revision,
            "task_id": self.task_id,
            "confirmed_work": dict(self.confirmed_work),
            "provider_evidence": [dict(item) for item in self.provider_evidence],
            "linked_provider_identities": [
                dict(item) for item in self.linked_provider_identities
            ],
            "series_binding": dict(self.series_binding) if self.series_binding else None,
            "season_binding": dict(self.season_binding) if self.season_binding else None,
            "library_domain": self.library_domain,
            "layout_mode": self.layout_mode,
            "manual_role_decisions": dict(self.manual_role_decisions),
            "provider_special_decisions": {
                str(key): dict(value)
                for key, value in self.provider_special_decisions.items()
            },
            "catalog_link_intent": (
                dict(self.catalog_link_intent) if self.catalog_link_intent else None
            ),
            "recovery_state": self.recovery_state,
            "execution_failure": (
                dict(self.execution_failure) if self.execution_failure else None
            ),
            "provider_link_binding": (
                dict(self.provider_link_binding)
                if self.provider_link_binding else None
            ),
            "source_episode_projection": (
                dict(self.source_episode_projection)
                if self.source_episode_projection else None
            ),
            "source_episode_projection_decision": (
                dict(self.source_episode_projection_decision)
                if self.source_episode_projection_decision else None
            ),
            "provider_subject_boundary_decisions": {
                str(key): dict(value)
                for key, value in self.provider_subject_boundary_decisions.items()
            },
            "tv_planning_authority": (
                self.tv_planning_authority.to_dict()
                if self.tv_planning_authority is not None else None
            ),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> "DecisionState":
        if not isinstance(value, Mapping):
            raise DecisionStateError("DecisionState 根节点必须是对象")
        version = int(value.get("schema_version", 0))
        if version != DECISION_STATE_SCHEMA_VERSION:
            raise DecisionStateError("DecisionState schema_version 不受支持")
        if "linked_provider_identities" not in value:
            raise DecisionStateError("DecisionState 缺少关联资料身份字段")
        evidence = value.get("provider_evidence", ())
        if not isinstance(evidence, list) or not all(isinstance(x, Mapping) for x in evidence):
            raise DecisionStateError("provider_evidence 格式无效")
        linked = value.get("linked_provider_identities")
        if not isinstance(linked, list) or not all(
            isinstance(item, Mapping) for item in linked
        ):
            raise DecisionStateError("linked_provider_identities 格式无效")
        roles = value.get("manual_role_decisions", {})
        if not isinstance(roles, Mapping):
            raise DecisionStateError("manual_role_decisions 格式无效")
        specials = value.get("provider_special_decisions", {})
        if not isinstance(specials, Mapping) or not all(
            isinstance(item, Mapping) for item in specials.values()
        ):
            raise DecisionStateError("provider_special_decisions 格式无效")
        boundary_decisions = value.get("provider_subject_boundary_decisions", {})
        if not isinstance(boundary_decisions, Mapping) or not all(
            isinstance(item, Mapping) for item in boundary_decisions.values()
        ):
            raise DecisionStateError("provider_subject_boundary_decisions 格式无效")
        def optional_mapping(name: str) -> Optional[Mapping[str, object]]:
            item = value.get(name)
            if item is not None and not isinstance(item, Mapping):
                raise DecisionStateError(f"{name} 格式无效")
            return dict(item) if isinstance(item, Mapping) else None
        confirmed = value.get("confirmed_work", {})
        if not isinstance(confirmed, Mapping):
            raise DecisionStateError("confirmed_work 格式无效")
        tv_authority_value = value.get("tv_planning_authority")
        if tv_authority_value is not None and not isinstance(
            tv_authority_value, Mapping
        ):
            raise DecisionStateError("tv_planning_authority 格式无效")
        return cls(
            episode_versions=tuple(EpisodeVersionDecision.from_mapping(r) for r in value.get("episode_versions", ())),
            episode_ranges=tuple(EpisodeRangeDecision.from_mapping(r) for r in value.get("episode_ranges", ())),
            task_id=str(value.get("task_id") or ""),
            revision=int(value.get("revision", 0)),
            confirmed_work=dict(confirmed),
            provider_evidence=tuple(dict(x) for x in evidence),
            linked_provider_identities=tuple(dict(item) for item in linked),
            series_binding=optional_mapping("series_binding"),
            season_binding=optional_mapping("season_binding"),
            library_domain=value.get("library_domain"),
            layout_mode=value.get("layout_mode"),
            manual_role_decisions={str(k): str(v) for k, v in roles.items()},
            provider_special_decisions={
                str(key): dict(item) for key, item in specials.items()
            },
            catalog_link_intent=optional_mapping("catalog_link_intent"),
            recovery_state=value.get("recovery_state"),
            execution_failure=optional_mapping("execution_failure"),
            provider_link_binding=optional_mapping("provider_link_binding"),
            source_episode_projection=optional_mapping(
                "source_episode_projection"
            ),
            source_episode_projection_decision=optional_mapping(
                "source_episode_projection_decision"
            ),
            provider_subject_boundary_decisions={
                str(key): dict(item)
                for key, item in boundary_decisions.items()
            },
            tv_planning_authority=(
                TVPlanningAuthority.from_mapping(tv_authority_value)
                if isinstance(tv_authority_value, Mapping)
                else None
            ),
            asset_operation=(AssetOperation.from_mapping(value['asset_operation']) if value.get('asset_operation') is not None else None),
            movie_version_selection=(MovieVersionSelection.from_mapping(value['movie_version_selection']) if value.get('movie_version_selection') is not None else None),
            content_selection=(ContentSelection.from_mapping(value['content_selection']) if value.get('content_selection') is not None else None),
            content_policy=(parse_content_policy(value['content_policy']) if 'content_policy' in value else None),
            initial_source_basis=(InitialSourceBasis.from_mapping(value['initial_source_basis'])
                                  if 'initial_source_basis' in value else None),
            pending_tv_selection=(PendingTVSelection.from_mapping(value['pending_tv_selection'])
                                  if 'pending_tv_selection' in value else None),
            completed_rebuild_observation=(CompletedRebuildObservation.from_mapping(value['completed_rebuild_observation'])
                if value.get('completed_rebuild_observation') is not None else None),
            updated_at=str(value.get("updated_at") or ""),
            schema_version=DECISION_STATE_SCHEMA_VERSION,
        )


class DecisionStateStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, task_id: str) -> Path:
        if not task_id or _SAFE_ID.fullmatch(str(task_id)) is None:
            raise DecisionStateError("DecisionState task_id 不安全")
        return self.root / f"{task_id}.json"

    def load(self, task_id: str) -> DecisionState:
        from .recognition_reset import require_ready
        require_ready(self.root.parent, task_id)
        path = self.path_for(task_id)
        try:
            state = DecisionState.from_dict(json.loads(path.read_text("utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DecisionStateError(f"无法读取 DecisionState：{exc}") from exc
        return state

    def load_readonly(self, task_id: str) -> DecisionState:
        """Load a current Decision for display without resetting its recovery gate."""

        path = self.path_for(task_id)
        try:
            return DecisionState.from_dict(json.loads(path.read_text("utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DecisionStateError(f"无法只读展示 DecisionState：{exc}") from exc

    def load_optional(self, task_id: str) -> Optional[DecisionState]:
        from .recognition_reset import require_ready
        require_ready(self.root.parent, task_id)
        path = self.path_for(task_id)
        return self.load(task_id) if path.exists() else None

    def create(self, task_id: str) -> DecisionState:
        with self._lock(task_id):
            if self.path_for(task_id).exists():
                raise DecisionStateError("DecisionState 已存在")
            from .recognition_reset import first_revision
            state = DecisionState(task_id=task_id, revision=first_revision(self.root.parent, task_id), updated_at=_utc_now())
            from .content_policy import validate_state_write
            if validate_state_write(self, None, state) is not state:
                raise DecisionStateError('Decision创建策略验证已断开')
            from .source_basis import validate_state_write as validate_basis_write
            validate_basis_write(self, None, state)
            self._atomic_write(state)
            return state

    @staticmethod
    def project_plan(state: DecisionState, plan: RenamePlan) -> RenamePlan:
        metadata = dict(plan.metadata)
        if state.pending_tv_selection is not None:
            metadata['pending_tv_selection'] = state.pending_tv_selection.to_dict()
        elif 'pending_tv_selection' in metadata:
            raise DecisionStateError('季度待确认缺少Decision依据')
        if state.initial_source_basis is not None:
            metadata['initial_source_basis'] = state.initial_source_basis.to_dict()
        elif 'initial_source_basis' in metadata:
            raise DecisionStateError('来源 basis 缺少Decision权威')
        if state.content_policy is not None:
            metadata['content_policy'] = state.content_policy.value
        elif 'content_policy' in metadata:
            raise DecisionStateError('Plan整理策略 policy 缺少Decision权威')
        work_identity = (
            WorkIdentity.from_dict(state.confirmed_work)
            if state.confirmed_work else None
        )
        decision_fingerprint = DecisionStateStore.fingerprint(state)
        metadata.update({
            "decision_state_revision": state.revision,
            "decision_state_fingerprint": decision_fingerprint,
        })
        receipt = metadata.get(
            "provider_subject_boundary_selection_receipt"
        )
        if (
            isinstance(receipt, Mapping)
            and receipt.get("schema_version") in {2, 3}
        ):
            metadata[
                "provider_subject_boundary_selection_receipt"
            ] = {
                **dict(receipt),
                "result_decision_revision": state.revision,
                "result_decision_fingerprint": decision_fingerprint,
            }
        binding = binding_from_decision(state)
        if binding is not None:
            metadata["catalog_binding_snapshot"] = binding.to_dict()
        else:
            metadata.pop("catalog_binding_snapshot", None)
        affiliation = (state.series_binding or state.catalog_link_intent or {}).get('affiliation')
        if affiliation is not None:
            from .work_affiliation import WorkAffiliation
            metadata['work_affiliation'] = WorkAffiliation.from_mapping(affiliation).to_dict()
        else:
            metadata.pop('work_affiliation', None)
        pending = (state.series_binding or state.catalog_link_intent or {}).get('pending_affiliation')
        if pending is not None:
            from .work_affiliation import PendingAffiliation
            metadata['pending_work_affiliation'] = PendingAffiliation.from_mapping(pending).to_dict()
        else:
            metadata.pop('pending_work_affiliation', None)
        if state.provider_link_binding is not None:
            metadata["provider_link_snapshot"] = dict(state.provider_link_binding)
        else:
            metadata.pop("provider_link_snapshot", None)
        if state.source_episode_projection is not None:
            metadata["source_episode_projection"] = dict(
                state.source_episode_projection
            )
            metadata.pop("source_episode_projection_candidates", None)
        elif "source_episode_projection_candidates" not in metadata:
            metadata.pop("source_episode_projection", None)
        if state.episode_versions:
            metadata['episode_versions'] = [d.to_dict() for d in state.episode_versions]
        elif metadata.get('episode_versions'):
            raise DecisionStateError('剧集版本缺少Decision权威')
        if state.episode_ranges:
            metadata['episode_ranges'] = [d.to_dict() for d in state.episode_ranges]
        elif metadata.get('episode_ranges'):
            raise DecisionStateError('连续集数缺少Decision权威')
        if state.provider_special_decisions:
            metadata["provider_special_decisions"] = {
                str(key): dict(value)
                for key, value in state.provider_special_decisions.items()
            }
        else:
            metadata.pop("provider_special_decisions", None)
        if state.series_binding is not None and pending is None:
            metadata['series_binding_snapshot'] = dict(state.series_binding)
            metadata['series_registration_authority'] = state.series_binding.get('registration_authority')
            series_key = str(state.series_binding.get("series_key") or "")
            canonical_root = str(
                state.series_binding.get("canonical_root") or ""
            )
            catalog_revision = state.series_binding.get("catalog_revision")
            if (
                not series_key
                or not isinstance(catalog_revision, int)
                or catalog_revision < 0
            ):
                raise DecisionStateError("作品系列决定不完整")
            series_projection = {
                "series_key": series_key,
                "series_catalog_revision": catalog_revision,
            }
            if canonical_root:
                series_projection["canonical_work_root"] = canonical_root
            metadata.update(series_projection)
        else:
            metadata.pop('series_binding_snapshot', None)
            metadata.pop('series_registration_authority', None)
        if state.season_binding is not None:
            binding_id = str(
                state.season_binding.get("season_binding_id") or ""
            )
            season_number = state.season_binding.get("season_number")
            season_source = str(
                state.season_binding.get("decision_source") or ""
            )
            if not binding_id or not isinstance(season_number, int):
                raise DecisionStateError("作品季度决定不完整")
            metadata.update({
                "season_binding_id": binding_id,
                "season_id": season_number,
                "season_number": season_number,
                "season_source": season_source or "provider_crosswalk",
            })
        linked_identities = tuple(
            ProviderIdentity.from_dict(item)
            for item in state.linked_provider_identities
        )
        from . import special_decision_projection
        projected = replace(
            plan, metadata=metadata, work_identity=work_identity,
            linked_provider_identities=linked_identities,
        )
        from .audio_projection import project_audio
        from . import content_selection
        if state.content_selection is not None:
            projected = content_selection.prepare_projection(projected, state.content_selection, state.movie_version_selection)
            from .content_associations import project_subtitles
            projected = project_subtitles(projected, state.content_selection.associations)
        elif projected.metadata.get('content_selection') is not None:
            raise DecisionStateError('内容选择缺少当前Decision权威')
        if state.movie_version_selection is not None:
            from .movie_version_selection import project_selection
            projected = project_selection(projected, state.movie_version_selection)
        elif projected.metadata.get('movie_version_selection') is not None:
            raise DecisionStateError('电影版本选择缺少当前Decision权威')
        projected = special_decision_projection.project_special_decisions(
            project_audio(projected), state.provider_special_decisions,
        )
        from .episode_range import project as project_ranges
        projected = project_ranges(projected, state.episode_ranges)
        from .episode_range import validate_projection as validate_ranges
        validate_ranges(projected)
        from . import episode_versions
        projected = episode_versions.project(projected, state.episode_versions)
        if type(projected) is not RenamePlan:
            raise DecisionStateError('剧集版本唯一投影器已断开')
        episode_versions.validate_projection(projected)
        if state.content_selection is not None:
            projected = project_subtitles(projected, state.content_selection.associations)
            projected = content_selection.filter_projection(projected, state.content_selection, state.movie_version_selection)
        return projected

    def prepare_save(self, state: DecisionState, *, expected_revision: int) -> DecisionState:
        """Freeze the exact bytes/fingerprint before the durable write begins."""

        return replace(
            state,
            revision=self._next_revision(state.task_id, expected_revision),
            updated_at=_utc_now(),
        )

    def _next_revision(self, task_id, expected_revision):
        from .recognition_reset import first_revision
        floor = first_revision(self.root.parent, task_id)
        if expected_revision != -1 and expected_revision < floor:
            raise DecisionStateError('此决定已因重新识别退出')
        return floor if expected_revision == -1 else int(expected_revision)+1

    def save_prepared(
        self,
        state: DecisionState,
        *,
        expected_revision: int,
    ) -> DecisionState:
        if state.revision != self._next_revision(state.task_id, expected_revision):
            raise DecisionStateError("预冻结 DecisionState revision 无效")
        with self._lock(state.task_id):
            current = self.load_optional(state.task_id)
            actual = current.revision if current is not None else -1
            if actual != expected_revision:
                raise DecisionStateError(
                    f"DecisionState 版本已变化：期望 {expected_revision}，实际 {actual}"
                )
            from .content_policy import validate_state_write
            if validate_state_write(self, current, state) is not state:
                raise DecisionStateError('Decision写入策略验证已断开')
            from .source_basis import validate_state_write as validate_basis_write
            validate_basis_write(self, current, state)
            if state.pending_tv_selection is not None or current is not None and current.pending_tv_selection is not None:
                from .source_basis import require_prepared_state_publication
                require_prepared_state_publication(self, current, state)
            self._atomic_write(state)
            return state

    def save(self, state: DecisionState, *, expected_revision: int) -> DecisionState:
        prepared = self.prepare_save(state, expected_revision=expected_revision)
        return self.save_prepared(prepared, expected_revision=expected_revision)

    def rollback_exact(
        self,
        written: DecisionState,
        previous: Optional[DecisionState],
    ) -> None:
        """Undo one exact just-written revision after its Plan write fails."""

        with self._lock(written.task_id):
            current = self.load_optional(written.task_id)
            if (
                current is None
                or self.fingerprint(current) != self.fingerprint(written)
            ):
                raise DecisionStateError(
                    "决定内容已被其他请求更新，无法安全回滚"
                )
            if previous is None:
                self.path_for(written.task_id).unlink(missing_ok=True)
            else:
                self._atomic_write(previous)

    def restore_recovery(
        self,
        task_id: str,
        *,
        expected_current_fingerprint: str,
        previous: Optional[DecisionState],
    ) -> None:
        """Restore a journaled Decision only when the current value is exact."""

        with self._lock(task_id):
            current = self.load_optional(task_id)
            if (
                current is None
                or self.fingerprint(current) != expected_current_fingerprint
            ):
                raise DecisionStateError(
                    "DecisionState 已变化，拒绝覆盖式恢复"
                )
            if previous is None:
                self.path_for(task_id).unlink(missing_ok=True)
            else:
                if previous.task_id != task_id:
                    raise DecisionStateError("恢复 DecisionState task_id 不一致")
                self._atomic_write(previous)

    @staticmethod
    def fingerprint(state: DecisionState) -> str:
        payload = json.dumps(
            state.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _primary_provider_key(
        evidence: tuple[Mapping[str, object], ...],
    ) -> tuple[str, str, str] | None:
        if not evidence:
            return None
        first = evidence[0]
        key = tuple(
            str(first.get(field) or "")
            for field in ("namespace", "entity_type", "provider_id")
        )
        return key if all(key) else None

    def _atomic_write(self, state: DecisionState) -> None:
        from .recognition_reset import first_revision
        if state.revision < first_revision(self.root.parent, state.task_id):
            raise DecisionStateError('此决定已因重新识别退出')
        destination = self.path_for(state.task_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                json.dump(state.to_dict(), handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _lock_resource(self, task_id):
        from .file_lock import LockResource
        return LockResource(self, self.root / f'.{self.path_for(task_id).name}.lock', f'decision_state:{task_id}')

    @contextmanager
    def _lock(self, task_id: str):
        from .file_lock import resource_guard
        try:
            with resource_guard(
                self._lock_resource(task_id),
                owner={"kind": "decision_state", "task_id": task_id},
            ):
                yield
        except FileLockBusyError as exc:
            raise DecisionStateError("DecisionState 正被其他请求更新，请刷新后重试") from exc


Planner = Callable[[DecisionState], RenamePlan]


class DecisionCommandService:
    """The sole write boundary for task decisions and their Plan projection."""

    def __init__(self, state_store: DecisionStateStore, plan_store: PlanStore, *, media_archive_context=None,
                 initial_movie_completion_context=None):
        self.state_store = state_store
        self.plan_store = plan_store
        self.media_archive_context = media_archive_context
        self.initial_movie_completion_context = initial_movie_completion_context

    def update(
        self,
        task_id: str,
        *,
        expected_revision: int,
        changes: Mapping[str, object],
        planner: Optional[Planner] = None,
        task_terminal: Optional[object] = None,
        cohort_authority: Optional["CohortAuthorityDraft"] = None,
        media_archive=None,
        initial_movie_completion=None,
    ) -> tuple[DecisionState, Optional[RenamePlan]]:
        if initial_movie_completion is not None and planner is None:
            raise DecisionStateError('电影编辑意图必须同笔生成新计划')
        current = self.state_store.load(task_id)
        if current.revision != expected_revision:
            raise DecisionStateError("DecisionState 版本已变化，请刷新后重试")
        allowed = {
            "confirmed_work", "provider_evidence", "series_binding",
            "linked_provider_identities",
            "season_binding", "library_domain", "layout_mode",
            "manual_role_decisions", "catalog_link_intent", "recovery_state",
            "execution_failure", "provider_link_binding",
            "provider_special_decisions",
            "source_episode_projection", "source_episode_projection_decision",
            "provider_subject_boundary_decisions",
            "tv_planning_authority",
            "completed_rebuild_observation",
            "asset_operation",
            "movie_version_selection",
            "content_selection",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise DecisionStateError(f"不允许更新 DecisionState 字段：{sorted(unknown)}")
        candidate = replace(current, **dict(changes))
        plan = None
        if planner is not None:
            # A planner exception must not advance authoritative state while
            # leaving the previous Plan in place.
            plan = planner(candidate)
            if plan.task_id != task_id:
                raise DecisionStateError("Planner 返回了其他任务的 Plan")
        if plan is None:
            from types import SimpleNamespace
            from .content_policy import _scope_binding
            context = SimpleNamespace(plan_store=self.plan_store, task_root=self.plan_store.root.parent / 'task')
            binding = _scope_binding(context, task_id)
            if binding.policy != candidate.content_policy:
                raise DecisionStateError('无Plan写入的默认整理策略 policy 与scope不一致')
            if current.initial_source_basis is not None:
                from .source_basis import validate_inheritance
                validate_inheritance(context, current.initial_source_basis, binding)
            if current.content_selection is not None or media_archive is not None:
                raise DecisionStateError('内容任务的Decision必须与Plan一同修订，不能脱离计划写入')
            if candidate.content_selection != current.content_selection:
                raise DecisionStateError('内容选择必须与Decision/Plan原子提交')
            if cohort_authority is not None:
                raise DecisionStateError(
                    "季度权威必须与Decision/Plan投影原子提交"
                )
            revised = self.state_store.save(
                candidate, expected_revision=expected_revision
            )
            return revised, None
        return self._commit_projection(
            current,
            candidate,
            plan,
            task_terminal=task_terminal,
            cohort_authority=cohort_authority,
            media_archive=media_archive,
            initial_movie_completion=initial_movie_completion,
        )

    def _commit_projection(
        self,
        previous: Optional[DecisionState],
        candidate: DecisionState,
        plan: RenamePlan,
        *,
        task_terminal: Optional[object] = None,
        cohort_authority: Optional["CohortAuthorityDraft"] = None,
        media_archive=None,
        initial_movie_completion=None,
    ) -> tuple[DecisionState, RenamePlan]:
        from .projection_authority import commit_projected_plan

        return commit_projected_plan(
            self.state_store,
            self.plan_store,
            previous=previous,
            candidate=candidate,
            draft_plan=plan,
            task_terminal=task_terminal,
            cohort_authority=cohort_authority,
            media_archive=media_archive,
            media_archive_context=self.media_archive_context,
            initial_movie_completion=initial_movie_completion,
            initial_movie_completion_context=self.initial_movie_completion_context,
        )
