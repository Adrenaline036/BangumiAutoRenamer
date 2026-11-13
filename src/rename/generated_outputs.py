"""冻结和核验Bangumi命名所需NFO；新建文件不覆盖既有对象。"""
from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import stat
import uuid
from xml.etree.ElementTree import Element, SubElement, tostring

from .special_catalog import plain, encode
from .plan_store import PlanStore

POLICY = 'generated-outputs.v2'
FIELD = 'generated_outputs_fingerprint'


def digest(value):
    from .file_digest import digest_bytes
    data = encode(value).encode('utf-8')
    return digest_bytes(data)


def safe_path(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('生成文件需要绝对目标路径')
    for parent in (path, *path.parents):
        if os.path.lexists(parent):
            st = parent.lstat()
            if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
                raise ValueError('生成文件路径不能经过链接或重解析点')
            if (parent != path or directory) and not stat.S_ISDIR(st.st_mode):
                raise ValueError('生成文件父路径不是普通目录')
    return path


def expected_files(plan):
    from .special_decision_projection import special_entry_id
    from .special_selection import build_bangumi_special, occupied_bangumi_episodes
    files, targets, slots = [], set(), set()
    decisions = plan.metadata.get('provider_special_decisions', {})
    for entry in plan.entries:
        if entry.source_path in plan.ignored_sources:
            continue
        value = decisions.get(special_entry_id(entry), {})
        if value.get('action') != 'bangumi_special':
            continue
        target, descriptor = build_bangumi_special(plan, special_entry_id(entry), value)
        if (entry.media_kind != 'video' or entry.target_path != target or entry.season_id != 0
                or entry.episode_id != value['local_episode'] or entry.presentation_asset.version_descriptor != descriptor):
            raise ValueError('Bangumi特别篇输出与人工决定不一致')
        if (value['local_episode'] in slots
                or value['local_episode'] in occupied_bangumi_episodes(plan, special_entry_id(entry))):
            raise ValueError('Bangumi特别篇本地集号重复')
        slots.add(value['local_episode'])
        xml = Element('episodedetails')
        for tag, text in (('title', value['title']), ('season', '0'), ('episode', str(value['local_episode']))):
            SubElement(xml, tag).text = text
        SubElement(xml, 'uniqueid', {'type':'Bangumi'}).text = value['bangumi_reference']['row']['provider_episode_id']
        content = tostring(xml, encoding='unicode')
        files.append(dict(kind='bangumi_episode_nfo', target=str(target.with_suffix('.nfo')), content=content,
            owner=special_entry_id(entry), video_target=str(target), local_episode=value['local_episode']))
    from .episode_range import from_plan as range_decisions, validate_projection as validate_ranges
    ranges = range_decisions(plan)
    if ranges:
        validate_ranges(plan)
        videos = {str(e.source_path):e for e in plan.entries if e.media_kind == 'video'}
        for decision in ranges:
            if decision.naming_mode != 'bangumi' or decision.source_path not in videos:
                continue
            entry = videos[decision.source_path]
            xml = Element('episodedetails')
            for tag, text in (('title', decision.title), ('season', str(decision.season)),
                              ('episode', str(decision.start)), ('episodenumberend', str(decision.end))):
                SubElement(xml, tag).text = text
            files.append(dict(kind='bangumi_episode_nfo', target=str(entry.target_path.with_suffix('.nfo')),
                content=tostring(xml, encoding='unicode'), owner=special_entry_id(entry),
                video_target=str(entry.target_path), local_episode=decision.start,
                season=decision.season, episode_end=decision.end))
    for row in files:
        key = os.path.normcase(row['target'])
        if key in targets or any(os.path.normcase(str(e.target_path)) == key for e in plan.entries):
            raise ValueError('生成文件与整理目标重名')
        targets.add(key)
    return files


def inspect_target(row):
    path = safe_path(row['target'])
    if not os.path.lexists(path):
        return None
    st = path.lstat()
    content = row['content'].encode('utf-8')
    if not stat.S_ISREG(st.st_mode) or st.st_size != len(content) or path.read_bytes() != content:
        raise ValueError('生成文件已有不同内容或不是普通文件，请先处理：' + str(path))
    after = path.lstat()
    before = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    if before != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('生成文件核验期间变化')
    return list(before)


def freeze_outputs(plan):
    files = expected_files(plan)
    expected_nfo = {r['target'] for r in files if r['kind'] == 'bangumi_episode_nfo'}
    range_output = bool(plan.metadata.get('episode_ranges'))
    planned_targets = {str(e.target_path) for e in plan.entries} if range_output else set()
    retired = {r['target'] for r in (plan.metadata.get('asset_operation') or {}).get('retirements', ())}
    if files:
        for row in files:
            try:
                row['existing'] = inspect_target(row)
            except ValueError:
                if not range_output:
                    raise
                row['existing'] = None
                row['conflict'] = '目标NFO内容不同，请先核对既有文件'
            if row['kind'] == 'bangumi_episode_nfo':
                folder = safe_path(Path(row['target']).parent, directory=True)
                if folder.exists():
                    token = re.compile(r'(?i)S0*' + str(row.get('season', 0)) + r'E0*' + str(row['local_episode']) + r'(?![0-9])')
                    stem = Path(row['target']).stem
                    if any(token.search(p.name) and p.stem != stem and str(p) not in retired
                           and str(p) not in planned_targets for p in folder.iterdir()):
                        if not range_output:
                            raise ValueError('特别篇本地集号已被其他文件占用，请刷新预览')
                        row['conflict'] = '分集已有其他目标，请核查或明确换源'
    # 已有NFO不能在切换命名方式或换源后悄悄控制另一个视频。
    candidates = {str(e.target_path.with_suffix('.nfo')) for e in plan.entries if e.media_kind == 'video'}
    for row in (plan.metadata.get('asset_operation') or {}).get('previous_assets', ()):
        if row.get('target_path'):
            candidates.add(str(Path(row['target_path']).with_suffix('.nfo')))
    for target in candidates - expected_nfo - retired:
        if os.path.lexists(target):
            raise ValueError('目标存在NFO；当前命名方式不生成NFO，请先处理已有文件：' + target)
    metadata = dict(plan.metadata)
    metadata.pop('generated_outputs', None)
    if files:
        metadata['generated_outputs'] = dict(policy=POLICY, files=files)
    return replace(plan, metadata=metadata)


def validate_manifest(plan):
    manifest = plan.metadata.get('generated_outputs')
    if manifest is None:
        if any(v.get('action') == 'bangumi_special' for v in plan.metadata.get('provider_special_decisions', {}).values()) or any(v.get('naming_mode') == 'bangumi' for v in plan.metadata.get('episode_ranges', ())):
            raise ValueError('Bangumi命名缺少冻结NFO清单')
        return None
    if not isinstance(manifest, Mapping) or set(manifest) != {'policy', 'files'} or manifest['policy'] != POLICY:
        raise ValueError('生成文件清单版本无效')
    rows = plain(manifest['files'])
    expected = expected_files(plan)
    if len(rows) != len(expected) or not rows:
        raise ValueError('生成文件清单与当前计划不一致')
    for row, target in zip(rows, expected):
        optional = {'conflict'} if 'conflict' in row else set()
        if optional and (not plan.metadata.get('episode_ranges') or type(row['conflict']) is not str or not row['conflict']):
            raise ValueError('生成文件待确认状态无效')
        if set(row) != set(target) | {'existing'} | optional or {k: v for k, v in row.items() if k not in {'existing','conflict'}} != target:
            raise ValueError('生成文件内容、位置或归属已变化')
        old = row['existing']
        if old is not None and (not isinstance(old, list) or len(old) != 4 or any(type(x) is not int for x in old)):
            raise ValueError('生成文件存在状态无效')
    return digest(plain(manifest))


def bind_outputs(plan, manifest):
    from .projection_authority import build_projection_receipt
    metadata = dict(plan.metadata)
    metadata.pop('generated_outputs', None)
    if manifest is not None:
        metadata['generated_outputs'] = plain(manifest)
    candidate = replace(plan, metadata=metadata)
    fingerprint = validate_manifest(candidate)
    authority = dict(plan.projection_receipt.authority)
    authority.pop(FIELD, None)
    if fingerprint:
        authority[FIELD] = fingerprint
    receipt = build_projection_receipt(authority)
    metadata['projection_receipt_fingerprint'] = receipt.fingerprint
    return replace(candidate, metadata=metadata, projection_receipt=receipt)


def seal_outputs(plan):
    frozen = freeze_outputs(plan)
    return bind_outputs(frozen, frozen.metadata.get('generated_outputs'))


@dataclass(frozen=True)
class ApprovedOutputs:
    task_id: str
    plan_fingerprint: str
    manifest_json: str
    plan: object
    completed_json: str | None = None


def _checked_target(row):
    if row.get('conflict'):
        raise ValueError('生成文件仍有未确认冲突，不能执行')
    current = inspect_target(row)
    if current != row['existing']:
        raise ValueError('生成文件目标状态已变化，请重新生成计划')
    return current


def approved_outputs(plan, fingerprint):
    from .projection_authority import validate_plan_projection_receipt
    validate_plan_projection_receipt(plan)
    if PlanStore.fingerprint(plan) != fingerprint or not validate_manifest(plan):
        raise ValueError('生成文件缺少匹配的批准计划')
    rows = plain(plan.metadata['generated_outputs'])
    for row in rows['files']:
        _checked_target(row)
    return ApprovedOutputs(plan.task_id, fingerprint, encode(rows), plan)


def write_outputs(batch, *, task_id, fingerprint, record_root):
    if type(batch) is not ApprovedOutputs or batch.task_id != task_id or batch.plan_fingerprint != fingerprint:
        raise ValueError('生成文件缺少类型化批准能力')
    from .projection_authority import validate_plan_projection_receipt
    validate_plan_projection_receipt(batch.plan)
    if (PlanStore.fingerprint(batch.plan) != fingerprint or batch.plan.task_id != task_id
            or encode(batch.plan.metadata.get('generated_outputs')) != batch.manifest_json):
        raise ValueError('生成文件批准能力与冻结计划不一致')
    manifest = json.loads(batch.manifest_json)
    rows = manifest['files']
    if batch.completed_json is not None:
        result = json.loads(batch.completed_json)
        validate_completed(batch.plan, {'generated_outputs':result})
        return result
    observed = [_checked_target(row) for row in rows]
    result = dict(policy=manifest['policy'], manifest_fingerprint=digest(manifest), task_id=task_id,
                  plan_fingerprint=fingerprint, status='pending', planned_files=rows, files=[], directory_witnesses=[])
    from ..utils.utils import safe_task_path
    safe_task_path(Path(record_root), task_id)
    journal_root = safe_path(Path(record_root), directory=True)
    journal_root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    def record():
        path = journal_root / f'{task_id}.generated-{token}-{len(result["files"])}-{len(result["directory_witnesses"])}-{result["status"]}.json'
        with path.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
    record()
    try:
        for row, existing in zip(rows, observed):
            path = safe_path(row['target'])
            if existing is None:
                from .trans import ensure_parent
                ensure_parent(path.parent, [], result, task_id=task_id, plan_fingerprint=fingerprint, save=record)
                safe_path(path)
                with path.open('xb') as stream:
                    stream.write(row['content'].encode('utf-8')); stream.flush(); os.fsync(stream.fileno())
            witness = inspect_target(row)
            result['files'].append(dict(target=row['target'], witness=witness,
                status='created' if existing is None else 'reused_identical'))
            record()
        result['status'] = 'completed'
        record()
    except Exception:
        result['status'] = 'needs_manual_check'
        record()
        raise ValueError('生成文件未全部完成；已写文件已保留，请核对本次生成记录')
    return result


def validate_completed(plan, ledger):
    fingerprint = validate_manifest(plan)
    if fingerprint is None:
        if ledger and ledger.get('generated_outputs') is not None:
            raise ValueError('旧计划执行记录夹带生成文件')
        return
    result = ledger.get('generated_outputs') if isinstance(ledger, Mapping) else None
    if (not isinstance(result, Mapping) or result.get('policy') != plan.metadata['generated_outputs']['policy']
            or result.get('manifest_fingerprint') != fingerprint or result.get('status') != 'completed'
            or result.get('task_id') != plan.task_id or result.get('plan_fingerprint') != PlanStore.fingerprint(plan)):
        raise ValueError('执行记录缺少完整生成文件结果')
    expected = plan.metadata['generated_outputs']['files']
    directories = result.get('directory_witnesses', [])
    if not isinstance(directories, list):
        raise ValueError('生成目录凭据格式无效')
    seen = set()
    for row in directories:
        if (not isinstance(row, Mapping) or set(row) != {'path', 'dev', 'ino', 'task_id', 'plan_fingerprint', 'created'}
                or not isinstance(row['path'], str) or type(row['dev']) is not int or type(row['ino']) is not int
                or row['created'] is not True or row['task_id'] != plan.task_id
                or row['plan_fingerprint'] != result['plan_fingerprint']):
            raise ValueError('生成目录凭据身份无效')
        path = safe_path(row['path'], directory=True)
        if path in seen or not any(path in Path(r['target']).parents for r in expected if r['existing'] is None):
            raise ValueError('生成目录凭据范围无效')
        seen.add(path)
        current = path.stat()
        if (row['dev'], row['ino']) != (current.st_dev, current.st_ino):
            raise ValueError('生成目录凭据已变化')
    if len(result.get('files', ())) != len(expected):
        raise ValueError('生成文件执行记录不完整')
    for saved, row in zip(result['files'], expected):
        if (saved.get('target') != row['target'] or set(saved) != {'target', 'witness', 'status'}
                or saved.get('status') != ('created' if row['existing'] is None else 'reused_identical')
                or saved.get('witness') != inspect_target(row)):
            raise ValueError('生成文件内容或执行证据已变化')


def validate_registered(payload):
    rows = payload.get('generated_outputs', [])
    if not isinstance(rows, list):
        raise ValueError('生成文件登记损坏')
    for row in rows:
        if isinstance(row, dict) and ('season' in row or 'episode_end' in row):
            from .episode_range import numbers
            numbers(row.get('season'), row.get('local_episode'), row.get('episode_end'))
        if (not isinstance(row, dict) or row.get('kind') != 'bangumi_episode_nfo'
                or not row.get('task_id') or not re.fullmatch('[0-9a-f]{64}', str(row.get('plan_fingerprint', '')))
                or not Path(row.get('target','')).is_relative_to(Path(payload['canonical_root']))
                or not isinstance(row.get('content'), str)
                or row.get('policy') != POLICY or 'sha256' in row
                or type(row.get('local_episode')) is not int or row['local_episode'] < 1):
            raise ValueError('生成文件登记身份或位置无效')


def register_outputs(payload, plan, ledger):
    operation = plan.metadata.get('asset_operation') or {}
    retired = {r['target'] for r in operation.get('retirements', ())} if operation.get('mode') == 'replace' else set()
    if plan.metadata.get('generated_outputs') is None and not retired:
        return False
    validate_completed(plan, ledger)
    rows = [r for r in payload.get('generated_outputs', []) if r['task_id'] != plan.task_id and r['target'] not in retired]
    new = [dict(plain(r), task_id=plan.task_id, plan_fingerprint=PlanStore.fingerprint(plan), policy=POLICY)
           for r in plan.metadata.get('generated_outputs', {}).get('files', ())]
    for row in new:
        for old in rows:
            if row['target'] == old['target'] and row['content'] != old['content']:
                raise ValueError('生成文件登记内容冲突')
            if (row['kind'] == old['kind'] == 'bangumi_episode_nfo'
                    and row.get('season', 0) == old.get('season', 0)
                    and max(row['local_episode'], old['local_episode']) <= min(row.get('episode_end', row['local_episode']), old.get('episode_end', old['local_episode']))
                    and row['target'] != old['target']):
                raise ValueError('特别篇集号与已有登记冲突')
    result = rows + new
    changed = result != payload.get('generated_outputs', [])
    payload['generated_outputs'] = result
    validate_registered(payload)
    return changed


def resume_outputs(plan, record_root):
    from .projection_authority import validate_plan_projection_receipt
    if plan.metadata.get('generated_outputs') is None:
        return None
    validate_plan_projection_receipt(plan)
    fingerprint = PlanStore.fingerprint(plan)
    matches = []
    for path in Path(record_root).iterdir():
        if not path.name.startswith(plan.task_id + '.generated-') or not path.name.endswith('-completed.json'):
            continue
        safe_path(path)
        result = json.loads(path.read_text('utf-8'))
        if result.get('plan_fingerprint') == fingerprint:
            validate_completed(plan, {'generated_outputs':result})
            matches.append(result)
    if len(matches) != 1:
        raise ValueError('生成文件未有唯一完整记录；请核对保留文件后重新生成计划')
    return ApprovedOutputs(plan.task_id,fingerprint,encode(plan.metadata['generated_outputs']),plan,encode(matches[0]))
