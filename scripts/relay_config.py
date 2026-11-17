# -*- coding: utf-8 -*-
"""单端口中继配置和本机设备凭据；账号原始密码只供生成登录二维码。"""
import http.client
import json
import os
import re
from urllib.parse import urlsplit

import mobile_auth

TOKEN_MAX = 256
DEFAULTS = {'url': '', 'token': '', 'username': '', 'password': '',
            'device_id': '', 'device_key': '', 'device_name': ''}


def config_path(kimi_home):
    return os.path.join(kimi_home, 'usage-dashboard', 'relay.json')


def normalize_url(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        return ''
    value = value.strip()
    if '://' not in value:
        value = 'http://' + value
    if any(ord(c) <= 32 or ord(c) > 126 for c in value):
        return ''
    try:
        u = urlsplit(value)
        if (u.scheme != 'http' or not u.hostname or u.username is not None or u.password is not None
                or u.path not in ('', '/') or u.query or u.fragment
                or not re.fullmatch(r'[A-Za-z0-9.\[\]:_-]+', u.netloc)):
            return ''
        port = u.port if u.port is not None else 80
        if not 1 <= port <= 65535:
            return ''
        host = u.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        return 'http://' + host + ((':' + str(port)) if port != 80 else '')
    except ValueError:
        return ''


def sanitize(data):
    out = dict(DEFAULTS)
    if not isinstance(data, dict):
        return out
    legacy = ''
    if 'url' not in data and isinstance(data.get('host'), str) and data['host']:
        legacy = 'http://%s:%s' % (data['host'], data.get('public_port', 47961))
    out['url'] = normalize_url(data.get('url', legacy))
    token = data.get('token', '')
    if isinstance(token, str) and len(token.strip()) <= TOKEN_MAX and all(33 <= ord(c) <= 126 for c in token.strip()):
        out['token'] = token.strip()
    for key, limit in (('username', 64), ('password', 128), ('device_name', 64)):
        value = data.get(key, '')
        if isinstance(value, str) and len(value) <= limit:
            try:
                value.encode('utf-8')
            except UnicodeError:
                continue
            out[key] = value
    for key in ('device_id', 'device_key'):
        value = data.get(key, '')
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{16,128}', value):
            out[key] = value
    return out


def load(kimi_home):
    return sanitize(mobile_auth._load(kimi_home, 'relay.json'))


def save(kimi_home, cfg):
    cfg = sanitize(cfg)
    if not mobile_auth._save(kimi_home, 'relay.json', cfg):
        raise ValueError('中继配置保存失败，请检查目录权限')
    return cfg


def bound(cfg):
    return bool(cfg['url'] and cfg['username'] and cfg['password'] and cfg['device_id'] and cfg['device_key'])


def redacted(cfg):
    cfg = sanitize(cfg)
    return {'url': cfg['url'], 'token_set': bool(cfg['token']),
            'username': cfg['username'], 'device_name': cfg['device_name'],
            'bound': bound(cfg)}


def public_origin(cfg):
    return sanitize(cfg)['url']


def public_host_header(cfg):
    return urlsplit(public_origin(cfg)).netloc


def request(cfg, path, body):
    u = urlsplit(cfg['url'])
    if not u.hostname:
        raise ValueError('请先保存服务器地址')
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30)
    try:
        conn.request('POST', path, json.dumps(body, ensure_ascii=False).encode('utf-8'),
                     {'Content-Type': 'application/json', 'Origin': cfg['url'],
                      'X-Relay-Token': cfg['token']})
        resp = conn.getresponse()
        raw = resp.read(16385)
        if len(raw) > 16384:
            raise ValueError('服务器返回内容过大')
        try:
            result = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise ValueError('服务器未提供新版账号接口，请同步升级中继服务器') from None
        if not isinstance(result, dict):
            raise ValueError('服务器返回格式错误')
        if resp.status != 200:
            errors = {'准入密钥错误', '账号已存在，请选择登录已有账号', '账号或密码错误',
                      '设备不存在', '设备未绑定或凭据已失效', '尝试过于频繁，请两分钟后重试'}
            error = result.get('error')
            raise ValueError(error if isinstance(error, str) and error in errors
                             else '注册或登录失败，请检查账号密码和服务器版本')
        return result
    except (OSError, http.client.HTTPException):
        raise ValueError('连接中继服务器失败，请检查地址、端口和网络') from None
    finally:
        conn.close()


def bind(kimi_home, username, password, device_name, register=False):
    cfg = load(kimi_home)
    if not cfg['url'] or not cfg['token']:
        raise ValueError('请先保存服务器地址和准入密钥')
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', username):
        raise ValueError('账号需为 1-64 位字母、数字或 _.@-')
    if not isinstance(password, str) or not 1 <= len(password) <= 128:
        raise ValueError('密码需为 1-128 个字符')
    if not isinstance(device_name, str) or not 1 <= len(device_name.strip()) <= 64:
        raise ValueError('电脑名称需为 1-64 个字符')
    if cfg['device_id'] and cfg['username'] != username:
        raise ValueError('请先解除原账号的本机绑定，再绑定其他账号')
    result = request(cfg, '/api/relay/bind', {
        'username': username, 'password': password, 'device_name': device_name.strip(),
        'register': register, 'device_id': cfg['device_id'], 'device_key': cfg['device_key']})
    if result.get('protocol') != '2':
        raise ValueError('中继协议不兼容，请同步升级服务器')
    nxt = sanitize(dict(cfg, username=username, password=password,
                        device_name=device_name.strip(), device_id=result.get('device_id'),
                        device_key=result.get('device_key')))
    if not bound(nxt):
        raise ValueError('服务器返回的设备凭据无效')
    try:
        save(kimi_home, nxt)
    except ValueError:
        raise ValueError('服务器已绑定，但本机凭据保存失败；请检查权限后从服务器网页解除此次绑定并重试') from None
    return redacted(nxt)


def unbind(kimi_home):
    cfg = load(kimi_home)
    if cfg['device_id'] and cfg['device_key']:
        try:
            request(cfg, '/api/relay/unbind-device', {'device_id': cfg['device_id'], 'device_key': cfg['device_key']})
        except ValueError as exc:
            if str(exc) != '设备未绑定或凭据已失效':
                raise
    cfg.update(username='', password='', device_id='', device_key='', device_name='')
    return redacted(save(kimi_home, cfg))
