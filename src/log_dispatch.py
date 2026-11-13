"""有界后台日志输出；生产者不格式化异常、不等待输出设备。"""
from collections import Counter
import copy
import logging
import queue
import threading
import time
import sys


class LogDispatch(logging.Handler):
    def __init__(self, sinks, capacity=512):
        super().__init__()
        self.sinks = tuple(sinks)
        self.records = queue.Queue(maxsize=capacity)
        self.dropped = Counter()
        self._state_lock = threading.Lock()
        self._closing = threading.Event()
        self._closed = False
        self.thread = threading.Thread(target=self._listen, name='bar-log-output', daemon=True)
        self.thread.start()

    def emit(self, record):
        # 浅拷贝事件本身；不在调用线程展开异常栈和任务局部变量。
        record = copy.copy(record)
        if isinstance(record.msg, dict):
            record.msg = dict(record.msg)
            if record.msg.get('exc_info') is True:
                record.msg['exc_info'] = sys.exc_info()
        if record.exc_info is True:
            record.exc_info = sys.exc_info()
        with self._state_lock:
            if self._closing.is_set():
                self.dropped[record.levelname] += 1
                return
            try:
                self.records.put_nowait(record)
            except queue.Full:
                if record.levelno >= logging.ERROR:
                    with self.records.mutex:
                        victim = next((item for item in self.records.queue if item.levelno < logging.ERROR), None)
                        if victim is not None:
                            self.records.queue.remove(victim)
                            self.records.queue.append(record)
                            self.dropped[victim.levelname] += 1
                            self.records.not_empty.notify()
                            return
                self.dropped[record.levelname] += 1

    def _output(self, record):
        for sink in self.sinks:
            if record.levelno >= sink.level:
                try:
                    sink.handle(record)
                except Exception:
                    with self._state_lock:
                        self.dropped['OUTPUT_ERROR'] += 1

    def _overflow(self):
        with self._state_lock:
            counts = dict(self.dropped)
            self.dropped.clear()
        if counts:
            level = logging.ERROR if any(counts.get(k) for k in ('ERROR','CRITICAL','OUTPUT_ERROR')) else logging.WARNING
            record = logging.LogRecord('bar.logging', level, '', 0,
                '日志输出未保留的条数（按级别）：%s', (counts,), None)
            self._output(record)

    def _listen(self):
        while not self._closing.is_set() or not self.records.empty():
            try:
                record = self.records.get(timeout=.2)
            except queue.Empty:
                self._overflow()
                continue
            try:
                self._output(record)
                self._overflow()
            finally:
                self.records.task_done()
        self._overflow()
        self._closed = True

    def drain(self, timeout=3.0):
        """只能在后台或同步退出阶段调用；返回未完成计数。"""
        self._closing.set()
        self.thread.join(timeout)
        with self._state_lock:
            return dict(stopped=not self.thread.is_alive(),
                        pending=self.records.unfinished_tasks, dropped=dict(self.dropped))
