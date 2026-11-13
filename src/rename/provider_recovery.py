"""失败后的资料恢复策略；只投影提示，不授予重试权限。"""
from collections.abc import Mapping


def transport_failure_text(context):
    """列表和作品卡共用受控文案，不显示底层异常或请求地址。"""
    if context.get('lifecycle') not in {'failed', 'interrupted'}:
        return ''
    if context.get('failure_code') == 'internal_busy':
        return '本次内部排队未能完成，所选作品已保留。请稍后在原任务重试。'
    evidence = context.get('provider_subject_evidence')
    diagnostic = evidence.get('transport_diagnostic') if isinstance(evidence, Mapping) else None
    if not isinstance(diagnostic, Mapping) or diagnostic.get('provider') != 'bangumi':
        return ''
    reason = {
        'timeout': {'connect': '连接超时', 'read': '响应读取超时'}.get(diagnostic.get('timeout_kind'), '请求超时'),
        'connection': '连接失败', 'rate_limited': '服务限流，请稍后重试',
        'server_error': '服务暂时异常', 'budget_exhausted': '本次读取时间已用完',
        'not_found': '条目不存在，请检查作品链接', 'unauthorized': '访问被拒绝，请检查访问配置',
        'invalid_response': '返回资料格式无效', 'request_failed': '请求失败',
    }.get(diagnostic.get('code'))
    if not reason:
        return ''
    stage = {'search': '搜索', 'detail': '作品详情', 'relations': '作品关系'}.get(diagnostic.get('stage'), '资料')
    return f'Bangumi {stage}读取失败：{reason}。'


def can_refresh_selection(context):
    if context.get('lifecycle') not in {'failed', 'interrupted', 'awaiting_user'}:
        return False
    if not isinstance(context.get('candidate'), Mapping):
        return False
    return bool(context.get('retry_requires_refresh') or
                (context.get('retryable') and context.get('failure_code') in {'provider_unavailable', 'internal_busy', 'selection_failed'}))


def selection_guidance(context):
    if context.get('lifecycle') in {'failed', 'interrupted', 'legacy_stale'}:
        if context.get('failure_code') in {'internal_busy', 'selection_failed'} and can_refresh_selection(context):
            return '所选作品已保留，请稍后点击“重新读取资料”重试本次选择。'
        if can_refresh_selection(context):
            return '所选作品已保留，资料尚未读取完成。请点击“重新获取资料”后再确认。'
        return '所选作品已保留，本次处理未完成。请按上方提示处理。'
    if context.get('lifecycle') == 'pending':
        return '正在读取所选作品资料，请稍候。'
    return '作品链接已保存。请继续本次处理或季度确认；需要改选时点击“换一个作品”。'
