# -*- coding: utf-8 -*-
"""
Kimi Code 用量面板 · 私人测试用公网中继（VPS 侧，纯标准库，Python>=3.8）

拓扑：
  手机浏览器 --HTTP/WS--> 公网口 :<public_port> --WS 帧--> worker 隧道
  worker --ws--> 隧道注册口 :<listen_port>（X-Relay-Token 共享密钥）

用法（CLI 与 worker 侧 relay.json 对应，默认值即未配置回退）：
  python3 relay_server.py [--tunnel-port 48213] [--public-port 47961]
                          [--public-host <host:port>] [--token <共享密钥>]
  --tunnel-port  worker 隧道注册口（默认 48213，worker relay.json 的 tunnel_port）
  --public-port  手机公网口（默认 47961，worker relay.json 的 public_port）
  --public-host  注册回执里 origin 的 host:port（默认 <本机 IP>:<public_port>；
                 须与 worker 桥白名单一致——worker 用 relay.json host:public_port）
  --token        X-Relay-Token 共享密钥（默认空 = 不校验；worker 用 relay.json token）

隧道注册口：worker 以 WebSocket 连到 /relay/register（路径固定），携带
X-Relay-Token（共享密钥，经环境变量 RELAY_TOKEN 注入，可经命令行参数覆盖）。
握手成功后回送 {type:'registered', tunnel_id, origin}；之后 worker 经该 WS
收/发「请求帧」与「WS 数据帧」，断连即清除隧道。

公网口：手机请求按 /t/<tunnel_id>/<rest> 路径寻址隧道。
  - 普通 HTTP：组装 {type:'http', id, method, path, headers, body(b64)} 推给
    worker，等待 {type:'http_resp', id, status, headers, body(b64)} 回写。
  - WebSocket 升级（仅 /mobile/ws 等路径，由 worker 桥自己裁决）：服务端与手机
    完成 RFC6455 握手后，把帧原样封进 {type:'ws'} 消息与 worker 双向互转。

第一阶段只做 HTTP（ws:// + http://），无 TLS。所有密钥/tunnel_id 只进日志
前缀，绝不回显密钥本体。本模块只依赖 Python>=3.8 标准库，不装任何第三方。
"""
import asyncio
import base64
import hashlib
import json
import logging
import secrets
import struct
import sys
import time
from urllib.parse import unquote

# ---------------- 配置 ----------------
LISTEN_HOST = '0.0.0.0'
TUNNEL_PORT = 48213         # worker 隧道注册口（ws://<vps>:<TUNNEL_PORT>/relay/register）
PUBLIC_PORT = 47961         # 手机公网口（http://<vps>:<PUBLIC_PORT>/t/<id>/...）
# 注册回执 origin 的 host:port（worker 桥按它做 Host/Origin 校验——必须与
# worker relay.json 的 host:public_port 一致；默认值在 main() 里按参数改写）。
PUBLIC_HOST_HEADER = '127.0.0.1:%d' % PUBLIC_PORT

MAX_HTTP_BODY = 32 * 1024 * 1024     # 与桥 PROXY_MAX_REQUEST_BODY 对齐
MAX_WS_MESSAGE = 4 * 1024 * 1024     # 与桥 _WS_MAX_MESSAGE 对齐（手机侧 WS 帧上限）
# 隧道（worker<->VPS）单帧上限。帧里是 base64 JSON，体积约为原始的 1.34 倍：
# 桥 PROXY_MAX_RESPONSE_BODY=128MB → 约 172MB。旧版误用 MAX_WS_MESSAGE(4MB)，
# SPA 主包 3.5MB → 隧道帧 4.7MB 超限 → 整条隧道被判违例拆掉 → 手机一连就断。
TUNNEL_MAX_FRAME = 192 * 1024 * 1024
HTTP_TIMEOUT = 60.0          # HTTP 请求在隧道内的等待上限
TUNNEL_IDLE_CLOSE = 90.0     # 隧道 WS 读空闲上限（worker 有心跳）
HEADER_MAX = 32 * 1024
RECONNECT_WAIT = 10.0        # 隧道重连空档内，公网请求最多等它回来的秒数
RELAY_COOKIE = 'kimi_relay_tid'   # 根路径请求的隧道归属（访问 /t/<tid>/ 时下发）
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
_TOK_ID_RE = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')

log = logging.getLogger('relay_server')


def _tok_ok(s, lo=1, hi=64):
    return isinstance(s, str) and lo <= len(s) <= hi and all(c in _TOK_ID_RE for c in s)


# ---------------- WebSocket 帧（RFC6455，纯标准库） ----------------
def _xor_mask(data, mask):
    """RFC6455 掩码：大整数一次异或，MB 级帧比逐字节生成器快两个数量级。"""
    n = len(data)
    if not n:
        return b''
    m = (bytes(mask) * (n // 4 + 1))[:n]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(m, 'big')).to_bytes(n, 'big')


def ws_frame(opcode, payload, mask_key=None):
    """opcode: 0/1/2/8/9/10；mask_key=None 表示不掩码（服务端侧）。"""
    b0 = 0x80 | opcode
    ln = len(payload)
    if ln < 126:
        head = bytes([b0, ln])
    elif ln < 65536:
        head = bytes([b0, 126]) + struct.pack('>H', ln)
    else:
        head = bytes([b0, 127]) + struct.pack('>Q', ln)
    if mask_key is None:
        return head + payload
    return head + mask_key + _xor_mask(payload, mask_key)


async def ws_read_frame(reader, masked_expected, max_len=MAX_WS_MESSAGE):
    """读一帧；返回 (fin, opcode, payload) 或 None（连接不可用/协议违例）。"""
    try:
        head = await asyncio.wait_for(reader.readexactly(2), timeout=TUNNEL_IDLE_CLOSE)
    except Exception:
        return None
    b0, b1 = head[0], head[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    if b0 & 0x70 or masked != masked_expected or opcode not in (0, 1, 2, 8, 9, 10):
        return None
    ln = b1 & 0x7F
    if opcode >= 8 and (not fin or ln > 125):
        return None
    if ln == 126:
        ext = await _read_n(reader, 2)
        if ext is None:
            return None
        ln = struct.unpack('>H', ext)[0]
    elif ln == 127:
        ext = await _read_n(reader, 8)
        if ext is None:
            return None
        ln = struct.unpack('>Q', ext)[0]
        if ln < 65536:
            return None
    if ln > max_len:
        return None
    mask = None
    if masked:
        mask = await _read_n(reader, 4)
        if mask is None:
            return None
    payload = await _read_n(reader, ln)
    if payload is None:
        return None
    if mask is not None:
        payload = _xor_mask(payload, mask)
    return fin, opcode, payload


async def _read_n(reader, n):
    try:
        return await asyncio.wait_for(reader.readexactly(n), timeout=TUNNEL_IDLE_CLOSE)
    except Exception:
        return None


def ws_client_frame(opcode, payload):
    """worker 侧发帧：必须掩码（RFC6455 客户端语义）。"""
    return ws_frame(opcode, payload, mask_key=secrets.token_bytes(4))


# ---------------- HTTP 头解析（极简、有界） ----------------
async def read_http_head(reader, max_bytes=HEADER_MAX):
    """读到 \r\n\r\n 为止；返回 (head_bytes, rest_bytes) 或 None。"""
    buf = bytearray()
    while b'\r\n\r\n' not in buf:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=30.0)
        except Exception:
            return None
        if not chunk:
            return None
        buf += chunk
        if len(buf) > max_bytes:
            return None
    head, rest = bytes(buf).split(b'\r\n\r\n', 1)
    return head, rest


def parse_request_head(head):
    """返回 (method, raw_path, version, headers_list[(name_lower, value)])。失败 None。"""
    try:
        lines = head.split(b'\r\n')
        first = lines[0].split(b' ', 2)
        if len(first) != 3:
            return None
        method = first[0].decode('latin-1')
        raw_path = first[1].decode('latin-1')
        if not raw_path.startswith('/') or raw_path.startswith('//'):
            return None
        headers = []
        for line in lines[1:]:
            name, sep, value = line.partition(b':')
            if not sep or not name:
                return None
            headers.append((name.decode('latin-1').strip().lower(),
                            value.decode('latin-1').strip()))
        return method, raw_path, 'HTTP/' + first[2].decode('latin-1'), headers
    except Exception:
        return None


def headers_map(headers):
    out = {}
    for k, v in headers:
        if k in out:
            out[k] = out[k] + ', ' + v
        else:
            out[k] = v
    return out


# ---------------- 隧道注册口 ----------------
class Tunnel:
    """一条 worker 隧道：tunnels[tunnel_id] = Tunnel。"""
    def __init__(self, tid, reader, writer):
        self.id = tid
        self.reader = reader
        self.writer = writer
        self.pending = {}          # http id -> asyncio.Future
        self.ws_subs = {}          # ws channel id -> PhoneWS
        self.alive = True
        self.lock = asyncio.Lock() # 写锁：多并发响应帧不乱序


TUNNELS = {}
# tid 墓碑：刚注销的 tunnel_id -> 过期 epoch。worker 掉线重连用
# X-Relay-Resume 复用旧 tid，让已下发给手机的 /t/<tid>/ 配对 URL 在
# 重连后继续有效——否则手机会收到 tunnel not found。
RECENT_TIDS = {}
RECENT_TTL = 600.0      # 墓碑保留时长（秒）
RECENT_MAX = 512        # 墓碑表上限，防内存膨胀


def _recent_remember(tid):
    if not _tok_ok(tid, 4, 32):
        return
    now = time.time()
    # 先清过期，再记
    for k in [k for k, exp in RECENT_TIDS.items() if exp <= now]:
        RECENT_TIDS.pop(k, None)
    while len(RECENT_TIDS) >= RECENT_MAX:
        oldest = min(RECENT_TIDS, key=lambda k: RECENT_TIDS[k])
        RECENT_TIDS.pop(oldest, None)
    RECENT_TIDS[tid] = now + RECENT_TTL


def _recent_valid(tid):
    """tid 是否可复用：仍活着，或在墓碑表未过期。"""
    if not _tok_ok(tid, 4, 32):
        return False
    if tid in TUNNELS:
        return True
    exp = RECENT_TIDS.get(tid)
    return exp is not None and exp > time.time()


async def tunnel_send(tun, msg):
    data = json.dumps(msg, ensure_ascii=False).encode('utf-8')
    async with tun.lock:
        try:
            tun.writer.write(ws_frame(1, data))
            await tun.writer.drain()
            return True
        except Exception:
            return False


async def relay_register(reader, writer, token):
    """处理一条到 /relay/register 的 WS：校验 X-Relay-Token，完成握手，注册隧道。"""
    head_rest = await read_http_head(reader)
    if head_rest is None:
        writer.close()
        return
    head, _rest = head_rest
    parsed = parse_request_head(head)
    if parsed is None:
        await _http_resp(writer, 400, 'bad request')
        return
    method, raw_path, _ver, headers = parsed
    hmap = headers_map(headers)
    if method != 'GET' or raw_path.split('?')[0] != '/relay/register':
        await _http_resp(writer, 404, 'not found')
        return
    if token and not secrets.compare_digest(hmap.get('x-relay-token') or '', token):
        await _http_resp(writer, 403, 'forbidden')
        return
    if (hmap.get('upgrade') or '').lower() != 'websocket':
        await _http_resp(writer, 400, 'upgrade required')
        return
    key = hmap.get('sec-websocket-key')
    if not key or len(key) > 64:
        await _http_resp(writer, 400, 'bad ws key')
        return
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode('ascii')).digest())
    writer.write((b'HTTP/1.1 101 Switching Protocols\r\n'
                  b'Upgrade: websocket\r\n'
                  b'Connection: Upgrade\r\n'
                  b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n'))
    try:
        await writer.drain()
    except Exception:
        return

    # tid 粘滞：worker 掉线重连时带 X-Relay-Resume=<旧 tid>，若该 tid 仍存活
    # 或在墓碑表未过期则复用，保证手机端已配对的 /t/<tid>/ URL 不失效。
    resume = hmap.get('x-relay-resume') or ''
    if resume and _recent_valid(resume) and resume not in TUNNELS:
        tid = resume
    else:
        tid = secrets.token_urlsafe(8)
    origin = 'http://%s/t/%s' % (PUBLIC_HOST_HEADER, tid)
    tun = Tunnel(tid, reader, writer)
    TUNNELS[tid] = tun
    RECENT_TIDS.pop(tid, None)   # 复用后从墓碑移除（它现在又活了）
    try:
        if not await tunnel_send(tun, {'type': 'registered', 'tunnel_id': tid, 'origin': origin}):
            return
        log.info('tunnel registered: %s', tid)
        # 主读循环：worker 的帧 = 响应 / ws 数据 / pong
        while True:
            frame = await ws_read_frame(reader, masked_expected=True,
                                        max_len=TUNNEL_MAX_FRAME)
            if frame is None:
                break
            fin, opcode, payload = frame
            if opcode == 8:
                break
            if opcode == 9:      # ping → pong
                try:
                    async with tun.lock:
                        tun.writer.write(ws_frame(10, payload))
                        await tun.writer.drain()
                except Exception:
                    break
                continue
            if opcode != 1:
                continue
            try:
                msg = json.loads(payload.decode('utf-8'))
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            mtype = msg.get('type')
            if mtype == 'http_resp':
                fut = tun.pending.pop(msg.get('id'), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
            elif mtype in ('ws_data', 'ws_close'):
                sub = tun.ws_subs.get(msg.get('id'))
                if sub is not None:
                    await sub.on_tunnel_frame(msg)
            elif mtype == 'pong':
                continue
    finally:
        tun.alive = False
        if TUNNELS.get(tid) is tun:
            TUNNELS.pop(tid, None)
        _recent_remember(tid)   # 入墓碑：重连可 X-Relay-Resume 复用
        for fut in tun.pending.values():
            if not fut.done():
                fut.cancel()
        for sub in list(tun.ws_subs.values()):
            await sub.close()
        try:
            writer.close()
        except Exception:
            pass
        log.info('tunnel unregistered: %s', tid)


def _root_tid(hmap):
    """根路径请求的隧道归属：Cookie → Referer；都没有返回 ''。"""
    for part in (hmap.get('cookie') or '').split(';'):
        k, sep, v = part.strip().partition('=')
        if sep and k == RELAY_COOKIE and _tok_ok(v.strip(), 4, 32):
            return v.strip()
    ref = hmap.get('referer') or ''
    i = ref.find('/t/')
    if i >= 0:
        cand = ref[i + 3:].split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
        if _tok_ok(cand, 4, 32):
            return cand
    return ''


async def _await_tunnel(tid):
    """取活隧道；tid 在墓碑表（worker 正在重连）时最多等 RECONNECT_WAIT 秒。"""
    tun = TUNNELS.get(tid)
    if tun is not None and tun.alive:
        return tun
    if not _recent_valid(tid):
        return None
    deadline = time.monotonic() + RECONNECT_WAIT
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        tun = TUNNELS.get(tid)
        if tun is not None and tun.alive:
            return tun
        if not _recent_valid(tid):
            return None
    return None


async def _tunnel_missing(writer, tid):
    if _recent_valid(tid):
        await _http_resp(writer, 503, 'tunnel reconnecting')
    else:
        await _http_resp(writer, 404, 'tunnel not found')


async def _http_resp(writer, code, text):
    body = text.encode('utf-8')
    writer.write((b'HTTP/1.1 %d\r\nContent-Type: text/plain; charset=utf-8\r\n'
                  b'Content-Length: %d\r\nConnection: close\r\n\r\n%s')
                 % (code, len(body), body))
    try:
        await writer.drain()
    except Exception:
        pass
    writer.close()


# ---------------- 公网口 ----------------
class PhoneWS:
    """公网口上与手机浏览器的一条 WS：与隧道内 {id} 通道双向映射。"""
    def __init__(self, tun, cid, reader, writer):
        self.tun = tun
        self.cid = cid
        self.reader = reader
        self.writer = writer
        self.alive = True

    async def on_tunnel_frame(self, msg):
        """worker 推来的 ws 数据帧 → 写回手机。"""
        if not self.alive:
            return
        try:
            if msg['type'] == 'ws_data':
                opcode = int(msg.get('opcode', 1))
                payload = base64.b64decode(msg.get('payload', ''))
                self.writer.write(ws_frame(opcode, payload))
                await self.writer.drain()
            elif msg['type'] == 'ws_close':
                await self.close()
        except Exception:
            await self.close()

    async def close(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self.writer.write(ws_frame(8, b''))
            await self.writer.drain()
        except Exception:
            pass
        try:
            self.writer.close()
        except Exception:
            pass
        # 通知 worker 关侧
        try:
            await tunnel_send(self.tun, {'type': 'ws_close', 'id': self.cid})
        except Exception:
            pass


async def phone_ws_pump(tun, cid, reader, writer):
    """手机→隧道的 WS 帧读循环（在 handshake 之后调用）。"""
    sub = PhoneWS(tun, cid, reader, writer)
    tun.ws_subs[cid] = sub
    try:
        while sub.alive and tun.alive:
            frame = await ws_read_frame(reader, masked_expected=True)
            if frame is None:
                break
            fin, opcode, payload = frame
            if opcode == 9:      # ping → pong
                try:
                    writer.write(ws_frame(10, payload))
                    await writer.drain()
                except Exception:
                    break
                continue
            if opcode == 8:
                break
            if opcode not in (0, 1, 2):
                continue
            ok = await tunnel_send(tun, {'type': 'ws_data', 'id': cid, 'opcode': opcode,
                                       'fin': fin, 'payload': base64.b64encode(payload).decode('ascii')})
            if not ok:
                break
    finally:
        sub.alive = False
        tun.ws_subs.pop(cid, None)
        try:
            writer.close()
        except Exception:
            pass
        try:
            await tunnel_send(tun, {'type': 'ws_close', 'id': cid})
        except Exception:
            pass


def _is_upgrade(headers):
    return 'upgrade' in {v.strip().lower() for v in headers_map(headers).get('connection', '').split(',')} \
        and (headers_map(headers).get('upgrade') or '').lower() == 'websocket'


async def public_conn(reader, writer):
    """公网口单连接：可能是普通 HTTP（读体→隧道→回写）或 WS 升级。"""
    try:
        await _public_conn(reader, writer)
    except Exception as e:
        log.exception('public_conn error: %r', e)
        try:
            writer.close()
        except Exception:
            pass


async def _public_conn(reader, writer):
    head_rest = await read_http_head(reader)
    if head_rest is None:
        writer.close()
        return
    head, rest = head_rest
    parsed = parse_request_head(head)
    if parsed is None:
        await _http_resp(writer, 400, 'bad request')
        return
    method, raw_path, _ver, headers = parsed
    hmap = headers_map(headers)

    # /t/<tunnel_id>/<rest>
    segs = raw_path.split('/')
    tun = None
    set_cookie = None
    if len(segs) >= 3 and segs[1] == 't' and _tok_ok(segs[2].split('?', 1)[0], 4, 32):
        tid = segs[2].split('?', 1)[0]
        tun = await _await_tunnel(tid)
        if tun is None:
            await _tunnel_missing(writer, tid)
            return
        set_cookie = tid
        inner = '/' + '/'.join(segs[3:])
        inner = inner.split('?', 1)[0]
        if not inner or inner == '/':
            inner = '/'
    else:
        # SPA 资源 / API / WS 用的是不带 /t/<id> 前缀的根绝对路径
        # （/assets/*、/api/v1/*、/api/v1/ws）。归属判定优先级：
        #   Cookie kimi_relay_tid（访问 /t/<tid>/ 时下发）→ Referer 里的
        #   /t/<tid>/ → 仅一条隧道时兜底；多隧道又无归属则 404（不猜）。
        tid = _root_tid(hmap)
        if tid:
            tun = await _await_tunnel(tid)
            if tun is None:
                await _tunnel_missing(writer, tid)
                return
        else:
            if len(TUNNELS) != 1:
                await _http_resp(writer, 404, 'unknown tunnel')
                return
            tun = next(iter(TUNNELS.values()))
            if not tun.alive:
                await _http_resp(writer, 404, 'tunnel not found')
                return
        inner = raw_path.split('?', 1)[0] or '/'
    # 保留 query（inner 此时是剥了前缀/未剥前缀的纯路径，query 不在其中）
    qidx = raw_path.find('?')
    query = raw_path[qidx:] if qidx >= 0 else ''
    inner_path = inner + query

    if _is_upgrade(headers):
        # WS 升级：只放行给 worker（桥自己裁决 /api/v1/ws 白名单）
        key = hmap.get('sec-websocket-key')
        if not key or len(key) > 64:
            await _http_resp(writer, 400, 'bad ws key')
            return
        cid = secrets.token_urlsafe(8)
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode('ascii')).digest())
        # 回显浏览器请求的 WS 子协议：SPA 用 'kimi-code.bearer.<凭据>' 子协议承载
        # 登录态，而浏览器规定服务端必须在 101 响应里回显所选子协议，否则直接
        # 判握手失败（页面报 WebSocket error，实时更新失效）。桥对该子协议只校验
        # 'kimi-code.bearer.' 前缀，故原样回显第一个即可；没有则不发这个头。
        proto = ''
        for p in (hmap.get('sec-websocket-protocol') or '').split(','):
            p = p.strip()
            if p.startswith('kimi-code.bearer.'):
                proto = p
                break
        resp = (b'HTTP/1.1 101 Switching Protocols\r\n'
                b'Upgrade: websocket\r\n'
                b'Connection: Upgrade\r\n'
                b'Sec-WebSocket-Accept: ' + accept + b'\r\n')
        if proto:
            resp += ('Sec-WebSocket-Protocol: %s\r\n' % proto).encode('latin-1')
        resp += b'\r\n'
        writer.write(resp)
        try:
            await writer.drain()
        except Exception:
            return
        # 把 rest 里可能已含的首帧留给手机侧 pump 的 reader（readexactly 已缓冲）
        # 告诉 worker 开一条 ws 通道
        ok = await tunnel_send(tun, {'type': 'ws_open', 'id': cid, 'path': inner_path,
                                     'headers': {k: v for k, v in headers
                                                 if k not in ('host', 'connection', 'upgrade',
                                                              'sec-websocket-key', 'sec-websocket-version',
                                                              'sec-websocket-extensions')}})
        if not ok:
            writer.close()
            return
        await phone_ws_pump(tun, cid, reader, writer)
        return

    # ---- 普通 HTTP ----
    # 读请求体（Content-Length；TE 不支持）
    body = b''
    if hmap.get('transfer-encoding'):
        await _http_resp(writer, 400, 'chunked not supported')
        return
    try:
        n = int(hmap.get('content-length') or '0')
    except ValueError:
        await _http_resp(writer, 400, 'bad content-length')
        return
    if n > MAX_HTTP_BODY:
        await _http_resp(writer, 413, 'body too large')
        return
    if n:
        need = n - len(rest)
        if need > 0:
            more = await _read_n(reader, need)
            if more is None:
                return
            rest += more
        body = rest[:n]
    req_id = secrets.token_urlsafe(10)
    t_start = time.monotonic()
    fut = asyncio.get_event_loop().create_future()
    tun.pending[req_id] = fut
    try:
        ok = await tunnel_send(tun, {
            'type': 'http', 'id': req_id, 'method': method, 'path': inner_path,
            'headers': {k: v for k, v in headers if k not in ('host', 'connection', 'content-length')},
            'body': base64.b64encode(body).decode('ascii')})
        if not ok:
            await _http_resp(writer, 502, 'tunnel write failed')
            return
        try:
            resp = await asyncio.wait_for(fut, timeout=HTTP_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning('%s %s -> 504 timeout', method, inner)
            await _http_resp(writer, 504, 'upstream timeout')
            return
        status = int(resp.get('status', 502))
        rheaders = resp.get('headers') or []
        rbody = base64.b64decode(resp.get('body', ''))
        out = bytearray(b'HTTP/1.1 %d\r\n' % status)
        sent_cl = False
        for k, v in rheaders:
            lk = k.lower()
            if lk in ('connection', 'transfer-encoding', 'content-length', 'server', 'date'):
                if lk == 'content-length':
                    sent_cl = True
                continue
            try:
                out += ('%s: %s\r\n' % (k, v)).encode('latin-1')
            except Exception as e:
                log.warning('hdr encode fail %r: %r', k, e)
                continue
        if set_cookie:
            out += ('Set-Cookie: %s=%s; Path=/; HttpOnly; SameSite=Lax\r\n'
                    % (RELAY_COOKIE, set_cookie)).encode('latin-1')
        out += b'Server: kimi-relay\r\n'
        out += b'Content-Length: %d\r\nConnection: close\r\n\r\n' % len(rbody)
        out += rbody
        writer.write(bytes(out))
        log.info('%s %s -> %d %dB %.2fs', method, inner, status, len(rbody),
                 time.monotonic() - t_start)
        await writer.drain()
    finally:
        tun.pending.pop(req_id, None)
        writer.close()


# ---------------- 入口 ----------------
async def main_async(listen_port, public_port, token):
    loop = asyncio.get_event_loop()
    ts = await asyncio.start_server(lambda r, w: relay_register(r, w, token),
                                    LISTEN_HOST, listen_port)
    ps = await asyncio.start_server(public_conn, LISTEN_HOST, public_port)
    log.info('relay up: tunnel :%d  public :%d', listen_port, public_port)
    async with ts, ps:
        await asyncio.gather(ts.serve_forever(), ps.serve_forever())


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    listen = TUNNEL_PORT
    public = PUBLIC_PORT
    public_host = ''
    token = ''
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ('--listen-port', '--tunnel-port') and i + 1 < len(args):
            listen = int(args[i + 1]); i += 2
        elif args[i] == '--public-port' and i + 1 < len(args):
            public = int(args[i + 1]); i += 2
        elif args[i] == '--public-host' and i + 1 < len(args):
            public_host = args[i + 1].strip(); i += 2
        elif args[i] == '--token' and i + 1 < len(args):
            token = args[i + 1]; i += 2
        else:
            i += 1
    if not token:
        token = __import__('os').environ.get('RELAY_TOKEN', '')
    # --public-host 决定注册回执里 origin 的 host:port（worker 桥白名单须匹配）；
    # 不传时回退到与旧版一致的固定 VPS IP（本脚本本来就只服务这一台机器，
    # 需要换机请显式传 --public-host <host:port>）。
    global PUBLIC_HOST_HEADER
    if public_host:
        PUBLIC_HOST_HEADER = public_host if ':' in public_host else '%s:%d' % (public_host, public)
    else:
        PUBLIC_HOST_HEADER = '114.66.24.119:%d' % public
    try:
        asyncio.run(main_async(listen, public, token))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
