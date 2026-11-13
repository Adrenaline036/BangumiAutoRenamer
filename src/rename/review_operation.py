"""Versioned application boundary for persisted review operations.

Reason codes and previews are explanatory projections.  Only a frozen
``ReviewCommand`` grants a business action, and every command is bound to one
operation revision, fingerprint, complete payload and idempotency key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import copy
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional

from .provider_link_decision import (
    build_provider_subject_cohort_preview,
    provider_subject_identity_candidates,
    provider_subject_cohort_request_fingerprint,
    resolve_provider_subject_boundary_decision,
    freeze_provider_subject_boundary_edges,
)
from .provider_selection_authority import (
    provider_candidate_group_kind,
    provider_selection_capability,
    task_library_domain_from_task,
    task_media_kind_from_task,
)
from .target_routing import TargetRootSnapshot, resolve_target_root


REVIEW_OPERATION_SCHEMA_VERSION = 2
PROVIDER_SELECTION_OUTCOME_SCHEMA_VERSION = 1


class TaskWorkflowState(str, Enum):
    RESOLVED = "resolved"
    ACTIONABLE = "actionable"
    BLOCKED = "blocked"


def _plain(value: object) -> object:
    return copy.deepcopy(value)


def _fingerprint(value: object, *, domain: str) -> str:
    payload = json.dumps(
        {"domain": domain, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class PreviewModel:
    payload: Mapping[str, object]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PreviewModel":
        return cls(MappingProxyType(dict(_plain(dict(value)))))

    def to_dict(self) -> dict[str, object]:
        return dict(_plain(dict(self.payload)))


@dataclass(frozen=True)
class ReviewCommand:
    kind: str
    expected_revision: int
    operation_fingerprint: str
    payload: Mapping[str, object]
    payload_fingerprint: str
    idempotency_key: str

    @classmethod
    def build(
        cls,
        *,
        kind: str,
        expected_revision: int,
        operation_fingerprint: str,
        payload: Mapping[str, object],
    ) -> "ReviewCommand":
        frozen_payload = dict(_plain(dict(payload)))
        payload_fingerprint = _fingerprint(
            frozen_payload, domain="bar.review-command-payload"
        )
        idempotency_key = _fingerprint(
            {
                "kind": kind,
                "expected_revision": expected_revision,
                "operation_fingerprint": operation_fingerprint,
                "payload_fingerprint": payload_fingerprint,
            },
            domain="bar.review-command-idempotency",
        )
        return cls(
            kind=str(kind),
            expected_revision=int(expected_revision),
            operation_fingerprint=str(operation_fingerprint),
            payload=MappingProxyType(frozen_payload),
            payload_fingerprint=payload_fingerprint,
            idempotency_key=idempotency_key,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReviewCommand":
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("review command payload is invalid")
        command = cls.build(
            kind=str(value.get("kind") or ""),
            expected_revision=int(value.get("expected_revision")),
            operation_fingerprint=str(value.get("operation_fingerprint") or ""),
            payload=payload,
        )
        if (
            value.get("payload_fingerprint") != command.payload_fingerprint
            or value.get("idempotency_key") != command.idempotency_key
        ):
            raise ValueError("review command fingerprint mismatch")
        return command

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "expected_revision": self.expected_revision,
            "operation_fingerprint": self.operation_fingerprint,
            "payload": dict(_plain(dict(self.payload))),
            "payload_fingerprint": self.payload_fingerprint,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class ReviewOperation:
    state: TaskWorkflowState
    revision: int
    fingerprint: str
    preview: Optional[PreviewModel] = None
    commands: tuple[ReviewCommand, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    explanation_codes: tuple[str, ...] = ()
    context: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    schema_version: int = REVIEW_OPERATION_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReviewOperation":
        if value.get("schema_version") != REVIEW_OPERATION_SCHEMA_VERSION:
            raise ValueError("review operation schema is invalid")
        state = TaskWorkflowState(str(value.get("state") or ""))
        preview_value = value.get("preview")
        preview = (
            PreviewModel.from_mapping(preview_value)
            if isinstance(preview_value, Mapping) else None
        )
        commands_value = value.get("commands", ())
        if not isinstance(commands_value, (list, tuple)):
            raise ValueError("review operation commands are invalid")
        if not all(isinstance(item, Mapping) for item in commands_value):
            raise ValueError("review operation command entry is invalid")
        context_value = value.get("context", {})
        if not isinstance(context_value, Mapping):
            raise ValueError("review operation context is invalid")
        commands = tuple(
            ReviewCommand.from_mapping(item)
            for item in commands_value
        )
        operation = cls(
            state=state,
            revision=int(value.get("revision")),
            fingerprint=str(value.get("fingerprint") or ""),
            preview=preview,
            commands=commands,
            blocking_reasons=tuple(
                str(item) for item in value.get("blocking_reasons", ()) or ()
            ),
            explanation_codes=tuple(
                str(item) for item in value.get("explanation_codes", ()) or ()
            ),
            context=MappingProxyType(dict(_plain(dict(context_value)))),
        )
        expected_fingerprint = _review_operation_fingerprint(
            state=operation.state,
            revision=operation.revision,
            preview=operation.preview,
            blocking_reasons=operation.blocking_reasons,
            explanation_codes=operation.explanation_codes,
            context=operation.context,
        )
        if operation.fingerprint != expected_fingerprint:
            raise ValueError("review operation fingerprint mismatch")
        if any(
            item.operation_fingerprint != operation.fingerprint
            for item in operation.commands
        ):
            raise ValueError("review command is bound to another operation")
        if operation.state == TaskWorkflowState.ACTIONABLE and not operation.commands:
            raise ValueError("actionable review operation has no command")
        if operation.state != TaskWorkflowState.ACTIONABLE and operation.commands:
            raise ValueError("non-actionable review operation has commands")
        if operation.context.get("kind") == "provider_selection":
            for command in operation.commands:
                payload = command.payload
                if not provider_selection_command_is_capable(
                    None,
                    operation.context,
                    kind=command.kind,
                    expected_revision=command.expected_revision,
                    evidence_fingerprint=str(
                        payload.get("evidence_fingerprint") or ""
                    ),
                    boundary_edge=dict(payload.get("boundary_edge") or {}),
                    same_work=bool(payload.get("same_work")),
                ):
                    raise ValueError("review command capability is invalid")
        return operation

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "state": self.state.value,
            "revision": self.revision,
            "fingerprint": self.fingerprint,
            "preview": self.preview.to_dict() if self.preview is not None else None,
            "commands": [item.to_dict() for item in self.commands],
            "blocking_reasons": list(self.blocking_reasons),
            "explanation_codes": list(self.explanation_codes),
            "context": dict(_plain(dict(self.context))),
        }


@dataclass(frozen=True)
class ProviderSelectionAwaitingUserOutcome:
    """Typed process-to-queue result for a legitimate provider decision gate.

    This value is not itself durable authority.  The queue must bind it to the
    current request/revision and project it through ``set_review_operation``.
    Raw mappings and exception text must never grant the same capability.
    """

    task_id: str
    candidate: Mapping[str, object]
    provider_subject_evidence: Mapping[str, object]
    state: str = "awaiting_user"
    schema_version: int = PROVIDER_SELECTION_OUTCOME_SCHEMA_VERSION

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        candidate: Mapping[str, object],
        provider_subject_evidence: Mapping[str, object],
    ) -> "ProviderSelectionAwaitingUserOutcome":
        return cls.from_mapping({
            "schema_version": PROVIDER_SELECTION_OUTCOME_SCHEMA_VERSION,
            "state": "awaiting_user",
            "task_id": task_id,
            "candidate": dict(_plain(dict(candidate))),
            "provider_subject_evidence": dict(
                _plain(dict(provider_subject_evidence))
            ),
        })

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "ProviderSelectionAwaitingUserOutcome":
        if (
            value.get("schema_version")
            != PROVIDER_SELECTION_OUTCOME_SCHEMA_VERSION
            or value.get("state") != "awaiting_user"
        ):
            raise ValueError("provider selection outcome schema is invalid")
        task_id = str(value.get("task_id") or "").strip()
        candidate_value = value.get("candidate")
        evidence_value = value.get("provider_subject_evidence")
        if not task_id or not isinstance(candidate_value, Mapping):
            raise ValueError("provider selection outcome identity is invalid")
        if not isinstance(evidence_value, Mapping):
            raise ValueError("provider selection outcome evidence is invalid")
        candidate = dict(_plain(dict(candidate_value)))
        evidence = dict(_plain(dict(evidence_value)))
        provider_id = str(candidate.get("provider_id") or "").strip()
        if (
            str(candidate.get("namespace") or "").casefold() != "bangumi"
            or str(candidate.get("entity_type") or "").casefold() != "subject"
            or not provider_id
            or str(evidence.get("selected_subject_id") or "").strip()
            != provider_id
        ):
            raise ValueError("provider selection outcome candidate is invalid")
        fingerprint = str(evidence.get("evidence_fingerprint") or "")
        if len(fingerprint) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in fingerprint
        ):
            raise ValueError("provider selection outcome fingerprint is invalid")
        if evidence.get("retry_requires_refresh") is not False:
            raise ValueError("provider selection outcome unexpectedly requires refresh")
        observations = evidence.get("subject_observations")
        subjects = evidence.get("bangumi_subjects")
        relations = evidence.get("bangumi_relations")
        edges = evidence.get("provider_subject_boundary_edges")
        if (
            not isinstance(observations, Mapping)
            or not isinstance(subjects, (list, tuple))
            or not isinstance(relations, (list, tuple))
            or not isinstance(edges, (list, tuple))
        ):
            raise ValueError("provider selection outcome observations are invalid")
        return cls(
            task_id=task_id,
            candidate=MappingProxyType(candidate),
            provider_subject_evidence=MappingProxyType(evidence),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "task_id": self.task_id,
            "candidate": dict(_plain(dict(self.candidate))),
            "provider_subject_evidence": dict(
                _plain(dict(self.provider_subject_evidence))
            ),
        }


def _review_operation_fingerprint(
    *,
    state: TaskWorkflowState,
    revision: int,
    preview: Optional[PreviewModel],
    blocking_reasons: tuple[str, ...],
    explanation_codes: tuple[str, ...],
    context: Mapping[str, object],
) -> str:
    return _fingerprint(
        {
            "schema_version": REVIEW_OPERATION_SCHEMA_VERSION,
            "state": state.value,
            "revision": int(revision),
            "preview": preview.to_dict() if preview is not None else None,
            "blocking_reasons": list(blocking_reasons),
            "explanation_codes": list(explanation_codes),
            "context": dict(_plain(dict(context))),
        },
        domain="bar.review-operation",
    )


def _new_review_operation(
    *,
    state: TaskWorkflowState,
    revision: int,
    context: Mapping[str, object],
    preview: Optional[PreviewModel] = None,
    blocking_reasons: tuple[str, ...] = (),
    explanation_codes: tuple[str, ...] = (),
    command_specs: tuple[tuple[str, Mapping[str, object]], ...] = (),
) -> ReviewOperation:
    frozen_context = MappingProxyType(dict(_plain(dict(context))))
    normalized_blocking = tuple(dict.fromkeys(blocking_reasons))
    normalized_explanations = tuple(dict.fromkeys(explanation_codes))
    fingerprint = _review_operation_fingerprint(
        state=state,
        revision=revision,
        preview=preview,
        blocking_reasons=normalized_blocking,
        explanation_codes=normalized_explanations,
        context=frozen_context,
    )
    commands = tuple(
        ReviewCommand.build(
            kind=kind,
            expected_revision=revision,
            operation_fingerprint=fingerprint,
            payload=payload,
        )
        for kind, payload in command_specs
    )
    return ReviewOperation(
        state=state,
        revision=revision,
        fingerprint=fingerprint,
        preview=preview,
        commands=commands,
        blocking_reasons=normalized_blocking,
        explanation_codes=normalized_explanations,
        context=frozen_context,
    )




def _preview_for_failure(
    task: Mapping[str, object],
    attempt: Mapping[str, object],
    failure: Mapping[str, object],
    *,
    expected_revision: int,
    edge: Mapping[str, object],
) -> dict[str, object]:
    existing_preview = attempt.get("provider_subject_cohort_preview")
    if isinstance(existing_preview, Mapping):
        request = existing_preview.get("request")
        request_fingerprint = str(
            existing_preview.get("request_fingerprint") or ""
        )
        edge_identity = (
            str(edge.get("from_subject_id") or ""),
            str(edge.get("to_subject_id") or ""),
            str(edge.get("relation") or "").casefold(),
        )
        try:
            existing_is_current = bool(
                existing_preview.get("schema_version") == 3
                and isinstance(request, Mapping)
                and request.get("expected_decision_revision") == expected_revision
                and str(request.get("evidence_fingerprint") or "")
                == str(failure.get("evidence_fingerprint") or "")
                and provider_subject_cohort_request_fingerprint(request)
                == request_fingerprint
                and edge_identity in {
                    (
                        str(item.get("from_subject_id") or ""),
                        str(item.get("to_subject_id") or ""),
                        str(item.get("relation") or "").casefold(),
                    )
                    for item in request.get("cohort_edges", ()) or ()
                    if isinstance(item, Mapping)
                }
            )
        except (TypeError, ValueError):
            existing_is_current = False
        if existing_is_current and _provider_target_snapshot_matches(task, request):
            return dict(_plain(dict(existing_preview)))
    try:
        from .pending_tv_selection import review_target_snapshot
        roots, roots_fingerprint = review_target_snapshot(task)
        return build_provider_subject_cohort_preview(
            failure,
            expected_decision_revision=expected_revision,
            boundary_edge=edge,
            target_root_snapshot=roots,
            target_root_config_fingerprint=roots_fingerprint,
            work_domain="animation",
            group_kind="tv_episodes",
        )
    except (TypeError, ValueError):
        try:
            resolution, decision = resolve_provider_subject_boundary_decision(
                failure, same_work=True, boundary_edge=edge
            )
        except (TypeError, ValueError):
            subjects = sorted(
                (
                    item for item in failure.get("bangumi_subjects", ()) or ()
                    if isinstance(item, Mapping)
                ),
                key=lambda item: (
                    str(item.get("air_date") or "9999-99-99"),
                    str(item.get("provider_id") or item.get("id") or ""),
                ),
            )
            selected = str(failure.get("selected_subject_id") or "")
            selected_row = next((
                item for item in subjects
                if str(item.get("provider_id") or item.get("id") or "")
                == selected
            ), None)
            if selected_row is None:
                return {
                    "schema_version": 1,
                    "season_assignments": [],
                    "outside_subject_titles": [],
                    "resolved_target_root": "",
                }
            season_number = subjects.index(selected_row) + 1
            return {
                "schema_version": 1,
                "season_assignments": [{
                    "subject_id": selected,
                    "title": str(selected_row.get("title") or selected),
                    "season_number": season_number,
                }],
                "outside_subject_titles": [],
                "resolved_target_root": "",
            }
        subjects = {
            str(item.get("provider_id") or item.get("id") or ""): item
            for item in failure.get("bangumi_subjects", ()) or ()
            if isinstance(item, Mapping)
        }
        subject_ids = tuple(
            str(item) for item in (decision or {}).get("cohort_subject_ids", ())
        )
        return {
            "schema_version": 1,
            "season_assignments": [
                {
                    "subject_id": subject_id,
                    "title": str(
                        subjects.get(subject_id, {}).get("title") or subject_id
                    ),
                    "season_number": resolution.season_assignments[subject_id],
                }
                for subject_id in subject_ids
                if subject_id in resolution.season_assignments
            ],
            "outside_subject_titles": [],
            "resolved_target_root": "",
        }


def provider_selection_command_is_capable(
    task: Optional[Mapping[str, object]],
    context: Mapping[str, object],
    *,
    kind: str,
    expected_revision: int,
    evidence_fingerprint: str,
    boundary_edge: Mapping[str, object],
    same_work: bool,
) -> bool:
    """旧关系网合包命令已退役；单条目确认使用正常作品选择。"""
    return False


def _provider_target_snapshot_matches(task, request=None):
    if task is None:
        return True
    try:
        from .pending_tv_selection import review_target_snapshot
        snapshot_value, snapshot_fingerprint = review_target_snapshot(task)
        if not isinstance(snapshot_value, Mapping):
            return False
        snapshot = TargetRootSnapshot.from_dict(snapshot_value)
        target_root = resolve_target_root("animation", "tv_episodes", snapshot)
    except (TypeError, ValueError):
        return False
    return (
        str(snapshot_fingerprint or "")
        == snapshot.fingerprint
        and (request is None or (
            str(request.get("target_root_config_fingerprint") or "") == snapshot.fingerprint
            and str(request.get("resolved_target_root") or "") == str(target_root)))
    )


def build_review_operation(
    task: Mapping[str, object],
) -> tuple[ReviewOperation, Optional[dict[str, object]]]:
    """Load current authority or expose historical state as blocked/stale."""

    existing = task.get("review_operation")
    if isinstance(existing, Mapping):
        try:
            operation = ReviewOperation.from_mapping(existing)
        except (TypeError, ValueError):
            context_value = existing.get("context")
            context = (
                dict(_plain(dict(context_value)))
                if isinstance(context_value, Mapping)
                else {"kind": "invalid_current", "lifecycle": "blocked"}
            )
            preview_value = existing.get("preview")
            preview = (
                PreviewModel.from_mapping(preview_value)
                if isinstance(preview_value, Mapping)
                else None
            )
            try:
                revision = int(existing.get("revision"))
            except (TypeError, ValueError):
                revision = 0
            return _blocked_operation(
                revision,
                ("review_operation_invalid",),
                context=context,
                preview=preview,
            ), None
        if operation.context.get("kind") == "provider_selection" and any(
            not provider_selection_command_is_capable(
                task,
                operation.context,
                kind=command.kind,
                expected_revision=command.expected_revision,
                evidence_fingerprint=str(
                    command.payload.get("evidence_fingerprint") or ""
                ),
                boundary_edge=dict(command.payload.get("boundary_edge") or {}),
                same_work=bool(command.payload.get("same_work")),
            )
            for command in operation.commands
        ):
            return _blocked_operation(
                operation.revision,
                ("review_command_capability_unavailable",),
                context=operation.context,
                preview=operation.preview,
            ), None
        if operation.context.get('lifecycle') == 'awaiting_season':
            from .ova_selection import pending_subject
            try:
                pending_subject(task, operation.context)
            except (ValueError, TypeError, KeyError):
                return _blocked_operation(operation.revision, ('review_operation_invalid',), context=operation.context), None
        return operation, None
    legacy = task.get("selection_operation")
    if isinstance(legacy, Mapping):
        try:
            revision = int(legacy.get("expected_decision_revision"))
        except (TypeError, ValueError):
            revision = 0
        stale_context = dict(_plain(dict(legacy)))
        stale_context.update({
            "kind": "legacy_provider_selection",
            "lifecycle": "legacy_stale",
        })
        return _blocked_operation(
            revision,
            ("legacy_review_operation_stale",),
            context=stale_context,
        ), None
    return _new_review_operation(
        state=TaskWorkflowState.RESOLVED,
        revision=0,
        context={"kind": "none", "lifecycle": "resolved"},
    ), None


def provider_selection_awaits_user(task: Mapping[str, object]) -> bool:
    """Return whether current typed authority still requires user judgment."""

    operation, _unused = build_review_operation(task)
    return (
        operation.context.get("kind") == "provider_selection"
        and operation.context.get("lifecycle") == "awaiting_user"
    )


def build_provider_selection_review_operation(
    task: Mapping[str, object],
    attempt: Mapping[str, object],
) -> ReviewOperation:
    """Create current typed authority from one explicit provider attempt."""

    current = dict(_plain(dict(attempt)))
    lifecycle = str(current.get("state") or "blocked")
    current.update({"kind": "provider_selection", "lifecycle": lifecycle})
    try:
        revision = int(current.get("expected_decision_revision"))
    except (TypeError, ValueError):
        revision = 0
    reasons = tuple(str(item) for item in current.get("reason_codes", ()) or ())
    if lifecycle == "pending":
        return _new_review_operation(
            state=TaskWorkflowState.BLOCKED,
            revision=revision,
            context=current,
            blocking_reasons=("selection_pending",),
            explanation_codes=reasons,
        )
    if lifecycle == 'awaiting_season':
        from .ova_selection import pending_subject
        try:
            pending_subject(task, current)
        except (ValueError, TypeError, KeyError):
            return _blocked_operation(revision, ('review_operation_invalid',), context=current)
        return _blocked_operation(revision, ('manual_season_required',), context=current)
    if lifecycle == "interrupted":
        return _new_review_operation(
            state=TaskWorkflowState.BLOCKED,
            revision=revision,
            context=current,
            blocking_reasons=reasons or ("app_restarted",),
            explanation_codes=reasons or ("app_restarted",),
        )
    if lifecycle not in {"failed", "awaiting_user"}:
        return _new_review_operation(
            state=TaskWorkflowState.RESOLVED,
            revision=revision,
            context=current,
            explanation_codes=reasons,
        )
    return _blocked_operation(revision, reasons or ("manual_season_required",), context=current)


def current_provider_selection_review_operation(
    task: Mapping[str, object],
) -> tuple[ReviewOperation, dict[str, object]]:
    operation, _unused = build_review_operation(task)
    context = dict(_plain(dict(operation.context)))
    if context.get("kind") != "provider_selection":
        raise ValueError("current provider selection authority is unavailable")
    return operation, context


def transition_provider_selection_review_operation(
    task: Mapping[str, object],
    changes: Mapping[str, object],
    *,
    expected_fingerprint: Optional[str] = None,
) -> ReviewOperation:
    operation, context = current_provider_selection_review_operation(task)
    if (
        expected_fingerprint is not None
        and operation.fingerprint != expected_fingerprint
    ):
        raise ValueError("review operation changed before transition")
    context.update(dict(_plain(dict(changes))))
    return build_provider_selection_review_operation(task, context)


def finish_provider_selection(task: dict, *, expected: ReviewOperation,
                              result_revision: int, result_fingerprint: str) -> None:
    """Finish only the captured typed request; unrelated operations are never cleared."""
    if type(expected) is not ReviewOperation:
        raise ValueError('作品选择缺少原始请求')
    active, context = current_provider_selection_review_operation(task)
    if (active.fingerprint != expected.fingerprint
            or context.get('lifecycle') not in {'failed', 'awaiting_user'}):
        raise ValueError('作品选择已被后来操作替换')
    set_review_operation(task, transition_provider_selection_review_operation(
        task, dict(state='succeeded', result_decision_revision=result_revision,
                   result_plan_fingerprint=result_fingerprint, failure_code=None,
                   reason_codes=[], retryable=False, boundary_resolution_failure=None),
        expected_fingerprint=expected.fingerprint))
    task.pop('provider_selection_failure', None)


def _blocked_operation(
    revision: int,
    reasons: tuple[str, ...],
    *,
    context: Optional[Mapping[str, object]] = None,
    preview: Optional[PreviewModel] = None,
) -> ReviewOperation:
    normalized = tuple(dict.fromkeys(reasons or ("review_blocked",)))
    return _new_review_operation(
        state=TaskWorkflowState.BLOCKED,
        revision=revision,
        context=context or {"kind": "blocked", "lifecycle": "blocked"},
        preview=preview,
        blocking_reasons=normalized,
        explanation_codes=normalized,
    )


def set_review_operation(
    task: dict,
    operation: ReviewOperation,
) -> dict:
    """Persist one validated current authority and retire its legacy mirror."""

    if not isinstance(operation, ReviewOperation):
        raise TypeError("current review operation must be typed")
    validated = ReviewOperation.from_mapping(operation.to_dict())
    task.pop("selection_operation", None)
    task.update({"review_operation": validated.to_dict()})
    return task


def normalize_task_review_authority(
    payload: Mapping[str, object],
    previous: Mapping[str, object],
) -> dict[str, object]:
    """Preserve current authority; retain legacy-only records as stale input."""

    normalized = dict(_plain(dict(payload)))
    current = normalized.get("review_operation")
    if not isinstance(current, Mapping):
        previous_current = previous.get("review_operation")
        if isinstance(previous_current, Mapping):
            normalized["review_operation"] = dict(_plain(dict(previous_current)))
            current = normalized["review_operation"]
    if isinstance(current, Mapping):
        normalized.pop("selection_operation", None)
        return normalized
    if "selection_operation" not in normalized:
        legacy = previous.get("selection_operation")
        if isinstance(legacy, Mapping):
            normalized["selection_operation"] = dict(_plain(dict(legacy)))
    return normalized
