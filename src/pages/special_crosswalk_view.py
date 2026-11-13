"""原位更新双侧资料控件，命名只影响提交方式。"""
from pathlib import Path
from ..rename.plan_review import entry_id
from ..rename.special_catalog import suggestions
from ..rename.special_selection import (observation, references, build_correspondence,
    build_descriptive_decision, build_descriptive_special, normalized_title, title_suggestion,
    build_bangumi_decision, build_bangumi_special, suggest_bangumi_episode)
from ..rename.content_selection import canonical_episode_target


def render(plan, *, ui_api, submit, notify):
    frozen, catalog, bgm, tmdb, recommended, videos = None, (), {}, {}, {}, {}
    state = dict(updating=False, edited=False, title_side='bangumi', right_manual=False)

    def load(updated):
        nonlocal plan, frozen, catalog, bgm, tmdb, recommended, videos
        plan = updated
        try:
            frozen = observation(plan)
        except ValueError:
            frozen = None
        catalog = frozen.to_dict().get('catalog', ()) if frozen else ()
        bgm = references(plan, 'bangumi') if catalog else {}
        tmdb = {r['row']['provider_episode_id']: r for r in references(plan, 'tmdb').values()} if catalog else {}
        recommended = suggestions(frozen) if frozen else {}
        videos = {entry_id(e): e for e in plan.entries if e.media_kind == 'video' and e.source_path not in plan.ignored_sources}

    load(plan)
    status = ui_api.label().classes('bar-help')
    confirmed = ui_api.label().classes('bar-help')
    files = ui_api.select({k:e.source_path.name for k,e in videos.items()}, label='选择视频',
        value=next(iter(videos)) if len(videos) == 1 else None).props('outlined dense').classes('w-full')
    with files.add_slot('append'):
        file_confirmed = ui_api.label('已确认').classes('bar-help whitespace-nowrap')
    with ui_api.row().classes('w-full items-center'):
        left = ui_api.select({}, label='Bangumi 编号', value=None).props('outlined dense clearable').classes('flex-1 min-w-0')
        ui_api.label('↔')
        right = ui_api.select({}, label='TMDB 编号', value=None).props('outlined dense clearable').classes('flex-1 min-w-0')
    with ui_api.expansion('查看双侧详细资料', value=False).classes('w-full') as details:
        details_text = ui_api.label().classes('whitespace-pre-wrap w-full')
    mode = ui_api.radio({'numbered':'使用 TMDB 编号', 'bangumi':'使用 Bangumi 命名', 'descriptive':'描述性命名（不编集号）'},
        value='numbered' if frozen and frozen.output_candidates_available else 'descriptive').props('inline')
    title = ui_api.input('描述标题（可修改）').props('outlined dense').classes('w-full')
    preview = ui_api.label().classes('bar-help break-all')

    def side_status(namespace):
        sources = [s for s in catalog if s['provider']['namespace'] == namespace]
        if not sources:
            if frozen is None:
                return '尚未获取资料'
            return {
                'unavailable': '作品资料获取失败，可重试或填写链接',
                'not_found': '未找到对应作品，请填写链接确认',
                'ambiguous': '有多个候选作品，请填写链接确认',
                'incomplete': '作品资料不完整，请重试或填写链接',
            }.get(frozen.to_dict().get('relation_reason'), '来源作品待确认，请填写链接')
        payload = frozen.to_dict()
        states = []
        for source in sources:
            item = next((v for v in payload.get('sources', {}).values() if v['snapshot']['provider'] == source['provider']), None)
            attempt = item['last_attempt'] if item else source
            stale = item and (item['snapshot_fingerprint'] != attempt['response_fingerprint'] or bool(item['history']))
            if attempt['state'] == 'unavailable_or_error':
                text = '获取失败，保留已有资料' if source['rows'] else '获取失败，请重试'
            elif not source['rows'] and source['complete']:
                text = '未找到特别篇资料'
            else:
                text = f'可选 {len(source["rows"])} 条资料' + ('，含旧资料' if stale else '')
                if attempt['state'] == 'incomplete':
                    text += '，本次资料不完整'
            states.append(text)
        return '；'.join(dict.fromkeys(states))

    def options():
        state['updating'] = True
        try:
            files.set_options({k: e.source_path.name for k, e in videos.items()},
                value=files.value if files.value in videos else None)
            confirmed_rows = [i for i, key in enumerate(videos)
                if key in plan.metadata.get('provider_special_decisions', {})]
            files.add_slot('option', '<q-item v-bind="props.itemProps">'
                '<q-item-section>{{ props.opt.label }}</q-item-section>'
                f'<q-item-section side v-if="{confirmed_rows}.includes(props.opt.value)" '
                'class="bar-help q-pr-lg">已确认</q-item-section></q-item>')
            files.update()
            left.set_options({k:f'Bangumi: {"正篇 EP" if r["row"].get("episode_type", 1) == 0 else "特别篇 SP"}{r["row"]["source_number"] if r["row"]["source_number"] is not None else "未提供"}'
                + (f'（条目 {r["provider"]["provider_id"]}）' if len(catalog)>2 else '') for k,r in bgm.items()}, value=left.value if left.value in bgm else None)
            def label(ref):
                n = ref['row']['source_number']
                return f'TMDB: S00E{int(n):02d}' if n and n.isdigit() and int(n)>0 else 'TMDB: 未提供可用集号'
            right.set_options({k:label(r) for k,r in tmdb.items()}, value=right.value if right.value in tmdb else None)
            for control, namespace, rows in ((left,'bangumi',bgm),(right,'tmdb',tmdb)):
                control.props(remove='display-value')
                if not rows:
                    control.props(f'display-value="{side_status(namespace)}"')
                control.set_enabled(bool(rows))
            status.set_text('Bangumi：' + side_status('bangumi') + '；TMDB：' + side_status('tmdb'))
            files.set_enabled(bool(videos))
            mode.set_enabled(plan.metadata.get('content_selection') is not None)
        finally:
            state['updating'] = False

    def chosen_reference():
        choices = {'bangumi':bgm.get(left.value), 'tmdb':tmdb.get(right.value)}
        namespace = state['title_side']
        if choices[namespace] is None:
            namespace = 'tmdb' if namespace == 'bangumi' else 'bangumi'
        ref = choices[namespace]
        return (f'{namespace}:{ref["provider"]["provider_id"]}:{ref["row"]["provider_episode_id"]}',ref) if ref else (None,None)

    def edit(*, previewing=False):
        key = files.value
        if not isinstance(key, str) or key not in videos:
            raise ValueError('请选择一个视频确认特别篇对应')
        if mode.value == 'numbered':
            build_correspondence(plan,right.value,left.value,key)
            return dict(entry_id=key, action='confirmed_special_correspondence', tmdb_episode_id=right.value, bangumi_key=left.value)
        if mode.value == 'bangumi':
            number = suggest_bangumi_episode(plan, key, left.value)
            choice = (key, left.value, title.value, number)
            if not previewing and state.get('bangumi_preview') != choice:
                raise ValueError('目标编号已变化，请重新预览后确认')
            build_bangumi_decision(plan, key, left.value, title.value, number)
            if previewing:
                state['bangumi_preview'] = choice
            return dict(entry_id=key, action='bangumi_special', bangumi_key=left.value, title=title.value, local_episode=number)
        reference_key, ref = chosen_reference()
        source = 'user'
        if not state['edited']:
            if ref and ref['row']['title']:
                try:
                    if normalized_title(ref['row']['title']) == normalized_title(title.value):
                        source = 'official'
                except ValueError:
                    pass
            elif ref is None and videos[key].source_path.stem == title.value:
                source = 'local'
        return dict(entry_id=key,action='descriptive_special',title=title.value,title_source=source,
                    reference_key=reference_key,bangumi_key=left.value,tmdb_episode_id=right.value)

    def refresh():
        if state['updating']:
            return
        key = files.value if isinstance(files.value, str) else None
        saved = plan.metadata.get('provider_special_decisions', {}).get(key)
        confirmed.set_text('已确认特别篇身份' if saved and key in videos else '')
        confirmed.set_visibility(bool(saved))
        file_confirmed.set_visibility(bool(saved))
        chunks = []
        for label,ref in (('Bangumi',bgm.get(left.value)),('TMDB',tmdb.get(right.value))):
            if ref is None:
                chunks.append(label+'：未选择条目')
            else:
                row=ref['row']
                chunks.append(f'{label} · 条目 {ref["provider"]["provider_id"]} / 分集 {row["provider_episode_id"]}\n'
                    f'标题：{row["title"] or "未提供"}\n原名：{row["original_title"] or "未提供"}\n'
                    f'日期：{row["air_date"] or "未提供"} · 时长：{row["runtime"] or "未提供"}\n'
                    f'{row["overview"] or "简介未提供"}\n{row["source_url"]}')
        if left.value in recommended and recommended[left.value] == right.value:
            chunks.append('匹配建议：标题及日期或时长一致，双方唯一；以你的选择为准。')
        details_text.set_text('\n\n'.join(chunks))
        title.set_visibility(mode.value in {'descriptive', 'bangumi'})
        if not state['edited'] and key in videos:
            _,ref=chosen_reference()
            if mode.value == 'bangumi':
                ref = bgm.get(left.value)
            initial=ref['row']['title'] if ref and ref['row']['title'] else videos[key].source_path.stem
            state['updating']=True
            title.set_value(title_suggestion(initial))
            state['updating']=False
        try:
            value=edit(previewing=True)
            if value['action']=='bangumi_special':
                decision=build_bangumi_decision(plan,value['entry_id'],value['bangumi_key'],value['title'],value['local_episode'])
                target,_=build_bangumi_special(plan,value['entry_id'],decision)
            elif value['action']=='descriptive_special':
                decision=build_descriptive_decision(plan,value['entry_id'],value['title'],value['title_source'],
                    value['reference_key'],value['bangumi_key'],value['tmdb_episode_id'])
                target,_=build_descriptive_special(plan,value['entry_id'],decision)
            else:
                decision=build_correspondence(plan,right.value,left.value,value['entry_id'])
                target=canonical_episode_target(Path(plan.metadata['canonical_work_root']),0,
                    decision['tmdb_row']['episode_number'],videos[value['entry_id']].source_path.suffix)
            preview.set_text('目标路径：'+str(target))
            confirm.enable()
        except (ValueError,KeyError,TypeError) as exc:
            preview.set_text(str(exc))
            confirm.disable()

    async def confirm_selection():
        try:
            value=edit()
        except (ValueError,KeyError,TypeError) as exc:
            notify(str(exc),type='warning')
            return
        confirm.disable()
        confirmed.set_text('正在处理…')
        confirmed.set_visibility(True)
        result = None
        try:
            result = await submit(value)
        finally:
            if not confirm.is_deleted:
                refresh()
                if result is None:
                    confirmed.set_text('确认失败或尚未完成，请查看提示；原选择已保留')
                    confirmed.set_visibility(True)

    confirm=ui_api.button('归入特别篇',on_click=confirm_selection,icon='o_playlist_add_check').props('outline color=blue-7')

    def choose_file():
        state.update(updating=True,edited=False,right_manual=False,title_side='bangumi')
        key=files.value if isinstance(files.value, str) else None
        decision=plan.metadata.get('provider_special_decisions',{}).get(key,{})
        bgm_ref=decision.get('bangumi_reference')
        tmdb_ref=decision.get('tmdb_reference')
        left.set_value(f'{bgm_ref["provider"]["provider_id"]}:{bgm_ref["row"]["provider_episode_id"]}' if bgm_ref
                       else next(iter(bgm)) if len(bgm)==1 else None)
        right.set_value(tmdb_ref['row']['provider_episode_id'] if tmdb_ref else
            (decision.get('tmdb_row') or {}).get('provider_episode_id') or recommended.get(left.value) or
            (next(iter(tmdb)) if len(tmdb)==1 else None))
        if decision.get('action') == 'confirmed_special_correspondence':
            mode.set_value('numbered')
        if decision.get('action') in {'descriptive_special','bangumi_special'}:
            state['edited']=True
            mode.set_value('bangumi' if decision['action']=='bangumi_special' else 'descriptive')
            title.set_value(decision['title'])
        state['updating']=False
        refresh()

    def choose_side(namespace):
        if state['updating']:
            return
        state['title_side']=namespace
        if namespace=='tmdb':
            state['right_manual']=True
        elif not state['right_manual'] and right.value is None and left.value in recommended:
            state['updating']=True
            right.set_value(recommended[left.value])
            state['updating']=False
        refresh()

    def title_changed():
        if not state['updating']:
            state['edited']=True
            refresh()

    def update(updated):
        load(updated)
        options()
        refresh()

    files.on_value_change(lambda _: choose_file() if not state['updating'] else None)
    left.on_value_change(lambda _:choose_side('bangumi'))
    right.on_value_change(lambda _:choose_side('tmdb'))
    mode.on_value_change(lambda _:refresh())
    title.on_value_change(lambda _:title_changed())
    options()
    choose_file()
    return dict(files=files,episode=right,bangumi=left,confirm=confirm,mode=mode,title=title,
                details=details,preview=preview,confirmed=confirmed,file_confirmed=file_confirmed,update=update,
                status=status,summary=lambda: 'Bangumi：' + side_status('bangumi') + '；TMDB：' + side_status('tmdb'))
