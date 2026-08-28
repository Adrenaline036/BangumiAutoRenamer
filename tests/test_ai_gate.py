import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.ai import client as ai_client_module
from src.ai.models import AIAnalysisResult, EpisodeMapping
from src.rename.ai_processor import AIProcessor


class AIGateTests(unittest.TestCase):
    def test_compose_can_force_ai_off_for_rule_only_acceptance(self):
        values = {
            'ai_provider': 'openai',
            'ai_enabled': True,
            'ai_confidence_threshold': 'Low',
        }
        provider = Mock()
        provider.is_available.return_value = True

        with (
            patch.dict(os.environ, {'BAR_FORCE_DISABLE_AI': 'true'}),
            patch.object(
                ai_client_module.cm,
                'get_config',
                side_effect=lambda key: values.get(key),
            ),
            patch.object(
                ai_client_module,
                'OpenAIClient',
                return_value=provider,
            ),
        ):
            client = ai_client_module.AIClient()

        self.assertFalse(client.enabled)
        self.assertFalse(client.is_available())

    def test_ai_mapping_uses_canonical_jellyfin_season_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            regular = root / 'episode.mkv'
            special = root / 'special.mkv'
            regular.touch()
            special.touch()
            result = AIAnalysisResult(
                confidence='High',
                reason='test',
                file_mapping=[
                    EpisodeMapping(
                        file_path=regular.name,
                        tmdb_season=4,
                        tmdb_episode=1,
                        episode_type='regular',
                        confidence='High',
                    ),
                    EpisodeMapping(
                        file_path=special.name,
                        tmdb_season=0,
                        tmdb_episode=1,
                        episode_type='special',
                        confidence='High',
                    ),
                ],
            )

            mapping = object.__new__(AIProcessor).apply_ai_mapping(
                result,
                root,
                root / 'library',
            )

        self.assertEqual('Season 04', mapping[regular].parent.name)
        self.assertEqual('Season 00', mapping[special].parent.name)


if __name__ == '__main__':
    unittest.main()
