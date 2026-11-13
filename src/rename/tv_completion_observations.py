"""Immutable complete TV observations; none of these grant write authority."""
from dataclasses import dataclass, fields
import json
import os
from pathlib import Path
import stat

from .content_associations import encode
from .provider_link_resolver import ProviderLinkResolution
from .provider_link_catalog import ProviderLinkCatalog, ProviderLinkMapping
from .series_registration import _plain


@dataclass(frozen=True)
class TVSeasonObservation:
    provider: tuple[str, str, str]
    available: tuple[int, ...]
    ova_subject: str | None = None

    def __post_init__(self):
        if (type(self.provider) is not tuple or len(self.provider) != 3
                or any(type(v) is not str or not v for v in self.provider)
                or type(self.available) is not tuple
                or any(type(n) is not int or n < 0 for n in self.available)
                or tuple(sorted(set(self.available))) != self.available):
            raise ValueError('TV季度资料必须是完整规范冻结观察')
        if self.ova_subject is not None:
            from .ova_selection import OvaSubject
            subject = OvaSubject(self.ova_subject)
            if self.provider != ('bangumi', 'subject', subject.provider_id) or self.available:
                raise ValueError('独立OVA季度观察不允许自动季号')

    @classmethod
    def capture(cls, work, info):
        from collections.abc import Mapping
        from .season_identity import provider_key
        rows = info.get('seasons', ())
        if not isinstance(rows, (tuple, list)) or any(not isinstance(row, Mapping)
                or type(row.get('season_number')) is not int or row['season_number'] < 0 for row in rows):
            rows = ()
        values = tuple(sorted({row['season_number'] for row in rows}))
        proof = None
        if '_bar_ova_subject' in info:
            from .ova_selection import OvaSubject
            proof = OvaSubject.capture(info['_bar_ova_subject']).payload
        return cls(provider_key(work.primary_provider.to_dict()), values, proof)

    def to_dict(self):
        value = dict(provider=list(self.provider), available=list(self.available))
        if self.ova_subject is not None:
            value["ova_subject"] = json.loads(self.ova_subject)
        return value

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict) or set(value) not in ({'provider', 'available'}, {'provider', 'available', 'ova_subject'}):
            raise ValueError('TV原季度观察缺失，请重新生成计划')
        return cls(tuple(value['provider']), tuple(value['available']),
            encode(value['ova_subject']) if 'ova_subject' in value else None)


@dataclass(frozen=True)
class FrozenTVResolution:
    payload: str

    def __post_init__(self):
        if type(self.payload) is not str:
            raise ValueError('TV资料观察必须是冻结规范JSON')
        value = json.loads(self.payload)
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(ProviderLinkResolution)}:
            raise ValueError('TV资料观察字段不完整')
        if encode(value) != self.payload:
            raise ValueError('TV资料观察不是规范表示')

    @classmethod
    def capture(cls, resolution):
        if type(resolution) is not ProviderLinkResolution:
            raise ValueError('TV资料必须来自实际resolver的类型化结果')
        return cls(encode({f.name: _plain(getattr(resolution, f.name)) for f in fields(resolution)}))

    def resolution(self):
        value = json.loads(self.payload)
        for key in ('season_assignments', 'evidence_path', 'reason_codes', 'derived_episode_ranges', 'boundaries'):
            if not isinstance(value[key], list):
                raise ValueError('TV资料冻结集合格式无效')
            value[key] = tuple(tuple(row) for row in value[key]) if key == 'season_assignments' else tuple(value[key])
        return ProviderLinkResolution(**value)


@dataclass(frozen=True)
class ProviderCatalogSnapshot:
    root: Path
    records: tuple[tuple[str, bytes], ...]

    def __post_init__(self):
        if (not isinstance(self.root, Path) or not self.root.is_absolute() or type(self.records) is not tuple
                or len(self.records) > 10000 or any(type(row) is not tuple or len(row) != 2
                    or type(row[0]) is not str or type(row[1]) is not bytes for row in self.records)):
            raise ValueError('跨站登记观察必须含完整冻结文件集合')
        names = [name for name, _ in self.records]
        if names != sorted(set(names)):
            raise ValueError('跨站登记观察文件重复或无序')
        for name, raw in self.records:
            mapping = ProviderLinkMapping.from_dict(json.loads(raw))
            if name != mapping.mapping_id + '.json':
                raise ValueError('跨站登记文件身份不一致')

    def mappings(self):
        return tuple(ProviderLinkMapping.from_dict(json.loads(raw)) for _, raw in self.records)


def observe_provider_catalog(catalog):
    """Read a bounded full-byte snapshot while the caller holds its public lock."""
    if type(catalog) is not ProviderLinkCatalog:
        raise ValueError('跨站登记观察缺少实际Catalog')
    root = catalog.root
    if not root.is_absolute():
        raise ValueError('跨站登记根必须是明确绝对路径')
    for parent in (root, *root.parents):
        if os.path.lexists(parent):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('跨站登记根不能经过链接或重解析点')
    def names():
        result = []
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.startswith('provider-link-') and entry.name.endswith('.json'):
                    result.append(entry.name)
                    if len(result) > 10000:
                        raise ValueError('跨站登记数量超过安全界限')
        return tuple(sorted(result))
    before_names = names()
    rows, total = [], 0
    for name in before_names:
        path = root / name
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400
                or before.st_size > 4 * 1024 * 1024):
            raise ValueError('跨站登记必须是安全容量内的普通文件')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(4 * 1024 * 1024 + 1)
        after = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if identity(before) != identity(opened) or identity(before) != identity(after) or len(raw) != before.st_size:
            raise ValueError('跨站登记文件在观察期间变化')
        total += len(raw)
        if total > 64 * 1024 * 1024:
            raise ValueError('跨站登记完整观察超过安全容量')
        rows.append((name, raw))
    if names() != before_names:
        raise ValueError('跨站登记集合在观察期间变化')
    return ProviderCatalogSnapshot(root, tuple(rows))


def verify_provider_catalog(catalog, snapshot):
    if type(snapshot) is not ProviderCatalogSnapshot or observe_provider_catalog(catalog) != snapshot:
        raise ValueError('跨站登记完整观察已变化，请重新预览')
