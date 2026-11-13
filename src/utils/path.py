import os
from pathlib import Path

MAIN_PATH = Path(__file__).parents[1]


def _resolve_data_path() -> Path:
    configured = os.getenv("BAR_DATA_DIR", "").strip()
    if not configured:
        return Path(__file__).parents[2] / "data"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


DATA_PATH = _resolve_data_path()

IMAGE = MAIN_PATH / 'images'
CONFIG_PATH = DATA_PATH / 'config.json'
LOG_PATH = DATA_PATH / 'log'
RECORD_PATH = DATA_PATH / 'record'
TASK_PATH = DATA_PATH / 'task'
PLAN_PATH = DATA_PATH / 'plan'
PLAN_REVIEW_PATH = DATA_PATH / 'plan_review'
WORK_CATALOG_PATH = DATA_PATH / 'work_catalog'
DECISION_STATE_PATH = DATA_PATH / 'decision_state'
SERIES_CATALOG_PATH = DATA_PATH / 'series_catalog'
RECOVERY_TRANSACTION_PATH = DATA_PATH / 'recovery_transactions'
PROVIDER_LINK_CATALOG_PATH = DATA_PATH / 'provider_link_catalog'
RECOVERY_ABANDON_AUDIT_PATH = DATA_PATH / 'recovery_abandon_audit'

log_path = LOG_PATH / 'BAR.log'
for i in [
    DATA_PATH,
    LOG_PATH,
    RECORD_PATH,
    TASK_PATH,
    PLAN_PATH,
    PLAN_REVIEW_PATH,
    WORK_CATALOG_PATH,
    DECISION_STATE_PATH,
    SERIES_CATALOG_PATH,
    RECOVERY_TRANSACTION_PATH,
    PROVIDER_LINK_CATALOG_PATH,
    RECOVERY_ABANDON_AUDIT_PATH,
]:
    i.mkdir(parents=True, exist_ok=True)
