# -*- coding: utf-8 -*-
"""
Kimi Code 用量面板 · 私人测试用公网中继（worker 侧，纯标准库，Python>=3.8）

拓扑（与 VPS 端 scripts/relay_server.py 配对）：
  手机浏览器 --HTTP/WS--> VPS 公网口 :<public_port> --WS 帧--> 本模块（worker 隧道）
  本模块 --HTTP/WS--> 127.0.0.1:<bridge_port>（本机桥，仍绑回环）

本模块在 worker 进程内运行：向 ws://<vps>:48213/relay/register 建立一条持久
WebSocket（X-Relay-Token 共享密钥，客户端帧必须掩码），注册后：

  - 收 {type:'http', id, method, path, headers, body(b64)}：
      向本机桥发一次普通 HTTP 请求（Host 重写为 VPS 公网口地址，
      Content-Length 按转发的 body 实算），把响应帧化回
      {type:'http_resp', id, status, headers, body(b64)}。
  - 收 {type:'ws_open', id, path, headers}：
      对本机桥发起一次 WebSocket 升级（同 Host 重写），之后把隧道侧
      {type:'ws_data', id, opcode, fin, payload(b64)} 与本机 socket 上的
      RFC6455 帧双向互转，{type:'ws_close'} 双向同步。
  - 周期性发 RFC6455 ping 保活；隧道断开会以指数退避重连（须先告知桥
    on_failure——桥语义是 fail-closed，重连只会用于下一次 start）。

与 ConnectorRuntime 的调用契约一致：start(bound_port, on_ready, on_fail)
立即返回、后台线程跑隧道；on_ready(origin) 在注册成功拿到
{registered, tunnel_id, origin} 后回调（origin 形如
'http://<vps>:<public_port>'，不带 /t/<id>——tid 经 self.tunnel_id 另取）；
on_failure(code) 在隧道不可用/断线时回调一次。stop() 同步停当前隧道，
shutdown() 永久关闭。code 只用桥白名单里的码（TUNNEL_* / START_CANCELLED）。

纯标准库：socket + ssl（预留 TLS，当前 ws:// 明文）+ base64 + hashlib +
json + secrets + struct + threading。不装任何第三方。
"""
import base64
import hashlib
import json
import secrets
import socket
import struct
import threading
import time

# ---------------- 配置 ----------------
# 默认值即「未配置」的兼容回退；持久化在 usage-dashboard/relay.json，由
# relay_config.py 统一读写校验。运行时经 RelayClient(...) 构造参数注入——
# 本模块不再用模块级常量写死目标 VPS（host/port/header 全部实例化）。
RELAY_HOST = ''
RELAY_PORT = 48213           # VPS 隧道注册口（ws://<vps>:<RELAY_PORT>/relay/register）
RELAY_PUBLIC_PORT = 47961    # VPS 公网口（http://<vps>:<RELAY_PUBLIC_PORT>/t/<id>/...）
# 共享密钥：运行期经 KIMI_RELAY_TOKEN 环境变量，或本机 usage-dashboard/relay.json
# 的 token 字段提供。**仓库不内置任何 token 默认值**——真实值只存在于部署环境与
# 本机配置，不进代码、不进提交。空串 = 不发 X-Relay-Token（仅服务器未启用
# --token 校验时可用）。
DEFAULT_RELAY_TOKEN = ''

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
CONNECT_TIMEOUT = 15.0
REGISTER_TIMEOUT = 20.0
HTTP_TIMEOUT = 60.0          # 隧道内一次 HTTP 往返的上限（与桥 PROXY 超时对齐）
PING_INTERVAL = 20.0         # 隧道保活 ping 周期
RECONNECT_BASE = 1.0         # 掉线重连起始退避（秒）
RECONNECT_MAX = 30.0         # 掉线重连退避上限（秒）
RECONNECT_GIVEUP = 300.0     # 连续重连多久仍失败才认 TUNNEL_EXITED（秒）
MAX_HTTP_BODY = 32 * 1024 * 1024
# 本端只用于【读】VPS 经隧道发来的帧，即手机上行方向：服务器公网口的请求体上限
# MAX_HTTP_BODY=32MiB 经 base64 封装（膨胀约 4/3）约 42.7MiB，故取 48MiB。
# 下行（电脑→手机）的大响应由本端发出，不收此限；那一侧由服务器的
# relay_server.TUNNEL_MAX_FRAME（192MiB）约束。取 4MiB 会让稍大的上传
# （如 32MiB 文件）读帧失败并拆掉隧道。
MAX_WS_MESSAGE = 48 * 1024 * 1024
HEADER_MAX = 32 * 1024
RECV_POLL = 0.5              # socket 轮询步长（让 stop 能及时生效）
# 帧内「无进展」停顿上限：帧一旦开始解析（已消费任意字节），超过此时长
# 没有任何新字节到达即判连接不可用并拆掉整条隧道——绝不带着半帧缓冲继续
# 复用。旧实现把 RECV_POLL(0.5s) 当作「整帧截止」，大帧跨过 0.5s 就中途
# 返回 None，而残留的半帧 payload 被下一次调用当成新帧头解析，帧边界错位，
# 上层 JSON.parse 遂报「Unterminated string in JSON」。
# 与 mobile_bridge.WS_FRAME_DEADLINE 同义，但按「无进展」而非「整帧总时长」
# 计（每收到一块字节即续期），避免大帧在慢链路上被误杀。
FRAME_STALL = 30.0
# 发帧超时与读轮询分开：socket 上的超时是【收发共用】的，直接沿用 RECV_POLL
# 会让超过 0.5s 才发完的帧（3.5MB SPA 主包这类）抛 socket.timeout，send_frame
# 随即把整条隧道判死 → 手机加载大资源时隧道反复拆建 → 白屏。大帧在公网上
# 发送需要更长时间，单列一个大超时。
SEND_TIMEOUT = 60.0

_TOK_ID_RE = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')


def _tok_ok(s, lo=1, hi=64):
    return isinstance(s, str) and lo <= len(s) <= hi and all(c in _TOK_ID_RE for c in s)


class RelayAuthError(Exception):
    """注册被服务器明确拒绝（HTTP 401/403）——共享密钥错/未授权。
    这是确定性配置错误，重连无意义：区别于超时/断连等瞬时故障，
    触发后应立即 _fail 让桥报「密钥错误」，不进退避重连循环
    （否则用户只能看到「一直正在开启中」直到 RECONNECT_GIVEUP）。"""


class FrameError(Exception):
    """帧已开始却读不完整（停顿/截断/协议违例）：帧边界已被污染。

    此时绝不能继续复用该连接的残留缓冲——那会把上半帧的 payload 字节
    当成下一帧的帧头，帧边界彻底错位，上层 JSON.parse 遂报
    「Unterminated string in JSON」。调用方必须丢弃整条连接。"""


# ---------------- WebSocket 帧（RFC6455，客户端侧必须掩码） ----------------
def _mask_payload(data, mask):
    """RFC6455 掩码。大整数一次异或，MB 级帧比逐字节生成器快两个数量级——
    掩码是发大帧前的必经步骤，慢在这里会直接顶到发送超时。"""
    n = len(data)
    if not n:
        return b''
    m = (bytes(mask) * (n // 4 + 1))[:n]
    return (int.from_bytes(data, 'big') ^ int.from_bytes(m, 'big')).to_bytes(n, 'big')


def _ws_frame(opcode, payload, mask=True):
    b0 = 0x80 | opcode
    ln = len(payload)
    mbit = 0x80 if mask else 0x00
    if ln < 126:
        head = bytes([b0, mbit | ln])
    elif ln < 65536:
        head = bytes([b0, mbit | 126]) + struct.pack('>H', ln)
    else:
        head = bytes([b0, mbit | 127]) + struct.pack('>Q', ln)
    if not mask:
        return head + payload
    mk = secrets.token_bytes(4)
    return head + mk + _mask_payload(payload, mk)


def _send_all(sock, data):
    sock.sendall(data)


class _WSock:
    """一条已建立的 WS 连接：同步读帧 + 线程安全写帧。"""

    def __init__(self, sock, masked_out):
        self.sock = sock
        self.masked_out = masked_out     # True=发帧掩码（worker→VPS）
        self.wlock = threading.Lock()
        self.rbuf = bytearray()
        self.alive = True

    def _set_timeout(self, t):
        try:
            if self.sock.gettimeout() != t:
                self.sock.settimeout(t)
        except Exception:
            pass

    def send_frame(self, opcode, payload=b''):
        if not self.alive:
            return False
        try:
            data = _ws_frame(opcode, payload, mask=self.masked_out)
            with self.wlock:
                self._set_timeout(SEND_TIMEOUT)
                try:
                    self.sock.sendall(data)
                finally:
                    self._set_timeout(RECV_POLL)
            return True
        except Exception:
            self.alive = False
            return False

    def send_json(self, msg):
        return self.send_frame(1, json.dumps(msg, ensure_ascii=False).encode('utf-8'))

    def _recv_n(self, n, stall=None):
        """读满 n 字节；按「无进展停顿」计时（每收到新字节即续期）。

        返回 bytes；失败返回 None——失败时一并置 alive=False（连接不可复用）。
        注意：失败时【不清空 rbuf】，由调用方 read_frame 抛 FrameError 让
        上层丢弃整条连接；绝不让残留半帧字节被当成下一帧的帧头。"""
        if stall is None:
            stall = FRAME_STALL           # 每次调用读模块常量，保持可调/可测
        last = time.monotonic()
        while len(self.rbuf) < n:
            if not self.alive:
                return None
            if time.monotonic() - last > stall:
                self.alive = False
                return None
            try:
                # 读与写共用一个 socket 超时；发送路径会临时调大它，这里每次
                # recv 前复位为轮询步长，保证读不会长时间阻塞、stop 能及时生效。
                self._set_timeout(RECV_POLL)
                chunk = self.sock.recv(min(65536, n - len(self.rbuf)))
            except socket.timeout:
                continue                       # 无进展，等 stall 判死
            except Exception:
                self.alive = False
                return None
            if not chunk:
                self.alive = False
                return None
            self.rbuf += chunk
            last = time.monotonic()            # 有进展：续期
        out = bytes(self.rbuf[:n])
        del self.rbuf[:n]
        return out

    def read_frame(self, timeout):
        """读一帧。

        成功 -> (fin, opcode, payload)
        帧【尚未开始】且空闲超时 -> None（未消费任何字节，调用方可继续轮询）
        帧【已开始】却读不完整 / 协议违例 -> FrameError（必须丢弃该连接）

        timeout 仅约束「帧间空闲等待」（轮询唤醒，让 stop 能及时生效）；
        帧一旦开始解析即改用 FRAME_STALL（无进展停顿上限），故大帧在慢链路
        上不会被 0.5s 轮询步长误杀，半帧缓冲也绝不会被复用。"""
        # 1) 帧间空闲等待：只等第一个字节
        if not self.rbuf:
            deadline = time.monotonic() + timeout
            while not self.rbuf:
                if not self.alive:
                    return None
                if time.monotonic() > deadline:
                    return None                  # 干净的空闲超时
                try:
                    self._set_timeout(RECV_POLL)
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    continue
                except Exception:
                    self.alive = False
                    return None
                if not chunk:
                    self.alive = False
                    return None
                self.rbuf += chunk
        # 2) 帧已开始：此后任何不完整都意味着帧边界被污染，丢弃连接
        head = self._recv_n(2)
        if head is None:
            raise FrameError('帧头不完整')
        b0, b1 = head[0], head[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        if b0 & 0x70 or opcode not in (0, 1, 2, 8, 9, 10):
            raise FrameError('帧头非法 opcode/保留位')
        ln = b1 & 0x7F
        if opcode >= 8 and (not fin or ln > 125):
            raise FrameError('控制帧非法')
        if ln == 126:
            ext = self._recv_n(2)
            if ext is None:
                raise FrameError('扩展长度不完整')
            ln = struct.unpack('>H', ext)[0]
        elif ln == 127:
            ext = self._recv_n(8)
            if ext is None:
                raise FrameError('扩展长度不完整')
            ln = struct.unpack('>Q', ext)[0]
            if ln < 65536:
                raise FrameError('非最短长度编码')
        if ln > MAX_WS_MESSAGE:
            raise FrameError('帧超长')
        mask = None
        if masked:
            mask = self._recv_n(4)
            if mask is None:
                raise FrameError('掩码键不完整')
        payload = self._recv_n(ln) if ln else b''
        if payload is None:
            raise FrameError('payload 不完整')
        if mask is not None:
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
        return fin, opcode, payload

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------- 本机桥 HTTP 转发 ----------------
def _bridge_http(port, method, path, headers, body, host_header):
    """对 127.0.0.1:<port> 发一次 HTTP，返回 (status, headers_list, body)。
    host_header：桥端 Host/Origin 校验期望的 'host:port'（公网 origin 的
    host 部分）——由 RelayClient 按当前配置传入，不再读模块常量。"""
    sock = socket.create_connection(('127.0.0.1', port), timeout=CONNECT_TIMEOUT)
    sock.settimeout(HTTP_TIMEOUT)
    try:
        req = bytearray()
        req += ('%s %s HTTP/1.1\r\n' % (method, path)).encode('latin-1')
        req += ('Host: %s\r\n' % host_header).encode('latin-1')
        sent_cl = False
        for k, v in (headers or {}).items():
            lk = k.lower()
            if lk in ('host', 'connection', 'content-length', 'transfer-encoding',
                      'x-relay-token', 'sec-websocket-key', 'sec-websocket-version',
                      'sec-websocket-extensions', 'upgrade'):
                continue
            # 剥转发注入/逐跳头，桥端 Host/Origin 校验基于公网 origin
            if lk.startswith('x-forwarded-') or lk.startswith('cf-'):
                continue
            req += ('%s: %s\r\n' % (k, v)).encode('latin-1')
        req += ('Content-Length: %d\r\n' % len(body)).encode('latin-1')
        req += b'Connection: close\r\n\r\n'
        req += body
        sock.sendall(bytes(req))
        # 读响应头
        head = bytearray()
        while b'\r\n\r\n' not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError('bridge closed')
            head += chunk
            if len(head) > HEADER_MAX:
                raise OSError('head too large')
        head_b, rest = bytes(head).split(b'\r\n\r\n', 1)
        lines = head_b.split(b'\r\n')
        status = int(lines[0].split(b' ', 2)[1])
        rheaders = []
        rmap = {}
        for line in lines[1:]:
            name, sep, value = line.partition(b':')
            if not sep:
                continue
            n_ = name.decode('latin-1').strip()
            v_ = value.decode('latin-1').strip()
            rheaders.append((n_, v_))
            rmap[n_.lower()] = v_
        # 读响应体：Content-Length 或读到 close
        out = bytearray(rest)
        cl = rmap.get('content-length')
        if cl is not None:
            need = int(cl)
            while len(out) < need:
                chunk = sock.recv(min(65536, need - len(out)))
                if not chunk:
                    break
                out += chunk
            out = out[:need]
        else:
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                out += chunk
                if len(out) > MAX_HTTP_BODY:
                    break
        return status, rheaders, bytes(out)
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ---------------- 本机桥 WS 转发 ----------------
def _bridge_ws(port, path, headers, host_header):
    """对本机桥发起 WS 升级，返回已握手完成的 socket；失败返回 None。
    host_header 同 _bridge_http。"""
    try:
        sock = socket.create_connection(('127.0.0.1', port), timeout=CONNECT_TIMEOUT)
        sock.settimeout(REGISTER_TIMEOUT)
        key = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
        req = ('GET %s HTTP/1.1\r\n'
               'Host: %s\r\n'
               'Upgrade: websocket\r\n'
               'Connection: Upgrade\r\n'
               'Sec-WebSocket-Key: %s\r\n'
               'Sec-WebSocket-Version: 13\r\n'
               % (path, host_header, key))
        # 透传桥裁决所需头（Origin / Sec-Fetch-* / Cookie / Sec-WebSocket-Protocol）
        for k, v in (headers or {}).items():
            lk = k.lower()
            if lk in ('host', 'connection', 'upgrade', 'sec-websocket-key',
                      'sec-websocket-version', 'content-length', 'x-relay-token'):
                continue
            req += '%s: %s\r\n' % (k, v)
        req += '\r\n'
        sock.sendall(req.encode('latin-1'))
        head = bytearray()
        while b'\r\n\r\n' not in head:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                return None
            head += chunk
            if len(head) > HEADER_MAX:
                sock.close()
                return None
        head_b, rest = bytes(head).split(b'\r\n\r\n', 1)
        lines = head_b.split(b'\r\n')
        if not lines[0].startswith(b'HTTP/1.') or b' 101' not in lines[0]:
            sock.close()
            return None
        # 握手后把已读到的 rest 交还给帧读循环（不丢首帧）
        sock.settimeout(RECV_POLL)
        return sock, rest
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        return None


def _pump_ws_to_tunnel(bridge_sock, tun_ws, cid, initial_rest):
    """本机桥→隧道方向：把桥来的帧原样封进 ws_data。

    _WSock 包的是「到本机桥」的连接：本模块对桥而言是 WS 客户端，发向桥的
    帧必须掩码（masked_out=True）；桥回来的帧是服务端语义（不掩码），
    read_frame 按对端形态解析。"""
    ws = _WSock(bridge_sock, masked_out=True)
    ws.rbuf = bytearray(initial_rest or b'')
    try:
        while tun_ws.alive and ws.alive:
            try:
                frame = ws.read_frame(RECV_POLL)
            except FrameError:
                # 帧边界已污染：整条桥连接作废，绝不带着半帧缓冲继续读
                break
            if frame is None:
                if not ws.alive or not tun_ws.alive:
                    break
                # 帧间空闲超时：只要连接活着就继续（RECV_POLL 是轮询步长）
                continue
            fin, opcode, payload = frame
            if opcode == 9:      # 桥 ping → 回 pong（本模块是桥的客户端，帧仍掩码）
                ws.send_frame(10, payload)
                continue
            if opcode == 8:
                break
            ok = tun_ws.send_json({'type': 'ws_data', 'id': cid, 'opcode': opcode,
                                   'fin': fin,
                                   'payload': base64.b64encode(payload).decode('ascii')})
            if not ok:
                break
    finally:
        try:
            tun_ws.send_json({'type': 'ws_close', 'id': cid})
        except Exception:
            pass
        ws.close()


# ---------------- RelayClient ----------------
class RelayClient:
    """与 ConnectorRuntime 同形：start(bound_port, on_ready, on_fail)。

    全部可配置参数经构造注入；默认即「未配置」的内置回退。public_host_header
    不传时由 relay_host + public_port 派生（桥 Host/Origin 校验期望的
    'host:port'）——relay_server 的 --public-host 须与之吻合，否则注册到的
    origin 与桥白名单不匹配。"""

    def __init__(self, relay_host=RELAY_HOST, relay_port=RELAY_PORT,
                 public_port=RELAY_PUBLIC_PORT, token=None,
                 public_host_header=None):
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.public_port = public_port
        if isinstance(public_host_header, str) and public_host_header.strip():
            self.public_host_header = public_host_header.strip()
        else:
            self.public_host_header = '%s:%d' % (relay_host, public_port)
        if token is None:
            try:
                import os
                token = os.environ.get('KIMI_RELAY_TOKEN') or DEFAULT_RELAY_TOKEN
            except Exception:
                token = DEFAULT_RELAY_TOKEN
        self._token = token
        self._lock = threading.RLock()
        self._closing = False
        self._started = False
        self._thread = None
        self._ws = None            # _WSock 到 VPS
        self.tunnel_id = ''        # 注册成功后由回调线程置位
        self.public_origin = ''    # 'http://<vps>:<public_port>'（无 /t/<id>）
        self._bound_port = 0
        self._on_ready = None
        self._on_fail = None
        self._ws_channels = {}     # cid -> bridge socket（本机桥连接）
        self._ws_lock = threading.Lock()
        self._connected_once = False  # 本轮 _run_once 是否成功走到 _ready

    # ---------- 生命周期 ----------
    def start(self, bound_port, on_ready, on_failure):
        with self._lock:
            if self._closing or self._started:
                return
            self._started = True
            self._bound_port = bound_port
            self._on_ready = on_ready
            self._on_fail = on_failure
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        """同步停当前隧道（允许之后再 start 新一轮）。"""
        with self._lock:
            ws = self._ws
            self._ws = None
            self._started = False
        if ws is not None:
            try:
                ws.send_frame(8, b'')
            except Exception:
                pass
            ws.close()
        self._close_channels()

    def shutdown(self):
        with self._lock:
            self._closing = True
        self.stop()

    def status(self):
        with self._lock:
            return {'state': 'ready' if (self._ws and self._ws.alive) else 'off',
                    'tunnel_id': self.tunnel_id}

    # ---------- 内部 ----------
    def _close_channels(self):
        with self._ws_lock:
            chans = list(self._ws_channels.values())
            self._ws_channels.clear()
        for s in chans:
            try:
                s.close()
            except Exception:
                pass

    def _fail(self, code):
        cb = None
        with self._lock:
            cb = self._on_fail
        if cb is not None:
            try:
                cb(code)
            except Exception:
                pass

    def _ready(self, origin):
        cb = None
        with self._lock:
            cb = self._on_ready
        if cb is not None:
            try:
                cb(origin)
            except Exception:
                pass

    def _run(self):
        """主循环：注册 → 处理隧道消息 → 断线自动重连（指数退避）。

        断线不再立即判失败：网络抖动/VPS 重启时原地重连，已配对会话与
        本机桥端口不动。仅当主动 stop/shutdown（_closing 或 _started 清
        位）才退出；连续 RECONNECT_GIVEUP 秒仍连不上才回调
        _fail('TUNNEL_EXITED') 让桥 teardown。
        """
        if not isinstance(self.relay_host, str) or not self.relay_host.strip():
            # 未配置中继服务器地址（relay.json host 为空/非法被清洗成 ''）：
            # 确定性配置错误，拿空串去 create_connection 只会白重连到
            # RECONNECT_GIVEUP。直接拒绝并回报 TUNNEL_AUTH_FAILED——现有
            # 白名单码，前端文案指向「中继服务器配置」，且不污染
            # _MOBILE_ERROR_CODES / mobile_worker 合同。
            with self._lock:
                self._started = False
            self._fail('TUNNEL_AUTH_FAILED')
            return
        backoff = RECONNECT_BASE
        dead_since = None
        while True:
            with self._lock:
                self._connected_once = False
            try:
                self._run_once()
            except RelayAuthError:
                # 服务器明确拒绝（401/403 密钥错/未授权）：确定性失败，
                # 不重连，立即回调让桥报「密钥错误」、状态转 failed。
                with self._lock:
                    self._started = False
                self._fail('TUNNEL_AUTH_FAILED')
                return
            except Exception:
                pass
            self._close_channels()
            with self._lock:
                ws = self._ws
                self._ws = None
                closing = self._closing or not self._started
                connected = self._connected_once
            if ws is not None:
                ws.close()
            if closing:
                return
            if connected:
                # 本轮曾连上又掉：视作新一段故障，退避与计时复位。
                backoff = RECONNECT_BASE
                dead_since = None
            now = time.monotonic()
            if dead_since is None:
                dead_since = now
            elif now - dead_since >= RECONNECT_GIVEUP:
                with self._lock:
                    self._started = False
                self._fail('TUNNEL_EXITED')
                return
            time.sleep(backoff)
            backoff = min(backoff * 2.0, RECONNECT_MAX)
            with self._lock:
                if self._closing or not self._started:
                    return


    def _run_once(self):
        # 1) TCP + WS 握手 + X-Relay-Token 注册
        sock = socket.create_connection((self.relay_host, self.relay_port),
                                        timeout=CONNECT_TIMEOUT)
        sock.settimeout(REGISTER_TIMEOUT)
        key = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
        resume = ''
        with self._lock:
            resume = self.tunnel_id or ''
        resume_hdr = ('X-Relay-Resume: %s\r\n' % resume) if resume else ''
        req = ('GET /relay/register HTTP/1.1\r\n'
               'Host: %s:%d\r\n'
               'Upgrade: websocket\r\n'
               'Connection: Upgrade\r\n'
               'Sec-WebSocket-Key: %s\r\n'
               'Sec-WebSocket-Version: 13\r\n'
               'X-Relay-Token: %s\r\n'
               '%s'
               '\r\n' % (self.relay_host, self.relay_port, key, self._token,
                        resume_hdr))
        sock.sendall(req.encode('latin-1'))
        head = bytearray()
        while b'\r\n\r\n' not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError('register closed')
            head += chunk
            if len(head) > HEADER_MAX:
                raise OSError('register head too large')
        head_b, rest = bytes(head).split(b'\r\n\r\n', 1)
        status_line = head_b.split(b'\r\n', 1)[0]
        if b' 101' not in status_line:
            # 401/403 = 服务器明确拒绝（共享密钥错/未授权）：确定性配置错误，
            # 重连只会原地打转，立刻失败让桥报「密钥错误」而非卡「开启中」。
            parts = status_line.split(b' ')
            code = parts[1] if len(parts) > 1 else b''
            if code in (b'401', b'403'):
                raise RelayAuthError('register refused: HTTP '
                                     + code.decode('ascii', 'replace'))
            raise OSError('register refused')
        sock.settimeout(RECV_POLL)
        ws = _WSock(sock, masked_out=True)
        ws.rbuf = bytearray(rest)
        with self._lock:
            if self._closing:
                ws.close()
                return
            self._ws = ws

        # 2) 等 registered
        deadline = time.monotonic() + REGISTER_TIMEOUT
        origin = ''
        while time.monotonic() < deadline:
            try:
                frame = ws.read_frame(RECV_POLL)
            except FrameError:
                raise OSError('register frame broken')
            if frame is None:
                if not ws.alive:
                    raise OSError('register read failed')
                continue
            fin, opcode, payload = frame
            if opcode == 8:
                raise OSError('register close')
            if opcode != 1:
                continue
            try:
                msg = json.loads(payload.decode('utf-8'))
            except Exception:
                continue
            if msg.get('type') == 'registered':
                self.tunnel_id = str(msg.get('tunnel_id') or '')
                # origin 形如 'http://<vps>:<port>/t/<id>'；桥只存不带 /t/ 的公网
                # origin（Host/Origin 校验用），tid 经 self.tunnel_id 另取。
                full = str(msg.get('origin') or '')
                if '/t/' in full:
                    origin = full.split('/t/', 1)[0]
                else:
                    origin = full
                self.public_origin = origin
                break
        if not origin:
            raise OSError('no registered')

        # 3) 通知桥就绪
        with self._lock:
            self._connected_once = True
        self._ready(origin)

        # 4) 主读循环：http / ws_open / ws_data / ws_close / ping
        last_ping = time.monotonic()
        while ws.alive:
            with self._lock:
                if self._closing or self._ws is not ws:
                    break
            # 保活 ping
            if time.monotonic() - last_ping >= PING_INTERVAL:
                if not ws.send_frame(9, b''):
                    break
                last_ping = time.monotonic()
            try:
                frame = ws.read_frame(RECV_POLL)
            except FrameError:
                # 隧道帧边界污染：断掉本连接，交外层退避重连（新连接新缓冲）
                break
            if frame is None:
                if not ws.alive:
                    break
                continue
            fin, opcode, payload = frame
            if opcode == 8:
                break
            if opcode == 10:      # pong
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
            if mtype == 'http':
                threading.Thread(target=self._serve_http,
                                 args=(ws, msg), daemon=True).start()
            elif mtype == 'ws_open':
                self._ws_open(ws, msg)
            elif mtype in ('ws_data', 'ws_close'):
                self._ws_dispatch(msg)
            elif mtype == 'ping':
                ws.send_json({'type': 'pong'})

    def _serve_http(self, ws, msg):
        """把一条公网 HTTP 请求转给本机桥，回传 http_resp。"""
        rid = msg.get('id')
        try:
            body = base64.b64decode(msg.get('body') or '')
            if len(body) > MAX_HTTP_BODY:
                raise ValueError('body')
            status, rheaders, rbody = _bridge_http(
                self._bound_port, str(msg.get('method') or 'GET'),
                str(msg.get('path') or '/'),
                msg.get('headers') or {}, body, self.public_host_header)
            resp = {'type': 'http_resp', 'id': rid, 'status': status,
                    'headers': rheaders,
                    'body': base64.b64encode(rbody).decode('ascii')}
        except Exception:
            resp = {'type': 'http_resp', 'id': rid, 'status': 502,
                    'headers': [], 'body': ''}
        ws.send_json(resp)

    def _ws_open(self, ws, msg):
        cid = msg.get('id')
        try:
            got = _bridge_ws(self._bound_port, str(msg.get('path') or '/'),
                             msg.get('headers') or {}, self.public_host_header)
            if got is None:
                ws.send_json({'type': 'ws_close', 'id': cid})
                return
            bsock, rest = got
            with self._ws_lock:
                self._ws_channels[cid] = bsock
            threading.Thread(target=_pump_ws_to_tunnel,
                             args=(bsock, ws, cid, rest), daemon=True).start()
        except Exception:
            try:
                ws.send_json({'type': 'ws_close', 'id': cid})
            except Exception:
                pass

    def _ws_dispatch(self, msg):
        """隧道来的 ws_data/ws_close → 落到对应本机桥 socket。"""
        cid = msg.get('id')
        with self._ws_lock:
            bsock = self._ws_channels.get(cid)
        if bsock is None:
            return
        if msg['type'] == 'ws_close':
            try:
                bsock.close()
            except Exception:
                pass
            with self._ws_lock:
                self._ws_channels.pop(cid, None)
            return
        try:
            opcode = int(msg.get('opcode', 1))
            payload = base64.b64decode(msg.get('payload') or '')
            # worker→桥方向是 WS 服务端→客户端：桥读的是未掩码帧？不——桥侧
            # _ws_tunnel 里 upstream 是手机→桥的 socket（桥当服务端），本模块
            # 是它的「手机」对等端：发给桥的帧必须掩码（客户端语义）。
            frame = _ws_frame(opcode, payload, mask=True)
            bsock.sendall(frame)
        except Exception:
            try:
                bsock.close()
            except Exception:
                pass
            with self._ws_lock:
                self._ws_channels.pop(cid, None)
