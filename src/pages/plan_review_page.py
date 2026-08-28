"""Responsive NiceGUI review workspace for persisted rename plans."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..config.config_manager import cm, effective_tmdb_api_key

from nicegui import ui

from ..logger import logger

from ..component.local_file_picker import local_file_picker
from ..element.red import notify
from ..rename.plan_review import (
    PlanReviewError,
    PlanReviewService,
    ReviewStatus,
    entry_id,
)
from ..rename.plan_execution import PlanExecutionError
from ..rename.plan_store import PlanStore, PlanStoreError
from ..rename.projection_authority import (
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
from ..rename.execution_recovery import requires_execution_reapproval
from ..rename.decision_state import (
    DecisionCommandService,
    DecisionStateError,
    DecisionStateStore,
)
from ..rename.series_catalog import SeriesCatalog, SeriesCatalogError
from ..rename.recovery_service import RecoveryService
from ..rename.recovery_transaction import (
    CatalogDetachService,
    RecoveryTransactionError,
    RecoveryTransactionStore,
)
from ..rename.plan_verifier import PlanVerifier
from ..rename.provider_search import ProviderSearchError, resolve_provider_item_url
from ..rename.provider_link_decision import (
    apply_source_episode_projection,
    build_provider_subject_boundary_failure,
    build_provider_subject_cohort_preview,
    provider_subject_cohort_request_fingerprint,
    provider_link_review_summary,
    resolve_provider_subject_boundary_decision,
    stage_provider_link_series_decision,
)
from ..rename.provider_link_resolver import (
    ProviderCohortAnchorError,
    trusted_relation_tmdb_anchor,
)
from ..rename.inventory_plan import InventoryDraftError, build_inventory_review_draft
from ..rename.trans import is_dry_run_enabled
from ..rename.rename_plan import (
    RenamePlan,
    SubtitleOverrideScope,
    WorkIdentity,
)
from ..rename.presentation import provider_identity_from_metadata
from ..rename.rename_plan import UnknownTokenAction, UnknownTokenDisposition
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
from ..utils.utils import get_task, mutate_task, write_task
from ..task_queue import is_ui_test_mode, task_queue
from .add_task_page import MEDIA_OPTIONS, media_flags
from .ui_terms import RENAME_PLAN_TERM, localize_plan_term


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


def _tv_font_observation(
    plan: RenamePlan,
    *,
    task_source_scope: TaskSourceScope,
    series_title: str,
    series_year: Optional[int],
) -> Optional[dict[str, object]]:
    """Freeze one series font archive from the reserved source inventory."""

    def has_independent_fonts_token(path: Path) -> bool:
        stem = unicodedata.normalize("NFKC", path.stem).casefold()
        token = "[fonts]"
        start = stem.find(token)
        while start >= 0:
            before = stem[start - 1] if start else ""
            end = start + len(token)
            after = stem[end] if end < len(stem) else ""
            if (not before or not before.isalnum()) and (
                not after or not after.isalnum()
            ):
                return True
            start = stem.find(token, start + 1)
        return False

    expected_locations = {
        str(Path(source).resolve(strict=False)).casefold()
        for source in plan.expected_sources
    }
    candidates = tuple(
        Path(identity.source_path)
        for identity in task_source_scope.source_identities
        if identity.location_key in expected_locations
        and has_independent_fonts_token(Path(identity.source_path))
    )
    if not candidates:
        return None
    unsupported = tuple(
        path for path in candidates if path.suffix.casefold() not in {".7z", ".zip"}
    )
    if unsupported:
        raise TVPlanningAuthorityError("TV 字体归档只支持7z/zip，拒绝rar")
    if len(candidates) != 1 or not series_title or not isinstance(series_year, int):
        raise TVPlanningAuthorityError("TV series font authority 不唯一或缺少系列名")
    source = candidates[0].resolve(strict=True)
    frozen = next(
        (
            identity for identity in task_source_scope.source_identities
            if identity.location_key == str(source).casefold()
        ),
        None,
    )
    if frozen is None:
        raise TVPlanningAuthorityError("TV 字体归档不属于冻结source scope")
    stat_result = source.stat()
    frozen_stat = (
        frozen.physical_key,
        frozen.size,
        frozen.mtime_ns,
    )
    current_stat = (
        f"device:{int(stat_result.st_dev)}:inode:{int(stat_result.st_ino)}",
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )
    if frozen_stat != current_stat:
        raise TVPlanningAuthorityError("TV 字体归档stat与冻结source scope不一致")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after_hash_stat = source.stat()
    if (
        int(after_hash_stat.st_dev),
        int(after_hash_stat.st_ino),
        int(after_hash_stat.st_size),
        int(after_hash_stat.st_mtime_ns),
    ) != (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    ):
        raise TVPlanningAuthorityError("TV 字体归档在hash期间发生漂移")
    return {
        "source": str(source),
        "series_title": series_title,
        "series_year": series_year,
        "size": int(stat_result.st_size),
        "content_sha256": digest.hexdigest(),
    }


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
        source_episode = source_episode_by_path.get(
            str(entry.source_path), target_episode
        )
        start = int(row.get("episode_start", 0))
        end = int(row.get("episode_end", 0))
        subject_id = str(row.get("bangumi_subject_id") or "")
        season_number = int(row.get("season_number", -1))
        record = TaskEpisodeOwnership(
            source_path=entry.source_path,
            bangumi_subject_id=subject_id,
            tmdb_tv_id=str(authority.work_identity.get("primary_provider", {}).get(
                "provider_id"
            ) or ""),
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
    tmdb_tv_id: str,
    season_assignments: Mapping[str, int],
    display_title: str,
    title_source: Mapping[str, object],
    series_year: Optional[int],
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
        "font_archive": _tv_font_observation(
            plan,
            task_source_scope=scope,
            series_title=display_title,
            series_year=series_year,
        ),
    }
    observation_body["evidence_fingerprint"] = _tv_observation_fingerprint(
        observation_body
    )
    blob_ref = ObservationBlobStore(
        Path(DECISION_STATE_PATH).parent / "tv_observation_blobs"
    ).put(observation_body)
    decisions = TVPlanningUserDecision.from_mapping({
        "decision_revision": expected_revision + 1,
        "confirmed_identity": ("tmdb", "tv", tmdb_tv_id),
        "confirmed_display_title": display_title,
        "title_source": (
            str(title_source.get("namespace") or ""),
            str(title_source.get("entity_type") or ""),
            str(title_source.get("provider_id") or ""),
        ),
        "season_assignments": dict(season_assignments),
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


def issue_definition(code: str, message: str) -> IssueDefinition:
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


def normalize_provider_candidates(metadata: Dict[str, object]) -> List[Dict[str, object]]:
    """Accept both the legacy group->candidate map and candidate lists."""

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
            item = dict(candidate)
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
    if any(str(item.get("entity_type")) == expected for item in candidates):
        return ""
    expected_label = "电影" if expected == "movie" else "电视剧"
    found = "、".join(dict.fromkeys(
        ENTITY_TYPE_LABELS.get(str(item.get("entity_type")), str(item.get("entity_type")))
        for item in candidates
    ))
    return f"按当前信息没有找到正确的{expected_label}匹配；以下是可能相关的{found or '其他类型'}结果，请确认。"


def can_execute_approved_plan(
    decision_status: ReviewStatus,
    task_status: str,
    *,
    test_mode: bool,
    preview_only: bool = False,
) -> bool:
    return (
        decision_status == ReviewStatus.APPROVED
        and task_status == "approved"
        and not test_mode
        and not preview_only
    )


def can_approve_plan(plan_status: str) -> bool:
    return str(plan_status) != "invalid"


def workflow_status_for_plan(plan_status: str) -> str:
    return "invalid" if str(plan_status) == "invalid" else "need_confirm"


def _task_terminal_for_projected_plan(task_id: str) -> TaskTerminalMutation:
    def update(_decision, projected, current):
        snapshot = build_plan_review_snapshot(projected)
        current.update({
            "workflow_status": workflow_status_for_plan(snapshot.plan_status),
            "plan_status": snapshot.plan_status,
            "plan_entry_count": len(snapshot.rows),
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


def export_plan_json(plan: RenamePlan) -> str:
    return json.dumps(plan.to_dict(), indent=2, ensure_ascii=False)


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


class PlanReviewDialog(ui.dialog):
    def __init__(self, plan: RenamePlan, task_data: Dict[str, object]):
        super().__init__()
        self.props("persistent")
        self.exit_requested = False
        self.plan = plan
        self.task_data = task_data
        self.snapshot = build_plan_review_snapshot(plan)
        self.review_service = PlanReviewService(
            PlanStore(PLAN_PATH),
            PLAN_REVIEW_PATH,
            decision_state_root=DECISION_STATE_PATH,
            task_root=TASK_PATH,
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
        self.task_decision = self.decision_state_store.load_optional(plan.task_id)
        self.decision = self.review_service.load_or_create(plan)
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

        with self, ui.card().classes("bar-dialog bar-review-dialog"):
            self._render_header()
            with ui.expansion(
                "重新识别设置", icon="refresh"
            ).classes("bar-confirm-card w-full"):
                self._render_rerecognition_settings()
            self.completion_container = ui.column().classes("w-full gap-2")
            self._render_completion_workflow()
            self.issues_container = ui.column().classes("w-full")
            self._render_issues()
            self._render_toolbar()
            self.entries_container = ui.column().classes(
                "bar-review-entries w-full gap-2"
            )
            self._render_entries()
            with ui.expansion(
                "高级手动修正（通常不需要）", icon="tune"
            ).classes("bar-advanced-review w-full"):
                ui.label(
                    "只有候选作品选择或自动判断无法解决时，才需要使用以下工具。"
                ).classes("bar-help")
                self._render_presentation_override()
                self._render_override()
                self._render_subtitle_override()
            self._render_actions()

    def _render_header(self) -> None:
        with ui.row().classes("bar-dialog-header w-full items-start gap-4"):
            with ui.column().classes("bar-dialog-header-content gap-1"):
                ui.label("整理任务").classes("bar-dialog-title")
                ui.label("批准和人工覆盖只记录决定，不执行文件操作。" ).classes(
                    "bar-muted"
                )
            with ui.row().classes("bar-dialog-header-actions items-center gap-2 flex-wrap justify-end"):
                self.plan_badge = ui.badge(
                    PLAN_STATUS_LABELS.get(
                        self.snapshot.plan_status, self.snapshot.plan_status
                    ),
                    color="orange-8"
                    if self.snapshot.plan_status == "need_confirm"
                    else "green-7",
                )
                self.review_badge = ui.badge(
                    REVIEW_STATUS_LABELS[self.decision.status], color="blue-7"
                )

        with ui.row().classes("bar-confirm-card w-full gap-5 flex-wrap"):
            authoritative_title = ""
            if self.task_decision is not None:
                authoritative_title = str(
                    self.task_decision.confirmed_work.get("title") or ""
                ).strip()
            if not authoritative_title and self.plan.work_identity is not None:
                authoritative_title = str(self.plan.work_identity.title or "").strip()
            self.header_title_value = _meta_item(
                "作品", authoritative_title or "未记录"
            )
            self.header_season_value = _meta_item(
                "季度", PlanReviewDialog._header_season_text(self)
            )
            self.header_recognition_value = _meta_item(
                "识别状态", PlanReviewDialog._header_recognition_text(self)
            )
            self.header_entry_count_value = _meta_item(
                f"{RENAME_PLAN_TERM}条目", str(len(self.snapshot.rows))
            )
            _meta_item("任务 ID", self.plan.task_id[:8], monospace=True)
        ui.label(f"源目录：{self.snapshot.source_root}").classes("bar-path-text")
        provider_link = provider_link_review_summary(
            self.plan.metadata.get("provider_link_snapshot")
        )
        if provider_link is not None:
            with ui.expansion(
                "已匹配作品季度",
                icon="account_tree",
                value=True,
            ).classes("bar-confirm-card w-full"):
                ui.label(
                    "这些条目将整理到同一部作品的对应季度。"
                ).classes("font-bold")
                for assignment in provider_link["assignments"]:
                    ui.label(str(assignment)).classes("bar-help")
                ui.label(str(provider_link["pending_notice"])).classes(
                    "bar-help text-orange-8"
                )
        if self.plan.migrated_from_schema == 4:
            with ui.row().classes(
                "bar-callout bar-callout-warning w-full items-start gap-2"
            ):
                ui.icon("warning", size="20px")
                with ui.column().classes("gap-1"):
                    ui.label("此整理计划已从 schema v4 转换").classes(
                        "font-bold text-orange-9"
                    )
                    ui.label(
                        "旧批准已经失效，必须重新处理并批准。原始 v4 历史不会被覆盖；"
                        "只有保存新版后才会产生新的 v5 版本。"
                    ).classes("bar-help")
                    if self.plan.migration_source_fingerprint:
                        ui.label(
                            "原始版本标识："
                            f"{self.plan.migration_source_fingerprint[:16]}…"
                        ).classes("bar-help font-mono")

        self.groups_container = ui.column().classes("w-full gap-2")
        self._render_groups()
        self.catalog_container = ui.column().classes("w-full gap-2")
        self._render_catalog_context()

    def _render_rerecognition_settings(self) -> None:
        """Keep the legacy edit fields inside the single task workspace."""

        ui.label(
            "只有当前识别结果不对或来源目录已变化时才需要修改。保存后会重新生成整理计划，旧批准不会沿用。"
        ).classes("bar-help")
        current_media_type = _media_type_from_flags(
            self.task_data.get("is_anime"), self.task_data.get("is_movie")
        )
        self.rerecognition_path = ui.input(
            "来源目录", value=str(self.task_data.get("path") or self.plan.source_root)
        ).props("outlined dense").classes("w-full")
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
            self.rerecognition_use_ai = ui.checkbox(
                "允许 AI 辅助", value=bool(self.task_data.get("use_ai", False))
            )
        with ui.row().classes("w-full gap-3 flex-wrap"):
            ui.button(
                icon="folder_open", on_click=self._pick_rerecognition_source
            ).props('outline color=blue-8 aria-label="从文件夹树选择来源目录"').tooltip(
                "从文件夹树选择来源目录"
            )
            ui.button(
                "保存并重新识别", icon="refresh", on_click=self._restart_recognition
            ).props("outline color=blue-8")

    async def _pick_rerecognition_source(self) -> None:
        current = str(self.rerecognition_path.value or cm.get_config("docker_mnt") or "/volume1")
        result = await local_file_picker(current, multiple=False, title="选择来源目录")
        if isinstance(result, (list, tuple)) and result:
            self.rerecognition_path.value = str(result[0])
            self.rerecognition_path.update()

    async def _restart_recognition(self) -> None:
        saved = await _reenqueue_from_workspace(
            self.plan.task_id,
            self.task_data,
            path_value=self.rerecognition_path.value,
            media_type=self.rerecognition_media_type.value,
            name_value=self.rerecognition_name.value,
            season_value=self.rerecognition_season.value,
            use_ai=bool(self.rerecognition_use_ai.value),
        )
        if saved:
            self._close_review()

    def _render_catalog_context(self) -> None:
        self.catalog_container.clear()
        with self.catalog_container:
            self._render_catalog_context_content()

    def _recover_missing_catalog(self) -> None:
        if self.task_decision is None:
            notify("旧任务缺少权威决策状态，请重新识别", type="warning")
            return
        try:
            self.recovery_service.rebuild_catalog(
                self.task_decision,
                self.plan,
                self.review_service.work_catalog,
                self.series_catalog,
            )
            self.task_decision, projected = self.decision_command_service.update(
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
        self._adopt_revision(projected)
        notify("已依据历史记录和现存文件重建作品登记", type="positive")

    def _recover_missing_materialization(self) -> None:
        if self.task_decision is None:
            notify("旧任务缺少权威决策状态，请重新识别", type="warning")
            return
        try:
            revised = self.recovery_service.rebuild_missing_materialization_plan(
                self.task_decision,
                self.plan,
                PlanStore(PLAN_PATH),
            )
            self.task_decision, projected = self.decision_command_service.update(
                self.plan.task_id,
                expected_revision=self.task_decision.revision,
                changes={"recovery_state": "materialization_missing"},
                planner=lambda _state: revised,
                task_terminal=_task_terminal_for_projected_plan(
                    self.plan.task_id
                ),
            )
            if projected is None:
                raise DecisionStateError("未生成恢复 Plan")
        except (DecisionStateError, ValueError) as exc:
            notify(f"无法生成恢复计划：{exc}", type="negative")
            return
        self._adopt_revision(projected)
        notify("已生成新的未批准整理计划；重新批准执行前不会创建文件", type="positive")

    def _render_catalog_context_content(self) -> None:
        """Show human-readable prior media state without exposing work UUIDs."""

        if (
            self.plan.metadata.get("mapping_source") == "inventory_review_draft"
            and not self.task_data.get("execution_status")
        ):
            return
        work = self.plan.work_identity
        group_kinds = {group.kind for group in self.plan.presentation_groups}
        group_kind = next(iter(group_kinds)) if len(group_kinds) == 1 else None
        if group_kind not in {"movie_continuous", "tv_episodes"}:
            return
        if self.plan.metadata.get("recovery_reason") == "catalog_detached":
            with ui.row().classes("bar-callout bar-callout-warning w-full gap-2"):
                ui.icon("warning")
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
                    ).props("outline color=blue-7 no-caps")
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
            if catalog_backend == "series":
                match = (
                    self.series_catalog.load(str(catalog_key))
                    if catalog_key and (
                        frozen_failure_key
                        or self.task_data.get("execution_status") in {
                            "completed", "catalog_update_pending"
                        }
                    )
                    else self.series_catalog.find_by_provider(
                        work.primary_provider.to_dict()
                    )
                )
            else:
                match = (
                    self.review_service.work_catalog.get_match(str(catalog_key))
                    if catalog_key
                    else self.review_service.work_catalog.find_exact(
                        work.primary_provider, group_kind=group_kind
                    )
                )
        except Exception as exc:
            if is_fresh_unexecuted_task(self.task_data, self.task_decision):
                return
            recovery = (
                self.recovery_service.diagnose(self.task_decision, self.plan)
                if self.task_decision is not None else None
            )
            with ui.row().classes("bar-callout bar-callout-warning w-full gap-2"):
                ui.icon("warning")
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
                            icon="restore",
                            on_click=self._recover_missing_catalog,
                        ).props("outline color=blue-7 no-caps")
                    elif recovery is not None and recovery.can_rebuild_plan:
                        ui.button(
                            "生成新的未批准整理计划",
                            icon="refresh",
                            on_click=self._recover_missing_materialization,
                        ).props("outline color=blue-7 no-caps")
                    else:
                        ui.button(
                            "恢复或重新选择源目录",
                            icon="folder_open",
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
                seasons = [
                    str(item.get("season_number"))
                    for item in match.season_bindings
                ]
                ui.label(
                    "已登记季度：" + ("、".join(seasons) if seasons else "尚无")
                ).classes("bar-help")
            if not match.canonical_root.is_dir():
                self._render_series_catalog_recovery_actions(
                    match.series_key, media_label
                )
            return
        if match is None:
            if frozen_failure_key:
                with ui.row().classes(
                    "bar-callout bar-callout-warning w-full gap-2"
                ):
                    ui.icon("warning")
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
                                icon="restore",
                                on_click=self._recover_missing_catalog,
                            ).props("outline color=blue-7 no-caps")
                return
            ui.label(
                f"本地作品登记：尚未发现同一资料条目的已整理{media_label}；"
                "本次真实执行成功后才会建立登记。"
            ).classes("bar-help")
            suggestions = self.review_service.work_catalog.suggestions(
                work.title, work.year, work.primary_provider.aliases,
                group_kind=group_kind,
                include_related_series=is_tv,
            )
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
                            icon="merge_type",
                            on_click=partial(
                                self._link_existing_catalog_work,
                                suggestion.work_key,
                            ),
                        ).props("outline color=blue-8")
        else:
            with ui.card().classes("bar-confirm-card w-full"):
                ui.label("已登记的同一系列" if is_tv else "已登记的同一电影").classes("font-bold")
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
                ui.label(
                    ("确认后，本任务会归入这个系列目录并写入相应 Season 目录。"
                     if is_tv else "确认后，本任务会向这个目录追加新规格，不会创建第二个同名目录。")
                ).classes("bar-help text-blue-8")
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
                        icon="drive_file_move",
                        on_click=partial(
                            self._link_existing_catalog_work, match.work_key
                        ),
                    ).props("outline color=orange-8")
                with ui.card().classes("bar-confirm-card w-full"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("drive_file_move", color="blue-8")
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
                            icon="folder_open",
                            on_click=self._pick_catalog_relocation_directory,
                        ).props(
                            'flat round color=blue-8 aria-label="从文件夹树选择移动后的目录"'
                        ).tooltip("从文件夹树选择")
                    with ui.row().classes("w-full gap-2 flex-wrap"):
                        ui.button(
                            "核对并重新定位登记",
                            icon="verified",
                            on_click=partial(
                                self._relocate_catalog_work, match.work_key
                            ),
                        ).props("outline color=blue-8")
                with ui.card().classes("bar-confirm-card w-full"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("delete_outline", color="red-7")
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
                        icon="delete_forever",
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
                    ui.icon("info")
                    ui.label(
                        "路径配置已在此整理计划生成后改变。当前计划仍使用已确认目录；"
                        "如需采用新目录，请返回任务点击“重试”生成并重新处理。"
                    )

    def _link_existing_catalog_work(self, work_key: str) -> None:
        try:
            revised = self.review_service.link_existing_catalog_work(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                work_key=work_key,
                reason="用户确认不同资料来源指向同一本地作品",
            )
        except PlanReviewError as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        self._adopt_revision(revised)
        self._render_catalog_context()
        notify("已关联既有作品目录；尚未创建或移动任何文件", type="positive")

    def _render_series_catalog_recovery_actions(
        self, series_key: str, media_label: str
    ) -> None:
        with ui.row().classes("bar-callout bar-callout-warning w-full gap-2"):
            ui.icon("warning")
            ui.label(
                "作品登记目录不存在。请选择原目录已移动或原目录已删除；"
                "BAR 不会替你移动或删除任何媒体文件。"
            )
        with ui.card().classes("bar-confirm-card w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("drive_file_move", color="blue-8")
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
                    icon="folder_open",
                    on_click=self._pick_catalog_relocation_directory,
                ).props(
                    'flat round color=blue-8 aria-label="从文件夹树选择移动后的目录"'
                ).tooltip("从文件夹树选择")
            ui.button(
                "核对并重新定位登记",
                icon="verified",
                on_click=partial(self._relocate_series_catalog, series_key),
            ).props("outline color=blue-8")
        with ui.card().classes("bar-confirm-card w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("delete_outline", color="red-7")
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
                icon="delete_forever",
                on_click=partial(self._archive_missing_series_catalog, series_key),
            ).props("outline color=red-7")

    def _relocate_series_catalog(self, series_key: str) -> None:
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
            self.series_catalog.relocate(
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

    def _archive_missing_series_catalog(self, series_key: str) -> None:
        confirmed = bool(
            getattr(self.catalog_delete_confirmation, "value", False)
        )
        if not confirmed:
            notify("请先确认原作品目录已经不存在", type="warning")
            return
        self._detach_missing_catalog("series", series_key)

    def _relocate_catalog_work(self, work_key: str) -> None:
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
            self.review_service.work_catalog.relocate(
                work_key,
                Path(value),
                allowed_root=allowed_root,
                task_root=TASK_PATH,
            )
        except (PlanReviewError, TypeError, ValueError, RuntimeError) as exc:
            notify(localize_plan_term(exc), type="negative")
            return
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

    def _archive_missing_catalog_work(self, work_key: str) -> None:
        confirmed = bool(
            getattr(self.catalog_delete_confirmation, "value", False)
        )
        if not confirmed:
            notify("请先确认原电影目录已经不存在", type="warning")
            return
        self._detach_missing_catalog("work", work_key)

    def _detach_missing_catalog(self, backend: str, catalog_key: str) -> None:
        if self.task_decision is None:
            notify("旧任务缺少权威决策状态，请重新识别", type="warning")
            return
        try:
            decision, revised, transaction = self.catalog_detach_service.execute(
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
        self._adopt_revision(revised)
        notify(
            "失效作品登记已安全归档，NAS 文件未被删除；"
            "已生成新的未批准整理计划，请重新检查并批准",
            type="positive",
        )

    def _render_detach_transaction_recovery(
        self, backend: str, catalog_key: str
    ) -> None:
        with ui.row().classes("bar-callout bar-callout-warning w-full gap-2"):
            ui.icon("warning")
            with ui.column().classes("gap-1"):
                ui.label("作品登记删除事务尚未完成").classes("font-bold")
                ui.label(
                    "在事务完成前不会执行整理计划。请继续事务；若阶段记录缺失或损坏，"
                    "请先按 Catalog 与内部归档证据对账。"
                ).classes("bar-help")
                try:
                    transaction = self.recovery_transaction_store.load_optional(
                        self.plan.task_id
                    )
                except RecoveryTransactionError:
                    transaction = None
                if transaction is None:
                    ui.button(
                        "对账并继续删除登记",
                        icon="rule",
                        on_click=partial(
                            self._reconcile_catalog_detach, backend, catalog_key
                        ),
                    ).props("outline color=orange-8 no-caps")
                else:
                    ui.button(
                        "继续删除登记事务",
                        icon="play_arrow",
                        on_click=partial(
                            self._detach_missing_catalog, backend, catalog_key
                        ),
                    ).props("outline color=blue-8 no-caps")

    def _reconcile_catalog_detach(self, backend: str, catalog_key: str) -> None:
        try:
            decision, revised, transaction = (
                self.catalog_detach_service.reconcile_and_execute(
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
        self._adopt_revision(revised)
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
            self._render_selection_operation()
            candidates = normalize_provider_candidates(dict(self.plan.metadata))
            work = self.snapshot.work_identity
            unresolved_work = str(work.get("status")) != "confirmed"
            if not unresolved_work and not self.provider_selection_expanded:
                self._render_selected_provider_identity()
            elif candidates or unresolved_work:
                if self.plan.metadata.get("presentation_assignment_state") == "placeholder":
                    ui.label(
                        "系统无法仅凭目录结构确定作品与呈现形式。请先从下列候选中确认；"
                        "当前文件分组只是安全占位，不会被自动执行。"
                    ).classes("bar-help text-orange-8")
                warning = (
                    candidate_type_warning(
                        self.task_data.get("is_movie", self.plan.metadata.get("is_movie")),
                        candidates,
                    )
                    if candidates else ""
                )
                if warning:
                    with ui.row().classes("bar-callout bar-callout-warning w-full"):
                        ui.icon("warning", size="20px")
                        ui.label(warning)
                ui.label("选择正确的作品").classes("text-subtitle2")
                with ui.row().classes("w-full items-start gap-4 flex-wrap"):
                    self._render_provider_column("bangumi", candidates)
                    self._render_provider_column("tmdb", candidates)
            if self.snapshot.presentation_groups:
                ui.label("当前作品信息").classes("text-subtitle2")
                with ui.card().classes("bar-group-summary w-full"):
                    ui.label(
                        str(work.get("title") or "尚未确认作品")
                    ).classes("bar-evidence font-bold")
                    if not unresolved_work:
                        domain = str(work.get("domain") or "")
                        group_kind = str(
                            self.snapshot.presentation_groups[0].get("kind") or ""
                        )
                        if domain not in {"animation", "general"}:
                            kind_text = "保存位置待确认"
                        elif group_kind == "tv_episodes":
                            kind_text = "动画剧集" if domain == "animation" else "普通剧集"
                        else:
                            kind_text = "动画电影" if domain == "animation" else "普通电影"
                        ui.label(f"作品类型：{kind_text}").classes("bar-help")
                    else:
                        ui.label("请从上方候选中确认作品；不会影响观看形式选择。").classes(
                            "bar-help text-orange-8"
                        )
                    if self.snapshot.linked_provider_identities:
                        linked_text = "；".join(
                            f"{str(item.get('namespace') or '资料').title()} "
                            f"#{item.get('provider_id') or '?'}"
                            for item in self.snapshot.linked_provider_identities
                        )
                        linked_label = (
                            "季度资料" if group_kind == "tv_episodes" else "关联资料"
                        )
                        ui.label(f"{linked_label}：{linked_text}").classes("bar-help")
                ui.label("当前呈现分组").classes("text-subtitle2")
                with ui.row().classes("w-full gap-2 flex-wrap"):
                    for group in self.snapshot.presentation_groups:
                        with ui.card().classes("bar-group-summary grow"):
                            ui.label(str(group["display_name"])).classes("bar-evidence")
                            if group["preferred_version_ref"]:
                                ui.label(
                                    f"偏好版本：{group['preferred_version_ref']}"
                                ).classes("bar-help")

    def _render_selection_operation(self) -> None:
        operation = self.task_data.get("selection_operation")
        if not isinstance(operation, Mapping):
            return
        state = str(operation.get("state") or "")
        if state == "pending":
            with ui.row().classes("bar-callout w-full items-center gap-2"):
                ui.spinner(size="20px")
                ui.label("正在更新作品信息，请稍候…").classes("font-bold")
            return
        if state not in {"failed", "interrupted", "awaiting_user"}:
            return
        reasons = {
            "provider_unavailable": "暂时无法读取作品资料",
            "relation_unresolved": "没有找到可靠的季度关系",
            "save_failed": "更新结果未能安全保存",
            "stale_request": "页面内容已经更新",
            "app_restarted": "处理期间应用重新启动",
            "selection_failed": "未能应用所选作品",
        }
        code = str(operation.get("failure_code") or "selection_failed")
        candidate = operation.get("candidate")
        reason_codes = {
            str(item) for item in operation.get("reason_codes", ()) or ()
        }
        refresh_required = bool(operation.get("retry_requires_refresh"))
        boundary_required = "subject_candidate_evidence_missing" in reason_codes
        boundary_edges = ()
        evidence = operation.get("provider_subject_evidence")
        if boundary_required and isinstance(evidence, Mapping):
            raw_edges = evidence.get("provider_subject_boundary_edges")
            if isinstance(raw_edges, (list, tuple)):
                boundary_edges = tuple(
                    dict(item) for item in raw_edges if isinstance(item, Mapping)
                )
        expected_revision = operation.get("expected_decision_revision")
        evidence_fingerprint = str(operation.get("evidence_fingerprint") or "")
        cohort_preview = operation.get("provider_subject_cohort_preview")
        boundary_failure = operation.get("boundary_resolution_failure")
        if (
            not isinstance(cohort_preview, Mapping)
            and isinstance(evidence, Mapping)
            and isinstance(expected_revision, int)
            and len(boundary_edges) == 1
        ):
            try:
                cohort_preview = build_provider_subject_cohort_preview(
                    evidence,
                    expected_decision_revision=expected_revision,
                    boundary_edge=boundary_edges[0],
                    target_root_snapshot=self.task_data.get(
                        "draft_root_snapshot"
                    ),
                    target_root_config_fingerprint=self.task_data.get(
                        "draft_root_config_fingerprint"
                    ),
                    work_domain="animation",
                    group_kind="tv_episodes",
                )
            except (TypeError, ValueError) as preview_error:
                boundary_failure = build_provider_subject_boundary_failure(
                    evidence, preview_error
                )
        with ui.card().classes("bar-callout bar-callout-warning w-full"):
            ui.label("这次选择未生效").classes("font-bold text-orange-9")
            ui.label(reasons.get(code, reasons["selection_failed"])).classes("bar-help")
            if isinstance(cohort_preview, Mapping):
                ui.label("本次确认将整理以下季度：").classes(
                    "font-bold text-blue-9"
                )
                for row in cohort_preview.get("season_assignments", ()) or ():
                    if not isinstance(row, Mapping):
                        continue
                    title = str(row.get("title") or "作品")
                    season_number = row.get("season_number")
                    ui.label(f"{title} → 第 {season_number} 季").classes(
                        "bar-help"
                    )
                outside = tuple(
                    str(item) for item in cohort_preview.get(
                        "outside_subject_titles", ()
                    ) or () if str(item)
                )
                if outside:
                    ui.label(
                        "仍需单独处理：" + "、".join(outside)
                    ).classes("bar-help text-orange-9")
                resolved_target_root = str(
                    cohort_preview.get("resolved_target_root") or ""
                ).strip()
                if resolved_target_root:
                    ui.label(
                        "整理目标：" + resolved_target_root
                    ).classes("bar-help text-blue-9")
            if isinstance(boundary_failure, Mapping):
                ui.label(
                    str(
                        boundary_failure.get("public_title")
                        or "无法安全确定季度边界"
                    )
                ).classes("font-bold text-red-8")
                affected = tuple(
                    str(item) for item in boundary_failure.get(
                        "affected_subject_titles", ()
                    ) or () if str(item)
                )
                if affected:
                    ui.label("涉及：" + "、".join(affected)).classes(
                        "bar-help"
                    )
                summary = str(
                    boundary_failure.get("technical_summary") or ""
                ).strip()
                if summary:
                    ui.label(summary).classes("bar-help text-grey-8")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                if isinstance(candidate, dict) and refresh_required:
                    ui.button(
                        "重新获取资料",
                        on_click=partial(self._select_provider_candidate, candidate),
                        icon="refresh",
                    ).props("unelevated color=blue-7 no-caps")
                if isinstance(expected_revision, int) and len(boundary_edges) == 1:
                    for edge in boundary_edges:
                        ui.button(
                            "确认与相邻作品一起整理",
                            on_click=partial(
                                self._apply_provider_subject_boundary_inline,
                                True,
                                expected_revision,
                                evidence_fingerprint,
                                edge,
                            ),
                            icon="link",
                        ).props("outline color=blue-7 no-caps")
                        ui.button(
                            "分开整理",
                            on_click=partial(
                                self._apply_provider_subject_boundary_inline,
                                False,
                                expected_revision,
                                evidence_fingerprint,
                                edge,
                            ),
                            icon="call_split",
                        ).props("outline color=blue-7 no-caps")
                ui.button(
                    "换一个作品",
                    on_click=self._show_provider_candidates,
                    icon="swap_horiz",
                ).props("outline color=blue-7 no-caps")
                ui.button(
                    "移除任务",
                    on_click=self._remove_provider_failure_task,
                    icon="delete_outline",
                ).props("flat color=red-7 no-caps")
                ui.button("关闭", on_click=self._close_review).props(
                    "flat color=grey-8"
                )

    def _reload_provider_subject_authority(self) -> bool:
        """Atomically replace the dialog cache from the three disk authorities."""

        task_id = self.plan.task_id
        plan_store = PlanStore(PLAN_PATH)
        decision_store = DecisionStateStore(DECISION_STATE_PATH)
        try:
            task = get_task(task_id)
            plan = plan_store.load(task_id)
            decision = decision_store.load(task_id)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            PlanStoreError,
            DecisionStateError,
            TypeError,
            ValueError,
        ):
            return False
        if not task or str(task.get("uuid") or "") != task_id:
            return False
        self.task_data = task
        self.plan = plan
        self.snapshot = build_plan_review_snapshot(plan)
        self.task_decision = decision
        self.decision_state_store = decision_store
        self.decision_command_service = DecisionCommandService(
            decision_store, plan_store
        )
        return True

    def _render_reloaded_provider_subject_authority(self) -> None:
        """Invalidate old controls and expose only callbacks frozen from disk."""

        instance_renderer = self.__dict__.get("_render_groups")
        if callable(instance_renderer):
            instance_renderer()
        elif getattr(self, "groups_container", None) is not None:
            self._render_groups()

    def _provider_subject_receipt_matches(
        self,
        *,
        request_fingerprint: str,
        expected_revision: int,
    ) -> bool:
        receipt = self.plan.metadata.get(
            "provider_subject_boundary_selection_receipt"
        )
        if not isinstance(receipt, Mapping) or self.task_decision is None:
            return False
        decision_revision = self.task_decision.revision
        valid = bool(
            receipt.get("schema_version") in {1, 2, 3}
            and receipt.get("request_fingerprint") == request_fingerprint
            and receipt.get("expected_decision_revision") == expected_revision
            and receipt.get("result_decision_revision") == decision_revision
            and decision_revision == expected_revision + 1
            and self.plan.metadata.get("decision_state_revision")
            == decision_revision
            and self.plan.metadata.get("decision_state_fingerprint")
            == DecisionStateStore.fingerprint(self.task_decision)
        )
        if receipt.get("schema_version") in {2, 3}:
            valid = bool(
                valid
                and receipt.get("result_decision_fingerprint")
                == DecisionStateStore.fingerprint(self.task_decision)
            )
        if receipt.get("schema_version") == 3:
            valid = bool(
                valid
                and receipt.get("target_root_config_fingerprint")
                == self.plan.metadata.get("target_root_config_fingerprint")
                and receipt.get("resolved_target_root")
                == self.plan.metadata.get("target_root")
            )
        return valid

    def _persist_provider_subject_boundary_failure(
        self,
        *,
        expected_revision: int,
        evidence_fingerprint: str,
        evidence: Mapping[str, object],
        error: BaseException,
    ) -> Optional[dict]:
        failure = build_provider_subject_boundary_failure(evidence, error)

        def persist(task: dict) -> Optional[dict]:
            operation = task.get("selection_operation")
            if (
                not isinstance(operation, dict)
                or operation.get("state") not in {"failed", "awaiting_user"}
                or operation.get("expected_decision_revision")
                != expected_revision
                or str(operation.get("evidence_fingerprint") or "")
                != evidence_fingerprint
            ):
                return None
            revised = dict(operation)
            revised["boundary_resolution_failure"] = failure
            task["selection_operation"] = revised
            return task

        updated = mutate_task(self.plan.task_id, persist)
        if updated is not None:
            self.task_data = updated
        return updated

    def _persist_tv_planning_authority_failure(
        self,
        *,
        expected_revision: int,
        evidence_fingerprint: str,
    ) -> Optional[dict]:
        occurred_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        operation_id = uuid.uuid4().hex
        failure = {
            "operation_id": operation_id,
            "code": "tv_planning_authority_unavailable",
            "phase": "tv_planning_authority",
            "decision_revision": expected_revision,
            "authority_fingerprint": None,
            "outcome": "failed",
            "occurred_at": occurred_at,
            "safe_actions": ["retry", "replan", "safe_abandon"],
        }

        def persist(task: dict) -> Optional[dict]:
            operation = task.get("selection_operation")
            if (
                not isinstance(operation, dict)
                or operation.get("expected_decision_revision")
                != expected_revision
                or str(operation.get("evidence_fingerprint") or "")
                != evidence_fingerprint
            ):
                return None
            revised = dict(operation)
            revised.update({
                "state": "failed",
                "failure_code": "tv_planning_authority_unavailable",
                "reason_codes": ["tv_planning_authority_unavailable"],
                "retryable": True,
            })
            task["selection_operation"] = revised
            history_value = task.get("operation_history", ())
            history = (
                [dict(item) for item in history_value]
                if isinstance(history_value, (list, tuple))
                and all(isinstance(item, Mapping) for item in history_value)
                else []
            )
            history.append({
                key: value for key, value in failure.items()
                if key != "safe_actions"
            })
            task["operation_history"] = history
            task["operation_history_state"] = "current"
            task["current_operation_failure"] = failure
            task["workflow_status"] = "invalid"
            task["error"] = "TV 规划依据未能安全保存，请重试或重新生成计划"
            task["completed_at"] = occurred_at
            return task

        updated = mutate_task(self.plan.task_id, persist)
        if updated is not None:
            self.task_data = updated
        return updated

    def _close_provider_subject_selection(
        self,
        *,
        expected_revision: int,
        evidence_fingerprint: str,
        result_revision: int,
    ) -> Optional[dict]:
        def close_selection(task: dict) -> Optional[dict]:
            current = task.get("selection_operation")
            if (
                not isinstance(current, dict)
                or current.get("state") not in {"failed", "awaiting_user"}
                or current.get("expected_decision_revision")
                != expected_revision
                or str(current.get("evidence_fingerprint") or "")
                != evidence_fingerprint
            ):
                return None
            updated_operation = dict(current)
            updated_operation.update({
                "state": "succeeded",
                "result_decision_revision": result_revision,
                "failure_code": None,
                "reason_codes": [],
                "retryable": False,
            })
            updated_operation.pop("boundary_resolution_failure", None)
            task["selection_operation"] = updated_operation
            task.pop("provider_selection_failure", None)
            task["workflow_status"] = "need_confirm"
            task["error"] = None
            task["completed_at"] = None
            return task

        updated_task = mutate_task(self.plan.task_id, close_selection)
        if updated_task is not None:
            self.task_data = updated_task
        return updated_task

    def _apply_provider_subject_boundary_inline(
        self,
        same_work: bool,
        expected_revision: int,
        evidence_fingerprint: str,
        boundary_edge: Mapping[str, object],
    ) -> None:
        if not self._reload_provider_subject_authority():
            notify("无法读取当前任务状态，请关闭后重新打开审核", type="negative")
            return
        operation = self.task_data.get("selection_operation")
        evidence = (
            operation.get("provider_subject_evidence")
            if isinstance(operation, Mapping) else None
        )
        cohort_preview = (
            operation.get("provider_subject_cohort_preview")
            if isinstance(operation, Mapping) else None
        )
        if (
            same_work
            and isinstance(operation, Mapping)
            and operation.get("state") == "succeeded"
            and not isinstance(cohort_preview, Mapping)
        ):
            self._render_reloaded_provider_subject_authority()
            notify("页面内容已经更新，请按当前状态重新选择", type="warning")
            return
        if same_work and not isinstance(cohort_preview, Mapping):
            if not isinstance(evidence, Mapping):
                self._render_reloaded_provider_subject_authority()
                notify("页面内容已经更新，请按当前状态重新选择", type="warning")
                return
            try:
                cohort_preview = build_provider_subject_cohort_preview(
                    evidence,
                    expected_decision_revision=expected_revision,
                    boundary_edge=boundary_edge,
                    target_root_snapshot=self.task_data.get(
                        "draft_root_snapshot"
                    ),
                    target_root_config_fingerprint=self.task_data.get(
                        "draft_root_config_fingerprint"
                    ),
                    work_domain="animation",
                    group_kind="tv_episodes",
                )
            except (TypeError, ValueError) as preview_error:
                self._persist_provider_subject_boundary_failure(
                    expected_revision=expected_revision,
                    evidence_fingerprint=evidence_fingerprint,
                    evidence=evidence,
                    error=preview_error,
                )
                if self._reload_provider_subject_authority():
                    self._render_reloaded_provider_subject_authority()
                notify("无法安全确定季度边界", type="negative")
                return
        preview_request: Optional[Mapping[str, object]] = None
        if same_work and isinstance(cohort_preview, Mapping):
            request_fingerprint = str(
                cohort_preview.get("request_fingerprint") or ""
            )
            preview_request = cohort_preview.get("request")
            if (
                not isinstance(preview_request, Mapping)
                or provider_subject_cohort_request_fingerprint(
                    preview_request
                ) != request_fingerprint
            ):
                self._render_reloaded_provider_subject_authority()
                notify("季度授权范围校验失败，请刷新后重试", type="warning")
                return
        else:
            request_fingerprint = provider_subject_cohort_request_fingerprint({
                "schema_version": 1,
                "same_work": bool(same_work),
                "expected_decision_revision": expected_revision,
                "evidence_fingerprint": evidence_fingerprint,
                "boundary_edge": dict(boundary_edge),
            })
        if len(request_fingerprint) != 64:
            self._render_reloaded_provider_subject_authority()
            notify("页面授权范围不完整，请刷新后重试", type="warning")
            return
        receipt_already_matches = self._provider_subject_receipt_matches(
            request_fingerprint=request_fingerprint,
            expected_revision=expected_revision,
        )
        routing_snapshot = None
        if not receipt_already_matches:
            from ..rename.target_routing import (
                resolve_target_root,
                snapshot_from_config_manager,
            )

            try:
                routing_snapshot = snapshot_from_config_manager(cm)
                live_target_root = resolve_target_root(
                    "animation", "tv_episodes", routing_snapshot
                )
            except (TypeError, ValueError) as routing_error:
                self._render_reloaded_provider_subject_authority()
                notify(localize_plan_term(routing_error), type="warning")
                return
            if same_work:
                preview_root_fingerprint = str(
                    preview_request.get("target_root_config_fingerprint")
                    if isinstance(preview_request, Mapping) else ""
                )
                preview_target_root = str(
                    preview_request.get("resolved_target_root")
                    if isinstance(preview_request, Mapping) else ""
                )
                if (
                    preview_root_fingerprint != routing_snapshot.fingerprint
                    or preview_target_root != str(live_target_root)
                ):
                    self._render_reloaded_provider_subject_authority()
                    notify(
                        "路径配置已经变化，请刷新页面并重新确认整理目标",
                        type="warning",
                    )
                    return
        if receipt_already_matches:
            result_revision = self.task_decision.revision
            if (
                isinstance(operation, Mapping)
                and operation.get("state") == "succeeded"
                and operation.get("result_decision_revision") == result_revision
            ):
                self._render_reloaded_provider_subject_authority()
                return
            repaired = self._close_provider_subject_selection(
                expected_revision=expected_revision,
                evidence_fingerprint=evidence_fingerprint,
                result_revision=result_revision,
            )
            self._render_reloaded_provider_subject_authority()
            if repaired is not None:
                notify(
                    "已确认一起整理" if same_work else "已确认分开整理",
                    type="positive",
                )
            else:
                notify("页面内容已经更新，请按当前状态继续", type="warning")
            return
        if (
            self.task_decision is None
            or not isinstance(operation, Mapping)
            or operation.get("state") not in {"failed", "awaiting_user"}
            or operation.get("expected_decision_revision") != expected_revision
            or str(operation.get("evidence_fingerprint") or "")
            != evidence_fingerprint
            or not isinstance(evidence, Mapping)
        ):
            self._render_reloaded_provider_subject_authority()
            notify("页面内容已经更新，请按当前状态重新选择", type="warning")
            return
        try:
            try:
                resolution, boundary_decision = (
                    resolve_provider_subject_boundary_decision(
                        evidence,
                        same_work=same_work,
                        boundary_edge=boundary_edge,
                    )
                )
            except (TypeError, ValueError) as boundary_error:
                self._persist_provider_subject_boundary_failure(
                    expected_revision=expected_revision,
                    evidence_fingerprint=evidence_fingerprint,
                    evidence=evidence,
                    error=boundary_error,
                )
                if self._reload_provider_subject_authority():
                    self._render_reloaded_provider_subject_authority()
                notify("无法安全确定季度边界", type="negative")
                return
            candidate = operation.get("candidate")
            if not isinstance(candidate, Mapping):
                raise DecisionStateError("所选作品资料不完整")
            tmdb_candidate = _trusted_relation_tmdb_anchor(
                evidence,
                str(resolution.tmdb_tv_id or ""),
            )
            air_date = str(tmdb_candidate.get("first_air_date") or "")
            provider_year = (
                int(air_date[:4]) if air_date[:4].isdigit() else None
            )
            from ..rename.canonical_naming import (
                CanonicalNamingError,
                resolve_title_authority,
                select_bangumi_work_title_owner,
            )

            title_selection = {
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": str(candidate.get("provider_id") or ""),
                "localized_title": str(candidate.get("title") or ""),
                "original_title": str(candidate.get("original_title") or ""),
            }
            if same_work:
                # A Bangumi sequel chain may represent several seasons of one
                # TMDB work.  The clicked season is the season authority, but
                # it must not rename the shared work root to "第二季" etc.
                # Choose the earliest frozen season as the stable Bangumi
                # title owner; the selected season remains bound separately by
                # the cohort receipt and season assignment below.
                try:
                    title_owner = select_bangumi_work_title_owner(
                        subjects=(
                            item
                            for item in evidence.get("bangumi_subjects", ()) or ()
                            if isinstance(item, Mapping)
                        ),
                        season_assignments=dict(resolution.season_assignments),
                    )
                except CanonicalNamingError as exc:
                    raise DecisionStateError(str(exc)) from exc
                title_selection = {
                    "namespace": "bangumi",
                    "entity_type": "subject",
                    "provider_id": str(
                        title_owner.get("provider_id")
                        or title_owner.get("id")
                        or ""
                    ),
                    "localized_title": str(
                        title_owner.get("title")
                        or title_owner.get("name_cn")
                        or ""
                    ),
                    "original_title": str(
                        title_owner.get("original_title")
                        or title_owner.get("name")
                        or ""
                    ),
                }

            try:
                naming_decision = resolve_title_authority(
                    selection=title_selection,
                    mapped_candidates=({
                        "namespace": "tmdb",
                        "entity_type": "tv",
                        "provider_id": str(resolution.tmdb_tv_id or ""),
                        "localized_title": str(
                            tmdb_candidate.get("title") or ""
                        ),
                        "original_title": str(
                            tmdb_candidate.get("original_title") or ""
                        ),
                    },),
                    primary_provider_identity={
                        "namespace": "tmdb",
                        "entity_type": "tv",
                        "provider_id": str(resolution.tmdb_tv_id or ""),
                    },
                )
            except CanonicalNamingError as exc:
                raise DecisionStateError(str(exc)) from exc
            provider_title = naming_decision.display_title
            if not self.plan.presentation_groups:
                raise DecisionStateError("整理计划缺少观看形式")
            group_id = self.plan.presentation_groups[0].group_id
            confirmed_plan = self.review_service.upsert_presentation_group(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                group_id=group_id,
                kind="tv_episodes",
                display_name="TV 分集版",
                provider_namespace="tmdb",
                provider_entity_type="tv",
                provider_id=str(resolution.tmdb_tv_id),
                provider_title=provider_title,
                provider_year=provider_year,
                provider_original_title=str(
                    tmdb_candidate.get("original_title") or ""
                ) or None,
                reason="用户确认相邻作品的整理边界",
                persist_revision=False,
            )
            if routing_snapshot is None:
                raise DecisionStateError("冻结目标配置不完整")
            from ..rename.target_routing import resolve_target_root

            target_root = resolve_target_root(
                "animation", "tv_episodes", routing_snapshot
            )
            confirmed_metadata = dict(confirmed_plan.metadata)
            confirmed_metadata.update({
                "allowed_target_root": str(target_root),
                "target_root": str(target_root),
                "canonical_naming_required": True,
                "canonical_naming_decision": naming_decision.to_dict(),
            })
            confirmed_plan = replace(
                confirmed_plan, metadata=confirmed_metadata
            )
            selected_subject_id = str(candidate.get("provider_id") or "")
            season_number = dict(resolution.season_assignments).get(
                selected_subject_id
            )
            if not isinstance(season_number, int):
                raise DecisionStateError("所选作品的季度编号不完整")
            completed_plan = self.review_service.complete_inventory_draft(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                group_id=group_id,
                group_kind="tv_episodes",
                target_root=target_root,
                reason="用户确认相邻作品的整理边界",
                root_snapshot=routing_snapshot.to_dict(),
                work_domain="animation",
                season_number=season_number,
                season_source="provider",
                series_key=str(resolution.proposed_series_key or ""),
                season_binding_id=f"season-binding-{uuid.uuid4()}",
                series_catalog_revision=0,
                persist_revision=False,
                _source_plan=confirmed_plan,
                _revision_parent_fingerprint=PlanStore.fingerprint(self.plan),
            )
            # The provider boundary decision supplies the missing work/season
            # authority.  Deterministic regular episodes and structural
            # ``13(OVA)`` bonus pairs therefore do not require a second,
            # per-file confirmation pass.  Other extras and provider specials
            # keep their existing explicit-review contracts.
            completed_plan = replace(
                completed_plan,
                entries=tuple(
                    replace(entry, requires_review=False)
                    if entry.role in {
                        "main_episode",
                        "episode_like_bonus",
                        "extra_subtitle",
                    }
                    else entry
                    for entry in completed_plan.entries
                ),
            )
            completed_metadata = dict(completed_plan.metadata)
            if same_work and isinstance(cohort_preview, Mapping):
                request = cohort_preview.get("request")
                if not isinstance(request, Mapping):
                    raise DecisionStateError("季度合并授权范围不完整")
                completed_metadata[
                    "provider_subject_boundary_selection_receipt"
                ] = {
                    "schema_version": 3,
                    "request_fingerprint": request_fingerprint,
                    "selected_subject_id": str(
                        request.get("selected_subject_id") or ""
                    ),
                    "cohort_subject_ids": list(
                        request.get("cohort_subject_ids", ()) or ()
                    ),
                    "cohort_edges": [
                        dict(item)
                        for item in request.get("cohort_edges", ()) or ()
                        if isinstance(item, Mapping)
                    ],
                    "subject_evidence_fingerprints": dict(
                        request.get("subject_evidence_fingerprints") or {}
                    ),
                    "selected_tmdb_tv_id": str(
                        request.get("selected_tmdb_tv_id") or ""
                    ),
                    "season_assignments": dict(
                        request.get("season_assignments") or {}
                    ),
                    "derived_episode_ranges": [
                        dict(item)
                        for item in request.get(
                            "derived_episode_ranges", ()
                        ) or () if isinstance(item, Mapping)
                    ],
                    "resolution_fingerprint": str(
                        request.get("resolution_fingerprint") or ""
                    ),
                    "cohort_decision_id": str(
                        request.get("cohort_decision_id") or ""
                    ),
                    "target_root_config_fingerprint": str(
                        request.get("target_root_config_fingerprint") or ""
                    ),
                    "resolved_target_root": str(
                        request.get("resolved_target_root") or ""
                    ),
                    "expected_decision_revision": expected_revision,
                    "result_decision_revision": expected_revision + 1,
                    "canonical_naming_decision_fingerprint": (
                        naming_decision.decision_fingerprint
                    ),
                }
            else:
                completed_metadata[
                    "provider_subject_boundary_selection_receipt"
                ] = {
                    "schema_version": 1,
                    "request_fingerprint": request_fingerprint,
                    "expected_decision_revision": expected_revision,
                    "result_decision_revision": expected_revision + 1,
                    "canonical_naming_decision_fingerprint": (
                        naming_decision.decision_fingerprint
                    ),
                }
            completed_plan = replace(
                completed_plan, metadata=completed_metadata
            )
            candidate_state = self.task_decision
            selected_evidence = {
                "status": "confirmed",
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": str(candidate.get("provider_id") or ""),
                "title": str(candidate.get("title") or ""),
                "original_title": str(candidate.get("original_title") or ""),
                "decision_source": "explicit_user",
            }
            primary_provider = provider_identity_from_metadata(
                namespace="tmdb",
                entity_type="tv",
                provider_id=str(resolution.tmdb_tv_id or ""),
                title=provider_title,
                year=provider_year,
                decision_source="explicit_user",
                original_title=str(
                    tmdb_candidate.get("original_title") or ""
                ) or None,
            )
            resolved_work = WorkIdentity(
                status="confirmed",
                domain="animation",
                title=provider_title,
                year=provider_year,
                primary_provider=primary_provider,
                domain_source="provider_selection",
            )
            provider_evidence = (primary_provider.to_dict(),) + tuple(
                item for item in candidate_state.provider_evidence
                if not (
                    (
                        item.get("namespace") == "bangumi"
                        and item.get("entity_type") == "subject"
                        and str(item.get("provider_id") or "")
                        == selected_evidence["provider_id"]
                    )
                    or (
                        item.get("namespace") == "tmdb"
                        and item.get("entity_type") == "tv"
                    )
                )
            ) + (selected_evidence,)
            linked_provider_identities = tuple(
                item for item in candidate_state.linked_provider_identities
                if not (
                    item.get("namespace") == "bangumi"
                    and item.get("entity_type") == "subject"
                    and str(item.get("provider_id") or "")
                    == selected_evidence["provider_id"]
                )
            ) + (selected_evidence,)
            season_binding_id = str(
                completed_plan.metadata.get("season_binding_id") or ""
            )
            if not season_binding_id:
                raise DecisionStateError("整理计划缺少季度决定编号")
            staging_state = replace(
                candidate_state,
                confirmed_work=resolved_work.to_dict(),
                provider_evidence=provider_evidence,
                linked_provider_identities=linked_provider_identities,
                season_binding={
                    "season_binding_id": season_binding_id,
                    "season_number": season_number,
                    "decision_source": str(
                        completed_plan.metadata.get("season_source") or "provider"
                    ),
                },
                library_domain="animation",
                layout_mode="tv_episodes",
            )
            canonical_root_value = str(
                completed_plan.metadata.get("canonical_work_root") or ""
            ).strip()
            if not canonical_root_value:
                raise DecisionStateError("整理计划缺少规范作品目录")
            canonical_root = Path(canonical_root_value)
            catalog_revision = completed_plan.metadata.get(
                "series_catalog_revision", 0
            )
            if not isinstance(catalog_revision, int):
                raise DecisionStateError("作品登记版本不完整")
            staged = stage_provider_link_series_decision(
                staging_state,
                resolution,
                canonical_root=canonical_root,
                catalog_revision=catalog_revision,
            )
            tv_planning_authority = _build_cohort_tv_planning_authority(
                plan=completed_plan,
                task_data=self.task_data,
                evidence=evidence,
                preview_request=(
                    preview_request
                    if isinstance(preview_request, Mapping)
                    else {
                        "derived_episode_ranges": [
                            dict(item)
                            for item in resolution.derived_episode_ranges
                            if isinstance(item, Mapping)
                        ],
                        "resolution_fingerprint": str(
                            resolution.resolution_fingerprint or ""
                        ),
                    }
                ),
                request_fingerprint=request_fingerprint,
                expected_revision=expected_revision,
                selected_subject_id=selected_subject_id,
                tmdb_tv_id=str(resolution.tmdb_tv_id or ""),
                season_assignments=dict(resolution.season_assignments),
                display_title=provider_title,
                title_source=title_selection,
                series_year=provider_year,
            )
            completed_plan = _with_task_episode_ownership(
                completed_plan,
                tv_planning_authority,
            )
            boundary_decisions = dict(
                self.task_decision.provider_subject_boundary_decisions
            )
            if same_work:
                if boundary_decision is None:
                    raise DecisionStateError("相邻作品决定不完整")
                cohort_edges = tuple(
                    item for item in boundary_decision.get(
                        "cohort_edges", ()
                    ) or () if isinstance(item, Mapping)
                )
                shared_decision = {
                    key: value
                    for key, value in boundary_decision.items()
                    if key not in {
                        "from_subject_id", "to_subject_id", "relation"
                    }
                }
                shared_decision.update({
                    "expected_decision_revision": expected_revision,
                    "request_fingerprint": request_fingerprint,
                })
                boundary_decisions = {}
                for edge in cohort_edges:
                    row = {
                        **shared_decision,
                        "from_subject_id": str(
                            edge.get("from_subject_id") or ""
                        ),
                        "to_subject_id": str(
                            edge.get("to_subject_id") or ""
                        ),
                        "relation": str(edge.get("relation") or "sequel"),
                    }
                    boundary_key = (
                        f"{row['from_subject_id']}->{row['to_subject_id']}"
                    )
                    boundary_decisions[boundary_key] = row
            changes = {
                "confirmed_work": staged.confirmed_work,
                "provider_evidence": staged.provider_evidence,
                "linked_provider_identities": (
                    staged.linked_provider_identities
                ),
                "provider_link_binding": staged.provider_link_binding,
                "series_binding": staged.series_binding,
                "season_binding": staged.season_binding,
                "library_domain": staged.library_domain,
                "layout_mode": staged.layout_mode,
                "catalog_link_intent": staged.catalog_link_intent,
                "provider_subject_boundary_decisions": boundary_decisions,
                "tv_planning_authority": tv_planning_authority,
            }

            def close_task(_decision, projected, current):
                active = current.get("selection_operation")
                if (
                    not isinstance(active, dict)
                    or active.get("state") not in {"failed", "awaiting_user"}
                    or active.get("expected_decision_revision")
                    != expected_revision
                    or str(active.get("evidence_fingerprint") or "")
                    != evidence_fingerprint
                ):
                    raise DecisionStateError("任务状态已经变化")
                active = dict(active)
                active.update({
                    "state": "succeeded",
                    "result_decision_revision": _decision.revision,
                    "failure_code": None,
                    "reason_codes": [],
                    "retryable": False,
                })
                active.pop("boundary_resolution_failure", None)
                current["selection_operation"] = active
                current.pop("provider_selection_failure", None)
                projected_verification = PlanVerifier().verify(projected)
                if projected_verification.issues:
                    logger.error(
                        "[作品边界确认] 投影仍被阻断："
                        + ",".join(
                            issue.code for issue in projected_verification.issues
                        )
                    )
                current.update({
                    "workflow_status": "need_confirm",
                    "error": None,
                    "completed_at": None,
                    "name": (
                        projected.work_identity.title
                        if projected.work_identity is not None
                        else current.get("name")
                    ),
                    "plan_status": build_plan_review_snapshot(
                        projected
                    ).plan_status,
                    "plan_entry_count": len(projected.entries),
                })
                return current

            self.task_decision, revised = self.decision_command_service.update(
                self.plan.task_id,
                expected_revision=expected_revision,
                changes=changes,
                planner=lambda _state: completed_plan,
                task_terminal=TaskTerminalMutation.prepare(
                    TASK_PATH, self.plan.task_id, close_task
                ),
            )
            if revised is None:
                raise DecisionStateError("未生成确认后的整理计划")
            self.task_data = get_task(self.plan.task_id)
        except (TVPlanningAuthorityError, TaskBatchScopeError, OSError) as exc:
            logger.exception(f"[TV规划权威] 提交前失败：{exc}")
            self._persist_tv_planning_authority_failure(
                expected_revision=expected_revision,
                evidence_fingerprint=evidence_fingerprint,
            )
            if self._reload_provider_subject_authority():
                self._render_reloaded_provider_subject_authority()
            notify("TV 规划依据未能安全保存，请重试或重新生成计划", type="negative")
            return
        except DecisionStateError as exc:
            logger.exception(f"[作品边界确认] Decision 提交失败：{exc}")
            if self._reload_provider_subject_authority():
                self._render_reloaded_provider_subject_authority()
            notify("页面内容已经更新，请按当前状态重新选择", type="warning")
            return
        except (PlanReviewError, PlanStoreError, TypeError, ValueError) as exc:
            logger.exception(f"[作品边界确认] Plan 提交失败：{exc}")
            if self._reload_provider_subject_authority():
                self._render_reloaded_provider_subject_authority()
            notify("所选边界未能安全保存，请检查当前状态后重试", type="negative")
            return
        self._adopt_revision(revised)
        notify(
            "已确认一起整理" if same_work else "已确认分开整理",
            type="positive",
        )

    def _remove_provider_failure_task(self) -> None:
        operation = self.task_data.get("selection_operation")
        if not isinstance(operation, Mapping) or operation.get("state") not in {
            "failed", "interrupted"
        }:
            notify("任务状态已经变化，请刷新后重试", type="warning")
            return
        task_id = self.plan.task_id if getattr(self, "plan", None) else str(
            self.task_data.get("uuid") or ""
        )
        try:
            task_queue.abandon_and_remove(
                task_id,
                record_root=RECORD_PATH,
                plan_root=PLAN_PATH,
                review_root=PLAN_REVIEW_PATH,
            )
        except (OSError, PlanExecutionError):
            notify("任务暂时无法移除，请稍后再试", type="negative")
            return
        self._close_review()
        try:
            from . import data_table_page

            data_table_page.refresh_dashboard()
            data_table_page._review_tasks.refresh()
        except (AttributeError, RuntimeError):
            pass
        notify("任务已移除；不会修改媒体文件或作品信息", type="positive")

    def _render_completion_workflow(self) -> None:
        self.completion_container.clear()
        with self.completion_container:
            mapping_source = str(self.plan.metadata.get("mapping_source") or "")
            issue_codes = {
                str(item.get("code") or "")
                for item in self.snapshot.issues
                if isinstance(item, Mapping)
            }
            primary_group = (
                self.plan.presentation_groups[0]
                if self.plan.presentation_groups else None
            )
            if (
                "subtitle_video_not_content_version" in issue_codes
                and mapping_source != "inventory_review_draft"
                and primary_group is not None
                and primary_group.kind == "movie_continuous"
            ):
                self._render_movie_primary_picker(
                    self._video_options(),
                    self._selected_movie_primary_sources(),
                )
            if mapping_source == "inventory_smart_completion":
                if self.snapshot.issues:
                    ui.label(
                        "系统已完成可确定的文件；下面只列出仍需你判断的项目。"
                    ).classes("bar-help text-orange-8")
                else:
                    with ui.row().classes(
                        "bar-callout bar-callout-success w-full items-start gap-2"
                    ):
                        ui.icon("task_alt", color="green-7", size="22px")
                        with ui.column().classes("gap-1"):
                            ui.label(
                                "整理计划已生成，尚未创建硬链接"
                            ).classes("font-bold text-green-8")
                            ui.label(
                                "请先核对下方源文件与建议目标，然后点击"
                                "“批准全部 · 不执行”；批准完成后，再点击红色"
                                "“执行已批准的整理计划”才会创建硬链接。"
                            ).classes("bar-help")
                            policy = self.plan.execution_policy
                            if (
                                policy is None
                                or policy.dry_run
                                or is_ui_test_mode()
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
                    layout_mode = (
                        self.task_decision.layout_mode
                        if self.task_decision is not None else None
                    )
                    selected_kind = (
                        "tv_episodes" if layout_mode == "tv_episodes"
                        else "movie_continuous" if layout_mode == "movie_versions"
                        else "movie_segments" if layout_mode == "movie_segments"
                        else None
                    )
                    self._render_completion_choice(
                        group,
                        selected_kind=selected_kind,
                    )
                return
            if mapping_source != "inventory_review_draft":
                return
            work = self.snapshot.work_identity
            confirmed = str(work.get("status")) == "confirmed"
            with ui.card().classes("bar-primary-workflow w-full"):
                ui.label("整理计划").classes("text-h6 font-bold")
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
                ui.label(
                    "这一步只生成新版整理计划，不会立即移动或创建文件。"
                ).classes("bar-help")

    def _render_library_choices(self) -> None:
        ui.label("保存位置").classes("text-subtitle2 font-bold")
        primary = (
            self.plan.work_identity.primary_provider
            if self.plan.work_identity is not None else None
        )
        movie = primary is not None and primary.entity_type == "movie"
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
                ).props(
                    ("color=blue-7 text-color=white no-caps"
                     if selected else "outline color=blue-7 no-caps ")
                    + f" aria-pressed={'true' if selected else 'false'}"
                ).classes("bar-completion-choice")
                if selected:
                    button.tooltip("已选择；选择整理方式后应用")

    def _video_options(self) -> Dict[str, str]:
        options: Dict[str, str] = {}
        for entry in self.plan.entries:
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
            confirm = ui.button("确定", icon="check").props(
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
            ui.label("整理方式").classes("text-subtitle2 font-bold")
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
            elif needs_season_choice:
                ui.label(f"自动识别：第 {season_value} 季").classes("bar-help")
            self._render_movie_primary_picker(video_options, selected_values)
            with ui.row().classes(
                "bar-completion-choice-row w-full gap-3 flex-wrap"
            ):
                tv_props = (
                    "color=blue-7 text-color=white no-caps"
                    if selected_kind == "tv_episodes" else "outline color=blue-7 no-caps"
                )
                movie_props = (
                    "color=blue-7 text-color=white no-caps"
                    if selected_kind == "movie_continuous" else "outline color=blue-7 no-caps"
                )
                ui.button(
                    "按季和集智能整理",
                    icon="video_library",
                    on_click=partial(
                        self._complete_inventory_draft,
                        group.group_id,
                        "tv_episodes",
                    ),
                ).props(tv_props).classes("bar-completion-choice")
                ui.button(
                    "按所选视频生成电影正片版本",
                    icon="movie",
                    on_click=partial(
                        self._complete_movie_with_primary,
                        group.group_id,
                    ),
                ).props(movie_props).classes("bar-completion-choice")
            ui.checkbox(
                "这些视频是同一电影的连续分段",
                value=selected_kind == "movie_segments",
                on_change=lambda event: self._toggle_movie_segments(
                    group.group_id,
                    bool(event.value),
                ),
            ).props("color=blue-7")

    def _toggle_movie_segments(self, group_id: str, enabled: bool) -> None:
        if enabled:
            self._complete_inventory_draft(group_id, "movie_continuous")
            return
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
        try:
            draft = self.review_service.reopen_inventory_draft(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                reason="用户取消连续分段整理方式",
                persist_revision=False,
            )
            self.task_decision, projected = self.decision_command_service.update(
                self.plan.task_id,
                expected_revision=self.task_decision.revision,
                changes={"layout_mode": None},
                planner=lambda _state: draft,
                task_terminal=_task_terminal_for_projected_plan(self.plan.task_id),
            )
            if projected is None:
                raise DecisionStateError("未生成 DecisionState 对应的 Plan")
            draft = projected
        except (DecisionStateError, PlanReviewError) as exc:
            notify(localize_plan_term(exc), type="warning")
            return
        self._adopt_revision(draft)
        notify("已取消连续分段整理方式；尚未执行文件操作。")

    def _inventory_draft_for_completion(self) -> RenamePlan:
        mapping_source = str(self.plan.metadata.get("mapping_source") or "")
        if mapping_source == "inventory_review_draft":
            return self.plan
        if mapping_source == "inventory_smart_completion":
            return self.review_service.reopen_inventory_draft(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                reason="用户重新选择整理方式",
                persist_revision=False,
            )
        raise PlanReviewError("当前整理计划不能重新选择整理方式")

    def _series_binding_for_tv(self, target_root: Path) -> dict:
        if self.task_decision is None or self.plan.work_identity is None:
            raise DecisionStateError("缺少剧集系列决策上下文")
        if self.task_decision.series_binding is not None:
            return dict(self.task_decision.series_binding)
        provider = self.plan.work_identity.primary_provider.to_dict()
        try:
            match = self.series_catalog.find_by_provider(provider)
        except SeriesCatalogError as exc:
            raise DecisionStateError(str(exc)) from exc
        if match is not None:
            return {
                "series_key": match.series_key,
                "canonical_root": str(match.canonical_root),
                "catalog_revision": match.revision,
                "decision_source": "exact_provider",
            }
        from ..rename.canonical_naming import (
            CanonicalNamingError,
            build_canonical_work_root,
            decision_from_work_identity,
            read_naming_decision,
        )

        try:
            raw = self.plan.metadata.get("canonical_naming_decision")
            naming_decision = (
                read_naming_decision(raw)
                if raw is not None
                else decision_from_work_identity(self.plan.work_identity)
            )
            canonical_root = build_canonical_work_root(
                resolved_target_root=target_root,
                decision=naming_decision,
                year=self.plan.work_identity.year,
            )
        except CanonicalNamingError as exc:
            raise DecisionStateError(str(exc)) from exc
        return {
            "series_key": f"series-{uuid.uuid4()}",
            "canonical_root": str(canonical_root),
            "catalog_revision": 0,
            "decision_source": "new_series_intent",
        }

    def _complete_movie_with_primary(self, group_id: str) -> None:
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
                    "未选视频进入版本级特典判断"
                ),
                primary_video_sources=tuple(
                    Path(str(value)) for value in selected_values
                ),
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
        self._adopt_revision(revised)
        notify("电影正片版本、特典和字幕已生成新版整理计划；尚未执行文件操作。")

    def _complete_inventory_draft(self, group_id: str, group_kind: str) -> None:
        from ..rename.target_routing import (
            resolve_target_root,
            snapshot_from_config_manager,
        )
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
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
                series_binding = self._series_binding_for_tv(target_root)
                decision_changes["series_binding"] = series_binding
                decision_changes["catalog_link_intent"] = None
                completion_kwargs.update({
                    "series_key": str(series_binding["series_key"]),
                    "season_binding_id": None,
                    "series_catalog_revision": int(
                        series_binding.get("catalog_revision") or 0
                    ),
                })
                selected_season = getattr(self, "tv_season_input", None)
                if selected_season is not None:
                    completion_kwargs["season_number"] = selected_season.value
                    completion_kwargs["season_source"] = "explicit_user"
                    decision_changes["season_binding"] = {
                        "season_binding_id": f"season-binding-{uuid.uuid4()}",
                        "season_number": int(selected_season.value),
                        "decision_source": "explicit_user",
                    }
                elif self.task_decision.season_binding is not None:
                    completion_kwargs["season_number"] = int(
                        self.task_decision.season_binding["season_number"]
                    )
                    completion_kwargs["season_source"] = str(
                        self.task_decision.season_binding.get("decision_source")
                        or "provider"
                    )
                else:
                    raise DecisionStateError("尚未确认季度，不能生成剧集 Plan")
                binding = decision_changes.get("season_binding") or self.task_decision.season_binding
                completion_kwargs["season_binding_id"] = str(
                    binding["season_binding_id"]
                )
            if group_kind == "movie_continuous":
                completion_kwargs["confirmed_segment_sources"] = tuple(
                    entry.source_path
                    for entry in draft.entries
                    if entry.media_kind == "video"
                )
            revised = self.review_service.complete_inventory_draft(
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
                _revision_parent_fingerprint=PlanStore.fingerprint(self.plan),
                **completion_kwargs,
            )
            if group_kind == "movie_continuous":
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
            self.task_decision, projected = self.decision_command_service.update(
                self.plan.task_id,
                expected_revision=self.task_decision.revision,
                changes=decision_changes,
                planner=lambda _state: revised,
                task_terminal=_task_terminal_for_projected_plan(self.plan.task_id),
            )
            if projected is None:
                raise DecisionStateError("未生成 DecisionState 对应的 Plan")
            revised = projected
        except (DecisionStateError, PlanReviewError, TypeError, ValueError) as exc:
            notify(localize_plan_term(exc), type="warning")
            return
        self._adopt_revision(revised)
        if self.snapshot.issues:
            notify(
                f"已完成可确定项目；仍有 {len(self.snapshot.issues)} 个结构问题需要确认",
                type="warning",
            )
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

    def _select_library_domain(self, domain: str) -> None:
        if domain not in {"animation", "general"}:
            return
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
        try:
            self.task_decision, projected = self.decision_command_service.update(
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
        self._adopt_revision(projected)
        notify(
            "保存位置已更新；请重新选择整理方式以生成对应目录，尚未执行文件操作。",
            type="warning",
        )

    def _change_group_kind(self, group_id: str, kind: str) -> None:
        try:
            revised = self.review_service.revise_presentation_group_kind(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                group_id=group_id,
                kind=kind,
                reason="用户确认本地文件的实际播放形式",
            )
        except PlanReviewError as exc:
            notify(localize_plan_term(exc))
            return
        self._adopt_revision(revised)
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
                    ui.button(
                        "选择这个作品",
                        on_click=partial(self._select_provider_candidate, candidate),
                        icon="check_circle",
                    ).props("color=blue-7 no-caps")
                    if url:
                        ui.link("打开资料页", url, new_tab=True).classes(
                            "bar-candidate-link"
                        )

    def _render_selected_provider_identity(self) -> None:
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
        ui.label("当前已选作品").classes("text-subtitle2")
        with ui.card().classes("bar-candidate-card w-full"):
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
                    ui.button(
                        "重新选择作品",
                        icon="swap_horiz",
                        on_click=self._show_provider_candidates,
                    ).props("outline color=blue-7 no-caps")
                    if url:
                        ui.link("打开资料页", url, new_tab=True).classes(
                            "bar-candidate-link"
                        )

    def _show_provider_candidates(self) -> None:
        self.provider_selection_expanded = True
        self._render_groups()

    def _render_provider_column(
        self,
        namespace: str,
        candidates: Sequence[Dict[str, object]],
    ) -> None:
        provider_label = "Bangumi" if namespace == "bangumi" else "TMDB"
        visible = provider_candidates_for_display(candidates, namespace)
        with ui.column().classes("bar-provider-column grow gap-3"):
            ui.label(f"{provider_label} 结果（最多 4 项）").classes(
                "text-subtitle2 font-bold"
            )
            manual_input = ui.input(
                label=f"粘贴 {provider_label} 作品详情页链接",
                placeholder=(
                    "https://bgm.tv/subject/123456"
                    if namespace == "bangumi"
                    else "https://www.themoviedb.org/movie/123456"
                ),
            ).props("outlined clearable").classes("w-full")
            if namespace == "bangumi":
                self.bangumi_manual_url_input = manual_input
            else:
                self.tmdb_manual_url_input = manual_input
            ui.button(
                "读取链接并选择此作品",
                icon="link",
                on_click=partial(self._select_manual_provider_url, namespace),
            ).props("outline color=blue-7 no-caps").classes("w-full")
            if not visible:
                ui.label(
                    "自动搜索没有可显示结果；仍可粘贴人工找到的详情页链接。"
                ).classes("bar-help text-grey-7")
            for candidate in visible:
                self._render_provider_candidate(candidate)

    async def _select_manual_provider_url(self, namespace: str) -> None:
        field = getattr(self, f"{namespace}_manual_url_input", None)
        value = str(field.value if field is not None else "").strip()
        if not value:
            notify("请先粘贴作品详情页链接", type="warning")
            return
        try:
            candidate = await asyncio.to_thread(
                resolve_provider_item_url,
                value,
                tmdb_api_key=effective_tmdb_api_key(cm),
                bangumi_token=cm.get_config("bangumi_token"),
                bangumi_user_agent=cm.get_config("bangumi_user_agent"),
            )
            if str(candidate.get("namespace")) != namespace:
                expected = "Bangumi" if namespace == "bangumi" else "TMDB"
                raise ProviderSearchError(f"请在 {expected} 栏粘贴对应网站的链接")
        except (ProviderSearchError, OSError, TypeError, ValueError) as exc:
            notify(str(exc), type="negative")
            return
        await self._select_provider_candidate(candidate)

    async def _select_provider_candidate(
        self, candidate: Dict[str, object]
    ) -> None:
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
        selection = {
            key: candidate.get(key)
            for key in (
                "namespace", "entity_type", "provider_id", "title",
                "original_title", "year", "poster_url", "source_url",
                "snapshot_at", "library_domain",
            )
        }
        try:
            await task_queue.enqueue_reidentify(
                self.plan.task_id,
                selection,
                self.task_decision.revision,
            )
        except PlanExecutionError as exc:
            notify(str(exc), type="warning")
            return
        self.task_data["provider_selection"] = selection
        self.task_data["selection_operation"] = {
            "state": "pending",
            "candidate": selection,
        }
        render_groups = getattr(self, "_render_groups", None)
        if callable(render_groups):
            render_groups()
        notify("正在更新作品信息", type="positive")
        waiter = getattr(task_queue, "wait_for_selection_result", None)
        if not callable(waiter):
            return
        operation = await waiter(self.plan.task_id)
        self.task_data = get_task(self.plan.task_id) or self.task_data
        if operation.get("state") == "succeeded":
            if not all(hasattr(self, name) for name in (
                "decision_state_store", "_adopt_revision",
                "_render_completion_workflow", "_render_issues", "_render_entries",
            )):
                return
            try:
                self.task_decision = self.decision_state_store.load(self.plan.task_id)
                self._adopt_revision(PlanStore(PLAN_PATH).load(self.plan.task_id))
                self._render_completion_workflow()
                self._render_issues()
                self._render_entries()
                notify("作品信息已更新，请继续检查", type="positive")
            except (DecisionStateError, PlanStoreError):
                notify("作品信息已更新，请刷新页面查看", type="warning")
            return
        if callable(render_groups):
            render_groups()

    def _render_issues(self) -> None:
        self.issues_container.clear()
        with self.issues_container:
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
            if str(self.plan.metadata.get("mapping_source") or "") == "inventory_review_draft":
                work = self.snapshot.work_identity
                entity_type = str(work.get("provider_entity_type") or "")
                with ui.column().classes("bar-issue-summary w-full gap-1"):
                    with ui.row().classes(
                        "bar-issue-heading w-full items-center gap-2 no-wrap"
                    ):
                        ui.icon("info", color="blue-7", size="24px")
                        ui.label(
                            "还差一步：选择电影正片"
                            if entity_type == "movie"
                            else (
                                "还差一步：确认按季和集整理"
                                if entity_type == "tv"
                                else "还差一步：确认作品和本地播放方式"
                            )
                        ).classes("font-bold text-blue-8")
                    ui.label(
                        "请使用上方“整理计划”中的正片选择入口。完成前显示的"
                        "目标越界、字幕未关联等都是占位草案的派生状态，不需要逐条处理。"
                    ).classes("bar-issue-detail")
                return
            summaries = summarize_plan_issues(
                self.snapshot.issues, self.snapshot.rows
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
                        ui.icon("warning", color="red-7", size="20px").classes(
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
                    if summary["code"] == "presentation_asset_owner_unresolved":
                        self._render_asset_owner_repair()
                    elif summary["code"] in {
                        "presentation_asset_role_unknown",
                        "episode_assignment_conflict",
                    }:
                        self._render_asset_role_repair()
                    elif summary["code"] == "provider_special_decision_required":
                        self._render_provider_special_repair()
                    elif summary["code"] == "source_episode_projection_decision_required":
                        self._render_source_episode_projection_repair()

    def _render_source_episode_projection_repair(self) -> None:
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
                ).props("outline color=blue-7 no-caps")

    def _apply_source_episode_projection_inline(
        self,
        projection: Mapping[str, object],
        label: str,
        expected_revision: Optional[int],
    ) -> None:
        if self.task_decision is None:
            notify("此旧任务需要重新识别后再确认集号", type="warning")
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
            projected_plan = apply_source_episode_projection(
                self.plan, plain_projection
            )
            self.task_decision, revised = self.decision_command_service.update(
                self.plan.task_id,
                expected_revision=expected_revision,
                changes={
                    "source_episode_projection": plain_projection,
                    "source_episode_projection_decision": {
                        "decision": str(projection.get("mode") or ""),
                        "label": label,
                        "projection_fingerprint": str(
                            plain_projection.get("projection_fingerprint") or ""
                        ),
                    },
                },
                planner=lambda _state: projected_plan,
                task_terminal=_task_terminal_for_projected_plan(self.plan.task_id),
            )
            if revised is None:
                raise DecisionStateError("未生成集号确认后的整理计划")
        except (DecisionStateError, PlanStoreError, TypeError, ValueError):
            notify("页面内容已经更新，请刷新后重新选择", type="warning")
            return
        self._adopt_revision(revised)
        notify(f"已选择：{label}", type="positive")

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
                icon="save",
                on_click=lambda: self._save_asset_owner_inline(
                    file_select.value, owner_select.value
                ),
            ).props("outline color=red-7")
            ui.button(
                "本次不整理",
                icon="do_not_disturb",
                on_click=lambda: self._ignore_entry_inline(file_select.value),
            ).props("flat color=grey-8")

    def _render_asset_role_repair(self) -> None:
        """Keep the common unknown-video repair beside its blocking issue."""

        affected = {}
        for entry in self.plan.entries:
            identifier = entry_id(entry)
            row = next(
                (item for item in self.snapshot.rows if item["entry_id"] == identifier),
                None,
            )
            if row and set(row.get("issue_codes", ())).intersection({
                "presentation_asset_role_unknown", "episode_assignment_conflict",
            }):
                affected[identifier] = _file_name(entry.source_path)
        if not affected:
            return
        ui.label("直接确认用途后，系统会从原始库存草案完整重建正片、特典、字幕和目标。") \
            .classes("bar-help")
        with ui.row().classes("w-full items-end gap-2 flex-wrap"):
            file_select = ui.select(affected, label="待确认文件").props(
                "outlined dense options-dense"
            ).classes("grow")
            ui.button(
                "设为特典并重建",
                icon="movie_filter",
                on_click=lambda: self._rebuild_inventory_role(
                    file_select.value, "extra"
                ),
            ).props("outline color=blue-7")
            ui.button(
                "作为正片候选并重建",
                icon="video_library",
                on_click=lambda: self._rebuild_inventory_role(
                    file_select.value, "main_candidate"
                ),
            ).props("outline color=blue-7")
            eligible_archives = {
                identifier for identifier in affected
                if next(
                    (
                        entry.source_path.suffix.casefold() in {".7z", ".zip"}
                        for entry in self.plan.entries
                        if entry_id(entry) == identifier
                    ),
                    False,
                )
            }
            if eligible_archives:
                ui.label("保留原文件，不会解包").classes("bar-help")
                ui.button(
                    "设为字体包",
                    icon="font_download",
                    on_click=lambda: self._confirm_font_inline(
                        file_select.value
                    ),
                ).props("outline color=blue-7")

    def _confirm_font_inline(self, identifier: object) -> None:
        if not identifier:
            notify("请先选择待确认文件", type="warning")
            return
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
        try:
            revised = self.review_service.confirm_font_archive(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                str(identifier),
                reason="用户确认该压缩包是字体包；保留原文件，不解包",
                persist_revision=False,
            )
            roles = dict(self.task_decision.manual_role_decisions)
            roles[str(identifier)] = "font"
            self.task_decision, projected = self.decision_command_service.update(
                self.plan.task_id,
                expected_revision=self.task_decision.revision,
                changes={"manual_role_decisions": roles},
                planner=lambda _state: revised,
                task_terminal=_task_terminal_for_projected_plan(self.plan.task_id),
            )
            if projected is None:
                raise DecisionStateError("未生成字体确认后的整理计划")
        except (DecisionStateError, PlanReviewError, TypeError, ValueError) as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        self._adopt_revision(projected)
        notify("已设为字体包；原文件保持不变", type="positive")

    def _render_provider_special_repair(self) -> None:
        candidates = self.plan.metadata.get("provider_special_candidates")
        if not isinstance(candidates, Mapping):
            return
        affected = {}
        for entry in self.plan.entries:
            identifier = entry_id(entry)
            row = next(
                (item for item in self.snapshot.rows if item["entry_id"] == identifier),
                None,
            )
            if (
                row
                and "provider_special_decision_required" in row.get("issue_codes", ())
                and isinstance(candidates.get(identifier), (list, tuple))
                and candidates[identifier]
            ):
                affected[identifier] = _file_name(entry.source_path)
        if not affected:
            return
        selected = next(iter(affected)) if len(affected) == 1 else None
        ui.label("官方特别篇候选").classes("bar-help font-bold")
        sample_identifier = selected or next(iter(affected))
        sample_rows = candidates.get(sample_identifier) or ()
        episode_options = {}
        for item in sample_rows:
            if not isinstance(item, Mapping):
                continue
            episode_number = item.get("episode_number")
            episode_key = str(item.get("provider_episode_id") or "").strip()
            if not episode_key or not isinstance(episode_number, int):
                continue
            title = str(item.get("title") or "标题未提供")
            air_date = str(item.get("air_date") or "日期未提供")
            runtime = item.get("runtime")
            runtime_text = (
                f"{runtime} 分钟"
                if isinstance(runtime, int) and not isinstance(runtime, bool)
                else "时长未提供"
            )
            episode_options[episode_key] = (
                f"S00E{episode_number:02d} · {title} · {air_date} · {runtime_text}"
            )
        if not episode_options:
            return
        selected_episode = (
            next(iter(episode_options)) if len(episode_options) == 1 else None
        )
        with ui.row().classes("w-full items-end gap-2 flex-wrap"):
            file_select = ui.select(
                affected, label="待确认文件", value=selected
            ).props("outlined dense options-dense").classes("grow")
            episode_select = ui.select(
                episode_options,
                label="选择特别篇",
                value=selected_episode,
            ).props("outlined dense options-dense").classes("grow")
            ui.button(
                "归入特别篇",
                icon="playlist_add_check",
                on_click=lambda: self._apply_provider_special_inline(
                    file_select.value,
                    "confirmed_official_special",
                    episode_select.value,
                ),
            ).props("outline color=blue-7")
            ui.button(
                "保留为附加内容",
                icon="movie_filter",
                on_click=lambda: self._apply_provider_special_inline(
                    file_select.value, "unmatched_extra", None
                ),
            ).props("flat color=grey-8")

    def _apply_provider_special_inline(
        self, identifier: object, action: str, provider_episode_id: object
    ) -> None:
        if not identifier:
            notify("请先选择待确认文件", type="warning")
            return
        if self.task_decision is None:
            notify("此旧任务尚无权威决策状态，请先重新识别", type="warning")
            return
        candidates = self.plan.metadata.get("provider_special_candidates")
        rows = (
            candidates.get(str(identifier))
            if isinstance(candidates, Mapping) else None
        )
        if not isinstance(rows, (list, tuple)) or not rows:
            notify("官方特别篇候选已变化，请刷新后重试", type="warning")
            return
        if action == "confirmed_official_special":
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
        try:
            revised = self.review_service.apply_provider_special_decision(
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
            self.task_decision, projected = self.decision_command_service.update(
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
        self._adopt_revision(projected)
        notify(
            "已归入特别篇" if action == "confirmed_official_special"
            else "已保留为附加内容",
            type="positive",
        )

    def _rebuild_inventory_role(self, identifier: object, role: str) -> None:
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
            if not self.plan.presentation_groups or self.plan.presentation_groups[0].kind != "tv_episodes":
                raise PlanReviewError("当前用途修正只适用于按季和集整理")
            revised = self.review_service.recomplete_inventory_with_role_decisions(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                decisions={str(identifier): role},
                reason="用户在问题卡中确认文件用途并要求完整重建",
                persist_revision=False,
            )
            roles = dict(self.task_decision.manual_role_decisions)
            roles[str(identifier)] = role
            self.task_decision, projected = self.decision_command_service.update(
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
        self._adopt_revision(revised)
        notify("用途已确认；正片、特典、字幕和目标已完整重建，尚未执行文件操作。")

    def _save_asset_owner_inline(self, identifier: object, owner: object) -> None:
        if not identifier or not owner:
            notify("请先选择待修正文件和归属版本", type="warning")
            return
        matching = [entry for entry in self.plan.entries if entry_id(entry) == identifier]
        if len(matching) != 1 or matching[0].presentation_asset is None:
            notify("找不到对应的呈现资源", type="negative")
            return
        asset = matching[0].presentation_asset
        try:
            revised = self.review_service.revise_presentation_asset(
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
        self._adopt_revision(revised)
        notify("资源归属已保存为新版整理计划；尚未执行文件操作", type="positive")

    def _ignore_entry_inline(self, identifier: object) -> None:
        if not identifier:
            notify("请先选择待修正文件", type="warning")
            return
        self.selected_ids = {str(identifier)}
        self._ignore_selected_entry()

    def _render_toolbar(self) -> None:
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            ui.input(
                "搜索文件名或路径",
                on_change=lambda event: self._set_query(event.value),
            ).props("clearable dense outlined").classes("grow")
            ui.toggle(
                {"all": "全部文件", "issues": "仅问题文件"},
                value="all",
                on_change=lambda event: self._set_view_mode(event.value),
            ).props(f"dense {SECONDARY_TOGGLE_PROPS}").classes(
                "bar-equal-filter-toggle"
            )
            self.selection_label = ui.label("已选择 0 项").classes("bar-help")

    def _render_entries(self) -> None:
        self.entries_container.clear()
        approved = set(self.decision.approved_entry_ids)
        rows = filter_plan_rows(
            self.snapshot.rows,
            self.query,
            self.review_only,
            subtitle_only=self.subtitle_only,
            unresolved_only=self.unresolved_only,
            conflict_only=self.conflict_only,
            unknown_only=self.unknown_only,
            language=self.language_filter,
            flag=self.flag_filter,
        )
        if self.view_mode == "issues":
            rows = [row for row in rows if row.get("execution_blocked")]
        page_count = max(1, (len(rows) + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
        self.page = min(max(1, self.page), page_count)
        start = (self.page - 1) * REVIEW_PAGE_SIZE
        visible_rows = rows[start:start + REVIEW_PAGE_SIZE]
        with self.entries_container:
            if not rows:
                with ui.column().classes("bar-empty-state w-full items-center"):
                    ui.icon("search_off", size="38px").classes("bar-muted")
                    ui.label("没有符合当前筛选条件的条目")
                return
            with ui.row().classes("w-full items-center justify-between gap-3"):
                ui.label(
                    f"共 {len(rows)} 个文件；当前显示 {start + 1}–"
                    f"{start + len(visible_rows)}"
                ).classes("bar-help")
                if page_count > 1:
                    with ui.row().classes("items-center gap-1"):
                        previous = ui.button(
                            icon="chevron_left",
                            on_click=lambda: self._change_page(-1),
                        ).props("flat dense round")
                        if self.page == 1:
                            previous.disable()
                        ui.label(f"{self.page} / {page_count}").classes("bar-help")
                        following = ui.button(
                            icon="chevron_right",
                            on_click=lambda: self._change_page(1),
                        ).props("flat dense round")
                        if self.page == page_count:
                            following.disable()
            for row in visible_rows:
                self._render_entry(row, str(row["entry_id"]) in approved)

    def _render_entry(self, row: Dict[str, object], approved: bool) -> None:
        identifier = str(row["entry_id"])
        is_inventory_draft = (
            str(self.plan.metadata.get("mapping_source") or "")
            == "inventory_review_draft"
        )
        review_class = (
            " bar-review-entry-review"
            if row.get("requires_review") and not is_inventory_draft else ""
        )
        if row.get("execution_blocked") and not is_inventory_draft:
            review_class += " bar-review-entry-blocked"
        with ui.card().classes(
            "bar-review-entry w-full" + review_class
        ):
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                checkbox = ui.checkbox(
                    value=identifier in self.selected_ids,
                    on_change=lambda event, eid=identifier: self._toggle_selected(
                        eid, event.value
                    ),
                ).props("color=red-6")
                if is_inventory_draft:
                    checkbox.disable()
                    checkbox.tooltip("请先使用上方“整理计划”，无需逐文件处理占位草案")
                elif row.get("execution_blocked"):
                    checkbox.tooltip("可选中后结构化修正；普通批准不能越过此阻断")
                with ui.column().classes("grow min-w-0 gap-2"):
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.label(f"#{row['id']}").classes("font-bold")
                        ui.label(_file_name(row["source"])).classes(
                            "bar-review-file-name"
                        )
                        ui.badge(
                            MEDIA_LABELS.get(
                                str(row.get("media_kind")),
                                str(row.get("media_kind")),
                            ),
                            color="blue-7",
                        ).props("outline")
                        ui.badge(
                            ROLE_LABELS.get(
                                str(row.get("role")), str(row.get("role"))
                            ),
                            color="grey-7",
                        ).props("outline")
                        if row.get("season") is not None:
                            ui.label(f"第 {row['season']} 季").classes("bar-meta-chip")
                        if row.get("episode") is not None:
                            ui.label(f"第 {row['episode']} 集").classes("bar-meta-chip")
                        if is_inventory_draft:
                            ui.badge("等待整包完成", color="blue-7")
                        elif row.get("requires_review"):
                            ui.badge("需要确认", color="orange-8")
                        if row.get("execution_blocked") and not is_inventory_draft:
                            ui.badge("阻断执行", color="red-7")
                        if approved:
                            ui.badge("已批准 · 未执行", color="green-7")
                        if row.get("presentation_group"):
                            ui.badge(
                                str(
                                    row.get("presentation_group_label")
                                    or row["presentation_group"]
                                ),
                                color="purple-7",
                            ).props("outline")
                        if row.get("asset_role"):
                            ui.badge(
                                ROLE_LABELS.get(
                                    str(row["asset_role"]), str(row["asset_role"])
                                ),
                                color="teal-7",
                            ).props("outline")
                    if row.get("issue_codes") and not is_inventory_draft:
                        ui.label(
                            "此文件：" + "、".join(row["issue_codes"])
                        ).classes("bar-review-file-issue")
                    with ui.expansion("查看路径与识别详情", icon="expand_more").classes(
                        "bar-entry-detail w-full"
                    ):
                        with ui.column().classes("w-full gap-2"):
                            ui.label("源文件").classes("bar-field-label")
                            ui.label(str(row["source"])).classes("bar-path-box w-full")
                            ui.label("建议目标").classes("bar-field-label")
                            ui.label(
                                "完成整包后生成"
                                if is_inventory_draft else str(row["target"])
                            ).classes("bar-path-box w-full")
                            if row.get("evidence"):
                                ui.label(
                                    "判断依据：" + str(row["evidence"])
                                ).classes("bar-evidence")
                            if row.get("version_ref"):
                                ui.label(
                                    f"版本：{row.get('version_label') or '默认单版本'} · "
                                    f"{row.get('version_ref')}"
                                ).classes("bar-evidence font-mono")
                            if row.get("media_kind") == "subtitle" and not is_inventory_draft:
                                with ui.column().classes("bar-subtitle-contract w-full gap-1"):
                                    ui.label(
                                        f"原始标签：{row.get('raw_label') or '空'} · "
                                        f"关联：{row.get('association_state')} · "
                                        f"解析：{row.get('parse_state')}"
                                    ).classes("bar-evidence")
                                    ui.label(
                                        "语言："
                                        f"{row.get('primary_language') or '未定'}"
                                        f"-{row.get('script') or '?'} · "
                                        f"附加：{', '.join(row.get('additional_languages', ())) or '无'} · "
                                        f"flags：{', '.join(row.get('flags', ())) or '无'}"
                                    ).classes("bar-evidence")
                                    if row.get("unknown_tokens"):
                                        ui.label(
                                            "未知 token：" + ", ".join(row["unknown_tokens"])
                                        ).classes("bar-error-text")

    def _render_presentation_override(self) -> None:
        with ui.expansion(
            "呈现分组与版本 · 结构化修正", icon="movie_filter"
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
                    icon="create_new_folder",
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
                    "单版本默认项可把下面四项全部留空；非默认版本必须完整填写。"
                ).classes("bar-help")
                ui.label(
                    "电影连续版的正片目标文件名由作品目录和版本信息自动生成。"
                    "保存这里的版本决定时，会替换此前填写的手工目标路径；"
                    "要区分电影版本，请修改下面的版本信息。"
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
                self.presentation_reason = ui.input(
                    "人工确认理由（必填）"
                ).props("outlined dense").classes("w-full")
                with ui.row().classes("w-full gap-3 flex-wrap"):
                    ui.button(
                        "保存呈现资产决定",
                        on_click=self._save_presentation_asset,
                        icon="save_as",
                    ).props("outline color=purple-7").classes("grow")
                    ui.button(
                        "本次不整理所选文件",
                        on_click=self._ignore_selected_entry,
                        icon="do_not_disturb",
                    ).props("outline color=grey-7").classes("grow")

    def _save_presentation_group(self) -> None:
        year_value = self.group_provider_year_input.value
        try:
            provider_year = int(year_value) if year_value not in (None, "") else None
            revised = self.review_service.upsert_presentation_group(
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
        self._adopt_revision(revised)
        notify("呈现分组已保存为新版整理计划；重新打开整理任务可刷新分组选项。")

    def _save_presentation_asset(self) -> None:
        if len(self.selected_ids) != 1:
            notify("呈现资产决定必须且只能选择一条记录")
            return
        selected_id = next(iter(self.selected_ids))
        try:
            revised = self.review_service.revise_presentation_asset(
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
        self._adopt_revision(revised)
        notify(
            "已保存新版整理计划并清空旧批准；结构仍有阻断时可继续逐项修正。"
        )

    def _ignore_selected_entry(self) -> None:
        if len(self.selected_ids) != 1:
            notify("不整理决定必须且只能选择一条记录")
            return
        try:
            revised = self.review_service.ignore_plan_entry(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                next(iter(self.selected_ids)),
                reason=self.presentation_reason.value,
            )
        except (PlanReviewError, ValueError) as exc:
            notify(localize_plan_term(exc))
            return
        self._adopt_revision(revised)
        notify("所选源文件保持不变，本次整理计划将不处理它。")

    def _render_override(self) -> None:
        with ui.expansion(
            f"人工覆盖目标 · 生成新版{RENAME_PLAN_TERM}，不执行",
            icon="edit_note",
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
                    icon="save_as",
                ).props("outline color=blue-7")

    def _render_subtitle_override(self) -> None:
        with ui.expansion(
            "字幕结构化决定 · 自动生成唯一目标", icon="subtitles"
        ).classes("w-full"):
            with ui.column().classes("w-full gap-3"):
                ui.label(
                    "选择一条字幕记录。这里只修改语言、标题、flags 和未知 token 处置，"
                    "父目录、视频 stem 与字幕格式由服务端锁定。"
                ).classes("bar-help")
                scope_options = {"file": "仅本文件"}
                if self.plan.series_scope is not None:
                    scope_options["series"] = (
                        f"当前系列（仅当前{RENAME_PLAN_TERM}精确匹配）"
                    )
                self.subtitle_rule_scope = ui.select(
                    scope_options,
                    value="file",
                    label="规则作用域",
                ).props("outlined dense options-dense").classes("w-full")
                if self.plan.series_scope is None:
                    ui.label(
                        f"当前{RENAME_PLAN_TERM}缺少稳定 TMDB TV 身份，"
                        "因此禁用“当前系列”；"
                        "不会用目录名或作品名猜测系列。"
                    ).classes("bar-help text-orange-8")
                else:
                    identity = self.plan.series_scope
                    ui.label(
                        f"系列身份：{identity.provider_namespace.upper()} "
                        f"{identity.provider_media_type.upper()} #{identity.provider_id}；"
                        f"系列规则只作用于当前{RENAME_PLAN_TERM}，"
                        "不会用于未来任务。"
                    ).classes("bar-help")
                video_options = {
                    str(entry.source_path): str(entry.source_path)
                    for entry in self.plan.entries if entry.media_kind == "video"
                }
                self.subtitle_video = ui.select(
                    video_options, label="关联视频源文件"
                ).props("outlined dense options-dense").classes("w-full")
                self.subtitle_rule_scope.on_value_change(
                    lambda event: self._subtitle_scope_changed(event.value)
                )
                with ui.row().classes("w-full gap-3 flex-wrap"):
                    self.subtitle_language = ui.select(
                        {"zh": "中文", "ja": "日语", "en": "英语", "ko": "韩语", "hi": "印地语"},
                        label="主语言",
                    ).props("outlined dense").classes("w-40")
                    self.subtitle_script = ui.select(
                        {None: "不适用", "Hans": "简体 Hans", "Hant": "繁体 Hant"},
                        value=None,
                        label="文字体系",
                    ).props("outlined dense").classes("w-40")
                    self.subtitle_region = ui.select(
                        {None: "不指定", "CN": "CN", "TW": "TW", "HK": "HK", "JP": "JP"},
                        value=None,
                        label="地区提示",
                    ).props("outlined dense").classes("w-40")
                self.subtitle_additional = ui.input(
                    "附加语言（逗号分隔，如 ja,en）"
                ).props("outlined dense").classes("w-full")
                self.subtitle_titles = ui.input(
                    "轨道标题 token（逗号分隔，顺序保留）"
                ).props("outlined dense").classes("w-full")
                self.subtitle_flags = ui.select(
                    ["default", "forced", "sdh"],
                    multiple=True,
                    label="flags（输出固定排序）",
                ).props("outlined dense use-chips").classes("w-full")
                self.subtitle_preserve_unknown = ui.input(
                    "保留为标题的未知 token（逗号分隔）"
                ).props("outlined dense").classes("w-full")
                self.subtitle_discard_unknown = ui.input(
                    "明确删除的未知 token（逗号分隔）"
                ).props("outlined dense").classes("w-full")
                self.subtitle_reason = ui.input("人工决定理由（必填）").props(
                    "outlined dense"
                ).classes("w-full")
                ui.label(
                    "保存会生成新 fingerprint、清空旧批准并重新运行 Verifier；"
                    "任何未知 token 未处置都会拒绝保存。"
                ).classes("bar-help text-orange-8")
                ui.button(
                    "保存字幕结构化决定",
                    on_click=self._override_subtitle_selected,
                    icon="save_as",
                ).props("outline color=blue-7")
                self.subtitle_rule_audit_container = ui.column().classes(
                    "w-full gap-2"
                )
                self._render_subtitle_rule_audit()

    def _subtitle_scope_changed(self, value: object) -> None:
        series = str(value) == SubtitleOverrideScope.SERIES.value
        self.subtitle_video.disable() if series else self.subtitle_video.enable()
        if series:
            self.subtitle_video.tooltip(
                "系列规则不会传播关联视频；每条字幕保留自己的已确认视频"
            )

    def _render_subtitle_rule_audit(self) -> None:
        container = getattr(self, "subtitle_rule_audit_container", None)
        if container is None:
            return
        container.clear()
        with container:
            if not self.plan.subtitle_override_rules:
                return
            ui.separator()
            ui.label(f"已应用字幕规则（撤销也会生成新版{RENAME_PLAN_TERM}）").classes(
                "text-subtitle2"
            )
            self.subtitle_revert_reason = ui.input(
                "撤销理由（必填）"
            ).props("outlined dense").classes("w-full")
            latest_id = self.plan.subtitle_override_rules[-1].rule_id
            for rule in reversed(self.plan.subtitle_override_rules):
                with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                    ui.label(
                        f"{rule.scope.value} · {rule.rule_fingerprint[:12]}… · "
                        f"engine {rule.engine_version} · {rule.reason}"
                    ).classes("bar-help font-mono")
                    button = ui.button(
                        "撤销" if rule.rule_id == latest_id else "历史规则",
                        on_click=partial(self._revert_subtitle_rule, rule.rule_id),
                        icon="undo" if rule.rule_id == latest_id else "history",
                    ).props("flat dense color=orange-8")
                    if rule.rule_id != latest_id:
                        button.disable()

    def _render_actions(self) -> None:
        with ui.row().classes(
            "bar-dialog-actions w-full items-center justify-between gap-3"
        ):
            ui.button(
                "导出 JSON", on_click=self._download, icon="download"
            ).props("flat color=grey-8")
            with ui.row().classes("gap-2 flex-wrap justify-end"):
                self.approve_selected_button = ui.button(
                    "批准所选 · 不执行",
                    on_click=self._approve_selected,
                    icon="done_all",
                ).props("outline color=orange-8")
                self.approve_all_button = ui.button(
                    "批准全部 · 不执行",
                    on_click=self._approve_all,
                    icon="verified",
                ).props("color=green-7")
                if not can_approve_plan(self.snapshot.plan_status):
                    self.approve_selected_button.disable()
                    self.approve_all_button.disable()
                self.execute_button = ui.button(
                    f"执行已批准的{RENAME_PLAN_TERM}",
                    on_click=self._confirm_execute,
                    icon="play_arrow",
                ).props("color=red-7")
                if not can_execute_approved_plan(
                    self.decision.status,
                    str(self.task_data.get("workflow_status")),
                    test_mode=is_ui_test_mode(),
                    preview_only=bool(self.task_data.get("preview_only")),
                ):
                    self.execute_button.disable()
                if is_ui_test_mode():
                    self.execute_button.tooltip("GUI 测试模式禁止文件执行")
                ui.button("关闭", on_click=self._close_review, icon="close").props(
                    "flat color=grey-8"
                )

    def _refresh_execute_button(self) -> None:
        enabled = can_execute_approved_plan(
            self.decision.status,
            str(self.task_data.get("workflow_status")),
            test_mode=is_ui_test_mode(),
            preview_only=bool(self.task_data.get("preview_only")),
        )
        self.execute_button.enable() if enabled else self.execute_button.disable()

    def _confirm_execute(self) -> None:
        if not can_execute_approved_plan(
            self.decision.status,
            str(self.task_data.get("workflow_status")),
            test_mode=is_ui_test_mode(),
            preview_only=bool(self.task_data.get("preview_only")),
        ):
            notify("只有当前版本全部批准后才能执行", type="warning")
            return
        fingerprint = PlanStore.fingerprint(self.plan)
        with ui.dialog() as confirm, ui.card().classes("bar-dialog max-w-lg"):
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
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=confirm.close).props("flat")
                ui.button(
                    "确认入队执行",
                    on_click=partial(
                        self._queue_execution, confirm, fingerprint
                    ),
                    icon="play_arrow",
                ).props("color=red-7")
        confirm.open()

    async def _queue_execution(self, confirm: ui.dialog, fingerprint: str) -> None:
        confirm.close()
        try:
            await task_queue.enqueue_approved(self.plan.task_id, fingerprint)
        except PlanExecutionError as exc:
            notify(localize_plan_term(exc), type="negative")
            return
        self.task_data = get_task(self.plan.task_id)
        self._refresh_execute_button()
        notify(
            f"已批准的{RENAME_PLAN_TERM} {fingerprint[:12]} 已加入执行队列",
            type="positive",
        )
        self._close_review()

    def _close_review(self) -> None:
        self.exit_requested = True
        self.close()

    def _set_query(self, value: str) -> None:
        self.query = value or ""
        self.page = 1
        self._render_entries()

    def _set_view_mode(self, value: object) -> None:
        self.view_mode = "issues" if str(value) == "issues" else "all"
        self.page = 1
        self._render_entries()

    def _change_page(self, delta: int) -> None:
        self.page = max(1, self.page + int(delta))
        self._render_entries()

    def _set_review_only(self, value: bool) -> None:
        self.review_only = bool(value)
        self._render_entries()

    def _set_filter(self, name: str, value: object) -> None:
        setattr(self, name, value or (False if name.endswith("_only") else ""))
        self._render_entries()

    def _toggle_selected(self, identifier: str, selected: bool) -> None:
        if selected:
            self.selected_ids.add(identifier)
        else:
            self.selected_ids.discard(identifier)
        self.selection_label.text = f"已选择 {len(self.selected_ids)} 项"

    def _download(self) -> None:
        ui.download.content(
            export_plan_json(self.plan),
            filename=f"{self.plan.task_id}.plan.json",
            media_type="application/json",
        )

    def _approve_selected(self) -> None:
        if not self.selected_ids:
            notify("请先选择要批准的条目")
            return
        self._approve(tuple(self.selected_ids))

    def _approve_all(self) -> None:
        self._approve(
            tuple(str(row["entry_id"]) for row in self.snapshot.rows)
        )

    def _approve(self, selected_ids: Tuple[str, ...]) -> None:
        fingerprint = PlanStore.fingerprint(self.plan)
        dialog_disposition = self.task_data.get(
            "execution_recovery_disposition"
        )
        dialog_workflow = self.task_data.get("workflow_status")
        current_task = get_task(self.plan.task_id)
        current_disposition = current_task.get(
            "execution_recovery_disposition"
        )
        reapproval_context = dialog_disposition == "reapproval_required"
        if (
            not current_task
            or current_disposition != dialog_disposition
            or current_task.get("workflow_status") != dialog_workflow
            or (
                reapproval_context
                and (
                    not requires_execution_reapproval(current_task)
                    or current_task.get("execution_requested_fingerprint")
                    != fingerprint
                )
            )
        ):
            notify("任务状态或整理版本已变化，请刷新后重新审核")
            return
        self.task_data = current_task
        try:
            self.decision = self.review_service.approve_entries(
                self.plan.task_id,
                fingerprint,
                selected_ids,
            )
        except PlanReviewError as exc:
            notify(localize_plan_term(exc))
            return
        self.review_badge.text = REVIEW_STATUS_LABELS[self.decision.status]
        self._refresh_plan_header()
        self._render_entries()
        if self.decision.status == ReviewStatus.APPROVED:
            def approve_current(payload: dict) -> dict:
                if (
                    payload.get("workflow_status") != dialog_workflow
                    or payload.get("execution_recovery_disposition")
                    != dialog_disposition
                ):
                    raise ValueError("任务状态已变化")
                if reapproval_context and (
                    not requires_execution_reapproval(payload)
                    or payload.get("execution_requested_fingerprint")
                    != fingerprint
                ):
                    raise ValueError("整理版本或核查状态已变化")
                payload["workflow_status"] = "approved"
                if reapproval_context:
                    payload.update(
                        {
                            "execution_reapproval_fingerprint": fingerprint,
                            "execution_recovery_disposition": None,
                            "execution_requires_manual_check": False,
                        }
                    )
                return payload

            try:
                updated = mutate_task(self.plan.task_id, approve_current)
            except (OSError, RuntimeError, TypeError, ValueError):
                notify("任务状态或整理版本已变化，请刷新后重新审核")
                self.task_data = get_task(self.plan.task_id)
                self._refresh_execute_button()
                return
            if updated is None:
                notify("任务状态或整理版本已变化，请刷新后重新审核")
                self.task_data = get_task(self.plan.task_id)
                self._refresh_execute_button()
                return
            self.task_data = updated
            notify(f"当前{RENAME_PLAN_TERM}已全部批准；尚未执行任何文件操作")
        else:
            notify(
                f"已记录 {len(self.decision.approved_entry_ids)} 条批准；"
                "尚未执行任何文件操作"
            )
        self._refresh_execute_button()

    def _adopt_revision(self, revised: RenamePlan) -> None:
        self.plan = revised
        self.snapshot = build_plan_review_snapshot(revised)
        self.decision = self.review_service.load_or_create(revised)
        self.task_data = get_task(self.plan.task_id) or self.task_data
        self.selected_ids.clear()
        self.selection_label.text = "已选择 0 项"
        self.review_badge.text = REVIEW_STATUS_LABELS[self.decision.status]
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

    def _override_selected(self) -> None:
        if len(self.selected_ids) != 1:
            notify("人工覆盖必须且只能选择一条记录")
            return
        selected_id = next(iter(self.selected_ids))
        try:
            revised = self.review_service.override_target(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                selected_id,
                self.override_target.value,
                self.override_reason.value,
            )
        except PlanReviewError as exc:
            notify(localize_plan_term(exc))
            return
        self.plan = revised
        self.snapshot = build_plan_review_snapshot(revised)
        self.decision = self.review_service.load_or_create(revised)
        self.task_data = get_task(self.plan.task_id) or self.task_data
        self.review_badge.text = REVIEW_STATUS_LABELS[self.decision.status]
        self._refresh_plan_header()
        self.override_target.value = ""
        self.override_reason.value = ""
        self.selected_ids.clear()
        self.selection_label.text = "已选择 0 项"
        self._render_entries()
        self._refresh_execute_button()
        notify(
            f"已保存新版{RENAME_PLAN_TERM}并清空旧批准；"
            "请重新批准全部条目，"
            "尚未执行任何文件操作"
        )

    def _override_subtitle_selected(self) -> None:
        if len(self.selected_ids) != 1:
            notify("字幕决定必须且只能选择一条记录")
            return
        selected_id = next(iter(self.selected_ids))
        matching = [entry for entry in self.plan.entries if entry_id(entry) == selected_id]
        if len(matching) != 1 or matching[0].subtitle is None:
            notify("所选记录不是结构化字幕条目")
            return
        unknown_tokens = tuple(matching[0].subtitle.unknown_tokens)
        preserve_values = _comma_values(self.subtitle_preserve_unknown.value)
        discard_values = _comma_values(self.subtitle_discard_unknown.value)
        if (
            len(set(preserve_values)) != len(preserve_values)
            or len(set(discard_values)) != len(discard_values)
        ):
            notify("未知 token 输入不能重复；请逐项保留其原始拼写")
            return
        preserve = set(preserve_values)
        discard = set(discard_values)
        if preserve & discard or preserve | discard != set(unknown_tokens):
            notify("未知 token 必须恰好分配到保留或删除，且不能重复")
            return
        reason = str(self.subtitle_reason.value or "").strip()
        dispositions = tuple(
            UnknownTokenDisposition(
                token,
                UnknownTokenAction.PRESERVE_AS_TITLE
                if token in preserve else UnknownTokenAction.DISCARD_MANUAL,
                "" if token in preserve else reason,
            )
            for token in unknown_tokens
        )
        titles = list(_comma_values(self.subtitle_titles.value))
        for token in matching[0].subtitle.unknown_tokens:
            if token in preserve and token not in titles:
                titles.append(token)
        flags = tuple(
            flag for flag in ("default", "forced", "sdh")
            if flag in tuple(self.subtitle_flags.value or ())
        )
        arguments = {
            "scope": SubtitleOverrideScope(self.subtitle_rule_scope.value),
            "associated_video_source_path": self.subtitle_video.value,
            "primary_language": self.subtitle_language.value,
            "script": self.subtitle_script.value,
            "region_hint": self.subtitle_region.value,
            "additional_languages": _comma_values(self.subtitle_additional.value),
            "track_title_tokens": tuple(titles),
            "flags": flags,
            "unknown_dispositions": dispositions,
            "reason": reason,
        }
        if arguments["scope"] == SubtitleOverrideScope.SERIES:
            self._preview_series_subtitle_override(selected_id, arguments)
            return
        self._save_subtitle_override(selected_id, arguments)

    def _preview_series_subtitle_override(
        self, selected_id: str, arguments: Dict[str, object]
    ) -> None:
        try:
            preview = self.review_service.apply_subtitle_override_rule(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                selected_id,
                **arguments,
                _persist=False,
            )
        except PlanReviewError as exc:
            notify(localize_plan_term(exc))
            return
        before = {entry_id(entry): entry for entry in self.plan.entries}
        impacted = [
            entry for entry in preview.entries
            if entry_id(entry) in before
            and entry.subtitle is not None
            and preview.subtitle_override_rules
            and entry.subtitle.applied_rule_id
            == preview.subtitle_override_rules[-1].rule_id
        ]
        selected = before[selected_id]
        raw_label = selected.subtitle.raw_label if selected.subtitle else ""
        with ui.dialog() as dialog, ui.card().classes("bar-dialog max-w-3xl"):
            ui.label("确认当前系列精确批量规则").classes("bar-dialog-title")
            ui.label(
                f"完整标签签名：{raw_label}；命中 {len(impacted)} 条。"
                f"仅修改当前{RENAME_PLAN_TERM}，不会用于未来任务；"
                "每条字幕保留自己的关联视频。"
            ).classes("bar-muted")
            with ui.scroll_area().classes("w-full max-h-96"):
                for entry in impacted:
                    original = before[entry_id(entry)]
                    with ui.column().classes("w-full gap-0 mb-3"):
                        ui.label(str(entry.source_path)).classes(
                            "bar-help break-all"
                        )
                        ui.label(f"旧：{original.target_path}").classes(
                            "bar-help break-all text-grey-7"
                        )
                        ui.label(f"新：{entry.target_path}").classes(
                            "bar-help break-all text-blue-8"
                        )
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button(
                    f"确认修改 {len(impacted)} 条并生成新版{RENAME_PLAN_TERM}",
                    on_click=lambda: (
                        dialog.close(),
                        self._save_subtitle_override(selected_id, arguments),
                    ),
                    icon="rule",
                ).props("color=orange-8")
        dialog.open()

    def _save_subtitle_override(
        self, selected_id: str, arguments: Dict[str, object]
    ) -> None:
        try:
            revised = self.review_service.apply_subtitle_override_rule(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                selected_id,
                **arguments,
            )
        except PlanReviewError as exc:
            notify(localize_plan_term(exc))
            return
        self.plan = revised
        self.snapshot = build_plan_review_snapshot(revised)
        self.decision = self.review_service.load_or_create(revised)
        self.task_data = get_task(self.plan.task_id) or self.task_data
        self.selected_ids.clear()
        self.selection_label.text = "已选择 0 项"
        self.review_badge.text = REVIEW_STATUS_LABELS[self.decision.status]
        self._refresh_plan_header()
        self._render_entries()
        self._render_subtitle_rule_audit()
        self._refresh_execute_button()
        notify(f"字幕决定已保存为新版{RENAME_PLAN_TERM}；旧批准已清空，尚未执行文件操作")

    def _revert_subtitle_rule(self, rule_id: str) -> None:
        reason_widget = getattr(self, "subtitle_revert_reason", None)
        reason = str(getattr(reason_widget, "value", "") or "").strip()
        try:
            revised = self.review_service.revert_subtitle_rule(
                self.plan.task_id,
                PlanStore.fingerprint(self.plan),
                rule_id,
                reason,
            )
        except PlanReviewError as exc:
            notify(localize_plan_term(exc))
            return
        self.plan = revised
        self.snapshot = build_plan_review_snapshot(revised)
        self.decision = self.review_service.load_or_create(revised)
        self.task_data = get_task(self.plan.task_id) or self.task_data
        self.selected_ids.clear()
        self.selection_label.text = "已选择 0 项"
        self.review_badge.text = REVIEW_STATUS_LABELS[self.decision.status]
        self._refresh_plan_header()
        self._render_entries()
        self._render_subtitle_rule_audit()
        self._refresh_execute_button()
        notify(f"字幕规则已撤销为新版{RENAME_PLAN_TERM}；旧批准未恢复，尚未执行文件操作")

    def _refresh_plan_header(self) -> None:
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
            (getattr(self, "header_title_value", None), authoritative_title or "未记录"),
            (getattr(self, "header_season_value", None), self._header_season_text()),
            (
                getattr(self, "header_recognition_value", None),
                self._header_recognition_text(),
            ),
            (
                getattr(self, "header_entry_count_value", None),
                str(len(self.snapshot.rows)),
            ),
        ):
            if element is not None:
                element.text = value
                element.update()
        if can_approve_plan(self.snapshot.plan_status):
            self.approve_selected_button.enable()
            self.approve_all_button.enable()
        else:
            self.approve_selected_button.disable()
            self.approve_all_button.disable()
        self._render_issues()

    def _header_season_text(self) -> str:
        value = self.plan.metadata.get("season_id")
        if isinstance(value, int) and value > 0:
            return f"第 {value} 季"
        return "不适用" if bool(self.plan.metadata.get("is_movie")) else "待确认"

    def _header_recognition_text(self) -> str:
        work = getattr(self.plan, "work_identity", None)
        if work is not None and work.status == "confirmed":
            return "已确认作品"
        return "等待选择作品"


def _meta_item(label: str, value: str, monospace: bool = False):
    with ui.column().classes("gap-0"):
        ui.label(label).classes("bar-help")
        return ui.label(value).classes(
            "font-medium " + ("font-mono" if monospace else "")
        )


def _comma_values(value: object) -> Tuple[str, ...]:
    return tuple(
        item.strip() for item in str(value or "").replace("，", ",").split(",")
        if item.strip()
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
    use_ai: bool,
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
    task_data.update({
        "path": source_path,
        "is_anime": is_anime,
        "is_movie": is_movie,
        "name": str(name_value or "").strip() or "等待识别",
        "season_id": season_id,
        "use_ai": use_ai,
    })
    write_task(task_id, task_data)
    await task_queue.reenqueue_workspace(
        task_id,
        Path(source_path),
        is_anime=is_anime,
        is_movie=is_movie,
        custom_name=str(name_value or "").strip() or None,
        custom_season_id=season_id,
        use_ai=use_ai,
        preview_only=bool(task_data.get("preview_only", False)),
    )
    notify("设置已保存，任务已重新加入队列", type="positive")
    return True


class RecognitionRecoveryDialog(ui.dialog):
    """Single-workspace fallback for tasks which do not have a readable plan."""

    def __init__(self, task_id: str, task_data: Dict[str, object], error: Exception):
        super().__init__()
        self.task_id = task_id
        self.task_data = task_data
        with self, ui.card().classes("bar-dialog bar-review-dialog"):
            ui.label("整理任务").classes("bar-dialog-title")
            ui.label(
                "当前没有可读取的整理计划。请修正重新识别设置后生成新计划。"
            ).classes("bar-muted")
            ui.label(
                "当前任务没有可读取的整理计划（可能尚未生成、已过期或已损坏）。"
            ).classes(
                "bar-task-feedback bar-error-text"
            )
            self.path_input = ui.input(
                "来源目录", value=str(task_data.get("path") or "")
            ).props("outlined dense").classes("w-full")
            with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                self.media_type = ui.select(
                    MEDIA_OPTIONS,
                    value=_media_type_from_flags(
                        task_data.get("is_anime"), task_data.get("is_movie")
                    ),
                    label="媒体类型",
                ).props("outlined dense options-dense").classes("w-48")
                self.name_input = ui.input(
                    "检索名称", value=str(task_data.get("name") or "")
                ).props("outlined dense clearable").classes("grow")
                self.season_input = ui.number(
                    "季度", value=task_data.get("season_id")
                ).props("outlined dense clearable").classes("w-32")
                self.use_ai = ui.checkbox(
                    "允许 AI 辅助", value=bool(task_data.get("use_ai", False))
                )
            with ui.row().classes("w-full gap-3"):
                ui.button(icon="folder_open", on_click=self._pick_source).props(
                    'outline color=blue-8 aria-label="从文件夹树选择来源目录"'
                ).tooltip("从文件夹树选择来源目录")
                ui.button(
                    "保存并重新识别", icon="refresh", on_click=self._restart
                ).props("unelevated color=blue-8")
                ui.button("关闭", on_click=self.close).props("flat color=grey-8")

    async def _pick_source(self) -> None:
        current = str(self.path_input.value or cm.get_config("docker_mnt") or "/volume1")
        result = await local_file_picker(current, multiple=False, title="选择来源目录")
        if isinstance(result, (list, tuple)) and result:
            self.path_input.value = str(result[0])
            self.path_input.update()

    async def _restart(self) -> None:
        saved = await _reenqueue_from_workspace(
            self.task_id,
            self.task_data,
            path_value=self.path_input.value,
            media_type=self.media_type.value,
            name_value=self.name_input.value,
            season_value=self.season_input.value,
            use_ai=bool(self.use_ai.value),
        )
        if saved:
            self.close()


async def plan_review_page(task_id: str) -> None:
    task_data = get_task(task_id)
    try:
        plan = PlanStore(PLAN_PATH).load(task_id)
    except PlanStoreError as exc:
        if task_data.get("plan_status") != "inventory_only":
            with _dialog_mount_context():
                dialog = RecognitionRecoveryDialog(task_id, task_data, exc)
            try:
                await dialog
            finally:
                dialog.delete()
            return
        try:
            plan = build_inventory_review_draft(
                task_data,
                dry_run=is_dry_run_enabled(),
            )
            decision_store = DecisionStateStore(DECISION_STATE_PATH)
            # Persisted provider payloads and candidates are observations.  If
            # the authoritative Decision/Plan pair is missing, persist an empty
            # Decision plus a blocked review Plan instead of guessing whether
            # an observation came from an explicit choice or a unique result.
            recognition = UnresolvedRecognitionInput(task_id=task_id)

            def finalize_inventory(_decision, projected, current):
                snapshot = build_plan_review_snapshot(projected)
                current.update({
                    "workflow_status": "invalid",
                    "plan_status": snapshot.plan_status,
                    "plan_entry_count": len(snapshot.rows),
                    "inventory_draft_created": True,
                })
                return current

            task_decision, plan = commit_recognition_projection(
                decision_store,
                PlanStore(PLAN_PATH),
                recognition,
                plan,
                task_terminal=TaskTerminalMutation.prepare(
                    TASK_PATH, task_id, finalize_inventory
                ),
            )
        except (InventoryDraftError, PlanStoreError) as draft_exc:
            notify(f"无法建立库存处理草案：{localize_plan_term(draft_exc)}")
            return
        snapshot = build_plan_review_snapshot(plan)
        task_data = get_task(task_id)
        notify("已从未漂移的只读库存建立阻断式处理草案；尚未执行文件操作")
    # This callback is invoked by a button inside the refreshable review-task
    # list.  If the dialog inherits that button's slot, the dashboard timer
    # deletes it as soon as approval changes the task signature.  Mount it on
    # the stable client root so approval can refresh the card behind it while
    # the dialog remains open for the execution step.
    with _dialog_mount_context():
        dialog = PlanReviewDialog(plan, task_data or get_task(task_id))
    try:
        await dialog
        if not getattr(dialog, "exit_requested", True):
            # A dashboard refresh can transiently detach the portal even
            # though the operator never asked to leave. Re-open the same
            # persisted revision once; do not enqueue recognition again.
            dialog.open()
            await dialog
    finally:
        dialog.delete()
