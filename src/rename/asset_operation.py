"""Typed, receipted asset intent. Observations are frozen before approval."""
from dataclasses import dataclass, replace
from .series_repair import is_repair
import hashlib
import json
from collections.abc import Mapping
from functools import wraps
from inspect import iscoroutinefunction
from threading import local


_display_reads = local()


def reuse_display_asset_reads(function):
    """Deduplicate immutable parsing inside one synchronous display preparation.

    No observations survive the call; approval/execution never enter this scope.
    Thread-local storage cannot escape into tasks spawned by the UI event loop.
    """
    if iscoroutinefunction(function):
        raise TypeError('显示读取复用不能跨越异步等待')

    @wraps(function)
    def read(*args, **kwargs):
        previous = getattr(_display_reads, 'values', None)
        if previous is not None:
            return function(*args, **kwargs)
        _display_reads.values = {}
        try:
            return function(*args, **kwargs)
        finally:
            _display_reads.values = previous
    return read


def require_tv_editable_operation(operation, mode):
    """Asset admission only; callers must still validate current unexecuted TV state."""
    if operation is None:
        return
    value = operation.to_dict()
    if mode != 'initial' and value['mode'] == 'append' and not value['replaces']:
        return
    EDIT_MODES = {'season', 'episode', 'special', 'special_refresh', 'role', 'content', 'work', 'affiliation', 'range'}
    if mode in EDIT_MODES - {'affiliation'} and value['policy'] == 'asset-operation-v3' and value['mode'] == 'replace':
        return
    raise ValueError('已规划的换源或恢复操作不能由TV内容确认替换')


def plain(value):
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


@dataclass(frozen=True)
class AssetOperation:
    payload: bytes

    def __post_init__(self):
        value = json.loads(self.payload)
        if (type(self.payload) is not bytes or self.payload != self.encode(value)
                or set(value) - {'requires_choice', 'movie_scope', 'retirements', 'library_roots', 'catalog_before', 'season_directory', 'completion_policy', 'repair_root'} != {'policy', 'mode', 'slot_key', 'replaces', 'previous_assets'}
                or value['policy'] not in {'asset-operation-v1', 'asset-operation-v2', 'asset-operation-v3'}
                or value['mode'] not in {'append', 'repair', 'replace'}
                or not isinstance(value['slot_key'], str) or not value['slot_key']
                or not isinstance(value['replaces'], list)
                or not isinstance(value['previous_assets'], list)):
            raise ValueError('媒体资产操作合同无效')
        if 'completion_policy' in value:
            from .replacement_completion import DISCARD_REPLACED_LINKS
            if (value['completion_policy'] != DISCARD_REPLACED_LINKS
                    or value['policy'] not in {'asset-operation-v2', 'asset-operation-v3'} or value['mode'] != 'replace'):
                raise ValueError('换源完成策略无效')
        if value['policy'] in {'asset-operation-v2', 'asset-operation-v3'}:
            from .asset_replacement import RetirementEvidence, safe_absolute
            if (not (value['mode'] == 'replace' or is_repair(value)) or not isinstance(value.get('retirements'), list)
                    or not isinstance(value.get('library_roots'), list) or not value['library_roots']):
                raise ValueError('整体换源缺少退场或媒体库边界证据')
            for row in value['retirements']:
                RetirementEvidence.from_dict(row)
            for path in value['library_roots']:
                safe_absolute(path)
            before = value.get('catalog_before')
            if not isinstance(before, dict) or set(before) != {'backend', 'key', 'slot_key', 'root', 'rows'}:
                raise ValueError('整体换源缺少登记逆向证据')
            if before['slot_key'] != value['slot_key'] or before['backend'] not in {'work', 'series'}:
                raise ValueError('整体换源登记逆向证据越界')
        elif any(k in value for k in ('retirements', 'library_roots', 'catalog_before')):
            raise ValueError('旧资产操作不能携带新换源授权')
        if is_repair(value):
            from .series_repair import validate_contract
            validate_contract(value)
        elif 'repair_root' in value:
            raise ValueError('目录核查凭据仅用于缺失补齐')
        if value['policy']=='asset-operation-v3':
            from .season_replacement import validate_contract
            validate_contract(value)
        elif 'season_directory' in value:
            raise ValueError('旧资产操作不能携带整季目录授权')
        if 'requires_choice' in value and value['requires_choice'] is not True:
            raise ValueError('资产选择状态无效')
        if value.get('movie_scope') is not None:
            from .movie_asset_scope import FrozenMovieAssetScope
            FrozenMovieAssetScope.from_mapping(value['movie_scope'])
        if value['replaces'] and value['mode'] != 'replace':
            raise ValueError('只有明确换源操作可以替换文件')
        if value['replaces']:
            from .asset_replacement import ReplacementEvidence
            rows = [ReplacementEvidence.from_dict(row) for row in value['replaces']]
            if len({row.target for row in rows}) != len(rows):
                raise ValueError('换源目标重复')

    @staticmethod
    def encode(value):
        return json.dumps(plain(value), ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError('媒体资产操作必须是冻结结构')
        payload = cls.encode(value)
        cache = getattr(_display_reads, 'values', None)
        if cache is None:
            return cls(payload)
        key = (cls, payload)
        if key not in cache:
            cache[key] = cls(payload)
        return cache[key]

    def to_dict(self):
        return json.loads(self.payload)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload).hexdigest()












def freeze_movie_operation(state, draft, data_root):
    from .movie_asset_scope import prepare_append, MovieSlotChoiceRequired, _selection
    from .work_catalog import WorkCatalog
    if state.completed_rebuild_observation is not None:
        projected = draft
        selection = _selection(projected)
        if selection is None:
            return replace(state, asset_operation=None)
        key, _root, versions = selection
        return replace(state, asset_operation=AssetOperation.from_mapping(dict(
            policy='asset-operation-v1', mode='repair',
            slot_key=f"work:{key}:version:{'+'.join(versions)}", replaces=[],
            previous_assets=[])))
    if state.asset_operation is not None and state.asset_operation.to_dict()['mode'] == 'replace':
        return state
    projected = draft
    try:
        scope = prepare_append(projected, WorkCatalog(data_root/'work_catalog'))
        if scope is None:
            return state
        value = scope.to_dict()
        key, versions = value['work_key'], value['versions']
        extra = {'movie_scope': value}
    except MovieSlotChoiceRequired:
        key, _root, versions = _selection(projected)
        extra = {'requires_choice': True}
    operation = AssetOperation.from_mapping(dict(policy='asset-operation-v1', mode='append',
        slot_key=f"work:{key}:version:{'+'.join(versions)}", replaces=[], previous_assets=[], **extra))
    return replace(state, asset_operation=operation)
