"""隔离队列夹具：暂停消费者与合成来源只存在于测试环境。"""
import asyncio
from pathlib import Path

from src.task_queue import TaskQueueManager as ProductionQueue
from src.rename.input_directory_discovery import collect_source, SourceGroup, discovery_task_groups
from src.rename.plan_execution import PlanExecutionError


class TaskQueueManager(ProductionQueue):
    def __init__(self, *, test_mode=False, **kwargs):
        self.test_mode = test_mode
        super().__init__(**kwargs)

    async def _worker(self):
        if self.test_mode:
            await asyncio.Event().wait()
        else:
            await super()._worker()

    def prepare_input_batch(self, paths, *, is_anime, is_movie, source, preview_only):
        if not self.test_mode:
            return super().prepare_input_batch(paths, is_anime=is_anime, is_movie=is_movie,
                                              source=source, preview_only=preview_only)
        if not paths:
            raise PlanExecutionError('批次至少需要一个来源')
        groups, seen = [], set()
        for path in map(Path, paths):
            key = str(path.absolute()).casefold()
            if key in seen:
                raise PlanExecutionError('批次来源路径重复')
            seen.add(key)
            try:
                groups.append(collect_source(path))
            except OSError:
                groups.append(SourceGroup(path, (str(path.resolve(strict=False)),), 'exact_file', ()))
        return self._prepare_batch(groups, is_anime=is_anime, is_movie=is_movie,
                                   source=source, preview_only=True)

    def prepare_discovered_batch(self, discovery, *, is_anime, is_movie, source):
        return self._prepare_batch(discovery_task_groups(discovery), is_anime=is_anime,
            is_movie=is_movie, source=source, preview_only=self.test_mode, discovery=discovery)

    async def _enqueue_execution(self, *args, **kwargs):
        if self.test_mode:
            raise PlanExecutionError('GUI 测试模式禁止执行已批准整理计划')
        return await super()._enqueue_execution(*args, **kwargs)

    async def replan_failed_execution(self, *args, **kwargs):
        if self.test_mode:
            raise PlanExecutionError('界面测试模式不会运行重新识别')
        return await super().replan_failed_execution(*args, **kwargs)

    def _prepare_reidentify_job(self, *args, **kwargs):
        if self.test_mode:
            raise PlanExecutionError('界面测试模式不会运行重新识别')
        return super()._prepare_reidentify_job(*args, **kwargs)
