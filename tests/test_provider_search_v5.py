import json
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path

import requests

from src.rename.plan_store import PlanStore, PlanStoreError
from src.rename.plan_review import (
    PlanReviewDecision,
    PlanReviewService,
    ReviewStatus,
)
from src.rename.plan_verifier import PlanVerifier
from src.rename.presentation import build_default_presentation_contracts
from src.rename.process import Rename
from src.rename.cleaner import SearchTitleCandidate
from src.rename.provider_search import (
    BangumiProviderClient,
    CANDIDATE_RANKING_VERSION,
    CandidateDecisionStatus,
    ProviderSearchCoordinator,
    ProviderSearchError,
    ProviderSearchStatus,
    ProviderWorkerOutput,
    build_search_policy,
    decide_provider_candidates,
    fuse_provider_candidates,
    parse_provider_item_url,
    resolve_provider_item_url,
)
from src.rename.rename_plan import (
    PlanEntry,
    PlanStatus,
    PresentationGroup,
    ProviderIdentity,
    RenamePlan,
)


class _Response:
    def __init__(self, payload, *, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


class _SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _RouteSession:
    def __init__(self, payloads):
        self.payloads = dict(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url not in self.payloads:
            raise AssertionError(f"unexpected URL: {url}")
        return _Response(self.payloads[url])


class _UnavailableBangumi:
    def search_animation(self, *_args, **_kwargs):
        raise ProviderSearchError("synthetic unavailable")


def _provider(namespace="tmdb", entity_type="movie", provider_id="997317"):
    return ProviderIdentity(
        "confirmed", namespace, entity_type, provider_id,
        "辉夜大小姐想让我告白：初吻不会结束", 2022,
        "manual_review", ("用户确认",),
    )


class ProviderSearchV5Tests(unittest.TestCase):
    def test_crosswalk_evidence_reads_bounded_relation_routes(self):
        base = "https://api.bgm.tv"
        subjects = {
            "1": {
                "id": 1, "name": "First", "platform": "TV",
                "date": "2020-01-01", "eps": 12,
            },
            "2": {
                "id": 2, "name": "Second", "platform": "TV",
                "date": "2021-01-01", "eps": 13,
            },
        }
        session = _RouteSession({
            f"{base}/v0/subjects/1": subjects["1"],
            f"{base}/v0/subjects/1/subjects": [{
                "relation": "续集", "subject": subjects["2"],
            }],
            f"{base}/v0/subjects/2": subjects["2"],
            f"{base}/v0/subjects/2/subjects": [{
                "relation": "前传", "subject": subjects["1"],
            }],
        })
        client = BangumiProviderClient(
            user_agent="BAR/test", session=session, sleeper=lambda _delay: None
        )

        frozen_subjects, frozen_relations = client.load_crosswalk_evidence("1")

        self.assertEqual({"1", "2"}, {
            item["provider_id"] for item in frozen_subjects
        })
        self.assertEqual({12, 13}, {
            item["episode_count"] for item in frozen_subjects
        })
        self.assertEqual(4, len(session.calls))
        self.assertTrue(all(call[1]["timeout"] for call in session.calls))
        self.assertEqual(
            {("1", "2", "续集"), ("2", "1", "前传")},
            {
                (item["from"], item["to"], item["relation"])
                for item in frozen_relations
            },
        )

    def test_crosswalk_never_uses_collection_popularity_as_episode_count(self):
        session = _RouteSession({
            "https://api.bgm.tv/v0/subjects/7": {
                "id": 7, "name": "Unknown Episodes", "platform": "TV",
                "collection": {"doing": 9999},
            },
            "https://api.bgm.tv/v0/subjects/7/subjects": [],
        })
        client = BangumiProviderClient(
            user_agent="BAR/test", session=session, sleeper=lambda _delay: None
        )

        subjects, _relations = client.load_crosswalk_evidence("7")

        self.assertIsNone(subjects[0]["episode_count"])

    def test_manual_provider_links_are_strictly_parsed(self):
        self.assertEqual(
            ("tmdb", "tv", "330299"),
            parse_provider_item_url("https://www.themoviedb.org/tv/330299-title"),
        )
        self.assertEqual(
            ("bangumi", "subject", "425211"),
            parse_provider_item_url("https://bgm.tv/subject/425211"),
        )
        with self.assertRaisesRegex(ProviderSearchError, "只支持"):
            parse_provider_item_url("https://example.com/subject/425211")

    def test_manual_tmdb_link_resolves_exact_stable_id(self):
        session = _Session({
            "id": 997317,
            "title": "辉夜大小姐想让我告白：初吻不会结束",
            "original_title": "Kaguya-sama wa Kokurasetai",
            "release_date": "2022-12-17",
            "poster_path": "/poster.jpg",
        })

        candidate = resolve_provider_item_url(
            "https://www.themoviedb.org/movie/997317",
            tmdb_api_key="secret",
            bangumi_user_agent="BAR/test",
            session=session,
        )

        self.assertEqual("997317", candidate["provider_id"])
        self.assertEqual("movie", candidate["entity_type"])
        self.assertEqual("movie_continuous", candidate["group_kind"])
        self.assertTrue(candidate["match_eligible"])
        self.assertEqual(
            "https://api.themoviedb.org/3/movie/997317",
            session.calls[0][0],
        )

    def test_manual_bangumi_link_resolves_exact_stable_id(self):
        session = _Session({
            "id": 425211,
            "name": "かぐや様は告らせたい",
            "name_cn": "辉夜大小姐想让我告白-初吻不会结束-",
            "date": "2023-03-31",
            "images": {"large": "https://lain.bgm.tv/pic/cover/l/example.jpg"},
        })

        candidate = resolve_provider_item_url(
            "https://bgm.tv/subject/425211",
            bangumi_user_agent="BAR/test",
            session=session,
        )

        self.assertEqual("425211", candidate["provider_id"])
        self.assertEqual("subject", candidate["entity_type"])
        self.assertEqual("bangumi", candidate["namespace"])
        self.assertTrue(candidate["match_eligible"])
    def test_animation_policy_schedules_all_configured_sources(self):
        policy = build_search_policy(
            domain="animation",
            media_hint="movie",
            tmdb_configured=True,
            bangumi_configured=True,
        )

        self.assertEqual(
            set(policy.expected_sources),
            {"bangumi", "tmdb_tv", "tmdb_movie"},
        )
        self.assertEqual(policy.scheduled_sources, policy.expected_sources)

    def test_general_explicit_media_policy_includes_bangumi(self):
        movie = build_search_policy(
            domain="general", media_hint="movie",
            tmdb_configured=True, bangumi_configured=True,
        )
        television = build_search_policy(
            domain="general", media_hint="tv",
            tmdb_configured=True, bangumi_configured=True,
        )

        self.assertEqual(movie.expected_sources, ("bangumi", "tmdb_movie"))
        self.assertEqual(television.expected_sources, ("bangumi", "tmdb_tv"))

    def test_bangumi_only_animation_search_is_visible_but_never_auto_selected(self):
        policy = build_search_policy(
            domain="animation", media_hint="unknown",
            tmdb_configured=False, bangumi_configured=True,
        )
        batch = ProviderSearchCoordinator({
            "bangumi": lambda: ProviderWorkerOutput(candidates=({
                "namespace": "bangumi", "entity_type": "subject",
                "provider_id": "7", "title": "Synthetic Animation",
                "year": 2024,
            },)),
        }).search(policy, query="Synthetic Animation", year=2024)
        decision = decide_provider_candidates(batch)

        self.assertEqual(
            [result.status for result in batch.results],
            [
                ProviderSearchStatus.SUCCESS,
                ProviderSearchStatus.UNCONFIGURED,
                ProviderSearchStatus.UNCONFIGURED,
            ],
        )
        self.assertEqual(decision.status, CandidateDecisionStatus.MANUAL_REQUIRED)
        self.assertEqual(len(batch.candidates), 1)

    def test_coordinator_starts_workers_concurrently_and_preserves_results(self):
        barrier = threading.Barrier(3)

        def worker(namespace, entity_type, provider_id):
            def run():
                barrier.wait(timeout=0.5)
                return ProviderWorkerOutput(candidates=({
                    "namespace": namespace,
                    "entity_type": entity_type,
                    "provider_id": provider_id,
                    "title": "Synthetic Work",
                    "year": 2024,
                },))
            return run

        policy = build_search_policy(
            domain="animation", media_hint="unknown",
            tmdb_configured=True, bangumi_configured=True,
        )
        batch = ProviderSearchCoordinator(
            {
                "bangumi": worker("bangumi", "subject", "1"),
                "tmdb_tv": worker("tmdb", "tv", "2"),
                "tmdb_movie": worker("tmdb", "movie", "3"),
            },
            deadline_seconds=1.0,
        ).search(policy, query="Synthetic Work", year=2024)

        self.assertTrue(batch.complete)
        self.assertEqual(
            {result.status for result in batch.results},
            {ProviderSearchStatus.SUCCESS},
        )
        self.assertEqual(len(batch.candidates), 3)

    def test_worker_exception_is_terminal_and_forces_manual_decision(self):
        def failed():
            raise RuntimeError("synthetic secret detail")

        def one_candidate():
            return ProviderWorkerOutput(candidates=({
                "namespace": "tmdb", "entity_type": "movie",
                "provider_id": "9", "title": "Only Candidate", "year": 2024,
            },))

        policy = build_search_policy(
            domain="unknown", media_hint="unknown",
            tmdb_configured=True, bangumi_configured=True,
        )
        batch = ProviderSearchCoordinator({
            "bangumi": failed,
            "tmdb_tv": lambda: ProviderWorkerOutput(),
            "tmdb_movie": one_candidate,
        }).search(policy, query="Only Candidate", year=2024)
        decision = decide_provider_candidates(batch)

        failed_result = next(
            item for item in batch.results if item.source == "bangumi"
        )
        self.assertEqual(failed_result.status, ProviderSearchStatus.ERROR)
        self.assertNotIn("synthetic secret detail", failed_result.message)
        self.assertFalse(batch.complete)
        self.assertEqual(decision.status, CandidateDecisionStatus.MANUAL_REQUIRED)
        self.assertIsNone(decision.decision_source)

    def test_overall_deadline_returns_without_waiting_for_slow_worker(self):
        release = threading.Event()

        def slow():
            release.wait(timeout=1.0)
            return ProviderWorkerOutput()

        policy = build_search_policy(
            domain="general", media_hint="movie",
            tmdb_configured=True, bangumi_configured=False,
        )
        started = time.monotonic()
        batch = ProviderSearchCoordinator(
            {"tmdb_movie": slow}, deadline_seconds=0.05,
        ).search(policy, query="Synthetic", year=None)
        elapsed = time.monotonic() - started
        release.set()

        self.assertLess(elapsed, 0.3)
        self.assertEqual(batch.results[0].status, ProviderSearchStatus.TIMEOUT)
        self.assertFalse(batch.complete)

    def test_bangumi_response_becomes_bounded_local_snapshot_without_token(self):
        session = _Session({"data": [{
            "id": 425211,
            "type": 2,
            "name": "かぐや様は告らせたい-ファーストキッスは終わらない-",
            "name_cn": "辉夜大小姐想让我告白：初吻不会结束",
            "date": "2022-12-17",
            "images": {"large": "https://lain.bgm.tv/pic/cover/l/example.jpg"},
        }]})
        client = BangumiProviderClient(
            token="secret-token", user_agent="BAR-Test/1.0 (test@example.invalid)",
            session=session,
        )

        result = client.search_animation("辉夜大小姐", year=2022)

        self.assertEqual(result[0]["provider_id"], "425211")
        self.assertEqual(result[0]["entity_type"], "subject")
        self.assertTrue(result[0]["snapshot_at"].endswith("+00:00"))
        self.assertNotIn("secret-token", json.dumps(result, ensure_ascii=False))
        headers = session.calls[0][1]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertIn("User-Agent", headers)
        self.assertEqual(session.calls[0][1]["json"]["filter"]["type"], [2])

    def test_animation_fusion_keeps_both_sources_and_explains_weight(self):
        candidates = [
            {"namespace": "tmdb", "entity_type": "movie", "provider_id": "997317",
             "title": "辉夜大小姐想让我告白：初吻不会结束", "year": 2022},
            {"namespace": "bangumi", "entity_type": "subject", "provider_id": "425211",
             "title": "辉夜大小姐想让我告白：初吻不会结束", "year": 2022},
        ]
        fused = fuse_provider_candidates(
            "辉夜大小姐想让我告白：初吻不会结束", candidates,
            year=2022, animation=True,
        )

        self.assertEqual({item["namespace"] for item in fused}, {"tmdb", "bangumi"})
        self.assertEqual(fused[0]["namespace"], "bangumi")
        self.assertIn("动画领域来源加权", fused[0]["match_reasons"])
        self.assertEqual(fused[0]["ranking_version"], CANDIDATE_RANKING_VERSION)
        self.assertEqual(fused[0]["match_score"], 0.9)
        self.assertEqual(
            fused[0]["match_components"]["title_similarity"], 1.0
        )

    def test_low_lexical_evidence_is_visible_but_not_rank_eligible(self):
        fused = fuse_provider_candidates(
            "辉夜大小姐想让我告白",
            [
                {"namespace": "bangumi", "entity_type": "subject",
                 "provider_id": "wrong", "title": "完全无关的机器人动画", "year": 2022},
                {"namespace": "tmdb", "entity_type": "movie",
                 "provider_id": "right", "title": "辉夜大小姐想让我告白", "year": 2022},
            ],
            year=2022, animation=True,
        )

        self.assertEqual(fused[0]["provider_id"], "right")
        wrong = next(item for item in fused if item["provider_id"] == "wrong")
        self.assertFalse(wrong["match_eligible"])
        self.assertEqual(wrong["match_score"], 0.0)
        self.assertIn("标题证据不足，仅作低置信参考", wrong["match_reasons"])

    def test_rate_limit_retry_is_bounded_and_token_never_enters_error(self):
        delays = []
        session = _SequenceSession([
            _Response({}, status_code=429, headers={"Retry-After": "99"}),
            _Response({}, status_code=429, headers={"Retry-After": "99"}),
        ])
        client = BangumiProviderClient(
            token="never-log-this-token", user_agent="BAR-Test/1.0",
            session=session, sleeper=delays.append,
        )

        with self.assertRaises(ProviderSearchError) as caught:
            client.search_animation("辉夜大小姐")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(delays, [])
        self.assertNotIn("never-log-this-token", str(caught.exception))

    def test_timeout_and_invalid_schema_fail_closed_without_credentials(self):
        timeout_client = BangumiProviderClient(
            token="secret-timeout-token", user_agent="BAR-Test/1.0",
            session=_SequenceSession([requests.Timeout(), requests.Timeout()]),
            sleeper=lambda _seconds: None,
        )
        with self.assertRaises(ProviderSearchError) as timeout_error:
            timeout_client.search_animation("赛马娘")
        self.assertNotIn("secret-timeout-token", str(timeout_error.exception))

        invalid_client = BangumiProviderClient(
            user_agent="BAR-Test/1.0", session=_Session({"unexpected": []}),
        )
        with self.assertRaisesRegex(ProviderSearchError, "返回资料格式无效"):
            invalid_client.search_animation("赛马娘")


    def test_v4_unique_provider_migrates_to_v5_top_level_and_changes_fingerprint(self):
        provider = _provider()
        group = PresentationGroup(
            "movie-continuous", "movie_continuous", "电影连续版", None, provider
        )
        entry = PlanEntry(Path("source/a.mkv"), Path("target/a.mkv"), media_kind="video")
        plan = RenamePlan(
            "migrate", Path("source"), (entry,), expected_sources=(entry.source_path,),
            metadata={"name": provider.title, "is_anime": True},
            presentation_groups=(group,),
        )
        v5_payload = plan.to_dict()
        v4_payload = dict(v5_payload)
        v4_payload["schema_version"] = 4
        v4_payload.pop("work_identity")
        v4_payload.pop("linked_provider_identities")
        v4_payload.pop("migrated_from_schema")
        v4_payload.pop("migration_source_fingerprint")
        v4_payload["presentation_groups"] = [dict(v5_payload["presentation_groups"][0])]
        v4_payload["presentation_groups"][0]["provider_identity"] = provider.to_dict()

        migrated = RenamePlan.from_dict(v4_payload)

        self.assertEqual(migrated.to_dict()["schema_version"], 7)
        self.assertNotIn("provider_identity", migrated.to_dict()["presentation_groups"][0])
        self.assertEqual(migrated.work_identity.primary_provider.provider_id, "997317")
        self.assertEqual(migrated.migrated_from_schema, 4)
        canonical_v4 = json.dumps(v4_payload, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"))
        original_fingerprint = hashlib.sha256(canonical_v4.encode()).hexdigest()
        self.assertEqual(migrated.migration_source_fingerprint, original_fingerprint)
        self.assertNotEqual(
            original_fingerprint,
            PlanStore.fingerprint(migrated),
        )

    def test_v4_history_is_verified_before_migration_and_never_overwritten(self):
        provider = _provider()
        group = PresentationGroup(
            "movie-continuous", "movie_continuous", "电影连续版", None, provider
        )
        plan = RenamePlan(
            "history-v4", Path("source"), (),
            metadata={"name": provider.title, "is_anime": True},
            presentation_groups=(group,),
        )
        payload = plan.to_dict()
        payload["schema_version"] = 4
        payload.pop("work_identity")
        payload.pop("linked_provider_identities")
        payload.pop("migrated_from_schema")
        payload.pop("migration_source_fingerprint")
        payload["presentation_groups"] = [{
            **payload["presentation_groups"][0],
            "provider_identity": provider.to_dict(),
        }]
        old_fingerprint = PlanStore.fingerprint_payload(payload)
        original_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = PlanStore(Path(temp_dir))
            history_path = (
                store.root / "history" / plan.task_id / f"{old_fingerprint}.json"
            )
            history_path.parent.mkdir(parents=True)
            history_path.write_text(original_json, encoding="utf-8")

            migrated = store.load_version(plan.task_id, old_fingerprint)

            self.assertEqual(history_path.read_text(encoding="utf-8"), original_json)
            self.assertEqual(migrated.migrated_from_schema, 4)
            self.assertEqual(migrated.migration_source_fingerprint, old_fingerprint)
            self.assertNotEqual(PlanStore.fingerprint(migrated), old_fingerprint)

            payload["metadata"]["name"] = "tampered"
            history_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(PlanStoreError, "fingerprint mismatch"):
                store.load_version(plan.task_id, old_fingerprint)

    def test_v4_approval_is_reset_after_current_plan_migrates(self):
        provider = _provider()
        entry = PlanEntry(
            Path("source/a.mkv"), Path("target/a.mkv"), media_kind="video"
        )
        plan = RenamePlan(
            "approved-v4", Path("source"), (entry,),
            expected_sources=(entry.source_path,),
            metadata={"name": provider.title, "is_anime": True},
            presentation_groups=(PresentationGroup(
                "movie", "movie_continuous", "电影", None, provider
            ),),
        )
        payload = plan.to_dict()
        payload["schema_version"] = 4
        payload.pop("work_identity")
        payload.pop("linked_provider_identities")
        payload.pop("migrated_from_schema")
        payload.pop("migration_source_fingerprint")
        payload["presentation_groups"] = [{
            **payload["presentation_groups"][0],
            "provider_identity": provider.to_dict(),
        }]
        old_fingerprint = PlanStore.fingerprint_payload(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlanStore(root / "plans")
            store.root.mkdir(parents=True)
            store.path_for(plan.task_id).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            review_root = root / "reviews"
            review_root.mkdir()
            old_decision = PlanReviewDecision(
                plan.task_id, old_fingerprint, ("legacy-entry",),
                ReviewStatus.APPROVED,
            )
            (review_root / f"{plan.task_id}.json").write_text(
                json.dumps(old_decision.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )

            migrated = store.load(plan.task_id)
            decision = PlanReviewService(store, review_root).load_or_create(migrated)

        self.assertEqual(migrated.migrated_from_schema, 4)
        self.assertNotEqual(PlanStore.fingerprint(migrated), old_fingerprint)
        self.assertEqual(decision.status, ReviewStatus.DRAFT)
        self.assertEqual(decision.approved_entry_ids, ())
        self.assertEqual(decision.plan_fingerprint, PlanStore.fingerprint(migrated))

    def test_v4_multiple_confirmed_ids_migrate_unresolved_and_fail_closed(self):
        groups = (
            PresentationGroup("movie", "movie_continuous", "电影", None, _provider()),
            PresentationGroup("tv", "tv_episodes", "分集", None,
                              _provider("tmdb", "tv", "330299")),
        )
        plan = RenamePlan(
            "multiple", Path("source"), (), metadata={"is_anime": True},
            presentation_groups=groups,
        )

        self.assertEqual(plan.work_identity.status, "unresolved")
        self.assertEqual(len(plan.linked_provider_identities), 2)
        result = PlanVerifier().verify(plan)
        self.assertEqual(result.status, PlanStatus.INVALID)
        self.assertIn("work_identity_unconfirmed", {issue.code for issue in result.issues})

    def test_bangumi_identity_does_not_force_episode_or_movie_layout(self):
        provider = _provider("bangumi", "subject", "425211")
        source = Path("source/01.mkv")
        target = Path("target/Season01/S01E01.mkv")
        groups, assets = build_default_presentation_contracts(
            {source: target}, group_id="episodes", group_kind="tv_episodes",
            display_name="分集版", provider_identity=provider,
        )
        entry = PlanEntry.from_paths(source, target)
        entry = __import__("dataclasses").replace(entry, presentation_asset=assets[source])
        plan = RenamePlan(
            "bgm-layout", Path("source"), (entry,), expected_sources=(source,),
            metadata={"is_anime": True}, presentation_groups=groups,
        )

        # Identity and episode layout are valid independent dimensions, but this
        # source-free structural fixture cannot stand in for a current Decision.
        report = PlanVerifier().verify(plan)
        self.assertEqual(PlanStatus.READY, report.status)
        self.assertEqual(set(), {issue.code for issue in report.issues})
        restored = RenamePlan.from_dict(plan.to_dict())
        self.assertEqual(restored.work_identity.primary_provider.provider_id, "425211")
        self.assertEqual(restored.work_identity.domain, "animation")
        self.assertEqual(
            restored.work_identity.domain_source, "provider_selection"
        )

    def test_domain_uses_metadata_only_as_pre_identity_evidence(self):
        tmdb = _provider()
        general = RenamePlan(
            "general", Path("source"), (), metadata={"is_anime": False},
            presentation_groups=(PresentationGroup(
                "movie", "movie_continuous", "电影", None, tmdb
            ),),
        )
        unknown = RenamePlan(
            "unknown", Path("source"), (), metadata={},
            presentation_groups=(PresentationGroup(
                "movie", "movie_continuous", "电影", None, tmdb
            ),),
        )
        conflict = RenamePlan(
            "conflict", Path("source"), (), metadata={"is_anime": False},
            presentation_groups=(PresentationGroup(
                "episodes", "tv_episodes", "分集", None,
                _provider("bangumi", "subject", "425211")
            ),),
        )

        self.assertEqual(
            (general.work_identity.domain, general.work_identity.domain_source),
            ("general", "task_metadata"),
        )
        self.assertEqual(
            (unknown.work_identity.domain, unknown.work_identity.domain_source),
            ("unknown", "unknown"),
        )
        self.assertEqual(
            (conflict.work_identity.domain, conflict.work_identity.domain_source),
            ("animation", "provider_selection"),
        )


if __name__ == "__main__":
    unittest.main()
