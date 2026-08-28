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
    WorkIntentError,
    WorkIntentStore,
    derive_work_intent_key,
    make_content_key,
    WORK_FONT_CONDITIONAL_ACTION,
)
from .utils import EXTRA_TAG, JELLYFIN_EXTRAS_DIR, is_font_archive
from .episode_inference import (
    EpisodeInferenceError,
    build_episode_hints,
    build_secondary_sequence_hints,
    build_semantic_context,
    normalize_target_stem,
)
from .media_classifier import (
    LocalRole,
    MediaClassification,
    MediaPreclassification,
    PreclassificationKind,
    classify_media,
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
from .asset_collision import AssetCollisionError, build_reuse_resolution, fingerprint_file
from .font_archive_contract import (
    FontArchiveContractError,
    canonical_font_archive_target,
    is_supported_font_archive,
)
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
    "unset", "inferred_path", "provider", "explicit_user",
    "legacy_placeholder", "legacy_explicit",
})


class ReviewStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    CANCELLED = "cancelled"


class PlanReviewError(RuntimeError):
    pass


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


class PlanReviewService:
    def __init__(
        self,
        plan_store: PlanStore,
        review_root: Path,
        catalog_root: Path = WORK_CATALOG_PATH,
        decision_state_root: Optional[Path] = None,
        task_root: Optional[Path] = None,
    ):
        self.plan_store = plan_store
        self.review_root = Path(review_root)
        self._path_helper = PlanStore(self.review_root)
        self.work_catalog = WorkCatalog(catalog_root)
        self.decision_state_store = DecisionStateStore(
            decision_state_root or (plan_store.root.parent / "decision_state")
        )
        self.task_root = Path(task_root or (plan_store.root.parent / "task"))
        self.series_catalog = SeriesCatalog(
            Path(catalog_root).parent / "series_catalog"
        )

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
    ) -> PlanReviewDecision:
        plan = self._current_plan(task_id, expected_fingerprint)
        verification = PlanVerifier().verify(plan)
        if verification.issues:
            codes = ", ".join(issue.code for issue in verification.issues)
            raise PlanReviewError(f"Plan 结构无效，不能批准：{codes}")
        valid_ids = {entry_id(entry) for entry in plan.entries}
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
        self._save(updated)
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
        return self._persist_recognition_revision(
            revised,
            RecognitionDecisionInput(
                task_id=task_id,
                work_identity=revised.work_identity,
                linked_provider_identities=tuple(dict.fromkeys(authority_linked)),
            ),
        )

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
        return self._persist_revision(revised)

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
        confirmed_segment_sources: Optional[Iterable[Path]] = None,
        work_domain: Optional[str] = None,
        role_decisions: Optional[Mapping[Path, str]] = None,
        season_number: Optional[int] = None,
        season_source: Optional[str] = None,
        series_key: Optional[str] = None,
        season_binding_id: Optional[str] = None,
        series_catalog_revision: int = 0,
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
        catalog_match = None
        series_match = None
        try:
            requested_key = plan.metadata.get("catalog_work_key")
            if (
                isinstance(requested_key, str)
                and plan.metadata.get("catalog_link_decision") == "explicit_user"
            ):
                catalog_match = self.work_catalog.get_match(requested_key)
            else:
                catalog_match = self.work_catalog.find_exact(
                    work.primary_provider, group_kind=group_kind
                )
            if catalog_match is not None and catalog_match.group_kind != group_kind:
                raise PlanReviewError("既有作品登记的观看形式与当前整理方式不一致")
        except WorkCatalogError as exc:
            raise PlanReviewError(str(exc)) from exc
        try:
            proposed_work_key = (
                derive_work_intent_key(
                    primary_provider=work.primary_provider,
                    group_kind=group_kind,
                )
                if group_kind == "movie_continuous"
                else plan.metadata.get("proposed_work_key")
            )
        except (TypeError, ValueError) as exc:
            raise PlanReviewError(f"无法建立稳定作品意图：{exc}") from exc
        if group_kind == "tv_episodes" and series_key:
            series_path = self.series_catalog.root / f"{series_key}.json"
            if series_path.exists():
                try:
                    series_match = self.series_catalog.load(str(series_key))
                    provider_match = self.series_catalog.find_by_provider(
                        work.primary_provider.to_dict()
                    )
                except SeriesCatalogError as exc:
                    raise PlanReviewError(str(exc)) from exc
                if provider_match is None or (
                    provider_match.series_key != series_match.series_key
                ):
                    raise PlanReviewError(
                        "SeriesCatalog 与当前作品资料身份不一致，请重新匹配"
                    )
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
        if group_kind == "movie_continuous":
            try:
                episode_hints = build_episode_hints(
                    (entry.source_path for entry in videos), work.title
                )
            except EpisodeInferenceError as exc:
                raise PlanReviewError(str(exc)) from exc

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
            preclassifications = {
                entry.source_path: self._preclassify_inventory_video(
                    item_path=entry.source_path,
                    candidate_name=entry.source_path.name,
                    season_id=default_season_id,
                    extra_context_name=build_semantic_context(
                        entry.source_path.name, work.title
                    ),
                    forced_role=supplied_roles.get(entry.source_path),
                )
                for entry in videos
            }
            main_candidates = tuple(
                source
                for source, preclassification in preclassifications.items()
                if preclassification.kind == PreclassificationKind.MAIN_CANDIDATE
            )
            secondary_sequence_hints = build_secondary_sequence_hints(
                main_candidates, work.title
            )
            try:
                episode_hints = build_episode_hints(main_candidates, work.title)
            except EpisodeInferenceError as exc:
                # A genuine main-to-main collision is handled as an unresolved
                # current-plan mapping below, rather than letting an old draft
                # escape the target root or preventing the user from repairing it.
                episode_hints = {}
                episode_assignment_conflicts = [
                    str(source) for source in main_candidates
                ]
            for entry in videos:
                preclassification = preclassifications[entry.source_path]
                if preclassification.kind != PreclassificationKind.MAIN_CANDIDATE:
                    classification = preclassification.classification
                elif episode_assignment_conflicts:
                    classification = MediaClassification(
                        LocalRole.UNKNOWN, default_season_id, "unknown", None,
                        "Low", "正片候选存在集号冲突，等待人工确认用途",
                    )
                else:
                    classification = classify_media(
                        item_path=entry.source_path,
                        candidate_name=entry.source_path.name,
                        season_id=default_season_id,
                        extra_context_name=build_semantic_context(
                            entry.source_path.name, work.title
                        ),
                        episode_hint=episode_hints.get(entry.source_path),
                        secondary_bonus_episode=secondary_sequence_hints.get(
                            entry.source_path
                        ),
                    )
                season_id = classification.season_scope
                normalized_stem = normalize_target_stem(
                    entry.source_path.stem, work.title
                ) or entry.source_path.stem
                suffix = entry.source_path.suffix.lower()
                if classification.local_role == LocalRole.MAIN_EPISODE:
                    episode = classification.episode_number
                    if episode is None:
                        unresolved_videos.append(entry.source_path.name)
                        continue
                    target = (
                        work_root
                        / f"Season {season_id:02d}"
                        / f"{folder_name} S{season_id:02d}E{episode:02d}{suffix}"
                    )
                    video_targets[entry.source_path] = target
                    content_video_targets[entry.source_path] = target
                    video_identity[entry.source_path] = (season_id, episode)
                    video_roles[entry.source_path] = "main_episode"
                    video_assets[entry.source_path] = PresentationAssetPlanContract(
                        selected_group.group_id,
                        AssetRole("content_version"),
                        None,
                        build_default_version_descriptor(
                            selected_group.group_id, season_id, episode
                        ),
                    )
                    continue
                if classification.local_role in {
                    LocalRole.DISC_EXTRA,
                    LocalRole.EPISODE_LIKE_BONUS,
                    LocalRole.PROVIDER_SPECIAL,
                }:
                    target = (
                        work_root
                        / f"Season {season_id:02d}"
                        / JELLYFIN_EXTRAS_DIR
                        / f"{normalized_stem}{suffix}"
                    )
                    video_targets[entry.source_path] = target
                    video_identity[entry.source_path] = (season_id, None)
                    video_roles[entry.source_path] = classification.local_role.value
                    video_assets[entry.source_path] = PresentationAssetPlanContract(
                        selected_group.group_id,
                        AssetRole("extra", "other"),
                        None,
                        None,
                    )
                    continue

                # Unknown is still a current-plan decision.  Never retain the
                # movie inventory draft target after the user switches to TV.
                try:
                    relative_source = entry.source_path.relative_to(plan.source_root)
                except ValueError:
                    relative_source = Path(entry.source_path.name)
                target = work_root / "ManualReview" / relative_source
                video_targets[entry.source_path] = target
                video_identity[entry.source_path] = (season_id, None)
                video_roles[entry.source_path] = "unknown"
                video_assets[entry.source_path] = PresentationAssetPlanContract(
                    selected_group.group_id,
                    AssetRole("unknown"),
                    None,
                    None,
                )
                if entry.source_path.name not in unresolved_videos:
                    unresolved_videos.append(entry.source_path.name)
        elif (
            len(videos) == 1
            or primary_video_source is not None
            or primary_video_sources is not None
        ):
            entries_by_source = {entry.source_path: entry for entry in videos}
            requested_sources = tuple(
                Path(item) for item in (primary_video_sources or ())
            )
            if primary_video_source is not None:
                requested_sources += (Path(primary_video_source),)
            if not requested_sources:
                requested_sources = (videos[0].source_path,)
            selected_sources = tuple(dict.fromkeys(requested_sources))
            if any(source not in entries_by_source for source in selected_sources):
                raise PlanReviewError("所选电影正片不在当前包的视频清单中")
            descriptors = {}
            labels = set()
            refs = set()
            for selected_source in selected_sources:
                entry = entries_by_source[selected_source]
                descriptor = _infer_movie_version_descriptor(
                    entry.source_path, selected_group.group_id, plan.source_root
                )
                label_key = _normalized_key(descriptor.display_label or "默认版本")
                if label_key in labels or descriptor.version_ref in refs:
                    raise PlanReviewError(
                        "多个电影正片无法生成唯一版本标签；请在文件名中区分 SDR、"
                        "DoVi、分辨率或发行版后重新识别"
                    )
                labels.add(label_key)
                refs.add(descriptor.version_ref)
                descriptors[selected_source] = descriptor
                label_suffix = (
                    f" - {descriptor.display_label}"
                    if descriptor.display_label else ""
                )
                video_targets[entry.source_path] = (
                    work_root
                    / f"{folder_name}{label_suffix}{entry.source_path.suffix.lower()}"
                )
                content_video_targets[entry.source_path] = video_targets[entry.source_path]
                video_identity[entry.source_path] = (None, None)
                video_roles[entry.source_path] = "movie_content"
                video_assets[entry.source_path] = PresentationAssetPlanContract(
                    selected_group.group_id,
                    AssetRole("content_version"),
                    None,
                    descriptor,
                )
            for extra in videos:
                if extra.source_path in descriptors:
                    continue
                owner_descriptor = _infer_owner_descriptor(
                    extra.source_path, tuple(descriptors.values())
                )
                target_version_label = _movie_target_version_label(
                    owner_descriptor.display_label
                    if owner_descriptor is not None else None
                )
                label_suffix = (
                    f" - {target_version_label}" if target_version_label else ""
                )
                role_label = _movie_extra_role_label(
                    extra.source_path,
                    use_stable_role=(len(descriptors) > 1),
                )
                extra_name = (
                    f"{folder_name}{label_suffix} - {role_label}"
                    f"{extra.source_path.suffix.lower()}"
                )
                video_targets[extra.source_path] = (
                    work_root / JELLYFIN_EXTRAS_DIR / extra_name
                )
                video_identity[extra.source_path] = (None, None)
                video_roles[extra.source_path] = "disc_extra"
                video_assets[extra.source_path] = PresentationAssetPlanContract(
                    selected_group.group_id,
                    AssetRole("extra", "other"),
                    None,
                    None,
                    owner_descriptor.version_ref if owner_descriptor is not None else None,
                )
        else:
            confirmed_segments = tuple(
                dict.fromkeys(
                    Path(item) for item in (confirmed_segment_sources or ())
                )
            )
            video_sources = {entry.source_path for entry in videos}
            if confirmed_segments and set(confirmed_segments) != video_sources:
                raise PlanReviewError("人工确认的连续分段必须包含当前包的全部视频")
            indexes = {
                entry.source_path: episode_hints.get(entry.source_path)
                for entry in videos
            }
            observed = {value for value in indexes.values() if value is not None}
            expected = set(range(1, len(videos) + 1))
            if confirmed_segments:
                segment_sources = confirmed_segments
                indexes = {
                    source: index
                    for index, source in enumerate(segment_sources, start=1)
                }
            elif len(observed) != len(videos) or observed != expected:
                raise PlanReviewError(
                    "连续电影包含多个视频，但文件名不能唯一证明完整分段；"
                    "请改选按季和集播放，或在文件列表中人工确认版本/分段"
                )
            else:
                segment_sources = tuple(
                    entry.source_path
                    for entry in sorted(
                        videos,
                        key=lambda item: indexes[item.source_path] or 0,
                    )
                )
            descriptor = build_default_version_descriptor(
                selected_group.group_id, None, None
            )
            entries_by_source = {entry.source_path: entry for entry in videos}
            for source in segment_sources:
                entry = entries_by_source[source]
                index = indexes[source]
                assert index is not None
                video_targets[entry.source_path] = (
                    work_root
                    / f"{folder_name}-cd{index}{entry.source_path.suffix.lower()}"
                )
                content_video_targets[entry.source_path] = video_targets[entry.source_path]
                video_identity[entry.source_path] = (None, None)
                video_roles[entry.source_path] = "movie_content"
                video_assets[entry.source_path] = PresentationAssetPlanContract(
                    selected_group.group_id,
                    AssetRole("content_version"),
                    SegmentDescriptor("cd", index, len(videos)),
                    descriptor,
                )

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
        video_target_by_stem = {
            unicodedata.normalize("NFKC", source.stem).casefold(): target
            for source, target in video_targets.items()
        }
        video_source_by_stem = {
            unicodedata.normalize("NFKC", source.stem).casefold(): source
            for source in video_targets
        }
        movie_descriptors = tuple(
            asset.version_descriptor
            for asset in video_assets.values()
            if asset.version_descriptor is not None
        )
        owner_by_video_source = {}
        for video_source, asset in video_assets.items():
            if asset.version_descriptor is not None:
                owner_by_video_source[video_source] = asset.version_descriptor.version_ref
            else:
                owner_by_video_source[video_source] = asset.owner_version_ref
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
                    # The strict subtitle contract intentionally permits only
                    # content-version subtitles.  A subtitle deterministically
                    # paired with an extra is therefore kept beside that extra
                    # as an associated resource, without making it part of the
                    # movie's primary subtitle decision surface.
                    revised_entries.append(replace(
                        original,
                        target_path=extra_decision.target_preview,
                        media_kind="associated",
                        role="extra_subtitle",
                        requires_review=True,
                        evidence=original.evidence + (
                            "字幕与特典视频同名，作为特典配套资源",
                        ),
                        subtitle=None,
                        presentation_asset=PresentationAssetPlanContract(
                            selected_group.group_id,
                            AssetRole("associated"),
                            None,
                            None,
                            owner_by_video_source.get(
                                extra_decision.association.associated_video
                            ),
                        ),
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
            matching_video = video_target_by_stem.get(
                unicodedata.normalize("NFKC", source.stem).casefold()
            )
            matching_video_source = video_source_by_stem.get(
                unicodedata.normalize("NFKC", source.stem).casefold()
            )
            try:
                relative_source = source.relative_to(plan.source_root)
            except ValueError:
                relative_source = Path(source.name)
            target = (
                matching_video.with_suffix(source.suffix.lower())
                if matching_video is not None
                else work_root / "Associated" / relative_source
            )
            owner_ref = owner_by_video_source.get(matching_video_source)
            if owner_ref is None and group_kind == "movie_continuous":
                if is_font_archive(source):
                    owner_ref = "shared"
                else:
                    inferred_owner = _infer_owner_descriptor(source, movie_descriptors)
                    owner_ref = (
                        inferred_owner.version_ref if inferred_owner is not None else None
                    )
            needs_archive_role = (
                group_kind == "movie_continuous"
                and is_supported_font_archive(source)
                and not is_font_archive(source)
            )
            revised_entries.append(replace(
                original,
                target_path=target,
                requires_review=True,
                evidence=original.evidence + ("按文件类型归为配套资源",),
                presentation_asset=PresentationAssetPlanContract(
                    selected_group.group_id,
                    AssetRole("unknown" if needs_archive_role else "associated"),
                    None,
                    None,
                    "shared" if needs_archive_role else owner_ref,
                ),
            ))

        revised_entries, unresolved_collisions = _resolve_asset_collisions(
            tuple(revised_entries), work_root, package_label=plan.source_root.name
        )

        metadata = dict(plan.metadata)
        context = _completion_context or InventoryCompletionContext(
            origin_plan_fingerprint=self.plan_store.fingerprint(plan),
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
        metadata.update({
            "target_root": str(target_root),
            "season_id": default_season_id if group_kind == "tv_episodes" else None,
            "season_number": default_season_id if group_kind == "tv_episodes" else None,
            "season_source": (
                resolved_season_source if group_kind == "tv_episodes" else "unset"
            ),
            "allowed_target_root": str(target_root),
            "initial_suggested_target_root": str(work_root),
            "mapping_source": "inventory_smart_completion",
            "completion_mode": completion_mode,
            "presentation_assignment_state": (
                "partial" if unresolved_videos else "confirmed"
            ),
            "smart_completion_unresolved_videos": tuple(unresolved_videos),
            "target_root_snapshot": snapshot.to_dict(),
            "target_root_config_fingerprint": snapshot.fingerprint,
            "proposed_work_key": proposed_work_key,
            "canonical_work_root": str(work_root),
            "canonical_naming_required": True,
            "canonical_naming_decision": naming_decision.to_dict(),
            "catalog_work_key": (
                catalog_match.work_key if catalog_match is not None else None
            ),
            "catalog_revision": (
                catalog_match.revision if catalog_match is not None else 0
            ),
            "catalog_match_source": (
                "exact_series_catalog" if series_match is not None
                else "exact_provider" if catalog_match is not None
                else "new_work"
            ),
            "series_key": series_key if group_kind == "tv_episodes" else None,
            "season_binding_id": (
                season_binding_id if group_kind == "tv_episodes" else None
            ),
            "series_catalog_revision": (
                int(series_catalog_revision) if group_kind == "tv_episodes" else 0
            ),
            "owner_version_model": 1 if group_kind == "movie_continuous" else 0,
            "asset_collision_unresolved": tuple(unresolved_collisions),
            "episode_assignment_conflict_sources": tuple(
                episode_assignment_conflicts
            ),
            "inventory_completion_context": context.to_dict(),
        })
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
        revised = self._finalize_completed_movie_plan(revised)
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
            changes = {"recovery_state": "healthy"}
            if group_kind == "tv_episodes":
                if series_key:
                    changes["series_binding"] = {
                        "series_key": str(series_key),
                        "canonical_root": str(metadata.get("canonical_work_root") or ""),
                        "catalog_revision": int(series_catalog_revision),
                        "decision_source": "new_series_intent",
                    }
                if season_binding_id and season_number is not None:
                    changes["season_binding"] = {
                        "season_binding_id": str(season_binding_id),
                        "season_number": int(season_number),
                        "decision_source": str(season_source or "provider_crosswalk"),
                    }
                changes["catalog_link_intent"] = None
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
        if forced_role not in {None, ""}:
            raise PlanReviewError("文件用途只能设为正片候选或特典")
        return preclassify_media(
            item_path, candidate_name, season_id, extra_context_name
        )

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
        if current.metadata.get("mapping_source") != "inventory_smart_completion":
            raise PlanReviewError("当前整理计划尚未选择整理方式")
        reason = str(reason).strip()
        if not reason:
            raise PlanReviewError("确认文件用途必须记录原因")
        context = self._completion_context_for_current(current)
        draft = self._load_inventory_draft(
            task_id, context.origin_plan_fingerprint
        )
        draft_metadata = dict(draft.metadata)
        if "canonical_naming_decision" in current.metadata:
            draft_metadata["canonical_naming_decision"] = current.metadata[
                "canonical_naming_decision"
            ]
        draft = replace(
            draft,
            metadata=draft_metadata,
            work_identity=current.work_identity,
        )
        group = next(
            (item for item in draft.presentation_groups
             if item.group_id == context.origin_group_id),
            None,
        )
        if group is None:
            raise PlanReviewError("原始整理方式已变化，请重新选择整理方式")
        entries_by_id = {entry_id(entry): entry for entry in draft.entries}
        merged = dict(context.manual_role_decisions)
        for identifier, role_value in decisions.items():
            entry = entries_by_id.get(str(identifier))
            if entry is None or entry.media_kind != "video":
                raise PlanReviewError("待确认文件已变化，请刷新后重试")
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
        return self.complete_inventory_draft(
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
            persist_revision=persist_revision,
        )

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
        draft = self._load_inventory_draft(
            current.task_id, context.origin_plan_fingerprint
        )
        draft_metadata = dict(draft.metadata)
        if "canonical_naming_decision" in current.metadata:
            draft_metadata["canonical_naming_decision"] = current.metadata[
                "canonical_naming_decision"
            ]
        draft = replace(
            draft,
            metadata=draft_metadata,
            work_identity=current.work_identity,
        )
        role_decisions = {
            item.source_path: item.role
            for item in context.manual_role_decisions.values()
        }
        completion_kwargs: dict[str, object] = {}
        if binding.backend == "series":
            series = getattr(decision, "series_binding", None) or {}
            if not str(series.get("canonical_root") or "").strip():
                raise PlanReviewError("SeriesIdentity 缺少规范目录，不能安全重规划")
            season = getattr(decision, "season_binding", None) or {}
            season_number = season.get("season_number", context.season_number)
            if season_number is None:
                raise PlanReviewError("SeriesIdentity 重规划缺少已确认季度")
            completion_kwargs.update({
                "series_key": binding.key,
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
            # Explicitly bind the historical draft to the authoritative Work
            # record before replaying the same completion resolver.
            draft_metadata = dict(draft.metadata)
            draft_metadata.update({
                "catalog_work_key": binding.key,
                "catalog_link_decision": "explicit_user",
            })
            draft = replace(draft, metadata=draft_metadata)
            completion_kwargs.update({
                "season_number": context.season_number,
                "season_source": context.season_source,
            })
        return self.complete_inventory_draft(
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

    def _completion_context_for_current(
        self, current: RenamePlan
    ) -> InventoryCompletionContext:
        raw = current.metadata.get("inventory_completion_context")
        if raw is not None:
            return InventoryCompletionContext.from_metadata(raw)
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
        return self._persist_revision(revised) if persist_revision else revised

    def link_existing_catalog_work(
        self,
        task_id: str,
        expected_fingerprint: str,
        *,
        work_key: str,
        reason: str,
    ) -> RenamePlan:
        """Persist an explicit cross-provider decision to reuse one movie work."""

        plan = self._current_plan(task_id, expected_fingerprint)
        reason = str(reason).strip()
        if not reason:
            raise PlanReviewError("关联既有电影必须记录理由")
        work = plan.work_identity
        if work is None or work.status != "confirmed":
            raise PlanReviewError("请先确认当前任务的作品身份")
        try:
            match = self.work_catalog.get_match(work_key)
        except WorkCatalogError as exc:
            raise PlanReviewError(str(exc)) from exc
        group_kinds = {group.kind for group in plan.presentation_groups}
        if len(group_kinds) != 1:
            raise PlanReviewError("当前计划的观看形式尚未唯一确定")
        group_kind = next(iter(group_kinds))
        suggestions = self.work_catalog.suggestions(
            work.title, work.year, work.primary_provider.aliases,
            group_kind=group_kind,
            include_related_series=(group_kind == "tv_episodes"),
        )
        exact = self.work_catalog.find_exact(
            work.primary_provider, group_kind=group_kind
        )
        allowed_keys = {item.work_key for item in suggestions}
        if exact is not None:
            allowed_keys.add(exact.work_key)
        if work_key not in allowed_keys:
            raise PlanReviewError("所选既有作品不在当前标题/年份候选中")
        if match.group_kind != group_kind:
            raise PlanReviewError("既有作品登记的观看形式与当前整理方式不一致")
        metadata = dict(plan.metadata)
        old_root_value = str(metadata.get("canonical_work_root") or "").strip()
        revised_entries = plan.entries
        if old_root_value and _path_key(Path(old_root_value)) != _path_key(match.canonical_root):
            rebased = tuple(
                replace(
                    entry,
                    target_path=_rebase_movie_target(
                        entry.target_path, Path(old_root_value), match.canonical_root
                    ),
                    collision_resolution=None,
                    evidence=entry.evidence + ("用户确认归入既有本地作品目录",),
                )
                for entry in plan.entries
            )
            revised_entries, unresolved = _resolve_asset_collisions(
                rebased, match.canonical_root, package_label=plan.source_root.name
            )
            metadata["asset_collision_unresolved"] = tuple(unresolved)
        metadata.update({
            "catalog_work_key": match.work_key,
            "catalog_revision": match.revision,
            "canonical_work_root": str(match.canonical_root),
            "catalog_link_decision": "explicit_user",
            "catalog_link_reason": reason,
            "catalog_allow_outside_current_root": True,
        })
        revised = replace(
            plan,
            entries=tuple(revised_entries),
            metadata=metadata,
            revision_parent_fingerprint=expected_fingerprint,
            revision_action="link_existing_catalog_work",
            revision_reason=reason,
        )
        recognition = RecognitionDecisionInput(
            task_id=task_id,
            work_identity=work,
            linked_provider_identities=tuple(
                item for item in plan.linked_provider_identities
                if item.status == "confirmed"
            ),
            season_number=None,
            season_source="unset",
            layout_mode=(
                "tv_episodes" if group_kind == "tv_episodes"
                else "movie_versions"
            ),
        )

        def transform_catalog_link(state):
            return replace(
                state,
                catalog_link_intent={
                    "work_key": match.work_key,
                    "catalog_revision": match.revision,
                },
                series_binding=None,
                season_binding=None,
                recovery_state="healthy",
            )

        return self._persist_recognition_revision(
            revised,
            recognition,
            state_transform=transform_catalog_link,
        )

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
        return self._persist_revision(revised)

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
        matching = [
            entry for entry in plan.entries if entry_id(entry) == selected_entry_id
        ]
        if len(matching) != 1:
            raise PlanReviewError("待忽略条目不存在或不唯一")
        selected = matching[0]
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

    def confirm_font_archive(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_id: str,
        *,
        reason: str,
        persist_revision: bool = True,
    ) -> RenamePlan:
        """Confirm one packed .7z/.zip as a shared movie or TV font asset."""

        plan = self._current_plan(task_id, expected_fingerprint)
        group_kinds = {group.kind for group in plan.presentation_groups}
        is_movie = group_kinds == {"movie_continuous"}
        is_tv = group_kinds == {"tv_episodes"}
        if not is_movie and not is_tv:
            raise PlanReviewError("当前文件不在可确认的整理审核中")
        if (
            is_movie
            and plan.metadata.get("mapping_source") != "inventory_smart_completion"
        ):
            raise PlanReviewError("当前文件不在电影整理审核中")
        matching = [
            (index, entry) for index, entry in enumerate(plan.entries)
            if entry_id(entry) == selected_entry_id
        ]
        if len(matching) != 1:
            raise PlanReviewError("待确认字体包不存在或不唯一")
        index, original = matching[0]
        if (
            original.media_kind != "associated"
            or not is_supported_font_archive(original.source_path)
        ):
            raise PlanReviewError("仅支持把 .7z 或 .zip 明确设为字体包")
        asset = original.presentation_asset
        if asset is None:
            raise PlanReviewError("字体包缺少电影呈现分组")
        entries = list(plan.entries)
        entries[index] = replace(
            original,
            role="font",
            requires_review=True,
            evidence=original.evidence + (str(reason).strip(),),
            presentation_asset=PresentationAssetPlanContract(
                asset.presentation_group,
                AssetRole("associated"),
                None,
                None,
                "shared",
            ),
        )
        revised = replace(
            plan,
            entries=tuple(entries),
            revision_parent_fingerprint=expected_fingerprint,
            revision_action=f"confirm_font_archive:{selected_entry_id}",
            revision_reason=str(reason).strip(),
        )
        if is_movie:
            revised = self._finalize_completed_movie_plan(revised)
        else:
            # Import lazily to avoid a module cycle while ensuring review and
            # production planning consume the same SeriesCatalog-aware font
            # finalizer rather than maintaining a second projection.
            from .process import _freeze_tv_font_asset_contract

            revised = _freeze_tv_font_asset_contract(revised)
        return self._persist_revision(revised) if persist_revision else revised

    def confirm_movie_font_archive(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_id: str,
        *,
        reason: str,
        persist_revision: bool = True,
    ) -> RenamePlan:
        """Compatibility entry for existing movie review callers."""

        return self.confirm_font_archive(
            task_id,
            expected_fingerprint,
            selected_entry_id,
            reason=reason,
            persist_revision=persist_revision,
        )

    def apply_provider_special_decision(
        self,
        task_id: str,
        expected_fingerprint: str,
        selected_entry_id: str,
        *,
        decision: Mapping[str, object],
        reason: str,
        persist_revision: bool = True,
    ) -> RenamePlan:
        """Project one explicit official-special or keep-extra decision."""

        plan = self._current_plan(task_id, expected_fingerprint)
        if {group.kind for group in plan.presentation_groups} != {"tv_episodes"}:
            raise PlanReviewError("特别篇决定只适用于按季和集整理")
        matching = [
            (index, entry) for index, entry in enumerate(plan.entries)
            if entry_id(entry) == selected_entry_id
        ]
        if len(matching) != 1:
            raise PlanReviewError("待确认特别篇文件不存在或不唯一")
        candidates = plan.metadata.get("provider_special_candidates")
        rows = (
            candidates.get(selected_entry_id)
            if isinstance(candidates, Mapping) else None
        )
        if not isinstance(rows, (list, tuple)) or not rows:
            raise PlanReviewError("当前文件没有冻结的官方特别篇候选")
        action = str(decision.get("action") or "")
        if action not in {"confirmed_official_special", "unmatched_extra"}:
            raise PlanReviewError("特别篇决定无效")
        index, original = matching[0]
        group_id = plan.presentation_groups[0].group_id
        work_root_value = str(
            plan.metadata.get("canonical_work_root")
            or plan.metadata.get("target_root")
            or ""
        ).strip()
        if not work_root_value:
            raise PlanReviewError("当前计划缺少作品目标目录")
        work_root = Path(work_root_value)
        metadata = dict(plan.metadata)
        stored = dict(metadata.get("provider_special_decisions") or {})
        stored[selected_entry_id] = dict(decision)
        metadata["provider_special_decisions"] = stored
        entries = list(plan.entries)
        if action == "confirmed_official_special":
            expected = {
                key: decision.get(key) for key in (
                    "provider_namespace", "provider_entity_type", "provider_id",
                    "season_number", "episode_number", "provider_episode_id",
                )
            }
            if not any(
                isinstance(row, Mapping)
                and all(row.get(key) == value for key, value in expected.items())
                for row in rows
            ):
                raise PlanReviewError("所选官方特别篇候选已变化，请刷新后重试")
            season = int(decision["season_number"])
            episode = int(decision["episode_number"])
            target = (
                work_root / f"Season {season:02d}"
                / f"{work_root.name} S{season:02d}E{episode:02d}"
                f"{original.source_path.suffix.lower()}"
            )
            entries[index] = replace(
                original,
                target_path=target,
                season_id=season,
                episode_id=episode,
                role="main_episode",
                requires_review=True,
                evidence=original.evidence + (str(reason).strip(),),
                presentation_asset=PresentationAssetPlanContract(
                    group_id,
                    AssetRole("content_version"),
                    None,
                    build_default_version_descriptor(group_id, season, episode),
                ),
            )
            for subtitle_index, subtitle_entry in enumerate(entries):
                subtitle = subtitle_entry.subtitle
                if (
                    subtitle is None
                    or subtitle.associated_video_source_path is None
                    or _path_key(subtitle.associated_video_source_path)
                    != _path_key(original.source_path)
                ):
                    continue
                subtitle_target = format_subtitle_target_fields(
                    target_video=target,
                    source_subtitle=subtitle_entry.source_path,
                    primary_language=subtitle.primary_language or "",
                    script=subtitle.script,
                    track_title_tokens=subtitle.track_title_tokens,
                    flags=subtitle.flags,
                )
                entries[subtitle_index] = replace(
                    subtitle_entry,
                    target_path=subtitle_target,
                    season_id=season,
                    episode_id=episode,
                    requires_review=True,
                )
        else:
            current_season = int(plan.metadata.get("season_number") or 1)
            stem = normalize_target_stem(original.source_path.stem, work_root.name)
            target = (
                work_root / f"Season {current_season:02d}" / JELLYFIN_EXTRAS_DIR
                / f"{stem or original.source_path.stem}{original.source_path.suffix.lower()}"
            )
            entries[index] = replace(
                original,
                target_path=target,
                season_id=current_season,
                episode_id=None,
                role="disc_extra",
                requires_review=True,
                evidence=original.evidence + (str(reason).strip(),),
                presentation_asset=PresentationAssetPlanContract(
                    group_id, AssetRole("extra", "other"), None, None
                ),
            )
        resolved, unresolved = _resolve_asset_collisions(
            tuple(entries), work_root, package_label=plan.source_root.name
        )
        metadata["asset_collision_unresolved"] = tuple(unresolved)
        revised = replace(
            plan,
            entries=tuple(resolved),
            metadata=metadata,
            revision_parent_fingerprint=expected_fingerprint,
            revision_action=f"provider_special:{selected_entry_id}:{action}",
            revision_reason=str(reason).strip(),
        )
        return self._persist_revision(revised) if persist_revision else revised

    def _persist_revision(self, revised: RenamePlan) -> RenamePlan:
        revised = self._finalize_completed_movie_plan(revised)

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
        revised = self._finalize_completed_movie_plan(revised)
        _decision, projected = commit_recognition_projection(
            self.decision_state_store,
            self.plan_store,
            recognition,
            revised,
            state_transform=state_transform,
            task_terminal=self._revision_task_terminal(revised.task_id),
        )
        return self._record_revision(projected)

    def _revision_task_terminal(self, task_id: str) -> TaskTerminalMutation:

        def update_task(_decision, projected, current):
            verification = PlanVerifier().verify(projected)
            status = (
                "need_confirm"
                if verification.status.value == "accepted"
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
            if projected.metadata.get("mapping_source") == "inventory_smart_completion":
                current["error"] = None
            return current

        return TaskTerminalMutation.prepare(
            self.task_root,
            task_id,
            update_task,
        )

    def _record_revision(self, revised: RenamePlan) -> RenamePlan:
        self._save(PlanReviewDecision(
            revised.task_id,
            self.plan_store.fingerprint(revised),
        ))
        return revised

    def _finalize_completed_movie_plan(self, plan: RenamePlan) -> RenamePlan:
        """Freeze strict movie-font routing at the single Plan persistence exit."""

        metadata = dict(plan.metadata)
        if (
            metadata.get("mapping_source") != "inventory_smart_completion"
            or {group.kind for group in plan.presentation_groups} != {"movie_continuous"}
            or plan.work_identity is None
            or plan.work_identity.status != "confirmed"
        ):
            return plan
        fonts = [
            entry for entry in plan.entries
            if is_font_archive(entry.source_path) or (
                entry.role == "font" and is_supported_font_archive(entry.source_path)
            )
        ]
        if not fonts:
            if not {
                "associated_asset_manifest", "work_font_intent"
            }.intersection(metadata):
                return plan
            metadata.pop("associated_asset_manifest", None)
            metadata.pop("work_font_intent", None)
            return replace(plan, metadata=metadata)
        root_value = str(metadata.get("canonical_work_root") or "").strip()
        work_key = str(
            metadata.get("catalog_work_key")
            or metadata.get("proposed_work_key")
            or ""
        )
        if not root_value or not work_key:
            return plan
        canonical_root = Path(root_value)
        unresolved = [
            item for item in metadata.get("asset_collision_unresolved", ())
            if not _unresolved_references_sources(item, {entry.source_path for entry in fonts})
        ]
        evidence_by_source = {}
        for entry in fonts:
            try:
                evidence_by_source[entry.source_path] = fingerprint_file(entry.source_path)
            except AssetCollisionError as exc:
                unresolved.append({
                    "code": "movie_font_source_evidence_unavailable",
                    "source_path": str(entry.source_path),
                    "target_path": str(entry.target_path),
                    "detail": str(exc),
                })
        identities = {
            (evidence.sha256, evidence.size)
            for evidence in evidence_by_source.values()
        }
        catalog_exists = (self.work_catalog.root / f"{work_key}.json").is_file()
        try:
            intent_store = WorkIntentStore(self.work_catalog.root)
            current_content_keys = tuple(
                make_content_key(size=size, digest=digest)
                for digest, size in identities
            )
            derived_intent_key = derive_work_intent_key(
                primary_provider=plan.work_identity.primary_provider,
                group_kind="movie_continuous",
            )
            if (
                catalog_exists
                and metadata.get("catalog_work_key")
                and derived_intent_key != work_key
            ):
                intent_snapshot = intent_store.load(work_key)
                if not set(current_content_keys).issubset(
                    set(intent_snapshot.content_keys)
                ):
                    raise WorkIntentError(
                        "既有作品登记未授权当前字体内容"
                    )
            else:
                intent_snapshot = intent_store.observe(
                    primary_provider=plan.work_identity.primary_provider,
                    group_kind="movie_continuous",
                    content_keys=current_content_keys,
                )
        except WorkIntentError as exc:
            raise PlanReviewError(f"无法冻结作品级字体意图：{exc}") from exc
        primary_content_key = intent_snapshot.primary_content_key
        registered_locations = set()
        try:
            if catalog_exists:
                registered_locations = {
                    _path_key(path)
                    for path in self.work_catalog.validated_font_locations(work_key)
                }
        except WorkCatalogError as exc:
            for entry in fonts:
                unresolved.append({
                    "code": "movie_font_catalog_evidence_conflict",
                    "source_path": str(entry.source_path),
                    "target_path": str(entry.target_path),
                    "detail": str(exc),
                })
        revised_entries = []
        manifest_assets = []
        planned_identities = set()
        planned_targets = set()
        for entry in plan.entries:
            if entry not in fonts:
                revised_entries.append(entry)
                continue
            source_evidence = evidence_by_source.get(entry.source_path)
            if source_evidence is None:
                revised_entries.append(entry)
                continue
            identity = (source_evidence.sha256, source_evidence.size)
            content_key = make_content_key(
                size=source_evidence.size,
                digest=source_evidence.sha256,
            )
            trusted = None
            try:
                if catalog_exists:
                    trusted = self.work_catalog.resolve_font_location(
                        work_key,
                        size=source_evidence.size,
                        digest=source_evidence.sha256,
                    )
                primary_target = canonical_font_archive_target(
                    canonical_root,
                    source_evidence.sha256,
                    disambiguate=False,
                    archive_suffix=entry.source_path.suffix,
                )
                target = trusted or canonical_font_archive_target(
                    canonical_root,
                    source_evidence.sha256,
                    disambiguate=(
                        content_key != primary_content_key
                        or _path_key(primary_target) in registered_locations
                    ),
                    archive_suffix=entry.source_path.suffix,
                )
            except (WorkCatalogError, FontArchiveContractError) as exc:
                unresolved.append({
                    "code": "movie_font_catalog_evidence_conflict",
                    "source_path": str(entry.source_path),
                    "target_path": str(entry.target_path),
                    "detail": str(exc),
                })
                revised_entries.append(entry)
                continue
            target_key = _path_key(target)
            if identity in planned_identities or target_key in planned_targets:
                unresolved.append({
                    "code": "movie_font_duplicate_content",
                    "source_path": str(entry.source_path),
                    "target_path": str(target),
                    "detail": "同一电影计划包含重复字体内容",
                })
            planned_identities.add(identity)
            planned_targets.add(target_key)
            collision = None
            if trusted is not None:
                try:
                    collision = build_reuse_resolution(entry.source_path, target)
                except AssetCollisionError as exc:
                    unresolved.append({
                        "code": "movie_font_catalog_evidence_conflict",
                        "source_path": str(entry.source_path),
                        "target_path": str(target),
                        "detail": str(exc),
                    })
            elif os.path.lexists(target):
                unresolved.append({
                    "code": "movie_font_unregistered_target_conflict",
                    "source_path": str(entry.source_path),
                    "target_path": str(target),
                    "detail": "字体目标已存在但没有可信作品登记",
                })
            group_id = plan.presentation_groups[0].group_id
            revised = replace(
                entry,
                target_path=target,
                media_kind="associated",
                role="font",
                collision_resolution=collision,
                presentation_asset=PresentationAssetPlanContract(
                    group_id,
                    AssetRole("associated"),
                    None,
                    None,
                    "shared",
                ),
            )
            revised_entries.append(revised)
            manifest_assets.append({
                "role": "fonts",
                "scope": "work",
                "season_number": None,
                "source_path": str(revised.source_path),
                "target_path": str(revised.target_path),
                "source_size": source_evidence.size,
                "source_sha256": source_evidence.sha256,
            })
        metadata["associated_asset_manifest"] = {
            "schema_version": 1,
            "scope": "work",
            "catalog_binding": {"backend": "work", "key": work_key},
            "assets": tuple(manifest_assets),
        }
        metadata["work_font_intent"] = intent_snapshot.to_plan_contract(
            store_root=intent_store.root,
            assets=(
                {
                    "source_path": item["source_path"],
                    "target_path": item["target_path"],
                    "content_key": make_content_key(
                        size=int(item["source_size"]),
                        digest=str(item["source_sha256"]),
                    ),
                    "action": WORK_FONT_CONDITIONAL_ACTION,
                }
                for item in manifest_assets
            ),
        )
        metadata["asset_collision_unresolved"] = tuple(unresolved)
        return replace(plan, entries=tuple(revised_entries), metadata=metadata)

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

        plan = self._current_plan(task_id, expected_fingerprint)
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
        matcher = _subtitle_matcher(contract)
        impacted = [selected_index]
        if parsed_scope == SubtitleOverrideScope.SERIES:
            impacted = [
                index for index, entry in enumerate(plan.entries)
                if entry.media_kind == "subtitle"
                and entry.subtitle is not None
                and _subtitle_matcher(entry.subtitle) == matcher
            ]
        if not impacted:
            raise PlanReviewError("字幕规则没有命中任何当前 Plan 条目")

        scope_id = (
            f"{plan.task_id}:{selected_entry_id}:{expected_fingerprint}"
            if parsed_scope == SubtitleOverrideScope.FILE
            else plan.series_scope.scope_id  # type: ignore[union-attr]
        )
        outcome = SubtitleRuleOutcome(
            primary_language=str(primary_language),
            script=script,
            region_hint=region_hint,
            additional_languages=tuple(str(item) for item in additional_languages),
            track_title_tokens=tuple(str(item) for item in track_title_tokens),
            flags=tuple(str(item) for item in flags),
            unknown_dispositions=disposition_tuple,
        )
        rule_id = uuid.uuid4().hex
        unsigned = SubtitleOverrideRule(
            rule_id=rule_id,
            scope=parsed_scope,
            scope_id=scope_id,
            base_plan_fingerprint=expected_fingerprint,
            selected_entry_id=selected_entry_id,
            matcher=matcher,
            outcome=outcome,
            reason=str(reason).strip(),
            rule_fingerprint="",
        )
        rule_fingerprint = _rule_fingerprint(unsigned)
        rule = replace(unsigned, rule_fingerprint=rule_fingerprint)

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

        revised = replace(
            plan,
            entries=tuple(entries),
            subtitle_override_rules=plan.subtitle_override_rules + (rule,),
            revision_parent_fingerprint=expected_fingerprint,
            revision_action=f"apply_subtitle_rule:{rule.rule_id}",
            revision_reason=str(reason).strip(),
        )
        verification = PlanVerifier().verify(
            revised, validate_projection_receipt=False
        )
        if verification.issues:
            codes = ", ".join(issue.code for issue in verification.issues)
            raise PlanReviewError(f"字幕人工规则未通过 Verifier：{codes}")
        if _persist:
            revised = self._persist_revision(revised)
        return revised

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
        try:
            base = self.plan_store.load_version(task_id, rules[0].base_plan_fingerprint)
        except PlanStoreError as exc:
            raise PlanReviewError(f"无法读取字幕规则基线 Plan：{exc}") from exc
        revised = replace(
            base,
            revision_parent_fingerprint=expected_fingerprint,
            revision_action=f"revert_subtitle_rule:{rule_id}",
            revision_reason=str(reason).strip(),
        )
        verification = PlanVerifier().verify(
            revised, validate_projection_receipt=False
        )
        base_issue_codes = tuple(
            issue.code for issue in PlanVerifier().verify(base).issues
        )
        revised_issue_codes = tuple(issue.code for issue in verification.issues)
        if revised_issue_codes != base_issue_codes:
            codes = ", ".join(issue.code for issue in verification.issues)
            raise PlanReviewError(f"撤销引入了新的 Verifier 问题：{codes}")
        return self._persist_revision(revised)

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
) -> tuple[Optional[str], Optional[str]]:
    """Parse one trusted evidence layer and reject same-layer conflicts."""

    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    token_text = re.sub(r"[^a-z0-9.+]+", " ", normalized)
    resolutions = set()
    if re.search(r"(?<!\d)(2160p|4k)(?!\d)", normalized):
        resolutions.add("2160p")
    if re.search(r"(?<!\d)1080[pi]?(?!\d)", normalized):
        resolutions.add("1080p")
    if re.search(r"(?<!\d)720[pi]?(?!\d)", normalized):
        resolutions.add("720p")
    if len(resolutions) > 1:
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
            if re.search(r"(?:p?8[ ._-]*1|8\.1)", normalized)
            else "Dolby Vision"
        )
    if re.search(r"(?<![a-z0-9])hdr10\s*\+(?![a-z0-9])", normalized):
        dynamic_ranges["hdr10"] = "HDR10+"
    elif re.search(r"(?<![a-z0-9])hdr10(?![a-z0-9])", normalized):
        dynamic_ranges["hdr10"] = "HDR10"
    if re.search(r"\bsdr\b", token_text):
        dynamic_ranges["sdr"] = "SDR"
    if len(dynamic_ranges) > 1:
        raise PlanReviewError(
            f"电影版本规格证据冲突：{layer_label}包含多个动态范围 token"
        )
    return (
        next(iter(resolutions), None),
        next(iter(dynamic_ranges.values()), None),
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
    if len(dynamic_families) > 1:
        raise PlanReviewError(
            "电影版本规格证据冲突：文件名与冻结 source_root 范围内目录"
            "给出不同动态范围"
        )
    dynamic_values = {value for value, _label in dynamic_range_evidence}
    if "Dolby Vision P8.1" in dynamic_values:
        dynamic_range = "Dolby Vision P8.1"
    elif "HDR10+" in dynamic_values:
        dynamic_range = "HDR10+"
    else:
        dynamic_range = next(iter(dynamic_values), None)
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
    """Freeze identical-content reuse or choose a version-scoped new target."""

    descriptors = {
        asset.version_descriptor.version_ref: asset.version_descriptor
        for entry in entries
        if (asset := entry.presentation_asset) is not None
        and asset.version_descriptor is not None
        and asset.version_descriptor.version_ref is not None
    }
    revised = []
    unresolved = []
    for entry in entries:
        if not os.path.lexists(entry.target_path):
            revised.append(entry)
            continue
        asset = entry.presentation_asset
        if (
            asset is None
            or asset.asset_role.kind not in {"extra", "associated"}
            or entry.media_kind == "subtitle"
        ):
            unresolved.append(entry.source_path.name)
            revised.append(entry)
            continue
        resolution = _reuse_resolution(entry.source_path, entry.target_path)
        if resolution is not None:
            revised.append(replace(
                entry,
                collision_resolution=resolution,
                evidence=entry.evidence + (
                    "既有目标与本次源文件完整 SHA-256 相同，将复用已有文件",
                ),
            ))
            continue
        owner = asset.owner_version_ref
        descriptor = descriptors.get(owner) if owner not in {None, "shared"} else None
        label = _safe_media_name(descriptor.display_label or "") if descriptor else ""
        package = _safe_media_name(package_label or "")
        candidate = None
        if label and asset.asset_role.kind == "associated":
            try:
                relative = entry.target_path.relative_to(work_root / "Associated")
            except ValueError:
                relative = Path(entry.target_path.name)
            candidate = work_root / "Associated" / label / relative
        elif label and asset.asset_role.kind == "extra":
            if _normalized_key(label) not in _normalized_key(entry.target_path.stem):
                candidate = entry.target_path.with_name(
                    f"{entry.target_path.stem} - {label}{entry.target_path.suffix}"
                )
        if candidate is not None:
            if not os.path.lexists(candidate):
                revised.append(replace(
                    entry,
                    target_path=candidate,
                    collision_resolution=None,
                    evidence=entry.evidence + (
                        f"同名资产内容不同，按版本 {label} 保存",
                    ),
                ))
                continue
            candidate_reuse = _reuse_resolution(entry.source_path, candidate)
            if candidate_reuse is not None:
                revised.append(replace(
                    entry,
                    target_path=candidate,
                    collision_resolution=candidate_reuse,
                    evidence=entry.evidence + (
                        f"版本目录中的既有资产与来源包内容相同，将复用已有文件",
                    ),
                ))
                continue

        package_candidate = None
        if package and asset.asset_role.kind == "associated":
            try:
                relative = entry.target_path.relative_to(work_root / "Associated")
            except ValueError:
                relative = Path(entry.target_path.name)
            package_candidate = (
                work_root / "Associated" / label / package / relative
                if label else work_root / "Associated" / "Packages" / package / relative
            )
        elif package and asset.asset_role.kind == "extra":
            suffix = f" - {label}" if label else ""
            package_candidate = entry.target_path.with_name(
                f"{entry.target_path.stem}{suffix} - {package}{entry.target_path.suffix}"
            )
        if package_candidate is not None:
            if not os.path.lexists(package_candidate):
                revised.append(replace(
                    entry,
                    target_path=package_candidate,
                    collision_resolution=None,
                    evidence=entry.evidence + (
                        "同名资产内容不同，按来源包建立独立目标",
                    ),
                ))
                continue
            package_reuse = _reuse_resolution(entry.source_path, package_candidate)
            if package_reuse is not None:
                revised.append(replace(
                    entry,
                    target_path=package_candidate,
                    collision_resolution=package_reuse,
                    evidence=entry.evidence + (
                        "同一来源包目标内容相同，将复用已有文件",
                    ),
                ))
                continue
        unresolved.append(entry.source_path.name)
        revised.append(entry)
    return revised, unresolved


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
