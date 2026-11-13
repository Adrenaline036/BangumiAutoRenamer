"""Build pre-discard-policy fixtures through the old frozen contract.

Only explicit compatibility tests use this. No execution/validation function is
patched: the field is absent before Decision, Plan and approval are generated.
"""
from contextlib import contextmanager
from functools import wraps
from inspect import iscoroutinefunction
from unittest.mock import patch


@contextmanager
def historical_replacement_fixture():
    from src.rename.asset_actions import AssetActionService
    from src.rename.asset_operation import AssetOperation
    prepare = AssetActionService.prepare_replacement
    def old_prepare(*args, **kwargs):
        operation, draft = prepare(*args, **kwargs)
        value = operation.to_dict()
        value.pop('completion_policy', None)
        return AssetOperation.from_mapping(value), draft
    with patch.object(AssetActionService, 'prepare_replacement', old_prepare):
        yield


def historical_replacement(function):
    if iscoroutinefunction(function):
        @wraps(function)
        async def run(*args, **kwargs):
            with historical_replacement_fixture():
                return await function(*args, **kwargs)
    else:
        @wraps(function)
        def run(*args, **kwargs):
            with historical_replacement_fixture():
                return function(*args, **kwargs)
    return run
