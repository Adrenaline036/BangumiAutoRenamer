import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from ..config.config_manager import cm
from ..logger import logger
from ..utils.path import RECORD_PATH
from ..utils.utils import safe_task_path
from .asset_collision import AssetCollisionError, validate_reuse_resolution
from .rename_plan import CollisionResolution


def is_dry_run_enabled() -> bool:
    return os.getenv("BAR_DRY_RUN", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


class Trans:
    """Execute a verified mapping with a recoverable per-item ledger."""

    def __init__(
        self,
        R: Dict[Path, Path],
        uuid: str,
        plan_fingerprint: str | None = None,
        *,
        mode: str | None = None,
        dry_run: bool | None = None,
        record_root: Path | None = None,
        operations: Sequence[Mapping[str, object]] | None = None,
    ) -> None:
        requested_mode = mode or cm.get_config("mode")
        self.mode = "链接" if requested_mode == "hardlink" else requested_mode
        self.policy_mode = {
            "链接": "hardlink", "复制": "copy", "剪切": "move"
        }.get(self.mode, str(requested_mode))
        self.dry_run = is_dry_run_enabled() if dry_run is None else dry_run
        self.record_root = Path(record_root) if record_root is not None else RECORD_PATH
        self.R = R
        self.uuid = uuid
        self.plan_fingerprint = plan_fingerprint
        self.operations = tuple(operations or (
            {
                "source": source,
                "target": target,
                "action": "create",
                "collision_resolution": None,
            }
            for source, target in R.items()
        ))

    def trans_file(self):
        record_path = safe_task_path(self.record_root, str(self.uuid))
        ledger_path = safe_task_path(
            self.record_root, str(self.uuid), suffix=".ledger.json"
        )
        mapping = {
            str(item["source"]): str(item["target"])
            for item in self.operations
        }
        self._atomic_json_write(record_path, mapping)

        ledger = {
            "schema_version": (
                2 if any(item.get("action") == "reuse_existing" for item in self.operations)
                else 1
            ),
            "task_id": str(self.uuid),
            "plan_fingerprint": self.plan_fingerprint,
            "mode": self.policy_mode,
            "dry_run": self.dry_run,
            "status": "planned",
            "entries": [
                {
                    "source": str(source),
                    "target": str(target),
                    "status": "planned",
                    "transfer_type": None,
                    "error": None,
                    "source_device": None,
                    "source_inode": None,
                    "target_device": None,
                    "target_inode": None,
                    "target_link_count": None,
                }
                for source, target in (
                    (Path(str(item["source"])), Path(str(item["target"])))
                    for item in self.operations
                )
            ],
        }
        for entry, operation in zip(ledger["entries"], self.operations):
            entry["action"] = str(operation.get("action") or "create")
            raw_authority = operation.get("conditional_authority")
            entry["conditional_authority"] = (
                dict(raw_authority) if isinstance(raw_authority, Mapping) else None
            )
            entry["source_size"] = None
            entry["source_sha256"] = None
            entry["target_size"] = None
            entry["target_sha256"] = None
        self._atomic_json_write(ledger_path, ledger)

        if not self.operations:
            message = "映射为空，拒绝执行"
            ledger["status"] = "failed"
            self._atomic_json_write(ledger_path, ledger)
            logger.error(f"[处理迁移] {message}")
            return message

        if ledger["dry_run"]:
            ledger["status"] = "dry_run"
            self._atomic_json_write(ledger_path, ledger)
            logger.warning(
                f"[映射预检] BAR_DRY_RUN 已启用；仅记录 {len(self.operations)} 条映射，"
                "不会创建、复制、移动或链接任何媒体文件"
            )
            for operation in self.operations:
                source_path = operation["source"]
                target_path = operation["target"]
                logger.info(f"[映射预检] {source_path} -> {target_path}")
            return None

        if self.mode not in {"剪切", "复制", "链接"}:
            message = "模式错误！仅支持剪切, 复制, 链接"
            ledger["status"] = "failed"
            if ledger["entries"]:
                ledger["entries"][0]["status"] = "failed"
                ledger["entries"][0]["error"] = message
            self._atomic_json_write(ledger_path, ledger)
            logger.error(f"[处理迁移] {message}")
            return message

        preflight_error = self._preflight(ledger)
        if preflight_error:
            ledger["status"] = "failed"
            self._atomic_json_write(ledger_path, ledger)
            logger.error(f"[处理迁移] {preflight_error}")
            return preflight_error

        created_indices: List[int] = []
        created_directories: List[Path] = []
        items = [
            (Path(str(item["source"])), Path(str(item["target"])))
            for item in self.operations
        ]
        for index, (source_path, target_path) in enumerate(items):
            entry = ledger["entries"][index]
            operation = self.operations[index]
            try:
                if operation.get("action") == "reuse_existing":
                    raw_resolution = operation.get("collision_resolution")
                    if not isinstance(raw_resolution, Mapping):
                        raise AssetCollisionError("既有目标复用缺少批准证据")
                    resolution = CollisionResolution.from_dict(raw_resolution)
                    source_evidence, target_evidence = validate_reuse_resolution(
                        source_path, target_path, resolution
                    )
                    entry.update({
                        "status": "reused_identical_asset",
                        "transfer_type": "reuse_existing",
                        "source_device": source_evidence.device,
                        "source_inode": source_evidence.inode,
                        "source_size": source_evidence.size,
                        "source_sha256": source_evidence.sha256,
                        "target_device": target_evidence.device,
                        "target_inode": target_evidence.inode,
                        "target_link_count": target_path.stat().st_nlink,
                        "target_size": target_evidence.size,
                        "target_sha256": target_evidence.sha256,
                    })
                    self._atomic_json_write(ledger_path, ledger)
                    continue
                self._ensure_parent(target_path.parent, created_directories)
                source_stat = source_path.stat()
                entry["source_device"] = source_stat.st_dev
                entry["source_inode"] = source_stat.st_ino

                if self.mode == "剪切":
                    shutil.move(str(source_path), str(target_path))
                    transfer_type = "move"
                elif self.mode == "复制":
                    shutil.copy2(source_path, target_path)
                    transfer_type = "copy"
                else:
                    os.link(source_path, target_path)
                    transfer_type = "hardlink"

                entry["transfer_type"] = transfer_type
                created_indices.append(index)
                self._verify_result(source_path, target_path, transfer_type, entry)
                entry["status"] = "created"
                self._atomic_json_write(ledger_path, ledger)
            except Exception as exc:
                entry["status"] = "failed"
                entry["error"] = str(exc)
                rollback_errors = self._rollback(
                    items, ledger, created_indices, created_directories
                )
                ledger["status"] = (
                    "rollback_failed"
                    if rollback_errors
                    else ("rolled_back" if created_indices else "failed")
                )
                if rollback_errors:
                    entry["error"] += "; rollback: " + "; ".join(
                        rollback_errors
                    )
                self._atomic_json_write(ledger_path, ledger)
                logger.error(f"[处理迁移] {entry['error']}")
                return entry["error"]

        ledger["status"] = "completed"
        self._atomic_json_write(ledger_path, ledger)
        return None

    def _preflight(self, ledger: dict):
        seen_targets = set()
        for index, operation in enumerate(self.operations):
            source_path = Path(str(operation["source"]))
            target_path = Path(str(operation["target"]))
            entry = ledger["entries"][index]
            reuse = operation.get("action") == "reuse_existing"
            if not source_path.exists():
                message = f"源路径不存在：{source_path}"
            elif not source_path.is_file():
                message = f"源路径不是普通文件：{source_path}"
            elif os.path.normpath(str(source_path)).casefold() == os.path.normpath(
                str(target_path)
            ).casefold():
                message = f"源路径与目标路径相同：{source_path}"
            elif reuse and not target_path.is_file():
                message = f"批准复用的既有目标不存在或不是普通文件：{target_path}"
            elif not reuse and os.path.lexists(target_path):
                message = f"目标已存在，拒绝覆盖：{target_path}"
            elif (not reuse and
                self.mode == "链接"
                and not source_path.is_dir()
                and source_path.stat().st_dev
                != self._existing_parent_device(target_path.parent)
            ):
                message = (
                    "源和目标不在同一文件系统，无法创建硬链接："
                    f"{source_path} -> {target_path}"
                )
            else:
                target_key = os.path.normpath(str(target_path)).casefold()
                if target_key in seen_targets:
                    message = f"目标路径重复：{target_path}"
                else:
                    seen_targets.add(target_key)
                    continue
            entry["status"] = "failed"
            entry["error"] = message
            return message
        return None

    @staticmethod
    def _existing_parent_device(parent: Path) -> int:
        """Return the device of the nearest existing target ancestor."""

        current = Path(parent)
        while not current.exists():
            if current.parent == current:
                raise OSError(f"目标路径没有可用的父目录：{parent}")
            current = current.parent
        return current.stat().st_dev

    @staticmethod
    def _verify_result(
        source_path: Path,
        target_path: Path,
        transfer_type: str,
        entry: dict,
    ) -> None:
        if target_path.is_symlink():
            raise OSError(f"目标意外成为软链接：{target_path}")
        if not target_path.exists():
            raise OSError(f"目标未创建：{target_path}")
        target_stat = target_path.stat()
        if int(target_stat.st_ino) <= 0:
            raise OSError("target physical identity/inode unavailable")
        entry["target_device"] = target_stat.st_dev
        entry["target_inode"] = target_stat.st_ino
        entry["target_link_count"] = target_stat.st_nlink

        if transfer_type == "hardlink":
            source_stat = source_path.stat()
            if int(source_stat.st_ino) <= 0:
                raise OSError("source physical identity/inode unavailable")
            if (
                source_stat.st_dev != target_stat.st_dev
                or source_stat.st_ino != target_stat.st_ino
                or target_stat.st_nlink < 2
            ):
                raise OSError(
                    "hardlink inode/device/link-count verification failed"
                )
        elif transfer_type == "copy":
            if source_path.stat().st_size != target_stat.st_size:
                raise OSError("copy size verification failed")
        elif transfer_type == "move" and source_path.exists():
            raise OSError("move verification failed: source still exists")

    def _rollback(
        self,
        items,
        ledger: dict,
        created_indices: List[int],
        created_directories: List[Path],
    ) -> List[str]:
        errors = []
        for index in reversed(created_indices):
            source_path, target_path = items[index]
            entry = ledger["entries"][index]
            try:
                if entry["transfer_type"] == "move":
                    if target_path.exists() and not source_path.exists():
                        shutil.move(str(target_path), str(source_path))
                    elif target_path.exists() or not source_path.exists():
                        raise OSError(
                            "move rollback cannot prove the original source state"
                        )
                elif os.path.lexists(target_path):
                    target_path.unlink()
                entry["status"] = "rolled_back"
            except Exception as exc:
                entry["status"] = "rollback_failed"
                errors.append(f"{target_path}: {exc}")
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        return errors

    @staticmethod
    def _ensure_parent(parent: Path, created_directories: List[Path]) -> None:
        missing = []
        current = parent
        while not current.exists():
            missing.append(current)
            current = current.parent
        parent.mkdir(parents=True, exist_ok=True)
        created_directories.extend(reversed(missing))

    @staticmethod
    def _atomic_json_write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as file:
                json.dump(payload, file, indent=2, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
