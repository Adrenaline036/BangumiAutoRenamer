import sys
import os
from pathlib import Path

sys.path.append(str(Path(__file__).parents[2]))
# __package__ = 'Bangumi_Auto_Rename.src'

from .logger import logger  # noqa: E402
from .web import ui  # noqa: E402 # type: ignore
from .connection_runtime import RECONNECT_TIMEOUT, configure_server_loop

if __name__ == '__mp_main__':
    logger.info('程序启动中...')

ui.run(
    port=int(os.getenv("BAR_UI_PORT", "5999")),
    storage_secret=os.getenv("BAR_UI_STORAGE_SECRET"),
    title='番剧自动重命名',
    favicon='🍭',
    log_config=None,
    reload=False,
    loop=configure_server_loop(),
    reconnect_timeout=RECONNECT_TIMEOUT,
    uvicorn_reload_excludes="*.log",
)
