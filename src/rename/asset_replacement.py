"""Immutable replacement observations; media mutations belong exclusively to Trans.

ApprovedReplacementBatch is an internal capability. Only the execution boundary,
after validating Decision, projection receipt and current approval, may issue it.
Deserializing an observation never produces an approval capability.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from .asset_collision import AssetCollisionError, requires_manual_collision
from .media_file_identity import MEDIA_IDENTITY, MediaFileIdentity, media_identity
from .small_file_identity import SMALL_CONTENT, SmallFileFingerprint, small_fingerprint
from .path_safety import first_reparse_component, is_reparse_or_symlink


def safe_absolute(path: Path | str) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("换源路径必须为绝对路径，且不得包含上级目录跳转")
    if first_reparse_component(path) is not None or is_reparse_or_symlink(path):
        raise ValueError("换源路径包含重解析点或符号链接，不能安全操作")
    return path


def observe_file(path: Path | str) -> SmallFileFingerprint | MediaFileIdentity:
    path = safe_absolute(path)
    try:
        result = media_identity(path) if requires_manual_collision(path) else small_fingerprint(path)
    except (AssetCollisionError, OSError) as exc:
        raise ValueError(f"无法核对换源文件，请检查文件是否可读：{path}") from exc
    if result.inode <= 0:
        raise ValueError("无法确认换源文件的物理身份")
    return result


def require_file(path: Path | str, expected: SmallFileFingerprint | MediaFileIdentity):
    path = safe_absolute(path)
    actual = media_identity(path) if type(expected) is MediaFileIdentity else observe_file(path)
    if actual != expected:
        raise ValueError(f"换源文件已变为不同实体或内容已变化，请重新核查：{path}")
    return actual


def ledger_file_fields(evidence, *, prefix: str) -> dict:
    """从当前类型化证据生成完成凭据字段，写入与恢复共用。"""
    if prefix not in {'source', 'target'} or type(evidence) not in {MediaFileIdentity, SmallFileFingerprint}:
        raise ValueError('换源完成凭据需要当前类型化文件证据')
    fields = ('size', 'device', 'inode')
    if type(evidence) is SmallFileFingerprint:
        fields += ('xxh3_128',)
    return {'evidence_kind': evidence.kind,
            **{f'{prefix}_{field}': getattr(evidence, field) for field in fields}}


def validate_ledger_file_fields(row, evidence) -> None:
    """完成换源的两端必须绑定同一批准实体及对应内容证据。"""
    if not isinstance(row, Mapping):
        raise ValueError('换源完成凭据条目无效')
    for prefix in ('source', 'target'):
        for key, expected in ledger_file_fields(evidence, prefix=prefix).items():
            actual = row.get(key)
            if type(actual) is not type(expected) or actual != expected:
                raise ValueError('换源完成凭据与批准文件证据不一致')
    count = row.get('target_link_count')
    if type(count) is not int or count < 2:
        raise ValueError('换源完成凭据缺少硬链接证明')


@dataclass(frozen=True)
class DirectoryWitness:
    path: str
    device: int
    inode: int

    @classmethod
    def observe(cls, path):
        path = safe_absolute(path)
        current = path.stat()
        if not path.is_dir():
            raise ValueError('换源上级路径不是目录')
        return cls(str(path), current.st_dev, current.st_ino)

    def validate(self) -> None:
        path = safe_absolute(self.path)
        if not path.is_dir():
            raise ValueError("换源文件的上级目录已缺失，请重新核查")
        current = path.stat()
        if (current.st_dev, current.st_ino) != (self.device, self.inode):
            raise ValueError("换源文件的上级目录身份已变化，请重新核查")


def _fingerprint(raw: object) -> SmallFileFingerprint | MediaFileIdentity:
    if isinstance(raw, Mapping) and raw.get('kind') == SMALL_CONTENT:
        if set(raw) != {'kind','size','xxh3_128','device','inode'}:
            raise ValueError('小文件内容证据含未知或混用字段')
        return SmallFileFingerprint(**raw)
    if isinstance(raw, Mapping) and raw.get('kind') == MEDIA_IDENTITY:
        if set(raw) != {'kind', 'size', 'device', 'inode'}:
            raise ValueError('媒体实体证据含未知或混用字段')
        return MediaFileIdentity(**raw)
    raise ValueError('换源文件证据格式不受支持')


@dataclass(frozen=True)
class ReplacementEvidence:
    source: str
    target: str
    target_root: str
    new_source: SmallFileFingerprint | MediaFileIdentity
    old_target: SmallFileFingerprint | MediaFileIdentity
    parent_chain: tuple[DirectoryWitness, ...]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["parent_chain"] = [asdict(row) for row in self.parent_chain]
        return value

    @classmethod
    def from_dict(cls, raw: Mapping) -> "ReplacementEvidence":
        if not isinstance(raw, Mapping) or set(raw) != {
            "source", "target", "target_root", "new_source", "old_target", "parent_chain"
        }:
            raise ValueError("invalid replacement evidence shape")
        if any(not isinstance(raw[name], str) for name in ("source", "target", "target_root")):
            raise ValueError("invalid replacement path evidence")
        chain = raw["parent_chain"]
        if not isinstance(chain, (list, tuple)) or not chain:
            raise ValueError("replacement parent witnesses missing")
        witnesses = []
        for row in chain:
            if (not isinstance(row, Mapping) or set(row) != {"path", "device", "inode"}
                    or not isinstance(row["path"], str)
                    or type(row["device"]) is not int or row["device"] < 0
                    or type(row["inode"]) is not int or row["inode"] <= 0):
                raise ValueError("invalid replacement parent witness")
            witnesses.append(DirectoryWitness(**row))
        result = cls(raw["source"], raw["target"], raw["target_root"],
                     _fingerprint(raw["new_source"]), _fingerprint(raw["old_target"]), tuple(witnesses))
        result.validate_paths()
        return result

    def validate_paths(self) -> None:
        source, target, root = (safe_absolute(value) for value in (self.source, self.target, self.target_root))
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise ValueError("replacement target escapes approved root") from exc
        if not relative.parts or relative.parts[0].casefold().startswith(".bar-replacements"):
            raise ValueError("replacement target uses reserved transaction namespace")
        if source == target or self.new_source == self.old_target:
            raise ValueError("replacement requires a distinct new source and old target")
        if self.new_source.device != self.old_target.device:
            raise ValueError("硬链接换源要求源文件与目标目录位于同一文件系统；请将新资源下载到目标所在磁盘后重新生成计划。原有文件未替换")
        expected_paths = {str(parent) for file in (source, target) for parent in file.parents}
        if {row.path for row in self.parent_chain} != expected_paths or len(self.parent_chain) != len(expected_paths):
            raise ValueError("replacement parent witness closure mismatch")

    def validate_parents(self):
        self.validate_paths()
        for row in self.parent_chain:
            row.validate()
        if Path(self.target_root).stat().st_dev != self.new_source.device:
            raise ValueError('换源暂存目录与目标目录不在同一文件系统，不能安全替换')

    def validate_target(self, current, *, target_may_be_new=False):
        allowed = (self.old_target, self.new_source) if target_may_be_new else (self.old_target,)
        if current not in allowed:
            raise ValueError('replacement old target identity/content drift')

    def validate_current(self, *, target_may_be_new=False):
        self.validate_parents()
        source = require_file(self.source, self.new_source)
        current = observe_file(self.target)
        self.validate_target(current, target_may_be_new=target_may_be_new)
        return source, current


def _replacement_evidence(source, target, target_root, new_source, old_target):
    source, target, root = (safe_absolute(path) for path in (source, target, target_root))
    parents = sorted({parent for file in (source, target) for parent in file.parents}, key=str)
    result = ReplacementEvidence(str(source), str(target), str(root), new_source, old_target,
                                 tuple(DirectoryWitness.observe(path) for path in parents))
    result.validate_paths()
    if root.stat().st_dev != new_source.device:
        raise ValueError('换源暂存目录与目标目录不在同一文件系统，不能安全替换')
    return result


def observe_replacement(source: Path, target: Path, target_root: Path) -> ReplacementEvidence:
    return _replacement_evidence(source, target, target_root, observe_file(source), observe_file(target))


@dataclass(frozen=True)
class RetirementEvidence:
    """An old owned target retiring without a same-path incoming file."""
    target: str
    target_root: str
    old_target: SmallFileFingerprint | MediaFileIdentity
    parent_chain: tuple[DirectoryWitness, ...]

    def to_dict(self):
        result = asdict(self)
        result['parent_chain'] = [asdict(row) for row in self.parent_chain]
        return result

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, Mapping) or set(raw) != {'target', 'target_root', 'old_target', 'parent_chain'}:
            raise ValueError('旧资产退场证据格式无效')
        if not isinstance(raw['parent_chain'], (list, tuple)):
            raise ValueError('旧资产退场目录凭据缺失')
        if any(not isinstance(raw[key], str) for key in ('target', 'target_root')):
            raise ValueError('旧资产退场路径必须为冻结字符串')
        for row in raw['parent_chain']:
            if (not isinstance(row, Mapping) or set(row) != {'path', 'device', 'inode'}
                    or not isinstance(row['path'], str) or type(row['device']) is not int
                    or row['device'] < 0 or type(row['inode']) is not int or row['inode'] <= 0):
                raise ValueError('旧资产退场目录身份无效')
        result = cls(raw['target'], raw['target_root'], _fingerprint(raw['old_target']),
                     tuple(DirectoryWitness(**row) for row in raw['parent_chain']))
        result.validate_paths()
        return result

    def validate_paths(self):
        target, root = safe_absolute(self.target), safe_absolute(self.target_root)
        if not target.is_relative_to(root) or target == root:
            raise ValueError('旧资产退场超出批准作品根')
        if ({row.path for row in self.parent_chain} != {str(p) for p in target.parents}
                or len(self.parent_chain) != len(target.parents)):
            raise ValueError('旧资产退场目录凭据不完整')

    def validate_current(self, *, retired=False):
        self.validate_paths()
        for row in self.parent_chain:
            row.validate()
        if retired and not path_present(Path(self.target)):
            return
        require_file(self.target, self.old_target)


def _retirement_evidence(target, target_root, old_target):
    target = safe_absolute(target)
    result = RetirementEvidence(str(target), str(safe_absolute(target_root)), old_target,
        tuple(DirectoryWitness.observe(p) for p in target.parents))
    result.validate_paths()
    return result


def observe_retirement(target: Path, target_root: Path) -> RetirementEvidence:
    return _retirement_evidence(target, target_root, observe_file(target))


@dataclass(frozen=True)
class ApprovedRollbackPreservation:
    """Only retain approved creates still owned by other registered consumers."""
    task_id: str
    plan_fingerprint: str
    preparation_fingerprint: str
    targets: tuple[tuple[str, SmallFileFingerprint | MediaFileIdentity], ...]

    def __post_init__(self):
        if (not isinstance(self.task_id, str) or not self.task_id
                or any(not isinstance(v, str) or len(v) != 64 or any(c not in '0123456789abcdef' for c in v)
                       for v in (self.plan_fingerprint, self.preparation_fingerprint))
                or type(self.targets) is not tuple):
            raise ValueError('回滚共享保护必须绑定当前批准与预检')
        for row in self.targets:
            if type(row) is not tuple or len(row) != 2 or type(row[1]) not in {MediaFileIdentity, SmallFileFingerprint}:
                raise ValueError('回滚共享保护缺少冻结文件身份')
            safe_absolute(row[0])
            _fingerprint(asdict(row[1]))
        if len({row[0] for row in self.targets}) != len(self.targets):
            raise ValueError('回滚共享保护目标重复')

    def to_dict(self):
        return dict(task_id=self.task_id, plan_fingerprint=self.plan_fingerprint,
            preparation_fingerprint=self.preparation_fingerprint,
            targets=[dict(target=path, evidence=asdict(evidence)) for path, evidence in self.targets])

    def validate(self, task_id, fingerprint, operations, journal):
        if (self.task_id, self.plan_fingerprint) != (task_id, fingerprint):
            raise ValueError('回滚共享保护不属于此批准计划')
        for path, evidence in self.targets:
            matches=[i for i,row in enumerate(operations) if row['target']==path and row['action']=='create']
            if len(matches)!=1 or journal is None or _fingerprint(journal['entries'][matches[0]]['source_evidence'])!=evidence:
                raise ValueError('共享保护只能保留本次真实创建且身份一致的目标')
            require_file(path,evidence)


@dataclass(frozen=True)
class ApprovedReplacementBatch:
    task_id: str
    plan_fingerprint: str
    evidence: tuple[ReplacementEvidence, ...]
    retirements: tuple[RetirementEvidence, ...] = ()
    target_root: str = ''
    library_roots: tuple[str, ...] = ()
    season_directory: bytes | None = None
    completion_policy: str | None = None

    def __post_init__(self):
        if (not isinstance(self.task_id, str) or not self.task_id
                or not isinstance(self.plan_fingerprint, str) or len(self.plan_fingerprint) != 64
                or not isinstance(self.evidence, tuple)
                or any(type(item) is not ReplacementEvidence for item in self.evidence)):
            raise ValueError("invalid approved replacement authority")
        from .replacement_completion import DISCARD_REPLACED_LINKS
        if self.completion_policy not in {None, DISCARD_REPLACED_LINKS}:
            raise ValueError("invalid replacement completion policy")
        if len({item.target for item in self.evidence}) != len(self.evidence):
            raise ValueError("duplicate approved replacement target")
        if (not isinstance(self.retirements, tuple)
                or any(type(item) is not RetirementEvidence for item in self.retirements)):
            raise ValueError('invalid approved retirement authority')
        rows = (*self.evidence, *self.retirements)
        if len({item.target for item in rows}) != len(rows):
            raise ValueError('重复的替换或退场目标')
        root = safe_absolute(self.target_root)
        if any(item.target_root != str(root) for item in rows):
            raise ValueError("replacement batch requires one approved target root")
        if not isinstance(self.library_roots, tuple) or not self.library_roots:
            raise ValueError('换源缺少冻结库根，必须重新生成计划')
        roots = tuple(safe_absolute(p) for p in self.library_roots)
        if not any(root.is_relative_to(p) for p in roots):
            raise ValueError('换源作品根不属于冻结媒体库')
        backup = self.transaction_directory
        if any(backup.is_relative_to(p) or p.is_relative_to(backup) for p in roots):
            raise ValueError('换源回滚区必须位于所有媒体库之外')
        if self.season_directory is not None:
            if type(self.season_directory) is not bytes:
                raise ValueError('整季执行范围必须冻结')
            scope = json.loads(self.season_directory)
            if not safe_absolute(scope['root']).is_relative_to(root) or Path(scope['root']) == root:
                raise ValueError('整季执行范围超出作品根')

    @property
    def transaction_key(self) -> str:
        return hashlib.sha256(json.dumps([self.task_id, self.plan_fingerprint],
                                        separators=(",", ":")).encode()).hexdigest()

    @property
    def transaction_directory(self) -> Path:
        roots = [Path(p) for p in self.library_roots if Path(self.target_root).is_relative_to(Path(p))]
        if not roots:
            raise ValueError('换源缺少可信库根')
        # Use the outermost configured containing library. Never infer a
        # library boundary from a work directory's name or parent depth.
        library = min(roots, key=lambda p: len(p.parts))
        return library.parent / '.bar-rollback' / self.transaction_key

    def validate_operations(self, task_id: str, plan_fingerprint: str, operations) -> None:
        if (task_id, plan_fingerprint) != (self.task_id, self.plan_fingerprint):
            raise ValueError("replacement approval task/plan mismatch")
        rows = []
        for operation in operations:
            if operation.get("action") != "replace":
                continue
            item = ReplacementEvidence.from_dict(operation.get("replacement_evidence"))
            if (str(operation["source"]), str(operation["target"])) != (item.source, item.target):
                raise ValueError("replacement operation path differs from approval")
            rows.append(item)
        if tuple(rows) != self.evidence:
            raise ValueError("replacement operation scope differs from approval")


def path_present(path: Path) -> bool:
    return os.path.lexists(path)


def approved_batch(plan, fingerprint: str) -> ApprovedReplacementBatch:
    """Called only after the execution boundary validates the current approval."""
    from .asset_operation import AssetOperation
    value = AssetOperation.from_mapping(plan.metadata.get('asset_operation')).to_dict()
    from .series_repair import is_repair
    if value['policy'] not in {'asset-operation-v2','asset-operation-v3'} or (value['mode'] != 'replace' and not is_repair(value)):
        raise ValueError('旧换源计划不能执行，请重新生成并批准整理计划')
    return ApprovedReplacementBatch(plan.task_id, fingerprint,
        tuple(ReplacementEvidence.from_dict(row) for row in value['replaces']),
        tuple(RetirementEvidence.from_dict(row) for row in value['retirements']),
        str(plan.metadata['canonical_work_root']), tuple(value['library_roots']),
        AssetOperation.encode(value['season_directory']) if value['policy']=='asset-operation-v3' else None,
        value.get('completion_policy'))
