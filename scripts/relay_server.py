# -*- coding: utf-8 -*-
"""私人 HTTP/WS 中继：单端口账号登录、设备绑定及请求转发（Python>=3.8）。"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import struct
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

LISTEN_HOST = '0.0.0.0'
PUBLIC_PORT = 47961
MAX_HTTP_BODY = 32 * 1024 * 1024
MAX_WS_MESSAGE = 4 * 1024 * 1024
TUNNEL_MAX_FRAME = 192 * 1024 * 1024
HEADER_MAX = 32 * 1024
HTTP_TIMEOUT = 60.0
SESSION_AGE = 604800
SESSION_COOKIE = 'kimi_relay_session'
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
PBKDF_ROUNDS = 600000
PROTOCOL = '2'
TUNNELS = {}
STORE = None
ADMISSION_TOKEN = ''
FAILURES = {}
log = logging.getLogger('relay_server')


def _tok_ok(value, lo=16, hi=128):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{%d,%d}' % (lo, hi), value) is not None


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), PBKDF_ROUNDS).hex()


class AccountStore:
    def __init__(self, path):
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
                name TEXT NOT NULL, secret_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                id_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
                version INTEGER NOT NULL, expires REAL NOT NULL);
        ''')
        self.db.commit()

    async def authenticate(self, username, password, create=False):
        if (not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', username)
                or not isinstance(password, str) or not 1 <= len(password) <= 128):
            raise ValueError('账号或密码格式无效')
        row = self.db.execute('SELECT * FROM accounts WHERE username=?', (username,)).fetchone()
        salt = row['salt'] if row else secrets.token_hex(16)
        value = await asyncio.get_running_loop().run_in_executor(None, _password_hash, password, salt)
        if create:
            if row:
                raise ValueError('账号已存在，请选择登录已有账号')
            try:
                with self.db:
                    self.db.execute('INSERT INTO accounts(username,salt,password_hash) VALUES(?,?,?)',
                                    (username, salt, value))
            except sqlite3.IntegrityError:
                raise ValueError('账号已存在，请选择登录已有账号') from None
            return self.db.execute('SELECT * FROM accounts WHERE username=?', (username,)).fetchone()
        if row is None or not hmac.compare_digest(row['password_hash'], value):
            raise ValueError('账号或密码错误')
        current = self.db.execute('SELECT * FROM accounts WHERE id=?', (row['id'],)).fetchone()
        if current['version'] != row['version']:
            raise ValueError('密码已修改，请重新登录')
        return current

    def session(self, sid):
        if not _tok_ok(sid):
            return None
        return self.db.execute('''SELECT a.*,s.expires FROM sessions s JOIN accounts a
            ON a.id=s.account_id WHERE s.id_hash=? AND s.expires>? AND s.version=a.version''',
                               (_digest(sid), time.time())).fetchone()

    def new_session(self, account):
        sid = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute('DELETE FROM sessions WHERE expires<=?', (time.time(),))
            self.db.execute('''DELETE FROM sessions WHERE id_hash IN
                (SELECT id_hash FROM sessions WHERE account_id=? ORDER BY expires DESC LIMIT -1 OFFSET 7)''',
                            (account['id'],))
            self.db.execute('INSERT INTO sessions VALUES(?,?,?,?)',
                            (_digest(sid), account['id'], account['version'], time.time() + SESSION_AGE))
        return sid

    def device(self, tid, account_id=None):
        row = self.db.execute('SELECT * FROM devices WHERE id=?', (tid,)).fetchone()
        return row if row and (account_id is None or row['account_id'] == account_id) else None


def _cookie(headers, name):
    values = [part.strip().partition('=')[2] for part in headers.get('cookie', '').split(';')
              if part.strip().partition('=')[0] == name]
    return values[0] if len(values) == 1 else ''


def _rate_ok(ip, username):
    now = time.time()
    for key in [k for k, v in FAILURES.items() if v[1] <= now]:
        FAILURES.pop(key, None)
    keys = ('ip:' + ip, 'user:' + str(username)[:64])
    return all(FAILURES.get(key, (0, 0))[0] < 10 for key in keys)


def _rate_fail(ip, username):
    now = time.time()
    for key in ('ip:' + ip, 'user:' + str(username)[:64]):
        count, expires = FAILURES.get(key, (0, now + 120))
        FAILURES[key] = (count + 1, expires)
    while len(FAILURES) > 2048:
        FAILURES.pop(next(iter(FAILURES)))


def ws_frame(opcode, payload=b'', fin=True):
    size = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    if size < 126:
        return head + bytes([size]) + payload
    if size < 65536:
        return head + b'\x7e' + struct.pack('>H', size) + payload
    return head + b'\x7f' + struct.pack('>Q', size) + payload


async def ws_read_frame(reader, max_len=MAX_WS_MESSAGE):
    try:
        head = await asyncio.wait_for(reader.readexactly(2), 90)
        fin, opcode = bool(head[0] & 128), head[0] & 15
        size = head[1] & 127
        if head[0] & 112 or not head[1] & 128 or opcode not in (0, 1, 2, 8, 9, 10):
            return None
        if opcode >= 8 and (not fin or size > 125):
            return None
        if size == 126:
            size = struct.unpack('>H', await asyncio.wait_for(reader.readexactly(2), 30))[0]
            if size < 126:
                return None
        elif size == 127:
            size = struct.unpack('>Q', await asyncio.wait_for(reader.readexactly(8), 30))[0]
            if size < 65536:
                return None
        if size > max_len:
            return None
        mask = await asyncio.wait_for(reader.readexactly(4), 30)
        payload = await asyncio.wait_for(reader.readexactly(size), 120)
        repeated = (mask * (size // 4 + 1))[:size]
        payload = (int.from_bytes(payload, 'big') ^ int.from_bytes(repeated, 'big')).to_bytes(size, 'big')
        return fin, opcode, payload
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None


async def read_request(reader):
    raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 30)
    if len(raw) > HEADER_MAX:
        raise ValueError('请求头过大')
    lines = raw[:-4].decode('latin-1').split('\r\n')
    first = lines[0].split(' ')
    if len(first) != 3 or first[2] != 'HTTP/1.1' or not first[1].startswith('/') or first[1].startswith('//'):
        raise ValueError('请求格式错误')
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(':')
        key = key.lower()
        value = value.strip()
        if not sep or not re.fullmatch(r'[a-z0-9!#$%&\'*+.^_`|~-]+', key) or any(ord(c) < 32 for c in value):
            raise ValueError('请求头格式错误')
        if key in headers:
            raise ValueError('重复请求头')
        headers[key] = value
    if 'transfer-encoding' in headers or not re.fullmatch(r'\d{1,10}', headers.get('content-length', '0')):
        raise ValueError('请求体格式错误')
    size = int(headers.get('content-length', '0'))
    limit = 8192 if first[1].startswith('/api/relay/') else MAX_HTTP_BODY
    if size > limit:
        raise ValueError('请求体过大')
    body = await asyncio.wait_for(reader.readexactly(size), 120) if size else b''
    return first[0], first[1], headers, body


async def response(writer, code, body, content_type='application/json; charset=utf-8', headers=()):
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False).encode('utf-8')
    elif isinstance(body, str):
        body = body.encode('utf-8')
    head = ('HTTP/1.1 %d\r\nContent-Type: %s\r\nContent-Length: %d\r\n'
            'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n'
            'Referrer-Policy: no-referrer\r\nConnection: close\r\n' % (code, content_type, len(body)))
    for key, value in headers:
        if '\r' in value or '\n' in value:
            continue
        head += '%s: %s\r\n' % (key, value)
    writer.write(head.encode('latin-1') + b'\r\n' + body)
    await writer.drain()
    writer.close()


async def redirect(writer, target):
    await response(writer, 302, b'', headers=(('Location', target),))


def _upgrade(headers):
    return headers.get('upgrade', '').lower() == 'websocket' and 'upgrade' in {
        x.strip().lower() for x in headers.get('connection', '').split(',')}


async def handshake(writer, headers):
    key = headers.get('sec-websocket-key', '')
    try:
        valid = len(base64.b64decode(key, validate=True)) == 16
    except ValueError:
        valid = False
    if not _upgrade(headers) or headers.get('sec-websocket-version') != '13' or not valid:
        raise ValueError('WebSocket 握手无效')
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode('ascii')).digest()).decode('ascii')
    out = 'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n' % accept
    proto = next((p.strip() for p in headers.get('sec-websocket-protocol', '').split(',')
                  if re.fullmatch(r'kimi-code\.bearer\.[A-Za-z0-9_.~-]+', p.strip())), '')
    if proto:
        out += 'Sec-WebSocket-Protocol: %s\r\n' % proto
    writer.write((out + '\r\n').encode('ascii'))
    await writer.drain()


class Tunnel:
    def __init__(self, device, reader, writer, origin):
        self.id = device['id']
        self.account_id = device['account_id']
        self.reader = reader
        self.writer = writer
        self.origin = origin
        self.bridge_key = secrets.token_urlsafe(32)
        self.pending = {}
        self.channels = {}
        self.lock = asyncio.Lock()
        self.alive = True

    async def send(self, msg):
        async with self.lock:
            if not self.alive:
                return False
            try:
                self.writer.write(ws_frame(1, json.dumps(msg, ensure_ascii=False).encode('utf-8')))
                await asyncio.wait_for(self.writer.drain(), HTTP_TIMEOUT)
                return True
            except (OSError, asyncio.TimeoutError):
                return False

    async def close(self):
        self.alive = False
        self.writer.close()
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.cancel()
        for channel in list(self.channels.values()):
            if not channel['ready'].done():
                channel['ready'].set_result(False)
            channel['active'].set()
            channel['writer'].close()


async def relay_register(reader, writer, headers, origin):
    if headers.get('x-relay-protocol') != PROTOCOL:
        await response(writer, 409, {'error': '中继协议不兼容，请同时升级服务器与本机插件'})
        return
    tid = headers.get('x-relay-device', '')
    device = STORE.device(tid) if _tok_ok(tid) else None
    secret = headers.get('x-relay-device-key', '')
    if not device or not _tok_ok(secret) or not hmac.compare_digest(device['secret_hash'], _digest(secret)):
        await response(writer, 403, {'error': '设备未绑定或凭据已失效'})
        return
    if tid in TUNNELS:
        await response(writer, 409, {'error': '此设备已有在线连接'}, headers=(('X-Relay-Protocol', PROTOCOL),))
        return
    tun = Tunnel(device, reader, writer, origin)
    TUNNELS[tid] = tun
    try:
        await handshake(writer, headers)
        if not await tun.send({'type': 'registered', 'protocol': PROTOCOL, 'tunnel_id': tid,
                               'origin': origin, 'bridge_key': tun.bridge_key}):
            return
        log.info('device connected: %s', tid)
        while tun.alive:
            frame = await ws_read_frame(reader, TUNNEL_MAX_FRAME)
            if frame is None:
                break
            fin, opcode, payload = frame
            if opcode == 8:
                break
            if opcode == 9:
                async with tun.lock:
                    writer.write(ws_frame(10, payload))
                    await writer.drain()
                continue
            if opcode == 10:
                continue
            if opcode != 1 or not fin:
                break
            msg = json.loads(payload.decode('utf-8'))
            if not isinstance(msg, dict):
                break
            if msg.get('type') == 'http_resp':
                fut = tun.pending.get(msg.get('id'))
                if fut and not fut.done():
                    fut.set_result(msg)
            elif msg.get('type') in ('ws_ready', 'ws_data', 'ws_close'):
                channel = tun.channels.get(msg.get('id'))
                if not channel:
                    continue
                if msg['type'] == 'ws_ready':
                    if not channel['ready'].done():
                        channel['ready'].set_result(True)
                elif msg['type'] == 'ws_close':
                    if not channel['ready'].done():
                        channel['ready'].set_result(False)
                    else:
                        channel['writer'].close()
                else:
                    await asyncio.wait_for(channel['active'].wait(), 25)
                    if not STORE.session(channel['sid']) or not STORE.device(tid, tun.account_id):
                        channel['writer'].close()
                        continue
                    opcode = int(msg.get('opcode', 1))
                    if opcode not in (0, 1, 2, 8, 9, 10):
                        break
                    channel['writer'].write(ws_frame(opcode, base64.b64decode(msg.get('payload', ''), validate=True),
                                                    msg.get('fin', True)))
                    await channel['writer'].drain()
    finally:
        if TUNNELS.get(tid) is tun:
            TUNNELS.pop(tid, None)
        await tun.close()
        log.info('device disconnected: %s', tid)


def _forward_headers(headers, tun, sid, account):
    out = {k: v for k, v in headers.items() if k not in (
        'host', 'connection', 'content-length', 'cookie', 'authorization', 'upgrade',
        'sec-websocket-key', 'sec-websocket-version', 'sec-websocket-extensions')
        and not k.startswith(('x-relay-', 'x-forwarded-', 'cf-'))}
    ticket = '%s.%d' % (sid, int(account['expires']))
    signature = hmac.new(tun.bridge_key.encode('ascii'), ticket.encode('ascii'), hashlib.sha256).hexdigest()
    out.update({'X-Relay-Authorization': ticket + '.' + signature, 'Accept-Encoding': 'identity'})
    return out


async def proxy_ws(tun, reader, writer, headers, inner, sid, account):
    cid = secrets.token_urlsafe(16)
    ready = asyncio.get_running_loop().create_future()
    channel = {'writer': writer, 'sid': sid, 'ready': ready, 'active': asyncio.Event()}
    tun.channels[cid] = channel
    watcher = None

    async def watch():
        while tun.alive and not writer.is_closing():
            await asyncio.sleep(1)
            if not STORE.session(sid) or not STORE.device(tun.id, account['id']):
                writer.close()
                return

    try:
        if not await tun.send({'type': 'ws_open', 'id': cid, 'path': inner,
                               'headers': _forward_headers(headers, tun, sid, account)}):
            await response(writer, 503, {'error': '设备连接已断开'})
            return
        try:
            accepted = await asyncio.wait_for(ready, 25)
        except asyncio.TimeoutError:
            accepted = False
        if not accepted or not STORE.session(sid) or not STORE.device(tun.id, account['id']):
            await response(writer, 502, {'error': '本机未接受 WebSocket 连接'})
            return
        await handshake(writer, headers)
        channel['active'].set()
        watcher = asyncio.create_task(watch())
        while tun.alive and STORE.session(sid) and not writer.is_closing():
            frame = await ws_read_frame(reader)
            if frame is None:
                break
            fin, opcode, payload = frame
            if not STORE.session(sid) or not STORE.device(tun.id, account['id']):
                break
            if opcode == 8:
                break
            if opcode == 9:
                writer.write(ws_frame(10, payload))
                await writer.drain()
                continue
            if opcode == 10:
                continue
            if not await tun.send({'type': 'ws_data', 'id': cid, 'opcode': opcode,
                                   'fin': fin, 'payload': base64.b64encode(payload).decode('ascii')}):
                break
    finally:
        if watcher is not None:
            watcher.cancel()
        channel['active'].set()
        tun.channels.pop(cid, None)
        writer.close()
        await tun.send({'type': 'ws_close', 'id': cid})


def _rewrite_content(body, headers, tid, inner):
    kind = next((str(v).lower() for k, v in headers if k.lower() == 'content-type'), '')
    prefix = '/t/' + tid
    if 'text/html' in kind and not inner.startswith(('/api/', '/assets/')):
        text = body.decode('utf-8')
        text = re.sub(r'((?:src|href)=[\"\'])/(?!/)', lambda m: m[1] + prefix + '/', text)
        script = '<script>' + DEVICE_SCRIPT.replace('__DEVICE_ID__', json.dumps(tid)) + '</script>'
        text = text.replace('<head>', '<head>' + script, 1) if '<head>' in text else script + text
        return text.encode('utf-8')
    if inner.startswith('/assets/') and any(x in kind for x in ('javascript', 'text/css')):
        text = body.decode('utf-8')
        text = re.sub(r'([\"\'`(])/(assets/)', lambda m: m[1] + prefix + '/' + m[2], text)
        return text.encode('utf-8')
    return body


async def proxy_http(tun, writer, method, inner, headers, body, sid, account):
    rid = secrets.token_urlsafe(16)
    fut = asyncio.get_running_loop().create_future()
    tun.pending[rid] = fut
    try:
        if not await tun.send({'type': 'http', 'id': rid, 'method': method, 'path': inner,
                               'headers': _forward_headers(headers, tun, sid, account),
                               'body': base64.b64encode(body).decode('ascii')}):
            await response(writer, 503, {'error': '设备连接已断开'})
            return
        try:
            result = await asyncio.wait_for(fut, HTTP_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await response(writer, 503, {'error': '设备连接中断或请求超时'})
            return
        if not STORE.session(sid) or not STORE.device(tun.id, account['id']):
            await response(writer, 401, {'error': '登录已失效'})
            return
        status = int(result.get('status', 502))
        rheaders = result.get('headers', [])
        payload = base64.b64decode(result.get('body', ''), validate=True)
        if 300 <= status < 400:
            location = next((v for k, v in rheaders if k.lower() == 'location'), '')
            target = urlsplit(urljoin('/t/' + tun.id + inner, location))
            if (not location or target.scheme or target.netloc
                    or not target.path.startswith('/t/' + tun.id + '/')):
                await response(writer, 502, {'error': '设备返回的跳转不被允许'})
                return
            await redirect(writer, target.geturl())
            return
        payload = _rewrite_content(payload, rheaders, tun.id, inner)
        kept = [(k, v) for k, v in rheaders if k.lower() not in (
            'connection', 'transfer-encoding', 'content-length', 'set-cookie', 'server', 'date',
            'content-encoding', 'etag', 'cache-control', 'content-type')]
        kind = next((v for k, v in rheaders if k.lower() == 'content-type'), 'application/octet-stream')
        await response(writer, status, payload, kind, kept)
    finally:
        tun.pending.pop(rid, None)


async def account_api(writer, path, method, headers, body, ip, account, sid):
    if method == 'GET' and path == '/api/relay/me':
        if not account:
            await response(writer, 401, {'error': '请先登录'})
            return
        devices = STORE.db.execute('SELECT id,name FROM devices WHERE account_id=? ORDER BY rowid', (account['id'],))
        await response(writer, 200, {'username': account['username'], 'devices': [
            {'id': d['id'], 'name': d['name'], 'online': d['id'] in TUNNELS} for d in devices]})
        return
    if method != 'POST':
        await response(writer, 404, {'error': '接口不存在'})
        return
    if len(body) > 8192:
        raise ValueError('请求体过大')
    data = json.loads(body.decode('utf-8'))
    if not isinstance(data, dict):
        raise ValueError('请求格式错误')
    if path == '/api/relay/unbind-device':
        tid, key = data.get('device_id', ''), data.get('device_key', '')
        device = STORE.device(tid) if _tok_ok(tid) else None
        if not device or not _tok_ok(key) or not hmac.compare_digest(device['secret_hash'], _digest(key)):
            await response(writer, 403, {'error': '设备未绑定或凭据已失效'})
            return
        with STORE.db:
            STORE.db.execute('DELETE FROM devices WHERE id=?', (tid,))
        tun = TUNNELS.get(tid)
        if tun:
            await tun.close()
        await response(writer, 200, {'ok': True})
        return
    if path in ('/api/relay/login', '/api/relay/bind'):
        username = data.get('username')
        if not _rate_ok(ip, username):
            await response(writer, 429, {'error': '尝试过于频繁，请两分钟后重试'})
            return
        if path.endswith('/bind'):
            token = headers.get('x-relay-token', '')
            if not token or not hmac.compare_digest(token.encode('utf-8'), ADMISSION_TOKEN.encode('utf-8')):
                _rate_fail(ip, username)
                await response(writer, 403, {'error': '准入密钥错误'})
                return
        try:
            name = data.get('device_name', '')
            if path.endswith('/bind') and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 64):
                raise ValueError('电脑名称需为 1-64 个字符')
            user = await STORE.authenticate(username, data.get('password'),
                                             create=path.endswith('/bind') and data.get('register') is True)
        except (ValueError, UnicodeError) as exc:
            _rate_fail(ip, username)
            errors = {'账号已存在，请选择登录已有账号', '账号或密码错误', '账号或密码格式无效',
                      '电脑名称需为 1-64 个字符', '密码已修改，请重新登录'}
            message = str(exc)
            await response(writer, 400, {'error': message if message in errors else '账号或密码格式无效'})
            return
        if path.endswith('/bind'):
            old_id, old_key = data.get('device_id', ''), data.get('device_key', '')
            old = STORE.device(old_id, user['id']) if _tok_ok(old_id) else None
            if old and _tok_ok(old_key) and hmac.compare_digest(old['secret_hash'], _digest(old_key)):
                tid, secret = old_id, old_key
                with STORE.db:
                    STORE.db.execute('UPDATE devices SET name=? WHERE id=?', (name.strip(), tid))
            else:
                tid, secret = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
                with STORE.db:
                    STORE.db.execute('INSERT INTO devices VALUES(?,?,?,?)', (tid, user['id'], name.strip(), _digest(secret)))
            await response(writer, 200, {'device_id': tid, 'device_key': secret,
                                         'username': user['username'], 'device_name': name.strip(), 'protocol': PROTOCOL})
        else:
            with STORE.db:
                if _tok_ok(sid):
                    STORE.db.execute('DELETE FROM sessions WHERE id_hash=?', (_digest(sid),))
            new_sid = STORE.new_session(user)
            await response(writer, 200, {'ok': True}, headers=(('Set-Cookie',
                '%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d' % (SESSION_COOKIE, new_sid, SESSION_AGE)),))
        return
    if not account:
        await response(writer, 401, {'error': '请先登录'})
        return
    if path == '/api/relay/logout':
        with STORE.db:
            STORE.db.execute('DELETE FROM sessions WHERE id_hash=?', (_digest(sid),))
        await response(writer, 200, {'ok': True}, headers=(('Set-Cookie',
            SESSION_COOKIE + '=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'),))
    elif path == '/api/relay/password':
        await STORE.authenticate(account['username'], data.get('old_password'))
        password = data.get('password')
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            raise ValueError('密码需为 1-128 个字符')
        salt = secrets.token_hex(16)
        value = await asyncio.get_running_loop().run_in_executor(None, _password_hash, password, salt)
        current = STORE.session(sid)
        if not current or current['version'] != account['version']:
            await response(writer, 401, {'error': '登录已失效，请重新登录'})
            return
        with STORE.db:
            STORE.db.execute('UPDATE accounts SET salt=?,password_hash=?,version=version+1 WHERE id=?',
                             (salt, value, account['id']))
            STORE.db.execute('DELETE FROM sessions WHERE account_id=?', (account['id'],))
        await response(writer, 200, {'ok': True})
    elif path == '/api/relay/unbind':
        tid = data.get('device_id', '')
        if not STORE.device(tid, account['id']):
            await response(writer, 404, {'error': '设备不存在'})
            return
        with STORE.db:
            STORE.db.execute('DELETE FROM devices WHERE id=?', (tid,))
        tun = TUNNELS.get(tid)
        if tun:
            await tun.close()
        await response(writer, 200, {'ok': True})
    else:
        await response(writer, 404, {'error': '接口不存在'})


async def public_conn(reader, writer):
    upgraded = False
    try:
        method, raw, headers, body = await read_request(reader)
        host = headers.get('host', '')
        parsed_host = urlsplit('http://' + host)
        if (not host or not parsed_host.hostname or parsed_host.username or parsed_host.password
                or parsed_host.path or parsed_host.query or parsed_host.fragment):
            raise ValueError('Host 无效')
        parsed_host.port
        origin = 'http://' + host.lower()
        path = raw.split('?', 1)[0]
        if method not in ('GET', 'HEAD') or _upgrade(headers):
            if path != '/relay/register' and headers.get('origin') != origin:
                await response(writer, 403, {'error': 'Origin 不被允许'})
                return
        if headers.get('sec-fetch-site') == 'cross-site' and (method != 'GET' or _upgrade(headers)):
            await response(writer, 403, {'error': '跨站请求被拒绝'})
            return
        if path == '/relay/register':
            if method != 'GET' or not _upgrade(headers) or body:
                raise ValueError('隧道注册请求无效')
            upgraded = True
            await relay_register(reader, writer, headers, origin)
            return
        sid = _cookie(headers, SESSION_COOKIE)
        account = STORE.session(sid)
        ip = str(writer.get_extra_info('peername')[0])
        if path.startswith('/api/relay/'):
            await account_api(writer, path, method, headers, body, ip, account, sid)
            return
        query = parse_qs(raw.partition('?')[2], keep_blank_values=True)
        device_query = query.get('relay_device', [])
        match = re.fullmatch(r'/t/([A-Za-z0-9_-]{16,128})(/.*)?', path)
        tid = match[1] if match else (device_query[0] if len(device_query) == 1 else '')
        if not tid and path == '/' and method == 'GET':
            await response(writer, 200, PORTAL_HTML, 'text/html; charset=utf-8')
            return
        if not account:
            if method == 'GET' and not _upgrade(headers) and not path.startswith(('/api/', '/assets/')):
                await redirect(writer, '/')
            else:
                await response(writer, 401, {'error': '登录已失效，请返回服务器首页登录'})
            return
        if not _tok_ok(tid) or not STORE.device(tid, account['id']):
            await response(writer, 403, {'error': '未指定设备或无权访问此设备'})
            return
        inner = (match[2] or '/') if match else path
        if (method == 'GET' and not _upgrade(headers) and match
                and not inner.startswith(('/api/', '/assets/', '/mobile/'))
                and '.' not in inner.rsplit('/', 1)[-1]):
            query.pop('relay_device', None)
            query.pop('kimi_onboarded', None)
            query['relay_device'] = [tid]
            query['kimi_onboarded'] = ['1']
            await redirect(writer, inner + '?' + urlencode(query, doseq=True))
            return
        tun = TUNNELS.get(tid)
        if not tun or not tun.alive:
            await response(writer, 503, {'error': '电脑离线，请返回服务器首页查看设备'})
            return
        query.pop('relay_device', None)
        suffix = urlencode(query, doseq=True)
        inner = inner + ('?' + suffix if suffix else '')
        if _upgrade(headers):
            if method != 'GET' or body or inner != '/api/v1/ws':
                raise ValueError('升级通道不被允许')
            upgraded = True
            await proxy_ws(tun, reader, writer, headers, inner, sid, account)
        else:
            await proxy_http(tun, writer, method, inner, headers, body, sid, account)
    except (ValueError, UnicodeError, KeyError, TypeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        if not upgraded and not writer.is_closing():
            await response(writer, 400, {'error': '请求无效，或账号密码不正确'})
    except (OSError, asyncio.TimeoutError):
        pass
    except Exception:
        log.error('relay request failed')
        if not upgraded and not writer.is_closing():
            await response(writer, 500, {'error': '服务器内部错误'})
    finally:
        writer.close()


PORTAL_HTML = '''<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<title>Kimi Code 远程控制</title><style>
body{font:15px system-ui;background:#101014;color:#eee;margin:0;padding:24px}main{max-width:520px;margin:5vh auto}
input,button{box-sizing:border-box;font:inherit;padding:12px;border-radius:8px;border:1px solid #444;background:#202027;color:inherit}
input{width:100%;margin:6px 0}button{cursor:pointer;margin:6px 6px 6px 0}.device{padding:16px;border:1px solid #444;border-radius:10px;margin:12px 0}
p{line-height:1.6;color:#bcbcc5}#msg{color:#f5ba75;white-space:pre-wrap}[hidden]{display:none!important}
</style></head><body><main><h1>Kimi Code 远程控制</h1><p>私人 HTTP/WS 中继。账号密码、二维码和控制流量为明文，请勿使用其他网站的密码或分享二维码。</p>
<p id="msg" role="status"></p><form id="login"><label>账号<input id="username" autocomplete="username" maxlength="64" required></label>
<label>密码<input id="password" type="password" autocomplete="current-password" maxlength="128" required></label><button>登录</button></form>
<section id="home" hidden><h2 id="who"></h2><div id="devices"></div><button id="refresh">刷新设备</button><button id="logout">退出登录</button>
<details><summary>修改账号密码</summary><form id="change"><input id="old" type="password" placeholder="原密码" autocomplete="current-password" required>
<input id="new" type="password" placeholder="新密码（1-128 字符）" autocomplete="new-password" maxlength="128" required><button>修改并注销全部登录</button></form>
<p>修改后旧二维码失效。请在电脑端用新密码重新登录绑定，以更新本机保存的密码和二维码。</p></details></section></main><script>
(function(){'use strict';var $=function(id){return document.getElementById(id)},target='',busy=false;
var params=new URLSearchParams(location.hash.slice(1));var auto=params.has('username')&&params.has('password');
if(location.hash)history.replaceState(null,'',location.pathname);
async function api(path,data){var r=await fetch('/api/relay/'+path,{method:data?'POST':'GET',credentials:'same-origin',cache:'no-store',
headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined});var d=await r.json();if(!r.ok)throw new Error(d.error||'请求失败');return d;}
function message(text){$('msg').textContent=text||'';}
async function home(){try{var d=await api('me');$('login').hidden=true;$('home').hidden=false;$('who').textContent=d.username+' 的电脑';$('devices').replaceChildren();
d.devices.forEach(function(device){var box=document.createElement('div');box.className='device';var name=document.createElement('strong');name.textContent=device.name+(device.online?' · 在线':' · 离线');box.append(name,document.createElement('br'));
var enter=document.createElement('button');enter.textContent='进入电脑';enter.disabled=!device.online;enter.onclick=function(){location.assign('/t/'+device.id+'/?kimi_onboarded=1#token=kimi-mobile-lan')};
var remove=document.createElement('button');remove.textContent='解除绑定';remove.onclick=async function(){if(!confirm('解除“'+device.name+'”的绑定并断开控制？'))return;try{await api('unbind',{device_id:device.id});await home()}catch(e){message(e.message)}};box.append(enter,remove);$('devices').append(box);
if(target===device.id&&device.online){target='';enter.click()}});if(!d.devices.length)message('还没有绑定的电脑，请从电脑端注册或登录并绑定。');
if(target){message('目标电脑离线或已解除绑定，可稍后刷新。');target=''}
}catch(e){$('login').hidden=false;$('home').hidden=true;message(e.message)}}
$('login').onsubmit=async function(e){if(e)e.preventDefault();if(busy)return;busy=true;var b=$('login').querySelector('button');b.disabled=true;message('正在登录…');
try{await api('login',{username:$('username').value,password:$('password').value});$('password').value='';message('');await home()}catch(err){message(err.message)}finally{busy=false;b.disabled=false}};
$('logout').onclick=async function(){try{await api('logout',{});location.replace('/')}catch(e){message(e.message)}};
$('refresh').onclick=home;$('change').onsubmit=async function(e){e.preventDefault();try{await api('password',{old_password:$('old').value,password:$('new').value});$('old').value='';$('new').value='';await home();message('密码已修改，请重新登录，并在电脑端更新保存的密码。')}catch(err){message(err.message)}};
if(auto){$('username').value=params.get('username');$('password').value=params.get('password');target=params.get('device')||'';params=null;$('login').onsubmit()}else{params=null;home()}
})();</script></body></html>'''


DEVICE_SCRIPT = r'''(function(){'use strict';var id=__DEVICE_ID__,prefix='/t/'+id;
function network(value){var u=new URL(value,location.href);if(u.origin===location.origin&&!u.pathname.startsWith('/t/')&&!u.pathname.startsWith('/api/relay/'))u.pathname=prefix+u.pathname;if(u.origin===location.origin)u.searchParams.delete('relay_device');return u.href}
function asset(value){if(typeof value!=='string')return value;var u=new URL(value,location.href);return u.origin===location.origin&&(u.pathname.startsWith('/assets/')||u.pathname.startsWith('/api/'))?network(value):value}
[[HTMLScriptElement,'src'],[HTMLLinkElement,'href'],[HTMLImageElement,'src'],[HTMLMediaElement,'src'],[HTMLSourceElement,'src']].forEach(function(item){var d=Object.getOwnPropertyDescriptor(item[0].prototype,item[1]);if(d&&d.set&&d.configurable)Object.defineProperty(item[0].prototype,item[1],Object.assign({},d,{set:function(value){d.set.call(this,asset(value))}}))});
var setAttribute=Element.prototype.setAttribute;Element.prototype.setAttribute=function(name,value){return setAttribute.call(this,name,/^(src|href)$/i.test(name)?asset(value):value)};
var fetch0=window.fetch;window.fetch=function(input,init){if(input instanceof Request)return fetch0.call(this,new Request(network(input.url),input),init);return fetch0.call(this,network(input),init)};
var open=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(method,url){var args=Array.from(arguments);args[1]=network(url);return open.apply(this,args)};
var WS=window.WebSocket;window.WebSocket=new Proxy(WS,{construct:function(Target,args){var u=new URL(args[0],location.href);if((u.protocol==='ws:'&&location.protocol==='http:')&&u.host===location.host&&!u.pathname.startsWith('/t/'))u.pathname=prefix+u.pathname;args[0]=u.href;return Reflect.construct(Target,args)}});
function route(url){if(url==null)return url;var u=new URL(url,location.href);if(u.origin===location.origin){u.searchParams.set('relay_device',id);u.searchParams.set('kimi_onboarded','1')}return u.href}
['pushState','replaceState'].forEach(function(key){var original=history[key];history[key]=function(state,title,url){return original.call(this,state,title,route(url))}});
var storage=Storage.prototype;['getItem','setItem','removeItem'].forEach(function(key){var original=storage[key];storage[key]=function(name){var args=Array.from(arguments);args[0]='kimi-relay:'+id+':'+name;return original.apply(this,args)}});
window.addEventListener('DOMContentLoaded',function(){var bar=document.createElement('div');bar.style.cssText='position:fixed;top:4px;right:8px;z-index:2147483640;display:flex;gap:6px';
[['电脑列表',function(){location.assign('/')}],['退出登录',async function(){try{var r=await fetch0('/api/relay/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!r.ok)throw Error();location.replace('/')}catch(e){alert('退出失败，请稍后重试')}}]].forEach(function(item){var b=document.createElement('button');b.textContent=item[0];b.style.cssText='padding:5px 8px;border:1px solid #777;border-radius:8px;background:#222;color:white;font-size:12px';b.onclick=item[1];bar.appendChild(b)});document.body.appendChild(bar)});
})();'''


async def main_async(port):
    server = await asyncio.start_server(public_conn, LISTEN_HOST, port, limit=HEADER_MAX + 4)
    log.info('relay up: HTTP/WS :%d', port)
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=PUBLIC_PORT)
    parser.add_argument('--token', default=os.environ.get('RELAY_TOKEN', ''))
    parser.add_argument('--data-dir', default=os.path.join(os.path.expanduser('~'), '.kimi-relay'))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口需为 1-65535')
    if not args.token or len(args.token) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in args.token):
        parser.error('必须通过 RELAY_TOKEN 或 --token 设置 1-256 位可见 ASCII 准入密钥')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    global STORE, ADMISSION_TOKEN
    ADMISSION_TOKEN = args.token
    os.umask(0o077)
    STORE = AccountStore(os.path.join(args.data_dir, 'relay.sqlite3'))
    try:
        asyncio.run(main_async(args.port))
    except KeyboardInterrupt:
        pass
    finally:
        STORE.db.close()


if __name__ == '__main__':
    main()
