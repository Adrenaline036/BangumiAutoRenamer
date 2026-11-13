"""Explicit historical SeriesCatalog input for read-only compatibility tests.

This does not prove an execution occurred and cannot produce a current series
registration authority. Tests using it must separately exercise the real current
producer and retain their no-network, anchor and persistence assertions.
"""
import json
from pathlib import Path


def write_legacy_series_catalog_fixture(catalog, existing_plan):
    """Seed one old-schema, no-current-proof record in an isolated test store."""
    metadata = existing_plan.metadata
    work = existing_plan.work_identity
    if work is None or work.status != 'confirmed' or work.primary_provider is None:
        raise AssertionError('legacy fixture needs its exact confirmed historical work')
    if metadata.get('series_registration_authority') is not None:
        raise AssertionError('current registration proof must not enter the legacy fixture adapter')
    series_key = str(metadata['series_key'])
    season = metadata['season_number']
    root = str(metadata['canonical_work_root'])
    binding_id = str(metadata['season_binding_id'])
    if type(season) is not int or season < 0 or not Path(root).is_absolute() or not binding_id:
        raise AssertionError('legacy fixture has incomplete season/root identity')
    provider = work.primary_provider.to_dict()
    payload = {
        # Font content-identity schema 3 predates series-registration-v1. The
        # historical distinction here is missing registration authority, not
        # an obsolete font schema that requires a different migration gate.
        'schema_version': 3,
        'series_key': series_key,
        'revision': 1,
        'canonical_root': root,
        'canonical_display_name': work.title,
        'domain': work.domain,
        'provider_evidence': [provider],
        'season_bindings': [{
            'season_binding_id': binding_id,
            'season_number': season,
            'provider_identity': provider,
            'decision_source': str(metadata.get('season_source') or 'provider'),
            'task_id': existing_plan.task_id,
        }],
        'materialization_state': 'materialized',
        'migration_source': None,
        'associated_assets': [],
    }
    # Validate the actual supported historical schema; never call today's
    # register_completed, sign a proof, or weaken its registration safety gate.
    catalog.validate_archived_payload(payload, series_key)
    catalog.root.mkdir(parents=True, exist_ok=True)
    path = catalog.root / f'{series_key}.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
    return catalog.load(series_key)
