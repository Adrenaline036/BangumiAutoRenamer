import unicodedata
from difflib import SequenceMatcher
from time import sleep
from typing import Any, Dict, List, Optional

import tmdbsimple as tmdb
import requests
from .provider_operation import tmdb_info

from ..logger import logger
from ..config.config_manager import cm, effective_tmdb_api_key


TMDB_REQUEST_TIMEOUT = (10, 15)
TMDB_MAX_ATTEMPTS = 2
TMDB_RETRY_DELAY_SECONDS = 2


class TMDBRequestError(RuntimeError):
    """TMDB 请求失败或所需证据无法读取。"""


class TMDBResponseError(TMDBRequestError):
    """已返回的资料结构或身份无效，不能当作可重试的网络故障。"""


def _request_failure_name(error: requests.RequestException) -> str:
    """Return a safe diagnostic without logging URLs or API credentials."""
    return type(error).__name__


def _normalize_search_title(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ' '.join(
        ''.join(char if char.isalnum() else ' ' for char in normalized).split()
    )


_SMALL_NUMBER_WORDS = {
    'zero': 0,
    'one': 1,
    'two': 2,
    'three': 3,
    'four': 4,
    'five': 5,
    'six': 6,
    'seven': 7,
    'eight': 8,
    'nine': 9,
    'ten': 10,
    'eleven': 11,
    'twelve': 12,
    'thirteen': 13,
    'fourteen': 14,
    'fifteen': 15,
    'sixteen': 16,
    'seventeen': 17,
    'eighteen': 18,
    'nineteen': 19,
}
_TENS_NUMBER_WORDS = {
    'twenty': 20,
    'thirty': 30,
    'forty': 40,
    'fifty': 50,
    'sixty': 60,
    'seventy': 70,
    'eighty': 80,
    'ninety': 90,
}


def _english_number_title_variant(value: str) -> Optional[str]:
    """Convert an all-number English title into a bounded discovery query.

    The result is only an additional provider query.  It never bypasses the
    normal title/alias confidence checks used to select the final TMDB item.
    """

    normalized = _normalize_search_title(value)
    tokens = normalized.split()
    if not tokens or any(token.isdigit() for token in tokens):
        return None

    allowed = set(_SMALL_NUMBER_WORDS) | set(_TENS_NUMBER_WORDS) | {
        'and',
        'hundred',
        'thousand',
    }
    if any(token not in allowed for token in tokens):
        return None

    total = 0
    current = 0
    saw_number = False
    for token in tokens:
        if token == 'and':
            continue
        saw_number = True
        if token in _SMALL_NUMBER_WORDS:
            current += _SMALL_NUMBER_WORDS[token]
        elif token in _TENS_NUMBER_WORDS:
            current += _TENS_NUMBER_WORDS[token]
        elif token == 'hundred':
            if current <= 0:
                return None
            current *= 100
        elif token == 'thousand':
            if current <= 0:
                return None
            total += current * 1000
            current = 0

    number = total + current
    if not saw_number or not 0 < number <= 9999:
        return None
    return str(number)


def _search_query_variants(query: str) -> List[str]:
    """Build bounded provider queries without changing title semantics."""

    source = unicodedata.normalize('NFKC', str(query or '')).strip()
    variants: List[str] = []
    for value in (
        query.strip(),
        ' '.join(
            ''.join(char if char.isalnum() else ' ' for char in source).split()
        ),
        ''.join(char for char in source if char.isalnum()),
        _english_number_title_variant(source),
    ):
        if value and value not in variants:
            variants.append(value)
    return variants[:4]


def _search_result_title_score(query: str, item: Dict[str, Any]) -> tuple:
    normalized_query = _normalize_search_title(query)
    query_tokens = set(normalized_query.split())
    query_numbers = {token for token in query_tokens if token.isdigit()}
    names = [
        _normalize_search_title(item.get(field, ''))
        for field in ('name', 'original_name')
    ]
    names.extend(
        _normalize_search_title(value)
        for value in item.get('_bar_alternative_titles', ())
    )
    names = [name for name in names if name]
    if not normalized_query or not names:
        return (0, 0, 0, 0.0)

    candidate_tokens = set(' '.join(names).split())
    number_match = int(not query_numbers or query_numbers <= candidate_tokens)
    exact = int(normalized_query in names)
    containment = int(any(
        f' {normalized_query} ' in f' {name} '
        or f' {name} ' in f' {normalized_query} '
        for name in names
    ))
    similarity = max(
        SequenceMatcher(None, normalized_query, name).ratio()
        for name in names
    )
    return (number_match, exact, containment, similarity)


def _select_search_target(query: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Choose the strongest lexical result instead of trusting provider rank 1."""

    return max(
        enumerate(results),
        key=lambda pair: (_search_result_title_score(query, pair[1]), -pair[0]),
    )[1]


def _enrich_tv_alternative_titles(
    query: str,
    results: List[Dict[str, Any]],
    limit: int = 8,
) -> None:
    """Resolve localized-title ambiguity with bounded TMDB alias lookups."""

    if not results:
        return
    initial = _select_search_target(query, results)
    initial_score = _search_result_title_score(query, initial)
    if initial_score[1] or (initial_score[0] and initial_score[2]):
        return

    for item in results[:limit]:
        item_id = item.get('id')
        if item_id is None:
            continue
        try:
            aliases = tmdb.TV(item_id).alternative_titles()
        except requests.RequestException as error:
            logger.warning(
                f'[电视剧搜索] 候选 id={item_id} 别名请求失败'
                f'（{_request_failure_name(error)}），继续使用基础字段'
            )
            continue
        titles = tuple(
            str(alias.get('title') or '').strip()
            for alias in (aliases or {}).get('results', [])
            if alias.get('title')
        )
        item['_bar_alternative_titles'] = titles
        enriched_score = _search_result_title_score(query, item)
        if enriched_score[1] or (enriched_score[0] and enriched_score[2]):
            break


def _complete_tv_subject_evidence(query, results, *, search_truncated):
    """逐个读取有界候选；排名不能代替身份、类型、季度和别名证据。"""
    limit = 8
    if not results:
        return ('', {'_bar_search_truncated': True, '_bar_search_alternatives': []}) if search_truncated else ('', None)
    complete = []
    for item in results[:limit]:
        identity = item.get('id')
        if type(identity) is not int or identity <= 0:
            raise TMDBResponseError('电视剧候选身份无效')
        television = tmdb.TV(identity)
        detail = tmdb_info(television, language='zh-CN')
        if (not isinstance(detail, dict) or type(detail.get('id')) is not int
                or detail['id'] != identity
                or not all(isinstance(detail.get(field), (list, tuple))
                           and all(isinstance(row, dict) for row in detail[field])
                           for field in ('genres', 'seasons'))
                or not any(isinstance(detail.get(field), str) and detail[field].strip()
                           for field in ('name', 'original_name'))):
            raise TMDBResponseError('电视剧详情身份或季度类型资料无效')
        aliases = television.alternative_titles()
        if (not isinstance(aliases, dict)
                or not isinstance(aliases.get('results'), (list, tuple))
                or ('id' in aliases and (type(aliases['id']) is not int or aliases['id'] != identity))
                or any(not isinstance(row, dict) or not isinstance(row.get('title'), str)
                       for row in aliases['results'])):
            raise TMDBResponseError('电视剧别名资料无效')
        # 详情的本地化名称不能抹掉搜索响应中已观察到的标题。
        titles = tuple(dict.fromkeys(value.strip() for value in (
            item.get('name'), item.get('original_name'),
            detail.get('name'), detail.get('original_name'),
            *(row['title'] for row in aliases['results']),
        ) if isinstance(value, str) and value.strip()))
        complete.append({**item, **detail, 'alternative_titles': titles,
                         '_bar_alternative_titles': titles})
    target = _select_search_target(query, complete)
    info = dict(target)
    info.update(
        _bar_search_name=target.get('name', ''),
        _bar_search_original_name=target.get('original_name', ''),
        _bar_search_alternative_titles=target['alternative_titles'],
        _bar_search_alternatives=[
            {**detail, 'name': searched.get('name', ''),
             'original_name': searched.get('original_name', '')}
            for searched, detail in zip(results, complete)
        ] + [dict(item) for item in results[limit:30]],
        _bar_search_truncated=search_truncated or len(results) > limit,
    )
    return str(target.get('name') or target.get('original_name')), info


class Search:
    subject_query_policy = 'tmdb-subject-airing-year-v1'

    def __init__(self) -> None:
        # Runtime-only override keeps credentials out of persisted config and
        # is especially useful for isolated local acceptance runs.
        self.TMDB_KEY = effective_tmdb_api_key(cm)
        tmdb.API_KEY = self.TMDB_KEY
        tmdb.REQUESTS_TIMEOUT = TMDB_REQUEST_TIMEOUT

    def get_season_info(
        self, tv_id: int, season_number: int
    ) -> Optional[Dict[str, Any]]:
        """
        获取指定季度的详细信息，包括剧集列表

        Args:
            tv_id: 电视剧ID
            season_number: 季度号

        Returns:
            筛选后的季度信息字典，失败返回None
        """
        for i in range(TMDB_MAX_ATTEMPTS):
            try:
                season = tmdb.TV_Seasons(tv_id, season_number)
                season_info = tmdb_info(season, language="zh-CN")

                if not season_info:
                    logger.warning(f"[季度信息] 未获取到Season {season_number}的信息")
                    return None

                # 筛选季度信息，只保留需要的字段
                filtered_season = {
                    "air_date": season_info.get("air_date"),
                    "episode_count": season_info.get("episode_count", 0),
                    "id": season_info.get("id"),
                    "name": season_info.get("name", ""),
                    "overview": season_info.get("overview", ""),
                    "season_number": season_info.get("season_number", season_number),
                    "episodes": [],
                }

                # 处理剧集信息
                episodes: List[Dict] = season_info.get("episodes", [])
                for episode in episodes:
                    filtered_episode = {
                        "id": episode.get("id"),
                        "air_date": episode.get("air_date"),
                        "episode_number": episode.get("episode_number"),
                        "episode_type": episode.get("episode_type", "regular"),
                        "name": episode.get("name", ""),
                        "overview": episode.get("overview", ""),
                        "runtime": episode.get("runtime"),
                        "season_number": episode.get("season_number", season_number),
                    }
                    filtered_season["episodes"].append(filtered_episode)

                logger.info(
                    f'[季度信息] 获取Season {season_number}信息成功，包含{len(filtered_season["episodes"])}集'
                )
                return filtered_season

            except requests.RequestException as error:
                failure = _request_failure_name(error)
                if i + 1 < TMDB_MAX_ATTEMPTS:
                    logger.warning(
                        f'[季度信息] Season {season_number} 请求失败'
                        f'（{failure}），{TMDB_RETRY_DELAY_SECONDS}秒后重试'
                    )
                    sleep(TMDB_RETRY_DELAY_SECONDS)
                else:
                    logger.error(
                        f'[季度信息] Season {season_number} 请求最终失败'
                        f'（{failure}）'
                    )

        logger.error(f"[季度信息] 获取Season {season_number}信息最终失败")
        return None



    def get_movie_info(
        self,
        query: str,
        year: int,
    ):
        for i in range(TMDB_MAX_ATTEMPTS):
            try:
                search = tmdb.Search()
                search.movie(
                    query=query,
                    language='zh-CN',
                    year=year if year != 0 else None,
                )
                target_list = search.__dict__['results']
                if target_list:
                    target = target_list[0]
                    name = target['title']
                    movie = tmdb.Movies(target['id'])
                    tmdb_info(movie, language='zh-CN')
                    movie.__dict__['_bar_search_name'] = target.get('title', '')
                    movie.__dict__['_bar_search_original_name'] = target.get(
                        'original_title',
                        '',
                    )
                    movie.__dict__['_bar_search_alternatives'] = [
                        {
                            'id': item.get('id'),
                            'title': item.get('title', ''),
                            'original_title': item.get('original_title', ''),
                            'release_date': item.get('release_date', ''),
                            'poster_path': item.get('poster_path', ''),
                        }
                        for item in target_list[:30]
                    ]
                    logger.debug(str(movie.__dict__))
                    return name, movie.__dict__
                return '', None
            except requests.RequestException as error:
                failure = _request_failure_name(error)
                if i + 1 < TMDB_MAX_ATTEMPTS:
                    logger.warning(
                        f'[电影搜索] TMDB 请求失败（{failure}），'
                        f'{TMDB_RETRY_DELAY_SECONDS}秒后重试'
                    )
                    sleep(TMDB_RETRY_DELAY_SECONDS)
                else:
                    logger.error(
                        f'[电影搜索] TMDB 请求最终失败（{failure}）'
                    )
                    raise TMDBRequestError('电影搜索网络请求失败') from error
        raise AssertionError('电影搜索重试循环异常结束')

    def get_movie_info_by_id(self, provider_id: int):
        """Resolve an operator-selected stable TMDB movie ID without searching."""

        for i in range(TMDB_MAX_ATTEMPTS):
            try:
                movie = tmdb.Movies(int(provider_id))
                payload = tmdb_info(movie, language='zh-CN') or movie.__dict__
                info = dict(movie.__dict__)
                info.update(payload if isinstance(payload, dict) else {})
                name = str(info.get('title') or info.get('original_title') or '')
                if not name or info.get('id') is None:
                    return '', None
                info['_bar_search_alternatives'] = [{
                    'id': info.get('id'),
                    'title': info.get('title', ''),
                    'original_title': info.get('original_title', ''),
                    'release_date': info.get('release_date', ''),
                    'poster_path': info.get('poster_path', ''),
                }]
                return name, info
            except (requests.RequestException, TypeError, ValueError) as error:
                if i + 1 < TMDB_MAX_ATTEMPTS:
                    sleep(TMDB_RETRY_DELAY_SECONDS)
                else:
                    raise TMDBRequestError(
                        '按电影 ID 获取详情失败'
                    ) from error

    def get_tv_info(
        self,
        query: str,
        year: int,
    ):
        return self._get_tv_info(query, year, complete_evidence=False)

    def get_tv_subject_observation_info(
        self,
        query: str,
        year: int,
    ):
        """按条目播出年份读取完整候选；整剧首播年份由普通搜索处理。"""

        return self._get_tv_info(query, year, complete_evidence=True)

    def _get_tv_info(
        self,
        query: str,
        year: int,
        *,
        complete_evidence: bool,
    ):
        for i in range(TMDB_MAX_ATTEMPTS):
            try:
                target_list: List[Dict[str, Any]] = []
                seen_ids = set()
                search_truncated = False
                for query_variant in _search_query_variants(query):
                    search = tmdb.Search()
                    year_filter = (
                        {'year': year} if year else {}
                    ) if complete_evidence else {
                        'first_air_date_year': year if year != 0 else None,
                    }
                    search.tv(
                        query=query_variant,
                        language='zh-CN',
                        **year_filter,
                    )
                    if complete_evidence and (
                        not isinstance(search.__dict__.get('results'), list)
                        or any(not isinstance(item, dict) or type(item.get('id')) is not int
                               or item['id'] <= 0 for item in search.__dict__['results'])
                        or any(type(search.__dict__.get(field)) is not int or search.__dict__[field] < 0
                               for field in ('total_results', 'total_pages'))
                        or search.__dict__['total_results'] < len(search.__dict__['results'])
                        or (search.__dict__['results'] and search.__dict__['total_pages'] < 1)
                    ):
                        raise TMDBResponseError('电视剧搜索候选资料无效')
                    logger.info(
                        f'[电视剧搜索] 查询变体={query_variant!r}；'
                        f'返回数={len(search.__dict__["results"])}'
                    )
                    logger.debug(
                        '[电视剧搜索] 候选摘要=' + str([
                            {
                                'id': item.get('id'),
                                'name': item.get('name', ''),
                                'original_name': item.get('original_name', ''),
                                'first_air_date': item.get('first_air_date', ''),
                            }
                            for item in search.__dict__['results'][:10]
                        ])
                    )
                    search_payload = search.__dict__
                    page_results = search_payload.get('results', ()) or ()
                    total_results = search_payload.get('total_results')
                    total_pages = search_payload.get('total_pages')
                    if (
                        isinstance(total_results, int)
                        and not isinstance(total_results, bool)
                        and total_results > len(page_results)
                    ) or (
                        isinstance(total_pages, int)
                        and not isinstance(total_pages, bool)
                        and total_pages > 1
                    ):
                        search_truncated = True
                    for item in search.__dict__['results']:
                        item_id = item.get('id')
                        dedupe_key = item_id if item_id is not None else (
                            item.get('name'),
                            item.get('original_name'),
                            item.get('first_air_date'),
                        )
                        if dedupe_key in seen_ids:
                            continue
                        seen_ids.add(dedupe_key)
                        target_list.append(item)

                    if target_list and not complete_evidence:
                        target = _select_search_target(query, target_list)
                        target_score = _search_result_title_score(query, target)
                        if target_score[1] or (
                            target_score[0] and target_score[2]
                        ):
                            break

                if complete_evidence:
                    return _complete_tv_subject_evidence(
                        query, target_list, search_truncated=search_truncated,
                    )
                if not target_list:
                    return '', None

                _enrich_tv_alternative_titles(query, target_list)
                target = _select_search_target(query, target_list)
                logger.info(
                    f'[电视剧搜索] 选定候选 id={target.get("id")}；'
                    f'name={target.get("name", "")}；'
                    f'original_name={target.get("original_name", "")}；'
                    f'first_air_date={target.get("first_air_date", "")}'
                )
                name = target['name']
                tv = tmdb.TV(target['id'])
                tmdb_info(tv, language='zh-CN')
                tv.__dict__['_bar_search_name'] = target.get('name', '')
                tv.__dict__['_bar_search_original_name'] = target.get(
                    'original_name',
                    '',
                )
                tv.__dict__['_bar_search_alternative_titles'] = target.get(
                    '_bar_alternative_titles',
                    (),
                )
                tv.__dict__['_bar_search_alternatives'] = [
                    {
                        'id': item.get('id'),
                        'name': item.get('name', ''),
                        'original_name': item.get('original_name', ''),
                        'first_air_date': item.get('first_air_date', ''),
                        'alternative_titles': item.get(
                            '_bar_alternative_titles',
                            (),
                        ),
                        'poster_path': item.get('poster_path', ''),
                        'genre_ids': tuple(item.get('genre_ids', ()) or ()),
                    }
                    for item in target_list[:30]
                ]
                tv.__dict__['_bar_search_truncated'] = (
                    search_truncated or len(target_list) > 30
                )
                logger.debug(str(tv.__dict__))
                return name, tv.__dict__
            except requests.RequestException as error:
                failure = _request_failure_name(error)
                if i + 1 < TMDB_MAX_ATTEMPTS:
                    logger.warning(
                        f'[电视剧搜索] TMDB 请求失败（{failure}），'
                        f'{TMDB_RETRY_DELAY_SECONDS}秒后重试'
                    )
                    sleep(TMDB_RETRY_DELAY_SECONDS)
                else:
                    logger.error(
                        f'[电视剧搜索] TMDB 请求最终失败（{failure}）'
                    )
                    raise TMDBRequestError('电视剧搜索网络请求失败') from error
        raise AssertionError('电视剧搜索重试循环异常结束')

    def get_tv_info_by_id(self, provider_id: int):
        """Resolve an operator-selected stable TMDB TV ID without searching."""

        for i in range(TMDB_MAX_ATTEMPTS):
            try:
                television = tmdb.TV(int(provider_id))
                payload = tmdb_info(television, language='zh-CN') or television.__dict__
                info = dict(television.__dict__)
                info.update(payload if isinstance(payload, dict) else {})
                name = str(info.get('name') or info.get('original_name') or '')
                if not name or info.get('id') is None:
                    return '', None
                info['_bar_search_alternatives'] = [{
                    'id': info.get('id'),
                    'name': info.get('name', ''),
                    'original_name': info.get('original_name', ''),
                    'first_air_date': info.get('first_air_date', ''),
                    'poster_path': info.get('poster_path', ''),
                }]
                return name, info
            except (requests.RequestException, TypeError, ValueError) as error:
                if i + 1 < TMDB_MAX_ATTEMPTS:
                    sleep(TMDB_RETRY_DELAY_SECONDS)
                else:
                    raise TMDBRequestError(
                        '按电视剧 ID 获取详情失败'
                    ) from error
