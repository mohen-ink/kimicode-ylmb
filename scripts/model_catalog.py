# -*- coding: utf-8 -*-
import copy
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import model_manager

MAX_PAGES = 10
MAX_MODELS = 5000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT = 10
TOTAL_TIMEOUT = 45
_DEFAULT_BASES = {
    'openai': 'https://api.openai.com/v1',
    'openai_responses': 'https://api.openai.com/v1',
    'anthropic': 'https://api.anthropic.com/v1',
    'google-genai': 'https://generativelanguage.googleapis.com/v1beta',
}
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_BLOCKED_HEADERS = frozenset({
    'host', 'content-length', 'transfer-encoding', 'connection', 'keep-alive',
    'te', 'trailer', 'upgrade', 'expect', 'accept-encoding',
})
_METADATA_HOSTS = frozenset({
    'metadata', 'metadata.google.internal', 'metadata.goog',
    'metadata.azure.internal', 'instance-data', 'instance-data.ec2.internal',
})
_METADATA_IPS = frozenset({'168.63.129.16', '100.100.100.200', 'fd00:ec2::254'})


class CatalogError(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CatalogError('上游返回重定向，出于密钥安全未跟随；请检查供应商 base_url')


def provider_snapshot(data, name):
    model_manager._string(name, 'provider', True)
    providers = data.get('providers', {})
    if not isinstance(providers, dict) or name not in providers:
        raise model_manager.ConfigError('provider 不存在')
    provider = providers[name]
    if not isinstance(provider, dict):
        raise model_manager.ConfigError('供应商配置必须是表')
    return copy.deepcopy(provider)


def _text(value, label, maximum=4096, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or
            (not empty and not value.strip()) or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise CatalogError(label + ' 格式无效')
    return value


def _blocked_address(address):
    try:
        ip = ipaddress.ip_address(address.split('%', 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, 'ipv4_mapped', None)
    if mapped is not None:
        ip = mapped
    return (str(ip) in _METADATA_IPS or ip.is_link_local or
            ip.is_unspecified or ip.is_multicast)


def _endpoint(protocol, provider):
    base = provider.get('base_url') or _DEFAULT_BASES.get(protocol, '')
    if not base:
        raise CatalogError('kimi 协议探测需要显式配置 base_url；仍可手动添加模型')
    _text(base, 'base_url', 4096)
    try:
        parsed = urlsplit(base)
        host = parsed.hostname
        port = parsed.port
        if (parsed.scheme not in ('http', 'https') or not host or
                parsed.username is not None or parsed.password is not None or
                '?' in base or '#' in base or '\\' in base or
                any(char.isspace() for char in base)):
            raise ValueError()
        base.encode('ascii')
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise CatalogError('base_url 必须是无用户信息、查询参数和片段的 HTTP(S) 地址') from None
    normalized_host = host.lower().rstrip('.')
    if (normalized_host in _METADATA_HOSTS or
            normalized_host.endswith('.metadata.google.internal') or
            _blocked_address(normalized_host)):
        raise CatalogError('拒绝向云元数据或特殊网络目标发送密钥')
    try:
        addresses = socket.getaddrinfo(host, port or (443 if parsed.scheme == 'https' else 80),
                                       type=socket.SOCK_STREAM)
    except OSError:
        raise CatalogError('无法解析供应商地址，请检查 base_url') from None
    if not addresses or any(_blocked_address(item[4][0]) for item in addresses):
        raise CatalogError('拒绝向云元数据或特殊网络目标发送密钥')
    path = parsed.path.rstrip('/')
    if not path.endswith('/models'):
        path += '/models'
    return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))


def _headers(protocol, provider):
    extra = provider.get('extra_headers', {})
    if not isinstance(extra, dict):
        raise CatalogError('extra_headers 必须是字符串表')
    headers, seen, secrets = {}, set(), []
    for name, value in extra.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise CatalogError('extra_headers 含不合法的头名称')
        lower = name.lower()
        if lower in seen or lower in _BLOCKED_HEADERS or lower.startswith('proxy-'):
            raise CatalogError('extra_headers 含重复或传输控制头')
        _text(value, 'extra_headers', 8192, empty=True)
        try:
            value.encode('latin-1')
        except UnicodeError:
            raise CatalogError('extra_headers 含无法发送的头值') from None
        seen.add(lower)
        headers[lower] = value
        if value and any(part in lower for part in ('authorization', 'key', 'token', 'secret', 'cookie')):
            secrets.append(value)
            if lower == 'authorization' and ' ' in value:
                secrets.append(value.split(' ', 1)[1])
    env_name = provider.get('api_key_env')
    key = provider.get('api_key') or ''
    if env_name is not None and env_name != '':
        _text(env_name, 'api_key_env', 256)
        key = os.environ.get(env_name, '')
        if not key:
            raise CatalogError('服务进程未设置供应商 api_key_env；请检查环境或使用手动添加')
    _text(key, 'API key', 8192, empty=True)
    if key:
        try:
            key.encode('latin-1')
        except UnicodeError:
            raise CatalogError('API key 格式无效') from None
        secrets.append(key)
        if protocol == 'anthropic':
            headers['x-api-key'] = key
        elif protocol == 'google-genai':
            headers['x-goog-api-key'] = key
        else:
            headers['authorization'] = 'Bearer ' + key
    if protocol == 'anthropic':
        headers.setdefault('anthropic-version', '2023-06-01')
    headers['accept'] = 'application/json'
    headers['accept-encoding'] = 'identity'
    return headers, secrets


def _page(opener, endpoint, params, headers, remaining):
    url = endpoint + ('?' + urlencode(params) if params else '')
    try:
        request = urllib.request.Request(url, headers=headers, method='GET')
        with opener.open(request, timeout=max(0.1, min(REQUEST_TIMEOUT, remaining))) as response:
            if response.status != 200:
                raise CatalogError('上游未返回可用模型列表')
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                raise CatalogError('上游响应超过单页大小限制，未返回模型；仍可手动添加')
            if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                raise CatalogError('上游未遵守未压缩响应要求，拒绝解析模型列表')
            chunks, size = [], 0
            deadline = time.monotonic() + remaining
            while size <= MAX_RESPONSE_BYTES:
                if time.monotonic() >= deadline:
                    raise CatalogError('模型探测达到总超时限制；请稍后再试或手动添加')
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b''.join(chunks)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CatalogError('上游响应超过单页大小限制，未返回模型；仍可手动添加')
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise CatalogError('上游模型列表不是有效 JSON') from None
        if not isinstance(payload, (list, dict)):
            raise CatalogError('上游模型列表格式不受支持')
        return payload, len(raw)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if status in (401, 403):
            message = '上游鉴权失败，请检查供应商密钥和权限；仍可手动添加模型'
        elif status == 404:
            message = '上游未提供 /models 接口，请检查 base_url 或手动添加模型'
        elif status == 429:
            message = '上游请求过于频繁，请稍后再试'
        elif 300 <= status < 400:
            message = '上游返回重定向，出于密钥安全未跟随'
        else:
            message = '上游模型探测失败（HTTP %d），仍可手动添加模型' % status
        raise CatalogError(message) from None
    except CatalogError:
        raise
    except Exception:
        raise CatalogError('无法连接上游或请求超时，请检查供应商配置；仍可手动添加模型') from None


def _entries(payload):
    if isinstance(payload, list):
        return payload
    for key in ('data', 'models'):
        if key in payload:
            if not isinstance(payload[key], list):
                raise CatalogError('上游模型列表格式不受支持')
            return payload[key]
    raise CatalogError('上游未返回 data/models 模型数组')


def _model(entry, protocol, secrets):
    if isinstance(entry, str):
        model_id, display = entry, entry
    elif isinstance(entry, dict):
        model_id = entry.get('id') or entry.get('name')
        display = entry.get('display_name') or entry.get('displayName') or model_id
    else:
        raise CatalogError('上游模型条目格式不受支持')
    _text(model_id, '上游模型 ID', 1024)
    _text(display, '上游模型显示名', 2048)
    if any(secret in model_id or secret in display for secret in secrets):
        raise CatalogError('上游返回的模型条目包含鉴权信息，拒绝返回')
    if protocol == 'google-genai' and model_id.startswith('models/'):
        model_id = model_id[len('models/'):]
        _text(model_id, '上游模型 ID', 1024)
    return {'id': model_id, 'display_name': display}


def _cursor(value):
    return _text(value, '上游分页标记', 4096)


def _next_params(payload, entries, protocol, endpoint):
    if not isinstance(payload, dict):
        return None
    if protocol == 'google-genai':
        token = payload.get('nextPageToken')
        return {'pageSize': 1000, 'pageToken': _cursor(token)} if token else None
    allowed = ({'limit', 'after_id', 'before_id'} if protocol == 'anthropic' else
               {'limit', 'after', 'before', 'offset', 'page', 'cursor', 'page_size', 'page_token'})
    links = payload.get('links')
    next_url = payload.get('next_page_url') or payload.get('next')
    if not next_url and isinstance(links, dict):
        next_url = links.get('next')
    if next_url:
        _text(next_url, '上游分页地址', 8192)
        try:
            target = urlsplit(urljoin(endpoint, next_url))
            original = urlsplit(endpoint)
            if (target.scheme != original.scheme or target.hostname != original.hostname or
                    target.port != original.port or target.path != original.path or
                    target.username is not None or target.password is not None or
                    target.fragment or '#' in next_url or '\\' in next_url):
                raise ValueError()
            pairs = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True,
                              max_num_fields=16)
            if not pairs or len({key for key, value in pairs}) != len(pairs):
                raise ValueError()
            params = dict(pairs)
            if set(params) - allowed:
                raise ValueError()
            for value in params.values():
                _cursor(value)
            return params
        except (ValueError, UnicodeError):
            raise CatalogError('上游分页地址越界或无效，未向其它地址发送密钥') from None
    more = payload.get('has_more', payload.get('hasMore', False))
    if type(more) is not bool:
        raise CatalogError('上游分页状态格式无效')
    cursor = payload.get('next_cursor') or payload.get('nextCursor')
    if cursor:
        return {'limit': 100, 'after_id' if protocol == 'anthropic' else 'cursor': _cursor(cursor)}
    if more:
        last = payload.get('last_id')
        if not last and entries:
            entry = entries[-1]
            last = entry.get('id') if isinstance(entry, dict) else entry
        if not last:
            raise CatalogError('上游分页缺少下一页标记')
        return {'limit': 100, 'after_id' if protocol == 'anthropic' else 'after': _cursor(last)}
    return None


def fetch_models(name, provider):
    protocol = provider.get('type')
    if model_manager.managed_provider(name, provider) or protocol == 'vertexai':
        raise CatalogError('托管/OAuth 或 Vertex 认证暂不支持安全探测；请使用手动添加')
    if protocol not in ('openai', 'openai_responses', 'kimi', 'anthropic', 'google-genai'):
        raise CatalogError('该供应商协议暂不支持探测；请使用手动添加')
    started = time.monotonic()
    headers, secrets = _headers(protocol, provider)
    endpoint = _endpoint(protocol, provider)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    params = ({'pageSize': 1000} if protocol == 'google-genai' else
              {'limit': 100} if protocol == 'anthropic' else {})
    models, seen_pages, total_bytes, limited = {}, set(), 0, ''
    for page_number in range(MAX_PAGES):
        remaining = TOTAL_TIMEOUT - (time.monotonic() - started)
        if remaining <= 0:
            if not models:
                raise CatalogError('模型探测达到总超时限制；请稍后再试或手动添加')
            limited = '总超时限制'
            break
        marker = tuple(sorted(params.items()))
        if marker in seen_pages:
            raise CatalogError('上游分页标记重复，停止探测；仍可手动添加')
        seen_pages.add(marker)
        payload, size = _page(opener, endpoint, params, headers, remaining)
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            limited = '总响应大小上限（8 MiB）'
            break
        entries = _entries(payload)
        for entry in entries:
            item = _model(entry, protocol, secrets)
            if item['id'] not in models and len(models) >= MAX_MODELS:
                limited = '模型数量上限（5000）'
                break
            models.setdefault(item['id'], item)
        if limited:
            break
        params = _next_params(payload, entries, protocol, endpoint)
        if params is None:
            break
        if page_number + 1 >= MAX_PAGES:
            limited = '分页上限（10 页）'
    result = sorted(models.values(), key=lambda item: item['id'])
    if limited:
        message = '已获取 %d 个模型，达到%s；列表可能不完整，可手动添加遗漏模型' % (len(result), limited)
    elif result:
        message = '已探测到 %d 个模型；列表不验证上游实际能力' % len(result)
    else:
        message = '上游返回空模型列表；可使用手动添加'
    return result, message
