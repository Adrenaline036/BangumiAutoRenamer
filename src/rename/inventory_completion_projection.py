"""Shared pure completion metadata."""




def project_completion_metadata(metadata, *, context, work_root, naming_decision, proposed_work_key,
        catalog_work_key, catalog_revision, catalog_match_source, completion_mode, unresolved_videos,
        unresolved_collisions=(), episode_assignment_conflicts=(), series_key=None, season_binding_id=None,
        series_catalog_revision=0):
    from .plan_review import InventoryCompletionContext
    from .target_routing import TargetRootSnapshot
    if type(context) is not InventoryCompletionContext:
        raise ValueError('完成投影缺少实际库存来源上下文')
    snapshot = TargetRootSnapshot.from_dict(context.root_snapshot)
    tv = context.group_kind == 'tv_episodes'
    return {**metadata, 'target_root': str(context.target_root), 'allowed_target_root': str(context.target_root),
        'season_id': context.season_number if tv else None, 'season_number': context.season_number if tv else None,
        'season_source': context.season_source if tv else 'unset',
        'initial_suggested_target_root': str(work_root), 'mapping_source': 'inventory_smart_completion',
        'completion_mode': completion_mode, 'presentation_assignment_state': 'partial' if unresolved_videos else 'confirmed',
        'smart_completion_unresolved_videos': tuple(unresolved_videos),
        'target_root_snapshot': snapshot.to_dict(), 'target_root_config_fingerprint': snapshot.fingerprint,
        'proposed_work_key': proposed_work_key, 'canonical_work_root': str(work_root), 'canonical_naming_required': True,
        'canonical_naming_decision': naming_decision.to_dict(), 'catalog_work_key': catalog_work_key,
        'catalog_revision': catalog_revision, 'catalog_match_source': catalog_match_source,
        'series_key': series_key if tv else None, 'season_binding_id': season_binding_id if tv else None,
        'series_catalog_revision': int(series_catalog_revision) if tv else 0,
        'owner_version_model': 1 if context.group_kind == 'movie_continuous' else 0,
        'asset_collision_unresolved': tuple(unresolved_collisions),
        'episode_assignment_conflict_sources': tuple(episode_assignment_conflicts),
        'inventory_completion_context': context.to_dict()}
