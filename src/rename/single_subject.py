"""单条目整理边界；历史合包记录可读取，不可继续产生副作用。"""
from collections.abc import Mapping

RETIRED = '合包功能已退役，请按单条目重新整理；原任务与源文件保留'


def _merged(assignments):
    seasons = {}
    rows = assignments.items() if isinstance(assignments, Mapping) else assignments or ()
    for row in rows:
        if isinstance(row, Mapping):
            subject, season = row.get('bangumi_subject_id'), row.get('season_number')
        else:
            subject, season = row
        if subject is not None and season is not None:
            seasons.setdefault(season, set()).add(str(subject))
    return any(len(subjects) > 1 for subjects in seasons.values())


def require_single_subject_resolution(resolution):
    if resolution is not None and _merged(resolution.season_assignments):
        raise ValueError(RETIRED)


def require_single_subject_plan(plan, state=None):
    metadata = plan.metadata
    if metadata.get('pending_tv_selection') is not None or getattr(state, 'pending_tv_selection', None) is not None:
        raise ValueError(RETIRED)
    for key in ('provider_link_snapshot', 'tv_completion_resolution'):
        value = metadata.get(key)
        if isinstance(value, Mapping) and _merged(value.get('season_assignments')):
            raise ValueError(RETIRED)
    owners = {str(row['bangumi_subject_id']) for row in metadata.get('task_episode_ownership', ())
              if isinstance(row, Mapping) and row.get('bangumi_subject_id')}
    if len(owners) > 1:
        raise ValueError(RETIRED)
    authority = getattr(state, 'tv_planning_authority', None)
    if authority is not None and _merged(authority.season_assignments):
        raise ValueError(RETIRED)
