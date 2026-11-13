"""合成测试使用与生产相同的首次采集入口。"""
from pathlib import Path
from src.rename.input_directory_discovery import collect_source


def collected_identity(path):
    return collect_source(path).identities[0]


def collected_files(path):
    return tuple(Path(row.source_path) for row in collect_source(path).identities)


async def admit_task(manager, path, *, task_id=None, is_anime=None, is_movie=None,
                     source='ui', preview_only=False, **fields):
    """测试通过现行批次入口接纳，可固定ID和人工字段。"""
    import asyncio
    from dataclasses import replace
    batch = await asyncio.to_thread(manager.prepare_input_batch, (path,), is_anime=is_anime,
        is_movie=is_movie, source=source, preview_only=preview_only)
    job, = batch['jobs']
    identifier = task_id or job.task_id
    batch['jobs'] = (replace(job, task_id=identifier, **fields),)
    batch['task_requests'] = (replace(batch['task_requests'][0], task_id=identifier),)
    return (await manager.enqueue_batch(**batch))[0]


async def admit_existing(manager, job, **scope):
    """已退出占用的历史夹具由完整批次重新接纳。"""
    from src.rename.task_batch_scope import BatchTaskSourceRequest
    import uuid
    request = BatchTaskSourceRequest(task_id=job.task_id, scope_kind='parent_directory', **scope)
    return (await manager.enqueue_batch(batch_id='fixture-' + uuid.uuid4().hex,
        task_requests=(request,), jobs=(job,)))[0]
