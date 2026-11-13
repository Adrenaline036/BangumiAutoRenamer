"""Immutable target-root snapshots used by reviewable plans.

The live configuration is consulted only when a new plan revision is built.
Approval and execution consume the serialized snapshot embedded in that
revision, so changing the GUI configuration cannot silently redirect an
already reviewed plan.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Iterable, Mapping, Optional

if TYPE_CHECKING:
    from .rename_plan import RenamePlan


ROOT_KEYS = (
    "bangumi_path",
    "movie_path",
    "anime_path",
    "anime_movie_path",
)


class TargetRoutingError(ValueError):
    """The target-root configuration cannot safely route a new revision."""


@dataclass(frozen=True)
class TargetRoutingProblem:
    """One stable fail-closed routing problem shared by every Plan gate."""

    code: str
    message: str
    target_path: Optional[Path] = None


@dataclass(frozen=True)
class FrozenTargetRouting:
    """A validated snapshot and the only root it may resolve to."""

    snapshot: "TargetRootSnapshot"
    resolved_target_root: Path


@dataclass(frozen=True)
class TargetRootSnapshot:
    bangumi_path: Path
    movie_path: Path
    anime_path: Path
    anime_movie_path: Path

    @classmethod
    def capture(cls, config: Mapping[str, object]) -> "TargetRootSnapshot":
        values = {}
        for key in ROOT_KEYS:
            raw = str(config.get(key) or "").strip()
            path = Path(raw)
            if not raw or not (
                path.is_absolute()
                or PureWindowsPath(raw).is_absolute()
                or PurePosixPath(raw).is_absolute()
                or raw.startswith("\\")
            ):
                raise TargetRoutingError(f"配置项 {key} 必须是非空绝对路径")
            values[key] = path
        return cls(**values)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "TargetRootSnapshot":
        return cls.capture(data)

    def to_dict(self) -> dict[str, str]:
        return {key: str(getattr(self, key)) for key in ROOT_KEYS}

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def resolve_target_root(
    domain: str,
    group_kind: str,
    snapshot: TargetRootSnapshot,
) -> Path:
    """Return the one configured root for the work domain and playback form."""

    if group_kind == "movie_continuous":
        if domain == "animation":
            return snapshot.anime_movie_path
        if domain == "general":
            return snapshot.movie_path
    elif group_kind == "tv_episodes":
        if domain == "animation":
            return snapshot.anime_path
        if domain == "general":
            return snapshot.bangumi_path
    raise TargetRoutingError(
        f"无法按作品域 {domain!r} 与观看形式 {group_kind!r} 选择目标目录"
    )


def _path_identity(path: Path | str) -> tuple[str, str]:
    """Normalize native, Windows and POSIX absolute paths without I/O."""

    raw = str(path).strip()
    windows = PureWindowsPath(raw)
    if windows.is_absolute():
        return "windows", windows.as_posix().rstrip("/").casefold()
    posix = PurePosixPath(raw)
    if posix.is_absolute():
        return "posix", str(posix).rstrip("/") or "/"
    native = os.path.normcase(os.path.abspath(os.path.normpath(raw)))
    return "native", native.replace("\\", "/").rstrip("/").casefold()


def _same_path(left: Path | str, right: Path | str) -> bool:
    return _path_identity(left) == _path_identity(right)


def _path_within(path: Path | str, root: Path | str) -> bool:
    path_kind, path_value = _path_identity(path)
    root_kind, root_value = _path_identity(root)
    if path_kind != root_kind:
        return False
    if path_value == root_value:
        return True
    return path_value.startswith(root_value.rstrip("/") + "/")


def inspect_frozen_target_routing(
    *,
    root_snapshot: object,
    root_config_fingerprint: object,
    domain: object,
    group_kind: object,
    target_root: object,
    canonical_work_root: object = None,
    target_paths: Iterable[Path] = (),
) -> tuple[Optional[FrozenTargetRouting], tuple[TargetRoutingProblem, ...]]:
    """Validate one frozen routing authority without consulting live config.

    Completion, verification, approval and execution all consume this same
    predicate.  Callers decide how to expose the stable problem codes, but no
    caller may infer a substitute root from metadata or the current GUI.
    """

    if root_snapshot is None or root_snapshot == {}:
        return None, (TargetRoutingProblem(
            "target_root_snapshot_missing",
            "整理计划缺少冻结目标配置；请重新生成整理计划",
        ),)
    if not isinstance(root_snapshot, Mapping):
        return None, (TargetRoutingProblem(
            "target_root_snapshot_invalid",
            "整理计划的冻结目标配置格式无效；请重新生成整理计划",
        ),)
    try:
        snapshot = TargetRootSnapshot.from_dict(root_snapshot)
    except (TypeError, TargetRoutingError, ValueError) as exc:
        return None, (TargetRoutingProblem(
            "target_root_snapshot_invalid",
            f"整理计划的冻结目标配置无效：{exc}",
        ),)
    fingerprint = (
        snapshot.fingerprint
        if root_config_fingerprint is None
        else str(root_config_fingerprint or "").strip()
    )
    if fingerprint != snapshot.fingerprint:
        return None, (TargetRoutingProblem(
            "target_root_snapshot_invalid",
            "整理计划的冻结目标配置指纹不一致；请重新生成整理计划",
        ),)
    try:
        resolved = resolve_target_root(
            str(domain or "").strip(),
            str(group_kind or "").strip(),
            snapshot,
        )
    except TargetRoutingError as exc:
        return None, (TargetRoutingProblem(
            "target_root_route_mismatch",
            str(exc),
        ),)
    requested_root = Path(str(target_root or "").strip())
    if not str(target_root or "").strip() or not _same_path(
        requested_root, resolved
    ):
        return None, (TargetRoutingProblem(
            "target_root_route_mismatch",
            "Plan 目标根与冻结配置的领域路由结果不一致",
            requested_root if str(requested_root) else None,
        ),)

    problems: list[TargetRoutingProblem] = []
    canonical: Optional[Path] = None
    if canonical_work_root is not None:
        raw_canonical = str(canonical_work_root or "").strip()
        canonical = Path(raw_canonical)
        if not raw_canonical or not _path_within(canonical, resolved):
            problems.append(TargetRoutingProblem(
                "target_root_route_mismatch",
                "作品规范目录越出冻结配置所解析的目标媒体库",
                canonical if raw_canonical else None,
            ))
    for path in target_paths:
        target = Path(path)
        if canonical is not None and not _path_within(target, canonical):
            problems.append(TargetRoutingProblem(
                "target_path_outside_canonical_root",
                "目标文件越出本作品的规范目录",
                target,
            ))
    return FrozenTargetRouting(snapshot, resolved), tuple(problems)


def inspect_plan_target_routing(
    plan: RenamePlan,
) -> tuple[Optional[FrozenTargetRouting], tuple[TargetRoutingProblem, ...]]:
    """Adapt a frozen inventory Plan to the shared routing predicate.

    Inventory Plans name the library root separately from the canonical work
    directory. Legacy work-root Plans must not be reinterpreted by this adapter.
    """
    group_kinds = {
        group.kind for group in plan.presentation_groups
        if group.kind in {"movie_continuous", "tv_episodes"}
    }
    return inspect_frozen_target_routing(
        root_snapshot=plan.metadata.get("target_root_snapshot"),
        root_config_fingerprint=plan.metadata.get("target_root_config_fingerprint"),
        domain=plan.work_identity.domain if plan.work_identity is not None else "",
        group_kind=next(iter(group_kinds)) if len(group_kinds) == 1 else "",
        target_root=plan.metadata.get("target_root"),
        canonical_work_root=plan.metadata.get("canonical_work_root"),
        target_paths=(entry.target_path for entry in plan.entries),
    )


def snapshot_from_config_manager(config_manager: object) -> TargetRootSnapshot:
    getter = getattr(config_manager, "get_config", None)
    if not callable(getter):
        raise TargetRoutingError("配置管理器缺少 get_config")
    return TargetRootSnapshot.capture({key: getter(key) for key in ROOT_KEYS})
