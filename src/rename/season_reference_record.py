"""Small optional reference records, independent of Plan and approval state."""
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .season_reference import SeasonReferences
from .special_catalog import identity, plain, validate_relation

POLICY='season-reference-v1'
_inflight={}


def record_path(task_root, task_id, provider):
    key=json.dumps([str(task_id),identity(provider),POLICY],sort_keys=True,separators=(',',':'))
    return Path(task_root).parent/'season_references'/(hashlib.sha256(key.encode()).hexdigest()+'.json')


def from_observation(provider, observation):
    if not observation:
        return None
    value=plain(observation)
    relation=value.get('relation')
    if relation is None:
        if value.get('schema_version') == 2 and value.get('provider_identity') == identity(provider):
            status = value.get('relation_reason')
            if status in {'incomplete', 'ambiguous', 'not_found', 'unavailable'}:
                return SeasonReferences(status=status)
        return None
    try:
        root=identity(provider)
        validate_relation(root,[dict(provider=root)],relation)
        return SeasonReferences(tuple(relation['subjects']),tuple(tuple(p) for p in relation['assignments']),
            'found',json.dumps(relation,ensure_ascii=False,sort_keys=True))
    except (ValueError,TypeError,KeyError):
        return None


def _decode(provider,value):
    if (not isinstance(value,dict) or set(value)!={'policy','provider','result'}
            or value['policy']!=POLICY or value['provider']!=identity(provider)):
        raise ValueError('季度参考记录身份不符')
    raw=value['result']
    if (set(raw)!={'subjects','assignments','status','relation_json'}
            or raw['status'] not in {'found','incomplete','ambiguous','not_found','unavailable'}
            or not isinstance(raw['subjects'],list) or len(raw['subjects'])>12):
        raise ValueError('季度参考记录不完整')
    ids=set()
    for row in raw['subjects']:
        if (not isinstance(row,dict) or not isinstance(row.get('provider_id'),str)
                or not row['provider_id'].isdigit() or row['provider_id'] in ids
                or not isinstance(row.get('title'),str)):
            raise ValueError('季度参考条目损坏')
        ids.add(row['provider_id'])
    if (not isinstance(raw['assignments'],list) or len(raw['assignments'])>12
            or any(not isinstance(row,list) or len(row)!=2 or not isinstance(row[0],str)
                   or row[0] not in ids or type(row[1]) is not int or row[1]<0
                   for row in raw['assignments'])):
        raise ValueError('季度参考对应损坏')
    result=SeasonReferences(tuple(raw['subjects']),tuple(tuple(p) for p in raw['assignments']),raw['status'],raw['relation_json'])
    if result.relation_json is not None:
        checked=from_observation(provider,dict(relation=result.frozen_relation()))
        if checked is None or checked.assignments!=result.assignments or checked.subjects!=result.subjects:
            raise ValueError('季度参考关系不一致')
    elif result.assignments:
        raise ValueError('季度参考编号缺少关系资料')
    return result


def read(path,provider):
    try:
        if not path.exists():return None
        if path.is_symlink() or path.stat().st_size>2_000_000:
            raise ValueError('季度参考记录异常')
        return _decode(provider,json.loads(path.read_text('utf-8')))
    except (OSError,ValueError,TypeError,KeyError):
        # Corrupt/failed reference never triggers repeated provider requests.
        return SeasonReferences(status='unavailable')


def save(path,provider,result):
    value=dict(policy=POLICY,provider=identity(provider),result=asdict(result))
    path.parent.mkdir(parents=True,exist_ok=True)
    handle,name=tempfile.mkstemp(prefix='.reference-',dir=path.parent)
    try:
        with os.fdopen(handle,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def start(task_root,task_id,provider,loader,*,observation=None,retry=False):
    """All controls join the same in-flight operation; completed work lives on disk."""
    loop=asyncio.get_running_loop()
    provider=identity(provider)
    path=record_path(task_root,task_id,provider)
    key=(loop,str(path.absolute()))
    current=_inflight.get(key)
    if current is not None:
        return current
    async def prepare():
        # A Plan projection can omit subjects or predate an explicit retry.
        # It is only a seed for an absent record, never a replacement for it.
        stored=None if retry else read(path,provider)
        if stored is not None:
            return stored
        observed=None if retry else from_observation(provider,observation)
        result=observed if observed is not None and (observed.subjects or observed.relation_json) else None
        if result is None:
            try:
                result=await asyncio.to_thread(loader,provider)
                if not isinstance(result,SeasonReferences):
                    raise ValueError('季度参考返回无效')
            except Exception:
                result=SeasonReferences(status='unavailable')
        try:
            save(path,provider,result)
        except OSError:
            # Keep the fetched result for this view even if it cannot be cached.
            pass
        return result
    future=loop.create_task(prepare())
    _inflight[key]=future
    future.add_done_callback(lambda task:_inflight.pop(key,None) if _inflight.get(key) is task else None)
    return future
