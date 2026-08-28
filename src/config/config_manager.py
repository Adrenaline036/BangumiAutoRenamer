import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, Optional, Union

from ..utils.path import CONFIG_PATH
from ..rename.file_lock import FileLockBusyError, advisory_file_lock

CONFIG_DEFAULT = {
    "api_key": "",
    "bangumi_enabled": True,
    "bangumi_token": "",
    "bangumi_user_agent": "BangumiAutoRenamer/0.3 (local media organizer)",
    "bangumi_path": "",
    "movie_path": "",
    "anime_path": "",
    "anime_movie_path": "",
    "mode": "链接",
    "docker_mnt": "/volume1",
    "ai_provider": "openai",
    "ai_api_key": "",
    "ai_base_url": "https://api.openai.com/v1",
    "ai_model": "gpt-4o-mini",
    "ai_temperature": 0.1,  # OpenAI温度
    "gemini_api_key": "",
    "gemini_base_url": "https://generativelanguage.googleapis.com",
    "gemini_model": "gemini-2.5-flash",
    "gemini_temperature": 0.5,  # Gemini温度
    "ai_enabled": False,
    "ai_confidence_threshold": "Medium",
    "openai_output_format": "function_calling",  # OpenAI输出格式选择
    "ai_auto_save": False,  # 是否自动保存AI分析结果
    "log_level": "INFO",  # 日志等级
}

CN_MAP = {
    "api_key": "🔑 TMDB API密钥",
    "bangumi_enabled": "🌐 启用 Bangumi 动画检索",
    "bangumi_token": "🔑 Bangumi Access Token（可选）",
    "bangumi_user_agent": "🪪 Bangumi User-Agent",
    "bangumi_path": "🎬 电视剧路径",
    "movie_path": "🎬 电影路径",
    "anime_path": "🎬 动画路径",
    "anime_movie_path": "🎬 动画电影路径",
    "mode": "💿 重命名模式",
    "docker_mnt": "📁 容器浏览根目录",
    "ai_provider": "🤖 AI提供商",
    "ai_api_key": "🤖 OpenAI API密钥",
    "ai_base_url": "🌐 OpenAI API地址",
    "ai_model": "🧠 OpenAI模型",
    "gemini_api_key": "💎 Gemini API密钥",
    "gemini_base_url": "🌐 Gemini API地址",
    "gemini_model": "💎 Gemini模型",
    "ai_enabled": "🚀 启用AI识别",
    "ai_confidence_threshold": "📊 AI置信度阈值",
    "openai_output_format": "🎯 OpenAI输出格式",
    "ai_temperature": "🔥 OpenAI温度",
    "gemini_temperature": "🔥 Gemini温度",
    "ai_auto_save": "💾 自动保存AI分析",
    "log_level": "📝 日志等级",
}


class ConfigManager:
    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.config_path = Path(config_path) if config_path is not None else CONFIG_PATH
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            with self._config_lock():
                if self.config_path.exists():
                    self.config = self._read_current_unlocked()
                else:
                    self.config = self._normalized(CONFIG_DEFAULT.copy())
                    self._write_unlocked(self.config)
        else:
            self.update_config()

    @property
    def lock_path(self) -> Path:
        return self.config_path.with_name(f".{self.config_path.name}.lock")

    @contextmanager
    def _config_lock(self):
        deadline = time.monotonic() + 10.0
        while True:
            try:
                with advisory_file_lock(
                    self.lock_path,
                    owner={"component": "config_manager"},
                ):
                    yield
                    return
            except FileLockBusyError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("配置文件正由另一进程保存，请稍后重试")
                time.sleep(0.01)

    @staticmethod
    def _normalized(data: Dict[str, Any]) -> Dict[str, Any]:
        current = {
            key: data.get(key, default)
            for key, default in CONFIG_DEFAULT.items()
        }
        if current.get("docker_mnt") == "/media":
            current["docker_mnt"] = "/volume1"
        for key in ("bangumi_path", "movie_path", "anime_path", "anime_movie_path"):
            value = current.get(key)
            if value == "/media":
                current[key] = "/volume1/Vault/Anime"
            elif isinstance(value, str) and value.startswith("/media/"):
                current[key] = "/volume1/Vault/Anime/" + value[len("/media/"):]
        return current

    def _read_current_unlocked(self) -> Dict[str, Any]:
        with open(self.config_path, "r", encoding="UTF-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("config.json 根节点必须是对象")
        return self._normalized(value)

    def _write_unlocked(self, value: Dict[str, Any]) -> None:
        temp_file_path = self.config_path.with_name(
            f".{self.config_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with open(temp_file_path, "x", encoding="UTF-8") as file:
                json.dump(value, file, indent=4, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            try:
                temp_file_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temp_file_path, self.config_path)
        finally:
            if temp_file_path.exists():
                temp_file_path.unlink()

    def write_config(self):
        with self._config_lock():
            value = self._normalized(dict(self.config))
            self._write_unlocked(value)
            self.config = value

    def update_config(self):
        with self._config_lock():
            with open(self.config_path, "r", encoding="UTF-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ValueError("config.json 根节点必须是对象")
            normalized = self._normalized(raw)
            if normalized != raw:
                self._write_unlocked(normalized)
            self.config = normalized

    def get_config(self, key: str) -> str:
        if key in self.config:
            return self.config[key]
        elif key in CONFIG_DEFAULT:
            return CONFIG_DEFAULT[key]
        else:
            return ''

    def set_config(self, key: str, value: Union[str, bool]) -> bool:
        if key not in CONFIG_DEFAULT:
            return False
        return self.set_configs({key: value})

    def set_configs(self, values: Dict[str, Any]) -> bool:
        if not values or any(key not in CONFIG_DEFAULT for key in values):
            return False
        patch = dict(values)
        for key, value in tuple(patch.items()):
            if key.endswith("_base_url") and value and isinstance(value, str):
                patch[key] = self._normalize_url(value)
        with self._config_lock():
            current = (
                self._read_current_unlocked()
                if self.config_path.exists()
                else CONFIG_DEFAULT.copy()
            )
            current.update(patch)
            current = self._normalized(current)
            self._write_unlocked(current)
            self.config = current
        return True

    def _normalize_url(self, url: str) -> str:
        """
        标准化URL格式，去除结尾斜杠并验证有效性

        Args:
            url: 原始URL

        Returns:
            标准化后的URL
        """
        if not url:
            return url

        # 去除结尾的斜杠
        url = url.rstrip('/')

        # 如果没有协议，默认添加https
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        # 验证URL格式
        try:
            parsed = urlparse(url)
            if not parsed.netloc:
                raise ValueError("Invalid URL format")
        except Exception:
            # 如果URL无效，返回原始值让用户自己处理
            pass

        return url

    def validate_url(self, url: str) -> bool:
        """
        验证URL是否有效

        Args:
            url: 要验证的URL

        Returns:
            是否为有效URL
        """
        if not url:
            return True  # 空URL视为有效（使用默认值）

        try:
            parsed = urlparse(url)
            return bool(parsed.netloc and parsed.scheme in ('http', 'https'))
        except Exception:
            return False


def effective_tmdb_api_key(config_manager: ConfigManager) -> str:
    """Return the env-first TMDB key without creating or writing config.

    Callers pass their existing config authority explicitly so reading a
    runtime credential never constructs ``ConfigManager`` or rewrites its
    persisted file.
    """
    runtime_key = os.getenv("TMDB_API_KEY", "").strip()
    if runtime_key:
        return runtime_key
    return str(config_manager.get_config("api_key") or "").strip()


cm = ConfigManager()
