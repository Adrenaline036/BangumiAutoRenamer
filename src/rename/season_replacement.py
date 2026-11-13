"""An explicitly adopted season directory, consumed by the existing transaction."""
from dataclasses import asdict, replace
import os
from pathlib import Path

from .asset_replacement import (DirectoryWitness, ReplacementEvidence, RetirementEvidence,
    _fingerprint, observe_file, _replacement_evidence, _retirement_evidence, require_file, safe_absolute)

POLICY = 'asset-operation-v3'
LIMIT = 20000


def season_root(work_root, number):
    if type(number) is not int or number < 0:
        raise ValueError('整季换源缺少确认季号')
    return safe_absolute(Path(work_root) / f'Season {number:02d}')


def inventory(root):
    root = safe_absolute(root)
    if not root.exists():
        return [], []
    if not root.is_dir():
        raise ValueError('季度目标不是目录')
    device = root.stat().st_dev
    directories, files, pending = [], [], [root]
    while pending:
        directory = safe_absolute(pending.pop())
        stat = directory.stat()
        if stat.st_dev != device:
            raise ValueError('季度目录跨越文件系统，不能整季替换')
        directories.append(asdict(DirectoryWitness(str(directory), stat.st_dev, stat.st_ino)))
        with os.scandir(directory) as entries:
            for entry in entries:
                path = safe_absolute(entry.path)
                stat = path.lstat()
                if stat.st_dev != device:
                    raise ValueError('季度文件跨越文件系统')
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(dict(target=str(path), evidence=asdict(observe_file(path))))
                else:
                    raise ValueError('季度含非普通文件，不能安全替换')
                if len(files) + len(directories) + len(pending) > LIMIT:
                    raise ValueError('季度目录项目过多，请缩小整理范围')
    return sorted(directories, key=lambda r:r['path']), sorted(files, key=lambda r:r['target'])


def pairs(plan):
    return sorted([dict(source=str(e.source_path), target=str(e.target_path)) for e in plan.entries],
                  key=lambda r:(r['source'], r['target']))


def generated_in_season(plan, root):
    from .generated_outputs import expected_files
    return [dict(target=r['target'], size=len(r['content'].encode('utf-8')),
                 **({'sha256':r['sha256']} if 'sha256' in r else {'content':r['content']}))
            for r in expected_files(plan) if r['kind'] == 'bangumi_episode_nfo'
            and Path(r['target']).parent == root]


def special_targets(plan, work, number):
    """Only confirmed SP bundles may extend a nonzero season's replacement set."""
    from .special_decision_projection import validated_special_decisions, video_bundle_closure, special_entry_id
    if number == 0:
        return set()
    identifiers, error = validated_special_decisions(plan)
    from .episode_range import validate_projection
    identifiers = identifiers | validate_projection(plan)
    if error:
        raise ValueError('特别篇决定与当前计划不一致')
    root = season_root(work, 0)
    targets = set()
    present = {special_entry_id(entry) for entry in plan.entries if entry.media_kind == 'video'}
    for identifier in identifiers & present:
        bundle = video_bundle_closure(plan, identifier)
        video = bundle[0][1]
        if video.season_id != 0 or video.target_path.parent != root:
            continue
        targets.update(str(entry.target_path) for _, entry in bundle if entry.target_path.parent == root)
    return targets


def validate_contract(value):
    before, scope = value['catalog_before'], value.get('season_directory')
    if (before['backend'] != 'series' or value.get('movie_scope') is not None
            or value['previous_assets'] or not isinstance(scope, dict)
            or set(scope) not in ({'root', 'directories', 'files', 'incoming'},
                                 {'root', 'directories', 'files', 'incoming', 'special_files'},
                                 {'root', 'directories', 'files', 'incoming', 'generated'},
                                 {'root', 'directories', 'files', 'incoming', 'special_files', 'generated'})
            or not isinstance(before['rows'], dict) or set(before['rows']) != {'bindings'}
            or not isinstance(before['rows']['bindings'], list)):
        raise ValueError('整季采用证据格式无效')
    prefix = f"series:{before['key']}:season:"
    if not value['slot_key'].startswith(prefix):
        raise ValueError('整季槽位不一致')
    suffix = value['slot_key'][len(prefix):]
    if not suffix.isdigit() or str(int(suffix)) != suffix:
        raise ValueError('整季槽位季号无效')
    root = season_root(before['root'], int(suffix))
    if scope['root'] != str(root):
        raise ValueError('整季证据超出确认季目录')
    for name in ('directories', 'files', 'incoming', *(['special_files'] if 'special_files' in scope else [])):
        if not isinstance(scope[name], list) or len(scope[name]) > LIMIT:
            raise ValueError('整季证据清单无效')
    directories = {}
    for row in scope['directories']:
        if (not isinstance(row, dict) or set(row) != {'path','device','inode'}
                or type(row['device']) is not int or type(row['inode']) is not int or row['inode'] <= 0):
            raise ValueError('整季目录身份无效')
        path = safe_absolute(row['path'])
        if not path.is_relative_to(root) or str(path) in directories:
            raise ValueError('整季目录范围或唯一性无效')
        directories[str(path)] = row
    if directories and str(root) not in directories:
        raise ValueError('整季证据缺少季根')
    for path in directories:
        if path != str(root) and str(Path(path).parent) not in directories:
            raise ValueError('整季父目录闭包缺失')
    incoming = {}
    for row in scope['incoming']:
        if not isinstance(row, dict) or set(row) != {'source','target'}:
            raise ValueError('整季新计划路径无效')
        source, target = safe_absolute(row['source']), safe_absolute(row['target'])
        if source.is_relative_to(root) or str(target) in incoming:
            raise ValueError('整季来源在旧季内或新目标重复')
        incoming[str(target)] = str(source)
    generated = scope.get('generated', [])
    if not isinstance(generated, list):
        raise ValueError('生成文件季度清单无效')
    generated_targets = set()
    for row in generated:
        import re
        if (not isinstance(row, dict) or set(row) not in ({'target', 'sha256', 'size'}, {'target', 'content', 'size'})
                or ('sha256' in row and not re.fullmatch('[0-9a-f]{64}', str(row['sha256'])))
                or ('content' in row and (not isinstance(row['content'], str) or len(row['content'].encode('utf-8')) != row['size']))
                or type(row.get('size')) is not int or row['size'] < 0):
            raise ValueError('生成文件季度证据无效')
        target = safe_absolute(row['target'])
        if target.parent != root or target.suffix.lower() != '.nfo' or str(target) in generated_targets or str(target) in incoming:
            raise ValueError('生成文件季度位置或唯一性无效')
        generated_targets.add(str(target))
    files = {}
    for row in scope['files']:
        if not isinstance(row, dict) or set(row) != {'target','evidence','disposition'}:
            raise ValueError('整季旧文件证据无效')
        target = safe_absolute(row['target'])
        evidence = _fingerprint(row['evidence'])
        if (not target.is_relative_to(root) or target == root or str(target.parent) not in directories
                or row['target'] in files or row['disposition'] not in {'replace','reuse','retire'}
                or evidence.device != directories[str(root)]['device']
                or getattr(evidence,'kind',None) not in {'hardlink-identity-v1','content-xxh3-128-v1'}):
            raise ValueError('整季文件范围或处置无效')
        if (row['target'] in set(incoming) | generated_targets) != (row['disposition'] != 'retire'):
            raise ValueError('整季退役范围与新计划不一致')
        files[row['target']] = row
    special_root = season_root(before['root'], 0)
    for row in scope.get('special_files', []):
        if (not isinstance(row, dict) or set(row) != {'target','evidence','disposition'}
                or row['disposition'] not in {'replace','reuse'}):
            raise ValueError('季外特别篇处置无效')
        target = safe_absolute(row['target'])
        evidence = _fingerprint(row['evidence'])
        if (root == special_root or target.parent != special_root or row['target'] in files
                or row['target'] not in incoming
                or getattr(evidence,'kind',None) not in {'hardlink-identity-v1','content-xxh3-128-v1'}):
            raise ValueError('季外特别篇范围无效')
        files[row['target']] = row
    if len(files) > LIMIT:
        raise ValueError('整季证据清单过多')
    if not files:
        raise ValueError('空季度无需覆盖授权')
    for name, disposition, cls in (('replaces','replace',ReplacementEvidence),('retirements','retire',RetirementEvidence)):
        rows = [cls.from_dict(r) for r in value[name]]
        if {r.target for r in rows} != {p for p,r in files.items() if r['disposition']==disposition}:
            raise ValueError('整季替换/退役闭包不一致')
        for row in rows:
            if row.target_root != before['root'] or asdict(row.old_target) != files[row.target]['evidence']:
                raise ValueError('整季旧实体证据不一致')
            if disposition == 'replace' and incoming[row.target] != row.source:
                raise ValueError('整季新来源与替换证据不一致')


def catalog_rows(actions, scope):
    _, key, _, root, number = scope
    if not actions.series._path(key).exists():
        return dict(bindings=[])
    match = actions.series.load(key)
    if match.canonical_root != root:
        raise ValueError('整季登记规范根变化')
    from .asset_operation import plain
    return dict(bindings=[plain(r) for r in match.season_bindings if r['season_number']==number])


def prepare(actions, plan, scope):
    from .asset_operation import AssetOperation
    from .asset_collision import reuse_resolution_for_observations
    from .media_file_identity import MediaFileIdentity
    from .target_routing import TargetRootSnapshot
    _, key, slot, work, number = scope
    root = season_root(work, number)
    incoming = pairs(plan)
    generated = generated_in_season(plan, root)
    if any(safe_absolute(r['source']).is_relative_to(root) or safe_absolute(r['source']) == safe_absolute(r['target']) for r in incoming):
        raise ValueError('新来源不能位于将替换的季度目录内')
    directories, files = inventory(root)
    special_files = [dict(target=target, evidence=asdict(observe_file(target)))
                     for target in sorted(special_targets(plan, work, number)) if safe_absolute(target).exists()]
    if not files and not special_files:
        return AssetOperation.from_mapping(dict(policy='asset-operation-v1',mode='append',slot_key=slot,
            replaces=[],previous_assets=[])), plan
    device = (root if root.exists() else safe_absolute(work)).stat().st_dev
    if any(safe_absolute(row['source']).stat().st_dev != device for row in incoming):
        raise ValueError('硬链接换源要求源文件与目标季度位于同一文件系统；请将新资源下载到目标所在磁盘后重新生成计划。原有文件未替换')
    all_files = files + special_files
    targets = {r['target']: _fingerprint(r['evidence']) for r in all_files}
    replacements, retirements, entries = [], [], []
    for entry in plan.entries:
        if str(entry.target_path) not in targets:
            entries.append(entry)
            continue
        source = observe_file(entry.source_path)
        target = targets[str(entry.target_path)]
        reusable = reuse_resolution_for_observations(source, target)
        if type(source) is MediaFileIdentity and reusable is not None and entry.target_path.stat().st_nlink < 2:
            reusable = None
        entries.append(replace(entry, collision_resolution=reusable))
        if reusable is None:
            replacements.append(_replacement_evidence(entry.source_path,entry.target_path,work,source,target))
    replace_targets = {r.target for r in replacements}
    new_targets = {r['target'] for r in incoming} | {r['target'] for r in generated}
    for row in all_files:
        row['disposition'] = ('replace' if row['target'] in replace_targets else
                              'reuse' if row['target'] in new_targets else 'retire')
        if row['disposition']=='retire':
            retirements.append(_retirement_evidence(Path(row['target']),work,targets[row['target']]))
    snapshot = TargetRootSnapshot.from_dict(plan.metadata.get('target_root_snapshot') or {})
    if plan.metadata.get('target_root_config_fingerprint') != snapshot.fingerprint:
        raise ValueError('换源缺少当前冻结库根，请重新生成计划')
    from .replacement_completion import DISCARD_REPLACED_LINKS
    value = dict(completion_policy=DISCARD_REPLACED_LINKS,policy=POLICY,mode='replace',slot_key=slot,previous_assets=[],
        replaces=[r.to_dict() for r in replacements],retirements=[r.to_dict() for r in retirements],
        library_roots=sorted(set(snapshot.to_dict().values())),
        catalog_before=dict(backend='series',key=key,slot_key=slot,root=str(work),rows=catalog_rows(actions,scope)),
        season_directory=dict(root=str(root),directories=directories,files=files,incoming=incoming))
    if special_files:
        value['season_directory']['special_files'] = special_files
    if generated:
        value['season_directory']['generated'] = generated
    operation = AssetOperation.from_mapping(value)
    resolved = {(str(e.source_path),str(e.target_path)) for e in entries if str(e.target_path) in targets}
    draft=replace(plan,entries=tuple(entries),projection_receipt=None,metadata={**plan.metadata,
        'asset_collision_unresolved':actions._clear_selected_collisions(plan,resolved),
        'asset_replacement_retained':(),
        'asset_replacement_retired':tuple(r['target'] for r in all_files if r['disposition']!='reuse')})
    return operation,draft


def validate_directory(scope, *, phase='old'):
    """Before writes require the exact old set; journal replay admits only frozen old/new paths."""
    for row in scope['directories']:
        DirectoryWitness(**row).validate()
    if phase == 'old' and not scope['directories'] and Path(scope['root']).exists():
        raise ValueError('季度目录已出现，请重新确认整季替换')
    special = {row['target']: asdict(observe_file(row['target'])) for row in scope.get('special_files', [])}
    if phase == 'old' and any(special[row['target']] != row['evidence'] for row in scope.get('special_files', [])):
        raise ValueError('所选特别篇目标已变化，请重新确认')
    _, current = inventory(scope['root'])
    actual = {r['target']:r['evidence'] for r in current}
    old = {r['target']:r['evidence'] for r in scope['files']}
    generated = {r['target']: r for r in scope.get('generated', [])}
    for path in set(actual) & set(generated):
        row = generated[path]
        from .file_digest import digest_bytes
        matches = actual[path].get('xxh3_128') == digest_bytes(row['content'].encode('utf-8'))
        if actual[path]['size'] != row['size'] or not matches:
            raise ValueError('生成NFO与冻结内容不一致')
    new = {r['target'] for r in scope['incoming'] if Path(r['target']).is_relative_to(Path(scope['root']))} | set(generated)
    # 生成物先写入，旧媒体校验仍逐项保持原实体；只接受本次冻结的新NFO。
    observed_old = {p: evidence for p, evidence in actual.items() if p in old or p not in generated}
    if phase=='old' and observed_old != old:
        raise ValueError('季度内容已变化，请重新确认整季替换')
    if phase=='completed' and set(actual) != new:
        raise ValueError('整季新内容尚未完整物化或出现额外文件')
    if phase=='journal' and not set(actual) <= set(old) | new:
        raise ValueError('整季恢复发现未授权文件，请人工处理')
    return {path: _fingerprint(row) for path, row in {**actual, **special}.items()}


def validate(actions, plan, value, *, completed=False, recovery=False, catalog_current=False):
    validate_contract(value)
    scope = actions._scope(plan)
    before = value['catalog_before']
    if value['season_directory'].get('generated', []) != generated_in_season(plan, season_root(scope[3],scope[4])):
        raise ValueError('整季生成文件与冻结计划不一致')
    if (scope[:3] != ('series',before['key'],value['slot_key']) or str(scope[3]) != before['root']
            or pairs(plan) != value['season_directory']['incoming']):
        raise ValueError('整季范围或新计划已变化，请重新确认')
    if not catalog_current and catalog_rows(actions,scope) != before['rows']:
        raise ValueError('整季原登记已变化，请重新确认')
    allowed = special_targets(plan, scope[3], scope[4])
    if any(row['target'] not in allowed for row in value['season_directory'].get('special_files', [])):
        raise ValueError('季外目标不是本计划已确认的特别篇配套')
    if recovery:
        return  # The existing journal validates each old/new entity and rollback backup.
    observed = validate_directory(value['season_directory'],phase='completed' if completed else 'old')
    if completed:
        for row in value['season_directory'].get('special_files', []):
            if row['disposition'] == 'reuse' and observed[row['target']] != _fingerprint(row['evidence']):
                raise ValueError('整季特别篇复用内容或身份变化')
    for row in value['replaces']:
        item = ReplacementEvidence.from_dict(row)
        item.validate_parents()
        require_file(item.source, item.new_source)
        current = observed.get(item.target)
        item.validate_target(current, target_may_be_new=completed)
        if completed and current != item.new_source:
            raise ValueError('整季新目标尚未完成')
    for row in value['retirements']:
        item = RetirementEvidence.from_dict(row)
        for parent in item.parent_chain:
            parent.validate()
        if (item.target in observed if completed else observed.get(item.target) != item.old_target):
            raise ValueError('整季旧文件尚未退役或已变化')
