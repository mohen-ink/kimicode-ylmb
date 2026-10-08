# -*- coding: utf-8 -*-
import ctypes
import hashlib
import http.client
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes

CONNECTOR_VERSION = '2026.9.3'
CONNECTOR_SIZE = 55366080
CONNECTOR_SHA256 = 'f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2'
CONNECTOR_URL = ('https://github.com/cloudflare/cloudflared/releases/download/'
                 '2026.9.3/cloudflared-windows-amd64.exe')
LICENSE_URL = 'https://raw.githubusercontent.com/cloudflare/cloudflared/2026.9.3/LICENSE'
LICENSE_SIZE = 11357
LICENSE_SHA256 = '58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd'
_PACKAGED_LICENSE = (Path(os.path.realpath(__file__)).parent.parent / 'assets' / 'vendor'
                     / 'cloudflared-2026.9.3-LICENSE')
CONSENT_VERSION = 'cloudflare-quick-2026-09-v1'
INSTALL_TIMEOUT = 180.0
DOWNLOAD_TIMEOUT = 30.0
STARTUP_TIMEOUT = 45.0
LICENSE_MAX_BYTES = 65536
LOG_LINE_MAX_BYTES = 4096
_CDN_HOSTS = frozenset(('release-assets.githubusercontent.com',
                        'objects.githubusercontent.com',
                        'github-releases.githubusercontent.com'))
_PUBLIC_ORIGIN_RE = re.compile(
    r'https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com\Z')

# 瞬时传输故障白名单 —— 只有"再发一次就可能成功"的错误才允许重试。
# Python 3.8 里 socket.timeout 既不是 TimeoutError 的子类也未继承 ConnectionError，
# 故必须单独列出，否则连接/读取阶段的超时（正是 README 承诺"瞬态自动重试"的主场景）
# 会被当作永久错误直接失败。
_TLS_TRANSIENT_MRO = frozenset(cls.__name__ for cls in (
    ssl.SSLEOFError, ssl.SSLSyscallError, ssl.SSLWantReadError, ssl.SSLWantWriteError))
_TRANSIENT_ERRORS = (socket.timeout, TimeoutError, ConnectionError, socket.gaierror,
                     http.client.IncompleteRead)


def _is_transient_error(error):
    """判定重试白名单；证书校验失败等安全类错误必须落回永久失败，绝不重试。

    顺序有意为之：证书类先单独判否，即便将来有人把 SSLError 整个加进瞬时元组，
    也不会把"证书校验失败"变成可重试（那等于把 MITM 变成偶发抖动）。
    urllib 会把底层异常包进 URLError.reason，故 reason 与原始异常走同一判定。
    """
    if isinstance(error, (ssl.SSLCertVerificationError, ssl.CertificateError)):
        return False
    if isinstance(error, _TRANSIENT_ERRORS):
        return True
    return any(cls.__name__ in _TLS_TRANSIENT_MRO for cls in type(error).__mro__)


def _read_packaged_license():
    def metadata():
        for directory in _PACKAGED_LICENSE.parents:
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise OSError('packaged license directory rejected')
        info = _PACKAGED_LICENSE.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or info.st_size != LICENSE_SIZE):
            raise OSError('packaged license file rejected')
        change_time = _file_change_time(_PACKAGED_LICENSE) if os.name == 'nt' else info.st_ctime_ns
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, change_time)

    signature = metadata()
    with _PACKAGED_LICENSE.open('rb') as stream:
        data = stream.read(LICENSE_SIZE + 1)
    if (len(data) != LICENSE_SIZE or hashlib.sha256(data).hexdigest() != LICENSE_SHA256
            or signature != metadata()):
        raise ValueError('packaged license integrity check')
    return data


def _supported_windows():
    return (os.name == 'nt' and platform.machine().upper() in ('AMD64', 'X86_64')
            and ctypes.sizeof(ctypes.c_void_p) == 8)


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _allowed_download_url(url, license_file=False):
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment):
            return False
        if license_file:
            return (parsed.hostname == 'raw.githubusercontent.com'
                    and parsed.path == '/cloudflare/cloudflared/2026.9.3/LICENSE'
                    and not parsed.query)
        if parsed.hostname in _CDN_HOSTS:
            return True
        return (parsed.hostname == 'github.com'
                and parsed.path == urllib.parse.urlsplit(CONNECTOR_URL).path
                and not parsed.query)
    except (ValueError, TypeError):
        return False


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def __init__(self, license_file, deadline):
        self.license_file = license_file
        self.deadline = deadline

    def redirect_request(self, request, fp, code, message, headers, newurl):
        if (time.monotonic() >= self.deadline
                or not _allowed_download_url(newurl, self.license_file)):
            raise urllib.error.URLError('download redirect rejected')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _download(url, destination, max_bytes, deadline):
    if url not in (CONNECTOR_URL, LICENSE_URL):
        raise ValueError('fixed download only')
    license_file = url == LICENSE_URL
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('download deadline')
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _OfficialRedirects(license_file, deadline))
    request = urllib.request.Request(url, headers={
        'User-Agent': 'kimi-mobile-connector/3.3.6', 'Accept-Encoding': 'identity'})
    with opener.open(request, timeout=min(DOWNLOAD_TIMEOUT, remaining)) as response:
        if not _allowed_download_url(response.geturl(), license_file):
            raise ValueError('download host rejected')
        declared = response.headers.get('Content-Length')
        if declared is not None and (not declared.isdigit() or int(declared) > max_bytes):
            raise ValueError('download size rejected')
        read = getattr(response, 'read1', response.read)
        received = 0
        with destination.open('xb') as stream:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('download deadline')
                sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                if sock is not None:
                    sock.settimeout(min(DOWNLOAD_TIMEOUT, remaining))
                block = read(min(65536, max_bytes - received + 1))
                if time.monotonic() >= deadline:
                    raise TimeoutError('download deadline')
                if not block:
                    break
                received += len(block)
                if received > max_bytes:
                    raise ValueError('download size rejected')
                stream.write(block)
            if declared is not None and received != int(declared):
                raise ValueError('download truncated')
            stream.flush()
            os.fsync(stream.fileno())
    return received


def _private_environment(directory):
    allowed = frozenset(('SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE',
                         'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS'))
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    root = str(directory)
    env.update({'HOME': root, 'USERPROFILE': root, 'APPDATA': root,
                'LOCALAPPDATA': root, 'TEMP': root, 'TMP': root})
    return env


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', ctypes.c_void_p),
               ('bInheritHandle', wintypes.BOOL)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR),
               ('lpDesktop', wintypes.LPWSTR), ('lpTitle', wintypes.LPWSTR),
               ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD),
               ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD),
               ('dwXCountChars', wintypes.DWORD), ('dwYCountChars', wintypes.DWORD),
               ('dwFillAttribute', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
               ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD),
               ('lpReserved2', ctypes.c_void_p), ('hStdInput', wintypes.HANDLE),
               ('hStdOutput', wintypes.HANDLE), ('hStdError', wintypes.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [('StartupInfo', _StartupInfo), ('lpAttributeList', ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE),
               ('dwProcessId', wintypes.DWORD), ('dwThreadId', wintypes.DWORD)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
               ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', wintypes.DWORD),
               ('MinimumWorkingSetSize', ctypes.c_size_t),
               ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
               ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
               ('SchedulingClass', wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimits), ('IoInfo', _IoCounters),
               ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
               ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class _WindowsApi:
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CloseHandle': (wintypes.BOOL, [wintypes.HANDLE]),
            'CreateJobObjectW': (wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
            'SetInformationJobObject': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
            'AssignProcessToJobObject': (wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
            'CreatePipe': (wintypes.BOOL, [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(_SecurityAttributes), wintypes.DWORD]),
            'SetHandleInformation': (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]),
            'CreateFileW': (wintypes.HANDLE, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_SecurityAttributes), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]),
            'InitializeProcThreadAttributeList': (wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]),
            'UpdateProcThreadAttribute': (wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]),
            'DeleteProcThreadAttributeList': (None, [ctypes.c_void_p]),
            'CreateProcessW': (wintypes.BOOL, [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(_ProcessInformation)]),
            'ResumeThread': (wintypes.DWORD, [wintypes.HANDLE]),
            'TerminateProcess': (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
            'WaitForSingleObject': (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
            'PeekNamedPipe': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]),
            'ReadFile': (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]),
        }
        for name, (restype, argtypes) in signatures.items():
            function = getattr(self.kernel, name)
            function.restype = restype
            function.argtypes = argtypes
            setattr(self, name, function)

    def check(self, result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result


class _WindowsJobProcess:
    def __init__(self, argv, cwd, env):
        self._api = _WindowsApi()
        self._lock = threading.RLock()
        self._guard_job = self._job = self._process = self._thread = None
        self._read_pipe = self._write_pipe = self._stdin = None
        self.pid = None
        attributes = None
        attributes_initialized = False
        try:
            self._guard_job = self._new_job()
            self._job = self._new_job()
            security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
            read_pipe, write_pipe = wintypes.HANDLE(), wintypes.HANDLE()
            self._api.check(self._api.CreatePipe(ctypes.byref(read_pipe), ctypes.byref(write_pipe), ctypes.byref(security), 65536))
            self._read_pipe, self._write_pipe = read_pipe.value, write_pipe.value
            self._api.check(self._api.SetHandleInformation(self._read_pipe, 1, 0))
            self._stdin = self._api.CreateFileW('NUL', 0x80000000, 3, ctypes.byref(security), 3, 0, None)
            if self._stdin == ctypes.c_void_p(-1).value:
                self._stdin = None
                raise ctypes.WinError(ctypes.get_last_error())
            size = ctypes.c_size_t()
            self._api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            self._api.check(self._api.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)))
            attributes_initialized = True
            handles = (wintypes.HANDLE * 2)(self._write_pipe, self._stdin)
            self._api.check(self._api.UpdateProcThreadAttribute(attributes, 0, 0x20002, handles, ctypes.sizeof(handles), None, None))
            jobs = (wintypes.HANDLE * 1)(self._guard_job)
            self._api.check(self._api.UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, ctypes.sizeof(jobs), None, None))
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100
            startup.StartupInfo.hStdInput = self._stdin
            startup.StartupInfo.hStdOutput = self._write_pipe
            startup.StartupInfo.hStdError = self._write_pipe
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment = ctypes.create_unicode_buffer(''.join(
                '{}={}\0'.format(key, value) for key, value in sorted(env.items(), key=lambda item: item[0].upper())) + '\0')
            info = _ProcessInformation()
            flags = 0x4 | 0x400 | 0x80000 | 0x08000000
            self._api.check(self._api.CreateProcessW(argv[0], command, None, None, True,
                flags, environment, str(cwd), ctypes.byref(startup), ctypes.byref(info)))
            self._process, self._thread, self.pid = info.hProcess, info.hThread, info.dwProcessId
            self._close_handle('_write_pipe')
            self._close_handle('_stdin')
            if not self._assign_job():
                raise ctypes.WinError(ctypes.get_last_error())
            if self._api.ResumeThread(self._thread) == 0xffffffff:
                raise ctypes.WinError(ctypes.get_last_error())
            self._close_handle('_thread')
        except BaseException:
            if self._process:
                self._api.TerminateProcess(self._process, 1)
                self._api.WaitForSingleObject(self._process, 2000)
            self.close()
            raise
        finally:
            if attributes_initialized:
                self._api.DeleteProcThreadAttributeList(attributes)

    def _new_job(self):
        job = self._api.check(self._api.CreateJobObjectW(None, None))
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        try:
            self._api.check(self._api.SetHandleInformation(job, 1, 0))
            self._api.check(self._api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            return job
        except BaseException:
            self._api.CloseHandle(job)
            raise

    def _assign_job(self):
        return self._api.AssignProcessToJobObject(self._job, self._process)

    def _close_handle(self, name):
        handle = getattr(self, name)
        if handle:
            setattr(self, name, None)
            self._api.CloseHandle(handle)

    def is_alive(self):
        with self._lock:
            return bool(self._process and self._api.WaitForSingleObject(self._process, 0) == 258)

    def read_chunk(self):
        with self._lock:
            if not self._read_pipe:
                return None
            available = wintypes.DWORD()
            if not self._api.PeekNamedPipe(self._read_pipe, None, 0, None, ctypes.byref(available), None):
                return None
            if not available.value:
                return b''
            buffer = ctypes.create_string_buffer(min(4096, available.value))
            received = wintypes.DWORD()
            if not self._api.ReadFile(self._read_pipe, buffer, len(buffer), ctypes.byref(received), None):
                return None
            return buffer.raw[:received.value]

    def close(self):
        with self._lock:
            self._close_handle('_job')
            self._close_handle('_guard_job')
            if self._process:
                self._api.WaitForSingleObject(self._process, 2000)
            for name in ('_read_pipe', '_write_pipe', '_stdin', '_thread', '_process'):
                self._close_handle(name)


_DIAGNOSTIC_KINDS = ('connection_registered', 'connection_unregistered',
                     'connection_retrying', 'origin_request_failed')
_DIAGNOSTIC_RING_MAX = 8
_DIAGNOSTIC_COUNTER_MAX = 65535
_DIAGNOSTIC_LINE_MAX = 1024
_DIAGNOSTIC_RULES = (
    ('connection_registered', (
        r'(?:^|\s)Registered tunnel connection(?:\s|$)',
        r'(?:^|\s)connIndex=[0-3](?:\s|$)',
        r'(?:^|\s)protocol=(?:quic|http2)(?:\s|$)',
    )),
    ('connection_unregistered', (
        r'(?:^|\s)Unregistered tunnel connection(?:\s|$)',
        r'(?:^|\s)connIndex=[0-3](?:\s|$)',
    )),
    ('connection_retrying', (
        r'(?:^|\s)Retrying connection in up to (?:[0-9]+(?:\.[0-9]+)?(?:ns|us|ms|s|m|h))+(?=\s|$)',
        r'(?:^|\s)connIndex=[0-3](?:\s|$)',
    )),
    ('origin_request_failed', (
        r'(?:^|\s)failed to serve incoming request(?:\s|$)',
        r'(?:^|\s)error=',
    )),
)
_DIAGNOSTIC_COMPILED = tuple(
    (kind, tuple(re.compile(pattern) for pattern in patterns))
    for kind, patterns in _DIAGNOSTIC_RULES)
_DIAGNOSTIC_INDEX_RE = re.compile(r'(?:^|\s)connIndex=([0-3])(?:\s|$)')


class _LogSignals:
    def __init__(self):
        self._partial = bytearray()
        self._discarding = False
        self.origin = None
        self.registered = False
        self._diag_lock = threading.Lock()
        self._diag_counts = {kind: 0 for kind in _DIAGNOSTIC_KINDS}
        self._diag_last = None
        self._diag_ring = []
        self._diag_active = set()

    def _classify(self, line):
        if len(line) > _DIAGNOSTIC_LINE_MAX:
            return None
        for kind, patterns in _DIAGNOSTIC_COMPILED:
            if all(pattern.search(line) for pattern in patterns):
                return kind
        return None

    def _record(self, kind, line):
        if kind is None:
            return
        index_match = _DIAGNOSTIC_INDEX_RE.search(line)
        index = int(index_match.group(1)) if index_match else None
        with self._diag_lock:
            if self._diag_counts[kind] < _DIAGNOSTIC_COUNTER_MAX:
                self._diag_counts[kind] += 1
            if kind == 'connection_registered' and index is not None:
                self._diag_active.add(index)
            elif kind == 'connection_unregistered' and index is not None:
                self._diag_active.discard(index)
            event = {'kind': kind, 'time': time.time()}
            self._diag_last = event
            self._diag_ring.append(event)
            del self._diag_ring[:-_DIAGNOSTIC_RING_MAX]

    def diagnostics(self):
        with self._diag_lock:
            counts = dict(self._diag_counts)
            active = sorted(self._diag_active)
            last = dict(self._diag_last) if self._diag_last else None
            ring = [dict(event) for event in self._diag_ring]
        if last is None:
            transport_state = 'unknown'
        elif active:
            transport_state = 'ready'
        else:
            transport_state = 'reconnecting'
        return {'counts': counts,
                'last_event': last,
                'recent_events': ring,
                'active_conn_indices': active,
                'active_conn_count': len(active),
                'transport_state': transport_state}

    def feed(self, chunk):
        for piece in chunk.splitlines(keepends=True):
            ended = piece.endswith(b'\n') or piece.endswith(b'\r')
            if self._discarding:
                if ended:
                    self._discarding = False
                continue
            if len(self._partial) + len(piece) > LOG_LINE_MAX_BYTES:
                self._partial.clear()
                self._discarding = not ended
                continue
            self._partial.extend(piece)
            if ended:
                line = self._partial.decode('utf-8', errors='replace').strip()
                self._partial.clear()
                self._record(self._classify(line), line)
                if self.origin is None:
                    for candidate in re.findall(r'(?<![^\s|])https?://[^\s|]+', line):
                        if _PUBLIC_ORIGIN_RE.fullmatch(candidate):
                            self.origin = candidate
                            break
                if (re.search(r'(?:^|\s)Registered tunnel connection(?:\s|$)', line)
                        and re.search(r'(?:^|\s)connIndex=[0-3](?:\s|$)', line)
                        and re.search(r'(?:^|\s)protocol=(?:quic|http2)(?:\s|$)', line)):
                    self.registered = True


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [('CreationTime', ctypes.c_longlong), ('LastAccessTime', ctypes.c_longlong),
               ('LastWriteTime', ctypes.c_longlong), ('ChangeTime', ctypes.c_longlong),
               ('FileAttributes', wintypes.DWORD)]


def _file_change_time(path):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _FileBasicInfo()
        if not kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.FileAttributes & 0x400:
            raise OSError('runtime reparse point')
        return info.ChangeTime
    finally:
        kernel.CloseHandle(handle)


class ConnectorRuntime:
    def __init__(self, kimi_home):
        self._home = Path(os.path.realpath(str(Path(kimi_home).absolute())))
        self._root = self._home / 'usage-dashboard' / 'runtime' / 'cloudflared' / CONNECTOR_VERSION
        self._exe = self._root / 'cloudflared-windows-amd64.exe'
        self._license = self._root / 'LICENSE'
        self._lock = threading.RLock()
        self._closed = False
        self._connector_state = 'missing'
        self._connector_error = None
        self._tunnel_state = 'off'
        self._tunnel_error = None
        self._public_origin = None
        self._cache = None
        self._install_generation = 0
        self._generation = 0
        self._install_thread = None
        self._start_thread = None
        self._process = None
        self._signals = None

    def _check_directories(self, create=False):
        directories = (self._home, self._home / 'usage-dashboard',
                       self._root.parent.parent, self._root.parent, self._root)
        for index, directory in enumerate(directories):
            if create:
                directory.mkdir(parents=index == 0, exist_ok=True)
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise OSError('runtime directory rejected')

    def _safe_metadata(self, path):
        self._check_directories()
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise OSError('runtime file rejected')
        change_time = _file_change_time(path) if os.name == 'nt' else info.st_ctime_ns
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, change_time)

    def _verified(self, full=False):
        try:
            signature = (self._safe_metadata(self._exe), self._safe_metadata(self._license))
            if signature[0][2] != CONNECTOR_SIZE or signature[1][2] != LICENSE_SIZE:
                self._cache = None
                return False
            if not full and self._cache == signature:
                return True
            if _hash_file(self._exe) != CONNECTOR_SHA256:
                self._cache = None
                return False
            with self._license.open('rb') as stream:
                license_data = stream.read(LICENSE_SIZE + 1)
            if (len(license_data) != LICENSE_SIZE
                    or hashlib.sha256(license_data).hexdigest() != LICENSE_SHA256):
                self._cache = None
                return False
            if signature != (self._safe_metadata(self._exe), self._safe_metadata(self._license)):
                self._cache = None
                return False
            self._cache = signature
            return True
        except OSError:
            self._cache = None
            return False

    def _snapshot(self):
        connector = {'state': self._connector_state, 'version': CONNECTOR_VERSION}
        tunnel = {'state': self._tunnel_state}
        if self._connector_error:
            connector['error_code'] = self._connector_error
        if self._tunnel_error:
            tunnel['error_code'] = self._tunnel_error
        result = {'connector': connector, 'tunnel': tunnel}
        if (self._tunnel_state == 'ready' and self._public_origin
                and self._process is not None and self._process.is_alive()):
            result['public_origin'] = self._public_origin
        signals = self._signals
        if signals is not None:
            result['diagnostics'] = signals.diagnostics()
        return result

    def status(self):
        with self._lock:
            if self._connector_state not in ('installing', 'failed'):
                self._connector_state = 'installed' if self._verified() else 'missing'
            return self._snapshot()

    def install(self, consent, consent_version):
        with self._lock:
            error = None
            if self._closed:
                error = 'START_CANCELLED'
            elif consent is not True or consent_version != CONSENT_VERSION:
                error = 'CONSENT_REQUIRED'
            elif not _supported_windows():
                error = 'CONNECTOR_UNSUPPORTED'
            elif self._install_thread is not None and self._install_thread.is_alive():
                error = 'CONNECTOR_BUSY'
            elif self._tunnel_state in ('starting', 'ready'):
                error = 'CONNECTOR_BUSY'
            if error:
                result = self._snapshot()
                result['connector']['error_code'] = error
                return result
            if self._connector_state == 'installed' and self._cache is not None:
                try:
                    if self._cache == (self._safe_metadata(self._exe), self._safe_metadata(self._license)):
                        return self._snapshot()
                except OSError:
                    pass
            self._install_generation += 1
            token = self._install_generation
            self._connector_state = 'installing'
            self._connector_error = None
            self._cache = None
            result = self._snapshot()
            self._install_thread = threading.Thread(target=self._install_worker, args=(token,), daemon=True)
            self._install_thread.start()
            return result

    def _install_expired(self, token):
        with self._lock:
            if not self._closed and token == self._install_generation and self._connector_state == 'installing':
                self._install_generation += 1
                self._connector_state = 'failed'
                self._connector_error = 'CONNECTOR_INSTALL_FAILED'

    def _fixed_exe_download_with_retry(self, part, token, deadline):
        for attempt in range(3):
            with self._lock:
                if self._closed or token != self._install_generation:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError('install deadline')
            try:
                result = _download(CONNECTOR_URL, part, CONNECTOR_SIZE, deadline)
            except Exception as error:
                with self._lock:
                    if self._closed or token != self._install_generation:
                        return
                    if time.monotonic() >= deadline:
                        raise TimeoutError('install deadline')
                if isinstance(error, urllib.error.HTTPError):
                    if error.code != 429 and not 500 <= error.code < 600:
                        raise
                    error.close()
                else:
                    reason = error.reason if isinstance(error, urllib.error.URLError) else error
                    if not _is_transient_error(reason):
                        raise
                if attempt == 2:
                    raise
                try:
                    part.unlink()
                except FileNotFoundError:
                    pass
                wait_until = time.monotonic() + attempt + 1
                while True:
                    with self._lock:
                        if self._closed or token != self._install_generation:
                            return
                        now = time.monotonic()
                        if now >= deadline:
                            raise TimeoutError('install deadline')
                        pause = min(.1, wait_until - now, deadline - now)
                    if pause <= 0:
                        break
                    time.sleep(pause)
            else:
                with self._lock:
                    if self._closed or token != self._install_generation:
                        return
                    if time.monotonic() >= deadline:
                        raise TimeoutError('install deadline')
                return result

    def _install_worker(self, token):
        part = license_part = None
        deadline = time.monotonic() + INSTALL_TIMEOUT
        timer = threading.Timer(INSTALL_TIMEOUT, self._install_expired, args=(token,))
        timer.daemon = True
        timer.start()
        error = 'CONNECTOR_INSTALL_FAILED'
        try:
            with self._lock:
                if self._closed or token != self._install_generation:
                    return
            try:
                data = _read_packaged_license()
            except ValueError:
                error = 'CONNECTOR_HASH_MISMATCH'
                raise
            with self._lock:
                if self._closed or token != self._install_generation:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError('install deadline')
                self._check_directories(create=True)
                suffix = secrets.token_hex(12)
                part = self._root / ('connector-' + suffix + '.part')
                license_part = self._root / ('license-' + suffix + '.part')
            self._fixed_exe_download_with_retry(part, token, deadline)
            with self._lock:
                if self._closed or token != self._install_generation:
                    return
            if part.stat().st_size != CONNECTOR_SIZE or _hash_file(part) != CONNECTOR_SHA256:
                error = 'CONNECTOR_HASH_MISMATCH'
                raise ValueError('connector integrity check')
            with self._lock:
                if self._closed or token != self._install_generation:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError('install deadline')
            with license_part.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if (self._safe_metadata(license_part)[2] != LICENSE_SIZE
                    or _hash_file(license_part) != LICENSE_SHA256):
                error = 'CONNECTOR_HASH_MISMATCH'
                raise ValueError('license integrity check')
            with self._lock:
                if self._closed or token != self._install_generation:
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError('install deadline')
                self._check_directories()
                os.replace(str(license_part), str(self._license))
                os.replace(str(part), str(self._exe))
                self._connector_state = 'installed'
                self._connector_error = None
                self._cache = None
        except Exception:
            with self._lock:
                if not self._closed and token == self._install_generation:
                    self._connector_state = 'failed'
                    self._connector_error = error
        finally:
            timer.cancel()
            for path in (part, license_part):
                if path is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def start(self, loopback_port, on_ready, on_failure):
        with self._lock:
            error = None
            if self._closed:
                error = 'START_CANCELLED'
            elif not _supported_windows():
                error = 'CONNECTOR_UNSUPPORTED'
            elif type(loopback_port) is not int or not 1 <= loopback_port <= 65535:
                error = 'TUNNEL_START_FAILED'
            elif self._tunnel_state in ('starting', 'ready') or self._connector_state == 'installing':
                error = 'CONNECTOR_BUSY'
            if error:
                threading.Thread(target=self._callback, args=(on_failure, error), daemon=True).start()
                return
            self._generation += 1
            token = self._generation
            self._tunnel_state = 'starting'
            self._tunnel_error = None
            self._public_origin = None
            self._start_thread = threading.Thread(target=self._start_worker,
                args=(token, loopback_port, on_ready, on_failure), daemon=True)
            self._start_thread.start()

    @staticmethod
    def _callback(callback, value):
        try:
            callback(value)
        except Exception:
            pass

    def _fail(self, token, code, on_failure):
        with self._lock:
            if self._closed or token != self._generation or self._tunnel_state == 'failed':
                return
            self._tunnel_state = 'failed'
            self._tunnel_error = code
            self._public_origin = None
            self._signals = None
            process, self._process = self._process, None
        try:
            if process is not None:
                process.close()
        finally:
            self._callback(on_failure, code)

    def _start_worker(self, token, port, on_ready, on_failure):
        process = None
        directory = None
        deadline = time.monotonic() + STARTUP_TIMEOUT
        try:
            with self._lock:
                if self._closed or token != self._generation:
                    return
                valid = self._verified(full=True)
                if not valid:
                    self._connector_state = 'missing'
            if not valid:
                self._fail(token, 'CONNECTOR_MISSING', on_failure)
                return
            directory = tempfile.TemporaryDirectory(prefix='session-', dir=str(self._root))
            private = Path(directory.name).absolute()
            config = private / 'config.yml'
            with config.open('xb') as stream:
                stream.write(b'{}\n')
            argv = [str(self._exe), 'tunnel', '--config', str(config), '--no-autoupdate',
                    '--url', 'http://127.0.0.1:{}'.format(port), '--metrics', '127.0.0.1:0',
                    '--management-diagnostics=false']
            with self._lock:
                if self._closed or token != self._generation:
                    return
                if not self._verified(full=True):
                    raise OSError('connector changed')
                if time.monotonic() >= deadline:
                    raise TimeoutError('startup deadline')
                process = _WindowsJobProcess(argv, str(private), _private_environment(private))
                self._process = process
                self._connector_state = 'installed'
                self._connector_error = None
            signals = _LogSignals()
            with self._lock:
                if self._closed or token != self._generation:
                    return
                self._signals = signals
            ready = False
            while True:
                with self._lock:
                    if self._closed or token != self._generation:
                        return
                if not ready and time.monotonic() >= deadline:
                    raise TimeoutError('startup deadline')
                for _ in range(16):
                    chunk = process.read_chunk()
                    if not chunk:
                        break
                    signals.feed(chunk)
                if not process.is_alive():
                    self._fail(token, 'TUNNEL_EXITED', on_failure)
                    return
                if not ready and signals.origin and signals.registered:
                    with self._lock:
                        if self._closed or token != self._generation:
                            return
                        child_alive = process.is_alive()
                        if child_alive:
                            if time.monotonic() >= deadline:
                                raise TimeoutError('startup deadline')
                            ready = True
                            self._tunnel_state = 'ready'
                            self._tunnel_error = None
                            self._public_origin = signals.origin
                    if not child_alive:
                        self._fail(token, 'TUNNEL_EXITED', on_failure)
                        return
                    self._callback(on_ready, signals.origin)
                time.sleep(0.03)
        except TimeoutError:
            self._fail(token, 'TUNNEL_TIMEOUT', on_failure)
        except Exception:
            self._fail(token, 'TUNNEL_START_FAILED', on_failure)
        finally:
            if process is not None:
                process.close()
            if directory is not None:
                try:
                    directory.cleanup()
                except OSError:
                    pass

    def stop(self):
        with self._lock:
            if self._connector_state == 'installing':
                self._install_generation += 1
                self._connector_state = 'failed'
                self._connector_error = 'START_CANCELLED'
            self._generation += 1
            self._tunnel_state = 'off'
            self._tunnel_error = None
            self._public_origin = None
            self._signals = None
            process, self._process = self._process, None
        if process is not None:
            process.close()

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._install_generation += 1
            if self._connector_state == 'installing':
                self._connector_state = 'failed'
                self._connector_error = 'START_CANCELLED'
        self.stop()
