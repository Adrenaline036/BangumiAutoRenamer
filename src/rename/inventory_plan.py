"""Turn a drift-checked M0 inventory into a blocked schema-v5 review draft."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Mapping

from .movie_assets import (
    DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW,
    MovieAssetInventory,
    compare_movie_asset_inventory,
)
from .presentation import (
    default_presentation_group_id,
    provider_identity_from_metadata,
)
from .rename_plan import (
    AssetRole,
    ExecutionPolicy,
    PlanEntry,
    PresentationAssetPlanContract,
    PresentationGroup,
    ProviderIdentity,
    RenamePlan,
    SubtitlePlanContract,
)
from .subtitle_labels import DimensionResolution


class InventoryDraftError(RuntimeError):
    pass


def build_inventory_review_draft(
    task_data: Mapping[str, object],
    *,
    dry_run: bool,
) -> RenamePlan:
    """Create a non-approved Plan only after the metadata snapshot is unchanged."""

    try:
        inventory = MovieAssetInventory.from_dict(task_data.get("movie_inventory"))
    except ValueError as exc:
        raise InventoryDraftError(f"电影资产清单无效：{exc}") from exc
    if DISC_STRUCTURE_REQUIRES_MANUAL_WORKFLOW in inventory.reason_codes:
        raise InventoryDraftError("光盘目录结构仍只支持只读清单，不能建立映射草案")
    source_root_value = str(task_data.get("path") or "").strip()
    target_root_value = str(task_data.get("draft_target_root") or "").strip()
    initial_target_value = str(
        task_data.get("draft_initial_target_root")
        or task_data.get("draft_target_root")
        or ""
    ).strip()
    selected_source = Path(source_root_value)
    source_root = (
        selected_source.parent if selected_source.is_file() else selected_source
    )
    target_root = Path(target_root_value)
    initial_target_root = Path(initial_target_value)
    if (
        not source_root_value
        or not target_root_value
        or not (selected_source.is_dir() or selected_source.is_file())
    ):
        raise InventoryDraftError("库存草案缺少有效源目录或源文件、目标根目录")
    if not target_root.is_absolute() or not initial_target_root.is_absolute():
        raise InventoryDraftError("库存草案的允许目标根和初始建议目录必须是绝对路径")
    if not _is_within(initial_target_root, target_root):
        raise InventoryDraftError(
            "库存草案的初始建议目录越出允许目标根；请检查两个目标字段是否传反"
        )
    comparison = compare_movie_asset_inventory(selected_source, inventory)
    if not comparison.unchanged:
        raise InventoryDraftError(
            "电影资产清单已漂移，必须重新扫描后再审核："
            f"新增 {len(comparison.added_paths)}，删除 {len(comparison.removed_paths)}，"
            f"变化 {len(comparison.changed)}"
        )
    provider_data = task_data.get("provider_identity")
    groups = []
    if isinstance(provider_data, Mapping):
        try:
            provider = provider_identity_from_metadata(
                namespace=str(provider_data.get("namespace") or ""),
                entity_type=str(provider_data.get("entity_type") or ""),
                provider_id=provider_data.get("provider_id"),
                title=str(provider_data.get("title") or ""),
                year=provider_data.get("year"),
                decision_source="provider_unique",
                evidence=("M0 清单建立前已确认的电影作品身份",),
                original_title=provider_data.get("original_title"),
                poster_url=provider_data.get("poster_url"),
                source_url=provider_data.get("source_url"),
                snapshot_at=provider_data.get("snapshot_at"),
            )
        except ValueError as exc:
            raise InventoryDraftError(f"电影 provider 身份无效：{exc}") from exc
        groups.append(PresentationGroup(
            default_presentation_group_id("movie_continuous"),
            "movie_continuous",
            "电影连续版",
            None,
            provider,
        ))
    else:
        raw_candidates = task_data.get("provider_candidates")
        if not isinstance(raw_candidates, Mapping) or not raw_candidates:
            raise InventoryDraftError("库存草案缺少 provider 身份或候选")
        for group_id, display_name in (
            ("movie_continuous", "电影连续版"),
            ("tv_episodes", "TV 分集版"),
        ):
            if group_id not in raw_candidates:
                continue
            groups.append(PresentationGroup(
                default_presentation_group_id(group_id),
                group_id,
                display_name,
                None,
                ProviderIdentity(
                    status="unresolved",
                    namespace=None,
                    entity_type=None,
                    provider_id=None,
                    title=None,
                    year=None,
                    decision_source="manual_review",
                    evidence=("自动模式同时命中 TV/movie；等待分别确认",),
                ),
            ))
    group = groups[0]
    raw_ownership = task_data.get("source_ownership_snapshot")
    claimed_locations = set()
    if isinstance(raw_ownership, Mapping):
        raw_claims = raw_ownership.get("claims", ())
        if isinstance(raw_claims, (list, tuple)):
            for claim in raw_claims:
                if (
                    isinstance(claim, Mapping)
                    and str(claim.get("state") or "active") == "active"
                    and str(claim.get("task_id") or "")
                    != str(task_data.get("uuid") or "")
                ):
                    location = str(
                        claim.get("location_key") or claim.get("source_path") or ""
                    )
                    if location:
                        claimed_locations.add(location.casefold())
    file_entries = tuple(entry for entry in inventory.entries if entry.size_bytes is not None)
    def source_path_for(relative_path: str) -> Path:
        if selected_source.is_file():
            if PurePosixPath(relative_path).name != selected_source.name:
                raise InventoryDraftError("单文件库存包含了其他来源")
            return selected_source
        return source_root / Path(*PurePosixPath(relative_path).parts)

    file_entries = tuple(
        entry for entry in file_entries
        if str(source_path_for(entry.relative_path).resolve(strict=False)).casefold()
        not in claimed_locations
    )

    video_sources = tuple(
        source_path_for(entry.relative_path)
        for entry in file_entries if entry.entry_type == "video"
    )
    entries = []
    for item in file_entries:
        source = source_path_for(item.relative_path)
        target = initial_target_root / "ManualReview" / Path(
            *PurePosixPath(item.relative_path).parts
        )
        if item.entry_type == "subtitle":
            subtitle = SubtitlePlanContract(
                associated_video_source_path=None,
                association_candidates=video_sources,
                raw_label=source.stem,
                primary_language=None,
                script=None,
                association_state=(
                    DimensionResolution.CONFLICT
                    if len(video_sources) > 1 else DimensionResolution.UNRESOLVED
                ),
                parse_state=DimensionResolution.UNRESOLVED,
                reason_codes=("INVENTORY_DRAFT_REQUIRES_SUBTITLE_REVIEW",),
                decision_source="inventory_draft",
            )
            entries.append(PlanEntry(
                source,
                target,
                media_kind="subtitle",
                role="subtitle",
                requires_review=True,
                evidence=("M0 只读库存；字幕尚未绑定具体内容版本",),
                subtitle=subtitle,
            ))
        else:
            media_kind = "video" if item.entry_type == "video" else "associated"
            entries.append(PlanEntry(
                source,
                target,
                media_kind=media_kind,
                requires_review=True,
                evidence=("M0 只读库存；分组、角色和目标尚待确认",),
                presentation_asset=PresentationAssetPlanContract(
                    group.group_id,
                    AssetRole("unknown"),
                    None,
                    None,
                ),
            ))
    if not entries:
        raise InventoryDraftError("电影资产清单没有可审核的文件条目")
    linked = []
    raw_linked = task_data.get("linked_provider_identities", ())
    if isinstance(raw_linked, (list, tuple)):
        for item in raw_linked:
            if not isinstance(item, Mapping):
                continue
            try:
                linked.append(ProviderIdentity.from_dict(item))
            except ValueError:
                continue
    return RenamePlan(
        task_id=str(task_data.get("uuid") or ""),
        source_root=source_root,
        entries=tuple(entries),
        expected_sources=tuple(entry.source_path for entry in entries),
        metadata={
            "name": task_data.get("name"),
            # ``season_id`` is retained only for old callers and old Plan
            # readability.  New completion code consumes the explicit value
            # and provenance pair below, so an inventory placeholder can no
            # longer masquerade as Season 00.
            "season_id": task_data.get("season_id"),
            "season_number": task_data.get("season_number"),
            "season_source": task_data.get("season_source", "unset"),
            "target_root": str(target_root),
            "is_anime": task_data.get("is_anime"),
            "is_movie": task_data.get("is_movie"),
            "mapping_source": "inventory_review_draft",
            "presentation_draft": True,
            "inventory_fingerprint": inventory.fingerprint,
            "provider_candidates": task_data.get("provider_candidates", {}),
            "presentation_assignment_state": (
                "placeholder" if not isinstance(provider_data, Mapping) else "draft"
            ),
            "allowed_target_root": str(target_root),
            "initial_suggested_target_root": str(initial_target_root),
            "proposed_work_key": task_data.get("proposed_work_key"),
            "draft_root_snapshot": task_data.get("draft_root_snapshot", {}),
            "draft_root_config_fingerprint": task_data.get(
                "draft_root_config_fingerprint", ""
            ),
        },
        execution_policy=ExecutionPolicy(target_root=target_root, dry_run=dry_run),
        presentation_groups=tuple(groups),
        linked_provider_identities=tuple(linked),
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True
