"""Read-only disjoint append predicate using receipted Plans and completed ledgers."""
import json
from pathlib import Path

from .projection_authority import validate_plan_projection_receipt
from .season_identity import provider_key


def logical_episodes(entries):
    from .episode_range import coverage
    return {slot for e in entries if e.media_kind=='video'
            and e.presentation_asset is not None
            and e.presentation_asset.asset_role.kind=='content_version'
            and e.season_id is not None and e.episode_id is not None for slot in coverage(e)}


def assert_disjoint(plan, peer, *, entries=None, peer_entries=None):
    for value in (plan,peer): validate_plan_projection_receipt(value)
    if (plan.metadata.get('canonical_work_root')!=peer.metadata.get('canonical_work_root')
        or plan.metadata.get('series_key')!=peer.metadata.get('series_key')
        or provider_key(plan.work_identity.primary_provider.to_dict())!=provider_key(peer.work_identity.primary_provider.to_dict())):
        raise ValueError('季度追加与原登记的作品身份或目录不一致')
    left=plan.entries if entries is None else entries
    right=peer.entries if peer_entries is None else peer_entries
    targets=lambda rows:{str(e.target_path).casefold() for e in rows}
    sources=lambda rows:{str(e.source_path).casefold() for e in rows}
    if (logical_episodes(left)&logical_episodes(right) or targets(left)&targets(right)
            or sources(left)&sources(right)):
        raise ValueError('所选分集或目标已经登记，请使用核查或明确换源')


def validate_peer_append(plan, catalog, row, *, completed=False):
    """Other owners need actual historical authority, not a same-season veto."""
    if row.get('task_id')==plan.task_id:
        return
    from .asset_actions import AssetActionService
    context=getattr(catalog,'asset_action_context',None)
    if not isinstance(context,AssetActionService):
        raise ValueError('季度追加缺少完成记录核验上下文')
    from .series_repair import is_repair
    value=plan.metadata.get('asset_operation')
    if is_repair(value):
        context.validate(plan,context.decisions.load(plan.task_id),completed=completed)
        if row['task_id'] in {r['task_id'] for r in value['previous_assets']}:
            return
    task=str(row['task_id'])
    from .completed_history import read_bytes
    raw=json.loads(read_bytes(context.records/f'{task}.ledger.json'))
    fingerprint = str(raw.get('plan_fingerprint') or '')
    historical, _, _ = context._history(task, fingerprint)
    same_season = row.get('season_number') == plan.metadata.get('season_number')
    if not same_season and not (plan.metadata.get('episode_ranges') or historical.metadata.get('episode_ranges')):
        return
    active=context._active_slot_entries(context._scope(historical), [(task,fingerprint)])
    for peer, entries in active_plans(context,active):
        if same_season:
            assert_disjoint(plan,peer,peer_entries=entries)
        elif (logical_episodes(plan.entries) & logical_episodes(entries)
                or {str(e.target_path).casefold() for e in plan.entries} & {str(e.target_path).casefold() for e in entries}):
            raise ValueError('所选分集或目标已经登记，请使用原任务核查或明确换源')


def active_plans(context, active):
    """Select only ledger-verified active pairs, keeping original receipt intact."""
    grouped={}
    for task, fingerprint, source, target in active:
        grouped.setdefault((task,fingerprint),set()).add((source,target))
    for (task,fingerprint), pairs in sorted(grouped.items()):
        peer,_,_=context._history(task,fingerprint)
        entries=tuple(e for e in peer.entries if (str(e.source_path),str(e.target_path)) in pairs)
        if len(entries)!=len(pairs):
            raise ValueError('活跃分集与冻结完成计划不一致')
        yield peer,entries


def validate_active_disjoint(context, active):
    prior=[]
    for peer,entries in active_plans(context,active):
        for other,other_entries in prior:
            assert_disjoint(peer,other,entries=entries,peer_entries=other_entries)
        prior.append((peer,entries))
