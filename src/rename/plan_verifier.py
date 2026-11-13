"""Structural verification for :class:`~src.rename.rename_plan.RenamePlan`."""

from __future__ import annotations

import os
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from .rename_plan import (
    SUBTITLE_OVERRIDE_ENGINE_VERSION,
    SUBTITLE_OVERRIDE_RULE_SCHEMA_VERSION,
    PlanEntry,
    PlanStatus,
    RenamePlan,
    SubtitleOverrideRule,
    SubtitleOverrideScope,
    UnknownTokenAction,
    EditionValue,
    EvidenceValue,
    PresentationGroup,
    VersionDescriptor,
    build_version_ref,
    format_presentation_version_label,
)
from .subtitle_labels import (
    DimensionResolution,
    SUBTITLE_ASSOCIATION_RULE_VERSION,
    SUBTITLE_LABEL_RULE_VERSION,
    SUBTITLE_SUFFIXES,
)
from .subtitle_workflow import format_subtitle_target_fields
from .presentation import PROVIDER_REGISTRY
from .projection_authority import (
    ProjectionAuthorityError,
    validate_plan_projection_receipt,
)
from .target_routing import inspect_plan_target_routing
from .canonical_naming import (
    CanonicalNamingError,
    is_portable_media_leaf,
    read_naming_decision,
)

from .media_classifier import LocalRole
from . import provider_special_evidence as provider_special


_MOVIE_EXTRA_SUBTYPES = frozenset({
    "trailer", "featurette", "interview", "behind_the_scenes",
    "deleted_scene", "scene", "short", "clip", "sample", "other",
})
_TV_EXTRA_SUBTYPES = frozenset(
    role.value for role in LocalRole if role is not LocalRole.MAIN_EPISODE
) | {"other"}


@dataclass(frozen=True)
class PlanIssue:
    code: str
    message: str
    source_path: Optional[Path] = None
    target_path: Optional[Path] = None


@dataclass(frozen=True)
class PlanVerificationResult:
    status: PlanStatus
    issues: Tuple[PlanIssue, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status == PlanStatus.ACCEPTED


def subtitle_conflicts(entries):
    """返回现行输出轨道冲突的两方，核查与界面共用同一分组。"""
    from .content_associations import subtitle_track_keys
    entries = tuple(entries)
    tracks = subtitle_track_keys((e.source_path, e.subtitle.associated_video_source_path,
        (e.subtitle.primary_language, e.subtitle.script, e.subtitle.region_hint,
         e.subtitle.additional_languages, e.subtitle.track_title_tokens, e.subtitle.flags))
        for e in entries if e.media_kind == 'subtitle' and e.subtitle is not None)
    semantics, defaults = {}, {}
    for index, entry in enumerate(entries):
        contract = entry.subtitle
        if entry.media_kind != 'subtitle' or contract is None:
            continue
        owner = (_path_key(contract.associated_video_source_path)
                 if contract.associated_video_source_path is not None else '<unresolved>')
        key = (owner, contract.primary_language, contract.script, contract.region_hint,
               contract.additional_languages, contract.track_title_tokens, contract.flags)
        if key in semantics and tracks[semantics[key].source_path] != tracks[entry.source_path]:
            yield index, 'duplicate_subtitle_semantics', semantics[key], entry
        else:
            semantics[key] = entry
        if 'default' in contract.flags and contract.associated_video_source_path is not None:
            if owner in defaults and tracks[defaults[owner].source_path] != tracks[entry.source_path]:
                yield index, 'multiple_default_subtitles', defaults[owner], entry
            else:
                defaults[owner] = entry


def tv_authority_issue(plan: RenamePlan, *, validate_projection_receipt=True) -> Optional[PlanIssue]:
    """Return one stable operator-facing issue for incomplete TV authority."""

    tv_groups = tuple(
        group for group in plan.presentation_groups if group.kind == "tv_episodes"
    )
    if not tv_groups:
        return None

    metadata = plan.metadata
    snapshot = metadata.get("provider_link_snapshot")
    authority_required = (
        isinstance(snapshot, Mapping)
        or metadata.get("tv_authority_required") is True
        or (
            metadata.get("is_anime") is True
            and metadata.get("is_movie") is False
        )
    )
    if not authority_required:
        return None
    series_key = str(metadata.get("series_key") or "").strip()
    season_binding_id = str(metadata.get("season_binding_id") or "").strip()
    season_number = metadata.get("season_number")
    catalog_revision = metadata.get("series_catalog_revision")
    canonical_root = str(metadata.get("canonical_work_root") or "").strip()
    common_complete = (
        bool(series_key)
        and bool(season_binding_id)
        and isinstance(season_number, int)
        and not isinstance(season_number, bool)
        and season_number >= 0
        and isinstance(catalog_revision, int)
        and not isinstance(catalog_revision, bool)
        and catalog_revision >= 0
        and bool(canonical_root)
    )
    provider_link_complete = (
        isinstance(snapshot, Mapping)
        and str(snapshot.get("series_key") or "").strip() == series_key
        and bool(str(snapshot.get("mapping_id") or "").strip())
        and str(snapshot.get("automation_level") or "") in {"L0", "L1", "L2", "L3"}
        and re.fullmatch(
            r"[0-9a-f]{64}",
            str(snapshot.get("evidence_fingerprint") or ""),
        )
        is not None
        and any(
            isinstance(item, Mapping)
            and item.get("season_number") == season_number
            for item in snapshot.get("season_assignments", ()) or ()
        )
    )
    typed_authority_complete = (
        metadata.get("tv_planning_authority_required") is True
        and metadata.get("tv_planning_authority_schema_version") == 2
        and metadata.get("tv_planning_authority_policy_version")
        == "tv-planning-authority-v2"
        and re.fullmatch(
            r"[0-9a-f]{64}",
            str(metadata.get("tv_planning_authority_fingerprint") or ""),
        ) is not None
    )
    complete = common_complete and (
        provider_link_complete or typed_authority_complete
    )
    provider = plan.work_identity.primary_provider if plan.work_identity else None
    from .series_registration import (validate_plan_series_registration,
        _validate_series_registration_contents, _is_manual_subject_registration)
    registration_reader = (validate_plan_series_registration if validate_projection_receipt
                           else _validate_series_registration_contents)
    try:
        manual_subject = _is_manual_subject_registration(plan, registration_reader(plan))
    except (ValueError, TypeError):
        manual_subject = False
    if provider is not None and ((provider.namespace, provider.entity_type) == ('tmdb', 'tv')
            or (provider.namespace, provider.entity_type) == ('bangumi', 'subject') and manual_subject):
        # Direct choices consume their frozen registration proof. Legacy
        # Bangumi inventory keeps its existing authority and routing checks.
        try:
            registration = registration_reader(plan)
            complete = (common_complete and registration is not None
                        and registration.namespace == provider.namespace and registration.series_key == series_key)
        except (ValueError, TypeError):
            complete = False
    if complete:
        return None
    return PlanIssue(
        "tv_authority_incomplete",
        "这部电视剧尚未确认所属系列和季度；请重新选择动画条目并完成季度匹配",
    )


def _provider_regular_episode_range_issues(
    plan: RenamePlan,
) -> tuple[PlanIssue, ...]:
    """Validate final regular episode projections against frozen authority."""

    snapshot = plan.metadata.get("provider_link_snapshot")
    if not any(e.media_kind == 'video' for e in plan.entries):
        return ()
    if not isinstance(snapshot, Mapping) or "derived_episode_ranges" not in snapshot:
        return ()
    selected_subject_id = str(snapshot.get("selected_subject_id") or "")
    tmdb_tv_id = str(snapshot.get("tmdb_tv_id") or "")
    raw_ranges = snapshot.get("derived_episode_ranges")
    if not isinstance(raw_ranges, (list, tuple)):
        return (PlanIssue(
            "provider_regular_episode_overflow_unresolved",
            "季度关系缺少可验证的分集范围；请重新匹配作品",
        ),)

    normalized = []
    for row in raw_ranges:
        if not isinstance(row, Mapping):
            continue
        subject_id = str(row.get("bangumi_subject_id") or "")
        row_tmdb_id = str(row.get("tmdb_tv_id") or "")
        season = row.get("season_number")
        start = row.get("episode_start")
        end = row.get("episode_end")
        if (
            not subject_id
            or not row_tmdb_id
            or isinstance(season, bool)
            or not isinstance(season, int)
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 1
            or end < start
        ):
            continue
        normalized.append((subject_id, row_tmdb_id, season, start, end))
    selected = [
        row for row in normalized
        if row[0] == selected_subject_id and row[1] == tmdb_tv_id
    ]
    if len(selected) != 1:
        return (PlanIssue(
            "provider_regular_episode_overflow_unresolved",
            "所选作品没有唯一冻结的普通分集范围；请重新匹配作品",
        ),)
    _subject_id, _tmdb_id, selected_season, start, end = selected[0]
    same_season = sorted(
        (row for row in normalized if row[1:3] == (tmdb_tv_id, selected_season)),
        key=lambda row: (row[3], row[4], row[0]),
    )
    for previous, current in zip(same_season, same_season[1:]):
        if current[3] <= previous[4]:
            return (PlanIssue(
                "provider_regular_episode_overflow_unresolved",
                "冻结的普通分集范围互相重叠；请重新匹配作品",
            ),)

    raw_ownership = plan.metadata.get("task_episode_ownership", ())
    owned_by_source = {}
    primary = plan.work_identity.primary_provider if plan.work_identity else None
    expected_work_key = (
        ":".join((
            str(primary.namespace or ""),
            str(primary.entity_type or ""),
            str(primary.provider_id or ""),
        ))
        if primary is not None else ""
    )
    expected_work_fingerprint = (
        hashlib.sha256(expected_work_key.encode("utf-8")).hexdigest()
        if expected_work_key else ""
    )
    if isinstance(raw_ownership, (list, tuple)):
        for row in raw_ownership:
            if not isinstance(row, Mapping):
                continue
            source_path = str(row.get("source_path") or "")
            row_subject = str(row.get("bangumi_subject_id") or "")
            row_work_key = str(row.get("work_identity_key") or "")
            row_work_fingerprint = str(
                row.get("work_identity_fingerprint") or ""
            )
            row_season = row.get("season_number")
            row_source_episode = row.get("source_episode")
            row_target_episode = row.get("target_episode")
            if (
                source_path
                and row_subject
                and row.get("schema_version") == 2
                and row_work_key == expected_work_key
                and row_work_fingerprint == expected_work_fingerprint
                and isinstance(row_season, int)
                and not isinstance(row_season, bool)
                and isinstance(row_source_episode, int)
                and not isinstance(row_source_episode, bool)
                and isinstance(row_target_episode, int)
                and not isinstance(row_target_episode, bool)
                and any(
                    candidate[0] == row_subject
                    and candidate[2] == row_season
                    and candidate[3] <= row_source_episode <= candidate[4]
                    for candidate in normalized
                )
            ):
                owned_by_source[os.path.normcase(source_path).casefold()] = (
                    row_season,
                    row_target_episode,
                )

    issues = []
    for entry in plan.entries:
        asset = entry.presentation_asset
        if (
            entry.media_kind != "video"
            or entry.season_id != selected_season
            or isinstance(entry.episode_id, bool)
            or not isinstance(entry.episode_id, int)
            or asset is None
            or asset.asset_role.kind != "content_version"
        ):
            continue
        owned = owned_by_source.get(
            os.path.normcase(str(entry.source_path)).casefold()
        )
        if owned == (entry.season_id, entry.episode_id):
            continue
        if entry.episode_id < start or entry.episode_id > end:
            issues.append(PlanIssue(
                "provider_regular_episode_overflow_unresolved",
                f"普通集号 E{entry.episode_id:02d} 不在已确认范围 "
                f"E{start:02d}–E{end:02d}；请确认是否为官方特别篇",
                entry.source_path,
                entry.target_path,
            ))

    return tuple(issues)






class PlanVerifier:
    """Reject incomplete, ambiguous, or internally conflicting plans."""

    def verify(
        self,
        plan: RenamePlan,
        *,
        validate_projection_receipt: bool = True,
    ) -> PlanVerificationResult:
        if plan.metadata.get("recovery_reason") == "catalog_detached":
            return PlanVerificationResult(
                PlanStatus.INVALID,
                (
                    PlanIssue(
                        "catalog_detached_replan_required",
                        "作品登记已解除；必须按当前配置重新生成整理计划后才能批准",
                    ),
                ),
            )

        issues: List[PlanIssue] = []
        from .episode_range import validate_projection as validate_ranges
        if any(r.get('conflict') for r in (plan.metadata.get('generated_outputs') or {}).get('files', ())):
            issues.append(PlanIssue('generated_output_conflict', 'NFO目标存在冲突，请核查或明确换源'))
        valid_range_ids = set()
        try:
            valid_range_ids = validate_ranges(plan)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(PlanIssue('episode_range_invalid', str(exc)))
        from .episode_versions import validate_projection as validate_versions
        try:
            valid_range_ids.update(validate_versions(plan))
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(PlanIssue('episode_versions_invalid', str(exc)))
        from .content_policy import require_media_ready, require_content_complete
        try:
            (require_media_ready if validate_projection_receipt else require_content_complete)(plan)
        except (ValueError, ProjectionAuthorityError) as exc:
            issues.append(PlanIssue('content_policy_pending', str(exc)))
        from .content_selection import validate_plan_content
        try:
            validate_plan_content(plan)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(PlanIssue('content_selection_invalid', str(exc)))
        if {'movie_continuous','tv_episodes'}.issubset({g.kind for g in plan.presentation_groups}):
            issues.append(PlanIssue('mixed_presentation_requires_partition',
                '电影连续版与剧集分集版不能在同一任务执行；请按观看形式拆为独立任务后分别确认'))
        if validate_projection_receipt and (
            plan.projection_receipt is not None
            or plan.metadata.get("projection_authority_required") is True
        ):
            try:
                validate_plan_projection_receipt(plan)
            except (ProjectionAuthorityError, TypeError, ValueError):
                issues.append(PlanIssue(
                    "projection_receipt_mismatch",
                    "整理计划与已冻结的权威凭据不一致；请重新生成整理计划",
                ))
        provider_snapshot = plan.metadata.get("provider_link_snapshot")
        if isinstance(provider_snapshot, Mapping):
            assignments = tuple(
                item for item in provider_snapshot.get("season_assignments", ()) or ()
                if isinstance(item, Mapping)
            )
            same_season_subjects = {}
            for item in assignments:
                season = item.get("season_number")
                subject = str(item.get("bangumi_subject_id") or "")
                if isinstance(season, int) and subject:
                    same_season_subjects.setdefault(season, set()).add(subject)
            if (
                int(provider_snapshot.get("schema_version") or 1) < 2
                and any(len(subjects) > 1 for subjects in same_season_subjects.values())
                and not provider_snapshot.get("derived_episode_ranges")
            ):
                issues.append(PlanIssue(
                    "provider_episode_ranges_missing_replan_required",
                    "旧季度关系没有保存分集范围；请重新匹配并生成新版整理计划",
                ))
            selected_subject_id = str(
                provider_snapshot.get("selected_subject_id") or ""
            )
            if selected_subject_id:
                identities = []
                primary = (
                    plan.work_identity.primary_provider
                    if plan.work_identity is not None else None
                )
                if primary is not None:
                    identities.append(primary)
                identities.extend(plan.linked_provider_identities)
                selected_present = any(
                    identity.status == "confirmed"
                    and identity.namespace == "bangumi"
                    and identity.entity_type == "subject"
                    and identity.provider_id == selected_subject_id
                    for identity in identities
                )
                if not selected_present:
                    issues.append(PlanIssue(
                        "linked_provider_identity_authority_missing",
                        "整理计划丢失了所选 Bangumi 作品身份；请重新识别作品",
                    ))
        authority_issue = tv_authority_issue(plan, validate_projection_receipt=validate_projection_receipt)
        if authority_issue is not None:
            issues.append(authority_issue)
        from dataclasses import replace
        inferred_entries = tuple(e for e in plan.entries if _entry_id(e) not in valid_range_ids)
        issues.extend(_provider_regular_episode_range_issues(replace(plan, entries=inferred_entries)))
        projection_candidates = plan.metadata.get(
            "source_episode_projection_candidates"
        )
        if (
            isinstance(projection_candidates, (tuple, list))
            and len(projection_candidates) > 1
            and not plan.metadata.get("source_episode_projection")
        ):
            issues.append(PlanIssue(
                "source_episode_projection_decision_required",
                "文件中的集号有两种安全解释，请明确选择后再批准",
            ))
        planned_sources: Dict[str, PlanEntry] = {}
        target_sources: Dict[str, PlanEntry] = {}
        video_episodes: Dict[Tuple[int, int, Optional[str]], PlanEntry] = {}
        video_sources: Dict[str, List[PlanEntry]] = {}
        rules_by_id = self._verify_subtitle_rules(plan, issues)

        expected = {_path_key(path): path for path in plan.expected_sources}
        ignored = {_path_key(path): path for path in plan.ignored_sources}
        target_root: Optional[Path] = None
        if "target_root" in plan.metadata:
            configured_root = plan.metadata.get("target_root")
            if not isinstance(configured_root, str) or not configured_root.strip():
                issues.append(
                    PlanIssue(
                        "invalid_target_root",
                        "Plan metadata.target_root 必须是非空路径字符串",
                    )
                )
            else:
                target_root = Path(configured_root)
        if plan.execution_policy is not None:
            policy = plan.execution_policy
            if not policy.target_root.is_absolute():
                issues.append(PlanIssue(
                    "execution_target_root_not_absolute",
                    "执行型 Plan 的 target_root 必须是绝对路径",
                ))
            if target_root is None or _path_key(policy.target_root) != _path_key(target_root):
                issues.append(PlanIssue(
                    "execution_target_root_mismatch",
                    "执行策略 target_root 与 Plan metadata 不一致",
                ))
            if policy.mode != "hardlink" or not policy.preserve_source:
                issues.append(PlanIssue(
                    "execution_policy_unsafe",
                    "S5 执行策略必须是保留源文件的硬链接",
                ))

        if plan.metadata.get("canonical_naming_required") is True:
            try:
                naming_decision = read_naming_decision(
                    plan.metadata.get("canonical_naming_decision")
                )
            except CanonicalNamingError:
                issues.append(PlanIssue(
                    "canonical_naming_policy_missing",
                    "这份整理计划缺少当前路径命名规则；请重新生成整理计划",
                ))
            else:
                if (
                    plan.work_identity is None
                    or plan.work_identity.title != naming_decision.display_title
                ):
                    issues.append(PlanIssue(
                        "canonical_naming_policy_mismatch",
                        "作品标题与冻结命名决定不一致；请重新生成整理计划",
                    ))
                checked: set[tuple[str, str]] = set()
                canonical_root = str(
                    plan.metadata.get("canonical_work_root") or ""
                ).strip()
                if canonical_root:
                    checked.add((Path(canonical_root).name, canonical_root))
                if target_root is not None:
                    for entry in plan.entries:
                        try:
                            relative = entry.target_path.relative_to(target_root)
                        except ValueError:
                            continue
                        for part in relative.parts:
                            checked.add((part, str(entry.target_path)))
                for leaf, location in sorted(checked):
                    if not is_portable_media_leaf(leaf):
                        issues.append(PlanIssue(
                            "canonical_path_leaf_invalid",
                            "目标路径包含 Windows 无法安全保存的名称；"
                            "请重新生成整理计划",
                            target_path=Path(location),
                        ))

        # Inventory smart-completion is the first production path whose
        # target metadata has library-root semantics and a frozen four-way
        # routing snapshot.  Legacy/rules Plans still use work-root semantics
        # and must not be silently reinterpreted here.
        if plan.metadata.get("mapping_source") == "inventory_smart_completion":
            _routing, routing_problems = inspect_plan_target_routing(plan)
            for problem in routing_problems:
                issues.append(PlanIssue(
                    problem.code,
                    problem.message,
                    target_path=problem.target_path,
                ))

        # Inventory completion keeps a genuine main-to-main numbering
        # collision as a reviewable Plan fact.  It must not disappear behind
        # an exception or be mistaken for a generic unknown-file warning.
        conflict_sources = plan.metadata.get("episode_assignment_conflict_sources")
        if isinstance(conflict_sources, (tuple, list)):
            for source in conflict_sources:
                issues.append(PlanIssue(
                    "episode_assignment_conflict",
                    "多个正片候选映射到同一集号；请在处理页确认哪一个是特典，"
                    "或保留为正片候选后重新生成。",
                    source_path=Path(str(source)),
                ))

        for entry in plan.entries:
            source_key = _path_key(entry.source_path)
            target_key = _path_key(entry.target_path)

            previous_source = planned_sources.get(source_key)
            if previous_source is not None:
                issues.append(
                    PlanIssue(
                        "duplicate_source",
                        f"源文件重复出现在计划中：{entry.source_path}",
                        source_path=entry.source_path,
                        target_path=entry.target_path,
                    )
                )
            else:
                planned_sources[source_key] = entry

            previous_target = target_sources.get(target_key)
            if previous_target is not None and _path_key(
                previous_target.source_path
            ) != source_key:
                issues.append(
                    PlanIssue(
                        "duplicate_target",
                        "多个源文件指向同一目标："
                        f"{previous_target.source_path} / {entry.source_path} "
                        f"-> {entry.target_path}",
                        source_path=entry.source_path,
                        target_path=entry.target_path,
                    )
                )
            else:
                target_sources[target_key] = entry

            if not _path_within(entry.source_path, plan.source_root):
                issues.append(
                    PlanIssue(
                        "source_outside_root",
                        f"源文件越出 Plan 源根目录：{entry.source_path}",
                        source_path=entry.source_path,
                        target_path=entry.target_path,
                    )
                )

            if target_root is not None and not _path_within(
                entry.target_path, target_root
            ):
                issues.append(
                    PlanIssue(
                        "target_outside_root",
                        f"目标文件越出 Plan 目标根目录：{entry.target_path}",
                        source_path=entry.source_path,
                        target_path=entry.target_path,
                    )
                )

            if source_key == target_key:
                issues.append(
                    PlanIssue(
                        "source_equals_target",
                        f"源路径与目标路径相同：{entry.source_path}",
                        source_path=entry.source_path,
                        target_path=entry.target_path,
                    )
                )

            if entry.season_id is not None and entry.season_id < 0:
                issues.append(
                    PlanIssue(
                        "negative_season",
                        f"季度不能为负数：{entry.season_id}",
                        source_path=entry.source_path,
                        target_path=entry.target_path,
                    )
                )

            from .episode_range import valid_episode
            if entry.episode_id is not None and not valid_episode(entry.episode_id):
                issues.append(
                    PlanIssue(
                        "invalid_episode",
                        f"集号必须是0至99999的整数：{entry.episode_id}",
                        source_path=entry.source_path,
                        target_path=entry.target_path,
                    )
                )

            if "s00e00" in entry.target_path.name.casefold():
                issues.append(
                    PlanIssue(
                        "s00e00_forbidden",
                        f"目标名不得包含 S00E00：{entry.target_path}",
                        source_path=entry.source_path,
                        target_path=entry.target_path,
                    )
                )

            if (
                entry.media_kind == "video"
                and entry.season_id is not None
                and entry.episode_id is not None
                and valid_episode(entry.episode_id)
            ):
                version_ref = (
                    entry.presentation_asset.version_descriptor.version_ref
                    if entry.presentation_asset is not None
                    and entry.presentation_asset.version_descriptor is not None
                    else None
                )
                from .episode_range import coverage
                from .rename_plan import build_version_ref
                try:
                    slots = coverage(entry)
                except ValueError:
                    slots = ()
                descriptor = entry.presentation_asset.version_descriptor if entry.presentation_asset else None
                for season, episode in sorted(slots):
                    slot_version = version_ref
                    if entry.episode_end is not None and descriptor is not None and descriptor.edition.value is not None:
                        slot_version = build_version_ref(entry.presentation_asset.presentation_group,
                            season, episode, descriptor.edition.value.key, descriptor.technical_variant_key)
                    episode_key = (season, episode, slot_version)
                    if episode_key in video_episodes:
                        issues.append(PlanIssue('duplicate_video_episode',
                            f'多个视频占用同一季集：S{season:02d}E{episode:02d}',
                            source_path=entry.source_path, target_path=entry.target_path))
                    else:
                        video_episodes[episode_key] = entry
            if entry.media_kind == "video":
                video_sources.setdefault(source_key, []).append(entry)

            if entry.media_kind == "subtitle" and entry.subtitle is None:
                issues.append(PlanIssue(
                    "subtitle_contract_missing",
                    "字幕条目缺少结构化字幕合同",
                    entry.source_path,
                    entry.target_path,
                ))
            if entry.media_kind != "subtitle" and entry.subtitle is not None:
                issues.append(PlanIssue(
                    "subtitle_contract_on_non_subtitle",
                    "非字幕条目不得携带字幕合同",
                    entry.source_path,
                    entry.target_path,
                ))

        self._verify_presentation(plan, issues, valid_range_ids)

        conflicts = {}
        for index, code, _, _ in subtitle_conflicts(plan.entries):
            conflicts.setdefault(index, []).append(code)
        referenced_rule_ids: set[str] = set()
        inherited = self._verify_inherited_subtitles(plan, issues)
        for index, entry in enumerate(plan.entries):
            contract = entry.subtitle
            if entry.media_kind != "subtitle" or contract is None:
                continue
            self._verify_subtitle_entry(
                entry, contract, video_sources, rules_by_id,
                referenced_rule_ids,
                {group.group_id for group in plan.presentation_groups},
                issues,
                inherited=inherited,
            )

            for code in conflicts.get(index, ()):
                message = ('同一视频存在语义完全相同的字幕轨道' if code == 'duplicate_subtitle_semantics'
                           else '同一视频只能有一条 default 外挂字幕')
                issues.append(PlanIssue(code, message, entry.source_path, entry.target_path))

        self._verify_material_rule_references(plan, rules_by_id, referenced_rule_ids, issues,
            {entry.source_path for entry in plan.entries}, inherited=inherited)
        # 同一文件的新人工决定覆盖旧决定，旧规则仍保留在可核对的历史中。
        from .rename_plan import SubtitleOverrideScope
        later_files = set()
        for rule in reversed(plan.subtitle_override_rules):
            if rule.scope == SubtitleOverrideScope.FILE:
                if rule.selected_entry_id in later_files:
                    referenced_rule_ids.add(rule.rule_id)
                if rule.rule_id in referenced_rule_ids:
                    later_files.add(rule.selected_entry_id)
        for rule_id in sorted(set(rules_by_id) - referenced_rule_ids):
            issues.append(PlanIssue(
                "subtitle_rule_unreferenced",
                f"字幕人工规则没有任何条目引用：{rule_id}",
            ))

        planned_keys = set(planned_sources)
        ignored_keys = set(ignored)
        expected_keys = set(expected)
        for source_key in sorted(expected_keys - planned_keys - ignored_keys):
            issues.append(
                PlanIssue(
                    "orphan_source",
                    f"源文件没有目标或忽略理由：{expected[source_key]}",
                    source_path=expected[source_key],
                )
            )

        for source_key in sorted(planned_keys & ignored_keys):
            entry = planned_sources[source_key]
            issues.append(
                PlanIssue(
                    "planned_and_ignored",
                    f"源文件同时被计划和忽略：{entry.source_path}",
                    source_path=entry.source_path,
                    target_path=entry.target_path,
                )
            )

        if expected_keys:
            for source_key in sorted(planned_keys - expected_keys):
                entry = planned_sources[source_key]
                issues.append(
                    PlanIssue(
                        "unexpected_source",
                        f"计划包含库存之外的源文件：{entry.source_path}",
                        source_path=entry.source_path,
                        target_path=entry.target_path,
                    )
                )

        if issues:
            return PlanVerificationResult(PlanStatus.INVALID, tuple(issues))
        if any(entry.requires_review for entry in plan.entries):
            return PlanVerificationResult(PlanStatus.NEED_CONFIRM)
        return PlanVerificationResult(PlanStatus.ACCEPTED)

    def _verify_presentation(
        self,
        plan: RenamePlan,
        issues: List[PlanIssue],
        valid_range_ids: set,
    ) -> None:
        if plan.metadata.get('movie_version_selection') is not None:
            from .movie_version_selection import validate_selection
            try:
                validate_selection(plan)
            except (ValueError, KeyError, TypeError) as exc:
                issues.append(PlanIssue('movie_version_selection_invalid', str(exc)))
        groups: Dict[str, PresentationGroup] = {}
        content_by_unit: Dict[Tuple[str, Optional[int], Optional[int]], List[PlanEntry]] = {}
        versions_by_group: Dict[str, Dict[str, List[PlanEntry]]] = {}
        labels_by_unit: Dict[Tuple[str, Optional[int], Optional[int]], Dict[str, PlanEntry]] = {}
        declared_movie_refs = {
            entry.presentation_asset.version_descriptor.version_ref
            for entry in plan.entries
            if entry.presentation_asset is not None
            and entry.presentation_asset.version_descriptor is not None
            and entry.presentation_asset.version_descriptor.version_ref
        }

        if not plan.presentation_groups:
            issues.append(PlanIssue(
                "presentation_groups_missing",
                "schema v5 整理计划必须声明呈现分组",
            ))
        for group in plan.presentation_groups:
            if not group.group_id or group.group_id in groups:
                issues.append(PlanIssue(
                    "presentation_group_id_invalid",
                    "呈现分组 ID 为空或重复",
                ))
                continue
            groups[group.group_id] = group
            if group.kind not in {"movie_continuous", "tv_episodes", "disc_structure"}:
                issues.append(PlanIssue(
                    "presentation_group_kind_invalid",
                    f"不支持的呈现分组类型：{group.kind}",
                ))

        work = plan.work_identity
        provider = work.primary_provider if work is not None else None
        if (
            work is None or work.status != "confirmed" or provider is None
            or provider.status != "confirmed"
            or not all((provider.namespace, provider.entity_type,
                        provider.provider_id, provider.title))
        ):
            issues.append(PlanIssue(
                "work_identity_unconfirmed",
                "整理计划缺少已确认的作品身份",
            ))
        elif provider.decision_source not in {
            "explicit_user", "provider_unique", "manual_review"
        }:
            issues.append(PlanIssue(
                "work_identity_source_invalid",
                "作品身份的确认来源无效",
            ))
        elif (
            provider.namespace not in PROVIDER_REGISTRY
            or provider.entity_type
            not in PROVIDER_REGISTRY.get(provider.namespace, frozenset())
        ):
            issues.append(PlanIssue(
                "work_identity_provider_invalid",
                "作品身份的 Provider namespace/type 未注册",
            ))
        if work is not None and work.domain not in {"animation", "general", "unknown"}:
            issues.append(PlanIssue(
                "work_identity_domain_invalid",
                f"不支持的作品领域：{work.domain}",
            ))
        if work is not None and work.domain_source not in {
            "provider_selection", "task_metadata", "manual_library_selection", "unknown",
        }:
            issues.append(PlanIssue(
                "work_identity_domain_source_invalid",
                f"不支持的作品领域来源：{work.domain_source}",
            ))
        for linked in plan.linked_provider_identities:
            if linked.status not in {"candidate", "confirmed", "rejected"}:
                issues.append(PlanIssue(
                    "linked_provider_status_invalid",
                    "关联资料身份状态无效",
                ))
            if (
                linked.namespace not in PROVIDER_REGISTRY
                or linked.entity_type
                not in PROVIDER_REGISTRY.get(linked.namespace, frozenset())
            ):
                issues.append(PlanIssue(
                    "linked_provider_registry_invalid",
                    "关联资料身份的 Provider namespace/type 未注册",
                ))

        if (plan.metadata.get('asset_operation') or {}).get('requires_choice'):
            message = ('已选择替换此电影版本，请核对并确认换源范围；完成前不能批准'
                       if (plan.metadata.get('movie_version_selection') or {}).get('replacement') is not None
                       else '该电影版本槽位已有登记，请新增其他版本或明确选择换源'
                       if {g.kind for g in plan.presentation_groups} == {'movie_continuous'}
                       else '本季度已有另一份资源，请明确选择换源；不会自动替换')
            issues.append(PlanIssue('asset_replace_choice_required', message))
        unresolved_collisions = plan.metadata.get("asset_collision_unresolved", ())
        if unresolved_collisions:
            count = (
                len(unresolved_collisions)
                if isinstance(unresolved_collisions, (list, tuple)) else 1
            )
            issues.append(PlanIssue(
                "asset_collision_unresolved",
                f"有 {count} 个同名附加内容不同且无法确定版本归属",
            ))

        if {group.kind for group in plan.presentation_groups} == {'movie_continuous'}:
            from .movie_asset_scope import validate_movie_authority
            try:
                validate_movie_authority(plan)
            except (ValueError, TypeError, KeyError) as exc:
                issues.append(PlanIssue('movie_asset_authority_invalid', str(exc)))
        from .audio_projection import validate_audio
        try:
            validate_audio(plan)
        except ValueError as exc:
            issues.append(PlanIssue('external_audio_projection_invalid',str(exc)))

        special_candidates = plan.metadata.get("provider_special_candidates")
        special_decisions = plan.metadata.get("provider_special_decisions")
        from .special_decision_projection import validated_special_decisions
        valid_special_ids, special_projection_error = validated_special_decisions(plan)
        if special_projection_error is not None:
            issues.append(PlanIssue(
                special_projection_error,
                "特别篇或附加内容决定与当前文件投影不一致；请重新生成整理计划",
            ))
        observation_value = plan.metadata.get("provider_special_observation")
        provider_special_observation = None
        if isinstance(observation_value, Mapping):
            try:
                provider_special_observation = (
                    provider_special.ProviderSpecialObservation.from_mapping(
                        observation_value
                    )
                )
            except provider_special.ProviderSpecialEvidenceError:
                provider_special_observation = None
        provider_special_authority_applicable = (
            isinstance(observation_value, Mapping)
            or bool(plan.metadata.get("provider_special_observation_required"))
            or (
                plan.work_identity is not None
                and plan.work_identity.primary_provider.namespace == "tmdb"
                and plan.work_identity.primary_provider.entity_type == "tv"
            )
            or any(
                identity.namespace == "tmdb" and identity.entity_type == "tv"
                for identity in plan.linked_provider_identities
            )
        )
        unavailable_special_eligibility = plan.metadata.get(
            "provider_special_eligibility_unavailable"
        )
        unavailable_ids = (
            {
                str(identifier)
                for identifier in unavailable_special_eligibility
                if isinstance(identifier, str) and identifier
            }
            if isinstance(unavailable_special_eligibility, (list, tuple))
            else set()
        )
        decided = (
            {key: value for key, value in special_decisions.items() if key in valid_special_ids}
            if isinstance(special_decisions, Mapping) else {}
        )
        candidates = (
            special_candidates if isinstance(special_candidates, Mapping) else {}
        )
        evidence_unavailable_ids: set[str] = set()
        for entry in plan.entries:
            if entry.media_kind != "video":
                continue
            identity = os.path.normpath(str(entry.source_path)).casefold()
            identifier = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
            if identifier in valid_special_ids or identifier in valid_range_ids:
                continue
            eligibility = provider_special._provider_special_eligibility(entry)
            if eligibility == "eligible" and provider_special_authority_applicable:
                if (
                    provider_special_observation is None
                    or provider_special_observation.state
                    in {"not_run", "unavailable_or_error"}
                ):
                    evidence_unavailable_ids.add(identifier)
                    continue
                if provider_special_observation.schema_version == 3 and not provider_special_observation.displayable:
                    evidence_unavailable_ids.add(identifier)
                    continue
                if provider_special_observation.schema_version in (2, 3):
                    issues.append(PlanIssue('provider_special_decision_required',
                        '特别篇资料不完整，请明确对应或描述性命名' if provider_special_observation.state == 'incomplete'
                        else '请确认特别篇对应或描述性命名；官方原始编号不直接决定输出集号',
                        entry.source_path, entry.target_path))
                    continue
                if provider_special_observation.state == "completed_with_rows":
                    rows = candidates.get(identifier)
                    if not rows:
                        evidence_unavailable_ids.add(identifier)
                    elif identifier not in decided:
                        issues.append(PlanIssue(
                            "provider_special_decision_required",
                            "发现官方特别篇候选，请确认归入特别篇或保留为附加内容",
                            entry.source_path,
                            entry.target_path,
                        ))
        if evidence_unavailable_ids:
            for entry in plan.entries:
                identity = os.path.normpath(str(entry.source_path)).casefold()
                identifier = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
                if identifier in evidence_unavailable_ids:
                    issues.append(PlanIssue(
                        "provider_special_evidence_unavailable",
                        "特别篇资料尚不可用，请打开特别篇选择获取资料或手动描述性归入",
                        entry.source_path,
                        entry.target_path,
                    ))
        if unavailable_ids:
            for entry in plan.entries:
                identity = os.path.normpath(str(entry.source_path)).casefold()
                identifier = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
                if identifier in unavailable_ids and identifier not in valid_special_ids and identifier not in valid_range_ids:
                    issues.append(PlanIssue(
                        "provider_special_eligibility_unavailable",
                        "附加视频缺少可信的特别篇资格，请重新生成整理计划",
                        entry.source_path,
                        entry.target_path,
                    ))

        rebuild_reuse_targets = set()
        operation = plan.metadata.get('asset_operation') or {}
        if operation.get('mode') == 'replace':
            try:
                from .projection_authority import validate_plan_projection_receipt
                from .asset_operation import AssetOperation
                validate_plan_projection_receipt(plan)
                frozen = AssetOperation.from_mapping(operation).to_dict()
                previous = {row['target_path']: row.get('target_evidence')
                            for row in frozen['previous_assets']}
                if frozen['policy']=='asset-operation-v3':
                    scope = frozen['season_directory']
                    previous = {row['target']:row['evidence'] for row in scope['files'] + scope.get('special_files', [])}
                for item in plan.entries:
                    collision = item.collision_resolution
                    evidence = previous.get(str(item.target_path))
                    if collision is not None and evidence is not None and (
                        collision.target_size == evidence['size']
                        and collision.target_xxh3_128 == evidence.get('xxh3_128')
                        and collision.evidence_kind == evidence.get('kind')
                        and collision.target_device == evidence['device']
                        and collision.target_inode == evidence['inode']
                    ):
                        rebuild_reuse_targets.add(str(item.target_path))
            except (ValueError, RuntimeError, KeyError, TypeError):
                pass
        if plan.metadata.get('completed_rebuild_observation') is not None:
            try:
                from .projection_authority import validate_plan_projection_receipt
                validate_plan_projection_receipt(plan)
                rebuild_reuse_targets.update(plan.metadata['completed_rebuild_observation']['reuse_targets'])
            except (ValueError, RuntimeError, KeyError, TypeError):
                pass
        for entry in plan.entries:
            asset = entry.presentation_asset
            collision = entry.collision_resolution
            rebuild_reuse = collision is not None and str(entry.target_path) in rebuild_reuse_targets
            if collision is not None:
                try:
                    from .rename_plan import CollisionResolution
                    from .asset_collision import requires_manual_collision
                    valid_evidence = CollisionResolution.from_dict(collision.to_dict()) == collision
                    if collision.evidence_kind == 'content-xxh3-128-v1':
                        valid_evidence = valid_evidence and not requires_manual_collision(entry.source_path)
                    elif collision.evidence_kind != 'hardlink-identity-v1':
                        valid_evidence = False
                except (ValueError, TypeError, KeyError):
                    valid_evidence = False
                if (
                    not valid_evidence or collision.action != "reuse_existing"
                    or collision.source_size != collision.target_size
                    or collision.source_xxh3_128 != collision.target_xxh3_128
                ):
                    issues.append(PlanIssue(
                        "asset_collision_resolution_invalid",
                        "既有目标复用证据不完整或内容身份不一致",
                        entry.source_path,
                        entry.target_path,
                    ))
                if entry.media_kind == "subtitle" and not rebuild_reuse:
                    issues.append(PlanIssue(
                        "subtitle_collision_resolution_forbidden",
                        "字幕不能通过附加资产复用合同绕过字幕关联规则",
                        entry.source_path,
                        entry.target_path,
                    ))
            if entry.media_kind == "subtitle":
                if asset is not None:
                    issues.append(PlanIssue(
                        "subtitle_presentation_asset_forbidden",
                        "字幕不得携带 presentation_asset，必须从关联视频派生",
                        entry.source_path,
                        entry.target_path,
                    ))
                continue
            if asset is None:
                issues.append(PlanIssue(
                    "presentation_asset_missing",
                    "所有非字幕条目都必须归入呈现分组",
                    entry.source_path,
                    entry.target_path,
                ))
                continue
            group = groups.get(asset.presentation_group)
            if group is None:
                issues.append(PlanIssue(
                    "presentation_group_reference_missing",
                    f"条目引用不存在的呈现分组：{asset.presentation_group}",
                    entry.source_path,
                    entry.target_path,
                ))
                continue
            if group.kind == "disc_structure":
                issues.append(PlanIssue(
                    "disc_structure_mapping_forbidden",
                    "光盘结构只允许只读库存，不得生成可执行映射",
                    entry.source_path,
                    entry.target_path,
                ))

            role = asset.asset_role
            if collision is not None and role.kind not in {"extra", "associated"} and not rebuild_reuse:
                issues.append(PlanIssue(
                    "asset_collision_role_invalid",
                    "当前只允许特典或配套资源复用已有同内容目标",
                    entry.source_path,
                    entry.target_path,
                ))
            if role.kind not in {"content_version", "extra", "associated", "unknown"}:
                issues.append(PlanIssue(
                    "presentation_asset_role_invalid",
                    f"不支持的呈现资产角色：{role.kind}",
                    entry.source_path,
                    entry.target_path,
                ))
            if role.kind == "unknown":
                issues.append(PlanIssue(
                    "presentation_asset_role_unknown",
                    "未知资产角色必须在批准前解决",
                    entry.source_path,
                    entry.target_path,
                ))
            valid_extra_subtypes = (
                _TV_EXTRA_SUBTYPES
                if group.kind == "tv_episodes"
                else _MOVIE_EXTRA_SUBTYPES
            )
            if role.kind == "extra" and role.subtype not in valid_extra_subtypes:
                issues.append(PlanIssue(
                    "presentation_extra_subtype_invalid",
                    "附加内容必须具有与呈现分组匹配的 subtype",
                    entry.source_path,
                    entry.target_path,
                ))
            if role.kind != "extra" and role.subtype is not None:
                issues.append(PlanIssue(
                    "presentation_asset_subtype_unexpected",
                    "只有 extra 角色可以携带 subtype",
                    entry.source_path,
                    entry.target_path,
                ))
            if plan.metadata.get("owner_version_model") == 1:
                owner = asset.owner_version_ref
                if role.kind == "content_version" and owner is not None:
                    issues.append(PlanIssue(
                        "presentation_content_owner_unexpected",
                        "正片版本自身不得声明 owner_version_ref",
                        entry.source_path,
                        entry.target_path,
                    ))
                elif (
                    group.kind == "movie_continuous"
                    and role.kind in {"extra", "associated"}
                    and owner not in declared_movie_refs | {"shared"}
                ):
                    issues.append(PlanIssue(
                        "presentation_asset_owner_unresolved",
                        "电影特典或配套资源必须归属某个正片版本或标记为所有版本通用",
                        entry.source_path,
                        entry.target_path,
                    ))
            if role.kind == "content_version" and asset.version_descriptor is None:
                issues.append(PlanIssue(
                    "presentation_version_descriptor_missing",
                    "内容版本缺少 version_descriptor",
                    entry.source_path,
                    entry.target_path,
                ))
                continue
            if role.kind != "content_version" and asset.version_descriptor is not None:
                issues.append(PlanIssue(
                    "presentation_version_descriptor_unexpected",
                    "非内容资产不得携带 version_descriptor",
                    entry.source_path,
                    entry.target_path,
                ))
            if asset.segment is not None:
                segment = asset.segment
                if role.kind != "content_version" or segment.scheme not in {
                    "cd", "part", "disc_part"
                } or segment.index < 1 or segment.total < 2 or segment.index > segment.total:
                    issues.append(PlanIssue(
                        "presentation_segment_invalid",
                        "分段仅允许内容版本使用有效 cd/part/disc_part 序号",
                        entry.source_path,
                        entry.target_path,
                    ))

            descriptor = asset.version_descriptor
            if descriptor is None:
                continue
            if group.kind == "movie_continuous":
                expected_stem = entry.target_path.parent.name
                if descriptor.display_label:
                    expected_stem += f" - {descriptor.display_label}"
                if asset.segment is not None:
                    expected_stem += (
                        f"-{asset.segment.scheme}{asset.segment.index}"
                    )
                if entry.target_path.stem != expected_stem:
                    issues.append(PlanIssue(
                        "presentation_movie_target_not_canonical",
                        "电影内容目标名必须以父目录名开头并使用规范版本标签",
                        entry.source_path,
                        entry.target_path,
                    ))
                if asset.segment is not None and descriptor.display_label and descriptor.identity_policy is None:
                    issues.append(PlanIssue(
                        "presentation_segment_version_conflict",
                        "Jellyfin 多段电影不能同时使用多版本标签",
                        entry.source_path,
                        entry.target_path,
                    ))
            bound_season = (
                entry.season_id if group.kind == "tv_episodes" else None
            )
            bound_episode = (
                entry.episode_id if group.kind == "tv_episodes" else None
            )
            unit = (asset.presentation_group, bound_season, bound_episode)
            from .special_selection import validate_descriptive_special
            if validate_descriptive_special(plan, entry):
                unit = (asset.presentation_group, bound_season, descriptor.version_ref)
            content_by_unit.setdefault(unit, []).append(entry)
            self._verify_version_descriptor(entry, descriptor, issues, plan=plan)
            edition = descriptor.edition.value
            if isinstance(edition, EditionValue) and descriptor.technical_variant_key and descriptor.identity_policy is None:
                expected_ref = build_version_ref(
                    asset.presentation_group,
                    bound_season,
                    bound_episode,
                    edition.key,
                    descriptor.technical_variant_key,
                )
                if descriptor.version_ref != expected_ref:
                    issues.append(PlanIssue(
                        "presentation_version_ref_mismatch",
                        "version_ref 与规范字段重算结果不一致",
                        entry.source_path,
                        entry.target_path,
                    ))
            if descriptor.version_ref:
                versions_by_group.setdefault(asset.presentation_group, {}).setdefault(
                    descriptor.version_ref, []
                ).append(entry)
            normalized_label = _canonical_label(descriptor.display_label)
            if normalized_label:
                previous = labels_by_unit.setdefault(unit, {}).get(normalized_label)
                if previous is not None and (
                    previous.presentation_asset.version_descriptor.version_ref
                    != descriptor.version_ref
                ):
                    issues.append(PlanIssue(
                        "presentation_version_label_duplicate",
                        "同一内容单元存在重复版本标签",
                        entry.source_path,
                        entry.target_path,
                    ))
                else:
                    labels_by_unit.setdefault(unit, {})[normalized_label] = entry

        for unit, entries in content_by_unit.items():
            refs = {
                entry.presentation_asset.version_descriptor.version_ref
                for entry in entries
            }
            for entry in entries:
                descriptor = entry.presentation_asset.version_descriptor
                if (
                    descriptor.identity_policy is None and descriptor.edition.kind == "structural_default"
                    and descriptor.technical_variant_key == "default"
                ):
                    all_segmented = all(
                        item.presentation_asset.segment is not None
                        for item in entries
                    )
                    if (
                        len(refs) != 1
                        or (len(entries) != 1 and not all_segmented)
                    ):
                        issues.append(PlanIssue(
                            "presentation_structural_default_not_unique",
                            "多个正片版本中有视频尚未明确规格；请在该文件的版本设置中确认分辨率或版本名称",
                            entry.source_path,
                            entry.target_path,
                        ))
                elif len(refs) > 1 and not descriptor.display_label:
                    issues.append(PlanIssue(
                        "presentation_version_label_missing",
                        "多版本内容必须具有非空且唯一的显示标签",
                        entry.source_path,
                        entry.target_path,
                    ))

        for group_id, version_entries in versions_by_group.items():
            group = groups[group_id]
            refs = set(version_entries)
            preferred = group.preferred_version_ref
            if preferred is not None and (
                group.kind != "movie_continuous" or preferred not in refs
            ):
                issues.append(PlanIssue(
                    "presentation_preferred_version_invalid",
                    f"分组 {group_id} 的 preferred_version_ref 无效",
                ))
            for version_ref, entries in version_entries.items():
                descriptor_shapes = {
                    json.dumps(
                        entry.presentation_asset.version_descriptor.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    for entry in entries
                }
                if len(descriptor_shapes) != 1:
                    issues.append(PlanIssue(
                        "presentation_shared_version_descriptor_drift",
                        f"共享 version_ref {version_ref} 的版本字段发生漂移",
                    ))
                segments = [
                    entry.presentation_asset.segment
                    for entry in entries
                    if entry.presentation_asset.segment is not None
                ]
                if segments:
                    totals = {segment.total for segment in segments}
                    indexes = {segment.index for segment in segments}
                    schemes = {segment.scheme for segment in segments}
                    if (
                        len(segments) != len(entries)
                        or len(totals) != 1
                        or len(schemes) != 1
                        or indexes != set(range(1, next(iter(totals)) + 1))
                    ):
                        issues.append(PlanIssue(
                            "presentation_segments_incomplete",
                            f"版本 {version_ref} 的分段不连续或 total 不一致",
                        ))
                elif len(entries) > 1:
                    issues.append(PlanIssue(
                        "presentation_duplicate_version_leaf",
                        f"版本 {version_ref} 存在多个未声明分段的内容文件",
                    ))

        for group_id, group in groups.items():
            if group_id not in versions_by_group:
                issues.append(PlanIssue(
                    "presentation_group_content_missing",
                    f"呈现分组 {group_id} 没有可执行的内容版本",
                ))
            elif (
                group.preferred_version_ref is not None
                and group.preferred_version_ref not in versions_by_group[group_id]
            ):
                issues.append(PlanIssue(
                    "presentation_preferred_version_invalid",
                    f"分组 {group_id} 的 preferred_version_ref 无效",
                ))

    def _verify_version_descriptor(
        self,
        entry: PlanEntry,
        descriptor: VersionDescriptor,
        issues: List[PlanIssue],
        *, plan=None,
    ) -> None:
        assert entry.presentation_asset is not None
        version = entry.presentation_asset.version_descriptor
        assert version is not None
        from .special_selection import DESCRIPTIVE_POLICY, validate_descriptive_special
        if version.identity_policy == DESCRIPTIVE_POLICY:
            if not validate_descriptive_special(plan, entry):
                issues.append(PlanIssue('descriptive_special_invalid', '描述性特别篇与冻结人工决定不一致', entry.source_path, entry.target_path))
            return
        from .episode_versions import POLICY as TV_VERSION_POLICY, validate_projection as validate_versions
        if version.identity_policy == TV_VERSION_POLICY:
            try:
                validate_versions(plan)
            except (ValueError, TypeError, KeyError) as exc:
                issues.append(PlanIssue('episode_versions_invalid', str(exc), entry.source_path, entry.target_path))
            return
        from .movie_version_selection import POLICY, safe_label
        if version.identity_policy is not None:
            if version.identity_policy != POLICY or not re.fullmatch(r'mv-[0-9a-f]{32}', str(version.version_ref)):
                issues.append(PlanIssue('movie_version_identity_invalid', '电影版本内部身份或合同版本无效', entry.source_path, entry.target_path))
            try:
                if safe_label(version.display_label) != version.display_label:
                    raise ValueError('电影版本标签未规范化')
            except ValueError as exc:
                issues.append(PlanIssue('movie_version_label_invalid', str(exc), entry.source_path, entry.target_path))
            for name in ('edition', 'resolution', 'dynamic_range', 'key_audio'):
                self._verify_evidence(entry, name, getattr(version, name), issues)
            return
        if not version.version_ref or not version.technical_variant_key:
            issues.append(PlanIssue(
                "presentation_version_identity_missing",
                "内容版本缺少 version_ref 或 technical_variant_key",
                entry.source_path,
                entry.target_path,
            ))
        fields = (
            ("edition", version.edition),
            ("resolution", version.resolution),
            ("dynamic_range", version.dynamic_range),
            ("key_audio", version.key_audio),
        )
        for field_name, evidence in fields[1:]:
            if evidence.value is not None and not isinstance(evidence.value, str):
                issues.append(PlanIssue(
                    "presentation_evidence_value_type_invalid",
                    f"{field_name} 的 value 必须是字符串或 null",
                    entry.source_path,
                    entry.target_path,
                ))
        for field_name, evidence in fields:
            self._verify_evidence(entry, field_name, evidence, issues)
        if not isinstance(version.edition.value, EditionValue):
            issues.append(PlanIssue(
                "presentation_edition_value_invalid",
                "edition 的 resolved value 必须是 EditionValue",
                entry.source_path,
                entry.target_path,
            ))
        structural_default = (
            version.edition.kind == "structural_default"
            and isinstance(version.edition.value, EditionValue)
            and version.edition.value.key == "default"
            and version.technical_variant_key == "default"
            and all(
                evidence.status == "unknown"
                and evidence.value is None
                and evidence.selected_source is None
                and not evidence.candidates
                for evidence in (
                    version.resolution,
                    version.dynamic_range,
                    version.key_audio,
                )
            )
        )
        if not structural_default:
            if version.edition.status != "resolved":
                issues.append(PlanIssue(
                    "presentation_evidence_unresolved",
                    "非默认内容版本的 edition 必须在批准前解决",
                    entry.source_path,
                    entry.target_path,
                ))
            technical_fields = (
                version.resolution, version.dynamic_range, version.key_audio
            )
            if not any(item.status == "resolved" for item in technical_fields):
                issues.append(PlanIssue(
                    "presentation_technical_identity_missing",
                    "非默认内容版本至少需要一个已确认的技术规格",
                    entry.source_path,
                    entry.target_path,
                ))
        edition_value = version.edition.value
        try:
            expected_label = format_presentation_version_label(
                edition_value.label
                if isinstance(edition_value, EditionValue)
                and edition_value.key != "default" else None,
                version.resolution.value if version.resolution.status == "resolved" else None,
                version.dynamic_range.value if version.dynamic_range.status == "resolved" else None,
                version.key_audio.value if version.key_audio.status == "resolved" else None,
            )
        except ValueError:
            issues.append(PlanIssue(
                "presentation_version_label_reserved",
                "版本标签不得包含呈现分组保留词",
                entry.source_path,
                entry.target_path,
            ))
        else:
            if version.display_label != expected_label:
                issues.append(PlanIssue(
                    "presentation_version_label_mismatch",
                    "display_label 与统一 formatter 重算结果不一致",
                    entry.source_path,
                    entry.target_path,
                ))

    def _verify_evidence(
        self,
        entry: PlanEntry,
        field_name: str,
        evidence: EvidenceValue,
        issues: List[PlanIssue],
    ) -> None:
        allowed_sources = {"media_probe", "filename_hint", "manual_confirmation"}
        if evidence.status not in {"unknown", "resolved", "conflict"}:
            issues.append(PlanIssue(
                "presentation_evidence_status_invalid",
                f"{field_name} 的证据状态无效",
                entry.source_path,
                entry.target_path,
            ))
            return
        if evidence.status in {"unknown", "conflict"}:
            if evidence.value is not None or evidence.selected_source is not None:
                issues.append(PlanIssue(
                    "presentation_evidence_unresolved_has_value",
                    f"{field_name} 未解决时 value/selected_source 必须为空",
                    entry.source_path,
                    entry.target_path,
                ))
            if evidence.status == "unknown" and evidence.candidates:
                issues.append(PlanIssue(
                    "presentation_evidence_unknown_has_candidates",
                    f"{field_name} 为 unknown 时不得携带 candidates",
                    entry.source_path,
                    entry.target_path,
                ))
            if evidence.status == "conflict" and len(evidence.candidates) < 2:
                issues.append(PlanIssue(
                    "presentation_evidence_conflict_incomplete",
                    f"{field_name} 冲突必须保留至少两个候选",
                    entry.source_path,
                    entry.target_path,
                ))
            if evidence.status == "conflict":
                issues.append(PlanIssue(
                    "presentation_evidence_conflict_unresolved",
                    f"{field_name} 的证据冲突必须在批准前解决",
                    entry.source_path,
                    entry.target_path,
                ))
        else:
            structural_default = (
                field_name == "edition"
                and evidence.kind == "structural_default"
                and isinstance(evidence.value, EditionValue)
                and evidence.value.key == "default"
                and evidence.value.label is None
                and evidence.selected_source is None
                and not evidence.candidates
            )
            if evidence.value is None:
                issues.append(PlanIssue(
                    "presentation_evidence_value_missing",
                    f"{field_name} 已解决但缺少 value",
                    entry.source_path,
                    entry.target_path,
                ))
            if not structural_default and evidence.selected_source is None:
                issues.append(PlanIssue(
                    "presentation_evidence_source_missing",
                    f"{field_name} 已解决但缺少 selected_source",
                    entry.source_path,
                    entry.target_path,
                ))
            if evidence.kind == "structural_default" and not structural_default:
                issues.append(PlanIssue(
                    "presentation_structural_default_invalid",
                    "结构默认哨兵字段形状不合法",
                    entry.source_path,
                    entry.target_path,
                ))
        for candidate in evidence.candidates:
            if candidate.source not in allowed_sources:
                issues.append(PlanIssue(
                    "presentation_evidence_candidate_source_invalid",
                    f"{field_name} 候选来源无效：{candidate.source}",
                    entry.source_path,
                    entry.target_path,
                ))
        if evidence.status == "resolved" and evidence.selected_source is not None:
            selected_candidates = [
                candidate for candidate in evidence.candidates
                if candidate.source == evidence.selected_source
            ]
            if selected_candidates and not any(
                candidate.value == evidence.value for candidate in selected_candidates
            ):
                issues.append(PlanIssue(
                    "presentation_evidence_selected_value_mismatch",
                    f"{field_name} 的 value 与 selected_source 候选不一致",
                    entry.source_path,
                    entry.target_path,
                ))
        if evidence.selected_source is not None:
            if evidence.selected_source not in allowed_sources or not any(
                candidate.source == evidence.selected_source
                for candidate in evidence.candidates
            ):
                issues.append(PlanIssue(
                    "presentation_evidence_selected_source_invalid",
                    f"{field_name} selected_source 没有对应候选",
                    entry.source_path,
                    entry.target_path,
                ))
            if evidence.selected_source == "manual_confirmation" and not (
                evidence.manual_reason and evidence.manual_reason.strip()
            ):
                issues.append(PlanIssue(
                    "presentation_manual_reason_missing",
                    f"{field_name} 人工确认必须填写理由",
                    entry.source_path,
                    entry.target_path,
                ))

    def _verify_subtitle_entry(
        self,
        entry: PlanEntry,
        contract: object,
        video_sources: Dict[str, List[PlanEntry]],
        rules_by_id: Dict[str, SubtitleOverrideRule],
        referenced_rule_ids: set[str],
        presentation_group_ids: set[str],
        issues: List[PlanIssue],
        *, inherited=None,
    ) -> None:
        # The caller guarantees this exact schema type through PlanEntry.
        subtitle = entry.subtitle
        assert subtitle is not None
        candidate_keys = tuple(
            _path_key(path) for path in subtitle.association_candidates
        )
        if len(set(candidate_keys)) != len(candidate_keys):
            issues.append(PlanIssue(
                "subtitle_association_candidates_duplicate",
                "字幕关联候选不得重复",
                entry.source_path,
                entry.target_path,
            ))
        candidate_groups = set()
        for candidate_path in subtitle.association_candidates:
            candidates = video_sources.get(_path_key(candidate_path), [])
            if len(candidates) != 1:
                issues.append(PlanIssue(
                    "subtitle_association_candidate_missing",
                    "字幕关联候选必须唯一指向当前整理计划的视频",
                    entry.source_path,
                    entry.target_path,
                ))
                continue
            candidate_asset = candidates[0].presentation_asset
            if candidate_asset is not None:
                candidate_groups.add(candidate_asset.presentation_group)
        if (
            len(candidate_groups) > 1
            and subtitle.association_state != DimensionResolution.CONFLICT
        ):
            issues.append(PlanIssue(
                "subtitle_cross_group_ambiguity_not_preserved",
                "跨呈现分组的字幕候选必须保持 conflict，禁止自动选边",
                entry.source_path,
                entry.target_path,
            ))
        if (
            subtitle.association_state == DimensionResolution.CONFLICT
            and len(subtitle.association_candidates) < 2
        ):
            issues.append(PlanIssue(
                "subtitle_association_conflict_candidates_missing",
                "字幕关联冲突必须保留至少两个候选",
                entry.source_path,
                entry.target_path,
            ))
        if (
            subtitle.association_state == DimensionResolution.RESOLVED
            and subtitle.associated_video_source_path is not None
            and subtitle.association_candidates
            and _path_key(subtitle.associated_video_source_path) not in candidate_keys
        ):
            issues.append(PlanIssue(
                "subtitle_association_selection_not_candidate",
                "已解决的字幕关联必须选择保留候选之一",
                entry.source_path,
                entry.target_path,
            ))
        if subtitle.association_state != DimensionResolution.RESOLVED:
            issues.append(PlanIssue(
                "subtitle_association_unresolved",
                "字幕关联尚未结构化解决，普通批准不能越过",
                entry.source_path,
                entry.target_path,
            ))
        if subtitle.parse_state != DimensionResolution.RESOLVED:
            issues.append(PlanIssue(
                "subtitle_parse_unresolved",
                "字幕标签尚未结构化解决，普通批准不能越过",
                entry.source_path,
                entry.target_path,
            ))
        if subtitle.association_rule_version != SUBTITLE_ASSOCIATION_RULE_VERSION:
            issues.append(PlanIssue(
                "subtitle_association_rule_stale",
                "字幕关联规则版本已过期，必须重新生成 Plan",
                entry.source_path,
                entry.target_path,
            ))
        if subtitle.label_rule_version != SUBTITLE_LABEL_RULE_VERSION:
            issues.append(PlanIssue(
                "subtitle_label_rule_stale",
                "字幕标签规则版本已过期，必须重新生成 Plan",
                entry.source_path,
                entry.target_path,
            ))

        matching_videos = (
            video_sources.get(_path_key(subtitle.associated_video_source_path), [])
            if subtitle.associated_video_source_path is not None else []
        )
        if len(matching_videos) != 1:
            issues.append(PlanIssue(
                "subtitle_video_entry_not_unique",
                "字幕关联的视频源在 Plan 中不存在或不唯一",
                entry.source_path,
                entry.target_path,
            ))
        else:
            video = matching_videos[0]
            video_asset = video.presentation_asset
            valid_video_role = (
                video_asset is not None
                and (
                    video_asset.asset_role.kind == "content_version"
                    or (
                        entry.role == "extra_subtitle"
                        and video_asset.asset_role.kind in {"extra", "unknown"}
                    )
                )
            )
            if (
                video_asset is None
                or not valid_video_role
                or video_asset.presentation_group not in presentation_group_ids
            ):
                issues.append(PlanIssue(
                    "subtitle_video_not_content_version",
                    "字幕只能关联到当前 Plan 中有有效呈现分组的内容版本或特典视频",
                    entry.source_path,
                    entry.target_path,
                ))
            try:
                expected = format_subtitle_target_fields(
                    target_video=video.target_path,
                    source_subtitle=entry.source_path,
                    primary_language=subtitle.primary_language or "",
                    script=subtitle.script,
                    track_title_tokens=subtitle.track_title_tokens,
                    flags=subtitle.flags,
                )
            except ValueError:
                expected = None
                issues.append(PlanIssue(
                    "subtitle_title_unsafe",
                    "字幕轨道标题含文件系统不安全字符",
                    entry.source_path,
                    entry.target_path,
                ))
            if (
                expected is not None
                and _path_key(expected) != _path_key(entry.target_path)
            ):
                issues.append(PlanIssue(
                    "subtitle_target_not_canonical",
                    f"字幕目标不等于统一 formatter 结果：{expected}",
                    entry.source_path,
                    entry.target_path,
                ))

        source_suffix = _normalized_suffix(entry.source_path)
        target_suffix = _normalized_suffix(entry.target_path)
        if source_suffix not in SUBTITLE_SUFFIXES or target_suffix != source_suffix:
            issues.append(PlanIssue(
                "subtitle_extension_changed",
                "字幕扩展名必须受支持且保持不变",
                entry.source_path,
                entry.target_path,
            ))
        self._verify_subtitle_metadata(entry, rules_by_id, referenced_rule_ids, issues, inherited=inherited)

    def _verify_subtitle_metadata(self, entry, rules_by_id, referenced_rule_ids, issues, *, inherited=None):
        """Shared rule/label semantics, independent of output collisions."""
        subtitle = entry.subtitle
        assert subtitle is not None
        from .inherited_subtitle_edits import InheritedPlanProof
        if subtitle.decision_source == 'manual_transfer':
            if (not subtitle.inherited_edit_fingerprint or type(inherited) is not InheritedPlanProof
                    or dict(inherited.contracts).get(entry.source_path) != subtitle):
                issues.append(PlanIssue('subtitle_inherited_reference_invalid',
                    '字幕继承引用缺少同源有限材料证明', entry.source_path, entry.target_path))
        elif subtitle.inherited_edit_fingerprint:
            issues.append(PlanIssue('subtitle_inherited_reference_unexpected',
                '非继承字幕不能携带有限引用', entry.source_path, entry.target_path))
        from .subtitle_languages import format_language, language_key, multilingual_title
        try:
            main = format_language(subtitle.primary_language, subtitle.script)
            if any(language_key(v) != v for v in subtitle.additional_languages):
                raise ValueError('附加语言代码未规范化')
            multilingual_title(main, subtitle.additional_languages)
        except ValueError as exc:
            issues.append(PlanIssue('subtitle_primary_language_invalid', str(exc), entry.source_path, entry.target_path))
        if tuple(flag for flag in ("default", "forced", "sdh") if flag in subtitle.flags) != subtitle.flags:
            issues.append(PlanIssue(
                "subtitle_flags_invalid",
                "字幕 flags 必须唯一且按 default/forced/sdh 固定排序",
                entry.source_path,
                entry.target_path,
            ))
        if subtitle.primary_language in subtitle.additional_languages or len(set(subtitle.additional_languages)) != len(subtitle.additional_languages):
            issues.append(PlanIssue(
                "subtitle_additional_languages_invalid",
                "附加语言不得与主语言重复或内部重复",
                entry.source_path,
                entry.target_path,
            ))

        canonical_unknown = tuple(
            _canonical_token(token) for token in subtitle.unknown_tokens
        )
        canonical_dispositions = tuple(
            _canonical_token(item.token) for item in subtitle.unknown_dispositions
        )
        if len(set(canonical_unknown)) != len(canonical_unknown):
            issues.append(PlanIssue(
                "subtitle_unknown_token_duplicate",
                "未知 token 存在重复或规范等价项，必须重新解析后人工处置",
                entry.source_path,
                entry.target_path,
            ))
        if (
            len(canonical_dispositions) != len(canonical_unknown)
            or set(canonical_dispositions) != set(canonical_unknown)
            or len(set(canonical_dispositions)) != len(canonical_dispositions)
        ):
            issues.append(PlanIssue(
                "subtitle_unknown_disposition_missing",
                "每个未知 token 都必须有明确人工处置",
                entry.source_path,
                entry.target_path,
            ))
        for item in subtitle.unknown_dispositions:
            if item.action == UnknownTokenAction.DISCARD_MANUAL and not item.reason.strip():
                issues.append(PlanIssue(
                    "subtitle_unknown_discard_reason_missing",
                    "删除未知 token 必须填写理由",
                    entry.source_path,
                    entry.target_path,
                ))
            if (
                item.action == UnknownTokenAction.PRESERVE_AS_TITLE
                and _canonical_token(item.token) not in {
                    _canonical_token(token) for token in subtitle.track_title_tokens
                }
            ):
                issues.append(PlanIssue(
                    "subtitle_preserved_unknown_not_in_title",
                    "保留的未知 token 必须出现在轨道标题中",
                    entry.source_path,
                    entry.target_path,
                ))
        if subtitle.decision_source not in {
            "automatic", "manual", "manual_file", "manual_series", "manual_transfer"
        }:
            issues.append(PlanIssue(
                "subtitle_decision_source_invalid",
                "字幕 decision_source 必须是 automatic 或 manual",
                entry.source_path,
                entry.target_path,
            ))
        if subtitle.decision_source.startswith("manual") and not subtitle.manual_reason.strip():
            issues.append(PlanIssue(
                "subtitle_manual_reason_missing",
                "字幕人工决定必须填写理由",
                entry.source_path,
                entry.target_path,
            ))

        provenance = (
            subtitle.applied_rule_id,
            subtitle.applied_rule_fingerprint,
            subtitle.applied_rule_scope,
            subtitle.applied_rule_engine_version,
        )
        if subtitle.decision_source in {"manual_file", "manual_series"}:
            rule = rules_by_id.get(subtitle.applied_rule_id)
            expected_scope = subtitle.decision_source.removeprefix("manual_")
            if rule is None:
                issues.append(PlanIssue(
                    "subtitle_applied_rule_missing",
                    "字幕人工规则来源在当前 Plan 中不存在或不唯一",
                    entry.source_path,
                    entry.target_path,
                ))
            else:
                referenced_rule_ids.add(rule.rule_id)
                matcher_matches = (
                    _canonical_token(subtitle.raw_label)
                    == rule.matcher.raw_label_canonical
                    and canonical_unknown == rule.matcher.unknown_tokens_canonical
                    and subtitle.association_rule_version
                    == rule.matcher.association_rule_version
                    and subtitle.label_rule_version == rule.matcher.label_rule_version
                )
                outcome_matches = (
                    subtitle.primary_language == rule.outcome.primary_language
                    and subtitle.script == rule.outcome.script
                    and subtitle.region_hint == rule.outcome.region_hint
                    and subtitle.additional_languages
                    == rule.outcome.additional_languages
                    and subtitle.flags == rule.outcome.flags
                )
                file_entry_matches = (
                    rule.scope != SubtitleOverrideScope.FILE
                    or _entry_id(entry) == rule.selected_entry_id
                )
                if (
                    subtitle.applied_rule_fingerprint != rule.rule_fingerprint
                    or subtitle.applied_rule_scope != rule.scope.value
                    or subtitle.applied_rule_scope != expected_scope
                    or subtitle.applied_rule_engine_version != rule.engine_version
                    or not matcher_matches
                    or not outcome_matches
                    or not file_entry_matches
                ):
                    issues.append(PlanIssue(
                        "subtitle_applied_rule_mismatch",
                        "字幕条目的人工规则来源、匹配器或结果与 Plan 内规则不一致",
                        entry.source_path,
                        entry.target_path,
                    ))
        elif any(provenance):
            issues.append(PlanIssue(
                "subtitle_rule_provenance_unexpected",
                "非 S4 人工规则字幕不得携带规则来源字段",
                entry.source_path,
                entry.target_path,
            ))

    def _verify_material_rule_references(self, plan, rules_by_id, referenced_rule_ids, issues, visible_sources, *, inherited=None):
        from .content_selection import from_plan
        choice = from_plan(plan)
        if choice is None:
            return
        for row in choice.to_dict()['materials']:
            if row['media_kind'] != 'subtitle' or Path(row['source_path']) in visible_sources:
                continue
            entry = PlanEntry.from_dict({**row, 'target_path': row['source_path'], 'collision_resolution': None})
            contract = entry.subtitle
            if contract is not None and (contract.decision_source in {'manual_file', 'manual_series', 'manual_transfer'}
                    or any((contract.applied_rule_id, contract.applied_rule_fingerprint,
                            contract.applied_rule_scope, contract.applied_rule_engine_version, contract.inherited_edit_fingerprint))):
                self._verify_subtitle_metadata(entry, rules_by_id, referenced_rule_ids, issues, inherited=inherited)

    def _verify_inherited_subtitles(self, plan, issues):
        from .inherited_subtitle_edits import validate_plan, InheritedPlanProof, POLICY
        try:
            result = validate_plan(plan)
            raw = plan.metadata.get('content_selection')
            from .source_decision_transfer import ORIGIN
            if isinstance(raw, Mapping) and (raw.get('policy') == POLICY or raw.get('origin_kind') == ORIGIN):
                if (type(result) is not InheritedPlanProof or result.task_id != plan.task_id
                        or result.content_fingerprint != plan.metadata.get('content_selection_fingerprint')):
                    raise ValueError('有限字幕共同验证结果已断开或不属于本计划')
            return result
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(PlanIssue('subtitle_inherited_content_invalid', str(exc)))
            return None

    def verify_subtitle_rule_materials(self, plan):
        """Canonical content consumer; no hidden output or collision checks."""
        issues = []
        rules = self._verify_subtitle_rules(plan, issues)
        inherited = self._verify_inherited_subtitles(plan, issues)
        self._verify_material_rule_references(plan, rules, set(), issues, set(), inherited=inherited)
        return tuple(issues)

    def _verify_subtitle_rules(
        self, plan: RenamePlan, issues: List[PlanIssue], *, invalid_rule_ids=None
    ) -> Dict[str, SubtitleOverrideRule]:
        rules_by_id: Dict[str, SubtitleOverrideRule] = {}
        fingerprints: set[str] = set()
        for rule in plan.subtitle_override_rules:
            start = len(issues)
            if not rule.rule_id or rule.rule_id in rules_by_id:
                issues.append(PlanIssue(
                    "subtitle_rule_id_invalid",
                    "字幕人工规则 ID 为空或重复",
                ))
            else:
                rules_by_id[rule.rule_id] = rule
            if rule.schema_version != SUBTITLE_OVERRIDE_RULE_SCHEMA_VERSION:
                issues.append(PlanIssue(
                    "subtitle_rule_schema_stale",
                    "字幕人工规则 schema 已过期，必须重新确认",
                ))
            if rule.engine_version != SUBTITLE_OVERRIDE_ENGINE_VERSION:
                issues.append(PlanIssue(
                    "subtitle_rule_engine_stale",
                    "字幕人工规则引擎版本已过期，必须重新确认",
                ))
            if (
                rule.matcher.association_rule_version
                != SUBTITLE_ASSOCIATION_RULE_VERSION
                or rule.matcher.label_rule_version != SUBTITLE_LABEL_RULE_VERSION
            ):
                issues.append(PlanIssue(
                    "subtitle_rule_matcher_stale",
                    "字幕人工规则匹配器版本已过期，必须重新确认",
                ))
            if not rule.reason.strip():
                issues.append(PlanIssue(
                    "subtitle_rule_reason_missing",
                    "字幕人工规则必须记录理由",
                ))
            calculated = _rule_fingerprint(rule)
            if not rule.rule_fingerprint or rule.rule_fingerprint != calculated:
                issues.append(PlanIssue(
                    "subtitle_rule_fingerprint_mismatch",
                    "字幕人工规则 fingerprint 不自洽",
                ))
            if rule.rule_fingerprint in fingerprints:
                issues.append(PlanIssue(
                    "subtitle_rule_fingerprint_duplicate",
                    "当前 Plan 含重复字幕人工规则",
                ))
            fingerprints.add(rule.rule_fingerprint)
            if rule.scope == SubtitleOverrideScope.SERIES:
                if plan.series_scope is None or rule.scope_id != plan.series_scope.scope_id:
                    issues.append(PlanIssue(
                        "subtitle_rule_series_scope_mismatch",
                        "系列字幕规则缺少稳定 TMDB TV 身份或作用域不匹配",
                    ))
            if invalid_rule_ids is not None and len(issues) > start:
                invalid_rule_ids.add(rule.rule_id)
        return rules_by_id


def _path_key(path: Path) -> str:
    """Compare paths with Windows/NAS-safe case folding without resolving."""

    return os.path.normpath(str(path)).casefold()


def _path_within(path: Path, root: Path) -> bool:
    """Return whether *path* is lexically contained by *root*."""

    candidate = _path_key(path)
    anchor = _path_key(root)
    try:
        return os.path.commonpath((candidate, anchor)) == anchor
    except ValueError:
        return False


def _normalized_suffix(path: Path) -> str:
    name = unicodedata.normalize("NFKC", path.name)
    dot = name.rfind(".")
    return name[dot:].casefold() if dot >= 0 else ""


def _canonical_token(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _canonical_label(value: Optional[str]) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _rule_fingerprint(rule: SubtitleOverrideRule) -> str:
    payload = json.dumps(
        rule.unsigned_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _entry_id(entry: PlanEntry) -> str:
    identity = os.path.normpath(str(entry.source_path)).casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
