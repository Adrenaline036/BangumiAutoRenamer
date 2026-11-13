"""Small asset-choice forms with explicit UI and transaction dependencies."""
from ..queue_runtime import ui_disk_call

from .tv_content_actions import await_result

from .progress_view import bar_progress

from src.component.dialog_frame import dialog_card
from pathlib import Path
from collections.abc import Mapping
from ..rename.plan_store import PlanStore
from ..rename.plan_review import PlanReviewError
from ..rename.decision_state import DecisionStateError
from .exceptional_episode_review import exceptional_entry_ids
from ..rename.provider_selection_authority import is_confirmed_movie_plan

INITIAL_MOVIE_REPLACEMENT_HELP = ('已有相同版本标签。要替换旧版本，请明确选择“已有版本换源”；'
    '要同时保留，请为新增版本填写不同标签。')




def visible_role_rows(plan, rows):
    covered = exceptional_entry_ids(plan, rows)
    return [{**row, 'issue_codes': [code for code in row.get('issue_codes', ())
                                  if not (row.get('entry_id') in covered and code == 'presentation_asset_role_unknown')]}
            for row in rows]


def visible_plan_issues(plan, issues, *, rows=()):
    """Collapse only a known pending-choice cascade; approval sees every issue."""
    covered = exceptional_entry_ids(plan, rows)
    covered_sources = {str(row.get('source')) for row in rows if row.get('entry_id') in covered}
    return [i for i in issues if not (
        i.get('code') == 'presentation_asset_role_unknown' and str(i.get('source')) in covered_sources)]


def is_initial_movie_selection(plan):
    return bool(plan.metadata.get('content_selection')
                and not plan.metadata.get('movie_version_selection')
                and plan.metadata.get('mapping_source') == 'inventory_review_draft')


def render_movie_choices(dialog, group, selected_kind, video_options, selected_values, *, ui, edit_versions):
    """Movie content choices only; work identity owns the media type."""
    primary = dialog.plan.work_identity.primary_provider if dialog.plan.work_identity else None
    if not is_confirmed_movie_plan(dialog.plan):
        return
    if dialog.plan.metadata.get('content_selection') is not None:
        ui.button('选择电影正片', icon='o_edit',
            on_click=lambda: edit_versions(dialog, group.group_id)).props('outline color=blue-7 no-caps')
        return  # One form owns first completion, segments and later versions.
    dialog._render_movie_primary_picker(video_options, selected_values)
    with ui.row().classes('bar-completion-choice-row w-full gap-3 flex-wrap'):
        ui.button('按所选视频生成电影正片版本', icon='o_movie',
            on_click=lambda: dialog._complete_movie_with_primary(group.group_id)).props('outline color=blue-7 no-caps').classes('bar-completion-choice')
        ui.button('电影正片版本编辑', icon='o_edit',
            on_click=lambda: edit_versions(dialog, group.group_id)).props('outline color=blue-7 no-caps').classes('bar-completion-choice')


def show_movie_versions(self, group_id: str, *, ui, notify, complete_movie,
                        replacement_only=False, work_key=None) -> None:
    """Choose task videos first; only selected versions have editable labels."""
    import asyncio
    from ..rename.movie_version_selection import safe_label
    if getattr(self, '_movie_version_worker', None) is not None:
        notify('电影版本操作仍在后台处理，请等待原操作结果后刷新。', type='info')
        return
    status = {'busy': False, 'closed': False}
    form_fingerprint = PlanStore.fingerprint(self.plan)
    options = self._video_options()
    frozen = self.plan.metadata.get('movie_version_selection') or {}
    content_choice = self.plan.metadata.get('content_selection')
    initial_content = is_initial_movie_selection(self.plan)
    existing = list(content_choice['version_groups'] if content_choice else frozen.get('groups', ()))
    covered = {p for group in existing for p in group['sources']}
    rows = [dict(group) for group in existing]
    pending_sources = set()
    if content_choice and not initial_content:
        from ..rename.content_selection import from_plan
        from ..rename.movie_content import pending_movie_sources
        pending_sources = set(pending_movie_sources(self.plan, from_plan(self.plan)))
    for source in options:
        if content_choice and not initial_content and source not in pending_sources:
            continue
        if initial_content and source in content_choice['excluded_sources']:
            continue
        if source not in covered:
            entry = next((e for e in self.plan.entries if str(e.source_path) == source), None)
            if entry and entry.presentation_asset and entry.presentation_asset.asset_role.kind == 'extra':
                continue  # Declared CM/NCOP/etc remain part of their owner group.
            if initial_content:
                from ..rename.media_classifier import observe_watchable_content
                if observe_watchable_content(Path(source), source_root=self.plan.source_root,
                        work_title=self.plan.work_identity.title if self.plan.work_identity else '').disposition == 'known_non_watchable':
                    continue  # Explicitly restored trailers stay extras, not movie versions.
            descriptor = entry.presentation_asset.version_descriptor if entry and entry.presentation_asset else None
            if descriptor is None and initial_content:
                from ..rename.plan_review import _infer_movie_version_descriptor
                descriptor = _infer_movie_version_descriptor(Path(source), group_id, self.plan.source_root)
            rows.append({'sources': [source], 'label': descriptor.display_label if descriptor else Path(source).stem})
    registered = {}
    key = str(work_key or self.plan.metadata.get('catalog_work_key') or self.plan.metadata.get('proposed_work_key') or '')
    if not key and self.plan.work_identity is not None:
        match = self.review_service.work_catalog.find_exact(self.plan.work_identity.primary_provider,
                                                            group_kind='movie_continuous')
        key = match.work_key if match is not None else ''
    if key and self.review_service.work_catalog._path(key).exists():
        for row in self.review_service.work_catalog.load(key).get('versions', ()):
            if str(row.get('version_ref', '')).startswith('mv-'):
                registered[row['version_ref']] = str(row.get('display_label') or '默认版本')
    if replacement_only and not registered:
        notify('此作品没有可换源的已登记电影版本。', type='warning')
        return
    rows_by_key = {row['sources'][0]: row for row in rows}
    label_values = {key: row.get('label') or '默认版本' for key, row in rows_by_key.items()}
    label_inputs = {}
    selected = (set() if initial_content else
                {p for g in frozen.get('groups', ()) for p in g['sources']} if content_choice else
                set(self._selected_movie_primary_sources()))
    with ui.dialog() as dialog, dialog_card().classes('scroll bar-dialog bar-confirm-card w-[800px] max-w-full'):
        ui.label('选择新来源与要替换的版本' if replacement_only else
                 '选择电影正片' if initial_content else '电影正片版本编辑').classes('text-h6')
        picker_options = {key: ' / '.join(options.get(p, Path(p).name) for p in row['sources'])
                          + ('（连续分段）' if len(row['sources']) > 1 else '')
                          for key, row in rows_by_key.items()}
        picker = ui.select(picker_options, multiple=True,
            value=[key for key, row in rows_by_key.items() if selected.intersection(row['sources'])],
            label='电影正片文件（可多选）').props('outlined use-chips options-dense').classes('w-full')
        # Reposition after the menu portal has mounted, including its first open.
        picker.on('popup-show', js_handler=
            f"() => requestAnimationFrame(() => runMethod({picker.id}, 'updateMenuPosition', []))")
        label_area = ui.column().classes('w-full gap-3')
        def render_selected_labels():
            label_values.update({key: field.value for key, field in label_inputs.items()})
            label_inputs.clear()
            label_area.clear()
            with label_area:
                for key in rows_by_key:
                    if key in picker.value:
                        ui.label(picker_options[key]).classes('bar-path-text')
                        label_inputs[key] = ui.input('版本标签', value=label_values[key]).props(
                            'outlined dense').classes('w-full')
                if any(len(rows_by_key[key]['sources']) > 1 for key in picker.value):
                    ui.label('已有连续分段作为一组选择，组内文件使用同一标签。').classes('bar-help')
        picker.on_value_change(render_selected_labels)
        render_selected_labels()
        continuous = ui.checkbox('将保留的视频按上述顺序视为同一版本的连续分段').props('color=blue-7')
        current_replacement = (frozen.get('replacement') or {}).get('version_ref', '')
        if replacement_only and current_replacement not in registered:
            current_replacement = next(iter(registered)) if len(registered) == 1 else None
        replacement = ui.select(registered if replacement_only else {'': '新增独立版本', **registered},
            value=current_replacement, label='已有版本换源').props(
            'outlined options-dense').classes('w-full')
        if replacement_only:
            ui.label('只替换选中的完整版本及其专属配套；其他版本和共享文件保留。').classes('bar-help')
        elif registered:
            ui.label(INITIAL_MOVIE_REPLACEMENT_HELP).classes('bar-help')
        message = ui.label('').classes('bar-help text-negative')
        progress = bar_progress(ui)
        progress.set_visibility(False)
        def alive():
            return (not status['closed'] and not dialog.is_deleted
                    and not getattr(self, 'is_deleted', False) and not getattr(self, 'exit_requested', False)
                    and bool(getattr(self, 'value', True)))
        def set_busy(value):
            status['busy'] = value
            for widget in (picker, *label_inputs.values(), continuous, replacement, save_button, cancel_button):
                widget.disable() if value else widget.enable()
            progress.set_visibility(value)
            dialog.props('persistent') if value else dialog.props(remove='persistent')
        def close():
            if status['busy']:
                message.set_text('已提交的版本操作仍在后台处理，关闭不会撤销写入，请等待结果。')
                return
            status['closed'] = True
            dialog.close()
        def disconnected():
            status['closed'] = True
        async def save():
            if not alive() or status['busy'] or getattr(self, '_movie_version_worker', None) is not None:
                return
            set_busy(True)
            try:
                if PlanStore.fingerprint(self.plan) != form_fingerprint:
                    raise ValueError('计划已在此窗口打开后更新，请关闭并重新打开版本窗口；旧表单不会覆盖后来选择')
                chosen = set(picker.value or ())
                if not chosen <= rows_by_key.keys():
                    raise ValueError('请选择本任务中的正片文件')
                kept_groups, hidden_groups = [], []
                for key, row in rows_by_key.items():
                    if key in chosen:
                        kept_groups.append(dict(sources=list(row['sources']), label=safe_label(label_inputs[key].value),
                            **({'technical_descriptor': row['descriptor']} if content_choice and not initial_content and 'descriptor' in row else {})))
                    elif content_choice and not initial_content and key in covered:
                        # The existing full registry supports restoring a version;
                        # its unselected label is not an editable form field.
                        hidden_groups.append(dict(sources=list(row['sources']), label=row['label'],
                            technical_descriptor=row['descriptor']))
                excluded = [p for key, row in rows_by_key.items() if key not in chosen and key not in pending_sources for p in row['sources']]
                if not kept_groups:
                    raise ValueError('请至少保留一个完整版本')
                if continuous.value:
                    kept_groups = [dict(sources=[p for g in kept_groups for p in g['sources']], label=kept_groups[0]['label'])]
                groups = kept_groups + hidden_groups if content_choice and not initial_content else kept_groups
                if replacement.value and len(kept_groups) != 1:
                    raise ValueError('换源时只可保留一个版本组')
                if replacement_only and replacement.value not in registered:
                    raise ValueError('请选择要替换的已登记版本')
                if not initial_content and self.plan.metadata.get('mapping_source') == 'inventory_review_draft':
                    self.movie_primary_video_input.value = [p for g in groups for p in g['sources']]
                    await await_result(complete_movie(self, group_id, version_groups=groups,
                                                                excluded_sources=excluded))
                    if self.plan.metadata.get('mapping_source') == 'inventory_review_draft':
                        raise ValueError('未能生成电影计划，请核对错误提示')
                service = self.review_service
                task_id, expected = self.plan.task_id, PlanStore.fingerprint(self.plan)
                replacement_ref = replacement.value or None
                combine_affiliation = work_key is not None and (initial_content or work_key != self.plan.metadata.get('catalog_work_key'))
            except (ValueError, RuntimeError, OSError) as exc:
                message.set_text(str(exc))
                set_busy(False)
                return
            message.set_text('正在后台核对来源并保存版本，界面可响应；请勿重复提交……')
            def operation():
                if combine_affiliation:
                    from ..rename.work_affiliation import prepare_existing, commit_choice
                    intent = prepare_existing(service, task_id, expected, key=work_key,
                        groups=kept_groups, excluded_sources=excluded, replace_version_ref=replacement_ref,
                        reason='明确确认电影归属、完整版本与换源范围')
                    revised = commit_choice(service, intent)
                elif initial_content:
                    intent = service.prepare_initial_movie_completion(task_id, expected,
                        groups=groups, excluded_sources=excluded, replace_version_ref=replacement_ref,
                        reason='明确确认首次电影完整版本、分段与保留范围')
                    revised = service.commit_initial_movie_completion(intent)
                else:
                    revised = service.revise_movie_versions(task_id, expected,
                        groups=groups, excluded_sources=excluded, replace_version_ref=replacement_ref)
                current = service.plan_store.load(task_id)
                state = service.decision_state_store.load(task_id)
                if (PlanStore.fingerprint(current) != PlanStore.fingerprint(revised)
                        or current.metadata.get('decision_state_fingerprint') != service.decision_state_store.fingerprint(state)):
                    raise ValueError('保存后任务已有更新，请刷新当前计划；不会覆盖后来的决定')
                return revised, state
            # Own the real worker separately from the UI waiter. Cancelling a
            # waiter cannot stop a thread or establish that no write occurred.
            worker = asyncio.create_task(ui_disk_call(operation))
            self._movie_version_worker = worker
            async def finish(done):
                if done is not getattr(self, '_movie_version_worker', None):
                    return
                if done.cancelled():
                    if alive():
                        message.set_text('后台终态尚未确认，保留操作锁定；请刷新核查，不要重复提交。')
                    return
                try:
                    revised, state = done.result()
                    if alive():
                        if PlanStore.fingerprint(self.plan) != expected:
                            raise ValueError('界面已有后来修订，请刷新当前结果；不会覆盖后来选择')
                        self.task_decision = state
                        await await_result(self._adopt_revision(revised))
                        with dialog:
                            notify('版本与排除范围已保存；换源仍需确认文件差异并重新批准' if replacement_ref is not None
                                   else '版本与排除范围已保存，请核对整理计划', type='positive')
                        status['closed'] = True
                        dialog.close()
                        if replacement_ref is not None:
                            # Continue only after durable success and current-owner adoption.
                            # The preview owns CAS and range confirmation.
                            from .asset_actions_view import preview_asset_replacement
                            self._movie_replacement_preview_task = asyncio.create_task(
                                preview_asset_replacement(self))
                except Exception as exc:
                    if alive():
                        message.set_text(INITIAL_MOVIE_REPLACEMENT_HELP if initial_content and
                            str(exc) == '该作品已有相同版本标签，请编辑标签或明确选择已登记版本换源'
                            else str(exc))
                finally:
                    self._movie_version_worker = None
                    if alive():
                        set_busy(False)
                    else:
                        status['busy'] = False
            async def complete():
                try:
                    await asyncio.shield(worker)
                except (Exception, asyncio.CancelledError):
                    pass
                with dialog.client:
                    await finish(worker)
            completion = asyncio.create_task(complete())
            try:
                await asyncio.shield(completion)
            except asyncio.CancelledError:
                if alive():
                    message.set_text('等待已中断，但原版本操作仍在后台处理；尚不能认定写入已取消。')
            except Exception:
                pass  # The one completion callback owns errors and busy state.
        with ui.row().classes('w-full justify-end gap-3').classes('bar-modal-footer'):
            cancel_button = ui.button('关闭', on_click=close, icon='o_close').props('flat color=grey-7').classes('order-last')
            save_button = ui.button('核对换源范围' if replacement_only else '保存正片版本', on_click=save,
                icon='o_save').props('unelevated color=blue-7').classes('bar-review-action')
        dialog.client.on_disconnect(disconnected)
        dialog.on_value_change(lambda event: disconnected() if not event.value else None)
    dialog.open()


def open_registered_movie(owner, *, ui, notify, work_key, replacement_only):
    """Use the same complete source form for explicit append or replacement."""
    groups = [g for g in owner.plan.presentation_groups if g.kind == 'movie_continuous']
    if len(groups) != 1:
        notify('请先确认唯一电影呈现组。', type='warning')
        return
    show_movie_versions(owner, groups[0].group_id, ui=ui, notify=notify,
        complete_movie=lambda view, group_id, **kwargs: view._complete_movie_with_primary(group_id, **kwargs),
        replacement_only=replacement_only, work_key=work_key)


def render_audio_corrections(owner, *, ui, notify):
    """Explicit owner selection; source and revision are captured when rendered."""
    from ..rename.content_selection import from_plan, DEFAULT_POLICY
    from .tv_content_actions import run_revision
    plan, state, service = owner.plan, owner.task_decision, owner.review_service
    choice = from_plan(plan)
    if state is None or choice is None or choice.to_dict()['policy'] != DEFAULT_POLICY:
        return
    videos = {str(entry.source_path): entry.source_path.name for entry in plan.entries
              if entry.media_kind == 'video' and entry.presentation_asset is not None
              and entry.presentation_asset.asset_role.kind != 'unknown'}
    if not videos:
        ui.label('请先确认视频用途，再指定外置音轨所属视频。').classes('bar-help')
        return
    fingerprint, revision = PlanStore.fingerprint(plan), state.revision

    def render_one(entry):
        with ui.row().classes('items-center gap-2 flex-wrap'):
            picker = ui.select(videos, label=f'音轨所属视频：{entry.source_path.name}').classes('min-w-80')

            async def save():
                selected = picker.value
                if selected not in videos:
                    notify('请明确选择此音轨所属的视频。', type='warning')
                    return

                def submit():
                    intent = service.prepare_content_audio_association(plan.task_id, fingerprint,
                        source_path=entry.source_path, video_source_path=selected,
                        reason='用户明确选择此外置音轨所属视频')
                    return service.commit_content_audio_association(intent)

                await run_revision(owner, submit=submit, expected_revision=revision,
                    expected_fingerprint=fingerprint, worker_field='_content_companion_worker',
                    closed_field='_content_companion_closed', notify=notify,
                    message='音轨归属已保存为新未批准计划；源文件保持不变。')

            ui.button(f'确认音轨归属：{entry.source_path.name}', on_click=save, icon='o_link')

    for entry in plan.entries:
        if entry.role == 'external_audio_unresolved':
            render_one(entry)


