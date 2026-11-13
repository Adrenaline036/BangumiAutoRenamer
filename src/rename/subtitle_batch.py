"""当前计划字幕批量决定；文件集合和结果一次冻结。"""
from dataclasses import replace
import uuid

from .rename_plan import SubtitleOverrideRule, SubtitleOverrideScope, SubtitleRuleOutcome
from .rename_plan import UnknownTokenAction, UnknownTokenDisposition
from .subtitle_languages import language_fields, language_key, multilingual_title, language_shaped_token


def rule_batches(rules):
    """同一次修订的规则共享真实base；不得跨组重用base或重复修改文件。"""
    seen, result, start = set(), [], 0
    while start < len(rules):
        base = rules[start].base_plan_fingerprint
        end = start + 1
        while end < len(rules) and rules[end].base_plan_fingerprint == base:
            end += 1
        group = rules[start:end]
        if not base or base in seen:
            raise ValueError('字幕修订基线重复或缺失')
        if len(group) > 1 and (any(r.scope != SubtitleOverrideScope.FILE for r in group)
                or len({r.selected_entry_id for r in group}) != len(group)
                or len({r.reason for r in group}) != 1):
            raise ValueError('字幕批量修订必须逐文件唯一且属于同一次决定')
        seen.add(base)
        result.append((start, end))
        start = end
    return tuple(result)


def make_rule(plan, fingerprint, entry, outcome, scope, reason):
    from .plan_review import entry_id, _subtitle_matcher, _rule_fingerprint
    selected = entry_id(entry)
    scope_id = (f'{plan.task_id}:{selected}:{fingerprint}' if scope == SubtitleOverrideScope.FILE
                else plan.series_scope.scope_id)
    rule = SubtitleOverrideRule(rule_id=uuid.uuid4().hex, scope=scope, scope_id=scope_id,
        base_plan_fingerprint=fingerprint, selected_entry_id=selected,
        matcher=_subtitle_matcher(entry.subtitle), outcome=outcome, reason=reason, rule_fingerprint='')
    return replace(rule, rule_fingerprint=_rule_fingerprint(rule))


def editable_tags(contract):
    from .subtitle_labels import parse_subtitle_label
    return tuple(dict.fromkeys((*contract.unknown_tokens, *contract.track_title_tokens, *contract.flags,
                               *(t.raw for t in parse_subtitle_label(contract.raw_label).tokens))))


def build_batch(plan, fingerprint, selected_entry_ids, **options):
    from .plan_review import apply_subtitle_rule_entries
    rules = build_rules(plan, fingerprint, selected_entry_ids, **options)
    revised = plan
    for rule in rules:
        revised = replace(revised, entries=apply_subtitle_rule_entries(revised, rule))
    return replace(revised, subtitle_override_rules=plan.subtitle_override_rules + rules,
        revision_parent_fingerprint=fingerprint, revision_action='apply_subtitle_batch', revision_reason='调整字幕结构')


def build_rules(plan, fingerprint, selected_entry_ids, *, main_language, additional_languages=(), remove_tags=()):
    from .plan_review import entry_id, _canonical_label
    selected = tuple(selected_entry_ids)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError('请选择字幕文件，不能重复选择同一文件')
    by_id = {entry_id(e): e for e in plan.entries if e.media_kind == 'subtitle' and e.subtitle is not None}
    if any(key not in by_id for key in selected):
        raise ValueError('所选字幕已变化，请重新打开字幕调整')
    from .content_associations import subtitle_pair_map
    pairs = subtitle_pair_map(e.source_path for e in by_id.values())
    paths = {by_id[key].source_path for key in selected}
    paths.update(pairs[p] for p in tuple(paths) if p in pairs)
    selected = tuple(key for key, entry in by_id.items() if entry.source_path in paths)
    main = language_key(main_language)
    additional = tuple(language_key(value) for value in additional_languages)
    title = multilingual_title(main, additional)
    primary, script = language_fields(main)
    remove = {_canonical_label(v) for v in remove_tags if str(v).strip()}
    available = {_canonical_label(tag) for key in selected for tag in editable_tags(by_id[key].subtitle)}
    if remove - available:
        raise ValueError('应删除的标签不在已选字幕中：' + '、'.join(sorted(remove - available)))
    rules = []
    for key in selected:
        entry, contract = by_id[key], by_id[key].subtitle
        old_main = f'zh-{contract.script}' if contract.primary_language == 'zh' and contract.script else contract.primary_language
        try:
            old_title = multilingual_title(old_main, contract.additional_languages)
        except ValueError:
            old_title = ''
        old_titles = {old_title, '日简双语', '日繁双语'}
        titles = tuple(v for v in contract.track_title_tokens
            if v not in old_titles and _canonical_label(v) not in remove and not language_shaped_token(v))
        if title:
            titles += (title,)
        dispositions = tuple(UnknownTokenDisposition(token,
            UnknownTokenAction.DISCARD_MANUAL if _canonical_label(token) in remove or language_shaped_token(token)
            else UnknownTokenAction.PRESERVE_AS_TITLE, '人工调整字幕标签') for token in contract.unknown_tokens)
        outcome = SubtitleRuleOutcome(primary_language=primary, script=script,
            region_hint=contract.region_hint if old_main == main else None,
            additional_languages=additional, track_title_tokens=titles,
            flags=tuple(v for v in contract.flags if _canonical_label(v) not in remove),
            unknown_dispositions=dispositions)
        if contract.associated_video_source_path is None:
            raise ValueError(f'字幕没有唯一对应的视频，请核对源文件名：{entry.source_path.name}')
        rule = make_rule(plan, fingerprint, entry, outcome, SubtitleOverrideScope.FILE, '调整字幕结构')
        rules.append(rule)
    return tuple(rules)


# 仅这些待确认项可随未改变的内容保留；其余错误仍由完整核查拒绝。
CONTENT_PENDING = frozenset({'content_policy_pending', 'provider_special_eligibility_unavailable',
                             'presentation_asset_role_unknown'})
VIDEO_PENDING = 'subtitle_video_not_content_version'


def issue_kind(code):
    if code in CONTENT_PENDING or code == VIDEO_PENDING:
        return 'content'
    if code.startswith('subtitle_association_') or code == 'subtitle_video_entry_not_unique':
        return 'association'
    if code.startswith('subtitle_') or code in {'duplicate_subtitle_semantics', 'multiple_default_subtitles'}:
        return 'subtitle'
    return 'other'


def content_basis(plan):
    """字幕修订可改变的字段之外，内容、作品和路径依据须逐项保持。"""
    from dataclasses import fields
    mutable = {'entries', 'metadata', 'subtitle_override_rules', 'revision_parent_fingerprint',
               'revision_action', 'revision_reason', 'projection_receipt'}
    basis = {f.name: getattr(plan, f.name) for f in fields(plan) if f.name not in mutable}
    basis['entries'] = tuple(e for e in plan.entries if e.media_kind != 'subtitle')
    metadata = dict(plan.metadata)
    for key in ('decision_state_revision', 'decision_state_fingerprint', 'content_selection_fingerprint'):
        metadata.pop(key, None)
    receipt = metadata.get('provider_subject_boundary_selection_receipt')
    if receipt is not None:
        metadata['provider_subject_boundary_selection_receipt'] = {k: v for k, v in receipt.items()
            if k not in {'result_decision_revision', 'result_decision_fingerprint'}}
    raw = metadata.get('content_selection')
    if raw is not None:
        choice = {k: v for k, v in raw.items() if k not in {'parent_plan_fingerprint', 'reason', 'evidence'}}
        choice['materials'] = tuple({k: v for k, v in row.items()
            if row['media_kind'] != 'subtitle' or k not in {'subtitle', 'requires_review', 'evidence'}}
            for row in raw['materials'])
        # 字幕关联的改变另有同源规则核查，其他内容关系不属于字幕编辑。
        subtitles = {row['source_path'] for row in raw['materials'] if row['media_kind'] == 'subtitle'}
        choice['associations'] = {**raw['associations'], 'relations': tuple(
            row for row in raw['associations']['relations'] if row['source_path'] not in subtitles)}
        metadata['content_selection'] = choice
    basis['metadata'] = metadata
    return basis


def verify_edit(current, revised):
    """保存局部字幕决定；其余旧待确认项保留，批准与执行仍完整核查。"""
    from .plan_verifier import PlanVerifier, subtitle_conflicts
    before = {e.source_path: e for e in current.entries}
    unchanged = {e.source_path for e in revised.entries if e == before.get(e.source_path)}
    verifier = PlanVerifier()
    old_issues = verifier.verify(current).issues
    authority_errors = [i.code for i in old_issues if i.code in {
        'projection_receipt_mismatch', 'tv_authority_incomplete'}]
    if authority_errors:
        raise ValueError('原计划权威核查失败：' + ', '.join(authority_errors))
    issues = verifier.verify(revised, validate_projection_receipt=False).issues
    same_content = content_basis(current) == content_basis(revised)
    conflicts = tuple(subtitle_conflicts(revised.entries))
    # 未归属音轨可单独待确认；投影本身失配不能作为旧待确认保留。
    from .audio_projection import audio_entries
    pending_audio = ({e.source_path for e in current.entries if e.role == 'external_audio_unresolved'}
                     if same_content and audio_entries(current, current.entries) == current.entries else set())
    def pending(issue):
        if pending_audio and issue in old_issues and (
                issue.code == 'external_audio_projection_invalid'
                or issue.code == 'presentation_asset_missing' and issue.source_path in pending_audio):
            return True
        kind = issue_kind(issue.code)
        if kind == 'content':
            # 字幕目标标签可改变；视频用途问题的依据是同一来源视频及完整内容basis。
            old = (any((i.code, i.source_path, i.message) == (issue.code, issue.source_path, issue.message)
                       for i in old_issues) if issue.code == VIDEO_PENDING else issue in old_issues)
            return same_content and old
        if issue.source_path not in unchanged or issue not in old_issues:
            return False
        if issue.code.startswith('subtitle_'):
            return True
        if issue.code in {'duplicate_subtitle_semantics', 'multiple_default_subtitles'}:
            return all(a.source_path in unchanged and b.source_path in unchanged
                for _, code, a, b in conflicts if code == issue.code and b.source_path == issue.source_path)
        return False
    blockers = [i for i in issues if not pending(i)]
    if not same_content:
        raise ValueError('字幕调整不能改变视频、作品或内容选择依据')
    if blockers:
        raise ValueError('字幕调整未通过核查：' + ', '.join(i.code for i in blockers))


def subtitle_statuses(plan):
    """从当前核查与人工决定投影灰字，不保存另一套状态。"""
    from . import content_selection as content
    from .plan_verifier import PlanVerifier, subtitle_conflicts, _path_key
    choice = content.from_plan(plan)
    materials = content.material_entries(plan, choice) if choice is not None else plan.entries
    subtitles = {e.source_path: e for e in materials if e.media_kind == 'subtitle' and e.subtitle is not None}
    verifier = PlanVerifier()
    issues = (*verifier.verify(plan, validate_projection_receipt=False).issues,
              *verifier.verify_subtitle_rule_materials(plan))
    invalid_rules, rule_issues = set(), []
    verifier._verify_subtitle_rules(plan, rule_issues, invalid_rule_ids=invalid_rules)
    # 重复指纹同样涉及先出现的规则；与核查的指纹等价关系保持一致。
    fingerprints = {}
    for rule in plan.subtitle_override_rules:
        fingerprints.setdefault(rule.rule_fingerprint, []).append(rule.rule_id)
    for ids in fingerprints.values():
        if len(ids) > 1:
            invalid_rules.update(ids)
    own = {p for p,e in subtitles.items() if e.subtitle.applied_rule_id in invalid_rules}
    video, association = set(), set()
    material_sources = {e.source_path for e in materials if e.media_kind == 'video'}
    association.update(p for p,e in subtitles.items() if e.subtitle.associated_video_source_path not in material_sources)
    for issue in issues:
        kind = issue_kind(issue.code)
        if kind == 'subtitle':
            if issue.source_path is not None:
                own.add(issue.source_path)
            elif issue.code == 'subtitle_inherited_content_invalid':
                own.update(p for p,e in subtitles.items() if e.subtitle.decision_source == 'manual_transfer')
        elif kind == 'association':
            association.add(issue.source_path)
        elif kind == 'content':
            video.add(issue.source_path)
    for _, _, first, second in subtitle_conflicts(plan.entries):
        own.update((first.source_path, second.source_path))
    targets = {}
    for entry in plan.entries:
        targets.setdefault(_path_key(entry.target_path), []).append(entry)
    for entries in targets.values():
        if len(entries) > 1:
            own.update(e.source_path for e in entries if e.subtitle is not None)
    result = {}
    manual = {'manual', 'manual_file', 'manual_series', 'manual_transfer'}
    for path, entry in subtitles.items():
        contract = entry.subtitle
        state = ('待修正' if path in own or contract.decision_source == 'inventory_draft' else
                 '已修正' if contract.decision_source in manual else '无需修正')
        hint = ('对应视频无法确认，需核对源文件' if path in association else
                '关联视频待确认' if path in video or contract.associated_video_source_path in video else '')
        result[path] = state, hint
    return result
