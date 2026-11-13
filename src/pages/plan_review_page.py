"""Responsive NiceGUI review workspace for persisted rename plans."""

from __future__ import annotations
from ..queue_runtime import ui_disk_call

from .tv_content_actions import await_result

from .progress_view import bar_progress

from src.component.dialog_frame import dialog_card

from ..rename.provider_selection_authority import is_confirmed_movie_plan

import asyncio
from dataclasses import dataclass, replace
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..config.config_manager import cm, effective_tmdb_api_key

from .plan_completion_status import completion_status, entry_status
from nicegui import ui

from ..logger import logger
from ..rename.asset_operation import reuse_display_asset_reads

from ..component.local_file_picker import local_file_picker
from ..element.red import notify
from ..rename.plan_review import (
    PlanReviewError,
    PlanReviewService,
    ReviewStatus,
    entry_id,
)
from ..rename.plan_execution import PlanExecutionError
from ..rename import plan_execution as plan_execution_contract
from ..rename.plan_store import PlanStore, PlanStoreError
from ..rename.special_catalog import plain
from ..rename.projection_authority import (
    ProjectionAuthorityError,
    RecognitionDecisionInput,
    TaskEpisodeOwnership,
    TaskTerminalMutation,
    UnresolvedRecognitionInput,
    commit_recognition_projection,
)
from ..rename.task_batch_scope import (
    BatchTaskSourceRequest,
    SourceIdentity as BatchSourceIdentity,
    TaskBatchScopeError,
    TaskBatchScopeManifestStore,
    TaskSourceScope,
)
from ..rename.tv_planning_authority import (
    FrozenTVPlanningObservationBatch,
    ObservationBlobStore,
    TVPlanningAuthority,
    TVPlanningAuthorityBuilder,
    TVPlanningAuthorityError,
    TVPlanningAuthorityInput,
    TVPlanningUserDecision,
)
from ..rename.decision_state import (
    DecisionCommandService,
    DecisionStateError,
    DecisionStateStore,
)
from ..rename.series_catalog import SeriesCatalog, SeriesCatalogError
from ..rename.recovery_service import RecoveryService, untrusted_projection_snapshot
from ..rename.recovery_transaction import (
    CatalogDetachService,
    RecoveryTransactionError,
    RecoveryTransactionStore,
)
from ..rename.plan_verifier import PlanVerifier
from ..rename.provider_search import resolve_provider_item_url
from ..rename.provider_selection_authority import (
    provider_candidate_group_kind,
    provider_selection_capability,
    task_library_domain_from_task,
    task_media_kind_from_context,
)
from ..rename.provider_link_decision import (
    build_provider_identity_selection_receipt, build_provider_subject_boundary_failure,
    is_provider_identity_boundary, provider_subject_cohort_request_fingerprint,
    provider_link_review_summary,
    resolve_provider_subject_boundary_decision,
    selected_provider_special_observation, stage_provider_link_series_decision,
)
from ..rename.provider_link_resolver import (
    ProviderCohortAnchorError,
    trusted_relation_tmdb_anchor,
)
from ..rename import provider_special_evidence as provider_special
from ..rename.review_operation import (
    TaskWorkflowState,
    build_review_operation,
    current_provider_selection_review_operation,
    provider_selection_awaits_user,
    provider_selection_command_is_capable,
)
from ..rename.successor_authority import (
    CohortAuthorityDraft,
    SuccessorAuthorityError,
)
from ..rename.inventory_plan import InventoryDraftError, build_inventory_review_draft
from ..rename.trans import is_dry_run_enabled
from ..rename.rename_plan import (
    RenamePlan,
    WorkIdentity,
)
from ..rename.presentation import provider_identity_from_metadata
from ..utils.path import (
    DECISION_STATE_PATH,
    PLAN_PATH,
    PLAN_REVIEW_PATH,
    RECORD_PATH,
    RECOVERY_TRANSACTION_PATH,
    SERIES_CATALOG_PATH,
    TASK_PATH,
    WORK_CATALOG_PATH,
)
from ..utils.utils import (
    get_task,
    mutate_task as update_task_record,
    write_task as save_task_record,
)
from ..task_queue import task_queue
from .add_task_page import MEDIA_OPTIONS, media_flags
from .exceptional_episode_review import (
    EXCEPTIONAL_ISSUE_GUIDANCE,
    render_cross_work_partition_action as _render_cross_work_partition_action,
)
from .review_operation_view import (
    ReviewOperationCallbacks,
    render_review_operation,
)
from .ui_terms import RENAME_PLAN_TERM, localize_plan_term


# Compatibility patch points for older incident-shaped tests.
mutate_task = update_task_record
write_task = save_task_record


def _update_task_record(*args, **kwargs):
    return globals()["mutate_task"](*args, **kwargs)
def _save_task_record(*args, **kwargs):
    return globals()["write_task"](*args, **kwargs)
def _provider_selection_task_media_kind(dialog: object) -> str:
    plan = getattr(dialog, "plan", None)
    return task_media_kind_from_context(getattr(dialog, "task_data", {}), getattr(plan, "metadata", {}))


def _candidate_capability(dialog, candidate):
    from ..rename.provider_selection_authority import selection_task_context, task_media_kind_from_task
    task = getattr(dialog, 'task_data', {})
    selected = selection_task_context(task, getattr(dialog, 'plan', None),
                                      getattr(dialog, 'task_decision', None), candidate)
    return provider_selection_capability(candidate,
        group_kind=provider_candidate_group_kind(candidate) or '',
        task_media_kind=(task_media_kind_from_task(selected) if selected is not task
                         else _provider_selection_task_media_kind(dialog)),
        task_library_domain=task_library_domain_from_task(selected))


MEDIA_LABELS = {
    "video": "视频", "subtitle": "字幕", "associated": "关联文件", "other": "其他"
}
ROLE_LABELS = {
    "main_episode": "正片",
    "movie_content": "正片",
    "content_version": "正片",
    "subtitle": "字幕",
    "episode_like_bonus": "OVA / 特别篇候选",
    "extra": "特典",
    "extra_subtitle": "特典字幕",
    "associated": "配套文件",
    "unknown": "待确认",
    "disc_extra": "碟片附加内容",
    "": "未分类",
}


def _tv_observation_fingerprint(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        {"domain": "bar.tv-planning-observation", "value": dict(value)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _trusted_relation_tmdb_anchor(
    evidence: Mapping[str, object],
    tmdb_tv_id: str,
) -> Mapping[str, object]:
    """Adapt the shared resolver gate to a user-facing Decision error."""

    subject_batch = evidence.get("subject_observations")
    if not isinstance(subject_batch, Mapping):
        raise DecisionStateError("季度关系缺少冻结的电视剧资料")
    try:
        return trusted_relation_tmdb_anchor(subject_batch, tmdb_tv_id)
    except ProviderCohortAnchorError as exc:
        if exc.reason_code == "cohort_anchor_snapshot_conflict":
            message = "季度关系中的电视剧锚点及季度集数互相矛盾"
        elif exc.reason_code == "subject_candidate_evidence_missing":
            message = "季度关系缺少可信的电视剧锚点"
        else:
            message = "季度关系的冻结资料不完整或异常，请重新读取作品资料"
        raise DecisionStateError(message) from exc


def _collect_tv_task_source_scope(
    plan: RenamePlan,
    task_data: Mapping[str, object],
    *,
    expected_revision: int,
    request_fingerprint: str,
) -> TaskSourceScope:
    """Freeze file identity outside the pure Builder and reserve legacy input."""

    identities = []
    for source in sorted(plan.expected_sources, key=lambda item: str(item).casefold()):
        resolved = Path(source).resolve(strict=True)
        stat = resolved.stat()
        if int(stat.st_ino) <= 0:
            raise TVPlanningAuthorityError("source physical identity 不可用")
        identities.append(BatchSourceIdentity(
            source_path=str(resolved),
            location_key=str(resolved).casefold(),
            physical_key=f"device:{int(stat.st_dev)}:inode:{int(stat.st_ino)}",
            size=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
        ))
    if not identities:
        raise TVPlanningAuthorityError("TV task source scope 为空")

    stored_scope = task_data.get("task_source_scope")
    if not isinstance(stored_scope, Mapping):
        raise TVPlanningAuthorityError(
            "任务缺少current source reservation，必须重新创建任务"
        )
    scope = TaskSourceScope.from_mapping(stored_scope)
    if scope.task_id != plan.task_id or scope.media_domain != "tv":
        raise TVPlanningAuthorityError("已冻结 task source scope 归属不一致")
    current_by_path = {item.location_key: item for item in identities}
    frozen_by_path = {
        item.location_key: item for item in scope.source_identities
    }
    for key, current in current_by_path.items():
        frozen = frozen_by_path.get(key)
        if frozen is None or (
            frozen.physical_key,
            frozen.size,
            frozen.mtime_ns,
        ) != (
            current.physical_key,
            current.size,
            current.mtime_ns,
        ):
            raise TVPlanningAuthorityError("已冻结 task source scope 已漂移")
    store = TaskBatchScopeManifestStore(
        Path(DECISION_STATE_PATH).parent / "task_batch_scope"
    )
    try:
        store.require_active_scope(
            scope,
            batch_id=str(task_data.get("batch_id") or "").strip() or None,
        )
    except TaskBatchScopeError as exc:
        raise TVPlanningAuthorityError(
            "TV task source reservation/lineage 不再active"
        ) from exc
    return scope


def _regular_tv_entry_episode(
    plan: RenamePlan,
    entry,
) -> Optional[int]:
    if isinstance(entry.episode_id, int) and not isinstance(entry.episode_id, bool):
        return int(entry.episode_id)
    if entry.media_kind == "subtitle" and entry.subtitle is not None:
        associated = entry.subtitle.associated_video_source_path
        if associated is not None:
            match = next(
                (
                    item for item in plan.entries
                    if item.source_path == associated
                    and isinstance(item.episode_id, int)
                    and not isinstance(item.episode_id, bool)
                ),
                None,
            )
            if match is not None:
                return int(match.episode_id)
    return None


def _entry_is_regular_tv_source(entry) -> bool:
    if entry.role == "main_episode" and entry.media_kind == "video":
        return True
    if entry.role == "subtitle" and entry.media_kind == "subtitle":
        return True
    asset = entry.presentation_asset
    return bool(
        entry.media_kind == "video"
        and asset is not None
        and asset.asset_role.kind == "content_version"
    )


def _bind_regular_sources_to_ranges(
    plan: RenamePlan,
    derived_ranges: Sequence[Mapping[str, object]],
    *,
    selected_subject_id: str,
    evidence_fingerprint: str,
) -> list[dict[str, object]]:
    """Bind each regular source to one frozen provider episode segment."""

    segments = [
        {
            **dict(item),
            "sources": [],
            "evidence_fingerprint": evidence_fingerprint,
        }
        for item in derived_ranges
    ]
    for entry in plan.entries:
        if not _entry_is_regular_tv_source(entry):
            continue
        episode = _regular_tv_entry_episode(plan, entry)
        if episode is None:
            if entry.media_kind == "subtitle":
                continue
            raise TVPlanningAuthorityError("普通分集source缺少可冻结的episode identity")
        matching = [
            segment for segment in segments
            if int(segment.get("episode_start", 0)) <= episode
            <= int(segment.get("episode_end", 0))
        ]
        selected = [
            segment for segment in matching
            if str(segment.get("bangumi_subject_id") or "")
            == selected_subject_id
        ]
        if len(selected) == 1:
            owner = selected[0]
        elif len(matching) == 1:
            owner = matching[0]
        else:
            raise TVPlanningAuthorityError(
                f"普通分集E{episode:02d}没有唯一source segment归属"
            )
        owner["sources"].append(str(entry.source_path.resolve(strict=False)))
    return segments


def _with_task_episode_ownership(
    plan: RenamePlan,
    authority: TVPlanningAuthority,
) -> RenamePlan:
    """Project frozen per-source segments into the canonical Plan envelope."""

    has_source_projection = "source_episode_projection" in plan.metadata
    projection_rows = plan.metadata.get("source_episode_projection", {})
    source_episode_by_path = {}
    if isinstance(projection_rows, Mapping):
        source_episode_by_path = {
            str(row.get("source_path") or ""): int(row["source_episode"])
            for row in projection_rows.get("episode_mapping", ()) or ()
            if isinstance(row, Mapping)
            and str(row.get("source_path") or "")
            and isinstance(row.get("source_episode"), int)
            and not isinstance(row.get("source_episode"), bool)
        }
    ranges = tuple(authority.episode_range_ownership)
    ownership = []
    primary = authority.work_identity.get("primary_provider", {})
    work_identity_key = ":".join(
        str(primary.get(field) or "")
        for field in ("namespace", "entity_type", "provider_id")
    )
    work_identity_fingerprint = hashlib.sha256(
        work_identity_key.encode("utf-8")
    ).hexdigest()
    for entry in plan.entries:
        if entry.media_kind != "video" or not _entry_is_regular_tv_source(entry):
            continue
        target_episode = _regular_tv_entry_episode(plan, entry)
        if target_episode is None:
            raise TVPlanningAuthorityError("普通视频缺少target episode identity")
        resolved_source = str(entry.source_path.resolve(strict=False))
        matches = [row for row in ranges if resolved_source in row.get("sources", ())]
        if len(matches) != 1:
            raise TVPlanningAuthorityError("普通视频没有唯一冻结source segment")
        row = matches[0]
        source_episode = source_episode_by_path.get(str(entry.source_path))
        if source_episode is None and not has_source_projection:
            source_episode = target_episode
        if source_episode is None:
            raise TVPlanningAuthorityError(
                "普通视频缺少冻结source episode projection"
            )
        start = int(row.get("episode_start", 0))
        end = int(row.get("episode_end", 0))
        subject_id = str(row.get("bangumi_subject_id") or "")
        season_number = int(row.get("season_number", -1))
        if season_number != entry.season_id:
            raise TVPlanningAuthorityError(
                "普通视频target season与冻结source segment不一致"
            )
        if source_episode < start or source_episode > end:
            raise TVPlanningAuthorityError(
                "普通视频source episode越出冻结source segment"
            )
        if source_episode != target_episode:
            raise TVPlanningAuthorityError(
                "普通视频target episode与冻结source episode不一致"
            )
        record = TaskEpisodeOwnership(
            source_path=entry.source_path,
            bangumi_subject_id=subject_id,
            work_identity_key=work_identity_key,
            work_identity_fingerprint=work_identity_fingerprint,
            season_number=season_number,
            source_episode=source_episode,
            target_episode=target_episode,
            range_authority_id=(
                f"subject:{subject_id}:season:{season_number}:{start}-{end}"
            ),
            evidence_fingerprint=str(row.get("evidence_fingerprint") or ""),
            decision_source="provider_subject_cohort_resolution",
        )
        ownership.append(record.to_dict())
    if any(
        entry.media_kind == "video" and _entry_is_regular_tv_source(entry)
        for entry in plan.entries
    ) and not ownership:
        raise TVPlanningAuthorityError("TV task episode ownership为空")
    metadata = dict(plan.metadata)
    metadata["task_episode_ownership"] = ownership
    return replace(plan, metadata=metadata)


def _build_cohort_tv_planning_authority(
    *,
    plan: RenamePlan,
    task_data: Mapping[str, object],
    evidence: Mapping[str, object],
    preview_request: Optional[Mapping[str, object]],
    request_fingerprint: str,
    expected_revision: int,
    selected_subject_id: str,
    tmdb_tv_id: Optional[str],
    season_assignments: Mapping[str, int],
    display_title: str,
    title_source: Mapping[str, object],
    series_year: Optional[int],
    primary_provider_identity: Optional[Mapping[str, object]] = None,
    metadata_provider_identities: Sequence[Mapping[str, object]] = (),
) -> TVPlanningAuthority:
    """The sole production cohort-confirmation call to the canonical Builder."""

    scope = _collect_tv_task_source_scope(
        plan,
        task_data,
        expected_revision=expected_revision,
        request_fingerprint=request_fingerprint,
    )
    subject_batch = evidence.get("subject_observations")
    if not isinstance(subject_batch, Mapping):
        raise TVPlanningAuthorityError("TV provider observation batch 缺失")
    provider_rows = []
    for item in subject_batch.get("subject_observations", ()) or ():
        if not isinstance(item, Mapping):
            raise TVPlanningAuthorityError("TV provider observation 格式无效")
        queries = tuple(
            row for row in item.get("queries", ()) or ()
            if isinstance(row, Mapping)
        )
        truncated = any(bool(row.get("truncated")) for row in queries)
        provider_rows.append({
            **dict(item),
            "complete": not truncated,
            "truncated": truncated,
            "evidence_fingerprint": str(
                item.get("response_fingerprint") or ""
            ),
        })
    if not provider_rows:
        raise TVPlanningAuthorityError("TV provider observations 为空")

    derived_ranges = tuple(
        item for item in (
            preview_request.get("derived_episode_ranges", ())
            if isinstance(preview_request, Mapping) else ()
        ) if isinstance(item, Mapping)
    )
    source_segments = _bind_regular_sources_to_ranges(
        plan,
        derived_ranges,
        selected_subject_id=selected_subject_id,
        evidence_fingerprint=str((
            preview_request.get("resolution_fingerprint")
            if isinstance(preview_request, Mapping) else ""
        ) or ""),
    )
    observation_body: dict[str, object] = {
        "schema_version": 1,
        "provider_observations": provider_rows,
        "selected_subject_id": selected_subject_id,
        "tmdb_tv_id": tmdb_tv_id,
        "source_owned_segments": source_segments,
        "non_regular_files": [],
    }
    observation_body["evidence_fingerprint"] = _tv_observation_fingerprint(
        observation_body
    )
    blob_ref = ObservationBlobStore(
        Path(DECISION_STATE_PATH).parent / "tv_observation_blobs"
    ).put(observation_body)
    effective_primary = dict(primary_provider_identity or {
        "namespace": "bangumi",
        "entity_type": "subject",
        "provider_id": selected_subject_id,
    })
    effective_metadata = tuple(
        dict(item) for item in metadata_provider_identities
    ) or (({
        "namespace": "tmdb",
        "entity_type": "tv",
        "provider_id": str(tmdb_tv_id),
        "status": "confirmed",
    },) if tmdb_tv_id else ())
    decisions = TVPlanningUserDecision.from_mapping({
        "decision_revision": expected_revision + 1,
        "confirmed_identity": tuple(
            str(effective_primary.get(field) or "")
            for field in ("namespace", "entity_type", "provider_id")
        ),
        "confirmed_display_title": display_title,
        "title_source": (
            str(title_source.get("namespace") or ""),
            str(title_source.get("entity_type") or ""),
            str(title_source.get("provider_id") or ""),
        ),
        "season_assignments": dict(season_assignments),
        "metadata_provider_identities": [
            dict(item) for item in effective_metadata
        ],
        "cohort_action": "same_work",
        "request_token": request_fingerprint,
    })
    authority_input = TVPlanningAuthorityInput(
        observations=FrozenTVPlanningObservationBatch.from_mapping(
            observation_body
        ),
        user_decisions=decisions,
        task_source_scope=scope,
    )
    return TVPlanningAuthorityBuilder().build(
        authority_input,
        observation_refs=(blob_ref,),
    )
PLAN_STATUS_LABELS = {
    "accepted": "结构通过",
    "need_confirm": "需要人工确认",
    "invalid": "结构无效",
    "fail_closed": "安全停止",
}
REVIEW_STATUS_LABELS = {
    ReviewStatus.DRAFT: "处理中",
    ReviewStatus.APPROVED: "已全部批准 · 未执行",
    ReviewStatus.CANCELLED: "已取消",
}
GROUP_LABELS = {
    "movie_continuous": "电影连续版",
    "tv_episodes": "TV 分集版",
}
ENTITY_TYPE_LABELS = {"movie": "电影", "tv": "电视剧", "subject": "条目"}
REVIEW_PAGE_SIZE = 50
SECONDARY_TOGGLE_PROPS = (
    "color=white text-color=blue-7 toggle-color=blue-7 "
    "toggle-text-color=white no-caps"
)


def render_cross_work_partition_action(on_click: Callable[[], object]) -> None:
    _render_cross_work_partition_action(ui, on_click)
    ui.label('仅核对所选视频及配套；确认后创建新卡，原已排除内容不进入新卡。').classes('bar-help')

ISSUE_GUIDANCE = {
    "work_identity_unconfirmed": (
        "还没有确认这是哪一部作品",
        "请从候选作品中选择正确条目。观看形式可以随后独立选择，不要求数据库必须存在对应的电影或分集条目。",
    ),
    "work_identity_provider_invalid": (
        "作品资料来源不受当前版本支持",
        "请重新选择 TMDB 电影、TMDB 电视剧或 Bangumi 动画条目。",
    ),
    "work_identity_domain_source_invalid": (
        "作品检索领域的判断依据无效",
        "请重新确认作品；检索领域只控制资料来源和排序，不会改变电影连续版或 TV 分集版。",
    ),
    "plan_migration_provenance_invalid": (
        "旧版整理计划的迁移记录不完整",
        "为保护旧批准和历史版本，当前计划不能批准。请保留原文件并重新生成 v5 整理计划。",
    ),
    "presentation_groups_missing": (
        "还没有建立任何观看形式",
        "请先确认作品，再建立电影连续版或 TV 分集版；没有观看形式时无法判断文件应放到哪里。",
    ),
    "presentation_asset_missing": (
        "这个视频还没有归入任何观看形式",
        "请确认它属于电影连续版还是 TV 分集版，并选择它是正片、特典还是配套文件。",
    ),
    "presentation_asset_role_unknown": (
        "还没确定这些文件的用途",
        "请在文件列表中把每个文件确认成正片、特典或配套文件。未确认前不能批准整理计划。",
    ),
    "episode_assignment_conflict": (
        "两个正片候选使用了同一个集号",
        "请在此确认其中的特典；确认后系统会从原始库存草案完整重建，而不会保留旧字幕或旧目标。",
    ),
    "provider_special_decision_required": (
        "官方资料中有可用的特别篇条目",
        "请明确归入特别篇，或保留为当前季度的附加内容。系统不会自动替你决定。",
    ),
    "source_episode_projection_decision_required": (
        "需要确认文件中的集号含义",
        "请选择文件名中的集号是整部作品的连续集号，还是本段从第 1 集重新编号。",
    ),
    "presentation_group_content_missing": (
        "这个观看形式还没有确认任何正片",
        "请把属于该观看形式的视频标记为“正片”。这与只读测试无关；只读模式只负责禁止移动文件。",
    ),
    "asset_collision_unresolved": (
        "发现同名但内容不同的附加内容",
        "系统不会覆盖已有文件。请只选择冲突文件，在“呈现分组与版本”中指定"
        "属于哪个正片版本、所有版本通用或不整理。",
    ),
    "subtitle_association_unresolved": (
        "字幕还没有确定跟随哪个视频",
        "请为这些字幕选择对应的正片。一个包里有电影版和分集版时，系统不会替你猜测。",
    ),
    "subtitle_parse_unresolved": (
        "字幕的语言或版本标签还没有确认",
        "请检查字幕语言、简繁和其他标签；确认前不能按普通流程批准。",
    ),
    "subtitle_association_rule_stale": (
        "字幕与视频的匹配结果来自旧规则",
        "请返回任务卡点击“重试”，重新生成整理计划；旧结果不会继续用于批准。",
    ),
    "subtitle_label_rule_stale": (
        "字幕标签识别结果来自旧规则",
        "请返回任务卡点击“重试”，重新生成整理计划；旧结果不会继续用于批准。",
    ),
    "subtitle_video_not_content_version": (
        "字幕所跟随的视频还没有被确认为正片",
        "请先确认对应视频的观看形式和文件用途，再处理这条字幕关联。",
    ),
    "subtitle_target_not_canonical": (
        "字幕的目标文件名与统一命名规则不一致",
        "请重新保存字幕语言和标签决定，让系统按对应正片重新生成目标文件名。",
    ),
    "subtitle_unknown_disposition_missing": (
        "字幕名中有无法识别的标签，尚未决定如何处理",
        "请确认该标签应保留、忽略还是改成已知含义；系统不会自行丢弃未知信息。",
    ),
}
ISSUE_GUIDANCE.update(EXCEPTIONAL_ISSUE_GUIDANCE)


@dataclass(frozen=True)
class IssueDefinition:
    """One UI exit for a blocking verifier code.

    ``inline`` repairs in this dialog; ``jump`` points at the relevant review
    control; ``external_recheck`` makes the prerequisite and retry path
    explicit instead of leaving a warning card without an action.
    """

    title: str
    detail: str
    action: str
    action_label: str


ISSUE_DEFINITIONS = {
    code: IssueDefinition(title, detail, "external_recheck", "重新识别并检查")
    for code, (title, detail) in ISSUE_GUIDANCE.items()
}
ISSUE_DEFINITIONS.update({
    "mixed_presentation_requires_partition": IssueDefinition(
        "电影连续版与剧集分集版需要分开整理",
        "请在下方文件列表勾选同一种观看形式的视频，再点击“作为独立作品整理”；原文件不会移动。",
        "inline", "作为独立作品整理",
    ),
    "presentation_asset_role_unknown": IssueDefinition(
        *ISSUE_GUIDANCE["presentation_asset_role_unknown"],
        "inline", "在此确认文件用途",
    ),
    "episode_assignment_conflict": IssueDefinition(
        *ISSUE_GUIDANCE["episode_assignment_conflict"],
        "inline", "在此确认冲突文件用途",
    ),
    "provider_special_decision_required": IssueDefinition(
        *ISSUE_GUIDANCE["provider_special_decision_required"],
        "inline", "在此确认特别篇归属",
    ),
    "presentation_asset_owner_unresolved": IssueDefinition(
        "电影特典或配套资源尚未确定属于哪个版本",
        "请在此指定所属版本或所有版本通用；系统不会覆盖同名但不同内容的资源。",
        "inline", "在此指定资源归属",
    ),
    "subtitle_association_unresolved": IssueDefinition(
        *ISSUE_GUIDANCE["subtitle_association_unresolved"],
        "jump", "打开字幕关联修正",
    ),
})
ISSUE_DEFINITIONS.update({
    code: IssueDefinition(*guidance, "inline", "在此确认异常视频归属")
    for code, guidance in EXCEPTIONAL_ISSUE_GUIDANCE.items()
})


def issue_definition(code: str, message: str) -> IssueDefinition:
    if code == 'asset_replace_choice_required':
        return IssueDefinition(message, '请在“已登记的同一作品”卡片中点击“换源”，核对文件差异后生成待批准计划。',
                               'jump', '核对换源范围')
    return ISSUE_DEFINITIONS.get(
        code,
        IssueDefinition(
            message or "需要人工处理",
            "此问题不能安全自动修正。请检查源文件或配置后点击任务卡“重试”重新生成整理计划。",
            "external_recheck", "重新识别并检查",
        ),
    )


def _file_name(value: object) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def summarize_plan_issues(
    issues: Sequence[Dict[str, str]],
    rows: Sequence[Dict[str, object]],
) -> List[Dict[str, object]]:
    """Collapse verifier noise by issue code while preserving exact filenames."""

    by_code: Dict[str, Dict[str, object]] = {}
    files_by_code: Dict[str, List[str]] = {}
    for row in rows:
        for code in row.get("issue_codes", ()):
            name = _file_name(row.get("source"))
            if name and name not in files_by_code.setdefault(str(code), []):
                files_by_code[str(code)].append(name)
    for issue in issues:
        code = str(issue.get("code") or "unknown")
        summary = by_code.setdefault(code, {
            "code": code,
            "message": str(issue.get("message") or "需要人工处理"),
            "title": issue_definition(code, str(issue.get("message") or "")).title,
            "detail": issue_definition(code, str(issue.get("message") or "")).detail,
            "action": issue_definition(code, str(issue.get("message") or "")).action,
            "files": [],
        })
        source_name = _file_name(issue.get("source"))
        if source_name and source_name not in summary["files"]:
            summary["files"].append(source_name)
    for code, summary in by_code.items():
        for name in files_by_code.get(code, []):
            if name not in summary["files"]:
                summary["files"].append(name)
        summary["file_count"] = len(summary["files"])
    return list(by_code.values())


from .asset_choice_forms import visible_plan_issues, visible_role_rows


def normalize_provider_candidates(metadata: Dict[str, object]) -> List[Dict[str, object]]:
    """Return independent JSON snapshots for legacy groups and candidate lists."""

    raw = metadata.get("provider_candidates") or {}
    normalized: List[Dict[str, object]] = []
    if not isinstance(raw, Mapping):
        return normalized
    for group_kind, value in raw.items():
        candidates = (
            value
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes))
            else [value]
        )
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            item = plain(candidate)
            item["group_kind"] = str(item.get("group_kind") or group_kind)
            normalized.append(item)
    return normalized


def provider_candidates_for_display(
    candidates: Sequence[Dict[str, object]],
    namespace: str,
    *,
    limit: int = 4,
) -> List[Dict[str, object]]:
    """Keep provider columns independent and intentionally bounded."""

    return [
        candidate for candidate in candidates
        if str(candidate.get("namespace") or "").casefold() == namespace.casefold()
    ][:max(0, int(limit))]


def provider_candidate_url(candidate: Dict[str, object]) -> str:
    namespace = str(candidate.get("namespace") or "").lower()
    entity_type = str(candidate.get("entity_type") or "").lower()
    provider_id = str(candidate.get("provider_id") or "").strip()
    if namespace == "tmdb" and entity_type in {"movie", "tv"} and provider_id:
        return f"https://www.themoviedb.org/{entity_type}/{provider_id}"
    if namespace == "bangumi" and provider_id:
        return f"https://bgm.tv/subject/{provider_id}"
    return ""


def provider_candidate_poster(candidate: Dict[str, object]) -> str:
    value = str(candidate.get("poster_url") or candidate.get("poster_path") or "")
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/") and str(candidate.get("namespace")).lower() == "tmdb":
        return f"https://image.tmdb.org/t/p/w342{value}"
    return ""


def user_recovery_detail(value: object) -> str:
    """Translate recovery diagnostics without exposing storage internals."""

    text = str(value or "")
    lowered = text.casefold()
    if "completed execution ledger" in lowered or "execution ledger" in lowered:
        return "找不到与本次整理对应的历史执行记录。"
    if "plan" in lowered:
        return text.replace("Plan", "整理计划").replace("plan", "整理计划")
    return text or "暂时无法读取本次整理记录。"


def is_fresh_unexecuted_task(
    task_data: Mapping[str, object], task_decision: object
) -> bool:
    """Return whether catalog lookup failures are irrelevant to a new task."""

    if task_data.get("execution_status") or task_data.get(
        "execution_requires_manual_check"
    ):
        return False
    recovery_state = getattr(task_decision, "recovery_state", None)
    return (
        recovery_state in {None, "", "healthy"}
        and not getattr(task_decision, "execution_failure", None)
    )


def candidate_type_warning(
    requested_is_movie: object,
    candidates: Sequence[Dict[str, object]],
) -> str:
    if requested_is_movie not in {True, False} or not candidates:
        return ""
    expected = "movie" if requested_is_movie is True else "tv"
    expected_group = "movie_continuous" if requested_is_movie is True else "tv_episodes"
    if any(provider_candidate_group_kind(item) == expected_group for item in candidates):
        return ""
    expected_label = "电影" if expected == "movie" else "电视剧"
    found = "、".join(dict.fromkeys(
        ENTITY_TYPE_LABELS.get(str(item.get("entity_type")), str(item.get("entity_type")))
        for item in candidates
    ))
    return f"按当前信息没有找到正确的{expected_label}匹配；以下是可能相关的{found or '其他类型'}结果，请确认。"


def _provider_selection_alive(owner) -> bool:
    """Both link lookup and queued selection stop updating a closed view."""
    if any(getattr(owner, name, False) for name in
           ('is_deleted', 'exit_requested', '_provider_link_closed')):
        return False
    if not getattr(owner, '_provider_link_lifecycle', False):
        client = getattr(owner, 'client', None)
        if callable(getattr(client, 'on_disconnect', None)):
            client.on_disconnect(lambda: setattr(owner, '_provider_link_closed', True))
        if callable(getattr(owner, 'on_value_change', None)):
            owner.on_value_change(lambda event: setattr(owner, '_provider_link_closed', True)
                                  if not event.value else None)
        owner._provider_link_lifecycle = True
    return True


def can_execute_approved_plan(
    decision_status: ReviewStatus,
    task_status: str,
    *,
    execution_policy,
    preview_only: bool = False,
) -> bool:
    if not (
        decision_status == ReviewStatus.APPROVED
        and task_status == "approved"
        and not preview_only
    ):
        return False
    try:
        plan_execution_contract.require_approved_execution_policy(execution_policy)
    except PlanExecutionError as exc:
        if exc.code in {
            plan_execution_contract.DRY_RUN_PLAN_RETRY_CODE,
            plan_execution_contract.MISSING_EXECUTION_POLICY_CODE,
        }:
            return False
        raise
    return True


def can_approve_plan(plan_status: str) -> bool:
    return str(plan_status) != "invalid"


def workflow_status_for_plan(plan_status: str) -> str:
    return "invalid" if str(plan_status) == "invalid" else "need_confirm"


def _task_terminal_for_projected_plan(task_id: str) -> TaskTerminalMutation:
    def update(_decision, projected, current):
        status = PlanVerifier().verify(projected).status.value
        current.update({
            "workflow_status": workflow_status_for_plan(status),
            "plan_status": status,
            "plan_entry_count": len(projected.entries),
            "completed_at": None,
        })
        if (
            projected.work_identity is not None
            and projected.work_identity.status == "confirmed"
            and projected.work_identity.title.strip()
        ):
            current["name"] = projected.work_identity.title.strip()
        return current

    return TaskTerminalMutation.prepare(TASK_PATH, task_id, update)


@dataclass(frozen=True)
class PlanReviewSnapshot:
    task_id: str
    source_root: str
    plan_status: str
    rows: Tuple[Dict[str, object], ...]
    issues: Tuple[Dict[str, str], ...]
    presentation_groups: Tuple[Dict[str, object], ...]
    work_identity: Dict[str, object]
    linked_provider_identities: Tuple[Dict[str, object], ...]
    opening_context: Optional[Tuple[object, object]] = None
    display_context: object = None


def _inventory_draft_for_completion(plan, service) -> RenamePlan:
    mapping_source = str(plan.metadata.get("mapping_source") or "")
    if mapping_source == "inventory_review_draft":
        return plan
    if mapping_source == "inventory_smart_completion":
        return service.reopen_inventory_draft(
            plan.task_id,
            PlanStore.fingerprint(plan),
            reason="用户修订整理计划",
            persist_revision=False,
        )
    raise PlanReviewError("当前整理计划不能修订，请重新识别")


def _series_binding_for_tv(plan, state, catalog, target_root: Path) -> dict:
    if state is None or plan.work_identity is None:
        raise DecisionStateError("缺少剧集系列决策上下文")
    from ..rename import series_registration
    if plan.work_identity.domain == "animation":
        series_registration.validate_plan_series_registration(plan, state)
        return dict(state.series_binding)
    from ..rename.canonical_naming import (
        CanonicalNamingError,
        build_canonical_work_root,
        decision_from_work_identity,
        read_naming_decision,
    )

    try:
        raw = plan.metadata.get("canonical_naming_decision")
        naming_decision = (
            read_naming_decision(raw)
            if raw is not None
            else decision_from_work_identity(plan.work_identity)
        )
        canonical_root = build_canonical_work_root(
            resolved_target_root=target_root,
            decision=naming_decision,
            year=plan.work_identity.year,
        )
    except CanonicalNamingError as exc:
        raise DecisionStateError(str(exc)) from exc
    return series_registration.manual_tv_binding(plan.work_identity, catalog, canonical_root)


def _complete_inventory_plan(plan, state, service, command, catalog, group_id, group_kind,
                             snapshot, domain, selected_season):
    """消费点击时冻结的输入；后台计算仍通过原决策命令提交。"""
    from ..rename.target_routing import resolve_target_root
    draft = _inventory_draft_for_completion(plan, service)
    target_root = resolve_target_root(
        domain,
        group_kind,
        snapshot,
    )
    completion_kwargs = {}
    decision_changes = {
        "library_domain": domain,
        "layout_mode": (
            "tv_episodes" if group_kind == "tv_episodes" else "movie_segments"
        ),
        "recovery_state": "healthy",
    }
    if group_kind == "tv_episodes":
        from ..rename.series_registration import SeriesRegistrationAuthority
        if not is_confirmed_movie_plan(draft):
            series_binding = _series_binding_for_tv(plan, state, catalog, target_root)
            decision_changes["series_binding"] = series_binding
            decision_changes["catalog_link_intent"] = None
            completion_kwargs.update({
                "series_key": str(series_binding["series_key"]),
                "series_registration_authority": SeriesRegistrationAuthority.from_mapping(series_binding['registration_authority']),
                "season_binding_id": None,
                "series_catalog_revision": int(series_binding.get("catalog_revision") or 0),
            })
        if selected_season is not None:
            completion_kwargs["season_number"] = selected_season
            completion_kwargs["season_source"] = "explicit_user"
            from ..rename.season_identity import confirmed_season_binding
            decision_changes["season_binding"] = confirmed_season_binding(
                draft.work_identity.primary_provider.to_dict(), int(selected_season),
                'explicit_user', previous=state)
        elif state.season_binding is not None:
            completion_kwargs["season_number"] = int(
                state.season_binding["season_number"]
            )
            completion_kwargs["season_source"] = str(
                state.season_binding.get("decision_source")
                or "provider"
            )
        else:
            raise DecisionStateError("尚未确认季度，不能生成剧集 Plan")
        binding = decision_changes.get("season_binding") or state.season_binding
        completion_kwargs["season_binding_id"] = str(
            binding["season_binding_id"]
        )
    if group_kind == "movie_continuous":
        completion_kwargs["confirmed_segment_sources"] = tuple(
            entry.source_path
            for entry in draft.entries
            if entry.media_kind == "video"
        )
    revised = service.complete_inventory_draft(
        draft.task_id,
        PlanStore.fingerprint(draft),
        group_id=group_id,
        group_kind=group_kind,
        target_root=target_root,
        reason=(
            "用户选择按季和集播放并确认智能生成"
            if group_kind == "tv_episodes"
            else "用户选择连续电影播放并确认智能生成"
        ),
        root_snapshot=snapshot.to_dict(),
        work_domain=domain,
        persist_revision=False,
        _source_plan=draft,
        _revision_parent_fingerprint=PlanStore.fingerprint(plan),
        **completion_kwargs,
    )
    if group_kind == "movie_continuous" or is_confirmed_movie_plan(draft):
        revised_metadata = (
            revised.metadata
            if isinstance(revised.metadata, Mapping) else {}
        )
        work_key = str(
            revised_metadata.get("catalog_work_key")
            or revised_metadata.get("proposed_work_key")
            or ""
        ).strip()
        decision_changes.update({
            "series_binding": None,
            "catalog_link_intent": (
                {
                    "work_key": work_key,
                    "catalog_revision": int(
                        revised_metadata.get("catalog_revision") or 0
                    ),
                }
                if work_key else None
            ),
        })
    decision_changes['movie_version_selection']=(
        service.movie_selection_for_completion(revised))
    saved_state, projected = command.update(
        plan.task_id,
        expected_revision=state.revision,
        changes=decision_changes,
        planner=lambda _state: revised,
        task_terminal=_task_terminal_for_projected_plan(plan.task_id),
    )
    if projected is None:
        raise DecisionStateError("未生成 DecisionState 对应的 Plan")
    return projected

def _prepare_catalog_display(plan, state, task, review, series, detach, recovery):
    """一次展示内的登记观察；不作为后续命令的权威或长期缓存。"""
    result = dict(pending=None, match=None, error=None, recovery=None, suggestions=(), present=False)
    try:
        result['pending'] = detach.pending_transaction(plan.task_id)
    except (ProjectionAuthorityError, ValueError, RuntimeError, OSError) as exc:
        result['pending'] = exc
        return result
    if result['pending'] is not None and result['pending'].phase != 'completed':
        return result
    kinds = {g.kind for g in plan.presentation_groups}
    kind = next(iter(kinds)) if len(kinds) == 1 else None
    if kind not in {'movie_continuous', 'tv_episodes'}:
        return result
    if (plan.metadata.get('mapping_source') == 'inventory_review_draft'
            and not task.get('execution_status') and kind != 'movie_continuous'):
        return result
    backend, key = catalog_backend_for_plan(plan, state)
    frozen = catalog_lookup_uses_frozen_key(state, backend, key)
    work = plan.work_identity
    if (work is None or work.status != 'confirmed') and not frozen:
        return result
    from ..rename.work_affiliation import from_plan
    affiliation = from_plan(plan)
    try:
        if backend == 'series':
            match = (series.load(str(key)) if key and (frozen
                or affiliation is not None and affiliation.to_dict()['action'] == 'existing'
                or task.get('execution_status') in {'completed', 'catalog_update_pending'})
                else series.find_by_provider(work.primary_provider.to_dict()))
        else:
            match = (review.work_catalog.get_match(str(key)) if key else
                     review.work_catalog.find_exact(work.primary_provider, group_kind=kind))
        result['match'] = match
        if match is not None:
            result['present'] = match.canonical_root.is_dir()
        elif backend != 'series' and not frozen and affiliation is None:
            result['suggestions'] = review.work_catalog.suggestions(work.title, work.year,
                work.primary_provider.aliases, group_kind=kind, include_related_series=kind == 'tv_episodes')
    except Exception as exc:
        result['error'] = exc
        if not is_fresh_unexecuted_task(task, state) and state is not None:
            result['recovery'] = recovery.diagnose(state, plan)
    return result


@reuse_display_asset_reads
def _prepare_revision_display(plan, review, series, detach, recovery, task_fallback):
    from ..utils.utils import read_task_at
    snapshot = build_plan_review_snapshot(plan)
    state = review.decision_state_store.load_optional(plan.task_id)
    task = read_task_at(review.task_root, plan.task_id) or task_fallback
    decision = review.load_or_create(plan)
    catalog = _prepare_catalog_display(plan, state, task, review, series, detach, recovery)
    current = review.plan_store.load(plan.task_id)
    if PlanStore.fingerprint(current) != PlanStore.fingerprint(plan):
        raise ValueError('整理计划已更新，不采用过期的页面结果')
    return snapshot, state, decision, task, catalog


@reuse_display_asset_reads
def prepare_plan_review_open(plan: RenamePlan) -> PlanReviewSnapshot:
    """Read display data in the worker; no UI or authoritative writes here."""
    from ..rename.work_catalog import WorkCatalog
    snapshot = build_plan_review_snapshot(plan)
    decisions = DecisionStateStore(DECISION_STATE_PATH)
    state = decisions.load_optional(plan.task_id)
    service = CatalogDetachService(
        transaction_store=RecoveryTransactionStore(RECOVERY_TRANSACTION_PATH),
        decision_store=decisions, plan_store=PlanStore(PLAN_PATH),
        work_catalog=WorkCatalog(WORK_CATALOG_PATH),
        series_catalog=SeriesCatalog(SERIES_CATALOG_PATH), task_root=TASK_PATH,
    )
    from ..rename.initial_movie_completion import RootSnapshotSource
    from ..rename.target_routing import snapshot_from_config_manager
    from ..rename.recognition_queue_admission import capture_workspace_open
    review = PlanReviewService(PlanStore(PLAN_PATH), PLAN_REVIEW_PATH,
        decision_state_root=DECISION_STATE_PATH, task_root=TASK_PATH,
        root_snapshot_source=RootSnapshotSource(lambda: snapshot_from_config_manager(cm)))
    task = get_task(plan.task_id) or {}
    recovery = RecoveryService(catalog_root=WORK_CATALOG_PATH, record_root=RECORD_PATH,
        series_catalog_root=SERIES_CATALOG_PATH, rebuild_service=review.rebuild_service)
    catalog = _prepare_catalog_display(plan, state, task, review, service.series_catalog, service, recovery)
    decision = review.load_or_create(plan)
    evidence = capture_workspace_open(task_queue, plan.task_id, task)
    return replace(snapshot, opening_context=(state, catalog['pending']),
                   display_context=(decision, task, catalog, evidence))


@reuse_display_asset_reads
def build_plan_review_snapshot(plan: RenamePlan) -> PlanReviewSnapshot:
    verification = PlanVerifier().verify(plan)
    presentation_groups_by_id = {
        group.group_id: group for group in plan.presentation_groups
    }
    issue_codes_by_source: Dict[str, List[str]] = {}
    for issue in verification.issues:
        if issue.source_path is not None:
            issue_codes_by_source.setdefault(str(issue.source_path), []).append(issue.code)
    rows = tuple(
        {
            "id": index,
            "entry_id": entry_id(entry),
            "source": str(entry.source_path),
            "target": str(entry.target_path),
            "season": entry.season_id,
            "episode": entry.episode_id,
            "episode_end": entry.episode_end,
            "media_kind": entry.media_kind,
            "role": entry.role,
            "requires_review": entry.requires_review,
            "evidence": "；".join(entry.evidence),
            "issue_codes": tuple(issue_codes_by_source.get(str(entry.source_path), ())),
            "execution_blocked": bool(issue_codes_by_source.get(str(entry.source_path))),
            "raw_label": entry.subtitle.raw_label if entry.subtitle else "",
            "associated_video": (
                str(entry.subtitle.associated_video_source_path)
                if entry.subtitle and entry.subtitle.associated_video_source_path is not None
                else ""
            ),
            "primary_language": entry.subtitle.primary_language if entry.subtitle else None,
            "script": entry.subtitle.script if entry.subtitle else None,
            "region_hint": entry.subtitle.region_hint if entry.subtitle else None,
            "additional_languages": entry.subtitle.additional_languages if entry.subtitle else (),
            "track_title_tokens": entry.subtitle.track_title_tokens if entry.subtitle else (),
            "flags": entry.subtitle.flags if entry.subtitle else (),
            "unknown_tokens": entry.subtitle.unknown_tokens if entry.subtitle else (),
            "association_state": (
                entry.subtitle.association_state.value if entry.subtitle else ""
            ),
            "parse_state": entry.subtitle.parse_state.value if entry.subtitle else "",
            "reason_codes": entry.subtitle.reason_codes if entry.subtitle else (),
            "decision_source": entry.subtitle.decision_source if entry.subtitle else "",
            "presentation_group": (
                entry.presentation_asset.presentation_group
                if entry.presentation_asset else ""
            ),
            "presentation_group_label": (
                presentation_groups_by_id[
                    entry.presentation_asset.presentation_group
                ].display_name
                if entry.presentation_asset
                and entry.presentation_asset.presentation_group
                in presentation_groups_by_id
                else ""
            ),
            "asset_role": (
                entry.presentation_asset.asset_role.kind
                if entry.presentation_asset else ""
            ),
            "asset_subtype": (
                entry.presentation_asset.asset_role.subtype
                if entry.presentation_asset else None
            ),
            "version_ref": (
                entry.presentation_asset.version_descriptor.version_ref
                if entry.presentation_asset
                and entry.presentation_asset.version_descriptor else None
            ),
            "version_label": (
                entry.presentation_asset.version_descriptor.display_label
                if entry.presentation_asset
                and entry.presentation_asset.version_descriptor else None
            ),
            "edition_key": (
                getattr(
                    entry.presentation_asset.version_descriptor.edition.value,
                    "key", "",
                )
                if entry.presentation_asset
                and entry.presentation_asset.version_descriptor else ""
            ),
            "edition_label": (
                getattr(
                    entry.presentation_asset.version_descriptor.edition.value,
                    "label", None,
                )
                if entry.presentation_asset
                and entry.presentation_asset.version_descriptor else None
            ),
            "resolution": (
                entry.presentation_asset.version_descriptor.resolution.value
                if entry.presentation_asset
                and entry.presentation_asset.version_descriptor else None
            ),
            "dynamic_range": (
                entry.presentation_asset.version_descriptor.dynamic_range.value
                if entry.presentation_asset
                and entry.presentation_asset.version_descriptor else None
            ),
            "key_audio": (
                entry.presentation_asset.version_descriptor.key_audio.value
                if entry.presentation_asset
                and entry.presentation_asset.version_descriptor else None
            ),
        }
        for index, entry in enumerate(plan.entries, start=1)
    )
    issues = tuple(
        {
            "code": issue.code,
            "message": localize_plan_term(issue.message),
            "source": str(issue.source_path or ""),
            "target": str(issue.target_path or ""),
        }
        for issue in verification.issues
    )
    return PlanReviewSnapshot(
        task_id=plan.task_id,
        source_root=str(plan.source_root),
        plan_status=verification.status.value,
        rows=rows,
        issues=issues,
        presentation_groups=tuple({
            "group_id": group.group_id,
            "kind": group.kind,
            "display_name": group.display_name,
            "preferred_version_ref": group.preferred_version_ref,
        } for group in plan.presentation_groups),
        work_identity=({
            "status": plan.work_identity.status,
            "domain": plan.work_identity.domain,
            "domain_source": plan.work_identity.domain_source,
            "title": plan.work_identity.title,
            "year": plan.work_identity.year,
            "provider_namespace": plan.work_identity.primary_provider.namespace,
            "provider_entity_type": plan.work_identity.primary_provider.entity_type,
            "provider": (
                f"{plan.work_identity.primary_provider.namespace or '?'} / "
                f"{plan.work_identity.primary_provider.entity_type or '?'} / "
                f"{plan.work_identity.primary_provider.provider_id or '?'}"
            ),
        } if plan.work_identity else {
            "status": "unresolved", "domain": "unknown",
            "domain_source": "unknown", "title": "未确认",
            "year": None, "provider": "? / ? / ?",
        }),
        linked_provider_identities=tuple({
            "status": identity.status,
            "namespace": identity.namespace,
            "entity_type": identity.entity_type,
            "provider_id": identity.provider_id,
            "title": identity.title,
        } for identity in plan.linked_provider_identities),
    )


def filter_plan_rows(
    rows: Sequence[Dict[str, object]],
    query: str,
    review_only: bool = False,
    *,
    subtitle_only: bool = False,
    unresolved_only: bool = False,
    conflict_only: bool = False,
    unknown_only: bool = False,
    language: str = "",
    flag: str = "",
) -> List[Dict[str, object]]:
    needle = str(query or "").strip().casefold()
    filtered = []
    for row in rows:
        if review_only and not row.get("requires_review"):
            continue
        if subtitle_only and row.get("media_kind") != "subtitle":
            continue
        if unresolved_only and not (
            row.get("association_state") == "unresolved"
            or row.get("parse_state") == "unresolved"
            or row.get("execution_blocked")
        ):
            continue
        if conflict_only and not (
            row.get("association_state") == "conflict"
            or row.get("parse_state") == "conflict"
            or any("conflict" in str(code) or "collision" in str(code)
                   for code in row.get("issue_codes", ()))
        ):
            continue
        if unknown_only and not row.get("unknown_tokens"):
            continue
        if language and row.get("primary_language") != language:
            continue
        if flag and flag not in row.get("flags", ()):
            continue
        searchable = " ".join(
            str(row.get(field, ""))
            for field in (
                "source",
                "target",
                "media_kind",
                "role",
                "evidence",
                "season",
                "episode",
                "raw_label",
                "associated_video",
                "primary_language",
                "script",
                "region_hint",
                "additional_languages",
                "track_title_tokens",
                "flags",
                "unknown_tokens",
                "reason_codes",
                "issue_codes",
                "presentation_group",
                "asset_role",
                "asset_subtype",
                "version_ref",
                "version_label",
                "edition_key",
                "edition_label",
                "resolution",
                "dynamic_range",
                "key_audio",
            )
        ).casefold()
        if needle and needle not in searchable:
            continue
        filtered.append(dict(row))
    return filtered


def catalog_backend_for_plan(
    plan: RenamePlan, decision: Optional[object]
) -> tuple[str, Optional[str]]:
    """Select the catalog that actually owns this Plan.

    New TV Plans carry a SeriesCatalog key.  Legacy TV Plans deliberately
    remain compatible with WorkCatalog, so the GUI must not route them to the
    new catalog merely because their presentation kind is ``tv_episodes``.
    """

    group_kinds = {group.kind for group in plan.presentation_groups}
    if group_kinds not in ({"movie_continuous"}, {"tv_episodes"}):
        return "none", None
    from ..rename.catalog_binding import recovery_binding

    binding = recovery_binding(plan, decision)
    return (
        (binding.backend, binding.key)
        if binding is not None else ("none", None)
    )


def catalog_lookup_uses_frozen_key(
    decision: Optional[object], backend: str, catalog_key: Optional[str]
) -> bool:
    """Use the exact registration named by an execution failure.

    Provider lookup is useful while planning a not-yet-materialized series, but
    it must never replace the catalog key already named by the Plan when the
    executor reports that registration's root missing.
    """

    from ..rename.catalog_binding import binding_from_failure

    failure = binding_from_failure(decision) if decision is not None else None
    return bool(
        failure is not None
        and catalog_key
        and failure.backend == backend
        and failure.key == catalog_key
    )


def _dialog_mount_context():
    """Mount long-lived dialogs outside refreshable dashboard subtrees."""

    return ui.context.client.content


from .plan_file_list import PlanFileListMixin


class PlanReviewDialog(PlanFileListMixin, ui.dialog):
    def __init__(self, plan: Optional[RenamePlan], task_data: Dict[str, object], *, snapshot=None, loading=False, recovery_error=None):
        super().__init__()
        self.props("persistent")
        self.exit_requested = False
        self.queue = task_queue
        self.plan = plan
        self.recovery_error = recovery_error
        self.task_data = task_data
        self.task_id = plan.task_id if plan is not None else str(task_data['uuid'])
        from ..rename.recognition_queue_admission import capture_workspace_open
        self.retry_open_evidence = capture_workspace_open(task_queue, self.task_id, task_data)
        with self:
            self.body = ui.card().classes('bar-dialog bar-review-dialog bar-modal-fixed')
        if loading:
            with self.body:
                with ui.column().classes('bar-modal-header w-full'):
                    ui.label('整理任务').classes('bar-dialog-title')
                with ui.column().classes('bar-modal-body w-full gap-3'):
                    bar_progress(ui)
                    ui.label('正在读取整理计划').classes('bar-help')
                with ui.row().classes('bar-modal-footer w-full justify-end'):
                    self._render_close_action()
        elif plan is None:
            operation, _ = build_review_operation(task_data)
            self.provider_selection_expanded = not bool(operation.context.get('candidate'))
            self._build_regions()
            with self.summary_region:
                self._render_header()
            with self.selection_region:
                self.groups_container = ui.column().classes('w-full gap-3')
                if recovery_error is not None:
                    ui.label('当前整理记录无法读取，请核对后再继续：' + str(recovery_error)).classes('bar-task-feedback bar-error-text')
                self._render_initial_selection()
                from ..rename.input_directory_discovery import source_group_pending
                if not source_group_pending(task_data):
                    self._render_recognition_options()
            with self.plan_region:
                from .task_source_files_view import render_task_source_files
                self.source_file_list = render_task_source_files(task_data)
            with self.actions_region:
                self.render_actions()
            self.client.on_disconnect(lambda: setattr(self, 'exit_requested', True) if self.plan is None else None)
            self.on_value_change(lambda event: setattr(self, 'exit_requested', True) if not event.value else None)
        else:
            self._load_plan_view(plan, snapshot=snapshot)

    def _load_plan_view(self, plan: RenamePlan, *, snapshot=None) -> None:
        """Hydrate the same dialog from an actual persisted plan, never a stub."""
        first_hydration = self.plan is None
        self.plan = plan
        if snapshot is not None and snapshot.display_context is not None:
            self.task_data = snapshot.display_context[1]
        else:
            self.task_data = get_task(self.task_id) or self.task_data
        if first_hydration and (snapshot is None or snapshot.display_context is None):
            from ..rename.recognition_queue_admission import capture_workspace_open
            self.retry_open_evidence = capture_workspace_open(task_queue, self.task_id, self.task_data)
        self.snapshot = snapshot if snapshot is not None else build_plan_review_snapshot(plan)
        from ..rename.initial_movie_completion import RootSnapshotSource
        from ..rename.target_routing import snapshot_from_config_manager
        self.review_service = PlanReviewService(
            PlanStore(PLAN_PATH),
            PLAN_REVIEW_PATH,
            decision_state_root=DECISION_STATE_PATH,
            task_root=TASK_PATH,
            root_snapshot_source=RootSnapshotSource(lambda: snapshot_from_config_manager(cm)),
        )
        self.decision_state_store = DecisionStateStore(DECISION_STATE_PATH)
        self.decision_command_service = DecisionCommandService(
            self.decision_state_store, PlanStore(PLAN_PATH)
        )
        self.series_catalog = SeriesCatalog(SERIES_CATALOG_PATH)
        self.recovery_service = RecoveryService(
            catalog_root=WORK_CATALOG_PATH,
            record_root=RECORD_PATH,
            series_catalog_root=SERIES_CATALOG_PATH,
            rebuild_service=self.review_service.rebuild_service,
        )
        self.recovery_transaction_store = RecoveryTransactionStore(
            RECOVERY_TRANSACTION_PATH
        )
        self.catalog_detach_service = CatalogDetachService(
            transaction_store=self.recovery_transaction_store,
            decision_store=self.decision_state_store,
            plan_store=PlanStore(PLAN_PATH),
            work_catalog=self.review_service.work_catalog,
            series_catalog=self.series_catalog,
            task_root=TASK_PATH,
        )
        # Read-only projection: opening the dialog never creates or migrates
        # authoritative state. Legacy tasks get it on explicit re-recognition.
        self.task_decision = (self.snapshot.opening_context[0]
                              if self.snapshot.opening_context is not None
                              else self.decision_state_store.load_optional(plan.task_id))
        if self.snapshot.display_context is None:
            self.decision = self.review_service.load_or_create(plan)
            self._catalog_display = _prepare_catalog_display(plan, self.task_decision, self.task_data,
                self.review_service, self.series_catalog, self.catalog_detach_service, self.recovery_service)
        else:
            self.decision, self.task_data, self._catalog_display, self.retry_open_evidence = self.snapshot.display_context
        self.query = ""
        self.review_only = False
        self.subtitle_only = False
        self.unresolved_only = False
        self.conflict_only = False
        self.unknown_only = False
        self.language_filter = ""
        self.flag_filter = ""
        self.view_mode = "all"
        self.page = 1
        self.selected_ids: set[str] = set()
        self.provider_selection_expanded = False
        self.pending_library_domain: Optional[str] = None  # form-only legacy

        self.body.clear()
        self._build_regions()
        with self.summary_region:
            self._render_header()
        self.snapshot = replace(self.snapshot, opening_context=None)
        with self.selection_region:
            self.groups_container = ui.column().classes('w-full gap-3')
            self._render_groups()
            if not self._replacement_unfinished():
                self._render_recognition_options()
            if not self._replacement_unfinished():
                self._render_matched_seasons()
        if self._replacement_unfinished() or self.plan.metadata.get('pending_work_affiliation') is not None:
            with self.plan_region:
                if self.plan.metadata.get('pending_work_affiliation') is not None:
                    from .work_affiliation_view import render
                    render(self, ui=ui, notify=notify)
                from .task_source_files_view import render_task_source_files
                self.source_file_list = render_task_source_files(self.task_data)
            with self.actions_region:
                self.render_actions()
            return
        with self.plan_region:
            self.catalog_container = ui.column().classes('w-full gap-2')
            self._render_catalog_context()
            self.completion_container = ui.column().classes('w-full gap-2')
            self._render_completion_workflow()
            self.issues_container = ui.column().classes('w-full')
            self._render_issues()
            self._render_plan_tools()
            self._render_toolbar()
            self.entries_container = ui.column().classes('bar-review-entries w-full gap-2')
            self._render_entries()
            self._render_manual_corrections()
        with self.actions_region:
            self.render_actions()

    def _build_regions(self):
        """区域由页面显式组织；状态刷新只更新区域内部。"""
        with self.body:
            self.summary_region = ui.column().classes('bar-modal-header bar-review-summary w-full gap-3')
            with ui.column().classes('bar-modal-body w-full gap-5'):
                self.selection_region = ui.column().classes('bar-review-section w-full gap-3')
                with self.selection_region:
                    ui.label('作品与季度确认').classes('text-lg font-bold')
                self.plan_region = ui.column().classes('bar-review-section w-full gap-3')
                with self.plan_region:
                    ui.label('计划预览' if self.plan is not None and not self._replacement_unfinished() and self.plan.metadata.get('pending_work_affiliation') is None else '来源文件').classes('text-lg font-bold')
            self.actions_region = ui.column().classes('bar-modal-footer w-full gap-2')

    def _render_recognition_options(self):
        with ui.expansion('重新识别设置', icon='o_refresh').classes('w-full') as options:
            self._render_rerecognition_settings()
        self.initial_options = options

    def _render_initial_selection(self):
        self.groups_container.clear()
        with self.groups_container:
            from ..rename.input_directory_discovery import source_group_pending
            if source_group_pending(self.task_data):
                ui.label('请核对本任务的来源文件，确认后继续识别。').classes('bar-help')
                ui.button('继续识别来源', icon='o_arrow_forward', on_click=self._confirm_source_group).classes('bar-review-action').props('color=green-7')
                return
            operation, _ = build_review_operation(self.task_data)
            selected = operation.context.get('candidate')
            title = selected.get('title') if isinstance(selected, Mapping) else ''
            ui.label(f'已选择作品：{title}' if title else '尚未选择作品').classes('text-subtitle1 font-bold')
            with ui.column().classes('bar-review-feedback w-full gap-2'):
                render_review_operation(ui, self.task_data, self._review_callbacks(), common_actions=False)

            if title:
                self._render_work_actions(operation.context)
            picker = ui.column().classes('bar-review-picker w-full gap-2')
            picker.set_visibility(self.provider_selection_expanded or not title)
            with picker:
                self._render_provider_link_input()
                candidates = normalize_provider_candidates(self.task_data)
                if candidates:
                    with ui.element('div').classes('bar-provider-grid w-full gap-4'):
                        self.render_provider_column('bangumi', candidates)
                        self.render_provider_column('tmdb', candidates)
            self.season_container = ui.column().classes('bar-review-season w-full gap-2')
            with self.season_container:
                self._render_season_placeholder('')

    def _review_callbacks(self):
        return ReviewOperationCallbacks(
            select_provider_candidate=self._select_provider_candidate,
            dispatch_review_command=self._dispatch_review_command,
            show_provider_candidates=self._show_provider_candidates,
            remove_provider_failure_task=self._remove_provider_failure_task,
            close_review=self._close_review)

    def _render_work_actions(self, context):
        from ..rename.provider_recovery import can_refresh_selection
        from ..rename.special_catalog import plain
        candidate = context.get('candidate')
        with ui.row().classes('bar-review-work-actions w-full items-center gap-2'):
            ui.button('选择／更换作品', icon='o_swap_horiz', on_click=self._show_provider_candidates).props('outline color=blue-7')
            refresh = ui.button('重新读取资料', icon='o_refresh',
                on_click=lambda: self._select_provider_candidate(plain(candidate))).props('outline color=blue-7')
            if not can_refresh_selection(context):
                refresh.disable()
                refresh.tooltip('当前没有可重试的资料读取；可更换作品或展开重新识别设置。')

    def _render_season_placeholder(self, reason):
        from .season_choice import season_choice
        if reason:
            ui.label(reason).classes('bar-help')
        selected = season_choice(ui, (), label='季度')
        for control in (selected.manual, selected.picker, selected.custom):
            control.disable()
        with selected.controls_row:
            ui.button('确认季度并继续', icon='o_check').props('color=blue-7').disable()

    def _render_manual_corrections(self) -> None:
        if self.plan.metadata.get('content_selection') is None:
            with ui.expansion('旧任务高级手动修正', icon='o_tune').classes('bar-advanced-review w-full'):
                self._render_presentation_override()
                self._render_override()
        # Language, subtitle owner and token decisions remain necessary. They
        # are structured content commands, not arbitrary target overrides.

    def _pre_admission_selection(self):
        from .provider_selection_work import current_work, _matches_result
        work = current_work(task_queue, self.task_id)
        return work if work is not None and not work.admitted and not work.cancelled and _matches_result(work, self.task_data) else None

    def _replacement_unfinished(self):
        operation, _ = build_review_operation(self.task_data)
        context = operation.context
        return bool(self.plan is not None and self.task_decision is not None and self.task_decision.confirmed_work
            and (self._pre_admission_selection() is not None or (context.get('kind') == 'provider_selection'
            and context.get('lifecycle') in {'pending', 'failed', 'awaiting_season'})))

    def _render_header(self) -> None:
        unresolved = self.plan is None or self._replacement_unfinished()
        with ui.row().classes("bar-dialog-header w-full items-start gap-4"):
            with ui.column().classes("bar-dialog-header-content gap-1"):
                ui.label("整理任务").classes("bar-dialog-title")
            with ui.row().classes("bar-dialog-header-actions items-center gap-2 flex-wrap justify-end"):
                self.plan_badge = ui.badge(
                    PLAN_STATUS_LABELS.get(
                        self.snapshot.plan_status, self.snapshot.plan_status
                    ) if not unresolved else PlanReviewDialog._header_recognition_text(self),
                    color="orange-8"
                    if unresolved or self.snapshot.plan_status == "need_confirm"
                    else "green-7",
                )
                self.plan_badge.set_visibility(entry_status(self.plan, self.task_data) != "已整理")
                self.review_badge = ui.badge(
                    (completion_status(self.plan, self.task_data) or REVIEW_STATUS_LABELS[self.decision.status]) if not unresolved else '尚未生成计划', color="blue-7"
                )

        with ui.row().classes("bar-confirm-card w-full gap-5 flex-wrap"):
            authoritative_title = ""
            if not unresolved and self.task_decision is not None:
                authoritative_title = str(
                    self.task_decision.confirmed_work.get("title") or ""
                ).strip()
            if not unresolved and not authoritative_title and self.plan.work_identity is not None:
                authoritative_title = str(self.plan.work_identity.title or "").strip()
            self.header_title_value = _meta_item(
                "作品", authoritative_title or PlanReviewDialog._pending_work_title(self)
            )
            self.header_season_value = _meta_item(
                "季度", '待确认' if unresolved else PlanReviewDialog._header_season_text(self)
            )
            self.header_recognition_value = _meta_item(
                "识别状态", PlanReviewDialog._header_recognition_text(self)
            )
            self.header_entry_count_value = _meta_item(
                f"{RENAME_PLAN_TERM}条目", '尚未生成' if unresolved else str(len(self.plan.entries))
            )
            _meta_item("任务 ID", self.task_id[:8], monospace=True)
        ui.label(f"源目录：{self.task_data.get('path', '') if unresolved else self.snapshot.source_root}").classes("bar-path-text")
        if unresolved:
            return

    def _render_matched_seasons(self):
        provider_link = provider_link_review_summary(
            self.plan.metadata.get("provider_link_snapshot")
        )
        if provider_link is not None:
            with ui.expansion(
                "已匹配作品季度",
                icon='o_account_tree',
                value=True,
            ).classes("bar-confirm-card w-full"):
                ui.label(
                    "这些条目将整理到同一部作品的对应季度。"
                ).classes("font-bold")
                for assignment in provider_link["assignments"]:
                    ui.label(str(assignment)).classes("bar-help")
                if not completion_status(self.plan, self.task_data):
                    ui.label(str(provider_link["pending_notice"])).classes("bar-help text-orange-8")


    def _render_rerecognition_settings(self) -> None:
        """Keep the legacy edit fields inside the single task workspace."""

        ui.label(
            "重新识别保留当前任务及来源文件，按本次类型、名称和季度重新检索。旧计划、批准和手动归属将退出。"
        ).classes("bar-help")
        current_media_type = _media_type_from_flags(
            self.task_data.get("is_anime"), self.task_data.get("is_movie")
        )
        self.rerecognition_path = ui.input(
                "来源目录", value=str(self.task_data.get("path") or (self.plan.source_root if self.plan else ''))
        ).props("outlined dense readonly").classes("w-full")
        with ui.row().classes("w-full items-end gap-3 flex-wrap"):
            self.rerecognition_media_type = ui.select(
                MEDIA_OPTIONS, value=current_media_type, label="媒体类型"
            ).props("outlined dense options-dense").classes("w-48")
            self.rerecognition_name = ui.input(
                "检索名称", value=str(self.task_data.get("name") or "")
            ).props("outlined dense clearable").classes("grow")
            self.rerecognition_season = ui.number(
                "季度", value=self.task_data.get("season_id")
            ).props("outlined dense clearable").classes("w-32")
        with ui.row().classes("w-full gap-3 flex-wrap"):
            ui.button(
                "保存并重新识别", icon='o_refresh', on_click=self._restart_recognition
            ).props("outline color=blue-8")

    async def _restart_recognition(self) -> None:
        saved = await _reenqueue_from_workspace(
            self.task_id,
            self.task_data,
            path_value=self.rerecognition_path.value,
            media_type=self.rerecognition_media_type.value,
            name_value=self.rerecognition_name.value,
            season_value=self.rerecognition_season.value,
            opened_evidence=self.retry_open_evidence,
        )
        if saved:
            self._close_review()

    def _render_catalog_context(self) -> None:
        self.catalog_container.clear()
        self._asset_choice_embedded = False
        with self.catalog_container:
            with ui.card().classes('bar-confirm-card w-full'):
                ui.label('已登记的同一作品').classes('font-bold')
                self._render_catalog_context_content()

    async def _recover_missing_catalog(self) -> None:
        from ..rename.replacement_completion import uses_completion_policy
        if uses_completion_policy(self.plan.metadata.get('asset_operation')):
            from .asset_recovery_view import open_asset_recovery
            await open_asset_recovery(task_queue, self.plan.task_id, self._close_review)
            return
        if self.task_decision is None:
            notify("旧任务缺少权威决策状态，请重新识别", type="warning")
            return
        try:
            await ui_disk_call(self.recovery_service.rebuild_catalog,
                self.task_decision,
                self.plan,
                self.review_service.work_catalog,
                self.series_catalog,
            )
            self.task_decision, projected = await ui_disk_call(self.decision_command_service.update,
                self.plan.task_id,
                expected_revision=self.task_decision.revision,
                changes={"recovery_state": "healthy"},
                planner=lambda _state: self.plan,
                task_terminal=_task_terminal_for_projected_plan(
                    self.plan.task_id
                ),
            )
            if projected is None:
                raise DecisionStateError("未生成恢复后的整理计划")
        except Exception as exc:
            notify(f"无法重建作品登记：{exc}", type="negative")
            return
        await await_result(self._adopt_revision(projected))
        notify("已依据历史记录和现存文件重建作品登记", type="positive")

    async def _recover_missing_materialization(self) -> None:
        if self.task_decision is None:
            notify("旧任务缺少权威决策状态，请重新识别", type="warning")
            return
        from .completed_rebuild_view import open_rebuild_dialog
        execution = task_queue.execution_service
        service = execution.rebuild_service
        async def adopt(projected):
            self.task_decision = service.decisions.load_readonly(self.plan.task_id)
            await await_result(self._adopt_revision(projected))
        await open_rebuild_dialog(self.plan.task_id, manager=task_queue, on_generated=adopt)

    def _render_catalog_context_content(self) -> None:
        """Show human-readable prior media state without exposing work UUIDs."""

        try:
            pending_detach = self._catalog_display['pending']
            if isinstance(pending_detach, Exception):
                raise pending_detach
        except ProjectionAuthorityError:
            ui.label(untrusted_projection_snapshot().detail).classes('bar-help')
            return
        except (ValueError, RuntimeError, OSError) as exc:
            ui.label(f'作品登记恢复记录需核查：{exc}').classes('bar-help')
            return
        if pending_detach is not None and pending_detach.phase != 'completed':
            self._render_detach_transaction_recovery(pending_detach.catalog_backend, pending_detach.catalog_key)
            return

        from .work_affiliation_view import render as render_affiliation
        with ui.row().classes('w-full items-center gap-3') as work_actions:
            if render_affiliation(self, ui=ui, notify=notify):
                return
            from .asset_actions_view import render_repair_choice
            render_repair_choice(self)
        from ..rename.work_affiliation import from_plan as affiliation_from_plan
        affiliation = affiliation_from_plan(self.plan)

        if (
            self.plan.metadata.get("mapping_source") == "inventory_review_draft"
            and not self.task_data.get("execution_status")
            and {g.kind for g in self.plan.presentation_groups} != {'movie_continuous'}
        ):
            return
        work = self.plan.work_identity
        group_kinds = {group.kind for group in self.plan.presentation_groups}
        group_kind = next(iter(group_kinds)) if len(group_kinds) == 1 else None
        if group_kind not in {"movie_continuous", "tv_episodes"}:
            return
        if self.plan.metadata.get("recovery_reason") == "catalog_detached":
            with ui.row().classes("bar-callout bar-callout-warning w-full gap-2"):
                ui.icon('o_warning')
                with ui.column().classes("gap-1"):
                    ui.label("作品登记已解除，当前目标路径不能继续使用").classes(
                        "font-bold"
                    )
                    ui.label(
                        "请按当前媒体库配置和原始文件库存重新生成整理计划；"
                        "旧批准不会继承。"
                    ).classes("bar-help")
                    ui.button(
                        "重新生成整理计划",
                        on_click=self._restart_recognition,
                    icon='o_refresh').props("outline color=blue-7 no-caps")
            return
        is_tv = group_kind == "tv_episodes"
        media_label = "剧集" if is_tv else "电影"
        catalog_backend, catalog_key = catalog_backend_for_plan(
            self.plan, self.task_decision
        )
        if (
            self.task_decision is not None
            and self.task_decision.recovery_state == "catalog_detach_pending"
            and catalog_backend in {"work", "series"}
            and catalog_key
        ):
            self._render_detach_transaction_recovery(
                catalog_backend, str(catalog_key)
            )
            return
        frozen_failure_key = catalog_lookup_uses_frozen_key(
            self.task_decision, catalog_backend, catalog_key
        )
        if (work is None or work.status != "confirmed") and not frozen_failure_key:
            return
        try:
            if self._catalog_display['error'] is not None:
                raise self._catalog_display['error']
            match = self._catalog_display['match']
        except Exception:
            if is_fresh_unexecuted_task(self.task_data, self.task_decision):
                return
            recovery = self._catalog_display['recovery']
            with ui.row().classes("bar-callout bar-callout-warning w-full gap-2"):
                ui.icon('o_warning')
                with ui.column().classes("gap-1"):
                    ui.label(user_recovery_detail(
                        recovery.detail if recovery is not None
                        else "旧任务缺少必要记录，请重新识别后恢复。"
                    ))
                    ui.label("暂时无法读取作品登记。请按下方提示处理。").classes(
                        "bar-help"
                    )
                    if recovery is not None and recovery.can_rebuild_catalog:
                        ui.button(
                            "从历史记录重建作品登记",
                            icon='o_restore',
                            on_click=self._recover_missing_catalog,
                        ).props("outline color=blue-7 no-caps")
                    elif recovery is not None and recovery.can_rebuild_plan:
                        ui.button(
                            "核查缺失内容并选择恢复方式",
                            icon='o_refresh',
                            on_click=self._recover_missing_materialization,
                        ).props("outline color=blue-7 no-caps")
                    else:
                        ui.button(
                            "恢复或重新选择源目录",
                            icon='o_folder_open',
                            on_click=self._restart_recognition,
                        ).props("outline color=blue-7 no-caps")
            return
        if catalog_backend == "series":
            if match is None:
                ui.label(
                    "本地系列登记：尚未发现同一 Provider 系列；"
                    "本次真实执行并验证 hardlink 后才会建立登记。"
                ).classes("bar-help")
                return
            with ui.card().classes("bar-confirm-card w-full"):
                ui.label("已登记的同一系列").classes("font-bold")
                ui.label(match.canonical_display_name)
                ui.label(f"目录：{match.canonical_root}").classes("bar-path-text")
                seasons = [str(number) for number in sorted({item.get('season_number')
                    for item in match.season_bindings if type(item.get('season_number')) is int})]
                ui.label(
                    "已登记季度：" + ("、".join(seasons) if seasons else "尚无")
                ).classes("bar-help")
                from .asset_actions_view import render_asset_choice
                render_asset_choice(self, embedded=True, action_row=work_actions)
                self._asset_choice_embedded = True
            if not self._catalog_display['present']:
                ui.label('目标目录当前缺失；请点“核查并补齐”，核对本次文件后生成待批准计划。系列登记保留。').classes('bar-help')
            return
        if match is None:
            if frozen_failure_key:
                with ui.row().classes(
                    "bar-callout bar-callout-warning w-full gap-2"
                ):
                    ui.icon('o_warning')
                    with ui.column().classes("gap-1"):
                        ui.label(
                            "整理计划引用的作品登记不存在，不能只显示错误后结束。"
                        ).classes("font-bold")
                        ui.label(
                            "请从可信历史记录与现存文件重建登记；若源目录也已变化，"
                            "请使用上方“重新识别设置”。"
                        ).classes("bar-help")
                        if self.task_decision is not None:
                            ui.button(
                                "从历史记录重建作品登记",
                                icon='o_restore',
                                on_click=self._recover_missing_catalog,
                            ).props("outline color=blue-7 no-caps")
                return
            ui.label(
                f"本地作品登记：尚未发现同一资料条目的已整理{media_label}；"
                "本次真实执行成功后才会建立登记。"
            ).classes("bar-help")
            if affiliation is not None:
                # The shared chooser reads registrations only when opened.
                return
            suggestions = self._catalog_display['suggestions']
            if suggestions:
                ui.label(
                    f"发现标题和年份相符的既有{media_label}，但资料来源不同。"
                    "只有你确认后才会合并："
                ).classes("bar-help text-orange-8")
                for suggestion in suggestions:
                    with ui.card().classes("bar-confirm-card w-full"):
                        ui.label(
                            f"{suggestion.title}"
                            f"{f' ({suggestion.year})' if suggestion.year else ''}"
                        ).classes("font-bold")
                        ui.label(f"目录：{suggestion.canonical_root}").classes(
                            "bar-path-text"
                        )
                        if is_tv:
                            season_labels = [
                                str(item.get("season_number"))
                                for item in suggestion.seasons
                            ]
                            ui.label(
                                "已登记季度：" + ("、".join(season_labels) if season_labels else "尚无")
                            ).classes("bar-help")
                        else:
                            ui.label(
                                "正片版本：" + "、".join(
                                    str(item.get("display_label") or "默认版本")
                                    for item in suggestion.versions
                                )
                            ).classes("bar-help")
                        ui.button(
                            ("确认归入此系列" if is_tv else "确认是同一电影，追加到此目录"),
                            icon='o_merge_type',
                            on_click=partial(
                                self._link_existing_catalog_work,
                                suggestion.work_key,
                            ),
                        ).props("outline color=blue-8")
        else:
            with ui.column().classes("w-full"):
                ui.label(
                    f"{match.title}{f' ({match.year})' if match.year else ''}"
                )
                ui.label(f"现有目录：{match.canonical_root}").classes(
                    "bar-path-text"
                )
                if is_tv:
                    seasons = [str(item.get("season_number")) for item in match.seasons]
                    ui.label("现有季度：" + ("、".join(seasons) if seasons else "尚无")).classes("bar-help")
                else:
                    labels = [
                        str(item.get("display_label") or "默认版本")
                        for item in match.versions
                    ]
                    ui.label(
                        "现有正片版本：" + ("、".join(labels) if labels else "尚无")
                    ).classes("bar-help")
                from .asset_actions_view import render_asset_choice
                render_asset_choice(self, embedded=True, movie_work_key=match.work_key if not is_tv else None,
                                    action_row=work_actions)
                self._asset_choice_embedded = True
                if is_tv:
                    ui.label("确认后，本任务会归入这个系列目录并写入相应 Season 目录。").classes("bar-help text-blue-8")
                try:
                    from ..rename.target_routing import (
                        resolve_target_root,
                        snapshot_from_config_manager,
                    )
                    current_movie_root = resolve_target_root(
                        work.domain,
                        group_kind,
                        snapshot_from_config_manager(cm),
                    )
                    outside = not str(match.canonical_root).casefold().startswith(
                        str(current_movie_root).rstrip("/\\").casefold() + os.sep
                    )
                except (TypeError, ValueError):
                    outside = False
                if (
                    outside
                    and self.plan.metadata.get("catalog_allow_outside_current_root") is not True
                ):
                    ui.label(
                        f"这个已登记目录不在当前{media_label}目标根下。系统不会自动跨根写入。"
                    ).classes("bar-help text-orange-8 font-bold")
                    ui.button(
                        "继续添加到已登记目录",
                        icon='o_drive_file_move',
                        on_click=partial(
                            self._link_existing_catalog_work, match.work_key
                        ),
                    ).props("outline color=orange-8")
                try:
                    directory_available = match.canonical_root.is_dir()
                except OSError:
                    directory_available = False
                if not directory_available:
                    ui.label('无法确认已登记目录仍然存在，请核查下方目录维护选项。').classes(
                        'bar-help text-orange-8 font-bold')
                with ui.expansion('作品登记维护', icon='o_settings',
                                  value=is_tv or not directory_available).classes('w-full'):
                    with ui.card().classes("bar-confirm-card w-full"):
                        with ui.row().classes("items-center gap-2"):
                            ui.icon('o_drive_file_move', color="blue-8")
                            ui.label("原目录已移动").classes("font-bold")
                        ui.label(
                            "BAR 不会替你移动文件。粘贴完整目录或从文件夹树选择；系统只读核对"
                            "全部登记资产和 hardlink inode，全部通过后仅更新本地作品登记。"
                        ).classes("bar-help")
                        with ui.row(wrap=False).classes("w-full items-center gap-2"):
                            self.catalog_relocation_input = ui.input(
                                label=f"移动后的完整{media_label}目录（可直接粘贴）"
                            ).props("outlined clearable").classes("flex-grow")
                            ui.button(
                                icon='o_folder_open',
                                on_click=self._pick_catalog_relocation_directory,
                            ).props(
                                'flat round color=blue-8 aria-label="从文件夹树选择移动后的目录"'
                            ).tooltip("从文件夹树选择")
                        with ui.row().classes("w-full gap-2 flex-wrap"):
                            ui.button(
                                "核对并重新定位登记",
                                icon='o_verified',
                                on_click=partial(
                                    self._relocate_catalog_work, match.work_key
                                ),
                            ).props("outline color=blue-8")
                    with ui.card().classes("bar-confirm-card w-full"):
                        with ui.row().classes("items-center gap-2"):
                            ui.icon('o_delete_outline', color="red-7")
                            ui.label("原目录已删除").classes("font-bold")
                        ui.label(
                            "仅删除这条失效的本地作品登记，并保存内部归档；"
                            "不会删除、移动或修改 NAS 中的任何文件。"
                        ).classes("bar-help")
                        self.catalog_delete_confirmation = ui.checkbox(
                            f"我确认原{media_label}目录已经不存在"
                        )
                        ui.button(
                            "删除失效的作品登记",
                            icon='o_delete_forever',
                            on_click=partial(
                                self._archive_missing_catalog_work, match.work_key
                            ),
                        ).props("outline color=red-7")

        frozen = str(
            self.plan.metadata.get("target_root_config_fingerprint") or ""
        )
        if frozen:
            try:
                from ..rename.target_routing import snapshot_from_config_manager

                live = snapshot_from_config_manager(cm).fingerprint
            except (TypeError, ValueError):
                live = ""
            if live and live != frozen:
                with ui.row().classes(
                    "bar-callout bar-callout-warning w-full items-start gap-2"
                ):
                    ui.icon('o_info')
                    ui.label(
                        "路径配置已在此整理计划生成后改变。当前计划仍使用已确认目录；"
                        "如需采用新目录，请返回任务点击“重试”生成并重新处理。"
                    )

    async def _link_existing_catalog_work(self, work_key: str) -> None:
        from .work_affiliation_view import show
        await show(self, ui=ui, notify=notify, selected_key=work_key)

    def _render_series_catalog_recovery_actions(
        self, series_key: str, media_label: str
    ) -> None:
        with ui.row().classes("bar-callout bar-callout-warning w-full gap-2"):
            ui.icon('o_warning')
            ui.label(
                "作品登记目录不存在。请选择原目录已移动或原目录已删除；"
                "BAR 不会替你移动或删除任何媒体文件。"
            )
        with ui.card().classes("bar-confirm-card w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.icon('o_drive_file_move', color="blue-8")
                ui.label("原目录已移动").classes("font-bold")
            ui.label(
                "粘贴完整目录或从文件夹树选择。系统会按所有已登记季度的历史记录"
                "逐文件核对，全部通过后才更新系列登记。"
            ).classes("bar-help")
            with ui.row(wrap=False).classes("w-full items-center gap-2"):
                self.catalog_relocation_input = ui.input(
                    label=f"移动后的完整{media_label}目录（可直接粘贴）"
                ).props("outlined clearable").classes("flex-grow")
                ui.button(
                    icon='o_folder_open',
                    on_click=self._pick_catalog_relocation_directory,
                ).props(
                    'flat round color=blue-8 aria-label="从文件夹树选择移动后的目录"'
                ).tooltip("从文件夹树选择")
            ui.button(
                "核对并重新定位登记",
                icon='o_verified',
                on_click=partial(self._relocate_series_catalog, series_key),
            ).props("outline color=blue-8")
        with ui.card().classes("bar-confirm-card w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.icon('o_delete_outline', color="red-7")
                ui.label("原目录已删除").classes("font-bold")
            ui.label(
                "仅归档这条失效的系列登记并清除本任务的旧系列绑定；"
                "不会删除、移动或修改 NAS 中的任何文件。"
            ).classes("bar-help")
            self.catalog_delete_confirmation = ui.checkbox(
                f"我确认原{media_label}目录已经不存在"
            )
            ui.button(
                "删除失效的作品登记",
                icon='o_delete_forever',
                on_click=partial(self._archive_missing_series_catalog, series_key),
            ).props("outline color=red-7")

    async def _relocate_series_catalog(self, series_key: str) -> None:
        value = str(getattr(self.catalog_relocation_input, "value", "") or "").strip()
        if not value:
            notify("请先填写移动后的完整作品目录", type="warning")
            return
        try:
            from ..rename.target_routing import (
                resolve_target_root,
                snapshot_from_config_manager,
            )
            work = self.plan.work_identity
            domain = (
                work.domain if work is not None
                else "animation" if self.task_data.get("is_anime") is True
                else "general"
            )
            allowed_root = resolve_target_root(
                domain, "tv_episodes", snapshot_from_config_manager(cm)
            )
            await ui_disk_call(self.series_catalog.relocate,
                series_key,
                Path(value),
                allowed_root=allowed_root,
                record_root=RECORD_PATH,
                task_root=TASK_PATH,
            )
        except (SeriesCatalogError, TypeError, ValueError, RuntimeError) as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        self._render_catalog_context()
        notify(
            "系列登记已更新；BAR 未移动、复制或删除任何媒体文件。"
            "请点击重试生成使用新目录的整理计划",
            type="positive",
        )

    async def _archive_missing_series_catalog(self, series_key: str) -> None:
        confirmed = bool(
            getattr(self.catalog_delete_confirmation, "value", False)
        )
        if not confirmed:
            notify("请先确认原作品目录已经不存在", type="warning")
            return
        await self._detach_missing_catalog("series", series_key)

    async def _relocate_catalog_work(self, work_key: str) -> None:
        value = str(getattr(self.catalog_relocation_input, "value", "") or "").strip()
        if not value:
            notify("请先填写移动后的完整作品目录", type="warning")
            return
        try:
            from ..rename.target_routing import (
                resolve_target_root,
                snapshot_from_config_manager,
            )
            work = self.plan.work_identity
            domain = (
                work.domain if work is not None
                else "animation" if self.task_data.get("is_anime") is True
                else "general"
            )
            group_kinds = {group.kind for group in self.plan.presentation_groups}
            if group_kinds not in ({"movie_continuous"}, {"tv_episodes"}):
                raise ValueError("当前整理计划没有单一可恢复观看形式")
            allowed_root = resolve_target_root(
                domain, next(iter(group_kinds)), snapshot_from_config_manager(cm)
            )
            await ui_disk_call(self.review_service.work_catalog.relocate,
                work_key,
                Path(value),
                allowed_root=allowed_root,
                task_root=TASK_PATH,
            )
        except (PlanReviewError, TypeError, ValueError, RuntimeError) as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        self._catalog_display = await ui_disk_call(_prepare_catalog_display, self.plan,
            self.task_decision, self.task_data, self.review_service, self.series_catalog,
            self.catalog_detach_service, self.recovery_service)
        self._render_catalog_context()
        notify("作品登记已更新；BAR 未移动、复制或删除任何媒体文件", type="positive")

    async def _pick_catalog_relocation_directory(self) -> None:
        current = str(
            getattr(self.catalog_relocation_input, "value", "")
            or cm.get_config("docker_mnt")
            or "/volume1"
        )
        result = await local_file_picker(
            current,
            multiple=False,
            title="选择移动后的作品目录",
        )
        if isinstance(result, (list, tuple)) and result:
            self.catalog_relocation_input.value = str(result[0])
            self.catalog_relocation_input.update()

    async def _archive_missing_catalog_work(self, work_key: str) -> None:
        confirmed = bool(
            getattr(self.catalog_delete_confirmation, "value", False)
        )
        if not confirmed:
            notify("请先确认原电影目录已经不存在", type="warning")
            return
        await self._detach_missing_catalog("work", work_key)

    async def _detach_missing_catalog(self, backend: str, catalog_key: str) -> None:
        if self.task_decision is None:
            notify("旧任务缺少权威决策状态，请重新识别", type="warning")
            return
        try:
            decision, revised, transaction = await ui_disk_call(self.catalog_detach_service.execute,
                self.plan.task_id,
                catalog_backend=backend,
                catalog_key=catalog_key,
            )
        except (
            RecoveryTransactionError,
            DecisionStateError,
            PlanStoreError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        if transaction.phase != "completed":
            notify("作品登记恢复事务尚未完成，执行仍被阻断", type="warning")
            return
        self.task_decision = decision
        await await_result(self._adopt_revision(revised))
        notify(
            "失效作品登记已安全归档，NAS 文件未被删除；"
            "已生成新的未批准整理计划，请重新检查并批准",
            type="positive",
        )

    def _render_detach_transaction_recovery(
        self, backend: str, catalog_key: str
    ) -> None:
        with ui.row().classes("bar-callout bar-callout-warning w-full gap-2"):
            ui.icon('o_warning')
            with ui.column().classes("gap-1"):
                ui.label("作品登记删除事务尚未完成").classes("font-bold")
                ui.label(
                    "在事务完成前不会执行整理计划。请继续事务；若阶段记录缺失或损坏，"
                    "请先按 Catalog 与内部归档证据对账。"
                ).classes("bar-help")
                try:
                    transaction = self._catalog_display['pending']
                    if isinstance(transaction, Exception):
                        raise transaction
                except RecoveryTransactionError:
                    transaction = None
                if transaction is None:
                    ui.button(
                        "对账并继续删除登记",
                        icon='o_rule',
                        on_click=partial(
                            self._reconcile_catalog_detach, backend, catalog_key
                        ),
                    ).props("outline color=orange-8 no-caps")
                elif transaction.phase == 'prepared':
                    ui.button('取消旧事务', icon='o_refresh', on_click=self._cancel_prepared_detach).classes(
                        'bar-review-action').props('outline color=blue-7 no-caps')
                else:
                    ui.button(
                        "继续删除登记事务",
                        icon='o_play_arrow',
                        on_click=partial(
                            self._detach_missing_catalog, backend, catalog_key
                        ),
                    ).props("outline color=blue-8 no-caps")

    async def _cancel_prepared_detach(self):
        try:
            decision, revised = await ui_disk_call(self.catalog_detach_service.cancel_prepared, self.plan.task_id)
            self.task_decision = decision
            await await_result(self._adopt_revision(revised))
            notify('已取消未执行的旧登记事务，媒体和登记保持不变', type='positive')
        except (OSError, ValueError, RuntimeError) as exc:
            notify(str(exc), type='negative')

    async def _reconcile_catalog_detach(self, backend: str, catalog_key: str) -> None:
        try:
            decision, revised, transaction = (
                await ui_disk_call(self.catalog_detach_service.reconcile_and_execute,
                    self.plan.task_id,
                    catalog_backend=backend,
                    catalog_key=catalog_key,
                )
            )
        except (
            RecoveryTransactionError,
            DecisionStateError,
            PlanStoreError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        self.task_decision = decision
        await await_result(self._adopt_revision(revised))
        if transaction is None:
            notify("Catalog 仍完整，已安全取消待处理删除事务", type="positive")
        else:
            notify(
                "已从 Catalog 归档证据恢复并完成事务；请重新检查整理计划",
                type="positive",
            )

    def _render_groups(self) -> None:
        self.groups_container.clear()
        with self.groups_container:
            from ..rename.input_directory_discovery import source_group_pending
            if source_group_pending(self.task_data):
                ui.label('请核对本任务的来源文件，确认后继续识别。').classes('bar-help')
                ui.button('继续识别来源', icon='o_arrow_forward', on_click=self._confirm_source_group).classes('bar-review-action').props('color=green-7')
                return
            operation, _ = build_review_operation(self.task_data)
            pending_read = self._pre_admission_selection()
            selected = pending_read.candidate if pending_read is not None else operation.context.get('candidate')
            work = self.snapshot.work_identity
            unresolved = str(work.get('status')) != 'confirmed' or self._replacement_unfinished()
            title = selected.get('title') if isinstance(selected, Mapping) else ''
            ui.label(('已选择作品：' + str(title)) if unresolved and title else
                     ('已确认作品：' + str(work.get('title'))) if not unresolved else '尚未选择作品').classes('text-subtitle1 font-bold')
            with ui.column().classes('w-full gap-2') as summary:
                self.current_work_summary = summary
                if not unresolved:
                    self._render_selected_provider_identity(operation.context)
                elif title:
                    ui.label('作品选择已保存，等待完成本次确认。').classes('bar-help')
                for group in self.snapshot.presentation_groups:
                    ui.label(str(group['display_name'])).classes('bar-evidence')
                    if group['preferred_version_ref']:
                        ui.label(f"偏好版本：{group['preferred_version_ref']}").classes('bar-help')
            with ui.column().classes('bar-review-feedback w-full gap-2'):
                self._render_selection_operation()

            if title and unresolved:
                self._render_work_actions(operation.context)
            if self.provider_selection_expanded or (unresolved and not title) or (self._replacement_unfinished() and operation.context.get('lifecycle') == 'failed'):
                candidates = normalize_provider_candidates(dict(self.plan.metadata))
                if not candidates and not getattr(self.task_decision, 'confirmed_work', None):
                    candidates = normalize_provider_candidates(self.task_data)
                with ui.column().classes('bar-review-picker w-full gap-2'):
                    if title or not unresolved:
                        ui.label('选择正确的作品').classes('text-subtitle2')
                    warning = candidate_type_warning(self.task_data.get('is_movie', self.plan.metadata.get('is_movie')), candidates) if candidates else ''
                    if warning:
                        ui.label(warning).classes('bar-help text-orange-8')
                    self._render_provider_link_input()
                    with ui.element('div').classes('bar-provider-grid w-full gap-4'):
                        self.render_provider_column('bangumi', candidates)
                        self.render_provider_column('tmdb', candidates)
            self.season_container = ui.column().classes('bar-review-season w-full gap-2')
            with self.season_container:
                from .general_season_view import render_unresolved_tmdb_season
                rendered = render_unresolved_tmdb_season(self, ui) if self.plan.metadata.get('content_policy') is not None else False
                if not rendered:
                    if not unresolved and self._general_tv_provider() is not None:
                        self._render_general_tv_season()
                    elif unresolved:
                        self._render_season_placeholder('选择作品后，可以独立整理并手动指定季度。')
                    else:
                        self.season_container.set_visibility(False)

    render_groups = _render_groups

    def _general_tv_provider(self):
        from .general_season_view import general_tv_provider
        return general_tv_provider(self)

    def _render_general_tv_season(self):
        from .general_season_view import render_general_tv_season
        render_general_tv_season(self, ui)

    async def _change_general_tv_season(self):
        from .general_season_view import change_general_tv_season
        await change_general_tv_season(self, ui)

    def _render_selection_operation(self) -> None:
        if self.plan.metadata.get('content_policy') is None:
            ui.label('旧季度确认入口已停用。请安全结束此任务后重新添加来源；源文件不会删除。').classes('bar-help')
            return
        from .tv_content_actions import revision_progress
        progress = revision_progress(self)
        render_review_operation(
            ui,
            self.task_data,
            ReviewOperationCallbacks(
                select_provider_candidate=self._select_provider_candidate,
                dispatch_review_command=self._dispatch_review_command,
                show_provider_candidates=self._show_provider_candidates,
                remove_provider_failure_task=self._remove_provider_failure_task,
                close_review=self._close_review,
            ),
            progress=progress, common_actions=False,
        )
        proof = self.task_decision.pending_tv_selection if self.task_decision is not None else None
        if proof is not None:
            from .tv_content_actions import run_revision
            fingerprint, revision = PlanStore.fingerprint(self.plan), self.task_decision.revision
            request_id, task_id = proof.to_dict()['request_id'], self.plan.task_id
            service = self.review_service
            ui.button('取消本次季度关系确认', on_click=lambda: run_revision(self,
                submit=lambda: service.cancel_tv_boundary_completion(task_id, fingerprint, request_id=request_id),
                expected_revision=revision, expected_fingerprint=fingerprint, notify=notify,
                message='已取消本次关系确认，原内容取舍已保留。'), icon='o_close').props('flat').bind_enabled_from(
                    progress, 'running', backward=lambda busy: not busy)

    def _dispatch_review_command(
        self, review_command: Mapping[str, object]
    ) -> Awaitable[None] | None:
        """Dispatch one frozen command once; explanations never grant access."""

        if self.plan.metadata.get('content_policy') is not None:
            if self.task_decision is None:
                notify('页面已经更新，请刷新后重新选择。', type='warning')
                return
            from .tv_boundary_view import confirm
            return confirm(self, dict(review_command), ui=ui, notify=notify)

        notify('旧季度确认入口已停用，请安全结束此任务后重新添加来源；源文件不会删除。', type='warning')


    async def _remove_provider_failure_task(self) -> None:
        operation, _unused = build_review_operation(self.task_data)
        if operation.context.get("lifecycle") not in {
            "failed", "awaiting_user", "interrupted", "legacy_stale",
        }:
            notify("任务状态已经变化，请刷新后重试", type="warning")
            return
        task_id = self.plan.task_id if getattr(self, "plan", None) else str(
            self.task_data.get("uuid") or ""
        )
        try:
            await task_queue.abandon_and_remove(
                task_id,
                record_root=RECORD_PATH,
                plan_root=PLAN_PATH,
                review_root=PLAN_REVIEW_PATH,
            )
        except (OSError, PlanExecutionError):
            notify("任务暂时无法移除，请稍后再试", type="negative")
            return
        if getattr(self, 'is_deleted', False) or getattr(self, 'exit_requested', False):
            return
        self._close_review()
        try:
            from . import data_table_page

            data_table_page.refresh_dashboard()
        except (AttributeError, RuntimeError):
            pass
        notify("任务已移除；不会修改媒体文件或作品信息", type="positive")

    def _render_completion_workflow(self) -> None:
        self._refresh_movie_primary_action()
        self.completion_container.clear()
        with self.completion_container:
            status = completion_status(self.plan, self.task_data)
            if status:
                ui.label(status).classes('font-bold text-green-8')
                ui.label('可使用“核查并补齐”检查现有整理结果。').classes('bar-help')
                return
            mapping_source = str(self.plan.metadata.get("mapping_source") or "")
            if mapping_source == "inventory_smart_completion":
                if not self.snapshot.issues:
                    with ui.row().classes(
                        "bar-callout bar-callout-success w-full items-start gap-2"
                    ):
                        ui.icon('o_task_alt', color="green-7", size="22px")
                        with ui.column().classes("gap-1"):
                            ui.label(
                                "整理计划已生成，尚未创建硬链接"
                            ).classes("font-bold text-green-8")
                            ui.label(
                                "请先核对下方源文件与建议目标，然后点击"
                                "“批准整理计划”；批准完成后，再点击"
                                "“执行整理计划”才会创建硬链接。"
                            ).classes("bar-help")
                            policy = self.plan.execution_policy
                            if (
                                policy is None or policy.dry_run
                            ):
                                ui.label(
                                    "当前整理计划是只读预演版本，不能创建硬链接。"
                                    "请在真实执行环境对任务点击“重试”，重新生成并处理计划。"
                                ).classes("bar-help text-orange-9 font-bold")
                            else:
                                ui.label(
                                    "当前任务允许实际整理；执行后请按历史记录"
                                    "独立核对文件。"
                                ).classes("bar-help text-red-8 font-bold")
                group = (
                    self.plan.presentation_groups[0]
                    if self.plan.presentation_groups else None
                )
                if group is not None:
                    self._render_library_choices()
                    primary = self.plan.work_identity.primary_provider if self.plan.work_identity else None
                    if primary is not None and not is_confirmed_movie_plan(self.plan):
                        self._render_completion_choice(group, selected_kind="tv_episodes")
                return
            if mapping_source != "inventory_review_draft":
                return
            work = self.snapshot.work_identity
            confirmed = str(work.get("status")) == "confirmed"
            with ui.column().classes("w-full gap-2"):
                if not confirmed:
                    ui.label("第 1 步：请先点击上方“选择这个作品”。").classes(
                        "bar-help text-orange-8 font-bold"
                    )
                    return
                group = self.plan.presentation_groups[0] if self.plan.presentation_groups else None
                if group is None:
                    ui.label("当前草案没有可用观看形式，请重新识别任务。").classes(
                        "bar-error-text"
                    )
                    return
                self._render_library_choices()
                video_options = {}
                for entry in self.plan.entries:
                    if entry.media_kind != "video":
                        continue
                    try:
                        label = str(entry.source_path.relative_to(self.plan.source_root))
                    except ValueError:
                        label = entry.source_path.name
                    video_options[str(entry.source_path)] = label
                self._render_completion_choice(
                    group,
                    selected_kind=None,
                    video_options=video_options,
                )

    def _render_library_choices(self) -> None:
        ui.label("保存位置").classes("text-subtitle2 font-bold")
        primary = (
            self.plan.work_identity.primary_provider
            if self.plan.work_identity is not None else None
        )
        movie = is_confirmed_movie_plan(self.plan)
        with ui.row().classes(
            "bar-completion-choice-row w-full gap-3 flex-wrap"
        ):
            for domain, label in (
                ("animation", "动画电影" if movie else "动画媒体库"),
                ("general", "普通电影" if movie else "普通影视媒体库"),
            ):
                selected = (
                    self.task_decision is not None
                    and self.task_decision.library_domain == domain
                )
                button = ui.button(
                    label,
                    on_click=partial(self._select_library_domain, domain),
                icon='o_animation' if domain == 'animation' else 'o_video_library').props(
                    ("color=blue-7 text-color=white no-caps"
                     if selected else "outline color=blue-7 no-caps ")
                    + f" aria-pressed={'true' if selected else 'false'}"
                ).classes("bar-completion-choice")
                if selected:
                    button.tooltip("已选择；生成新版整理计划后应用")

    def _video_options(self) -> Dict[str, str]:
        options: Dict[str, str] = {}
        entries = list(self.plan.entries)
        context = self.plan.metadata.get('inventory_completion_context') or {}
        if context.get('origin_plan_fingerprint'):
            try:
                entries += list(self.review_service.plan_store.load_version(
                    self.plan.task_id, context['origin_plan_fingerprint']).entries)
            except (ValueError, RuntimeError, OSError):
                pass  # The edit command reports a missing history before mutation.
        for entry in entries:
            if entry.media_kind != "video":
                continue
            try:
                label = str(entry.source_path.relative_to(self.plan.source_root))
            except ValueError:
                label = entry.source_path.name
            options[str(entry.source_path)] = label
        return options

    def _selected_movie_primary_sources(self) -> List[str]:
        selected: List[str] = []
        for entry in self.plan.entries:
            asset = entry.presentation_asset
            if (
                entry.media_kind == "video"
                and asset is not None
                and asset.asset_role.kind == "content_version"
            ):
                selected.append(str(entry.source_path))
        return selected

    def _render_movie_primary_picker(
        self,
        video_options: Dict[str, str],
        selected_values: Sequence[str],
    ) -> None:
        self.movie_primary_video_input = ui.select(
            video_options,
            value=list(selected_values),
            label="选择电影正片版本（可多选）",
            with_input=True,
            multiple=True,
        ).props(
            "outlined clearable use-input use-chips input-debounce=0 options-dense "
            "behavior=menu popup-content-class=bar-movie-primary-menu"
        ).classes("bar-movie-primary-picker w-full")
        if not selected_values and len(video_options) == 1:
            self.movie_primary_video_input.value = [next(iter(video_options))]
            self.movie_primary_video_input.update()

        picker = self.movie_primary_video_input
        with picker.add_slot("append"):
            confirm = ui.button("确定", icon='o_check').props(
                "flat dense color=blue-7 no-caps"
            ).classes("bar-movie-primary-confirm")
        confirm.set_visibility(False)

        async def close_popup(_) -> None:
            await picker.run_method("hidePopup")

        confirm.on(
            "click",
            close_popup,
            js_handler="(event) => { event.stopPropagation(); emit(); }",
        )
        picker.on("popup-show", lambda _: confirm.set_visibility(True))
        picker.on("popup-hide", lambda _: confirm.set_visibility(False))

    def _render_completion_choice(
        self,
        group,
        *,
        selected_kind: Optional[str],
        video_options: Optional[Dict[str, str]] = None,
    ) -> None:
        if video_options is None:
            video_options = self._video_options()
        selected_values = (
            self._selected_movie_primary_sources()
            if selected_kind == "movie_continuous" else []
        )
        with ui.column().classes("bar-completion-choice-card w-full gap-2"):
            binding = (
                self.task_decision.season_binding
                if self.task_decision is not None else None
            )
            raw_season = binding.get("season_number") if binding else None
            source = str(binding.get("decision_source") or "") if binding else ""
            try:
                season_value = int(raw_season) if raw_season not in (None, "") else 1
            except (TypeError, ValueError):
                season_value = 1
            if source in {"", "unset", "legacy_placeholder"}:
                season_value = 1
            # A trustworthy provider/path season is a decision, not a field the
            # user has to repeat.  Expose an editor only for legacy/ambiguous
            # plans; this prevents accidental Season 00/01 overrides.
            self.tv_season_input = None
            needs_season_choice = (
                group.kind == "tv_episodes" or selected_kind == "tv_episodes"
            )
            if needs_season_choice and source in {"", "unset", "legacy_placeholder"}:
                self.tv_season_input = ui.number(
                    "归入季度（仅在无法自动识别时填写）",
                    value=season_value, min=1, precision=0,
                ).props("outlined dense").classes("w-72")
                self.tv_season_input.tooltip(
                    "旧计划未记录季度来源；已建议第 1 季，点击剧集整理时才会确认。"
                )
            primary = self.plan.work_identity.primary_provider if self.plan.work_identity else None
            movie_confirmed = is_confirmed_movie_plan(self.plan)
            if not movie_confirmed:
                with ui.row().classes("bar-completion-choice-row w-full gap-3 flex-wrap"):
                    ui.button(
                        "重新生成剧集整理计划" if self.plan.metadata.get("mapping_source") == "inventory_smart_completion"
                        else "生成剧集整理计划",
                        icon='o_video_library',
                        on_click=partial(
                            self._complete_inventory_draft,
                            group.group_id,
                            "tv_episodes",
                        ),
                    ).props("outline color=blue-7 no-caps").classes("bar-completion-choice")

    def _inventory_draft_for_completion(self):
        return _inventory_draft_for_completion(self.plan, self.review_service)

    def _series_binding_for_tv(self, target_root):
        return _series_binding_for_tv(self.plan, self.task_decision, self.series_catalog, target_root)

    def _edit_movie_version_groups(self, group_id: str) -> None:
        from .asset_choice_forms import show_movie_versions
        show_movie_versions(self, group_id, ui=ui, notify=notify, complete_movie=PlanReviewDialog._complete_movie_with_primary)

    async def _complete_movie_with_primary(self, group_id: str, *, version_groups=None, excluded_sources=()) -> None:
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
        selected = getattr(self, "movie_primary_video_input", None)
        selected_value = selected.value if selected is not None else None
        if not selected_value:
            notify("请先在上方选择至少一个电影正片版本", type="warning")
            return
        selected_values = (
            list(selected_value)
            if isinstance(selected_value, (list, tuple, set))
            else [selected_value]
        )
        if self.task_decision.content_selection is not None:
            from .movie_content_actions import complete
            return await await_result(complete(self, group_id, selected=selected_values, groups=version_groups,
                            excluded=excluded_sources, notify=notify))
        from ..rename.target_routing import (
            resolve_target_root,
            snapshot_from_config_manager,
        )
        draft = None
        try:
            snapshot = snapshot_from_config_manager(cm)
            if self.plan.work_identity is None:
                raise ValueError("整理计划尚未确认作品域")
            domain = self._completion_domain()
            if domain is None:
                return
            draft = self._inventory_draft_for_completion()
            target_root = resolve_target_root(
                domain,
                "movie_continuous",
                snapshot,
            )
            revised = self.review_service.complete_inventory_draft(
                draft.task_id,
                PlanStore.fingerprint(draft),
                group_id=group_id,
                group_kind="movie_continuous",
                target_root=target_root,
                reason=(
                    f"用户明确选择 {len(selected_values)} 个电影正片版本；"
                    "版本组与未整理范围将冻结在当前决定中"
                ),
                primary_video_sources=tuple(
                    Path(str(value)) for value in selected_values
                ),
                movie_version_groups=version_groups,
                excluded_movie_sources=excluded_sources,
                root_snapshot=snapshot.to_dict(),
                work_domain=domain,
                persist_revision=False,
                _source_plan=draft,
                _revision_parent_fingerprint=PlanStore.fingerprint(self.plan),
            )
            revised_metadata = (
                revised.metadata if isinstance(revised.metadata, Mapping) else {}
            )
            work_key = str(
                revised_metadata.get("catalog_work_key")
                or revised_metadata.get("proposed_work_key")
                or ""
            ).strip()
            self.task_decision, projected = self.decision_command_service.update(
                self.plan.task_id,
                expected_revision=self.task_decision.revision,
                changes={
                    "library_domain": domain,
                    "layout_mode": "movie_versions",
                    "movie_version_selection": self.review_service.movie_selection_for_completion(revised),
                    "recovery_state": "healthy",
                    "series_binding": None,
                    "catalog_link_intent": (
                        {
                            "work_key": work_key,
                            "catalog_revision": int(
                                revised_metadata.get("catalog_revision") or 0
                            ),
                        }
                        if work_key else None
                    ),
                },
                planner=lambda _state: revised,
                task_terminal=_task_terminal_for_projected_plan(self.plan.task_id),
            )
            if projected is None:
                raise DecisionStateError("未生成 DecisionState 对应的 Plan")
            revised = projected
        except (DecisionStateError, PlanReviewError, TypeError, ValueError) as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        await await_result(self._adopt_revision(revised))
        notify("电影正片版本、特典和字幕已生成新版整理计划；尚未执行文件操作。")

    async def _complete_inventory_draft(self, group_id: str, group_kind: str) -> None:
        from ..rename.target_routing import snapshot_from_config_manager
        from .tv_content_actions import run_revision
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
        domain = self._completion_domain()
        if domain is None:
            return
        plan, state = self.plan, self.task_decision
        if plan.work_identity is None:
            notify("整理计划尚未确认作品域", type="warning")
            return
        snapshot = snapshot_from_config_manager(cm)
        season = getattr(self, 'tv_season_input', None)
        season = season.value if season is not None else None
        service, command, catalog = self.review_service, self.decision_command_service, self.series_catalog
        saved = await run_revision(self,
            submit=lambda: _complete_inventory_plan(plan, state, service, command, catalog,
                group_id, group_kind, snapshot, domain, season),
            expected_revision=state.revision, expected_fingerprint=PlanStore.fingerprint(plan),
            notify=notify, message='', quiet=True)
        if saved is not None:
            if self.snapshot.issues:
                notify(f"已完成可确定项目；仍有 {len(self.snapshot.issues)} 个结构问题需要确认", type="warning")
            else:
                notify("智能整理完成；请核对文件列表后批准全部", type="positive")

    def _completion_domain(self) -> Optional[str]:
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别生成新整理计划", type="warning")
            return None
        selected = str(self.task_decision.library_domain or "").strip()
        if selected not in {"animation", "general"}:
            notify(
                "请先选择保存到“动画媒体库”还是“普通影视媒体库”",
                type="warning",
            )
            return None
        return selected

    async def _select_library_domain(self, domain: str) -> None:
        if domain not in {"animation", "general"}:
            return
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
        if is_confirmed_movie_plan(self.plan):
            from ..rename.movie_work_correction import observe_domain, prepare_observed
            from ..rename.initial_movie_completion import commit
            from ..rename.work_affiliation import AffiliationRequired
            try:
                observation = await ui_disk_call(observe_domain, self.review_service, self.plan.task_id,
                    PlanStore.fingerprint(self.plan), expected_revision=self.task_decision.revision, domain=domain)
                intent = await ui_disk_call(prepare_observed, self.review_service, observation)
                projected = await ui_disk_call(commit, self.review_service, intent)
            except AffiliationRequired as exc:
                from .work_affiliation_view import show
                await show(self, ui=ui, notify=notify, correction_inputs=exc.inputs)
                return
            except (ValueError, RuntimeError, OSError) as exc:
                notify(str(exc), type='warning')
                return
            await await_result(self._adopt_revision(projected))
            notify('保存位置已更新，请核对新目标并重新批准。', type='positive')
            return
        try:
            self.task_decision, projected = await ui_disk_call(self.decision_command_service.update,
                self.plan.task_id,
                expected_revision=self.task_decision.revision,
                changes={"library_domain": domain},
                planner=lambda _state: self.plan,
                task_terminal=_task_terminal_for_projected_plan(
                    self.plan.task_id
                ),
            )
            if projected is None:
                raise DecisionStateError("未生成媒体库切换后的整理计划")
        except DecisionStateError as exc:
            notify(str(exc), type="warning")
            return
        await await_result(self._adopt_revision(projected))
        notify(
            "保存位置已更新；请生成新版整理计划并核对目标目录，尚未执行文件操作。",
            type="warning",
        )

    async def _change_group_kind(self, group_id: str, kind: str) -> None:
        try:
            revised = await ui_disk_call(self.review_service.revise_presentation_group_kind,
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                group_id=group_id,
                kind=kind,
                reason="用户确认本地文件的实际播放形式",
            )
        except PlanReviewError as exc:
            notify(localize_plan_term(exc))
            return
        await await_result(self._adopt_revision(revised))
        self._render_groups()
        self._render_issues()
        self._render_entries()
        notify("观看形式已更新；作品条目保持不变，尚未执行或移动文件。")

    def _render_provider_candidate(self, candidate: Dict[str, object]) -> None:
        poster = provider_candidate_poster(candidate)
        namespace = str(candidate.get("namespace") or "未知来源").upper()
        entity_type = str(candidate.get("entity_type") or "")
        type_label = ENTITY_TYPE_LABELS.get(entity_type, entity_type or "类型未知")
        provider_id = str(candidate.get("provider_id") or "未知")
        url = provider_candidate_url(candidate)
        capability = _candidate_capability(self, candidate)
        # Search observations can lack type details. The initial click reads
        # that exact item first; select() applies the same capability gate.
        actionable = bool(url) if self.plan is None else capability.actionable
        with ui.card().classes("bar-candidate-card w-full"):
            if poster:
                ui.image(poster).classes("bar-candidate-poster").props(
                    'fit="cover" loading="lazy"'
                )
            with ui.column().classes("grow min-w-0 gap-2"):
                ui.label(str(candidate.get("title") or "未命名作品")).classes(
                    "text-subtitle1 font-bold"
                )
                ui.label(
                    f"{type_label} · {candidate.get('year') or '年份未知'} · "
                    f"{namespace} #{provider_id}"
                ).classes("bar-evidence")
                reasons = candidate.get("match_reasons") or ()
                if reasons:
                    ui.label("；".join(str(item) for item in reasons)).classes(
                        "bar-help"
                    )
                if candidate.get("match_eligible") is False:
                    ui.label(
                        "低置信参考：标题证据不足，不会自动确认或改变观看形式。"
                    ).classes("bar-help text-orange-8")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    select_button = ui.button(
                        "选择这个作品",
                        on_click=(
                            partial(self._select_provider_candidate, candidate)
                            if actionable else None
                        ),
                        icon='o_check_circle',
                    ).props("color=blue-7 no-caps")
                    select_button.set_enabled(actionable)
                    if not actionable:
                        select_button.tooltip(capability.message)
                        ui.label(capability.message).classes(
                            "bar-help text-orange-8"
                        )
                    if url:
                        ui.link("打开资料页", url, new_tab=True).classes(
                            "bar-candidate-link"
                        )

    def _render_selected_provider_identity(self, context) -> None:
        identity = self.plan.work_identity.primary_provider
        candidate = {
            "namespace": identity.namespace,
            "entity_type": identity.entity_type,
            "provider_id": identity.provider_id,
            "title": identity.title,
            "year": identity.year,
            "poster_url": identity.poster_url,
            "source_url": identity.source_url,
        }
        poster = provider_candidate_poster(candidate)
        url = provider_candidate_url(candidate)
        type_label = ENTITY_TYPE_LABELS.get(identity.entity_type, "类型未知")
        with ui.row().classes("w-full items-start gap-3 flex-nowrap"):
            if poster:
                ui.image(poster).classes("bar-candidate-poster").props(
                    'fit="cover" loading="lazy"'
                )
            with ui.column().classes("grow min-w-0 gap-2"):
                ui.label(identity.title or "未命名作品").classes(
                    "text-subtitle1 font-bold"
                )
                ui.label(
                    f"{type_label} · {identity.year or '年份未知'} · "
                    f"{identity.namespace.upper()} #{identity.provider_id}"
                ).classes("bar-evidence")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    if url:
                        ui.link("打开资料页", url, new_tab=True).classes(
                            "bar-candidate-link"
                        )

                self._render_work_actions(context)

    def _show_provider_candidates(self) -> None:
        self.provider_selection_expanded = True
        if self.plan is None:
            self._render_initial_selection()
        else:
            self._render_groups()

    def _render_provider_column(
        self,
        namespace: str,
        candidates: Sequence[Dict[str, object]],
    ) -> None:
        provider_label = "Bangumi" if namespace == "bangumi" else "TMDB"
        visible = provider_candidates_for_display(candidates, namespace)
        with ui.column().classes("bar-provider-column gap-3"):
            ui.label(f"{provider_label} 结果（最多 4 项）").classes(
                "text-subtitle2 font-bold"
            )
            if not visible:
                ui.label(
                    "自动搜索没有可显示结果；仍可粘贴人工找到的详情页链接。"
                ).classes("bar-help text-grey-7")
            for candidate in visible:
                self._render_provider_candidate(candidate)

    render_provider_column = _render_provider_column

    def _render_provider_link_input(self) -> None:
        field = getattr(self, 'provider_url_input', None)
        value = getattr(field, 'value', '')
        if field is None:
            from .provider_selection_work import current_work, _matches_result
            work = current_work(task_queue, self.task_id)
            if work is not None and _matches_result(work, self.task_data):
                value = work.draft.get('url') or work.url
        self.provider_url_input = ui.input(
            label="Bangumi / TMDB 作品详情页链接",
            placeholder="粘贴 bgm.tv/subject 或 themoviedb.org/tv、movie 详情页链接",
            value=value,
        ).props("outlined clearable").classes("w-full")
        self.provider_submit_button = ui.button("读取链接并选择此作品", icon='o_link',
            on_click=self._select_manual_provider_url,
        ).props("outline color=blue-7 no-caps").classes("w-full")

    async def _select_manual_provider_url(self) -> None:
        from .provider_selection_work import select
        await select(self)

    async def _select_provider_candidate(self, candidate, *, season_number=None) -> None:
        from .provider_selection_work import select
        await select(self, candidate, season_number=season_number)

    async def _preview_asset_replacement(self):
        from .asset_actions_view import preview_asset_replacement
        await preview_asset_replacement(self)

    def _render_issues(self) -> None:
        self.issues_container.clear()
        with self.issues_container:
            from ..rename.default_content import source_only_pending
            if source_only_pending(self.plan, self.task_decision) and not self.plan.entries:
                with ui.column().classes('bar-issue-summary w-full gap-1'):
                    with ui.row().classes('bar-issue-heading w-full items-center gap-2 no-wrap'):
                        ui.icon('o_warning', color='red-7', size='20px').classes('bar-issue-icon')
                        ui.label('当前没有纳入的文件').classes('bar-error-title')
                    ui.label('请在“内容保留与排除”中恢复需要整理的内容。').classes('bar-issue-detail')
            chosen_projection = self.plan.metadata.get("source_episode_projection")
            if isinstance(chosen_projection, Mapping):
                mode = str(chosen_projection.get("mode") or "")
                chosen_label = {
                    "absolute": "沿用文件中的集号",
                    "relative": "从本段第1集重新编号",
                }.get(mode)
                if chosen_label:
                    ui.label(f"已选择：{chosen_label}").classes(
                        "bar-help text-green-8"
                    )
            if (str(self.plan.metadata.get("mapping_source") or "") == "inventory_review_draft"
                    and not any(i['code']=='mixed_presentation_requires_partition' for i in self.snapshot.issues)):
                work = self.snapshot.work_identity
                entity_type = str(work.get("provider_entity_type") or "")
                with ui.column().classes("bar-issue-summary w-full gap-1"):
                    with ui.row().classes(
                        "bar-issue-heading w-full items-center gap-2 no-wrap"
                    ):
                        ui.icon('o_info', color="blue-7", size="24px")
                        ui.label(
                            "还差一步：选择电影正片"
                            if is_confirmed_movie_plan(self.plan)
                            else (
                                "还差一步：生成剧集整理计划"
                                if entity_type == "tv"
                                else "还差一步：确认作品和本地播放方式"
                            )
                        ).classes("font-bold text-blue-8")
                    ui.label(
                        ("请点击工具行的“正片选择与标签编辑”。" if is_confirmed_movie_plan(self.plan)
                         else "请先选择正确作品，再确认本地播放方式。") + "完成前显示的"
                        "目标越界、字幕未关联等都是占位草案的派生状态，不需要逐条处理。"
                    ).classes("bar-issue-detail")
                return
            from .asset_actions_view import render_asset_choice
            if not getattr(self, "_asset_choice_embedded", False):
                render_asset_choice(self)
            summaries = summarize_plan_issues(
                visible_plan_issues(self.plan, self.snapshot.issues, rows=self.snapshot.rows),
                visible_role_rows(self.plan, self.snapshot.rows)
            )
            if not summaries:
                return
            ui.label(
                f"需要处理 {len(summaries)} 类问题（已合并重复提示）"
            ).classes("text-subtitle2 text-red-7")
            for summary in summaries:
                files = list(summary["files"])
                with ui.column().classes("bar-issue-summary w-full gap-1"):
                    with ui.row().classes("bar-issue-heading w-full items-center gap-2 no-wrap"):
                        ui.icon('o_warning', color="red-7", size="20px").classes(
                            "bar-issue-icon"
                        )
                        ui.label(str(summary["title"])).classes("bar-error-title")
                    if summary.get("detail"):
                        ui.label(str(summary["detail"])).classes("bar-issue-detail")
                    if len(files) == 1:
                        ui.label(f"文件：{files[0]}").classes("bar-evidence")
                    elif files:
                        preview = "、".join(files[:5])
                        suffix = f"，另有 {len(files) - 5} 个" if len(files) > 5 else ""
                        ui.label(
                            f"影响 {len(files)} 个文件：{preview}{suffix}"
                        ).classes("bar-evidence")
                    else:
                        ui.label("范围：整个包").classes("bar-evidence")
                    ui.label(f"技术代码：{summary['code']}").classes(
                        "bar-help font-mono"
                    )
                    if summary["code"] == "mixed_presentation_requires_partition":
                        render_cross_work_partition_action(self._partition_selected_as_cross_work)
                    elif summary["code"] == "presentation_asset_owner_unresolved":
                        self._render_asset_owner_repair()
                    elif summary["code"] == "external_audio_projection_invalid":
                        from .asset_choice_forms import render_audio_corrections
                        render_audio_corrections(self, ui=ui, notify=notify)
                        ui.label('独立CD音频或不需要的音轨无需关联视频；可点击下方按钮，预览并确认本次不整理。').classes('bar-help')
                        audio_ids = tuple(entry_id(entry) for entry in self.plan.entries
                                          if entry.role == 'external_audio_unresolved')
                        ui.button('选择本次不整理的音轨',
                            on_click=lambda _=None, keys=audio_ids: self._open_content_exclusion(
                                keys),
                            icon='o_do_not_disturb').props('outline color=blue-7')
                    elif summary["code"] in {
                        "presentation_asset_role_unknown",
                        "episode_assignment_conflict",
                    }:
                        if self.plan.metadata.get("content_selection") is None:
                            self._render_asset_role_repair()
                    elif summary["code"] == "source_episode_projection_decision_required":
                        self._render_source_episode_projection_repair()

    def _exclude_unresolved_audio(self, key: str) -> None:
        self._open_content_exclusion((key,))

    def _open_content_exclusion(self, identifiers) -> None:
        from .content_review_view import open_content_workbench
        if self.exit_requested or self.is_deleted:
            return
        selected = self._exceptional_entry_ids(identifiers)
        available = {entry_id(entry) for entry in self.plan.entries}
        if not selected or not set(selected) <= available:
            notify('请先选择本次不整理的文件；文件已变化时请重新打开任务。', type='warning')
            return
        open_content_workbench(self, ui=ui, notify=notify, selected_ids=selected)

    def _render_source_episode_projection_repair(self) -> None:
        if self.task_decision is None or self.task_decision.content_selection is None:
            ui.label('旧集号修改入口已停用，请安全结束此任务后重新添加来源。').classes('bar-help')
            return
        candidates = self.plan.metadata.get("source_episode_projection_candidates")
        if not isinstance(candidates, (list, tuple)):
            return
        by_mode = {
            str(item.get("mode") or ""): dict(item)
            for item in candidates
            if isinstance(item, Mapping)
        }
        actions = (
            ("absolute", "沿用文件中的集号"),
            ("relative", "从本段第1集重新编号"),
        )
        with ui.row().classes("items-center gap-2 flex-wrap"):
            for mode, label in actions:
                candidate = by_mode.get(mode)
                if candidate is None:
                    continue
                ui.button(
                    label,
                    on_click=partial(
                        self._apply_source_episode_projection_inline,
                        candidate,
                        label,
                        self.task_decision.revision if self.task_decision else None,
                    ),
                icon='o_format_list_numbered' if mode == 'absolute' else 'o_restart_alt').props("outline color=blue-7 no-caps")

    def _apply_source_episode_projection_inline(
        self,
        projection: Mapping[str, object],
        label: str,
        expected_revision: Optional[int],
    ) -> None:
        if self.task_decision is None or self.task_decision.content_selection is None:
            notify('旧集号修改入口已停用，请安全结束此任务后重新添加来源；源文件不会删除。', type='warning')
            return
        if not isinstance(expected_revision, int):
            notify("页面内容已经更新，请刷新后重新选择", type="warning")
            return
        try:
            fingerprint = str(projection.get("projection_fingerprint") or "")
            serialized_candidates = (
                self.plan.to_dict().get("metadata", {}).get(
                    "source_episode_projection_candidates"
                ) or ()
            )
            plain_projection = next(
                (
                    dict(item) for item in serialized_candidates
                    if isinstance(item, Mapping)
                    and str(item.get("projection_fingerprint") or "")
                    == fingerprint
                ),
                None,
            )
            if plain_projection is None:
                raise DecisionStateError("集号候选已经变化")
            from .tv_content_actions import revise
            return revise(self, expected_revision=expected_revision, mode='episode',
                expected_fingerprint=PlanStore.fingerprint(self.plan),
                edit={'projection_fingerprint': fingerprint}, notify=notify, message=f'已选择：{label}')
        except (DecisionStateError, PlanStoreError, TypeError, ValueError):
            notify("页面内容已经更新，请刷新后重新选择", type="warning")
            return

    def _render_asset_owner_repair(self) -> None:
        affected = {}
        for entry in self.plan.entries:
            identifier = entry_id(entry)
            row = next(
                (item for item in self.snapshot.rows if item["entry_id"] == identifier),
                None,
            )
            if row and "presentation_asset_owner_unresolved" in row.get("issue_codes", ()):
                affected[identifier] = _file_name(entry.source_path)
        owner_options = {"shared": "所有版本通用"}
        for entry in self.plan.entries:
            asset = entry.presentation_asset
            descriptor = asset.version_descriptor if asset else None
            if descriptor and descriptor.version_ref:
                owner_options[descriptor.version_ref] = descriptor.display_label or "默认电影版本"
        if not affected or len(owner_options) == 1:
            return
        ui.label("在这里直接指定资源归属；只需要处理存在冲突的文件。").classes("bar-help")
        with ui.row().classes("w-full items-end gap-2 flex-wrap"):
            file_select = ui.select(affected, label="待修正文件").props(
                "outlined dense options-dense"
            ).classes("grow")
            owner_select = ui.select(owner_options, label="归属版本").props(
                "outlined dense options-dense"
            ).classes("w-56")
            ui.button(
                "保存归属",
                icon='o_save',
                on_click=lambda: self._save_asset_owner_inline(
                    file_select.value, owner_select.value
                ),
            ).props("outline color=red-7")
            ui.button(
                "本次不整理",
                icon='o_do_not_disturb',
                on_click=lambda: self._ignore_entry_inline(file_select.value),
            ).props("flat color=grey-8")

    def _render_asset_role_repair(self) -> None:
        """Keep the common unknown-video repair beside its blocking issue."""

        affected = {}
        role_rows = visible_role_rows(self.plan, self.snapshot.rows)
        for entry in self.plan.entries:
            identifier = entry_id(entry)
            row = next(
                (item for item in role_rows if item["entry_id"] == identifier),
                None,
            )
            if row and set(row.get("issue_codes", ())).intersection({
                "presentation_asset_role_unknown", "episode_assignment_conflict",
            }):
                affected[identifier] = _file_name(entry.source_path)
        if not affected:
            return
        current_content = self.plan.metadata.get('content_selection') is not None
        ui.label("不需要的内容请在文件列表旁的“内容保留与排除”中调整；此处可更正正片用途。" if current_content else
                 "直接确认用途后，系统会从原始库存草案完整重建正片、特典、字幕和目标。") \
            .classes("bar-help")
        with ui.row().classes("w-full items-end gap-2 flex-wrap"):
            file_select = ui.select(affected, label="待确认文件").props(
                "outlined dense options-dense"
            ).classes("grow")
            if not current_content:
                ui.button("设为特典并重建", icon='o_movie_filter',
                    on_click=lambda: self._rebuild_inventory_role(file_select.value, "extra")
                ).props("outline color=blue-7")
            ui.button(
                "确认为正片候选" if current_content else "作为正片候选并重建",
                icon='o_video_library',
                on_click=lambda: self._rebuild_inventory_role(
                    file_select.value, "main_candidate"
                ),
            ).props("outline color=blue-7")
    @staticmethod
    def _exceptional_entry_ids(value: object) -> tuple[str, ...]:
        raw = value if isinstance(value, (list, tuple, set)) else (value,)
        return tuple(dict.fromkeys(
            str(item or "").strip() for item in raw if str(item or "").strip()
        ))

    def _apply_exceptional_special_inline(
        self,
        identifiers: object,
        action: str,
        provider_episode_id: object,
    ) -> None:
        selected = self._exceptional_entry_ids(identifiers)
        if action == 'unmatched_extra' and self.plan.metadata.get('content_selection') is not None:
            if provider_episode_id is not None:
                notify('附加内容不能混入官方特别篇集号。', type='warning')
                return
            return self._open_content_exclusion(selected)
        if len(selected) != 1:
            notify("请至少选择一个视频；归入官方特别篇时请只选一个。", type="warning")
            return
        return self._apply_provider_special_inline(
            selected[0], action, provider_episode_id
        )

    async def _open_source_regrouping(self, *, selected_entry_ids=None) -> None:
        from .source_regrouping_view import SourceRegroupingDialog
        if self.exit_requested or self.is_deleted or getattr(self, '_regrouping_opening', False):
            return
        self._regrouping_opening = True
        fingerprint = PlanStore.fingerprint(self.plan)
        try:
            preview = await ui_disk_call(task_queue.preview_source_regrouping,
                task_id=self.plan.task_id, expected_plan_fingerprint=fingerprint,
                selected_entry_ids=selected_entry_ids)
            if self.exit_requested or self.is_deleted or not self.value or PlanStore.fingerprint(self.plan) != fingerprint:
                return
            previous = getattr(self, 'regrouping_dialog', None)
            if previous is not None and not previous.is_deleted:
                previous.close()
                previous.delete()
            with self.client.content:
                popup = SourceRegroupingDialog(self, task_queue, selected_entry_ids=selected_entry_ids, preview=preview)
            self.regrouping_dialog = popup
            popup.open()
        except (ValueError, RuntimeError, OSError) as exc:
            if not self.is_deleted and not self.exit_requested:
                notify(localize_plan_term(exc), type='warning')
        finally:
            self._regrouping_opening = False

    async def _partition_exceptional_as_cross_work(self, identifiers: object) -> None:
        await self._open_selected_source_regrouping(self._exceptional_entry_ids(identifiers))

    async def _open_selected_source_regrouping(self, identifiers) -> None:
        await self._open_source_regrouping(selected_entry_ids=identifiers)

    async def _apply_provider_special_inline(
        self, identifier: object, action: str, provider_episode_id: object
    ) -> None:
        if action == 'unmatched_extra' and self.plan.metadata.get('content_selection') is not None:
            if provider_episode_id is not None:
                notify('附加内容不能混入官方特别篇集号。', type='warning')
                return
            return await await_result(self._open_content_exclusion((identifier,)))
        if not identifier:
            notify("请先选择待确认文件", type="warning")
            return
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
        from ..rename.provider_special_evidence import official_special_rows
        try:
            rows = official_special_rows(self.plan, str(identifier))
        except ValueError as exc:
            notify(str(exc), type='warning')
            return
        if action == "confirmed_official_special":
            if not isinstance(rows, (list, tuple)) or not rows:
                notify("官方特别篇候选已变化，请刷新后重试", type="warning")
                return
            selected_key = str(provider_episode_id or "").strip()
            matching = [
                row for row in rows
                if isinstance(row, Mapping)
                and str(row.get("provider_episode_id") or "") == selected_key
            ]
            if len(matching) != 1:
                notify("请选择一个官方特别篇", type="warning")
                return
            row = matching[0]
            decision = {
                key: row.get(key) for key in (
                    "provider_namespace", "provider_entity_type", "provider_id",
                    "season_number", "episode_number", "provider_episode_id",
                )
            }
            decision["action"] = action
        elif action == "unmatched_extra":
            decision = {"action": action}
        else:
            notify("特别篇决定无效", type="negative")
            return
        if self.task_decision.content_selection is not None:
            from .tv_content_actions import revise
            return await await_result(revise(self, expected_fingerprint=PlanStore.fingerprint(self.plan),
                expected_revision=self.task_decision.revision, mode='special',
                edit={'entry_id': str(identifier), 'action': action,
                      'provider_episode_id': decision.get('provider_episode_id')},
                notify=notify, message=('已保留为本季附加内容，目标在 Extras；仍需审核批准，尚未执行。'
                    if action == 'unmatched_extra' else '特别篇归属已确认；视频与关联字幕、音轨已一并更新，尚未执行。')))
        try:
            revised = await ui_disk_call(self.review_service.apply_provider_special_decision,
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                str(identifier),
                decision=decision,
                reason=(
                    "用户确认归入官方特别篇"
                    if action == "confirmed_official_special"
                    else "用户确认保留为附加内容"
                ),
                persist_revision=False,
            )
            decisions = dict(self.task_decision.provider_special_decisions)
            decisions[str(identifier)] = decision
            self.task_decision, projected = await ui_disk_call(self.decision_command_service.update,
                self.plan.task_id,
                expected_revision=self.task_decision.revision,
                changes={"provider_special_decisions": decisions},
                planner=lambda _state: revised,
                task_terminal=_task_terminal_for_projected_plan(self.plan.task_id),
            )
            if projected is None:
                raise DecisionStateError("未生成特别篇确认后的整理计划")
        except (DecisionStateError, PlanReviewError, TypeError, ValueError) as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        await await_result(self._adopt_revision(projected))
        notify(
            "已归入特别篇" if action == "confirmed_official_special"
            else "已保留为附加内容",
            type="positive",
        )

    async def _rebuild_inventory_role(self, identifier: object, role: str) -> None:
        if role == 'extra' and self.plan.metadata.get('content_selection') is not None:
            return await await_result(self._open_content_exclusion((identifier,)))
        if not identifier:
            notify("请先选择待确认文件", type="warning")
            return
        matching = [entry for entry in self.plan.entries if entry_id(entry) == identifier]
        if len(matching) != 1:
            notify("找不到待确认文件", type="negative")
            return
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
        try:
            if self.task_decision.content_selection is not None and self.task_decision.layout_mode == 'tv_episodes':
                from .tv_content_actions import revise
                return await await_result(revise(self,
                    expected_fingerprint=PlanStore.fingerprint(self.plan),
                    expected_revision=self.task_decision.revision, mode='role',
                    edit={'entry_id': str(identifier), 'role': role}, notify=notify,
                    message='用途已确认；当前TV视频与配套资源已完整重建，尚未执行文件操作。'))
            revised = await ui_disk_call(self.review_service.recomplete_inventory_with_role_decisions,
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                decisions={str(identifier): role},
                reason="用户在问题卡中确认文件用途并要求完整重建",
                persist_revision=False,
            )
            roles = dict(self.task_decision.manual_role_decisions)
            roles[str(identifier)] = role
            self.task_decision, projected = await ui_disk_call(self.decision_command_service.update,
                self.plan.task_id,
                expected_revision=self.task_decision.revision,
                changes={"manual_role_decisions": roles},
                planner=lambda _state: revised,
                task_terminal=_task_terminal_for_projected_plan(self.plan.task_id),
            )
            if projected is None:
                raise DecisionStateError("未生成用途修正后的 Plan")
            revised = projected
        except (DecisionStateError, PlanReviewError, TypeError, ValueError) as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        await await_result(self._adopt_revision(revised))
        notify("已设为正片候选；请在电影版本入口明确选择版本与保留范围，尚未执行。"
               if role == 'main_candidate' and any(g.kind == 'movie_continuous' for g in revised.presentation_groups)
               else "用途已确认；正片、特典、字幕和目标已完整重建，尚未执行文件操作。")

    async def _save_asset_owner_inline(self, identifier: object, owner: object) -> None:
        if not identifier or not owner:
            notify("请先选择待修正文件和归属版本", type="warning")
            return
        matching = [entry for entry in self.plan.entries if entry_id(entry) == identifier]
        if len(matching) != 1 or matching[0].presentation_asset is None:
            notify("找不到对应的呈现资源", type="negative")
            return
        asset = matching[0].presentation_asset
        try:
            revised = await ui_disk_call(self.review_service.revise_presentation_asset,
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                str(identifier),
                presentation_group=asset.presentation_group,
                role_kind=asset.asset_role.kind,
                role_subtype=asset.asset_role.subtype,
                owner_version_ref=str(owner),
                reason="用户在问题卡中确认资源所属版本",
            )
        except (PlanReviewError, ValueError) as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        await await_result(self._adopt_revision(revised))
        notify("资源归属已保存为新版整理计划；尚未执行文件操作", type="positive")

    async def _ignore_entry_inline(self, identifier: object) -> None:
        if not identifier:
            notify("请先选择待修正文件", type="warning")
            return
        self.selected_ids = {str(identifier)}
        await self._ignore_selected_entry()


    def _render_presentation_override(self) -> None:
        with ui.expansion(
            "呈现分组与版本 · 结构化修正", icon='o_movie_filter'
        ).classes("w-full"):
            with ui.column().classes("w-full gap-3"):
                ui.label("建立或更新呈现分组").classes("text-subtitle2")
                with ui.row().classes("w-full gap-3 flex-wrap"):
                    self.group_id_input = ui.input("分组 ID").props(
                        "outlined dense"
                    ).classes("grow")
                    self.group_kind_input = ui.select(
                        {
                            "movie_continuous": "电影连续版",
                            "tv_episodes": "TV 分集版",
                        },
                        label="分组类型",
                    ).props("outlined dense").classes("grow")
                    self.group_name_input = ui.input("分组显示名称").props(
                        "outlined dense"
                    ).classes("grow")
                with ui.row().classes("w-full gap-3 flex-wrap"):
                    self.group_namespace_input = ui.select(
                        {"tmdb": "TMDB", "bangumi": "Bangumi"},
                        label="Provider",
                    ).props("outlined dense").classes("grow")
                    self.group_entity_input = ui.select(
                        {"movie": "movie", "tv": "tv", "subject": "subject"},
                        label="实体类型",
                    ).props("outlined dense").classes("grow")
                    self.group_provider_id_input = ui.input("Provider ID").props(
                        "outlined dense"
                    ).classes("grow")
                    self.group_provider_title_input = ui.input("Provider 标题").props(
                        "outlined dense"
                    ).classes("grow")
                    self.group_provider_year_input = ui.number("年份").props(
                        "outlined dense"
                    ).classes("w-32")
                self.group_reason_input = ui.input("分组身份确认理由").props(
                    "outlined dense"
                ).classes("w-full")
                ui.button(
                    "保存呈现分组",
                    on_click=self._save_presentation_group,
                    icon='o_create_new_folder',
                ).props("outline color=purple-7")
                ui.separator()
                ui.label(
                    "选择一条非字幕记录。分组、资产角色和版本字段会进入整理计划"
                    "版本标识；保存只生成新版整理计划，不移动媒体。"
                ).classes("bar-help")
                group_options = {
                    group.group_id: f"{group.display_name} ({group.kind})"
                    for group in self.plan.presentation_groups
                }
                self.presentation_group = ui.select(
                    group_options, label="呈现分组"
                ).props("outlined dense options-dense").classes("w-full")
                with ui.row().classes("w-full gap-3 flex-wrap"):
                    self.presentation_role = ui.select(
                        {
                            "content_version": "内容版本",
                            "extra": "特典",
                            "associated": "关联文件",
                            "unknown": "未知（保持阻断）",
                        },
                        label="资产角色",
                    ).props("outlined dense").classes("w-48")
                    self.presentation_subtype = ui.select(
                        {
                            "trailer": "预告",
                            "featurette": "花絮",
                            "interview": "访谈",
                            "behind_the_scenes": "幕后",
                            "deleted_scene": "删除场景",
                            "scene": "场景",
                            "short": "短片",
                            "clip": "片段",
                            "sample": "样片",
                            "other": "其他",
                        },
                        label="特典子类型",
                    ).props("outlined dense").classes("w-48")
                    self.presentation_preferred = ui.checkbox(
                        "设为电影组偏好版本"
                    ).props("color=purple-7")
                owner_options = {"shared": "所有版本通用"}
                for entry in self.plan.entries:
                    asset = entry.presentation_asset
                    descriptor = asset.version_descriptor if asset is not None else None
                    if descriptor is not None and descriptor.version_ref:
                        owner_options[descriptor.version_ref] = (
                            descriptor.display_label or "默认电影版本"
                        )
                self.presentation_owner = ui.select(
                    owner_options,
                    label="特典/配套资源所属版本",
                ).props("outlined clearable options-dense").classes("w-full")
                ui.label(
                    "只有特典或配套资源需要选择；内容相同只表示物理复用，"
                    "不会自动改成所有版本通用。"
                ).classes("bar-help")
                ui.label(
                    "电影请使用上方“电影正片版本编辑”；下面规格字段用于分集呈现确认。"
                ).classes("bar-help")
                ui.label(
                    "电影连续版的正片目标文件名由作品目录和版本信息自动生成。"
                    "保存这里的版本决定时，会替换此前填写的手工目标路径；"
                    "要区分电影版本，请在电影版本列表编辑标签。"
                ).classes("bar-help text-orange-8")
                with ui.row().classes("w-full gap-3 flex-wrap"):
                    self.presentation_edition_key = ui.input(
                        "edition key（如 theatrical）"
                    ).props("outlined dense").classes("grow")
                    self.presentation_edition_label = ui.input(
                        "剪辑语义（如 剧场版）"
                    ).props("outlined dense").classes("grow")
                    self.presentation_resolution = ui.input(
                        "分辨率（如 1080p）"
                    ).props("outlined dense").classes("grow")
                    self.presentation_dynamic_range = ui.input(
                        "动态范围（如 SDR/HDR10）"
                    ).props("outlined dense").classes("grow")
                    self.presentation_audio = ui.input(
                        "关键音频（如 FLAC 2.0）"
                    ).props("outlined dense").classes("grow")
                if {g.kind for g in self.plan.presentation_groups} == {'movie_continuous'}:
                    for control in (self.presentation_edition_key, self.presentation_edition_label,
                                    self.presentation_resolution, self.presentation_dynamic_range, self.presentation_audio):
                        control.set_visibility(False)
                self.presentation_reason = ui.input(
                    "人工确认理由（必填）"
                ).props("outlined dense").classes("w-full")
                with ui.row().classes("w-full gap-3 flex-wrap"):
                    ui.button(
                        "保存呈现资产决定",
                        on_click=self._save_presentation_asset,
                        icon='o_save_as',
                    ).props("outline color=purple-7").classes("grow")
                    ui.button(
                        "本次不整理所选文件",
                        on_click=self._ignore_selected_entry,
                        icon='o_do_not_disturb',
                    ).props("outline color=grey-7").classes("grow")
                    render_cross_work_partition_action(
                        self._partition_selected_as_cross_work
                    )

    async def _save_presentation_group(self) -> None:
        year_value = self.group_provider_year_input.value
        try:
            provider_year = int(year_value) if year_value not in (None, "") else None
            revised = await ui_disk_call(self.review_service.upsert_presentation_group,
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                group_id=self.group_id_input.value,
                kind=self.group_kind_input.value,
                display_name=self.group_name_input.value,
                provider_namespace=self.group_namespace_input.value,
                provider_entity_type=self.group_entity_input.value,
                provider_id=self.group_provider_id_input.value,
                provider_title=self.group_provider_title_input.value,
                provider_year=provider_year,
                reason=self.group_reason_input.value,
            )
        except (PlanReviewError, TypeError, ValueError) as exc:
            notify(localize_plan_term(exc))
            return
        await await_result(self._adopt_revision(revised))
        notify("呈现分组已保存为新版整理计划；重新打开整理任务可刷新分组选项。")

    async def _save_presentation_asset(self) -> None:
        if len(self.selected_ids) != 1:
            notify("呈现资产决定必须且只能选择一条记录")
            return
        selected_id = next(iter(self.selected_ids))
        try:
            revised = await ui_disk_call(self.review_service.revise_presentation_asset,
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                selected_id,
                presentation_group=self.presentation_group.value,
                role_kind=self.presentation_role.value,
                role_subtype=self.presentation_subtype.value,
                owner_version_ref=self.presentation_owner.value,
                edition_key=self.presentation_edition_key.value,
                edition_label=self.presentation_edition_label.value,
                resolution=self.presentation_resolution.value,
                dynamic_range=self.presentation_dynamic_range.value,
                key_audio=self.presentation_audio.value,
                preferred=bool(self.presentation_preferred.value),
                reason=self.presentation_reason.value,
            )
        except (PlanReviewError, ValueError) as exc:
            notify(localize_plan_term(exc))
            return
        await await_result(self._adopt_revision(revised))
        notify(
            "已保存新版整理计划并清空旧批准；结构仍有阻断时可继续逐项修正。"
        )

    async def _ignore_selected_entry(self) -> None:
        if self.plan.metadata.get('content_selection') is not None:
            from .content_review_view import open_content_workbench
            return await await_result(open_content_workbench(self, ui=ui, notify=notify))
        if len(self.selected_ids) != 1:
            notify("不整理决定必须且只能选择一条记录")
            return
        try:
            revised = await ui_disk_call(self.review_service.ignore_plan_entry,
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                next(iter(self.selected_ids)),
                reason=self.presentation_reason.value,
            )
        except (PlanReviewError, ValueError) as exc:
            notify(localize_plan_term(exc))
            return
        await await_result(self._adopt_revision(revised))
        notify("所选源文件保持不变，本次整理计划将不处理它。")

    async def _partition_selected_as_cross_work(self) -> None:
        await self._open_selected_source_regrouping(tuple(sorted(self.selected_ids)))

    def _render_override(self) -> None:
        with ui.expansion(
            f"人工覆盖目标 · 生成新版{RENAME_PLAN_TERM}，不执行",
            icon='o_edit_note',
        ).classes("w-full"):
            with ui.column().classes("w-full gap-3"):
                ui.label("先在上方选择且仅选择一条记录。" ).classes("bar-help")
                ui.label(
                    "此处不用于给电影连续版正片命名：电影正片目标由作品目录和版本信息统一生成，"
                    "之后保存版本决定会覆盖这里填写的目标路径。"
                ).classes("bar-help text-orange-8")
                self.override_target = ui.input("新目标路径").props(
                    "outlined dense"
                ).classes("w-full")
                self.override_reason = ui.input("覆盖理由（必填）").props(
                    "outlined dense"
                ).classes("w-full")
                ui.label(
                    f"覆写会生成新版{RENAME_PLAN_TERM}并清空旧批准；"
                    "保存后必须重新批准全部条目。"
                ).classes("bar-help text-orange-8")
                ui.button(
                    f"保存为新版{RENAME_PLAN_TERM}",
                    on_click=self._override_selected,
                    icon='o_save_as',
                ).props("outline color=blue-7")

    def render_actions(self) -> None:
        unresolved = self.plan is None or self._replacement_unfinished() or self.plan.metadata.get('pending_work_affiliation') is not None
        with ui.row().classes(
            "bar-plan-actions bar-modal-footer w-full items-center gap-3"
        ):
            with ui.row().classes("w-full gap-2 flex-wrap items-center"):
                ui.button('安全结束任务', on_click=self._safe_end, icon='o_delete').classes('bar-review-action').props('color=red-7')
                if unresolved:
                    self._render_close_action()
                    return
                self.approve_all_button = ui.button(
                    "批准整理计划",
                    on_click=None if unresolved else self._approve_all,
                    icon='o_verified',
                ).classes("bar-review-action").props("color=amber-7 text-color=white")
                if unresolved or not can_approve_plan(self.snapshot.plan_status):
                    self.approve_all_button.disable()
                self.execute_button = ui.button(
                    f"执行{RENAME_PLAN_TERM}",
                    on_click=None if unresolved else self._confirm_execute,
                    icon='o_play_arrow',
                ).classes("bar-review-action").props("color=green-7")
                if unresolved or not can_execute_approved_plan(
                    self.decision.status,
                    str(self.task_data.get("workflow_status")),
                    preview_only=bool(self.task_data.get("preview_only")),
                    execution_policy=self.plan.execution_policy,
                ):
                    self.execute_button.disable()
                if (
                    not unresolved and self.plan.execution_policy is not None and self.plan.execution_policy.dry_run
                ):
                    self.execute_button.tooltip(
                        plan_execution_contract.DRY_RUN_PLAN_RETRY_MESSAGE
                    )
                self._render_close_action()

    def _render_close_action(self):
        ui.button('关闭', on_click=self._close_review, icon='o_close').props(
            'flat color=grey-8').classes('bar-review-action ml-auto')

    async def _confirm_source_group(self):
        try:
            await task_queue.confirm_source_group(self.task_id, opened_evidence=self.retry_open_evidence)
        except (ValueError, RuntimeError, OSError) as exc:
            notify(f'无法继续识别：{exc}', type='negative')
            return
        self._close_review()
        from .data_table_page import show_task_queue
        show_task_queue(self.client)

    def _refresh_execute_button(self) -> None:
        enabled = can_execute_approved_plan(
            self.decision.status,
            str(self.task_data.get("workflow_status")),
            preview_only=bool(self.task_data.get("preview_only")),
            execution_policy=self.plan.execution_policy,
        )
        self.execute_button.enable() if enabled else self.execute_button.disable()

    def _confirm_execute(self) -> None:
        if not can_execute_approved_plan(
            self.decision.status,
            str(self.task_data.get("workflow_status")),
            preview_only=bool(self.task_data.get("preview_only")),
            execution_policy=self.plan.execution_policy,
        ):
            if (
                self.plan.execution_policy is not None
                and self.plan.execution_policy.dry_run
            ):
                notify(
                    plan_execution_contract.DRY_RUN_PLAN_RETRY_MESSAGE,
                    type="warning",
                )
            else:
                notify("只有当前版本全部批准后才能执行", type="warning")
            return
        fingerprint = PlanStore.fingerprint(self.plan)
        from .queue_confirmation import QueueConfirmation
        with ui.dialog().props('persistent') as confirm, dialog_card().classes("scroll bar-dialog max-w-lg"):
            ui.label(f"确认执行已批准的{RENAME_PLAN_TERM}？").classes(
                "bar-dialog-title"
            )
            ui.label(
                "执行将按已批准策略实际创建硬链接；不会移动或复制源文件。"
                f"系统会在后台再次校验{RENAME_PLAN_TERM}版本、"
                "全部批准状态与结构完整性。"
            ).classes("bar-muted")
            ui.label(f"版本标识：{fingerprint[:16]}…").classes(
                "font-mono bar-help"
            )
            message = ui.label('确认后先进行后台校验，校验通过才会入队。').classes('bar-help')
            progress = bar_progress(ui)
            progress.set_visibility(False)
            confirm.queue_confirmation = QueueConfirmation(confirm, message, progress)
            with ui.row().classes("w-full justify-end gap-2").classes('bar-modal-footer'):
                ui.button("取消", on_click=confirm.queue_confirmation.cancel, icon='o_close').props("flat")
                confirm.queue_confirmation.button = ui.button(
                    "确认入队执行",
                    on_click=partial(
                        self._queue_execution, confirm, fingerprint
                    ),
                    icon='o_play_arrow',
                ).props("color=red-7")
        confirm.open()

    async def _queue_execution(self, confirm: ui.dialog, fingerprint: str) -> None:
        from .queue_confirmation import QueueConfirmation
        if getattr(confirm, 'queue_confirmation', None) is None:
            confirm.queue_confirmation = QueueConfirmation(confirm)

        if confirm.queue_confirmation.closed:
            return
        from .execution_wait import submit
        try:
            submit(task_queue, self.plan.task_id, fingerprint)
        except (ValueError, RuntimeError) as exc:
            notify(str(exc), type='warning')
            return
        confirm.queue_confirmation.closed = True
        confirm.close()
        self._close_review()

    async def _safe_end(self) -> None:
        from .data_table_page import _confirm_safe_end
        await _confirm_safe_end(self.task_id, on_success=self._close_review)

    def _close_review(self) -> None:
        self.exit_requested = True
        self.close()


    def _set_review_only(self, value: bool) -> None:
        self.review_only = bool(value)
        self._render_entries()

    def _set_filter(self, name: str, value: object) -> None:
        setattr(self, name, value or (False if name.endswith("_only") else ""))
        self._render_entries()

    def _toggle_selected(self, identifier: str, selected: bool) -> None:
        if identifier not in {entry_id(entry) for entry in self.plan.entries}:
            return
        if selected:
            self.selected_ids.add(identifier)
        else:
            self.selected_ids.discard(identifier)
        self.selection_label.text = f"已选择 {len(self.selected_ids)} 项"

    async def _approve_selected(self) -> None:
        if not self.selected_ids:
            notify("请先选择要批准的条目")
            return
        await self._approve(tuple(self.selected_ids))

    async def _approve_all(self) -> None:
        await self._approve(tuple(entry_id(entry) for entry in self.plan.entries))

    async def _approve(self, selected_ids: Tuple[str, ...]) -> None:
        from ..rename.plan_review import ApprovalTaskContext
        from ..utils.utils import read_task_at
        from .tv_content_actions import run_revision, revision_progress
        state = self.task_decision
        if state is None:
            notify('当前计划缺少决定记录，仅供只读查看；请重新生成。', type='warning')
            return
        fingerprint = PlanStore.fingerprint(self.plan)
        context = ApprovalTaskContext(state.revision, DecisionStateStore.fingerprint(state),
            self.task_data.get('workflow_status'), self.task_data.get('execution_recovery_disposition'))
        service, task_id = self.review_service, self.plan.task_id
        result = {}

        def submit():
            result['decision'] = service.approve_entries(
                task_id, fingerprint, selected_ids, task_context=context)
            result['task'] = read_task_at(service.task_root, task_id)
            return service.plan_store.load(task_id)

        def adopt(_plan):
            # Only consume the worker's saved result; no UI-thread record reads.
            self.decision = result['decision']
            self.task_data = result['task']
            self.review_badge.text = (completion_status(self.plan, self.task_data) or REVIEW_STATUS_LABELS[self.decision.status])
            self._render_entries()
            self._refresh_execute_button()

        def finish():
            enabled = can_approve_plan(self.snapshot.plan_status)
            for button in (self.approve_all_button,):
                button.enable() if enabled else button.disable()
            self._refresh_execute_button()

        if not revision_progress(self).running:
            self.approve_all_button.disable()
            self.execute_button.disable()
        await run_revision(self, submit=submit, expected_revision=state.revision,
            expected_fingerprint=fingerprint, notify=notify, adopt=adopt, on_finished=finish,
            approval=True,
            started_message='正在后台批准并核对文件状态；关闭窗口不影响本次保存。',
            message='批准决定已保存；尚未执行任何文件操作。')

    async def _adopt_revision(self, revised: RenamePlan) -> None:
        from ..rename.review_operation import build_review_operation
        before = self.plan
        serial = getattr(self, '_display_serial', 0) + 1
        self._display_serial = serial
        snapshot, state, decision, task, catalog = await ui_disk_call(
            _prepare_revision_display, revised, self.review_service, self.series_catalog,
            self.catalog_detach_service, self.recovery_service, dict(self.task_data))
        if (getattr(self, 'is_deleted', False) or getattr(self, 'exit_requested', False)
                or getattr(self, '_tv_content_closed', False)
                or getattr(self, '_tv_content_worker_disconnected', False)
                or self._display_serial != serial or self.plan is not before):
            raise ValueError('页面已关闭或已有后来修订，不采用过期的页面结果')
        if not hasattr(self, 'selection_label') or self.plan.metadata.get('pending_work_affiliation') is not None:
            self._load_plan_view(revised, snapshot=snapshot)
            return
        previous_operation, _ = build_review_operation(self.task_data)
        self.plan, self.snapshot, self.task_decision = revised, snapshot, state
        self.decision, self.task_data, self._catalog_display = decision, task, catalog
        self._provider_presented = None
        current_operation, _ = build_review_operation(self.task_data)
        previous_context, current_context = previous_operation.context, current_operation.context
        if (previous_context.get('kind') == 'provider_selection'
                and previous_context.get('lifecycle') in {'failed', 'awaiting_user', 'awaiting_season'}
                and current_context.get('lifecycle') == 'succeeded'
                and current_context.get('request_id') == previous_context.get('request_id')
                and current_context.get('result_plan_fingerprint') == PlanStore.fingerprint(revised)):
            self.provider_selection_expanded = False
        self.selected_ids.clear()
        self.selection_label.text = "已选择 0 项"
        self.review_badge.text = (completion_status(self.plan, self.task_data) or REVIEW_STATUS_LABELS[self.decision.status])
        # Current content pages do not construct the retired layout editor.
        # Historical pages may still own it while adopting a migration.
        if hasattr(self, 'presentation_group'):
            self.presentation_group.options = {
                group.group_id: f"{group.display_name} ({group.kind})"
                for group in revised.presentation_groups
            }
            self.presentation_group.update()
        self._render_groups()
        self._render_catalog_context()
        self._render_completion_workflow()
        self._render_issues()
        self._refresh_plan_header()
        self._render_entries()
        self._refresh_execute_button()

    async def _override_selected(self) -> None:
        if len(self.selected_ids) != 1:
            notify("人工覆盖必须且只能选择一条记录")
            return
        selected_id = next(iter(self.selected_ids))
        try:
            revised = await ui_disk_call(self.review_service.override_target,
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                selected_id,
                self.override_target.value,
                self.override_reason.value,
            )
        except PlanReviewError as exc:
            notify(localize_plan_term(exc))
            return
        await self._adopt_revision(revised)
        self.override_target.value = ''
        self.override_reason.value = ''
        notify(
            f"已保存新版{RENAME_PLAN_TERM}并清空旧批准；"
            "请重新批准全部条目，"
            "尚未执行任何文件操作"
        )

    def _refresh_plan_header(self) -> None:
        PlanReviewDialog._refresh_official_special_action(self)
        self.plan_badge.set_visibility(entry_status(self.plan, self.task_data) != "已整理")
        self.plan_badge.text = PLAN_STATUS_LABELS.get(
            self.snapshot.plan_status, self.snapshot.plan_status
        )
        self.plan_badge.props(
            "color=" + (
                "red-7" if self.snapshot.plan_status == "invalid"
                else "orange-8" if self.snapshot.plan_status == "need_confirm"
                else "green-7"
            )
        )
        authoritative_title = ""
        if self.task_decision is not None:
            authoritative_title = str(
                self.task_decision.confirmed_work.get("title") or ""
            ).strip()
        if not authoritative_title and self.plan.work_identity is not None:
            authoritative_title = str(self.plan.work_identity.title or "").strip()
        for element, value in (
            (getattr(self, "header_title_value", None), authoritative_title or PlanReviewDialog._pending_work_title(self)),
            (getattr(self, "header_season_value", None), self._header_season_text()),
            (
                getattr(self, "header_recognition_value", None),
                self._header_recognition_text(),
            ),
            (
                getattr(self, "header_entry_count_value", None),
                str(len(self.plan.entries)),
            ),
        ):
            if element is not None:
                element.text = value
                element.update()
        if can_approve_plan(self.snapshot.plan_status):
            self.approve_all_button.enable()
        else:
            self.approve_all_button.disable()
        self._render_issues()

    def _header_season_text(self) -> str:
        value = self.plan.metadata.get("season_id")
        if isinstance(value, int) and value > 0:
            return f"第 {value} 季"
        return "不适用" if bool(self.plan.metadata.get("is_movie")) else "待确认"

    def _header_recognition_text(self) -> str:
        if self._replacement_unfinished():
            return '本次选择未完成'
        work = getattr(self.plan, "work_identity", None)
        if work is not None and work.status == "confirmed":
            return "已确认作品"
        operation, _ = build_review_operation(self.task_data)
        if operation.context.get('candidate'):
            return '本次选择未完成'
        return "等待选择作品"

    def _pending_work_title(self) -> str:
        operation, _ = build_review_operation(self.task_data)
        pending_read = self._pre_admission_selection()
        candidate = pending_read.candidate if pending_read is not None else operation.context.get('candidate')
        if isinstance(candidate, Mapping) and candidate.get('title'):
            return f"{candidate['title']}（待确认）"
        return '尚未选择'


def _meta_item(label: str, value: str, monospace: bool = False):
    with ui.column().classes("gap-0"):
        ui.label(label).classes("bar-help")
        return ui.label(value).classes(
            "font-medium " + ("font-mono" if monospace else "")
        )


def _media_type_from_flags(is_anime: object, is_movie: object) -> str:
    mapping = {
        (None, None): "auto",
        (True, False): "anime",
        (True, True): "anime_movie",
        (False, True): "movie",
        (False, False): "tv",
    }
    return mapping.get((is_anime, is_movie), "auto")


async def _reenqueue_from_workspace(
    task_id: str,
    task_data: Dict[str, object],
    *,
    path_value: object,
    media_type: object,
    name_value: object,
    season_value: object,
    opened_evidence=None,
) -> bool:
    source_path = str(path_value or "").strip()
    if not source_path:
        notify("请先填写或选择来源目录", type="warning")
        return False
    try:
        season_id = int(season_value) if season_value not in (None, "") else None
        is_anime, is_movie = media_flags(str(media_type or "auto"))
    except (TypeError, ValueError, KeyError):
        notify("季度或媒体类型无效", type="negative")
        return False
    if opened_evidence is None:
        notify("缺少开窗时的任务依据，请关闭后重新打开", type="warning")
        return False
    with ui.dialog() as confirmation, ui.card():
        ui.label('重新识别此任务').classes('text-lg font-bold')
        ui.label('来源文件和任务卡保留。旧计划、批准、版本选择和特别篇归属将退出，按当前设置重新识别。')
        with ui.row():
            ui.button('取消', on_click=lambda: confirmation.submit(False), icon='o_close').props('flat')
            ui.button('确认重新识别', on_click=lambda: confirmation.submit(True), icon='o_refresh')
    if not await confirmation:
        return False
    try:
        await task_queue.reenqueue_workspace(
            task_id, Path(source_path), is_anime=is_anime, is_movie=is_movie,
            custom_name=str(name_value or "").strip() or None,
            custom_season_id=season_id,
            preview_only=bool(task_data.get("preview_only", False)),
            opened_evidence=opened_evidence,
        )
    except (PlanExecutionError, RuntimeError, ValueError, OSError) as exc:
        notify(str(exc), type="warning")
        return False
    notify("任务已重新加入队列", type="positive")
    return True


async def _read_manual_provider_url(value):
    return await ui_disk_call(resolve_provider_item_url, value,
        tmdb_api_key=effective_tmdb_api_key(cm), bangumi_token=cm.get_config('bangumi_token'),
        bangumi_user_agent=cm.get_config('bangumi_user_agent'))




class RecognitionRecoveryDialog(PlanReviewDialog):
    """旧计划异常和来源确认使用同一个整理窗口。"""
    def __init__(self, task_id, task_data, error):
        from ..rename.input_directory_discovery import source_group_pending
        super().__init__(None, task_data,
            recovery_error=None if source_group_pending(task_data) else error)

    async def _select_url(self):
        from .provider_selection_work import select
        await select(self)


async def plan_review_page(task_id: str) -> None:
    try:
        client = ui.context.client
    except RuntimeError:
        client = None  # Non-UI callers exercise the same preparation path.
    openings = getattr(client, '_bar_review_openings', set())
    if task_id in openings:
        return
    if client is not None:
        client._bar_review_openings = openings
    openings.add(task_id)
    try:
        await _open_plan_review_page(task_id, client)
    finally:
        openings.discard(task_id)


async def _open_plan_review_page(task_id: str, client) -> None:
    from .provider_selection_work import waiting
    from .data_table_page import show_task_queue
    if waiting(task_queue, task_id) or task_queue.is_active(task_id) or task_id in task_queue._pending:
        show_task_queue(client)
        return
    task_data = await ui_disk_call(get_task, task_id)
    try:
        plan = await ui_disk_call(PlanStore(PLAN_PATH).load, task_id)
    except PlanStoreError as exc:
        # A first failed lookup is not a corrupt historical Plan. Keep the
        # normal unresolved workspace (including direct links) available.
        def workspace_missing():
            return (not PlanStore(PLAN_PATH).path_for(task_id).exists()
                    and not DecisionStateStore(DECISION_STATE_PATH).path_for(task_id).exists())
        first_workspace = await ui_disk_call(workspace_missing)
        from ..rename.input_directory_discovery import source_group_pending
        if not first_workspace or source_group_pending(task_data):
            with client.content if client is not None else _dialog_mount_context():
                dialog = RecognitionRecoveryDialog(task_id, task_data, exc)
            try:
                from .provider_selection_work import attach
                attach(dialog)
                next_action = await dialog
            finally:
                dialog.delete()
            if next_action == 'review':
                await _open_plan_review_page(task_id, client)
            return
        plan = None
    if waiting(task_queue, task_id) or task_queue.is_active(task_id) or task_id in task_queue._pending:
        show_task_queue(client)
        return
    # This callback is invoked by a button inside the refreshable review-task
    # list.  If the dialog inherits that button's slot, the dashboard timer
    # deletes it as soon as approval changes the task signature.  Mount it on
    # the stable client root so approval can refresh the card behind it while
    # the dialog remains open for the execution step.
    if not task_data:
        task_data = await ui_disk_call(get_task, task_id)
    if client is not None and client.id not in client.instances:
        return
    with client.content if client is not None else _dialog_mount_context():
        dialog = PlanReviewDialog(plan, task_data, loading=plan is not None)
    try:
        if plan is not None:
            if (not await _prepare_visible_plan_dialog(dialog, plan, client)
                    or dialog.exit_requested or dialog.is_deleted or not dialog.value
                    or dialog.submitted.is_set()
                    or (client is not None and client.id not in client.instances)):
                return
        from .provider_selection_work import attach
        attach(dialog)
        await dialog
        if not getattr(dialog, "exit_requested", True):
            # A dashboard refresh can transiently detach the portal even
            # though the operator never asked to leave. Re-open the same
            # persisted revision once; do not enqueue recognition again.
            dialog.open()
            await dialog
    finally:
        if not dialog.is_deleted:
            dialog.delete()


async def _prepare_visible_plan_dialog(dialog, plan: RenamePlan, client, *, is_current=None) -> bool:
    """Show feedback first; only a still-open, current window may consume the read."""
    fingerprint = PlanStore.fingerprint(plan)
    documents = frozenset(getattr(client, '_socket_to_document_id', {}).values())
    reconnected = asyncio.Event()
    dialog.submitted.clear()
    dialog.open()

    def disconnected():
        if not dialog.is_deleted:
            dialog._close_review()

    if client is not None:
        client.on_disconnect(disconnected)
        client.on_connect(reconnected.set)
    worker = asyncio.create_task(ui_disk_call(prepare_plan_review_open, plan))
    closed = asyncio.create_task(dialog.submitted.wait())
    reconnect_waits = []
    try:
        await asyncio.wait((worker, closed), return_when=asyncio.FIRST_COMPLETED)
        # 同一页面短暂断线时保留读取结果；实际关闭仍立即结束等待。
        while documents and not client._socket_to_document_id and not closed.done():
            reconnected.clear()
            connected = asyncio.create_task(reconnected.wait())
            reconnect_waits.append(connected)
            await asyncio.wait((connected, closed), return_when=asyncio.FIRST_COMPLETED)
        # A close wins even if the read finished in the same event-loop turn.
        if (dialog.submitted.is_set() or dialog.exit_requested or dialog.is_deleted
                or not dialog.value
                or (is_current is not None and not is_current())
                or (client is not None and client.id not in client.instances)
                or (documents and documents != frozenset(client._socket_to_document_id.values()))):
            return False
        snapshot = worker.result()
        current = await ui_disk_call(PlanStore(PLAN_PATH).load, plan.task_id)
        if (dialog.submitted.is_set() or dialog.exit_requested or dialog.is_deleted
                or not dialog.value or (is_current is not None and not is_current())
                or (client is not None and client.id not in client.instances)
                or (documents and documents != frozenset(client._socket_to_document_id.values()))):
            return False
        if PlanStore.fingerprint(current) != fingerprint:
            notify('整理计划已更新，请重新打开任务', type='warning')
            return False
        dialog._load_plan_view(plan, snapshot=snapshot)
        return True
    except Exception:
        logger.exception('读取整理计划失败')
        notify('读取整理计划失败，请关闭后重新打开任务', type='warning')
        return False
    finally:
        if client is not None and disconnected in client.disconnect_handlers:
            client.disconnect_handlers.remove(disconnected)
        if client is not None and reconnected.set in client.connect_handlers:
            client.connect_handlers.remove(reconnected.set)
        for pending in (worker, closed, *reconnect_waits):
            if not pending.done():
                pending.cancel()
        # 页面关闭不采用结果；已经启动的审核记录准备仍可完成。
        await asyncio.gather(worker, closed, *reconnect_waits, return_exceptions=True)
