# -*- coding: utf-8 -*-
"""
Kimi Code 用量面板 · 手机桥独立 worker 进程与 daemon 侧客户端。

worker（run_worker）在独立 detached 子进程中持有 MobileBridgeManager 与
ConnectorRuntime：usage daemon 退出/重启只 begin_shutdown（detach，不 stop），
桥会话、隧道与 owner 复核在 worker 内存中原样存活；新 daemon 经同版本记录
重连接管。显式 stop / owner 失联按 bridge 既有 fail-closed 语义吊销并退出。

worker 控制 HTTP 仅绑 127.0.0.1 随机端口，全部端点要求 X-Kimi-Mobile-Worker
IPC secret（secret 仅存 ACL 受限的 ipc.secret 文件，绝不进 argv/env/日志/记录）。
公开面固定 /api/mobile/{status,start,stop,connector/install,pair/rotate}；
内部面 /api/mobile/internal/{meta,trusted_origins,stop-worker} 同样须 secret，
且仅 daemon 本进程使用。Host 精确、关键头单值、TE 拒、body 有界。

状态目录 <KIMI_HOME>/usage-dashboard/runtime/mobile-worker：
  worker.json    进程身份记录（pid/creation_ticks/port/protocol/version，原子发布）
  ipc.secret     IPC 共享密钥（当前用户+SYSTEM ACL，0600 语义；生命周期内不可变）
  startup.lock   启动互斥（真 OS 锁：同一时刻只有一个 spawn/attach 决策）
  worker.lock    worker 终身锁（真 OS 锁：持锁即存活凭证，进程死亡自动释放）
目录/文件拒符号链接与 reparse point；记录严格 schema（白名单键+取值边界），
PID 复用（ABA）以 creation_ticks 比对拒；旧版本 in-use worker 不接管、不擅杀，
仅读 status/显式 stop 并提示"停止后升级"。daemon 处于 Job 中（或 Job 探测失败——fail closed 等同）时 spawn 带
CREATE_BREAKAWAY_FROM_JOB（=subprocess.CREATE_BREAKAWAY_FROM_JOB，0x01000000；
Popen 失败即拒，绝不静默继承 Job）；Job 探测只读，绝不把本进程挂进任何 Job。
off 状态 orphan worker 30 分钟无认证 IPC 活动自动退出并清理。

spawn/ensure 失败按**阶段**留可诊断但不泄密的痕迹：阶段 + 固定错误码
（state-dir / startup-busy / lock-held / spawn-denied / spawn-error /
child-exit / boot-timeout / version-mismatch），daemon 侧可经
last_spawn_failure() 读有界诊断（stage/code/exit_code/stderr_tail/when，
stderr 尾部经脱敏与截断），公开 status 只多一条固定中文 worker_notice
（有界、无路径/secret/token/argv/子进程输出）。失败一律 fail-closed：
start 在 worker 未就绪时把该阶段的固定码（WORKER_*，白名单内）作为公开
错误码透出；无阶段诊断的失败仍回退 TUNNEL_START_FAILED，未知阶段一律回退，
绝不外泄 stderr/路径/secret。

协议 protocol=1，版本 version=3.3.3。本模块只依赖 Python>=3.8 标准库。
"""
import ctypes
import http.client
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mobile_bridge import (
    MobileBridgeError, MobileBridgeManager, _is_loopback_ip, _is_rfc1918,
    _is_strict_digits, _load_instances, _pid_alive, _pid_creation_ticks,
    _pid_is_desktop, _proc_image_name, _tcp_listener_pid,
    _TRUSTED_APP_ORIGIN, local_lan_addresses)

WORKER_PROTOCOL = 1
WORKER_VERSION = '3.3.3'
WORKER_HEADER = 'X-Kimi-Mobile-Worker'
WORKER_MAX_BODY = 8192
SECRET_BYTES = 32
RECORD_MAX_BYTES = 4096
_PREFERRED_PORT = 39283
_PORT_SCAN_MAX = 100
_STARTUP_LOCK_STALE = 120.0
_WORKER_BOOT_TIMEOUT = 15.0
_IMAGE_NAMES = frozenset(('python.exe', 'pythonw.exe'))
_RECORD_KEYS = frozenset(('pid', 'creation_ticks', 'port', 'protocol', 'version'))
_CREATE_BREAKAWAY = (getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', None)
                     or 0x01000000)   # Win32 CREATE_BREAKAWAY_FROM_JOB
_DETACHED = 0x00000008
_NO_WINDOW = 0x08000000
_DEFAULT_IDLE_TIMEOUT = 1800.0     # orphan off worker 空闲退出（30 分钟）
_STDERR_TAIL_MAX = 240             # 子进程 stderr 尾部上限（诊断，已脱敏）

# 测试注入点：非 None 时替代 <KIMI_HOME>/usage-dashboard/runtime/mobile-worker
_RUNTIME_STATE_DIR_OVERRIDE = None
_SPAWN_TARGET_OVERRIDE = None      # 测试用：直接 python -u mobile_worker.py
_SPAWN_SNIPPET_OVERRIDE = None     # 测试用：替代默认 -c 启动串
_RUNTIME_HOME_OVERRIDE = None      # 测试用：worker 内覆盖 KIMI_HOME

# spawn/ensure 失败阶段 → 固定错误码 + 公开中文提示（白名单：任何用户数据、
# 路径、secret、argv 都不可能进入这两个表，因此公开面结构上不泄密）。
_SPAWN_FAILURE_CODES = {
    'state-dir': 'WORKER_STATE_DIR_UNAVAILABLE',
    'startup-busy': 'WORKER_STARTUP_BUSY',
    'lock-held': 'WORKER_LOCK_HELD',
    'spawn-denied': 'WORKER_SPAWN_DENIED',
    'spawn-error': 'WORKER_SPAWN_FAILED',
    'child-exit': 'WORKER_CHILD_EXITED',
    'boot-timeout': 'WORKER_BOOT_TIMEOUT',
    'version-mismatch': 'WORKER_VERSION_MISMATCH',
}
_SPAWN_FAILURE_REASONS = {
    'state-dir': '工作目录不可用',
    'startup-busy': '有其他启动操作正在进行',
    'lock-held': '已有实例在运行但身份无法核实',
    'spawn-denied': '系统拒绝以独立进程启动',
    'spawn-error': '无法启动独立进程',
    'child-exit': '独立进程启动后立即退出',
    'boot-timeout': '独立进程启动超时',
    'version-mismatch': '独立进程版本与当前插件不一致',
}
# 子进程 stderr 尾部脱敏：盘符路径 / UNC / URL / 长十六进制 / 长不透明串
_REDACT_PATTERNS = (
    re.compile(r'[A-Za-z]:\\[^\s]*'),
    re.compile(r'\\\\[^\s]*'),
    re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://[^\s]*'),
    re.compile(r'(?<![0-9A-Za-z])[0-9a-fA-F]{24,}(?![0-9A-Za-z])'),
    re.compile(r'(?<![0-9A-Za-z])[0-9A-Za-z_\-]{40,}(?![0-9A-Za-z])'),
)


def _redact_tail(raw, cap=_STDERR_TAIL_MAX):
    """子进程 stderr 尾部的诊断投影：只保留可读片段，抹掉路径/URL/长 token，
    折叠控制字符与空白并截断。返回有界纯文本（绝不含换行——杜绝伪造多行
    提示或注入命令）。"""
    if isinstance(raw, bytes):
        text = raw.decode('utf-8', 'replace')
    elif isinstance(raw, str):
        text = raw
    else:
        return ''
    for pat in _REDACT_PATTERNS:
        text = pat.sub('<redacted>', text)
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > cap:
        text = text[-cap:]
    return text


def _spawn_notice(stage):
    """公开面提示：只用固定文案表拼装（未知阶段 → 空串，绝不回退拼接原始
    文本）。"""
    reason = _SPAWN_FAILURE_REASONS.get(stage)
    if not reason:
        return ''
    return '手机连接助手启动失败：%s，请稍后重试。' % reason


# ---------------- 状态目录 / ACL / 记录 ----------------
def _runtime_dir(home=None):
    if _RUNTIME_STATE_DIR_OVERRIDE:
        return _RUNTIME_STATE_DIR_OVERRIDE
    home = home or os.environ.get('KIMI_HOME') or os.path.join(
        os.path.expanduser('~'), '.kimi-code')
    return os.path.join(home, 'usage-dashboard', 'runtime', 'mobile-worker')


def _is_reparse(path):
    """路径是符号链接/reparse point → True；不存在 → False（调用方按
    'missing 即不可用' 处理）。读取错误（权限等）视为不安全 → True。"""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, 'st_file_attributes', 0) & 0x400)  # REPARSE_POINT


def _is_safe_dir(path):
    return (not _is_reparse(path)) and os.path.isdir(path)


def _icacls(args):
    """单条 icacls 命令，fail closed：启动失败/超时/非零退出一律拒；经
    %SystemRoot%\\System32 绝对路径调用，不依赖 PATH/PATHEXT。

    **保留但不再用于收敛路径**：`_ensure_dir_acl` / `_ensure_file_acl` 已改为
    单次受保护 DACL 替换（见 `_set_protected_dacl`）。此 helper 仅为历史
    诊断/外部脚本兼容保留，进程内已无调用者。"""
    exe = os.path.join(os.environ.get('SystemRoot') or r'C:\Windows',
                       'System32', 'icacls.exe')
    try:
        r = subprocess.run([exe] + args, capture_output=True, timeout=15, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        raise MobileBridgeError('worker ACL 设置失败')
    if r.returncode != 0:
        raise MobileBridgeError('worker ACL 设置失败')


def _current_user_sid():
    """当前进程 token 的实际用户 SID（OpenProcessToken → TokenUser，
    显式 64 位句柄类型防截断）。绝不凭 USERNAME 字符串授权——字符串
    仅作环境探测；实际授权主体以进程真实身份为准（本机/域同名账户
    时 icacls 账户名解析可能命中另一主体，SID 则唯一）。"""
    adv = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    prev_restype = kernel32.GetCurrentProcess.restype
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    adv.OpenProcessToken.restype = ctypes.c_int
    adv.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                     ctypes.POINTER(ctypes.c_void_p)]
    try:
        token = ctypes.c_void_p()
        if not adv.OpenProcessToken(kernel32.GetCurrentProcess(),
                                    0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        try:
            n = ctypes.c_ulong(0)
            adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(n))  # TokenUser
            buf = ctypes.create_string_buffer(n.value)
            if not adv.GetTokenInformation(token, 1, buf, n, ctypes.byref(n)):
                return None
            psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents
            if not psid:
                return None
            p = ctypes.c_void_p()
            if not adv.ConvertSidToStringSidW(psid, ctypes.byref(p)):
                return None
            try:
                return ctypes.wstring_at(p.value)
            finally:
                kernel32.LocalFree(p)
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return None
    finally:
        # 恢复共享 DLL 对象上的 restype：windll.kernel32 是全局缓存的
        # 同一对象，restype 改动会泄漏给进程内其他裸用该 API 的代码
        kernel32.GetCurrentProcess.restype = prev_restype


_SID_RE = re.compile(r'^S-\d+(-\d+)+$')
_ADVAPI32 = None
_KERNEL32 = None
_SE_FILE_OBJECT = 1                # SE_FILE_OBJECT
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1


def _advapi32():
    """专用 advapi32 句柄（显式 64 位签名）：

    - 用独立 `ctypes.WinDLL` 实例而不是 `ctypes.windll.advapi32`——后者是
      进程级共享对象，改它的 argtypes/restype 会泄漏给 mobile_bridge 等
      其它裸用 ctypes 的代码；
    - 句柄/SID/指针一律 `c_void_p` 语义，防 64 位截断。"""
    global _ADVAPI32
    if _ADVAPI32 is None:
        adv = ctypes.WinDLL('advapi32')
        convert = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.restype = ctypes.c_int
        convert.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong,
                            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        get_dacl = adv.GetSecurityDescriptorDacl
        get_dacl.restype = ctypes.c_int
        get_dacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                             ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(ctypes.c_int)]
        set_named = adv.SetNamedSecurityInfoW
        set_named.restype = ctypes.c_ulong
        set_named.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_ulong,
                              ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p]
        _ADVAPI32 = adv
    return _ADVAPI32


def _local_free(ptr):
    """释放 API 分配的 SD（LocalFree）。专用 kernel32 句柄，签名显式。"""
    global _KERNEL32
    if _KERNEL32 is None:
        k32 = ctypes.WinDLL('kernel32')
        k32.LocalFree.restype = ctypes.c_void_p
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        _KERNEL32 = k32
    _KERNEL32.LocalFree(ptr)


def _acl_user():
    """授权主体（`*SID` 形式，供 icacls 风格调用/外部脚本使用）：USERNAME
    缺失即拒（授权对象不确定时绝不收敛——绝不只授权 SYSTEM 锁死当前用户，
    也绝不跳过收敛 fail open）；实际授权以当前进程 token 的真实身份 SID
    下发（本机/域同名账户时账户名解析可能命中另一主体，SID 唯一）。"""
    user = os.environ.get('USERNAME') or ''
    if not user:
        raise MobileBridgeError('worker ACL 授权主体不可用')
    sid = _current_user_sid()
    if not sid:
        raise MobileBridgeError('worker ACL 授权主体不可用')
    return '*' + sid


def _acl_sid():
    """收敛用的授权主体（纯 SID 文本，无 `*` 前缀）。经 `_acl_user` 取当前
    进程 token 的真实身份，并**严格校验 SID 形态**：该字符串会被拼进 SDDL，
    任何越界字符（`)`/`;`/空白等）都可能伪造或追加 ACE，因此不合法一律
    fail closed——绝不把未校验文本送进安全描述符构造。"""
    user = _acl_user()
    sid = user[1:] if user.startswith('*') else user
    if not _SID_RE.match(sid):
        raise MobileBridgeError('worker ACL 授权主体不可用')
    return sid


def _set_protected_dacl(path, is_dir):
    """**一次 API 调用**把目标 DACL 置为受保护最小授权：当前用户真实 SID +
    SYSTEM 完全控制（目录带 (OI)(CI)，`D:P` 断继承）。旧授权的移除与新授权
    的建立发生在同一次 DACL 写入内，不存在"先清空、后重建"的中间空窗。

    任何失败（SID 不可用/非法、SDDL 构造失败、取 DACL 失败、DACL 为空或
    指针为空、API 返回非零）一律 fail closed：抛 MobileBridgeError，绝不降级
    为部分收敛、绝不放宽安全边界、绝不重试掩盖（空 DACL 等于人人放行，必须拒）。
    POSIX 走 `os.chmod`（原子，无 ACL 相位），失败同样 fail closed。"""
    if os.name != 'nt':
        try:
            os.chmod(path, 0o700 if is_dir else 0o600)
        except OSError:
            raise MobileBridgeError('worker ACL 设置失败')
        return
    sid = _acl_sid()
    flags = 'OICI' if is_dir else ''
    sddl = 'D:P(A;%s;FA;;;%s)(A;%s;FA;;;SY)' % (flags, sid, flags)
    adv = _advapi32()
    sd = ctypes.c_void_p()
    try:
        built = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(sd), None)
    except OSError:
        raise MobileBridgeError('worker ACL 设置失败')
    if not built or not sd.value:
        raise MobileBridgeError('worker ACL 设置失败')
    try:
        present = ctypes.c_int(0)
        dacl = ctypes.c_void_p()
        defaulted = ctypes.c_int(0)
        ok = adv.GetSecurityDescriptorDacl(
            sd, ctypes.byref(present), ctypes.byref(dacl),
            ctypes.byref(defaulted))
        # 取 DACL 失败、DACL 不存在，或 present 但指针为空（= 空 DACL，
        # 直接下发等于人人放行）都必须拒绝，绝不放行
        if not ok or not present.value or not dacl.value:
            raise MobileBridgeError('worker ACL 设置失败')
        rc = adv.SetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, dacl, None)
        if rc != 0:
            raise MobileBridgeError('worker ACL 设置失败')
    except OSError:
        # API 调用本身抛错（advapi32 不可用/被挂起）同样是收敛失败
        raise MobileBridgeError('worker ACL 设置失败')
    finally:
        _local_free(sd)


def _ensure_dir_acl(path):
    """目录 ACL 收敛到 当前用户 + SYSTEM 完全控制（受保护、不继承），
    单次 `_set_protected_dacl`；任何失败 fail closed。"""
    _set_protected_dacl(path, True)


def _ensure_file_acl(path):
    """文件 ACL 收敛到 当前用户 + SYSTEM 完全控制（受保护、不继承），
    单次 `_set_protected_dacl`；任何失败 fail closed。"""
    _set_protected_dacl(path, False)


def _ensure_runtime_dir(home=None):
    d = _runtime_dir(home)
    parent = os.path.dirname(d)
    if os.path.lexists(d) and not _is_safe_dir(d):
        raise MobileBridgeError('worker 状态目录不安全')
    if os.path.lexists(parent) and not _is_safe_dir(parent):
        raise MobileBridgeError('worker 状态目录不安全')
    os.makedirs(d, exist_ok=True)
    if not _is_safe_dir(d):
        raise MobileBridgeError('worker 状态目录不安全')
    _ensure_dir_acl(d)
    return d


def _safe_read(path, cap):
    if _is_reparse(path):
        return None
    try:
        with open(path, 'rb') as f:
            return f.read(cap + 1)
    except OSError:
        return None


def _atomic_write(path, data):
    """有界 JSON 原子发布：同目录 tmp + os.replace；拒链接路径。"""
    if _is_reparse(path):
        raise MobileBridgeError('worker 记录路径不安全')
    tmp = path + '.tmp.%d.%d' % (os.getpid(), int(time.time() * 1000))
    if _is_reparse(tmp):
        raise MobileBridgeError('worker 记录路径不安全')
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_secret(state_dir=None):
    d = state_dir or _runtime_dir()
    raw = _safe_read(os.path.join(d, 'ipc.secret'), 512)
    if raw is None:
        return None
    try:
        text = raw.decode('ascii').strip()
    except Exception:
        return None
    if len(text) != SECRET_BYTES * 2 or any(
            c not in '0123456789abcdef' for c in text):
        return None
    return text


def _write_secret(d):
    secret = secrets.token_hex(SECRET_BYTES)
    path = os.path.join(d, 'ipc.secret')
    _atomic_write(path, secret.encode('ascii'))
    _ensure_file_acl(path)
    return secret


def _read_record(state_dir=None):
    d = state_dir or _runtime_dir()
    raw = _safe_read(os.path.join(d, 'worker.json'), RECORD_MAX_BYTES)
    if raw is None or len(raw) > RECORD_MAX_BYTES:
        return None
    try:
        rec = json.loads(raw.decode('utf-8'))
    except Exception:
        return None
    if not isinstance(rec, dict) or set(rec) != _RECORD_KEYS:
        return None
    try:
        pid = int(rec['pid'])
        creation = int(rec['creation_ticks'])
        port = int(rec['port'])
        protocol = int(rec['protocol'])
    except (TypeError, ValueError):
        return None
    if not (0 < pid < 2 ** 31 and creation > 0
            and 1024 < port <= 65535 and protocol == WORKER_PROTOCOL):
        return None
    if not isinstance(rec['version'], str) or len(rec['version']) > 64:
        return None
    return {'pid': pid, 'creation_ticks': creation, 'port': port,
            'protocol': protocol, 'version': rec['version']}


def _write_record(d, port, version=None):
    rec = {'pid': os.getpid(), 'creation_ticks': _pid_creation_ticks(os.getpid()),
           'port': port, 'protocol': WORKER_PROTOCOL,
           'version': version or WORKER_VERSION}
    if not rec['creation_ticks']:
        raise MobileBridgeError('worker 进程身份不可用')
    path = os.path.join(d, 'worker.json')
    _atomic_write(path, json.dumps(rec, separators=(',', ':')).encode('utf-8'))
    _ensure_file_acl(path)


def _remove_record(d):
    path = os.path.join(d, 'worker.json')
    try:
        if not _is_reparse(path):
            os.remove(path)
    except OSError:
        pass


# ---------------- 锁（真 OS 锁：句柄语义，持锁者死亡自动释放） ----------------
if os.name == 'nt':
    import msvcrt

    def _os_lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _os_unlock(fh):
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _os_lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _os_unlock(fh):
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class _StartupLock(object):
    """启动互斥（真 OS 锁）：daemon 侧 spawn/attach 决策串行；持锁者死亡
    锁自动失效——无 mtime 竞争、无残留。"""

    def __init__(self, path):
        self.path = path
        self._fh = None

    def __enter__(self):
        deadline = time.monotonic() + 30.0
        while True:
            # 打开前拒符号链接/reparse point：绝不把互斥落到任意目标文件上
            if _is_reparse(self.path):
                raise MobileBridgeError('CONNECTOR_BUSY')
            fh = open(self.path, 'a+b')
            try:
                _os_lock(fh)
            except OSError:
                fh.close()
                if time.monotonic() >= deadline:
                    raise MobileBridgeError('CONNECTOR_BUSY')
                time.sleep(0.05)
                continue
            self._fh = fh
            return self

    def __exit__(self, *exc):
        try:
            if self._fh is not None:
                _os_unlock(self._fh)
                self._fh.close()
        finally:
            self._fh = None
        return False


class _LifetimeLock(object):
    """worker 终身锁（真 OS 锁）：同目录只许一个 worker；锁在打开句柄上，
    worker 死亡（含崩溃/强杀）OS 自动释放——不存在"文件残留即永久锁死"。
    锁文件被外部删除不影响持锁者排他性。释放时只解锁/关闭句柄，绝不删除
    文件（删除会与新 worker 并发重开产生 TOCTOU 竞争；残留文件无害——
    排他性来自 OS 锁而非文件存在）。"""

    def __init__(self, path):
        self.path = path
        self._fh = None

    def acquire(self):
        # 打开前拒符号链接/reparse point：绝不把终身锁落到任意目标文件上
        if _is_reparse(self.path):
            return False
        try:
            fh = open(self.path, 'a+b')
        except OSError:
            return False
        try:
            _os_lock(fh)
        except OSError:
            fh.close()
            return False
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode('ascii'))
            fh.flush()
        except Exception:
            pass
        self._fh = fh
        return True

    def release(self):
        try:
            if self._fh is not None:
                _os_unlock(self._fh)
                self._fh.close()
        finally:
            self._fh = None


def _lifetime_lock_held(d):
    """保守探测：能否拿到终身锁。拿不到=有活 worker 持有（或 I/O 异常，
    同样按"被持有"处理，fail closed）。绝不删活锁。"""
    lock = _LifetimeLock(os.path.join(d, 'worker.lock'))
    if lock.acquire():
        lock.release()
        return False
    return True


# ---------------- 进程身份校验 ----------------
def _pid_matches_record(rec):
    """严格核实记录中的 PID 就是本插件 worker：存活 + creation_ticks 一致
    （拒 PID 复用冒名）+ 可执行映像为 python/pythonw + 端口 listener 归属该
    PID。任何核实环节异常一律拒（fail closed）。"""
    try:
        pid = rec['pid']
        if not _pid_alive(pid):
            return False
        if _pid_creation_ticks(pid) != rec['creation_ticks']:
            return False
        if _tcp_listener_pid('127.0.0.1', rec['port']) != pid:
            return False
        return _proc_image_name(pid) in _IMAGE_NAMES
    except Exception:
        return False


# ---------------- Windows Job（daemon 侧只读探测 + spawn 标志） ----------------
def _typed_kernel32():
    """显式 argtypes/restype 的 kernel32 句柄：防 64 位 HANDLE 截断。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.IsProcessInJob.restype = ctypes.c_int
    kernel32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_int)]
    return kernel32


def _in_job():
    """只读探测本进程是否处于 Job——绝不 AssignProcessToJobObject 本进程。
    探测失败返回 None（fail closed：调用方按"可能在 Job 中"处理，spawn
    必须带 breakaway，失败即拒，绝不静默继承 Job）。"""
    if os.name != 'nt':
        return False
    try:
        kernel32 = _typed_kernel32()
        flag = ctypes.c_int(0)
        if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(),
                                       None, ctypes.byref(flag)):
            return None
        return bool(flag.value)
    except Exception:
        return None


# ---------------- daemon 侧客户端 ----------------
class _WorkerUnreachable(MobileBridgeError):
    """worker 连接层不可达（socket/协议失败）：句柄不可信，调用方须丢弃重连。"""


class _WorkerHTTPError(MobileBridgeError):
    """worker 返回非 200：携带真实 HTTP 状态码，供调用方精确判定（绝不靠
    错误文案猜状态）；403 表示缓存的句柄认证已被拒。"""

    def __init__(self, msg, status):
        MobileBridgeError.__init__(self, msg)
        self.status = status


def _unreachable(exc):
    return isinstance(exc, _WorkerUnreachable)


def _auth_rejected(exc):
    """句柄认证被 worker 拒（HTTP 403）——按状态码判定，绝不猜字符串。"""
    return isinstance(exc, _WorkerHTTPError) and exc.status == 403


class MobileWorkerClient(object):
    """usage daemon 的手机桥适配器。句柄只指向经严格核实（记录 + 进程身份 +
    认证 meta）的 live worker；绝不回退进程内 bridge。begin_shutdown 仅释放
    句柄（detach，worker 继续跑）；shutdown 即 detach。status/stop 不隐式
    拉起进程；start/install 显式确保 worker。protocol1 旧版本 live worker
    允许读 status 与显式 stop（安全迁移），禁止 start/rotate/install。

    状态目录严格基于构造时传入的 kimi_home——绝不读 KIMI_HOME 环境变量，
    绝不写真实用户目录以外的路径。"""

    def __init__(self, kimi_home):
        self.kimi_home = kimi_home
        self._lock = threading.RLock()
        self._worker = None          # (port, secret, version)
        self._closing = False
        self._last_failure = None    # 最近一次 spawn/ensure 失败诊断（有界）

    # ---------- 失败诊断（可诊断、不泄密） ----------
    def last_spawn_failure(self):
        """最近一次 spawn/ensure 失败的**有界**诊断（无失败 → None）：
        {stage, code, [exit_code], [stderr_tail], when}。stderr 尾部已脱敏
        截断（无路径/URL/长 token/换行）。这是 daemon 侧排查信息，**不得**
        直接进入公开面——公开 status 只用固定文案表的 worker_notice。"""
        with self._lock:
            return dict(self._last_failure) if self._last_failure else None

    def _note_failure(self, stage, exit_code=None, stderr=''):
        """记录一次失败阶段：stage 与 code 均取自阶段白名单（未知 stage →
        通用 WORKER_START_FAILED），故任何用户数据/路径/secret/argv 结构上
        都进不来。"""
        with self._lock:
            self._last_failure = {
                'stage': stage,
                'code': _SPAWN_FAILURE_CODES.get(stage, 'WORKER_START_FAILED'),
                'when': time.time(),
            }
            if exit_code is not None:
                self._last_failure['exit_code'] = exit_code
            tail = _redact_tail(stderr)
            if tail:
                self._last_failure['stderr_tail'] = tail

    def _clear_failure(self):
        with self._lock:
            self._last_failure = None

    def _failure_notice(self):
        with self._lock:
            failure = self._last_failure
        return _spawn_notice(failure['stage']) if failure else ''

    # ---------- 状态目录（仅传入 home） ----------
    def _runtime_dir_for(self):
        return _runtime_dir(self.kimi_home)

    def _state_dir(self):
        return _ensure_runtime_dir(self.kimi_home)

    def _read_record(self):
        return _read_record(self._runtime_dir_for())

    def _read_secret(self):
        return _read_secret(self._runtime_dir_for())

    # ---------- 底层 ----------
    def _call(self, port, secret, method, path, body=None, timeout=45.0):
        """单次 IPC；连接层失败抛 `_WorkerUnreachable`，非 200 抛
        `_WorkerHTTPError`（带真实 status）——调用方按精确状态判定句柄去留，
        绝不靠错误文案猜。任何路径都只发一次请求，绝不自动重发。"""
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        try:
            headers = {'Host': '127.0.0.1:%d' % port, WORKER_HEADER: secret,
                       'Connection': 'close'}
            data = None
            if body is not None:
                data = json.dumps(body).encode('utf-8')
                headers['Content-Type'] = 'application/json'
            conn.request(method, path, data, headers)
            resp = conn.getresponse()
            raw = resp.read(1024 * 1024)
            if resp.status != 200:
                try:
                    msg = json.loads(raw.decode('utf-8'))['error']
                except Exception:
                    msg = 'worker 请求失败(%d)' % resp.status
                raise _WorkerHTTPError(str(msg)[:200], resp.status)
            data = json.loads(raw.decode('utf-8'))
            if not isinstance(data, dict):
                raise _WorkerUnreachable('worker 响应无效')
            return data
        except (MobileBridgeError, _WorkerUnreachable):
            raise
        except Exception:
            raise _WorkerUnreachable('worker 不可达')
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---------- 记录核实 / 重连 ----------
    def _verify_meta(self, rec, secret):
        """认证 meta 强核实：版本/协议/PID 必须与记录精确一致（同版本与
        protocol1 旧版本共用同一证明强度——绝不只凭记录里的 version 字符串
        判定旧版本）。"""
        try:
            meta = self._call(rec['port'], secret, 'GET',
                              '/api/mobile/internal/meta', timeout=5.0)
        except MobileBridgeError:
            return None
        if (not isinstance(meta.get('version'), str)
                or meta.get('version') != rec['version']
                or meta.get('protocol') != WORKER_PROTOCOL
                or meta.get('pid') != rec['pid']):
            return None
        return meta

    def _attach(self):
        """当前版本句柄：缓存即返；无缓存则全量重证（记录 + 进程身份 +
        认证 meta：版本/协议/PID 与记录一致）后接管。不凭端口信任外部进程。"""
        with self._lock:
            if self._worker is not None:
                return self._worker
            if self._closing:
                return None
            try:
                d = self._state_dir()
            except MobileBridgeError:
                return None
            rec = _read_record(d)
            if rec is None or rec['version'] != WORKER_VERSION:
                return None
            if not _pid_matches_record(rec):
                return None
            secret = _read_secret(d)
            if not secret:
                return None
            if self._verify_meta(rec, secret) is None:
                return None
            self._worker = (rec['port'], secret, rec['version'])
            self._clear_failure()        # 已接管 live worker：失败状态过期
            return self._worker

    def _old_worker_handle(self):
        """protocol1 旧版本 live worker 的只读/停止句柄：(port, secret)。
        与 _attach 同一证明强度：本地记录严格 schema + 进程身份（存活/
        creation_ticks/端口监听/映像名）+ 认证 meta（meta.version == 记录
        version 且 != 当前版本、meta.protocol == 1、meta.pid == 记录 pid）。
        任何环节失败 → None（绝不只凭 JSON 里被改写的 version 字段放行）。"""
        if self._closing:
            return None
        try:
            d = self._state_dir()
        except MobileBridgeError:
            return None
        rec = _read_record(d)
        if rec is None or rec['version'] == WORKER_VERSION:
            return None
        if not _pid_matches_record(rec):
            return None
        secret = _read_secret(d)
        if not secret:
            return None
        if self._verify_meta(rec, secret) is None:
            return None
        return (rec['port'], secret)

    def _drop(self):
        with self._lock:
            self._worker = None

    def _invoke(self, method, path, body=None, ensure=False, timeout=45.0,
                write=False):
        """单次调用，绝不重发。缓存句柄失效（不可达/认证被拒）即丢弃，
        下次调用按正常强校验重新 attach；本方法不重试——写操作尤其不得
        自动重试/重复提交。"""
        handle = self._attach()
        if handle is None and ensure:
            if write and self._old_worker_handle() is not None:
                # 写操作路由到旧版本 worker：拒绝并给出安全迁移提示
                raise MobileBridgeError('旧版本正在运行，停止后升级')
            if self._ensure_worker():
                handle = self._attach()
        if handle is None:
            return None
        try:
            return self._call(handle[0], handle[1], method, path, body, timeout)
        except MobileBridgeError as e:
            # 连接层不可达（状态未知）与 403 认证被拒（句柄过期）都使缓存
            # 句柄不可信：立即丢弃；绝不在此重发请求，由上层下次调用重 attach
            if _unreachable(e) or _auth_rejected(e):
                self._drop()
                return None
            raise

    # ---------- 显式拉起 ----------
    def _ensure_worker(self):
        with self._lock:
            if self._closing:
                return False
        try:
            d = self._state_dir()
        except MobileBridgeError:
            # 状态目录不可用（ACL/权限/不安全路径）：阶段可诊断，仍 fail closed
            self._note_failure('state-dir')
            return False
        try:
            with _StartupLock(os.path.join(d, 'startup.lock')):
                if self._attach() is not None:
                    self._clear_failure()   # worker 已在运行：旧失败不再适用
                    return True
                rec = _read_record(d)
                if (rec is not None and _pid_matches_record(rec)
                        and _read_secret(d)):
                    return False      # 任何版本 live worker：不接管不擅杀
                if _lifetime_lock_held(d) and not (
                        rec and _pid_matches_record(rec)):
                    # 锁被持有但记录不可用：异常，fail closed（阶段可诊断）
                    self._note_failure('lock-held')
                    return False
                if not self._spawn(d):
                    return False
                # spawn 后记录必须回读为当前版本（worker 进程可能被替换为
                # 旧构建）：否则不接管，让上层走旧版本安全提示路径
                rec = _read_record(d)
                if (rec is not None and rec['version'] == WORKER_VERSION
                        and _pid_matches_record(rec)):
                    return True
                self._note_failure('version-mismatch')
                return False
        except MobileBridgeError:
            # 启动互斥不可得（并发 spawn / 被占用）：阶段可诊断，仍 fail closed
            self._note_failure('startup-busy')
            return False

    def _spawn(self, d):
        """拉起独立 worker：成功即**清除**失败诊断并返回 True；任何失败在
        返回 False 前**记录阶段 + 固定错误码 +（可用时）退出码与脱敏 stderr
        尾部**——绝不把路径/secret/argv/原始输出放进公开面，也绝不无
        breakaway 重试。"""
        if _SPAWN_SNIPPET_OVERRIDE is not None:
            argv = [sys.executable, '-B', '-c', _SPAWN_SNIPPET_OVERRIDE]
        elif _SPAWN_TARGET_OVERRIDE is not None:
            argv = [sys.executable, '-B', _SPAWN_TARGET_OVERRIDE]
        else:
            argv = [sys.executable, '-B', '-c',
                    'import sys;sys.path.insert(0,%r);'
                    'import mobile_worker;mobile_worker._main()'
                    % os.path.dirname(os.path.abspath(__file__))]
        env = dict(os.environ)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['KIMI_HOME'] = _RUNTIME_HOME_OVERRIDE or self.kimi_home
        env['MW_STATE_DIR'] = d
        flags = 0
        if os.name == 'nt':
            flags = _DETACHED | _NO_WINDOW
            in_job = _in_job()
            if in_job is not False:
                # daemon 确认在 Job 中，或探测失败（None，fail closed 等同
                # 在 Job 中）：必须 breakaway，Popen 失败即拒——绝不静默把
                # worker 继承进 daemon 的 Job
                flags |= _CREATE_BREAKAWAY
        # stderr 落**匿名临时文件**（close 即删）：有界诊断用，无管道背压
        # （worker 写满也不会被卡住），读取也不阻塞在活着的子进程上；
        # stdin/stdout 仍丢弃，绝不把内容写进任何日志。
        stderr_fh = None
        try:
            stderr_fh = tempfile.TemporaryFile()
            proc = subprocess.Popen(
                argv, cwd=d, env=env, creationflags=flags,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=stderr_fh, close_fds=True)
        except PermissionError:
            # breakaway 被系统拒绝（Job 未设 BREAKAWAY_OK / 权限不足）：
            # 绝不降级为无 breakaway 重试，绝不静默继承 Job
            self._close_quiet(stderr_fh)
            self._note_failure('spawn-denied')
            return False
        except Exception:
            self._close_quiet(stderr_fh)
            self._note_failure('spawn-error')
            return False
        try:
            deadline = time.monotonic() + _WORKER_BOOT_TIMEOUT
            while time.monotonic() < deadline:
                rc = proc.poll()
                if rc is not None:
                    self._note_failure(
                        'child-exit', exit_code=rc,
                        stderr=self._read_stderr(proc, stderr_fh))
                    return False
                rec = _read_record(d)
                if rec is not None and rec['pid'] == proc.pid:
                    if rec['version'] != WORKER_VERSION:
                        # 子进程已发布同 PID 的"旧版本"记录：不接管，且不
                        # 白等满启动超时——阶段立即可诊断（fail closed）
                        self._note_failure('version-mismatch')
                        return False
                    if _pid_matches_record(rec):
                        self._clear_failure()
                        return True
                time.sleep(0.1)
            self._note_failure('boot-timeout',
                               stderr=self._read_stderr(proc, stderr_fh))
            return False
        finally:
            self._close_quiet(stderr_fh)

    # ---------- 失败诊断的 I/O 辅助 ----------
    def _close_quiet(self, fh):
        try:
            if fh is not None:
                fh.close()
        except Exception:
            pass

    def _read_stderr(self, proc, stderr_fh, cap=8192):
        """有界读取子进程 stderr 诊断片段（**绝不阻塞**）：stderr 以匿名临时
        文件承载（close 即删），不是管道——写满不会卡住 worker，读也不会
        阻塞在"还活着的子进程"上。返回原始片段（脱敏在 _note_failure 内）。"""
        source = getattr(proc, 'stderr', None)
        if source is None:
            source = stderr_fh
        if source is None:
            return b''
        try:
            try:
                source.seek(0)
            except Exception:
                pass
            raw = source.read(cap)
        except Exception:
            return b''
        return raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode(
            'utf-8', 'replace')

    # ---------- 查询/控制 ----------
    def _local_off_status(self):
        """无 live worker 的本地状态：off + connector 安装态（只读文件，不下载）。
        不附任何凭据/URL/进程细节；仅在**本客户端**记录过启动失败时附一条
        固定文案的有界提示（可诊断，结构上不含路径/secret/argv/子进程输出）。"""
        try:
            from mobile_tunnel import ConnectorRuntime
            connector = MobileBridgeManager._normalize_connector_status(
                ConnectorRuntime(self.kimi_home).status())
        except Exception:
            connector = {'state': 'missing', 'version': '2026.9.3'}
        out = {'enabled': False, 'state': 'off', 'mode': 'lan',
               'owner_origin': '', 'device_count': 0,
               'connector': connector, 'tunnel': {'state': 'off'},
               'pair_state': 'missing'}
        try:
            out['addresses'] = local_lan_addresses()
        except Exception:
            out['addresses'] = []
        notice = self._failure_notice()
        if notice:
            out['worker_notice'] = notice
        return out

    def _old_upgrade_notice(self, version):
        return ('旧版本(v%s)手机连接进程正在运行，停止后升级；'
                '停止旧版本后即可在新版本中使用手机连接。' % version)

    def _old_worker_status(self, handle, version):
        """旧版本 live worker 的实际状态（protocol1 兼容读）：读取其真实
        status，保证 state/enabled 反映 worker 实况（停止按钮可用），
        绝不伪造 off；附安全升级提示。"""
        try:
            st = self._call(handle[0], handle[1], 'GET', '/api/mobile/status',
                            timeout=5.0)
        except MobileBridgeError:
            st = None
        if not isinstance(st, dict) or st.get('state') not in ('off', 'on'):
            st = self._local_off_status()
        st['worker_notice'] = self._old_upgrade_notice(version)
        return st

    def status(self):
        # 当前版本 live worker：直接读其 status
        handle = self._attach()
        if handle is not None:
            try:
                return self._call(handle[0], handle[1], 'GET',
                                  '/api/mobile/status', timeout=45.0)
            except MobileBridgeError as e:
                if not _unreachable(e):
                    raise
                self._drop()
        # 旧版本 live worker：同一强核实后读其实际状态 + 迁移提示（可信的
        # 判定不依赖 JSON 里被改写的 version——meta 必须逐字段吻合记录）
        old = self._old_worker_handle()
        if old is not None:
            rec = self._read_record()
            return self._old_worker_status(old, rec['version'])
        return self._local_off_status()

    def _start_failure_code(self):
        """无 live worker 时把最终失败原因精确化为可诊断码：以最近一次
        spawn/ensure 阶段的固定 WORKER_* 码为准（白名单内），无该诊断则
        回退通用码。只返回固定码本身，绝不外泄 stage 细节/stderr/路径。"""
        failure = self.last_spawn_failure()
        code = failure['code'] if failure else None
        if code in _SPAWN_FAILURE_CODES.values():
            return code
        return 'TUNNEL_START_FAILED'

    def start(self, owner_origin, address=None, mode='lan', relay_consent=False,
              consent_version=None):
        # 参数校验先于一切副作用（含拉起 worker）：坏参数必须透出桥的中文
        # 校验错误，绝不吞成 TUNNEL_START_FAILED。
        if mode not in ('lan', 'internet'):
            raise MobileBridgeError('mode 不被支持')
        if mode == 'lan':
            if not isinstance(address, str) or not _is_rfc1918(address):
                raise MobileBridgeError('请选择一个本机局域网 IPv4 地址')
            if address not in local_lan_addresses():
                raise MobileBridgeError('所选地址不是本机网卡地址')
        body = {'owner_origin': owner_origin, 'mode': mode}
        if mode == 'internet':
            body['relay_consent'] = relay_consent
            body['consent_version'] = consent_version
        else:
            body['address'] = address
        st = self._invoke('POST', '/api/mobile/start', body,
                          ensure=True, timeout=90.0, write=True)
        if st is None:
            if self._closing:
                raise MobileBridgeError('START_CANCELLED')
            # worker 未就绪的真实原因按阶段固定码透出（WORKER_*）；无诊断时
            # 仍回退 TUNNEL_START_FAILED（旧合同），绝不暴露细节
            raise MobileBridgeError(self._start_failure_code())
        return st

    def stop(self):
        # 当前版本 live worker：显式 stop 并令其进程退出
        handle = self._attach()
        if handle is not None:
            try:
                st = self._call(handle[0], handle[1], 'POST',
                                '/api/mobile/stop', {}, timeout=45.0)
            except MobileBridgeError as e:
                if not _unreachable(e):
                    raise
                self._drop()
                st = None
            if st is not None:
                self._stop_worker_process()
                return st
        # 旧版本 live worker：显式 stop 放行（protocol1 兼容迁移）。只凭
        # 强核实的 (port, secret) 句柄发 stop/stop-worker，不读其响应；
        # 停掉后返回本地 off（不再附迁移提示——旧版本已不存在）。
        old = self._old_worker_handle()
        if old is not None:
            for path in ('/api/mobile/stop', '/api/mobile/internal/stop-worker'):
                try:
                    self._call(old[0], old[1], 'POST', path, {}, timeout=5.0)
                except MobileBridgeError:
                    pass
            self._drop()
        return self._local_off_status()

    def _stop_worker_process(self):
        """显式 stop 后让 worker 进程退出（worker 自行清理记录与锁）。"""
        handle = self._attach()
        if handle is None:
            return
        try:
            self._call(handle[0], handle[1], 'POST',
                       '/api/mobile/internal/stop-worker', {}, timeout=5.0)
        except MobileBridgeError:
            pass
        self._drop()

    def install_connector(self, consent, consent_version):
        st = self._invoke('POST', '/api/mobile/connector/install',
                          {'consent': consent, 'consent_version': consent_version},
                          ensure=True, timeout=240.0, write=True)
        if st is None:
            if self._closing:
                raise MobileBridgeError('START_CANCELLED')
            raise MobileBridgeError('CONNECTOR_INSTALL_FAILED')
        return st

    def pair_rotate(self):
        # 写操作：当前版本句柄缺失时必须 fail closed 报"停止后升级"（旧版本
        # live worker 或不可核实状态），绝不静默拉起新 worker 顶替或返回 None
        if self._attach() is None:
            raise MobileBridgeError('旧版本正在运行，停止后升级')
        st = self._invoke('POST', '/api/mobile/pair/rotate', {}, write=True)
        if st is None:
            raise MobileBridgeError('旧版本正在运行，停止后升级')
        return st

    def _registered_control_origins(self):
        """本地独立枚举合法控制 Origin：app origin + 活着的桌面注册实例
        （只读注册文件 + PID 存活/桌面映像核实；不构造 manager、不拉起
        worker、无运行时副作用——与 in-proc 桥首次 start 前的语义一致）。
        foreign Origin 照旧拒绝。"""
        trusted = {_TRUSTED_APP_ORIGIN}
        for i in _load_instances(self.kimi_home):
            try:
                if (_is_loopback_ip(i['host']) and _pid_alive(i['pid'])
                        and _pid_is_desktop(i['pid'])):
                    trusted.add('http://%s:%d' % (i['host'], i['port']))
            except Exception:
                continue
        return trusted

    def _trusted_control_origins(self):
        # worker 存活时以 worker 的权威枚举为准；无 worker 时回退本地注册
        # 枚举（首次 start 前 native owner Origin 也必须被放行）
        st = self._invoke('GET', '/api/mobile/internal/trusted_origins')
        if st is not None and isinstance(st.get('origins'), list):
            out = set()
            for o in st['origins']:
                if isinstance(o, str) and len(o) <= 200:
                    out.add(o)
            return out
        return self._registered_control_origins()

    def begin_shutdown(self):
        """detach ONLY：释放句柄，不通知 worker——桥/隧道/会话继续存活，
        下个 daemon 实例经记录重连。"""
        with self._lock:
            self._closing = True
            self._worker = None

    def shutdown(self):
        self.begin_shutdown()


# ---------------- worker 进程侧 ----------------
class _WorkerHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


class _WorkerHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'kimi-mobile-worker'
    sys_version = ''

    def version_string(self):
        return self.server_version

    def log_message(self, *a):
        pass

    @property
    def mgr(self):
        return self.server.manager

    def _fail(self, code, msg):
        self.close_connection = True
        body = json.dumps({'error': msg}, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _guards_ok(self):
        if getattr(self.headers, 'defects', None):
            return self._fail(400, '请求头不合法')
        for h in ('Host', 'Origin', 'Content-Length', 'Transfer-Encoding',
                  WORKER_HEADER, 'Content-Type'):
            if len(self.headers.get_all(h) or []) > 1:
                return self._fail(400, '请求头不合法')
        if self.headers.get_all('Transfer-Encoding'):
            return self._fail(400, '请求体格式不被支持')
        cls = self.headers.get_all('Content-Length') or []
        if cls and not _is_strict_digits(cls[0]):
            return self._fail(400, '请求头不合法')
        if self.headers.get('Origin') is not None:
            return self._fail(403, 'Origin 不被允许')
        peer = self.client_address[0] if self.client_address else ''
        if not _is_loopback_ip(peer):
            return self._fail(403, '仅允许本机访问')
        host = (self.headers.get('Host') or '').strip()
        if host not in ('127.0.0.1:%d' % self.server.server_port,
                        'localhost:%d' % self.server.server_port,
                        '[::1]:%d' % self.server.server_port):
            return self._fail(403, 'Host 不被允许')
        supplied = self.headers.get(WORKER_HEADER) or ''
        if not supplied or not secrets.compare_digest(
                supplied, self.server.secret):
            return self._fail(403, '未授权')
        self.server.last_ipc = time.monotonic()
        return True

    def _body(self):
        cls = self.headers.get_all('Content-Length') or []
        if not cls:
            raise MobileBridgeError('请求头不合法')
        n = int(cls[0])
        if n > WORKER_MAX_BODY:
            raise MobileBridgeError('请求体过大')
        if n == 0:
            return {}
        deadline = time.monotonic() + 15.0
        out = bytearray()
        while len(out) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MobileBridgeError('请求体读取超时')
            self.connection.settimeout(remaining)
            chunk = self.rfile.read1(min(n - len(out), 65536))
            if not chunk:
                raise MobileBridgeError('请求体不完整')
            out.extend(chunk)
        try:
            body = json.loads(bytes(out).decode('utf-8'))
        except Exception:
            raise MobileBridgeError('请求体不是合法 JSON')
        if not isinstance(body, dict):
            raise MobileBridgeError('请求体格式错误')
        return body

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        try:
            if not self._guards_ok():
                return
            path = (self.path or '').split('?')[0]
            cmd = self.command
            if cmd == 'GET' and path == '/api/mobile/status':
                self._json(self.mgr.status())
            elif cmd == 'GET' and path == '/api/mobile/internal/meta':
                self._json({'protocol': WORKER_PROTOCOL,
                            'version': getattr(self.server, 'worker_version',
                                               WORKER_VERSION),
                            'pid': os.getpid()})
            elif cmd == 'GET' and path == '/api/mobile/internal/trusted_origins':
                self._json({'origins': sorted(self.mgr._trusted_control_origins())})
            elif cmd == 'POST' and path == '/api/mobile/stop':
                if self._body():
                    raise MobileBridgeError('请求参数不被允许')
                self._json(self.mgr.stop())
            elif cmd == 'POST' and path == '/api/mobile/start':
                body = self._body()
                mode = body.get('mode', 'lan')
                allowed = ({'owner_origin', 'mode', 'relay_consent', 'consent_version'}
                           if mode == 'internet' else {'owner_origin', 'mode', 'address'})
                if set(body) - allowed:
                    raise MobileBridgeError('请求参数不被允许')
                self._json(self.mgr.start(
                    body.get('owner_origin'), body.get('address'), mode,
                    body.get('relay_consent', False), body.get('consent_version')))
            elif cmd == 'POST' and path == '/api/mobile/connector/install':
                body = self._body()
                if set(body) - {'consent', 'consent_version'}:
                    raise MobileBridgeError('请求参数不被允许')
                self._json(self.mgr.install_connector(
                    body.get('consent'), body.get('consent_version')))
            elif cmd == 'POST' and path == '/api/mobile/pair/rotate':
                if self._body():
                    raise MobileBridgeError('请求参数不被允许')
                # 换发配对码走桥的 canonical rotate（含 owner re-proof；
                # owner 失联按桥语义 fail-closed 吊销整代）。
                self.mgr.rotate_pair()
                self._json(self.mgr.status())
            elif cmd == 'POST' and path == '/api/mobile/internal/stop-worker':
                if self._body():
                    raise MobileBridgeError('请求参数不被允许')
                self._json({'ok': True})
                threading.Thread(target=self.server.initiate_shutdown,
                                 kwargs={'code': 'STOP_REQUESTED'}, daemon=True).start()
            else:
                self._fail(404, '接口不存在')
        except MobileBridgeError as e:
            self._fail(400, str(e)[:160])
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            self._fail(500, '处理失败')


def run_worker(port=0, state_dir=None, kimi_home=None, idle_timeout=None,
               version=None):
    """worker 主入口：独立持有 MobileBridgeManager + ConnectorRuntime。
    终身锁防重复；owner 失联/显式 stop fail-closed 退出并清理记录。
    off 状态且 idle_timeout 内无任何认证 IPC 活动的 orphan worker 自动退出
    （secret 生命周期内不可变——整个运行期只有启动时写入的一次）。
    任何启动失败都清理锁/记录/secret，绝不留死锁或 stale pid。
    version 默认当前构建 WORKER_VERSION（生产不传）；旧构建测试进程可显式
    传入，使记录与 /internal/meta 同源该版本——不改模块全局。"""
    if idle_timeout is None:
        idle_timeout = _DEFAULT_IDLE_TIMEOUT
    if version is None:
        version = WORKER_VERSION
    home = kimi_home or _RUNTIME_HOME_OVERRIDE or os.environ.get('KIMI_HOME') \
        or os.path.join(os.path.expanduser('~'), '.kimi-code')
    # 状态目录：显式 state_dir / 测试注入 override / MW_STATE_DIR（_spawn 经它
    # 保证 worker 与 client 同目录）按原样使用（由提供方负责就绪与安全）；
    # 否则由 home 派生并确保（生产默认，与 client._runtime_dir_for 同目录）。
    if state_dir or _RUNTIME_STATE_DIR_OVERRIDE or os.environ.get('MW_STATE_DIR'):
        d = state_dir or _RUNTIME_STATE_DIR_OVERRIDE \
            or os.environ.get('MW_STATE_DIR')
        os.makedirs(d, exist_ok=True)
    else:
        d = _ensure_runtime_dir(home)
    # 终身锁（真 OS 锁）防同目录重复 worker；持锁即存活凭证，进程死亡自动
    # 释放。Windows 上 msvcrt 强制锁拒一切读——锁文件只作互斥，不含敏感内容。
    lock = _LifetimeLock(os.path.join(d, 'worker.lock'))
    if not lock.acquire():
        return 2
    home = kimi_home or _RUNTIME_HOME_OVERRIDE or os.environ.get('KIMI_HOME') \
        or os.path.join(os.path.expanduser('~'), '.kimi-code')
    secret = None
    mgr = None
    httpd = None
    exit_code = {'code': 0}
    try:
        secret = _write_secret(d)
        mgr = MobileBridgeManager(home)
        candidates = ([port] if port else
                      list(range(_PREFERRED_PORT,
                                 _PREFERRED_PORT + _PORT_SCAN_MAX)))
        for candidate in candidates:
            try:
                httpd = _WorkerHTTPServer(('127.0.0.1', candidate), _WorkerHandler)
                break
            except OSError:
                pass
        if httpd is None:
            # 端口全被占/绑定失败：按契约返回非零（finally 已清理锁/记录/
            # secret），绝不让异常逃逸出 run_worker。
            return 4
        httpd.manager = mgr
        httpd.secret = secret
        httpd.last_ipc = time.monotonic()
        httpd.worker_version = version

        def initiate_shutdown(code=0):
            exit_code['code'] = code
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        httpd.initiate_shutdown = initiate_shutdown
        _write_record(d, httpd.server_address[1], version=version)

        def watch():
            while True:
                time.sleep(1.0)
                if mgr._closing:
                    initiate_shutdown(0)
                    return
                with mgr._lock:
                    off = mgr._state == 'off'
                    failed_owner = (off and mgr._tunnel.get('error_code')
                                    == 'OWNER_LOST')
                # owner 失联已被桥吊销（会话全清，不可恢复）：worker 无存在
                # 意义，fail-closed 退出；显式 stop（tunnel off）不影响。
                if failed_owner:
                    initiate_shutdown('OWNER_LOST')
                    return
                # orphan off worker：无桥会话且长时间无认证 IPC 活动——没有
                # daemon 再需要它，自动退出并清理（下次 start 重新拉起）。
                if off and time.monotonic() - httpd.last_ipc > idle_timeout:
                    initiate_shutdown(0)
                    return

        threading.Thread(target=watch, daemon=True).start()
        httpd.serve_forever(poll_interval=0.3)
        if exit_code['code'] == 'OWNER_LOST':
            return 3
        return 0
    finally:
        if mgr is not None:
            try:
                mgr.shutdown()
            except Exception:
                pass
        if httpd is not None:
            try:
                httpd.server_close()
            except Exception:
                pass
        _remove_record(d)
        if secret is not None:
            try:
                os.remove(os.path.join(d, 'ipc.secret'))
            except OSError:
                pass
        lock.release()


def _main():
    sys.dont_write_bytecode = True
    code = run_worker()
    if isinstance(code, int):
        sys.exit(code)
    sys.exit(0)


if __name__ == '__main__':
    _main()
