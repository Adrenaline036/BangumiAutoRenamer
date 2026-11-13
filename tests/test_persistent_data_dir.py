import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.config.config_manager import ConfigManager


class PersistentDataDirTests(unittest.TestCase):
    def test_manual_media_mount_paths_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps({
                "docker_mnt": "/media",
                "anime_path": "/media",
                "anime_movie_path": "/media/AnimeMovie",
                "movie_path": "/volume1/Media/Movie",
            }), encoding="utf-8")

            config = ConfigManager(config_path)

            self.assertEqual("/media", config.get_config("docker_mnt"))
            self.assertEqual("/media", config.get_config("anime_path"))
            self.assertEqual(
                "/media/AnimeMovie",
                config.get_config("anime_movie_path"),
            )
            self.assertEqual("/volume1/Media/Movie", config.get_config("movie_path"))

    def test_bar_data_dir_redirects_every_mutable_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env["BAR_DATA_DIR"] = temp_dir
            script = """
import json
from src.utils import path
print(json.dumps({
    'data': str(path.DATA_PATH),
    'config': str(path.CONFIG_PATH),
    'log': str(path.LOG_PATH),
    'record': str(path.RECORD_PATH),
    'task': str(path.TASK_PATH),
    'plan': str(path.PLAN_PATH),
    'review': str(path.PLAN_REVIEW_PATH),
}))
"""
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).parents[1],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            paths = json.loads(result.stdout.strip())
            root = Path(temp_dir).resolve()

            self.assertEqual(root, Path(paths.pop("data")))
            for value in paths.values():
                self.assertTrue(Path(value).is_relative_to(root), value)

    def test_config_survives_manager_recreation_in_external_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            first = ConfigManager(config_path)
            first.set_config("api_key", "persistent-tmdb-key")
            first.set_config("anime_path", "/volume1/Media/Anime")
            first.set_config("movie_path", "/volume1/Media/Movie")

            second = ConfigManager(config_path)

            self.assertEqual("persistent-tmdb-key", second.get_config("api_key"))
            self.assertEqual("/volume1/Media/Anime", second.get_config("anime_path"))
            self.assertEqual("/volume1/Media/Movie", second.get_config("movie_path"))
            self.assertFalse((config_path.parent / ".config.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
