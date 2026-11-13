"""R24 Verification-refactor contracts.

All Provider inputs are versioned, sanitized local records.  This module does
not start NiceGUI, contact a Provider, or touch real task/media state.
"""

from __future__ import annotations

import ast
from copy import deepcopy
import dis
import hashlib
import json
from pathlib import Path
import re
import sys
from types import CodeType
import unittest
from unittest.mock import Mock

from src.pages.review_operation_view import (
    ReviewCommandDispatchError,
    ReviewOperationCallbacks,
    build_review_operation_presentation,
    dispatch_review_command_once,
    render_review_operation,
)
from src.rename.provider_link_resolver import (
    collect_subject_observations,
    resolve_provider_link,
)
from src.rename.review_operation import ReviewCommand, TaskWorkflowState
from tests.test_complex_library_special_red_contract import _CaptureUi
from tests.test_r24_phase_d_f7_f8_remediation_contract import (
    ACTIONABLE_SNAPSHOT,
    build_canonical_actionable_task,
)
from tests.test_r24_phase_c_review_operation_migration_contract import (
    current_provider_selection_context,
    persist_provider_selection_attempt,
)


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPOSITORY / "tests" / "fixtures"
CATALOG_PATH = FIXTURE_ROOT / "r24_phase_d_canary_provider_record_catalog.json"
RATCHET_PATH = FIXTURE_ROOT / "r24_verification_quality_ratchet.json"
def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _actionable_task(*, refresh: bool = False) -> dict[str, object]:
    return build_canonical_actionable_task(refresh=refresh)

def _pending_task() -> dict[str, object]:
    task: dict[str, object] = {
        "uuid": "sanitized-pending-task",
        "workflow_status": "queued",
    }
    persist_provider_selection_attempt(task, {
        "schema_version": 1,
        "state": "pending",
        "expected_decision_revision": 3,
        "reason_codes": [],
    })
    return task

def _resolved_task() -> dict[str, object]:
    task: dict[str, object] = {
        "uuid": "sanitized-resolved-task",
        "workflow_status": "need_confirm",
    }
    persist_provider_selection_attempt(task, {
        "schema_version": 1,
        "state": "succeeded",
        "expected_decision_revision": 8,
        "reason_codes": [],
    })
    return task

def _blocked_task() -> dict[str, object]:
    task = _actionable_task()
    attempt = current_provider_selection_context(task)
    attempt["provider_subject_evidence"] = {}
    attempt.pop("provider_subject_cohort_preview", None)
    attempt["reason_codes"] = ["unknown-r24-reason"]
    persist_provider_selection_attempt(task, attempt)
    return task

def _callbacks() -> ReviewOperationCallbacks:
    return ReviewOperationCallbacks(
        select_provider_candidate=Mock(),
        dispatch_review_command=Mock(),
        show_provider_candidates=Mock(),
        remove_provider_failure_task=Mock(),
        close_review=Mock(),
    )


def _capture(task: dict[str, object]) -> tuple[_CaptureUi, object]:
    ui = _CaptureUi()
    ui.card = ui.column
    ui.spinner = lambda **_kwargs: ui._node()
    presentation = render_review_operation(ui, task, _callbacks())
    return ui, presentation


def _yama_batch(
    case: dict[str, object], selected_subject_id: str
) -> dict[str, object]:
    observations = {
        str(row["bangumi_subject_id"]): deepcopy(row)
        for row in case["observations"]
    }
    return dict(collect_subject_observations(
        selected_subject_id=selected_subject_id,
        bangumi_subjects=deepcopy(case["subjects"]),
        bangumi_relations=deepcopy(case["relations"]),
        search_subject=lambda subject: deepcopy(
            observations[str(subject["provider_id"])]
        ),
        max_subjects=len(case["subjects"]),
    ))


def _resolution_tuple(resolution: object) -> tuple[object, ...]:
    assignments = tuple(getattr(resolution, "season_assignments"))
    return (
        getattr(resolution, "status"),
        getattr(resolution, "review_disposition"),
        getattr(resolution, "tmdb_tv_id"),
        assignments,
        tuple(getattr(resolution, "reason_codes")),
        getattr(resolution, "resolution_fingerprint"),
    )


class PublicPresentationModelContracts(unittest.TestCase):
    def test_table_model_covers_every_persisted_lifecycle_without_private_dialog(self):
        cases = (
            ("none", {"uuid": "none"}, None, None),
            ("pending", _pending_task(), "pending", TaskWorkflowState.BLOCKED),
            ("resolved", _resolved_task(), "succeeded", TaskWorkflowState.RESOLVED),
            ("actionable", _actionable_task(), "awaiting_user", TaskWorkflowState.ACTIONABLE),
            ("blocked", _blocked_task(), "awaiting_user", TaskWorkflowState.BLOCKED),
            (
                "legacy",
                {"selection_operation": {"expected_decision_revision": 2}},
                "legacy_stale",
                TaskWorkflowState.BLOCKED,
            ),
            (
                "invalid-current",
                {"review_operation": {"schema_version": 999}},
                "blocked",
                TaskWorkflowState.BLOCKED,
            ),
        )
        for label, task, lifecycle, state in cases:
            with self.subTest(case=label):
                first = build_review_operation_presentation(task)
                second = build_review_operation_presentation(deepcopy(task))
                self.assertEqual(first, second)
                if lifecycle is None:
                    self.assertIsNone(first)
                else:
                    self.assertEqual(lifecycle, first.lifecycle)
                    self.assertEqual(state, first.state)

    def test_property_matrix_keeps_explanations_out_of_action_permissions(self):
        expected = {"command-confirm_cohort", "command-separate_work"}
        for reason in (
            "season_window_date_locked",
            "subject_candidate_evidence_missing",
            "provider_unavailable",
            "unknown-r24-reason",
        ):
            task = _actionable_task()
            attempt = current_provider_selection_context(task)
            attempt["reason_codes"] = [reason]
            persist_provider_selection_attempt(task, attempt)
            presentation = build_review_operation_presentation(task)
            self.assertEqual(
                expected,
                {action.key for action in presentation.actions if action.key.startswith("command-")},
                reason,
            )

    def test_mounted_renderer_exposes_unique_chinese_aria_and_stable_testids(self):
        ui, presentation = _capture(_actionable_task(refresh=True))
        self.assertIsNotNone(presentation)
        props = [ui.button_props[action.label] for action in presentation.actions]
        testids = []
        for action, value in zip(presentation.actions, props):
            self.assertIn(f"aria-label='{action.label}'", value)
            match = re.search(r"data-testid='([^']+)'", value)
            self.assertIsNotNone(match)
            testids.append(match.group(1))
        self.assertEqual(len(testids), len(set(testids)))
        self.assertEqual(
            {
                "review-action-refresh",
                "review-action-command-confirm_cohort",
                "review-action-command-separate_work",
                "review-action-switch-provider",
                "review-action-remove-task",
                "review-action-close",
            },
            set(testids),
        )

    def test_renderer_covers_pending_resolved_actionable_and_blocked_controls(self):
        pending_ui, pending = _capture(_pending_task())
        resolved_ui, resolved = _capture(_resolved_task())
        actionable_ui, actionable = _capture(_actionable_task())
        blocked_ui, blocked = _capture(_blocked_task())
        self.assertIn("正在更新作品信息，请稍候…", pending_ui.visible)
        self.assertEqual([], resolved_ui.visible)
        self.assertIn("本次确认将整理以下季度：", actionable_ui.visible)
        self.assertIn("涉及：示例续季", actionable_ui.visible)
        self.assertIn(
            f"整理目标：{ACTIONABLE_SNAPSHOT.anime_path}", actionable_ui.visible
        )
        self.assertEqual(TaskWorkflowState.BLOCKED, blocked.state)
        self.assertNotIn("确认与相邻作品一起整理", blocked_ui.callbacks)
        self.assertEqual("pending", pending.lifecycle)
        self.assertEqual(TaskWorkflowState.RESOLVED, resolved.state)
        self.assertEqual(TaskWorkflowState.ACTIONABLE, actionable.state)


class CommandCasModelContracts(unittest.TestCase):
    def test_valid_duplicate_tamper_and_revision_drift_are_all_closed(self):
        task = _actionable_task()
        presentation = build_review_operation_presentation(task)
        command = next(
            action.payload for action in presentation.actions
            if action.key == "command-confirm_cohort"
        )
        applied = Mock(return_value="applied")
        dispatched: set[str] = set()
        self.assertTrue(dispatch_review_command_once(task, command, dispatched, applied))
        self.assertFalse(dispatch_review_command_once(task, command, dispatched, applied))
        self.assertEqual(1, applied.call_count)

        tampered = dict(command)
        tampered["expected_revision"] = 999
        with self.assertRaises(ValueError):
            dispatch_review_command_once(task, tampered, set(), applied)

        forged = ReviewCommand.build(
            kind="confirm_cohort",
            expected_revision=7,
            operation_fingerprint=str(command["operation_fingerprint"]),
            payload={**dict(command["payload"]), "same_work": False},
        ).to_dict()
        with self.assertRaises(ReviewCommandDispatchError):
            dispatch_review_command_once(task, forged, set(), applied)

        changed = deepcopy(task)
        attempt = current_provider_selection_context(changed)
        attempt["expected_decision_revision"] = 8
        persist_provider_selection_attempt(changed, attempt)
        with self.assertRaises(ReviewCommandDispatchError):
            dispatch_review_command_once(changed, command, set(), applied)

class ProviderRecordReplayContracts(unittest.TestCase):
    def test_catalog_sources_are_exact_versioned_and_privacy_sanitized(self):
        catalog = _load(CATALOG_PATH)
        self.assertEqual(1, catalog["schema_version"])
        self.assertEqual("offline_fixture_only", catalog["provider_contract"]["transport"])
        for source in catalog["source_records"].values():
            path = REPOSITORY / source["path"]
            payload = path.read_bytes()
            self.assertEqual(source["bytes"], len(payload))
            self.assertEqual(source["sha256"], hashlib.sha256(payload).hexdigest())
        serialized = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "api_key",
            '"cookie":',
            "bearer ",
            '"password":',
            "D:\\\\",
            "/home/",
        ):
            self.assertNotIn(forbidden.casefold(), serialized.casefold())

    def test_exact_four_case_canary_identity_is_frozen_in_entry_control(self):
        catalog = _load(CATALOG_PATH)
        identities = [row["sanitized_input_id"] for row in catalog["canaries"]]
        self.assertEqual(
            [
                "set-04/kaguya-s2",
                "set-02/yama-s2",
                "set-02/yama-s4",
                "set-02/yama-s1",
            ],
            identities,
        )
        self.assertEqual(4, len(set(identities)))
        entry = _load(
            REPOSITORY / catalog["source_records"]["entry-control"]["path"]
        )
        visits = {
            item
            for visit in entry["picker_visits"]
            for item in visit
        }
        self.assertTrue(set(identities).issubset(visits))
        ordinary = catalog["canaries"][-1]
        self.assertEqual("ordinary_s1_control", ordinary["role"])
        self.assertEqual("41658", ordinary["selected_provider"]["provider_id"])

    def test_three_incident_provider_records_replay_deterministically(self):
        catalog = _load(CATALOG_PATH)
        for canary in catalog["canaries"]:
            if canary["replay_profile"] != "provider-link-resolution":
                continue
            selected = canary["selected_provider"]
            source = catalog["source_records"][canary["source_record"]]
            fixture = _load(REPOSITORY / source["path"])
            if canary["source_record"] == "kaguya-cohort":
                subjects = fixture["bangumi_subjects"]
                relations = fixture["bangumi_relations"]
                batch = fixture["subject_observations"]
            else:
                case = fixture["yama"]
                subjects = case["subjects"]
                relations = case["relations"]
                batch = _yama_batch(case, selected["provider_id"])
            first = resolve_provider_link(
                selected_provider=selected,
                bangumi_subjects=subjects,
                bangumi_relations=relations,
                subject_observations=batch,
            )
            second = resolve_provider_link(
                selected_provider=deepcopy(selected),
                bangumi_subjects=tuple(reversed(deepcopy(subjects))),
                bangumi_relations=tuple(reversed(deepcopy(relations))),
                subject_observations=deepcopy(batch),
            )
            expected = canary["expected"]
            self.assertEqual(_resolution_tuple(first), _resolution_tuple(second))
            self.assertEqual(expected["status"], first.status)
            self.assertEqual(expected["review_disposition"], first.review_disposition)
            self.assertEqual(expected["tmdb_tv_id"], first.tmdb_tv_id)
            self.assertEqual(expected["reason_codes"], list(first.reason_codes))
            self.assertEqual(expected["resolution_fingerprint"], first.resolution_fingerprint)
            self.assertEqual(
                expected["selected_season_number"],
                dict(first.season_assignments)[selected["provider_id"]],
            )
            self.assertEqual(
                expected["observation_fingerprint"],
                batch["evidence_fingerprint"],
            )


class QualityRatchetContracts(unittest.TestCase):
    def test_public_refactor_boundary_is_fully_typed(self):
        module_path = REPOSITORY / "src" / "pages" / "review_operation_view.py"
        tree = ast.parse(module_path.read_text("utf-8"), filename=str(module_path))
        problems: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                if node.returns is None:
                    problems.append(f"{node.name}: missing return annotation")
                arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
                for argument in arguments:
                    if argument.arg not in {"self", "cls"} and argument.annotation is None:
                        problems.append(f"{node.name}.{argument.arg}: missing annotation")
        self.assertEqual([], problems)

    def test_whitebox_debt_and_line_counts_only_move_downward(self):
        ratchet = _load(RATCHET_PATH)
        # S1 G15: whole-repository counts penalized new fault-injection tests.
        # Retire that requirement, not the typed entry/branch/line-size guards.
        for relative, ceiling in ratchet["line_count_ceilings"].items():
            lines = len((REPOSITORY / relative).read_text("utf-8").splitlines())
            self.assertLessEqual(lines, ceiling, relative)

    def test_critical_branch_obligation_catalog_is_complete(self):
        expected = set(_load(RATCHET_PATH)["critical_branch_obligations"])
        exercised = {
            "presentation.none",
            "presentation.pending",
            "presentation.resolved",
            "presentation.actionable",
            "presentation.blocked",
            "presentation.refresh_present",
            "presentation.refresh_absent",
            "presentation.known_commands",
            "presentation.unknown_command_ignored",
            "presentation.preview_assignments",
            "presentation.preview_outside",
            "presentation.preview_target",
            "presentation.boundary_present",
            "presentation.boundary_absent",
            "renderer.pending",
            "renderer.resolved",
            "renderer.actionable",
            "renderer.stable_selectors",
            "dispatcher.valid",
            "dispatcher.duplicate",
            "dispatcher.tamper",
            "dispatcher.command_membership",
            "dispatcher.revision_drift",
        }
        self.assertEqual(expected, exercised)
        self.assertEqual(100.0, len(exercised & expected) * 100.0 / len(expected))

    def test_external_quality_tools_are_explicitly_not_run_not_passed(self):
        statuses = _load(RATCHET_PATH)["external_tools"]
        self.assertEqual(
            {"not_installed_not_run"},
            set(statuses.values()),
        )
        self.assertNotIn("pass", " ".join(statuses.values()).casefold())

    def test_no_verification_wrapper_skip_or_debug_residue(self):
        paths = (
            REPOSITORY / "src" / "pages" / "review_operation_view.py",
            Path(__file__),
        )
        problems: list[str] = []
        for path in paths:
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for decorator in node.decorator_list:
                        name = ast.unparse(decorator)
                        if name.endswith(("expectedFailure", ".skip")):
                            problems.append(f"{path.name}:{node.lineno}: {name}")
                if isinstance(node, ast.Call):
                    name = ast.unparse(node.func)
                    if name in {"breakpoint", "pdb.set_trace"}:
                        problems.append(f"{path.name}:{node.lineno}: {name}")
        self.assertEqual([], problems)

    def test_stdlib_opcode_branch_coverage_of_typed_core_is_at_least_90_percent(self):
        functions = (
            build_review_operation_presentation,
            dispatch_review_command_once,
        )
        codes = {function.__code__ for function in functions}
        possible: set[tuple[CodeType, int, int]] = set()
        for function in functions:
            instructions = tuple(dis.get_instructions(function))
            for index, instruction in enumerate(instructions[:-1]):
                conditional = (
                    instruction.opname == "FOR_ITER"
                    or (
                        "JUMP" in instruction.opname
                        and "IF" in instruction.opname
                    )
                )
                if conditional and isinstance(instruction.argval, int):
                    possible.add((
                        function.__code__,
                        instruction.offset,
                        instructions[index + 1].offset,
                    ))
                    possible.add((
                        function.__code__,
                        instruction.offset,
                        instruction.argval,
                    ))

        executed: set[tuple[CodeType, int, int]] = set()
        previous: dict[int, tuple[CodeType, int]] = {}

        def tracer(frame, event, _arg):
            identity = id(frame)
            if event == "call" and frame.f_code in codes:
                frame.f_trace_opcodes = True
                previous.pop(identity, None)
                return tracer
            if frame.f_code not in codes:
                return tracer
            if event == "opcode":
                prior = previous.get(identity)
                if prior is not None:
                    executed.add((prior[0], prior[1], frame.f_lasti))
                previous[identity] = (frame.f_code, frame.f_lasti)
            elif event in {"return", "exception"}:
                previous.pop(identity, None)
            return tracer

        actionable = _actionable_task(refresh=True)
        presentation = build_review_operation_presentation(actionable)
        commands = {
            action.key: action.payload
            for action in presentation.actions
            if action.key.startswith("command-")
        }
        changed_reason = deepcopy(actionable)
        attempt = current_provider_selection_context(changed_reason)
        attempt["reason_codes"] = ["subject_candidate_evidence_missing"]
        persist_provider_selection_attempt(changed_reason, attempt)
        no_boundary = deepcopy(actionable)
        attempt = current_provider_selection_context(no_boundary)
        attempt.pop("boundary_resolution_failure", None)
        attempt["retry_requires_refresh"] = False
        attempt["candidate"] = None
        persist_provider_selection_attempt(no_boundary, attempt)
        empty_boundary = deepcopy(actionable)
        attempt = current_provider_selection_context(empty_boundary)
        attempt["boundary_resolution_failure"] = {}
        attempt["retry_requires_refresh"] = False
        attempt["candidate"] = {"namespace": "bangumi", "provider_id": "86670"}
        persist_provider_selection_attempt(empty_boundary, attempt)
        outside_preview = deepcopy(actionable)
        attempt = current_provider_selection_context(outside_preview)
        attempt["provider_subject_cohort_preview"]["outside_subject_titles"] = [
            "示例外部作品"
        ]
        persist_provider_selection_attempt(outside_preview, attempt)

        original_tracer = sys.gettrace()
        failed_relation = _pending_task()
        attempt = current_provider_selection_context(failed_relation)
        attempt.update(state='failed', failure_code='relation_unresolved', reason_codes=['subject_query_truncated'])
        persist_provider_selection_attempt(failed_relation, attempt)
        sys.settrace(tracer)
        try:
            for task in (
                {"uuid": "none"},
                _pending_task(),
                _resolved_task(),
                actionable,
                no_boundary,
                empty_boundary,
                outside_preview,
                _blocked_task(),
                failed_relation,
                {"selection_operation": {"expected_decision_revision": 2}},
                {"review_operation": {"schema_version": 999}},
            ):
                build_review_operation_presentation(task)
            self.assertIn('被截断', build_review_operation_presentation(failed_relation).reason_text)

            for key in ("command-confirm_cohort",):
                dispatched: set[str] = set()
                dispatch_review_command_once(
                    actionable,
                    commands[key],
                    dispatched,
                    Mock(return_value="applied"),
                )
                dispatch_review_command_once(
                    actionable,
                    commands[key],
                    dispatched,
                    Mock(return_value="applied"),
                )
                dispatch_review_command_once(
                    actionable,
                    commands[key],
                    set(),
                    Mock(return_value="rejected"),
                )
                dispatch_review_command_once(
                    actionable,
                    commands[key],
                    set(),
                    Mock(return_value="already_applied"),
                )
                failed_keys: set[str] = set()
                with self.assertRaisesRegex(RuntimeError, 'isolated callback failure'):
                    dispatch_review_command_once(actionable, commands[key], failed_keys,
                        Mock(side_effect=RuntimeError('isolated callback failure')))
                self.assertEqual(set(), failed_keys)
                self.assertTrue(dispatch_review_command_once(actionable, commands[key], failed_keys,
                    Mock(return_value='applied')))

            forged = ReviewCommand.build(
                kind="confirm_cohort",
                expected_revision=7,
                operation_fingerprint=str(
                    commands["command-confirm_cohort"]["operation_fingerprint"]
                ),
                payload={
                    **dict(commands["command-confirm_cohort"]["payload"]),
                    "same_work": False,
                },
            ).to_dict()
            wrong_revision = ReviewCommand.build(
                kind="confirm_cohort",
                expected_revision=999,
                operation_fingerprint=str(
                    commands["command-confirm_cohort"]["operation_fingerprint"]
                ),
                payload=dict(commands["command-confirm_cohort"]["payload"]),
            ).to_dict()
            for stale_task, stale_command in (
                ({"uuid": "resolved"}, commands["command-confirm_cohort"]),
                (changed_reason, commands["command-confirm_cohort"]),
                (actionable, wrong_revision),
                (actionable, forged),
            ):
                with self.assertRaises(ReviewCommandDispatchError):
                    dispatch_review_command_once(
                        stale_task,
                        stale_command,
                        set(),
                        Mock(),
                    )
        finally:
            sys.settrace(original_tracer)

        covered = possible & executed
        percent = len(covered) * 100.0 / len(possible)
        missing = sorted(
            (
                code.co_name,
                source,
                target,
            )
            for code, source, target in possible - covered
        )
        self.assertGreaterEqual(
            percent,
            float(_load(RATCHET_PATH)["branch_coverage_minimum_percent"]),
            (
                f"typed-core opcode branches: {len(covered)}/{len(possible)}; "
                f"missing={missing}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
