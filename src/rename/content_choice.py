"""Explicit read-only previews and one atomic content Decision transaction."""
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path

from . import content_selection as content
from .content_associations import encode, observe_content_associations, source_key
from .movie_version_selection import MovieVersionSelection


@dataclass(frozen=True)
class ContentChangePreview:
    payload_json: str

    def __post_init__(self):
        try:
            value = json.loads(self.payload_json)
            if (not isinstance(value, dict) or set(value) != {'policy', 'kind', 'task_id', 'plan_fingerprint',
                    'decision_fingerprint', 'selection', 'movie_selection', 'selected_entry_ids'}
                    or value['policy'] != 'content-change-preview-v1'
                    or value['kind'] not in {'exclude', 'restore'}
                    or encode(value) != self.payload_json or not isinstance(value['selected_entry_ids'], list)):
                raise ValueError('内容选择预览无效')
            selection = content.ContentSelection.from_mapping(value['selection'])
            if (selection.to_dict()['task_id'] != value['task_id']
                    or selection.to_dict()['parent_plan_fingerprint'] != value['plan_fingerprint']
                    or not isinstance(value['decision_fingerprint'], str)
                    or content._HASH.fullmatch(value['decision_fingerprint']) is None):
                raise ValueError('内容选择预览绑定不一致')
            if value['movie_selection'] is not None:
                MovieVersionSelection.from_mapping(value['movie_selection'])
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError('内容选择预览无效') from exc

    def to_dict(self):
        return json.loads(self.payload_json)

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload_json.encode('utf-8')).hexdigest()


def _scope(service, task_id):
    return content.scope_fingerprint(service.task_root, task_id)


def _load(service, task_id, fingerprint):
    plan = service._current_plan(task_id, fingerprint)
    state = service.decision_state_store.load(task_id)
    if plan.metadata.get('decision_state_fingerprint') != service.decision_state_store.fingerprint(state):
        raise ValueError('内容选择的Decision与Plan已变化')
    return plan, state


def validate_scope(service, plan, choice):
    content.require_plan_binding(plan, choice)
    if _scope(service, plan.task_id) != choice.to_dict()['scope_fingerprint']:
        raise ValueError('内容选择的任务来源范围已变化')


def _evidence(value, movie, *, old=None):
    # Exclusion is a user/classifier choice, not a promise about file bytes.
    temporary = content.ContentSelection.from_mapping({**value, 'evidence': []})
    paths = content.omitted_sources(temporary, movie)
    return [{'source_path': str(path), 'kind': 'omission-v1'} for path in sorted(paths, key=str)]


def _preview(service, plan, state, choice, movie, kind, selected=()):
    content.validate_omission_manifest(choice, movie_selection=movie)
    return ContentChangePreview(encode(dict(policy='content-change-preview-v1', kind=kind,
        task_id=plan.task_id, plan_fingerprint=service.plan_store.fingerprint(plan),
        decision_fingerprint=service.decision_state_store.fingerprint(state),
        selection=choice.to_dict(), movie_selection=movie.to_dict() if movie is not None else None,
        selected_entry_ids=list(selected))))












def prepare_change(service, task_id, fingerprint, *, selected_entry_ids, action, reason):
    plan, state = _load(service, task_id, fingerprint)
    from .default_content import require_content_edit
    from ..utils.utils import read_task_at
    require_content_edit(read_task_at(service.task_root, task_id), plan, state)
    choice = content.validate_plan_content(plan, state)
    if choice is None:
        raise ValueError('内容调整缺少当前内容决定')
    content.validate_source_identities(choice, plan=plan)
    if _scope(service, task_id) != choice.to_dict()['scope_fingerprint']:
        raise ValueError('当前任务来源范围已变化')
    return _prepare_change(service, plan, state, fingerprint,
        selected_entry_ids=selected_entry_ids, action=action, reason=reason)


def _prepare_change(service, plan, state, fingerprint, *, selected_entry_ids, action, reason):
    """从同一份当前材料计算人工选择；调用方负责当前任务和来源校验。"""
    choice = state.content_selection
    if choice is None or action not in {'exclude', 'restore'} or not str(reason).strip():
        raise ValueError('内容选择动作、材料或原因无效')
    value = choice.to_dict()
    selectors = tuple(selected_entry_ids)
    if not selectors or len(set(selectors)) != len(selectors):
        raise ValueError('请选取不重复的内容条目')
    by_id = {hashlib.sha256(source_key(row['source_path']).encode('utf-8')).hexdigest()[:20]: row['source_path']
             for row in value['materials']}
    if any(key not in by_id for key in selectors):
        raise ValueError('所选内容不在冻结来源材料内')
    selected = {by_id[key] for key in selectors}
    explicit = set(value['excluded_sources'])
    movie = state.movie_version_selection
    group_by_source = {path: group for group in value['version_groups'] for path in group['sources']}
    affected_refs = {group_by_source[path]['version_ref'] for path in selected if path in group_by_source}
    if movie is not None:
        movie_value = movie.to_dict()
        groups = {g['version_ref']: g for g in movie_value['groups']}
        if action == 'exclude':
            groups = {ref: g for ref, g in groups.items() if ref not in affected_refs}
        else:
            for group in value['version_groups']:
                if group['version_ref'] in affected_refs:
                    groups.setdefault(group['version_ref'], group)
        if not groups:
            raise ValueError('至少保留一个完整电影版本；整个任务无需整理时请安全结束任务')
        kept = {p for group in groups.values() for p in group['sources']}
        movie = MovieVersionSelection.from_mapping({**movie_value, 'groups': list(groups.values()),
            'excluded_sources': sorted(group_by_source.keys() - kept),
            'replacement': None if affected_refs else movie_value['replacement']})
    individual = selected - group_by_source.keys()
    explicit = explicit | individual if action == 'exclude' else explicit - individual
    value.update(excluded_sources=sorted(explicit), parent_plan_fingerprint=fingerprint, reason=str(reason).strip())
    value['evidence'] = _evidence(value, movie, old=choice)
    return _preview(service, plan, state, content.ContentSelection.from_mapping(value), movie, action, selectors)


def _pending_tv_content(plan, state):
    return bool(state.confirmed_work and 'tv_completion_resolution' in plan.metadata
                and {g.kind for g in plan.presentation_groups} == {'tv_episodes'})


def _content_candidate(plan, state, choice, movie):
    candidate = replace(state, content_selection=choice, movie_version_selection=movie)
    if _pending_tv_content(plan, state):
        candidate = replace(candidate, asset_operation=None)
    return candidate


def _draft(service, plan, state, choice, movie, kind):
    from .decision_state import DecisionStateStore
    omitted = content.omitted_sources(choice, movie)
    fingerprint = service.plan_store.fingerprint(plan)
    candidate = _content_candidate(plan, state, choice, movie)
    if _pending_tv_content(plan, state):
        # 内容修订撤销已有目标操作，提交时从当前决定重新计算。
        plan = replace(plan, metadata={k: v for k, v in plan.metadata.items()
                                      if k not in {'asset_operation'}})
    revised = replace(plan, entries=content.material_entries(plan, choice), ignored_sources=tuple(sorted(omitted, key=str)),
        metadata={**plan.metadata, 'content_selection': choice.to_dict(), 'content_selection_fingerprint': choice.fingerprint},
        projection_receipt=None, revision_parent_fingerprint=fingerprint,
        revision_action=f'content_{kind}', revision_reason=choice.to_dict()['reason'])
    revised = DecisionStateStore.project_plan(candidate, revised)
    if movie is not None:
        service._validate_movie_labels(revised)
    return revised


def commit(service, preview):
    if not isinstance(preview, ContentChangePreview):
        raise ValueError('只能确认冻结的内容选择预览')
    value = preview.to_dict()
    plan, state = _load(service, value['task_id'], value['plan_fingerprint'])
    if service.decision_state_store.fingerprint(state) != value['decision_fingerprint']:
        raise ValueError('内容决定已变化，请重新预览')
    if value['kind'] in {'exclude', 'restore'} and _pending_tv_content(plan, state):
        return service.revise_tv_content(plan.task_id, value['plan_fingerprint'],
            expected_revision=state.revision, mode='content', edit=value)
    choice = content.ContentSelection.from_mapping(value['selection'])
    movie = MovieVersionSelection.from_mapping(value['movie_selection']) if value['movie_selection'] is not None else None
    if state.content_selection is None:
        raise ValueError('内容调整缺少当前来源决定')
    current_preview = prepare_change(service, plan.task_id, value['plan_fingerprint'],
        selected_entry_ids=value['selected_entry_ids'], action=value['kind'], reason=choice.to_dict()['reason'])
    if current_preview != preview:
        raise ValueError('内容来源证据或预览选择已变化，请重新核对')
    if _scope(service, plan.task_id) != choice.to_dict()['scope_fingerprint']:
        raise ValueError('任务来源范围已变化')
    if state.content_selection is not None:
        content.validate_omission_manifest(state.content_selection, movie_selection=state.movie_version_selection)
    content.validate_source_identities(choice, plan=plan)
    content.validate_omission_manifest(choice, movie_selection=movie)
    candidate = _content_candidate(plan, state, choice, movie)
    draft = _draft(service, plan, state, choice, movie, value['kind'])
    from .decision_state import DecisionCommandService
    _, projected = DecisionCommandService(service.decision_state_store, service.plan_store).update(
        plan.task_id, expected_revision=state.revision,
        changes={'content_selection': choice, 'movie_version_selection': movie,
                 'asset_operation': candidate.asset_operation, 'tv_planning_authority': candidate.tv_planning_authority},
        planner=lambda _: draft, task_terminal=service._revision_task_terminal(plan.task_id))
    return service._record_revision(projected)


def subtitle_edit_view(plan, *, selection=None):
    """Temporary source-material view, never an accepted output Plan."""
    choice = selection or content.from_plan(plan)
    if choice is None:
        return plan
    from .projection_authority import validate_plan_projection_receipt
    from .content_associations import project_subtitles
    validate_plan_projection_receipt(plan)
    if content.from_plan(plan) is not None:
        content.validate_plan_content(plan)
    entries = content.material_entries(plan, choice)
    return project_subtitles(replace(plan, entries=parse_subtitle_placeholders(entries)), choice.associations)


def parse_subtitle_placeholders(entries):
    """One pure source parser for the edit view and first complete mapping."""
    from .subtitle_workflow import VideoPathMapping, build_subtitle_decisions, decision_to_plan_contract
    placeholders = [entry.source_path for entry in entries if entry.subtitle is not None
                    and entry.subtitle.decision_source == 'inventory_draft'
                    and not entry.subtitle.applied_rule_id]
    parsed = {item.source_subtitle: decision_to_plan_contract(item) for item in build_subtitle_decisions(
        placeholders, tuple(VideoPathMapping(entry.source_path, entry.target_path)
                            for entry in entries if entry.media_kind == 'video'))}
    entries = tuple(replace(entry, subtitle=parsed[entry.source_path])
                    if entry.source_path in parsed else entry for entry in entries)
    return entries


def subtitle_revision_materials_from_store(plan_store, current, rules, proposed_relations, *, selection=None, view=None):
    """Recompute exactly the material/association change allowed by rules."""
    from .content_associations import ContentAssociations, project_subtitles
    from .plan_review import apply_subtitle_rule_entries, entry_id, _subtitle_matcher
    from .plan_verifier import PlanVerifier
    from .rename_plan import SubtitleOverrideScope
    from .projection_authority import validate_plan_projection_receipt
    old = selection or content.from_plan(current)
    if old is None:
        raise ValueError('字幕材料修订缺少内容权威')
    value = old.to_dict()
    previous_rules = current.subtitle_override_rules
    rules = tuple(rules)
    issues = []
    PlanVerifier()._verify_subtitle_rules(replace(current, subtitle_override_rules=rules), issues)
    if issues:
        raise ValueError('字幕规则合同无效：' + ', '.join(issue.code for issue in issues))
    if rules == previous_rules:
        return value['materials'], value['associations']
    view = view or subtitle_edit_view(current, selection=old)
    old_entries = {entry.source_path: entry for entry in view.entries}
    relations = value['associations']
    changed = set()
    from .subtitle_batch import rule_batches
    if len(rules) > len(previous_rules) and rules[:len(previous_rules)] == previous_rules:
        appended = rules[len(previous_rules):]
        if len(appended) > 1 and (any(r.scope != SubtitleOverrideScope.FILE for r in appended)
                or len({r.selected_entry_id for r in appended}) != len(appended)):
            raise ValueError('字幕批次必须逐文件唯一')
        fingerprint = plan_store.fingerprint(current)
        proposed = {row['source_path']: row for row in
            ContentAssociations.from_mapping(proposed_relations).to_dict()['relations']}
        for rule in appended:
            if rule.base_plan_fingerprint != fingerprint:
                raise ValueError('新字幕规则必须绑定实际当前计划')
            selected = [entry for entry in view.entries if entry.subtitle is not None
                        and entry_id(entry) == rule.selected_entry_id]
            if len(selected) != 1 or _subtitle_matcher(selected[0].subtitle) != rule.matcher:
                raise ValueError('字幕规则所选来源或匹配器不属于当前材料')
            owner = None
            if rule.scope == SubtitleOverrideScope.FILE:
                if rule.scope_id != f'{current.task_id}:{rule.selected_entry_id}:{rule.base_plan_fingerprint}':
                    raise ValueError('文件字幕规则作用域与选中来源及当前计划不一致')
                row = proposed.get(str(selected[0].source_path))
                if row is None or row['state'] != 'linked' or len(row['owners']) != 1:
                    raise ValueError('文件字幕规则缺少唯一视频关联')
                owner = Path(row['owners'][0])
            entries = apply_subtitle_rule_entries(view, rule, associated_video_source_path=owner)
            view = replace(view, entries=entries)
            for row in relations['relations']:
                entry = next((entry for entry in entries if str(entry.source_path) == row['source_path']), None)
                if entry is not None and entry.subtitle is not None:
                    before = old_entries[entry.source_path].subtitle
                    after_owner = entry.subtitle.associated_video_source_path
                    if before is None or before.associated_video_source_path != after_owner:
                        row.update(owners=[str(after_owner)] if after_owner is not None else [],
                            state='linked' if after_owner is not None else 'unresolved', source='manual')
        changed = {str(entry.source_path) for entry in entries
                   if entry.subtitle is not None and entry.subtitle != old_entries[entry.source_path].subtitle}
    elif previous_rules and rules == previous_rules[:rule_batches(previous_rules)[-1][0]]:
        removed = previous_rules[-1]
        removed_ids = {r.rule_id for r in previous_rules[len(rules):]}
        base = plan_store.load_version(current.task_id, removed.base_plan_fingerprint)
        validate_plan_projection_receipt(base)
        if base.subtitle_override_rules != rules:
            raise ValueError('撤销字幕规则的基准与保留规则前缀不一致')
        base_view = subtitle_edit_view(base)
        baseline = {entry.source_path: entry for entry in base_view.entries}
        base_choice = content.from_plan(base)
        if base_choice is not None:
            base_relations = {row['source_path']: row for row in base_choice.associations.to_dict()['relations']}
        else:
            base_relations = {row['source_path']: row for row in observe_content_associations(base.entries,
                task_id=base.task_id, source_root=base.source_root).to_dict()['relations']}
        restored = set()
        entries = []
        for entry in view.entries:
            if entry.subtitle is not None and entry.subtitle.applied_rule_id in removed_ids:
                original = baseline.get(entry.source_path)
                if original is None or original.subtitle is None or str(entry.source_path) not in base_relations:
                    raise ValueError('原规则基准缺少完整字幕材料或关联')
                entry = replace(entry, subtitle=original.subtitle, collision_resolution=None)
                restored.add(str(entry.source_path))
            entries.append(entry)
        relations['relations'] = [base_relations[row['source_path']] if row['source_path'] in restored else row
            for row in relations['relations']]
        changed = restored
    else:
        raise ValueError('只允许追加字幕批次或撤销最后一批，旧规则前缀不能改写')
    associations = ContentAssociations.from_mapping(relations)
    normalized = project_subtitles(replace(view, entries=tuple(entries)), associations)
    subtitles = {str(entry.source_path): entry for entry in normalized.entries if entry.subtitle is not None}
    materials = [content.source_material(subtitles[row['source_path']])
        if row['source_path'] in subtitles and row['source_path'] in changed else row for row in value['materials']]
    return materials, associations.to_dict()


def validate_subtitle_contracts(draft, choice):
    value = choice.to_dict()
    # Never use material_entries(draft) here: that adapter intentionally keeps
    # draft-visible entries. Build the expected contracts from frozen source
    # facts alone, normalize through the one association projector, and reject
    # a contradictory draft instead of silently correcting its visible output.
    from .rename_plan import PlanEntry
    from .content_associations import project_subtitles
    source_entries = tuple(PlanEntry.from_dict({**row, 'target_path': row['source_path'],
        'collision_resolution': None}) for row in value['materials'])
    expected_view = project_subtitles(replace(draft, entries=source_entries), choice.associations)
    contracts = {entry.source_path: entry.subtitle for entry in expected_view.entries
        if entry.media_kind == 'subtitle' and entry.subtitle is not None}
    for entry in draft.entries:
        if entry.source_path in contracts or entry.media_kind == 'subtitle' or entry.subtitle is not None:
            if (entry.media_kind != 'subtitle' or entry.source_path not in contracts
                    or entry.subtitle != contracts[entry.source_path]):
                raise ValueError('实际字幕条目与本次冻结材料及关联不一致')
    from .plan_verifier import PlanVerifier
    issues = PlanVerifier().verify_subtitle_rule_materials(draft)
    if issues:
        raise ValueError('字幕材料规则来源无效：' + ', '.join(issue.code for issue in issues))
    return value['materials'], value['associations']


def unfinished_edit_draft(service, current, state, choice, rules, *, action, reason):
    """Rebuild editable fields from actual source facts, never caller targets."""
    from .decision_state import DecisionStateStore
    from .rename_plan import PlanEntry
    from .subtitle_workflow import format_subtitle_target_fields
    from .plan_review import _is_within
    before = {row['source_path']: row for row in state.content_selection.to_dict()['materials']}
    after = {row['source_path']: row for row in choice.to_dict()['materials']}
    entries = content.material_entries(current, state.content_selection)
    by_source = {entry.source_path: entry for entry in entries}
    revised = []
    for entry in entries:
        row = after[str(entry.source_path)]
        if row != before[str(entry.source_path)]:
            if entry.subtitle is None:
                raise ValueError('未完成编辑不能改写非字幕材料')
            frozen = PlanEntry.from_dict({**row, 'target_path': row['source_path'], 'collision_resolution': None})
            contract = frozen.subtitle
            owner = by_source.get(contract.associated_video_source_path) if contract else None
            target = entry.target_path
            if owner is not None and contract.primary_language:
                target = format_subtitle_target_fields(target_video=owner.target_path,
                    source_subtitle=entry.source_path, primary_language=contract.primary_language,
                    script=contract.script, track_title_tokens=contract.track_title_tokens, flags=contract.flags)
                root = Path(str(current.metadata['target_root']))
                if owner.media_kind != 'video' or not _is_within(target, root):
                    raise ValueError('字幕目标不在实际视频的合法目标范围内')
            elif contract is not None and contract.applied_rule_id:
                raise ValueError('人工字幕规则缺少当前视频或语言')
            entry = replace(entry, subtitle=contract, target_path=target, collision_resolution=None,
                requires_review=frozen.requires_review, evidence=frozen.evidence)
        revised.append(entry)
    draft = replace(current, entries=tuple(revised), subtitle_override_rules=tuple(rules),
        ignored_sources=tuple(sorted(content.omitted_sources(choice, None), key=str)),
        metadata={**current.metadata, 'content_selection': choice.to_dict(), 'content_selection_fingerprint': choice.fingerprint},
        projection_receipt=None, revision_parent_fingerprint=service.plan_store.fingerprint(current),
        revision_action=action, revision_reason=reason)
    return DecisionStateStore.project_plan(replace(state, content_selection=choice), draft)


def validate_unfinished_edit(service, current, state, candidate, draft, *, content_only=False):
    """Shared edit gate; leaves the complete execution report untouched."""
    from .default_content import unfinished_source_edit
    if not unfinished_source_edit(service, current, state, content_only=content_only):
        raise ValueError('当前计划不是未完成来源编辑')
    if replace(candidate, content_selection=state.content_selection) != state:
        raise ValueError('未完成来源编辑只能修订内容或字幕，不能隐式完成其它决定')
    choice = candidate.content_selection
    content.validate_source_identities(choice, plan=current)
    content.validate_omission_manifest(choice, movie_selection=None)
    expected = unfinished_edit_draft(service, current, state, choice, draft.subtitle_override_rules,
        action=draft.revision_action, reason=draft.revision_reason)
    if draft != expected and not (draft == current and candidate == state):
        raise ValueError('未完成编辑的实际Plan、字幕目标或未选条目与唯一重建不一致')


def validate_commit(service, envelope, *, automatic_publication=None, correction_result=None, pending_tv_publication=None, current_plan=None):
    """Inside the canonical projection lock, before any journal write."""
    previous, candidate = envelope.previous_decision, envelope.decision
    old = previous.content_selection if previous is not None else None
    choice = candidate.content_selection
    from .inherited_subtitle_edits import require_unchanged, InheritanceWriteCheck
    checked = require_unchanged(old, choice)
    if type(checked) is not InheritanceWriteCheck or checked.previous is not old or checked.candidate is not choice:
        raise ValueError('有限字幕普通提交保护已断开')
    from .public_revision import validate_publication
    publication = validate_publication(service, envelope, current_plan=current_plan)
    if publication is not None:
        if envelope.initial_tv_completion is not None:
            from .initial_tv_completion import InitialTVCompletion, validate_commit as validate_tv
            if type(validate_tv(service, envelope)) is not InitialTVCompletion:
                raise ValueError('TV提交校验已断开')
        elif envelope.movie_change is not None:
            from .movie_content import MovieChange, validate_movie_commit
            if type(validate_movie_commit(service, envelope)) is not MovieChange:
                raise ValueError('电影提交校验已断开')
        return
    if envelope.initial_default is not None:
        from .default_content import validate_first_publication
        validate_first_publication(service, envelope)
        return
    if envelope.initial_movie_completion is not None and (
            old is None or choice is None or old.to_dict()['policy'] != content.DEFAULT_POLICY
            or choice.to_dict()['policy'] != content.DEFAULT_POLICY
            or envelope.initial_movie_selection is not None or envelope.movie_change is not None
            or envelope.media_archive is not None):
        raise ValueError('首次完整电影命令不能缺内容权威或混用其它事务')
    if choice is None:
        if old is not None or envelope.draft_plan.metadata.get('content_selection') is not None:
            raise ValueError('内容选择权威已断开，不能沿旧路径重新签发')
        return
    current_plan = current_plan or service.plan_store.load(candidate.task_id)
    value = choice.to_dict()
    if choice != old and value['parent_plan_fingerprint'] != service.plan_store.fingerprint(current_plan):
        raise ValueError('内容预览的原计划已变化')
    if old is None:
        if value['policy'] == content.DEFAULT_POLICY:
            from .default_content import build_default_initialization, validate_initial_draft, source_only_pending
            expected, movie = build_default_initialization(service, current_plan, previous)
            validate_initial_draft(service, current_plan, previous, expected, movie, envelope.draft_plan)
            if replace(candidate, content_selection=previous.content_selection,
                       movie_version_selection=previous.movie_version_selection) != previous:
                raise ValueError('默认初始化不能顺带更换其它决定')
        else:
            raise ValueError('内容初始化缺少当前来源清单')
        if choice != expected or candidate.movie_version_selection != movie:
            raise ValueError('首次内容迁移与原计划的来源材料、关联或选择不一致')
    else:
        old_value = old.to_dict()
        if value['policy'] != old_value['policy'] or any(value.get(k) != old_value.get(k) for k in content.DEFAULT_FIELDS):
            raise ValueError('内容选择不能替换原策略或默认材料来源')
        if any(value[k] != old_value[k] for k in ('scope_fingerprint', 'origin_plan_fingerprint', 'identities')):
            raise ValueError('内容选择不能悄悄替换原来源身份')
        from .movie_content import validate_ordinary_selection
        if pending_tv_publication is not None:
            from .pending_tv_selection import PendingTVPublication
            if (type(pending_tv_publication) is not PendingTVPublication or envelope.pending_tv_selection is None
                    or pending_tv_publication.decision != candidate or pending_tv_publication.draft != envelope.draft_plan
                    or choice != old):
                raise ValueError('季度待确认必须使用唯一保全重建结果')
        elif correction_result is not None:
            from .content_correction import ContentCorrectionResult
            if (type(correction_result) is not ContentCorrectionResult or envelope.content_correction is None
                    or correction_result.decision != candidate or correction_result.draft != envelope.draft_plan):
                raise ValueError('配套确认必须使用唯一来源重建结果')
        elif automatic_publication is not None:
            from .automatic_recognition import AutomaticPublication
            if (type(automatic_publication) is not AutomaticPublication
                    or automatic_publication.stage not in {'observation', 'movie_identity'}
                    or automatic_publication.decision != candidate or automatic_publication.draft != envelope.draft_plan
                    or choice != old or envelope.automatic_recognition is None):
                raise ValueError('自动观察或作品交接必须保留原内容并使用唯一重建结果')
        elif envelope.initial_movie_completion is not None:
            from .initial_movie_completion import InitialMovieCompletion, validate_commit as validate_initial_completion
            if type(validate_initial_completion(service, envelope)) is not InitialMovieCompletion:
                raise ValueError('首次电影完成共享验证已断开')
        elif (old_value['policy'] == content.DEFAULT_POLICY and not previous.confirmed_work
              and candidate.confirmed_work):
            raise ValueError('v2首次确认作品必须通过实际选择请求交接，不能沿raw旧路签发')
        else:
            validate_ordinary_selection(current_plan, previous, candidate, envelope.draft_plan)
            from .default_content import unfinished_source_edit
            content_only = (envelope.draft_plan.revision_action in {'content_restore', 'content_exclude'}
                and envelope.draft_plan.subtitle_override_rules == current_plan.subtitle_override_rules)
            if unfinished_source_edit(service, current_plan, previous, content_only=content_only):
                validate_unfinished_edit(service, current_plan, previous, candidate, envelope.draft_plan, content_only=content_only)
            _validate_nonmovie_change(service, current_plan, envelope, old, choice)
    if _scope(service, candidate.task_id) != value['scope_fingerprint']:
        raise ValueError('内容选择的任务范围已变化')
    content.validate_source_identities(old if correction_result is not None else choice, plan=current_plan)
    content.validate_omission_manifest(choice, movie_selection=candidate.movie_version_selection)


def _validate_nonmovie_change(service, current_plan, envelope, old, choice):
    value, old_value = choice.to_dict(), old.to_dict()
    if (value['excluded_sources'] != old_value['excluded_sources']
            and _pending_tv_content(current_plan, envelope.previous_decision)):
        raise ValueError('当前TV内容更改必须通过完整TV重建事务')
    from .media_archive import MediaArchiveIntent
    from .movie_version_selection import rebind_selection
    previous_movie = envelope.previous_decision.movie_version_selection
    current_movie = envelope.decision.movie_version_selection
    # A proven rollback starts from an executed task and clears the old
    # replacement intent without changing the user's selection. The canonical
    # archive validator still verifies its journal before any publication.
    rollback_rebind = (
        type(envelope.media_archive) is MediaArchiveIntent
        and envelope.media_archive.action == 'restart_after_rollback'
        and choice == old and previous_movie is not None
        and current_movie == rebind_selection(previous_movie, previous_movie.to_dict()['work_key']))
    if (value['excluded_sources'] != old_value['excluded_sources']
            or current_movie != previous_movie and not rollback_rebind):
        from .default_content import require_content_edit
        from ..utils.utils import read_task_at
        require_content_edit(read_task_at(service.task_root, current_plan.task_id), current_plan, envelope.previous_decision)
    if (value['associations']['sources'] != old_value['associations']['sources']
            or value['version_groups'] != old_value['version_groups']):
        raise ValueError('内容选择不能通过旧命令改写来源种类或完整版本证明')
    if value['materials'] != old_value['materials'] or value['associations'] != old_value['associations']:
        raise ValueError('来源材料和字幕关联的修改必须经过对应公共命令')
    content.validate_omission_manifest(old, movie_selection=envelope.previous_decision.movie_version_selection)


def build_subtitle_revision(service, current, state, command):
    from .content_associations import ContentAssociations
    from .decision_state import DecisionStateStore
    from .rename_plan import PlanEntry
    choice = state.content_selection
    movie = state.movie_version_selection
    if choice is None:
        raise ValueError('字幕调整缺少当前内容决定和冻结配套关系')
    content.validate_omission_manifest(choice, movie_selection=movie)
    view = None
    if command.batch:
        if command.rules or command.owners:
            raise ValueError('批量字幕输入不能混入另一套规则或关联')
        from .subtitle_batch import build_rules
        selected, main, additional, remove = command.batch
        view = subtitle_edit_view(current, selection=choice)
        rules = build_rules(view, command.plan_fingerprint, selected, main_language=main,
            additional_languages=additional, remove_tags=remove)
        command = replace(command, rules=current.subtitle_override_rules + rules)
    value = choice.to_dict()
    relations = value['associations']
    owners = dict(command.owners)
    available = {row['source_path'] for row in value['materials'] if row['media_kind'] == 'subtitle'}
    if len(owners) != len(command.owners) or not owners.keys() <= available:
        raise ValueError('字幕关联来源超出当前材料或重复')
    for row in relations['relations']:
        if row['source_path'] in owners:
            owner = owners[row['source_path']]
            targets = [owner] if owner is not None else []
            if row['owners'] != targets:
                row.update(owners=targets, state='linked' if owner else 'unresolved', source='manual')
    value['materials'], value['associations'] = subtitle_revision_materials_from_store(service.plan_store, current,
        command.rules, relations, selection=choice, view=view)
    value.update(parent_plan_fingerprint=command.plan_fingerprint, reason=command.reason)
    value['evidence'] = _evidence(value, movie, old=choice)
    revised_choice = content.ContentSelection.from_mapping(value)
    candidate = replace(state, content_selection=revised_choice, movie_version_selection=movie)
    frozen = {row['source_path']: PlanEntry.from_dict({**row, 'target_path': row['source_path'],
              'collision_resolution': None}) for row in value['materials']}
    old_rows = {row['source_path']: row for row in choice.to_dict()['materials']}
    changed = {row['source_path'] for row in value['materials'] if row != old_rows.get(row['source_path'])}
    entries = tuple(replace(e, subtitle=frozen[str(e.source_path)].subtitle,
        requires_review=frozen[str(e.source_path)].requires_review, evidence=frozen[str(e.source_path)].evidence)
        if str(e.source_path) in changed else e for e in current.entries)
    revised = replace(current, entries=entries, subtitle_override_rules=command.rules,
        projection_receipt=None, revision_parent_fingerprint=command.plan_fingerprint,
        revision_action=command.action, revision_reason=command.reason,
        metadata={**current.metadata, 'content_selection': revised_choice.to_dict(),
                  'content_selection_fingerprint': revised_choice.fingerprint},
        ignored_sources=tuple(sorted(content.omitted_sources(revised_choice, movie), key=str)))
    from .default_content import unfinished_source_edit
    if unfinished_source_edit(service, current, state):
        revised = unfinished_edit_draft(service, current, state, revised_choice, command.rules,
            action=command.action, reason=command.reason)
    else:
        from .subtitle_batch import verify_edit
        revised = DecisionStateStore.project_plan(candidate, revised)
        before = current if state.content_selection is not None else replace(current,
            metadata={**current.metadata, 'content_selection': choice.to_dict(), 'content_selection_fingerprint': choice.fingerprint,
                      **({'movie_version_selection': movie.to_dict(), 'movie_version_selection_fingerprint': movie.fingerprint} if movie else {})})
        verify_edit(before, revised)
    if validate_subtitle_contracts(revised, revised_choice) != (value['materials'], value['associations']):
        raise ValueError('字幕材料校验已断开')
    return candidate, revised
