"""Successful Provider JSON reuse within one synchronous worker operation only."""
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import json

_responses = ContextVar('bar_provider_operation', default=None)


def provider_operation(function):
    @wraps(function)
    def run(*args, **kwargs):
        if _responses.get() is not None:
            return function(*args, **kwargs)
        token = _responses.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _responses.reset(token)
    return run


def successful_json(key, fetch):
    cache = _responses.get()
    if cache is None:
        return fetch()
    if key not in cache:
        value = fetch()  # Exceptions and malformed/non-JSON results never enter the cache.
        json.dumps(value, allow_nan=False)
        cache[key] = deepcopy(value)
    return deepcopy(cache[key])


def tmdb_info(client, **params):
    """Explicit wrapper for TMDB info, preserving library attribute population."""
    import tmdbsimple
    from tmdbsimple.base import TMDB
    if not isinstance(client, TMDB):  # Existing test/provider adapters keep their contract.
        return client.info(**params)
    key = ('tmdb-info', type(client), client.base_uri, client.BASE_PATH,
           client.URLS['info'], id(client.session), tmdbsimple.API_KEY,
           tuple((name, getattr(client, name, None)) for name in ('id', 'tv_id', 'season_number')),
           json.dumps(params, sort_keys=True), json.dumps(client.headers, sort_keys=True))
    value = successful_json(key, lambda: client.info(**params))
    client._set_attrs_to_values(value)
    return value
