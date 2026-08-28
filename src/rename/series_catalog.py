"""TV series identity and season bindings, independent from movie catalog."""

from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from .asset_collision import AssetCollisionError, fingerprint_file
from .file_lock import FileLockBusyError, advisory_file_lock
from .plan_store import PlanStore
from .projection_authority import (
    ProjectionAuthorityError,
    validate_plan_projection_receipt,
)
from .utils import is_font_archive


SERIES_CATALOG_SCHEMA_VERSION = 3
SUPPORTED_SERIES_CATALOG_SCHEMA_VERSIONS = frozenset({1, 2, 3})
_SAFE_KEY = re.compile(r"^series-[0-9a-f-]{36}$")


class SeriesCatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeriesMatch:
    schema_version: int
    series_key: str
    revision: int
    canonical_root: Path
    canonical_display_name: str
    season_bindings: tuple[Mapping[str, object], ...]
    associated_assets: tuple[Mapping[str, object], ...] = ()


class SeriesCatalog:
    def __init__(self, root: Path):
        self.root = Path(root)

    @contextmanager
    def lock(self, series_key: str):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / f".{self._path(series_key).name}.lock"
        try:
            with advisory_file_lock(
                lock_path,
                owner={"kind": "series_catalog", "series_key": series_key},
            ):
                yield
        except FileLockBusyError as exc:
            raise SeriesCatalogError("该系列正在由另一个任务更新") from exc

    def load(self, series_key: str) -> SeriesMatch:
        try:
            payload = json.loads(self._path(series_key).read_text("utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise SeriesCatalogError(f"无法读取 SeriesCatalog：{exc}") from exc
        return self._match(payload)

    def validate_archived_payload(
        self, payload: object, expected_key: str
    ) -> None:
        """Validate archived evidence with the same schema contract as load()."""

        match = self._match(payload)
        if match.series_key != expected_key:
            raise SeriesCatalogError("SeriesCatalog series_key 不匹配")

    def validated_associated_locations(
        self,
        series_key: str,
        *,
        scope: str,
        role: str,
    ) -> tuple[Path, ...]:
        """Return only registered locations whose current bytes still match."""

        if scope != "series" or role != "fonts":
            raise SeriesCatalogError("字体资产范围或用途无效")
        match = self.load(series_key)
        assets = match.associated_assets
        if match.schema_version == 2:
            assets = tuple(_upgrade_v2_assets(
                assets,
                canonical_root=match.canonical_root,
            ))
        locations = []
        for asset in assets:
            if not isinstance(asset, Mapping):
                raise SeriesCatalogError("SeriesCatalog associated asset 损坏")
            if _asset_scope(asset) != scope or _asset_role(asset) != role:
                continue
            target = Path(_asset_target(asset))
            if not _is_trusted_existing_within(
                target,
                match.canonical_root / "Associated",
            ):
                raise SeriesCatalogError("字体物理位置越出作品 Associated 目录")
            try:
                evidence = fingerprint_file(target)
            except AssetCollisionError as exc:
                raise SeriesCatalogError(str(exc)) from exc
            if (
                evidence.size != _asset_size(asset)
                or evidence.sha256 != _asset_digest(asset)
            ):
                raise SeriesCatalogError("字体物理位置与登记内容不一致")
            locations.append(target)
        return tuple(sorted(locations, key=_path_key))

    def find_by_provider(self, provider: Mapping[str, object]) -> Optional[SeriesMatch]:
        needle = self._provider_key(provider)
        if needle is None or not self.root.exists():
            return None
        matches = []
        for path in self.root.glob("series-*.json"):
            try:
                payload = json.loads(path.read_text("utf-8"))
                evidence = payload.get("provider_evidence", [])
                if any(self._provider_key(item) == needle for item in evidence if isinstance(item, Mapping)):
                    matches.append(self._match(payload))
            except (OSError, json.JSONDecodeError, SeriesCatalogError):
                continue
        if len(matches) > 1:
            raise SeriesCatalogError("同一 Provider 系列证据指向多个本地 series_key")
        return matches[0] if matches else None

    def resolve_associated_location(
        self,
        series_key: str,
        *,
        scope: str,
        role: str,
        size: int,
        digest: str,
    ) -> Optional[Path]:
        """Return one trusted content location without scanning the media tree."""

        match = self.load(series_key)
        assets = match.associated_assets
        if match.schema_version == 2:
            assets = tuple(_upgrade_v2_assets(
                assets,
                canonical_root=match.canonical_root,
            ))
        if (
            scope != "series"
            or role != "fonts"
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise SeriesCatalogError("字体内容身份无效")
        matches = []
        for asset in assets:
            if not isinstance(asset, Mapping):
                raise SeriesCatalogError("SeriesCatalog associated asset 损坏")
            if (
                _asset_scope(asset) == scope
                and _asset_role(asset) == role
                and _asset_size(asset) == size
                and _asset_digest(asset) == digest
            ):
                matches.append(Path(_asset_target(asset)))
        locations = {_path_key(path): path for path in matches}
        if len(locations) > 1:
            raise SeriesCatalogError("同一字体内容登记了多个物理位置")
        if not locations:
            return None
        target = next(iter(locations.values()))
        associated_root = match.canonical_root / "Associated"
        if not _is_trusted_existing_within(target, associated_root):
            raise SeriesCatalogError("字体物理位置越出作品 Associated 目录")
        try:
            evidence = fingerprint_file(target)
        except AssetCollisionError as exc:
            raise SeriesCatalogError(str(exc)) from exc
        if evidence.size != size or evidence.sha256 != digest:
            raise SeriesCatalogError("字体物理位置与登记内容不一致")
        return target

    def bind_season(
        self,
        series_key: str,
        *,
        expected_revision: int,
        season_number: int,
        provider_identity: Mapping[str, object],
        decision_source: str,
        binding_id: Optional[str] = None,
    ) -> SeriesMatch:
        path = self._path(series_key)
        payload = json.loads(path.read_text("utf-8"))
        if int(payload.get("revision", 0)) != expected_revision:
            raise SeriesCatalogError("SeriesCatalog 版本已变化")
        bindings = list(payload.get("season_bindings", []))
        provider_key = self._provider_key(provider_identity)
        for item in bindings:
            if item.get("season_number") == season_number and self._provider_key(item.get("provider_identity", {})) == provider_key:
                return self._match(payload)
        bindings.append({
            "season_binding_id": binding_id or f"season-binding-{uuid.uuid4()}",
            "season_number": int(season_number),
            "provider_identity": dict(provider_identity),
            "decision_source": decision_source,
        })
        payload["season_bindings"] = bindings
        payload["revision"] = expected_revision + 1
        candidate = self._match(payload)
        self._atomic_write(series_key, payload)
        return candidate

    def register_completed(
        self,
        plan,
        *,
        expected_revision: int = 0,
        completed_ledger: Optional[Mapping[str, object]] = None,
    ) -> SeriesMatch:
        """Persist a TV series fact only after execution has completed."""
        try:
            validate_plan_projection_receipt(plan)
        except (ProjectionAuthorityError, TypeError, ValueError) as exc:
            raise SeriesCatalogError(
                "整理计划权威凭据不一致，拒绝季度登记"
            ) from exc
        metadata = plan.metadata
        series_key = str(metadata.get("series_key") or "")
        binding_id = str(metadata.get("season_binding_id") or "")
        season_number = metadata.get("season_number")
        root_value = str(metadata.get("canonical_work_root") or "")
        if not series_key or not binding_id or not isinstance(season_number, int):
            raise SeriesCatalogError("TV Plan 缺少 SeriesIdentity/SeasonIdentity")
        if plan.work_identity is None or not root_value:
            raise SeriesCatalogError("TV Plan 缺少作品身份或规范根")
        path = self._path(series_key)
        provider = plan.work_identity.primary_provider.to_dict()
        plan_fingerprint = PlanStore.fingerprint(plan)
        manifest_rows = _validated_associated_manifest_rows(
            plan,
            season_number=season_number,
        )
        if plan.metadata.get("associated_asset_manifest") is None:
            has_legacy_fonts = any(
                is_font_archive(entry.source_path) for entry in plan.entries
            )
            if has_legacy_fonts and completed_ledger is None:
                raise SeriesCatalogError(
                    "旧 TV Plan 缺少字体清单，必须提供匹配的 completed execution evidence"
                )
            if has_legacy_fonts:
                manifest_rows = _validated_legacy_associated_rows(
                    plan,
                    completed_ledger,
                    season_number=season_number,
                )
        if path.exists():
            payload = json.loads(path.read_text("utf-8"))
            self._match(payload)
            if int(payload.get("revision", 0)) != expected_revision:
                raise SeriesCatalogError("SeriesCatalog 版本已变化")
        else:
            if expected_revision != 0:
                raise SeriesCatalogError("SeriesCatalog 不存在但期望非零 revision")
            payload = {
                "schema_version": SERIES_CATALOG_SCHEMA_VERSION,
                "series_key": series_key,
                "revision": 0,
                "canonical_root": root_value,
                "canonical_display_name": plan.work_identity.title,
                "domain": plan.work_identity.domain,
                "provider_evidence": [provider],
                "season_bindings": [],
                "materialization_state": "materialized",
                "migration_source": None,
                "associated_assets": [],
            }
        if Path(str(payload["canonical_root"])) != Path(root_value):
            raise SeriesCatalogError("SeriesCatalog 规范根与已执行 Plan 不一致")
        bindings = list(payload.get("season_bindings", []))
        expected_binding = {
            "season_binding_id": binding_id,
            "season_number": season_number,
            "provider_identity": provider,
            "decision_source": str(metadata.get("season_source") or "provider"),
            "task_id": plan.task_id,
        }
        existing_binding = next(
            (
                item for item in bindings
                if item.get("season_binding_id") == binding_id
            ),
            None,
        )
        if existing_binding is None:
            bindings.append(expected_binding)
            binding_changed = True
        elif any(
            existing_binding.get(field) != expected_binding[field]
            for field in expected_binding
        ):
            raise SeriesCatalogError("同一季度登记重放内容不一致")
        else:
            binding_changed = False
        payload["season_bindings"] = bindings
        schema_version = payload.get("schema_version")
        if schema_version == 1:
            associated_assets = []
        elif schema_version == 2:
            associated_assets = _upgrade_v2_assets(
                payload.get("associated_assets", ()),
                canonical_root=Path(str(payload["canonical_root"])),
            )
        else:
            associated_assets = list(payload.get("associated_assets", []))
        asset_changed = False
        for row in manifest_rows:
            if _register_associated_asset(
                associated_assets,
                row=row,
                task_id=plan.task_id,
                plan_fingerprint=plan_fingerprint,
                season_number=season_number,
            ):
                asset_changed = True
        changed = binding_changed or asset_changed
        if not changed:
            # Reading and replaying a valid v1/v2 fact must not rewrite it merely
            # to advance the storage schema.  Migration accompanies a trusted
            # new binding/association only.
            return self._match(payload)
        payload["schema_version"] = SERIES_CATALOG_SCHEMA_VERSION
        payload["associated_assets"] = associated_assets
        payload["revision"] = int(payload.get("revision", 0)) + 1
        payload["materialization_state"] = "materialized"
        candidate = self._match(payload)
        self._atomic_write(series_key, payload)
        return candidate

    def backfill_legacy_completed(
        self,
        series_key: str,
        evidence: tuple[tuple[object, Mapping[str, object]], ...],
    ) -> SeriesMatch:
        """Validate every legacy task in memory, then publish one atomic v3 write."""

        path = self._path(series_key)
        try:
            original = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise SeriesCatalogError(f"无法读取 SeriesCatalog：{exc}") from exc
        current = self._match(original)
        if current.schema_version != 1:
            return current
        if not evidence:
            raise SeriesCatalogError("旧作品季度登记缺少可核对的已完成任务")
        original_bindings = original.get("season_bindings")
        if not isinstance(original_bindings, list) or not original_bindings:
            raise SeriesCatalogError("旧作品季度登记缺少可核对的已完成任务")
        expected_by_binding: dict[str, str] = {}
        expected_task_ids: set[str] = set()
        for binding in original_bindings:
            if not isinstance(binding, Mapping):
                raise SeriesCatalogError("旧作品季度登记损坏")
            binding_id = str(binding.get("season_binding_id") or "")
            task_id = str(binding.get("task_id") or "")
            if (
                not binding_id
                or not task_id
                or binding_id in expected_by_binding
                or task_id in expected_task_ids
            ):
                raise SeriesCatalogError("旧作品季度登记无法与已完成任务一一对应")
            expected_by_binding[binding_id] = task_id
            expected_task_ids.add(task_id)
        candidate = copy.deepcopy(original)
        validated_bindings: set[str] = set()
        validated_task_ids: set[str] = set()
        for plan, completed_ledger in evidence:
            metadata = plan.metadata
            binding_id = str(metadata.get("season_binding_id") or "")
            season_number = metadata.get("season_number")
            root_value = str(metadata.get("canonical_work_root") or "")
            if (
                str(metadata.get("series_key") or "") != series_key
                or not binding_id
                or not isinstance(season_number, int)
                or isinstance(season_number, bool)
                or plan.work_identity is None
                or not root_value
            ):
                raise SeriesCatalogError("旧 TV Plan 缺少匹配的系列或季度身份")
            if (
                expected_by_binding.get(binding_id) != plan.task_id
                or binding_id in validated_bindings
                or plan.task_id in validated_task_ids
            ):
                raise SeriesCatalogError("旧 TV Plan 未与季度登记一一对应")
            if Path(str(candidate.get("canonical_root") or "")) != Path(root_value):
                raise SeriesCatalogError("旧 TV Plan 规范根与系列登记不一致")
            rows = _validated_associated_manifest_rows(
                plan,
                season_number=season_number,
            )
            if metadata.get("associated_asset_manifest") is None:
                rows = _validated_legacy_associated_rows(
                    plan,
                    completed_ledger,
                    season_number=season_number,
                )
            provider = plan.work_identity.primary_provider.to_dict()
            expected_binding = {
                "season_binding_id": binding_id,
                "season_number": season_number,
                "provider_identity": provider,
                "decision_source": str(metadata.get("season_source") or "provider"),
                "task_id": plan.task_id,
            }
            bindings = list(candidate.get("season_bindings", []))
            existing_binding = next(
                (
                    item for item in bindings
                    if isinstance(item, Mapping)
                    and item.get("season_binding_id") == binding_id
                ),
                None,
            )
            binding_changed = existing_binding is None
            if existing_binding is None:
                bindings.append(expected_binding)
            elif any(
                existing_binding.get(field) != expected_binding[field]
                for field in expected_binding
            ):
                raise SeriesCatalogError("旧 TV 季度登记与 completed Plan 不一致")
            assets = list(candidate.get("associated_assets", []))
            asset_changed = False
            fingerprint = PlanStore.fingerprint(plan)
            for row in rows:
                if _register_associated_asset(
                    assets,
                    row=row,
                    task_id=plan.task_id,
                    plan_fingerprint=fingerprint,
                    season_number=season_number,
                ):
                    asset_changed = True
            if binding_changed or asset_changed:
                candidate["schema_version"] = SERIES_CATALOG_SCHEMA_VERSION
                candidate["season_bindings"] = bindings
                candidate["associated_assets"] = assets
                candidate["revision"] = int(candidate.get("revision", 0)) + 1
                candidate["materialization_state"] = "materialized"
            validated_bindings.add(binding_id)
            validated_task_ids.add(plan.task_id)
        if (
            validated_bindings != set(expected_by_binding)
            or validated_task_ids != expected_task_ids
        ):
            raise SeriesCatalogError("旧作品季度登记没有完整的已完成证据")
        if candidate == original:
            return current
        validated = self._match(candidate)
        self._atomic_write(series_key, candidate)
        return validated

    def relocate(
        self,
        series_key: str,
        new_root: Path,
        *,
        allowed_root: Path,
        record_root: Path,
        task_root: Path,
    ) -> SeriesMatch:
        """Verify a manually moved whole series tree, then update only its fact."""

        new_root = Path(new_root)
        allowed_root = Path(allowed_root)
        if not _is_trusted_existing_within(new_root, allowed_root):
            raise SeriesCatalogError("新目录不在当前配置的电视剧目标根内")
        if not new_root.is_dir() or _is_reparse_or_symlink(new_root):
            raise SeriesCatalogError("新目录不存在，或是符号链接/重解析点")
        with self.lock(series_key):
            payload = json.loads(self._path(series_key).read_text("utf-8"))
            match = self._match(payload)
            old_root = match.canonical_root
            if old_root == new_root:
                raise SeriesCatalogError("新旧目录相同，无需重新定位")
            if os.path.lexists(old_root):
                raise SeriesCatalogError("旧登记目录仍存在；请先确认没有重复目录")
            task_ids = _binding_task_ids(payload)
            active = _active_binding_tasks(task_ids, task_root)
            if active:
                raise SeriesCatalogError(
                    f"该系列仍有 {len(active)} 个活动任务，禁止重新定位"
                )
            verified = 0
            for task_id in task_ids:
                ledger_path = Path(record_root) / f"{task_id}.ledger.json"
                try:
                    ledger = json.loads(ledger_path.read_text("utf-8"))
                except (OSError, json.JSONDecodeError, TypeError) as exc:
                    raise SeriesCatalogError(
                        f"无法读取季度 {task_id[:8]} 的 completed ledger"
                    ) from exc
                if ledger.get("task_id") != task_id or ledger.get("status") != "completed":
                    raise SeriesCatalogError(
                        f"季度 {task_id[:8]} 没有可信 completed ledger"
                    )
                entries = ledger.get("entries")
                if not isinstance(entries, list) or not entries:
                    raise SeriesCatalogError(
                        f"季度 {task_id[:8]} 的 ledger 没有资产清单"
                    )
                for entry in entries:
                    if not isinstance(entry, Mapping):
                        raise SeriesCatalogError("execution ledger 资产条目损坏")
                    old_target = Path(str(entry.get("target") or ""))
                    source = Path(str(entry.get("source") or ""))
                    try:
                        relative = old_target.relative_to(old_root)
                    except ValueError as exc:
                        raise SeriesCatalogError("ledger 资产越出原系列目录") from exc
                    new_target = new_root / relative
                    if not source.is_file() or not new_target.is_file():
                        raise SeriesCatalogError(f"重新定位后资产缺失：{relative}")
                    status = entry.get("status")
                    transfer_type = entry.get("transfer_type")
                    if status == "reused_identical_asset":
                        if transfer_type != "reuse_existing":
                            raise SeriesCatalogError(
                                f"既有资产复用记录不一致：{relative}"
                            )
                        try:
                            source_evidence = fingerprint_file(source)
                            target_evidence = fingerprint_file(new_target)
                        except AssetCollisionError as exc:
                            raise SeriesCatalogError(str(exc)) from exc
                        if (
                            source_evidence.size != entry.get("source_size")
                            or source_evidence.sha256 != entry.get("source_sha256")
                            or source_evidence.device != entry.get("source_device")
                            or source_evidence.inode != entry.get("source_inode")
                            or target_evidence.size != entry.get("target_size")
                            or target_evidence.sha256 != entry.get("target_sha256")
                            or target_evidence.device != entry.get("target_device")
                            or target_evidence.inode != entry.get("target_inode")
                            or source_evidence.sha256 != target_evidence.sha256
                        ):
                            raise SeriesCatalogError(
                                f"既有资产复用证明已变化：{relative}"
                            )
                    elif status in {None, "created"}:
                        if transfer_type not in {None, "hardlink"}:
                            raise SeriesCatalogError(
                                f"资产执行方式不是 hardlink：{relative}"
                            )
                        source_stat = source.stat()
                        target_stat = new_target.stat()
                        if (
                            source_stat.st_dev != target_stat.st_dev
                            or source_stat.st_ino != target_stat.st_ino
                            or target_stat.st_nlink < 2
                        ):
                            raise SeriesCatalogError(
                                f"资产不再满足原 hardlink 证明：{relative}"
                            )
                    else:
                        raise SeriesCatalogError(
                            f"execution ledger 资产状态不可信：{relative}"
                        )
                    verified += 1
            if verified == 0:
                raise SeriesCatalogError("系列登记没有可核对的已执行资产")
            catalog_assets = payload.get("associated_assets", [])
            if payload.get("schema_version") == 2:
                catalog_assets = _upgrade_v2_assets(
                    catalog_assets,
                    canonical_root=old_root,
                )
            relocated_assets = []
            for asset in catalog_assets:
                if not isinstance(asset, Mapping):
                    raise SeriesCatalogError("SeriesCatalog associated asset 损坏")
                old_target = Path(_asset_target(asset))
                try:
                    relative = old_target.relative_to(old_root)
                except ValueError as exc:
                    raise SeriesCatalogError(
                        "SeriesCatalog associated asset 越出原系列目录"
                    ) from exc
                new_target = new_root / relative
                try:
                    evidence = fingerprint_file(new_target)
                except AssetCollisionError as exc:
                    raise SeriesCatalogError(str(exc)) from exc
                if (
                    evidence.size != _asset_size(asset)
                    or evidence.sha256 != _asset_digest(asset)
                ):
                    raise SeriesCatalogError(
                        f"重新定位后的字体资产与登记不一致：{relative}"
                    )
                relocated = dict(asset)
                if isinstance(asset.get("location"), Mapping):
                    relocated["location"] = {
                        **dict(asset["location"]),
                        "target_path": str(new_target),
                    }
                    relocated.pop("target_path", None)
                else:
                    relocated["target_path"] = str(new_target)
                relocated_assets.append(relocated)
            payload.update({
                "schema_version": SERIES_CATALOG_SCHEMA_VERSION,
                "revision": int(payload.get("revision", 0)) + 1,
                "canonical_root": str(new_root),
                "relocated_from": str(old_root),
                "updated_at": _utc_now(),
                "associated_assets": relocated_assets,
            })
            candidate = self._match(payload)
            self._atomic_write(series_key, payload)
            return candidate

    def archive_missing(self, series_key: str, *, task_root: Path) -> Path:
        """Archive a stale series registration without touching media files."""

        with self.lock(series_key):
            path = self._path(series_key)
            payload = json.loads(path.read_text("utf-8"))
            match = self._match(payload)
            if os.path.lexists(match.canonical_root):
                raise SeriesCatalogError(
                    "原登记目录仍存在，不能删除登记；如已移动请选择“原目录已移动”"
                )
            active = _active_binding_tasks(_binding_task_ids(payload), task_root)
            if active:
                raise SeriesCatalogError(
                    f"该系列仍有 {len(active)} 个活动任务，禁止删除登记"
                )
            archive_root = self.root / ".deleted"
            archive_root.mkdir(parents=True, exist_ok=True)
            archive_path = archive_root / (
                f"{series_key}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
                f".{uuid.uuid4().hex}.json"
            )
            try:
                os.replace(path, archive_path)
            except OSError as exc:
                raise SeriesCatalogError(f"无法归档并删除系列登记：{exc}") from exc
            return archive_path

    def shadow_legacy_record(self, value: object) -> dict:
        """Classify only; never write or allocate a series key."""
        if not isinstance(value, Mapping):
            return {"classification": "damaged", "reason": "record_not_object"}
        if value.get("group_kind") != "tv_episodes":
            return {"classification": "not_tv", "reason": "group_kind"}
        providers = value.get("providers", [])
        roots = str(value.get("canonical_root") or "").strip()
        strong = [p for p in providers if isinstance(p, Mapping) and p.get("namespace") == "tmdb" and p.get("entity_type") == "tv" and p.get("provider_id")]
        if roots and len({self._provider_key(p) for p in strong}) == 1 and strong:
            return {"classification": "exact", "reason": "single_tmdb_tv_provider"}
        return {"classification": "manual_required", "reason": "insufficient_series_evidence"}

    def _path(self, series_key: str) -> Path:
        if _SAFE_KEY.fullmatch(str(series_key)) is None:
            raise SeriesCatalogError("series_key 无效")
        return self.root / f"{series_key}.json"

    def _atomic_write(self, key: str, payload: dict) -> None:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _provider_key(value: Mapping[str, object]) -> Optional[tuple[str, str, str]]:
        fields = tuple(str(value.get(k) or "") for k in ("namespace", "entity_type", "provider_id"))
        return fields if all(fields) else None

    @staticmethod
    def _match(payload: Mapping[str, object]) -> SeriesMatch:
        if not isinstance(payload, Mapping):
            raise SeriesCatalogError("SeriesCatalog 根节点不是对象")
        schema_version = payload.get("schema_version")
        if schema_version not in SUPPORTED_SERIES_CATALOG_SCHEMA_VERSIONS:
            raise SeriesCatalogError("SeriesCatalog schema_version 不受支持")
        series_key = str(payload.get("series_key") or "")
        if _SAFE_KEY.fullmatch(series_key) is None:
            raise SeriesCatalogError("SeriesCatalog series_key 无效")
        revision = payload.get("revision")
        if not isinstance(revision, int) or revision < 0:
            raise SeriesCatalogError("SeriesCatalog revision 无效")
        if not str(payload.get("canonical_root") or "").strip():
            raise SeriesCatalogError("SeriesCatalog 缺少 canonical_root")
        if not str(payload.get("canonical_display_name") or "").strip():
            raise SeriesCatalogError("SeriesCatalog 缺少 canonical_display_name")
        if not isinstance(payload.get("provider_evidence"), list):
            raise SeriesCatalogError("SeriesCatalog provider_evidence 损坏")
        if not isinstance(payload.get("season_bindings"), list):
            raise SeriesCatalogError("SeriesCatalog season_bindings 损坏")
        associated_assets = payload.get("associated_assets", [])
        if schema_version in {2, 3} and not isinstance(associated_assets, list):
            raise SeriesCatalogError("SeriesCatalog associated_assets 损坏")
        if schema_version == 3:
            identities = set()
            locations = set()
            association_ids = set()
            for asset in associated_assets:
                _validate_v3_asset(asset)
                identity = (
                    _asset_scope(asset),
                    _asset_role(asset),
                    _asset_content_id(asset),
                )
                if identity in identities:
                    raise SeriesCatalogError("SeriesCatalog 字体内容身份重复")
                identities.add(identity)
                location = _path_key(Path(_asset_target(asset)))
                if location in locations:
                    raise SeriesCatalogError("SeriesCatalog 字体物理位置重复")
                locations.add(location)
                if not _is_within(
                    Path(_asset_target(asset)),
                    Path(str(payload["canonical_root"])) / "Associated",
                ):
                    raise SeriesCatalogError("SeriesCatalog 字体物理位置越界")
                for association in asset.get("associations", ()):
                    association_id = str(association.get("association_id") or "")
                    if association_id in association_ids:
                        raise SeriesCatalogError("SeriesCatalog 字体关联身份重复")
                    association_ids.add(association_id)
        return SeriesMatch(
            schema_version=schema_version,
            series_key=series_key,
            revision=revision,
            canonical_root=Path(str(payload["canonical_root"])),
            canonical_display_name=str(payload["canonical_display_name"]),
            season_bindings=tuple(payload.get("season_bindings", ())),
            associated_assets=tuple(associated_assets),
        )


def _validated_associated_manifest_rows(
    plan,
    *,
    season_number: int,
) -> tuple[dict[str, object], ...]:
    manifest = plan.metadata.get("associated_asset_manifest")
    if manifest is None:
        return ()
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        raise SeriesCatalogError("TV Plan 字体来源清单格式无效")
    rows = manifest.get("assets")
    if not isinstance(rows, (list, tuple)):
        raise SeriesCatalogError("TV Plan 字体来源清单资产无效")
    entries = {
        (_path_key(entry.source_path), _path_key(entry.target_path)): entry
        for entry in plan.entries
    }
    canonical_root = Path(str(plan.metadata.get("canonical_work_root") or ""))
    validated = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise SeriesCatalogError("TV Plan 字体来源清单条目无效")
        source = Path(str(raw.get("source_path") or ""))
        target = Path(str(raw.get("target_path") or ""))
        size = raw.get("source_size")
        digest = str(raw.get("source_sha256") or "")
        if (
            raw.get("role") != "fonts"
            or raw.get("scope") != "series"
            or raw.get("season_number") != season_number
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or (_path_key(source), _path_key(target)) not in entries
            or not _is_trusted_existing_within(
                target, canonical_root / "Associated"
            )
        ):
            raise SeriesCatalogError("TV Plan 字体来源清单与整理计划不一致")
        try:
            source_evidence = fingerprint_file(source)
            target_evidence = fingerprint_file(target)
        except AssetCollisionError as exc:
            raise SeriesCatalogError(str(exc)) from exc
        if (
            source_evidence.size != size
            or source_evidence.sha256 != digest
            or target_evidence.size != size
            or target_evidence.sha256 != digest
        ):
            raise SeriesCatalogError("字体源文件或目标与已批准清单不一致")
        validated.append({
            "role": "fonts",
            "scope": "series",
            "source_path": str(source),
            "target_path": str(target),
            "size": size,
            "sha256": digest,
        })
    return tuple(validated)


def _validated_legacy_associated_rows(
    plan,
    completed_ledger: Mapping[str, object],
    *,
    season_number: int,
) -> tuple[dict[str, object], ...]:
    """Recover only font facts proven by one exact historical execution."""

    fingerprint = PlanStore.fingerprint(plan)
    if (
        not isinstance(completed_ledger, Mapping)
        or completed_ledger.get("status") != "completed"
        or completed_ledger.get("task_id") != plan.task_id
        or completed_ledger.get("plan_fingerprint") != fingerprint
    ):
        raise SeriesCatalogError("旧 TV Plan 的 completed execution evidence 不匹配")
    ledger_entries = completed_ledger.get("entries")
    if not isinstance(ledger_entries, list):
        raise SeriesCatalogError("旧 TV Plan 的 execution entries 损坏")
    expected_pairs = {
        (_path_key(entry.source_path), _path_key(entry.target_path))
        for entry in plan.entries
    }
    recorded_pairs = {
        (
            _path_key(Path(str(item.get("source") or ""))),
            _path_key(Path(str(item.get("target") or ""))),
        )
        for item in ledger_entries
        if isinstance(item, Mapping)
    }
    if (
        len(ledger_entries) != len(plan.entries)
        or len(recorded_pairs) != len(ledger_entries)
        or recorded_pairs != expected_pairs
    ):
        raise SeriesCatalogError("旧 TV Plan 与 execution entries 不完整一致")
    font_entries = [
        entry for entry in plan.entries if is_font_archive(entry.source_path)
    ]
    if not font_entries:
        return ()
    if len(font_entries) != 1:
        raise SeriesCatalogError("旧 TV Plan 的字体条目不是唯一值")
    entry = font_entries[0]
    recorded = next(
        item for item in ledger_entries
        if _path_key(Path(str(item.get("source") or "")))
        == _path_key(entry.source_path)
        and _path_key(Path(str(item.get("target") or "")))
        == _path_key(entry.target_path)
    )
    if (
        recorded.get("status") not in {None, "created"}
        or recorded.get("transfer_type") != "hardlink"
    ):
        raise SeriesCatalogError("旧 TV 字体没有可信 hardlink execution evidence")
    canonical_root = Path(str(plan.metadata.get("canonical_work_root") or ""))
    if (
        not _is_within(entry.source_path, plan.source_root)
        or not _is_trusted_existing_within(
            entry.target_path, canonical_root / "Associated"
        )
    ):
        raise SeriesCatalogError("旧 TV 字体来源或目标越出批准边界")
    try:
        source = fingerprint_file(entry.source_path)
        target = fingerprint_file(entry.target_path)
        target_stat = entry.target_path.stat()
    except (AssetCollisionError, OSError) as exc:
        raise SeriesCatalogError(str(exc)) from exc
    if (
        source.device != target.device
        or source.inode != target.inode
        or target_stat.st_nlink < 2
        or source.size != target.size
        or source.sha256 != target.sha256
        or recorded.get("source_device") != source.device
        or recorded.get("target_device") != target.device
        or recorded.get("source_inode") != source.inode
        or recorded.get("target_inode") != target.inode
        or _recorded_value_changed(recorded, "source_size", source.size)
        or _recorded_value_changed(recorded, "target_size", target.size)
        or _recorded_value_changed(recorded, "source_sha256", source.sha256)
        or _recorded_value_changed(recorded, "target_sha256", target.sha256)
        or recorded.get("target_link_count", 0) < 2
    ):
        raise SeriesCatalogError("旧 TV 字体 hardlink/content evidence 已变化")
    return ({
        "role": "fonts",
        "scope": "series",
        "source_path": str(entry.source_path),
        "target_path": str(entry.target_path),
        "size": source.size,
        "sha256": source.sha256,
    },)


def _recorded_value_changed(
    recorded: Mapping[str, object], key: str, current: object
) -> bool:
    """Compare optional evidence without inventing fields absent from old ledgers."""

    value = recorded.get(key)
    return value is not None and value != current


def _upgrade_v2_assets(
    assets: object,
    *,
    canonical_root: Path,
) -> list[dict[str, object]]:
    if not isinstance(assets, (list, tuple)):
        raise SeriesCatalogError("SeriesCatalog associated_assets 损坏")
    upgraded: dict[tuple[str, str, str], dict[str, object]] = {}
    for raw in assets:
        if not isinstance(raw, Mapping):
            raise SeriesCatalogError("SeriesCatalog associated asset 损坏")
        target = Path(str(raw.get("target_path") or ""))
        size = raw.get("size")
        digest = str(raw.get("sha256") or "")
        associations = raw.get("associations")
        if (
            raw.get("role") != "fonts"
            or raw.get("scope") != "series"
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or raw.get("asset_id")
            != _associated_asset_id(target, size=size, digest=digest)
            or not isinstance(associations, list)
            or not all(isinstance(item, Mapping) for item in associations)
            or not _is_trusted_existing_within(
                target, canonical_root / "Associated"
            )
        ):
            raise SeriesCatalogError("SeriesCatalog v2 字体资产损坏")
        _validate_associations(
            associations,
            size=size,
            digest=digest,
            message="SeriesCatalog v2 字体关联损坏",
        )
        try:
            evidence = fingerprint_file(target)
        except AssetCollisionError as exc:
            raise SeriesCatalogError(str(exc)) from exc
        if evidence.size != size or evidence.sha256 != digest:
            raise SeriesCatalogError("SeriesCatalog v2 字体位置与内容不一致")
        content_id = _content_id(size=size, digest=digest)
        key = ("series", "fonts", content_id)
        previous = upgraded.get(key)
        if previous is not None:
            if _path_key(Path(str(previous["target_path"]))) != _path_key(target):
                raise SeriesCatalogError("同一字体内容登记了多个物理位置")
            known_ids = {
                str(item.get("association_id") or "")
                for item in previous["associations"]
                if isinstance(item, Mapping)
            }
            for association in associations:
                association_id = str(association.get("association_id") or "")
                if association_id in known_ids:
                    raise SeriesCatalogError("SeriesCatalog v2 字体关联重复")
                previous["associations"].append(dict(association))
            continue
        upgraded[key] = {
            "asset_id": content_id,
            "content_id": content_id,
            "role": "fonts",
            "scope": "series",
            "target_path": str(target),
            "size": size,
            "sha256": digest,
            "associations": [dict(item) for item in associations],
        }
    return list(upgraded.values())


def _validate_v3_asset(asset: object) -> None:
    if not isinstance(asset, Mapping):
        raise SeriesCatalogError("SeriesCatalog v3 associated asset 损坏")
    size = _asset_size(asset)
    digest = _asset_digest(asset)
    content_id = _asset_content_id(asset)
    target = _asset_target(asset)
    associations = asset.get("associations")
    if (
        _asset_scope(asset) != "series"
        or _asset_role(asset) != "fonts"
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or content_id != _content_id(size=size, digest=digest)
        or _asset_id(asset) != content_id
        or not target
        or not isinstance(associations, list)
        or not associations
    ):
        raise SeriesCatalogError("SeriesCatalog v3 字体身份或位置损坏")
    _validate_associations(
        associations,
        size=size,
        digest=digest,
        message="SeriesCatalog v3 字体关联损坏",
    )


def _validate_associations(
    associations: object,
    *,
    size: int,
    digest: str,
    message: str,
) -> None:
    if not isinstance(associations, list) or not associations:
        raise SeriesCatalogError(message)
    seen_associations = set()
    for association in associations:
        if not isinstance(association, Mapping):
            raise SeriesCatalogError(message)
        association_id = str(association.get("association_id") or "")
        task_id = str(association.get("task_id") or "")
        plan_fingerprint = str(association.get("plan_fingerprint") or "")
        source_path = str(association.get("source_path") or "")
        season_number = association.get("season_number")
        if (
            re.fullmatch(r"[0-9a-f]{64}", association_id) is None
            or association_id in seen_associations
            or not task_id
            or re.fullmatch(r"[0-9a-f]{64}", plan_fingerprint) is None
            or not source_path
            or not isinstance(season_number, int)
            or isinstance(season_number, bool)
            or season_number < 1
            or association.get("source_size") != size
            or association.get("source_sha256") != digest
        ):
            raise SeriesCatalogError(message)
        seen_associations.add(association_id)


def _asset_identity(asset: Mapping[str, object]) -> Mapping[str, object]:
    nested = asset.get("physical_identity")
    return nested if isinstance(nested, Mapping) else asset


def _asset_location(asset: Mapping[str, object]) -> Mapping[str, object]:
    nested = asset.get("location")
    return nested if isinstance(nested, Mapping) else asset


def _asset_content_id(asset: Mapping[str, object]) -> str:
    return str(_asset_identity(asset).get("content_id") or "")


def _asset_id(asset: Mapping[str, object]) -> str:
    return str(asset.get("asset_id") or _asset_identity(asset).get("asset_id") or "")


def _asset_scope(asset: Mapping[str, object]) -> str:
    return str(_asset_identity(asset).get("scope") or asset.get("scope") or "")


def _asset_role(asset: Mapping[str, object]) -> str:
    return str(_asset_identity(asset).get("role") or asset.get("role") or "")


def _asset_size(asset: Mapping[str, object]):
    return _asset_identity(asset).get("size")


def _asset_digest(asset: Mapping[str, object]) -> str:
    return str(_asset_identity(asset).get("sha256") or "")


def _asset_target(asset: Mapping[str, object]) -> str:
    return str(_asset_location(asset).get("target_path") or "")


def _content_id(*, size: int, digest: str) -> str:
    return hashlib.sha256(f"{size}\0{digest}".encode("utf-8")).hexdigest()


def _register_associated_asset(
    assets: list,
    *,
    row: Mapping[str, object],
    task_id: str,
    plan_fingerprint: str,
    season_number: int,
) -> bool:
    target_key = _path_key(Path(str(row["target_path"])))
    source_key = _path_key(Path(str(row["source_path"])))
    size = int(row["size"])
    digest = str(row["sha256"])
    content_id = _content_id(size=size, digest=digest)
    association_id = hashlib.sha256(
        f"{task_id}\0{plan_fingerprint}\0{source_key}".encode("utf-8")
    ).hexdigest()
    expected_association = {
        "association_id": association_id,
        "season_number": season_number,
        "task_id": task_id,
        "plan_fingerprint": plan_fingerprint,
        "source_path": str(row["source_path"]),
        "source_size": size,
        "source_sha256": digest,
    }
    physical = None
    for item in assets:
        if not isinstance(item, Mapping):
            raise SeriesCatalogError("SeriesCatalog associated asset 损坏")
        associations = item.get("associations")
        if not isinstance(associations, list):
            raise SeriesCatalogError("SeriesCatalog associated asset associations 损坏")
        for association in associations:
            if not isinstance(association, Mapping):
                raise SeriesCatalogError("SeriesCatalog associated asset association 损坏")
            same_task_source = (
                str(association.get("task_id") or "") == task_id
                and _path_key(Path(str(association.get("source_path") or "")))
                == source_key
            )
            same_id = association.get("association_id") == association_id
            if same_task_source or same_id:
                if (
                    dict(association) != expected_association
                    or _path_key(Path(_asset_target(item)))
                    != target_key
                    or _asset_size(item) != size
                    or _asset_digest(item) != digest
                ):
                    raise SeriesCatalogError("同一字体季度关联重放内容不一致")
                return False
        item_target_key = _path_key(Path(_asset_target(item)))
        item_content_id = _content_id(
            size=_asset_size(item), digest=_asset_digest(item)
        )
        if item_content_id == content_id:
            if (
                item_target_key != target_key
                or _asset_role(item) != "fonts"
                or _asset_scope(item) != "series"
                or _asset_size(item) != size
                or _asset_digest(item) != digest
            ):
                raise SeriesCatalogError("同一字体内容存在多个物理位置")
            physical = item
        elif item_target_key == target_key:
            raise SeriesCatalogError("同一字体目标的内容身份发生冲突")
    if physical is None:
        assets.append({
            "asset_id": content_id,
            "content_id": content_id,
            "role": "fonts",
            "scope": "series",
            "target_path": str(row["target_path"]),
            "size": size,
            "sha256": digest,
            "associations": [expected_association],
        })
    else:
        physical["associations"].append(expected_association)
    return True


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(Path(path)))


def _associated_asset_id(target: Path, *, size: int, digest: str) -> str:
    return hashlib.sha256(
        f"{_path_key(target)}\0{size}\0{digest}".encode("utf-8")
    ).hexdigest()


def _binding_task_ids(payload: Mapping[str, object]) -> tuple[str, ...]:
    bindings = payload.get("season_bindings")
    if not isinstance(bindings, list):
        raise SeriesCatalogError("SeriesCatalog season_bindings 损坏")
    task_ids = tuple(dict.fromkeys(
        str(item.get("task_id") or "").strip()
        for item in bindings
        if isinstance(item, Mapping) and item.get("task_id")
    ))
    if not task_ids:
        raise SeriesCatalogError("SeriesCatalog 缺少可核对的已完成任务")
    return task_ids


def _active_binding_tasks(task_ids: tuple[str, ...], task_root: Path) -> list[str]:
    active = []
    active_states = {
        "queued", "running", "executing", "approved", "need_confirm",
        "catalog_update_pending",
    }
    for task_id in task_ids:
        path = Path(task_root) / f"{task_id}.json"
        try:
            task = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if task.get("workflow_status") in active_states:
            active.append(task_id)
    return active


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
        return True
    except ValueError:
        return False


def _is_trusted_existing_within(path: Path, root: Path) -> bool:
    """Reject lexical escapes and symlink/reparse traversal for stored locations."""

    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    if not _is_within(path, root):
        return False
    try:
        if not root.is_dir() or not path.exists():
            return False
        current = path
        while True:
            if _is_reparse_or_symlink(current):
                return False
            if current == root:
                break
            if current.parent == current:
                return False
            current = current.parent
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & 0x400)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
