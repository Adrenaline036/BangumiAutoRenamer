"""从 Bangumi 自身的完整 TV 链给出当前条目的季度建议。"""
from collections.abc import Mapping
from contextlib import nullcontext
import unicodedata

from .provider_link_resolver import _topological_chain
from .provider_selection_authority import is_bangumi_serial_platform, provider_candidate_group_kind
from .provider_transport import BangumiTransport, ProviderSearchError


def season_advice(client, selected):
    """关系缺失、歧义或读取失败均不产生默认季号。"""
    transport = getattr(client, '_transport', None)
    budget = transport.operation() if isinstance(transport, BangumiTransport) else nullcontext()
    try:
        with budget:
            return _read_chain(client, selected)
    except (ProviderSearchError, OSError, ValueError, TypeError, KeyError):
        return ()


def _read_chain(client, selected):
    seed = str(selected['id'])
    pending, details, visited, nodes, edges = [seed], {seed: selected}, set(), set(), set()
    tv_nodes = set()
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        if len(visited) >= 20:
            return ()
        visited.add(current)
        detail = details.get(current)
        if detail is None:
            detail = client._get_json(f'/v0/subjects/{current}')
        if (not isinstance(detail, Mapping) or type(detail.get('id')) is not int
                or str(detail['id']) != current or type(detail.get('type')) is not int
                or not isinstance(detail.get('platform'), str)):
            return ()
        if (detail['type'] != selected['type'] or provider_candidate_group_kind(dict(
                namespace='bangumi', entity_type='subject', platform=detail['platform'])) is None):
            return ()
        nodes.add(current)
        if is_bangumi_serial_platform(detail['platform']):
            tv_nodes.add(current)
        payload = client._get_json(f'/v0/subjects/{current}/subjects')
        rows = payload if isinstance(payload, list) else payload.get('data') if isinstance(payload, Mapping) else None
        if (not isinstance(rows, list) or isinstance(payload, Mapping)
                and type(payload.get('total')) is int and payload['total'] > len(rows)):
            return ()
        for row in rows:
            if not isinstance(row, Mapping):
                return ()
            relation = unicodedata.normalize('NFKC', str(row.get('relation') or '')).casefold()
            if relation not in {'前传', '续集', 'prequel', 'sequel'}:
                continue
            related = row.get('subject', row)
            identity = str(related.get('id') or '') if isinstance(related, Mapping) else ''
            if not identity.isdigit():
                return ()
            edges.add((identity, current) if relation in {'前传', 'prequel'} else (current, identity))
            if identity not in visited:
                pending.append(identity)
    edges = {(a,b) for a,b in edges if a in nodes and b in nodes}
    if seed not in nodes or any(sum(a == node for a,b in edges) > 1 or sum(b == node for a,b in edges) > 1 for node in nodes):
        return ()
    chain = _topological_chain(nodes, edges)
    tv_chain = tuple(n for n in chain if n in tv_nodes) if chain else ()
    return ({'season_number': tv_chain.index(seed) + 1},) if seed in tv_chain else ()
