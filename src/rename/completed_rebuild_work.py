"""Exact WorkCatalog evidence for restoring previously registered targets.

This module does not select history paths or confer approval. The caller owns
the typed rebuild observation and supplies a history loader which validates
task/Decision/Plan, archived ledger, source identity and surviving targets.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
from typing import Callable, Mapping

from .path_safety import first_reparse_component
from .plan_store import PlanStore
from .projection_authority import validate_plan_projection_receipt
from .rename_plan import RenamePlan


PeerLoader = Callable[[str, str | None], tuple[RenamePlan, Mapping[str, object]]]


@dataclass(frozen=True)
class WorkRebuildContext:
    """Explicit dependency injection; neither callback may trust raw metadata."""

    proof_for_plan: Callable[[RenamePlan], Mapping[str, object] | None]
    peer_loader: PeerLoader


def _plain(value):
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _key(path):
    return os.path.normcase(os.path.abspath(Path(path)))


def _work_key(plan):
    return str(plan.metadata.get("catalog_work_key") or plan.metadata.get("proposed_work_key") or "")


def _association(plan, entry):
    contract = entry.presentation_asset
    return {"source": str(entry.source_path), "task_id": plan.task_id,
            "owner_version_ref": contract.owner_version_ref if contract else None,
            "media_kind": entry.media_kind, "role": entry.role}


def _version(plan, entry):
    contract = entry.presentation_asset
    if contract is None or contract.asset_role.kind != "content_version":
        return None
    descriptor = contract.version_descriptor
    return {"target": str(entry.target_path), "source": str(entry.source_path),
            "version_ref": descriptor.version_ref if descriptor else None,
            "technical_variant_key": descriptor.technical_variant_key if descriptor else None,
            "display_label": descriptor.display_label if descriptor else None,
            "task_id": plan.task_id, "ledger_fingerprint": PlanStore.fingerprint(plan)}


def _origin_valid(catalog, payload, plan, ledger):
    validate_plan_projection_receipt(plan)
    if (not catalog.compatible_base(payload, plan)
            or plan.execution_policy is None or plan.execution_policy.mode != "hardlink"
            or not plan.execution_policy.preserve_source or plan.execution_policy.dry_run
            or ledger.get("task_id") != plan.task_id or ledger.get("status") != "completed"
            or ledger.get("plan_fingerprint") != PlanStore.fingerprint(plan)
            or ledger.get("mode") != "hardlink" or ledger.get("dry_run") is not False):
        raise ValueError("作品恢复缺少对应的原始已完成身份和执行证据")
    rows = ledger.get("entries")
    if not isinstance(rows, (list, tuple)) or len(rows) != len(plan.entries):
        raise ValueError("作品恢复原执行记录条目不完整")
    pairs = [(str(row.get("source")), str(row.get("target"))) for row in rows if isinstance(row, Mapping)]
    if pairs != [(str(entry.source_path), str(entry.target_path)) for entry in plan.entries]:
        raise ValueError("作品恢复原执行记录与计划资产不同")


def _semantic_plan(plan):
    """Keep business decisions exact while allowing create/reuse observations."""
    payload = plan.to_dict()
    entries = []
    for entry in payload["entries"]:
        row = dict(entry)
        row.pop("collision_resolution", None)
        row.pop("evidence", None)
        entries.append(row)
    return _plain({
        "task_id": plan.task_id, "source_root": str(plan.source_root),
        "expected_sources": [str(path) for path in plan.expected_sources],
        "entries": entries,
        **{name: payload.get(name) for name in (
            "work_identity", "linked_provider_identities", "presentation_groups",
            "subtitle_override_rules", "series_scope", "execution_policy")},
        "metadata": {name: plan.metadata.get(name) for name in (
            "canonical_work_root", "canonical_title", "target_root", "target_root_snapshot",
            "season_number", "season_binding_id", "season_source")},
    })


def inspect_work_rebuild(catalog, origin_plan: RenamePlan, *, peer_loader: PeerLoader) -> dict:
    """Prove this completed version, not unrelated versions' live sources."""
    from .work_catalog import copy_assets, catalog_record_fingerprint
    from .movie_asset_scope import _safe_record, selected_dependencies

    work_key = _work_key(origin_plan)
    payload = catalog.load(work_key)
    _safe_record(payload, physical_targets={str(entry.target_path) for entry in origin_plan.entries})
    fingerprint = catalog_record_fingerprint(payload)
    root = Path(str(payload["canonical_root"]))
    if not root.is_absolute() or first_reparse_component(root) is not None:
        raise ValueError("作品登记恢复目录不安全")
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("作品登记恢复版本无效")
    peers = {}

    def load_peer(task_id, plan_fingerprint=None):
        identity = (str(task_id), plan_fingerprint)
        if identity in peers:
            return peers[identity]
        if not task_id:
            raise ValueError("作品登记资产缺少原任务")
        peer, ledger = peer_loader(str(task_id), plan_fingerprint)
        if peer.task_id != task_id or (plan_fingerprint and PlanStore.fingerprint(peer) != plan_fingerprint):
            raise ValueError("作品登记恢复引用了不同任务或历史版本")
        _origin_valid(catalog, payload, peer, ledger)
        peers[identity] = (peer, ledger)
        return peer, ledger

    origin, _ledger = load_peer(origin_plan.task_id, PlanStore.fingerprint(origin_plan))
    selected_targets = {str(entry.target_path) for entry in origin.entries}
    assets = []
    for row in copy_assets(payload.get("assets", [])):
        if row.get("target") in selected_targets:
            assets.append({**row, "associations": [dict(item) for item in row.get("associations", ())
                if item.get("task_id") == origin.task_id]})
    if not assets:
        raise ValueError("作品登记没有可证明的恢复资产")
    targets = set()
    represented = set()
    for asset in assets:
        target_value = str(asset.get("target") or "")
        target = Path(target_value)
        target_key = _key(target)
        try:
            target.resolve(strict=False).relative_to(root.resolve(strict=False))
        except (OSError, ValueError) as exc:
            raise ValueError("作品登记资产越出原目录") from exc
        if not target.is_absolute() or target_key in targets or first_reparse_component(target) is not None:
            raise ValueError("作品登记恢复目标重复或包含重解析点")
        if os.path.lexists(target) and not target.is_file():
            raise ValueError("作品登记恢复目标不是普通文件")
        targets.add(target_key)
        associations = asset.get("associations")
        if not isinstance(associations, list) or not associations:
            raise ValueError("作品登记资产没有历史归属证明")
        seen_associations = set()
        for association in associations:
            if not isinstance(association, Mapping):
                raise ValueError("作品登记资产关联损坏")
            peer, ledger = load_peer(str(association.get("task_id") or ""))
            matches = [entry for entry in peer.entries if str(entry.target_path) == target_value
                       and _association(peer, entry) == dict(association)]
            if len(matches) != 1:
                raise ValueError("作品登记资产与原始计划的版本、来源或角色不同")
            entry = matches[0]
            identity = (peer.task_id, str(entry.source_path), target_value)
            if identity in seen_associations:
                raise ValueError("作品登记资产存在重复来源关联")
            seen_associations.add(identity)
            represented.add(identity)
            expected_scope = "shared" if entry.presentation_asset and entry.presentation_asset.owner_version_ref == "shared" else "version"
            if asset.get("scope") != expected_scope:
                raise ValueError("作品登记资产作用域与原计划不同")
            content_key = asset.get('content_key')
            from .asset_collision import requires_manual_collision
            if content_key is not None and str(content_key).startswith('xxh3-128:') and not requires_manual_collision(entry.source_path):
                from .small_file_identity import small_fingerprint
                evidence=small_fingerprint(entry.source_path)
                if (content_key != f'xxh3-128:{evidence.size}:{evidence.xxh3_128}'
                        or asset.get('size') != evidence.size or asset.get('xxh3_128') != evidence.xxh3_128
                        or asset.get('sha256') is not None):
                    raise ValueError('作品登记内容与历史来源不一致')
                if target.is_file():
                    actual=small_fingerprint(target)
                    if (actual.size,actual.xxh3_128) != (evidence.size,evidence.xxh3_128):
                        raise ValueError('作品登记目标的内容已被替换')
            elif content_key is not None:
                raise ValueError('作品登记资产内容证据格式不受支持')
    versions = payload.get("versions")
    if not isinstance(versions, list):
        raise ValueError("作品登记版本清单损坏")
    versions = [row for row in versions if row.get("task_id") == origin.task_id]
    seen_versions = set()
    for row in versions:
        if not isinstance(row, Mapping) or not row.get("ledger_fingerprint"):
            raise ValueError("作品登记版本缺少原执行指纹")
        peer, _ledger = load_peer(str(row.get("task_id") or ""), str(row["ledger_fingerprint"]))
        if sum(_version(peer, entry) == dict(row) for entry in peer.entries) != 1:
            raise ValueError("作品登记版本与原始完成计划不同")
        target_key = _key(str(row.get("target") or ""))
        if target_key in seen_versions or target_key not in targets:
            raise ValueError("作品登记版本重复或没有资产归属")
        seen_versions.add(target_key)
    for peer, _ledger in peers.values():
        for entry in peer.entries:
            if (peer.task_id, str(entry.source_path), str(entry.target_path)) not in represented:
                raise ValueError("原始完成计划含有尚未登记的资产")
            version = _version(peer, entry)
            if version is not None and version not in versions:
                raise ValueError("原始完成计划的内容版本没有登记")
    if catalog_record_fingerprint(catalog.load(work_key)) != fingerprint:
        raise ValueError("核查期间作品登记发生变化")
    unique_peers = {(peer.task_id, PlanStore.fingerprint(peer)): ledger for peer, ledger in peers.values()}
    scope = {"identity": {name: payload.get(name) for name in (
        "schema_version", "work_key", "canonical_root", "domain", "group_kind", "providers")},
        "dependencies": selected_dependencies(payload, origin, repair=True)}
    return {
        "schema_version": 2, "work_key": work_key, "catalog_revision": revision,
        "catalog_fingerprint": catalog_record_fingerprint(scope), "canonical_root": str(root),
        "origin_task_id": origin.task_id, "origin_plan_fingerprint": PlanStore.fingerprint(origin),
        "missing_targets": sorted(target for target in targets if not Path(target).is_file()),
        "peers": [{"task_id": task, "plan_fingerprint": fp,
                   "ledger_fingerprint": catalog_record_fingerprint(_plain(ledger))}
                  for (task, fp), ledger in sorted(unique_peers.items())],
    }


def validate_work_rebuild(plan, catalog, proof, *, peer_loader: PeerLoader):
    """Consume an approved typed proof; new missing facts require a new Plan."""
    if not isinstance(proof, Mapping) or proof.get("schema_version") != 2:
        raise ValueError("电影重建缺少完整作品恢复证明")
    if proof.get("origin_task_id") != plan.task_id:
        raise ValueError("电影恢复证明属于其他任务")
    origin, _ledger = peer_loader(plan.task_id, str(proof.get("origin_plan_fingerprint") or ""))
    if _semantic_plan(origin) != _semantic_plan(plan):
        raise ValueError("电影重建改变了原作品、版本或资产决定")
    current = inspect_work_rebuild(catalog, origin, peer_loader=peer_loader)
    for name in ("work_key", "catalog_fingerprint", "canonical_root",
                 "origin_task_id", "origin_plan_fingerprint", "peers"):
        if _plain(current[name]) != _plain(proof.get(name)):
            raise ValueError("电影重建的登记或历史证据已变化")
    missing = proof.get("missing_targets")
    if not isinstance(missing, (list, tuple)) or not set(current["missing_targets"]).issubset(set(missing)):
        raise ValueError("批准后出现新的缺失目标，请重新核查")
    binding = plan.metadata.get("catalog_binding_snapshot")
    if (not isinstance(binding, Mapping) or dict(binding) != {
            "backend": "work", "key": current["work_key"], "revision": proof["catalog_revision"]}
            or plan.metadata.get("catalog_work_key") != current["work_key"]
            or plan.metadata.get("catalog_revision") != proof["catalog_revision"]):
        raise ValueError("电影重建计划与当前登记绑定不一致")
    return catalog.load(current["work_key"])


def validate_work_rebuild_completed(plan, ledger, catalog, context: WorkRebuildContext):
    """Prove new physical completion without rewriting old registered facts."""
    from .execution_recovery import _validate_completed_hardlink_ledger
    from .plan_execution import PlanExecutionService
    from .work_catalog import _ledger_identity_matches_current

    if not isinstance(context, WorkRebuildContext):
        raise ValueError("电影重建缺少可信恢复上下文")
    proof = context.proof_for_plan(plan)
    if proof is None:
        raise ValueError("电影重建没有经过权威验证")
    current = validate_work_rebuild(plan, catalog, proof, peer_loader=context.peer_loader)
    PlanExecutionService._validate_frozen_source_identities(plan)
    _origin_valid(catalog, current, plan, ledger)
    for entry, row in zip(plan.entries, ledger["entries"]):
        if not _ledger_identity_matches_current(
            row, source_stat=entry.source_path.stat(), target_stat=entry.target_path.stat(),
            require_identity=True, require_hardlink=row.get("status") == "created",
        ):
            raise ValueError("电影重建执行记录与当前文件身份不一致")
    _validate_completed_hardlink_ledger(plan=plan, ledger=ledger, fingerprint=PlanStore.fingerprint(plan))
    return current
