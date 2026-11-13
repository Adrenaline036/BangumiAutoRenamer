"""Pure attachment projection from final video decisions, never episode guessing."""
from dataclasses import replace
from pathlib import Path

from .rename_plan import AssetRole, PresentationAssetPlanContract




def audio_entries(plan, source_entries):
    from .content_associations import from_plan, relation_map
    associations = from_plan(plan)
    frozen = relation_map(associations)
    videos={}
    for entry in source_entries:
        if entry.media_kind=='video' and entry.presentation_asset is not None:
            videos.setdefault(str(entry.source_path), []).append(entry)
    entries=[]
    for entry in source_entries:
        relation = frozen.get(entry.source_path)
        is_audio = relation is not None and relation['kind'] == 'audio'
        if not is_audio:
            entries.append(entry)
            continue
        matches = videos.get(relation['owners'][0], ()) if relation['state'] == 'linked' else ()
        if len(matches)!=1:
            root=Path(plan.metadata.get('canonical_work_root') or
                      (plan.execution_policy.target_root if plan.execution_policy else entry.target_path.parent))
            relative=(entry.source_path.relative_to(plan.source_root)
                      if entry.source_path!=plan.source_root else Path(entry.source_path.name))
            entries.append(replace(entry,target_path=root/'ManualReview'/'Audio'/relative,
                season_id=None,episode_id=None,media_kind='associated',
                role='external_audio_unresolved',requires_review=True,presentation_asset=None))
            continue
        video=matches[0]
        asset=video.presentation_asset
        owner=asset.version_descriptor.version_ref if asset.version_descriptor else asset.owner_version_ref
        entries.append(replace(entry,target_path=video.target_path.with_suffix(entry.source_path.suffix.lower()),
            season_id=video.season_id,episode_id=video.episode_id,episode_end=video.episode_end,media_kind='associated',
            role='external_audio',requires_review=False,presentation_asset=PresentationAssetPlanContract(
                asset.presentation_group,AssetRole('associated'),None,None,owner)))
    return tuple(entries)


def project_audio(plan):
    return replace(plan, entries=audio_entries(plan, plan.entries))


def validate_audio(plan):
    if audio_entries(plan, plan.entries)!=plan.entries:
        raise ValueError('配套音轨未从当前已确认视频派生，请重新生成整理计划')
    if any(e.role=='external_audio_unresolved' for e in plan.entries):
        raise ValueError('配套音轨没有唯一对应视频，请核对源文件；也可明确选择本次不整理该音轨')
