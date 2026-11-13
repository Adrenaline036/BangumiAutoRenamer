"""Catalog membership and explicit same-owner reentry, never source release."""
from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path

from .catalog_binding import binding_from_plan
from .completed_history import archive_exact, read_bytes
from .decision_state import DecisionCommandService
from .file_lock import advisory_file_lock, task_operation_lock
from .path_safety import first_reparse_component
from .plan_store import PlanStore
from .projection_authority import TaskTerminalMutation, validate_plan_projection_receipt
from .season_identity import provider_key


@dataclass(frozen=True)
class CatalogMembership:
    state: str
    backend: str
    key: str
    revision: int


def membership(rebuild, plan):
    """Only explicit Catalog rows/history distinguish current from superseded.

    This predicate reads metadata, not source or target contents. An absent
    directory is never evidence of supersession.
    """
    validate_plan_projection_receipt(plan)
    binding = binding_from_plan(plan)
    if binding is None or plan.work_identity is None or plan.work_identity.status != "confirmed":
        raise ValueError("资产缺少可信作品登记身份")
    catalog = rebuild.work if binding.backend == "work" else rebuild.series
    path = catalog._path(binding.key)
    if first_reparse_component(path) is not None:
        raise ValueError("资产登记路径不安全")
    if not path.exists():
        return CatalogMembership("unregistered", binding.backend, binding.key, binding.revision)
    record = json.loads(read_bytes(path))
    catalog.validate_archived_payload(record, binding.key)
    if Path(record["canonical_root"]) != Path(plan.metadata.get("canonical_work_root") or ""):
        raise ValueError("资产登记规范根与计划不同")
    if record.get("domain") != plan.work_identity.domain:
        raise ValueError("资产登记媒体域与计划不同")
    history = record.get("asset_history", ())
    if not isinstance(history, (list, tuple)):
        raise ValueError("资产历史格式损坏")
    origin = plan
    observation = plan.metadata.get("completed_rebuild_observation")
    if isinstance(observation, Mapping):
        origin = rebuild.plans.load_version(plan.task_id, str(observation["origin_plan_fingerprint"]))
    if binding.backend == "work" and {g.kind for g in plan.presentation_groups} == {"movie_continuous"}:
        from .completed_rebuild_work import _version
        if not catalog.compatible_base(record, plan):
            raise ValueError("资产与作品登记身份不同")
        expected = [_version(origin, entry) for entry in origin.entries if _version(origin, entry) is not None]
        if not expected:
            raise ValueError("电影资产缺少版本身份")
        refs = {row["version_ref"] for row in expected}
        current = [row for row in record["versions"] if row.get("version_ref") in refs]
        is_current = all(row in current for row in expected)
        historical = [row for event in history for row in event.get("versions", ())]
        superseded = (all(row in historical for row in expected) and bool(current)
            and all(row.get("task_id") != plan.task_id for row in current))
    else:
        season = plan.metadata.get("season_number")
        provider = provider_key(plan.work_identity.primary_provider.to_dict())
        if type(season) is not int or provider is None:
            raise ValueError("季度资产缺少明确身份")
        def selected(row):
            return row.get("season_number") == season and provider_key(
                row.get("provider_identity") or row.get("provider")) == provider
        current = [row for row in record.get("season_bindings", record.get("seasons", ())) if selected(row)]
        is_current = any(row.get("task_id") == plan.task_id for row in current)
        historical = [row for event in history for row in event.get("previous_bindings", ()) if selected(row)]
        superseded = (any(row.get("task_id") == plan.task_id for row in historical)
            and bool(current) and all(row.get("task_id") != plan.task_id for row in current))
    state = "current" if is_current else "superseded" if superseded else "unregistered"
    return CatalogMembership(state, binding.backend, binding.key, int(record["revision"]))


def inspect_reentry(rebuild, review_service, task_id):
    """Validate old completion/source ownership without demanding its old targets."""
    from .plan_review import PlanReviewService
    from .plan_execution import PlanExecutionService
    if not isinstance(review_service, PlanReviewService):
        raise ValueError("重新添加已接替来源需要当前计划审核服务")
    if (review_service.plan_store.root != rebuild.plans.root
            or review_service.decision_state_store.root != rebuild.decisions.root
            or review_service.task_root != rebuild.tasks or review_service.record_root != rebuild.records
            or review_service.work_catalog.root != rebuild.work.root
            or review_service.series_catalog.root != rebuild.series.root):
        raise ValueError("重新添加来源的审核上下文不一致")
    task, plan, state = rebuild._current(task_id)
    fingerprint = PlanStore.fingerprint(plan)
    if (task.get("workflow_status") not in {"completed", "partial_failed"} or task.get("execution_status") != "completed"
            or task.get("execution_fingerprint") != fingerprint
            or task.get("execution_requires_manual_check") or task.get("execution_may_have_written_targets")):
        raise ValueError("只有可信完成来源可生成新的待选择计划")
    member = membership(rebuild, plan)
    if member.state != "superseded":
        raise ValueError("原来源没有明确被接替的登记历史")
    if state.completed_rebuild_observation is not None:
        rebuild._signed_observation(plan, state)
    PlanExecutionService._validate_frozen_source_identities(plan)
    from .content_selection import validate_media_intent
    validate_media_intent(plan, state, task_root=rebuild.tasks)
    actions = review_service.asset_actions()
    historical, rows, ledger_sha = actions._history(task_id, fingerprint)
    if historical != plan:
        raise ValueError("重新添加来源的历史计划不一致")
    for entry in plan.entries:
        row = rows[(str(entry.source_path), str(entry.target_path))]
        actions._registered_identity(row)
        from .completed_history import validate_recorded_media_source
        validate_recorded_media_source(entry.source_path, row)
    ledger = read_bytes(rebuild.records / f"{task_id}.ledger.json")
    if hashlib.sha256(ledger).hexdigest() != ledger_sha:
        raise ValueError("当前完成记录与已归档历史冲突")
    mapping = read_bytes(rebuild.records / f"{task_id}.json")
    return task, plan, state, member, ledger, hashlib.sha256(mapping).hexdigest()


def reentry_evidence(rebuild, review_service, task_id):
    task, plan, state, member, ledger, mapping_sha = inspect_reentry(rebuild, review_service, task_id)
    catalog = rebuild.work if member.backend == 'work' else rebuild.series
    from .rebuild_observation import digest
    return digest({'task': task, 'plan': PlanStore.fingerprint(plan),
        'decision': rebuild.decisions.fingerprint(state), 'membership': member.__dict__,
        'ledger': hashlib.sha256(ledger).hexdigest(), 'mapping': mapping_sha,
        'catalog': hashlib.sha256(read_bytes(catalog._path(member.key))).hexdigest()})


def generate_reentry(rebuild, review_service, task_id):
    """Archive exact old bytes, then ask the canonical producer for a new choice.

    No replacement observation is selected here. The existing explicit user
    confirmation remains the only way to choose/approve a replacement.
    """
    rebuild.plans.path_for(task_id)
    inspect_reentry(rebuild, review_service, task_id)
    with task_operation_lock(rebuild.tasks, task_id, operation='completed_source_reentry'), \
            advisory_file_lock(rebuild.tasks / f".{task_id}.rebuild.lock", require_single_link=True):
        task, old, state, member, ledger, mapping_sha = inspect_reentry(rebuild, review_service, task_id)
        catalog = rebuild.work if member.backend == "work" else rebuild.series
        with catalog.lock(member.key):
            task, old, state, member, ledger, mapping_sha = inspect_reentry(rebuild, review_service, task_id)
            changes = dict(asset_operation=None, completed_rebuild_observation=None,
                           recovery_state="healthy", execution_failure=None)
            metadata = {key: value for key, value in old.metadata.items()
                if key not in {"asset_operation", "completed_rebuild_observation", "asset_replacement_retained"}}
            if member.backend == "work":
                intent = dict(state.catalog_link_intent or {})
                intent.update(work_key=member.key, catalog_revision=member.revision)
                if intent.get('affiliation') is not None:
                    from .work_affiliation import refresh_registered
                    intent['affiliation'] = refresh_registered(intent['affiliation'], rebuild.work.load(member.key))
                changes["catalog_link_intent"] = intent
                metadata.update(catalog_work_key=member.key, catalog_revision=member.revision)
            else:
                from .series_registration import SeriesRegistrationAuthority, _materialized_evidence, rebind_materialized_registration
                current_binding = dict(state.series_binding or {})
                proof = SeriesRegistrationAuthority.from_mapping(current_binding["registration_authority"])
                materialized = _materialized_evidence(proof, rebuild.series, rebuild.series.load(member.key),
                    library_domain=old.work_identity.domain)
                proof = rebind_materialized_registration(proof, materialized)
                current_binding.update(catalog_revision=member.revision, registration_authority=proof.to_dict())
                if proof.affiliation:
                    current_binding['affiliation'] = json.loads(proof.affiliation)
                changes["series_binding"] = current_binding
            draft = replace(old, entries=tuple(replace(e, collision_resolution=None) for e in old.entries),
                metadata=metadata, projection_receipt=None, revision_parent_fingerprint=PlanStore.fingerprint(old),
                revision_action="completed_source_reentry", revision_reason="重新添加已被接替来源，等待明确选择资产操作")
            from .content_selection import validate_media_intent
            validate_media_intent(old, state, task_root=rebuild.tasks)
            from .media_archive import MediaArchiveContext, MediaArchiveIntent
            archive_context = MediaArchiveContext.for_rebuild(rebuild, review_service=review_service)
            archive = MediaArchiveIntent.prepare(archive_context, old, state,
                action='completed_source_reentry', archive_fingerprint=PlanStore.fingerprint(old),
                ledger_bytes=ledger, mapping_sha256=mapping_sha)
            def terminal(_decision, _plan, payload):
                retained = {k: v for k, v in payload.items() if not k.startswith('execution_')}
                return {**retained, "workflow_status": "need_confirm", "queue_operation": None,
                    "execution_status": None, "approved_at": None, "completed_at": None,
                    "error": None}
            _, result = DecisionCommandService(rebuild.decisions, rebuild.plans,
                media_archive_context=archive_context).update(task_id,
                media_archive=archive,
                expected_revision=state.revision, changes=changes, planner=lambda _candidate: draft,
                task_terminal=TaskTerminalMutation.prepare(rebuild.tasks, task_id, terminal))
            return result
