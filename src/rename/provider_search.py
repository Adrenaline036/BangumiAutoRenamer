"""Multi-provider discovery for schema v5.

Provider responses are converted immediately into small local snapshots.  The
snapshots are evidence for human selection; they never decide playback layout
or approve a rename plan.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import Enum
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple
import time
import unicodedata
from urllib.parse import urlparse

import requests


BANGUMI_API_BASE = "https://api.bgm.tv"
BANGUMI_TIMEOUT = (5, 12)
BANGUMI_MAX_RESULTS = 12
BANGUMI_MAX_ATTEMPTS = 2
CANDIDATE_RANKING_VERSION = "v1"
TITLE_WEIGHT = 0.68
YEAR_MATCH_BONUS = 0.12
YEAR_CONFLICT_PENALTY = -0.18
ANIMATION_PROVIDER_BONUS = 0.10
MIN_TITLE_SIMILARITY = 0.28
MAX_RETRY_DELAY_SECONDS = 2.0
DEFAULT_PROVIDER_DEADLINE_SECONDS = 60.0
MANUAL_PROVIDER_RANKING_VERSION = "manual-url-v1"

SOURCE_BANGUMI = "bangumi"
SOURCE_TMDB_TV = "tmdb_tv"
SOURCE_TMDB_MOVIE = "tmdb_movie"


class ProviderSearchStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    ERROR = "error"
    TIMEOUT = "timeout"
    UNCONFIGURED = "unconfigured"
    CANCELLED = "cancelled"


class CandidateDecisionStatus(str, Enum):
    AUTO_ELIGIBLE = "auto_eligible"
    MANUAL_REQUIRED = "manual_required"
    NO_MATCH = "no_match"


@dataclass(frozen=True)
class SearchPolicy:
    """Provider scope derived only from domain, media hint and configuration."""

    domain: str
    media_hint: str
    expected_sources: Tuple[str, ...]
    scheduled_sources: Tuple[str, ...]


@dataclass(frozen=True)
class ProviderWorkerOutput:
    candidates: Tuple[Mapping[str, object], ...] = ()
    runtime: object = None


@dataclass(frozen=True)
class ProviderSearchResult:
    source: str
    status: ProviderSearchStatus
    candidates: Tuple[Mapping[str, object], ...] = ()
    elapsed_ms: int = 0
    message: str = ""
    runtime: object = None


@dataclass(frozen=True)
class ProviderSearchBatch:
    policy: SearchPolicy
    results: Tuple[ProviderSearchResult, ...]
    candidates: Tuple[Mapping[str, object], ...]

    @property
    def complete(self) -> bool:
        terminal_complete = {
            ProviderSearchStatus.SUCCESS,
            ProviderSearchStatus.EMPTY,
        }
        return bool(self.results) and all(
            result.status in terminal_complete for result in self.results
        )


@dataclass(frozen=True)
class CandidateDecision:
    status: CandidateDecisionStatus
    selected_candidate: Optional[Mapping[str, object]]
    decision_source: Optional[str]
    evidence: Tuple[str, ...]


def build_search_policy(
    *,
    domain: str,
    media_hint: str,
    tmdb_configured: bool,
    bangumi_configured: bool,
) -> SearchPolicy:
    """Build the frozen v5 search matrix without looking at local layout."""

    domain = str(domain or "unknown").casefold()
    media_hint = str(media_hint or "unknown").casefold()
    if domain not in {"animation", "general", "unknown"}:
        raise ValueError(f"不支持的作品领域: {domain}")
    if media_hint not in {"movie", "tv", "unknown"}:
        raise ValueError(f"不支持的媒体提示: {media_hint}")

    expected: List[str] = []
    if domain in {"animation", "unknown"}:
        if bangumi_configured:
            expected.append(SOURCE_BANGUMI)
        expected.extend((SOURCE_TMDB_TV, SOURCE_TMDB_MOVIE))
    elif media_hint == "movie":
        expected.append(SOURCE_TMDB_MOVIE)
    elif media_hint == "tv":
        expected.append(SOURCE_TMDB_TV)
    else:
        expected.extend((SOURCE_TMDB_TV, SOURCE_TMDB_MOVIE))

    scheduled = tuple(
        source for source in expected
        if (
            (source == SOURCE_BANGUMI and bangumi_configured)
            or (source != SOURCE_BANGUMI and tmdb_configured)
        )
    )
    return SearchPolicy(domain, media_hint, tuple(expected), scheduled)


def decide_provider_candidates(batch: ProviderSearchBatch) -> CandidateDecision:
    """Make the provider decision without mutating plans or runtime state."""

    eligible = tuple(
        item for item in batch.candidates if bool(item.get("match_eligible"))
    )
    statuses = ", ".join(
        f"{result.source}={result.status.value}" for result in batch.results
    )
    if not eligible:
        return CandidateDecision(
            CandidateDecisionStatus.NO_MATCH,
            None,
            None,
            ("没有达到最低标题证据的候选", statuses),
        )
    if batch.complete and len(eligible) == 1:
        return CandidateDecision(
            CandidateDecisionStatus.AUTO_ELIGIBLE,
            eligible[0],
            "provider_unique",
            ("全部计划来源已完成且只有一个合格候选", statuses),
        )
    reason = (
        "存在多个合格候选，需要人工选择"
        if len(eligible) > 1
        else "至少一个计划来源未完整结束，禁止自动选择单个候选"
    )
    return CandidateDecision(
        CandidateDecisionStatus.MANUAL_REQUIRED,
        None,
        None,
        (reason, statuses),
    )


ProviderWorker = Callable[[], ProviderWorkerOutput]


class ProviderSearchCoordinator:
    """Run the complete provider policy in one bounded concurrent batch."""

    def __init__(
        self,
        workers: Mapping[str, ProviderWorker],
        *,
        deadline_seconds: float = DEFAULT_PROVIDER_DEADLINE_SECONDS,
        max_workers: int = 3,
    ) -> None:
        self._workers = dict(workers)
        self._deadline_seconds = max(0.001, float(deadline_seconds))
        self._max_workers = max(1, int(max_workers))

    @staticmethod
    def _consume_late_result(future: Future) -> None:
        """Observe late worker failures after a coordinator deadline."""

        if future.cancelled():
            return
        try:
            future.exception()
        except BaseException:
            # The batch already recorded a timeout.  This callback exists only
            # to consume the late terminal state and retain no result object.
            return

    @staticmethod
    def _run_worker(source: str, worker: ProviderWorker) -> ProviderSearchResult:
        started = time.monotonic()
        try:
            output = worker()
            if not isinstance(output, ProviderWorkerOutput):
                raise TypeError("provider worker returned an unsupported result")
            candidates = tuple(dict(item) for item in output.candidates)
            status = (
                ProviderSearchStatus.SUCCESS
                if candidates else ProviderSearchStatus.EMPTY
            )
            return ProviderSearchResult(
                source,
                status,
                candidates,
                round((time.monotonic() - started) * 1000),
                runtime=output.runtime,
            )
        except Exception as exc:
            return ProviderSearchResult(
                source,
                ProviderSearchStatus.ERROR,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                message=f"{source} worker failed ({type(exc).__name__})",
            )

    def search(
        self,
        policy: SearchPolicy,
        *,
        query: str,
        year: Optional[int],
    ) -> ProviderSearchBatch:
        executor = ThreadPoolExecutor(
            max_workers=min(self._max_workers, max(1, len(policy.scheduled_sources))),
            thread_name_prefix="bar-provider",
        )
        future_by_source: Dict[str, Future] = {}
        for source in policy.scheduled_sources:
            worker = self._workers.get(source)
            if worker is not None:
                future_by_source[source] = executor.submit(
                    self._run_worker, source, worker
                )

        done, pending = wait(
            tuple(future_by_source.values()), timeout=self._deadline_seconds
        )
        result_by_source: Dict[str, ProviderSearchResult] = {}
        for source in policy.expected_sources:
            future = future_by_source.get(source)
            if future is None:
                result_by_source[source] = ProviderSearchResult(
                    source,
                    ProviderSearchStatus.UNCONFIGURED,
                    message=f"{source} is not configured",
                )
            elif future in done:
                result_by_source[source] = future.result()
            else:
                cancelled = future.cancel()
                result_by_source[source] = ProviderSearchResult(
                    source,
                    (
                        ProviderSearchStatus.CANCELLED
                        if cancelled else ProviderSearchStatus.TIMEOUT
                    ),
                    elapsed_ms=round(self._deadline_seconds * 1000),
                    message=f"{source} exceeded the provider deadline",
                )
                if not cancelled:
                    future.add_done_callback(self._consume_late_result)
        executor.shutdown(wait=False, cancel_futures=True)

        results = tuple(result_by_source[source] for source in policy.expected_sources)
        raw_candidates = (
            candidate for result in results for candidate in result.candidates
        )
        fused = fuse_provider_candidates(
            query,
            raw_candidates,
            year=year,
            animation=policy.domain == "animation",
        )
        return ProviderSearchBatch(policy, results, tuple(fused))


class ProviderSearchError(RuntimeError):
    """A provider could not return a trustworthy bounded response."""


def parse_provider_item_url(url: str) -> Tuple[str, str, str]:
    """Parse one supported public detail URL without following the supplied host."""

    value = str(url or "").strip()
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise ProviderSearchError("条目链接格式无效") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = tuple(part for part in parsed.path.split("/") if part)
    if host in {"www.themoviedb.org", "themoviedb.org"}:
        provider_id = parts[1].split("-", 1)[0] if len(parts) >= 2 else ""
        if len(parts) < 2 or parts[0] not in {"movie", "tv"} or not provider_id.isdigit():
            raise ProviderSearchError("TMDB 链接必须是 /movie/数字 或 /tv/数字")
        return "tmdb", parts[0], provider_id
    if host in {"bgm.tv", "www.bgm.tv", "bangumi.tv", "chii.in"}:
        if len(parts) < 2 or parts[0] != "subject" or not parts[1].isdigit():
            raise ProviderSearchError("Bangumi 链接必须是 /subject/数字")
        return "bangumi", "subject", parts[1]
    raise ProviderSearchError("只支持 Bangumi 或 TMDB 的作品详情页链接")


def resolve_provider_item_url(
    url: str,
    *,
    tmdb_api_key: str = "",
    bangumi_token: str = "",
    bangumi_user_agent: str,
    session: Optional[requests.Session] = None,
) -> Dict[str, object]:
    """Resolve an operator-pasted detail URL into a frozen candidate snapshot."""

    namespace, entity_type, provider_id = parse_provider_item_url(url)
    client = session or requests.Session()
    snapshot_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    headers = {"Accept": "application/json"}
    if namespace == "tmdb":
        key = str(tmdb_api_key or "").strip()
        if not key:
            raise ProviderSearchError("尚未配置 TMDB API 密钥，无法读取该链接")
        response = client.get(
            f"https://api.themoviedb.org/3/{entity_type}/{provider_id}",
            params={"api_key": key, "language": "zh-CN"},
            headers=headers,
            timeout=BANGUMI_TIMEOUT,
        )
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise ProviderSearchError("TMDB 条目读取失败，请检查链接和 API 密钥") from exc
        if not isinstance(payload, Mapping):
            raise ProviderSearchError("TMDB 条目返回结构无效")
        title_key = "title" if entity_type == "movie" else "name"
        original_key = "original_title" if entity_type == "movie" else "original_name"
        date_key = "release_date" if entity_type == "movie" else "first_air_date"
        title = str(payload.get(title_key) or payload.get(original_key) or "").strip()
        date = str(payload.get(date_key) or "")
        if not title:
            raise ProviderSearchError("TMDB 条目没有可用标题")
        poster_path = str(payload.get("poster_path") or "")
        candidate = {
            "namespace": "tmdb",
            "entity_type": entity_type,
            "provider_id": provider_id,
            "title": title,
            "original_title": str(payload.get(original_key) or ""),
            "aliases": [],
            "year": int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
            "poster_path": poster_path,
            "poster_url": (
                f"https://image.tmdb.org/t/p/w342{poster_path}"
                if poster_path.startswith("/") else poster_path
            ),
            "source_url": f"https://www.themoviedb.org/{entity_type}/{provider_id}",
            "group_kind": "movie_continuous" if entity_type == "movie" else "tv_episodes",
        }
    else:
        user_agent = str(bangumi_user_agent or "").strip()
        if not user_agent:
            raise ProviderSearchError("尚未配置 Bangumi User-Agent")
        headers["User-Agent"] = user_agent
        token = str(bangumi_token or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = client.get(
            f"{BANGUMI_API_BASE}/v0/subjects/{provider_id}",
            headers=headers,
            timeout=BANGUMI_TIMEOUT,
        )
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise ProviderSearchError("Bangumi 条目读取失败，请检查链接") from exc
        if not isinstance(payload, Mapping):
            raise ProviderSearchError("Bangumi 条目返回结构无效")
        title = str(payload.get("name_cn") or payload.get("name") or "").strip()
        date = str(payload.get("date") or "")
        images = payload.get("images") if isinstance(payload.get("images"), Mapping) else {}
        if not title:
            raise ProviderSearchError("Bangumi 条目没有可用标题")
        candidate = {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": provider_id,
            "title": title,
            "original_title": str(payload.get("name") or ""),
            "aliases": [],
            "year": int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
            "poster_url": str(images.get("large") or images.get("common") or ""),
            "source_url": f"https://bgm.tv/subject/{provider_id}",
            "group_kind": "",
        }
    candidate.update({
        "snapshot_at": snapshot_at,
        "provider_rank": 0,
        "ranking_version": MANUAL_PROVIDER_RANKING_VERSION,
        "match_score": 1.0,
        "match_eligible": True,
        "match_reasons": ("用户粘贴作品详情页链接",),
    })
    return candidate


def _normalized(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


class BangumiProviderClient:
    def __init__(
        self,
        *,
        token: str = "",
        user_agent: str,
        session: Optional[requests.Session] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        user_agent = str(user_agent).strip()
        if not user_agent:
            raise ValueError("Bangumi User-Agent 不能为空")
        self._token = str(token).strip()
        self._user_agent = user_agent
        self._session = session or requests.Session()
        self._sleeper = sleeper

    def search_animation(
        self,
        query: str,
        *,
        year: Optional[int] = None,
        limit: int = 8,
    ) -> List[Dict[str, object]]:
        query = str(query).strip()
        if not query:
            return []
        headers = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        filters: Dict[str, object] = {"type": [2]}
        if year:
            filters["air_date"] = [f">={int(year)}-01-01", f"<{int(year)+1}-01-01"]
        last_error: Optional[BaseException] = None
        payload = None
        for attempt in range(BANGUMI_MAX_ATTEMPTS):
            response = None
            try:
                response = self._session.post(
                    f"{BANGUMI_API_BASE}/v0/search/subjects",
                    params={"limit": min(max(int(limit), 1), BANGUMI_MAX_RESULTS), "offset": 0},
                    json={"keyword": query, "sort": "match", "filter": filters},
                    headers=headers,
                    timeout=BANGUMI_TIMEOUT,
                )
                response.raise_for_status()
                payload = response.json()
                break
            except (requests.RequestException, ValueError, TypeError) as exc:
                last_error = exc
                if attempt + 1 < BANGUMI_MAX_ATTEMPTS:
                    retry_after = 0.0
                    if getattr(response, "status_code", None) == 429:
                        try:
                            retry_after = float(
                                getattr(response, "headers", {}).get("Retry-After", 0)
                            )
                        except (TypeError, ValueError):
                            retry_after = 0.0
                    if retry_after > 0:
                        self._sleeper(min(retry_after, MAX_RETRY_DELAY_SECONDS))
        if payload is None:
            raise ProviderSearchError(
                f"Bangumi 搜索失败（{type(last_error).__name__}）"
            ) from last_error
        if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
            raise ProviderSearchError("Bangumi 搜索返回结构不符合预期")
        snapshot_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        candidates: List[Dict[str, object]] = []
        for item in payload["data"]:
            if not isinstance(item, Mapping) or item.get("id") is None:
                continue
            title = str(item.get("name_cn") or item.get("name") or "").strip()
            if not title:
                continue
            date = str(item.get("date") or "")
            images = item.get("images") if isinstance(item.get("images"), Mapping) else {}
            provider_id = str(item["id"])
            candidates.append({
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": provider_id,
                "title": title,
                "original_title": str(item.get("name") or ""),
                "aliases": [],
                "year": int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
                "poster_url": str(images.get("large") or images.get("common") or ""),
                "source_url": f"https://bgm.tv/subject/{provider_id}",
                "snapshot_at": snapshot_at,
                "provider_rank": len(candidates),
            })
        return candidates

    def load_crosswalk_evidence(
        self,
        subject_id: str,
        *,
        max_subjects: int = 12,
    ) -> Tuple[Tuple[Dict[str, object], ...], Tuple[Dict[str, object], ...]]:
        """Freeze one bounded prequel/sequel component from Bangumi."""

        seed = str(subject_id or "").strip()
        if not seed.isdigit():
            raise ProviderSearchError("Bangumi 条目 ID 无效")
        limit = min(max(int(max_subjects), 1), 20)
        pending = [seed]
        visited: set[str] = set()
        subjects: Dict[str, Dict[str, object]] = {}
        relations: Dict[Tuple[str, str, str], Dict[str, object]] = {}
        while pending and len(visited) < limit:
            current = pending.pop(0)
            if current in visited:
                continue
            visited.add(current)
            detail = self._get_json(f"/v0/subjects/{current}")
            snapshot = self._crosswalk_subject_snapshot(detail)
            if snapshot is not None:
                subjects[current] = snapshot
            related_payload = self._get_json(f"/v0/subjects/{current}/subjects")
            related_items = (
                related_payload
                if isinstance(related_payload, list)
                else related_payload.get("data", ())
                if isinstance(related_payload, Mapping)
                else ()
            )
            for item in related_items:
                if not isinstance(item, Mapping):
                    continue
                relation = str(item.get("relation") or "").strip()
                # Bangumi currently returns related subjects as flat objects;
                # older deployments and fixtures used {"subject": {...}}.
                # The relation item only supplies an id and traversal edge.
                # A full detail request below remains the TV evidence gate.
                nested = item.get("subject")
                related = nested if isinstance(nested, Mapping) else item
                related_id = str(related.get("id") or "")
                if not related_id.isdigit():
                    continue
                normalized = unicodedata.normalize("NFKC", relation).casefold()
                if normalized not in {"前传", "续集", "prequel", "sequel"}:
                    continue
                key = (current, related_id, relation)
                relations[key] = {
                    "from": current,
                    "to": related_id,
                    "relation": relation,
                }
                if related_id not in visited and related_id not in pending:
                    pending.append(related_id)
        if seed not in subjects:
            raise ProviderSearchError("Bangumi 条目缺少可用的 TV 证据")
        return tuple(subjects.values()), tuple(relations.values())

    def load_subject_evidence(self, subject_id: str) -> Dict[str, object]:
        """Load one exact Bangumi subject for non-series evidence linking."""

        provider_id = str(subject_id or "").strip()
        if not provider_id.isdigit():
            raise ProviderSearchError("Bangumi 条目 ID 无效")
        payload = self._get_json(f"/v0/subjects/{provider_id}")
        if not isinstance(payload, Mapping):
            raise ProviderSearchError("Bangumi 条目返回结构无效")
        title = str(payload.get("name_cn") or payload.get("name") or "").strip()
        date = str(payload.get("date") or "")
        images = payload.get("images") if isinstance(payload.get("images"), Mapping) else {}
        if not title:
            raise ProviderSearchError("Bangumi 条目没有可用标题")
        return {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": provider_id,
            "title": title,
            "original_title": str(payload.get("name") or ""),
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "platform": str(payload.get("platform") or ""),
            "poster_url": str(images.get("large") or images.get("common") or ""),
            "source_url": f"https://bgm.tv/subject/{provider_id}",
            "snapshot_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }

    def _get_json(self, endpoint: str) -> object:
        headers = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        last_error: Optional[BaseException] = None
        for attempt in range(BANGUMI_MAX_ATTEMPTS):
            response = None
            try:
                response = self._session.get(
                    f"{BANGUMI_API_BASE}{endpoint}",
                    headers=headers,
                    timeout=BANGUMI_TIMEOUT,
                )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError, TypeError) as exc:
                last_error = exc
                if attempt + 1 < BANGUMI_MAX_ATTEMPTS:
                    retry_after = 0.0
                    if getattr(response, "status_code", None) == 429:
                        try:
                            retry_after = float(
                                getattr(response, "headers", {}).get(
                                    "Retry-After", 0
                                )
                            )
                        except (TypeError, ValueError):
                            retry_after = 0.0
                    if retry_after > 0:
                        self._sleeper(
                            min(retry_after, MAX_RETRY_DELAY_SECONDS)
                        )
        raise ProviderSearchError(
            f"Bangumi 关系读取失败（{type(last_error).__name__}）"
        ) from last_error

    @staticmethod
    def _crosswalk_subject_snapshot(
        value: Mapping[str, object],
    ) -> Optional[Dict[str, object]]:
        provider_id = str(value.get("id") or "")
        if not provider_id.isdigit():
            return None
        platform = str(value.get("platform") or "").strip()
        subject_type = value.get("type")
        if platform and unicodedata.normalize("NFKC", platform).upper() != "TV":
            return None
        if not platform and subject_type not in {2, "2", None}:
            return None
        episode_count = value.get("eps")
        if not isinstance(episode_count, int):
            episode_count = None
        return {
            "namespace": "bangumi",
            "entity_type": "subject",
            "provider_id": provider_id,
            "title": str(value.get("name_cn") or value.get("name") or ""),
            "original_title": str(value.get("name") or ""),
            "air_date": str(value.get("date") or ""),
            "episode_count": episode_count,
            "platform": platform or "TV",
        }


def fuse_provider_candidates(
    query: str,
    candidates: Iterable[Mapping[str, object]],
    *,
    year: Optional[int] = None,
    animation: bool,
    limit: int = 12,
) -> List[Dict[str, object]]:
    """Deduplicate and rank snapshots without hiding cross-provider conflicts."""

    query_norm = _normalized(query)
    unique: Dict[tuple, Dict[str, object]] = {}
    for raw in candidates:
        item = dict(raw)
        key = (
            str(item.get("namespace") or "").casefold(),
            str(item.get("entity_type") or "").casefold(),
            str(item.get("provider_id") or ""),
        )
        if not all(key) or key in unique:
            continue
        names = [item.get("title"), item.get("original_title")]
        names.extend(item.get("aliases") or ())
        similarities = [SequenceMatcher(None, query_norm, _normalized(name)).ratio()
                        for name in names if _normalized(name)]
        title_score = max(similarities, default=0.0)
        item_year = item.get("year")
        year_score = (
            YEAR_MATCH_BONUS if year and item_year == year
            else (YEAR_CONFLICT_PENALTY if year and item_year else 0.0)
        )
        domain_score = (
            ANIMATION_PROVIDER_BONUS
            if animation and key[0] == "bangumi" else 0.0
        )
        title_gate = title_score >= MIN_TITLE_SIMILARITY
        score = (
            max(0.0, min(1.0, title_score * TITLE_WEIGHT + year_score + domain_score))
            if title_gate else 0.0
        )
        item["ranking_version"] = CANDIDATE_RANKING_VERSION
        item["match_eligible"] = title_gate
        item["match_components"] = {
            "title_similarity": round(title_score, 4),
            "title_gate": title_gate,
            "year_score": year_score,
            "domain_score": domain_score,
            "provider_rank": int(item.get("provider_rank") or 0),
        }
        item["match_score"] = round(score, 4)
        item["match_reasons"] = [
            f"标题相似度 {title_score:.2f}",
            "达到最低标题证据" if title_gate else "标题证据不足，仅作低置信参考",
            "年份吻合" if year_score > 0 else ("年份冲突" if year_score < 0 else "年份未限定"),
            "动画领域来源加权" if domain_score else "通用来源权重",
        ]
        unique[key] = item
    return sorted(
        unique.values(),
        key=lambda item: (not bool(item.get("match_eligible")),
                          -float(item.get("match_score") or 0),
                          int(item.get("provider_rank") or 0)),
    )[:max(1, int(limit))]
