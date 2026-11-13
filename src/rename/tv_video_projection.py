"""Deterministic TV video mapping; no source observation or write authority."""
from dataclasses import dataclass, replace
from pathlib import Path

from .rename_plan import PlanEntry, PresentationAssetPlanContract


@dataclass(frozen=True)
class TVVideoInput:
    videos: tuple[PlanEntry, ...]
    source_root: Path
    work_title: str
    group_id: str
    work_root: Path
    season_id: int
    roles: tuple[tuple[Path, str], ...] = ()
    season_source: str = 'source_observation'
    ranges: tuple = ()
    versions: tuple = ()


@dataclass(frozen=True)
class TVVideoMapping:
    source: Path
    target: Path
    season_id: int
    episode_id: int | None
    asset: PresentationAssetPlanContract
    role: str
    is_content: bool
    episode_end: int | None = None


@dataclass(frozen=True)
class TVVideoProjection:
    mappings: tuple[TVVideoMapping, ...]
    unresolved: tuple[str, ...]
    episode_conflicts: tuple[str, ...]


def project_tv_videos(value: TVVideoInput) -> TVVideoProjection:
    """Consume an explicitly resolved season and frozen paths, never a callback."""
    from .plan_review import PlanReviewError, PlanReviewService
    from .episode_inference import (
        EpisodeInferenceError, build_episode_hints, build_secondary_sequence_hints,
        build_semantic_context, normalize_target_stem, validate_episode_mapping,
    )
    from .media_classifier import LocalRole, MediaClassification, PreclassificationKind, classify_media
    from .rename_plan import AssetRole, build_default_version_descriptor
    from .utils import canonical_extras_target

    if type(value) is not TVVideoInput:
        raise PlanReviewError('剧集视频映射缺少冻结输入')
    if type(value.season_id) is not int or value.season_id < 0:
        raise PlanReviewError('剧集视频映射需要明确有效季度')
    if value.season_source not in {'source_observation', 'explicit_user'}:
        raise PlanReviewError('剧集视频映射缺少有效季度来源')
    if (type(value.videos) is not tuple or type(value.roles) is not tuple
            or any(type(row) is not tuple or len(row) != 2 for row in value.roles)):
        raise PlanReviewError('剧集视频映射输入必须不可变')
    videos = value.videos
    if not videos or any(not isinstance(entry, PlanEntry) or entry.media_kind != 'video' for entry in videos):
        raise PlanReviewError('剧集视频映射缺少有效视频')
    from .episode_range import EpisodeRangeDecision, project_entry
    if type(value.ranges) is not tuple or any(type(d) is not EpisodeRangeDecision for d in value.ranges):
        raise PlanReviewError('范围映射缺少类型化决定')
    range_map = {Path(d.source_path):d for d in value.ranges}
    if len(range_map) != len(value.ranges) or range_map.keys() - {e.source_path for e in videos}:
        raise PlanReviewError('连续集数来源不属于当前视频')
    ranged = tuple(project_entry(e, range_map[e.source_path], value.work_root, value.group_id)
                   for e in videos if e.source_path in range_map)
    videos = tuple(e for e in videos if e.source_path not in range_map)
    from .episode_versions import EpisodeVersionDecision, project_entry as project_version
    if type(value.versions) is not tuple or any(type(d) is not EpisodeVersionDecision for d in value.versions):
        raise PlanReviewError('剧集版本缺少类型化决定')
    version_map = {Path(d.source_path): d for d in value.versions}
    if len(version_map) != len(value.versions) or version_map.keys() - {e.source_path for e in videos}:
        raise PlanReviewError('剧集版本来源不属于当前普通视频')
    ranged += tuple(project_version(e, version_map[e.source_path], value.work_root, value.group_id)
                    for e in videos if e.source_path in version_map)
    videos = tuple(e for e in videos if e.source_path not in version_map)
    work_root = value.work_root
    supplied_roles = dict(value.roles)
    video_targets, content_video_targets, video_assets = {}, {}, {}
    video_identity, video_roles = {}, {}
    unresolved_videos, episode_assignment_conflicts = [], []
    preclassifications = {
        entry.source_path: PlanReviewService._preclassify_inventory_video(
            item_path=entry.source_path,
            candidate_name=entry.source_path.name,
            season_id=value.season_id,
            extra_context_name=build_semantic_context(
                entry.source_path.name, value.work_title
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
    from .provider_link_decision import tv_technical_reference_paths
    references, representatives = tv_technical_reference_paths(main_candidates)
    secondary = build_secondary_sequence_hints(references, value.work_title)
    secondary_sequence_hints = {source: secondary[representative] for source, representative in representatives.items()
                                if representative in secondary}
    try:
        inferred = build_episode_hints(references, value.work_title)
        episode_hints = {source: inferred[representative] for source, representative in representatives.items()
                         if representative in inferred}
    except EpisodeInferenceError:
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
                LocalRole.UNKNOWN, value.season_id, "unknown", None,
                "Low", "正片候选存在集号冲突，等待人工确认用途",
            )
        else:
            classification = classify_media(
                item_path=entry.source_path,
                candidate_name=entry.source_path.name,
                season_id=value.season_id,
                extra_context_name=build_semantic_context(
                    entry.source_path.name, value.work_title
                ),
                episode_hint=episode_hints.get(entry.source_path),
                secondary_bonus_episode=secondary_sequence_hints.get(
                    entry.source_path
                ),
            )
        # Keep multi-season source observations distinct unless the current
        # Decision explicitly pins this card to a user-selected season.
        season_id = value.season_id if value.season_source == 'explicit_user' else classification.season_scope
        normalized_stem = normalize_target_stem(
            entry.source_path.stem, value.work_title
        ) or entry.source_path.stem
        suffix = entry.source_path.suffix.lower()
        if classification.local_role == LocalRole.MAIN_EPISODE:
            episode = classification.episode_number
            if episode is None:
                unresolved_videos.append(entry.source_path.name)
                continue
            from .content_selection import canonical_episode_target
            target = canonical_episode_target(work_root, season_id, episode, suffix)
            video_targets[entry.source_path] = target
            content_video_targets[entry.source_path] = target
            video_identity[entry.source_path] = (season_id, episode)
            video_roles[entry.source_path] = "main_episode"
            video_assets[entry.source_path] = PresentationAssetPlanContract(
                value.group_id,
                AssetRole("content_version"),
                None,
                build_default_version_descriptor(
                    value.group_id, season_id, episode
                ),
            )
            continue
        if classification.local_role in {
            LocalRole.DISC_EXTRA,
            LocalRole.EPISODE_LIKE_BONUS,
            LocalRole.PROVIDER_SPECIAL,
        }:
            target = canonical_extras_target(work_root / f"Season {season_id:02d}",
                                             f"{normalized_stem}{suffix}")
            video_targets[entry.source_path] = target
            video_identity[entry.source_path] = (season_id, None)
            video_roles[entry.source_path] = classification.local_role.value
            video_assets[entry.source_path] = PresentationAssetPlanContract(
                value.group_id,
                AssetRole("extra", "other"),
                None,
                None,
            )
            continue

        # Unknown is still a current-plan decision.  Never retain the
        # movie inventory draft target after the user switches to TV.
        try:
            relative_source = entry.source_path.relative_to(value.source_root)
        except ValueError:
            relative_source = Path(entry.source_path.name)
        target = work_root / "ManualReview" / relative_source
        video_targets[entry.source_path] = target
        video_identity[entry.source_path] = (season_id, None)
        video_roles[entry.source_path] = "unknown"
        video_assets[entry.source_path] = PresentationAssetPlanContract(
            value.group_id,
            AssetRole("unknown"),
            None,
            None,
        )
        if entry.source_path.name not in unresolved_videos:
            unresolved_videos.append(entry.source_path.name)

    # Explicit episode markers may still classify as MAIN when batch hints
    # return no column (for example just two different files marked E01).
    # Validate the final identities with the same canonical predicate.
    from .provider_link_decision import bind_tv_episode_technical_versions
    rebound = bind_tv_episode_technical_versions(tuple(replace(entry,
        season_id=video_identity[entry.source_path][0], episode_id=video_identity[entry.source_path][1],
        presentation_asset=video_assets[entry.source_path]) for entry in videos))
    for entry in rebound:
        video_assets[entry.source_path] = entry.presentation_asset
    technical_groups = {}
    for entry in rebound:
        if entry.source_path in content_video_targets:
            technical_groups.setdefault((entry.season_id, entry.episode_id), []).append(entry)
    duplicate_versions = set()
    for members in technical_groups.values():
        if len(members) < 2:
            continue
        descriptors = [entry.presentation_asset.version_descriptor for entry in members]
        keys = [descriptor.technical_variant_key if descriptor else None for descriptor in descriptors]
        if all(key and key.startswith('resolution-') for key in keys) and len(set(keys)) == len(keys):
            duplicate_versions.update(entry.source_path for entry in members[1:])
            from .content_selection import canonical_episode_target
            for entry, descriptor in zip(members, descriptors):
                target = canonical_episode_target(work_root, entry.season_id, entry.episode_id,
                    entry.source_path.suffix, technical_label=descriptor.resolution.value)
                video_targets[entry.source_path] = content_video_targets[entry.source_path] = target
    seasons = {}
    for source in content_video_targets:
        if source in duplicate_versions:
            continue
        season, episode = video_identity[source]
        seasons.setdefault(season, {})[source] = episode
    for season, episodes in seasons.items():
        try:
            validate_episode_mapping(episodes)
        except EpisodeInferenceError:
            for source in tuple(content_video_targets):
                if video_identity[source][0] != season:
                    continue
                if str(source) not in episode_assignment_conflicts:
                    episode_assignment_conflicts.append(str(source))
                relative = source.relative_to(value.source_root) if source.is_relative_to(value.source_root) else Path(source.name)
                video_targets[source] = work_root / 'ManualReview' / relative
                content_video_targets.pop(source)
                video_identity[source] = (season, None)
                video_roles[source] = 'unknown'
                video_assets[source] = PresentationAssetPlanContract(value.group_id, AssetRole('unknown'), None, None)
                if source.name not in unresolved_videos:
                    unresolved_videos.append(source.name)

    return TVVideoProjection(
        tuple(TVVideoMapping(source, target, *video_identity[source], video_assets[source],
                             video_roles[source], source in content_video_targets)
              for source, target in video_targets.items()) + tuple(
                  TVVideoMapping(e.source_path, e.target_path, e.season_id, e.episode_id,
                      e.presentation_asset, e.role, True, e.episode_end) for e in ranged),
        tuple(unresolved_videos),
        tuple(episode_assignment_conflicts),
    )
