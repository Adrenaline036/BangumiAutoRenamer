"""General-TV season correction from the current plan's frozen observations."""
from collections.abc import Mapping
from ..element.red import notify


def render_unresolved_tmdb_season(self, ui):
    """Let a failed first selection continue through the same queue admission."""
    from . import plan_review_page as page
    from ..rename.review_operation import build_review_operation
    from ..rename.provider_selection_authority import provider_candidate_group_kind
    state = self.task_decision
    operation, _ = build_review_operation(self.task_data)
    context = dict(operation.context)
    candidate = context.get('candidate')
    from ..rename.ova_selection import pending_subject
    try:
        ova = pending_subject(self.task_data, context)
    except (ValueError, TypeError, KeyError):
        return
    if (state is None or (state.confirmed_work and not ova)
            or not ova and context.get('lifecycle') not in {'failed', 'awaiting_user'}
            or not isinstance(candidate, Mapping)
            or provider_candidate_group_kind(candidate) != 'tv_episodes'):
        return
    fingerprint = page.PlanStore.fingerprint(self.plan)
    revision = state.revision
    with ui.column().classes('w-full gap-3'):
        ui.label('请选择本次整理的季度，也可以手动填写季号。').classes('bar-help')
        from .season_choice import season_choice
        from .season_reference_view import attach
        available = getattr(ova, 'available', ())
        selected = season_choice(ui, available, current=None if available else 1, label='季度（特别篇为 0）')
        selected.manual.set_value(True)
        number = self.unresolved_season_input = selected.custom
        async def apply():
            if self.exit_requested or self.is_deleted or number.is_deleted:
                return
            try:
                value = selected()
                latest, _ = build_review_operation(page.get_task(self.task_id))
                if (self.task_decision is not state or state.revision != revision
                        or page.PlanStore.fingerprint(self.plan) != fingerprint
                        or dict(latest.context) != context):
                    raise ValueError('作品或计划已更新，请重新打开任务')
                await self._select_provider_candidate(dict(candidate), season_number=int(value))
            except (ValueError, TypeError, OverflowError) as exc:
                notify(str(exc), type='warning')
        with selected.controls_row:
            ui.button('确认季度编号', icon='o_check', on_click=apply).props('color=blue-7 no-caps')
    return True


def general_tv_provider(self):
    state = self.task_decision
    primary = state.confirmed_work.get('primary_provider') if state is not None else None
    if state is None or state.library_domain not in {'general', 'animation'} or not isinstance(primary, Mapping):
        return None
    if (primary.get('namespace'), primary.get('entity_type')) not in {('bangumi', 'subject'), ('tmdb', 'tv')}:
        return None
    return dict(primary)

def render_general_tv_season(self, ui):
    if self._general_tv_provider() is None:
        return
    binding = self.task_decision.season_binding or {}
    number = binding.get('season_number')
    origin = '人工确认' if binding.get('decision_source') == 'explicit_user' else '自动推荐，尚可更改'
    with ui.row().classes('w-full items-center gap-3'):
        ui.label(f'季度：第 {number} 季（{origin}）' if number is not None else '季度尚未确认').classes('bar-help')
        ui.button('更改季度', icon='o_edit', on_click=self._change_general_tv_season).props('outline color=blue-7 no-caps')
    self.general_season_container = ui.column().classes('w-full gap-2')

async def change_general_tv_season(self, ui):
    from ..rename.plan_store import PlanStore
    from ..rename.tv_completion_observations import TVSeasonObservation
    from ..rename.season_identity import provider_key
    from ..rename.series_registration import _plain
    provider = self._general_tv_provider()
    if provider is None:
        notify('此入口用于已确认的剧集人工季号', type='warning')
        return
    revision = self.task_decision.revision
    fingerprint = PlanStore.fingerprint(self.plan)
    container = self.general_season_container
    container.clear()
    with container:
        ui.label('使用当前计划已保存的季度资料；不会重新搜索作品。').classes('bar-help')
    try:
        observation = TVSeasonObservation.from_mapping(_plain(self.plan.metadata.get('tv_completion_seasons')))
        if observation.provider != provider_key(provider):
            raise ValueError('季度资料与当前作品不一致')
    except (TypeError, ValueError):
        container.clear()
        with container:
            ui.label('当前计划没有可核对的季度资料，原计划未改变。请重新生成计划后再选择季度。').classes('bar-help text-orange-8')
        return
    if self.task_decision.revision != revision or self._general_tv_provider() != provider:
        return
    current = (self.task_decision.season_binding or {}).get('season_number')
    container.clear()
    with container:
        from .season_choice import season_choice
        selected = season_choice(ui, observation.available, current=current,
                                 label='选择季度（特别篇需明确选择第 0 季）')
        self.general_season_choice = selected
        from .season_reference_view import attach
        async def change_subject(candidate):
            self._show_provider_candidates()
            await self._select_provider_candidate(candidate)
        async def apply_season():
            try:
                number = selected()
            except ValueError as exc:
                notify(str(exc), type='warning')
                return
            if (self.task_decision.revision != revision or self._general_tv_provider() != provider
                    or PlanStore.fingerprint(self.plan) != fingerprint):
                notify('作品或计划已更新，请重新打开季度列表', type='warning')
                return
            if self.plan.metadata.get('tv_completion_resolution') is not None:
                candidate = dict(provider, library_domain=self.task_decision.library_domain)
                if provider['namespace'] == 'bangumi':
                    candidate['platform'] = 'TV'
                await self._select_provider_candidate(candidate, season_number=number)
                return
            from .tv_content_actions import revise
            await revise(self, mode='season', edit={'season_number': number},
                expected_revision=revision, expected_fingerprint=fingerprint,
                notify=notify, message='季度已更正；请重新检查并批准新计划。')
        with selected.controls_row:
            ui.button('确认季度并生成新计划', icon='o_check', on_click=apply_season).props('color=blue-7 no-caps')
