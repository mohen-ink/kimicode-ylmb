# -*- coding: utf-8 -*-
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from mobile_security import _set_protected_dacl
from mobile_tunnel import (_WindowsJobProcess, _file_change_time, _hash_file,
                           _private_environment, _supported_windows)

CONSENT_VERSION = 'frp-tcp-http-v1'
RELAY_VERSION = '0.67.0'
ARCHIVE_URL = ('https://github.com/fatedier/frp/releases/download/v0.67.0/'
               'frp_0.67.0_windows_amd64.zip')
ARCHIVE_SIZE = 13729508
ARCHIVE_SHA256 = '8baf23e3fbd486f6ba0913501372c5ff0053efa88a8b8d391f3605ced43d2af5'
CONNECTOR_SIZE = 16326144
CONNECTOR_SHA256 = '4606ce1567074e102a703db6a662d23d6a13f2cbffbc054faa4e110ff7a75582'
LICENSE_SIZE = 11358
LICENSE_SHA256 = 'c6596eb7be8581c18be736c846fb9173b69eccf6ef94c5135893ec56bd92ba08'
EXE_MEMBER = 'frp_0.67.0_windows_amd64/frpc.exe'
LICENSE_MEMBER = 'frp_0.67.0_windows_amd64/LICENSE'
INSTALL_TIMEOUT = 180.0
DOWNLOAD_TIMEOUT = 30.0
STARTUP_TIMEOUT = 45.0
HEARTBEAT_INTERVAL = 5
HEARTBEAT_TIMEOUT = 15
LOG_LINE_MAX_BYTES = 4096
LOG_CHUNK_MAX_BYTES = 4096
LOG_BLOCKS_PER_POLL = 16
CA_MAX_BYTES = 65536
CA_MAX_CERTIFICATES = 4
_PACKAGED_ROOT = Path(os.path.realpath(os.path.abspath(__file__))).parent.parent
_PACKAGED_LICENSE = _PACKAGED_ROOT / 'assets' / 'vendor' / 'frp-0.67.0-LICENSE'
_CDN_HOSTS = frozenset(('release-assets.githubusercontent.com',
                        'objects.githubusercontent.com',
                        'github-releases.githubusercontent.com'))
_CONFIG_FIELDS = frozenset(('server_ip', 'server_port', 'remote_port', 'token', 'ca_cert'))
_CERTIFICATE_RE = re.compile(
    r'-----BEGIN CERTIFICATE-----\n([A-Za-z0-9+/=\n]+)\n-----END CERTIFICATE-----')
_NONPUBLIC_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
    '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
    '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15',
    '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4'))


def validate_config(config):
    try:
        if type(config) is not dict or set(config) != _CONFIG_FIELDS:
            raise ValueError()
        server_ip = config['server_ip']
        if type(server_ip) is not str:
            raise ValueError()
        address = ipaddress.IPv4Address(server_ip)
        if (str(address) != server_ip or not address.is_global
                or address.is_multicast or address.is_reserved
                or any(address in network for network in _NONPUBLIC_NETWORKS)):
            raise ValueError()
        for name in ('server_port', 'remote_port'):
            if type(config[name]) is not int or not 1 <= config[name] <= 65535:
                raise ValueError()
        if config['server_port'] == config['remote_port']:
            raise ValueError()
        token = config['token']
        if (type(token) is not str or not 32 <= len(token) <= 512
                or any(not 33 <= ord(char) <= 126 for char in token)):
            raise ValueError()
        ca = config['ca_cert']
        if type(ca) is not str or len(ca) > CA_MAX_BYTES:
            raise ValueError()
        ca.encode('ascii')
        ca = ca.replace('\r\n', '\n').strip(' \t\n')
        certificates = []
        position = 0
        for match in _CERTIFICATE_RE.finditer(ca):
            if ca[position:match.start()].strip(' \t\n'):
                raise ValueError()
            base64.b64decode(match.group(1).replace('\n', ''), validate=True)
            certificates.append(match.group(0))
            position = match.end()
        if (ca[position:].strip(' \t\n')
                or not 1 <= len(certificates) <= CA_MAX_CERTIFICATES
                or len(set(certificates)) != len(certificates)):
            raise ValueError()
        ca = '\n'.join(certificates) + '\n'
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=ca)
        counts = context.cert_store_stats()
        if counts['x509'] != len(certificates) or counts['x509_ca'] != len(certificates):
            raise ValueError()
        return {'server_ip': server_ip, 'server_port': config['server_port'],
                'remote_port': config['remote_port'], 'token': token, 'ca_cert': ca}
    except (ValueError, TypeError, KeyError, UnicodeError, ssl.SSLError):
        raise ValueError('RELAY_CONFIG_INVALID') from None


def public_origin(config):
    valid = validate_config(config)
    return 'http://{}:{}'.format(valid['server_ip'], valid['remote_port'])


def _check_directory(path):
    for directory in (path,) + tuple(path.parents):
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise OSError('runtime directory rejected')


def _safe_metadata(path):
    _check_directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or info.st_nlink != 1):
        raise OSError('runtime file rejected')
    changed = _file_change_time(path) if os.name == 'nt' else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, changed)


def _verified_file(path, size, digest):
    before = _safe_metadata(path)
    if before[2] != size or _hash_file(path) != digest:
        return False
    return before == _safe_metadata(path)


def _read_packaged_license():
    package_root = Path(os.path.realpath(str(_PACKAGED_ROOT)))
    license_path = package_root / 'assets' / 'vendor' / 'frp-0.67.0-LICENSE'
    before = _safe_metadata(license_path)
    with license_path.open('rb') as stream:
        data = stream.read(LICENSE_SIZE + 1)
    if (len(data) != LICENSE_SIZE or hashlib.sha256(data).hexdigest() != LICENSE_SHA256
            or before != _safe_metadata(license_path)):
        raise ValueError('license integrity check')
    return data


def _allowed_download_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment):
            return False
        if parsed.hostname in _CDN_HOSTS:
            return True
        return (parsed.hostname == 'github.com'
                and parsed.path == urllib.parse.urlsplit(ARCHIVE_URL).path
                and not parsed.query)
    except (ValueError, TypeError):
        return False


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def __init__(self, deadline):
        self.deadline = deadline

    def redirect_request(self, request, fp, code, message, headers, newurl):
        if time.monotonic() >= self.deadline or not _allowed_download_url(newurl):
            raise urllib.error.URLError('download redirect rejected')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _download(destination, deadline, cancelled):
    remaining = deadline - time.monotonic()
    if remaining <= 0 or cancelled():
        raise TimeoutError('download deadline')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        _OfficialRedirects(deadline))
    request = urllib.request.Request(ARCHIVE_URL, headers={
        'User-Agent': 'kimi-mobile-relay/0.67.0', 'Accept-Encoding': 'identity'})
    with opener.open(request, timeout=min(DOWNLOAD_TIMEOUT, remaining)) as response:
        if not _allowed_download_url(response.geturl()):
            raise ValueError('download host rejected')
        declared = response.headers.get('Content-Length')
        if declared is not None and (not declared.isdigit() or int(declared) != ARCHIVE_SIZE):
            raise ValueError('download size rejected')
        received = 0
        read = getattr(response, 'read1', response.read)
        with destination.open('xb') as stream:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or cancelled():
                    raise TimeoutError('download deadline')
                sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                if sock is not None:
                    sock.settimeout(min(DOWNLOAD_TIMEOUT, remaining))
                block = read(min(65536, ARCHIVE_SIZE - received + 1))
                if time.monotonic() >= deadline or cancelled():
                    raise TimeoutError('download deadline')
                if not block:
                    break
                received += len(block)
                if received > ARCHIVE_SIZE:
                    raise ValueError('download size rejected')
                stream.write(block)
            if received != ARCHIVE_SIZE:
                raise ValueError('download truncated')
            stream.flush()
            os.fsync(stream.fileno())


def _extract_fixed(archive, exe, license_path, license_data):
    if not _verified_file(archive, ARCHIVE_SIZE, ARCHIVE_SHA256):
        raise ValueError('archive integrity check')
    signature = _safe_metadata(archive)
    with zipfile.ZipFile(archive) as source:
        for member, target, size, digest in (
                (EXE_MEMBER, exe, CONNECTOR_SIZE, CONNECTOR_SHA256),
                (LICENSE_MEMBER, license_path, LICENSE_SIZE, LICENSE_SHA256)):
            matches = [info for info in source.infolist() if info.filename == member]
            if len(matches) != 1:
                raise ValueError('archive member rejected')
            info = matches[0]
            mode = info.external_attr >> 16
            if (info.is_dir() or info.file_size != size or info.flag_bits & 1
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError('archive member rejected')
            received = 0
            with source.open(info) as stream, target.open('xb') as output:
                while True:
                    block = stream.read(min(65536, size - received + 1))
                    if not block:
                        break
                    received += len(block)
                    if received > size:
                        raise ValueError('archive size rejected')
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if received != size or not _verified_file(target, size, digest):
                raise ValueError('member integrity check')
    if signature != _safe_metadata(archive) or license_path.read_bytes() != license_data:
        raise ValueError('archive changed')


def _write_private(path, data):
    _check_directory(path.parent)
    with path.open('xb') as stream:
        _set_protected_dacl(str(path), False)
        _safe_metadata(path)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _safe_metadata(path)


def _config_bytes(config, port, ca_path, proxy_name):
    quote = json.dumps
    lines = [
        'serverAddr = ' + quote(config['server_ip']),
        'serverPort = ' + str(config['server_port']),
        'loginFailExit = true',
        'log.to = "console"', 'log.level = "info"', 'log.disablePrintColor = true',
        'webServer.port = 0',
        'auth.method = "token"', 'auth.token = ' + quote(config['token']),
        'auth.additionalScopes = ["HeartBeats", "NewWorkConns"]',
        'transport.protocol = "tcp"', 'transport.tcpMux = false',
        'transport.dialServerTimeout = 10',
        'transport.heartbeatInterval = ' + str(HEARTBEAT_INTERVAL),
        'transport.heartbeatTimeout = ' + str(HEARTBEAT_TIMEOUT),
        'transport.tls.enable = true', 'transport.tls.disableCustomTLSFirstByte = true',
        'transport.tls.trustedCaFile = ' + quote(ca_path.as_posix()),
        'transport.tls.serverName = ' + quote(config['server_ip']),
        '', '[[proxies]]', 'name = ' + quote(proxy_name), 'type = "tcp"',
        'localIP = "127.0.0.1"', 'localPort = ' + str(port),
        'remotePort = ' + str(config['remote_port']),
        'transport.useEncryption = false', 'transport.useCompression = false', '']
    return '\n'.join(lines).encode('utf-8')


class _LogSignals:
    def __init__(self, proxy_name):
        self._partial = bytearray()
        self.proxy_name = proxy_name
        self.logged_in = False
        self.registered = False
        self.failure = None

    def _line(self, raw):
        if self.failure:
            return
        line = raw.decode('utf-8', errors='replace').strip()
        record = re.fullmatch(
            r'(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)? )?'
            r'\[([IWED])\] \[(?:client/)?(service|control|connector)\.go:\d+\] '
            r'(?:\[[^\]\r\n]+\] )?(.*)', line)
        if record is None:
            return
        level, source, message = record.groups()
        if source == 'service':
            if (level == 'I' and re.fullmatch(
                    r'login to server success, get run id \[[A-Za-z0-9_-]{1,128}\]', message)):
                if self.logged_in:
                    self.failure = 'TUNNEL_EXITED'
                self.logged_in = True
            elif message == 'try to connect to server...' and self.logged_in:
                self.failure = 'TUNNEL_EXITED'
            elif message.startswith(('connect to server error:', 'new control error:')):
                self.failure = 'TUNNEL_START_FAILED' if not self.logged_in else 'TUNNEL_EXITED'
        if source == 'control':
            if message == 'heartbeat timeout':
                self.failure = 'TUNNEL_TIMEOUT'
            elif (message.startswith('pong message contains error:')
                  or message == 'control message dispatcher exited'
                  or 'session closed' in message.lower()):
                self.failure = 'TUNNEL_EXITED'
            elif message.startswith('[' + self.proxy_name + '] start error:'):
                self.failure = 'TUNNEL_START_FAILED'
            elif level == 'I' and message == '[' + self.proxy_name + '] start proxy success':
                if self.logged_in:
                    self.registered = True
        if self.logged_in and ('session closed' in message.lower()
                               or 'connection closed' in message.lower()
                               or message.startswith('StartWorkConn contains error:')):
            self.failure = 'TUNNEL_EXITED'

    def feed(self, chunk):
        if type(chunk) is not bytes or len(chunk) > LOG_CHUNK_MAX_BYTES:
            self.failure = 'TUNNEL_START_FAILED'
            self._partial.clear()
            return
        for piece in chunk.splitlines(keepends=True):
            if len(self._partial) + len(piece) > LOG_LINE_MAX_BYTES:
                self.failure = 'TUNNEL_START_FAILED'
                self._partial.clear()
                return
            self._partial.extend(piece)
            if piece.endswith((b'\n', b'\r')):
                self._line(bytes(self._partial))
                self._partial.clear()

    def clear(self):
        self._partial.clear()


def _cleanup_session(directory):
    if directory is None:
        return
    _check_directory(directory)
    for name in ('frpc.toml', 'ca.pem'):
        path = directory / name
        try:
            _safe_metadata(path)
        except FileNotFoundError:
            continue
        path.unlink()
    directory.rmdir()


class RelayRuntime:
    def __init__(self, kimi_home):
        self._home = Path(os.path.realpath(os.path.abspath(str(kimi_home))))
        self._root = self._home / 'usage-dashboard' / 'runtime' / 'frp' / RELAY_VERSION
        package = Path(os.path.realpath(str(_PACKAGED_ROOT)))
        runtime = Path(os.path.realpath(str(self._root)))
        if runtime == package or package in runtime.parents:
            raise ValueError('RELAY_CONFIG_INVALID')
        self._exe = self._root / 'frpc.exe'
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
        self._session = None

    def _check_directories(self, create=False):
        directories = (self._home, self._home / 'usage-dashboard',
                       self._root.parent.parent, self._root.parent, self._root)
        for index, directory in enumerate(directories):
            if create:
                directory.mkdir(parents=index == 0, exist_ok=True)
            _check_directory(directory)
        if create:
            _set_protected_dacl(str(self._root), True)

    def _verified(self, full=False):
        try:
            self._check_directories()
            signature = (_safe_metadata(self._exe), _safe_metadata(self._license))
            if signature[0][2] != CONNECTOR_SIZE or signature[1][2] != LICENSE_SIZE:
                self._cache = None
                return False
            if not full and signature == self._cache:
                return True
            if (not _verified_file(self._exe, CONNECTOR_SIZE, CONNECTOR_SHA256)
                    or not _verified_file(self._license, LICENSE_SIZE, LICENSE_SHA256)
                    or signature != (_safe_metadata(self._exe), _safe_metadata(self._license))):
                self._cache = None
                return False
            self._cache = signature
            return True
        except OSError:
            self._cache = None
            return False

    def _snapshot(self):
        connector = {'state': self._connector_state, 'version': RELAY_VERSION}
        tunnel = {'state': self._tunnel_state}
        if self._connector_error:
            connector['error_code'] = self._connector_error
        if self._tunnel_error:
            tunnel['error_code'] = self._tunnel_error
        result = {'connector': connector, 'tunnel': tunnel}
        if (self._tunnel_state == 'ready' and self._public_origin and self._process
                and self._process.is_alive()):
            result['public_origin'] = self._public_origin
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
            elif (self._tunnel_state in ('starting', 'ready')
                    or self._start_thread is not None and self._start_thread.is_alive()
                    or self._install_thread is not None and self._install_thread.is_alive()):
                error = 'CONNECTOR_BUSY'
            if error:
                result = self._snapshot()
                result['connector']['error_code'] = error
                return result
            if self._verified(full=True):
                self._connector_state = 'installed'
                self._connector_error = None
                return self._snapshot()
            self._install_generation += 1
            generation = self._install_generation
            self._connector_state = 'installing'
            self._connector_error = None
            self._cache = None
            result = self._snapshot()
            self._install_thread = threading.Thread(target=self._install_worker,
                args=(generation,), daemon=True)
            self._install_thread.start()
            return result

    def _install_cancelled(self, generation):
        with self._lock:
            return self._closed or generation != self._install_generation

    def _install_expired(self, generation):
        with self._lock:
            if not self._install_cancelled(generation) and self._connector_state == 'installing':
                self._install_generation += 1
                self._connector_state = 'failed'
                self._connector_error = 'CONNECTOR_INSTALL_FAILED'

    def _install_worker(self, generation):
        paths = []
        deadline = time.monotonic() + INSTALL_TIMEOUT
        timer = threading.Timer(INSTALL_TIMEOUT, self._install_expired, args=(generation,))
        timer.daemon = True
        timer.start()
        code = 'CONNECTOR_INSTALL_FAILED'
        try:
            if self._install_cancelled(generation):
                return
            try:
                license_data = _read_packaged_license()
            except ValueError:
                code = 'CONNECTOR_HASH_MISMATCH'
                raise
            with self._lock:
                if self._install_cancelled(generation):
                    return
                self._check_directories(create=True)
                suffix = secrets.token_hex(12)
                archive = self._root / ('archive-' + suffix + '.part')
                exe = self._root / ('exe-' + suffix + '.part')
                license_path = self._root / ('license-' + suffix + '.part')
                paths = [archive, exe, license_path]
            _download(archive, deadline, lambda: self._install_cancelled(generation))
            if self._install_cancelled(generation):
                return
            try:
                _extract_fixed(archive, exe, license_path, license_data)
            except (ValueError, zipfile.BadZipFile):
                code = 'CONNECTOR_HASH_MISMATCH'
                raise
            with self._lock:
                if self._install_cancelled(generation):
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError('install deadline')
                self._check_directories()
                for target in (self._exe, self._license):
                    try:
                        _safe_metadata(target)
                    except FileNotFoundError:
                        pass
                os.replace(str(license_path), str(self._license))
                os.replace(str(exe), str(self._exe))
                if not self._verified(full=True):
                    code = 'CONNECTOR_HASH_MISMATCH'
                    raise ValueError('install integrity check')
                self._connector_state = 'installed'
                self._connector_error = None
        except Exception:
            with self._lock:
                if not self._install_cancelled(generation):
                    self._connector_state = 'failed'
                    self._connector_error = code
                    self._cache = None
        finally:
            timer.cancel()
            for path in paths:
                try:
                    _safe_metadata(path)
                    path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _callback(callback, value):
        try:
            callback(value)
        except Exception:
            pass

    def start(self, loopback_port, config, on_ready, on_failure):
        try:
            valid = validate_config(config)
        except ValueError:
            threading.Thread(target=self._callback,
                args=(on_failure, 'RELAY_CONFIG_INVALID'), daemon=True).start()
            return
        with self._lock:
            error = None
            if self._closed:
                error = 'START_CANCELLED'
            elif not _supported_windows():
                error = 'CONNECTOR_UNSUPPORTED'
            elif type(loopback_port) is not int or not 1 <= loopback_port <= 65535:
                error = 'TUNNEL_START_FAILED'
            elif (self._tunnel_state in ('starting', 'ready')
                    or self._connector_state == 'installing'
                    or self._start_thread is not None and self._start_thread.is_alive()):
                error = 'CONNECTOR_BUSY'
            if error:
                threading.Thread(target=self._callback, args=(on_failure, error), daemon=True).start()
                return
            self._generation += 1
            generation = self._generation
            self._tunnel_state = 'starting'
            self._tunnel_error = None
            self._public_origin = None
            self._start_thread = threading.Thread(target=self._start_worker,
                args=(generation, loopback_port, valid, on_ready, on_failure), daemon=True)
            self._start_thread.start()

    def _fail(self, generation, code, on_failure):
        with self._lock:
            if (self._closed or generation != self._generation
                    or self._tunnel_state in ('failed', 'off')):
                return
            self._tunnel_state = 'failed'
            self._tunnel_error = code
            self._public_origin = None
            process, self._process = self._process, None
            directory, self._session = self._session, None
            if process is not None:
                process.close()
            try:
                _cleanup_session(directory)
            except OSError:
                pass
        self._callback(on_failure, code)

    def _start_worker(self, generation, port, config, on_ready, on_failure):
        process = directory = signals = None
        failure_code = 'TUNNEL_START_FAILED'
        deadline = time.monotonic() + STARTUP_TIMEOUT
        try:
            with self._lock:
                if self._closed or generation != self._generation:
                    return
                if not self._verified(full=True):
                    self._connector_state = 'missing'
                    failure_code = 'CONNECTOR_MISSING'
                    raise OSError('connector missing')
                directory = Path(tempfile.mkdtemp(prefix='session-', dir=str(self._root)))
                self._session = directory
                _check_directory(directory)
                _set_protected_dacl(str(directory), True)
                proxy_name = 'kimi-mobile-' + secrets.token_hex(12)
                ca_path = directory / 'ca.pem'
                config_path = directory / 'frpc.toml'
                _write_private(ca_path, config['ca_cert'].encode('ascii'))
                _write_private(config_path, _config_bytes(config, port, ca_path, proxy_name))
                origin = 'http://{}:{}'.format(config['server_ip'], config['remote_port'])
                config.clear()
                if not self._verified(full=True):
                    raise OSError('connector changed')
                if time.monotonic() >= deadline:
                    raise TimeoutError('startup deadline')
                process = _WindowsJobProcess([str(self._exe), '-c', str(config_path)],
                    str(directory), _private_environment(directory))
                self._process = process
                self._connector_state = 'installed'
                self._connector_error = None
            signals = _LogSignals(proxy_name)
            ready = False
            while True:
                with self._lock:
                    if self._closed or generation != self._generation:
                        return
                if not ready and time.monotonic() >= deadline:
                    raise TimeoutError('startup deadline')
                for _ in range(LOG_BLOCKS_PER_POLL):
                    chunk = process.read_chunk()
                    if chunk is None:
                        self._fail(generation, 'TUNNEL_EXITED', on_failure)
                        return
                    if not chunk:
                        break
                    signals.feed(chunk)
                    if signals.failure:
                        self._fail(generation, signals.failure, on_failure)
                        return
                if not process.is_alive():
                    self._fail(generation, 'TUNNEL_EXITED', on_failure)
                    return
                if not ready and signals.logged_in and signals.registered:
                    with self._lock:
                        if self._closed or generation != self._generation:
                            return
                        if not process.is_alive():
                            failure_code = 'TUNNEL_EXITED'
                            raise OSError('connector exited')
                        if time.monotonic() >= deadline:
                            raise TimeoutError('startup deadline')
                        ready = True
                        self._tunnel_state = 'ready'
                        self._public_origin = origin
                    self._callback(on_ready, origin)
                time.sleep(0.03)
        except TimeoutError:
            self._fail(generation, 'TUNNEL_TIMEOUT', on_failure)
        except Exception:
            self._fail(generation, failure_code, on_failure)
        finally:
            config.clear()
            if signals is not None:
                signals.clear()
            if process is not None:
                process.close()
            try:
                _cleanup_session(directory)
            except OSError:
                pass
            with self._lock:
                if self._process is process:
                    self._process = None
                if self._session == directory:
                    self._session = None

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
            process, self._process = self._process, None
            directory, self._session = self._session, None
            if process is not None:
                process.close()
            try:
                _cleanup_session(directory)
            except OSError:
                pass
            thread = self._start_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.stop()
