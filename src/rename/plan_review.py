"""Version-bound human review decisions for rename plans.

This module records decisions only.  It never invokes the transfer executor.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Optional, Tuple

from .decision_state import DecisionStateStore
from .plan_store import PlanStore, PlanStoreError
from .source_partition import SourcePartitionError, SourcePartitionStore
from .projection_authority import (
    RecognitionDecisionInput,
    TaskTerminalMutation,
    commit_recognition_projection,
    persist_plan_revision,
)
from .plan_verifier import PlanVerifier
from .rename_plan import (
    AssetRole,
    EditionValue,
    EvidenceCandidate,
    EvidenceValue,
    PlanEntry,
    SegmentDescriptor,
    PresentationAssetPlanContract,
    PresentationGroup,
    ProviderIdentity,
    RenamePlan,
    SubtitlePlanContract,
    SubtitleOverrideRule,
    SubtitleOverrideScope,
    SubtitleRuleMatcher,
    SubtitleRuleOutcome,
    UnknownTokenDisposition,
    WorkIdentity,
    VersionDescriptor,
    build_version_ref,
    derive_work_domain,
)
from ..utils.path import WORK_CATALOG_PATH
from .work_catalog import WorkCatalog, WorkCatalogError
from .work_intent import (
    derive_work_intent_key,
)
from .utils import EXTRA_TAG, canonical_extras_target, jellyfin_season_dir
from .media_classifier import (
    LocalRole,
    MediaClassification,
    MediaPreclassification,
    PreclassificationKind,
    preclassify_media,
)
from .subtitle_labels import DimensionResolution
from .subtitle_workflow import (
    VideoPathMapping,
    build_subtitle_decisions,
    decision_to_plan_contract,
    format_subtitle_target_fields,
)
from .presentation import rebind_content_version_identity
from .presentation import (
    build_manual_version_descriptor,
    default_presentation_group_id,
    provider_identity_from_metadata,
)
from .rename_plan import build_default_version_descriptor
from .asset_collision import AssetCollisionError, build_reuse_resolution

from .canonical_naming import (
    CanonicalNamingError,
    build_canonical_work_root,
    resolve_fresh_collision_root,
    canonicalize_media_leaf,
    decision_from_work_identity,
    read_naming_decision,
)
from .series_catalog import SeriesCatalog, SeriesCatalogError


REVIEW_SCHEMA_VERSION = 1
_SEASON_SOURCES = frozenset({
    "unset", "inferred_path", "provider", "provider_crosswalk", "explicit_user",
    "legacy_placeholder", "legacy_explicit",
})


class ReviewStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class PlanReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalTaskContext:
    """The exact visible task/Decision state captured before queueing approval."""
    decision_revision: int
    decision_fingerprint: str
    workflow_status: Optional[str]
    recovery_disposition: Optional[str]

    def validate(self, task, state, fingerprint):
        from .execution_recovery import requires_execution_reapproval
        if (not task or state is None or state.revision != self.decision_revision
                or DecisionStateStore.fingerprint(state) != self.decision_fingerprint
                or task.get('workflow_status') != self.workflow_status
                or task.get('execution_recovery_disposition') != self.recovery_disposition
                or self.workflow_status in {'queued', 'executing', 'completed', 'retired'}
                or (self.recovery_disposition == 'reapproval_required' and (
                    not requires_execution_reapproval(task)
                    or task.get('execution_requested_fingerprint') != fingerprint))):
            raise PlanReviewError('任务状态或整理版本已变化，请刷新后重新审核')


def _season_decision_from_metadata(
    metadata: Mapping[str, object],
) -> tuple[Optional[int], str]:
    """Read season state without treating old ``0`` as an explicit special.

    Old plans did not persist provenance.  Their zero is intrinsically
    ambiguous, so it becomes a legacy placeholder and must be confirmed again
    by the TV completion action.
    """

    raw_source = str(metadata.get("season_source") or "").strip()
    raw_value = metadata.get("season_number")
    if raw_value in (None, ""):
        raw_value = metadata.get("season_id")
    if raw_source and raw_source not in _SEASON_SOURCES:
        raise PlanReviewError("季度来源无效，无法安全生成剧集目标")
    if raw_value in (None, ""):
        return None, raw_source or "unset"
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise PlanReviewError("季度编号无效，无法生成剧集目标") from exc
    if value < 0:
        raise PlanReviewError("季度编号不能为负数")
    if not raw_source or raw_source == "unset":
        return (None, "legacy_placeholder") if value == 0 else (value, "legacy_explicit")
    return value, raw_source


@dataclass(frozen=True)
class InventoryRoleDecision:
    """One replayable user decision for an inventory video."""

    source_path: Path
    role: str

    def to_dict(self) -> dict:
        return {"source_path": str(self.source_path), "role": self.role}

    @classmethod
    def from_dict(cls, data: object) -> "InventoryRoleDecision":
        if not isinstance(data, Mapping):
            raise PlanReviewError("人工用途记录格式无效")
        source = data.get("source_path")
        role = data.get("role")
        if not isinstance(source, str) or not source.strip() or not isinstance(role, str):
            raise PlanReviewError("人工用途记录不完整")
        return cls(Path(source), role.strip())


@dataclass(frozen=True)
class InventoryCompletionContext:
    """Frozen input needed to replay a completed inventory plan safely.

    Completion normalizes presentation group IDs.  Keeping the source draft
    fingerprint and original group ID separate prevents a later role repair
    from treating the normalized ID as an ID in the source draft.
    """

    origin_plan_fingerprint: str
    origin_group_id: str
    group_kind: str
    work_domain: str
    target_root: Path
    root_snapshot: Mapping[str, object]
    manual_role_decisions: Mapping[str, InventoryRoleDecision]
    season_number: Optional[int] = None
    season_source: str = "unset"
    schema_version: int = 3

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "origin_plan_fingerprint": self.origin_plan_fingerprint,
            "origin_group_id": self.origin_group_id,
            "group_kind": self.group_kind,
            "work_domain": self.work_domain,
            "target_root": str(self.target_root),
            "root_snapshot": dict(self.root_snapshot),
            "manual_role_decisions": {
                identifier: decision.to_dict()
                for identifier, decision in self.manual_role_decisions.items()
            },
            "season_number": self.season_number,
            "season_source": self.season_source,
        }

    @classmethod
    def from_metadata(cls, value: object) -> "InventoryCompletionContext":
        if not isinstance(value, Mapping) or value.get("schema_version") not in {2, 3}:
            raise PlanReviewError("当前整理计划缺少可重放的用途确认上下文")
        decisions_raw = value.get("manual_role_decisions", {})
        if not isinstance(decisions_raw, Mapping):
            raise PlanReviewError("人工用途记录格式无效")
        decisions = {
            str(identifier): InventoryRoleDecision.from_dict(item)
            for identifier, item in decisions_raw.items()
        }
        fields = ("origin_plan_fingerprint", "origin_group_id", "group_kind", "work_domain", "target_root")
        if any(not isinstance(value.get(name), str) or not str(value.get(name)).strip() for name in fields):
            raise PlanReviewError("用途确认上下文不完整")
        snapshot = value.get("root_snapshot", {})
        if not isinstance(snapshot, Mapping):
            raise PlanReviewError("用途确认的目标根快照无效")
        return cls(
            origin_plan_fingerprint=str(value["origin_plan_fingerprint"]),
            origin_group_id=str(value["origin_group_id"]),
            group_kind=str(value["group_kind"]),
            work_domain=str(value["work_domain"]),
            target_root=Path(str(value["target_root"])),
            root_snapshot=dict(snapshot),
            manual_role_decisions=decisions,
            season_number=(
                int(value["season_number"])
                if value.get("season_number") is not None else None
            ),
            season_source=str(value.get("season_source") or "unset"),
        )


@dataclass(frozen=True)
class PlanReviewDecision:
    task_id: str
    plan_fingerprint: str
    approved_entry_ids: Tuple[str, ...] = ()
    status: ReviewStatus = ReviewStatus.DRAFT

    def to_dict(self) -> dict:
        return {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "task_id": self.task_id,
            "plan_fingerprint": self.plan_fingerprint,
            "approved_entry_ids": list(self.approved_entry_ids),
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "PlanReviewDecision":
        if data.get("schema_version") != REVIEW_SCHEMA_VERSION:
            raise ValueError("Unsupported review schema_version")
        task_id = data.get("task_id")
        fingerprint = data.get("plan_fingerprint")
        approved = data.get("approved_entry_ids", [])
        status = data.get("status")
        if not isinstance(task_id, str) or not isinstance(fingerprint, str):
            raise ValueError("Review task_id and fingerprint must be strings")
        if not isinstance(approved, list) or not all(
            isinstance(item, str) for item in approved
        ):
            raise ValueError("approved_entry_ids must be a list of strings")
        try:
            review_status = ReviewStatus(status)
        except ValueError as exc:
            raise ValueError(f"Unsupported review status: {status!r}") from exc
        return cls(
            task_id=task_id,
            plan_fingerprint=fingerprint,
            approved_entry_ids=tuple(approved),
            status=review_status,
        )


def entry_id(entry: PlanEntry) -> str:
    identity = os.path.normpath(str(entry.source_path)).casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]




def provider_special_entry_closure(plan: RenamePlan, selected_entry_id: str) -> tuple[tuple[int, PlanEntry], ...]:
    """Compatibility entrypoint delegating to the canonical typed bundle."""
    from . import special_decision_projection
    try:
        return special_decision_projection.video_bundle_closure(plan, selected_entry_id)
    except special_decision_projection.SpecialDecisionProjectionError as exc:
        raise PlanReviewError(str(exc)) from exc


def apply_subtitle_rule_entries(plan, rule, *, associated_video_source_path=None):
    """One pure rule application for public edits and canonical verification."""
    parsed_scope, outcome, reason = rule.scope, rule.outcome, rule.reason
    impacted = [index for index, entry in enumerate(plan.entries)
        if entry.subtitle is not None and (
            entry_id(entry) == rule.selected_entry_id if parsed_scope == SubtitleOverrideScope.FILE
            else _subtitle_matcher(entry.subtitle) == rule.matcher)]
    if not impacted or (parsed_scope == SubtitleOverrideScope.FILE and len(impacted) != 1):
        raise PlanReviewError('字幕规则没有唯一选中来源或匹配材料')
    entries = list(plan.entries)
    for index in impacted:
        current = entries[index]
        current_contract = current.subtitle
        assert current_contract is not None
        selected_video = (
            associated_video_source_path or current_contract.associated_video_source_path
            if parsed_scope == SubtitleOverrideScope.FILE
            else current_contract.associated_video_source_path
        )
        if selected_video is None:
            raise PlanReviewError("未关联字幕必须先选择一个视频源文件")
        associated_source = Path(selected_video)
        videos = [
            entry for entry in plan.entries
            if entry.media_kind == "video"
            and os.path.normpath(str(entry.source_path)).casefold()
            == os.path.normpath(str(associated_source)).casefold()
        ]
        if len(videos) != 1:
            raise PlanReviewError("字幕关联的视频条目不存在或不唯一")

        titles = _titles_for_contract(
            outcome.track_title_tokens,
            current_contract.unknown_tokens,
            outcome.unknown_dispositions,
        )
        revised_contract = replace(
            current_contract,
            associated_video_source_path=associated_source,
            primary_language=outcome.primary_language,
            script=outcome.script,
            region_hint=outcome.region_hint,
            additional_languages=outcome.additional_languages,
            track_title_tokens=titles,
            flags=outcome.flags,
            unknown_dispositions=_dispositions_for_contract(
                current_contract.unknown_tokens, outcome.unknown_dispositions
            ),
            association_state=DimensionResolution.RESOLVED,
            parse_state=DimensionResolution.RESOLVED,
            decision_source=f"manual_{parsed_scope.value}",
            manual_reason=str(reason).strip(),
            reason_codes=current_contract.reason_codes + ("SUBTITLE_MANUAL_RULE",),
            applied_rule_id=rule.rule_id,
            applied_rule_fingerprint=rule.rule_fingerprint,
            applied_rule_scope=rule.scope.value,
            applied_rule_engine_version=rule.engine_version,
            inherited_edit_fingerprint='',
        )
        try:
            target = format_subtitle_target_fields(
                target_video=videos[0].target_path,
                source_subtitle=current.source_path,
                primary_language=revised_contract.primary_language or "",
                script=revised_contract.script,
                track_title_tokens=revised_contract.track_title_tokens,
                flags=revised_contract.flags,
            )
        except ValueError as exc:
            raise PlanReviewError(f"字幕目标无法格式化：{exc}") from exc
        entries[index] = replace(
            current,
            target_path=target,
            subtitle=revised_contract,
            requires_review=True,
            evidence=current.evidence + (str(reason).strip(),),
        )
    return tuple(entries)


class PlanReviewService:
    def __init__(
        self,
        plan_store: PlanStore,
        review_root: Path,
        catalog_root: Path = WORK_CATALOG_PATH,
        decision_state_root: Optional[Path] = None,
        task_root: Optional[Path] = None,
        provider_link_catalog_root: Optional[Path] = None,
        record_root: Optional[Path] = None,
        root_snapshot_source=None,
    ):
        self.plan_store = plan_store
        self.root_snapshot_source = root_snapshot_source
        self.review_root = Path(review_root)
        self._path_helper = PlanStore(self.review_root)
        self.work_catalog = WorkCatalog(catalog_root)
        self.decision_state_store = DecisionStateStore(
            decision_state_root or (plan_store.root.parent / "decision_state")
        )
        self.task_root = Path(task_root or (plan_store.root.parent / "task"))
        self.record_root = Path(record_root or plan_store.root.parent / 'record')
        self.series_catalog = SeriesCatalog(
            Path(catalog_root).parent / "series_catalog"
        )
        from .provider_link_catalog import ProviderLinkCatalog
        self.provider_link_catalog = ProviderLinkCatalog(
            provider_link_catalog_root or Path(catalog_root).parent / "provider_link_catalog")
        from .completed_rebuild import CompletedRebuildService
        self.rebuild_service = CompletedRebuildService.from_execution_context(
            plan_store=self.plan_store, task_root=self.task_root,
            decision_root=self.decision_state_store.root, record_root=self.record_root,
            catalog_root=self.work_catalog.root, series_catalog_root=self.series_catalog.root,
            provider_link_catalog_root=self.provider_link_catalog.root)
        self.work_catalog.rebuild_context = self.rebuild_service.work_context()

    def prepare_initial_movie_completion(self, task_id, expected_fingerprint, *, groups, excluded_sources=(),
                                         reason, replace_version_ref=None):
        from .initial_movie_completion import prepare
        return prepare(self, task_id, expected_fingerprint, groups=groups, excluded_sources=excluded_sources,
            reason=reason, replace_version_ref=replace_version_ref)

    def commit_initial_movie_completion(self, intent):
        from .initial_movie_completion import commit
        return commit(self, intent)

    def prepare_movie_work_correction(self, task_id, expected_fingerprint, *, expected_revision, candidate):
        from .movie_work_correction import prepare
        return prepare(self, task_id, expected_fingerprint, expected_revision=expected_revision, candidate=candidate)

    def prepare_content_audio_association(self, task_id, expected_fingerprint, *, source_path, video_source_path, reason):
        from .content_correction import prepare_audio
        return prepare_audio(self, task_id, expected_fingerprint, source_path=source_path,
                             video_source_path=video_source_path, reason=reason)

    def commit_content_audio_association(self, intent):
        from .content_correction import commit
        return commit(self, intent)

    def prepare_tv_boundary_completion(self, task_id, expected_fingerprint, *, review_command):
        from .tv_boundary_completion import prepare
        return prepare(self, task_id, expected_fingerprint, review_command=review_command)

    def commit_tv_boundary_completion(self, intent):
        from .tv_boundary_completion import commit
        return commit(self, intent)

    def cancel_tv_boundary_completion(self, task_id, expected_fingerprint, *, request_id):
        from .pending_tv_selection import cancel
        return cancel(self, task_id, expected_fingerprint, request_id=request_id)

    def observe_work_correction(self, task_id, expected_fingerprint, *, expected_revision, candidate):
        from .work_correction import observe
        return observe(self, task_id, expected_fingerprint, expected_revision=expected_revision, candidate=candidate)

    def prepare_work_correction(self, observation, *, season_number, reason):
        from .work_correction import prepare
        return prepare(self, observation, season_number=season_number, reason=reason)

    def commit_work_correction(self, intent):
        from .work_correction import commit
        return commit(self, intent)

    def revise_tv_content(self, task_id, expected_fingerprint, *, expected_revision, mode, edit, affiliation_choice=None):
        from .initial_tv_completion import prepare, commit
        from .tv_completion_observations import FrozenTVResolution, TVSeasonObservation
        from .content_associations import encode
        from .series_registration import _plain
        from .rename_plan import WorkIdentity, ProviderIdentity
        from .projection_authority import RecognitionDecisionInput
        from .content_choice import _load
        current, state = _load(self, task_id, expected_fingerprint)
        if type(expected_revision) is not int or state.revision != expected_revision or mode not in {'role', 'episode', 'special', 'special_refresh', 'season', 'content', 'affiliation', 'range', 'versions'}:
            raise PlanReviewError('TV确认页面已变化，请刷新后重新选择')
        if 'tv_completion_resolution' not in current.metadata:
            raise PlanReviewError('旧TV计划尚无完整冻结观察，请重新生成')
        raw = current.metadata['tv_completion_resolution']
        resolution = FrozenTVResolution(encode(_plain(raw))).resolution() if raw is not None else None
        recognition = RecognitionDecisionInput(task_id, WorkIdentity.from_dict(state.confirmed_work),
            tuple(ProviderIdentity.from_dict(row) for row in state.linked_provider_identities),
            (state.season_binding or {}).get('season_number'), (state.season_binding or {}).get('decision_source') or 'provider_crosswalk',
            'tv_episodes', DecisionStateStore.fingerprint(state))
        special = _plain(current.metadata.get('tv_completion_special'))
        if mode == 'special_refresh':
            if edit and set(edit) != {'counterpart_url'}:
                raise PlanReviewError('获取特别篇资料只能确认另一侧作品链接')
            from .provider_special_evidence import special_provider_ids, ProviderSpecialObservation, validate_special_identity
            from .special_crosswalk import observe
            primary = current.work_identity.primary_provider
            provider = primary.to_dict()
            known = [p.to_dict() for p in current.linked_provider_identities
                     if (p.namespace, p.entity_type) in {('bangumi', 'subject'), ('tmdb', 'tv')} and p.status == 'confirmed']
            known.extend(dict(namespace='tmdb', entity_type='tv', provider_id=i)
                         for i in special_provider_ids(current, resolution))
            if edit:
                from .provider_search import parse_provider_item_url
                from .special_catalog import identity
                url = edit['counterpart_url']
                if not isinstance(url, str) or not url.strip():
                    raise PlanReviewError('请填写另一侧作品的公开条目链接')
                namespace, entity_type, provider_id = parse_provider_item_url(url)
                linked = identity(dict(namespace=namespace, entity_type=entity_type, provider_id=provider_id))
                if namespace == provider['namespace']:
                    raise PlanReviewError('请填写另一侧数据库的作品链接；当前作品保持不变')
                known.append(linked)
            previous = _plain(current.metadata.get('provider_special_observation'))
            if previous is not None:
                validate_special_identity(current, ProviderSpecialObservation.from_mapping(previous), resolution)
            observed = observe(provider, current.metadata.get('season_number'), providers=known, previous=previous, discover_counterpart=True)
            special = observed.to_dict()
            edit = {}
        intent = prepare(self, recognition, resolution=resolution,
            seasons=TVSeasonObservation.from_mapping(_plain(current.metadata.get('tv_completion_seasons'))),
            special=special, mode=mode, edit=edit, affiliation_choice=affiliation_choice)
        _, projected = commit(self, intent, task_terminal=self._revision_task_terminal(task_id))
        return self._record_revision(projected)

    def load(self, task_id: str) -> PlanReviewDecision:
        path = self._path_helper.path_for(task_id)
        try:
            with open(path, "r", encoding="utf-8") as file:
                payload = json.load(file)
            if not isinstance(payload, dict):
                raise ValueError("Review root must be an object")
            decision = PlanReviewDecision.from_dict(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PlanReviewError(f"无法读取审核决定：{exc}") from exc
        if decision.task_id != str(task_id):
            raise PlanReviewError("审核决定 task_id 不匹配")
        return decision

    def delete(self, task_id: str) -> None:
        self._path_helper.path_for(task_id).unlink(missing_ok=True)

    def load_or_create(self, plan: RenamePlan) -> PlanReviewDecision:
        fingerprint = self.plan_store.fingerprint(plan)
        review_path = self._path_helper.path_for(plan.task_id)
        if not review_path.exists():
            decision = PlanReviewDecision(plan.task_id, fingerprint)
            self._save(decision)
            return decision
        decision = self.load(plan.task_id)
        if decision.plan_fingerprint != fingerprint:
            decision = PlanReviewDecision(plan.task_id, fingerprint)
            self._save(decision)
        return decision

    def approve_entries(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_ids: Iterable[str],
        *, task_context: Optional[ApprovalTaskContext] = None,
    ) -> PlanReviewDecision:
        from .file_lock import task_operation_lock, FileLockBusyError
        from .projection_authority import ProjectionAuthorityError
        try:
            with task_operation_lock(self.task_root, task_id,
                    operation='plan_approval', fingerprint=expected_fingerprint):
                return self._approve_entries_with_operation_lock(
                    task_id, expected_fingerprint, selected_entry_ids, task_context=task_context)
        except FileLockBusyError as exc:
            raise PlanReviewError('任务正在执行、批准或安全结束，请稍后重试') from exc
        except ProjectionAuthorityError as exc:
            raise PlanReviewError('当前计划缺少有效核对凭据或凭据已失效，只读查看，不能批准。请安全结束此任务后重新添加来源；源文件不会删除。') from exc

    def _approve_entries_with_operation_lock(self, task_id, expected_fingerprint, selected_entry_ids, *, task_context=None):
        partition_store = SourcePartitionStore(
            self.plan_store.root.parent / "source_partition",
            allowed_roots=(self.plan_store.root.parent,),
        )
        try:
            with partition_store.task_lock(task_id):
                if partition_store.pending_for_task(task_id):
                    raise PlanReviewError(
                        "source partition尚未完成，当前Plan不能批准"
                    )
                from .projection_authority import ProjectionCommitService
                projection = ProjectionCommitService(self.decision_state_store, self.plan_store,
                                                     task_root=self.task_root)
                with projection._transaction_lock(task_id):
                    return self._approve_entries_without_partition_guard(
                        task_id, expected_fingerprint, selected_entry_ids, task_context=task_context)
        except SourcePartitionError as exc:
            raise PlanReviewError(str(exc)) from exc

    def _approve_entries_without_partition_guard(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_ids: Iterable[str],
        *, task_context: Optional[ApprovalTaskContext] = None,
    ) -> PlanReviewDecision:
        plan = self._current_plan(task_id, expected_fingerprint)
        from ..utils.utils import task_record_snapshot_at, replace_task_record_at_if_sha256
        _raw, task, task_sha = task_record_snapshot_at(self.task_root, task_id)
        if task_context is not None and type(task_context) is not ApprovalTaskContext:
            raise PlanReviewError('批准缺少有效的任务上下文')
        from .recovery_transaction import require_no_pending_detach
        require_no_pending_detach(plan_store=self.plan_store, task_root=self.task_root,
            decision_root=self.decision_state_store.root, task_id=task_id)
        from .selection_gate import validate_selection_for_plan, require_not_retired
        from ..utils.utils import read_task_at
        try:
            require_not_retired(task_id, self.plan_store.root, task_root=self.task_root,
                                plan_fingerprint=expected_fingerprint)
            validate_selection_for_plan(read_task_at(self.task_root, task_id))
        except ValueError as exc:
            raise PlanReviewError(str(exc)) from exc
        from . import content_selection
        content_state = self.decision_state_store.load_optional(task_id)
        if task_context is not None:
            task_context.validate(task, content_state, expected_fingerprint)
        choice = content_selection.validate_plan_content(plan, content_state)
        if choice is not None:
            if content_state is None:
                raise PlanReviewError('内容选择缺少当前Decision权威')
            from .content_choice import validate_scope
            validate_scope(self, plan, choice)
            content_selection.validate_omission_manifest(choice, movie_selection=content_state.movie_version_selection)
        verification = PlanVerifier().verify(plan)
        if verification.issues:
            codes = ", ".join(issue.code for issue in verification.issues)
            raise PlanReviewError(f"Plan 结构无效，不能批准：{codes}")
        try:
            self.asset_actions().validate(plan, self.decision_state_store.load(task_id))
        except (ValueError, RuntimeError, OSError) as exc:
            raise PlanReviewError(str(exc)) from exc
        valid_ids = {entry_id(entry) for entry in plan.entries}
        if plan.metadata.get('completed_rebuild_observation') is not None:
            from .completed_rebuild import CompletedRebuildService
            try:
                CompletedRebuildService.from_execution_context(plan_store=self.plan_store,
                    task_root=self.task_root, decision_root=self.decision_state_store.root,
                    record_root=self.record_root, catalog_root=self.work_catalog.root,
                    series_catalog_root=self.series_catalog.root,
                    provider_link_catalog_root=self.provider_link_catalog.root).validate_plan(plan)
            except (ValueError, RuntimeError, OSError) as exc:
                raise PlanReviewError(str(exc)) from exc
        from . import series_registration
        if series_registration.is_series_plan(plan):
            try:
                series_registration.validate_plan_series_registration(plan, self.decision_state_store.load(plan.task_id))
                series_registration.validate_current_catalog(plan, self.series_catalog, self.provider_link_catalog)
                series_registration.validate_current_task_set(self.plan_store, self.decision_state_store, self.task_root,
                    expected_plan=plan, record_root=self.record_root, series_catalog=self.series_catalog, provider_catalog=self.provider_link_catalog)
            except (ValueError, OSError, RuntimeError) as exc:
                raise PlanReviewError(str(exc)) from exc
        requested = set(selected_entry_ids)
        unknown = requested - valid_ids
        if unknown:
            raise PlanReviewError(f"批准包含未知条目：{sorted(unknown)}")
        decision = self.load_or_create(plan)
        approved = set(decision.approved_entry_ids) | requested
        status = (
            ReviewStatus.APPROVED
            if approved == valid_ids
            else ReviewStatus.DRAFT
        )
        updated = PlanReviewDecision(
            task_id=plan.task_id,
            plan_fingerprint=expected_fingerprint,
            approved_entry_ids=tuple(sorted(approved)),
            status=status,
        )
        # Projection writers use the same lock. Still re-read at the write
        # boundary, so an external record edit cannot bless a stale decision.
        self._current_plan(task_id, expected_fingerprint)
        latest_state = self.decision_state_store.load_optional(task_id)
        if (latest_state != content_state or (content_state is not None and (
                plan.metadata.get('decision_state_fingerprint') != DecisionStateStore.fingerprint(content_state)
                or plan.metadata.get('decision_state_revision') != content_state.revision))):
            raise PlanReviewError('批准期间作品决定已变化，请重新打开计划')
        _raw, latest_task, latest_sha = task_record_snapshot_at(self.task_root, task_id)
        if latest_sha != task_sha:
            raise PlanReviewError('批准期间任务状态已变化，请重新打开计划')
        self._save(updated)
        if task_context is not None and status == ReviewStatus.APPROVED:
            task_context.validate(latest_task, latest_state, expected_fingerprint)
            latest_task['workflow_status'] = 'approved'
            if task_context.recovery_disposition == 'reapproval_required':
                latest_task.update(execution_reapproval_fingerprint=expected_fingerprint,
                    execution_recovery_disposition=None, execution_requires_manual_check=False)
            try:
                replace_task_record_at_if_sha256(self.task_root, task_id,
                    expected_sha256=task_sha, data=latest_task)
            except (ValueError, RuntimeError, OSError) as exc:
                raise PlanReviewError('批准记录已保存，但任务状态已变化；请重新打开核对，不会自动执行。') from exc
        return updated

    def override_target(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_id: str,
        target_path: Path,
        reason: str,
    ) -> RenamePlan:
        plan = self._current_plan(task_id, expected_fingerprint)
        if not str(reason).strip():
            raise PlanReviewError("人工覆盖必须填写理由")
        if not isinstance(target_path, (str, Path)) or not str(target_path).strip():
            raise PlanReviewError("人工覆盖必须填写目标路径")
        requested_target = Path(target_path)
        target_root = _target_root(plan)
        if not _is_within(requested_target, target_root):
            raise PlanReviewError(
                f"人工覆盖目标越出任务目标根目录：{target_root}"
            )
        matching = [
            index
            for index, entry in enumerate(plan.entries)
            if entry_id(entry) == selected_entry_id
        ]
        if len(matching) != 1:
            raise PlanReviewError("人工覆盖条目不存在或不唯一")
        index = matching[0]
        entries = list(plan.entries)
        original = entries[index]
        if original.media_kind == "subtitle":
            raise PlanReviewError(
                "字幕不能手写完整目标路径；请使用结构化字幕决定入口"
            )
        derived = PlanEntry.from_paths(original.source_path, requested_target)
        group_kinds = {
            group.group_id: group.kind for group in plan.presentation_groups
        }
        presentation_asset = rebind_content_version_identity(
            original.presentation_asset,
            group_kind=group_kinds.get(
                original.presentation_asset.presentation_group, ""
            ) if original.presentation_asset is not None else "",
            season_id=derived.season_id,
            episode_id=derived.episode_id,
        )
        entries[index] = replace(
            original,
            target_path=requested_target,
            season_id=derived.season_id,
            episode_id=derived.episode_id,
            requires_review=True,
            evidence=original.evidence + (str(reason).strip(),),
            presentation_asset=presentation_asset,
        )
        revised = replace(plan, entries=tuple(entries))
        verification = PlanVerifier().verify(
            revised, validate_projection_receipt=False
        )
        if verification.issues and not bool(plan.metadata.get("presentation_draft")):
            codes = ", ".join(issue.code for issue in verification.issues)
            raise PlanReviewError(f"人工覆盖未通过 Verifier：{codes}")
        return self._persist_revision(revised)

    def override_subtitle_decision(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_id: str,
        *,
        associated_video_source_path: Optional[Path] = None,
        primary_language: str,
        script: Optional[str],
        region_hint: Optional[str] = None,
        additional_languages: Iterable[str] = (),
        track_title_tokens: Iterable[str] = (),
        flags: Iterable[str] = (),
        unknown_dispositions: Iterable[UnknownTokenDisposition] = (),
        reason: str,
    ) -> RenamePlan:
        """Compatibility entry point for one file-scoped S4 rule."""

        return self.apply_subtitle_override_rule(
            task_id,
            expected_fingerprint,
            selected_entry_id,
            scope=SubtitleOverrideScope.FILE,
            associated_video_source_path=associated_video_source_path,
            primary_language=primary_language,
            script=script,
            region_hint=region_hint,
            additional_languages=additional_languages,
            track_title_tokens=track_title_tokens,
            flags=flags,
            unknown_dispositions=unknown_dispositions,
            reason=reason,
        )

    def upsert_presentation_group(
        self,
        task_id: str,
        expected_fingerprint: str,
        *,
        group_id: str,
        kind: str,
        display_name: str,
        provider_namespace: str,
        provider_entity_type: str,
        provider_id: str,
        provider_title: str,
        provider_year: Optional[int],
        reason: str,
        provider_original_title: Optional[str] = None,
        provider_aliases: Iterable[str] = (),
        provider_poster_url: Optional[str] = None,
        provider_source_url: Optional[str] = None,
        provider_snapshot_at: Optional[str] = None,
        persist_revision: bool = True,
    ) -> RenamePlan:
        """Create or update one confirmed group without executing media."""

        plan = self._current_plan(task_id, expected_fingerprint)
        reason = str(reason).strip()
        group_id = str(group_id).strip()
        display_name = str(display_name).strip()
        if not group_id or not display_name or not reason:
            raise PlanReviewError("呈现分组 ID、显示名称和确认理由均为必填")
        if kind not in {"movie_continuous", "tv_episodes"}:
            raise PlanReviewError("首版只允许电影连续版或 TV 分集版分组")
        try:
            identity = provider_identity_from_metadata(
                namespace=provider_namespace,
                entity_type=provider_entity_type,
                provider_id=provider_id,
                title=provider_title,
                year=provider_year,
                decision_source="manual_review",
                evidence=(reason,),
            )
        except ValueError as exc:
            raise PlanReviewError(str(exc)) from exc
        identity = replace(
            identity,
            original_title=(str(provider_original_title).strip() or None)
            if provider_original_title is not None else None,
            aliases=tuple(str(item).strip() for item in provider_aliases if str(item).strip()),
            poster_url=(str(provider_poster_url).strip() or None)
            if provider_poster_url is not None else None,
            source_url=(str(provider_source_url).strip() or None)
            if provider_source_url is not None else None,
            snapshot_at=(str(provider_snapshot_at).strip() or None)
            if provider_snapshot_at is not None else None,
        )
        existing = [group for group in plan.presentation_groups if group.group_id == group_id]
        preferred = existing[0].preferred_version_ref if existing else None
        replacement_group = PresentationGroup(
            group_id=group_id,
            kind=kind,
            display_name=display_name,
            preferred_version_ref=(preferred if kind == "movie_continuous" else None),
        )
        groups = tuple(
            replacement_group if group.group_id == group_id else group
            for group in plan.presentation_groups
        )
        if not existing:
            groups += (replacement_group,)
        old_primary = (
            plan.work_identity.primary_provider if plan.work_identity else None
        )
        linked = [
            item for item in plan.linked_provider_identities
            if all((item.namespace, item.entity_type, item.provider_id, item.title))
        ]
        if old_primary is not None and all((
            old_primary.namespace, old_primary.entity_type,
            old_primary.provider_id, old_primary.title,
        )) and (
            old_primary.namespace, old_primary.entity_type, old_primary.provider_id
        ) != (identity.namespace, identity.entity_type, identity.provider_id):
            linked.append(replace(old_primary, status="candidate"))
        linked = [item for item in linked if (
            item.namespace, item.entity_type, item.provider_id
        ) != (identity.namespace, identity.entity_type, identity.provider_id)]
        domain, domain_source = derive_work_domain(plan.metadata, identity)
        revised = replace(
            plan,
            presentation_groups=groups,
            work_identity=WorkIdentity(
                status="confirmed",
                domain=domain,
                title=identity.title or "",
                year=identity.year,
                primary_provider=identity,
                domain_source=domain_source,
            ),
            linked_provider_identities=tuple(linked),
            revision_parent_fingerprint=expected_fingerprint,
            revision_action=f"upsert_presentation_group:{group_id}",
            revision_reason=reason,
        )
        if not persist_revision:
            return revised
        authority_linked = [
            item for item in plan.linked_provider_identities
            if item.status == "confirmed"
            and (
                item.namespace, item.entity_type, item.provider_id
            ) != (identity.namespace, identity.entity_type, identity.provider_id)
        ]
        if old_primary is not None and (
            old_primary.namespace, old_primary.entity_type, old_primary.provider_id
        ) != (identity.namespace, identity.entity_type, identity.provider_id):
            authority_linked.append(old_primary)
        assert revised.work_identity is not None
        revised, binding_transform = self._prepare_manual_presentation_binding(revised)
        return self._persist_recognition_revision(
            revised,
            RecognitionDecisionInput(
                task_id=task_id,
                work_identity=revised.work_identity,
                linked_provider_identities=tuple(dict.fromkeys(authority_linked)),
            ),
            state_transform=binding_transform,
        )

    def _prepare_manual_presentation_binding(self, revised, *, preserve_binding=False):
        """Explicit manual layout producer using the receipted inventory root.

        This does not resolve a season or rename media. Mixed presentations keep
        their explicitly chosen inventory work directory and one work binding.
        """
        if preserve_binding:
            current=self.decision_state_store.load(revised.task_id)
            if (current.confirmed_work == (revised.work_identity.to_dict() if revised.work_identity else {})
                    and (current.catalog_link_intent is not None or current.series_binding is not None)):
                return revised,None
        has_tv=any(g.kind=='tv_episodes' for g in revised.presentation_groups)
        if not has_tv:
            if revised.work_identity is not None and revised.work_identity.status=='confirmed':
                key=derive_work_intent_key(primary_provider=revised.work_identity.primary_provider,
                    group_kind='movie_continuous')
                revised=replace(revised,metadata={**revised.metadata,'proposed_work_key':key})
                return revised,lambda state:replace(state,
                    tv_planning_authority=None,series_binding=None,season_binding=None,
                    layout_mode='movie_versions',catalog_link_intent={'work_key':key,'catalog_revision':0})
            return revised, None
        work=revised.work_identity
        if work is None or work.status!='confirmed':
            return revised, None
        root=Path(str(revised.metadata.get('canonical_work_root') or ''))
        policy=revised.execution_policy
        if policy is None or not root.is_absolute() or not root.is_relative_to(policy.target_root) or '..' in root.parts:
            raise PlanReviewError('人工呈现缺少已确认的绝对作品目录')
        key=derive_work_intent_key(primary_provider=work.primary_provider,group_kind='tv_episodes')
        revised=replace(revised,metadata={**revised.metadata,'canonical_work_root':str(root),'proposed_work_key':key})
        def transform(state):
            return replace(state,
                catalog_link_intent=(None if state.series_binding is not None else
                    {'work_key':key,'catalog_revision':0}))
        return revised,transform

    def revise_presentation_group_kind(
        self,
        task_id: str,
        expected_fingerprint: str,
        *,
        group_id: str,
        kind: str,
        reason: str,
    ) -> RenamePlan:
        """Change only local playback layout while preserving work identity."""

        plan = self._current_plan(task_id, expected_fingerprint)
        if kind not in {"movie_continuous", "tv_episodes"}:
            raise PlanReviewError("只允许选择连续电影或分集剧集")
        reason = str(reason).strip()
        if not reason:
            raise PlanReviewError("修改观看形式必须记录理由")
        current = next(
            (group for group in plan.presentation_groups if group.group_id == group_id),
            None,
        )
        if current is None:
            raise PlanReviewError("要修改的观看形式不存在")
        assigned = tuple(
            entry for entry in plan.entries
            if entry.presentation_asset is not None
            and entry.presentation_asset.presentation_group == group_id
            and entry.presentation_asset.asset_role.kind != "unknown"
        )
        if assigned:
            raise PlanReviewError(
                "已有文件完成用途或版本确认；请先撤销这些决定，再修改观看形式"
            )
        replacement = replace(
            current,
            kind=kind,
            display_name=(
                "电影连续版" if kind == "movie_continuous" else "TV 分集版"
            ),
            preferred_version_ref=(
                current.preferred_version_ref if kind == "movie_continuous" else None
            ),
        )
        revised = replace(
            plan,
            presentation_groups=tuple(
                replacement if group.group_id == group_id else group
                for group in plan.presentation_groups
            ),
            revision_parent_fingerprint=expected_fingerprint,
            revision_action=f"revise_presentation_group_kind:{group_id}:{kind}",
            revision_reason=reason,
        )
        revised, transform=self._prepare_manual_presentation_binding(revised)
        return self._persist_manual_presentation_revision(revised, transform)

    def _persist_manual_presentation_revision(self, revised, transform=None):
        """Manual asset decisions advance their frozen operation in one transaction."""
        self._require_legacy_content_task(revised.task_id)
        if transform is None:
            revised, transform = self._prepare_manual_presentation_binding(revised, preserve_binding=True)
        if transform is None:
            return self._persist_revision(revised)
        task_id = revised.task_id
        state=self.decision_state_store.load(task_id)
        candidate=transform(state)
        from .decision_state import DecisionCommandService
        _, projected=DecisionCommandService(self.decision_state_store,self.plan_store).update(
            task_id,expected_revision=state.revision,
            changes={'catalog_link_intent':candidate.catalog_link_intent,
                'tv_planning_authority':candidate.tv_planning_authority,'series_binding':candidate.series_binding,
                'season_binding':candidate.season_binding,'layout_mode':candidate.layout_mode},
            planner=lambda _:revised,task_terminal=self._revision_task_terminal(task_id))
        return self._record_revision(projected)

    @staticmethod
    def movie_selection_for_completion(plan):
        from .movie_version_selection import from_plan
        return from_plan(plan) if plan.metadata.get('movie_version_selection') is not None else None




    def prepare_default_content_initialization(self, task_id, expected_fingerprint):
        from .default_content import prepare
        return prepare(self, task_id, expected_fingerprint)

    def commit_default_content_initialization(self, preview):
        from .default_content import commit
        return commit(self, preview)

    def prepare_content_change(self, task_id, expected_fingerprint, *, selected_entry_ids, action, reason):
        from .content_choice import prepare_change
        return prepare_change(self, task_id, expected_fingerprint,
            selected_entry_ids=selected_entry_ids, action=action, reason=reason)

    def commit_content_change(self, preview):
        from .content_choice import commit
        return commit(self, preview)

    def revise_movie_versions(self, task_id, expected_fingerprint, *, groups, excluded_sources=(),
                              replace_version_ref=None, preferred_source=None):
        """One Decision transaction changes complete groups, labels and exclusion."""
        return self.commit_movie_version_change(self.prepare_movie_version_change(
            task_id, expected_fingerprint, groups=groups, excluded_sources=excluded_sources,
            replace_version_ref=replace_version_ref, preferred_source=preferred_source))

    def prepare_movie_version_change(self, task_id, expected_fingerprint, **kwargs):
        from .movie_content import prepare
        return prepare(self, task_id, expected_fingerprint, **kwargs)

    def commit_movie_version_change(self, intent):
        from .movie_content import commit
        return commit(self, intent)

    def _validate_movie_labels(self, plan):
        from .movie_version_selection import from_plan, validate_catalog_labels
        selection = from_plan(plan)
        value = selection.to_dict()
        key = value['work_key']
        if not self.work_catalog._path(key).exists():
            return
        rows = self.work_catalog.load(key)['versions']
        validate_catalog_labels(selection, rows)

    def complete_inventory_draft(
        self,
        task_id: str,
        expected_fingerprint: str,
        *,
        group_id: str,
        group_kind: str,
        target_root: Path,
        reason: str,
        root_snapshot: Mapping[str, object],
        primary_video_source: Optional[Path] = None,
        primary_video_sources: Optional[Iterable[Path]] = None,
        movie_version_groups=None,
        excluded_movie_sources=(),
        confirmed_segment_sources: Optional[Iterable[Path]] = None,
        work_domain: Optional[str] = None,
        role_decisions: Optional[Mapping[Path, str]] = None,
        season_number: Optional[int] = None,
        season_source: Optional[str] = None,
        series_key: Optional[str] = None,
        season_binding_id: Optional[str] = None,
        series_catalog_revision: int = 0,
        series_registration_authority=None,
        persist_revision: bool = True,
        _source_plan: Optional[RenamePlan] = None,
        _revision_parent_fingerprint: Optional[str] = None,
        _completion_context: Optional[InventoryCompletionContext] = None,
    ) -> RenamePlan:
        """Turn a conservative inventory draft into one reviewable mapping.

        Only deterministic filename evidence is applied.  Files which cannot
        be assigned without guessing retain their blocking ``unknown`` role.
        The whole proposal is persisted as one revision, so old approvals can
        never survive a bulk completion.
        """

        plan = _source_plan or self._current_plan(task_id, expected_fingerprint)
        from .content_selection import require_legacy_writer
        existing_content_state = self.decision_state_store.load_optional(task_id)
        if existing_content_state is not None and existing_content_state.content_selection is not None:
            raise PlanReviewError('新内容计划不能通过旧库存整体完成入口覆盖；请使用统一内容选择')
        require_legacy_writer(plan)
        if plan.metadata.get('work_affiliation') is not None or plan.metadata.get('pending_work_affiliation') is not None:
            raise PlanReviewError('作品归属计划必须使用完整内容确认入口，不能退回旧库存完成')
        if plan.task_id != task_id:
            raise PlanReviewError("用途确认的来源计划不属于当前任务")
        if plan.metadata.get("mapping_source") != "inventory_review_draft":
            raise PlanReviewError("当前整理计划不是可智能完成的库存草案")
        if group_kind not in {"movie_continuous", "tv_episodes"}:
            raise PlanReviewError("只允许选择连续电影或分集剧集")
        reason = str(reason).strip()
        if not reason:
            raise PlanReviewError("智能完成必须记录观看形式确认理由")
        work = plan.work_identity
        if work is None or work.status != "confirmed":
            raise PlanReviewError("请先从候选作品中确认正确作品")
        requested_domain = str(work_domain or "").strip()
        if requested_domain:
            if requested_domain not in {"animation", "general"}:
                raise PlanReviewError("目标媒体库只能选择动画或普通影视")
            if work.domain not in {"unknown", requested_domain}:
                raise PlanReviewError("目标媒体库选择与已经确认的作品领域冲突")
            if work.domain == "unknown":
                work = replace(
                    work,
                    domain=requested_domain,
                    domain_source="manual_library_selection",
                )
        if work.domain == "unknown":
            raise PlanReviewError("请先选择保存到动画媒体库还是普通影视媒体库")
        group = next(
            (item for item in plan.presentation_groups if item.group_id == group_id),
            None,
        )
        if group is None:
            raise PlanReviewError("待完成的观看形式不存在")
        target_root = Path(target_root)
        from .target_routing import (
            inspect_frozen_target_routing,
        )

        routing, routing_problems = inspect_frozen_target_routing(
            root_snapshot=root_snapshot,
            root_config_fingerprint=None,
            domain=work.domain,
            group_kind=group_kind,
            target_root=target_root,
        )
        if routing_problems:
            raise PlanReviewError(routing_problems[0].message)
        if routing is None:
            raise PlanReviewError("整理计划缺少冻结目标配置；请重新生成整理计划")
        snapshot = routing.snapshot

        try:
            raw_naming_decision = plan.metadata.get(
                "canonical_naming_decision"
            )
            naming_decision = (
                read_naming_decision(raw_naming_decision)
                if raw_naming_decision is not None
                else decision_from_work_identity(work)
            )
            generated_work_root = build_canonical_work_root(
                resolved_target_root=target_root,
                decision=naming_decision,
                year=work.year,
            )
        except CanonicalNamingError as exc:
            raise PlanReviewError(str(exc)) from exc
        work = replace(work, title=naming_decision.display_title)
        from . import series_registration
        registration = series_registration_authority
        if group_kind == "tv_episodes" and (series_key or work.primary_provider.entity_type == 'tv'):
            if registration is None:
                if work.primary_provider.namespace == 'tmdb' and work.primary_provider.entity_type == 'tv':
                    manual = series_registration.manual_tv_binding(work, self.series_catalog, generated_work_root)
                    registration = series_registration.SeriesRegistrationAuthority.from_mapping(manual['registration_authority'])
                    series_key, series_catalog_revision = manual['series_key'], manual['catalog_revision']
                else:
                    current_state = self.decision_state_store.load(task_id)
                    registration = series_registration.SeriesRegistrationAuthority.from_mapping(
                        (current_state.series_binding or {}).get('registration_authority'))
                    series_key = series_key or (current_state.series_binding or {}).get('series_key')
                    series_catalog_revision = int((current_state.series_binding or {}).get('catalog_revision', 0))
            if not isinstance(registration, series_registration.SeriesRegistrationAuthority):
                raise PlanReviewError('剧集整理必须使用类型化系列登记权威')
            registration = series_registration.SeriesRegistrationAuthority.from_mapping(registration.to_dict())
            if registration.policy_version == series_registration.AFFILIATION_POLICY:
                raise PlanReviewError('新版作品归属必须使用完整剧集确认入口')
            provider = work.primary_provider
            if (registration.series_key != series_key or provider.namespace != registration.namespace
                or provider.provider_id not in dict(registration.season_membership)
                or registration.namespace == 'bangumi' and dict(registration.season_membership)[provider.provider_id] != season_number):
                raise PlanReviewError('当前作品季度与系列登记权威不一致')
        catalog_match = None
        series_match = None
        try:
            requested_key = plan.metadata.get("catalog_work_key")
            if (
                isinstance(requested_key, str)
                and plan.metadata.get("catalog_link_decision") == "explicit_user"
            ):
                raise PlanReviewError('旧字符串归属不能确认已有作品，请重新生成并使用完整作品归属预览')
            else:
                catalog_match = self.work_catalog.find_exact(
                    work.primary_provider, group_kind=group_kind
                )
            if catalog_match is not None and catalog_match.group_kind != group_kind:
                raise PlanReviewError("既有作品登记的观看形式与当前整理方式不一致")
            if catalog_match is not None:
                from . import work_catalog
                work_catalog.require_matching_work_domain(catalog_match.domain, work.domain)
        except WorkCatalogError as exc:
            raise PlanReviewError(str(exc)) from exc
        try:
            proposed_work_key = (
                derive_work_intent_key(
                    primary_provider=work.primary_provider,
                    group_kind=group_kind,
                )
                if not series_key
                else plan.metadata.get("proposed_work_key")
            )
        except (TypeError, ValueError) as exc:
            raise PlanReviewError(f"无法建立稳定作品意图：{exc}") from exc
        if group_kind == "tv_episodes" and series_key:
            series_path = self.series_catalog.root / f"{series_key}.json"
            if series_path.exists():
                try:
                    series_match = self.series_catalog.load(str(series_key))
                except SeriesCatalogError as exc:
                    raise PlanReviewError(str(exc)) from exc
                series_registration.validate_series_catalog_snapshot(registration, self.series_catalog,
                    int(series_catalog_revision), str(series_match.canonical_root), library_domain=work.domain)
                if series_match.revision != int(series_catalog_revision):
                    raise PlanReviewError(
                        "SeriesCatalog 已变化，请刷新后重新生成整理计划"
                    )
                if not _is_within(series_match.canonical_root, target_root):
                    raise PlanReviewError("SeriesCatalog 规范根越出当前剧集媒体库")
            elif int(series_catalog_revision):
                raise PlanReviewError(
                    "SeriesCatalog 登记已丢失，请先修复作品登记"
                )
            elif registration.materialized_mapping:
                raise PlanReviewError('系列登记证据对应的作品已丢失，请重新生成')
        if series_match is not None:
            work_root = series_match.canonical_root
        elif catalog_match is not None:
            if (
                not _is_within(catalog_match.canonical_root, target_root)
                and plan.metadata.get("catalog_allow_outside_current_root") is not True
            ):
                raise PlanReviewError(
                    "该作品已登记在当前配置根目录之外。请先在作品登记中明确选择"
                    "“继续添加到已登记目录”或完成目录重定位，系统不会另建同名目录"
                )
            work_root = catalog_match.canonical_root
            # The already materialized directory is the naming authority.
            # A later task may select the same work from another provider with
            # a different translated title; never create a second filename
            # family inside the canonical directory.
        else:
            collision_roots = tuple(
                match.canonical_root
                for match in self.work_catalog.suggestions(
                    naming_decision.display_title,
                    work.year,
                    naming_decision.aliases,
                    group_kind=group_kind,
                )
            )
            try:
                work_root = resolve_fresh_collision_root(
                    generated_root=generated_work_root,
                    decision=naming_decision,
                    registered_roots=collision_roots,
                )
            except CanonicalNamingError as exc:
                raise PlanReviewError(str(exc)) from exc
        folder_name = work_root.name
        _routing, routing_problems = inspect_frozen_target_routing(
            root_snapshot=snapshot.to_dict(),
            root_config_fingerprint=snapshot.fingerprint,
            domain=work.domain,
            group_kind=group_kind,
            target_root=target_root,
            canonical_work_root=work_root,
        )
        if routing_problems:
            raise PlanReviewError(routing_problems[0].message)
        videos = tuple(
            entry for entry in plan.entries if entry.media_kind == "video"
        )
        if not videos:
            raise PlanReviewError("当前包没有可识别的视频文件")
        # Episode inference is intentionally invoked only after the shared
        # role pre-classifier has selected main candidates (TV), or for the
        # explicit movie-segment proof below.  It must never decide a role.
        episode_hints = {}
        selected_group = replace(
            group,
            group_id=default_presentation_group_id(group_kind),
            kind=group_kind,
            display_name=(
                "电影连续版" if group_kind == "movie_continuous" else "TV 分集版"
            ),
            preferred_version_ref=None,
            provider_identity=None,
        )
        video_targets = {}
        content_video_targets = {}
        video_assets = {}
        video_identity = {}
        video_roles = {}
        unresolved_videos = []
        episode_assignment_conflicts = []
        supplied_roles = {
            Path(source): str(role).strip()
            for source, role in (role_decisions or {}).items()
        }
        completion_mode = (
            "tv_episodes"
            if group_kind == "tv_episodes"
            else "movie_versions"
            if (
                len(videos) == 1
                or primary_video_source is not None
                or primary_video_sources is not None
            )
            else "movie_segments"
        )
        if group_kind == "tv_episodes":
            stored_season, stored_source = _season_decision_from_metadata(
                plan.metadata
            )
            if season_number is not None:
                try:
                    default_season_id = int(season_number)
                except (TypeError, ValueError) as exc:
                    raise PlanReviewError("季度编号无效，无法生成剧集目标") from exc
                if default_season_id < 0:
                    raise PlanReviewError("季度编号不能为负数")
                resolved_season_source = str(season_source or "explicit_user")
                if resolved_season_source not in _SEASON_SOURCES - {"unset", "legacy_placeholder"}:
                    raise PlanReviewError("季度确认来源无效")
            elif stored_season is not None and stored_source not in {
                "unset", "legacy_placeholder",
            }:
                default_season_id = stored_season
                resolved_season_source = stored_source
            elif (
                plan.metadata.get("season_id") is None
                and plan.metadata.get("season_number") is None
            ):
                # Compatibility for programmatically-created fresh drafts
                # which predate any season field.  Persisted historical M0
                # drafts always wrote season_id=0 and reach the explicit
                # legacy_placeholder error below instead.
                default_season_id = 1
                resolved_season_source = "inferred_path"
            else:
                raise PlanReviewError("请先确认归入第几季；历史 Season 00 占位不能自动沿用")
            from . import tv_video_projection
            mapped = tv_video_projection.project_tv_videos(
                tv_video_projection.TVVideoInput(
                    videos=videos, source_root=plan.source_root, work_title=work.title,
                    group_id=selected_group.group_id, work_root=work_root,
                    season_id=default_season_id, roles=tuple(supplied_roles.items()),
                )
            )
            if type(mapped) is not tv_video_projection.TVVideoProjection:
                raise PlanReviewError('剧集视频映射未返回有效结果')
            video_targets = {row.source: row.target for row in mapped.mappings}
            content_video_targets = {row.source: row.target for row in mapped.mappings if row.is_content}
            video_assets = {row.source: row.asset for row in mapped.mappings}
            video_identity = {row.source: (row.season_id, row.episode_id) for row in mapped.mappings}
            video_roles = {row.source: row.role for row in mapped.mappings}
            unresolved_videos = list(mapped.unresolved)
            episode_assignment_conflicts = list(mapped.episode_conflicts)
        else:
            from . import movie_video_projection
            requested_sources = (
                tuple(Path(p) for p in (primary_video_sources or ()))
                + ((Path(primary_video_source),) if primary_video_source is not None else ())
                if primary_video_source is not None or primary_video_sources is not None else None
            )
            mapped = movie_video_projection.project_movie_videos(
                movie_video_projection.MovieVideoInput(
                    videos=videos, source_root=plan.source_root, work_title=work.title,
                    group_id=selected_group.group_id, work_root=work_root,
                    primary_sources=requested_sources,
                    confirmed_segments=tuple(Path(p) for p in (confirmed_segment_sources or ())),
                    excluded_sources=tuple(Path(p) for p in excluded_movie_sources),
                    roles=tuple(supplied_roles.items()), groups_supplied=movie_version_groups is not None,
                )
            )
            if type(mapped) is not movie_video_projection.MovieVideoProjection:
                raise PlanReviewError('电影视频映射未返回有效结果')
            video_targets = {row.source: row.target for row in mapped.mappings}
            content_video_targets = {row.source: row.target for row in mapped.mappings if row.is_content}
            video_assets = {row.source: row.asset for row in mapped.mappings}
            video_identity = {row.source: (None, None) for row in mapped.mappings}
            video_roles = {row.source: row.role for row in mapped.mappings}
            unresolved_videos = list(mapped.unresolved)

        subtitle_entries = tuple(
            entry for entry in plan.entries if entry.media_kind == "subtitle"
        )
        subtitle_decisions = {
            decision.source_subtitle: decision
            for decision in build_subtitle_decisions(
                (entry.source_path for entry in subtitle_entries),
                tuple(
                    VideoPathMapping(source, target)
                    for source, target in content_video_targets.items()
                ),
            )
        }
        extra_video_targets = {
            source: target
            for source, target in video_targets.items()
            if source not in content_video_targets
        }
        extra_subtitle_decisions = {
            decision.source_subtitle: decision
            for decision in build_subtitle_decisions(
                (entry.source_path for entry in subtitle_entries),
                tuple(
                    VideoPathMapping(source, target)
                    for source, target in extra_video_targets.items()
                ),
            )
        }
        revised_entries = []
        for original in plan.entries:
            source = original.source_path
            if source in video_targets:
                season_id, episode_id = video_identity[source]
                revised_entries.append(replace(
                    original,
                    target_path=video_targets[source],
                    season_id=season_id,
                    episode_id=episode_id,
                    role=video_roles[source],
                    requires_review=True,
                    evidence=original.evidence + (reason, "文件名序列唯一，可生成规范目标",),
                    presentation_asset=video_assets[source],
                ))
                continue
            if original.media_kind == "subtitle":
                decision = subtitle_decisions.get(source)
                extra_decision = extra_subtitle_decisions.get(source)
                if decision is not None and decision.target_preview is not None:
                    revised_entries.append(replace(
                        original,
                        target_path=decision.target_preview,
                        requires_review=decision.blocks_execution,
                        evidence=original.evidence + decision.reason_codes,
                        subtitle=decision_to_plan_contract(decision),
                    ))
                elif (
                    extra_decision is not None
                    and extra_decision.target_preview is not None
                    and not extra_decision.blocks_execution
                ):
                    # Keep the deterministic extra-video association typed.
                    # Source partitioning must be able to close this dependency
                    # without re-guessing from filenames or target placement.
                    extra_identity = video_identity.get(
                        extra_decision.association.associated_video,
                        (None, None),
                    )
                    revised_entries.append(replace(
                        original,
                        target_path=extra_decision.target_preview,
                        season_id=extra_identity[0],
                        episode_id=extra_identity[1],
                        media_kind="subtitle",
                        role="extra_subtitle",
                        requires_review=True,
                        evidence=original.evidence + (
                            "字幕与特典视频同名，作为特典配套资源",
                        ),
                        subtitle=decision_to_plan_contract(extra_decision),
                        presentation_asset=None,
                    ))
                else:
                    # Completion must refresh even an unresolved subtitle.  A
                    # retained inventory contract has empty rule versions and
                    # an obsolete movie-draft target, which creates misleading
                    # stale-rule and target-outside-root errors.
                    try:
                        relative_subtitle = source.relative_to(plan.source_root)
                    except ValueError:
                        relative_subtitle = Path(source.name)
                    refreshed = decision or extra_decision
                    revised_entries.append(replace(
                        original,
                        target_path=(
                            work_root / "ManualReview" / "Subtitles" / relative_subtitle
                        ),
                        requires_review=True,
                        evidence=original.evidence + (
                            "已按本次视频映射刷新字幕规则；仍需人工确认",
                        ),
                        subtitle=(
                            decision_to_plan_contract(refreshed)
                            if refreshed is not None else None
                        ),
                    ))
                continue


        revised_entries, unresolved_collisions = _resolve_asset_collisions(
            tuple(revised_entries), work_root, package_label=plan.source_root.name
        )

        metadata = dict(plan.metadata)
        origin_fingerprint = self.plan_store.fingerprint(plan)
        if _completion_context is None and group_kind == 'tv_episodes':
            # Boundary selection can supply a newly identified, unpersisted
            # draft. Bind its inventory lineage to the actual reviewed parent.
            try:
                origin = self.plan_store.load_version(task_id, _revision_parent_fingerprint or expected_fingerprint)
            except PlanStoreError as exc:
                raise PlanReviewError('找不到经过核验的库存父计划，请刷新后重新选择整理方式') from exc
            if origin.metadata.get('mapping_source') != 'inventory_review_draft':
                origin_context = self._completion_context_for_current(origin)
                origin = self._load_inventory_draft(task_id, origin_context.origin_plan_fingerprint)
            if (origin.source_root != plan.source_root
                    or not origin.metadata.get('inventory_fingerprint')
                    or origin.metadata.get('inventory_fingerprint') != plan.metadata.get('inventory_fingerprint')
                    or not any(g.group_id == group_id for g in origin.presentation_groups)):
                raise PlanReviewError('库存父计划与本次整理来源不一致')
            origin_fingerprint = self.plan_store.fingerprint(origin)
        context = _completion_context or InventoryCompletionContext(
            origin_plan_fingerprint=origin_fingerprint,
            origin_group_id=group_id,
            group_kind=group_kind,
            work_domain=work.domain,
            target_root=target_root,
            root_snapshot=snapshot.to_dict(),
            manual_role_decisions={
                entry_id(entry): InventoryRoleDecision(entry.source_path, supplied_roles.get(entry.source_path, ""))
                for entry in videos
                if supplied_roles.get(entry.source_path)
            },
            season_number=(default_season_id if group_kind == "tv_episodes" else None),
            season_source=(resolved_season_source if group_kind == "tv_episodes" else "unset"),
        )
        from .inventory_completion_projection import project_completion_metadata
        metadata = project_completion_metadata(metadata, context=context, work_root=work_root,
            naming_decision=naming_decision, proposed_work_key=proposed_work_key,
            catalog_work_key=catalog_match.work_key if catalog_match else None,
            catalog_revision=catalog_match.revision if catalog_match else 0,
            catalog_match_source='exact_series_catalog' if series_match else 'exact_provider' if catalog_match else 'new_work',
            completion_mode=completion_mode, unresolved_videos=unresolved_videos,
            unresolved_collisions=unresolved_collisions, episode_assignment_conflicts=episode_assignment_conflicts,
            series_key=series_key, season_binding_id=season_binding_id, series_catalog_revision=series_catalog_revision)
        if not isinstance(metadata, dict):
            raise PlanReviewError('完成上下文唯一投影器已断开')
        policy = plan.execution_policy
        revised = replace(
            plan,
            entries=tuple(revised_entries),
            metadata=metadata,
            work_identity=work,
            presentation_groups=(selected_group,),
            execution_policy=(
                replace(policy, target_root=target_root)
                if policy is not None else None
            ),
            revision_parent_fingerprint=(
                _revision_parent_fingerprint or expected_fingerprint
            ),
            revision_action=f"complete_inventory_draft:{group_kind}",
            revision_reason=reason,
        )
        if group_kind == 'movie_continuous':
            from .movie_version_selection import create_selection, project_selection
            prior = self.decision_state_store.load_optional(task_id)
            record = self.work_catalog.load(catalog_match.work_key) if catalog_match is not None else None
            selection = create_selection(revised, groups=movie_version_groups,
                registered_versions=record['versions'] if record else (),
                excluded_sources=excluded_movie_sources, previous=prior.movie_version_selection if prior else None)
            from .movie_version_selection import validate_catalog_labels
            validate_catalog_labels(selection, record['versions'] if record else ())
            revised = project_selection(revised, selection)
        if not persist_revision:
            return revised
        linked = tuple(
            item for item in plan.linked_provider_identities
            if item.status == "confirmed"
        )
        recognition = RecognitionDecisionInput(
            task_id=task_id,
            work_identity=work,
            linked_provider_identities=linked,
            season_number=(season_number if group_kind == "tv_episodes" else None),
            season_source=str(season_source or "unset"),
            layout_mode=(
                "tv_episodes" if group_kind == "tv_episodes" else "movie_versions"
            ),
        )

        def transform_completion(state):
            changes = {"recovery_state": "healthy",
                       'movie_version_selection':self.movie_selection_for_completion(revised)}
            if group_kind == "tv_episodes":
                if series_key:
                    changes["series_binding"] = {
                        "series_key": str(series_key),
                        "canonical_root": str(metadata.get("canonical_work_root") or ""),
                        "catalog_revision": int(series_catalog_revision),
                        "decision_source": "new_series_intent",
                        "registration_authority": registration.to_dict(),
                    }
                if season_binding_id and season_number is not None:
                    changes["season_binding"] = {
                        "season_binding_id": str(season_binding_id),
                        "season_number": int(season_number),
                        "decision_source": str(season_source or "provider_crosswalk"),
                    }
                work_key=str(metadata.get('catalog_work_key') or metadata.get('proposed_work_key') or '').strip()
                changes["catalog_link_intent"] = (
                    {'work_key':work_key,'catalog_revision':int(metadata.get('catalog_revision') or 0)}
                    if not series_key and work_key else None)
            else:
                work_key = str(
                    metadata.get("catalog_work_key")
                    or metadata.get("proposed_work_key")
                    or ""
                ).strip()
                changes.update({
                    "series_binding": None,
                    "season_binding": None,
                    "catalog_link_intent": (
                        {
                            "work_key": work_key,
                            "catalog_revision": int(metadata.get("catalog_revision") or 0),
                        }
                        if work_key else None
                    ),
                })
            return replace(state, **changes)

        return self._persist_recognition_revision(
            revised,
            recognition,
            state_transform=transform_completion,
        )


    @staticmethod
    def _preclassify_inventory_video(
        *,
        item_path: Path,
        candidate_name: str,
        season_id: int,
        extra_context_name: str,
        forced_role: Optional[str],
    ) -> MediaPreclassification:
        """Apply an auditable human role decision before number inference."""

        if forced_role == "extra":
            return MediaPreclassification(
                PreclassificationKind.CONFIRMED_EXTRA,
                MediaClassification(
                    LocalRole.DISC_EXTRA, season_id, "extra", None, "Manual",
                    "用户在处理页确认该文件是特典",
                ),
            )
        if forced_role == "main_candidate":
            return MediaPreclassification(
                PreclassificationKind.MAIN_CANDIDATE,
                MediaClassification(
                    LocalRole.UNKNOWN, season_id, "normal_candidate", None,
                    "Manual", "用户在处理页确认该文件参加正片编号",
                ),
            )
        if forced_role == 'needs_correspondence':
            return MediaPreclassification(PreclassificationKind.UNKNOWN,
                MediaClassification(LocalRole.UNKNOWN, season_id, 'unknown', None, 'Manual',
                                    '作品已更正，请重新确认此视频的集号或特别篇对应'))
        if forced_role not in {None, ""}:
            raise PlanReviewError("文件用途只能设为正片候选或特典")
        return preclassify_media(
            item_path, candidate_name, season_id, extra_context_name
        )

    def _prepare_replay_draft(self, current: RenamePlan, context: InventoryCompletionContext) -> RenamePlan:
        """Keep origin classification inputs but never expand current ownership."""
        draft = self._load_inventory_draft(current.task_id, context.origin_plan_fingerprint)
        owned = {_path_key(path) for path in current.expected_sources}
        if not owned.issubset({_path_key(path) for path in draft.expected_sources}):
            raise PlanReviewError("当前来源与原始库存不一致，不能重建")
        metadata = dict(draft.metadata)
        for key in (
            "canonical_naming_decision", "provider_special_observation",
            "provider_special_observation_required", "provider_special_candidates",
            "provider_special_eligibility_unavailable", "provider_special_decisions",
            "provider_regular_episode_overflows", "source_partition_decisions",
            "source_partition_fingerprint",
        ):
            metadata.pop(key, None)
            if key in current.metadata:
                metadata[key] = current.metadata[key]
        return replace(
            draft, metadata=metadata, work_identity=current.work_identity,
            execution_policy=current.execution_policy,
            entries=tuple(e for e in draft.entries if _path_key(e.source_path) in owned),
            expected_sources=tuple(p for p in draft.expected_sources if _path_key(p) in owned),
            ignored_sources=tuple(p for p in draft.ignored_sources if _path_key(p) in owned),
        )

    def retry_tv_inventory(self, observation: RenamePlan, recognition: RecognitionDecisionInput):
        """Rebuild a previously completed TV inventory, not the shallow rule map.

        History supplies classification inputs only after a fresh bounded scan
        verifies their identity. The caller commits the result exactly once.
        """
        state = self.decision_state_store.load_optional(observation.task_id)
        if state is None or state.layout_mode != 'tv_episodes' or recognition.layout_mode != 'tv_episodes':
            return None
        current = self.plan_store.load(observation.task_id)
        if current.metadata.get('mapping_source') != 'inventory_smart_completion':
            return None
        candidate = recognition.candidate(state)
        old = state.confirmed_work.get('primary_provider') or {}
        new = candidate.confirmed_work.get('primary_provider') or {}
        fields = ('namespace', 'entity_type', 'provider_id')
        if any(old.get(key) != new.get(key) for key in fields):
            return None
        context = self._completion_context_for_current(current)
        if (context.group_kind != 'tv_episodes'
                or current.work_identity.domain != recognition.work_identity.domain
                or context.season_number != recognition.season_number
                or current.source_root != observation.source_root):
            raise PlanReviewError('原整理范围或季度已变化，请重新确认整理方式')
        draft = self._prepare_replay_draft(current, context)
        from .movie_assets import inventory_movie_assets
        inventory = inventory_movie_assets(observation.source_root)
        if inventory.fingerprint != draft.metadata.get('inventory_fingerprint'):
            raise PlanReviewError('源文件清单已变化，不能沿用旧附加内容选择，请重新核查来源')
        from .projection_authority import InventoryReplayGuard, SourceIdentity
        guard = InventoryReplayGuard(self.decision_state_store.fingerprint(state), self.plan_store.fingerprint(current),
            observation.source_root, inventory.fingerprint,
            tuple(SourceIdentity.from_path(p, inventory_fingerprint=inventory.fingerprint) for p in draft.expected_sources))
        from .target_routing import TargetRootSnapshot, resolve_target_root
        snapshot = TargetRootSnapshot.from_dict(observation.metadata['target_root_snapshot'])
        target = resolve_target_root(recognition.work_identity.domain, 'tv_episodes', snapshot)
        metadata = dict(draft.metadata)
        # Provider observations belong to this retry, never to historical output.
        for key in ('canonical_naming_decision', 'provider_special_observation',
                    'provider_special_candidates', 'provider_special_eligibility_unavailable',
                    'provider_regular_episode_overflows'):
            metadata.pop(key, None)
            if key in observation.metadata:
                metadata[key] = observation.metadata[key]
        draft = replace(draft, metadata=metadata, projection_receipt=None, work_identity=recognition.work_identity,
            linked_provider_identities=recognition.linked_provider_identities,
            execution_policy=observation.execution_policy)
        revised = self.complete_inventory_draft(observation.task_id, self.plan_store.fingerprint(current),
            group_id=context.origin_group_id, group_kind='tv_episodes', target_root=target,
            root_snapshot=snapshot.to_dict(), work_domain=recognition.work_identity.domain,
            season_number=recognition.season_number, season_source=recognition.season_source,
            role_decisions={v.source_path: v.role for v in context.manual_role_decisions.values()},
            reason='重试：按已核验库存重新生成完整季度计划',
            _source_plan=draft, _completion_context=replace(context, root_snapshot=snapshot.to_dict(), target_root=target),
            persist_revision=False)
        return replace(revised, metadata={**revised.metadata, 'inventory_retry_guard_required': True}), guard

    def retry_movie_inventory(self, draft, recognition):
        """Replay explicit choices on a fresh scan, never re-enable an old Plan."""
        state = self.decision_state_store.load_optional(draft.task_id)
        if state is None or recognition.candidate(state).movie_version_selection is None:
            return None
        current = self.plan_store.load(draft.task_id)
        context = self._completion_context_for_current(current)
        original = self._prepare_replay_draft(current, context)
        if (context.group_kind != 'movie_continuous'
                or original.source_root != draft.source_root
                or original.metadata.get('inventory_fingerprint') != draft.metadata.get('inventory_fingerprint')
                or current.work_identity.domain != recognition.work_identity.domain):
            return None
        from .target_routing import TargetRootSnapshot, resolve_target_root
        from .movie_version_selection import project_selection, rebind_selection
        snapshot = TargetRootSnapshot.from_dict(draft.metadata['draft_root_snapshot'])
        target = resolve_target_root(recognition.work_identity.domain, context.group_kind, snapshot)
        draft = replace(draft, work_identity=recognition.work_identity,
                        linked_provider_identities=recognition.linked_provider_identities)
        replay_context = replace(context, root_snapshot=snapshot.to_dict(), target_root=target)
        revised = self.complete_inventory_draft(draft.task_id, self.plan_store.fingerprint(current),
            group_id=context.origin_group_id, group_kind=context.group_kind, target_root=target,
            root_snapshot=snapshot.to_dict(), work_domain=recognition.work_identity.domain,
            reason='重试：按新扫描及当前配置重新生成电影计划',
            role_decisions={v.source_path: v.role for v in context.manual_role_decisions.values()},
            _source_plan=draft, _completion_context=replay_context, persist_revision=False,
            **self._movie_replay_arguments(current, state=state))
        selection = state.movie_version_selection
        key = str(revised.metadata.get('catalog_work_key') or revised.metadata.get('proposed_work_key') or '')
        if (selection.to_dict()['work_key'] != key
                or current.metadata.get('canonical_work_root') != revised.metadata.get('canonical_work_root')):
            selection = rebind_selection(selection, key)
        preferred = {g.group_id: g.preferred_version_ref for g in current.presentation_groups}
        revised = project_selection(replace(revised, presentation_groups=tuple(
            replace(g, preferred_version_ref=preferred.get(g.group_id)) for g in revised.presentation_groups)), selection)
        return revised

    def recomplete_inventory_with_role_decisions(
        self,
        task_id: str,
        expected_current_fingerprint: str,
        *,
        decisions: Mapping[str, Optional[str]],
        reason: str,
        persist_revision: bool = True,
    ) -> RenamePlan:
        """Atomically replay a completed inventory plan with user role choices.

        The source inventory draft is read from history only.  No intermediate
        draft revision is saved, so an exception leaves the current plan and
        its approval state untouched.
        """

        current = self._current_plan(task_id, expected_current_fingerprint)
        from .content_selection import require_legacy_writer
        require_legacy_writer(current)
        if current.metadata.get("mapping_source") != "inventory_smart_completion":
            raise PlanReviewError("当前整理计划尚未选择整理方式")
        reason = str(reason).strip()
        if not reason:
            raise PlanReviewError("确认文件用途必须记录原因")
        context = self._completion_context_for_current(current)
        draft = self._prepare_replay_draft(current, context)
        group = next(
            (item for item in draft.presentation_groups
             if item.group_id == context.origin_group_id),
            None,
        )
        if group is None:
            raise PlanReviewError("原始整理方式已变化，请重新选择整理方式")
        entries_by_id = {entry_id(entry): entry for entry in draft.entries}
        from . import special_decision_projection
        transferred = special_decision_projection._transferred_ids(current)
        merged = {key: value for key, value in context.manual_role_decisions.items()
                  if key not in transferred}
        special_decisions = current.metadata.get("provider_special_decisions", {})
        state = self.decision_state_store.load(task_id)
        movie_selection = state.movie_version_selection if context.group_kind == 'movie_continuous' else None
        movie_reserved = set()
        if movie_selection is not None:
            value = movie_selection.to_dict()
            movie_reserved = {Path(p) for g in value['groups'] for p in g['sources']} | {
                Path(p) for p in value['excluded_sources']}
        for identifier, role_value in decisions.items():
            entry = entries_by_id.get(str(identifier))
            if entry is None or entry.media_kind != "video":
                raise PlanReviewError("待确认文件已变化，请刷新后重试")
            if identifier in special_decisions:
                raise PlanReviewError("该文件已有特别篇或附加内容决定，请先在异常分集归属中修改决定")
            if entry.source_path in movie_reserved:
                raise PlanReviewError("该视频已选为电影正片或明确排除，请先编辑电影版本标签与保留范围")
            if role_value is None or not str(role_value).strip():
                merged.pop(str(identifier), None)
                continue
            role = str(role_value).strip()
            if role not in {"extra", "main_candidate"}:
                raise PlanReviewError("文件用途只能设为正片候选或特典")
            merged[str(identifier)] = InventoryRoleDecision(entry.source_path, role)
        for identifier, decision in merged.items():
            entry = entries_by_id.get(identifier)
            if entry is None or entry.source_path != decision.source_path:
                raise PlanReviewError("人工用途对应的源文件已变化，请重新选择整理方式")
        role_decisions = {
            decision.source_path: decision.role for decision in merged.values()
        }
        replay_context = replace(context, manual_role_decisions=merged)
        movie_kwargs = self._movie_replay_arguments(current, state=state)
        revised = self.complete_inventory_draft(
            task_id,
            expected_current_fingerprint,
            group_id=context.origin_group_id,
            group_kind=context.group_kind,
            target_root=context.target_root,
            reason=reason,
            root_snapshot=context.root_snapshot,
            work_domain=context.work_domain,
            role_decisions=role_decisions,
            season_number=context.season_number,
            season_source=context.season_source,
            _source_plan=draft,
            _revision_parent_fingerprint=expected_current_fingerprint,
            _completion_context=replay_context,
            **movie_kwargs,
            persist_revision=False,
        )
        if movie_selection is not None:
            # Roles change only unselected assets, never the frozen movie choice.
            from .movie_version_selection import project_selection
            preferred = {g.group_id: g.preferred_version_ref for g in current.presentation_groups}
            revised = project_selection(replace(revised, presentation_groups=tuple(
                replace(g, preferred_version_ref=preferred.get(g.group_id)) for g in revised.presentation_groups)), movie_selection)
        revised = special_decision_projection.project_special_decisions(revised, special_decisions)
        if not persist_revision:
            return revised
        from .decision_state import DecisionCommandService
        _state, projected = DecisionCommandService(self.decision_state_store, self.plan_store).update(
            task_id, expected_revision=state.revision,
            changes={"manual_role_decisions": {key: value.role for key, value in merged.items()}},
            planner=lambda _state: revised, task_terminal=self._revision_task_terminal(task_id),
        )
        return self._record_revision(projected)

    def replan_for_catalog_drift(
        self, decision: object, current: RenamePlan
    ) -> RenamePlan:
        """Replay the inventory planner against the current catalog authority.

        This is deliberately not a metadata-only rebase.  The original
        inventory draft and its manual role decisions are replayed so a
        WorkCatalog -> SeriesCatalog transition recalculates every target.
        Persistence and DecisionState projection remain the caller's job.
        """

        from .catalog_binding import binding_from_decision

        if current.metadata.get('work_affiliation') is not None:
            raise PlanReviewError('当前作品归属需要通过完整作品确认重新生成，不能使用旧登记重规划')

        group_kinds = {group.kind for group in current.presentation_groups}
        if group_kinds != {"tv_episodes"}:
            raise PlanReviewError(
                "当前仅允许剧集登记身份漂移触发自动重规划；"
                "电影登记变化必须人工重新处理并批准"
            )
        if current.metadata.get("mapping_source") != "inventory_smart_completion":
            raise PlanReviewError("登记身份变化，但当前计划缺少可重放的库存规划上下文")
        context = self._completion_context_for_current(current)
        binding = binding_from_decision(decision)
        if binding is None:
            raise PlanReviewError("当前 DecisionState 缺少可执行的作品登记身份")
        draft = self._prepare_replay_draft(current, context)
        owned = {_path_key(p) for p in current.expected_sources}
        role_decisions = {
            item.source_path: item.role
            for item in context.manual_role_decisions.values()
            if _path_key(item.source_path) in owned
        }
        completion_kwargs: dict[str, object] = {}
        if binding.backend == "series":
            from .series_registration import SeriesRegistrationAuthority
            series = getattr(decision, "series_binding", None) or {}
            if not str(series.get("canonical_root") or "").strip():
                raise PlanReviewError("SeriesIdentity 缺少规范目录，不能安全重规划")
            season = getattr(decision, "season_binding", None) or {}
            season_number = season.get("season_number", context.season_number)
            if season_number is None:
                raise PlanReviewError("SeriesIdentity 重规划缺少已确认季度")
            completion_kwargs.update({
                "series_key": binding.key,
                "series_registration_authority": SeriesRegistrationAuthority.from_mapping(series.get('registration_authority')),
                "series_catalog_revision": binding.revision,
                "season_number": int(season_number),
                "season_source": str(
                    season.get("decision_source") or context.season_source
                ),
                "season_binding_id": str(
                    season.get("season_binding_id")
                    or current.metadata.get("season_binding_id")
                    or ""
                ) or None,
            })
        else:
            raise PlanReviewError('旧作品登记需经完整作品归属确认，不能由重规划补写人工关联')
        revised = self.complete_inventory_draft(
            current.task_id,
            self.plan_store.fingerprint(current),
            group_id=context.origin_group_id,
            group_kind=context.group_kind,
            target_root=context.target_root,
            reason="执行前发现作品登记身份变化，按当前 DecisionState 重新规划",
            root_snapshot=context.root_snapshot,
            work_domain=context.work_domain,
            role_decisions=role_decisions,
            _source_plan=draft,
            _revision_parent_fingerprint=self.plan_store.fingerprint(current),
            _completion_context=context,
            persist_revision=False,
            **completion_kwargs,
        )
        from . import special_decision_projection
        return special_decision_projection.project_special_decisions(
            revised, decision.provider_special_decisions,
        )

    def _movie_replay_arguments(self, current, *, state=None):
        if not any(g.kind == 'movie_continuous' for g in current.presentation_groups):
            return {}
        state = state if state is not None else self.decision_state_store.load(current.task_id)
        if state.movie_version_selection is None:
            raise PlanReviewError('旧电影缺少完整版本选择，请重新生成')
        value = state.movie_version_selection.to_dict()
        return dict(primary_video_sources=tuple(Path(p) for row in value['groups'] for p in row['sources']),
                    movie_version_groups=[dict(sources=row['sources'], label=row['label']) for row in value['groups']],
                    excluded_movie_sources=value['excluded_sources'])

    def _completion_context_for_current(
        self, current: RenamePlan
    ) -> InventoryCompletionContext:
        raw = current.metadata.get("inventory_completion_context")
        if raw is not None:
            context = InventoryCompletionContext.from_metadata(raw)
            # Older boundary completion referenced an unpersisted intermediate
            # draft. Only an authenticated ancestry link with the same inventory
            # can repair that pointer; corrupt existing history is never ignored.
            origin = self.plan_store._history_path(current.task_id, context.origin_plan_fingerprint)
            if context.group_kind == 'tv_episodes' and not origin.exists():
                draft = self._find_inventory_draft(current)
                if (draft.source_root != current.source_root
                        or not draft.metadata.get('inventory_fingerprint')
                        or draft.metadata.get('inventory_fingerprint') != current.metadata.get('inventory_fingerprint')
                        or not any(g.group_id == context.origin_group_id for g in draft.presentation_groups)):
                    raise PlanReviewError('库存历史与当前计划不一致，不能恢复整理选择')
                context = replace(context, origin_plan_fingerprint=self.plan_store.fingerprint(draft))
            return context
        draft = self._find_inventory_draft(current)
        candidates = [
            group for group in draft.presentation_groups
            if group.kind == current.presentation_groups[0].kind
        ] if current.presentation_groups else []
        if len(candidates) != 1:
            candidates = list(draft.presentation_groups) if len(draft.presentation_groups) == 1 else []
        if len(candidates) != 1 or not current.presentation_groups:
            raise PlanReviewError("原始草案存在多个观看形式，请重新选择整理方式")
        group_kind = current.presentation_groups[0].kind
        target_root = str(current.metadata.get("target_root") or "").strip()
        work = current.work_identity
        if not target_root or work is None or work.domain not in {"animation", "general"}:
            raise PlanReviewError("当前整理计划缺少冻结的保存位置，请重新选择整理方式")
        return InventoryCompletionContext(
            origin_plan_fingerprint=self.plan_store.fingerprint(draft),
            origin_group_id=candidates[0].group_id,
            group_kind=group_kind,
            work_domain=work.domain,
            target_root=Path(target_root),
            root_snapshot=dict(current.metadata.get("target_root_snapshot") or {}),
            manual_role_decisions={},
            season_number=_season_decision_from_metadata(current.metadata)[0],
            season_source=_season_decision_from_metadata(current.metadata)[1],
        )

    def _load_inventory_draft(self, task_id: str, fingerprint: str) -> RenamePlan:
        try:
            draft = self.plan_store.load_version(task_id, fingerprint)
        except PlanStoreError as exc:
            raise PlanReviewError("找不到原始库存草案，请重新选择整理方式") from exc
        if draft.metadata.get("mapping_source") != "inventory_review_draft":
            raise PlanReviewError("原始库存草案无效，请重新选择整理方式")
        return draft

    def _find_inventory_draft(self, current: RenamePlan) -> RenamePlan:
        cursor = current
        visited = {self.plan_store.fingerprint(current)}
        while cursor.revision_parent_fingerprint:
            fingerprint = cursor.revision_parent_fingerprint
            if fingerprint in visited:
                raise PlanReviewError("整理计划历史形成循环，无法安全恢复")
            visited.add(fingerprint)
            try:
                parent = self.plan_store.load_version(current.task_id, fingerprint)
            except PlanStoreError as exc:
                raise PlanReviewError("找不到原始库存草案，无法重新选择整理方式") from exc
            if parent.metadata.get("mapping_source") == "inventory_review_draft":
                return parent
            cursor = parent
        raise PlanReviewError("找不到已确认作品的原始库存草案")

    def reopen_inventory_draft(
        self,
        task_id: str,
        expected_fingerprint: str,
        *,
        reason: str,
        persist_revision: bool = True,
    ) -> RenamePlan:
        """Restore the latest confirmed inventory draft from Plan history.

        Smart completion is intentionally reversible before execution.  The
        historical draft is copied into a new revision whose parent is the
        current completed Plan, so neither the completed result nor its audit
        chain is overwritten.  As with every revision, old approval is reset.
        """

        current = self._current_plan(task_id, expected_fingerprint)
        if current.metadata.get("mapping_source") != "inventory_smart_completion":
            raise PlanReviewError("当前整理计划尚未选择整理方式")
        reason = str(reason).strip()
        if not reason:
            raise PlanReviewError("重新选择整理方式必须记录原因")

        draft = self._find_inventory_draft(current)
        if draft.work_identity is None or draft.work_identity.status != "confirmed":
            raise PlanReviewError("找不到已确认作品的原始库存草案")

        revised = replace(
            draft,
            revision_parent_fingerprint=expected_fingerprint,
            revision_action="reopen_inventory_draft",
            revision_reason=reason,
        )
        if persist_revision and current.metadata.get('movie_version_selection') is not None:
            from .decision_state import DecisionCommandService
            state = self.decision_state_store.load(task_id)
            _, projected = DecisionCommandService(self.decision_state_store, self.plan_store).update(task_id,
                expected_revision=state.revision, changes={'movie_version_selection': None, 'asset_operation': None,
                    'layout_mode': None}, planner=lambda _: revised,
                task_terminal=self._revision_task_terminal(task_id))
            return self._record_revision(projected)
        return self._persist_revision(revised) if persist_revision else revised

    def link_existing_catalog_work(
        self,
        task_id: str,
        expected_fingerprint: str,
        *,
        work_key: str,
        reason: str,
    ) -> RenamePlan:
        """Boundary adapter: both backends use their complete locked planners."""
        from .work_affiliation import prepare_existing, commit_choice
        try:
            return commit_choice(self, prepare_existing(self, task_id, expected_fingerprint,
                key=work_key, reason=reason))
        except ValueError as exc:
            raise PlanReviewError(str(exc)) from exc

    def rebase_movie_plan_to_current_catalog(
        self,
        task_id: str,
        expected_fingerprint: str,
        *,
        reason: str,
    ) -> RenamePlan:
        """Refresh a stale approved catalog Plan without executing file operations.

        A second movie-version task may have been approved before the first task
        materialized its WorkCatalog record and shared assets.  The old approval
        must not authorize decisions based on those newly appeared files.  This
        method only creates a new revision, freezes fresh collision evidence and
        clears the old approval through ``_persist_revision``.
        """

        plan = self._current_plan(task_id, expected_fingerprint)
        if {group.kind for group in plan.presentation_groups} not in (
            {"movie_continuous"}, {"tv_episodes"},
        ):
            raise PlanReviewError("只有单一电影或单一剧集整理计划可以按作品登记重基")
        work = plan.work_identity
        if work is None or work.status != "confirmed":
            raise PlanReviewError("整理计划缺少已确认作品身份，不能自动重基")
        reason = str(reason).strip()
        if not reason:
            raise PlanReviewError("整理计划重基必须记录理由")

        match = None
        requested_key = plan.metadata.get("catalog_work_key")
        try:
            if isinstance(requested_key, str) and requested_key:
                match = self.work_catalog.get_match(requested_key)
            else:
                group_kind = next(iter({group.kind for group in plan.presentation_groups}))
                match = self.work_catalog.find_exact(
                    work.primary_provider, group_kind=group_kind
                )
                proposed_key = plan.metadata.get("proposed_work_key")
                if (
                    match is None
                    and isinstance(proposed_key, str)
                    and (self.work_catalog.root / f"{proposed_key}.json").exists()
                ):
                    match = self.work_catalog.get_match(proposed_key)
            if match is not None:
                from . import work_catalog
                work_catalog.require_matching_work_domain(match.domain, work.domain)
        except WorkCatalogError as exc:
            raise PlanReviewError(str(exc)) from exc

        metadata = dict(plan.metadata)
        old_root_value = str(metadata.get("canonical_work_root") or "").strip()
        if not old_root_value:
            raise PlanReviewError("整理计划缺少原规范作品目录，不能安全重基")
        old_root = Path(old_root_value)
        new_root = match.canonical_root if match is not None else old_root

        rebased_entries = []
        for entry in plan.entries:
            target = _rebase_movie_target(entry.target_path, old_root, new_root)
            rebased_entries.append(replace(
                entry,
                target_path=target,
                collision_resolution=None,
                evidence=entry.evidence + (reason,),
            ))
        resolved_entries, unresolved = _resolve_asset_collisions(
            tuple(rebased_entries), new_root, package_label=plan.source_root.name
        )
        metadata.update({
            "canonical_work_root": str(new_root),
            "catalog_work_key": match.work_key if match is not None else None,
            "catalog_revision": match.revision if match is not None else 0,
            "catalog_match_source": (
                "execution_rebase" if match is not None else "target_drift_rebase"
            ),
            "asset_collision_unresolved": tuple(unresolved),
        })
        revised = replace(
            plan,
            entries=tuple(resolved_entries),
            metadata=metadata,
            revision_parent_fingerprint=expected_fingerprint,
            revision_action="rebase_movie_plan_to_current_catalog",
            revision_reason=reason,
        )
        return self._persist_revision(revised)

    def revise_presentation_asset(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_id: str,
        *,
        presentation_group: str,
        role_kind: str,
        role_subtype: Optional[str] = None,
        owner_version_ref: Optional[str] = None,
        edition_key: str = "",
        edition_label: Optional[str] = None,
        resolution: str = "",
        dynamic_range: str = "",
        key_audio: str = "",
        preferred: bool = False,
        reason: str,
    ) -> RenamePlan:
        """Persist one structured asset decision as a new, unapproved revision."""

        plan = self._current_plan(task_id, expected_fingerprint)
        reason = str(reason).strip()
        if not reason:
            raise PlanReviewError("呈现资产人工决定必须填写理由")
        matching = [
            (index, entry) for index, entry in enumerate(plan.entries)
            if entry_id(entry) == selected_entry_id
        ]
        if len(matching) != 1:
            raise PlanReviewError("呈现资产条目不存在或不唯一")
        index, original = matching[0]
        if original.media_kind == "subtitle":
            raise PlanReviewError("字幕必须通过关联视频继承呈现分组")
        groups = {group.group_id: group for group in plan.presentation_groups}
        group = groups.get(str(presentation_group))
        if group is None:
            raise PlanReviewError("所选呈现分组不存在；请先建立并确认分组")
        if group.kind == 'movie_continuous' and role_kind == 'content_version':
            from .movie_version_selection import from_plan
            selection = from_plan(plan).to_dict() if plan.metadata.get('movie_version_selection') is not None else {
                'groups': [], 'excluded_sources': []}
            if plan.metadata.get('content_selection') is not None:
                from .content_selection import from_plan as content_from_plan
                selection['groups'] = content_from_plan(plan).to_dict()['version_groups']
            chosen = next((row for row in selection['groups'] if str(original.source_path) in row['sources']), None)
            if chosen is None:
                # This is an explicit user decision over a receipted inventory,
                # not a reader upgrading an old Plan. Existing complete groups
                # remain frozen; only this unassigned video starts a new group.
                if original.presentation_asset is not None and original.presentation_asset.segment is not None:
                    raise PlanReviewError('分段电影请在版本列表中明确选择完整版本组')
                chosen = dict(sources=[str(original.source_path)], label='默认版本')
                selection['groups'].append(chosen)
            values = tuple(str(value or '').strip() for value in
                           (edition_key, resolution, dynamic_range, key_audio))
            if any(values) and not all(values):
                raise PlanReviewError('非默认版本必须完整填写 edition key、分辨率、动态范围和关键音频')
            descriptor = (build_manual_version_descriptor(
                presentation_group=group.group_id, group_kind=group.kind,
                season_id=None, episode_id=None, edition_key=values[0],
                edition_label=edition_label, resolution=values[1], dynamic_range=values[2],
                key_audio=values[3], reason=reason) if all(values) else None)
            label = ' '.join(str(part).strip() for part in (edition_label, resolution, dynamic_range, key_audio) if str(part or '').strip())
            return self.revise_movie_versions(task_id, expected_fingerprint,
                groups=[dict(sources=row['sources'], label=label if row is chosen and label else row['label'],
                    **({'technical_descriptor': descriptor.to_dict()} if row is chosen and descriptor is not None else {}))
                    for row in selection['groups']],
                excluded_sources=selection['excluded_sources'],
                preferred_source=original.source_path if preferred else None)
        if role_kind not in {"content_version", "extra", "associated", "unknown"}:
            raise PlanReviewError("不支持的呈现资产角色")
        if role_kind == "extra" and role_subtype not in {
            "trailer", "featurette", "interview", "behind_the_scenes",
            "deleted_scene", "scene", "short", "clip", "sample", "other",
        }:
            raise PlanReviewError("特典必须选择受支持的子类型")
        if role_kind != "extra":
            role_subtype = None

        owner_version_ref = str(owner_version_ref or "").strip() or None
        declared_refs = {
            asset.version_descriptor.version_ref
            for entry in plan.entries
            if (asset := entry.presentation_asset) is not None
            and asset.version_descriptor is not None
            and asset.version_descriptor.version_ref is not None
        }
        if role_kind in {"extra", "associated"} and group.kind == "movie_continuous":
            if owner_version_ref not in declared_refs | {"shared"}:
                raise PlanReviewError("电影特典或配套资源必须选择所属版本或所有版本通用")
        else:
            owner_version_ref = None

        descriptor = None
        target_path = original.target_path
        if role_kind == "content_version":
            values = (
                str(edition_key or "").strip(),
                str(resolution or "").strip(),
                str(dynamic_range or "").strip(),
                str(key_audio or "").strip(),
            )
            if not any(values):
                descriptor = build_default_version_descriptor(
                    group.group_id,
                    original.season_id if group.kind == "tv_episodes" else None,
                    original.episode_id if group.kind == "tv_episodes" else None,
                )
            elif not all(values):
                raise PlanReviewError(
                    "非默认版本必须完整填写 edition key、分辨率、动态范围和关键音频"
                )
            else:
                try:
                    descriptor = build_manual_version_descriptor(
                        presentation_group=group.group_id,
                        group_kind=group.kind,
                        season_id=original.season_id,
                        episode_id=original.episode_id,
                        edition_key=values[0],
                        edition_label=edition_label,
                        resolution=values[1],
                        dynamic_range=values[2],
                        key_audio=values[3],
                        reason=reason,
                        previous=(
                            original.presentation_asset.version_descriptor
                            if original.presentation_asset is not None else None
                        ),
                    )
                except ValueError as exc:
                    raise PlanReviewError(str(exc)) from exc
            if group.kind == "movie_continuous":
                base = original.target_path.parent.name
                label = descriptor.display_label
                target_path = original.target_path.with_name(
                    f"{base}{' - ' + label if label else ''}"
                    f"{original.target_path.suffix}"
                )
        asset = PresentationAssetPlanContract(
            presentation_group=group.group_id,
            asset_role=AssetRole(role_kind, role_subtype),
            segment=None,
            version_descriptor=descriptor,
            owner_version_ref=owner_version_ref,
        )
        entries = list(plan.entries)
        entries[index] = replace(
            original,
            target_path=target_path,
            presentation_asset=asset,
            requires_review=True,
            evidence=original.evidence + (reason,),
        )
        revised_groups = []
        for item in plan.presentation_groups:
            new_preferred = item.preferred_version_ref
            if item.group_id == group.group_id and preferred:
                if item.kind != "movie_continuous" or descriptor is None:
                    raise PlanReviewError("只有电影内容版本可以设为偏好版本")
                new_preferred = descriptor.version_ref
            elif (
                item.preferred_version_ref is not None
                and item.preferred_version_ref
                == getattr(
                    getattr(original.presentation_asset, "version_descriptor", None),
                    "version_ref",
                    None,
                )
            ):
                new_preferred = None
            revised_groups.append(replace(item, preferred_version_ref=new_preferred))
        metadata = dict(plan.metadata)
        resolved_entries, unresolved_collisions = _resolve_asset_collisions(
            tuple(entries),
            Path(str(metadata.get("canonical_work_root") or _target_root(plan))),
            package_label=plan.source_root.name,
        )
        metadata["asset_collision_unresolved"] = tuple(unresolved_collisions)
        revised = replace(
            plan,
            entries=tuple(resolved_entries),
            metadata=metadata,
            presentation_groups=tuple(revised_groups),
            revision_parent_fingerprint=expected_fingerprint,
            revision_action=f"revise_presentation_asset:{selected_entry_id}",
            revision_reason=reason,
        )
        return self._persist_manual_presentation_revision(revised)

    def ignore_plan_entry(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_id: str,
        *,
        reason: str,
    ) -> RenamePlan:
        """Explicitly exclude one source from this plan without changing it."""

        plan = self._current_plan(task_id, expected_fingerprint)
        reason = str(reason).strip()
        if not reason:
            raise PlanReviewError("不整理文件必须填写理由")
        if plan.metadata.get('content_selection') is not None:
            return self.commit_content_change(self.prepare_content_change(task_id, expected_fingerprint,
                selected_entry_ids=(selected_entry_id,), action='exclude', reason=reason))
        matching = [
            entry for entry in plan.entries if entry_id(entry) == selected_entry_id
        ]
        if len(matching) != 1:
            raise PlanReviewError("待忽略条目不存在或不唯一")
        selected = matching[0]
        if plan.metadata.get('movie_version_selection') is not None:
            from .movie_version_selection import from_plan
            value = from_plan(plan).to_dict()
            omitted = next((row for row in value['groups'] if str(selected.source_path) in row['sources']), None)
            groups = [dict(sources=row['sources'], label=row['label']) for row in value['groups'] if row is not omitted]
            if not groups:
                raise PlanReviewError('至少保留一个完整电影版本；不整理任何版本请安全放弃任务')
            excluded = set(value['excluded_sources']) | set(omitted['sources'] if omitted else [str(selected.source_path)])
            return self.revise_movie_versions(task_id, expected_fingerprint, groups=groups, excluded_sources=sorted(excluded))
        remaining = tuple(entry for entry in plan.entries if entry is not selected)
        metadata = dict(plan.metadata)
        work_root_value = str(metadata.get("canonical_work_root") or "").strip()
        unresolved = []
        if work_root_value:
            remaining_list, unresolved = _resolve_asset_collisions(
                remaining, Path(work_root_value), package_label=plan.source_root.name
            )
            remaining = tuple(remaining_list)
        metadata["asset_collision_unresolved"] = tuple(unresolved)
        revised = replace(
            plan,
            entries=remaining,
            ignored_sources=tuple(dict.fromkeys(
                plan.ignored_sources + (selected.source_path,)
            )),
            metadata=metadata,
            revision_parent_fingerprint=expected_fingerprint,
            revision_action=f"ignore_plan_entry:{selected_entry_id}",
            revision_reason=reason,
        )
        return self._persist_revision(revised)

    def build_source_partition_revisions(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_ids: Iterable[str],
        *,
        partition_id: str,
        partition_fingerprint: str,
        child_task_id: str,
        reason: str,
    ) -> RenamePlan:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise PlanReviewError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def build_source_partition_revision(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_id: str,
        *,
        partition_id: str,
        partition_fingerprint: str,
        child_task_id: str,
        reason: str,
    ) -> RenamePlan:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise PlanReviewError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')

    def record_source_partition_revision(self, revised: RenamePlan) -> RenamePlan:
        """Retired: no alternate writer or automatic compatibility fallback."""
        raise PlanReviewError('旧二分拆卡已停用，请使用“调整计划范围”；无法处理的旧任务请安全结束后重新添加。')



    def apply_provider_special_decision(
        self, task_id: str, expected_fingerprint: str, selected_entry_id: str, *,
        decision: Mapping[str, object], reason: str, persist_revision: bool = True,
    ) -> RenamePlan:
        """Commit explicit authority; pure projection is shared with replay/verifier."""
        from . import special_decision_projection
        from .decision_state import DecisionCommandService
        plan = self._current_plan(task_id, expected_fingerprint)
        if not str(reason).strip():
            raise PlanReviewError("特别篇决定必须记录理由")
        state = self.decision_state_store.load_optional(task_id)
        decisions = dict(state.provider_special_decisions if state is not None else
                         plan.metadata.get("provider_special_decisions") or {})
        if state is not None and state.content_selection is not None:
            raise PlanReviewError('当前内容计划请使用完整TV特别篇确认入口')
        decisions[selected_entry_id] = dict(decision)
        try:
            revised = special_decision_projection.project_special_decisions(plan, decisions)
        except special_decision_projection.SpecialDecisionProjectionError as exc:
            raise PlanReviewError(str(exc)) from exc
        resolved, unresolved = _resolve_asset_collisions(
            revised.entries, Path(str(revised.metadata.get("canonical_work_root") or
                                     revised.metadata.get("target_root"))),
            package_label=plan.source_root.name,
        )
        # A collision fallback is not a new authority for a decided bundle.
        # Keep canonical paths blocked instead of independently relocating sidecars.
        coherent = []
        for original, candidate in zip(revised.entries, resolved):
            if candidate.target_path != original.target_path:
                unresolved.append(original.source_path.name)
                coherent.append(original)
            else:
                coherent.append(candidate)
        revised = replace(
            revised, entries=tuple(coherent),
            metadata={**revised.metadata, "asset_collision_unresolved": tuple(unresolved)},
            revision_parent_fingerprint=expected_fingerprint,
            revision_action=f"provider_special:{selected_entry_id}:{decision.get('action')}",
            revision_reason=str(reason).strip(),
        )
        if not persist_revision:
            return revised
        if state is None:
            raise PlanReviewError("此旧任务尚无权威决策状态，请先重新识别")
        _state, projected = DecisionCommandService(self.decision_state_store, self.plan_store).update(
            task_id, expected_revision=state.revision,
            changes={"provider_special_decisions": decisions}, planner=lambda _state: revised,
            task_terminal=self._revision_task_terminal(task_id),
        )
        return self._record_revision(projected)

    def _persist_revision(self, revised: RenamePlan) -> RenamePlan:
        self._require_legacy_content_task(revised.task_id)
        from .content_selection import require_legacy_writer
        require_legacy_writer(revised)

        revised = persist_plan_revision(
            self.decision_state_store,
            self.plan_store,
            revised,
            task_terminal=self._revision_task_terminal(revised.task_id),
        )
        return self._record_revision(revised)

    def _persist_recognition_revision(
        self,
        revised: RenamePlan,
        recognition: RecognitionDecisionInput,
        *,
        state_transform=None,
    ) -> RenamePlan:
        self._require_legacy_content_task(revised.task_id)
        _decision, projected = commit_recognition_projection(
            self.decision_state_store,
            self.plan_store,
            recognition,
            revised,
            state_transform=state_transform,
            task_terminal=self._revision_task_terminal(revised.task_id),
        )
        return self._record_revision(projected)

    def _require_legacy_content_task(self, task_id):
        state = self.decision_state_store.load_optional(task_id)
        if state is not None and state.content_selection is not None:
            raise PlanReviewError('新内容计划的旧整体改写入口尚未迁移，请使用统一内容选择')

    def _revision_task_terminal(self, task_id: str) -> TaskTerminalMutation:
        from .review_operation import current_provider_selection_review_operation, finish_provider_selection
        selection = None

        def update_task(_decision, projected, current):
            verification = PlanVerifier().verify(projected)
            status = (
                "need_confirm"
                if verification.status.value == "accepted" or projected.metadata.get('pending_work_affiliation') is not None
                else verification.status.value
            )
            if (
                projected.work_identity is not None
                and projected.work_identity.status == "confirmed"
                and projected.work_identity.title.strip()
            ):
                current["name"] = projected.work_identity.title.strip()
            current.update({
                "workflow_status": status,
                "plan_status": verification.status.value,
                "plan_entry_count": len(projected.entries),
                "completed_at": None,
            })
            if projected.revision_action in {'tv_content_season', 'tv_content_work', 'tv_content_range'} or (projected.revision_action == 'work_affiliation_pending' and _decision.season_binding is not None):
                binding = _decision.season_binding
                current.update(season_id=binding['season_number'],
                    season_number=binding['season_number'], season_source=binding['decision_source'])
            if projected.revision_action == 'movie_domain_correction':
                current.update(is_anime=projected.work_identity.domain == 'animation', approved_at=None)
            if projected.revision_action == 'tv_content_work':
                current.update(approved_at=None, error=None)
            if projected.metadata.get("mapping_source") == "inventory_smart_completion":
                current["error"] = None
            if selection is not None and projected.revision_action in {
                    'tv_content_season', 'tv_content_work', 'initial_tv_completion',
                    'initial_movie_completion', 'movie_work_correction'}:
                finish_provider_selection(current, expected=selection,
                    result_revision=_decision.revision,
                    result_fingerprint=PlanStore.fingerprint(projected))
            return current

        terminal = TaskTerminalMutation.prepare(
            self.task_root,
            task_id,
            update_task,
        )
        try:
            active, context = current_provider_selection_review_operation(terminal.previous_payload)
            if context.get('lifecycle') in {'failed', 'awaiting_user'}:
                selection = active
        except ValueError:
            pass  # This revision need not originate from a provider selection.
        return terminal

    def asset_actions(self):
        from .asset_actions import AssetActionService
        service=AssetActionService(plan_store=self.plan_store, decision_store=self.decision_state_store,
            task_root=self.task_root, record_root=self.record_root, work_catalog=self.work_catalog,
            series_catalog=self.series_catalog)
        self.series_catalog.asset_action_context=service
        return service

    def confirm_asset_repair(self, task_id, expected_fingerprint, *, expected_operation_fingerprint, recovery_preparation=None):
        return self._confirm_asset_action(task_id, expected_fingerprint,
            expected_operation_fingerprint=expected_operation_fingerprint,
            recovery_preparation=recovery_preparation, repair=True)

    def confirm_asset_replacement(self, task_id, expected_fingerprint, *, expected_operation_fingerprint, recovery_preparation=None):
        return self._confirm_asset_action(task_id, expected_fingerprint,
            expected_operation_fingerprint=expected_operation_fingerprint,
            recovery_preparation=recovery_preparation, repair=False)

    def _confirm_asset_action(self, task_id, expected_fingerprint, *, expected_operation_fingerprint, recovery_preparation, repair):
        from .decision_state import DecisionCommandService
        current = self._current_plan(task_id, expected_fingerprint)
        selection = self.movie_selection_for_completion(current)
        if selection is not None:
            value = selection.to_dict()
            chosen = value['replacement']
            if chosen is None:
                raise PlanReviewError('请先从已登记电影版本下拉列表明确选择换源版本')
            record = self.work_catalog.load(value['work_key'])
            actual = [dict(row) for row in record['versions'] if row.get('version_ref') == chosen['version_ref']]
            if actual != chosen['versions']:
                raise PlanReviewError('所选电影版本登记已变化，请重新选择换源范围')
        payload = json.loads((self.task_root / f'{task_id}.json').read_text('utf-8'))
        ordinary = (payload.get('workflow_status') in {'need_confirm', 'invalid', 'approved'}
            and not payload.get('execution_requires_manual_check')
            and not payload.get('execution_may_have_written_targets')
            and payload.get('execution_status') in {None, 'not_started', 'dry_run'})
        if not ordinary:
            from .asset_execution_recovery import AssetExecutionRecovery
            context = getattr(self, 'asset_recovery_context', None)
            if recovery_preparation is None or context is None:
                raise PlanReviewError('当前任务必须先核查，不能改写正在执行或已完成的批准')
            checked = AssetExecutionRecovery(context).validate_preparation(recovery_preparation)
            if (checked.state != 'replan_required' or checked.plan.task_id != task_id
                    or self.plan_store.fingerprint(checked.plan) != expected_fingerprint):
                raise PlanReviewError('旧事务不满足安全重新生成条件')
        actions = self.asset_actions()
        operation, draft = (actions.prepare_missing if repair else actions.prepare_replacement)(current)
        if operation.fingerprint != expected_operation_fingerprint or operation.to_dict()['mode'] != ('repair' if repair else 'replace'):
            raise PlanReviewError('换源范围或文件已变化，请重新核对')
        state = self.decision_state_store.load(task_id)
        def update_task(_decision, projected, payload):
            retained = {key: value for key, value in payload.items() if not key.startswith('execution_')}
            return {**retained, 'workflow_status': 'need_confirm', 'execution_status': None, 'queue_operation': None,
                'approved_at': None, 'completed_at': None, 'error': None,
                'execution_requires_manual_check': False, 'execution_may_have_written_targets': False}
        _, revised = DecisionCommandService(self.decision_state_store, self.plan_store).update(
            task_id, expected_revision=state.revision, changes={'asset_operation': operation},
            planner=lambda _: draft, task_terminal=TaskTerminalMutation.prepare(self.task_root, task_id, update_task))
        return self._record_revision(revised)

    def _record_revision(self, revised: RenamePlan) -> RenamePlan:
        self._save(PlanReviewDecision(
            revised.task_id,
            self.plan_store.fingerprint(revised),
        ))
        return revised



    def persist_unapproved_revision(self, revised: RenamePlan) -> RenamePlan:
        """Persist a service-generated revision and invalidate old approval."""

        return self._persist_revision(revised)

    def apply_subtitle_override_rule(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_id: str,
        *,
        scope: SubtitleOverrideScope,
        associated_video_source_path: Optional[Path] = None,
        primary_language: str,
        script: Optional[str],
        region_hint: Optional[str] = None,
        additional_languages: Iterable[str] = (),
        track_title_tokens: Iterable[str] = (),
        flags: Iterable[str] = (),
        unknown_dispositions: Iterable[UnknownTokenDisposition] = (),
        reason: str,
        _persist: bool = True,
    ) -> RenamePlan:
        """Apply one immutable file/current-Plan-series rule atomically."""

        current = self._current_plan(task_id, expected_fingerprint)
        from .content_choice import subtitle_edit_view
        plan = subtitle_edit_view(current)
        if not str(reason).strip():
            raise PlanReviewError("字幕人工决定必须填写理由")
        matching = [
            index for index, entry in enumerate(plan.entries)
            if entry_id(entry) == selected_entry_id
        ]
        if len(matching) != 1:
            raise PlanReviewError("字幕审核条目不存在或不唯一")
        selected_index = matching[0]
        original = plan.entries[selected_index]
        if original.media_kind != "subtitle" or original.subtitle is None:
            raise PlanReviewError("所选条目不是结构化字幕条目")
        contract = original.subtitle
        try:
            parsed_scope = SubtitleOverrideScope(scope)
        except ValueError as exc:
            raise PlanReviewError("不支持的字幕人工规则作用域") from exc
        if parsed_scope == SubtitleOverrideScope.SERIES and plan.series_scope is None:
            raise PlanReviewError("当前 Plan 缺少稳定 TMDB TV 身份，不能使用当前系列作用域")
        disposition_tuple = tuple(unknown_dispositions)
        _validate_unknown_dispositions(contract.unknown_tokens, disposition_tuple)
        outcome = SubtitleRuleOutcome(
            primary_language=str(primary_language),
            script=script,
            region_hint=region_hint,
            additional_languages=tuple(str(item) for item in additional_languages),
            track_title_tokens=tuple(str(item) for item in track_title_tokens),
            flags=tuple(str(item) for item in flags),
            unknown_dispositions=disposition_tuple,
        )
        from .subtitle_batch import make_rule
        rule = make_rule(plan, expected_fingerprint, original, outcome, parsed_scope, str(reason).strip())

        owners = ((str(original.source_path), str(associated_video_source_path)),) if associated_video_source_path is not None else ()
        return self._commit_subtitle_edit(current, plan.subtitle_override_rules + (rule,),
            owners=owners, action=f'apply_subtitle_rule:{rule.rule_id}', reason=str(reason).strip(), persist=_persist)

    def adjust_subtitles(self, task_id, expected_fingerprint, selected_entry_ids, *,
                         main_language, additional_languages=(), remove_tags=(), _persist=True):
        """保存当前计划明确选中的字幕，整批只有一个新修订。"""
        current = self._current_plan(task_id, expected_fingerprint)
        return self._commit_subtitle_edit(current, (),
            batch=(tuple(selected_entry_ids), main_language, tuple(additional_languages), tuple(remove_tags)),
            action='apply_subtitle_batch', reason='调整字幕结构', persist=_persist)

    def _commit_subtitle_edit(self, current, rules, *, action, reason, owners=(), persist=True, batch=()):
        from .public_revision import SubtitleChange, commit
        command = SubtitleChange(current.task_id, self.plan_store.fingerprint(current), rules, owners, action, reason, batch)
        _, projected = commit(self, command, task_terminal=self._revision_task_terminal(current.task_id), persist=persist)
        return self._record_revision(projected) if persist else projected

    def revert_subtitle_rule(
        self,
        task_id: str,
        expected_fingerprint: str,
        rule_id: str,
        reason: str,
    ) -> RenamePlan:
        """Revert by creating a new revision; old approvals are never revived."""

        current = self._current_plan(task_id, expected_fingerprint)
        if not str(reason).strip():
            raise PlanReviewError("撤销字幕规则必须填写理由")
        rules = [rule for rule in current.subtitle_override_rules if rule.rule_id == rule_id]
        if len(rules) != 1:
            raise PlanReviewError("待撤销的字幕规则不存在或不唯一")
        if not current.subtitle_override_rules or current.subtitle_override_rules[-1].rule_id != rule_id:
            raise PlanReviewError("首版只允许撤销最后应用的字幕规则，避免丢失后续修订")
        from .subtitle_batch import rule_batches
        first = rule_batches(current.subtitle_override_rules)[-1][0]
        return self._commit_subtitle_edit(current, current.subtitle_override_rules[:first],
            action=f'revert_subtitle_rule:{rule_id}', reason=str(reason).strip())

    def _current_plan(
        self, task_id: str, expected_fingerprint: str
    ) -> RenamePlan:
        try:
            plan = self.plan_store.load(task_id)
        except PlanStoreError as exc:
            raise PlanReviewError(f"无法读取当前 Plan：{exc}") from exc
        current = self.plan_store.fingerprint(plan)
        if current != expected_fingerprint:
            raise PlanReviewError("Plan 版本已变化，请刷新后重试")
        return plan

    def _save(self, decision: PlanReviewDecision) -> None:
        path = self._path_helper.path_for(decision.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as file:
                json.dump(decision.to_dict(), file, indent=2, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError) as exc:
            raise PlanReviewError(f"无法保存审核决定：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)


def _target_root(plan: RenamePlan) -> Path:
    configured = plan.metadata.get("target_root")
    if isinstance(configured, str) and configured.strip():
        return Path(configured)
    parents = [str(entry.target_path.parent) for entry in plan.entries]
    if not parents:
        raise PlanReviewError("空 Plan 没有可审核的目标根目录")
    try:
        return Path(os.path.commonpath(parents))
    except ValueError as exc:
        raise PlanReviewError("Plan 目标路径不在同一根目录") from exc


def _is_within(path: Path, root: Path) -> bool:
    candidate = os.path.normpath(str(path)).casefold()
    anchor = os.path.normpath(str(root)).casefold()
    try:
        return os.path.commonpath((candidate, anchor)) == anchor
    except ValueError:
        return False


def _path_key(path: Path) -> str:
    return os.path.normpath(str(path)).casefold()


def _canonical_label(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _safe_media_name(value: str) -> str:
    """Return a Jellyfin-safe leaf without changing the confirmed title fact."""

    try:
        return canonicalize_media_leaf(value)
    except CanonicalNamingError:
        # Legacy callers use an empty value as a local "cannot derive" signal.
        # Canonical root construction itself never uses this compatibility seam.
        return ""


def _movie_version_tokens(
    value: str,
    *,
    layer_label: str,
    collection_hint: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """Parse one trusted evidence layer and reject same-layer conflicts."""

    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    token_text = re.sub(r"[^a-z0-9.+]+", " ", normalized)
    resolutions = set()
    # Mastering labels such as RM4K are not encoded pixel dimensions.
    if re.search(r"(?<![a-z0-9])(2160p|4k)(?![a-z0-9])", normalized):
        resolutions.add("2160p")
    if re.search(r"(?<![a-z0-9])1080[pi]?(?![a-z0-9])", normalized):
        resolutions.add("1080p")
    if re.search(r"(?<![a-z0-9])720[pi]?(?![a-z0-9])", normalized):
        resolutions.add("720p")
    for width, height in re.findall(r"(?<!\d)(\d{3,4})\s*[x×]\s*(\d{3,4})(?!\d)", normalized):
        width, height = int(width), int(height)
        # Retain a class, not a claim that a cropped image has 2160 rows.
        if width in {3840, 4096} and 1080 < height <= 2160:
            resolutions.add('2160p')
        elif (width == 1920 and 720 < height <= 1080) or (height == 1080 and 1280 < width < 3840):
            resolutions.add('1080p')
        elif width == 1280 and 480 < height <= 720:
            resolutions.add('720p')
    if len(resolutions) > 1 and not collection_hint:
        raise PlanReviewError(
            f"电影版本规格证据冲突：{layer_label}包含多个分辨率 token"
        )

    dynamic_ranges = {}
    if re.search(
        r"\b(dovi|dolby\s*vision|dv(?:\s*p?8(?:\.1)?)?)\b",
        token_text,
    ):
        dynamic_ranges["dolby_vision"] = (
            "Dolby Vision P8.1"
            if re.search(r"(?:dovi|dv|dolby[ ._-]*vision)[ ._-]*p?8[ ._-]*1(?!\d)", normalized)
            else "Dolby Vision"
        )
    if re.search(r"(?<![a-z0-9])hdr10\s*\+(?![a-z0-9])", normalized):
        dynamic_ranges["hdr10"] = "HDR10+"
    elif re.search(r"(?<![a-z0-9])hdr10(?![a-z0-9])", normalized):
        dynamic_ranges["hdr10"] = "HDR10"
    if re.search(r"\bsdr\b", token_text):
        dynamic_ranges["sdr"] = "SDR"
    incompatible = 'sdr' in dynamic_ranges and len(dynamic_ranges) > 1
    if incompatible and not collection_hint:
        raise PlanReviewError(
            f"电影版本规格证据冲突：{layer_label}包含多个动态范围 token"
        )
    return (
        next(iter(resolutions), None) if len(resolutions) <= 1 else None,
        (' '.join(dynamic_ranges.values()) or None) if not incompatible else None,
    )


def _infer_movie_version_descriptor(
    source: Path,
    group_id: str,
    source_root: Path,
) -> VersionDescriptor:
    """Infer explicit tokens only inside the frozen source-root boundary."""

    source = Path(source)
    source_root = Path(source_root)
    try:
        relative_source = source.relative_to(source_root)
    except ValueError as exc:
        raise PlanReviewError(
            "电影正片不在冻结 source_root 范围内；请重新生成整理计划"
        ) from exc

    layers = [("文件名", source.name)]
    for depth, name in enumerate(reversed(relative_source.parts[:-1]), start=1):
        layers.append((f"source_root 范围内第 {depth} 级父目录", name))
    if source_root.name:
        layers.append(("source_root 目录名", source_root.name))

    resolution_evidence = []
    dynamic_range_evidence = []
    for layer_label, value in layers:
        layer_resolution, layer_dynamic_range = _movie_version_tokens(
            value,
            layer_label=layer_label,
            collection_hint=layer_label != "文件名",
        )
        if layer_resolution is not None:
            resolution_evidence.append((layer_resolution, layer_label))
        if layer_dynamic_range is not None:
            dynamic_range_evidence.append((layer_dynamic_range, layer_label))

    resolution_values = {value for value, _label in resolution_evidence}
    if len(resolution_values) > 1:
        raise PlanReviewError(
            "电影版本规格证据冲突：文件名与冻结 source_root 范围内目录"
            "给出不同分辨率"
        )
    resolution = next(iter(resolution_values), None)

    dynamic_families = {
        (
            "dolby_vision" if value.startswith("Dolby Vision")
            else "hdr10" if value.startswith("HDR10")
            else value.casefold()
        )
        for value, _label in dynamic_range_evidence
    }
    if 'sdr' in dynamic_families and len(dynamic_families) > 1:
        raise PlanReviewError(
            "电影版本规格证据冲突：文件名与冻结 source_root 范围内目录"
            "给出不同动态范围"
        )
    dynamic_values = {value for value, _label in dynamic_range_evidence}
    dynamic_parts = []
    if any('Dolby Vision' in value for value in dynamic_values):
        dynamic_parts.append('Dolby Vision P8.1' if any('P8.1' in value for value in dynamic_values) else 'Dolby Vision')
    if any('HDR10+' in value for value in dynamic_values):
        dynamic_parts.append('HDR10+')
    elif any('HDR10' in value for value in dynamic_values):
        dynamic_parts.append('HDR10')
    if 'SDR' in dynamic_values:
        dynamic_parts.append('SDR')
    dynamic_range = ' '.join(dynamic_parts) or None
    parts = tuple(item for item in (resolution, dynamic_range) if item)
    label = " ".join(parts) or None
    technical_key = "-".join(
        re.sub(r"[^a-z0-9]+", "-", item.casefold()).strip("-") for item in parts
    ) or "default"

    def field(
        value: Optional[str],
        observations: list[tuple[str, str]],
    ) -> EvidenceValue:
        return (
            EvidenceValue(
                status="resolved", value=value, selected_source="filename_hint",
                candidates=(EvidenceCandidate(
                    value=value,
                    source="filename_hint",
                    evidence=tuple(
                        f"{layer_label}包含明确规格 token：{observed_value}"
                        for observed_value, layer_label in observations
                    ),
                ),),
                manual_reason=None,
            )
            if value else EvidenceValue.unknown()
        )

    return VersionDescriptor(
        version_ref=build_version_ref(
            group_id, None, None, "default", technical_key
        ),
        edition=EvidenceValue(
            status="resolved", value=EditionValue("default", None),
            selected_source=None, candidates=(), manual_reason=None,
            kind="structural_default",
        ),
        technical_variant_key=technical_key,
        resolution=field(resolution, resolution_evidence),
        dynamic_range=field(dynamic_range, dynamic_range_evidence),
        key_audio=EvidenceValue.unknown(),
        display_label=label,
    )


def _normalized_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _movie_target_version_label(value: Optional[str]) -> Optional[str]:
    """Use a compact stable filename label without changing descriptor evidence."""

    if value is None:
        return None
    return str(value).replace("Dolby Vision", "DoVi")


def _movie_extra_role_label(source: Path, *, use_stable_role: bool) -> str:
    """Derive an order-independent role token for multi-version movie extras."""

    if not use_stable_role:
        return source.stem
    tokens = re.findall(r"\[([^\[\]]+)\]", source.stem)
    for token in tokens:
        normalized = unicodedata.normalize("NFKC", token).strip()
        if any(
            re.search(
                rf"(?<![a-z0-9]){re.escape(str(tag).casefold())}(?![a-z])",
                normalized.casefold(),
            )
            for tag in EXTRA_TAG
        ):
            return _safe_media_name(normalized)
    if tokens:
        role = unicodedata.normalize("NFKC", tokens[-1]).strip()
        if role:
            return _safe_media_name(role)
    return _safe_media_name(source.stem)


def _infer_owner_descriptor(
    source: Path, descriptors: Tuple[VersionDescriptor, ...]
) -> Optional[VersionDescriptor]:
    if len(descriptors) == 1:
        return descriptors[0]
    haystack = _normalized_key(" / ".join(Path(source).parts[-4:]))
    matched = []
    for descriptor in descriptors:
        tokens = []
        if isinstance(descriptor.resolution.value, str):
            tokens.append(descriptor.resolution.value.casefold())
        if isinstance(descriptor.dynamic_range.value, str):
            dynamic = descriptor.dynamic_range.value.casefold()
            if "dolby vision" in dynamic:
                tokens.extend(("dovi", "dolby vision", "dv p8", "dv p8.1"))
            elif dynamic == "sdr":
                tokens.append("sdr")
            elif "hdr10" in dynamic:
                tokens.append("hdr10")
        distinctive = tuple(token for token in tokens if token not in {"2160p", "1080p", "720p"})
        wanted = distinctive or tuple(tokens)
        if wanted and any(token in haystack for token in wanted):
            matched.append(descriptor)
    return matched[0] if len(matched) == 1 else None


def _resolve_asset_collisions(
    entries: Tuple[PlanEntry, ...],
    work_root: Path,
    *,
    package_label: Optional[str] = None,
) -> tuple[list[PlanEntry], list[str]]:
    """Observe and replay the shared frozen collision projector."""
    from .collision_projection import observe_collisions, CollisionProbes, CollisionProjection
    try:
        _probes, result = observe_collisions(tuple(entries), work_root, package_label=package_label)
        if type(_probes) is not CollisionProbes or type(result) is not CollisionProjection:
            raise PlanReviewError('碰撞观察或投影器已断开')
        return list(result.entries), list(result.unresolved)
    except AssetCollisionError as exc:
        raise PlanReviewError(str(exc)) from exc


def _unresolved_references_sources(
    item: object, sources: set[Path]
) -> bool:
    source_keys = {_path_key(path) for path in sources}
    if isinstance(item, Mapping):
        value = str(item.get("source_path") or "")
        return bool(value) and _path_key(Path(value)) in source_keys
    text = str(item)
    return any(path.name in text for path in sources)


def _reuse_resolution(source: Path, target: Path):
    try:
        return build_reuse_resolution(source, target)
    except AssetCollisionError as exc:
        raise PlanReviewError(str(exc)) from exc


def _rebase_movie_target(target: Path, old_root: Path, new_root: Path) -> Path:
    """Move a frozen target mapping to the catalog root and naming authority."""

    target = Path(target)
    if _path_key(old_root) == _path_key(new_root):
        return target
    try:
        relative = target.relative_to(old_root)
    except ValueError as exc:
        raise PlanReviewError("旧整理计划含有规范作品目录之外的目标，不能自动重基") from exc
    parts = list(relative.parts)
    if parts and parts[-1].startswith(old_root.name):
        parts[-1] = f"{new_root.name}{parts[-1][len(old_root.name):]}"
    return new_root.joinpath(*parts)


def _subtitle_matcher(contract: SubtitlePlanContract) -> SubtitleRuleMatcher:
    return SubtitleRuleMatcher(
        raw_label_canonical=_canonical_label(contract.raw_label),
        unknown_tokens_canonical=tuple(
            _canonical_label(token) for token in contract.unknown_tokens
        ),
        association_rule_version=contract.association_rule_version,
        label_rule_version=contract.label_rule_version,
    )


def _validate_unknown_dispositions(
    unknown_tokens: Tuple[str, ...],
    dispositions: Tuple[UnknownTokenDisposition, ...],
) -> None:
    canonical_unknown = tuple(_canonical_label(token) for token in unknown_tokens)
    if len(set(canonical_unknown)) != len(canonical_unknown):
        raise PlanReviewError("未知 token 存在重复或规范等价项，必须逐项重新解析")
    canonical_dispositions = tuple(
        _canonical_label(item.token) for item in dispositions
    )
    if (
        len(canonical_dispositions) != len(canonical_unknown)
        or set(canonical_dispositions) != set(canonical_unknown)
        or len(set(canonical_dispositions)) != len(canonical_dispositions)
    ):
        raise PlanReviewError("每个未知 token 必须且只能有一个人工处置")
    for item in dispositions:
        if item.action.value == "discard_manual" and not item.reason.strip():
            raise PlanReviewError("删除未知 token 必须逐项填写理由")


def _dispositions_for_contract(
    unknown_tokens: Tuple[str, ...],
    selected_dispositions: Tuple[UnknownTokenDisposition, ...],
) -> Tuple[UnknownTokenDisposition, ...]:
    by_canonical = {
        _canonical_label(item.token): item for item in selected_dispositions
    }
    result = []
    for raw_token in unknown_tokens:
        selected = by_canonical.get(_canonical_label(raw_token))
        if selected is None:
            raise PlanReviewError("系列规则遇到不一致的未知 token，已停止整批保存")
        result.append(replace(selected, token=raw_token))
    return tuple(result)


def _titles_for_contract(
    requested_titles: Tuple[str, ...],
    unknown_tokens: Tuple[str, ...],
    selected_dispositions: Tuple[UnknownTokenDisposition, ...],
) -> Tuple[str, ...]:
    unknown_keys = {_canonical_label(token) for token in unknown_tokens}
    titles = [
        str(token) for token in requested_titles
        if _canonical_label(token) not in unknown_keys
    ]
    dispositions = _dispositions_for_contract(unknown_tokens, selected_dispositions)
    present = {_canonical_label(token) for token in titles}
    for item in dispositions:
        if item.action.value == "preserve_as_title":
            canonical = _canonical_label(item.token)
            if canonical not in present:
                titles.append(item.token)
                present.add(canonical)
    return tuple(titles)


def _rule_fingerprint(rule: SubtitleOverrideRule) -> str:
    payload = json.dumps(
        rule.unsigned_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
