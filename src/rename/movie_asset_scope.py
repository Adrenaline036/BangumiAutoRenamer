"""Frozen, read-only Catalog dependencies for one movie version append.

Missing materialization is not retirement. Only the selected version and
shared target content are CAS dependencies; another version's revision is not.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import re
from pathlib import Path
from collections.abc import Mapping

from .path_safety import first_reparse_component


class MovieSlotChoiceRequired(ValueError):
    def __init__(self, work_key, versions):
        self.work_key = work_key
        self.versions = tuple(versions)
        self.slot_key = f"work:{work_key}:version:{'+'.join(versions)}"
        super().__init__("所选电影版本槽位已有登记，请使用修复或明确换源")


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plain(value):
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


@dataclass(frozen=True)
class FrozenMovieAssetScope:
    payload_json: str

    def __post_init__(self):
        value = json.loads(self.payload_json)
        if (set(value) != {"policy", "work_key", "canonical_root", "versions", "dependencies"}
                or value["policy"] != "movie-append-scope-v1"
                or not isinstance(value["work_key"], str) or not value["work_key"]
                or not Path(value["canonical_root"]).is_absolute()
                or not isinstance(value["versions"], list) or not value["versions"]
                or any(not isinstance(v, str) or not v for v in value["versions"])
                or sorted(set(value["versions"])) != value["versions"]
                or not isinstance(value["dependencies"], dict)
                or _encode(value) != self.payload_json):
            raise ValueError("电影版本依赖观察无效")

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError("电影版本依赖观察格式无效")
        return cls(_encode(_plain(value)))

    def to_dict(self):
        return json.loads(self.payload_json)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()


def _selection(plan):
    if (plan.work_identity is None or plan.work_identity.status != "confirmed"
            or not any(g.kind=='movie_continuous' for g in plan.presentation_groups)):
        return None
    movie_groups={g.group_id for g in plan.presentation_groups if g.kind=='movie_continuous'}
    versions = sorted({e.presentation_asset.version_descriptor.version_ref for e in plan.entries
                       if e.presentation_asset is not None
                       and e.presentation_asset.presentation_group in movie_groups
                       and e.presentation_asset.asset_role.kind == "content_version"
                       and e.presentation_asset.version_descriptor is not None})
    key = str(plan.metadata.get("catalog_work_key") or plan.metadata.get("proposed_work_key") or "")
    root = Path(str(plan.metadata.get("canonical_work_root") or ""))
    if not versions or not re.fullmatch(r"work-[0-9a-fA-F-]{32,36}", key) or not root.is_absolute():
        return None
    return key, root, versions


def _safe_record(record, *, physical_targets):
    """Validate every recorded path lexically, but stat only selected dependencies.

    The canonical root is always a physical dependency. An unrelated version's
    missing/file/directory state cannot grant authority or veto this slot.
    """
    root = Path(str(record["canonical_root"]))
    if not root.is_absolute() or first_reparse_component(root) is not None:
        raise ValueError("作品目录含重解析点或无效路径")
    if os.path.lexists(root) and not root.is_dir():
        raise ValueError("作品目录不是普通目录")
    root_key = os.path.normcase(os.path.abspath(root))
    physical = {os.path.normcase(os.path.abspath(path)) for path in physical_targets}

    def safe_target(path):
        # Normalize traversal before containment checks, without resolving the
        # unselected path through the live filesystem.
        normalized = Path(os.path.abspath(path))
        if (not path.is_absolute() or not normalized.is_relative_to(Path(root_key))
                or os.path.normcase(str(normalized)) == root_key):
            raise ValueError("作品登记含外来路径或无效目标")
        if os.path.normcase(str(normalized)) in physical and (
                first_reparse_component(path) is not None
                or (os.path.lexists(path) and not path.is_file())):
            raise ValueError("所选作品登记含重解析点或非普通文件")

    for row in record.get("assets", ()):
        if not isinstance(row, Mapping):
            raise ValueError("作品资产登记格式损坏")
        path = Path(str(row.get("target") or ""))
        safe_target(path)
    targets = set()
    for row in record.get("versions", ()):
        if not isinstance(row, Mapping):
            raise ValueError("作品版本登记格式损坏")
        path = Path(str(row.get("target") or ""))
        safe_target(path)
        if str(path) in targets:
            raise ValueError("作品版本登记目标无效或重复")
        targets.add(str(path))


def selected_dependencies(record, plan, *, repair=False):
    """Shared content excludes unrelated associations, never its byte identity."""
    from .work_catalog import copy_assets
    selection = _selection(plan)
    if selection is None:
        raise ValueError("计划没有唯一电影版本范围")
    _key, _root, refs = selection
    targets = {str(e.target_path) for e in plan.entries}
    if record is not None:
        _safe_record(record, physical_targets=targets)
    versions = [dict(row) for row in (record or {}).get("versions", ()) if row.get("version_ref") in refs]
    assets = []
    for row in copy_assets((record or {}).get("assets", [])):
        if row.get("target") not in targets:
            continue
        item = dict(row)
        if item.get("scope") == "shared":
            item.pop("associations", None)
        elif repair:
            item["associations"] = [dict(a) for a in item.get("associations", ())
                                    if a.get("task_id") == plan.task_id]
        assets.append(item)
    return _plain({"versions": sorted(versions, key=_encode), "assets": sorted(assets, key=_encode)})


def prepare_append(plan, catalog):
    selection = _selection(plan)
    if selection is None or plan.metadata.get("completed_rebuild_observation") is not None:
        return None
    operation = plan.metadata.get("asset_operation")
    if isinstance(operation, Mapping) and operation.get("mode") == "replace":
        return None
    key, root, refs = selection
    record = catalog.load(key) if catalog._path(key).exists() else None
    if record is not None and not catalog.compatible_base(record, plan, allow_explicit_provider_append=True):
        raise ValueError("电影版本与登记作品身份不一致")
    if plan.metadata.get('movie_version_selection') is not None:
        from .movie_version_selection import from_plan, validate_catalog_labels
        try:
            validate_catalog_labels(from_plan(plan), (record or {}).get('versions', ()))
        except ValueError as exc:
            raise MovieSlotChoiceRequired(key, refs) from exc
    deps = selected_dependencies(record, plan)
    if deps["versions"]:
        raise MovieSlotChoiceRequired(key, refs)
    return FrozenMovieAssetScope.from_mapping(dict(policy="movie-append-scope-v1", work_key=key,
        canonical_root=str(root), versions=refs, dependencies=deps))


def validate_scope(plan, catalog, observation, *, payload=None):
    if not isinstance(observation, FrozenMovieAssetScope):
        raise ValueError("电影追加必须消费类型化版本依赖")
    value = observation.to_dict()
    selection = _selection(plan)
    if selection is None or selection != (value["work_key"], Path(value["canonical_root"]), value["versions"]):
        raise ValueError("电影追加作品、根或版本身份已变化")
    record = payload if payload is not None else (catalog.load(value["work_key"])
        if catalog._path(value["work_key"]).exists() else None)
    if record is not None and not catalog.compatible_base(record, plan, allow_explicit_provider_append=True):
        raise ValueError("电影追加与当前作品身份不一致")
    if plan.metadata.get('movie_version_selection') is not None:
        from .movie_version_selection import from_plan, validate_catalog_labels
        validate_catalog_labels(from_plan(plan), (record or {}).get('versions', ()))
    if selected_dependencies(record, plan) != value["dependencies"]:
        raise ValueError("所选电影版本或依赖资产登记已变化，请重新生成")
    return True


def validate_append(plan, catalog, *, payload=None):
    from .asset_operation import AssetOperation
    from .projection_authority import validate_plan_projection_receipt
    raw = plan.metadata.get("asset_operation")
    if not isinstance(raw, Mapping) or raw.get("movie_scope") is None:
        return False
    operation = AssetOperation.from_mapping(raw).to_dict()
    if operation["mode"] != "append":
        raise ValueError("非追加操作不能使用电影追加依赖")
    validate_plan_projection_receipt(plan)
    return validate_scope(plan, catalog, FrozenMovieAssetScope.from_mapping(operation["movie_scope"]), payload=payload)


def validate_movie_authority(plan):
    """Every executable movie, including old rules Plans, uses this guard."""
    if not any(g.kind=='movie_continuous' for g in plan.presentation_groups):
        return
    from .movie_version_selection import validate_selection
    validate_selection(plan)
    from .asset_operation import AssetOperation
    from .catalog_binding import CatalogBinding
    selection=_selection(plan)
    binding=plan.metadata.get('catalog_binding_snapshot')
    if selection is None or not isinstance(binding,Mapping):
        raise ValueError('电影缺少已确认版本和作品登记权威；请重新生成整理计划')
    bound=CatalogBinding(**dict(binding))
    key,root,versions=selection
    if bound.backend!='work' or bound.key!=key:
        raise ValueError('电影版本与作品登记身份不一致')
    operation=AssetOperation.from_mapping(plan.metadata.get('asset_operation')).to_dict()
    expected_slot=f"work:{key}:version:{'+'.join(versions)}"
    if operation.get('requires_choice') or operation['slot_key']!=expected_slot:
        raise ValueError('电影版本已有登记或操作未确认；请核查或明确换源')
    if operation['mode']=='append':
        scope=FrozenMovieAssetScope.from_mapping(operation.get('movie_scope')).to_dict()
        if (scope['work_key'],Path(scope['canonical_root']),scope['versions'])!=(key,root,versions):
            raise ValueError('电影追加范围与当前版本不一致')
