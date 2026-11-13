"""Bangumi 请求预算与可安全保存的失败信息。"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time

import requests


class ProviderSearchError(RuntimeError):
    """资料不能作为可靠观察使用。"""


class ProviderRequestError(ProviderSearchError):
    def __init__(self, *, stage, code, attempts, elapsed, status=None, timeout_kind=None):
        self.diagnostic = dict(provider='bangumi', stage=stage, code=code,
                               attempts=attempts, elapsed_seconds=round(elapsed, 3))
        if isinstance(status, int):
            self.diagnostic['status'] = status
        if code == 'timeout' and timeout_kind in {'connect', 'read'}:
            self.diagnostic['timeout_kind'] = timeout_kind
        self.retryable = code in {'timeout', 'connection', 'rate_limited', 'server_error', 'budget_exhausted'}
        reason = {'timeout': '请求超时', 'connection': '连接失败', 'rate_limited': '服务限流',
                  'server_error': '服务暂时异常', 'budget_exhausted': '本次读取时间已用完',
                  'not_found': '条目不存在', 'unauthorized': '访问未获授权',
                  'invalid_response': '返回资料格式无效', 'request_failed': '请求失败'}[code]
        label = {'search': '搜索', 'detail': '条目', 'relations': '关系'}[stage]
        super().__init__(f'Bangumi {label}读取失败：{reason}')


def retry_after_seconds(value, now):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = (parsed - now).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, delay) if math.isfinite(delay) else 0.0


class BangumiTransport:
    """同步请求只在剩余预算内重试；底层 DNS 与阻塞读不能强制中断。"""
    def __init__(self, session, headers, *, sleeper=time.sleep, clock=time.monotonic,
                 wall_clock=lambda: datetime.now(timezone.utc), request_budget=20.0,
                 relation_budget=60.0):
        self.session, self.headers = session, headers
        self.sleep, self.clock, self.wall_clock = sleeper, clock, wall_clock
        self.request_budget, self.relation_budget = request_budget, relation_budget
        self.deadline = ContextVar('bangumi_deadline', default=None)

    @contextmanager
    def operation(self):
        existing = self.deadline.get()
        token = self.deadline.set(existing if existing is not None else self.clock() + self.relation_budget)
        try:
            yield
        finally:
            self.deadline.reset(token)

    def check_deadline(self, stage):
        deadline = self.deadline.get()
        if deadline is not None and self.clock() >= deadline:
            raise ProviderRequestError(stage=stage, code='budget_exhausted', attempts=0, elapsed=0)

    def request(self, method, endpoint, *, stage, **kwargs):
        started = self.clock()
        deadline = min(self.deadline.get() or float('inf'), started + self.request_budget)
        code, status = 'budget_exhausted', None
        for attempt in range(1, 4):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ProviderRequestError(stage=stage, code='budget_exhausted', attempts=attempt-1,
                                           elapsed=self.clock()-started)
            response = None
            status = None
            try:
                response = getattr(self.session, method)(
                    'https://api.bgm.tv' + endpoint, headers=self.headers,
                    timeout=(min(5.0, remaining/2), min(12.0, remaining/2)), **kwargs)
                raw_status = getattr(response, 'status_code', None)
                status = raw_status if isinstance(raw_status, int) else None
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, (dict, list)):
                    raise ValueError('invalid response')
                if self.clock() >= deadline:
                    raise ProviderRequestError(stage=stage, code='budget_exhausted', attempts=attempt,
                                               elapsed=self.clock()-started)
                if stage == 'detail' and not isinstance(payload, dict):
                    raise ValueError('invalid detail')
                if stage == 'search' and (not isinstance(payload, dict) or not isinstance(payload.get('data'), list)):
                    raise ValueError('invalid search')
                if stage == 'relations' and not (isinstance(payload, list) or
                        isinstance(payload, dict) and isinstance(payload.get('data'), list)):
                    raise ValueError('invalid relations')
                return payload
            except ProviderRequestError:
                raise
            except (requests.RequestException, ValueError, TypeError) as exc:
                if isinstance(exc, requests.Timeout):
                    code = 'timeout'
                elif isinstance(exc, requests.ConnectionError):
                    code = 'connection'
                elif status == 429:
                    code = 'rate_limited'
                elif status is not None and 500 <= status < 600:
                    code = 'server_error'
                elif status == 404:
                    code = 'not_found'
                elif status in {401, 403}:
                    code = 'unauthorized'
                elif isinstance(exc, (ValueError, TypeError)):
                    code = 'invalid_response'
                else:
                    code = 'request_failed'
                failure = ProviderRequestError(stage=stage, code=code, attempts=attempt,
                                               elapsed=self.clock()-started, status=status,
                                               timeout_kind=('connect' if isinstance(exc, requests.ConnectTimeout)
                                                             else 'read' if isinstance(exc, requests.ReadTimeout) else None))
                if not failure.retryable or attempt == 3:
                    raise failure from None
                delay = 0.5 * attempt
                if status in {429, 503}:
                    delay = max(delay, retry_after_seconds(response.headers.get('Retry-After'), self.wall_clock()))
                if delay >= deadline - self.clock():
                    raise failure from None
                self.sleep(delay)
        raise AssertionError('unreachable')
