"""Fail-closed JSON persistence for immutable rename plans."""

from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from pathlib import Path

from .rename_plan import RenamePlan


_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class PlanStoreError(RuntimeError):
    """A persisted plan could not be saved or trusted."""


class PlanStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, task_id: str) -> Path:
        value = str(task_id)
        if (
            not value
            or value in {".", ".."}
            or _SAFE_TASK_ID.fullmatch(value) is None
        ):
            raise PlanStoreError(f"Unsafe task_id for Plan storage: {value!r}")
        return self.root / f"{value}.json"

    def save(self, plan: RenamePlan) -> Path:
        if plan.migrated_from_schema is not None:
            raise PlanStoreError(
                "Legacy Plan is read-only; generate a current projection"
            )
        if plan.projection_receipt is None:
            raise PlanStoreError(
                "Current Plan requires a typed ProjectionReceipt"
            )
        try:
            from .projection_authority import validate_plan_projection_receipt

            validate_plan_projection_receipt(plan)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise PlanStoreError(f"ProjectionReceipt is not trusted: {exc}") from exc
        destination = self.path_for(plan.task_id)
        self.root.mkdir(parents=True, exist_ok=True)
        fingerprint = self.fingerprint(plan)
        history_path = self._history_path(plan.task_id, fingerprint)
        try:
            if not history_path.exists():
                self._atomic_write(history_path, plan.to_dict())
            self._atomic_write(destination, plan.to_dict())
        except (OSError, TypeError, ValueError) as exc:
            raise PlanStoreError(
                f"Failed to persist Plan for task {plan.task_id}: {exc}"
            ) from exc
        return destination

    def delete_current(self, task_id: str) -> None:
        """Remove only the mutable current pointer, preserving history/audit."""

        self.path_for(task_id).unlink(missing_ok=True)

    def restore_current(self, plan: RenamePlan) -> None:
        """Restore a previously trusted current pointer after revision failure."""

        if plan.projection_receipt is None:
            raise PlanStoreError("Cannot restore an unreceipted current Plan")
        destination = self.path_for(plan.task_id)
        self._atomic_write(destination, plan.to_dict())

    def load(self, task_id: str) -> RenamePlan:
        path = self.path_for(task_id)
        return self._load_path(path, str(task_id))

    def load_version(self, task_id: str, fingerprint: str) -> RenamePlan:
        path = self._history_path(task_id, fingerprint)
        payload = self._read_payload(path, str(task_id))
        # Trust is established against the original persisted bytes-as-JSON
        # contract before any in-memory schema migration changes the payload.
        if self.fingerprint_payload(payload) != fingerprint:
            raise PlanStoreError("Historical Plan fingerprint mismatch")
        return self._plan_from_payload(payload, str(task_id))

    def delete(self, task_id: str) -> None:
        """Delete only the current Plan and exact task history."""

        self.path_for(task_id).unlink(missing_ok=True)
        history_dir = self.root / "history" / str(task_id)
        if history_dir.exists():
            for item in history_dir.iterdir():
                if item.is_file() and re.fullmatch(
                    r"[0-9a-f]{64}\.json", item.name
                ):
                    item.unlink()
            history_dir.rmdir()
        history_root = self.root / "history"
        if history_root.exists():
            try:
                history_root.rmdir()
            except OSError:
                pass

    @staticmethod
    def fingerprint(plan: RenamePlan) -> str:
        return PlanStore.fingerprint_payload(plan.to_dict())

    @staticmethod
    def fingerprint_payload(payload: dict) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _history_path(self, task_id: str, fingerprint: str) -> Path:
        self.path_for(task_id)
        if re.fullmatch(r"[0-9a-f]{64}", str(fingerprint)) is None:
            raise PlanStoreError("Invalid Plan fingerprint")
        return self.root / "history" / str(task_id) / f"{fingerprint}.json"

    def _load_path(self, path: Path, expected_task_id: str) -> RenamePlan:
        payload = self._read_payload(path, expected_task_id)
        return self._plan_from_payload(payload, expected_task_id)

    def _read_payload(self, path: Path, expected_task_id: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as file:
                payload = json.load(file)
            if not isinstance(payload, dict):
                raise ValueError("Plan root must be an object")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PlanStoreError(
                f"Failed to load trusted Plan for task {expected_task_id}: {exc}"
            ) from exc
        return payload

    def _plan_from_payload(self, payload: dict, expected_task_id: str) -> RenamePlan:
        try:
            plan = RenamePlan.from_dict(payload)
        except (TypeError, ValueError) as exc:
            raise PlanStoreError(
                f"Failed to load trusted Plan for task {expected_task_id}: {exc}"
            ) from exc
        if plan.task_id != expected_task_id:
            raise PlanStoreError(
                "Plan task_id mismatch: requested "
                f"{expected_task_id}, got {plan.task_id}"
            )
        return plan

    def _atomic_write(self, destination: Path, payload: dict) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / (
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(temporary, "x", encoding="utf-8") as file:
                json.dump(payload, file, indent=2, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
