"""TV series identity and season bindings, independent from movie catalog."""

from __future__ import annotations

from .series_repair import is_repair

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

from .asset_collision import AssetCollisionError
from .plan_store import PlanStore
from .projection_authority import (
    ProjectionAuthorityError,
    validate_plan_projection_receipt,
)


SERIES_CATALOG_SCHEMA_VERSION = 4
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


class SeriesCatalog:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock_root = self.root / '.locks'

    @contextmanager
    def lock(self, series_key: str, *, wait_seconds=0.0, read_only=False):
        from .catalog_namespace import locked
        with locked(self, series_key, wait_seconds=wait_seconds, read_only=read_only) as lease:
            yield lease

    def namespace_snapshot(self, series_key):
        from .catalog_namespace import observe
        return observe(self, series_key)

    def verify_namespace_snapshot(self, series_key, snapshot):
        from .catalog_namespace import verify
        verify(self, series_key, snapshot)

    def validate_namespace_candidate(self, series_key, candidate):
        from .catalog_namespace import validate
        return validate(self, series_key, candidate)

    def _require_namespace_candidate(self, series_key, candidate):
        from .catalog_namespace import consume
        proof = self.validate_namespace_candidate(series_key, candidate)
        consume(self, series_key, candidate, proof)

    def _validate_namespace_record(self, payload, key):
        self.validate_archived_payload(payload, key)
        if type(payload['revision']) is not int or type(payload['schema_version']) is not int:
            raise SeriesCatalogError('系列登记版本类型无效')
        self._namespace_providers(payload)

    @staticmethod
    def _namespace_providers(payload):
        from .series_registration import SeriesRegistrationAuthority
        def identity(row):
            if not isinstance(row, Mapping):
                raise SeriesCatalogError('系列登记资料条目身份损坏')
            values = tuple(row.get(field) for field in ('namespace', 'entity_type', 'provider_id'))
            if (any(not isinstance(v, str) or not v or v.strip() != v for v in values)
                    or values[:2] not in {('tmdb', 'tv'), ('bangumi', 'subject')}
                    or row.get('status', 'confirmed') != 'confirmed'):
                raise SeriesCatalogError('系列登记资料条目身份损坏')
            return values
        evidence = payload.get('provider_evidence')
        if not isinstance(evidence, list) or not evidence:
            raise SeriesCatalogError('系列登记缺少资料条目身份')
        providers = {identity(row) for row in evidence}
        bindings = payload.get('season_bindings')
        if not isinstance(bindings, list):
            raise SeriesCatalogError('系列季度登记损坏')
        for row in bindings:
            if (not isinstance(row, Mapping) or type(row.get('season_number')) is not int
                    or row['season_number'] < 0 or not isinstance(row.get('season_binding_id'), str)
                    or not row['season_binding_id'] or identity(row.get('provider_identity')) not in providers):
                raise SeriesCatalogError('系列季度身份与资料条目证据不一致')
            for previous in row.get('binding_history', ()):
                if (not isinstance(previous, Mapping)
                        or identity(previous.get('provider_identity')) != identity(row['provider_identity'])
                        or previous.get('season_number') != row['season_number']
                        or previous.get('task_id') != row.get('task_id')):
                    raise SeriesCatalogError('系列季度历史身份不一致')
        authority = payload.get('series_registration_authority')
        if authority is not None:
            try:
                proof = SeriesRegistrationAuthority.from_mapping(authority)
            except (ValueError, TypeError, KeyError) as exc:
                raise SeriesCatalogError('系列登记权威无效') from exc
            if proof.series_key != payload['series_key']:
                raise SeriesCatalogError('系列登记权威与作品键不一致')
            from .series_registration import registration_current_root
            if proof.relocation and payload.get('canonical_root') != registration_current_root(proof):
                raise SeriesCatalogError('系列当前位置与重新定位观察不一致')
            if proof.affiliation:
                from .work_affiliation import WorkAffiliation
                affiliation = WorkAffiliation(proof.affiliation).to_dict()
                authorized = {tuple(row) for row in affiliation['work_members']}
                if (payload.get('domain') != affiliation['domain']
                        or payload.get('canonical_root') != registration_current_root(proof)
                        or not providers <= authorized):
                    raise SeriesCatalogError('系列资料条目或媒体库不属于已确认作品归属')
                providers.update(authorized)
                return [dict(namespace=ns, entity_type=kind, provider_id=pid)
                        for ns, kind, pid in sorted(providers)]
            from .series_registration import registration_domain, MANUAL_GENERAL_POLICY
            if proof.policy_version == MANUAL_GENERAL_POLICY and payload.get('domain') != 'general':
                raise SeriesCatalogError('真人Bangumi登记缺少明确媒体域')
            domain = payload.get('domain', registration_domain(proof))
            if domain not in {'animation', 'general'} or (proof.namespace == 'bangumi' and domain != registration_domain(proof)):
                raise SeriesCatalogError('系列媒体域与登记权威不一致')
            members = dict(proof.season_membership)
            historical = json.loads(proof.materialized_mapping) if proof.materialized_mapping else {}
            historical_providers = {identity(row.get('provider_identity'))
                                    for row in historical.get('asset_bindings', ())}
            for namespace, _kind, provider_id in providers:
                if namespace == proof.namespace:
                    if provider_id not in members:
                        raise SeriesCatalogError('系列资料条目不属于登记权威成员')
                elif (namespace, _kind, provider_id) not in historical_providers:
                    raise SeriesCatalogError('跨站系列成员缺少已登记映射权威')
            for row in bindings:
                namespace, _, provider_id = identity(row['provider_identity'])
                if namespace == 'bangumi' and namespace == proof.namespace and members[provider_id] != row['season_number']:
                    raise SeriesCatalogError('系列季度与登记权威不一致')
            providers.update((proof.namespace, 'tv' if proof.namespace == 'tmdb' else 'subject', member)
                             for member in members)
        return [dict(namespace=ns, entity_type=kind, provider_id=pid)
                for ns, kind, pid in sorted(providers)]

    def require_namespace_plan(self, plan, series_key):
        """Prospective identity/root check only; no media scan or reapproval."""
        from .catalog_namespace import require_lease
        from .series_registration import validate_plan_series_registration, _plain
        require_lease(self, series_key)
        validate_plan_projection_receipt(plan)
        proof = validate_plan_series_registration(plan)
        if proof is None or proof.series_key != series_key or plan.work_identity is None or plan.work_identity.status != 'confirmed':
            raise SeriesCatalogError('计划缺少一致的系列登记身份')
        path = self._path(series_key)
        record = json.loads(path.read_text('utf-8')) if path.exists() else {}
        if record and record.get('domain') != plan.work_identity.domain:
            raise SeriesCatalogError('已登记媒体库与本次明确选择不同')
        providers = list(record.get('provider_evidence', ()))
        provider = plan.work_identity.primary_provider.to_dict()
        if self._provider_key(provider) not in {self._provider_key(row) for row in providers}:
            providers.append(provider)
        candidate = {**record, 'schema_version': record.get('schema_version', SERIES_CATALOG_SCHEMA_VERSION),
            'series_key': series_key, 'revision': record.get('revision', 0),
            'canonical_root': str(plan.metadata.get('canonical_work_root') or ''),
            'canonical_display_name': plan.work_identity.title,
            'domain': record.get('domain', plan.work_identity.domain),
            'provider_evidence': providers, 'season_bindings': record.get('season_bindings', []),
            'associated_assets': record.get('associated_assets', []),
            'series_registration_authority': _plain(proof.to_dict())}
        self._require_namespace_candidate(series_key, candidate)

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


    def find_by_provider(self, provider: Mapping[str, object]) -> Optional[SeriesMatch]:
        needle = self._provider_key(provider)
        if needle is None or not self.root.exists():
            return None
        matches = []
        for path in self.root.glob("series-*.json"):
            try:
                payload = json.loads(path.read_text("utf-8"))
                self.validate_archived_payload(payload, path.stem)
                evidence = payload.get("provider_evidence", [])
                if any(self._provider_key(item) == needle for item in evidence if isinstance(item, Mapping)):
                    matches.append(self._match(payload))
            except (OSError, json.JSONDecodeError, SeriesCatalogError) as exc:
                raise SeriesCatalogError('系列登记记录损坏，请先修复登记，不能按无匹配处理') from exc
        if len(matches) > 1:
            raise SeriesCatalogError("同一 Provider 系列证据指向多个本地 series_key")
        return matches[0] if matches else None


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
        from .catalog_namespace import writer_scope
        with writer_scope(self, series_key):
            return self._bind_season_locked(series_key, expected_revision=expected_revision,
                season_number=season_number, provider_identity=provider_identity,
                decision_source=decision_source, binding_id=binding_id)

    def _bind_season_locked(self, series_key, *, expected_revision, season_number,
                            provider_identity, decision_source, binding_id=None):
        path = self._path(series_key)
        payload = json.loads(path.read_text("utf-8"))
        self._require_namespace_candidate(series_key, payload)
        if int(payload.get("revision", 0)) != expected_revision:
            raise SeriesCatalogError("SeriesCatalog 版本已变化")
        bindings = list(payload.get("season_bindings", []))
        provider_key = self._provider_key(provider_identity)
        for item in bindings:
            if item.get("season_number") == season_number and self._provider_key(item.get("provider_identity", {})) == provider_key:
                return self._match(payload)
        from .season_identity import confirmed_season_binding
        bindings.append({
            "season_binding_id": binding_id or confirmed_season_binding(
                provider_identity, int(season_number), decision_source)['season_binding_id'],
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
        from .catalog_namespace import writer_scope
        key = str(plan.metadata.get('series_key') or '')
        with writer_scope(self, key):
            return self._register_completed_locked(plan, expected_revision=expected_revision,
                completed_ledger=completed_ledger)

    def _register_completed_locked(self, plan, *, expected_revision=0, completed_ledger=None):
        """Persist a TV series fact only after execution has completed."""
        try:
            validate_plan_projection_receipt(plan)
            from . import series_registration
            series_registration.validate_plan_series_registration(plan)
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
        self.require_namespace_plan(plan, series_key)
        path = self._path(series_key)
        provider = plan.work_identity.primary_provider.to_dict()
        plan_fingerprint = PlanStore.fingerprint(plan)
        if path.exists():
            payload = json.loads(path.read_text("utf-8"))
            self._match(payload)
            operation = plan.metadata.get('asset_operation') or {}
            if operation.get('mode') == 'replace' or is_repair(operation):
                from .asset_actions import AssetActionService
                from .execution_recovery import _validate_completed_hardlink_ledger
                from .recovery_service import _series_catalog_materializes_plan
                context = getattr(self, 'asset_action_context', None)
                if not isinstance(context, AssetActionService):
                    raise SeriesCatalogError('换源登记缺少统一资产守卫')
                _validate_completed_hardlink_ledger(plan=plan, ledger=completed_ledger, fingerprint=plan_fingerprint)
                if _series_catalog_materializes_plan(self._match(payload), plan, completed_ledger=completed_ledger):
                    return self._match(payload)
                context.validate(plan, context.decisions.load(plan.task_id), completed=True)
            series_registration.validate_series_catalog_snapshot(
                series_registration.validate_plan_series_registration(plan), self,
                expected_revision, root_value, season_number=season_number, task_id=plan.task_id,plan=plan,completed=True)
        else:
            if expected_revision != 0:
                raise SeriesCatalogError("SeriesCatalog 不存在但期望非零 revision")
            if (plan.metadata.get('asset_operation') or {}).get('policy')=='asset-operation-v3':
                from .asset_actions import AssetActionService
                from .execution_recovery import _validate_completed_hardlink_ledger
                context=getattr(self,'asset_action_context',None)
                if not isinstance(context,AssetActionService):
                    raise SeriesCatalogError('整季登记缺少统一资产守卫')
                _validate_completed_hardlink_ledger(plan=plan,ledger=completed_ledger,fingerprint=plan_fingerprint)
                context.validate(plan,context.decisions.load(plan.task_id),completed=True)
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
            }
        if Path(str(payload["canonical_root"])) != Path(root_value):
            raise SeriesCatalogError("SeriesCatalog 规范根与已执行 Plan 不一致")
        from .season_identity import normalize_bindings, binding_signature, binding_contains
        original_bindings = list(payload.get("season_bindings", []))
        if (plan.metadata.get('asset_operation') or {}).get('mode') == 'replace' or is_repair(plan.metadata.get('asset_operation')):
            operation=plan.metadata['asset_operation']
            if operation['policy']=='asset-operation-v3':
                from .asset_operation import plain
                superseded=plain(operation['catalog_before']['rows']['bindings'])
                if [r for r in original_bindings if r.get('season_number')==season_number] != superseded:
                    raise SeriesCatalogError('整季换源的原季度登记已变化')
                retained=[]
            else:
                previous_tasks = {row['task_id'] for row in operation['previous_assets']}
                superseded = [row for row in original_bindings
                    if row.get('season_number') == season_number and row.get('task_id') in previous_tasks]
                if not superseded:
                    raise SeriesCatalogError('换源的原季度登记已变化')
                retained=[row['target_path'] for row in operation['previous_assets'] if row['disposition']=='retain']
            history = list(payload.get('asset_history', []))
            history.append(dict(task_id=plan.task_id, plan_fingerprint=plan_fingerprint,
                previous_bindings=superseded, retained_targets=retained))
            if operation['policy']=='asset-operation-v3':
                history[-1]['season_directory']=plain(operation['season_directory'])
            payload['asset_history'] = history
            original_bindings = [row for row in original_bindings if row not in superseded]
        bindings = normalize_bindings(original_bindings)
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
                if binding_signature(item) == binding_signature(expected_binding)
            ),
            None,
        )
        if existing_binding is None:
            bindings.append(expected_binding)
            binding_changed = True
        elif binding_signature(existing_binding) != binding_signature(expected_binding):
            raise SeriesCatalogError("同一季度登记重放内容不一致")
        else:
            if not binding_contains(existing_binding, binding_id):
                existing_binding.setdefault('binding_history', []).append(expected_binding)
            binding_changed = bindings != original_bindings
        payload["season_bindings"] = normalize_bindings(bindings)
        provider_evidence = list(payload.get('provider_evidence') or ())
        if not any(self._provider_key(item) == self._provider_key(provider) for item in provider_evidence):
            provider_evidence.append(provider)
        payload['provider_evidence'] = provider_evidence
        payload['series_registration_authority'] = series_registration._plain(plan.metadata['series_registration_authority'])
        from .generated_outputs import register_outputs
        generated_changed = register_outputs(payload, plan, completed_ledger)
        changed = binding_changed or generated_changed
        if not changed:
            return self._match(payload)
        payload["schema_version"] = SERIES_CATALOG_SCHEMA_VERSION
        payload["revision"] = int(payload.get("revision", 0)) + 1
        payload["materialization_state"] = "materialized"
        candidate = self._match(payload)
        self._atomic_write(series_key, payload)
        return candidate



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
                            from .asset_collision import requires_manual_collision, validate_reuse_resolution
                            from .rename_plan import CollisionResolution
                            if requires_manual_collision(source):
                                if entry.get('evidence_kind') != 'hardlink-identity-v1':
                                    raise ValueError('旧媒体复用记录仅供查看，请重新生成计划后再定位')
                                resolution = CollisionResolution.from_dict(dict(
                                    action='reuse_existing', evidence_kind=entry['evidence_kind'],
                                    source_size=entry.get('source_size'), target_size=entry.get('target_size'),
                                    target_device=entry.get('target_device'), target_inode=entry.get('target_inode')))
                                source_evidence, target_evidence = validate_reuse_resolution(source, new_target, resolution)
                            elif entry.get('evidence_kind') == 'content-xxh3-128-v1':
                                resolution = CollisionResolution.from_dict(dict(
                                    action='reuse_existing',evidence_kind=entry['evidence_kind'],
                                    source_size=entry.get('source_size'),target_size=entry.get('target_size'),
                                    source_xxh3_128=entry.get('source_xxh3_128'),target_xxh3_128=entry.get('target_xxh3_128'),
                                    target_device=entry.get('target_device'),target_inode=entry.get('target_inode')))
                                source_evidence,target_evidence=validate_reuse_resolution(source,new_target,resolution)
                            else:
                                raise ValueError('旧小文件复用记录仅供查看，请重新生成计划后再定位')
                        except (AssetCollisionError, ValueError) as exc:
                            raise SeriesCatalogError(str(exc)) from exc
                        if (
                            source_evidence.size != entry.get("source_size")
                            or getattr(source_evidence, 'xxh3_128', None) != entry.get('source_xxh3_128')
                            or getattr(target_evidence, 'xxh3_128', None) != entry.get('target_xxh3_128')
                            or source_evidence.device != entry.get("source_device")
                            or source_evidence.inode != entry.get("source_inode")
                            or target_evidence.size != entry.get("target_size")
                            or target_evidence.device != entry.get("target_device")
                            or target_evidence.inode != entry.get("target_inode")
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
            if payload.get('series_registration_authority') is not None:
                from .series_registration import SeriesRegistrationAuthority, relocated_registration
                original = SeriesRegistrationAuthority.from_mapping(payload['series_registration_authority'])
                payload['series_registration_authority'] = relocated_registration(original, payload, new_root).to_dict()
            payload.update({
                "schema_version": SERIES_CATALOG_SCHEMA_VERSION,
                "revision": int(payload.get("revision", 0)) + 1,
                "canonical_root": str(new_root),
                "relocated_from": str(old_root),
                "updated_at": _utc_now(),
            })
            candidate = self._match(payload)
            self._atomic_write(series_key, payload)
            return candidate

    def archive_missing(self, series_key: str, *, task_root: Path,
                        expected_fingerprint: Optional[str] = None) -> Path:
        """Archive a stale series registration without touching media files."""

        with self.lock(series_key):
            path = self._path(series_key)
            from .catalog_archive import require_archive_fingerprint
            try:
                require_archive_fingerprint(path, expected_fingerprint)
            except ValueError as exc:
                raise SeriesCatalogError(str(exc)) from exc
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
            self._require_namespace_candidate(series_key, None)
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


    def _path(self, series_key: str) -> Path:
        if _SAFE_KEY.fullmatch(str(series_key)) is None:
            raise SeriesCatalogError("series_key 无效")
        return self.root / f"{series_key}.json"

    def _atomic_write(self, key: str, payload: dict) -> None:
        from .catalog_namespace import require_write
        require_write(self, key)
        self._require_namespace_candidate(key, payload)
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
        if schema_version != SERIES_CATALOG_SCHEMA_VERSION:
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
        from .generated_outputs import validate_registered
        validate_registered(payload)
        return SeriesMatch(
            schema_version=schema_version,
            series_key=series_key,
            revision=revision,
            canonical_root=Path(str(payload["canonical_root"])),
            canonical_display_name=str(payload["canonical_display_name"]),
            season_bindings=tuple(payload.get("season_bindings", ())),
        )




































def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(Path(path)))




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
