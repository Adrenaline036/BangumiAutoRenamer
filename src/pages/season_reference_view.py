"""Background labels for the existing season forms, scoped to their live page."""
import asyncio

from ..rename.season_reference import load
from .season_choice import season_label


def prepare(owner,provider=None,*,observation=None,retry=False):
    state=getattr(owner,'task_decision',None)
    if state is None or state.library_domain not in {'animation','general'}:return None
    plan=getattr(owner,'plan',None)
    if provider is None:
        if plan is None or plan.work_identity is None:return None
        provider=plan.work_identity.primary_provider.to_dict()
    if (provider.get('namespace'),provider.get('entity_type')) not in {('bangumi','subject'),('tmdb','tv')}:
        return None
    if observation is None and plan is not None:
        observation=plan.metadata.get('tv_completion_special')
    from ..rename.season_reference_record import start
    try:
        future=start(owner.review_service.task_root,owner.task_id,provider,load,observation=observation,retry=retry)
    except RuntimeError:
        return None  # A non-UI caller without an event loop does not spawn a worker.
    owner._season_reference_task=future
    return future


def attach(owner, ui, choice, *, provider, title, is_current, on_subject):
    container = choice.reference_container
    client = getattr(container, 'client', None)
    if client is None or owner.task_decision.library_domain not in {'animation','general'}:
        return
    sequence = getattr(owner, '_season_reference_sequence', 0) + 1
    owner._season_reference_sequence = sequence
    active = True

    def close(*_args):
        nonlocal active
        active = False

    client.on_disconnect(close)
    owner.on_value_change(lambda event: close() if not event.value else None)

    def current():
        return (active and not container.is_deleted and not getattr(owner, 'exit_requested', False)
                and not getattr(owner, 'is_deleted', False)
                and owner._season_reference_sequence == sequence and is_current())

    identity = str(provider['provider_id'])
    bangumi = provider['namespace'] == 'bangumi'
    rows = {}
    with container:
        if bangumi:
            async def select_subject(event):
                target = str(event.value)
                if not current() or target == identity or target not in rows:
                    return
                # This navigates to the existing correction preview, not a save.
                picker.set_value(identity)
                await on_subject(dict(rows[target]))
            picker = ui.select({identity: f'当前条目 · {title}'}, value=identity,
                               label='Bangumi 季度条目', on_change=select_subject).props('outlined dense').classes('w-full')
        status = ui.label('正在补充季度参考…可以先选择或手动填写。').classes('bar-help')

    async def refresh():
        try:
            future=prepare(owner,dict(provider))
            if future is None:return
            result = await asyncio.shield(future)
        except Exception:
            if current():
                with client:
                    status.text = '季度参考读取失败，可继续手动选择。'
            return
        if not current():
            return
        with client:
            if bangumi:
                rows.update({str(row['provider_id']): row for row in result.subjects})
                assignments = dict(result.assignments)
                options = {identity: f'当前条目 · {title}'}
                for key, row in rows.items():
                    number = assignments.get(key)
                    options[key] = ((season_label(number) if number is not None else '季度待定')
                                    + f' · {row["title"]}（#{key}）' + (' · 当前条目' if key == identity else ''))
                picker.set_options(options, value=identity)
                choice.suggest_current_subject(assignments.get(identity))
            else:
                choice.annotate(result.names_by_season(), not_found=result.status == 'not_found')
            status.text = {
                'unavailable':'季度参考读取失败，可继续手动选择。',
                'incomplete':'季度参考资料不完整，可继续选择或手动填写。',
                'ambiguous':'季度参考存在多个可能对应，以你的选择为准。',
                'not_found':'未找到确定对应，可继续选择或手动填写。',
                'found':'名称和推荐季号仅供参考，以你的选择为准。',
            }[result.status]

    choice.reference_task = asyncio.create_task(refresh())
