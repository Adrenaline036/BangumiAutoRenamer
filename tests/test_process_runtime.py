import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.rename.process import Rename, _with_parent_title_candidates
from src.rename.cleaner import parse_release_title
from src.rename.plan_store import PlanStore
from src.rename.plan_verifier import PlanVerifier
from src.rename.rename_plan import PlanStatus


class ProcessRuntimeDependencyTests(unittest.TestCase):
    def test_bracketed_parent_title_is_added_for_cross_language_tv_discovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / '[A-1] 辉夜大小姐想让我告白'
            child = parent / (
                '[VCB-Studio] Kaguya-sama wa Kokurasetai - '
                'First Kiss wa Owaranai [Ma10p_1080p]'
            )
            child.mkdir(parents=True)
            parsed = parse_release_title(child.name)

            candidates = _with_parent_title_candidates(parsed.candidates, child)

        self.assertIn('辉夜大小姐想让我告白', [item.title for item in candidates])
        self.assertTrue(any('上一级作品目录' in item.reason for item in candidates))

    def test_unbracketed_generic_parent_is_not_added(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            child = Path(temp_dir) / 'Movie Title'
            child.mkdir()
            parsed = parse_release_title(child.name)
            self.assertEqual(
                parsed.candidates,
                _with_parent_title_candidates(parsed.candidates, child),
            )

    def test_multi_video_movie_stops_at_inventory_without_plan_mapping(self):
        renamer = object.__new__(Rename)
        renamer.search = Mock(TMDB_KEY='offline-test-key')
        renamer.search.get_tv_info.return_value = ('', None)
        renamer.search.get_movie_info.return_value = (
            'Kaguya-sama: The First Kiss That Never Ends',
            {
                'id': 997317,
                'title': 'Kaguya-sama: The First Kiss That Never Ends',
                'release_date': '2022-12-17',
                'genres': [{'name': 'Animation'}],
            },
        )
        renamer.R = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'Kaguya-sama First Kiss 2022'
            source.mkdir()
            (source / 'Movie Cut.mkv').touch()
            (source / 'TV Part 01.mkv').touch()
            task_root = root / 'tasks'
            plan_root = root / 'plans'
            task_root.mkdir()
            plan_root.mkdir()
            with (
                patch('src.rename.process.TASK_PATH', task_root),
                patch('src.rename.process.PLAN_PATH', plan_root),
            ):
                result = renamer._process(
                    source,
                    _is_anime=True,
                    _tuuid='movie-inventory-test',
                )

            task = json.loads(
                (task_root / 'movie-inventory-test.json').read_text('utf-8')
            )
            plan_files = list(plan_root.iterdir())

        self.assertIn(
            'MULTIPLE_VIDEO_FILES_REQUIRE_LAYOUT_CLASSIFICATION', result
        )
        self.assertEqual('invalid', task['workflow_status'])
        self.assertEqual('inventory_only', task['plan_status'])
        self.assertTrue(task['execution_blocked'])
        self.assertEqual('requires_manual_workflow', task['inventory_status'])
        self.assertEqual(0, task['plan_entry_count'])
        self.assertEqual(2, task['movie_inventory']['video_count'])
        self.assertIsNone(task['is_movie'])
        self.assertIsNone(task['provider_identity'])
        self.assertEqual(
            {'997317'},
            {
                str(candidate['provider_id'])
                for candidate in task['provider_candidates']['movie_continuous']
            },
        )
        self.assertTrue(all(
            not Path(entry['relative_path']).is_absolute()
            for entry in task['movie_inventory']['entries']
        ))
        self.assertEqual([], plan_files)
        self.assertEqual({}, renamer.R)

    def test_multi_video_movie_keeps_low_confidence_tv_choices_for_manual_review(self):
        renamer = object.__new__(Rename)
        renamer.search = Mock(TMDB_KEY='offline-test-key')
        renamer.search.get_tv_info.return_value = (
            '辉夜大小姐想让我告白：初吻不会结束',
            {
                'id': 330299,
                'name': '辉夜大小姐想让我告白：初吻不会结束',
                'original_name': 'かぐや様は告らせたい-ファーストキッスは終わらない-',
                'first_air_date': '2023-04-01',
                'genres': [{'name': 'Animation'}],
                '_bar_search_alternatives': [{
                    'id': 330299,
                    'name': '辉夜大小姐想让我告白：初吻不会结束',
                    'original_name': 'かぐや様は告らせたい-ファーストキッスは終わらない-',
                    'first_air_date': '2023-04-01',
                }],
            },
        )
        renamer.search.get_movie_info.return_value = (
            'Kaguya-sama: The First Kiss That Never Ends',
            {
                'id': 997317,
                'title': 'Kaguya-sama: The First Kiss That Never Ends',
                'release_date': '2022-12-17',
                'genres': [{'name': 'Animation'}],
            },
        )
        renamer.R = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parent = root / '[A-1] 辉夜大小姐想让我告白'
            source = parent / (
                '[VCB-Studio] Kaguya-sama wa Kokurasetai - '
                'First Kiss wa Owaranai [Ma10p_1080p]'
            )
            source.mkdir(parents=True)
            (source / 'Movie Cut.mkv').touch()
            (source / 'TV Part 01.mkv').touch()
            task_root = root / 'tasks'
            plan_root = root / 'plans'
            task_root.mkdir()
            plan_root.mkdir()
            with (
                patch('src.rename.process.TASK_PATH', task_root),
                patch('src.rename.process.PLAN_PATH', plan_root),
            ):
                renamer._process(source, _is_anime=True, _tuuid='kaguya-candidates')
            task = json.loads((task_root / 'kaguya-candidates.json').read_text('utf-8'))

        self.assertIsNone(task['is_movie'])
        self.assertEqual(
            {'movie_continuous'},
            set(task['provider_candidates']),
        )
        self.assertEqual(
            {'997317', '330299'},
            {
                str(candidate['provider_id'])
                for candidate in task['provider_candidates']['movie_continuous']
            },
        )

    def test_multi_video_movie_keeps_bangumi_choice_without_tmdb_tv_result(self):
        renamer = object.__new__(Rename)
        renamer.search = Mock(TMDB_KEY='offline-test-key')
        renamer.search.get_tv_info.return_value = ('', None)
        renamer.search.get_movie_info.return_value = (
            'Uma Musume Pretty Derby: ROAD TO THE TOP',
            {
                'id': 123456,
                'title': 'Uma Musume Pretty Derby: ROAD TO THE TOP',
                'release_date': '2024-05-10',
                'genres': [{'name': 'Animation'}],
            },
        )
        renamer.bangumi_search = Mock()
        renamer.bangumi_search.search_animation.return_value = [{
            'namespace': 'bangumi',
            'entity_type': 'subject',
            'provider_id': '444888',
            'title': '赛马娘 巅峰之路',
            'original_title': 'ウマ娘 プリティーダービー ROAD TO THE TOP',
            'aliases': ['Uma Musume Pretty Derby: ROAD TO THE TOP'],
            'year': 2023,
            'provider_rank': 0,
        }]
        renamer.R = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parent = root / '[Cygames] 赛马娘'
            source = parent / (
                '[VCB-Studio] Uma Musume Pretty Derby - ROAD TO THE TOP '
                '[Ma10p_1080p]'
            )
            source.mkdir(parents=True)
            (source / 'Movie Cut.mkv').touch()
            (source / 'TV Part 01.mkv').touch()
            task_root = root / 'tasks'
            plan_root = root / 'plans'
            task_root.mkdir()
            plan_root.mkdir()
            with (
                patch('src.rename.process.TASK_PATH', task_root),
                patch('src.rename.process.PLAN_PATH', plan_root),
            ):
                renamer._process(source, _is_anime=True, _tuuid='uma-candidates')
            task = json.loads((task_root / 'uma-candidates.json').read_text('utf-8'))

        candidates = task['provider_candidates']['movie_continuous']
        self.assertIsNone(task['is_movie'])
        self.assertEqual(
            {('tmdb', '123456'), ('bangumi', '444888')},
            {
                (candidate['namespace'], str(candidate['provider_id']))
                for candidate in candidates
            },
        )

    def test_task_entry_reaches_type_detection_after_title_cleanup(self):
        renamer = object.__new__(Rename)
        renamer.search = SimpleNamespace(TMDB_KEY='offline-test-key')

        def stop_after_title_cleanup(*args, **kwargs):
            raise RuntimeError('type detection reached')

        renamer.check_task_type = stop_after_title_cleanup

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'Example Show Season 2 [1080p]'
            source.mkdir()

            with self.assertRaisesRegex(RuntimeError, 'type detection reached'):
                renamer._process(source)

    def test_task_entry_passes_clean_title_and_year_to_type_detection(self):
        renamer = object.__new__(Rename)
        renamer.search = SimpleNamespace(TMDB_KEY='offline-test-key')
        captured = {}

        def stop_after_title_cleanup(
            _uuid,
            rtpath_name,
            year,
            path,
            is_anime=None,
            is_movie=None,
            search_candidates=None,
        ):
            captured['title'] = rtpath_name
            captured['year'] = year
            captured['candidates'] = search_candidates
            raise RuntimeError('type detection reached')

        renamer.check_task_type = stop_after_title_cleanup

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / (
                'Lucky Star 2007 [BD 1920x1080 HEVC FLAC] '
                '- LittleBakas!'
            )
            source.mkdir()

            with self.assertRaisesRegex(RuntimeError, 'type detection reached'):
                renamer._process(source)

        self.assertEqual('Lucky Star', captured['title'])
        self.assertEqual(2007, captured['year'])
        self.assertEqual(
            ('Lucky Star 2007', 0),
            captured['candidates'][1].key,
        )

    def test_custom_title_rebuilds_candidates_and_supplies_year(self):
        renamer = object.__new__(Rename)
        renamer.search = SimpleNamespace(TMDB_KEY='offline-test-key')
        captured = {}

        def stop_after_title_cleanup(
            _uuid,
            rtpath_name,
            year,
            path,
            is_anime=None,
            is_movie=None,
            search_candidates=None,
        ):
            captured['title'] = rtpath_name
            captured['year'] = year
            captured['candidates'] = search_candidates
            raise RuntimeError('type detection reached')

        renamer.check_task_type = stop_after_title_cleanup

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'Ranma ½ [BDRip]'
            source.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'type detection reached'):
                renamer._process(source, cus_name='Ranma ½ 1989')

        self.assertEqual('Ranma ½', captured['title'])
        self.assertEqual(1989, captured['year'])
        self.assertEqual(1989, captured['candidates'][0].year)

    def test_lucky_star_full_process_uses_tv_route_and_removes_metadata_year(self):
        renamer = object.__new__(Rename)
        renamer.search = Mock(TMDB_KEY='offline-test-key')
        renamer.search.get_tv_info.return_value = (
            '幸运☆星',
            {
                'id': 3197,
                'name': '幸运☆星',
                'original_name': 'Lucky Star',
                'first_air_date': '2007-04-08',
                'genres': [{'name': 'Animation'}],
                'seasons': [{'season_number': 1, 'name': 'Season 1'}],
            },
        )
        renamer.search.get_movie_info.return_value = ('', None)
        renamer.ai_processor = SimpleNamespace(
            ai_client=SimpleNamespace(is_available=lambda: False)
        )
        renamer.R = {}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / (
                'Lucky Star 2007 [BD 1920x1080 HEVC FLAC] '
                '- LittleBakas!'
            )
            source.mkdir()
            (source / (
                'Lucky Star 2007 - EP01 '
                '[BD 1920x1080 HEVC-yuv420p10 FLAC Chap] '
                '- LittleBakas!.mkv'
            )).touch()
            (source / (
                'Lucky Star 2007 - OVA '
                '[BD 1920x1080 HEVC-yuv420p10 FLAC Chap] '
                '- LittleBakas!.mkv'
            )).touch()
            extras = source / 'SPs'
            extras.mkdir()
            (extras / (
                'Lucky Star 2007 - 特典映像1 '
                '[BD 1920x1080 HEVC-yuv420p10 FLAC] '
                '- LittleBakas!.mkv'
            )).touch()
            renamer.ANIME_PATH = root / 'Anime'
            renamer.ANIME_MOVIE_PATH = root / 'AnimeMovies'
            renamer.MOVIE_PATH = root / 'Movies'
            renamer.BANGUMI_PATH = root / 'TV'
            renamer.get_season_id = Mock(return_value=1)

            with (
                patch('src.rename.process.TASK_PATH', root / 'tasks'),
                patch('src.rename.process.PLAN_PATH', root / 'plan'),
            ):
                (root / 'tasks').mkdir()
                result = renamer._process(source, _is_anime=True)
                persisted_plan = PlanStore(root / 'plan').load(
                    renamer.last_plan.task_id
                )
                mapping = persisted_plan.as_mapping()

        verification = PlanVerifier().verify(persisted_plan)
        self.assertEqual(PlanStatus.INVALID, verification.status)
        self.assertIn(
            "tv_authority_incomplete",
            {issue.code for issue in verification.issues},
        )
        self.assertIn("尚未确认所属系列和季度", result)
        self.assertEqual(persisted_plan, renamer.last_plan)
        self.assertEqual(
            ["tv-episodes"],
            [group.group_id for group in persisted_plan.presentation_groups],
        )
        renamer.search.get_movie_info.assert_called()
        targets = list(mapping.values())
        self.assertTrue(any(
            target.name == 'S01E01 - Lucky Star - EP01.mkv'
            for target in targets
        ))
        self.assertTrue(any(
            target.name == 'Lucky Star - OVA.mkv'
            for target in targets
        ))
        self.assertTrue(any(
            target.parent.name == 'Extras'
            and target.name == 'Lucky Star - 特典映像1.mkv'
            for target in targets
        ))
        self.assertTrue(all(
            'Lucky Star 2007 -' not in str(target)
            for target in targets
        ))


if __name__ == '__main__':
    unittest.main()
