import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from src.rename.provider_link_catalog import (
    ProviderLinkCatalog,
    ProviderLinkCatalogError,
    ProviderLinkMapping,
)
from src.rename.provider_link_resolver import resolve_provider_link


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _payload(
    *,
    mapping_id: str,
    identity_kind: str,
    identity_key: str,
    provider: dict,
    status: str = "materialized_confirmed",
    season_edges=(),
) -> dict:
    snapshot = {
        "fixture": mapping_id,
        "source_urls": (
            "https://www.themoviedb.org/tv/1396-breaking-bad"
            if provider["entity_type"] == "tv"
            else "https://www.themoviedb.org/movie/278-the-shawshank-redemption"
        ),
    }
    return {
        "schema_version": 1,
        "mapping_id": mapping_id,
        "revision": 0,
        "local_identity": {"kind": identity_kind, "key": identity_key},
        "status": status,
        "provider_edges": ({**provider, "role": identity_kind},),
        "season_edges": tuple(season_edges),
        "evidence_snapshot": snapshot,
        "evidence_fingerprint": _fingerprint(snapshot),
        "confirmed_by": "test",
        "confirmed_at": "2026-08-19T06:30:00+08:00",
        "materialized_at": "2026-08-19T06:31:00+08:00",
    }


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


class ProviderLinkCatalogG1Tests(unittest.TestCase):
    def test_resolver_rejects_unknown_online_parameter_instead_of_swallowing_it(self):
        with self.assertRaises(TypeError):
            resolve_provider_link(
                selected_provider={
                    "namespace": "bangumi",
                    "entity_type": "subject",
                    "provider_id": "41658",
                },
                bangumi_relation_typo=(),
            )

    def test_ordinary_tv_roundtrip_and_exact_lookup_use_series_identity(self):
        mapping = ProviderLinkMapping.from_dict(
            _payload(
                mapping_id=_id("provider-link"),
                identity_kind="series",
                identity_key=_id("series"),
                provider={
                    "namespace": "tmdb",
                    "entity_type": "tv",
                    "provider_id": "1396",
                },
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            catalog = ProviderLinkCatalog(Path(directory) / "provider_links")
            saved = catalog.save(mapping, expected_revision=-1)
            match = catalog.find_exact(
                {"namespace": "tmdb", "entity_type": "tv", "provider_id": "1396"}
            )

        self.assertEqual(saved, match.mapping)
        self.assertEqual("series", match.mapping.local_identity_kind)
        self.assertIsNotNone(match.mapping.local_series_key)
        self.assertIsNone(match.mapping.local_work_key)
        self.assertIsNone(match.season_number)

    def test_ordinary_movie_roundtrip_and_l0_are_offline_work_identity(self):
        provider = {
            "namespace": "tmdb",
            "entity_type": "movie",
            "provider_id": "278",
        }
        mapping = ProviderLinkMapping.from_dict(
            _payload(
                mapping_id=_id("provider-link"),
                identity_kind="work",
                identity_key=_id("work"),
                provider=provider,
            )
        )
        calls = []

        def provider_must_not_run(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("ordinary movie L0 must remain offline")

        result = resolve_provider_link(
            selected_provider=provider,
            existing_links=(mapping,),
            fetch_tmdb=provider_must_not_run,
            fetch_bangumi=provider_must_not_run,
        )

        self.assertEqual("L0", result.automation_level)
        self.assertEqual(mapping.local_work_key, result.work_key)
        self.assertIsNone(result.series_key)
        self.assertIsNone(result.season_number)
        self.assertEqual([], calls)

    def test_animation_season_edge_remains_many_to_one_ready(self):
        series_key = _id("series")
        snapshot = {"fixture": "attack-on-titan-split-cour"}
        payload = {
            "schema_version": 1,
            "mapping_id": _id("provider-link"),
            "revision": 0,
            "local_series_key": series_key,
            "status": "materialized_confirmed",
            "provider_edges": (
                {
                    "namespace": "tmdb",
                    "entity_type": "tv",
                    "provider_id": "1429",
                    "role": "series",
                },
            ),
            "season_edges": (
                {
                    "namespace": "bangumi",
                    "entity_type": "subject",
                    "provider_id": "217300",
                    "tmdb_tv_id": "1429",
                    "season_number": 3,
                    "episode_start": 1,
                    "episode_end": 12,
                },
                {
                    "namespace": "bangumi",
                    "entity_type": "subject",
                    "provider_id": "263750",
                    "tmdb_tv_id": "1429",
                    "season_number": 3,
                    "episode_start": 13,
                    "episode_end": 22,
                },
            ),
            "evidence_snapshot": snapshot,
            "evidence_fingerprint": _fingerprint(snapshot),
        }
        mapping = ProviderLinkMapping.from_dict(payload)

        first = resolve_provider_link(
            selected_provider={
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "217300",
            },
            existing_links=(mapping,),
        )
        second = resolve_provider_link(
            selected_provider={
                "namespace": "bangumi",
                "entity_type": "subject",
                "provider_id": "263750",
            },
            existing_links=(mapping,),
        )

        self.assertEqual(2, len(mapping.season_edges))
        self.assertEqual((3, 3), (first.season_number, second.season_number))
        self.assertEqual(("L0", "L0"), (first.automation_level, second.automation_level))

    def test_work_identity_rejects_season_edges(self):
        payload = _payload(
            mapping_id=_id("provider-link"),
            identity_kind="work",
            identity_key=_id("work"),
            provider={
                "namespace": "tmdb",
                "entity_type": "movie",
                "provider_id": "278",
            },
            season_edges=(
                {
                    "namespace": "bangumi",
                    "entity_type": "subject",
                    "provider_id": "1",
                    "tmdb_tv_id": "1396",
                    "season_number": 1,
                },
            ),
        )
        with self.assertRaisesRegex(ProviderLinkCatalogError, "Work.*season_edges"):
            ProviderLinkMapping.from_dict(payload)

    def test_stale_revision_cannot_overwrite_newer_status(self):
        mapping = ProviderLinkMapping.from_dict(
            _payload(
                mapping_id=_id("provider-link"),
                identity_kind="series",
                identity_key=_id("series"),
                provider={
                    "namespace": "tmdb",
                    "entity_type": "tv",
                    "provider_id": "1396",
                },
                status="inferred",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            catalog = ProviderLinkCatalog(Path(directory) / "provider_links")
            created = catalog.save(mapping, expected_revision=-1)
            approved = ProviderLinkMapping.from_dict(
                {**created.to_dict(), "status": "user_confirmed"}
            )
            current = catalog.save(approved, expected_revision=created.revision)
            with self.assertRaisesRegex(ProviderLinkCatalogError, "版本已变化"):
                catalog.save(approved, expected_revision=created.revision)

            persisted = catalog.load(mapping.mapping_id)

        self.assertEqual(current.revision, persisted.revision)
        self.assertEqual("user_confirmed", persisted.status)

    def test_active_mappings_cannot_claim_same_provider_or_local_identity(self):
        provider = {
            "namespace": "tmdb",
            "entity_type": "movie",
            "provider_id": "278",
        }
        first = ProviderLinkMapping.from_dict(
            _payload(
                mapping_id=_id("provider-link"),
                identity_kind="work",
                identity_key=_id("work"),
                provider=provider,
            )
        )
        same_provider = ProviderLinkMapping.from_dict(
            _payload(
                mapping_id=_id("provider-link"),
                identity_kind="work",
                identity_key=_id("work"),
                provider=provider,
            )
        )
        same_local = ProviderLinkMapping.from_dict(
            _payload(
                mapping_id=_id("provider-link"),
                identity_kind="work",
                identity_key=first.local_work_key,
                provider={**provider, "provider_id": "13"},
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            catalog = ProviderLinkCatalog(Path(directory) / "provider_links")
            catalog.save(first, expected_revision=-1)
            with self.assertRaisesRegex(ProviderLinkCatalogError, "身份冲突"):
                catalog.save(same_provider, expected_revision=-1)
            with self.assertRaisesRegex(ProviderLinkCatalogError, "身份冲突"):
                catalog.save(same_local, expected_revision=-1)

    def test_superseded_mapping_releases_identity_for_rebind(self):
        provider = {
            "namespace": "tmdb",
            "entity_type": "movie",
            "provider_id": "278",
        }
        first = ProviderLinkMapping.from_dict(
            _payload(
                mapping_id=_id("provider-link"),
                identity_kind="work",
                identity_key=_id("work"),
                provider=provider,
            )
        )
        replacement = ProviderLinkMapping.from_dict(
            _payload(
                mapping_id=_id("provider-link"),
                identity_kind="work",
                identity_key=_id("work"),
                provider=provider,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            catalog = ProviderLinkCatalog(Path(directory) / "provider_links")
            created = catalog.save(first, expected_revision=-1)
            superseded = ProviderLinkMapping.from_dict(
                {**created.to_dict(), "status": "superseded"}
            )
            catalog.save(superseded, expected_revision=created.revision)
            saved = catalog.save(replacement, expected_revision=-1)

        self.assertEqual(replacement.mapping_id, saved.mapping_id)

    def test_catalog_wide_live_lock_blocks_and_legacy_residue_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = ProviderLinkCatalog(Path(directory) / "provider_links")
            lock_path = catalog.root / ".provider-link-catalog.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("legacy-token", encoding="ascii")
            with catalog.lock():
                with self.assertRaises(ProviderLinkCatalogError):
                    with catalog.lock():
                        pass

    def test_fingerprint_mismatch_fails_before_catalog_write(self):
        payload = _payload(
            mapping_id=_id("provider-link"),
            identity_kind="series",
            identity_key=_id("series"),
            provider={
                "namespace": "tmdb",
                "entity_type": "tv",
                "provider_id": "1396",
            },
        )
        payload["evidence_snapshot"]["tampered"] = True
        with self.assertRaisesRegex(ProviderLinkCatalogError, "fingerprint"):
            ProviderLinkMapping.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
