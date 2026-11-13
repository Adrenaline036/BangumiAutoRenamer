"""Deterministic movie video mapping, before subtitle, collision and persistence."""
from dataclasses import dataclass
from pathlib import Path

from .rename_plan import PlanEntry, PresentationAssetPlanContract


@dataclass(frozen=True)
class MovieVideoInput:
    videos: tuple[PlanEntry, ...]
    source_root: Path
    work_title: str
    group_id: str
    work_root: Path
    primary_sources: tuple[Path, ...] | None = None
    confirmed_segments: tuple[Path, ...] = ()
    excluded_sources: tuple[Path, ...] = ()
    roles: tuple[tuple[Path, str], ...] = ()
    groups_supplied: bool = False


@dataclass(frozen=True)
class MovieVideoMapping:
    source: Path
    target: Path
    asset: PresentationAssetPlanContract
    role: str
    is_content: bool


@dataclass(frozen=True)
class MovieVideoProjection:
    mappings: tuple[MovieVideoMapping, ...]
    unresolved: tuple[str, ...]


def project_movie_videos(value: MovieVideoInput) -> MovieVideoProjection:
    """Consume frozen observations only; this result grants no write authority."""
    # These shared pure helpers remain single definitions in the legacy module.
    # Lazy imports avoid a module cycle; no service instance or callback is used.
    from .plan_review import (
        PlanReviewError, PlanReviewService, _infer_movie_version_descriptor,
        _normalized_key, _infer_owner_descriptor, _movie_target_version_label,
        _movie_extra_role_label,
    )
    from .episode_inference import build_episode_hints, EpisodeInferenceError, build_semantic_context
    from .media_classifier import PreclassificationKind
    from .rename_plan import AssetRole, SegmentDescriptor, build_default_version_descriptor
    from .utils import canonical_extras_target

    if type(value) is not MovieVideoInput:
        raise PlanReviewError('电影视频映射缺少冻结输入')
    tuples = (value.videos, value.confirmed_segments, value.excluded_sources, value.roles)
    if (any(type(item) is not tuple for item in tuples)
            or value.primary_sources is not None and type(value.primary_sources) is not tuple
            or type(value.groups_supplied) is not bool
            or any(type(row) is not tuple or len(row) != 2 for row in value.roles)):
        raise PlanReviewError('电影视频映射输入必须不可变')
    videos = value.videos
    if not videos or any(not isinstance(entry, PlanEntry) or entry.media_kind != 'video' for entry in videos):
        raise PlanReviewError('电影视频映射缺少有效视频')
    work_root = value.work_root
    folder_name = work_root.name
    primary_video_source = None
    primary_video_sources = value.primary_sources
    confirmed_segment_sources = value.confirmed_segments
    excluded_movie_sources = value.excluded_sources
    movie_version_groups = () if value.groups_supplied else None
    supplied_roles = dict(value.roles)
    video_targets, content_video_targets, video_assets = {}, {}, {}
    video_identity, video_roles, unresolved_videos = {}, {}, []
    try:
        episode_hints = build_episode_hints((entry.source_path for entry in videos), value.work_title)
    except EpisodeInferenceError as exc:
        raise PlanReviewError(str(exc)) from exc

    if (
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
                entry.source_path, value.group_id, value.source_root
            )
            label_key = _normalized_key(descriptor.display_label or "默认版本")
            if (label_key in labels or descriptor.version_ref in refs) and movie_version_groups is None:
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
                value.group_id,
                AssetRole("content_version"),
                None,
                descriptor,
            )
        for extra in videos:
            if extra.source_path in descriptors:
                continue
            classified = PlanReviewService._preclassify_inventory_video(item_path=extra.source_path,
                candidate_name=extra.source_path.name, season_id=0,
                extra_context_name=build_semantic_context(extra.source_path.name, value.work_title),
                forced_role=supplied_roles.get(extra.source_path))
            if classified.kind != PreclassificationKind.CONFIRMED_EXTRA:
                video_targets[extra.source_path] = work_root / 'ManualReview' / extra.source_path.name
                video_identity[extra.source_path] = (None, None)
                video_roles[extra.source_path] = 'movie_version_unselected'
                video_assets[extra.source_path] = PresentationAssetPlanContract(
                    value.group_id, AssetRole('unknown'), None, None)
                if extra.source_path not in {Path(p) for p in excluded_movie_sources}:
                    unresolved_videos.append(extra.source_path.name)
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
            video_targets[extra.source_path] = canonical_extras_target(work_root, extra_name)
            video_identity[extra.source_path] = (None, None)
            video_roles[extra.source_path] = "disc_extra"
            video_assets[extra.source_path] = PresentationAssetPlanContract(
                value.group_id,
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
            value.group_id, None, None
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
                value.group_id,
                AssetRole("content_version"),
                SegmentDescriptor("cd", index, len(videos)),
                descriptor,
            )

    return MovieVideoProjection(
        tuple(MovieVideoMapping(source, target, video_assets[source], video_roles[source],
                                source in content_video_targets)
              for source, target in video_targets.items()),
        tuple(unresolved_videos),
    )
