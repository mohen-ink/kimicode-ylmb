# -*- coding: utf-8 -*-
import http.client
import json
import math
import re
from urllib.parse import urlsplit

HOST = '127.0.0.1'
PORT = 39281
READ_ROUTES = {
    'modes': '/api/modes',
    'manager': '/api/model-manager',
    'audit': '/api/data',
}
WRITE_ROUTES = {
    'apply': '/api/modes/apply',
    'lock': '/api/modes/lock',
    'unlock': '/api/modes/unlock',
    'restore': '/api/modes/restore',
    'reset': '/api/modes/reset',
    'save': '/api/modes/save',
    'provider': '/api/model-manager/provider',
    'model': '/api/model-manager/model',
    'batch': '/api/model-manager/batch',
    'catalog': '/api/model-manager/catalog',
    'default': '/api/set-default',
    'capability': '/api/toggle-capability',
    'price': '/api/set-price',
    'match-prices': '/api/models-dev/match',
    'apply-prices': '/api/models-dev/apply',
    'enable-all': '/api/auto-enable-all',
    'issue': '/api/issue-action',
}


class PanelError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _text(value, limit=256, allow_empty=False):
    if (not isinstance(value, str) or len(value) > limit or
            (not allow_empty and not value) or
            any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise PanelError('面板参数不合法')
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PanelError('价格或汇率不是有效数字')
    if value < 0 or value > 1000000:
        raise PanelError('数值超出允许范围')
    return value


def _fields(data, allowed, required=()):
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise PanelError('面板参数不被允许或缺少字段')
    return data


def _mode(data):
    _fields(data, ('name', 'scene', 'main', 'subagents', 'effort'),
            ('name', 'scene', 'main', 'subagents', 'effort'))
    for key in ('name', 'scene', 'main'):
        _text(data[key], 128)
    if not isinstance(data['subagents'], list) or len(data['subagents']) > 3:
        raise PanelError('子代理数量不合法')
    for item in data['subagents']:
        _text(item, 128)
    if not isinstance(data['effort'], str) or data['effort'] not in ('', 'off', 'on', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise PanelError('思考档位不合法')
    return data


def _model(data):
    _fields(data, ('alias', 'provider', 'model', 'display_name', 'max_context_size',
                   'capabilities', 'support_efforts', 'default_effort'),
            ('alias', 'provider', 'model', 'max_context_size', 'capabilities',
             'support_efforts', 'default_effort'))
    for key in ('alias', 'provider', 'model'):
        _text(data[key], 256)
    if 'display_name' in data:
        _text(data['display_name'], 256, True)
    if type(data['max_context_size']) is not int or not 1 <= data['max_context_size'] <= 10000000:
        raise PanelError('上下文长度不合法')
    for key, allowed in (('capabilities', {'image_in', 'video_in', 'thinking', 'always_thinking', 'tool_use', 'dynamically_loaded_tools'}),
                         ('support_efforts', {'low', 'medium', 'high', 'xhigh', 'max'})):
        values = data[key]
        if not isinstance(values, list) or len(values) > 8 or any(type(v) is not str or v not in allowed for v in values):
            raise PanelError('模型能力参数不合法')
    if data['default_effort'] is not None and data['default_effort'] not in ('low', 'medium', 'high', 'xhigh', 'max'):
        raise PanelError('默认思考档位不合法')
    return data


def validate(action, body):
    if action in ('apply', 'lock'):
        _fields(body, ('index', 'session_id'), ('index',))
        if type(body['index']) is not int or not 0 <= body['index'] < 6:
            raise PanelError('档位序号不合法')
        if 'session_id' in body and (not isinstance(body['session_id'], str) or
                                    not re.fullmatch(r'session_[A-Za-z0-9_-]{8,100}', body['session_id'])):
            raise PanelError('会话标识不合法')
    elif action in ('unlock', 'restore', 'reset'):
        _fields(body, ())
    elif action == 'save':
        _fields(body, ('modes',), ('modes',))
        if not isinstance(body['modes'], list) or not 2 <= len(body['modes']) <= 6:
            raise PanelError('档位数量不合法')
        for mode in body['modes']:
            _mode(mode)
    elif action == 'provider':
        _fields(body, ('version', 'original_name', 'provider'), ('version', 'original_name', 'provider'))
        _text(body['version'], 128)
        if body['original_name'] is not None:
            _text(body['original_name'], 128)
        provider = _fields(body['provider'], ('name', 'type', 'base_url', 'key_action', 'api_key'),
                           ('name', 'type', 'base_url', 'key_action'))
        _text(provider['name'], 128)
        _text(provider['type'], 64)
        _text(provider['base_url'], 2048, True)
        if provider['key_action'] not in ('keep', 'replace', 'clear'):
            raise PanelError('密钥操作不合法')
        if provider['key_action'] == 'replace':
            _text(provider.get('api_key'), 4096)
        elif 'api_key' in provider:
            raise PanelError('密钥字段不被允许')
    elif action == 'model':
        _fields(body, ('version', 'original_alias', 'model', 'set_default'),
                ('version', 'original_alias', 'model'))
        _text(body['version'], 128)
        if body['original_alias'] is not None:
            _text(body['original_alias'], 256)
        _model(body['model'])
        if 'set_default' in body and type(body['set_default']) is not bool:
            raise PanelError('默认模型参数不合法')
    elif action == 'batch':
        _fields(body, ('version', 'provider', 'upserts', 'removes'),
                ('version', 'provider', 'upserts', 'removes'))
        _text(body['version'], 128)
        _text(body['provider'], 128)
        for name in ('upserts', 'removes'):
            if not isinstance(body[name], list) or len(body[name]) > 50:
                raise PanelError('批量模型数量不合法')
        for item in body['upserts']:
            _fields(item, ('original_alias', 'model'), ('original_alias', 'model'))
            if item['original_alias'] is not None:
                _text(item['original_alias'], 256)
            _model(item['model'])
        for alias in body['removes']:
            _text(alias, 256)
    elif action == 'catalog':
        _fields(body, ('version', 'provider'), ('version', 'provider'))
        _text(body['version'], 128)
        _text(body['provider'], 128)
    elif action == 'default':
        _fields(body, ('alias',), ('alias',))
        _text(body['alias'], 256)
    elif action == 'capability':
        _fields(body, ('alias', 'capability', 'enabled'), ('alias', 'capability', 'enabled'))
        _text(body['alias'], 256)
        if body['capability'] not in ('image_in', 'thinking', 'always_thinking', 'tool_use') or type(body['enabled']) is not bool:
            raise PanelError('能力选项不合法')
    elif action == 'price':
        _fields(body, ('alias', 'mode', 'price', 'input', 'output', 'cache_hit', 'cache_write'),
                ('alias', 'mode'))
        _text(body['alias'], 256)
        if body['mode'] not in ('', 'volume', 'per_call'):
            raise PanelError('计价模式不合法')
        for name in ('price', 'input', 'output', 'cache_hit', 'cache_write'):
            if name in body:
                _number(body[name])
    elif action == 'match-prices':
        _fields(body, ('rate',))
        if 'rate' in body and not 0 < _number(body['rate']) <= 100:
            raise PanelError('汇率不合法')
    elif action == 'apply-prices':
        _fields(body, ('rows',), ('rows',))
        if not isinstance(body['rows'], list) or not 0 < len(body['rows']) <= 200:
            raise PanelError('价格项目数量不合法')
        for row in body['rows']:
            _fields(row, ('alias', 'selected', 'cost'), ('alias', 'selected', 'cost'))
            _text(row['alias'], 256)
            if type(row['selected']) is not bool:
                raise PanelError('价格选择值不合法')
            _fields(row['cost'], ('mode', 'input', 'output', 'cache_hit', 'cache_write'),
                    ('mode', 'input', 'output', 'cache_hit'))
            if row['cost']['mode'] != 'volume':
                raise PanelError('价格模式不合法')
            for key, value in row['cost'].items():
                if key != 'mode':
                    _number(value)
    elif action == 'enable-all':
        _fields(body, ())
    elif action == 'issue':
        _fields(body, ('action', 'alias', 'code'), ('action',))
        if body['action'] not in ('fix', 'fix_all', 'ignore', 'unignore', 'unignore_all'):
            raise PanelError('问题处理方式不合法')
        for key in ('alias', 'code'):
            if key in body:
                _text(body[key], 256, True)
        if body['action'] in ('fix', 'ignore', 'unignore') and not all(body.get(k) for k in ('alias', 'code')):
            raise PanelError('缺少模型或问题代码')
    else:
        raise PanelError('接口不存在', 404)
    return body


def _safe_message(value):
    if not isinstance(value, str) or len(value) > 320 or re.search(r'(?i)(https?://|\\\\|[A-Za-z]:[/\\]|/(?:Users|home|root|etc)/)', value):
        return '操作未完成，请在电脑端检查配置'
    return value


def _public(action, result):
    if not isinstance(result, dict):
        raise PanelError('面板响应格式异常', 502)
    if action == 'modes':
        return {key: result[key] for key in ('modes', 'active', 'last_applied', 'locked', 'has_snapshot',
                                            'current', 'undefined_pool', 'models', 'subs_max', 'min', 'max') if key in result}
    if action == 'audit':
        models = []
        for item in result.get('models', [])[:500]:
            if not isinstance(item, dict):
                continue
            issues = []
            for issue in item.get('effort_issues', [])[:20]:
                if isinstance(issue, dict):
                    issues.append({'level': issue.get('level', ''),
                                   'code': issue.get('code', ''),
                                   'msg': _safe_message(issue.get('msg')),
                                   'fix': _safe_message(issue.get('fix')) if issue.get('fix') else '',
                                   'dismissed': issue.get('dismissed') is True})
            models.append({'alias': item.get('alias', ''), 'issues': issues})
        return {'models': models}
    if action == 'manager':
        providers = []
        for item in result.get('providers', [])[:100]:
            if not isinstance(item, dict):
                continue
            base = item.get('base_url', '')
            try:
                path = urlsplit(base).path.rstrip('/').lower() if isinstance(base, str) else ''
            except ValueError:
                path = '/hidden'
            hidden = (not isinstance(base, str) or '?' in base or '#' in base or '@' in base
                      or path not in ('', '/v1', '/api', '/api/v1'))
            providers.append({'name': item.get('name', ''), 'type': item.get('type', ''),
                              'base_url': '' if hidden else base, 'url_hidden': hidden,
                              'has_api_key': item.get('has_api_key') is True,
                              'managed': item.get('managed') is True})
        models = [{key: item[key] for key in ('alias', 'provider', 'model', 'display_name',
                                            'max_context_size', 'capabilities', 'support_efforts',
                                            'default_effort', 'managed', 'delete_protected', 'delete_reason')
                   if key in item} for item in result.get('models', [])[:500] if isinstance(item, dict)]
        return {'version': result.get('version', ''), 'editable': result.get('editable') is True,
                'message': _safe_message(result.get('message', '')), 'providers': providers,
                'models': models, 'default_model': result.get('default_model', ''),
                'provider_types': result.get('provider_types', []),
                'effort_levels': result.get('effort_levels', [])}
    if action in ('apply', 'lock', 'unlock', 'restore', 'reset', 'save'):
        return {'success': result.get('success') is True,
                'message': _safe_message(result.get('message')),
                'data': _public('modes', result['data']) if isinstance(result.get('data'), dict) else None}
    if action == 'catalog':
        rows = result.get('models')
        return {'success': result.get('success') is True, 'message': _safe_message(result.get('message')),
                'version': result.get('version'), 'provider': result.get('provider'),
                'models': [{k: row[k] for k in ('id', 'name', 'model', 'display_name') if k in row}
                           for row in rows[:500] if isinstance(row, dict)] if isinstance(rows, list) else []}
    if action == 'match-prices':
        rows = result.get('rows')
        return {'success': result.get('success') is True,
                'message': _safe_message(result.get('message')),
                'rows': [{k: row[k] for k in ('alias', 'model', 'matched_id', 'provider', 'score',
                                             'exact', 'cost', 'rate') if k in row}
                         for row in rows[:200] if isinstance(row, dict)] if isinstance(rows, list) else []}
    output = {k: result[k] for k in ('success', 'code', 'version', 'changed') if k in result}
    output['message'] = _safe_message(result.get('message'))
    return output


def call(action, method, body=None):
    path = READ_ROUTES.get(action) if method == 'GET' else WRITE_ROUTES.get(action) if method == 'POST' else None
    if path is None:
        raise PanelError('接口不存在', 404)
    payload = None
    if method == 'POST':
        payload = json.dumps(validate(action, body), ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(payload) > 16384:
            raise PanelError('请求体过大', 413)
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15 if action in ('catalog', 'match-prices') else 5)
    try:
        headers = {'Accept': 'application/json', 'Host': '%s:%d' % (HOST, PORT)}
        if action == 'manager' or method == 'POST':
            headers['X-Kimi-Usage-Control'] = '1'
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        if response.length is not None and response.length > 1024 * 1024:
            raise PanelError('面板响应过大', 502)
        content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise PanelError('面板响应过大', 502)
        result = json.loads(content.decode('utf-8'))
        return response.status, _public(action, result)
    except PanelError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise PanelError('电脑面板暂时不可用', 503)
    finally:
        conn.close()
