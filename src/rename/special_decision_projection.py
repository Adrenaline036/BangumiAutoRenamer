"""Pure projection of explicit special decisions and typed video dependencies.

No Provider, filesystem or persistence access is allowed in this module.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import hashlib
import os
from pathlib import Path

from .episode_inference import normalize_target_stem
from .rename_plan import (
    AssetRole, PlanEntry, PresentationAssetPlanContract, RenamePlan,
    build_default_version_descriptor,
)
from .utils import JELLYFIN_EXTRAS_DIR


class SpecialDecisionProjectionError(ValueError):
    pass


def source_key(path: Path) -> str:
    return os.path.normpath(str(path)).casefold()


def special_entry_id(entry: PlanEntry) -> str:
    return hashlib.sha256(source_key(entry.source_path).encode("utf-8")).hexdigest()[:20]


def video_bundle_closure(plan: RenamePlan, identifier: str) -> tuple[tuple[int, PlanEntry], ...]:
    """One video plus exact typed subtitles and unambiguous associated assets."""
    keys = [source_key(e.source_path) for e in plan.entries]
    if len(keys) != len(set(keys)):
        raise SpecialDecisionProjectionError("视频依赖闭包包含重复来源")
    matches = [(i, e) for i, e in enumerate(plan.entries) if special_entry_id(e) == identifier]
    if len(matches) != 1:
        raise SpecialDecisionProjectionError("待确认特别篇文件不存在或不唯一")
    index, video = matches[0]
    if video.media_kind != "video":
        raise SpecialDecisionProjectionError("特别篇决定只能应用于视频条目")
    asset = video.presentation_asset
    if asset is None or not any(g.group_id == asset.presentation_group for g in plan.presentation_groups):
        raise SpecialDecisionProjectionError("待确认特别篇缺少有效展示分组")
    from .content_associations import from_plan, associated_source_closure
    associations = from_plan(plan)
    if associations is not None:
        closure = set(associated_source_closure(associations, [video.source_path]))
        return ((index, video),) + tuple((i, e) for i, e in enumerate(plan.entries)
            if i != index and e.source_path in closure)
    result = [(index, video)]
    videos = [e for e in plan.entries if e.media_kind == "video" and e.presentation_asset is not None]
    for i, entry in enumerate(plan.entries):
        if i == index:
            continue
        if entry.subtitle is not None:
            linked = entry.subtitle.associated_video_source_path
            if linked is not None and source_key(linked) == source_key(video.source_path):
                if entry.presentation_asset is not None:
                    raise SpecialDecisionProjectionError("字幕不得携带 presentation_asset，必须从关联视频派生")
                result.append((i, entry))
            continue
        if entry.media_kind != "associated" or entry.presentation_asset is None:
            continue
        owners = [v for v in videos
                  if v.presentation_asset.presentation_group == entry.presentation_asset.presentation_group
                  and source_key(v.target_path.parent) == source_key(entry.target_path.parent)
                  and v.target_path.stem.casefold() == entry.target_path.stem.casefold()]
        if video in owners:
            if len(owners) != 1:
                raise SpecialDecisionProjectionError("配套音轨或资产的视频归属不唯一")
            result.append((i, entry))
    return tuple(result)


def _transferred_ids(plan: RenamePlan) -> set[str]:
    rows = plan.metadata.get("source_partition_decisions", ())
    return {str(identifier) for row in rows if isinstance(row, Mapping) and row.get("action") == "cross_work"
            for identifier in row.get("selected_entry_ids", ())} if isinstance(rows, (list, tuple)) else set()


def project_special_decisions(plan: RenamePlan, decisions: Mapping[str, object]) -> RenamePlan:
    """Reapply every owned explicit decision after work/root/season are frozen."""
    if not isinstance(decisions, Mapping):
        raise SpecialDecisionProjectionError("特别篇决定集合无效")
    if not decisions:
        return plan
    from . import content_selection
    selection = content_selection.from_plan(plan)
    if selection is not None:
        # Excluded official/explicit sources still supply current projection facts.
        plan = replace(plan, entries=content_selection.material_entries(plan, selection))
    if {g.kind for g in plan.presentation_groups} != {"tv_episodes"}:
        raise SpecialDecisionProjectionError("特别篇决定只适用于按季和集整理")
    root = Path(str(plan.metadata.get("canonical_work_root") or plan.metadata.get("target_root") or ""))
    if not root.is_absolute():
        raise SpecialDecisionProjectionError("当前计划缺少绝对作品目标目录")
    current = plan
    present = {special_entry_id(e) for e in plan.entries}
    transferred = _transferred_ids(plan)
    for identifier, raw in decisions.items():
        if identifier not in present:
            if identifier in transferred:
                continue
            raise SpecialDecisionProjectionError("特别篇决定对应文件已变化")
        if not isinstance(raw, Mapping):
            raise SpecialDecisionProjectionError("特别篇决定无效")
        action = raw.get("action")
        if action not in {"unmatched_extra", "confirmed_official_special", 'confirmed_special_correspondence', 'descriptive_special', 'bangumi_special'}:
            raise SpecialDecisionProjectionError("特别篇决定无效")
        closure = video_bundle_closure(current, str(identifier))
        index, video = closure[0]
        group = video.presentation_asset.presentation_group
        if action == 'bangumi_special':
            from .special_selection import build_bangumi_special
            target, descriptor = build_bangumi_special(plan, identifier, raw)
            season, episode, role = 0, raw['local_episode'], 'main_episode'
            asset = PresentationAssetPlanContract(group, AssetRole('content_version'), None, descriptor)
        elif action == 'descriptive_special':
            from .special_selection import build_descriptive_special
            target, descriptor = build_descriptive_special(plan, identifier, raw)
            season, episode, role = 0, None, 'main_episode'
            asset = PresentationAssetPlanContract(group, AssetRole('content_version'), None, descriptor)
        elif action == 'confirmed_special_correspondence':
            from .special_selection import validate_correspondence
            row = validate_correspondence(plan, raw, identifier)
            season, episode, role = 0, row['episode_number'], 'main_episode'
            target = content_selection.canonical_episode_target(root, season, episode, video.source_path.suffix)
            descriptor = build_default_version_descriptor(group, season, episode)
            asset = PresentationAssetPlanContract(group, AssetRole('content_version'), None, descriptor)
        elif action == "confirmed_official_special":
            from .provider_special_evidence import official_special_rows
            rows = official_special_rows(plan, identifier, include_omitted=True)
            if not isinstance(rows, (list, tuple)) or not rows:
                raise SpecialDecisionProjectionError("当前文件没有冻结的官方特别篇候选")
            fields = ("provider_namespace", "provider_entity_type", "provider_id", "season_number", "episode_number", "provider_episode_id")
            season, episode = raw.get("season_number"), raw.get("episode_number")
            if (type(season) is not int or season != 0 or type(episode) is not int or episode < 1
                    or any(not isinstance(raw.get(k), str) or not raw[k].strip() for k in fields if k not in {"season_number", "episode_number"})
                    or not any(isinstance(row, Mapping) and all(row.get(k) == raw.get(k) for k in fields) for row in rows)):
                raise SpecialDecisionProjectionError("所选官方特别篇候选已变化，请刷新后重试")
            target = content_selection.canonical_episode_target(root, season, episode, video.source_path.suffix)
            descriptor = build_default_version_descriptor(group, season, episode)
            role = "main_episode"
            asset = PresentationAssetPlanContract(group, AssetRole("content_version"), None, descriptor)
        else:
            if set(raw) != {"action"}:
                raise SpecialDecisionProjectionError("附加内容决定不得混入官方特别篇身份")
            season = plan.metadata.get("season_number", 1)
            if type(season) is not int or season < 0:
                raise SpecialDecisionProjectionError("当前计划季度无效")
            episode, descriptor, role = None, None, "disc_extra"
            stem = normalize_target_stem(video.source_path.stem, root.name)
            target = root / f"Season {season:02d}" / JELLYFIN_EXTRAS_DIR / f"{stem or video.source_path.stem}{video.source_path.suffix.lower()}"
            asset = PresentationAssetPlanContract(group, AssetRole("extra", "other"), None, None)
        entries = list(current.entries)
        entries[index] = replace(video, target_path=target, season_id=season, episode_id=episode, episode_end=None,
                                 role=role, requires_review=True, presentation_asset=asset,
                                 collision_resolution=video.collision_resolution if video.target_path == target else None)
        for i, entry in closure[1:]:
            subtitle = entry.subtitle
            from .content_associations import subtitle_target
            linked_target = (subtitle_target(entry, subtitle, target) if subtitle is not None
                             else target.with_suffix(entry.source_path.suffix.lower()))
            linked_asset = None if subtitle is not None else PresentationAssetPlanContract(
                group, AssetRole("associated"), None, None,
                descriptor.version_ref if descriptor is not None else None,
            )
            entries[i] = replace(entry, target_path=linked_target, season_id=season, episode_id=episode, episode_end=None,
                                 requires_review=True, presentation_asset=linked_asset,
                                 collision_resolution=entry.collision_resolution if entry.target_path == linked_target else None)
        current = replace(current, entries=tuple(entries))
    # Catalog replay can move a video after the first audio projection. Derive
    # audio again from the final video authority, not its previous target stem.
    from .audio_projection import project_audio
    from .presentation import associate_resolved_extra_subtitles
    result = associate_resolved_extra_subtitles(project_audio(replace(current,
        metadata={**current.metadata, "provider_special_decisions": dict(decisions)})))
    return content_selection.filter_projection(result, selection, None) if selection is not None else result


def special_projection_signature(entry: PlanEntry) -> tuple:
    return (entry.target_path, entry.role, entry.season_id, entry.episode_id,
            entry.presentation_asset, entry.subtitle, entry.episode_end)


def validated_special_decisions(plan: RenamePlan) -> tuple[set[str], str | None]:
    """Verifier side: never infer acceptance from a decision dictionary key."""
    decisions = plan.metadata.get("provider_special_decisions", {})
    if not decisions:
        return set(), None
    try:
        expected = project_special_decisions(plan, decisions)
    except (SpecialDecisionProjectionError, TypeError, ValueError):
        return set(), "provider_special_decision_invalid"
    if any(special_projection_signature(a) != special_projection_signature(b)
           for a, b in zip(plan.entries, expected.entries)):
        return set(), "provider_special_decision_projection_mismatch"
    return set(decisions), None
