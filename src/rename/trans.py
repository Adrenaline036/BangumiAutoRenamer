import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Mapping, Sequence

from ..config.config_manager import cm
from ..logger import logger
from ..utils.path import RECORD_PATH
from ..utils.utils import safe_task_path
from .asset_collision import AssetCollisionError, validate_reuse_resolution
from .rename_plan import CollisionResolution

if TYPE_CHECKING:
    from .asset_replacement import ApprovedReplacementBatch


def is_dry_run_enabled() -> bool:
    return os.getenv("BAR_DRY_RUN", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def ensure_parent(parent, created_directories, ledger, *, task_id, plan_fingerprint, save):
    missing = []
    current = parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    from .path_safety import first_reparse_component
    if first_reparse_component(parent) is not None:
        raise ValueError('目标目录包含重解析路径，拒绝创建')
    for directory in reversed(missing):
        directory.mkdir()  # 检查后出现的目录不冒认为本次创建。
        created_directories.append(directory)
        stat = directory.stat()
        ledger['directory_witnesses'].append(dict(path=str(directory),
            dev=stat.st_dev, ino=stat.st_ino, task_id=task_id,
            plan_fingerprint=plan_fingerprint, created=True))
        # 持久化前中断的目录没有凭据，下一次仍不能自动接管。
        save()


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
        replacement_authority: "ApprovedReplacementBatch | None" = None,
        rollback_preservation=None,
        generated_outputs=None,
        source_identities=None,
    ) -> None:
        requested_mode = mode or cm.get_config("mode")
        self.mode = "链接" if requested_mode == "hardlink" else requested_mode
        self.policy_mode = "hardlink" if self.mode == "链接" else str(requested_mode)
        self.dry_run = is_dry_run_enabled() if dry_run is None else dry_run
        self.record_root = Path(record_root) if record_root is not None else RECORD_PATH
        self.R = R
        self.uuid = uuid
        self.plan_fingerprint = plan_fingerprint
        self.source_identities = source_identities
        self.media_unchanged = False
        self.generated_outputs = generated_outputs
        self.generated_result = None
        self.replacement_authority = replacement_authority
        self.rollback_preservation = rollback_preservation
        if rollback_preservation is not None:
            from .asset_replacement import ApprovedRollbackPreservation
            if type(rollback_preservation) is not ApprovedRollbackPreservation:
                raise ValueError('回滚共享保护必须为类型化批准能力')
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
        if self.generated_outputs is not None and not self.dry_run:
            from .generated_outputs import write_outputs
            self.generated_result = write_outputs(self.generated_outputs, task_id=str(self.uuid),
                fingerprint=self.plan_fingerprint, record_root=self.record_root)
        if self.replacement_authority is not None or any(item.get("action") == "replace" for item in self.operations):
            return self._trans_replacements()
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
            "directory_witnesses": [],
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
        if self.generated_result is not None:
            ledger['generated_outputs'] = self.generated_result
        for entry, operation in zip(ledger["entries"], self.operations):
            entry["action"] = str(operation.get("action") or "create")
            raw_authority = operation.get("conditional_authority")
            entry["conditional_authority"] = (
                dict(raw_authority) if isinstance(raw_authority, Mapping) else None
            )
            entry["source_size"] = None
            entry["target_size"] = None
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

        if self.mode != "链接":
            message = "执行仅支持保留来源的硬链接"
            ledger["status"] = "failed"
            if ledger["entries"]:
                ledger["entries"][0]["status"] = "failed"
                ledger["entries"][0]["error"] = message
            self._atomic_json_write(ledger_path, ledger)
            logger.error(f"[处理迁移] {message}")
            return message

        preflight_error = self._preflight(ledger)
        self._atomic_json_write(ledger_path, ledger)
        if preflight_error:
            ledger["status"] = "failed"
            self._atomic_json_write(ledger_path, ledger)
            logger.error(f"[处理迁移] {preflight_error}")
            self.media_unchanged = self.generated_outputs is None
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
                        source_path, target_path, resolution, source_identity=self.source_identities[source_path]
                    )
                    entry.update({
                        "status": "reused_identical_asset",
                        "transfer_type": "reuse_existing",
                        "source_device": source_evidence.device,
                        "source_inode": source_evidence.inode,
                        "source_size": source_evidence.size,
                        "target_device": target_evidence.device,
                        "target_inode": target_evidence.inode,
                        "target_link_count": target_path.stat().st_nlink,
                        "target_size": target_evidence.size,
                        "evidence_kind": resolution.evidence_kind,
                    })
                    if resolution.evidence_kind == 'content-xxh3-128-v1':
                        entry.update(source_xxh3_128=source_evidence.xxh3_128,
                                     target_xxh3_128=target_evidence.xxh3_128)
                    self._atomic_json_write(ledger_path, ledger)
                    continue
                self._ensure_parent(target_path.parent, created_directories, ledger, ledger_path)
                os.link(source_path, target_path, follow_symlinks=False)
                entry["transfer_type"] = "hardlink"
                created_indices.append(index)
                from .media_file_identity import verify_link_target
                observed = verify_link_target(target_path, self.source_identities[source_path])
                entry.update(target_device=observed.st_dev, target_inode=observed.st_ino,
                             target_size=observed.st_size, target_link_count=observed.st_nlink,
                             evidence_kind='hardlink-identity-v1')
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
                self.media_unchanged = (self.generated_outputs is None and not rollback_errors
                                        and not os.path.lexists(target_path))
                return entry["error"]

        ledger["status"] = "completed"
        self._atomic_json_write(ledger_path, ledger)
        return None

    def _preflight(self, ledger: dict):
        from .media_file_identity import media_identity, MediaFileIdentity
        identities = self.source_identities
        self.source_identities = {} if identities is None else dict(identities)
        seen = set()
        for operation, row in zip(self.operations, ledger['entries']):
            source, target = Path(operation['source']), Path(operation['target'])
            try:
                if identities is None:
                    self.source_identities[source] = media_identity(source)
                expected = self.source_identities[source]
                if type(expected) is not MediaFileIdentity:
                    raise ValueError('来源缺少冻结文件身份')
                if source == target or target in seen:
                    raise ValueError('来源与目标相同或目标重复')
                seen.add(target)
                if operation.get('action') != 'reuse_existing' and os.path.lexists(target):
                    raise ValueError(f'目标已存在，拒绝覆盖：{target}')
                if (operation.get('action') != 'reuse_existing'
                        and expected.device != self._existing_parent_device(target.parent)):
                    raise ValueError('源和目标不在同一文件系统，无法创建硬链接')
                row.update(source_device=expected.device, source_inode=expected.inode, source_size=expected.size)
            except (KeyError, ValueError, OSError) as exc:
                row.update(status='failed', error=str(exc))
                return str(exc)
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
                if os.path.lexists(target_path):
                    target_path.unlink()
                entry["status"] = "rolled_back"
            except Exception as exc:
                entry["status"] = "rollback_failed"
                errors.append(f"{target_path}: {exc}")
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError as exc:
                errors.append(f"{directory}: {exc}")
        return errors

    def _ensure_parent(self, parent: Path, created_directories: List[Path], ledger: dict, ledger_path: Path) -> None:
        ensure_parent(parent, created_directories, ledger, task_id=str(self.uuid),
            plan_fingerprint=self.plan_fingerprint, save=lambda: self._atomic_json_write(ledger_path, ledger))

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

    def _replacement_batch(self):
        from .asset_replacement import ApprovedReplacementBatch
        from .asset_collision import requires_manual_collision
        from .media_file_identity import MediaFileIdentity
        from .small_file_identity import SmallFileFingerprint
        if type(self.replacement_authority) is not ApprovedReplacementBatch:
            raise ValueError("目标替换缺少类型化批准 authority")
        batch = self.replacement_authority
        batch.validate_operations(str(self.uuid), self.plan_fingerprint, self.operations)
        # Old media approvals remain readable, but must not read untyped backup
        # files or silently upgrade a pending transaction to the new contract.
        for item in batch.evidence:
            if (type(item.new_source) is not (MediaFileIdentity if requires_manual_collision(item.source) else SmallFileFingerprint)
                    or type(item.old_target) is not (MediaFileIdentity if requires_manual_collision(item.target) else SmallFileFingerprint)):
                raise ValueError('旧换源任务使用媒体摘要记录；请保留原任务及备份，用原版本完成恢复后重新生成计划')
        for item in batch.retirements:
            if type(item.old_target) is not (MediaFileIdentity if requires_manual_collision(item.target) else SmallFileFingerprint):
                raise ValueError('旧换源任务使用媒体摘要记录；请保留原任务及备份，用原版本完成恢复后重新生成计划')
        if self.policy_mode != "hardlink":
            raise ValueError("approved replacement only supports source-preserving hardlinks")
        return batch

    def _replacement_operations(self, batch):
        from .asset_replacement import safe_absolute
        root = Path(batch.target_root)
        rows = []
        targets = set()
        for raw in self.operations:
            source, target = (safe_absolute(str(raw[name])) for name in ("source", "target"))
            try:
                relative = target.relative_to(root)
            except ValueError as exc:
                raise ValueError("replacement batch operation escapes approved root") from exc
            if not relative.parts or relative.parts[0].casefold().startswith(".bar-replacements"):
                raise ValueError("replacement operation targets reserved transaction paths")
            action = str(raw.get("action") or "create")
            if action not in {"create", "reuse_existing", "replace"}:
                raise ValueError("unknown replacement batch action")
            if target in targets:
                raise ValueError("duplicate replacement batch target")
            targets.add(target)
            row = dict(source=str(source), target=str(target), action=action)
            if action == "replace":
                row["replacement_evidence"] = raw["replacement_evidence"]
            elif action == "reuse_existing":
                row["collision_resolution"] = raw.get("collision_resolution")
            rows.append(row)
        if any(Path(row["source"]) in targets for row in rows):
            raise ValueError("replacement batch cannot consume one of its own targets")
        return rows

    def _read_replacement_journal(self, batch, operations):
        from .asset_replacement import DirectoryWitness, require_file, _fingerprint, safe_absolute
        directory = safe_absolute(batch.transaction_directory)
        path = directory / "journal.json"
        if not directory.exists():
            return None
        safe_absolute(path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("replacement transaction has no trustworthy journal")
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
            if (journal["schema_version"] != 2 or journal["task_id"] != str(self.uuid)
                    or journal["plan_fingerprint"] != self.plan_fingerprint
                    or journal["operations"] != operations
                    or journal.get('retirements') != [row.to_dict() for row in batch.retirements]
                    or journal.get('season_directory') != (json.loads(batch.season_directory) if batch.season_directory else None)
                    or journal.get('completion_policy') != batch.completion_policy
                    or len(journal["entries"]) != len(operations)):
                raise ValueError("replacement journal differs from approved plan")
            saved = journal.get('rollback_preservation')
            if saved is not None and (self.rollback_preservation is None or saved != self.rollback_preservation.to_dict()):
                raise ValueError('此事务已冻结回滚共享保护，只能通过原回滚入口继续')
            DirectoryWitness(**journal["directory"]).validate()
            if journal["directory"]["path"] != str(directory):
                raise ValueError("replacement journal directory mismatch")
            from .asset_collision import requires_manual_collision
            from .media_file_identity import MediaFileIdentity
            from .small_file_identity import SmallFileFingerprint
            for operation, row in zip(operations, journal["entries"]):
                observed = _fingerprint(row["source_evidence"])
                if type(observed) is not (MediaFileIdentity if requires_manual_collision(operation['source']) else SmallFileFingerprint):
                    raise ValueError('旧换源事务使用媒体摘要记录；请保留原任务及备份，用原版本完成恢复')
            # Every artifact must retain the exact approved inode and bytes.
            for index, operation in enumerate(operations):
                staged = directory / f'new-{index}'
                if operation['action']=='create' and os.path.lexists(staged):
                    require_file(staged, _fingerprint(journal['entries'][index]['source_evidence']))
                if operation["action"] == "replace":
                    evidence = next(item for item in batch.evidence if item.target == operation["target"])
                    for name, expected in ((f"old-{index}", evidence.old_target),
                                           (f"new-{index}", evidence.new_source),
                                           (f"restore-{index}", evidence.old_target)):
                        candidate = directory / name
                        if os.path.lexists(candidate):
                            require_file(candidate, expected)
            for index, evidence in enumerate(batch.retirements):
                backup = directory / f'retired-{index}'
                if os.path.lexists(backup):
                    require_file(backup, evidence.old_target)
        except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
            raise ValueError("replacement journal is invalid") from exc
        return journal

    def _validate_replacement_state(self, batch, operations, journal, *, rollback=False):
        from .replacement_completion import require_inverse_allowed, validate_cleanup
        if rollback:
            require_inverse_allowed({'completion_policy': batch.completion_policy})
        cleaning = validate_cleanup(batch, operations, journal)
        observed_targets = {}
        if batch.season_directory is not None:
            from .season_replacement import validate_directory
            observed_targets = validate_directory(json.loads(batch.season_directory),phase='old' if journal is None else 'journal')
        if self.rollback_preservation is not None:
            if not rollback:
                raise ValueError('回滚共享保护不能用于正向执行')
            self.rollback_preservation.validate(str(self.uuid), self.plan_fingerprint, operations, journal)
        from .asset_replacement import observe_file, require_file, _fingerprint, safe_absolute
        if rollback:
            if journal is None:
                raise ValueError('回滚需要原始事务记录')
            for evidence in (*batch.evidence, *batch.retirements):
                target = safe_absolute(evidence.target)
                for row in evidence.parent_chain:
                    if Path(row.path) in target.parents:
                        row.validate()
            for index, operation in enumerate(operations):
                target = safe_absolute(operation['target'])
                old = next((e for e in batch.evidence if e.target == str(target)), None)
                actual = observe_file(target) if os.path.lexists(target) else None
                if actual is not None:
                    allowed = {_fingerprint(journal['entries'][index]['source_evidence'])}
                    if operation['action'] == 'reuse_existing':
                        reuse = CollisionResolution.from_dict(operation['collision_resolution'])
                        if reuse.evidence_kind == 'hardlink-identity-v1':
                            allowed = {_fingerprint(dict(kind=reuse.evidence_kind, size=reuse.target_size,
                                device=reuse.target_device, inode=reuse.target_inode))}
                        elif reuse.evidence_kind == 'content-xxh3-128-v1':
                            allowed = {_fingerprint(dict(kind=reuse.evidence_kind, size=reuse.target_size,
                                xxh3_128=reuse.target_xxh3_128, device=reuse.target_device, inode=reuse.target_inode))}
                        else:
                            raise ValueError('目标复用证据格式不受支持')
                    if old is not None:
                        allowed.add(old.old_target)
                    if actual not in allowed:
                        raise ValueError('回滚目标已由其他操作改变，拒绝覆盖')
                if old is not None and actual != old.old_target:
                    require_file(batch.transaction_directory / f'old-{index}', old.old_target)
            for index, evidence in enumerate(batch.retirements):
                if os.path.lexists(evidence.target):
                    require_file(evidence.target, evidence.old_target)
                else:
                    require_file(batch.transaction_directory / f'retired-{index}', evidence.old_target)
            return
        verified = {}
        for evidence in batch.evidence:
            evidence.validate_parents()
            source = require_file(evidence.source, evidence.new_source)
            target = observed_targets[evidence.target] if evidence.target in observed_targets else observe_file(evidence.target)
            evidence.validate_target(target, target_may_be_new=journal is not None)
            verified[evidence.target] = source, target
        sources = []
        for index, evidence in enumerate(batch.retirements):
            if evidence.target in observed_targets:
                evidence.validate_paths()
                for parent in evidence.parent_chain:
                    parent.validate()
                if observed_targets[evidence.target] != evidence.old_target:
                    raise ValueError('待退场旧文件已变化')
            else:
                evidence.validate_current(retired=journal is not None)
            if not os.path.lexists(evidence.target) and not cleaning:
                require_file(batch.transaction_directory / f'retired-{index}', evidence.old_target)
        for index, operation in enumerate(operations):
            source, target = Path(operation["source"]), Path(operation["target"])
            safe_absolute(target)
            current_source = verified[str(target)][0] if str(target) in verified else observe_file(source)
            sources.append(current_source)
            if current_source.device != self._existing_parent_device(target.parent):
                raise ValueError("硬链接换源要求全部源文件与目标目录位于同一文件系统；请将新资源下载到目标所在磁盘后重新生成计划。原有文件未替换")
            if journal is not None:
                if current_source != _fingerprint(journal["entries"][index]["source_evidence"]):
                    raise ValueError("换源来源与原事务内容或身份不同")
            if operation["action"] == "create" and os.path.lexists(target):
                if journal is None:
                    raise ValueError("unknown target exists before replacement batch")
                require_file(target, current_source)
            elif operation["action"] == "reuse_existing":
                resolution = CollisionResolution.from_dict(operation["collision_resolution"])
                from .media_file_identity import MediaFileIdentity
                if (getattr(current_source, 'xxh3_128', None) != resolution.source_xxh3_128
                        and resolution.evidence_kind == 'content-xxh3-128-v1'):
                    raise ValueError('换源复用来源内容与批准决定不同')
                validate_reuse_resolution(source, target, resolution, source_identity=MediaFileIdentity(
                    current_source.size, current_source.device, current_source.inode))
        if journal is not None:
            # A new target cannot be accepted without the durable old backup.
            for index, operation in enumerate(operations):
                if operation["action"] != "replace":
                    continue
                evidence = next(item for item in batch.evidence if item.target == operation["target"])
                if verified[evidence.target][1] == evidence.new_source and not cleaning:
                    require_file(batch.transaction_directory / f"old-{index}", evidence.old_target)
        return sources

    def _trans_replacements(self, *, rollback=False):
        """Per-file recoverable switches, deliberately not a multi-file atomic commit."""
        from .asset_replacement import DirectoryWitness, observe_file, safe_absolute
        from .file_lock import advisory_file_lock, read_lock_metadata
        batch = self._replacement_batch()
        operations = self._replacement_operations(batch)
        if self.dry_run:
            journal = self._read_replacement_journal(batch, operations)
            self._validate_replacement_state(batch, operations, journal, rollback=rollback)
            if journal and journal.get('cleanup'):
                raise ValueError('媒体已完成，请通过原任务继续收尾；不能重放媒体')
            return None
        safe_absolute(self.record_root.absolute())
        # The execution service also holds its ordinary work/series lock. This
        # persistent OS lock serializes direct retries sharing the record store.
        lock_path = self.record_root / ".asset-replacement.lock"
        safe_absolute(lock_path.absolute())
        if lock_path.exists():
            metadata = read_lock_metadata(lock_path)
            if metadata is None:
                # Windows prohibits reading the locked first byte, while the
                # diagnostic JSON after it remains readable. Diagnostics only
                # identify our reserved file; the OS lock below owns exclusion.
                try:
                    with lock_path.open("rb", buffering=0) as handle:
                        handle.seek(1)
                        metadata = json.loads(handle.read().decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    metadata = None
            if not metadata or metadata.get("purpose") != "asset-replacement":
                raise ValueError("replacement lock path contains an unknown file")
        with advisory_file_lock(lock_path, owner={"purpose": "asset-replacement"}, require_single_link=True):
            journal = self._read_replacement_journal(batch, operations)
            sources = self._validate_replacement_state(batch, operations, journal, rollback=rollback)
            if journal and journal.get('cleanup'):
                raise ValueError('媒体已完成，请通过原任务继续收尾；不能重放媒体')
            directory = batch.transaction_directory
            if journal is None:
                namespace = safe_absolute(directory.parent)
                namespace.mkdir(exist_ok=True)
                directory.mkdir()  # An unjournaled/foreign transaction is never adopted.
                current = directory.stat()
                journal = dict(schema_version=2, task_id=str(self.uuid),
                               plan_fingerprint=self.plan_fingerprint, operations=operations,
                               retirements=[row.to_dict() for row in batch.retirements],
                               directory=asdict(DirectoryWitness(str(directory), current.st_dev, current.st_ino)),
                               status="prepared", entries=[dict(status="planned", source_evidence=asdict(
                                   evidence)) for evidence in sources])
                if batch.completion_policy is not None:
                    journal['completion_policy'] = batch.completion_policy
                if batch.season_directory is not None:
                    journal['season_directory'] = json.loads(batch.season_directory)
                self._write_replacement_journal(directory, journal)
            ledger_path = safe_task_path(self.record_root, str(self.uuid), suffix=".ledger.json")
            if rollback:
                if self.rollback_preservation is not None:
                    journal['rollback_preservation'] = self.rollback_preservation.to_dict()
                    self._write_replacement_journal(directory, journal)
                self._rollback_replacement_batch(batch, operations, journal)
                ledger = self._replacement_ledger(batch, operations, journal, "rolled_back")
                self._atomic_json_write(ledger_path, ledger)
                return None
            self._atomic_json_write(safe_task_path(self.record_root, str(self.uuid)),
                                    {item["source"]: item["target"] for item in operations})
            try:
                self._prepare_replacement_links(batch, operations, journal)
                for index, operation in enumerate(operations):
                    self._execute_replacement_entry(batch, operation, index, journal)
                self._retire_old_assets(batch, journal)
                journal["status"] = "completed"
                self._write_replacement_journal(directory, journal)
                ledger = self._replacement_ledger(batch, operations, journal, "completed")
                self._atomic_json_write(ledger_path, ledger)
                return None
            except Exception as exc:
                journal["error"] = str(exc)
                if batch.completion_policy is not None:
                    status = "failed"  # Keep the journaled forward boundary for explicit resume.
                else:
                    try:
                        self._rollback_replacement_batch(batch, operations, journal)
                        status = "rolled_back"
                    except Exception as rollback_error:
                        journal["rollback_error"] = str(rollback_error)
                        status = "rollback_failed"
                journal["status"] = status
                self._write_replacement_journal(directory, journal)
                self._atomic_json_write(ledger_path,
                                        self._replacement_ledger(batch, operations, journal, status))
                return str(exc)

    def _prepare_replacement_links(self, batch, operations, journal):
        """Acquire every incoming hardlink before changing any old library file."""
        from .asset_replacement import require_file, _fingerprint, safe_absolute
        for index, operation in enumerate(operations):
            if operation['action']=='reuse_existing':
                continue
            evidence = _fingerprint(journal['entries'][index]['source_evidence'])
            require_file(operation['source'], evidence)
            staged = safe_absolute(batch.transaction_directory / f'new-{index}')
            if not os.path.lexists(staged):
                os.link(operation['source'], staged)
            require_file(staged, evidence)
        journal['incoming_prepared'] = True
        self._write_replacement_journal(batch.transaction_directory, journal)

    def _retire_old_assets(self, batch, journal):
        from .asset_replacement import require_file, safe_absolute
        for index, evidence in enumerate(batch.retirements):
            target = safe_absolute(evidence.target)
            backup = safe_absolute(batch.transaction_directory / f'retired-{index}')
            evidence.validate_current(retired=True)
            if not os.path.lexists(backup):
                require_file(target, evidence.old_target)
                os.link(target, backup)
            require_file(backup, evidence.old_target)
            journal['retirement_phase'] = {'index': index, 'status': 'backed_up'}
            self._write_replacement_journal(batch.transaction_directory, journal)
            if os.path.lexists(target):
                require_file(target, evidence.old_target)
                target.unlink()
            journal['retirement_phase'] = {'index': index, 'status': 'retired'}
            self._write_replacement_journal(batch.transaction_directory, journal)

    def _execute_replacement_entry(self, batch, operation, index, journal):
        from .asset_replacement import observe_file, require_file, _fingerprint, safe_absolute
        directory = batch.transaction_directory
        source, target = Path(operation["source"]), Path(operation["target"])
        require_file(source, _fingerprint(journal["entries"][index]["source_evidence"]))
        safe_absolute(target)
        if operation["action"] == "replace":
            evidence = next(item for item in batch.evidence if item.target == str(target))
            evidence.validate_parents()
            current = observe_file(target)
            evidence.validate_target(current, target_may_be_new=True)
            backup, prepared = directory / f"old-{index}", directory / f"new-{index}"
            if not os.path.lexists(backup):
                if current != evidence.old_target:
                    raise ValueError("换源原目标已变化且回滚备份缺失")
                os.link(target, backup)
            require_file(backup, evidence.old_target)
            journal["entries"][index]["status"] = "backed_up"
            self._write_replacement_journal(directory, journal)
            if observe_file(target) != evidence.new_source:
                if not os.path.lexists(prepared):
                    os.link(source, prepared)
                require_file(prepared, evidence.new_source)
                journal["entries"][index]["status"] = "prepared_link"
                self._write_replacement_journal(directory, journal)
                evidence.validate_current()
                require_file(backup, evidence.old_target)
                os.replace(prepared, target)
            require_file(target, evidence.new_source)
        elif operation["action"] == "reuse_existing":
            validate_reuse_resolution(source, target, CollisionResolution.from_dict(operation["collision_resolution"]))
        else:
            if not os.path.lexists(target):
                # Missing directories keep the same witnessed creation contract.
                journal.setdefault("directory_witnesses", [])
                self._ensure_parent(target.parent, [], journal, directory / "journal.json")
                os.replace(directory / f'new-{index}', target)
            require_file(target, _fingerprint(journal["entries"][index]["source_evidence"]))
        if operation['action'] != 'reuse_existing':
            from .media_file_identity import MediaFileIdentity, verify_link_target
            captured = _fingerprint(journal['entries'][index]['source_evidence'])
            verify_link_target(target, MediaFileIdentity(captured.size, captured.device, captured.inode))
        journal["entries"][index]["status"] = "completed"
        self._write_replacement_journal(directory, journal)

    def _rollback_replacement_batch(self, batch, operations, journal):
        from .asset_replacement import safe_absolute
        directory = safe_absolute(batch.transaction_directory)
        errors = []
        for index in reversed(range(len(operations))):
            try:
                self._rollback_replacement_entry(batch, operations[index], index, journal)
                journal["entries"][index]["status"] = "rolled_back"
                self._write_replacement_journal(directory, journal)
            except Exception as exc:
                journal["entries"][index]["status"] = "rollback_failed"
                errors.append(f"{operations[index]['target']}: {exc}")
        from .asset_replacement import require_file
        for index, evidence in enumerate(batch.retirements):
            try:
                target = safe_absolute(evidence.target)
                backup = directory / f'retired-{index}'
                if not os.path.lexists(target):
                    require_file(backup, evidence.old_target)
                    os.link(backup, target)
                require_file(target, evidence.old_target)
                journal['retirement_phase'] = {'index': index, 'status': 'restored'}
                self._write_replacement_journal(directory, journal)
            except Exception as exc:
                errors.append(f'{evidence.target}: {exc}')
        journal["status"] = "rollback_failed" if errors else "rolled_back"
        self._write_replacement_journal(directory, journal)
        if errors:
            raise ValueError("replacement rollback incomplete: " + "; ".join(errors))

    def _rollback_replacement_entry(self, batch, operation, index, journal):
        from .asset_replacement import observe_file, require_file, _fingerprint, safe_absolute
        directory = safe_absolute(batch.transaction_directory)
        target = safe_absolute(operation["target"])
        protected = dict(self.rollback_preservation.targets).get(str(target)) if self.rollback_preservation else None
        if protected is not None:
            require_file(target, protected)
            return
        if operation["action"] == "replace":
            evidence = next(item for item in batch.evidence if item.target == str(target))
            for row in evidence.parent_chain:
                if Path(row.path) in target.parents:
                    row.validate()
            current = observe_file(target) if os.path.lexists(target) else None
            if current is None or current == evidence.new_source:
                backup, restore = directory / f"old-{index}", directory / f"restore-{index}"
                require_file(backup, evidence.old_target)
                if not os.path.lexists(restore):
                    os.link(backup, restore)
                require_file(restore, evidence.old_target)
                if current is not None:
                    require_file(target, evidence.new_source)
                os.replace(restore, target)
            require_file(target, evidence.old_target)
        elif operation["action"] == "create" and os.path.lexists(target):
            require_file(target, _fingerprint(journal["entries"][index]["source_evidence"]))
            target.unlink()

    def _write_replacement_journal(self, directory, journal):
        from .asset_replacement import DirectoryWitness, safe_absolute
        directory = safe_absolute(directory)
        if journal["directory"]["path"] != str(directory):
            raise ValueError("replacement journal directory differs from transaction")
        DirectoryWitness(**journal["directory"]).validate()
        path = safe_absolute(directory / "journal.json")
        if os.path.lexists(path) and (not path.is_file() or path.stat().st_nlink != 1):
            raise ValueError("replacement journal is not an independent regular file")
        self._atomic_json_write(path, journal)

    def _replacement_ledger(self, batch, operations, journal, status):
        from .asset_replacement import observe_file, _fingerprint, ledger_file_fields
        rows = []
        for index, operation in enumerate(operations):
            source = _fingerprint(journal["entries"][index]["source_evidence"])
            row = dict(source=operation["source"], target=operation["target"],
                       action=operation["action"], status=("reused_identical_asset" if operation["action"] == "reuse_existing"
                       else "created") if status == "completed" else status,
                       transfer_type="reuse_existing" if operation["action"] == "reuse_existing" else "hardlink",
                       **ledger_file_fields(source, prefix='source'),
                       error=journal.get("error"), conditional_authority=None)
            if status == "completed":
                target = observe_file(operation["target"])
                if target.kind != source.kind:
                    raise ValueError('换源两端文件证据种类不一致')
                row.update(**ledger_file_fields(target, prefix='target'),
                           target_link_count=Path(operation["target"]).stat().st_nlink)
            if operation["action"] == "replace":
                row["replacement_evidence"] = operation["replacement_evidence"]
                row["replacement_backup"] = str(batch.transaction_directory / f"old-{index}")
            rows.append(row)
        return dict(schema_version=3, task_id=str(self.uuid), plan_fingerprint=self.plan_fingerprint,
                    mode="hardlink", dry_run=False, status=status, entries=rows,
                    directory_witnesses=journal.get("directory_witnesses", []),
                    replacement_journal=str(batch.transaction_directory / "journal.json"),
                    **({'generated_outputs':self.generated_result} if self.generated_result is not None else {}))

    def recover_replacements(self, *, rollback=False):
        """Resume or restore this exact approved batch; never accept raw journal authority."""
        return self._trans_replacements(rollback=rollback)
