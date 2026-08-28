"""Execution-backed local catalog for incrementally assembled media works."""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
import uuid
from contextlib import contextmanager
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

from ..utils.path import WORK_CATALOG_PATH
from .file_lock import FileLockBusyError, advisory_file_lock
from .rename_plan import ProviderIdentity, RenamePlan
from .plan_store import PlanStore
from .projection_authority import (
    ProjectionAuthorityError,
    validate_plan_projection_receipt,
)
from .asset_collision import AssetCollisionError, fingerprint_file
from .font_archive_contract import (
    FontArchiveContractError,
    validate_completed_movie_font_assets,
)


WORK_CATALOG_SCHEMA_VERSION = 4
_SUPPORTED_WORK_CATALOG_SCHEMAS = {1, 2, 3, WORK_CATALOG_SCHEMA_VERSION}
_WORK_KEY = re.compile(r"^work-[0-9a-fA-F-]{32,36}$")


class WorkCatalogError(RuntimeError):
    pass


def catalog_record_fingerprint(payload: Mapping[str, object]) -> str:
    """Return a stable fingerprint for runtime sibling-append authority."""

    canonical = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CatalogMatch:
    work_key: str
    revision: int
    title: str
    year: Optional[int]
    domain: str
    canonical_root: Path
    versions: tuple[Mapping[str, object], ...]
    group_kind: str = "movie_continuous"
    seasons: tuple[Mapping[str, object], ...] = ()


class WorkCatalog:
    def __init__(self, root: Path = WORK_CATALOG_PATH) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_root = self.root / ".locks"
        self.lock_root.mkdir(parents=True, exist_ok=True)

    def load(self, work_key: str) -> dict:
        path = self._path(work_key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkCatalogError(f"无法读取作品登记：{exc}") from exc
        self._validate(payload, work_key)
        return payload

    def validate_archived_payload(
        self, payload: object, expected_key: str
    ) -> None:
        """Validate archived evidence with the same schema contract as load()."""

        self._validate(payload, expected_key)

    def get_match(self, work_key: str) -> CatalogMatch:
        return _as_match(self.load(work_key))

    def find_exact(
        self, provider: ProviderIdentity, *, group_kind: Optional[str] = None
    ) -> Optional[CatalogMatch]:
        key = _provider_key(provider)
        if key is None:
            return None
        matches = []
        for path in sorted(self.root.glob("work-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self._validate(payload, str(payload.get("work_key") or ""))
            except (OSError, json.JSONDecodeError, WorkCatalogError):
                continue
            if group_kind and payload.get("group_kind") != group_kind:
                continue
            if any(_provider_mapping_key(item) == key for item in payload["providers"]):
                matches.append(payload)
        if len(matches) > 1:
            raise WorkCatalogError("同一资料条目指向多个本地作品，必须先修复登记")
        return _as_match(matches[0]) if matches else None

    def suggestions(
        self,
        title: str,
        year: Optional[int],
        aliases: Iterable[str] = (),
        *,
        group_kind: Optional[str] = None,
        include_related_series: bool = False,
    ) -> tuple[CatalogMatch, ...]:
        wanted = {_canonical_title(title), *(_canonical_title(x) for x in aliases)}
        wanted.discard("")
        results = []
        for path in sorted(self.root.glob("work-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self._validate(payload, str(payload.get("work_key") or ""))
            except (OSError, json.JSONDecodeError, WorkCatalogError):
                continue
            if group_kind and payload.get("group_kind") != group_kind:
                continue
            names = {
                _canonical_title(str(payload.get("title") or "")),
                *(_canonical_title(str(x)) for x in payload.get("aliases", [])),
            }
            same_year = year is None or payload.get("year") is None or payload.get("year") == year
            related = (
                include_related_series
                and any(_titles_related(left, right) for left in wanted for right in names)
            )
            if (wanted.intersection(names) or related) and same_year:
                results.append(_as_match(payload))
        return tuple(results)

    def validated_font_locations(self, work_key: str) -> tuple[Path, ...]:
        """Return every trusted work-scoped font location after byte checks."""

        return tuple(path for _asset, path in self._validated_font_assets(work_key))

    def resolve_font_location(
        self, work_key: str, *, size: int, digest: str
    ) -> Optional[Path]:
        wanted = f"sha256:{int(size)}:{str(digest).casefold()}"
        matches = [
            path for asset, path in self._validated_font_assets(work_key)
            if asset.get("content_key") == wanted
        ]
        if len(matches) > 1:
            raise WorkCatalogError("同一电影字体内容登记了多个位置，必须先人工核查")
        return matches[0] if matches else None

    def _validated_font_assets(
        self, work_key: str
    ) -> tuple[tuple[Mapping[str, object], Path], ...]:
        payload = self.load(work_key)
        root = Path(str(payload.get("canonical_root") or ""))
        results = []
        locations_by_content: dict[str, set[str]] = {}
        for asset in _upgrade_assets(payload.get("assets", [])):
            associations = asset.get("associations")
            strict = [
                item for item in associations
                if isinstance(item, Mapping)
                and item.get("media_kind") == "associated"
                and item.get("role") == "font"
                and item.get("owner_version_ref") == "shared"
            ] if isinstance(associations, list) else []
            if not strict:
                continue
            content_key = str(asset.get("content_key") or "")
            size = asset.get("size")
            digest = str(asset.get("sha256") or "").casefold()
            target = Path(str(asset.get("target") or ""))
            if (
                asset.get("scope") != "shared"
                or not isinstance(size, int)
                or isinstance(size, bool)
                or content_key != f"sha256:{size}:{digest}"
                or not _is_within(target, root / "Associated")
                or not target.is_file()
                or _is_reparse_or_symlink(target)
            ):
                raise WorkCatalogError("电影字体登记证据损坏，必须先人工核查")
            try:
                evidence = fingerprint_file(target)
            except AssetCollisionError as exc:
                raise WorkCatalogError(str(exc)) from exc
            if evidence.size != size or evidence.sha256 != digest:
                raise WorkCatalogError("电影字体登记位置的内容已变化，必须先人工核查")
            locations_by_content.setdefault(content_key, set()).add(
                os.path.normcase(os.path.abspath(target))
            )
            results.append((asset, target))
        if any(len(paths) != 1 for paths in locations_by_content.values()):
            raise WorkCatalogError("同一电影字体内容登记了多个位置，必须先人工核查")
        return tuple(results)

    def materializes_completed_plan(
        self,
        payload: Mapping[str, object],
        plan: RenamePlan,
        ledger: Mapping[str, object],
    ) -> bool:
        """Prove that one persisted record exactly contains this completed Plan."""

        work_key = str(
            plan.metadata.get("catalog_work_key")
            or plan.metadata.get("proposed_work_key")
            or ""
        )
        try:
            self._validate(payload, work_key)
            fonts = validate_completed_movie_font_assets(plan, ledger)
        except (WorkCatalogError, FontArchiveContractError):
            return False
        if any((
            ledger.get("status") != "completed",
            ledger.get("task_id") != plan.task_id,
            ledger.get("plan_fingerprint") != PlanStore.fingerprint(plan),
        )):
            return False
        if not self.compatible_base(payload, plan):
            return False
        rows = ledger.get("entries")
        if not isinstance(rows, list) or len(rows) != len(plan.entries):
            return False
        rows_by_pair: dict[tuple[str, str], list[Mapping[str, object]]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                return False
            pair = (str(row.get("source") or ""), str(row.get("target") or ""))
            rows_by_pair.setdefault(pair, []).append(row)
        assets = _upgrade_assets(payload.get("assets", []))
        conditional_fonts = _plan_conditional_font_assets(plan)
        for entry in plan.entries:
            contract = entry.presentation_asset
            expected = {
                "source": str(entry.source_path),
                "task_id": plan.task_id,
                "owner_version_ref": contract.owner_version_ref if contract else None,
                "media_kind": entry.media_kind,
                "role": entry.role,
            }
            matching = [asset for asset in assets if str(asset.get("target")) == str(entry.target_path)]
            if len(matching) != 1:
                return False
            associations = matching[0].get("associations")
            if not isinstance(associations, list) or expected not in associations:
                return False
            ledger_rows = rows_by_pair.get(
                (str(entry.source_path), str(entry.target_path)), []
            )
            if len(ledger_rows) != 1:
                return False
            row = ledger_rows[0]
            if entry.collision_resolution is None:
                if row.get("status") == "reused_identical_asset":
                    authority = row.get("conditional_authority")
                    approved_content = conditional_fonts.get((
                        _path_key(entry.source_path),
                        _path_key(entry.target_path),
                    ))
                    if not isinstance(authority, Mapping) or any((
                        approved_content is None,
                        row.get("action") != "reuse_existing",
                        row.get("transfer_type") != "reuse_existing",
                        authority.get("policy") != "completed-sibling-append-v1",
                        authority.get("work_key") != work_key,
                        authority.get("content_key") != approved_content,
                        _path_key(Path(str(authority.get("target_path") or "")))
                        != _path_key(entry.target_path),
                    )):
                        return False
                    continue
                if any((
                    row.get("status") != "created",
                    row.get("action") != "create",
                    row.get("transfer_type") != "hardlink",
                )):
                    return False
                try:
                    source_stat = entry.source_path.stat()
                    target_stat = entry.target_path.stat()
                except OSError:
                    return False
                if (
                    source_stat.st_dev != target_stat.st_dev
                    or source_stat.st_ino != target_stat.st_ino
                    or target_stat.st_nlink < 2
                ):
                    return False
                if not _ledger_identity_matches_current(
                    row, source_stat=source_stat, target_stat=target_stat
                ):
                    return False
            elif any((
                row.get("status") != "reused_identical_asset",
                row.get("action") != "reuse_existing",
                row.get("transfer_type") != "reuse_existing",
            )):
                return False
            else:
                try:
                    source_stat = entry.source_path.stat()
                    target_stat = entry.target_path.stat()
                except OSError:
                    return False
                if not _ledger_identity_matches_current(
                    row, source_stat=source_stat, target_stat=target_stat
                ):
                    return False
            contract = entry.presentation_asset
            if contract is not None and contract.asset_role.kind == "content_version":
                descriptor = contract.version_descriptor
                expected_version = {
                    "target": str(entry.target_path),
                    "source": str(entry.source_path),
                    "version_ref": descriptor.version_ref if descriptor else None,
                    "technical_variant_key": (
                        descriptor.technical_variant_key if descriptor else None
                    ),
                    "display_label": descriptor.display_label if descriptor else None,
                    "task_id": plan.task_id,
                    "ledger_fingerprint": str(ledger.get("plan_fingerprint") or ""),
                }
                if expected_version not in payload.get("versions", []):
                    return False
        for item in fonts:
            matching = next(
                (asset for asset in assets if str(asset.get("target")) == str(item.entry.target_path)),
                None,
            )
            if not isinstance(matching, Mapping) or any((
                matching.get("content_key") != item.content_key,
                matching.get("size") != item.size,
                matching.get("sha256") != item.sha256,
                matching.get("scope") != "shared",
            )):
                return False
        return True

    def compatible_base(
        self,
        payload: Mapping[str, object],
        plan: RenamePlan,
        *,
        allow_explicit_provider_append: bool = False,
    ) -> bool:
        """Check immutable work authority before appending a new revision."""

        work_key = str(
            plan.metadata.get("catalog_work_key")
            or plan.metadata.get("proposed_work_key")
            or ""
        )
        try:
            self._validate(payload, work_key)
        except WorkCatalogError:
            return False
        group_kinds = {group.kind for group in plan.presentation_groups}
        if len(group_kinds) != 1 or payload.get("group_kind") != next(iter(group_kinds)):
            return False
        expected_root = str(plan.metadata.get("canonical_work_root") or "")
        if expected_root and (
            os.path.normcase(os.path.abspath(str(payload.get("canonical_root") or "")))
            != os.path.normcase(os.path.abspath(expected_root))
        ):
            return False
        work = plan.work_identity
        if work is None or work.status != "confirmed":
            return False
        primary_key = _provider_mapping_key(work.primary_provider.to_dict())
        if primary_key is None:
            return False
        if primary_key in {
            _provider_mapping_key(item) for item in payload.get("providers", [])
        }:
            return True
        if not allow_explicit_provider_append:
            return False
        metadata = plan.metadata
        try:
            frozen_revision = int(metadata.get("catalog_revision"))
            current_revision = int(payload.get("revision"))
        except (TypeError, ValueError):
            return False
        return all((
            metadata.get("catalog_link_decision") == "explicit_user",
            str(metadata.get("catalog_work_key") or "") == work_key,
            frozen_revision == current_revision,
        ))

    def _approved_completed_sibling_append(
        self,
        existing: Mapping[str, object],
        plan: RenamePlan,
        ledger: Mapping[str, object],
        *,
        expected_revision: Optional[int],
    ) -> bool:
        """Allow revision crossing only for one ledger-frozen sibling append."""

        intent = plan.metadata.get("work_font_intent")
        frozen = existing.get("work_intent")
        if (
            expected_revision is None
            or not isinstance(intent, Mapping)
            or not isinstance(frozen, Mapping)
            or intent.get("append_policy") != "completed-sibling-append-v1"
            or int(existing.get("revision") or 0) <= int(expected_revision)
        ):
            return False
        compared = (
            "schema_version", "policy_version", "work_key", "group_kind",
            "content_revision", "primary_content_key",
            "primary_decision_source", "snapshot_fingerprint", "append_policy",
        )
        if (
            any(frozen.get(key) != intent.get(key) for key in compared)
            or tuple(frozen.get("content_keys", ()))
            != tuple(intent.get("content_keys", ()))
        ):
            return False
        rows = ledger.get("entries")
        if not isinstance(rows, list):
            return False
        work_key = str(existing.get("work_key") or "")
        fingerprint = catalog_record_fingerprint(existing)
        conditional_fonts = _plan_conditional_font_assets(plan)
        approved_reuse_pairs = set()
        for row in rows:
            if not isinstance(row, Mapping):
                return False
            if row.get("status") != "reused_identical_asset":
                continue
            authority = row.get("conditional_authority")
            pair = (
                _path_key(Path(str(row.get("source") or ""))),
                _path_key(Path(str(row.get("target") or ""))),
            )
            approved_content = conditional_fonts.get(pair)
            if not isinstance(authority, Mapping) or any((
                approved_content is None,
                authority.get("policy") != "completed-sibling-append-v1",
                authority.get("work_key") != work_key,
                authority.get("catalog_schema_version") != existing.get("schema_version"),
                authority.get("catalog_revision") != existing.get("revision"),
                authority.get("catalog_fingerprint") != fingerprint,
                authority.get("content_key") != approved_content,
            )):
                return False
            approved_reuse_pairs.add(pair)
        if not approved_reuse_pairs:
            return False
        for entry in plan.entries:
            if not os.path.lexists(entry.target_path):
                return False
            pair = (_path_key(entry.source_path), _path_key(entry.target_path))
            if (
                entry.collision_resolution is None
                and not _samefile(entry.source_path, entry.target_path)
                and pair not in approved_reuse_pairs
            ):
                return False
        return True

    def register_completed(
        self,
        plan: RenamePlan,
        ledger: Mapping[str, object],
        *,
        expected_revision: Optional[int] = None,
        lock_held: bool = False,
    ) -> dict:
        try:
            validate_plan_projection_receipt(plan)
        except (ProjectionAuthorityError, TypeError, ValueError) as exc:
            raise WorkCatalogError(
                "整理计划权威凭据不一致，拒绝作品登记"
            ) from exc
        metadata = plan.metadata
        work_key = str(
            metadata.get("catalog_work_key")
            or metadata.get("proposed_work_key")
            or ""
        )
        if not _WORK_KEY.fullmatch(work_key):
            raise WorkCatalogError("整理计划缺少有效的 proposed_work_key")
        work = plan.work_identity
        if work is None or work.status != "confirmed":
            raise WorkCatalogError("整理计划缺少已确认作品身份")
        group_kinds = {group.kind for group in plan.presentation_groups}
        if group_kinds not in ({"movie_continuous"}, {"tv_episodes"}):
            raise WorkCatalogError("作品登记只接受单一电影或单一剧集整理计划")
        group_kind = next(iter(group_kinds))
        canonical_root_value = str(metadata.get("canonical_work_root") or "").strip()
        if not canonical_root_value:
            targets = tuple(entry.target_path for entry in plan.entries)
            if not targets:
                raise WorkCatalogError("整理计划没有目标文件")
            canonical_root_value = str(_common_work_root(targets, metadata))
        canonical_root = Path(canonical_root_value)
        lock_context = nullcontext() if lock_held else self.lock(work_key)
        with lock_context:
            path = self._path(work_key)
            existing = self.load(work_key) if path.exists() else None
            if existing and existing.get("group_kind") != group_kind:
                raise WorkCatalogError("既有作品登记的观看形式与当前整理计划不一致")
            if existing is not None and not self.compatible_base(
                existing, plan, allow_explicit_provider_append=True
            ):
                raise WorkCatalogError("既有作品登记与当前整理计划的作品身份不一致")
            current_revision = int(existing.get("revision", 0)) if existing else 0
            try:
                completed_fonts = validate_completed_movie_font_assets(plan, ledger)
            except FontArchiveContractError as exc:
                raise WorkCatalogError(str(exc)) from exc
            if expected_revision is not None and current_revision != expected_revision:
                if (
                    existing is not None
                    and current_revision == expected_revision + 1
                    and self.materializes_completed_plan(existing, plan, ledger)
                ):
                    return existing
                if not (
                    existing is not None
                    and self._approved_completed_sibling_append(
                        existing,
                        plan,
                        ledger,
                        expected_revision=expected_revision,
                    )
                ):
                    raise WorkCatalogError("作品登记已被其他任务更新，请重新生成整理计划")
            fonts_by_target = {
                str(item.entry.target_path): item for item in completed_fonts
            }
            provider = work.primary_provider.to_dict()
            providers = list(existing.get("providers", [])) if existing else []
            if _provider_mapping_key(provider) not in {
                _provider_mapping_key(item) for item in providers
            }:
                collision = self.find_exact(work.primary_provider, group_kind=group_kind)
                if collision is not None and collision.work_key != work_key:
                    raise WorkCatalogError("provider 身份已登记到另一个本地作品")
                providers.append(provider)
            versions = list(existing.get("versions", [])) if existing else []
            seasons = list(existing.get("seasons", [])) if existing else []
            assets = _upgrade_assets(existing.get("assets", [])) if existing else []
            known_targets = {str(item.get("target")) for item in versions}
            assets_by_target = {str(item.get("target")): item for item in assets}
            for entry in plan.entries:
                contract = entry.presentation_asset
                target_key = str(entry.target_path)
                association = {
                    "source": str(entry.source_path),
                    "task_id": plan.task_id,
                    "owner_version_ref": (
                        contract.owner_version_ref if contract is not None else None
                    ),
                    "media_kind": entry.media_kind,
                    "role": entry.role,
                }
                collision = entry.collision_resolution
                existing_asset = assets_by_target.get(target_key)
                font = fonts_by_target.get(target_key)
                if existing_asset is None:
                    existing_asset = {
                        "target": target_key,
                        "content_key": (
                            font.content_key if font is not None else
                            f"sha256:{collision.target_size}:{collision.target_sha256}"
                            if collision is not None else None
                        ),
                        "size": font.size if font is not None else (
                            collision.target_size if collision is not None else None
                        ),
                        "sha256": font.sha256 if font is not None else (
                            collision.target_sha256 if collision is not None else None
                        ),
                        "scope": (
                            "shared"
                            if contract is not None and contract.owner_version_ref == "shared"
                            else "version"
                        ),
                        "associations": [association],
                    }
                    assets.append(existing_asset)
                    assets_by_target[target_key] = existing_asset
                else:
                    associations = existing_asset.setdefault("associations", [])
                    association_key = (
                        association["source"], association["task_id"],
                        association["owner_version_ref"],
                    )
                    if association_key not in {
                        (item.get("source"), item.get("task_id"), item.get("owner_version_ref"))
                        for item in associations if isinstance(item, Mapping)
                    }:
                        associations.append(association)
                    if collision is not None:
                        content_key = f"sha256:{collision.target_size}:{collision.target_sha256}"
                        recorded = existing_asset.get("content_key")
                        if recorded not in {None, content_key}:
                            raise WorkCatalogError("既有资产内容身份与批准的复用证据冲突")
                        existing_asset.update({
                            "content_key": content_key,
                            "size": collision.target_size,
                            "sha256": collision.target_sha256,
                        })
                    if font is not None:
                        if existing_asset.get("content_key") not in {None, font.content_key}:
                            raise WorkCatalogError("既有电影字体位置登记了不同内容")
                        duplicate = next((
                            item for item in assets
                            if item is not existing_asset
                            and item.get("content_key") == font.content_key
                        ), None)
                        if duplicate is not None:
                            raise WorkCatalogError("同一电影字体内容登记了多个位置")
                        existing_asset.update({
                            "content_key": font.content_key,
                            "size": font.size,
                            "sha256": font.sha256,
                            "scope": "shared",
                        })
                    if contract is not None and contract.owner_version_ref == "shared":
                        existing_asset["scope"] = "shared"
                if (
                    contract is None
                    or contract.asset_role.kind != "content_version"
                    or str(entry.target_path) in known_targets
                ):
                    continue
                descriptor = contract.version_descriptor
                versions.append({
                    "target": str(entry.target_path),
                    "source": str(entry.source_path),
                    "version_ref": descriptor.version_ref if descriptor else None,
                    "technical_variant_key": (
                        descriptor.technical_variant_key if descriptor else None
                    ),
                    "display_label": descriptor.display_label if descriptor else None,
                    "task_id": plan.task_id,
                    "ledger_fingerprint": str(ledger.get("plan_fingerprint") or ""),
                })
            if group_kind == "tv_episodes":
                season_number = metadata.get("season_number", metadata.get("season_id"))
                if not isinstance(season_number, int) or season_number < 0:
                    raise WorkCatalogError("剧集整理计划缺少已确认季度")
                season_provider = work.primary_provider.to_dict()
                season_key = (_provider_mapping_key(season_provider), season_number)
                known_seasons = {
                    (_provider_mapping_key(item.get("provider")), item.get("season_number"))
                    for item in seasons if isinstance(item, Mapping)
                }
                if season_key not in known_seasons:
                    seasons.append({
                        "season_number": season_number,
                        "provider": season_provider,
                        "task_id": plan.task_id,
                    })
            record_title = str(existing.get("title")) if existing else work.title
            record_year = existing.get("year") if existing else work.year
            record_domain = str(existing.get("domain")) if existing else work.domain
            record_aliases = list(existing.get("aliases", [])) if existing else []
            for alias in work.primary_provider.aliases:
                if alias not in record_aliases:
                    record_aliases.append(alias)
            payload = {
                "schema_version": WORK_CATALOG_SCHEMA_VERSION,
                "work_key": work_key,
                "revision": current_revision + 1,
                "title": record_title,
                "year": record_year,
                "domain": record_domain,
                "group_kind": group_kind,
                "canonical_root": str(canonical_root),
                "aliases": record_aliases,
                "providers": providers,
                "versions": versions,
                "assets": assets,
                "seasons": seasons,
                "work_intent": _catalog_work_intent(
                    metadata.get("work_font_intent"),
                    existing.get("work_intent") if existing else None,
                ),
                "updated_at": _utc_now(),
            }
            self._validate(payload, work_key)
            if completed_fonts and not self.materializes_completed_plan(payload, plan, ledger):
                raise WorkCatalogError("电影字体登记候选未能精确物化已完成计划")
            _atomic_json_write(path, payload)
            return payload

    def relocate(
        self,
        work_key: str,
        new_root: Path,
        *,
        allowed_root: Path,
        task_root: Path,
    ) -> dict:
        """Verify a user-moved whole movie directory, then update facts only."""

        new_root = Path(new_root)
        allowed_root = Path(allowed_root)
        if not _is_within(new_root, allowed_root):
            raise WorkCatalogError("新目录不在当前配置的电影目标根内")
        if not new_root.is_dir() or _is_reparse_or_symlink(new_root):
            raise WorkCatalogError("新目录不存在，或是符号链接/重解析点")
        with self.lock(work_key):
            record = self.load(work_key)
            old_root = Path(str(record["canonical_root"]))
            if old_root == new_root:
                raise WorkCatalogError("新旧目录相同，无需重新定位")
            if os.path.lexists(old_root):
                raise WorkCatalogError("旧登记目录仍存在；请先确认没有重复目录")
            active = self._active_tasks_for_work(work_key, task_root)
            if active:
                raise WorkCatalogError(
                    f"该作品仍有 {len(active)} 个活动任务，禁止重新定位"
                )
            if not record.get("assets"):
                raise WorkCatalogError(
                    "旧作品登记没有完整资产清单，不能自动重新定位"
                )
            relocated_assets = []
            for item in record.get("assets", []):
                old_target = Path(str(item.get("target") or ""))
                try:
                    relative = old_target.relative_to(old_root)
                except ValueError as exc:
                    raise WorkCatalogError("登记资产越出原作品目录") from exc
                new_target = new_root / relative
                sources = _asset_sources(item)
                if not sources or not new_target.is_file():
                    raise WorkCatalogError(f"重新定位后资产缺失：{relative}")
                target_stat = new_target.stat()
                linked_source = next((
                    source for source in sources
                    if source.is_file()
                    and source.stat().st_dev == target_stat.st_dev
                    and source.stat().st_ino == target_stat.st_ino
                ), None)
                if linked_source is None or target_stat.st_nlink < 2:
                    raise WorkCatalogError(f"资产不再满足原 hardlink 证明：{relative}")
                relocated_assets.append({**item, "target": str(new_target)})
            relocated_versions = []
            for item in record.get("versions", []):
                old_target = Path(str(item.get("target") or ""))
                try:
                    relative = old_target.relative_to(old_root)
                except ValueError as exc:
                    raise WorkCatalogError("登记正片越出原作品目录") from exc
                relocated_versions.append({
                    **item, "target": str(new_root / relative)
                })
            updated = {
                **record,
                "revision": int(record["revision"]) + 1,
                "canonical_root": str(new_root),
                "assets": relocated_assets,
                "versions": relocated_versions,
                "relocated_from": str(old_root),
                "updated_at": _utc_now(),
            }
            _atomic_json_write(self._path(work_key), updated)
            return updated

    def archive_missing(
        self,
        work_key: str,
        *,
        task_root: Path,
    ) -> Path:
        """Remove a stale registration without touching any media files.

        The original JSON is atomically moved below ``.deleted`` so this UI
        action is reversible by an administrator. It is deliberately refused
        while the registered directory still exists or an active task refers
        to the work.
        """

        with self.lock(work_key):
            path = self._path(work_key)
            record = self.load(work_key)
            canonical_root = Path(str(record["canonical_root"]))
            if os.path.lexists(canonical_root):
                raise WorkCatalogError(
                    "原登记目录仍存在，不能删除登记；如已移动请选择“原目录已移动”"
                )
            active = self._active_tasks_for_work(work_key, task_root)
            if active:
                raise WorkCatalogError(
                    f"该作品仍有 {len(active)} 个活动任务，禁止删除登记"
                )
            archive_root = self.root / ".deleted"
            archive_root.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archive_path = archive_root / (
                f"{work_key}.{timestamp}.{uuid.uuid4().hex}.json"
            )
            try:
                os.replace(path, archive_path)
            except OSError as exc:
                raise WorkCatalogError(f"无法归档并删除作品登记：{exc}") from exc
            return archive_path

    @staticmethod
    def _active_tasks_for_work(work_key: str, task_root: Path) -> list[str]:
        active = []
        for task_path in Path(task_root).glob("*.json"):
            try:
                task = json.loads(task_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if task.get("workflow_status") in {
                "queued", "running", "executing", "approved",
                "need_confirm", "catalog_update_pending",
            } and work_key in json.dumps(task, ensure_ascii=False):
                active.append(str(task.get("uuid") or task_path.stem))
        return active

    @contextmanager
    def lock(self, work_key: str, *, wait_seconds: float = 0.0):
        self._path(work_key)
        lock_path = self.lock_root / f"{work_key}.lock"
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        while True:
            try:
                with advisory_file_lock(
                    lock_path,
                    owner={"kind": "work_catalog", "work_key": work_key},
                ):
                    yield
                    return
            except FileLockBusyError as exc:
                if time.monotonic() >= deadline:
                    raise WorkCatalogError(
                        "该电影作品已有任务正在执行，请稍后重试"
                    ) from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _path(self, work_key: str) -> Path:
        if not _WORK_KEY.fullmatch(str(work_key)):
            raise WorkCatalogError(f"不安全的 work_key：{work_key!r}")
        return self.root / f"{work_key}.json"

    @staticmethod
    def _validate(payload: object, work_key: str) -> None:
        if not isinstance(payload, dict):
            raise WorkCatalogError("作品登记根节点不是对象")
        if payload.get("schema_version") not in _SUPPORTED_WORK_CATALOG_SCHEMAS:
            raise WorkCatalogError("作品登记 schema_version 不受支持")
        if payload.get("work_key") != work_key or not _WORK_KEY.fullmatch(work_key):
            raise WorkCatalogError("作品登记 work_key 不匹配")
        if not isinstance(payload.get("providers"), list) or not isinstance(payload.get("versions"), list):
            raise WorkCatalogError("作品登记 provider/version 列表损坏")
        if "assets" in payload and not isinstance(payload.get("assets"), list):
            raise WorkCatalogError("作品登记 assets 列表损坏")
        if not str(payload.get("canonical_root") or "").strip():
            raise WorkCatalogError("作品登记缺少 canonical_root")
        work_intent = payload.get("work_intent")
        content_keys = (
            work_intent.get("content_keys")
            if isinstance(work_intent, Mapping) else None
        )
        if work_intent is not None and (
            not isinstance(work_intent, Mapping)
            or work_intent.get("schema_version") != 1
            or work_intent.get("policy_version") != "work-intent-v1"
            or work_intent.get("work_key") != work_key
            or work_intent.get("group_kind") != payload.get("group_kind")
            or work_intent.get("append_policy") != "completed-sibling-append-v1"
            or not isinstance(work_intent.get("content_revision"), int)
            or isinstance(work_intent.get("content_revision"), bool)
            or int(work_intent.get("content_revision") or 0) < 1
            or not isinstance(content_keys, list)
            or not content_keys
            or any(not isinstance(value, str) for value in content_keys)
            or content_keys != sorted(set(content_keys))
            or any(
                re.fullmatch(r"sha256:\d+:[0-9a-f]{64}", str(value)) is None
                for value in content_keys
            )
            or (
                work_intent.get("primary_content_key") is not None
                and work_intent.get("primary_content_key") not in content_keys
            )
            or (
                (work_intent.get("primary_content_key") is None)
                != (work_intent.get("primary_decision_source") is None)
            )
            or re.fullmatch(
                r"[0-9a-f]{64}",
                str(work_intent.get("snapshot_fingerprint") or ""),
            ) is None
        ):
            raise WorkCatalogError("作品登记 work_intent 权威损坏")


def _plan_conditional_font_assets(
    plan: RenamePlan,
) -> dict[tuple[str, str], str]:
    intent = plan.metadata.get("work_font_intent")
    assets = intent.get("assets") if isinstance(intent, Mapping) else None
    if not isinstance(assets, (list, tuple)):
        return {}
    result = {}
    for item in assets:
        if (
            not isinstance(item, Mapping)
            or item.get("action") != "create_or_reuse_registered_identical"
        ):
            return {}
        source = Path(str(item.get("source_path") or ""))
        target = Path(str(item.get("target_path") or ""))
        content_key = str(item.get("content_key") or "")
        if not str(source) or not str(target) or not re.fullmatch(
            r"sha256:\d+:[0-9a-f]{64}", content_key
        ):
            return {}
        result[(_path_key(source), _path_key(target))] = content_key
    return result


def _catalog_work_intent(
    raw: object,
    existing: object,
) -> Optional[dict[str, object]]:
    """Project only identity/content authority; never persist source locations."""

    if raw is None:
        return dict(existing) if isinstance(existing, Mapping) else None
    if not isinstance(raw, Mapping):
        raise WorkCatalogError("电影字体 Plan 缺少有效的 work_intent")
    projection = {
        "schema_version": raw.get("schema_version"),
        "policy_version": raw.get("policy_version"),
        "work_key": raw.get("work_key"),
        "group_kind": raw.get("group_kind"),
        "content_revision": raw.get("content_revision"),
        "content_keys": [str(value) for value in raw.get("content_keys", ())],
        "primary_content_key": raw.get("primary_content_key"),
        "primary_decision_source": raw.get("primary_decision_source"),
        "snapshot_fingerprint": raw.get("snapshot_fingerprint"),
        "append_policy": raw.get("append_policy"),
    }
    if isinstance(existing, Mapping):
        immutable = (
            "schema_version", "policy_version", "work_key", "group_kind",
            "content_revision", "content_keys", "primary_content_key",
            "primary_decision_source", "snapshot_fingerprint", "append_policy",
        )
        if any(existing.get(key) != projection.get(key) for key in immutable):
            raise WorkCatalogError("电影字体 work_intent 与既有作品登记冲突")
    return projection


def _upgrade_assets(raw_assets: object) -> list[dict]:
    """Return a writable v2 view without mutating a stored v1 record."""

    if not isinstance(raw_assets, list):
        raise WorkCatalogError("作品登记 assets 列表损坏")
    upgraded = []
    for raw in raw_assets:
        if not isinstance(raw, Mapping):
            raise WorkCatalogError("作品登记 asset 条目损坏")
        associations = raw.get("associations")
        if isinstance(associations, list):
            upgraded.append({
                **raw,
                "associations": [dict(item) for item in associations if isinstance(item, Mapping)],
            })
            continue
        source = str(raw.get("source") or "")
        upgraded.append({
            "target": str(raw.get("target") or ""),
            "content_key": None,
            "size": None,
            "sha256": None,
            "scope": "version",
            "associations": [{
                "source": source,
                "task_id": str(raw.get("task_id") or ""),
                "owner_version_ref": None,
                "media_kind": str(raw.get("media_kind") or ""),
                "role": str(raw.get("role") or ""),
            }],
        })
    return upgraded


def _ledger_identity_matches_current(
    row: Mapping[str, object], *, source_stat: os.stat_result, target_stat: os.stat_result
) -> bool:
    """Accept complete matching identity evidence or a fully legacy row."""

    fields = (
        "source_device", "source_inode", "target_device", "target_inode",
        "target_link_count",
    )
    present = tuple(row.get(field) is not None for field in fields)
    if not any(present):
        return True
    if not all(present):
        return False
    try:
        return all((
            int(row["source_device"]) == source_stat.st_dev,
            int(row["source_inode"]) == source_stat.st_ino,
            int(row["target_device"]) == target_stat.st_dev,
            int(row["target_inode"]) == target_stat.st_ino,
            int(row["target_link_count"]) >= 2,
        ))
    except (KeyError, TypeError, ValueError):
        return False


def _asset_sources(item: Mapping[str, object]) -> tuple[Path, ...]:
    associations = item.get("associations")
    if isinstance(associations, list):
        return tuple(
            Path(str(association.get("source")))
            for association in associations
            if isinstance(association, Mapping) and association.get("source")
        )
    source = item.get("source")
    return (Path(str(source)),) if source else ()


def _provider_key(provider: ProviderIdentity) -> Optional[tuple[str, str, str]]:
    if not all((provider.namespace, provider.entity_type, provider.provider_id)):
        return None
    return (
        str(provider.namespace).casefold(),
        str(provider.entity_type).casefold(),
        str(provider.provider_id),
    )


def _provider_mapping_key(provider: object) -> Optional[tuple[str, str, str]]:
    if not isinstance(provider, Mapping):
        return None
    values = tuple(str(provider.get(key) or "").strip() for key in (
        "namespace", "entity_type", "provider_id"
    ))
    if not all(values):
        return None
    return (values[0].casefold(), values[1].casefold(), values[2])


def _samefile(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _as_match(payload: Mapping[str, object]) -> CatalogMatch:
    return CatalogMatch(
        work_key=str(payload["work_key"]),
        revision=int(payload["revision"]),
        title=str(payload["title"]),
        year=payload.get("year") if isinstance(payload.get("year"), int) else None,
        domain=str(payload["domain"]),
        canonical_root=Path(str(payload["canonical_root"])),
        versions=tuple(payload.get("versions", ())),
        group_kind=str(payload.get("group_kind") or "movie_continuous"),
        seasons=tuple(payload.get("seasons", ())),
    )


def _canonical_title(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def _titles_related(left: str, right: str) -> bool:
    """Offer a manually-confirmed TV-series candidate without auto-merging."""

    if len(left) < 4 or len(right) < 4:
        return False
    return left in right or right in left


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & 0x400)


def _common_work_root(targets: tuple[Path, ...], metadata: Mapping[str, object]) -> Path:
    target_root = Path(str(metadata.get("target_root") or ""))
    first = targets[0]
    try:
        relative = first.relative_to(target_root)
    except ValueError as exc:
        raise WorkCatalogError("目标文件越出已批准目标根") from exc
    if not relative.parts:
        raise WorkCatalogError("无法从目标路径确定电影目录")
    return target_root / relative.parts[0]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "x", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
