"""Phase A regression contracts for subject-owned TV work/season segmentation.

This module is deliberately pure-model evidence.  It does not create tasks,
Plans, Catalog rows, media files, or hardlinks.  The later Phase B contract is
responsible for proving the real AddTask -> Provider -> worker -> Plan path.

These tests remain pure-model evidence after GREEN. They keep every RED
assertion while deliberately leaving real AddTask/Provider/worker persistence
to the later Phase B contract.
"""

from __future__ import annotations

import re
import hashlib
import json
import unittest
from typing import Mapping

import src.rename.provider_link_resolver as resolver_api


def _subject(
    provider_id: str,
    title: str,
    original_title: str,
    air_date: str,
    episode_count: int,
) -> dict[str, object]:
    return {
        "namespace": "bangumi",
        "entity_type": "subject",
        "provider_id": provider_id,
        "title": title,
        "original_title": original_title,
        "air_date": air_date,
        "episode_count": episode_count,
        "platform": "TV",
    }


def _tmdb(
    provider_id: str,
    title: str,
    original_title: str,
    first_air_date: str,
    seasons: tuple[tuple[int, str, int], ...],
) -> dict[str, object]:
    return {
        "namespace": "tmdb",
        "entity_type": "tv",
        "provider_id": provider_id,
        "tmdb_tv_id": provider_id,
        "title": title,
        "original_title": original_title,
        "first_air_date": first_air_date,
        "seasons": tuple(
            {
                "season_number": number,
                "air_date": air_date,
                "episode_count": count,
            }
            for number, air_date, count in seasons
        ),
    }


def _relation(source: str, target: str) -> dict[str, str]:
    return {"from": source, "to": target, "relation": "sequel"}


def _query(
    query: str,
    locale: str,
    *,
    candidate: Mapping[str, object] | None = None,
    candidates: tuple[Mapping[str, object], ...] | None = None,
    outcome: str = "ok",
    truncated: bool = False,
) -> dict[str, object]:
    candidate_rows = candidates
    if candidate_rows is None:
        candidate_rows = () if candidate is None else (candidate,)
    normalized_candidates = []
    for item in candidate_rows:
        row = dict(item)
        if row.get("namespace") == "tmdb" and row.get("entity_type") == "tv":
            row.setdefault("tmdb_tv_id", row.get("provider_id"))
        row.setdefault("match_tier", "exact")
        row.setdefault(
            "score_components",
            {"number_match": True, "exact": True, "containment": True},
        )
        normalized_candidates.append(row)
    return {
        "query": query,
        "locale": locale,
        "year": None,
        "outcome": outcome,
        "truncated": truncated,
        "candidates": tuple(normalized_candidates),
    }


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _observation(
    subject_id: str,
    *queries: Mapping[str, object],
) -> dict[str, object]:
    ordered_queries = tuple(
        sorted(
            (dict(item) for item in queries),
            key=lambda item: (
                str(item.get("locale") or ""),
                str(item.get("query") or ""),
                str(item.get("year") or ""),
            ),
        )
    )
    payload = {
        "bangumi_subject_id": subject_id,
        "observed_at": "2026-08-24T00:00:00+00:00",
        "policy_version": "subject-candidate-v1",
        "queries": ordered_queries,
    }
    return {**payload, "response_fingerprint": _canonical_hash(payload)}


def _batch(
    selected_subject_id: str,
    observations: tuple[Mapping[str, object], ...],
    *,
    truncated: bool = False,
    boundary_complete: bool = True,
) -> dict[str, object]:
    ordered_observations = tuple(
        sorted(
            (dict(item) for item in observations),
            key=lambda item: str(item.get("bangumi_subject_id") or ""),
        )
    )
    payload = {
        "schema_version": 1,
        "selected_subject_id": selected_subject_id,
        "observed_at": "2026-08-24T00:00:00+00:00",
        "policy_version": "subject-candidate-v1",
        "truncated": truncated,
        "boundary_complete": boundary_complete,
        "subject_observations": ordered_observations,
    }
    return {**payload, "evidence_fingerprint": _canonical_hash(payload)}


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return payload
    raise AssertionError(f"{label} must be a mapping-compatible value")


def _resolve_subject_owned(
    *,
    selected_subject_id: str,
    subjects: tuple[Mapping[str, object], ...],
    relations: tuple[Mapping[str, object], ...],
    observations: Mapping[str, object],
):
    try:
        return resolver_api.resolve_provider_link(
            selected_provider={
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": selected_subject_id,
            },
            bangumi_subjects=subjects,
            bangumi_relations=relations,
            subject_observations=observations,
        )
    except TypeError as exc:
        raise AssertionError(
            "resolve_provider_link does not consume subject-owned observations"
        ) from exc


def _reason_codes(value: object) -> set[str]:
    return {str(item) for item in (_field(value, "reason_codes", ()) or ())}


def _resolution_fingerprint(value: object) -> str:
    fingerprint = str(_field(value, "resolution_fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise AssertionError("resolution must expose a canonical SHA-256 fingerprint")
    return fingerprint


def _named_fingerprint(value: object, field: str) -> str:
    fingerprint = str(_field(value, field) or "")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise AssertionError(f"{field} must be a canonical SHA-256 fingerprint")
    return fingerprint


def _episode_pairs(value: object) -> tuple[tuple[int, int], ...]:
    rows = _field(value, "episode_mapping", ()) or ()
    if isinstance(rows, Mapping):
        return tuple(sorted((int(source), int(target)) for source, target in rows.items()))
    pairs = []
    for row in rows:
        mapping = _mapping(row, "episode mapping row")
        pairs.append((int(mapping["source_episode"]), int(mapping["target_episode"])))
    return tuple(sorted(pairs))


def _expected_projection_fingerprint(value: object) -> str:
    payload = {
        "mode": str(_field(value, "mode") or ""),
        "offset": int(_field(value, "offset", 0)),
        "inventory_scope": str(_field(value, "inventory_scope") or ""),
        "target_episode_start": int(_field(value, "target_episode_start", 0)),
        "target_episode_end": int(_field(value, "target_episode_end", 0)),
        "episode_mapping": tuple(
            {"source_episode": source, "target_episode": target}
            for source, target in _episode_pairs(value)
        ),
    }
    return _canonical_hash(payload)


def _range_rows(value: object) -> tuple[tuple[str, str, int, int, int], ...]:
    rows = []
    for item in (_field(value, "derived_episode_ranges", ()) or ()):
        row = _mapping(item, "derived episode range")
        rows.append(
            (
                str(row.get("bangumi_subject_id") or ""),
                str(row.get("tmdb_tv_id") or ""),
                int(row["season_number"]),
                int(row["episode_start"]),
                int(row["episode_end"]),
            )
        )
    return tuple(sorted(rows))


def _assert_ranges(
    case: unittest.TestCase,
    value: object,
    expected: tuple[tuple[str, str, int, int, int], ...],
) -> None:
    case.assertEqual(tuple(sorted(expected)), _range_rows(value))
    validate = getattr(resolver_api, "validate_derived_episode_ranges", None)
    case.assertTrue(callable(validate), "Phase A lacks range reservation validation")
    validation = validate(_field(value, "derived_episode_ranges", ()) or ())
    case.assertTrue(bool(_field(validation, "valid", False)))


def _boundary_rows(value: object) -> tuple[tuple[str, str, str, str, str], ...]:
    rows = []
    for item in (_field(value, "boundaries", ()) or ()):
        row = _mapping(item, "work boundary")
        rows.append(
            (
                str(row.get("from_subject_id") or ""),
                str(row.get("to_subject_id") or ""),
                str(row.get("relation") or ""),
                str(row.get("boundary_kind") or ""),
                str(row.get("outside_tmdb_tv_id") or ""),
            )
        )
    return tuple(sorted(rows))


STEINS = _subject("10380", "命运石之门", "STEINS;GATE", "2011-04-06", 24)
STEINS_ZERO = _subject(
    "129807",
    "命运石之门 0",
    "シュタインズ・ゲート ゼロ",
    "2018-04-12",
    23,
)
TMDB_STEINS = _tmdb("42509", "命运石之门", "Steins;Gate", "2011-04-06", ((1, "2011-04-06", 24),))
TMDB_STEINS_ZERO = _tmdb(
    "78102",
    "命运石之门 0",
    "Steins;Gate 0",
    "2018-04-12",
    ((1, "2018-04-12", 23),),
)

YAMA_SUBJECTS = (
    _subject("41658", "向山进发", "ヤマノススメ", "2013-01-03", 13),
    _subject("86670", "向山进发 第二季", "ヤマノススメ セカンドシーズン", "2014-07-09", 24),
    _subject("216310", "向山进发 第三季", "ヤマノススメ サードシーズン", "2018-07-02", 13),
    _subject("290980", "向山进发 Next Summit", "ヤマノススメ Next Summit", "2022-10-05", 12),
)
YAMA_RELATIONS = tuple(
    _relation(left["provider_id"], right["provider_id"])
    for left, right in zip(YAMA_SUBJECTS, YAMA_SUBJECTS[1:])
)
TMDB_YAMA = _tmdb(
    "53918",
    "向山进发",
    "Encouragement of Climb",
    "2013-01-03",
    (
        (1, "2013-01-03", 13),
        (2, "2014-07-09", 24),
        (3, "2018-07-02", 13),
        (4, "2022-10-05", 12),
    ),
)

EIGHTY_SIX = (
    _subject("302189", "86—不存在的战区—", "86―エイティシックス―", "2021-04-11", 11),
    _subject("331887", "86—不存在的战区— 第2部分", "86―エイティシックス― 第2クール", "2021-10-03", 12),
)
TMDB_EIGHTY_SIX = _tmdb(
    "100565",
    "86—不存在的战区—",
    "86 EIGHTY-SIX",
    "2021-04-11",
    ((1, "2021-04-11", 23),),
)


class ProviderSubjectSegmentationPhaseARedContracts(unittest.TestCase):
    def test_selected_subject_is_collected_first_and_truncation_is_not_a_boundary(self):
        collector = getattr(resolver_api, "collect_subject_observations", None)
        self.assertTrue(
            callable(collector),
            "Phase A lacks the bounded subject-owned observation collector",
        )
        calls: list[str] = []

        def search_subject(subject: Mapping[str, object]):
            subject_id = str(subject["provider_id"])
            calls.append(subject_id)
            legacy_candidate = {
                key: value for key, value in TMDB_YAMA.items() if key != "tmdb_tv_id"
            }
            raw_query = _query(
                str(subject["original_title"]),
                "ja-JP",
                candidate=legacy_candidate,
            )
            raw_query["candidates"] = tuple(
                {
                    key: value
                    for key, value in candidate.items()
                    if key != "tmdb_tv_id"
                }
                for candidate in raw_query["candidates"]
            )
            return _observation(
                subject_id,
                raw_query,
            )

        result = collector(
            selected_subject_id="290980",
            bangumi_subjects=YAMA_SUBJECTS,
            bangumi_relations=YAMA_RELATIONS,
            search_subject=search_subject,
            max_subjects=2,
        )
        payload = _mapping(result, "subject observation batch")
        self.assertEqual("290980", calls[0], "selected subject must never be starved")
        self.assertTrue(payload.get("truncated"), "bounded traversal must persist truncation")
        self.assertFalse(
            payload.get("boundary_complete", True),
            "request-cap truncation must not masquerade as a work boundary",
        )
        rows = payload.get("subject_observations")
        self.assertIsInstance(rows, (list, tuple))
        selected = next(row for row in rows if row["bangumi_subject_id"] == "290980")
        self.assertRegex(str(selected.get("observed_at") or ""), r"^\d{4}-\d{2}-\d{2}T")
        self.assertTrue(str(selected.get("policy_version") or ""))
        self.assertRegex(str(selected.get("response_fingerprint") or ""), r"^[0-9a-f]{64}$")
        self.assertRegex(str(payload.get("evidence_fingerprint") or ""), r"^[0-9a-f]{64}$")
        resolution = _resolve_subject_owned(
            selected_subject_id="290980",
            subjects=YAMA_SUBJECTS,
            relations=YAMA_RELATIONS,
            observations=payload,
        )
        self.assertEqual("manual_required", _field(resolution, "review_disposition"))
        self.assertIn("subject_evidence_truncated", _reason_codes(resolution))

        calls.clear()
        complete = collector(
            selected_subject_id="290980",
            bangumi_subjects=YAMA_SUBJECTS,
            bangumi_relations=YAMA_RELATIONS,
            search_subject=search_subject,
            max_subjects=4,
        )
        complete_payload = _mapping(complete, "complete subject observation batch")
        self.assertEqual("290980", calls[0])
        self.assertFalse(bool(complete_payload.get("truncated")))
        self.assertTrue(bool(complete_payload.get("boundary_complete")))
        self.assertRegex(str(complete_payload.get("evidence_fingerprint") or ""), r"^[0-9a-f]{64}$")
        for observation in complete_payload.get("subject_observations") or ():
            observation_core = {
                key: value
                for key, value in observation.items()
                if key != "response_fingerprint"
            }
            self.assertEqual(
                _canonical_hash(observation_core),
                observation.get("response_fingerprint"),
            )
            for query in observation.get("queries") or ():
                for candidate in query.get("candidates") or ():
                    self.assertEqual(str(candidate.get("provider_id") or ""), str(candidate.get("tmdb_tv_id") or ""))
        complete_core = {
            key: value
            for key, value in complete_payload.items()
            if key != "evidence_fingerprint"
        }
        self.assertEqual(
            _canonical_hash(complete_core),
            complete_payload.get("evidence_fingerprint"),
        )
        complete_resolution = _resolve_subject_owned(
            selected_subject_id="290980",
            subjects=YAMA_SUBJECTS,
            relations=YAMA_RELATIONS,
            observations=complete_payload,
        )
        self.assertEqual("reviewable", _field(complete_resolution, "review_disposition"))
        self.assertEqual(tuple(subject["provider_id"] for subject in YAMA_SUBJECTS), tuple(_mapping(_field(complete_resolution, "work_segment"), "Yama collected segment").get("subject_ids") or ()))
        _assert_ranges(
            self,
            complete_resolution,
            tuple(
                (str(subject["provider_id"]), "53918", index, 1, int(subject["episode_count"]))
                for index, subject in enumerate(YAMA_SUBJECTS, start=1)
            ),
        )
        _resolution_fingerprint(complete_resolution)

    def test_generic_maximal_segment_is_id_agnostic_and_order_stable(self):
        def build(prefix: str, tmdb_a: str, tmdb_b: str):
            subjects = tuple(
                _subject(
                    f"{prefix}{index}",
                    "Generic A" if index in {1, 2, 3} else "Generic B",
                    (
                        f"Generic A Season {index}"
                        if index in {1, 2, 3}
                        else f"Generic B Season {index}"
                    ),
                    f"20{10 + index}-01-01",
                    12,
                )
                for index in range(5)
            )
            relations = tuple(
                _relation(f"{prefix}{index}", f"{prefix}{index + 1}")
                for index in range(4)
            )
            work_a = _tmdb(
                tmdb_a,
                "Generic A",
                "Generic A",
                "2011-01-01",
                ((1, "2011-01-01", 12), (2, "2012-01-01", 12), (3, "2013-01-01", 12)),
            )
            work_b = _tmdb(tmdb_b, "Generic B", "Generic B", "2010-01-01", ((1, "2010-01-01", 12),))
            observations = tuple(
                _observation(
                    str(subject["provider_id"]),
                    _query(
                        str(subject["original_title"]),
                        "en-US",
                        candidate=work_a if index in {1, 2, 3} else work_b,
                    ),
                )
                for index, subject in enumerate(subjects)
            )
            return subjects, relations, observations

        for prefix, tmdb_a, tmdb_b in (("node-", "900100", "900200"), ("renamed-", "810100", "810200")):
            subjects, relations, observations = build(prefix, tmdb_a, tmdb_b)
            forward = _resolve_subject_owned(
                selected_subject_id=f"{prefix}2",
                subjects=subjects,
                relations=relations,
                observations=_batch(f"{prefix}2", observations),
            )
            reverse = _resolve_subject_owned(
                selected_subject_id=f"{prefix}2",
                subjects=tuple(reversed(subjects)),
                relations=tuple(reversed(relations)),
                observations=_batch(f"{prefix}2", tuple(reversed(observations))),
            )
            expected_segment = tuple(f"{prefix}{index}" for index in (1, 2, 3))
            for result in (forward, reverse):
                segment = _mapping(_field(result, "work_segment"), "generic work segment")
                self.assertEqual(expected_segment, tuple(segment.get("subject_ids") or ()))
                self.assertEqual(tmdb_a, str(_field(result, "tmdb_tv_id") or ""))
                self.assertEqual("reviewable", _field(result, "review_disposition"))
                _assert_ranges(
                    self,
                    result,
                    tuple((f"{prefix}{index}", tmdb_a, season, 1, 12) for season, index in enumerate((1, 2, 3), start=1)),
                )
                self.assertEqual(
                    (
                        (f"{prefix}0", f"{prefix}1", "sequel", "different_unique_tmdb_id", tmdb_b),
                        (f"{prefix}3", f"{prefix}4", "sequel", "different_unique_tmdb_id", tmdb_b),
                    ),
                    _boundary_rows(result),
                )
            self.assertEqual(_resolution_fingerprint(forward), _resolution_fingerprint(reverse))

    def test_proven_work_boundary_shields_selected_segment_from_remote_unknown(self):
        selected = _subject("boundary-a", "Boundary A", "Boundary A", "2020-01-01", 12)
        boundary = _subject("boundary-b", "Boundary B", "Boundary B", "2021-01-01", 12)
        remote = _subject("boundary-c", "Boundary C", "Boundary C", "2022-01-01", 12)
        selected_work = _tmdb("tv-a", "Boundary A", "Boundary A", "2020-01-01", ((1, "2020-01-01", 12),))
        other_work = _tmdb("tv-b", "Boundary B", "Boundary B", "2021-01-01", ((1, "2021-01-01", 12),))
        selected_observation = _observation("boundary-a", _query("Boundary A", "en-US", candidate=selected_work))
        boundary_observation = _observation("boundary-b", _query("Boundary B", "en-US", candidate=other_work))
        remote_unknown = _observation("boundary-c", _query("Boundary C", "en-US", outcome="no_match"))
        subjects = (selected, boundary, remote)
        relations = (_relation("boundary-a", "boundary-b"), _relation("boundary-b", "boundary-c"))

        complete = _resolve_subject_owned(
            selected_subject_id="boundary-a",
            subjects=subjects,
            relations=relations,
            observations=_batch("boundary-a", (selected_observation, boundary_observation, remote_unknown)),
        )
        bounded = _resolve_subject_owned(
            selected_subject_id="boundary-a",
            subjects=subjects,
            relations=relations,
            observations=_batch(
                "boundary-a",
                (selected_observation, boundary_observation),
                truncated=True,
                boundary_complete=False,
            ),
        )
        for result in (complete, bounded):
            self.assertEqual("reviewable", _field(result, "review_disposition"))
            segment = _mapping(_field(result, "work_segment"), "bounded work segment")
            self.assertEqual(("boundary-a",), tuple(segment.get("subject_ids") or ()))
            self.assertEqual(
                (
                    {
                        "from_subject_id": "boundary-a",
                        "to_subject_id": "boundary-b",
                        "relation": "sequel",
                        "boundary_kind": "different_unique_tmdb_id",
                        "outside_tmdb_tv_id": "tv-b",
                    },
                ),
                tuple(_field(result, "boundaries", ()) or ()),
            )
        self.assertEqual(_resolution_fingerprint(complete), _resolution_fingerprint(bounded))

        unresolved_neighbour = _resolve_subject_owned(
            selected_subject_id="boundary-a",
            subjects=(selected, boundary),
            relations=(_relation("boundary-a", "boundary-b"),),
            observations=_batch(
                "boundary-a",
                (
                    selected_observation,
                    _observation("boundary-b", _query("Boundary B", "en-US", outcome="no_match")),
                ),
            ),
        )
        self.assertEqual("manual_required", _field(unresolved_neighbour, "review_disposition"))
        self.assertIn("subject_candidate_evidence_missing", _reason_codes(unresolved_neighbour))

    def test_branch_and_cycle_graphs_fail_closed_before_ranges(self):
        candidate = _tmdb(
            "700001",
            "Graph Work",
            "Graph Work",
            "2020-01-01",
            ((1, "2020-01-01", 12), (2, "2021-01-01", 12), (3, "2022-01-01", 12)),
        )
        subjects = tuple(
            _subject(f"g{index}", "Graph Work", f"Graph Work Season {index + 1}", f"202{index}-01-01", 12)
            for index in range(3)
        )
        observations = tuple(
            _observation(str(item["provider_id"]), _query(str(item["original_title"]), "en-US", candidate=candidate))
            for item in subjects
        )
        cases = (
            ((_relation("g0", "g1"), _relation("g0", "g2")), "branching_sequel_graph"),
            ((_relation("g0", "g1"), _relation("g1", "g2"), _relation("g2", "g0")), "cyclic_sequel_graph"),
        )
        for relations, reason in cases:
            result = _resolve_subject_owned(
                selected_subject_id="g1",
                subjects=subjects,
                relations=relations,
                observations=_batch("g1", observations),
            )
            self.assertEqual("blocked", _field(result, "review_disposition"))
            self.assertIn(reason, _reason_codes(result))
            self.assertFalse(_field(result, "derived_episode_ranges", ()))

    def test_steins_selected_subjects_form_two_tmdb_work_segments(self):
        subjects = (STEINS, STEINS_ZERO)
        relations = (_relation("10380", "129807"),)
        observations = (
            _observation("10380", _query("STEINS;GATE", "ja-JP", candidate=TMDB_STEINS)),
            _observation("129807", _query("Steins;Gate 0", "en-US", candidate=TMDB_STEINS_ZERO)),
        )
        main = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=subjects,
            relations=relations,
            observations=_batch("10380", observations),
        )
        zero = _resolve_subject_owned(
            selected_subject_id="129807",
            subjects=subjects,
            relations=relations,
            observations=_batch("129807", observations),
        )
        main_segment = _mapping(_field(main, "work_segment"), "Steins main segment")
        zero_segment = _mapping(_field(zero, "work_segment"), "Steins Zero segment")
        self.assertEqual("42509", str(_field(main, "tmdb_tv_id") or ""))
        self.assertEqual(("10380",), tuple(main_segment.get("subject_ids") or ()))
        self.assertEqual("78102", str(_field(zero, "tmdb_tv_id") or ""))
        self.assertEqual(("129807",), tuple(zero_segment.get("subject_ids") or ()))
        self.assertEqual("reviewable", _field(main, "review_disposition"))
        self.assertEqual("reviewable", _field(zero, "review_disposition"))
        self.assertEqual(
            (("10380", "129807", "sequel", "different_unique_tmdb_id", "78102"),),
            _boundary_rows(main),
        )
        self.assertEqual(
            (("10380", "129807", "sequel", "different_unique_tmdb_id", "42509"),),
            _boundary_rows(zero),
        )
        _assert_ranges(self, main, (("10380", "42509", 1, 1, 24),))
        _assert_ranges(self, zero, (("129807", "78102", 1, 1, 23),))
        self.assertNotEqual(_resolution_fingerprint(main), _resolution_fingerprint(zero))

    def test_yama_subject_owned_evidence_keeps_one_work_and_four_seasons(self):
        observations = tuple(
            _observation(
                str(subject["provider_id"]),
                _query(str(subject["original_title"]), "ja-JP", candidate=TMDB_YAMA),
            )
            for subject in YAMA_SUBJECTS
        )
        result = _resolve_subject_owned(
            selected_subject_id="290980",
            subjects=YAMA_SUBJECTS,
            relations=YAMA_RELATIONS,
            observations=_batch("290980", observations),
        )
        segment = _mapping(_field(result, "work_segment"), "Yama work segment")
        self.assertEqual("53918", str(_field(result, "tmdb_tv_id") or ""))
        self.assertEqual(
            tuple(subject["provider_id"] for subject in YAMA_SUBJECTS),
            tuple(segment.get("subject_ids") or ()),
        )
        self.assertEqual(
            {"41658": 1, "86670": 2, "216310": 3, "290980": 4},
            dict(_field(result, "season_assignments", ()) or ()),
        )
        self.assertEqual("one_subject_one_season", _field(result, "mapping_shape"))
        self.assertEqual("reviewable", _field(result, "review_disposition"))
        _assert_ranges(
            self,
            result,
            tuple(
                (str(subject["provider_id"]), "53918", index, 1, int(subject["episode_count"]))
                for index, subject in enumerate(YAMA_SUBJECTS, start=1)
            ),
        )

    def test_eighty_six_split_cour_freezes_exact_non_overlapping_ranges(self):
        observations = tuple(
            _observation(
                str(subject["provider_id"]),
                _query(str(subject["original_title"]), "ja-JP", candidate=TMDB_EIGHTY_SIX),
            )
            for subject in EIGHTY_SIX
        )
        result = _resolve_subject_owned(
            selected_subject_id="331887",
            subjects=EIGHTY_SIX,
            relations=(_relation("302189", "331887"),),
            observations=_batch("331887", observations),
        )
        rows = {
            str(row["bangumi_subject_id"]): row
            for row in (_field(result, "derived_episode_ranges", ()) or ())
        }
        self.assertEqual({"302189", "331887"}, set(rows))
        self.assertEqual((1, 1, 11), (
            rows["302189"]["season_number"],
            rows["302189"]["episode_start"],
            rows["302189"]["episode_end"],
        ))
        self.assertEqual((1, 12, 23), (
            rows["331887"]["season_number"],
            rows["331887"]["episode_start"],
            rows["331887"]["episode_end"],
        ))
        self.assertEqual("many_subjects_one_season", _field(result, "mapping_shape"))
        self.assertEqual("reviewable", _field(result, "review_disposition"))
        _assert_ranges(
            self,
            result,
            (("302189", "100565", 1, 1, 11), ("331887", "100565", 1, 12, 23)),
        )

    def test_season_window_supports_later_unique_and_rejects_ambiguity_or_gap(self):
        subjects = (
            _subject("window-a", "Window Work", "Window Work Part 1", "2021-01-01", 10),
            _subject("window-b", "Window Work", "Window Work Part 2", "2021-07-01", 12),
        )
        relations = (_relation("window-a", "window-b"),)

        def resolve(candidate: Mapping[str, object]):
            observations = tuple(
                _observation(str(item["provider_id"]), _query(str(item["original_title"]), "en-US", candidate=candidate))
                for item in subjects
            )
            return _resolve_subject_owned(
                selected_subject_id="window-b",
                subjects=subjects,
                relations=relations,
                observations=_batch("window-b", observations),
            )

        unique = resolve(_tmdb(
            "600001", "Window Work", "Window Work", "2019-01-01",
            ((1, "2019-01-01", 5), (2, "2021-01-01", 10), (3, "2021-07-01", 12), (4, "2023-01-01", 8)),
        ))
        self.assertEqual({"window-a": 2, "window-b": 3}, dict(_field(unique, "season_assignments", ()) or ()))
        self.assertEqual("reviewable", _field(unique, "review_disposition"))
        _assert_ranges(self, unique, (("window-a", "600001", 2, 1, 10), ("window-b", "600001", 3, 1, 12)))

        date_locked = resolve(_tmdb(
            "600004", "Window Work", "Window Work", "2010-01-01",
            ((1, "2010-01-01", 4), (2, "2021-01-01", 11), (3, "2021-07-01", 13), (4, "2030-01-01", 4)),
        ))
        self.assertEqual({"window-a": 2, "window-b": 3}, dict(_field(date_locked, "season_assignments", ()) or ()))
        self.assertEqual("reviewable", _field(date_locked, "review_disposition"))
        self.assertIn("season_window_date_locked", _reason_codes(date_locked))
        self.assertEqual("date-count-delta-v1", _field(date_locked, "season_count_policy_version"))
        _assert_ranges(self, date_locked, (("window-a", "600004", 2, 1, 11), ("window-b", "600004", 3, 1, 13)))

        reverse_delta = resolve(_tmdb(
            "600006", "Window Work", "Window Work", "2010-01-01",
            ((1, "2010-01-01", 4), (2, "2021-01-01", 9), (3, "2021-07-01", 11), (4, "2030-01-01", 4)),
        ))
        self.assertEqual("reviewable", _field(reverse_delta, "review_disposition"))
        self.assertEqual("date-count-delta-v1", _field(reverse_delta, "season_count_policy_version"))
        _assert_ranges(self, reverse_delta, (("window-a", "600006", 2, 1, 9), ("window-b", "600006", 3, 1, 11)))

        mixed_delta = resolve(_tmdb(
            "600008", "Window Work", "Window Work", "2010-01-01",
            ((1, "2010-01-01", 4), (2, "2021-01-01", 11), (3, "2021-07-01", 11), (4, "2030-01-01", 4)),
        ))
        self.assertEqual("reviewable", _field(mixed_delta, "review_disposition"))
        self.assertEqual("date-count-delta-v1", _field(mixed_delta, "season_count_policy_version"))
        _assert_ranges(self, mixed_delta, (("window-a", "600008", 2, 1, 11), ("window-b", "600008", 3, 1, 11)))

        boundary_mismatch = resolve(_tmdb(
            "600007", "Window Work", "Window Work", "2010-01-01",
            ((1, "2010-01-01", 4), (2, "2021-01-01", 12), (3, "2021-07-01", 14), (4, "2030-01-01", 4)),
        ))
        self.assertNotEqual("reviewable", _field(boundary_mismatch, "review_disposition"))
        self.assertIn("season_episode_count_mismatch", _reason_codes(boundary_mismatch))
        self.assertFalse(_field(boundary_mismatch, "derived_episode_ranges", ()))

        cancelling_mismatch = resolve(_tmdb(
            "600009", "Window Work", "Window Work", "2010-01-01",
            ((1, "2010-01-01", 4), (2, "2021-01-01", 12), (3, "2021-07-01", 10), (4, "2030-01-01", 4)),
        ))
        self.assertNotEqual("reviewable", _field(cancelling_mismatch, "review_disposition"))
        self.assertIn("season_episode_count_mismatch", _reason_codes(cancelling_mismatch))
        self.assertFalse(_field(cancelling_mismatch, "derived_episode_ranges", ()))

        gross_mismatch = resolve(_tmdb(
            "600005", "Window Work", "Window Work", "2010-01-01",
            ((1, "2010-01-01", 4), (2, "2021-01-01", 99), (3, "2021-07-01", 99), (4, "2030-01-01", 4)),
        ))
        self.assertNotEqual("reviewable", _field(gross_mismatch, "review_disposition"))
        self.assertIn("season_episode_count_mismatch", _reason_codes(gross_mismatch))
        self.assertFalse(_field(gross_mismatch, "derived_episode_ranges", ()))

        ambiguous = resolve(_tmdb(
            "600002", "Window Work", "Window Work", "2021-01-01",
            ((1, "2021-01-01", 10), (2, "2021-07-01", 12), (3, "2021-01-01", 10), (4, "2021-07-01", 12)),
        ))
        self.assertEqual("blocked", _field(ambiguous, "review_disposition"))
        self.assertIn("season_window_ambiguous", _reason_codes(ambiguous))
        self.assertFalse(_field(ambiguous, "derived_episode_ranges", ()))

        gap = resolve(_tmdb(
            "600003", "Window Work", "Window Work", "2021-01-01",
            ((1, "2021-01-01", 10), (3, "2021-07-01", 12)),
        ))
        self.assertEqual("blocked", _field(gap, "review_disposition"))
        self.assertIn("tmdb_season_gap", _reason_codes(gap))
        self.assertFalse(_field(gap, "derived_episode_ranges", ()))

    def test_source_projection_selects_absolute_relative_and_e12_ambiguity(self):
        project = getattr(resolver_api, "derive_source_episode_projection", None)
        self.assertTrue(callable(project), "Phase A lacks source episode projection")
        absolute = project(
            source_episode_numbers=tuple(range(12, 24)),
            target_episode_start=12,
            target_episode_end=23,
            inventory_scope="complete",
        )
        relative = project(
            source_episode_numbers=tuple(range(1, 13)),
            target_episode_start=12,
            target_episode_end=23,
            inventory_scope="complete",
        )
        relative_shifted = project(
            source_episode_numbers=tuple(range(1, 13)),
            target_episode_start=13,
            target_episode_end=24,
            inventory_scope="complete",
        )
        relative_partial_scope = project(
            source_episode_numbers=tuple(range(1, 13)),
            target_episode_start=12,
            target_episode_end=23,
            inventory_scope="partial",
        )
        ambiguous = project(
            source_episode_numbers=(12,),
            target_episode_start=12,
            target_episode_end=23,
            inventory_scope="partial",
        )
        ambiguous_repeated = project(
            source_episode_numbers=(12,),
            target_episode_start=12,
            target_episode_end=23,
            inventory_scope="partial",
        )
        self.assertEqual("absolute", _field(absolute, "mode"))
        self.assertEqual(0, _field(absolute, "offset"))
        self.assertEqual(tuple((episode, episode) for episode in range(12, 24)), _episode_pairs(absolute))
        self.assertEqual("relative", _field(relative, "mode"))
        self.assertEqual(11, _field(relative, "offset"))
        self.assertEqual(tuple((episode, episode + 11) for episode in range(1, 13)), _episode_pairs(relative))
        self.assertEqual("relative", _field(relative_shifted, "mode"))
        self.assertEqual(tuple((episode, episode + 12) for episode in range(1, 13)), _episode_pairs(relative_shifted))
        self.assertEqual(_episode_pairs(relative), _episode_pairs(relative_partial_scope))
        reordered = project(
            source_episode_numbers=tuple(reversed(range(1, 13))),
            target_episode_start=12,
            target_episode_end=23,
            inventory_scope="complete",
        )
        self.assertEqual(_episode_pairs(relative), _episode_pairs(reordered))
        self.assertEqual(
            _named_fingerprint(relative, "projection_fingerprint"),
            _named_fingerprint(reordered, "projection_fingerprint"),
        )
        self.assertNotEqual(
            _named_fingerprint(absolute, "projection_fingerprint"),
            _named_fingerprint(relative, "projection_fingerprint"),
        )
        self.assertNotEqual(
            _named_fingerprint(relative, "projection_fingerprint"),
            _named_fingerprint(relative_shifted, "projection_fingerprint"),
        )
        self.assertNotEqual(
            _named_fingerprint(relative, "projection_fingerprint"),
            _named_fingerprint(relative_partial_scope, "projection_fingerprint"),
        )
        for projection in (absolute, relative, reordered, relative_shifted, relative_partial_scope):
            self.assertEqual(
                _expected_projection_fingerprint(projection),
                _named_fingerprint(projection, "projection_fingerprint"),
            )
        self.assertEqual("manual_required", _field(ambiguous, "status"))
        self.assertIn("source_episode_projection_ambiguous", _reason_codes(ambiguous))
        candidates = _field(ambiguous, "candidates", ()) or ()
        self.assertEqual(2, len(candidates), "E12 must preserve both E12 and E23 choices")
        self.assertEqual(
            {((12, 12),), ((12, 23),)},
            {_episode_pairs(candidate) for candidate in candidates},
        )
        self.assertEqual(
            2,
            len({_named_fingerprint(candidate, "projection_fingerprint") for candidate in candidates}),
        )
        for candidate in candidates:
            self.assertEqual(
                _expected_projection_fingerprint(candidate),
                _named_fingerprint(candidate, "projection_fingerprint"),
            )
        self.assertEqual(
            {
                (_episode_pairs(candidate), _named_fingerprint(candidate, "projection_fingerprint"))
                for candidate in candidates
            },
            {
                (_episode_pairs(candidate), _named_fingerprint(candidate, "projection_fingerprint"))
                for candidate in (_field(ambiguous_repeated, "candidates", ()) or ())
            },
        )
        for projection in (absolute, relative):
            targets = tuple(target for _source, target in _episode_pairs(projection))
            self.assertEqual(len(targets), len(set(targets)))
            self.assertTrue(all(12 <= target <= 23 for target in targets))

    def test_unknown_neighbour_is_not_a_positive_work_boundary(self):
        subjects = (STEINS, STEINS_ZERO)
        relations = (_relation("10380", "129807"),)
        selected = _observation(
            "10380", _query("STEINS;GATE", "ja-JP", candidate=TMDB_STEINS)
        )
        failed_neighbour = _observation(
            "129807",
            _query("Steins;Gate 0", "en-US", outcome="transport_error"),
        )
        transport = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=subjects,
            relations=relations,
            observations=_batch("10380", (selected, failed_neighbour), boundary_complete=False),
        )
        truncated = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=subjects,
            relations=relations,
            observations=_batch("10380", (selected,), truncated=True, boundary_complete=False),
        )
        natural_end = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=_batch("10380", (selected,), boundary_complete=True),
        )
        self.assertEqual("manual_required", _field(transport, "review_disposition"))
        self.assertIn("subject_candidate_transport_error", _reason_codes(transport))
        self.assertEqual("manual_required", _field(truncated, "review_disposition"))
        self.assertIn("subject_evidence_truncated", _reason_codes(truncated))
        self.assertEqual("reviewable", _field(natural_end, "review_disposition"))

    def test_locale_evidence_converges_or_conflicts_without_missing_penalty(self):
        converged = _observation(
            "10380",
            _query("命运石之门", "zh-CN", candidate=TMDB_STEINS),
            _query("STEINS;GATE", "ja-JP", outcome="no_match"),
            _query("STEINS;GATE", "en-US", outcome="no_match"),
        )
        accepted = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=_batch("10380", (converged,)),
        )
        conflicting = _observation(
            "10380",
            _query("命运石之门", "zh-CN", candidate=TMDB_STEINS),
            _query("Steins Gate Zero", "en-US", candidate=TMDB_STEINS_ZERO),
        )
        rejected = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=_batch("10380", (conflicting,)),
        )
        self.assertEqual("42509", str(_field(accepted, "tmdb_tv_id") or ""))
        self.assertEqual("reviewable", _field(accepted, "review_disposition"))
        self.assertNotIn("locale_evidence_missing", _reason_codes(accepted))
        self.assertEqual("blocked", _field(rejected, "review_disposition"))
        self.assertIn("locale_candidate_conflict", _reason_codes(rejected))

    def test_observation_outcomes_authority_and_fingerprints_are_fail_closed(self):
        exact_zh = _query("命运石之门", "zh-CN", candidate=TMDB_STEINS)
        exact_ja = _query("STEINS;GATE", "ja-JP", candidate=TMDB_STEINS)
        ordered = _observation("10380", exact_zh, exact_ja)
        reversed_queries = _observation("10380", exact_ja, exact_zh)
        self.assertEqual(ordered["response_fingerprint"], reversed_queries["response_fingerprint"])
        accepted_a = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=_batch("10380", (ordered,)),
        )
        accepted_b = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=_batch("10380", (reversed_queries,)),
        )
        self.assertEqual(_resolution_fingerprint(accepted_a), _resolution_fingerprint(accepted_b))

        drifted_candidate = {**TMDB_STEINS, "title": "STEINS;GATE"}
        drifted = _observation("10380", _query("STEINS;GATE", "ja-JP", candidate=drifted_candidate))
        self.assertNotEqual(ordered["response_fingerprint"], drifted["response_fingerprint"])
        self.assertNotEqual(
            _batch("10380", (ordered,))["evidence_fingerprint"],
            _batch("10380", (drifted,))["evidence_fingerprint"],
        )
        drifted_result = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=_batch("10380", (drifted,)),
        )
        self.assertEqual(_resolution_fingerprint(accepted_a), _resolution_fingerprint(drifted_result))

        unsupported_policy = dict(_batch("10380", (ordered,)))
        unsupported_policy["policy_version"] = "subject-candidate-v999"
        unsupported_core = {key: value for key, value in unsupported_policy.items() if key != "evidence_fingerprint"}
        unsupported_policy["evidence_fingerprint"] = _canonical_hash(unsupported_core)
        unsupported = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=unsupported_policy,
        )
        self.assertNotEqual("reviewable", _field(unsupported, "review_disposition"))
        self.assertIn("subject_observation_batch_policy_unsupported", _reason_codes(unsupported))

        nested_policy = dict(ordered)
        nested_policy["policy_version"] = "subject-candidate-v999"
        nested_core = {
            key: value
            for key, value in nested_policy.items()
            if key != "response_fingerprint"
        }
        nested_policy["response_fingerprint"] = _canonical_hash(nested_core)
        nested_unsupported = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=_batch("10380", (nested_policy,)),
        )
        self.assertNotEqual("reviewable", _field(nested_unsupported, "review_disposition"))
        self.assertIn("subject_observation_policy_unsupported", _reason_codes(nested_unsupported))

        unsupported_schema = dict(_batch("10380", (ordered,)))
        unsupported_schema["schema_version"] = 999
        unsupported_schema_core = {
            key: value
            for key, value in unsupported_schema.items()
            if key != "evidence_fingerprint"
        }
        unsupported_schema["evidence_fingerprint"] = _canonical_hash(unsupported_schema_core)
        bad_schema = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=unsupported_schema,
        )
        self.assertNotEqual("reviewable", _field(bad_schema, "review_disposition"))
        self.assertIn("subject_observation_batch_schema_unsupported", _reason_codes(bad_schema))

        invalid_boolean = dict(_batch("10380", (ordered,)))
        invalid_boolean["truncated"] = "false"
        invalid_boolean_core = {
            key: value
            for key, value in invalid_boolean.items()
            if key != "evidence_fingerprint"
        }
        invalid_boolean["evidence_fingerprint"] = _canonical_hash(invalid_boolean_core)
        bad_boolean = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=invalid_boolean,
        )
        self.assertNotEqual("reviewable", _field(bad_boolean, "review_disposition"))
        self.assertIn("subject_observation_batch_invalid", _reason_codes(bad_boolean))

        tampered_batch = dict(_batch("10380", (ordered,)))
        tampered_batch["evidence_fingerprint"] = "0" * 64
        bad_batch_fingerprint = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=tampered_batch,
        )
        self.assertNotEqual("reviewable", _field(bad_batch_fingerprint, "review_disposition"))
        self.assertIn("subject_evidence_fingerprint_mismatch", _reason_codes(bad_batch_fingerprint))

        fuzzy = {
            **TMDB_STEINS,
            "match_tier": "fuzzy",
            "score_components": {"number_match": False, "exact": False, "containment": True},
            "margin": 0.01,
        }
        tie_a = {**TMDB_STEINS, "match_tier": "exact"}
        tie_b = {**TMDB_STEINS_ZERO, "match_tier": "exact"}
        cases = (
            (_observation("10380", _query("fuzzy", "en-US", candidates=(fuzzy,))), "subject_candidate_confidence_insufficient"),
            (_observation("10380", _query("tie", "en-US", candidates=(tie_a, tie_b))), "subject_candidate_tie"),
            (_observation("10380", _query("none", "en-US", outcome="no_match")), "subject_candidate_evidence_missing"),
            (_observation("10380", _query("invalid", "en-US", outcome="invalid_response")), "subject_candidate_invalid_response"),
            (_observation("10380", _query("budget", "en-US", outcome="budget_exhausted")), "subject_candidate_budget_exhausted"),
            (_observation("10380", _query("truncated", "en-US", candidate=TMDB_STEINS, truncated=True)), "subject_query_truncated"),
        )
        for observation, reason in cases:
            result = _resolve_subject_owned(
                selected_subject_id="10380",
                subjects=(STEINS,),
                relations=(),
                observations=_batch("10380", (observation,), boundary_complete=False),
            )
            self.assertNotEqual("reviewable", _field(result, "review_disposition"))
            self.assertIn(reason, _reason_codes(result))

        future_outcome = _observation(
            "10380",
            _query("future", "en-US", candidate=TMDB_STEINS, outcome="future_outcome"),
        )
        inconsistent_outcome = _observation(
            "10380",
            _query("none", "en-US", candidate=TMDB_STEINS, outcome="no_match"),
        )
        movie_candidate = {
            **TMDB_STEINS,
            "entity_type": "movie",
        }
        mismatched_identity = {
            **TMDB_STEINS,
            "tmdb_tv_id": "78102",
        }
        invalid_query_boolean = _query("bool", "en-US", candidate=TMDB_STEINS)
        invalid_query_boolean["truncated"] = "false"
        wrong_entity = _observation(
            "10380",
            _query("movie", "en-US", candidate=movie_candidate),
        )
        for observation, reason in (
            (future_outcome, "subject_query_outcome_unsupported"),
            (inconsistent_outcome, "subject_query_outcome_inconsistent"),
            (wrong_entity, "subject_candidate_identity_invalid"),
            (
                _observation(
                    "10380",
                    _query("mismatch", "en-US", candidate=mismatched_identity),
                ),
                "subject_candidate_identity_invalid",
            ),
            (
                _observation("10380", invalid_query_boolean),
                "subject_observation_invalid",
            ),
        ):
            result = _resolve_subject_owned(
                selected_subject_id="10380",
                subjects=(STEINS,),
                relations=(),
                observations=_batch("10380", (observation,)),
            )
            self.assertNotEqual("reviewable", _field(result, "review_disposition"))
            self.assertIn(reason, _reason_codes(result))

        duplicate_payload = {
            **_batch("10380", (ordered,)),
            "subject_observations": (ordered, ordered),
        }
        duplicate_core = {
            key: value
            for key, value in duplicate_payload.items()
            if key != "evidence_fingerprint"
        }
        duplicate_payload["evidence_fingerprint"] = _canonical_hash(duplicate_core)
        duplicate = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=duplicate_payload,
        )
        self.assertIn("subject_observation_duplicate", _reason_codes(duplicate))
        mismatch = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=_batch("129807", (ordered,)),
        )
        self.assertIn("subject_observation_selected_mismatch", _reason_codes(mismatch))
        tampered = dict(ordered)
        tampered["response_fingerprint"] = "0" * 64
        bad_fingerprint = _resolve_subject_owned(
            selected_subject_id="10380",
            subjects=(STEINS,),
            relations=(),
            observations=_batch("10380", (tampered,)),
        )
        self.assertIn("subject_observation_fingerprint_mismatch", _reason_codes(bad_fingerprint))

    def test_episode_domain_and_inventory_scope_are_structured(self):
        project = getattr(resolver_api, "derive_source_episode_projection", None)
        self.assertTrue(callable(project), "Phase A lacks source episode projection")
        decimal = project(
            source_episode_numbers=(1, 2, 6.5),
            target_episode_start=1,
            target_episode_end=12,
            inventory_scope="complete",
        )
        complete_gap = project(
            source_episode_numbers=(1, 3),
            target_episode_start=1,
            target_episode_end=3,
            inventory_scope="complete",
        )
        partial_gap = project(
            source_episode_numbers=(1, 3),
            target_episode_start=1,
            target_episode_end=3,
            inventory_scope="partial",
        )
        unknown = project(
            source_episode_numbers=(1, 2),
            target_episode_start=1,
            target_episode_end=12,
            inventory_scope="unknown",
        )
        duplicate = project(
            source_episode_numbers=(1, 1, 2),
            target_episode_start=1,
            target_episode_end=12,
            inventory_scope="complete",
        )
        outside = project(
            source_episode_numbers=(24,),
            target_episode_start=12,
            target_episode_end=23,
            inventory_scope="complete",
        )
        self.assertEqual("blocked", _field(decimal, "status"))
        self.assertIn("non_integer_main_episode", _reason_codes(decimal))
        self.assertEqual("blocked", _field(complete_gap, "status"))
        self.assertIn("complete_inventory_episode_gap", _reason_codes(complete_gap))
        self.assertEqual("manual_required", _field(partial_gap, "status"))
        self.assertIn("partial_inventory_confirmation_required", _reason_codes(partial_gap))
        self.assertEqual("manual_required", _field(unknown, "status"))
        self.assertIn("unknown_inventory_confirmation_required", _reason_codes(unknown))
        self.assertEqual("blocked", _field(duplicate, "status"))
        self.assertIn("duplicate_source_episode", _reason_codes(duplicate))
        self.assertEqual("blocked", _field(outside, "status"))
        self.assertIn("source_episode_projection_out_of_range", _reason_codes(outside))

    def test_overlapping_tmdb_ranges_are_rejected_order_independently(self):
        validate = getattr(resolver_api, "validate_derived_episode_ranges", None)
        self.assertTrue(callable(validate), "Phase A lacks range reservation validation")
        ranges = (
            {
                "bangumi_subject_id": "302189",
                "tmdb_tv_id": "100565",
                "season_number": 1,
                "episode_start": 1,
                "episode_end": 11,
            },
            {
                "bangumi_subject_id": "foreign",
                "tmdb_tv_id": "100565",
                "season_number": 1,
                "episode_start": 11,
                "episode_end": 23,
            },
        )
        forward = validate(ranges)
        reverse = validate(tuple(reversed(ranges)))
        self.assertFalse(bool(_field(forward, "valid", True)))
        self.assertFalse(bool(_field(reverse, "valid", True)))
        self.assertIn("episode_range_overlap", _reason_codes(forward))
        self.assertEqual(_reason_codes(forward), _reason_codes(reverse))
        self.assertEqual(
            _named_fingerprint(forward, "validation_fingerprint"),
            _named_fingerprint(reverse, "validation_fingerprint"),
        )

    def test_one_subject_spanning_multiple_tmdb_seasons_fails_closed(self):
        subject = _subject("multi", "跨季条目", "Multi Season Subject", "2020-01-01", 24)
        candidate = _tmdb(
            "900001",
            "跨季作品",
            "Multi Season Work",
            "2020-01-01",
            ((1, "2020-01-01", 12), (2, "2020-04-01", 12)),
        )
        observation = _observation(
            "multi", _query("Multi Season Subject", "en-US", candidate=candidate)
        )
        result = _resolve_subject_owned(
            selected_subject_id="multi",
            subjects=(subject,),
            relations=(),
            observations=_batch("multi", (observation,)),
        )
        self.assertEqual("blocked", _field(result, "review_disposition"))
        self.assertIn("one_subject_multiple_seasons_unsupported", _reason_codes(result))
        self.assertFalse(_field(result, "derived_episode_ranges", ()))

    def test_legacy_yama_single_candidate_control(self):
        result = resolver_api.resolve_provider_link(
            selected_provider={
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "290980",
            },
            bangumi_subjects=YAMA_SUBJECTS,
            bangumi_relations=YAMA_RELATIONS,
            tmdb_candidates=(TMDB_YAMA,),
        )
        self.assertEqual("L2", result.automation_level)
        self.assertEqual("inferred", result.status)
        self.assertEqual("53918", result.tmdb_tv_id)
        self.assertEqual(
            {"41658": 1, "86670": 2, "216310": 3, "290980": 4},
            dict(result.season_assignments),
        )

    def test_legacy_split_cour_partition_control(self):
        result = resolver_api.resolve_provider_link(
            selected_provider={
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "331887",
            },
            bangumi_subjects=EIGHTY_SIX,
            bangumi_relations=(_relation("302189", "331887"),),
            tmdb_candidates=(TMDB_EIGHTY_SIX,),
        )
        self.assertEqual("L3", result.automation_level)
        self.assertEqual("inferred", result.status)
        self.assertEqual({"302189": 1, "331887": 1}, dict(result.season_assignments))
        self.assertIn("split_cour_many_to_one", result.reason_codes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
