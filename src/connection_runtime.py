"""页面会话恢复与最小连接诊断，不参与任务持久状态。"""
import asyncio
import sys
import time

RECONNECT_TIMEOUT = 30
_monitor = None


def configure_server_loop():
    """Windows Web 服务使用 socket 循环；Linux 保持服务器默认策略。"""
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        return 'none'
    return 'auto'


def install_page_status():
    from nicegui import ui
    ui.add_body_html('''<script>
    (() => {
      let attempts = 0;
      const attach = () => {
        if (!window.socket) { if (++attempts < 100) setTimeout(attach, 100); return; }
        const popup = document.getElementById('popup');
        if (popup) {
          const lines = popup.querySelectorAll('span');
          if (lines[0]) lines[0].textContent = '连接暂时中断';
          if (lines[1]) lines[1].textContent = '正在重连，已保存的任务仍保留';
        }
        const allowed = new Set(['ping timeout','transport close','transport error','io server disconnect','io client disconnect']);
        window.socket.on('disconnect', reason => console.warn('[BAR连接]', new Date().toISOString(), allowed.has(reason) ? reason : 'unknown'));
        window.socket.on('connect', () => console.info('[BAR连接]', new Date().toISOString(), 'connected'));
      };
      setTimeout(attach, 0);
    })();
    </script>''')


async def start_monitor():
    global _monitor
    if _monitor is not None and not _monitor.done():
        return
    async def watch():
        from .logger import logger
        last_report = 0.0
        while True:
            begin = time.monotonic()
            await asyncio.sleep(2)
            delay = time.monotonic() - begin - 2
            if delay > 1 and time.monotonic() - last_report > 30:
                logger.warning('界面事件循环延迟', delay_ms=round(delay*1000))
                last_report = time.monotonic()
    _monitor = asyncio.create_task(watch(), name='bar-loop-diagnostic')


async def stop_monitor():
    global _monitor
    if _monitor is not None:
        _monitor.cancel()
        try:
            await _monitor
        except asyncio.CancelledError:
            pass
        _monitor = None
