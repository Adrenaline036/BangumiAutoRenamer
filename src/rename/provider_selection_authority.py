"""Shared provider-candidate capability and presentation authority.

Provider namespace is evidence, not permission to enter a projector.  Every
UI, queue and process boundary consumes the same decision before a candidate
can replace the current work authority.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping


GENERAL_BANGUMI_TV_PLATFORMS = frozenset({'日剧', '欧美剧', '华语剧', '电视剧'})
_TV_BANGUMI_PLATFORMS = frozenset({"tv", "web", "ova"}) | GENERAL_BANGUMI_TV_PLATFORMS
_MOVIE_BANGUMI_PLATFORMS = frozenset({"movie", "剧场版", "电影"})


def is_bangumi_serial_platform(platform, *, include_general=True):
    """常规剧集证据接受TV/WEB；OVA由独立入口处理。"""
    value = unicodedata.normalize('NFKC', str(platform or '')).strip().casefold()
    return value in {'tv', 'web'} or include_general and value in GENERAL_BANGUMI_TV_PLATFORMS


def validate_bangumi_subject(info, provider_id, domain, group_kind):
    """Validate fetched subject identity/type before any planning adapter."""
    if (not isinstance(info, Mapping) or type(info.get('id')) is not int
            or str(info['id']) != str(provider_id)
            or domain not in {'animation', 'general'}
            or type(info.get('type')) is not int
            or info['type'] != (2 if domain == 'animation' else 6)
            or any(key in info and not isinstance(info[key], str) for key in ('name', 'name_cn', 'date'))
            or not str(info.get('name_cn') or info.get('name') or '').strip()
            or provider_candidate_group_kind(dict(namespace='bangumi', entity_type='subject',
                platform=info.get('platform'))) != group_kind):
        raise ValueError('Bangumi 条目身份、作品类型或媒体库无法核对；原计划未改变')


@dataclass(frozen=True)
class ProviderSelectionCapability:
    actionable: bool
    group_kind: str | None
    reason_code: str
    message: str


def provider_candidate_group_kind(
    candidate: Mapping[str, object],
) -> str | None:
    namespace = str(candidate.get("namespace") or "").strip().casefold()
    entity_type = str(candidate.get("entity_type") or "").strip().casefold()
    platform = str(candidate.get("platform") or "").strip().casefold()
    if namespace == "tmdb":
        if entity_type == "movie":
            return "movie_continuous"
        if entity_type == "tv":
            return "tv_episodes"
        return None
    if namespace == "bangumi" and entity_type == "subject":
        if platform in _MOVIE_BANGUMI_PLATFORMS:
            return "movie_continuous"
        if platform in _TV_BANGUMI_PLATFORMS:
            return "tv_episodes"
    return None


def task_media_kind_from_flag(value: object) -> str:
    if value is True:
        return "movie"
    if value is False:
        return "tv"
    return "mixed"


def is_confirmed_movie_plan(plan) -> bool:
    """Project confirmed movie semantics without treating subject as TV."""
    work = plan.work_identity if plan is not None else None
    primary = work.primary_provider if work is not None else None
    if work is None or work.status != 'confirmed' or primary is None:
        return False
    if (primary.namespace, primary.entity_type) == ('tmdb', 'movie'):
        return True
    if (primary.namespace, primary.entity_type) != ('bangumi', 'subject'):
        return False
    if plan.metadata.get('movie_version_selection') is not None:
        return bool(plan.presentation_groups) and all(g.kind == 'movie_continuous' for g in plan.presentation_groups)
    result = plan.metadata.get('initial_movie_selection_result')
    return isinstance(result, Mapping) and tuple(result.get('candidate_identity', ())) == (
        primary.namespace, primary.entity_type, primary.provider_id)


def task_library_domain_from_flag(value: object) -> str:
    """Return the explicit library domain without guessing from a Provider."""

    if value is True:
        return "animation"
    if value is False:
        return "general"
    return "mixed"


def task_library_domain_from_task(task: Mapping[str, object]) -> str:
    return task_library_domain_from_flag(task.get("is_anime"))


def task_media_kind_from_task(task: Mapping[str, object]) -> str:
    """Read explicit media kind or one unambiguous frozen candidate group."""

    explicit = task_media_kind_from_flag(task.get("is_movie"))
    if explicit != "mixed":
        return explicit
    candidates = task.get("provider_candidates")
    if not isinstance(candidates, Mapping):
        return "mixed"
    populated = {
        str(group_kind)
        for group_kind, rows in candidates.items()
        if str(group_kind) in {"tv_episodes", "movie_continuous"}
        and isinstance(rows, (list, tuple))
        and bool(rows)
    }
    if populated == {"tv_episodes"}:
        return "tv"
    if populated == {"movie_continuous"}:
        return "movie"
    return "mixed"


def task_media_kind_from_context(
    task: Mapping[str, object],
    plan_metadata: Mapping[str, object] | None = None,
) -> str:
    """Resolve task kind first, then an older Plan metadata fallback."""

    task_kind = task_media_kind_from_task(task)
    if task_kind != "mixed" or task.get("is_movie") is not None:
        return task_kind
    return task_media_kind_from_flag(
        plan_metadata.get("is_movie")
        if isinstance(plan_metadata, Mapping)
        else None
    )


def unconfirmed_automatic_type(task, plan, state):
    """原自动任务的库存显示类型不构成已确认作品类型。"""
    from .automatic_recognition import AutomaticRecognitionOrigin
    from .default_content import source_only_pending
    if plan is None or state is None or not source_only_pending(plan, state):
        return False
    origin = plan.metadata.get('automatic_recognition_origin')
    if not origin or origin != task.get('automatic_recognition_origin'):
        return False
    return AutomaticRecognitionOrigin.from_mapping(origin).is_movie is None


def selection_task_context(task, plan, state, candidate):
    """人工选择在未确认的自动库存上明确类型；已指定类型的任务保持原约束。"""
    if not unconfirmed_automatic_type(task, plan, state):
        return task
    from .automatic_recognition import AutomaticRecognition, AutomaticRecognitionOrigin, parse_batch
    origin = AutomaticRecognitionOrigin.from_mapping(plan.metadata['automatic_recognition_origin'])
    kind = provider_candidate_group_kind(candidate)
    anime = origin.is_anime
    domain = candidate.get('library_domain')
    if anime is None and domain not in {'animation', 'general'}:
        raw = plan.metadata.get('automatic_recognition')
        if raw:
            proof = AutomaticRecognition.from_mapping(raw)
            from .tmdb_genre import classify_tmdb_animation_evidence
            for row in parse_batch(proof.batch_json).results:
                info = row.runtime.get('info') or {}
                if (candidate.get('namespace') == 'tmdb'
                        and row.source == 'tmdb_' + str(candidate.get('entity_type'))
                        and str(info.get('id') or '') == str(candidate.get('provider_id'))):
                    anime = classify_tmdb_animation_evidence(genres=info.get('genres', ()),
                        genre_ids=info.get('genre_ids', ())).is_anime
    elif anime is None:
        anime = domain == 'animation'
    if kind is None or type(anime) is not bool:
        return task
    return {**task, 'is_anime': anime, 'is_movie': kind == 'movie_continuous'}


def provider_selection_capability(
    candidate: Mapping[str, object],
    *,
    group_kind: str,
    task_media_kind: str,
    task_library_domain: str = "mixed",
) -> ProviderSelectionCapability:
    """Return the one canonical actionability decision for a candidate."""

    candidate_group = provider_candidate_group_kind(candidate)
    expected_group = {
        "movie": "movie_continuous",
        "tv": "tv_episodes",
    }.get(str(task_media_kind or "").strip().casefold())
    if candidate_group is None:
        return ProviderSelectionCapability(
            False, None, "provider_media_kind_unresolved",
            "资料条目缺少可验证的电影或剧集类型，不能用于整理。",
        )
    if str(group_kind or "") != candidate_group:
        return ProviderSelectionCapability(
            False, candidate_group, "provider_presentation_mismatch",
            "该资料条目属于另一种观看形式，请先确认电影或分集版。",
        )
    if expected_group is None:
        return ProviderSelectionCapability(
            False, candidate_group, "task_media_kind_unresolved",
            "当前任务尚未确认电影或分集版，不能直接选择作品。",
        )
    if expected_group != candidate_group:
        return ProviderSelectionCapability(
            False, candidate_group, "task_media_kind_mismatch",
            "该资料条目与任务的电影/剧集类型不一致。",
        )

    namespace = str(candidate.get("namespace") or "").strip().casefold()
    entity_type = str(candidate.get("entity_type") or "").strip().casefold()
    library_domain = str(task_library_domain or "").strip().casefold()
    if namespace == 'bangumi':
        observed_domain = str(candidate.get('library_domain') or '').casefold()
        platform = str(candidate.get('platform') or '').strip().casefold()
        if platform in {'电影'} | GENERAL_BANGUMI_TV_PLATFORMS:
            observed_domain = 'general'
        if (library_domain in {'animation', 'general'}
                and observed_domain in {'animation', 'general'} and observed_domain != library_domain):
            return ProviderSelectionCapability(False, candidate_group, 'provider_library_domain_mismatch',
                '条目类型与当前动画/影视媒体库不一致，请核对作品类型。')
    if namespace == "tmdb" and entity_type == "movie":
        return ProviderSelectionCapability(
            True, candidate_group, "tmdb_movie_supported", "可用于电影整理。"
        )
    if namespace == "bangumi" and entity_type == "subject" and candidate_group == "tv_episodes":
        return ProviderSelectionCapability(
            True, candidate_group, "bangumi_tv_subject_supported",
            "可用于季度关系和剧集整理。",
        )
    if namespace == "tmdb" and entity_type == "tv":
        if library_domain in {"general", "animation"}:
            return ProviderSelectionCapability(
                True, candidate_group, "tmdb_general_tv_supported" if library_domain == "general" else "tmdb_animation_tv_supported",
                "可使用此条目的剧集编号整理；资料来源不改变所选媒体库。",
            )
        return ProviderSelectionCapability(False, candidate_group, "task_library_domain_unresolved",
            "请先确认放入动画库还是影视库，再选择剧集资料。")
    if namespace == "bangumi" and candidate_group == "movie_continuous":
        return ProviderSelectionCapability(
            True, candidate_group, "bangumi_movie_supported",
            "可使用此 Bangumi 条目整理电影；资料来源不改变所选媒体库。",
        )
    return ProviderSelectionCapability(
        False, candidate_group, "provider_projector_unavailable",
        "当前资料条目没有可安全使用的整理投影器。",
    )


def partition_provider_candidates_by_presentation(
    candidates: Iterable[Mapping[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """Keep movie and episodic observations in separate presentation groups."""

    grouped: dict[str, list[dict[str, object]]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        group_kind = provider_candidate_group_kind(candidate)
        if group_kind is None:
            group_kind = 'unresolved'
        grouped.setdefault(group_kind, []).append(dict(candidate))
    return grouped
