# -*- coding: utf-8 -*-
import copy
import hashlib
import math
from datetime import date, datetime, time
from urllib.parse import urlsplit

PROVIDER_TYPES = ('openai', 'openai_responses', 'anthropic', 'google-genai', 'kimi', 'vertexai')
EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')
CAPABILITIES = ('tool_use', 'thinking', 'always_thinking', 'image_in', 'video_in',
                'audio_in', 'dynamically_loaded_tools')
MODEL_FIELDS = {'provider': str, 'model': str, 'display_name': str,
                'max_context_size': int, 'capabilities': list, 'support_efforts': list,
                'default_effort': str, 'adaptive_thinking': bool}


class ConfigError(ValueError):
    pass


def parse(content):
    try:
        import tomllib as parser
    except ImportError:
        try:
            import toml as parser
        except ImportError:
            raise ConfigError('缺少严格 TOML 解析器，拒绝改写') from None
    try:
        return parser.loads(content)
    except Exception:
        raise ConfigError('TOML 解析失败，拒绝改写') from None


def semantic_equal(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return (left.keys() == right.keys() and
                all(semantic_equal(left[key], right[key]) for key in left))
    if isinstance(left, list) and isinstance(right, list):
        return (len(left) == len(right) and
                all(semantic_equal(a, b) for a, b in zip(left, right)))
    if type(left) is not type(right):
        return False
    if isinstance(left, float):
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right)
        if left == 0.0 and right == 0.0:
            return math.copysign(1.0, left) == math.copysign(1.0, right)
    if isinstance(left, (datetime, date, time)):
        return left.isoformat() == right.isoformat()
    return left == right


def _toml_string(value):
    if not isinstance(value, str):
        raise ConfigError('TOML 键必须是字符串')
    escapes = {'"': '\\"', '\\': '\\\\', '\b': '\\b', '\t': '\\t',
               '\n': '\\n', '\f': '\\f', '\r': '\\r'}
    parts = ['"']
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise ConfigError('TOML 字符串含无效 Unicode，拒绝改写')
        if char in escapes:
            parts.append(escapes[char])
        elif code < 32 or code == 127:
            parts.append('\\u%04X' % code)
        else:
            parts.append(char)
    parts.append('"')
    return ''.join(parts)


def _toml_keys(table):
    if any(not isinstance(key, str) for key in table):
        raise ConfigError('TOML 键必须是字符串')
    return sorted(table)


def _toml_value(value):
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) is int:
        return str(value)
    if type(value) is float:
        if math.isnan(value):
            return '-nan' if math.copysign(1.0, value) < 0 else 'nan'
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return '[ ' + ', '.join(_toml_value(item) for item in value) + ' ]'
    if isinstance(value, dict):
        return '{ ' + ', '.join(_toml_string(key) + ' = ' + _toml_value(value[key])
                               for key in _toml_keys(value)) + ' }'
    raise ConfigError('配置含不支持的 TOML 值类型，拒绝改写')


def dumps(data):
    try:
        if not isinstance(data, dict):
            raise ConfigError('TOML 根节点必须是表')
        lines = []

        def emit(table, path):
            keys = _toml_keys(table)
            if path:
                if lines:
                    lines.append('')
                lines.append('[' + '.'.join(_toml_string(key) for key in path) + ']')
            for key in keys:
                if not isinstance(table[key], dict):
                    lines.append(_toml_string(key) + ' = ' + _toml_value(table[key]))
            for key in keys:
                if isinstance(table[key], dict):
                    emit(table[key], path + (key,))

        emit(data, ())
        result = '\n'.join(lines) + ('\n' if lines else '')
        if not semantic_equal(data, parse(result)):
            raise ConfigError('完整序列化前后语义与预期不一致，配置未改动')
        return result
    except ConfigError:
        raise
    except Exception:
        raise ConfigError('配置无法完整序列化，拒绝改写') from None


def version(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def effective(model, key):
    overrides = model.get('overrides', {})
    if not isinstance(overrides, dict):
        raise ConfigError('模型 overrides 必须是表')
    return overrides[key] if key in overrides else model.get(key)


def managed_provider(name, provider):
    return name.startswith('managed:') or 'oauth' in provider


def managed_model(model, providers):
    name = model.get('provider', '')
    return managed_provider(name, providers.get(name, {}))


def _table(data, path, create=False):
    current = data
    if not isinstance(current, dict):
        raise ConfigError('TOML 根节点必须是表')
    for key in path:
        if create and key not in current:
            current[key] = {}
        current = current.get(key)
        if not isinstance(current, dict):
            raise ConfigError('TOML 路径必须指向表，拒绝改写')
    return current


def _patch_fields_data(data, path, updates):
    if not any(value is not None for value in updates.values()):
        try:
            target = _table(data, path)
        except ConfigError:
            return
    else:
        target = _table(data, path, create=True)
    for key, value in updates.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = copy.deepcopy(value)


def patch_fields(content, path, updates):
    data = parse(content)
    _patch_fields_data(data, path, updates)
    return dumps(data)


def _string(value, label, required=False):
    if not isinstance(value, str) or (required and not value.strip()) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigError(label + ' 必须是有效字符串')
    return value


def _array(value, label):
    if not isinstance(value, list):
        raise ConfigError(label + ' 必须是字符串数组')
    for item in value:
        _string(item, label, True)
    if len(set(value)) != len(value):
        raise ConfigError(label + ' 不允许重复值')
    return list(value)


def normalize_model_alias(alias, provider):
    _string(alias, 'alias', True)
    _string(provider, 'provider', True)
    prefix = provider + '/'
    return alias if alias.startswith(prefix) else prefix + alias


def _ordered_efforts(value):
    efforts = _array(value, 'support_efforts')
    if any(level not in EFFORT_LEVELS for level in efforts):
        raise ConfigError('support_efforts 含不支持的档位')
    return [level for level in EFFORT_LEVELS if level in efforts]


def normalize_support_efforts_data(data):
    models = data.get('models', {})
    if not isinstance(models, dict):
        raise ConfigError('models 必须是表')
    for model in models.values():
        if not isinstance(model, dict):
            raise ConfigError('模型必须是表')
        overrides = model.get('overrides', {})
        if not isinstance(overrides, dict):
            raise ConfigError('模型 overrides 必须是表')
        for target in (model, overrides):
            if 'support_efforts' in target:
                target['support_efforts'] = _ordered_efforts(target['support_efforts'])
    return data


def normalize_support_efforts(content):
    return dumps(normalize_support_efforts_data(parse(content)))


def _validate_updates(model, updates):
    if not isinstance(updates, dict) or not updates or set(updates) - set(MODEL_FIELDS):
        raise ConfigError('不支持的模型更新字段')
    for key, value in updates.items():
        if key == 'default_effort' and value is None:
            continue
        kind = MODEL_FIELDS[key]
        if kind is int:
            if type(value) is not int or not 1 <= value <= 9223372036854775807:
                raise ConfigError('max_context_size 必须是正整数')
        elif kind is bool:
            if type(value) is not bool:
                raise ConfigError(key + ' 必须是布尔值')
        elif kind is list:
            _array(value, key)
        else:
            _string(value, key, key in ('provider', 'model'))
    if 'capabilities' in updates:
        old = _array(effective(model, 'capabilities') or [], 'capabilities')
        caps = updates['capabilities']
        if any(cap not in CAPABILITIES and cap not in old for cap in caps):
            raise ConfigError('不支持的模型能力')
        updates['capabilities'] = caps + [cap for cap in old if cap not in CAPABILITIES and cap not in caps]
        if 'always_thinking' in old and 'always_thinking' not in updates['capabilities']:
            updates['capabilities'].append('always_thinking')
    efforts = _ordered_efforts(updates.get('support_efforts', effective(model, 'support_efforts') or []))
    if 'support_efforts' in updates:
        updates['support_efforts'] = efforts
    default = updates.get('default_effort', effective(model, 'default_effort'))
    if default is not None and (default not in EFFORT_LEVELS or (efforts and default not in efforts)):
        raise ConfigError('default_effort 无效或不在 support_efforts 内')


def _update_model_data(data, alias, updates):
    _string(alias, 'alias', True)
    models, providers = _table(data, ('models',)), data.get('providers', {})
    if not isinstance(providers, dict):
        raise ConfigError('providers 必须是表')
    if alias not in models or not isinstance(models[alias], dict):
        raise ConfigError('模型不存在')
    model = models[alias]
    changes = copy.deepcopy(updates)
    _validate_updates(model, changes)
    managed = managed_model(model, providers)
    for key in ('provider', 'model'):
        if managed and key in changes and changes[key] != model.get(key):
            raise ConfigError('托管模型的 provider/model 不可修改')
    if 'provider' in changes and changes['provider'] not in providers:
        raise ConfigError('provider 不存在')
    top, overrides = {}, {}
    for key, value in changes.items():
        if key in ('provider', 'model'):
            if not managed:
                top[key] = value
        elif managed or key in model.get('overrides', {}):
            overrides[key] = value
        else:
            top[key] = value
        if key == 'default_effort' and value is None:
            if key in model:
                top[key] = None
            if key in model.get('overrides', {}):
                overrides[key] = None
    if top:
        _patch_fields_data(data, ('models', alias), top)
    if overrides:
        _patch_fields_data(data, ('models', alias, 'overrides'), overrides)


def update_model(content, alias, updates):
    data = parse(content)
    _update_model_data(data, alias, updates)
    return dumps(data)


def _upsert_model_data(data, original_alias, item, set_default=False):
    if not isinstance(item, dict) or set(item) - {'alias', *MODEL_FIELDS} or 'adaptive_thinking' in item:
        raise ConfigError('模型参数不被允许')
    if type(set_default) is not bool:
        raise ConfigError('set_default 必须是布尔值')
    alias = _string(item.get('alias'), 'alias', True)
    models, providers = data.get('models', {}), data.get('providers', {})
    if not isinstance(models, dict) or not isinstance(providers, dict):
        raise ConfigError('providers/models 必须是表')
    if original_alias is not None:
        _string(original_alias, 'original_alias', True)
        if original_alias != alias:
            raise ConfigError('模型别名不可重命名')
        if alias not in models:
            raise ConfigError('原模型不存在')
    changes = copy.deepcopy({key: item[key] for key in MODEL_FIELDS if key in item})
    for key in ('provider', 'model', 'display_name', 'max_context_size', 'support_efforts', 'default_effort'):
        if key not in changes:
            raise ConfigError('缺少模型字段：' + key)
    changes.setdefault('capabilities', [])
    if changes['default_effort'] is not None and not changes['support_efforts']:
        raise ConfigError('默认思考强度必须在非空的支持档位列表中')
    if original_alias is None:
        _validate_updates({}, changes)
        if changes['provider'] not in providers:
            raise ConfigError('provider 不存在')
        alias = normalize_model_alias(alias, changes['provider'])
        if alias in models:
            raise ConfigError('模型别名已存在')
        _patch_fields_data(data, ('models', alias), changes)
    else:
        _update_model_data(data, alias, changes)
    if set_default:
        _set_default_model_data(data, alias)


def upsert_model(content, original_alias, item, set_default=False):
    data = parse(content)
    _upsert_model_data(data, original_alias, item, set_default)
    return dumps(data)


def config_references(data):
    references = {}

    def add(alias, reason):
        if alias is not None and alias != '':
            _string(alias, '模型引用', True)
            references.setdefault(alias, reason)

    add(data.get('default_model'), '被 default_model 引用，请先更换默认模型')
    secondary = data.get('secondary_model')
    if isinstance(secondary, str):
        add(secondary, '被 secondary_model 引用，请先解除子代理引用')
    elif secondary is not None:
        if not isinstance(secondary, dict):
            raise ConfigError('secondary_model 必须是表或模型别名')
        add(secondary.get('default_model'), '被 secondary_model.default_model 引用，请先解除子代理引用')
        pool = secondary.get('models', {})
        if not isinstance(pool, dict):
            raise ConfigError('secondary_model.models 必须是表')
        for alias in pool:
            add(alias, '被 secondary_model.models 引用，请先移出子代理模型池')
    return references


def deletion_reasons(data, references=None):
    models, providers = data.get('models', {}), data.get('providers', {})
    if not isinstance(models, dict) or not isinstance(providers, dict):
        raise ConfigError('providers/models 必须是表')
    protected = config_references(data)
    for alias, reason in (references or {}).items():
        _string(alias, '模型引用', True)
        _string(reason, '删除保护原因', True)
        protected.setdefault(alias, reason)
    for alias, model in models.items():
        if not isinstance(model, dict):
            raise ConfigError('模型必须是表')
        if managed_model(model, providers):
            protected[alias] = '托管/OAuth 模型由官方管理，不允许删除'
    return protected


def delete_model(content, alias):
    _string(alias, 'alias', True)
    data = parse(content)
    models = _table(data, ('models',))
    if alias not in models or not isinstance(models[alias], dict):
        raise ConfigError('模型不存在')
    reason = deletion_reasons(data).get(alias)
    if reason:
        raise ConfigError('模型不可删除：' + reason)
    del models[alias]
    return dumps(data)


def batch_models(content, provider, upserts, removes, references=None):
    _string(provider, 'provider', True)
    data = parse(content)
    providers, models = data.get('providers', {}), data.get('models', {})
    if not isinstance(providers, dict) or provider not in providers or not isinstance(providers[provider], dict):
        raise ConfigError('provider 不存在或配置无效')
    if not isinstance(models, dict):
        raise ConfigError('models 必须是表')
    if not isinstance(upserts, list) or not isinstance(removes, list):
        raise ConfigError('upserts/removes 必须是数组')
    if not upserts and not removes:
        raise ConfigError('没有需要保存的模型更改')
    if len(upserts) + len(removes) > 1000:
        raise ConfigError('单次批量操作最多 1000 项')
    removed = _array(removes, 'removes')
    protected = deletion_reasons(data, references)
    touched, items = set(removed), []
    required = {'alias', 'provider', 'model', 'display_name', 'max_context_size',
                'capabilities', 'support_efforts', 'default_effort'}
    for alias in removed:
        old = models.get(alias)
        if not isinstance(old, dict):
            raise ConfigError('待删除模型不存在')
        if old.get('provider') != provider:
            raise ConfigError('只能删除当前供应商的模型')
        if protected.get(alias):
            raise ConfigError('模型不可删除：' + protected[alias])
    for operation in upserts:
        if not isinstance(operation, dict) or set(operation) != {'original_alias', 'model'}:
            raise ConfigError('批量模型操作参数不被允许')
        item = operation['model']
        if not isinstance(item, dict) or set(item) != required:
            raise ConfigError('批量模型字段不被允许或缺少必填字段')
        item = copy.deepcopy(item)
        alias = _string(item['alias'], 'alias', True)
        if item['provider'] != provider:
            raise ConfigError('只能保存当前供应商的模型')
        original = operation['original_alias']
        if original is None:
            alias = normalize_model_alias(alias, provider)
            item['alias'] = alias
        if alias in touched:
            raise ConfigError('同一模型不允许重复操作或同时更新与删除')
        touched.add(alias)
        if original is None:
            if alias in models:
                raise ConfigError('模型别名已存在')
            if managed_provider(provider, providers[provider]):
                raise ConfigError('托管/OAuth 供应商不允许新增本地模型')
        else:
            _string(original, 'original_alias', True)
            if original != alias:
                raise ConfigError('模型别名不可重命名')
            old = models.get(alias)
            if not isinstance(old, dict):
                raise ConfigError('原模型不存在')
            if old.get('provider') != provider:
                raise ConfigError('只能编辑当前供应商的模型')
        items.append((original, item))
    for alias in removed:
        del models[alias]
    for original, item in items:
        _upsert_model_data(data, original, item)
    return dumps(data)


def _upsert_provider_data(data, original_name, item):
    if not isinstance(item, dict) or set(item) - {'name', 'type', 'base_url', 'key_action', 'api_key'}:
        raise ConfigError('供应商参数不被允许')
    name = _string(item.get('name'), 'name', True)
    providers = data.get('providers', {})
    if not isinstance(providers, dict):
        raise ConfigError('providers 必须是表')
    old = providers.get(name, {})
    if not isinstance(old, dict):
        raise ConfigError('供应商必须是表')
    if original_name is not None:
        _string(original_name, 'original_name', True)
        if original_name != name:
            raise ConfigError('供应商不可重命名')
        if name not in providers:
            raise ConfigError('原供应商不存在')
    elif name in providers:
        raise ConfigError('供应商名称已存在')
    if managed_provider(name, old):
        raise ConfigError('托管或 OAuth 供应商只读')
    protocol = _string(item.get('type'), 'type', True)
    if protocol not in PROVIDER_TYPES and protocol != old.get('type'):
        raise ConfigError('不支持的供应商协议')
    base = _string(item.get('base_url'), 'base_url')
    if base:
        try:
            parsed = urlsplit(base)
            valid = parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
            parsed.port
        except ValueError:
            valid = False
        if not valid or any(c.isspace() for c in base):
            raise ConfigError('base_url 必须是有效的 http(s) URL')
    action = item.get('key_action')
    if action not in ('keep', 'replace', 'clear'):
        raise ConfigError('key_action 必须是 keep/replace/clear')
    changes = {'type': protocol, 'base_url': base}
    if action == 'replace':
        if old.get('api_key_env'):
            raise ConfigError('供应商使用 api_key_env，请先手动解除环境变量密钥绑定再替换 API key')
        changes['api_key'] = _string(item.get('api_key'), 'api_key', True)
    elif action == 'clear':
        changes['api_key'] = None
    _patch_fields_data(data, ('providers', name), changes)


def upsert_provider(content, original_name, item):
    data = parse(content)
    _upsert_provider_data(data, original_name, item)
    return dumps(data)


def _set_default_model_data(data, alias):
    if not isinstance(alias, str) or alias not in data.get('models', {}):
        raise ConfigError('模型不存在')
    data['default_model'] = alias


def set_default_model(content, alias):
    data = parse(content)
    _set_default_model_data(data, alias)
    return dumps(data)


def snapshot(content, references=None):
    result = {'version': version(content), 'editable': True, 'message': '', 'providers': [],
              'models': [], 'default_model': '', 'provider_types': list(PROVIDER_TYPES),
              'effort_levels': list(EFFORT_LEVELS)}
    try:
        data = parse(content)
        dumps(data)
        providers, models = data.get('providers', {}), data.get('models', {})
        if not isinstance(providers, dict) or not isinstance(models, dict):
            raise ConfigError('providers/models 必须是表')
        result['default_model'] = _string(data.get('default_model', ''), 'default_model')
        protected = deletion_reasons(data, references)
        for name, provider in providers.items():
            if not isinstance(provider, dict):
                raise ConfigError('供应商必须是表')
            _string(provider.get('type', ''), 'type')
            _string(provider.get('base_url', ''), 'base_url')
            result['providers'].append({'name': name, 'type': provider.get('type', ''),
                                        'base_url': provider.get('base_url', ''),
                                        'has_api_key': bool(provider.get('api_key') or provider.get('api_key_env')),
                                        'managed': managed_provider(name, provider)})
        for alias, model in models.items():
            if not isinstance(model, dict):
                raise ConfigError('模型必须是表')
            for key in ('provider', 'model'):
                _string(model.get(key, ''), key)
            _string(effective(model, 'display_name') or alias, 'display_name')
            for key in ('capabilities', 'support_efforts'):
                _array(effective(model, key) or [], key)
            context = effective(model, 'max_context_size')
            if context is not None and type(context) is not int:
                raise ConfigError('max_context_size 必须是整数')
            default = effective(model, 'default_effort')
            if default is not None:
                _string(default, 'default_effort')
            result['models'].append({'alias': alias, 'provider': model.get('provider', ''),
                                     'model': model.get('model', ''),
                                     'display_name': effective(model, 'display_name') or alias,
                                     'max_context_size': effective(model, 'max_context_size') or 250000,
                                     'capabilities': effective(model, 'capabilities') or [],
                                     'support_efforts': effective(model, 'support_efforts') or [],
                                     'default_effort': effective(model, 'default_effort'),
                                     'managed': managed_model(model, providers),
                                     'delete_protected': alias in protected,
                                     'delete_reason': protected.get(alias, '')})
    except (ConfigError, TypeError, AttributeError):
        result.update(editable=False, message='配置无法安全解析或完整序列化，拒绝改写', providers=[], models=[],
                      default_model='')
    return result
