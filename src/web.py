import asyncio
from typing import cast
from pathlib import Path

from fastapi import Request
from nicegui import ui, app

from .logger import logger
from .models import TaskModel
from .main_page import main_page
from .utils.utils import no_scroll_bar
from .task_queue import task_queue
from .connection_runtime import install_page_status, start_monitor, stop_monitor
from .logger import stop_logging

ANI_TAG = ['动漫', 'anime', '动画']
MOVIE_TAG = ['电影', 'movie', '剧场', '剧场版']


@ui.page('/')
def main():
    ui.add_head_html(no_scroll_bar)
    install_page_status()
    main_page()


app.on_startup(task_queue.start)
app.on_shutdown(task_queue.stop)
app.on_startup(start_monitor)
app.on_shutdown(stop_monitor)
app.on_shutdown(stop_logging)


def _form_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {'true', '1', 'yes', 'on'}:
            return True
        if normalized in {'false', '0', 'no', 'off'}:
            return False
    return None


@app.post('/sendTask')
async def _send_task(request: Request):
    data: TaskModel = cast(TaskModel, dict(await request.form()))
    logger.info(f'[收到任务] {data}')
    path = data.get('path', '')
    anime_input = data.get('is_anime', '')
    is_anime = _form_bool(anime_input)
    process_input = data.get('no_process', '')
    no_process = _form_bool(process_input)
    if no_process is None:
        no_process = bool(process_input)
    tag = data.get('tag', '')

    tag_list = [str(i).strip().lower() for i in tag.split(',')]

    if 'no_process' in tag_list:
        no_process = True

    if not isinstance(path, str) or not path:
        logger.error('[结束任务] 路径为空！')
        return {'code': 400, 'data': '路径为空！'}

    if no_process:
        logger.info(f'[结束任务] {path}忽略, 不处理！')
        return {'code': 202, 'data': f'{path}忽略, 不处理！'}

    _path = Path(path)
    if not await asyncio.to_thread(_path.exists):
        logger.error(f'[结束任务] 路径{path}不存在！')
        return {'code': 404, 'data': f'路径{path}不存在！'}

    if is_anime is None and not anime_input:
        for i in ANI_TAG:
            if i in tag_list:
                is_anime = True
                break
        else:
            is_anime = False
    for i in MOVIE_TAG:
        if i in tag_list:
            is_movie = True
            break
    else:
        is_movie = None

    prepared = await task_queue._disk(task_queue.prepare_input_batch,
        (_path,),
        is_anime=is_anime,
        is_movie=is_movie,
        source='webhook',
        preview_only=False,
    )
    task_id = (await task_queue.enqueue_batch(**prepared))[0]
    logger.info(f'[任务队列] webhook 任务 {task_id} 已接收')
    return {
        'code': 202,
        'data': '任务已加入队列，可在 WebUI 查看排队与执行状态。',
        'task_id': task_id,
        'status': 'queued',
    }
