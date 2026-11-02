# -*- coding: utf-8 -*-
"""MCP 服务器配置读写：全局 ~/.kimi-code/mcp.json 的严格校验、原子写入与版本冲突检测。

字段对齐 Kimi Code 原生 schema：
  stdio  -> command / args / env / cwd
  http   -> url / headers / auth / bearerTokenEnvVar
  sse    -> 同 http
  公共   -> enabled / deferred / startupTimeoutMs / toolTimeoutMs / enabledTools / disabledTools
缺省 transport 时按是否存在 command / url 推断，写回时一律显式写出。
"""
import hashlib
import json
import os
import re
import tempfile
from urllib.parse import urlsplit

TRANSPORTS = ('stdio', 'http', 'sse')
COMMON_FIELDS = {'enabled', 'deferred', 'startupTimeoutMs', 'toolTimeoutMs',
                 'enabledTools', 'disabledTools'}
STDIO_FIELDS = {'command', 'args', 'env', 'cwd'}
REMOTE_FIELDS = {'url', 'headers', 'auth', 'bearerTokenEnvVar'}
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
MAX_TIMEOUT = 2147483647


class StoreError(ValueError):
    pass


def config_path(kimi_home=None):
    home = (kimi_home or os.environ.get('KIMI_CODE_HOME') or
            os.environ.get('KIMI_HOME') or os.path.join(os.path.expanduser('~'), '.kimi-code'))
    return os.path.join(home, 'mcp.json')


def read_content(kimi_home=None, path=None):
    target = path or config_path(kimi_home)
    try:
        with open(target, 'rb') as source:
            return source.read().decode('utf-8')
    except FileNotFoundError:
        return ''
    except (OSError, UnicodeError):
        raise StoreError('无法读取 mcp.json')


def version(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def parse(content):
    if not content.strip():
        return {}, {}
    try:
        data = json.loads(content)
    except ValueError:
        raise StoreError('mcp.json 不是合法 JSON，拒绝改写') from None
    if not isinstance(data, dict):
        raise StoreError('mcp.json 根节点必须是对象，拒绝改写')
    servers = data.get('mcpServers', {})
    if not isinstance(servers, dict):
        raise StoreError('mcp.json 的 mcpServers 必须是对象，拒绝改写')
    return data, servers


def dumps(data, servers):
    merged = dict(data)
    merged['mcpServers'] = servers
    text = json.dumps(merged, ensure_ascii=False, indent=2) + '\n'
    try:
        back = json.loads(text)
    except ValueError:
        raise StoreError('序列化结果无法解析，配置未改动') from None
    if back.get('mcpServers') != servers:
        raise StoreError('序列化前后内容不一致，配置未改动')
    return text


def _text(value, label, limit, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise StoreError(label + '不是有效字符串')
    if not allow_empty and not value.strip():
        raise StoreError(label + '不能为空')
    return value


def _string_list(value, label, limit=64, item_limit=2048):
    if not isinstance(value, list) or len(value) > limit:
        raise StoreError(label + '必须是数组')
    out = []
    for item in value:
        out.append(_text(item, label, item_limit))
    return out


def _mapping(value, label, max_items=64):
    if not isinstance(value, dict) or len(value) > max_items:
        raise StoreError(label + '必须是对象')
    out = {}
    for key, item in value.items():
        _text(key, label + '的键', 128)
        out[key] = _text(item, label + '的值', 4096, allow_empty=True)
    return out


def _timeout(value, label):
    if type(value) is not int or not 1 <= value <= MAX_TIMEOUT:
        raise StoreError(label + '必须是 1~%d 的整数' % MAX_TIMEOUT)
    return value


def infer_transport(server):
    transport = server.get('transport')
    if transport is None:
        if isinstance(server.get('command'), str):
            return 'stdio'
        if isinstance(server.get('url'), str):
            return 'http'
        return None
    return transport


def validate_server(item):
    """校验并归一化单个服务器配置，返回只含允许字段的副本。"""
    if not isinstance(item, dict):
        raise StoreError('服务器配置必须是对象')
    if set(item) - (COMMON_FIELDS | STDIO_FIELDS | REMOTE_FIELDS | {'transport'}):
        raise StoreError('服务器配置含不支持的字段')
    transport = infer_transport(item)
    if transport not in TRANSPORTS:
        raise StoreError('transport 必须是 stdio / http / sse，或提供 command / url')
    allowed = COMMON_FIELDS | (STDIO_FIELDS if transport == 'stdio' else REMOTE_FIELDS) | {'transport'}
    extra = set(item) - allowed
    if extra:
        raise StoreError('%s 类型不支持字段：%s' % (transport, ', '.join(sorted(extra))))
    out = {'transport': transport}
    if transport == 'stdio':
        out['command'] = _text(item.get('command'), 'command', 1024)
        if 'args' in item:
            out['args'] = _string_list(item['args'], 'args', 64, 2048)
        if 'env' in item:
            out['env'] = _mapping(item['env'], 'env')
        if 'cwd' in item:
            out['cwd'] = _text(item['cwd'], 'cwd', 1024)
    else:
        url = item.get('url')
        _text(url, 'url', 2048)
        try:
            parsed = urlsplit(url)
            parsed.port
            valid = parsed.scheme in ('http', 'https') and bool(parsed.hostname)
        except ValueError:
            valid = False
        if not valid or any(ch.isspace() for ch in url):
            raise StoreError('url 必须是有效的 http(s) 地址')
        out['url'] = url
        if 'headers' in item:
            out['headers'] = _mapping(item['headers'], 'headers')
        if 'auth' in item:
            if item['auth'] != 'oauth':
                raise StoreError('auth 目前只支持 oauth')
            out['auth'] = 'oauth'
        if 'bearerTokenEnvVar' in item:
            out['bearerTokenEnvVar'] = _text(item['bearerTokenEnvVar'], 'bearerTokenEnvVar', 256)
    if 'enabled' in item:
        if type(item['enabled']) is not bool:
            raise StoreError('enabled 必须是布尔值')
        out['enabled'] = item['enabled']
    if 'deferred' in item:
        if type(item['deferred']) is not bool:
            raise StoreError('deferred 必须是布尔值')
        out['deferred'] = item['deferred']
    for key in ('startupTimeoutMs', 'toolTimeoutMs'):
        if key in item:
            out[key] = _timeout(item[key], key)
    for key in ('enabledTools', 'disabledTools'):
        if key in item:
            out[key] = _string_list(item[key], key, 200, 256)
    return out


def _normalize_name(value):
    name = _text(value, '服务器名称', 64).strip()
    if not NAME_RE.match(name):
        raise StoreError('服务器名称只能包含字母、数字、点、下划线和连字符')
    return name


def _merge_unknown(old, new):
    """保留我们没有建模的既有字段，避免手写配置被静默丢弃。"""
    if not isinstance(old, dict):
        return new
    merged = dict(new)
    for key, value in old.items():
        merged.setdefault(key, value)
    return merged


def upsert_server(content, original_name, item):
    data, servers = parse(content)
    if original_name is not None:
        _text(original_name, 'original_name', 64)
    name = _normalize_name(item.get('name'))
    if original_name is not None and original_name != name:
        raise StoreError('服务器名称不可重命名')
    if original_name is not None and original_name not in servers:
        raise StoreError('原服务器不存在')
    if original_name is None and name in servers:
        raise StoreError('服务器名称已存在')
    body = dict(item)
    body.pop('name', None)
    validated = validate_server(body)
    servers[name] = _merge_unknown(servers.get(name), validated)
    return dumps(data, servers)


def delete_server(content, name):
    data, servers = parse(content)
    name = _normalize_name(name)
    if name not in servers:
        raise StoreError('服务器不存在')
    remaining = {key: value for key, value in servers.items() if key != name}
    return dumps(data, remaining)


def set_enabled(content, name, enabled):
    data, servers = parse(content)
    name = _normalize_name(name)
    if name not in servers:
        raise StoreError('服务器不存在')
    if type(enabled) is not bool:
        raise StoreError('enabled 必须是布尔值')
    current = servers[name]
    if not isinstance(current, dict):
        raise StoreError('该服务器配置不是对象，拒绝改写')
    updated = dict(current)
    updated['enabled'] = enabled
    servers[name] = updated
    return dumps(data, servers)


def secret_keys(value):
    """列出配置中出现的敏感字段名（env / headers 的键），值永不外发。"""
    if not isinstance(value, dict):
        return [], []
    env = value.get('env')
    headers = value.get('headers')
    return (sorted(env) if isinstance(env, dict) else [],
            sorted(headers) if isinstance(headers, dict) else [])


def snapshot(content):
    result = {'version': version(content), 'editable': True, 'message': '',
              'servers': [], 'transports': list(TRANSPORTS)}
    try:
        data, servers = parse(content)
        dumps(data, servers)
        for name, item in servers.items():
            if not isinstance(item, dict):
                raise StoreError('服务器 %s 不是对象' % name)
            env_keys, header_keys = secret_keys(item)
            transport = infer_transport(item) or ''
            row = {'name': name, 'transport': transport,
                   'enabled': item.get('enabled') is not False,
                   'deferred': item.get('deferred') is True,
                   'enabled_tools': list(item.get('enabledTools') or []),
                   'disabled_tools': list(item.get('disabledTools') or []),
                   'env_keys': env_keys, 'header_keys': header_keys,
                   'auth': item.get('auth') or '',
                   'valid': True, 'error': ''}
            for key in ('startupTimeoutMs', 'toolTimeoutMs'):
                row[key[0].lower() + key[1:]] = item.get(key)
            if transport == 'stdio':
                row['command'] = item.get('command') or ''
                row['args'] = list(item.get('args') or [])
                row['cwd'] = item.get('cwd') or ''
            else:
                row['url'] = item.get('url') or ''
                row['bearer_token_env'] = item.get('bearerTokenEnvVar') or ''
            if transport not in TRANSPORTS:
                row.update(valid=False, error='缺少 command 或 url，Kimi 无法识别该服务器')
            result['servers'].append(row)
        result['servers'].sort(key=lambda row: (not row['enabled'], row['name'].lower()))
    except StoreError as error:
        result.update(editable=False, message=str(error), servers=[])
    except Exception:
        result.update(editable=False, message='mcp.json 无法安全解析，拒绝改写', servers=[])
    return result


def write_atomic(content, path=None):
    """原子替换 mcp.json，沿用 config.toml 的受保护 DACL 与 fsync 语义。"""
    target = path or config_path()
    directory = os.path.dirname(target) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp = None
    try:
        from mobile_security import _set_protected_dacl
        fd, tmp = tempfile.mkstemp(prefix='.mcp-write-', suffix='.json', dir=directory)
        with os.fdopen(fd, 'wb') as handle:
            _set_protected_dacl(tmp)
            handle.write(content.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        tmp = None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def apply(content, base_content, path=None):
    if content is None:
        raise StoreError('配置未生成')
    if read_content(path=path) != base_content:
        raise StoreError('mcp.json 已被其他操作更改，请刷新后重试')
    write_atomic(content, path)
