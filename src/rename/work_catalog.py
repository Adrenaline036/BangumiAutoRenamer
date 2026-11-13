"""Execution-backed local catalog for incrementally assembled media works."""

from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from contextlib import contextmanager
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

from ..utils.path import WORK_CATALOG_PATH
from .rename_plan import ProviderIdentity, RenamePlan
from .plan_store import PlanStore
from .projection_authority import (
    ProjectionAuthorityError,
    validate_plan_projection_receipt,
)
from .asset_collision import AssetCollisionError



WORK_CATALOG_SCHEMA_VERSION = 4
_WORK_KEY = re.compile(r"^work-[0-9a-fA-F-]{32,36}$")


class WorkCatalogError(RuntimeError):
    pass


def require_matching_work_domain(registered_domain, candidate_domain):
    """Same provider/root is not permission to cross registered libraries."""
    labels = {'animation': '动画', 'general': '普通影视'}
    if (registered_domain not in labels or candidate_domain not in labels
            or registered_domain != candidate_domain):
        registered = labels.get(registered_domain, '未确认')
        candidate = labels.get(candidate_domain, '未确认')
        raise WorkCatalogError(
            f'作品已登记到{registered}媒体库，当前选择为{candidate}媒体库。'
            '请核对已登记作品和作品类型后重新生成计划；不能直接跨库追加、换源或迁移。')


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
    def __init__(self, root: Path = WORK_CATALOG_PATH, *, rebuild_context=None, asset_action_context=None) -> None:
        self.root = Path(root)
        self.lock_root = self.root / ".locks"
        self.rebuild_context = rebuild_context
        self.asset_action_context = asset_action_context

    def _rebuild_context_for(self, plan):
        """A marker selects a strict validator, never authorizes a bypass."""
        if plan.metadata.get("completed_rebuild_observation") is None:
            return None
        from .completed_rebuild_work import WorkRebuildContext
        if not isinstance(self.rebuild_context, WorkRebuildContext):
            raise WorkCatalogError("电影重建缺少明确配置的历史证据上下文")
        if not isinstance(self.rebuild_context.proof_for_plan(plan), Mapping):
            raise WorkCatalogError("电影重建恢复证明未通过当前权威验证")
        return self.rebuild_context

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




    def materializes_completed_plan(
        self,
        payload: Mapping[str, object],
        plan: RenamePlan,
        ledger: Mapping[str, object],
    ) -> bool:
        """Prove that one persisted record exactly contains this completed Plan."""

        if plan.metadata.get("completed_rebuild_observation") is not None:
            from .completed_rebuild_work import validate_work_rebuild_completed
            try:
                current = validate_work_rebuild_completed(
                    plan, ledger, self, self._rebuild_context_for(plan)
                )
                return catalog_record_fingerprint(payload) == catalog_record_fingerprint(current)
            except (OSError, TypeError, ValueError, RuntimeError):
                return False

        work_key = str(
            plan.metadata.get("catalog_work_key")
            or plan.metadata.get("proposed_work_key")
            or ""
        )
        try:
            self._validate(payload, work_key)
        except WorkCatalogError:
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
        assets = copy_assets(payload.get("assets", []))
        from .media_file_identity import frozen_sources, verify_recorded_target
        try:
            sources = frozen_sources(plan)
        except ValueError:
            return False
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
            replacement = row.get("action") == "replace"
            if replacement:
                from .asset_operation import AssetOperation
                from .asset_replacement import ReplacementEvidence, observe_file
                try:
                    operation = AssetOperation.from_mapping(plan.metadata.get("asset_operation")).to_dict()
                    matches = [ReplacementEvidence.from_dict(item) for item in operation["replaces"]
                        if item["target"] == str(entry.target_path) and item["source"] == str(entry.source_path)]
                    if (operation["mode"] != "replace" or len(matches) != 1
                            or row.get("replacement_evidence") != matches[0].to_dict()
                            or observe_file(entry.source_path) != matches[0].new_source
                            or observe_file(entry.target_path) != matches[0].new_source):
                        return False
                except (OSError, TypeError, ValueError):
                    return False
            if entry.collision_resolution is None:
                if any((
                    row.get("status") != "created",
                    row.get("action") != ("replace" if replacement else "create"),
                    row.get("transfer_type") != "hardlink",
                )):
                    return False
            elif any((row.get('status') != 'reused_identical_asset',
                      row.get('action') != 'reuse_existing', row.get('transfer_type') != 'reuse_existing')):
                return False
            try:
                verify_recorded_target(plan, entry, row, sources=sources)
            except (OSError, ValueError):
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
        return True

    def compatible_base(
        self,
        payload: Mapping[str, object],
        plan: RenamePlan,
        *,
        allow_explicit_provider_append: bool = False,
    ) -> bool:
        """Check immutable work authority before appending a new revision."""

        work = plan.work_identity
        if work is None or work.status != 'confirmed':
            return False
        work_key = str(
            plan.metadata.get("catalog_work_key")
            or plan.metadata.get("proposed_work_key")
            or ""
        )
        try:
            self._validate(payload, work_key)
            require_matching_work_domain(payload.get('domain'), work.domain)
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
        primary_key = _provider_mapping_key(work.primary_provider.to_dict())
        if primary_key is None:
            return False
        from .work_affiliation import from_plan, digest
        try:
            affiliation = from_plan(plan)
        except (ValueError, RuntimeError, TypeError):
            return False
        if primary_key in {
            _provider_mapping_key(item) for item in payload.get("providers", [])
        }:
            return True
        if affiliation is None:
            return False
        bound = affiliation.to_dict()
        return (bound['action'] == 'existing' and bound['key'] == work_key
                and bound['catalog_revision'] == payload.get('revision')
                and bound['record_fingerprint'] == digest(payload))


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
            self.require_namespace_plan(plan, work_key)
            path = self._path(work_key)
            existing = self.load(work_key) if path.exists() else None
            if plan.metadata.get("completed_rebuild_observation") is not None:
                from .completed_rebuild_work import validate_work_rebuild_completed
                try:
                    restored = validate_work_rebuild_completed(
                        plan, ledger, self, self._rebuild_context_for(plan)
                    )
                    if expected_revision is None or plan.metadata.get("catalog_revision") != expected_revision:
                        raise ValueError("电影重建登记版本与批准版本不一致")
                    return restored
                except (OSError, TypeError, ValueError, RuntimeError) as exc:
                    raise WorkCatalogError(f"电影重建完成证明不一致：{exc}") from exc
            if existing and existing.get("group_kind") != group_kind:
                raise WorkCatalogError("既有作品登记的观看形式与当前整理计划不一致")
            if existing is not None and not self.compatible_base(
                existing, plan, allow_explicit_provider_append=True
            ):
                raise WorkCatalogError("既有作品登记与当前整理计划的作品身份不一致")
            current_revision = int(existing.get("revision", 0)) if existing else 0
            operation = metadata.get("asset_operation")
            replacing = isinstance(operation, Mapping) and operation.get("mode") == "replace"
            if replacing:
                from .asset_actions import AssetActionService
                if not isinstance(self.asset_action_context, AssetActionService):
                    raise WorkCatalogError("作品换源缺少类型化资产验证服务")
            if existing is not None and self.materializes_completed_plan(existing, plan, ledger):
                return existing
            if replacing:
                try:
                    self.asset_action_context.validate(plan, self.asset_action_context.decisions.load(plan.task_id), completed=True)
                except (OSError, TypeError, ValueError, RuntimeError) as exc:
                    raise WorkCatalogError(f"作品换源完成证据不一致：{exc}") from exc
            from .movie_asset_scope import validate_append
            try:
                scoped_append = validate_append(plan, self, payload=existing) if not replacing else False
            except (OSError, TypeError, ValueError, RuntimeError) as exc:
                raise WorkCatalogError(f"电影追加依赖不一致：{exc}") from exc
            if expected_revision is not None and current_revision != expected_revision:
                if (
                    existing is not None
                    and current_revision == expected_revision + 1
                    and self.materializes_completed_plan(existing, plan, ledger)
                ):
                    return existing
                if not (replacing or scoped_append):
                    raise WorkCatalogError("作品登记已被其他任务更新，请重新生成整理计划")
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
            assets = copy_assets(existing.get("assets", [])) if existing else []
            asset_history = list(existing.get("asset_history", ())) if existing else []
            if replacing:
                previous = operation["previous_assets"]
                new_targets = {str(entry.target_path) for entry in plan.entries}
                changed = ({row['target_path'] for row in previous}
                    if operation['policy'] == 'asset-operation-v2' else
                    {row["target_path"] for row in previous if row["target_path"] in new_targets})
                selected_refs = {e.presentation_asset.version_descriptor.version_ref for e in plan.entries
                    if e.presentation_asset and e.presentation_asset.version_descriptor
                    and e.presentation_asset.asset_role.kind == "content_version"}
                old_versions = [row for row in versions if row.get("version_ref") in selected_refs
                    and (operation['policy'] == 'asset-operation-v2' or row.get("target") in changed)]
                old_assets = [row for row in assets if row.get("target") in changed and row.get('scope') != 'shared']
                asset_history.append({"task_id": plan.task_id, "plan_fingerprint": PlanStore.fingerprint(plan),
                    "versions": old_versions, "assets": old_assets})
                versions = [row for row in versions if row not in old_versions]
                assets = [row for row in assets if row not in old_assets]
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
                if existing_asset is None:
                    existing_asset = {
                        "target": target_key,
                        "content_key": (
                            collision.content_key if collision is not None else None
                        ),
                        "size": (
                            collision.target_size if collision is not None else None
                        ),
                        "xxh3_128": collision.target_xxh3_128 if collision is not None else None,
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
                    if collision is not None and collision.evidence_kind == 'hardlink-identity-v1':
                        existing_asset.update({'content_key': None, 'size': collision.target_size, 'xxh3_128': None})
                    elif collision is not None:
                        content_key = collision.content_key
                        recorded = existing_asset.get("content_key")
                        if recorded not in {None, content_key}:
                            raise WorkCatalogError("既有资产内容身份与批准的复用证据冲突")
                        existing_asset.update({
                            "content_key": content_key,
                            "size": collision.target_size,
                            "xxh3_128": collision.target_xxh3_128,
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
                "updated_at": _utc_now(),
            }
            if asset_history:
                payload["asset_history"] = asset_history
            self._validate(payload, work_key)
            if (scoped_append or replacing) and not self.materializes_completed_plan(payload, plan, ledger):
                raise WorkCatalogError("电影登记候选未能精确对应已完成计划")
            self.write_locked(work_key, payload)
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
            self.write_locked(work_key, updated)
            return updated

    def archive_missing(
        self,
        work_key: str,
        *,
        task_root: Path,
        expected_fingerprint: Optional[str] = None,
    ) -> Path:
        """Remove a stale registration without touching any media files.

        The original JSON is atomically moved below ``.deleted`` so this UI
        action is reversible by an administrator. It is deliberately refused
        while the registered directory still exists or an active task refers
        to the work.
        """

        with self.lock(work_key):
            path = self._path(work_key)
            from .catalog_archive import require_archive_fingerprint
            try:
                require_archive_fingerprint(path, expected_fingerprint)
            except ValueError as exc:
                raise WorkCatalogError(str(exc)) from exc
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
            self._require_namespace_candidate(work_key, None)
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
    def lock(self, work_key: str, *, wait_seconds: float = 0.0, read_only=False):
        from .catalog_namespace import locked
        with locked(self, work_key, wait_seconds=wait_seconds, read_only=read_only) as lease:
            yield lease

    def namespace_snapshot(self, work_key):
        from .catalog_namespace import observe
        return observe(self, work_key)

    def verify_namespace_snapshot(self, work_key, snapshot):
        from .catalog_namespace import verify
        verify(self, work_key, snapshot)

    def validate_namespace_candidate(self, work_key, candidate):
        from .catalog_namespace import validate
        return validate(self, work_key, candidate)

    def _require_namespace_candidate(self, work_key, candidate):
        from .catalog_namespace import consume
        proof = self.validate_namespace_candidate(work_key, candidate)
        consume(self, work_key, candidate, proof)

    def write_locked(self, work_key, payload):
        """All work-record writes, including rollback, use this held boundary."""
        from .catalog_namespace import require_write
        require_write(self, work_key)
        self._validate(payload, work_key)
        self._require_namespace_candidate(work_key, payload)
        _atomic_json_write(self._path(work_key), payload)

    def require_namespace_plan(self, plan, work_key):
        """Pre-media namespace check; does not replace approved Plan validation."""
        from .catalog_namespace import require_lease
        require_lease(self, work_key)
        expected_key = plan.metadata.get('catalog_work_key') or plan.metadata.get('proposed_work_key')
        if expected_key != work_key or plan.work_identity is None or plan.work_identity.status != 'confirmed':
            raise WorkCatalogError('作品登记命名空间与计划身份不一致')
        record = self.load(work_key) if self._path(work_key).exists() else {}
        providers = list(record.get('providers', ()))
        for provider in (plan.work_identity.primary_provider, *plan.linked_provider_identities):
            if provider.status == 'confirmed' and _provider_key(provider) not in {
                    _provider_mapping_key(row) for row in providers}:
                providers.append(provider.to_dict())
        root = plan.metadata.get('canonical_work_root')
        if not root:
            root = str(_common_work_root(tuple(e.target_path for e in plan.entries), plan.metadata))
        candidate = {**record, 'schema_version': WORK_CATALOG_SCHEMA_VERSION,
            'work_key': work_key, 'canonical_root': str(root), 'providers': providers,
            'versions': record.get('versions', [])}
        self._require_namespace_candidate(work_key, candidate)

    def _path(self, work_key: str) -> Path:
        if not _WORK_KEY.fullmatch(str(work_key)):
            raise WorkCatalogError(f"不安全的 work_key：{work_key!r}")
        return self.root / f"{work_key}.json"

    @staticmethod
    def _validate(payload: object, work_key: str) -> None:
        if not isinstance(payload, dict):
            raise WorkCatalogError("作品登记根节点不是对象")
        if payload.get("schema_version") != WORK_CATALOG_SCHEMA_VERSION:
            raise WorkCatalogError("作品登记 schema_version 不受支持")
        if payload.get("work_key") != work_key or not _WORK_KEY.fullmatch(work_key):
            raise WorkCatalogError("作品登记 work_key 不匹配")
        if not isinstance(payload.get("providers"), list) or not isinstance(payload.get("versions"), list):
            raise WorkCatalogError("作品登记 provider/version 列表损坏")
        if "assets" in payload and not isinstance(payload.get("assets"), list):
            raise WorkCatalogError("作品登记 assets 列表损坏")
        if not str(payload.get("canonical_root") or "").strip():
            raise WorkCatalogError("作品登记缺少 canonical_root")


def copy_assets(raw_assets: object) -> list[dict]:
    if not isinstance(raw_assets, list) or any(not isinstance(row, Mapping)
            or not isinstance(row.get('associations'), list)
            or any(not isinstance(item, Mapping) for item in row['associations']) for row in raw_assets):
        raise WorkCatalogError('作品登记资产或关联列表损坏')
    return [{**row, 'associations': [dict(item) for item in row['associations']]} for row in raw_assets]


def _ledger_identity_matches_current(
    row: Mapping[str, object], *, source_stat: os.stat_result, target_stat: os.stat_result,
    require_identity: bool = False, require_hardlink: bool = True,
) -> bool:
    """Match recorded identity; legacy absence is only for ordinary old callers."""

    fields = (
        "source_device", "source_inode", "target_device", "target_inode",
        "target_link_count",
    )
    present = tuple(row.get(field) is not None for field in fields)
    if not any(present):
        return not require_identity
    if not all(present):
        return False
    if require_identity and any(type(row[field]) is not int for field in fields):
        return False
    try:
        return all((
            int(row["source_device"]) == source_stat.st_dev,
            int(row["source_inode"]) == source_stat.st_ino,
            int(row["target_device"]) == target_stat.st_dev,
            int(row["target_inode"]) == target_stat.st_ino,
            int(row["target_link_count"]) >= (2 if require_hardlink else 1),
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
