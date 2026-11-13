"""Complete movie edit intent, shared reconstruction, and canonical context."""
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path

from . import content_selection as content
from .content_associations import encode
from .movie_version_selection import MovieVersionSelection, create_selection, project_selection, validate_catalog_labels
from .plan_store import PlanStore


def _key(plan):
    from .movie_version_selection import from_plan
    return from_plan(plan).to_dict()['work_key']


def _catalog(catalog, plan):
    key = _key(plan)
    path = catalog._path(key)
    if not path.exists():
        return 'missing', None
    raw = path.read_bytes()
    payload = json.loads(raw)
    catalog._validate(payload, key)
    if not catalog.compatible_base(payload, plan, allow_explicit_provider_append=True):
        raise ValueError('电影版本编辑的作品登记身份不一致')
    return hashlib.sha256(raw).hexdigest(), payload


@dataclass(frozen=True)
class MovieChangeContext:
    service: object
    plan_store: object
    state_store: object
    task_root: Path
    catalog: object
    catalog_root: Path

    @classmethod
    def from_service(cls, service):
        from .plan_review import PlanReviewService
        if not isinstance(service, PlanReviewService):
            raise ValueError('电影编辑缺少已配置审核服务')
        return cls(service, service.plan_store, service.decision_state_store, Path(service.task_root),
            service.work_catalog, Path(service.work_catalog.root))

    def validate(self, transaction):
        if (type(self).from_service(self.service) != self or self.plan_store is not transaction.plan_store
                or self.state_store is not transaction.state_store or self.task_root != transaction.task_root):
            raise ValueError('电影编辑的服务、来源存储或登记根已变化')

    @contextmanager
    def publication_lock(self, transaction, intent):
        self.validate(transaction)
        if type(intent) is not MovieChangeIntent:
            raise ValueError('电影编辑缺少类型化冻结意图')
        current = self.plan_store.load(intent.task_id)
        key = _key(current)
        # The existing Catalog lock is outside projection, including when
        # the record does not exist yet. No inverse lock acquisition in build.
        with self.catalog.lock(key, read_only=True):
            yield


@dataclass(frozen=True)
class MovieChangeIntent:
    task_id: str
    plan_fingerprint: str
    decision_fingerprint: str
    groups_json: str
    excluded_sources: tuple[str, ...]
    preferred_source: str | None
    replace_version_ref: str | None
    catalog_fingerprint: str


@dataclass(frozen=True)
class MovieChange:
    choice: content.ContentSelection
    movie: MovieVersionSelection
    draft: object


def _groups_input(groups):
    from .rename_plan import VersionDescriptor
    result = []
    for row in groups:
        if not isinstance(row, dict) or set(row) - {'sources', 'label', 'technical_descriptor'}:
            raise ValueError('电影编辑只接收来源、标签及明确的技术规格，不接收自报版本身份')
        item = dict(sources=[str(Path(p)) for p in row['sources']], label=row.get('label'))
        if row.get('technical_descriptor') is not None:
            item['technical_descriptor'] = VersionDescriptor.from_dict(row['technical_descriptor']).to_dict()
        result.append(item)
    return result


def pending_movie_sources(plan, selection):
    """Frozen, unexcluded movie videos which still need an explicit role."""
    if not isinstance(selection, content.ContentSelection):
        raise ValueError('电影候选缺少冻结内容决定')
    value = selection.to_dict()
    movie = MovieVersionSelection.from_mapping(plan.metadata.get('movie_version_selection')).to_dict()
    group_id = movie['group_id']
    if not any(g.group_id == group_id and g.kind == 'movie_continuous' for g in plan.presentation_groups):
        raise ValueError('电影候选缺少对应的呈现组')
    kinds = {row['path']: row['kind'] for row in value['associations']['sources']}
    covered = {p for group in value['version_groups'] for p in group['sources']}
    excluded = set(value['excluded_sources']) | {str(p) for p in plan.ignored_sources}
    expected = {str(p) for p in plan.expected_sources}
    result = []
    for row in value['materials']:
        source, asset = row['source_path'], row.get('presentation_asset')
        if (row['media_kind'] == 'video' and kinds.get(source) == 'video'
                and source in expected and source not in covered | excluded
                and Path(source).is_relative_to(plan.source_root)
                and asset and asset['presentation_group'] == group_id
                and asset['asset_role']['kind'] == 'unknown'):
            result.append(source)
    return tuple(result)


def validate_added_movie_sources(plan, selection, rows):
    """Shared by ordinary editing and an existing movie's affiliation edit."""
    old_groups = selection.to_dict()['version_groups']
    old_members = {p for group in old_groups for p in group['sources']}
    added = {p for row in rows for p in row['sources']} - old_members
    if not added:
        return  # Existing explicit label/segment editing keeps its contract.
    if not added <= set(pending_movie_sources(plan, selection)):
        raise ValueError('新增正片必须是本任务内尚待确认、且已纳入整理的视频')
    original_groups = {tuple(group['sources']) for group in old_groups}
    if any(old_members.intersection(row['sources']) and tuple(row['sources']) not in original_groups for row in rows):
        raise ValueError('新增正片请独立成组；先保存后再调整已有版本的连续分段')


def prepare(service, task_id, fingerprint, *, groups, excluded_sources=(), preferred_source=None,
            replace_version_ref=None):
    from .content_choice import _load
    plan, state = _load(service, task_id, fingerprint)
    catalog_fingerprint, payload = _catalog(service.work_catalog, plan)
    intent = MovieChangeIntent(task_id, fingerprint, service.decision_state_store.fingerprint(state),
        encode(_groups_input(groups)), tuple(sorted(str(Path(p)) for p in excluded_sources)),
        str(Path(preferred_source)) if preferred_source is not None else None,
        replace_version_ref, catalog_fingerprint)
    return intent


def build_movie_change(service, current, state, intent, catalog):
    """One reconstruction for public preparation and locked canonical checks."""
    from .decision_state import DecisionStateStore
    from .content_choice import _evidence
    from .projection_authority import validate_plan_projection_receipt
    if (type(intent) is not MovieChangeIntent or intent.task_id != current.task_id
            or intent.plan_fingerprint != PlanStore.fingerprint(current)
            or intent.decision_fingerprint != DecisionStateStore.fingerprint(state)):
        raise ValueError('电影版本预览的当前计划或决定已变化')
    validate_plan_projection_receipt(current)
    old = content.validate_plan_content(current, state)
    prior_movie = state.movie_version_selection
    if old is None:
        raise ValueError('电影编辑缺少当前内容决定和冻结配套关系')
    if state.movie_version_selection is None:
        raise ValueError('电影版本编辑需要当前完整内容决定')
    content.validate_source_identities(old, plan=current)
    content.validate_omission_manifest(old, movie_selection=prior_movie)
    value, movie_value = old.to_dict(), prior_movie.to_dict()
    if content.scope_fingerprint(service.task_root, current.task_id) != value['scope_fingerprint']:
        raise ValueError('电影来源范围已变化')
    rows = _groups_input(json.loads(intent.groups_json))
    if encode(rows) != intent.groups_json:
        raise ValueError('电影编辑输入并非冻结规范结构')
    original_members = {p for g in value['version_groups'] for p in g['sources']}
    members = [p for g in rows for p in g['sources']]
    excluded = set(intent.excluded_sources)
    validate_added_movie_sources(current, old, rows)
    if (not original_members or len(members) != len(set(members)) or not original_members <= set(members)
            or len(excluded) != len(intent.excluded_sources) or not excluded <= original_members
            or any(not row['sources'] or bool(excluded.intersection(row['sources']))
                and not set(row['sources']) <= excluded for row in rows)):
        raise ValueError('编辑必须完整覆盖原电影版本；不能重复、遗漏或只排除半个版本')
    kept_rows = [row for row in rows if not set(row['sources']) <= excluded]
    if not kept_rows:
        raise ValueError('至少保留一个完整电影版本')
    view = replace(current, entries=content.material_entries(current, old))
    old_full = MovieVersionSelection.from_mapping({**movie_value, 'groups': value['version_groups'],
        'excluded_sources': [], 'replacement': None})
    full = create_selection(view, rows, associations=old.associations, registered_versions=catalog['versions'] if catalog else (), previous=old_full)
    full_value = full.to_dict()
    from .movie_version_selection import replacement_from_catalog
    replacement = replacement_from_catalog(catalog, intent.replace_version_ref, kept_groups=kept_rows)
    if replacement is not None:
        replaced_group = create_selection(view, kept_rows, associations=old.associations, registered_versions=catalog['versions'] if catalog else (), previous=old_full, replacement=replacement).to_dict()['groups'][0]
        full_value['groups'] = [replaced_group if set(g['sources']) == set(kept_rows[0]['sources']) else g
            for g in full_value['groups']]
        full = MovieVersionSelection.from_mapping(full_value)
    selected_groups = [g for g in full_value['groups'] if not set(g['sources']) <= excluded]
    movie = MovieVersionSelection.from_mapping({**full_value, 'groups': selected_groups,
        'excluded_sources': sorted(excluded), 'replacement': replacement})
    if catalog is not None:
        validate_catalog_labels(movie, catalog['versions'])
    new_ref_by_source = {p: g['version_ref'] for g in full_value['groups'] for p in g['sources']}
    ref_map = {g['version_ref']: {new_ref_by_source[p] for p in g['sources']} for g in value['version_groups']}
    relations = {r['source_path']: r for r in value['associations']['relations']}
    owned_entries = []
    for entry in view.entries:
        owner = entry.presentation_asset.owner_version_ref if entry.presentation_asset else None
        if owner not in {None, 'shared'}:
            targets = ref_map.get(owner, set())
            relation = relations.get(str(entry.source_path), {})
            # A frozen one-video association resolves paired subtitles/audio
            # even when their former complete version has now been split.
            owners = relation.get('owners', ())
            if len(targets) != 1 and len(owners) == 1 and owners[0] in new_ref_by_source:
                targets = {new_ref_by_source[owners[0]]}
            if len(targets) != 1:
                raise ValueError('重新分组后特典所属版本存在歧义，请重新选择所属版本')
            entry = replace(entry, presentation_asset=replace(entry.presentation_asset,
                owner_version_ref=next(iter(targets))))
        owned_entries.append(entry)
    view = replace(view, entries=tuple(owned_entries))
    group = next(g for g in current.presentation_groups if g.group_id == movie_value['group_id'])
    preferred = group.preferred_version_ref
    if intent.preferred_source is not None:
        preferred = next((g['version_ref'] for g in selected_groups if intent.preferred_source in g['sources']), None)
        if preferred is None:
            raise ValueError('偏好版本必须属于本次保留的完整版本组')
    elif preferred is not None:
        targets = ref_map.get(preferred, set())
        if len(targets) != 1:
            raise ValueError('重新分组后偏好版本存在歧义，请重新选择偏好版本')
        preferred = next(iter(targets))
    if preferred is not None and preferred not in {g['version_ref'] for g in selected_groups}:
        raise ValueError('原偏好版本未保留，请重新选择偏好版本')
    full_view = project_selection(view, full)
    projected = {str(e.source_path): e for e in full_view.entries}
    materials = [{**row, 'role': projected[row['source_path']].role,
        'presentation_asset': projected[row['source_path']].presentation_asset.to_dict()
        if projected[row['source_path']].presentation_asset else None}
        if row['source_path'] in projected else row for row in value['materials']]
    value.update(materials=materials, version_groups=full_value['groups'],
        parent_plan_fingerprint=intent.plan_fingerprint, reason='用户确认完整电影版本组、标签与保留范围')
    value['evidence'] = _evidence(value, movie, old=old)
    choice = content.ContentSelection.from_mapping(value)
    draft = replace(full_view, ignored_sources=tuple(sorted(content.omitted_sources(choice, movie), key=str)),
        projection_receipt=None, revision_parent_fingerprint=intent.plan_fingerprint,
        revision_action='content_movie_versions', revision_reason=value['reason'],
        presentation_groups=tuple(replace(g, preferred_version_ref=preferred)
            if g.group_id == movie_value['group_id'] else g for g in full_view.presentation_groups),
        metadata={**full_view.metadata, 'content_selection': choice.to_dict(),
            'content_selection_fingerprint': choice.fingerprint})
    candidate = replace(state, content_selection=choice, movie_version_selection=movie, asset_operation=None)
    draft = DecisionStateStore.project_plan(candidate, draft)
    return MovieChange(choice, movie, draft)


def commit(service, intent):
    from .public_revision import commit as publish
    _, projected = publish(service, intent, task_terminal=service._revision_task_terminal(intent.task_id))
    return service._record_revision(projected)


def validate_movie_commit(transaction, envelope):
    from .public_revision import require_publication
    result = require_publication(transaction, envelope)
    if type(result.built) is not MovieChange or envelope.movie_change != result.command:
        raise ValueError('电影提交未使用锁内唯一生成结果')
    return result.built


def validate_ordinary_selection(current, previous, candidate, draft):
    """Selection-only revisions may consume, never rewrite, complete rows."""
    old = previous.movie_version_selection
    new = candidate.movie_version_selection
    if old is None and new is None:
        return
    if old is None or new is None:
        raise ValueError('电影版本权威不能通过普通内容命令替换')
    before, after = old.to_dict(), new.to_dict()
    proof = {g['version_ref']: g for g in candidate.content_selection.to_dict()['version_groups']}
    if (any(g != proof.get(g['version_ref']) for g in after['groups'])
            or any(after[k] != before[k] for k in ('policy', 'task_id', 'work_key', 'group_id'))
            or after['replacement'] not in (None, before['replacement'])
            or draft.presentation_groups != current.presentation_groups):
        raise ValueError('完整电影版本或引用变化必须通过统一版本编辑意图')
