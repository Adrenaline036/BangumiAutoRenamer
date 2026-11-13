"""季度缺失资产的非破坏性接续，使用现有批准与执行日志。"""
from dataclasses import replace
from pathlib import Path
import os

def is_repair(value):
    return bool(value and value.get('policy') == 'asset-operation-v2' and value.get('mode') == 'repair')

def root_identity(path):
    from .asset_replacement import safe_absolute
    root=safe_absolute(path)
    st=root.stat()
    if not root.is_dir():
        raise ValueError('媒体库目录不可访问，不能按目标缺失补齐')
    with os.scandir(root) as rows:
        next(rows,None)
    return dict(path=str(root),device=st.st_dev,inode=st.st_ino)

def validate_contract(value):
    from .asset_replacement import safe_absolute
    if (value['replaces'] or value['retirements'] or 'completion_policy' in value
            or not value['slot_key'].startswith('series:') or not value['previous_assets']):
        raise ValueError('补齐不能替换或退场现存文件')
    root=value.get('repair_root')
    if (not isinstance(root,dict) or set(root)!={'path','device','inode'}
            or type(root['device']) is not int or type(root['inode']) is not int or root['inode']<=0
            or root['path'] not in value['library_roots']):
        raise ValueError('补齐缺少媒体库目录身份')
    safe_absolute(root['path'])
    for row in value['previous_assets']:
        if row['disposition'] not in {'missing','reuse','retain'}:
            raise ValueError('补齐包含不允许的旧资产动作')
        if row['disposition']=='missing' and row['target_evidence'] is not None:
            raise ValueError('缺失登记不能携带现存目标')

def selected_previous(context,plan,previous):
    from .series_asset_scope import logical_episodes
    targets={str(e.target_path) for e in plan.entries}
    episodes=logical_episodes(plan.entries)
    for row in previous:
        old,_ledger,_sha=context._history(row['task_id'],row['plan_fingerprint'])
        entry=next(e for e in old.entries if str(e.source_path)==row['source_path']
            and str(e.target_path)==row['target_path'])
        old_episodes = logical_episodes([entry])
        overlap=row['target_path'] in targets or bool(old_episodes&episodes)
        if overlap and entry.episode_end is not None and not old_episodes <= episodes:
            raise ValueError('原视频包含多集，必须完整补齐其起止集数；原登记保留')
        if overlap and row['target_path'] not in targets and row['target_evidence'] is not None:
            raise ValueError('该分集仍有其他现存目标，不能作为缺失内容补齐')
        row['disposition']=('missing' if row['target_evidence'] is None else 'reuse') if overlap else 'retain'
    owners={r['task_id'] for r in previous if r['disposition']!='retain'}
    return [r for r in previous if r['task_id'] in owners]

def prepare(context,plan):
    from .asset_replacement import safe_absolute
    from .asset_collision import build_reuse_resolution
    from .asset_operation import AssetOperation
    from .target_routing import TargetRootSnapshot
    from .plan_execution import PlanExecutionService
    PlanExecutionService._validate_frozen_source_identities(plan)
    scope,previous,snapshot=context._previous(plan,with_snapshot=True)
    if scope[0]!='series':
        raise ValueError('核查并补齐只适用于当前季度')
    roots=TargetRootSnapshot.from_dict(plan.metadata.get('target_root_snapshot') or {}).to_dict()
    containers=[Path(p) for p in roots.values() if scope[3].is_relative_to(Path(p))]
    if not containers:
        raise ValueError('补齐作品目录不属于已确认媒体库')
    witness=root_identity(min(containers,key=lambda p:len(p.parts)))
    entries=[]
    for entry in plan.entries:
        target=safe_absolute(entry.target_path)
        target.relative_to(scope[3])
        resolution=build_reuse_resolution(entry.source_path,target) if os.path.lexists(target) else None
        if os.path.lexists(target) and resolution is None:
            raise ValueError('现存目标与本次来源不一致，不能补齐或覆盖：'+str(target))
        entries.append(replace(entry,collision_resolution=resolution))
    previous=selected_previous(context,plan,previous)
    if not any(r['disposition']=='missing' for r in previous):
        raise ValueError('未发现本次需要接续的缺失登记')
    value=dict(policy='asset-operation-v2',mode='repair',slot_key=scope[2],
        replaces=[],previous_assets=previous,retirements=[],
        library_roots=sorted(set(roots.values())),repair_root=witness,
        catalog_before=dict(backend=scope[0],key=scope[1],slot_key=scope[2],root=str(scope[3]),
            rows={k:v for k,v in snapshot.items() if k!='active_entries'}))
    draft=replace(plan,entries=tuple(entries),projection_receipt=None,metadata={**plan.metadata,
        'asset_collision_unresolved':context._clear_selected_collisions(plan,
            {(str(e.source_path),str(e.target_path)) for e in entries if e.collision_resolution is not None})})
    return AssetOperation.from_mapping(value),draft

def validate(context,plan,value,*,completed=False):
    from .plan_execution import PlanExecutionService
    validate_contract(value)
    PlanExecutionService._validate_frozen_source_identities(plan)
    if root_identity(value['repair_root']['path'])!=value['repair_root']:
        raise ValueError('媒体库目录身份已变化，请重新核查补齐范围')
    scope,previous,snapshot=context._previous(plan,expected=value['previous_assets'],
        completed=completed,with_snapshot=True)
    previous=selected_previous(context,plan,previous)
    before=dict(backend=scope[0],key=scope[1],slot_key=scope[2],root=str(scope[3]),
        rows={k:v for k,v in snapshot.items() if k!='active_entries'})
    if scope[0]!='series' or scope[2]!=value['slot_key'] or before!=value['catalog_before'] or previous!=value['previous_assets']:
        raise ValueError('补齐登记范围或文件已变化，请重新核查')
    if not scope[3].is_relative_to(Path(value['repair_root']['path'])):
        raise ValueError('补齐范围超出媒体库')

