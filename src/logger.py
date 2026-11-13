import os
import logging
import asyncio
import atexit
from logging.handlers import TimedRotatingFileHandler

import structlog
from .log_dispatch import LogDispatch

from .utils.path import log_path

file_handler = TimedRotatingFileHandler(
    filename=log_path,
    when="midnight",
    interval=1,
    backupCount=7,
    encoding="utf-8",
)
file_handler.suffix = "%Y-%m-%d"
file_formatter = structlog.stdlib.ProcessorFormatter(
    processors=[
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
        structlog.stdlib.add_log_level,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(ensure_ascii=False),
    ]
)
file_handler.setFormatter(file_formatter)

# 配置控制台日志格式化器（带颜色）
console_formatter = structlog.stdlib.ProcessorFormatter(
    processors=[
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback),
    ]
)
console_handler = logging.StreamHandler()
console_handler.setFormatter(console_formatter)

from .config.config_manager import cm

# 获取日志级别配置
log_level_str = cm.get_config("log_level") or "INFO"
log_level = getattr(logging, log_level_str.upper(), logging.INFO)

# 设置控制台和文件处理器的日志级别
console_handler.setLevel(log_level)
file_handler.setLevel(log_level)

producer_handler = LogDispatch([file_handler, console_handler])
logging.basicConfig(
    level=log_level,
    # format="%(message)s",
    handlers=[producer_handler],
)

atexit.register(producer_handler.drain)


async def stop_logging():
    def finish():
        result = producer_handler.drain()
        if not result['stopped']:
            print('日志输出关闭超时，未完成条数：', result['pending'])
        return result
    return await asyncio.to_thread(finish)

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,  # 过滤日志级别
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,  # 使用格式化器包装
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET),
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=False,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger()
logging.getLogger('niceGUI').propagate = False

logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)

def update_log_level_from_config() -> None:
    """
    Update log levels of console and file handlers based on the current configuration.
    """
    # Retrieve desired log level from configuration
    log_level_str = cm.get_config("log_level") or "INFO"
    level = getattr(logging, log_level_str.upper(), logging.INFO)
    # Update handler levels
    console_handler.setLevel(level)
    file_handler.setLevel(level)
    # Update root logger level
    logging.getLogger().setLevel(level)
