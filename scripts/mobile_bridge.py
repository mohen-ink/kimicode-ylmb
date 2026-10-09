# -*- coding: utf-8 -*-
"""
Kimi Code 用量面板 · 手机浏览器桥接（同桌面 owner，显式开启）

本机控制面 /api/mobile/* 仅接受 loopback、严格 Host、自定义控制头与可信
Origin；手机面不暴露控制面。LAN 显式绑定所选本机 RFC1918 IPv4，明文 HTTP
只适合可信局域网。internet 显式同意 Cloudflare 中继后只绑定 127.0.0.1，
ConnectorRuntime 确认 Quick Tunnel 就绪且 owner 复核通过后才发布 HTTPS 配对链接。

配对码只在 URL fragment，限时且一次性；会话 Cookie 为 HttpOnly/Strict，
internet 额外设置 Secure。HTTP 精确 method+route allowlist 与 WS 上行消息
allowlist 保持机器级接口不可达。真实 server.token 仅在进程内存向已核实的
owner 注入；HTTP 响应与 WS 完整帧/重组消息均过滤凭据，不向手机发送。

每次启动、listener、handler、会话、授权快照、socket 与 WS 配额均绑定同代；
stop 立即吊销并关闭全部连接，owner 失联与隧道失败均 fail-closed。
shutdown 永久关闭管理器及 connector。本模块只依赖 Python>=3.8 标准库。
"""
import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from email.errors import StartBoundaryNotFoundDefect, MultipartInvariantViolationDefect
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote, quote

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

try:
    import mobile_usage as _mobile_usage
except ImportError:                          # 仅本脚本被单独复制时降级
    _mobile_usage = None

CONTROL_PREFIX = '/api/mobile'
RELAY_CONSENT_VERSION = 'cloudflare-quick-2026-09-v1'
# 公开错误码白名单。WORKER_* 是 mobile_worker spawn/ensure 的固定阶段码
# （与 _SPAWN_FAILURE_CODES 取值一致），只透出固定码本身，不含任何诊断细节。
_MOBILE_ERROR_CODES = frozenset((
    'CONNECTOR_MISSING', 'CONNECTOR_INSTALL_FAILED', 'CONNECTOR_HASH_MISMATCH',
    'CONNECTOR_UNSUPPORTED', 'CONNECTOR_BUSY', 'CONSENT_REQUIRED',
    'TUNNEL_START_FAILED', 'TUNNEL_TIMEOUT', 'TUNNEL_EXITED', 'OWNER_LOST',
    'TUNNEL_AUTH_FAILED', 'START_CANCELLED', 'SERVER_TOKEN_UNAVAILABLE',
    'WORKER_STATE_DIR_UNAVAILABLE', 'WORKER_STARTUP_BUSY', 'WORKER_LOCK_HELD',
    'WORKER_SPAWN_DENIED', 'WORKER_SPAWN_FAILED', 'WORKER_CHILD_EXITED',
    'WORKER_BOOT_TIMEOUT', 'WORKER_VERSION_MISMATCH',
))
BODY_DEADLINE_SECONDS = 15.0      # 控制面读体累计上限（控制面不放宽）
HEADER_DEADLINE_SECONDS = 15.0
# 文件上传（POST /api/v1/files）体读取的有界策略：只此一条路由放宽到
# 可容纳蜂窝慢速上行，仍有硬上界——停滞（超过 IDLE 无新字节）或累计超过
# MAX 即 fail-closed；控制面与其它路径保持 BODY_DEADLINE_SECONDS。
UPLOAD_BODY_IDLE_SECONDS = 120.0
UPLOAD_BODY_MAX_SECONDS = 600.0
INTERNET_RATE_BURST = 20.0
INTERNET_RATE_PER_SECOND = 1.0
INTERNET_RATE_BUCKETS_MAX = 1024
# 已配对会话独立限流：'paired:global' + 每 sid 一桶；匿名仍走
# 'global'+'ip:'，互不清空连坐（bucket 值 = (tokens,last,burst,rate)）
PAIRED_GLOBAL_RATE_BURST = 600.0
PAIRED_GLOBAL_RATE_PER_SECOND = 30.0
PAIRED_SID_RATE_BURST = 120.0
PAIRED_SID_RATE_PER_SECOND = 8.0
# 手机 POST /api/v1/config：仅这几个白名单键严格校验后原样转发
CONFIG_POST_MAX_BODY = 8192
_CONFIG_POST_BOOL_KEYS = frozenset(
    ('auto_session_title', 'default_plan_mode'))
# thinking 嵌套域只放 enabled/effort：keep 无前端必要、forcedEffort 属 env 强制
_CONFIG_POST_THINKING_KEYS = frozenset(('enabled', 'effort'))
# native effort 输入词汇（minimal 只是 Gemini 输出 ThinkingLevel，非输入值）
_CONFIG_POST_EFFORTS = frozenset(
    ('off', 'on', 'low', 'medium', 'high', 'xhigh', 'max'))


def _read_with_deadline(handler, size, seconds=BODY_DEADLINE_SECONDS, idle=None):
    """按累计 deadline 读满 size 字节；idle 给定时同时限制「两次到达之间的等待」
    （蜂窝慢速上行可用大 idle + 大 total 放宽，停滞仍被有界打断）。"""
    deadline = time.monotonic() + seconds
    connection = handler.connection
    old_timeout = connection.gettimeout()
    out = bytearray()
    try:
        while len(out) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MobileBridgeError('请求体读取超时')
            connection.settimeout(remaining if idle is None else min(idle, remaining))
            try:
                chunk = handler.rfile.read1(min(size - len(out), 65536))
            except socket.timeout:
                raise MobileBridgeError('请求体读取超时')
            if not chunk:
                raise MobileBridgeError('请求体不完整')
            out.extend(chunk)
        if time.monotonic() > deadline:
            raise MobileBridgeError('请求体读取超时')
        return bytes(out)
    finally:
        connection.settimeout(old_timeout)

class _HeaderDeadlineReader(object):
    def __init__(self, source, connection):
        self.source = source
        self.connection = connection
        self.old_timeout = connection.gettimeout()
        self.deadline = time.monotonic() + HEADER_DEADLINE_SECONDS

    def readline(self, size=-1):
        out = bytearray()
        while size < 0 or len(out) < size:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout('request headers timed out')
            self.connection.settimeout(remaining)
            available = self.source.peek(1)
            if not available:
                break
            count = len(available) if size < 0 else min(len(available), size - len(out))
            newline = available.find(b'\n', 0, count)
            if newline >= 0:
                count = newline + 1
            chunk = self.source.read1(count)
            if not chunk:
                break
            out.extend(chunk)
            if chunk.endswith(b'\n'):
                break
        if time.monotonic() > self.deadline:
            raise socket.timeout('request headers timed out')
        return bytes(out)

    def __getattr__(self, name):
        return getattr(self.source, name)


LAN_PREFERRED_PORT = 39282
LAN_PORT_SCAN_MAX = 100          # 39282 起最多向上扫 100 个端口
PAIR_TTL_SECONDS = 600           # 配对 token 10 分钟
SESSION_TTL_SECONDS = 86400      # 会话 idle TTL 24h（滑动，兼容别名）
SESSION_MAX_AGE_SECONDS = 604800 # 会话绝对寿命 7d（Cookie Max-Age 也按此签发）
MAX_DEVICES = 8
CONTROL_MAX_BODY = 8192
CONTROL_DRAIN_CAP = 64 * 1024    # 控制面错误前排空上限：更大 body 直接弃连不读
PROXY_MAX_REQUEST_BODY = 32 * 1024 * 1024   # 上传文件等正常体积内放行
PROXY_MAX_RESPONSE_BODY = 128 * 1024 * 1024
PROXY_UPSTREAM_TIMEOUT = 45
WS_UPGRADE_MAX_HEADER = 16384
EXCHANGE_MAX_FAILS = 5           # 每 IP 60s 内失败 5 次 → 禁 120s
EXCHANGE_FAIL_WINDOW = 60.0
EXCHANGE_BAN_SECONDS = 120.0
EXCHANGE_FAILS_MAX = 1024        # 失败桶硬上限，溢出先清过期再逐最旧
OWNER_WATCH_INTERVAL = 2.0       # owner pid 失联巡检周期
OWNER_LOST_GRACE = 3             # 连续 N 次巡检失败才判 OWNER_LOST——健康检查
                               # （healthz 2.5s 超时）可因 owner 瞬时卡顿假阴，
                               # 单次失败即 teardown 会把隧道+全部会话误杀。
MAX_LAN_CONNECTIONS = 64         # LAN 面并发连接上限（process_request 前获取）
MAX_WS_TOTAL = 32                # WS 隧道全局上限
MAX_WS_PER_SESSION = 4           # 单会话（cookie SID）WS 上限
DRAIN_TIMEOUT = 1.0              # 错误前请求体排空有界：最多等 1s
WS_SESSION_RECHECK_SECONDS = 1.0 # 已建立 WS 的会话有效期复查周期
WS_RECV_POLL = 0.5               # WS 双向 recv 有界轮询：stop/会话失效须唤醒
WS_FRAME_DEADLINE = 10.0         # 单个 WS 帧（头→payload 完整）累计到达上限
KA_IDLE_TIMEOUT = 5.0            # keep-alive 空闲连接占用 conn 名额的上限秒数

# 发给手机的占位凭据：SPA 经 #token 存库后用其做 Bearer / kimi-code.bearer.* 子协议。
# 值本身无任何权限——桥在转发时剥掉、向 owner 注入真实 token。
PLACEHOLDER_CREDENTIAL = 'kimi-mobile-lan'
SESSION_COOKIE = 'kimi_mb'
_WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
_RFC1918 = (ipaddress.ip_network('10.0.0.0/8'),
            ipaddress.ip_network('172.16.0.0/12'),
            ipaddress.ip_network('192.168.0.0/16'))
_TRUSTED_APP_ORIGIN = 'app://renderer'

# 机器级 / 越权面：任何方法都不放行（按路径段精确匹配，防尾缀绕过）
_API_DENY_PREFIXES = (
    '/api/v1/debug', '/api/v1/shutdown', '/api/v1/remote-control',
    '/api/v1/authority', '/api/v1/providers', '/api/v1/plugins',
    '/api/v1/mcp', '/api/v1/oauth', '/api/v1/connections',
    '/api/v1/gui-store', '/api/v1/runtime', '/api/v1/ws',
    '/api/v1/fs',      # 覆盖 /api/v1/fs::browse /fs::content 等全部 fs: 动作
    '/api/oauth',
)
# 只读面（GET/HEAD）：SPA 渲染与侧边栏引导所需的必要读
_API_READ_ONLY = (
    '/api/v1/healthz', '/api/v1/meta', '/api/v1/models',
    '/api/v1/workspaces', '/api/v1/skills', '/api/v1/tools',
    '/api/v1/capabilities', '/api/v1/search', '/api/v1/media',
    '/api/v1/auth',        # models_ready 探测，非 secret
    '/api/v1/config',      # 原生已脱敏；POST 见 _route 的 _config_post 白名单分支
)
# v2 只放行主侧边栏 boot 的那一条读，且必须精确匹配：
# /api/v2/sessions/…（子资源）与 /api/v2/sessions:<action> 是 v2 重启后的
# 归档/恢复写面，前缀放行会被顺带打开。
_V2_READ_EXACT = ('/api/v2/sessions',)
# v2 仅放行这两条精确写动作（批量归档/恢复，桌面侧栏多选归档走这套路由）；
# 不做前缀匹配，v2 其它写面（含 :delete 等）与子资源一律拒绝。
_V2_WRITE_EXACT = frozenset(('/api/v2/sessions:archive', '/api/v2/sessions:restore'))
# 非 /api/ 静态面白名单（收紧）：SPA index 与 native 客户端路由 + /assets/
# 下的构建产物。任何其它非 API 路径都不再转发给 owner——否则等于给攻击者
# 一个「任意路径打到本机 owner」的转发器。
_STATIC_EXACT = frozenset(('/', '/index.html', '/favicon.ico'))
_STATIC_ASSET_PREFIX = '/assets/'
_STATIC_ASSET_RE = re.compile(r'[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)*')
_SPA_EXACT = frozenset(('/admin/sessions',))
_SPA_ONE_SEGMENT_PREFIXES = ('/sessions/', '/devices/')


def _static_allowed(path):
    """非 /api/ 面的只读白名单：SPA index、构建产物与 native 客户端路由。

    收紧前凡是 GET 的非 /api/ 路径都会被转发给 owner，等于把一个任意路径
    转发器交给手机端。此处只放行确知存在的静态面，其余一律不转发。
    """
    if path in _STATIC_EXACT:
        return True
    if path.startswith(_STATIC_ASSET_PREFIX):
        rest = path[len(_STATIC_ASSET_PREFIX):]
        if not rest or rest.endswith('/'):
            return False
        return bool(_STATIC_ASSET_RE.fullmatch(rest))
    if path in _SPA_EXACT:
        return True
    for prefix in _SPA_ONE_SEGMENT_PREFIXES:
        if path.startswith(prefix):
            rest = path[len(prefix):]
            return bool(rest) and '/' not in rest
    return False
# 读写面仅 /api/v1/sessions 与 /api/v1/files（外加 _config_post 的
# POST /api/v1/config 白名单特例），但都不做前缀放行——
# 见 _session_api_allowed / _files_api_allowed 的精确 method+route 表。
# 剥插件注入的 <script>：src 位置任意、单/双引号、可有 defer/async/其他属性、
# 可自闭合；不带 src 的 inline 与 native module 不受影响（须含已知资产名）。
_INJECT_TAG_RE = re.compile(
    r'\s*<script\b[^>]*?\bsrc\s*=\s*["\']/assets/(?:vendor/qrcodegen'
    r'|kimi-(?:embedded|usage)-(?:data|widget)|kimi-remote-(?:qr|api|widget)'
    r'|kimi-mobile-api)\.js[^"\']*["\'][^>]*>(?:</script>)?\s*',
    re.IGNORECASE)

# ---------- sessions 精确路由表（对照 native kap-server 路由） ----------
# sessions/{id} 下已知的 GET 子段（native GET 路由，读面）；
# 不在表内的子段（export/runtime 等 native POST-only）GET 也一并拒。
_SESSION_GET_SEGMENTS = frozenset((
    'approvals', 'children', 'file-history', 'fs', 'goal', 'media',
    'messages', 'profile', 'prompts', 'questions', 'runtime', 'skills',
    'snapshot', 'status', 'tasks', 'transcript', 'warnings',
))
# native fs 只读动作（list/read/list_many/stat/stat_many/search/grep/
# git_status/diff 实际都是 POST /sessions/{id}/fs:<action>）；
# mkdir/open/open-in/reveal 是写或桌面操作，一律拒。
_SESSION_FS_READ_ACTIONS = frozenset((
    'list', 'read', 'list_many', 'stat', 'stat_many',
    'search', 'grep', 'git_status', 'diff',
))
# session 级 :action（POST /sessions/{id}:<action>）；archive/delete 放行
# （delete 不可逆、archive 可 restore；均仅 POST，GET 仍走只读表）
_SESSION_POST_ACTIONS = frozenset(('fork', 'compact', 'undo', 'abort', 'btw',
                                   'restore', 'archive', 'delete'))
_SESSION_PROMPT_ACTIONS = frozenset(('abort', 'steer'))
_SESSION_QUESTION_ACTIONS = frozenset(('resolve', 'dismiss'))
_SESSION_TASK_ACTIONS = frozenset(('cancel', 'detach'))

_HOP_BY_HOP = frozenset((
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade', 'host',
    'forwarded', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto',
    'x-real-ip', 'cookie', 'authorization', 'origin', 'expect',
    'content-length', 'accept-encoding',
))
_RESP_HOP_BY_HOP = _HOP_BY_HOP | frozenset((
    'access-control-allow-origin', 'access-control-allow-methods',
    'access-control-allow-headers', 'access-control-allow-credentials',
    'access-control-expose-headers', 'access-control-max-age',
    'content-length', 'content-encoding', 'keep-alive',
    # owner 的凭据/身份头与跳转/内部重定向头一律剥掉，不进手机
    'set-cookie', 'www-authenticate', 'server', 'date',
    'location', 'refresh',
))
_WS_MAX_MESSAGE = 4 * 1024 * 1024   # WS 消息重组上限（文本+二进制）
_WS_TERM_PREFIX = 'terminal_'       # type 前缀拦截在解码后的 JSON 对象上做
# client→upstream 数据消息 type 白名单（native 会话协议，未知一律断隧道）
_WS_ALLOWED_TYPES = frozenset((
    'client_hello', 'subscribe', 'subscribe_v2',
    'unsubscribe', 'unsubscribe_v2', 'abort', 'pong',
))
_WS_PROTO_TOKEN_RE = re.compile(r'^kimi-code\.bearer\.[A-Za-z0-9!#$&\'*+\-.^_`|~]{1,256}$')
_TOKEN_SAFE_RE = re.compile(r'^[A-Za-z0-9_.~-]{1,256}$')
# 请求行 raw path 必须按 RFC3986 pchar/percent 编码：空白、引号、反斜杠、
# 尖括号、花括号、竖线、^、`、[] 与控制字符只允许以 %XX 形式出现
_RAW_BAD_CHAR_RE = re.compile(r'[ \t\r\n\x00-\x1f\x7f"\'<>{}|^`\\\[\]]')
_PCT_RE = re.compile(r'%[0-9A-Fa-f]{2}')
# 编码分隔符单独拒：防止解码后多出新路径段绕过路由表
_ENCODED_SEP_RE = re.compile(r'%2f|%5c', re.IGNORECASE)


# 手机用量入口：注入到原生 SPA index 的同源「弹层」组件（按钮 + 遮罩/底部
# sheet + 共享用量渲染脚本）。点击不跳页——在当前聊天页上就地展开/收起；
# 标记/样式/查询全部限定在 #kuOv/#kuBox 容器内，不污染宿主 SPA。
# 取数与跳转经 JS 端 root 前缀拼同源路径（relay 下 root=/t/<id>），不丢隧道
# 归属。mobile_usage 缺失时降级为空（连入口按钮都没有，好过留死按钮）。
_USAGE_ENTRY_HTML = (
    _mobile_usage.USAGE_OVERLAY_HTML.encode('utf-8')
    if _mobile_usage is not None else b'')

# 局域网是明文 HTTP＝非安全上下文，浏览器不提供 crypto.randomUUID，原生前端
# 渲染输入区时会抛错。仅在缺失时补一个基于 getRandomValues 的实现。
_RANDOM_UUID_SHIM = (
    b'<script>(function(){var c=window.crypto;'
    b'if(!c||typeof c.randomUUID==="function"||!c.getRandomValues)return;'
    b'c.randomUUID=function(){var b=new Uint8Array(16);c.getRandomValues(b);'
    b'b[6]=b[6]&15|64;b[8]=b[8]&63|128;var h=[];'
    b'for(var i=0;i<16;i++)h.push((b[i]+256).toString(16).slice(1));'
    b'return h.slice(0,4).join("")+"-"+h.slice(4,6).join("")+"-"'
    b'+h.slice(6,8).join("")+"-"+h.slice(8,10).join("")+"-"'
    b'+h.slice(10).join("")};})();</script>')

# ---------- M6：代理面同源可执行内容防护 ----------
# 经 /fs/*、/files/{id}、/media 取回的内容由本地工作区决定，被 agent 写成
# HTML/SVG 时浏览器会当同源文档执行——加 sandbox + attachment 降级。
# 例外：SPA 文档/native 路由与 /assets/ 产物是本站自带；桥自有的配对落地页
# 与用量页走 _send_raw，不经此处。
_GUARD_CSP = 'sandbox'
_GUARD_ATTACHMENT = 'attachment'
_SPA_DOC_PATHS = _STATIC_EXACT | _SPA_EXACT
_GUARD_CONTENT_TYPES = ('text/html', 'image/svg+xml')


def _is_spa_document(path):
    """SPA 自身文档（导航目标，不强制下载）。"""
    if path in _SPA_DOC_PATHS:
        return True
    for prefix in _SPA_ONE_SEGMENT_PREFIXES:
        rest = path[len(prefix):]
        if path.startswith(prefix) and rest and '/' not in rest:
            return True
    return False


def _executable_content_guard(path, content_type):
    """HTML/SVG 且内容来自工作区时返回强制头，否则 ()。按解码值判定，
    排白名单写法：新增透传路由默认仍受防护。"""
    if not content_type:
        return ()
    if not any(t in content_type.lower() for t in _GUARD_CONTENT_TYPES):
        return ()
    if _is_spa_document(path) or path.startswith(_STATIC_ASSET_PREFIX):
        return ()
    return (('Content-Security-Policy', _GUARD_CSP),
            ('Content-Disposition', _GUARD_ATTACHMENT))


def _merge_guard_headers(headers, guard):
    """并入强制头：同名上游头只留一个强制值（disposition 保留首条 filename），
    其余重复上游头丢弃。"""
    forced = {k.lower(): v for k, v in guard}
    done = set()
    out = []
    for k, v in headers:
        lk = k.lower()
        if lk not in forced:
            out.append((k, v))
        elif lk in done:
            continue
        elif lk == 'content-disposition':
            _, sep, params = v.partition(';')
            out.append((k, forced[lk] + (sep + params if sep else '')))
            done.add(lk)
        else:
            out.append((k, forced[lk]))
            done.add(lk)
    out.extend((k, v) for k, v in guard if k.lower() not in done)
    return out



class MobileBridgeError(Exception):
    """可回显给前端的中文安全错误。"""


# ---------------- 基础工具 ----------------
def _is_loopback_ip(host):
    try:
        ip = ipaddress.ip_address(host.strip('[]'))
        return ip.is_loopback
    except ValueError:
        return host == 'localhost'


def _is_rfc1918(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _RFC1918)


_LAN_ADDR_CACHE = {'t': 0.0, 'addrs': []}
_LAN_ADDR_CACHE_TTL = 10.0     # status 轮询频繁，网卡列表缓存 10s
_LAN_ADDR_EMPTY_TTL = 3.0      # 空结果也缓存（避免无网络时每次轮询都跑 PowerShell），但更短便于联网后尽快恢复


def local_lan_addresses():
    """本机 RFC1918 IPv4 候选网卡地址（多网卡由 UI 显式选择，不绑 0.0.0.0）。"""
    now = time.time()
    ttl = _LAN_ADDR_CACHE_TTL if _LAN_ADDR_CACHE['addrs'] else _LAN_ADDR_EMPTY_TTL
    if _LAN_ADDR_CACHE['t'] and now - _LAN_ADDR_CACHE['t'] < ttl:
        return list(_LAN_ADDR_CACHE['addrs'])
    addrs = _enum_lan_addresses()
    _LAN_ADDR_CACHE['t'] = now
    _LAN_ADDR_CACHE['addrs'] = addrs
    return list(addrs)


def _enum_lan_addresses():
    found = []

    def add(ip):
        ip = str(ip or '').strip()
        if ip and _is_rfc1918(ip) and ip not in found:
            found.append(ip)

    try:
        q = subprocess.run([
            'powershell', '-NoProfile', '-Command',
            "(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "Where-Object {$_.PrefixOrigin -ne 'WellKnown'} | "
            "Select-Object -ExpandProperty IPAddress) -join \"`n\""
        ], capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
        for line in (q.stdout or '').splitlines():
            add(line.strip())
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip)
    except Exception:
        pass
    if not found:
        # UDP 不真正发包，仅借路由表取默认出口 IP 兜底
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('192.168.255.255', 9))
            add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return found


_PROCESS_QUERY_LIMITED = 0x1000
_PROCESS_QUERY_IMAGE = _PROCESS_QUERY_LIMITED   # QueryFullProcessImageName 只需 LIMITED


def _proc_handle(pid, access=0x1000):
    try:
        import ctypes
        return ctypes.windll.kernel32.OpenProcess(access, False, int(pid)) or None
    except Exception:
        return None


def _proc_close(h):
    try:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass


def _pid_alive(pid):
    """OpenProcess + STILL_ACTIVE；OpenProcess 拒绝访问(受限进程)视为活着。"""
    import ctypes
    h = _proc_handle(pid)
    if not h:
        # 权限受限/进程不可枚举：宁可相信 registry 记录
        return True
    try:
        code = ctypes.c_ulong(0)
        if ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == 259            # STILL_ACTIVE
        return True
    except Exception:
        return True
    finally:
        _proc_close(h)


def _pid_creation_ticks(pid):
    """进程创建时刻 FILETIME int；同一 pid 不同创建时刻 = PID 复用冒名，拒。"""
    import ctypes
    h = _proc_handle(pid, 0x1000)
    if not h:
        return None
    try:
        t1, t2, t3, t4 = (ctypes.c_ulonglong() for _ in range(4))
        if ctypes.windll.kernel32.GetProcessTimes(
                h, ctypes.byref(t1), ctypes.byref(t2),
                ctypes.byref(t3), ctypes.byref(t4)):
            return int(t1.value)
    except Exception:
        pass
    finally:
        _proc_close(h)
    return None


def _proc_image_name(pid):
    """进程可执行文件名（basename, lower）。只需 QUERY_LIMITED_INFORMATION，
    不申请 VM_READ 等更高权限。"""
    import ctypes
    h = _proc_handle(pid, _PROCESS_QUERY_IMAGE)
    if not h:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(4096)
        size = ctypes.c_ulong(4096)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    except Exception:
        pass
    finally:
        _proc_close(h)
    return ''


def _pid_is_desktop(pid):
    """核实 PID 是 Kimi Code 桌面进程——精确 exe 名，不是 startswith。"""
    return _proc_image_name(pid) == 'kimi code.exe'


def _tcp_listener_pid(ip, port):
    """GetExtendedTcpTable：精确查某 IP:port LISTEN 的 PID（ctypes，不用 PowerShell）。"""
    try:
        import ctypes
        from ctypes import wintypes
        AF_INET, TCP_TABLE_OWNER_PID_ALL = 2, 5
        size = wintypes.DWORD(0)
        # 先探测缓冲区大小
        ctypes.windll.iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
        buf = ctypes.create_string_buffer(size.value)
        if ctypes.windll.iphlpapi.GetExtendedTcpTable(
                buf, ctypes.byref(size), False, AF_INET,
                TCP_TABLE_OWNER_PID_ALL, 0) != 0:
            return None
        # MIB_TCPTABLE_OWNER_PID: numEntries + rows(state, localAddr, localPort,
        # remoteAddr, remotePort, owningPid)
        n = int.from_bytes(buf.raw[:4], 'little')
        row = 24
        want_ip = socket.inet_aton(ip)          # 网络序
        for i in range(n):
            off = 4 + i * row
            state = int.from_bytes(buf.raw[off + 0:off + 4], 'little')
            lip = buf.raw[off + 4:off + 8]
            lport = int.from_bytes(buf.raw[off + 8:off + 10], 'big')
            pid = int.from_bytes(buf.raw[off + 20:off + 24], 'little')
            if state == 2 and lip == want_ip and lport == port:  # LISTEN=2
                return pid
    except Exception:
        pass
    return None


def _load_instances(kimi_home):
    """读取 ~/.kimi-code/server/instances/*.json，返回 [{pid,host,port}]。"""
    out = []
    d = os.path.join(kimi_home, 'server', 'instances')
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(d, name), encoding='utf-8') as f:
                info = json.load(f)
            pid, host, port = int(info['pid']), str(info['host']), int(info['port'])
            if host == 'localhost':
                host = '127.0.0.1'
            sid = info.get('serverId') or info.get('server_id')
            started = info.get('startedAt') or info.get('started_at') or 0
            out.append({'pid': pid, 'host': host, 'port': port,
                        'server_id': str(sid or ''),
                        'started_at': float(started)})
        except Exception:
            continue
    return out


def _parse_owner_origin(raw):
    """校验 owner_origin 为纯 loopback http origin：无 userinfo/路径/query/fragment。"""
    if not isinstance(raw, str) or len(raw) > 200:
        raise MobileBridgeError('owner 地址格式无效')
    try:
        u = urlsplit(raw)
    except Exception:
        raise MobileBridgeError('owner 地址格式无效')
    if u.scheme != 'http' or not u.hostname:
        raise MobileBridgeError('owner 必须是本机 http 地址')
    if u.username or u.password:
        raise MobileBridgeError('owner 地址不允许携带凭据')
    if u.path not in ('', '/') or u.query or u.fragment:
        raise MobileBridgeError('owner 地址不允许携带路径或参数')
    host = '127.0.0.1' if u.hostname == 'localhost' else u.hostname.strip('[]')
    if not _is_loopback_ip(host):
        raise MobileBridgeError('owner 必须是 loopback 地址')
    try:
        port = u.port or 80
    except ValueError:
        raise MobileBridgeError('owner 端口无效')
    if not (1 <= port <= 65535):
        raise MobileBridgeError('owner 端口无效')
    return host, port


def _read_server_token(kimi_home):
    """只读兼容入口；缺失与不可用不同，不在读取时创建凭据。"""
    from mobile_credentials import read_server_token, CredentialUnavailable
    try:
        return read_server_token(kimi_home)
    except CredentialUnavailable:
        raise MobileBridgeError('SERVER_TOKEN_UNAVAILABLE') from None


def _ensure_server_token(kimi_home):
    from mobile_credentials import ensure_server_token, CredentialUnavailable
    try:
        return ensure_server_token(kimi_home)
    except CredentialUnavailable:
        raise MobileBridgeError('SERVER_TOKEN_UNAVAILABLE') from None


def _owner_responds(host, port, timeout=2.5):
    """healthz 必须 200——只 2xx 才认 owner 存活。"""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request('GET', '/api/v1/healthz')
        r = conn.getresponse()
        r.read(1024)
        conn.close()
        return r.status == 200
    except Exception:
        return False


def _safe_error_text(e):
    return str(e)[:160]


def _is_strict_digits(s):
    """严格 ASCII 纯数字（str.isdigit 会放行 Unicode 数字字符，不用它）。"""
    return bool(s) and all('0' <= c <= '9' for c in s)


def _json_no_dup_object(pairs):
    """object_pairs_hook：重复键直接拒（默认解析静默取后者）。"""
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise ValueError('duplicate key')
        obj[k] = v
    return obj


def _token_variants(token):
    """server.token 的所有外发形态（原文 / percent / base64 / urlsafe-b64，
    含去 '=' 变体），返回 bytes 列表供体检与脱敏共用。"""
    if not token:
        return []
    tb = token.encode('utf-8')
    return [tb,
            quote(token).encode('ascii'),
            quote(token, safe='').encode('ascii'),
            base64.b64encode(tb),
            base64.b64encode(tb).rstrip(b'='),
            base64.urlsafe_b64encode(tb),
            base64.urlsafe_b64encode(tb).rstrip(b'=')]


def _contains_token(haystack, token):
    """出向泄露体检：haystack(bytes) 中是否出现 server.token 的原文、
    percent 编码或 base64 形态。"""
    return any(v and v in haystack for v in _token_variants(token))


# 命中 token 时的脱敏占位符：替换而非整包 502。transcript / messages 等
# 读面会把用户自己的会话历史透给手机端，历史里可能合法含 token 原文
# （例如 panel_url.txt 写入的 ?token= 串），那属于数据内容而非凭据外泄，
# 整包 502 会让正常读面瘫痪；改为擦除成占位符既保不泄露又放行内容。
_TOKEN_REDACTION = b'[redacted-token]'


def _redact_token(haystack, token):
    """把 haystack(bytes) 中出现的所有 token 形态替换为占位符后返回。"""
    for v in _token_variants(token):
        if v:
            haystack = haystack.replace(v, _TOKEN_REDACTION)
    return haystack


# ---------------- connector diagnostics 白名单投影 ----------------
# 只投影 mobile_tunnel status 顶层 diagnostics 的六个固定字段；未知字段、
# 来源 URL、原始 token、日志行原文一律不转发。任何一项不合规 → 整个
# diagnostics 省略（status 其余字段不受影响）。
_DIAG_KINDS = frozenset(('connection_registered', 'connection_unregistered',
                         'connection_retrying', 'origin_request_failed'))
_DIAG_TRANSPORT_STATES = frozenset(('unknown', 'ready', 'reconnecting'))
_DIAG_COUNT_MAX = 65535
_DIAG_EVENTS_MAX = 8
_DIAG_INDEX_MAX = 4
_DIAG_KEYS = frozenset(('counts', 'last_event', 'recent_events',
                        'active_conn_indices', 'active_conn_count',
                        'transport_state'))


def _is_strict_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _diag_event_ok(event):
    return (isinstance(event, dict) and set(event.keys()) == {'kind', 'time'}
            and event['kind'] in _DIAG_KINDS
            and isinstance(event['time'], float)
            and math.isfinite(event['time']) and event['time'] >= 0)


def _project_connector_diagnostics(diag):
    if not isinstance(diag, dict) or set(diag.keys()) != _DIAG_KEYS:
        return None
    counts = diag['counts']
    if (not isinstance(counts, dict)
            or set(counts.keys()) != _DIAG_KINDS
            or any(not _is_strict_int(v) or not (0 <= v <= _DIAG_COUNT_MAX)
                   for v in counts.values())):
        return None
    last_event = diag['last_event']
    if last_event is not None and not _diag_event_ok(last_event):
        return None
    events = diag['recent_events']
    if (not isinstance(events, list) or len(events) > _DIAG_EVENTS_MAX
            or any(not _diag_event_ok(e) for e in events)):
        return None
    indices = diag['active_conn_indices']
    if (not isinstance(indices, list) or len(indices) > _DIAG_INDEX_MAX
            or any(not _is_strict_int(i) or not (0 <= i < _DIAG_INDEX_MAX)
                   for i in indices)
            or len(set(indices)) != len(indices)):
        return None
    count = diag['active_conn_count']
    if not _is_strict_int(count) or count != len(indices):
        return None
    if diag['transport_state'] not in _DIAG_TRANSPORT_STATES:
        return None
    return {'counts': dict(counts),
            'last_event': (dict(last_event) if last_event is not None else None),
            'recent_events': [dict(e) for e in events],
            'active_conn_indices': list(indices),
            'active_conn_count': count,
            'transport_state': diag['transport_state']}


# ---------------- 管理器 ----------------
class MobileBridgeManager(object):
    """本机控制面与 LAN/internet 手机代理的生命周期管理。状态仅内存持有。"""

    def __init__(self, kimi_home):
        self.kimi_home = kimi_home
        self._lock = threading.RLock()
        self._state = 'off'                    # off|starting|on|stopping
        self._owner_origin = ''
        self._owner_host = ''
        self._owner_port = 0
        self._owner_pid = 0
        self._owner_server_id = ''
        self._owner_creation_ticks = 0
        self._address = ''
        self._port = 0
        self._server = None                    # ThreadingHTTPServer
        self._serve_thread = None
        self._watch_thread = None
        self._server_token = None              # 运行时内存注入用，永不外发
        self._pair_tokens = {}                 # token -> expires_epoch
        self._sessions = {}                    # sid -> {created, expires, last_seen, generation}
        self._exchange_fails = {}              # ip -> {'fails':int,'first':ts,'banned':ts}
        self._open_sockets = set()             # 已建立 client/upstream socket，stop 强关
        self._conn_sem = threading.BoundedSemaphore(MAX_LAN_CONNECTIONS)
        self._ws_total = 0
        self._ws_by_sid = {}                   # sid -> 活跃 WS 数
        self._generation = 0
        self._closing = False
        self._mode = 'lan'
        self._public_origin = ''
        self._tunnel = {'state': 'off'}
        self._pair_state = 'missing'
        self._rate_buckets = {}
        self._connector_lock = threading.RLock()
        try:
            from mobile_tunnel import ConnectorRuntime
            self._connector = ConnectorRuntime(kimi_home)
        except ImportError:
            self._connector = None
        # relay：私人 VPS 中转（worker→VPS 持久 WS 隧道），与 connector 并列的
        # 第三种公网入口。桥仍绑 127.0.0.1；RelayClient 把公网流量本机回连进桥。
        # relay_host 记录本次 start 时生效的中继 host（用于 _tunnel_ready 的
        # origin 白名单——不再写死某个 VPS IP，跟随 relay.json 配置）。
        self._relay = None
        self._relay_tunnel_id = ''
        self._relay_host = ''

    # ---------- 连接/WS 限额 ----------
    def try_acquire_conn(self):
        """process_request 线程化之前获取名额；满则调用方直接关 socket。"""
        return self._conn_sem.acquire(blocking=False)

    def release_conn(self):
        try:
            self._conn_sem.release()
        except ValueError:
            pass

    def generation_valid(self, generation, starting=False):
        with self._lock:
            return (not self._closing and generation == self._generation
                    and self._state in (('starting', 'on') if starting else ('on',)))

    def ws_acquire(self, sid, generation=None):
        with self._lock:
            if generation is None:
                generation = self._generation
            if not self.generation_valid(generation):
                return False
            if self._ws_total >= MAX_WS_TOTAL:
                return False
            if self._ws_by_sid.get(sid, 0) >= MAX_WS_PER_SESSION:
                return False
            self._ws_total += 1
            self._ws_by_sid[sid] = self._ws_by_sid.get(sid, 0) + 1
            return True

    def ws_release(self, sid, generation=None):
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._ws_total = max(0, self._ws_total - 1)
            n = self._ws_by_sid.get(sid, 0)
            if n <= 1:
                self._ws_by_sid.pop(sid, None)
            else:
                self._ws_by_sid[sid] = n - 1

    @staticmethod
    def _normalize_connector_status(snapshot):
        failed = {'state': 'failed', 'version': '2026.9.3',
                  'error_code': 'CONNECTOR_INSTALL_FAILED'}
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('connector'), dict):
            return failed
        connector = snapshot['connector']
        state = connector.get('state')
        if (state not in ('missing', 'installing', 'installed', 'failed')
                or connector.get('version') != '2026.9.3'):
            return failed
        out = {'state': state, 'version': '2026.9.3'}
        error = connector.get('error_code')
        if error is not None:
            if not isinstance(error, str) or error not in _MOBILE_ERROR_CODES:
                return failed
            out['error_code'] = error
        elif state == 'failed':
            out['error_code'] = 'CONNECTOR_INSTALL_FAILED'
        return out

    def _connector_status(self):
        if self._connector is None:
            return {'state': 'missing', 'version': '2026.9.3'}, None
        try:
            snapshot = self._connector.status()
        except Exception:
            return {'state': 'failed', 'version': '2026.9.3',
                    'error_code': 'CONNECTOR_INSTALL_FAILED'}, None
        connector = self._normalize_connector_status(snapshot)
        diagnostics = None
        if isinstance(snapshot, dict):
            diagnostics = _project_connector_diagnostics(snapshot.get('diagnostics'))
        return connector, diagnostics

    def _relay_config(self):
        """读 <kimi_home>/usage-dashboard/relay.json，返回生效的规范化中继配置。

        worker 侧是配置的唯一消费端：daemon 负责写入，worker 每次 start 时
        经 relay_config.load 读取（文件不存在/损坏 → 内置默认，与未配置等价）。
        """
        try:
            import relay_config
            return relay_config.load(self.kimi_home)
        except Exception:
            # 模块不可用/读取异常：仍给一份默认，让 RelayClient 自行兜底
            return {'host': 'your-relay-host', 'tunnel_port': 48213,
                    'public_port': 47961, 'token': ''}

    # ---------- 状态 ----------
    def status(self):
        with self._lock:
            include_lan = self._mode == 'lan' or self._state == 'off'
        addrs = local_lan_addresses() if include_lan else None
        connector, diagnostics = self._connector_status()
        with self._lock:
            now = time.time()
            live = [s for s in self._sessions.values() if s['expires'] > now]
            st = {
                'enabled': self._state == 'on', 'state': self._state,
                'mode': self._mode, 'owner_origin': self._owner_origin,
                'device_count': len(live), 'connector': connector,
                'tunnel': dict(self._tunnel), 'pair_state': self._pair_state,
            }
            if diagnostics is not None:
                st['connector_diagnostics'] = diagnostics
            if addrs is not None:
                st['addresses'] = addrs
            if self._mode == 'lan':
                st['address'], st['port'] = self._address, self._port
            best = max(self._pair_tokens.values(), default=0.0)
            if self._state == 'on' and self._mode in ('internet', 'relay') and self._tunnel['state'] == 'ready':
                st['public_origin'] = self._public_origin
            if self._state == 'on' and best > now:
                st['url'] = self._pair_url_locked()
                st['expires_at'] = int(best)
            elif self._pair_state == 'available':
                st['pair_state'] = 'expired'
            return st

    def install_connector(self, consent, consent_version):
        if consent is not True or consent_version != RELAY_CONSENT_VERSION:
            raise MobileBridgeError('CONSENT_REQUIRED')
        with self._connector_lock:
            with self._lock:
                if self._closing:
                    raise MobileBridgeError('START_CANCELLED')
            if self._connector is None:
                raise MobileBridgeError('CONNECTOR_MISSING')
            result = self._normalize_connector_status(
                self._connector.install(consent, consent_version))
            if result.get('error_code'):
                raise MobileBridgeError(result['error_code'])
            if result['state'] not in ('installed', 'installing'):
                raise MobileBridgeError('CONNECTOR_INSTALL_FAILED')
        return self.status()

    # ---------- 启停 ----------
    def start(self, owner_origin, address=None, mode='lan', relay_consent=False,
              consent_version=None):
        if mode not in ('lan', 'internet', 'relay'):
            raise MobileBridgeError('模式无效')
        if mode == 'internet':
            if address is not None:
                raise MobileBridgeError('外网模式不接受监听地址')
            if relay_consent is not True or consent_version != RELAY_CONSENT_VERSION:
                raise MobileBridgeError('CONSENT_REQUIRED')
        elif mode == 'relay':
            if address is not None:
                raise MobileBridgeError('中继模式不接受监听地址')
        host, port = _parse_owner_origin(owner_origin)
        norm_origin = 'http://%s:%d' % (host, port)
        with self._lock:
            if self._closing:
                raise MobileBridgeError('START_CANCELLED')
            if self._state in ('starting', 'stopping'):
                raise MobileBridgeError('CONNECTOR_BUSY')
            if self._state == 'on':
                same = (norm_origin == self._owner_origin and mode == self._mode
                        and (mode == 'internet' or address == self._address))
                if not same:
                    raise MobileBridgeError('请先停止再重新开启')
                generation = None
            else:
                self._generation += 1
                generation = self._generation
                self._state = 'starting'
                self._mode = mode
                self._tunnel = {'state': 'starting' if mode in ('internet', 'relay') else 'off'}
                self._public_origin = ''
                self._pair_state = 'missing'
        if generation is None:
            return self.status()
        httpd = None
        published = False
        try:
            if mode == 'internet':
                connector, _ = self._connector_status()
                if connector.get('error_code'):
                    raise MobileBridgeError(connector['error_code'])
                if connector['state'] != 'installed':
                    raise MobileBridgeError('CONNECTOR_BUSY' if connector['state'] == 'installing'
                                            else 'CONNECTOR_MISSING')
                bind_address = '127.0.0.1'
            elif mode == 'relay':
                # relay：与 internet 一样绑回环——公网流量由 RelayClient 经
                # 本机回连送进来；不查 connector（不走 cloudflared）。
                bind_address = '127.0.0.1'
            else:
                if not isinstance(address, str) or not _is_rfc1918(address):
                    raise MobileBridgeError('请选择一个本机局域网 IPv4 地址')
                if address not in local_lan_addresses():
                    raise MobileBridgeError('所选地址不是本机网卡地址')
                bind_address = address
            inst = next((i for i in _load_instances(self.kimi_home)
                         if i['host'] == host and i['port'] == port), None)
            if (inst is None or not _pid_alive(inst['pid'])
                    or not _pid_is_desktop(inst['pid']) or not inst.get('server_id')):
                raise MobileBridgeError('OWNER_LOST')
            creation = _pid_creation_ticks(inst['pid'])
            if (creation is None or _tcp_listener_pid(host, port) != inst['pid']
                    or not _owner_responds(host, port)):
                raise MobileBridgeError('OWNER_LOST')
            if not self.generation_valid(generation, starting=True):
                raise MobileBridgeError('START_CANCELLED')
            token = _ensure_server_token(self.kimi_home)
            httpd, bound_port = self._bind(bind_address)
            httpd.generation = generation
            with self._lock:
                if not self.generation_valid(generation, starting=True):
                    raise MobileBridgeError('START_CANCELLED')
                self._owner_origin = norm_origin
                self._owner_host, self._owner_port = host, port
                self._owner_pid = inst['pid']
                self._owner_server_id = inst['server_id']
                self._owner_creation_ticks = creation
                self._address, self._port = bind_address, bound_port
                self._server = httpd
                self._server_token = token
                self._serve_thread = threading.Thread(
                    target=httpd.serve_forever, kwargs={'poll_interval': 0.4}, daemon=True)
                self._serve_thread.start()
                published = True
                if mode == 'lan':
                    self._state = 'on'
                    self._issue_pair_token_locked()
                self._watch_thread = threading.Thread(
                    target=self._watch_owner, args=(generation,), daemon=True)
                self._watch_thread.start()
            if mode == 'internet':
                with self._connector_lock:
                    if not self.generation_valid(generation, starting=True):
                        raise MobileBridgeError('START_CANCELLED')
                    self._connector.start(
                        bound_port, lambda origin: self._tunnel_ready(generation, origin),
                        lambda code: self._tunnel_failed(generation, code))
            elif mode == 'relay':
                with self._connector_lock:
                    if not self.generation_valid(generation, starting=True):
                        raise MobileBridgeError('START_CANCELLED')
                    cfg = self._relay_config()
                    if self._relay is not None:
                        # 配置可能已变：不复用旧实例——按本次生效配置新建。
                        try:
                            self._relay.shutdown()
                        except Exception:
                            pass
                    from mobile_relay import RelayClient
                    self._relay = RelayClient(
                        relay_host=cfg['host'], relay_port=cfg['tunnel_port'],
                        public_port=cfg['public_port'],
                        token=cfg['token'] or None)
                    self._relay_host = cfg['host']
                    self._relay.start(
                        bound_port, lambda origin: self._tunnel_ready(generation, origin),
                        lambda code: self._tunnel_failed(generation, code))
            return self.status()
        except Exception as exc:
            if httpd is not None and not published:
                httpd.server_close()
            code = str(exc) if str(exc) in _MOBILE_ERROR_CODES else 'TUNNEL_START_FAILED'
            self._stop_generation(generation, code)
            if isinstance(exc, MobileBridgeError):
                raise
            raise MobileBridgeError(code)

    def _tunnel_ready(self, generation, origin):
        # internet：cloudflared 回 https://<sub>.trycloudflare.com；
        # relay：RelayClient 回 http://<vps>:<port>（不带 /t/<id>——tid 另存）。
        # relay 白名单按本次 start 配置的中继 host 匹配（self._relay_host），
        # 端口仍 \d+——不再写死任何固定 VPS IP。
        ok_origin = isinstance(origin, str) and (
            re.fullmatch(r'https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com',
                         origin) is not None
            or (isinstance(self._relay_host, str) and self._relay_host
                and re.fullmatch(r'http://%s:\d+' % re.escape(self._relay_host),
                                 origin) is not None))
        if not ok_origin:
            self._tunnel_failed(generation, 'TUNNEL_START_FAILED')
            return
        if not self._owner_still_mine(generation):
            # 启动期的 owner 校验：这里是 start 流程的一环，仍属“一次性确认”，
            # 但 teardown 交回 _watch_owner 的连续判定，单次抖动不拆隧道。
            self._tunnel_failed(generation, 'OWNER_LOST')
            return
        with self._lock:
            if not self.generation_valid(generation, starting=True):
                return
            if self._mode not in ('internet', 'relay'):
                return
            if self._mode == 'relay':
                # tid 从 RelayClient 取（注册时已写 self.tunnel_id）；无效则失败。
                tid = getattr(self._relay, 'tunnel_id', '') if self._relay is not None else ''
                if not isinstance(tid, str) or not (4 <= len(tid) <= 32):
                    if self._state == 'starting':
                        self._tunnel_failed(generation, 'TUNNEL_START_FAILED')
                    return
                self._relay_tunnel_id = tid
            if self._state == 'starting':
                # 首次 ready：转 on
                self._public_origin = origin
                self._tunnel = {'state': 'ready'}
                self._state = 'on'
                self._issue_pair_token_locked()
            elif self._state == 'on':
                # relay 断线重连后的二次 ready：只刷新 origin/tid，配对会话不动。
                # tid 因 X-Relay-Resume 复用通常不变；变了也安全更新。
                self._public_origin = origin
                self._tunnel = {'state': 'ready'}

    def _tunnel_failed(self, generation, code):
        code = code if code in _MOBILE_ERROR_CODES else 'TUNNEL_START_FAILED'
        self._stop_generation(generation, code)

    def stop(self):
        return self._stop_generation(None)

    def begin_shutdown(self):
        with self._lock:
            self._closing = True

    def shutdown(self):
        self.begin_shutdown()
        self.stop()
        with self._connector_lock:
            if self._connector is not None:
                self._connector.shutdown()
            if self._relay is not None:
                try:
                    self._relay.shutdown()
                except Exception:
                    pass

    def _stop_generation(self, expected, error_code=None):
        with self._lock:
            noop = (self._state in ('off', 'stopping')
                    or (expected is not None and expected != self._generation))
            if not noop:
                self._state = 'stopping'
                self._generation += 1
                claim = self._generation
                httpd = self._server
                socks = list(self._open_sockets)
                self._pair_tokens.clear()
                self._sessions.clear()
                self._server_token = None
                self._server = None
                self._public_origin = ''
                self._relay_tunnel_id = ''
                self._pair_state = 'missing'
                self._tunnel = ({'state': 'failed', 'error_code': error_code}
                                if error_code in _MOBILE_ERROR_CODES else {'state': 'off'})
        if noop:
            return self.status()
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        with self._connector_lock:
            if self._connector is not None:
                try:
                    self._connector.stop()
                except Exception:
                    pass
            if self._relay is not None:
                try:
                    self._relay.stop()
                except Exception:
                    pass
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        with self._lock:
            if self._generation == claim and self._state == 'stopping':
                self._state = 'off'
                self._open_sockets.clear()
                self._ws_total = 0
                self._ws_by_sid.clear()
                self._exchange_fails.clear()
                self._rate_buckets.clear()
                self._owner_origin = ''
                self._owner_host = ''
                self._owner_port = 0
                self._owner_pid = 0
                self._owner_server_id = ''
                self._owner_creation_ticks = 0
                self._address = ''
                self._port = 0
        return self.status()

    def _owner_still_mine(self, generation=None):
        with self._lock:
            if generation is None:
                generation = self._generation
            if not self.generation_valid(generation, starting=True):
                return False
            pid, host, port = self._owner_pid, self._owner_host, self._owner_port
            sid, creation = self._owner_server_id, self._owner_creation_ticks
        if (not pid or not _pid_alive(pid) or not creation
                or _pid_creation_ticks(pid) != creation or not _pid_is_desktop(pid)
                or _tcp_listener_pid(host, port) != pid or not sid):
            return False
        inst = next((i for i in _load_instances(self.kimi_home)
                     if i['host'] == host and i['port'] == port), None)
        if (inst is None or inst.get('server_id') != sid or inst['pid'] != pid
                or not _owner_responds(host, port)):
            return False
        return self.generation_valid(generation, starting=True)

    def _watch_owner(self, generation):
        misses = 0
        while self.generation_valid(generation, starting=True):
            time.sleep(OWNER_WATCH_INTERVAL)
            if not self.generation_valid(generation, starting=True):
                return
            if not self._owner_still_mine(generation):
                # 连续 OWNER_LOST_GRACE 次失败才 teardown：healthz 单次
                # 超时/卡顿不致误杀隧道与全部已配对会话。
                misses += 1
                if misses < OWNER_LOST_GRACE:
                    continue
                self._stop_generation(generation, 'OWNER_LOST')
                return
            misses = 0

    def _bind(self, address):
        for port in range(LAN_PREFERRED_PORT, LAN_PREFERRED_PORT + LAN_PORT_SCAN_MAX):
            try:
                httpd = _LanHTTPServer((address, port), _LanHandler, self)
                return httpd, port
            except OSError:
                continue
        raise MobileBridgeError('TUNNEL_START_FAILED')

    # ---------- 配对 / 会话 ----------
    def _pair_url_locked(self):
        now = time.time()
        best = None
        for tok, exp in self._pair_tokens.items():
            if exp > now and (best is None or exp > self._pair_tokens[best]):
                best = tok
        if best is None:
            return None
        if self._mode == 'relay':
            # relay：_public_origin 只存 http://<vps>:<port>（Host/Origin 校验用），
            # 配对 URL 另拼 /t/<tid> 前缀给 VPS 公网口路由。
            origin = self._public_origin + '/t/' + self._relay_tunnel_id
        elif self._mode == 'internet':
            origin = self._public_origin
        else:
            origin = 'http://%s:%d' % (self._address, self._port)
        return origin + '/mobile/pair#pair=' + best

    def _issue_pair_token_locked(self):
        now = time.time()
        for tok in [t for t, e in self._pair_tokens.items() if e <= now]:
            del self._pair_tokens[t]
        self._pair_tokens[secrets.token_urlsafe(24)] = now + PAIR_TTL_SECONDS
        self._pair_state = 'available'

    def _exchange_rate_ok(self, ip):
        with self._lock:                         # 可被持锁/未持锁路径调用（RLock）
            rec = self._exchange_fails.get(ip)
            if rec and rec.get('banned', 0) > time.time():
                return False
            return True

    def _exchange_failed(self, ip):
        """失败计数（失败桶有界 EXCHANGE_FAILS_MAX：先清过期，再逐最旧 IP）。"""
        with self._lock:
            now = time.time()
            rec = self._exchange_fails.setdefault(
                ip, {'fails': 0, 'first': now, 'banned': 0})
            if now - rec['first'] > EXCHANGE_FAIL_WINDOW:
                rec['fails'] = 0
                rec['first'] = now
            rec['fails'] += 1
            if rec['fails'] >= EXCHANGE_MAX_FAILS:
                rec['banned'] = now + EXCHANGE_BAN_SECONDS
                rec['fails'] = 0
                rec['first'] = now
            if len(self._exchange_fails) > EXCHANGE_FAILS_MAX:
                for k in [k for k, v in self._exchange_fails.items()
                          if v.get('banned', 0) <= now
                          and now - v['first'] > EXCHANGE_FAIL_WINDOW]:
                    del self._exchange_fails[k]
            while len(self._exchange_fails) > EXCHANGE_FAILS_MAX:
                oldest = min(self._exchange_fails,
                             key=lambda k: self._exchange_fails[k]['first'])
                del self._exchange_fails[oldest]

    def exchange_pair_token(self, ip, token, generation=None):
        with self._lock:
            if generation is None:
                generation = self._generation
            if not self.generation_valid(generation) or not self._exchange_rate_ok(ip):
                return None
            if not isinstance(token, str) or len(token) > 200 or not _TOKEN_SAFE_RE.fullmatch(token):
                self._exchange_failed(ip)
                return None
            now = time.time()
            exp = self._pair_tokens.get(token)
            if exp is None or exp <= now:
                self._exchange_failed(ip)
                return None
            live = {s: v for s, v in self._sessions.items() if v['expires'] > now}
            if len(live) >= MAX_DEVICES:
                raise MobileBridgeError('已达最大配对设备数，请先停止后重开')
            self._sessions = live
            sid = secrets.token_urlsafe(32)
            self._sessions[sid] = {'created': now, 'expires': now + SESSION_TTL_SECONDS,
                                   'last_seen': now, 'generation': generation}
            del self._pair_tokens[token]
            self._pair_state = 'used'
            return sid

    def session_valid(self, sid, generation=None):
        if not isinstance(sid, str) or len(sid) > 200 or not _TOKEN_SAFE_RE.fullmatch(sid):
            return False
        with self._lock:
            if generation is None:
                generation = self._generation
            if not self.generation_valid(generation):
                return False
            return self._session_live_locked(sid, generation)

    def _session_live_locked(self, sid, generation):
        """只验证不续期（持锁内联版，watchdog/authorization/限流共用）。
        fail-closed：generation 缺失/不符、created/expires 缺失或非有限数值
        （bool/NaN/inf/字符串）一律清除并拒；expires 超 idle 或 created 超
        绝对寿命同样清除，绝不复活、绝不默认上限。"""
        session = self._sessions.get(sid)
        if session is None:
            return False
        if session.get('generation') != generation:
            return False
        created, expires = session.get('created'), session.get('expires')
        if (not isinstance(created, (int, float))
                or not isinstance(expires, (int, float))
                or isinstance(created, bool) or isinstance(expires, bool)
                or not math.isfinite(created) or not math.isfinite(expires)):
            del self._sessions[sid]
            return False
        now = time.time()
        if expires <= now or created + SESSION_MAX_AGE_SECONDS <= now:
            del self._sessions[sid]
            return False
        return True

    def session_touch(self, sid, generation=None):
        """真实已认证活动才调用：先验证（同 session_valid 语义，过期不复活），
        再滑动 expires=min(now+idle, created+absolute) 并更新 last_seen。"""
        if not isinstance(sid, str) or len(sid) > 200 or not _TOKEN_SAFE_RE.fullmatch(sid):
            return False
        with self._lock:
            if generation is None:
                generation = self._generation
            if not self.generation_valid(generation):
                return False
            if not self._session_live_locked(sid, generation):
                return False
            session = self._sessions[sid]
            now = time.time()
            # created 已经过 _session_live_locked 严格校验（有限数值），
            # 绝对上限无条件约束，绝不默认
            session['expires'] = min(now + SESSION_TTL_SECONDS,
                                     session['created'] + SESSION_MAX_AGE_SECONDS)
            session['last_seen'] = now
            return True

    def rotate_pair(self, generation=None):
        """轮换配对 token：当前 generation 且 owner 有效；旧未用 token 全部
        作废，签发一个新的 10 分钟一次性 token。SID/generation/public_origin/
        sockets 不动；owner 失联 fail-closed（整代停止，与看门狗同义）。"""
        with self._lock:
            if generation is None:
                generation = self._generation
        if not self._owner_still_mine(generation):
            # 请求热路径：owner 疑似失联只让本请求失败（503），不就地
            # teardown——teardown 统一由 _watch_owner 连续 OWNER_LOST_GRACE
            # 次确认后执行，避免单次 healthz 抖动拆隧道、清全部已配对会话。
            raise MobileBridgeError('OWNER_LOST')
        with self._lock:
            if not self.generation_valid(generation):
                raise MobileBridgeError('手机连接尚未就绪或已停止')
            now = time.time()
            self._pair_tokens.clear()
            token = secrets.token_urlsafe(24)
            self._pair_tokens[token] = now + PAIR_TTL_SECONDS
            self._pair_state = 'available'
            return token

    def track_socket(self, sock, on=True, generation=None, starting=False):
        with self._lock:
            if generation is None:
                generation = self._generation
            if on:
                if self.generation_valid(generation, starting=starting):
                    self._open_sockets.add(sock)
                    return True
                close = True
            else:
                if generation == self._generation:
                    self._open_sockets.discard(sock)
                close = False
        if close:
            try:
                sock.close()
            except Exception:
                pass
        return False

    def upstream_snapshot(self, generation):
        with self._lock:
            if not self.generation_valid(generation):
                return None
            snapshot = (self._owner_host, self._owner_port, self._server_token)
        if not self._owner_still_mine(generation):
            # 请求热路径不就地 teardown：返回 None 让请求 503，
            # teardown 由 _watch_owner 连续确认后统一执行。
            return None
        with self._lock:
            if (not self.generation_valid(generation)
                    or snapshot != (self._owner_host, self._owner_port, self._server_token)):
                return None
            return snapshot

    def authorization_valid(self, generation, snapshot, sid):
        with self._lock:
            return (self.generation_valid(generation)
                    and snapshot == (self._owner_host, self._owner_port, self._server_token)
                    and self.session_valid(sid, generation))

    def first_authorization_valid(self, generation, snapshot, sid):
        if not self.authorization_valid(generation, snapshot, sid):
            return False
        if not self._owner_still_mine(generation):
            # 请求热路径不就地 teardown：返回 False 让握手失败，
            # teardown 由 _watch_owner 连续确认后统一执行。
            return False
        return self.authorization_valid(generation, snapshot, sid)

    def upstream_target(self, generation=None):
        if generation is None:
            with self._lock:
                generation = self._generation
        snapshot = self.upstream_snapshot(generation)
        return snapshot[:2] if snapshot else None

    def inject_token(self, generation=None):
        with self._lock:
            if generation is None:
                generation = self._generation
            if not self.generation_valid(generation):
                return None
            return self._server_token

    def bound_host(self):
        with self._lock:
            return (self._address, self._port)

    def request_context(self, generation):
        with self._lock:
            if not self.generation_valid(generation):
                return None
            if self._mode in ('internet', 'relay'):
                if self._tunnel['state'] != 'ready' or not self._public_origin:
                    return None
                return self._mode, self._public_origin
            return self._mode, 'http://%s:%d' % (self._address, self._port)

    def internet_rate_ok(self, generation, ip, sid=None):
        """匿名 'global'+'ip:'(20/1)；sid 经 session_valid 确认后改用
        'paired:global'(600/30)+'sid:'+sid(120/8)，伪造 sid 只落匿名桶。
        bucket 值 = (tokens,last,burst,rate)；global 两键不参与逐出。"""
        with self._lock:
            if not self.generation_valid(generation) or self._mode not in ('internet', 'relay'):
                return False
            if sid is not None and self.session_valid(sid, generation):
                keys = (('paired:global', PAIRED_GLOBAL_RATE_BURST,
                         PAIRED_GLOBAL_RATE_PER_SECOND),
                        ('sid:' + sid, PAIRED_SID_RATE_BURST,
                         PAIRED_SID_RATE_PER_SECOND))
            else:
                keys = (('global', INTERNET_RATE_BURST, INTERNET_RATE_PER_SECOND),
                        ('ip:' + ip, INTERNET_RATE_BURST, INTERNET_RATE_PER_SECOND))
            now = time.monotonic()
            for key, burst, rate in keys:
                if key not in self._rate_buckets:
                    if len(self._rate_buckets) >= INTERNET_RATE_BUCKETS_MAX:
                        candidates = [k for k in self._rate_buckets
                                      if k not in ('global', 'paired:global')]
                        if candidates:
                            oldest = min(candidates,
                                         key=lambda k: self._rate_buckets[k][1])
                            del self._rate_buckets[oldest]
                        else:
                            return False       # 桶表已满且无可逐出者
                    self._rate_buckets[key] = (burst, now, burst, rate)
                tokens, last, b_, r_ = self._rate_buckets[key]
                self._rate_buckets[key] = (min(b_, tokens + max(0.0, now - last) * r_),
                                           now, b_, r_)
            if any(self._rate_buckets[k][0] < 1.0 for k, _, _ in keys):
                return False
            for key, _, _ in keys:
                tokens, last, b_, r_ = self._rate_buckets[key]
                self._rate_buckets[key] = (tokens - 1.0, last, b_, r_)
            return True

    # ---------- 控制面（service.py 回调） ----------
    def _trusted_control_origins(self):
        trusted = {_TRUSTED_APP_ORIGIN}
        # 只信活着的桌面注册实例：死掉的 registry 残留不给 origin 白名单
        for i in _load_instances(self.kimi_home):
            if (_is_loopback_ip(i['host']) and _pid_alive(i['pid'])
                    and _pid_is_desktop(i['pid'])):
                trusted.add('http://%s:%d' % (i['host'], i['port']))
        return trusted

    def _control_origin_ok(self, handler):
        origin = handler.headers.get('Origin')
        if origin is None:
            # 无 Origin：非浏览器本机调用可放行（自定义头已挡跨站表单/简单请求）
            return True, ''
        trusted = self._trusted_control_origins()
        if origin in trusted:
            return True, origin
        return False, ''

    def handle_control(self, handler):
        """处理 /api/mobile* 全部方法（含 OPTIONS）。返回 True=已处理。"""
        path = (handler.path or '').split('?')[0]
        if not (path == CONTROL_PREFIX or path.startswith(CONTROL_PREFIX + '/')):
            return False
        # 头解析缺陷与重复/非法 TE/CL 统一拒——mobile 路由在 service 层
        # legacy guard 之前分流，不能依赖上游的宽松解析
        if getattr(handler.headers, 'defects', None):
            self._control_error(handler, '', 400, '请求头不合法')
            return True
        for h in ('Host', 'Origin', 'Content-Length', 'Transfer-Encoding',
                  'X-Kimi-Mobile-Control', 'Content-Type', 'Sec-Fetch-Site'):
            if len(handler.headers.get_all(h) or []) > 1:
                self._control_error(handler, '', 400, '请求头不合法')
                return True
        if handler.headers.get_all('Transfer-Encoding'):
            self._control_error(handler, '', 400, '请求体格式不被支持')
            return True
        _cls = handler.headers.get_all('Content-Length') or []
        if _cls and not _is_strict_digits(_cls[0]):
            self._control_error(handler, '', 400, '请求头不合法')
            return True
        peer = handler.client_address[0] if handler.client_address else ''
        if not _is_loopback_ip(peer):
            self._control_error(handler, '', 403, '仅允许本机访问')
            return True
        server_port = getattr(handler.server, 'server_port', 0)
        host_hdr = (handler.headers.get('Host') or '').strip()
        allowed_hosts = {'127.0.0.1:%d' % server_port,
                         'localhost:%d' % server_port,
                         '[::1]:%d' % server_port}
        if host_hdr not in allowed_hosts:
            self._control_error(handler, '', 403, 'Host 不被允许')
            return True
        method = handler.command
        origin_ok, origin = self._control_origin_ok(handler)
        # 可信 app origin（app://renderer）的 cross-site fetch metadata 是正常
        # 情形（app:// 与 http://loopback 跨 scheme）；仅该可信 app 豁免，
        # foreign/null Origin 的 cross-site 一律拒
        if ((handler.headers.get('Sec-Fetch-Site') or '').lower() == 'cross-site'
                and origin != _TRUSTED_APP_ORIGIN):
            self._control_error(handler, '', 403, '跨站请求被拒绝')
            return True
        if method == 'OPTIONS':
            # 预检只放行可信 Origin；foreign/null Origin 一律拒
            if not origin:
                self._control_error(handler, '', 403, 'Origin 不被允许')
            else:
                self._control_cors_ok(handler, origin)
            return True
        if not origin_ok:
            self._control_error(handler, origin, 403, 'Origin 不被允许')
            return True
        if (handler.headers.get('X-Kimi-Mobile-Control') or '') != '1':
            self._control_error(handler, origin, 403, '缺少控制头')
            return True
        try:
            if method == 'GET' and path == CONTROL_PREFIX + '/status':
                self._control_json(handler, origin, self.status())
            elif method == 'POST' and path == CONTROL_PREFIX + '/connector/install':
                body = self._control_body(handler)
                if set(body) - {'consent', 'consent_version'}:
                    raise MobileBridgeError('请求参数不被允许')
                self._control_json(handler, origin, self.install_connector(
                    body.get('consent'), body.get('consent_version')))
            elif method == 'POST' and path == CONTROL_PREFIX + '/start':
                body = self._control_body(handler)
                mode = body.get('mode', 'lan')
                allowed = ({'owner_origin', 'mode', 'relay_consent', 'consent_version'}
                           if mode == 'internet' else {'owner_origin', 'mode', 'address'})
                if set(body) - allowed:
                    raise MobileBridgeError('请求参数不被允许')
                self._control_json(handler, origin, self.start(
                    body.get('owner_origin'), body.get('address'), mode,
                    body.get('relay_consent', False), body.get('consent_version')))
            elif method == 'POST' and path == CONTROL_PREFIX + '/stop':
                if self._control_body(handler):
                    raise MobileBridgeError('请求参数不被允许')
                self._control_json(handler, origin, self.stop())
            elif method == 'POST' and path == CONTROL_PREFIX + '/pair/rotate':
                # body 严格 {}：任何键/非对象 JSON 一律拒（沿用 stop 的守卫模式）
                if self._control_body(handler):
                    raise MobileBridgeError('请求参数不被允许')
                self.rotate_pair()
                self._control_json(handler, origin, self.status())
            else:
                self._control_error(handler, origin, 404, '接口不存在')
        except MobileBridgeError as e:
            self._control_error(handler, origin, 400, str(e))
        except Exception:
            self._control_error(handler, origin, 500, '处理失败')
        return True

    def _control_body(self, handler):
        if handler.headers.get_all('Transfer-Encoding'):
            raise MobileBridgeError('请求体格式不被支持')
        cls = handler.headers.get_all('Content-Length') or []
        if len(cls) > 1:
            raise MobileBridgeError('请求头不合法')
        if not cls or not _is_strict_digits(cls[0]):
            raise MobileBridgeError('请求头不合法')
        n = int(cls[0])
        if n > CONTROL_MAX_BODY:
            raise MobileBridgeError('请求体过大')
        if n == 0:
            return {}
        try:
            raw = _read_with_deadline(handler, n)
        except Exception:
            raise MobileBridgeError('请求体不是合法 JSON')
        finally:
            handler._mb_body_done = True
        try:
            body = json.loads(raw.decode('utf-8'))
        except Exception:
            raise MobileBridgeError('请求体不是合法 JSON')
        if not isinstance(body, dict):
            raise MobileBridgeError('请求体格式错误')
        return body

    def _send(self, handler, code, body, ctype='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(
            body, ensure_ascii=False).encode('utf-8')
        handler.send_response(code)
        handler.send_header('Content-Type', ctype)
        handler.send_header('Content-Length', str(len(data)))
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        handler.wfile.write(data)

    def _control_cors(self, handler, origin):
        if origin:
            handler.send_header('Access-Control-Allow-Origin', origin)
            handler.send_header('Vary', 'Origin')

    def _control_cors_ok(self, handler, origin):
        handler.send_response(204)
        self._control_cors(handler, origin)
        handler.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        handler.send_header('Access-Control-Allow-Headers',
                            'X-Kimi-Mobile-Control, Content-Type')
        handler.send_header('Access-Control-Allow-Private-Network', 'true')
        handler.send_header('Access-Control-Max-Age', '300')
        handler.send_header('Content-Length', '0')
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()

    def _control_json(self, handler, origin, obj, code=200, close=False):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        handler.send_response(code)
        self._control_cors(handler, origin)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control', 'no-store')
        if close:
            handler.send_header('Connection', 'close')
        handler.end_headers()
        handler.wfile.write(body)

    def _control_drain(self, handler):
        """尽量读掉未消费的请求体再答/关连——Windows 上 socket 里留有未读
        数据时 close 会发 RST，客户端可能收不到响应。有界：CL 非法/超
        CONTROL_DRAIN_CAP/TE 存在即直接弃连；慢客户端最多等 DRAIN_TIMEOUT。"""
        if getattr(handler, '_mb_body_done', False):
            return
        if handler.headers.get_all('Transfer-Encoding'):
            return
        cls = handler.headers.get_all('Content-Length') or []
        if len(cls) > 1 or (cls and not _is_strict_digits(cls[0])):
            return
        cl = int(cls[0]) if cls else 0
        if not (0 < cl <= CONTROL_DRAIN_CAP):
            return
        try:
            _read_with_deadline(handler, cl, DRAIN_TIMEOUT)
        except Exception:
            pass
        finally:
            handler._mb_body_done = True

    def _control_error(self, handler, origin, code, msg):
        # 错误统一收口：先排空未读 body 再带 Connection: close 关连——
        # 防 Windows RST 丢响应，也防 body 字节污染后续解析
        handler.close_connection = True
        self._control_drain(handler)
        payload = {'error': msg}
        if msg in _MOBILE_ERROR_CODES:
            payload['error_code'] = msg
        self._control_json(handler, origin, payload, code, close=True)


# ---------------- LAN 代理面 ----------------
class _LanHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, addr, handler_cls, manager):
        self.manager = manager
        self.generation = manager._generation
        self._conn_sem = threading.BoundedSemaphore(MAX_LAN_CONNECTIONS)
        super(_LanHTTPServer, self).__init__(addr, handler_cls)

    def process_request(self, request, client_address):
        if (not self.manager.generation_valid(self.generation, starting=True)
                or not self._conn_sem.acquire(blocking=False)):
            request.close()
            return
        try:
            super(_LanHTTPServer, self).process_request(request, client_address)
        except Exception:
            self._conn_sem.release()
            request.close()

    def shutdown_request(self, request):
        self._conn_sem.release()
        super(_LanHTTPServer, self).shutdown_request(request)


class _LanHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 30
    server_version = 'kimi-mobile'
    sys_version = ''

    def version_string(self):
        # 不回显 BaseHTTP/Python 版本，也不透传上游 Server 头
        return 'kimi-mobile'

    def log_message(self, *a):
        pass

    def _send_status(self, code, message=None):
        # 首个发往客户端的响应起始行——必须在任何字节写出前标记：
        # 之后若下游（客户端）写失败，不再尝试第二个 HTTP 响应（见 _proxy_http）
        # 同时在此收口请求体：响应头缓冲区尚未 flush，若需要弃连还能补上
        # Connection: close（否则残留 body 会被当成下一个请求）。
        self._response_started = True
        self._settle_body_before_response()
        # send_response_only：不自动添加 Server/Date——上游的已剥，
        # 本桥统一输出受控 Server 与 RFC7231 Date
        self.send_response_only(code, message)
        self.send_header('Server', self.version_string())
        self.send_header('Date', formatdate(timeval=None, localtime=False, usegmt=True))

    @property
    def mgr(self):
        return self.server.manager

    # ---------- 连接生命周期（含 keep-alive 空闲期，覆盖整个连接而非单请求） ----------
    def setup(self):
        self._generation = self.server.generation
        super(_LanHandler, self).setup()
        self.mgr.track_socket(self.request, generation=self._generation, starting=True)

    def finish(self):
        try:
            self.mgr.track_socket(self.request, on=False, generation=self._generation)
        except Exception:
            pass
        try:
            super(_LanHandler, self).finish()
        except Exception:
            pass

    def _finish_header_read(self):
        reader = getattr(self, '_header_reader', None)
        if reader is not None:
            self._header_reader = None
            self.rfile = reader.source
            try:
                self.connection.settimeout(reader.old_timeout)
            except OSError:
                pass

    def handle_one_request(self):
        self._header_reader = _HeaderDeadlineReader(self.rfile, self.connection)
        self.rfile = self._header_reader
        try:
            super(_LanHandler, self).handle_one_request()
        finally:
            self._finish_header_read()

    def parse_request(self):
        try:
            return super(_LanHandler, self).parse_request()
        finally:
            self._finish_header_read()

    def handle(self):
        """同 BaseHTTPRequestHandler.handle，但 keep-alive 空闲等待有界：
        rfile.peek 等待由 KA_IDLE_TIMEOUT 限制，请求头另有累计 deadline；
        桥转为非 on（stop）时空闲连接立即让出 conn 名额。"""
        self.close_connection = True
        try:
            try:
                self.handle_one_request()
            except socket.timeout:
                return
            while not self.close_connection:
                # keep-alive 空闲探测：短超时 peek，超时/桥停即让出名额
                try:
                    self.connection.settimeout(KA_IDLE_TIMEOUT)
                    self.rfile.peek(1)
                except Exception:
                    break
                if not self.mgr.generation_valid(self._generation):
                    break
                try:
                    self.connection.settimeout(self.timeout)
                except Exception:
                    break
                try:
                    self.handle_one_request()
                except socket.timeout:
                    break
        except (ConnectionAbortedError, ConnectionResetError,
                BrokenPipeError):
            pass

    # ---------- 入口 ----------
    def do_OPTIONS(self):
        # LAN 面不提供 CORS：预检一律拒
        self._err(403, '不允许跨站预检请求')

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _dispatch(self):
        # socket 生命周期由 setup()/finish() 登记，这里只管单请求；
        # 每请求重置「响应是否已开始」标志（keep-alive 复用同一 handler）
        self._body_done = False
        self._response_started = False
        self._reuse_blocked = False
        self._conn_hdr_sent = False
        try:
            return self._route()
        except MobileBridgeError as e:
            try:
                self._err(400, str(e))
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            try:
                self._err(500, '代理内部错误')
            except Exception:
                pass
        finally:
            # 有路径（如 generation 失效）不发任何响应就返回：此时也必须收口
            # 请求体，否则残留字节会被当成下一个请求。已写出响应的情况由
            # _send_status 在发头前处理（连关闭头才有机会带上）。
            if not self._response_started:
                self._settle_body_before_response()

    def _route(self):
        if not self._request_ok():
            return
        raw_path = (self.path or '').split('?', 1)[0]
        path = self._clean_path()
        if path is None:
            return self._err(400, '请求路径不合法')
        # 升级请求：仅 /api/v1/ws，且必须已配对
        conn_hdr = ','.join(self.headers.get_all('Connection') or [])
        if 'upgrade' in conn_hdr.lower():
            if path == '/api/v1/ws' and self.command == 'GET':
                sid = self._session_sid()
                if sid is None:
                    return self._err(403, '未配对或会话已过期，请重新扫码配对')
                if not self.mgr.ws_acquire(sid, self._generation):
                    return self._err(503, 'WebSocket 连接数已达上限')
                try:
                    return self._ws_tunnel(raw_path, sid)
                finally:
                    self.mgr.ws_release(sid, self._generation)
            return self._err(403, '该升级通道不被允许')
        if path == '/mobile/pair' and self.command == 'GET':
            return self._pair_landing()
        if path == '/mobile/pair/exchange' and self.command == 'POST':
            return self._pair_exchange()
        if path == '/mobile/usage':
            if self.command not in ('GET', 'HEAD'):
                return self._err(404, '接口不存在')
            if not self._session_ok():
                # 相对 Location：同目录换页。relay 下浏览器按 /t/<tid>/mobile/usage
                # 解析到 /t/<tid>/mobile/pair，隧道前缀自动保留（桥看不到 tid）。
                return self._redirect('pair')
            return self._usage_page()
        if path == '/mobile/usage/data':
            if self.command not in ('GET', 'HEAD'):
                return self._err(404, '接口不存在')
            if not self._session_ok():
                return self._err(403, '未配对或会话已过期，请重新扫码配对')
            return self._usage_data()
        if path.startswith('/mobile/'):
            return self._err(404, '接口不存在')
        if not self._session_ok():
            if path.startswith('/api/'):
                return self._err(403, '未配对或会话已过期，请重新扫码配对')
            if self.command == 'GET' and ('.' not in path.rsplit('/', 1)[-1]):
                # 相对 Location：浏览器按自身完整 URL 解析，relay 下自动保住
                # /t/<tid> 前缀（本桥看不到 tid，用 ../ 上探到隧道根再进
                # mobile/pair）；LAN 或前缀已丢时上探到站点根，等效原根绝对
                # 跳转，且 cookie/Referer 兜底路由仍在。
                ups = max(0, path.count('/') - 1)
                return self._redirect('../' * ups + 'mobile/pair')
            return self._err(403, '未配对或会话已过期，请重新扫码配对')
        # 会话有效 + 路径合法（allowlist）才算真实活动并滑动 idle；
        # 匿名/伪造/被拒路径一律不续期。config POST 例外：touch 在
        # _config_post 完成白名单校验后、转发前进行（被拒 body 不算活动）。
        if self._api_allowed(self.command, path):
            self.mgr.session_touch(self._session_sid(), self._generation)
        if path == '/api/v1/config' and self.command == 'POST':
            # 特殊分支（不经 _api_allowed）：白名单小 JSON 补丁才转发
            return self._config_post(raw_path)
        if not self._api_allowed(self.command, path):
            return self._err(403, '该接口不在允许范围')
        return self._proxy_http(raw_path)

    # ---------- 请求安全基线 ----------
    def _request_ok(self):
        context = self.mgr.request_context(self._generation)
        if context is None:
            self._err(503, '手机连接尚未就绪或已停止')
            return False
        mode, expected_origin = context
        # relay 与 internet 共享「公网面」语义：匿名/配对限流、回环来源限制
        # （两者都经本机回连进桥）、配对/WS 白名单；仅 Secure cookie 仅
        # internet（relay 是 http:// 明文，Secure 位会让浏览器拒收）。
        self._internet = mode in ('internet', 'relay')
        self._secure_cookie = mode == 'internet'
        self._client_ip = self.client_address[0]
        # HTTP 只解析头部；这两种邮件正文完整性标记不代表 HTTP 头非法。
        if any(type(d) not in (StartBoundaryNotFoundDefect, MultipartInvariantViolationDefect)
               for d in (getattr(self.headers, 'defects', None) or ())):
            self._err(400, '请求头不合法')
            return False
        for h in ('Host', 'Origin', 'Cookie', 'Content-Length', 'Transfer-Encoding',
                  'CF-Connecting-IP', 'Upgrade', 'Sec-WebSocket-Key',
                  'Sec-WebSocket-Version', 'Sec-WebSocket-Protocol',
                  'Sec-WebSocket-Extensions', 'Sec-Fetch-Site',
                  'Sec-Fetch-Dest', 'Sec-Fetch-Mode'):
            if len(self.headers.get_all(h) or []) > 1:
                self._err(400, '请求头不合法')
                return False
        if self.headers.get_all('Transfer-Encoding'):
            self._err(400, '请求体格式不被支持')
            return False
        cl = self.headers.get('Content-Length')
        if cl is not None and (len(cl) > 10 or not _is_strict_digits(cl)):
            self._err(400, '请求头不合法')
            return False
        host_hdr = self.headers.get('Host') or ''
        if host_hdr != expected_origin.split('://', 1)[1]:
            self._err(403, 'Host 不被允许')
            return False
        if self._internet:
            if not _is_loopback_ip(self.client_address[0]):
                self._err(403, '请求来源不被允许')
                return False
            cf_ip = self.headers.get('CF-Connecting-IP')
            if cf_ip is not None:
                try:
                    normalized = str(ipaddress.ip_address(cf_ip))
                    if '%' in cf_ip or cf_ip != normalized:
                        raise ValueError()
                except ValueError:
                    self._err(400, '请求头不合法')
                    return False
                self._client_ip = normalized
            if not self.mgr.internet_rate_ok(self._generation, self._client_ip,
                                             self._session_sid()):
                self._err(429, '尝试过于频繁，请稍后再试')
                return False
        origin = self.headers.get('Origin')
        upgrade = 'upgrade' in {v.strip().lower() for v in
                                ','.join(self.headers.get_all('Connection') or []).split(',')}
        if upgrade or self.command not in ('GET', 'HEAD'):
            if origin != expected_origin:
                self._err(403, 'Origin 不被允许')
                return False
        elif origin is not None and origin != expected_origin:
            self._err(403, 'Origin 不被允许')
            return False
        sfs = (self.headers.get('Sec-Fetch-Site') or '').lower()
        if sfs == 'cross-site':
            raw = (self.path or '').split('?', 1)[0]
            is_pair_nav = (
                self.command in ('GET', 'HEAD') and raw == '/mobile/pair'
                and (self.headers.get('Sec-Fetch-Dest') or '').lower() == 'document'
                and (self.headers.get('Sec-Fetch-Mode') or '').lower() == 'navigate')
            if not is_pair_nav:
                self._err(403, '跨站请求被拒绝')
                return False
        if len(self.headers) > 64:
            self._err(400, '请求头过多')
            return False
        if not (self.path or '').startswith('/') or self.path.startswith('//'):
            self._err(400, '请求路径不合法')
            return False
        return self.mgr.generation_valid(self._generation)

    def _clean_path(self):
        """校验按解码值做，转发保留 raw percent 编码（UTF-8 中文文件名可用）。

        raw：仅 RFC3986 pchar + 合法 %XX；空白/引号/反斜杠等必须以 %XX 出现；
        %2F/%5C（编码分隔符）单独拒——解码后会多出新路径段绕过路由表。
        decoded：必须是严格 UTF-8，拒控制字符/NUL/反斜杠/点段(. ..)；
        空格、引号、# 等在已编码前提下放行（文件名可含）。
        """
        raw = (self.path or '').split('?')[0]
        if not raw or not raw.startswith('/') or raw.startswith('//'):
            return None
        if _RAW_BAD_CHAR_RE.search(raw) or _ENCODED_SEP_RE.search(raw):
            return None
        if _PCT_RE.sub('', raw).find('%') >= 0:   # % 后面必须紧跟两位 hex
            return None
        try:
            decoded = unquote(raw, errors='strict')   # 非法 UTF-8 序列拒
        except Exception:
            return None
        if '\\' in decoded:
            return None
        for ch in decoded:
            if ord(ch) < 0x20 or ch == '\x7f':    # 控制字符/NUL 一律拒
                return None
        segs = decoded.split('/')
        if any(s == '.' or s == '..' for s in segs):
            return None
        return decoded

    # ---------- 配对 ----------
    def _pair_landing(self):
        # 已配对会话重入（刷新/回退到 /mobile/pair）直接进 SPA——
        # 否则会看到落地页的「配对码缺失」误报。带 kimi_onboarded=1 跳过
        # 桌面登录引导（手机无需该引导；native 侧仅写 localStorage）
        # 已配对重入属真实已认证活动，滑动 idle。
        # Location 用相对路径 '../?kimi_onboarded=1'：本页浏览器路径是
        # <root>/mobile/pair，上探一级即站点根（relay 下 root=/t/<tid>——
        # 桥看不到 tid，相对寻址自动保留前缀；LAN 下等效原 '/' 跳转）。
        if self._session_ok():
            self.mgr.session_touch(self._session_sid(), self._generation)
            return self._redirect('../?kimi_onboarded=1')
        ip = self._client_ip
        if not self.mgr._exchange_rate_ok(ip):
            return self._err(429, '尝试过于频繁，请稍后再试')
        body = _PAIR_LANDING_HTML.encode('utf-8')
        self._send_raw(200, body, 'text/html; charset=utf-8')

    def _pair_exchange(self):
        ip = self._client_ip
        if not self.mgr._exchange_rate_ok(ip):
            return self._err(429, '尝试过于频繁，请稍后再试')
        body = self._read_body(4096)
        if body is None:
            return
        try:
            data = json.loads(body.decode('utf-8'))
            token = data.get('token') if isinstance(data, dict) else None
        except Exception:
            token = None
        try:
            sid = self.mgr.exchange_pair_token(ip, token, self._generation)
        except MobileBridgeError as e:
            return self._err(400, str(e))
        if not sid or not self.mgr.session_valid(sid, self._generation):
            return self._err(403, '配对码无效或已过期')
        payload = json.dumps(
            {'ok': True,
             'redirect': '/?kimi_onboarded=1#token=' + PLACEHOLDER_CREDENTIAL},
            ensure_ascii=False).encode('utf-8')
        self._send_status(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Set-Cookie',
                         '%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s'
                         % (SESSION_COOKIE, sid, SESSION_MAX_AGE_SECONDS,
                            '; Secure' if self._secure_cookie else ''))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self._end_headers()
        self.wfile.write(payload)

    def _session_ok(self):
        return self._session_sid() is not None

    def _session_sid(self):
        cookie = self.headers.get('Cookie') or ''
        values = []
        for part in cookie.split(';'):
            name, _, val = part.strip().partition('=')
            if name == SESSION_COOKIE:
                values.append(val)
        if len(values) != 1 or not self.mgr.session_valid(values[0], self._generation):
            return None
        return values[0]

    # ---------- 手机用量页（只读，同源自包含） ----------
    _USAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; "
                  "style-src 'unsafe-inline'; connect-src 'self'; "
                  "img-src 'none'; font-src 'none'; base-uri 'none'; "
                  "form-action 'none'; frame-ancestors 'none'")

    def _usage_page(self):
        # 已配对只读页：自包含模板（无外部/loopback 引用），滑动静默续期交给
        # 数据轮询（data 接口 touch）；页面本身打开也计真实活动
        if _mobile_usage is None:
            return self._err(503, '用量功能不可用')
        self.mgr.session_touch(self._session_sid(), self._generation)
        body = _mobile_usage.USAGE_PAGE_HTML.encode('utf-8')
        token = self.mgr.inject_token(self._generation)
        body = _redact_token(body, token)
        self._send_raw(200, body, 'text/html; charset=utf-8', extra_headers=[
            ('Content-Security-Policy', self._USAGE_CSP),
            ('Referrer-Policy', 'no-referrer')])

    def _usage_data(self):
        if _mobile_usage is None:
            return self._err(503, '用量数据暂时不可用')
        try:
            data = _mobile_usage.fetch_usage()
        except Exception:
            # 不回显 raw 错误（端口/地址/Errno 一律不外发）
            return self._err(503, '用量数据暂时不可用')
        body = json.dumps(data, ensure_ascii=False,
                          allow_nan=False).encode('utf-8')
        token = self.mgr.inject_token(self._generation)
        body = _redact_token(body, token)
        # 数据接口成功响应才计真实已认证活动（失败/被拒不滑动静默 idle）
        self.mgr.session_touch(self._session_sid(), self._generation)
        self._send_raw(200, body, 'application/json; charset=utf-8')

    # ---------- API allowlist ----------
    @staticmethod
    def _api_allowed(method, path):
        if not path.startswith('/api/'):
            # 非 /api/ 面＝静态/SPA：只放行白名单，只读
            return method in ('GET', 'HEAD') and _static_allowed(path)
        for p in _API_DENY_PREFIXES:
            # 段精确 + 冒号动作前缀（/api/v1/fs::browse 命中 /api/v1/fs）
            if (path == p or path.startswith(p + '/')
                    or path.startswith(p + ':')):
                return False
        if '/terminals' in path:
            return False
        if (path == '/api/v1/sessions'
                or path.startswith('/api/v1/sessions/')):
            return _LanHandler._session_api_allowed(method, path)
        if path == '/api/v1/files' or path.startswith('/api/v1/files/'):
            return _LanHandler._files_api_allowed(method, path)
        if path in _V2_READ_EXACT:
            return method in ('GET', 'HEAD')
        if path in _V2_WRITE_EXACT:
            return method == 'POST'
        for p in _API_READ_ONLY:
            if path == p or path.startswith(p + '/') or path.startswith(p + ':'):
                return method in ('GET', 'HEAD')
        return False

    @staticmethod
    def _files_api_allowed(method, path):
        # native: POST /files 上传，GET /files/{file_id} 下载；其余一律拒
        if path == '/api/v1/files':
            return method == 'POST'
        rest = path[len('/api/v1/files/'):]
        if '/' in rest or ':' in rest or not rest:
            return False
        return method in ('GET', 'HEAD')

    @staticmethod
    def _session_api_allowed(method, path):
        """native 路由逐条对照：known GET 读段 + 精确 POST 写面；
        未知子资源/未知动作一律拒（不再前缀放行）。"""
        if path == '/api/v1/sessions':
            return method in ('GET', 'HEAD', 'POST')   # 列表读 + 新建会话
        segs = path.split('/')[4:]                     # ['',api,v1,sessions,…]
        sid = segs[0]
        if not sid:
            return False
        sid_id, sep, sid_action = sid.partition(':')
        if not sid_id:
            return False
        if sep:                                        # POST /sessions/{id}:action
            if len(segs) != 1:
                return False
            return method == 'POST' and sid_action in _SESSION_POST_ACTIONS
        if len(segs) == 1:                             # /sessions/{id} 详情
            return method in ('GET', 'HEAD')
        seg1 = segs[1]
        if seg1 == 'terminals':                        # 终端入口全方法拒
            return False
        if ':' in seg1:
            # /sessions/{id}/prompts:steer 与 fs:<action>（native POST 读动作，
            # 均为单冒号路由）
            if len(segs) != 2:
                return False
            if seg1 == 'prompts:steer':
                return method == 'POST'
            if seg1.startswith('fs:'):
                return (method in ('GET', 'HEAD', 'POST')
                        and seg1[3:] in _SESSION_FS_READ_ACTIONS)
            return False
        if seg1 == 'fs':                               # GET /sessions/{id}/fs/{*}
            return method in ('GET', 'HEAD')
        if method in ('GET', 'HEAD'):
            if len(segs) == 2:
                return seg1 in _SESSION_GET_SEGMENTS
            if len(segs) == 3:
                if seg1 in ('approvals', 'messages', 'tasks', 'media'):
                    return ':' not in segs[2]
                if seg1 == 'file-history':
                    return segs[2] in ('changes', 'content')
                if seg1 == 'transcript':
                    return segs[2] in ('ops', 'user-messages', 'plan')
                return False
            return seg1 == 'fs'                  # /fs/* 任意深度文件下载
        if method != 'POST':
            return False
        if len(segs) == 2:
            # export 仅 POST（导出是写触发动作，返回 ZIP）；GET 走上方只读表，
            # 不在 _SESSION_GET_SEGMENTS 内故仍拒。
            return seg1 in ('prompts', 'profile', 'children', 'export')
        if len(segs) != 3:
            return False
        tail = segs[2]
        if not tail:
            return False
        if seg1 == 'approvals':                        # POST approvals/{id} 裁决
            return ':' not in tail
        if seg1 == 'title':
            return tail == 'generate'
        tid, sep2, act = tail.rpartition(':')
        if seg1 == 'prompts':
            return bool(sep2) and bool(tid) and act in _SESSION_PROMPT_ACTIONS
        if seg1 == 'questions':                        # bare=resolve 或 :resolve/:dismiss
            if not sep2:
                return True
            return bool(tid) and act in _SESSION_QUESTION_ACTIONS
        if seg1 == 'tasks':
            return bool(sep2) and bool(tid) and act in _SESSION_TASK_ACTIONS
        return False

    def _body_limits(self, raw_path):
        """(idle, total) 体读取策略：只对精确 POST /api/v1/files 放宽到可容纳
        蜂窝慢速上行；其余路径（含控制面）保持 BODY_DEADLINE_SECONDS。"""
        path = (raw_path or '').split('?', 1)[0]
        if self.command == 'POST' and path == '/api/v1/files':
            return UPLOAD_BODY_IDLE_SECONDS, UPLOAD_BODY_MAX_SECONDS
        return None, None

    # ---------- 读取与转发 ----------
    def _config_post(self, raw_path):
        """POST /api/v1/config 的手机写面（此处已认证）。白名单：
        default_model（非空 str，≤256，无控制字符）、
        auto_session_title/default_plan_mode（严格 bool）、
        thinking 非空 dict（enabled 严格 bool；effort 严格枚举）。
        校验全部通过才计会话活动并滑动 idle，然后把已读 body 原样转发上游。"""
        body = self._read_body(CONFIG_POST_MAX_BODY)
        if body is None:
            return
        try:
            data = json.loads(body.decode('utf-8'),
                              object_pairs_hook=_json_no_dup_object)
        except Exception:
            return self._err(400, '配置请求体必须是合法 JSON 对象且不得含重复键')
        if not isinstance(data, dict) or not data:
            return self._err(400, '配置补丁必须是非空 JSON 对象')
        allowed = _CONFIG_POST_BOOL_KEYS | {'default_model', 'thinking'}
        for key in data:
            if key not in allowed:
                # 不回显攻击者键名（可含控制字符/超长）
                return self._err(403, '手机端仅支持默认模型、思考偏好、计划模式和自动标题；'
                                      '权限、密钥等全局配置请在电脑端修改')
        if 'default_model' in data:
            v = data['default_model']
            if (not isinstance(v, str) or not v.strip() or len(v) > 256
                    or any(ord(c) < 0x20 or c == '\x7f' for c in v)):
                return self._err(400, 'default_model 必须是非空且不超过 256 字符的字符串，不含控制字符')
        for key in _CONFIG_POST_BOOL_KEYS:
            if key in data and not isinstance(data[key], bool):
                return self._err(400, '%s 必须是布尔值' % key)
        if 'thinking' in data:
            v = data['thinking']
            if not isinstance(v, dict) or not v:
                return self._err(400, 'thinking 必须是非空对象')
            for k in v:
                if k not in _CONFIG_POST_THINKING_KEYS:
                    return self._err(403, '手机端仅支持默认模型、思考偏好、计划模式和自动标题；'
                                          '权限、密钥等全局配置请在电脑端修改')
            if ('enabled' in v and not isinstance(v['enabled'], bool)):
                return self._err(400, 'thinking.enabled 必须是布尔值')
            if ('effort' in v and (not isinstance(v['effort'], str)
                                   or v['effort'] not in _CONFIG_POST_EFFORTS)):
                return self._err(400, 'thinking.effort 取值不被支持')
        # 白名单全部通过才计为真实已认证活动（被拒 body 不滑动 idle）
        self.mgr.session_touch(self._session_sid(), self._generation)
        return self._proxy_http(raw_path, body=body)

    def _read_body(self, limit, idle=None, total=None):
        if self.headers.get_all('Transfer-Encoding'):
            self._err(400, '请求体格式不被支持')
            return None
        cls = self.headers.get_all('Content-Length') or []
        if len(cls) > 1:
            self._err(400, '请求头不合法')
            return None
        if cls and not _is_strict_digits(cls[0]):  # 严格 ASCII 纯数字：'+5'/'5x'/空白全拒
            self._err(400, '请求头不合法')
            return None
        n = int(cls[0]) if cls else 0
        if n > limit:
            self._err(413, '请求体过大')
            return None
        try:
            data = (_read_with_deadline(
                self, n, BODY_DEADLINE_SECONDS if total is None else total,
                idle=idle) if n else b'')
            self._body_done = True
            return data
        except Exception:
            self._body_done = True
            self._err(400, '读取请求体失败')
            return None

    def _forward_headers(self):
        out = []
        conn_tokens = set()
        for hv in (self.headers.get_all('Connection') or []):
            for t in hv.split(','):
                t = t.strip().lower()
                if t:
                    conn_tokens.add(t)
        for name in self.headers.keys():
            ln = name.lower()
            if (ln in _HOP_BY_HOP or ln in conn_tokens
                    or ln.startswith(('x-forwarded-', 'cf-'))
                    or ln == 'x-kimi-mobile-control'):
                continue
            if ln in ('sec-websocket-key', 'sec-websocket-version',
                      'sec-websocket-protocol', 'sec-websocket-extensions'):
                continue
            val = self.headers.get(name)
            if val is None:
                continue
            out.append((name, val))
        return out

    def _proxy_http(self, raw_path, body=None):
        # body=None → 通用路径自行读体；已读字节由调用方（如 _config_post）传入
        if body is None:
            body = self._read_body(PROXY_MAX_REQUEST_BODY,
                                   *self._body_limits(raw_path))
            if body is None:
                return
        sid = self._session_sid()
        snapshot = self.mgr.upstream_snapshot(self._generation)
        if not snapshot or not sid:
            return self._err(503, '手机连接已停止')
        host, port, token = snapshot
        conn = None
        tracked_sock = None
        try:
            conn = http.client.HTTPConnection(host, port, timeout=PROXY_UPSTREAM_TIMEOUT)
            conn.connect()
            tracked_sock = conn.sock
            if tracked_sock is None or not self.mgr.track_socket(
                    tracked_sock, generation=self._generation):
                return self._err(503, '手机连接已停止')
            headers = dict(self._forward_headers())
            headers['Host'] = '%s:%d' % (host, port)
            headers['Authorization'] = 'Bearer %s' % token
            headers['Accept-Encoding'] = 'identity'
            headers.pop('Content-Length', None)
            if body or self.command in ('POST', 'PUT', 'PATCH'):
                headers['Content-Length'] = str(len(body))
            conn.putrequest(self.command, self._raw_target(raw_path),
                            skip_host=True, skip_accept_encoding=True)
            for k, v in headers.items():
                conn.putheader(k, v)
            if not self.mgr.first_authorization_valid(self._generation, snapshot, sid):
                return self._err(503, '手机连接已停止')
            conn.endheaders(body)
            resp = conn.getresponse()
            encodings = resp.headers.get_all('Content-Encoding') or []
            if (getattr(resp.headers, 'defects', None) or len(encodings) > 1
                    or (encodings and encodings[0].strip(' \t').lower() != 'identity')):
                return self._err(502, '上游响应不被允许')
            response_deadline = time.monotonic() + PROXY_UPSTREAM_TIMEOUT
            data = bytearray()
            while len(data) <= PROXY_MAX_RESPONSE_BODY:
                if not self.mgr.authorization_valid(self._generation, snapshot, sid):
                    return
                remaining = response_deadline - time.monotonic()
                if remaining <= 0:
                    return self._err(502, '上游响应超时')
                tracked_sock.settimeout(remaining)
                chunk = resp.read1(min(65536, PROXY_MAX_RESPONSE_BODY + 1 - len(data)))
                if not chunk:
                    if resp.length not in (0, None):
                        return self._err(502, '上游响应不完整')
                    break
                data.extend(chunk)
            if len(data) > PROXY_MAX_RESPONSE_BODY:
                return self._err(502, '响应过大')
            data = bytes(data)
            # 上游 3xx/Location/Refresh 一律不外发：防借跳转把客户端（与凭据
            # 上下文）带离本桥范围；native 正常不返回重定向
            if 300 <= resp.status < 400:
                return self._err(502, '上游响应不被允许')
            content_type = ''
            rh = []
            # 先解析上游 Connection 提名的 hop-by-hop token，再统一剥
            skip = set(_RESP_HOP_BY_HOP)
            for k, v in resp.getheaders():
                if k.lower() == 'connection':
                    for t in v.split(','):
                        t = t.strip().lower()
                        if t:
                            skip.add(t)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in skip or lk.startswith('x-forwarded-') or lk.startswith('x-accel-'):
                    continue
                if lk == 'content-type':
                    content_type = v
                rh.append((k, v))
            # M6：工作区来的 HTML/SVG 不得被当同源文档执行。在凭据体检前并入
            # 出向头，使体检覆盖最终真正发出的头。
            guard = _executable_content_guard(self._clean_path() or raw_path,
                                              content_type)
            if guard:
                rh = _merge_guard_headers(rh, guard)
            # 原生 SPA index：剥插件注入 tag（含 usage/remote/mobile/vendor-qr），
            # 保留原生 head inline 与 module 脚本；随后追加同源 /mobile/usage 入口
            if 'text/html' in content_type.lower():
                try:
                    data = _INJECT_TAG_RE.sub(
                        '\n', data.decode('utf-8', 'replace')).encode('utf-8')
                    # 仅 SPA index（非 /api/ 路径）追加同源 /mobile/usage 入口；
                    # /api/ 提供的 HTML 内容保持字节不变
                    if not raw_path.startswith('/api/'):
                        if b'<head>' in data:
                            data = data.replace(b'<head>', b'<head>' + _RANDOM_UUID_SHIM, 1)
                        if b'</body>' in data:
                            data = data.replace(b'</body>', _USAGE_ENTRY_HTML + b'</body>', 1)
                        else:
                            data += _USAGE_ENTRY_HTML
                except Exception:
                    pass
            # 出向体检：响应体/响应头中若出现真实 server.token 的原文、
            # percent 编码或 base64 形态，做脱敏替换而非整包 502——transcript
            # 等读面会把含 token 的会话历史透给手机端，属数据内容非凭据外泄。
            data = _redact_token(data, token)
            rh = [(k, _redact_token(v.encode('latin-1', 'replace'),
                                   token).decode('latin-1'))
                  for k, v in rh]
            try:
                response_json = json.loads(data)
            except (ValueError, TypeError):
                normalized = b''
            else:
                normalized = json.dumps(response_json, ensure_ascii=False).encode('utf-8')
                if len(normalized) > PROXY_MAX_RESPONSE_BODY:
                    return self._err(502, '响应过大')
            # 规范化重编码可能把 token 变成新的转义形态，再擦一遍兜底
            normalized = _redact_token(normalized, token)
            if not self.mgr.authorization_valid(self._generation, snapshot, sid):
                return
            self._send_status(resp.status)
            for k, v in rh:
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self._end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)
        except MobileBridgeError:
            raise
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 上游在上行/响应阶段关闭（endheaders/getresponse/read）时向客户端
            # 回一个受控 502；BrokenPipe/Reset 也可能来自 endheaders 的上游写，
            # 不只下游 wfile——因此用 _response_started 区分：响应已开始
            # （200 成功分支已发状态行）后的下游写失败安静吞掉，绝不第二次
            # 响应；响应尚未开始的上游失败恰好补一个 502。
            if not getattr(self, '_response_started', False):
                try:
                    self._err(502, '上游不可达')
                except Exception:
                    pass
        except Exception:
            if not getattr(self, '_response_started', False):
                try:
                    self._err(502, '上游不可达')
                except Exception:
                    pass
        finally:
            try:
                if tracked_sock is not None:
                    self.mgr.track_socket(tracked_sock, on=False, generation=self._generation)
            except Exception:
                pass
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

    def _raw_target(self, raw_path):
        q = (self.path or '')
        i = q.find('?')
        return raw_path + (q[i:] if i >= 0 else '')

    # ---------- WebSocket 隧道 + 帧过滤 ----------
    def _ws_tunnel(self, raw_path, sid):
        snapshot = self.mgr.upstream_snapshot(self._generation)
        if not snapshot or not self.mgr.session_valid(sid, self._generation):
            return self._err(503, '手机连接已停止')
        if (self.headers.get('Upgrade') or '').lower() != 'websocket':
            return self._err(400, '升级请求不合法')
        if int(self.headers.get('Content-Length') or '0'):
            return self._err(400, '升级请求不合法')
        key = self.headers.get('Sec-WebSocket-Key')
        if not key or len(key) > 64:
            return self._err(400, '升级请求不合法')
        try:
            if len(base64.b64decode(key, validate=True)) != 16:
                return self._err(400, '升级请求不合法')
        except Exception:
            return self._err(400, '升级请求不合法')
        if (self.headers.get('Sec-WebSocket-Version') or '') != '13':
            return self._err(400, '升级请求不合法')
        offered = [p.strip() for p in
                   (self.headers.get('Sec-WebSocket-Protocol') or '').split(',') if p.strip()]
        client_proto = next((p for p in offered if p.startswith('kimi-code.bearer.')), None)
        if client_proto is not None and not _WS_PROTO_TOKEN_RE.fullmatch(client_proto):
            return self._err(400, '升级请求不合法')
        host, port, token = snapshot
        upstream = None
        client = self.connection
        handshake_done = False
        try:
            if not self.mgr.track_socket(client, generation=self._generation):
                return
            upstream = socket.create_connection((host, port), timeout=15)
            upstream.settimeout(WS_RECV_POLL)
            if not self.mgr.track_socket(upstream, generation=self._generation):
                return self._err(503, '手机连接已停止')
            up_key = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
            q = self._raw_target(raw_path)
            req = ('GET %s HTTP/1.1\r\n'
                   'Host: %s:%d\r\n'
                   'Upgrade: websocket\r\n'
                   'Connection: Upgrade\r\n'
                   'Sec-WebSocket-Key: %s\r\n'
                   'Sec-WebSocket-Version: 13\r\n'
                   'Authorization: Bearer %s\r\n'
                   '\r\n' % (q, host, port, up_key, token))
            def valid():
                return self.mgr.authorization_valid(self._generation, snapshot, sid)
            if (not self.mgr.first_authorization_valid(self._generation, snapshot, sid)
                    or not self._ws_send_all(upstream, req.encode('latin-1'), valid)):
                return
            head = b''
            deadline = time.monotonic() + 15.0
            while b'\r\n\r\n' not in head:
                if not valid() or time.monotonic() >= deadline:
                    return self._err(502, '上游 WebSocket 握手失败')
                try:
                    chunk = upstream.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return self._err(502, '上游 WebSocket 握手失败')
                head += chunk
                if len(head) > WS_UPGRADE_MAX_HEADER:
                    return self._err(502, '上游 WebSocket 握手失败')
            head, rest = head.split(b'\r\n\r\n', 1)
            lines = head.split(b'\r\n')
            if not re.fullmatch(rb'HTTP/1\.[01] 101(?: [^\r\n]*)?', lines[0]):
                return self._err(502, '上游拒绝 WebSocket 连接')
            fields = {}
            for line in lines[1:]:
                name, sep, value = line.partition(b':')
                if (not sep or not re.fullmatch(rb'[A-Za-z0-9-]+', name)
                        or any(c < 32 and c != 9 for c in value)):
                    return self._err(502, '上游握手无效')
                name = name.lower()
                if name in fields:
                    return self._err(502, '上游握手无效')
                fields[name] = value.strip()
            expect = base64.b64encode(hashlib.sha1((up_key + _WS_GUID).encode('ascii')).digest())
            if (fields.get(b'sec-websocket-accept') != expect
                    or fields.get(b'upgrade', b'').lower() != b'websocket'
                    or b'upgrade' not in {p.strip().lower() for p in fields.get(b'connection', b'').split(b',')}
                    or b'sec-websocket-extensions' in fields
                    or b'sec-websocket-protocol' in fields):
                return self._err(502, '上游握手无效')
            if not valid():
                return
            accept = base64.b64encode(
                hashlib.sha1((key + _WS_GUID).encode('ascii')).digest()).decode('ascii')
            resp = ['HTTP/1.1 101 Switching Protocols', 'Upgrade: websocket',
                    'Connection: Upgrade', 'Sec-WebSocket-Accept: %s' % accept]
            if client_proto:
                resp.append('Sec-WebSocket-Protocol: %s' % client_proto)
            self.wfile.write(('\r\n'.join(resp) + '\r\n\r\n').encode('latin-1'))
            self.wfile.flush()
            handshake_done = True
            client.settimeout(WS_RECV_POLL)
            self.close_connection = True
            self._pump_bidirectional(client, upstream, sid, token, rest)
        except MobileBridgeError:
            raise
        except Exception:
            if not handshake_done:
                try:
                    self._err(502, '上游 WebSocket 握手失败')
                except Exception:
                    pass
        finally:
            self.mgr.track_socket(client, on=False, generation=self._generation)
            if upstream is not None:
                self.mgr.track_socket(upstream, on=False, generation=self._generation)
                try:
                    upstream.close()
                except Exception:
                    pass

    @staticmethod
    def _ws_recv_exact(sock, n, buf, can_wait, deadline=None):
        """从缓冲/套接字精确取 n 字节（缓冲优先），返回 (data, rest_buf) 或
        (None, rest_buf)。sock 已设 WS_RECV_POLL 超时：timeout 属正常，
        查 can_wait()/deadline 后继续等；缓冲与已收进度跨 timeout 保留——
        绝不重读已消费字节。deadline 为累计到达上限（monotonic）。"""
        out = bytearray()
        if buf:
            take = min(n, len(buf))
            out += buf[:take]
            buf = buf[take:]
        while len(out) < n:
            if deadline is not None and time.monotonic() > deadline:
                return None, buf
            if not can_wait():
                return None, buf
            try:
                chunk = sock.recv(n - len(out))
            except socket.timeout:
                continue                       # 轮询唤醒，进度保留在 out/buf
            if not chunk:
                return None, buf
            out += chunk
        return bytes(out), buf

    @staticmethod
    def _ws_send_all(sock, data, can_wait):
        """send 循环：WS_RECV_POLL 超时下 send 可能 partial/超时，超时重试。
        返回 False=连接不可用或 can_wait() 终止。"""
        mv = memoryview(data)
        deadline = time.monotonic() + WS_FRAME_DEADLINE
        while mv:
            if not can_wait() or time.monotonic() >= deadline:
                return False
            try:
                sent = sock.send(mv)
            except socket.timeout:
                continue
            except (InterruptedError, BlockingIOError):
                continue
            if sent <= 0:
                return False
            mv = mv[sent:]
        return True

    def _ws_read_frame(self, source, buf, can_wait, masked_expected, message_deadline=None):
        first, buf = self._ws_recv_exact(source, 1, buf, can_wait, message_deadline)
        if first is None:
            return None
        deadline = time.monotonic() + WS_FRAME_DEADLINE
        if message_deadline is not None:
            deadline = min(deadline, message_deadline)
        second, buf = self._ws_recv_exact(source, 1, buf, can_wait, deadline)
        if second is None:
            return None
        b0, b1 = first[0], second[0]
        fin, opcode, masked = bool(b0 & 128), b0 & 15, bool(b1 & 128)
        length = b1 & 127
        if b0 & 112 or masked != masked_expected or opcode not in (0, 1, 2, 8, 9, 10):
            return None
        if opcode >= 8 and (not fin or length > 125):
            return None
        if length in (126, 127):
            marker = length
            ext, buf = self._ws_recv_exact(source, 2 if marker == 126 else 8,
                                           buf, can_wait, deadline)
            if ext is None:
                return None
            length = int.from_bytes(ext, 'big')
            if (marker == 126 and length < 126) or (marker == 127 and (length < 65536 or ext[0] & 128)):
                return None
        if length > _WS_MAX_MESSAGE:
            return None
        mask = None
        if masked:
            mask, buf = self._ws_recv_exact(source, 4, buf, can_wait, deadline)
            if mask is None:
                return None
        payload, buf = self._ws_recv_exact(source, length, buf, can_wait, deadline)
        if payload is None:
            return None
        if mask is not None:
            payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        if opcode == 8 and payload:
            if len(payload) == 1:
                return None
            code = int.from_bytes(payload[:2], 'big')
            if code not in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014) and not 3000 <= code <= 4999:
                return None
            payload[2:].decode('utf-8', 'strict')
        return fin, opcode, payload, buf

    @staticmethod
    def _ws_downstream_frame(opcode, payload):
        length = len(payload)
        if length < 126:
            head = bytes([128 | opcode, length])
        elif length < 65536:
            head = bytes([128 | opcode, 126]) + length.to_bytes(2, 'big')
        else:
            head = bytes([128 | opcode, 127]) + length.to_bytes(8, 'big')
        return head + payload

    def _ws_message_pump(self, source, target, done, to_owner, token=None, initial=b''):
        def can_wait():
            return (not done.is_set() and self.mgr.generation_valid(self._generation)
                    and (not hasattr(self, '_ws_sid')
                         or self.mgr.session_valid(self._ws_sid, self._generation)))
        buf = initial
        fragments = bytearray()
        frag_opcode = None
        message_deadline = None
        encode = self._ws_upstream_frame if to_owner else self._ws_downstream_frame
        try:
            while can_wait():
                frame = self._ws_read_frame(source, buf, can_wait, to_owner, message_deadline)
                if frame is None:
                    break
                fin, opcode, payload, buf = frame
                if not to_owner and _contains_token(payload, token):
                    break
                if opcode >= 8:
                    if not self._ws_send_all(target, encode(opcode, payload), can_wait):
                        break
                    if opcode == 8:
                        break
                    continue
                if opcode in (1, 2):
                    if frag_opcode is not None:
                        break
                    if not fin:
                        frag_opcode, fragments = opcode, bytearray(payload)
                        message_deadline = time.monotonic() + WS_FRAME_DEADLINE
                        continue
                    message_opcode, message = opcode, payload
                else:
                    if frag_opcode is None or len(fragments) + len(payload) > _WS_MAX_MESSAGE:
                        break
                    fragments.extend(payload)
                    if not fin:
                        continue
                    message_opcode, message = frag_opcode, bytes(fragments)
                    frag_opcode, fragments, message_deadline = None, bytearray(), None
                text = message.decode('utf-8', 'strict') if message_opcode == 1 or to_owner else None
                if to_owner:
                    obj = json.loads(text)
                    if not isinstance(obj, dict) or not isinstance(obj.get('type'), str):
                        break
                    mtype = obj['type']
                    if mtype.startswith(_WS_TERM_PREFIX):
                        continue
                    if mtype not in _WS_ALLOWED_TYPES:
                        break
                    # 实际合法上行消息活动才算会话活动（terminal_* 拦截帧/非法
                    # 消息/空转/watchdog 复查都不算）
                    if hasattr(self, '_ws_sid'):
                        self.mgr.session_touch(self._ws_sid, self._generation)
                else:
                    if _contains_token(message, token):
                        break
                    if text is not None:
                        try:
                            normalized = json.dumps(json.loads(text), ensure_ascii=False).encode('utf-8')
                        except (ValueError, TypeError):
                            normalized = b''
                        if _contains_token(normalized, token):
                            break
                        # owner 应用层 ping：桥代答 pong（直接回上游，掩码帧），
                        # 不再透传给手机——手机不参与心跳，避免 20s heartbeat timeout。
                        try:
                            if json.loads(text).get('type') == 'ping':
                                if not self._ws_send_all(source,
                                        self._ws_upstream_frame(1, b'{"type":"pong"}'),
                                        can_wait):
                                    break
                                continue
                        except (ValueError, TypeError, AttributeError):
                            pass
                if not self._ws_send_all(target, encode(message_opcode, message), can_wait):
                    break
        except Exception:
            pass
        finally:
            done.set()

    def _ws_client_pump(self, client, upstream, done):
        self._ws_message_pump(client, upstream, done, True)

    def _ws_server_pump(self, upstream, client, done, token, initial=b''):
        self._ws_message_pump(upstream, client, done, False, token, initial)

    @staticmethod
    def _ws_upstream_frame(opcode, payload):
        """发往上游的帧：桥对 owner 是客户端，RFC6455 必须掩码。"""
        ln = len(payload)
        if ln < 126:
            head = bytes([0x80 | opcode, 0x80 | ln])
        elif ln < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + ln.to_bytes(2, 'big')
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + ln.to_bytes(8, 'big')
        mask = secrets.token_bytes(4)
        return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    def _pump_bidirectional(self, client, upstream, sid, token, initial=b''):
        done = threading.Event()
        self._ws_sid = sid
        # owner 心跳协议：握手后须先发 client_hello，再对每个应用层 ping 回 pong，
        # 否则 owner 在 ~20s 无心跳时以 1001 heartbeat timeout 主动关闭隧道。
        # 手机端 HTTP-only、不懂此协议，故由桥代为握手；下行 ping 由 server pump 代答。
        try:
            self._ws_send_all(upstream,
                self._ws_upstream_frame(1, b'{"type":"client_hello"}'),
                lambda: not done.is_set())
        except Exception:
            pass

        def session_watchdog():
            while not done.wait(WS_SESSION_RECHECK_SECONDS):
                if self.mgr.session_valid(sid, self._generation):
                    continue
                done.set()
                for sock in (client, upstream):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                return

        thread = threading.Thread(target=self._ws_server_pump,
            args=(upstream, client, done, token, initial), daemon=True)
        thread.start()
        watcher = threading.Thread(target=session_watchdog, daemon=True)
        watcher.start()
        try:
            self._ws_client_pump(client, upstream, done)
        finally:
            done.set()
            for sock in (client, upstream):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
            thread.join(timeout=WS_RECV_POLL + 1.0)
            watcher.join(timeout=WS_RECV_POLL + 1.0)

    # ---------- 输出 ----------
    def _send_raw(self, code, body, ctype, extra_headers=None):
        self._send_status(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self._end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _redirect(self, location):
        self._send_status(302)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self._end_headers()

    _DRAIN_CAP = 4 * 1024 * 1024

    _BODY_UNCONSUMED = 0        # 未消费：先尝试有界排空
    _BODY_CONSUMED = 1          # 已完整消费/无体：沿用既有连接行为
    _BODY_MUST_CLOSE = 2        # 非法/超限/TE：不读也不复用，必须弃连

    def _body_state(self):
        """按请求头判定未消费请求体的处置类别（纯判定，不做 IO）。"""
        if getattr(self, '_body_done', False):
            return self._BODY_CONSUMED
        if self.headers.get_all('Transfer-Encoding'):
            return self._BODY_MUST_CLOSE     # 长度未知：既不读也不复用
        cls = self.headers.get_all('Content-Length') or []
        if not cls:
            return self._BODY_CONSUMED       # 无体请求：可直接复用
        if len(cls) > 1 or not _is_strict_digits(cls[0]):
            return self._BODY_MUST_CLOSE
        n = int(cls[0])
        if n == 0:
            return self._BODY_CONSUMED       # 显式空体：无需排空
        if n > self._DRAIN_CAP:
            return self._BODY_MUST_CLOSE
        return self._BODY_UNCONSUMED

    def _settle_body_before_response(self):
        """响应头写出前收口请求体：已消费（含无体、上传被 proxy 读走）则维持
        既有连接行为；合法小体有限排空后可复用；非法/超限/TE/排空失败一律
        弃连。排空只多等 DRAIN_TIMEOUT，慢客户端拖不住 handler。"""
        state = self._body_state()
        if state == self._BODY_CONSUMED:
            return
        if state == self._BODY_UNCONSUMED and self._drain_body():
            return
        self.close_connection = True
        self._reuse_blocked = True

    def _emit_connection_header(self):
        """响应头收尾前补连接头：需弃连且本次还没显式写过才补，避免重复。"""
        if (getattr(self, '_reuse_blocked', False)
                and not getattr(self, '_conn_hdr_sent', False)):
            self.send_header('Connection', 'close')

    def _end_headers(self):
        self._emit_connection_header()
        self.end_headers()

    def _drain_body(self):
        """读掉未消费的合法小请求体；返回是否完整排空。有界：CL 非法/超限/
        TE 存在即跳过不读；慢客户端最多等 DRAIN_TIMEOUT 秒。"""
        if self._body_state() != self._BODY_UNCONSUMED:
            return False
        cl = int((self.headers.get_all('Content-Length') or ['0'])[0])
        ok = False
        try:
            _read_with_deadline(self, cl, DRAIN_TIMEOUT)
            ok = True
        except Exception:
            pass
        finally:
            # 排空尝试只做一次（_err 与 _settle 都可能触发）
            self._body_done = True
        return ok

    def _err(self, code, msg):
        # 提前错误统一关连接：先排空未消费请求体（否则 RST 丢响应/污染复用）
        self.close_connection = True
        self._drain_body()
        close_hdr = [('Connection', 'close')]
        self._conn_hdr_sent = True
        if code == 429:
            close_hdr.append(('Retry-After', '1'))
        if (self.path or '').startswith('/api/'):
            # envelope：error 兼容旧前端；code=HTTP 状态码，msg/data 对齐 native
            body = json.dumps({'error': msg, 'code': code, 'msg': msg,
                               'data': None}, ensure_ascii=False).encode('utf-8')
            return self._send_raw(code, body, 'application/json; charset=utf-8',
                                  extra_headers=close_hdr)
        body = ('<!doctype html><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width,initial-scale=1"><title>%d</title>'
                '<p style="font-family:sans-serif;padding:2em">%s</p>'
                % (code, msg.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
                ).encode('utf-8')
        self._send_raw(code, body, 'text/html; charset=utf-8',
                       extra_headers=close_hdr)


# ---------------- 配对落地页 ----------------
_PAIR_LANDING_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Kimi Code · 配对</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#101014;color:#e8e8ec;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.c{max-width:26em;text-align:center;padding:2em}
h1{font-size:1.15em;font-weight:600}
p{line-height:1.7;color:#a8a8b3;font-size:.9em}
.err{color:#ff7d6b}
.ok{color:#7ddba3}
</style></head><body><div class="c">
<h1>Kimi Code 手机配对</h1>
<p id="msg">正在配对…</p>
<p style="font-size:.78em">配对后这台手机可通过 Kimi Code 操作本机会话（含文件与命令权限）。
仅在自己的可信设备上配对；外网模式经 Cloudflare 中继，局域网模式仅用于可信网络。
二维码/链接等同授权凭证，不要分享给他人。</p>
</div>
<script>
(function(){
var m=document.getElementById('msg');
function fail(t){m.textContent=t;m.className='err';}
try{
  var h=location.hash||'';
  var tok=new URLSearchParams(h.slice(1)).get('pair');
  if(!tok){fail('配对码缺失，请重新扫码或使用完整链接。');return;}
  history.replaceState(null,'',location.pathname);   // 立即抹除 fragment 中的配对码
  // 站点根：本页路径是 <root>/mobile/pair（relay 下 root=/t/<id>，否则 root=''）。
  // fetch/跳转一律基于 root 拼同源绝对路径，保证经中继前缀打开时不丢隧道归属。
  var root=location.pathname.slice(0,-'/mobile/pair'.length);
  fetch(root+'/mobile/pair/exchange',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:tok})}).then(function(r){
    return r.json().then(function(d){return {s:r.status,d:d};});
  }).then(function(r){
    if(r.s===200&&r.d&&r.d.redirect){m.textContent='配对成功，正在进入…';m.className='ok';
      // 桥回的 redirect 是站点内绝对路径（'/?kimi_onboarded=1#token=...'）；
      // relay 下要补回 /t/<id> 前缀——root 已从本页路径剥出。
      var rd=r.d.redirect;
      if(root&&rd.charAt(0)==='/')rd=root+rd;
      location.replace(rd);return;}
    fail((r.d&&r.d.error)||'配对失败，请重新扫码。');
  }).catch(function(){fail('网络错误，请确认手机网络及电脑服务仍在运行。');});
}catch(e){fail('浏览器不兼容，请换用系统浏览器。');}
})();
</script></body></html>"""


__all__ = ['MobileBridgeManager', 'MobileBridgeError']
