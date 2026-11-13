"""ERR048 historical compatibility, not current-execution provenance.

The historical Plan/Decision/receipt and completed ledger are explicit synthetic
inputs. Their tiny source/target files really are hardlinks in TemporaryDirectory.
Only the new Bangumi peer uses the normal AddTask/selection production pipeline.
No fixture upgrades the historical receipt with a registration authority.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from src.rename.decision_state import DecisionStateStore
from src.rename.plan_store import PlanStore
from src.rename.provider_link_catalog import ProviderLinkCatalog, ProviderLinkMapping
from src.rename.provider_link_resolver import ProviderLinkResolution
from src.rename.rename_plan import ExecutionPolicy, PlanEntry, ProviderIdentity, RenamePlan, WorkIdentity
from src.rename.series_catalog import SeriesCatalog
from src.rename import series_registration as registration
from tests.legacy_series_fixture import write_legacy_series_catalog_fixture
from tests.presentation_fixtures import with_tv_presentation
from tests.projection_test_support import save_plan_fixture
from tests.test_r24_err048_series_registration_contract import MEMBERS, MultiTaskFlow


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def _mapping(key, *, tv_id='83121'):
    evidence = {'fixture': 'historical materialized crosswalk', 'tmdb_tv_id': tv_id,
        'members': list(MEMBERS)}
    return ProviderLinkMapping.from_dict({
        'schema_version': 1, 'mapping_id': f'provider-link-{uuid.uuid4()}',
        'revision': 0, 'local_series_key': key, 'status': 'materialized_confirmed',
        'provider_edges': [
            {'namespace': 'tmdb', 'entity_type': 'tv', 'provider_id': tv_id, 'role': 'series'},
            *[{'namespace': 'bangumi', 'entity_type': 'subject', 'provider_id': subject,
               'role': 'season'} for subject in MEMBERS]],
        'season_edges': [
            {'namespace': 'bangumi', 'entity_type': 'subject', 'provider_id': subject,
             'tmdb_tv_id': tv_id, 'season_number': season}
            for season, subject in enumerate(MEMBERS, 1)],
        'evidence_snapshot': evidence, 'evidence_fingerprint': _digest(evidence),
    })


def _historical(root, *, frozen_execution=False):
    """Explicit old receipt, genuine tiny hardlink, matching completed evidence."""
    root = Path(root)
    key = f'series-{uuid.uuid4()}'
    task_id = 'historical-completed-tv'
    source = root / 'historical-source' / 'Episode 01.mkv'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'isolated historical episode bytes\n')
    work_root = root / 'Anime' / 'Historical Show (2019)'
    target = work_root / 'Season 01' / 'Historical Show S01E01.mkv'
    target.parent.mkdir(parents=True)
    os.link(source, target)
    provider = ProviderIdentity('confirmed', 'tmdb', 'tv', '83121',
        'Historical Show', 2019, 'explicit_user')
    work = WorkIdentity('confirmed', 'animation', 'Historical Show', 2019,
        provider, 'provider_selection')
    plans, decisions = PlanStore(root / 'plans'), DecisionStateStore(root / 'decisions')
    plan = save_plan_fixture(plans, with_tv_presentation(RenamePlan(
        task_id=task_id, source_root=source.parent,
        execution_policy=ExecutionPolicy(target_root=root/'Anime') if frozen_execution else None,
        entries=(PlanEntry(source, target, season_id=1, episode_id=1,
            media_kind='video', role='main_episode'),), work_identity=work,
        metadata={'series_key': key, 'season_binding_id': 'historical-season-1',
            'season_number': 1, 'season_source': 'provider_crosswalk',
            'series_catalog_revision': 0, 'canonical_work_root': str(work_root)})),
        state_store=decisions)
    fingerprint = plans.fingerprint(plan)
    task = {'uuid': task_id, 'workflow_status': 'completed', 'execution_status': 'completed',
        'execution_fingerprint': fingerprint, 'plan_fingerprint': fingerprint,
        'execution_requires_manual_check': False, 'execution_may_have_written_targets': False}
    task_path = root / 'tasks' / f'{task_id}.json'
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(json.dumps(task), encoding='utf-8')
    ledger = {'task_id': task_id, 'status': 'completed', 'plan_fingerprint': fingerprint,
        'entries': [{'source': str(source), 'target': str(target), 'status': 'created',
            'action': 'create', 'transfer_type': 'hardlink'}]}
    ledger_path = root / 'records' / f'{task_id}.ledger.json'
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
    catalog = SeriesCatalog(root / 'series_catalog')
    write_legacy_series_catalog_fixture(catalog, plan)
    protected = (plans.path_for(task_id), decisions.path_for(task_id), task_path,
        ledger_path, catalog._path(key), source, target)
    return {'plan': plan, 'decision': decisions.load(task_id), 'task': task,
        'catalog': catalog, 'protected': {path: path.read_bytes() for path in protected},
        'key': key, 'source': source, 'target': target}






if __name__ == '__main__':
    unittest.main()
