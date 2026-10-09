import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mobile_relay as relay


CA_CERT = '''-----BEGIN CERTIFICATE-----
MIIEgDCCAuigAwIBAgIJAMstgJlaaVJbMA0GCSqGSIb3DQEBCwUAME0xCzAJBgNV
BAYTAlhZMSYwJAYDVQQKDB1QeXRob24gU29mdHdhcmUgRm91bmRhdGlvbiBDQTEW
MBQGA1UEAwwNb3VyLWNhLXNlcnZlcjAeFw0xODA4MjkxNDIzMTZaFw0zNzEwMjgx
NDIzMTZaME0xCzAJBgNVBAYTAlhZMSYwJAYDVQQKDB1QeXRob24gU29mdHdhcmUg
Rm91bmRhdGlvbiBDQTEWMBQGA1UEAwwNb3VyLWNhLXNlcnZlcjCCAaIwDQYJKoZI
hvcNAQEBBQADggGPADCCAYoCggGBANCgm7G5O3nuMS+4URwBde0JWUysyL9qCvh6
CPAl4yV7avjE2KqgYAclsM9zcQVSaL8Gk64QYZa8s2mBGn0Z/CCGj5poG+3N4mxh
Z8dOVepDBiEb6bm+hF/C2uuJiOBCpkVJKtC5a4yTyUQ7yvw8lH/dcMWt2Es73B74
VUu1J4b437CDz/cWN78TFzTUyVXtaxbJf60gTvAe2Ru/jbrNypbvHmnLUWZhSA3o
eaNZYdQQjeANOwuFttWFEt2lB8VL+iP6VDn3lwvJREceVnc8PBMBC2131hS6RPRT
NVbZPbk+NV/bM5pPWrk4RMkySf5m9h8al6rKTEr2uF5Af/sLHfhbodz4wC7QbUn1
0kbUkFf+koE0ri04u6gXDOHlP+L3JgVUUPVksxxuRP9vqbQDlukOwojYclKQmcZB
D0aQWbg+b9Linh02gpXTWIoS8+LYDSBRI/CQLZo+fSaGsqfX+ShgA+N3x4gEyf6J
d3AQT8Ogijv0q0J74xSS2K4W1qHefQIDAQABo2MwYTAdBgNVHQ4EFgQU8+yUjvKO
MMSOaMK/jmoZwMGfdmUwHwYDVR0jBBgwFoAU8+yUjvKOMMSOaMK/jmoZwMGfdmUw
DwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMCAYYwDQYJKoZIhvcNAQELBQAD
ggGBAIsAVHKzjevzrzSf1mDq3oQ/jASPGaa+AmfEY8V040c3WYOUBvFFGegHL9ZO
S0+oPccHByeS9H5zT4syGZRGeiXE2cQnsBFjOmCLheFzTzQ7a6Q0jEmOzc9PsmUn
QRmw/IAxePJzapt9cTRQ/Hio2gW0nFs6mXprXe870+k7MwESZc9eB9gZr9VT6vAQ
rMS2Jjw0LnTuZN0dNnWJRACwDf0vswHMGosCzWzogILKv4LXAJ3YNhXSBzf8bHMd
2qgc6CCOMnr+bScW5Fhs6z7w/iRSKXG4lntTS0UgVUBehhvsyUaRku6sk2WRLpS2
tqzoozSJpBoSDU1EpVLti5HuL6avpJUl+c7HW6cA05PKtDxdTfexPMxttEW+gu0Y
kMiG0XVRUARM6E/S1lCqdede/6F7Jxkca0ksbE1rY8w7cwDzmSbQgofTqTactD25
SGiokvAnjgzNFXZChIDJP6N+tN3X+Kx2umCXPFofTt5x7gk5EN0x1WhXXRrlQroO
aOZF0w==
-----END CERTIFICATE-----
'''


def config():
    return {'server_ip': '8.8.8.8', 'server_port': 7000, 'remote_port': 6000,
            'token': 'relay-test-token-' + 'x' * 32, 'ca_cert': CA_CERT}


def login():
    return (b'2026-10-09 12:00:00.000 [I] [client/service.go:296] [run-id] '
            b'login to server success, get run id [run-id]\n')


def registered(name):
    return ('2026-10-09 12:00:00.000 [I] [client/control.go:170] [run-id] '
            '[%s] start proxy success\n' % name).encode()


class ConfigTests(unittest.TestCase):
    def test_valid_normalization_and_origin(self):
        original = config()
        original['ca_cert'] = ' \n' + CA_CERT.replace('\n', '\r\n') + '\n '
        normalized = relay.validate_config(original)
        self.assertEqual(normalized['ca_cert'], CA_CERT)
        self.assertEqual(relay.public_origin(original), 'http://8.8.8.8:6000')
        self.assertIsNot(normalized, original)
        self.assertNotEqual(original['ca_cert'], CA_CERT)

    def test_exact_fields(self):
        for candidate in (None, [], {}, dict(config(), extra=True)):
            with self.subTest(candidate_type=type(candidate)):
                with self.assertRaisesRegex(ValueError, '^RELAY_CONFIG_INVALID$'):
                    relay.validate_config(candidate)
        for key in config():
            candidate = config()
            del candidate[key]
            with self.subTest(missing=key):
                with self.assertRaisesRegex(ValueError, '^RELAY_CONFIG_INVALID$'):
                    relay.validate_config(candidate)

    def test_public_canonical_ipv4_only(self):
        for address in ('localhost', 'example.com', '::1', '8.8.8.8:7000',
                        ' 8.8.8.8', '008.008.008.008', '0.0.0.0', '10.0.0.1',
                        '100.64.0.1', '127.0.0.1', '169.254.1.1', '172.16.0.1',
                        '192.168.1.1', '192.0.0.9', '192.0.2.1', '192.88.99.1',
                        '198.18.0.1', '198.51.100.1', '203.0.113.1',
                        '224.0.0.1', '240.0.0.1', '255.255.255.255', 134744072):
            with self.subTest(address=address):
                with self.assertRaisesRegex(ValueError, '^RELAY_CONFIG_INVALID$'):
                    relay.validate_config(dict(config(), server_ip=address))

    def test_ports_and_token(self):
        for key in ('server_port', 'remote_port'):
            for value in (True, False, 0, -1, 65536, 7000.0, '7000', None):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, '^RELAY_CONFIG_INVALID$'):
                        relay.validate_config(dict(config(), **{key: value}))
        with self.assertRaises(ValueError):
            relay.validate_config(dict(config(), remote_port=7000))
        for token in ('', 'x' * 31, 'x' * 513, 'x' * 32 + '\n', 'x' * 32 + ' ',
                      'x' * 32 + '\x00', 'x' * 32 + '\x7f', 'x' * 32 + '中', None):
            with self.subTest(token_length=len(token) if token else 0):
                with self.assertRaisesRegex(ValueError, '^RELAY_CONFIG_INVALID$'):
                    relay.validate_config(dict(config(), token=token))
        self.assertEqual(relay.validate_config(dict(config(), token='x' * 512))['token'],
                         'x' * 512)

    def test_ca_rejects_extra_private_key_malformed_duplicates_and_oversize(self):
        for ca in ('', 'not-pem', CA_CERT + 'extra',
                   CA_CERT.replace('MIIEgD', '!!!!!!'), CA_CERT * 2,
                   CA_CERT + '\n-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----',
                   'x' * (relay.CA_MAX_BYTES + 1), None):
            with self.subTest(ca_length=len(ca) if ca else 0):
                with self.assertRaisesRegex(ValueError, '^RELAY_CONFIG_INVALID$'):
                    relay.validate_config(dict(config(), ca_cert=ca))

    def test_non_ca_certificate_is_rejected(self):
        with mock.patch.object(relay.ssl, 'SSLContext') as context:
            context.return_value.cert_store_stats.return_value = {'x509': 1, 'x509_ca': 0}
            with self.assertRaisesRegex(ValueError, '^RELAY_CONFIG_INVALID$'):
                relay.validate_config(config())

    def test_tls_and_tcp_are_not_user_overridable(self):
        value = config()
        value['token'] = 'x' * 32 + '\\"'
        text = relay._config_bytes(relay.validate_config(value), 12345,
                                   Path('C:/runtime/ca.pem'), 'kimi-mobile-test').decode()
        for line in ('serverAddr = "8.8.8.8"', 'transport.tls.serverName = "8.8.8.8"',
                     'transport.tls.enable = true', 'transport.protocol = "tcp"',
                     'transport.tcpMux = false', 'localIP = "127.0.0.1"',
                     'localPort = 12345', 'type = "tcp"', 'loginFailExit = true',
                     'log.to = "console"', 'log.disablePrintColor = true',
                     'transport.heartbeatTimeout = 15', 'webServer.port = 0'):
            self.assertIn(line, text)
        self.assertNotIn('insecureSkipVerify', text)
        self.assertNotIn('includes', text)
        self.assertNotIn('healthCheck', text)
        self.assertIn('auth.token = ' + json.dumps(value['token']), text)


class LogTests(unittest.TestCase):
    def test_only_login_and_exact_proxy_success_count(self):
        signals = relay._LogSignals('mine')
        signals.feed(b'frpc started\n' + registered('other'))
        self.assertFalse(signals.logged_in)
        self.assertFalse(signals.registered)
        raw = login() + registered('mine')
        for index in range(0, len(raw), 7):
            signals.feed(raw[index:index + 7])
        self.assertTrue(signals.logged_in)
        self.assertTrue(signals.registered)
        self.assertIsNone(signals.failure)

    def test_registration_before_login_is_not_ready(self):
        signals = relay._LogSignals('mine')
        signals.feed(registered('mine') + login())
        self.assertFalse(signals.registered)
        signals.feed(registered('mine'))
        self.assertTrue(signals.registered)

    def test_registration_failure_disconnect_and_relogin_are_terminal(self):
        cases = (
            (b'[W] [client/control.go:168] [id] [mine] start error: port taken\n',
             'TUNNEL_START_FAILED'),
            (b'[W] [client/control.go:273] [id] heartbeat timeout\n', 'TUNNEL_TIMEOUT'),
            (b'[E] [client/control.go:190] [id] pong message contains error: auth\n',
             'TUNNEL_EXITED'),
            (b'[I] [client/service.go:310] [id] try to connect to server...\n',
             'TUNNEL_EXITED'),
            (b'[W] [client/connector.go:100] session closed\n', 'TUNNEL_EXITED'),
            (login(), 'TUNNEL_EXITED'))
        for line, code in cases:
            with self.subTest(code=code):
                signals = relay._LogSignals('mine')
                signals.feed(login() + registered('mine') + line)
                self.assertEqual(signals.failure, code)
                signals.feed(registered('mine'))
                self.assertEqual(signals.failure, code)

    def test_failed_first_login(self):
        signals = relay._LogSignals('mine')
        signals.feed(b'[W] [client/service.go:310] connect to server error: secret\n')
        self.assertEqual(signals.failure, 'TUNNEL_START_FAILED')
        self.assertFalse(signals.registered)
        self.assertNotIn('secret', repr(vars(signals)))

    def test_bounded_partial_and_chunks_fail_closed(self):
        signals = relay._LogSignals('mine')
        signals.feed(b'x' * relay.LOG_LINE_MAX_BYTES)
        signals.feed(b'x')
        self.assertEqual(signals.failure, 'TUNNEL_START_FAILED')
        self.assertEqual(len(signals._partial), 0)
        oversized = relay._LogSignals('mine')
        oversized.feed(b'x' * (relay.LOG_CHUNK_MAX_BYTES + 1))
        self.assertEqual(oversized.failure, 'TUNNEL_START_FAILED')
        self.assertEqual(len(oversized._partial), 0)


class FakeProcess:
    def __init__(self):
        self.chunks = queue.Queue()
        self.alive = True
        self.closed = False

    def read_chunk(self):
        try:
            return self.chunks.get_nowait()
        except queue.Empty:
            return b''

    def is_alive(self):
        return self.alive

    def close(self):
        self.closed = True
        self.alive = False


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = relay.RelayRuntime(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(relay, '_supported_windows', return_value=True))
        self.verified = self.stack.enter_context(mock.patch.object(
            self.runtime, '_verified', return_value=True))
        self.runtime._check_directories(create=True)
        self.process = FakeProcess()
        self.spawn = self.stack.enter_context(mock.patch.object(
            relay, '_WindowsJobProcess', side_effect=self.create_process))
        self.ready = threading.Event()
        self.failed = threading.Event()
        self.spawned = threading.Event()
        self.origins = []
        self.errors = []
        self.proxy_name = None
        self.auto_register = True
        self.argv = self.env = self.session = None
        self.addCleanup(self.runtime.shutdown)

    def create_process(self, argv, cwd, env):
        self.argv, self.env, self.session = argv, env, Path(cwd)
        text = (self.session / 'frpc.toml').read_text()
        self.proxy_name = re.search(r'^name = "([^"]+)"$', text, re.MULTILINE).group(1)
        if self.auto_register:
            self.process.chunks.put(login() + registered(self.proxy_name))
        self.spawned.set()
        return self.process

    def on_ready(self, origin):
        self.origins.append(origin)
        self.ready.set()

    def on_failure(self, code):
        self.errors.append(code)
        self.failed.set()

    def start(self):
        self.runtime.start(12345, config(), self.on_ready, self.on_failure)

    def wait_ready(self):
        self.start()
        self.assertTrue(self.ready.wait(3))
        self.assertFalse(self.failed.is_set())

    def assert_no_sessions(self):
        self.assertEqual(list(self.runtime._root.glob('session-*')), [])

    def test_secret_only_in_private_files_not_status_argv_or_env(self):
        self.wait_ready()
        state = self.runtime.status()
        self.assertEqual(state['tunnel']['state'], 'ready')
        self.assertEqual(state['public_origin'], 'http://8.8.8.8:6000')
        secret = config()['token']
        self.assertIn(secret, (self.session / 'frpc.toml').read_text())
        for public in (json.dumps(state), json.dumps(self.argv), json.dumps(self.env)):
            self.assertNotIn(secret, public)
            self.assertNotIn(CA_CERT, public)
        self.assertEqual(self.argv[1:], ['-c', str(self.session / 'frpc.toml')])
        self.assertEqual((self.session / 'ca.pem').read_text(), CA_CERT)
        self.runtime.stop()
        self.assertTrue(self.process.closed)
        self.assertEqual(self.runtime.status()['tunnel']['state'], 'off')
        self.assertNotIn('public_origin', self.runtime.status())
        self.assert_no_sessions()

    def test_live_process_alone_is_not_ready(self):
        self.auto_register = False
        self.start()
        self.assertTrue(self.spawned.wait(3))
        self.assertFalse(self.ready.wait(.1))
        self.assertEqual(self.runtime.status()['tunnel']['state'], 'starting')
        self.process.chunks.put(login())
        self.assertTrue(self.spawned.wait(3))
        self.assertFalse(self.ready.wait(.1))
        self.process.chunks.put(registered(self.proxy_name))
        self.assertTrue(self.ready.wait(3))

    def test_disconnect_immediately_revokes_without_reconnect(self):
        self.wait_ready()
        self.process.chunks.put(b'[I] [client/service.go:310] try to connect to server...\n')
        self.assertTrue(self.failed.wait(3))
        self.runtime._start_thread.join(3)
        self.assertEqual(self.errors, ['TUNNEL_EXITED'])
        self.assertEqual(len(self.origins), 1)
        self.assertTrue(self.process.closed)
        self.assertNotIn('public_origin', self.runtime.status())
        self.assertEqual(self.spawn.call_count, 1)
        self.assert_no_sessions()

    def test_registration_failure_does_not_call_ready(self):
        self.auto_register = False
        self.start()
        self.assertTrue(self.spawned.wait(3))
        self.assertFalse(self.ready.wait(.1))
        self.process.chunks.put(login() + ('[W] [client/control.go:168] [id] [%s] '
            'start error: secret-message\n' % self.proxy_name).encode())
        self.assertTrue(self.failed.wait(3))
        self.runtime._start_thread.join(3)
        self.assertEqual(self.errors, ['TUNNEL_START_FAILED'])
        self.assertEqual(self.origins, [])
        self.assertNotIn('secret-message', json.dumps(self.runtime.status()))
        self.assert_no_sessions()

    def test_dead_child_never_advertises_origin(self):
        self.process.alive = False
        self.start()
        self.assertTrue(self.failed.wait(3))
        self.assertEqual(self.errors, ['TUNNEL_EXITED'])
        self.assertFalse(self.ready.is_set())
        self.assertNotIn('public_origin', self.runtime.status())

    def test_heartbeat_timeout_removes_origin(self):
        self.wait_ready()
        self.process.chunks.put(b'[W] [client/control.go:273] heartbeat timeout\n')
        self.assertTrue(self.failed.wait(3))
        self.assertEqual(self.errors, ['TUNNEL_TIMEOUT'])
        self.assertNotIn('public_origin', self.runtime.status())

    def test_timeout_and_missing_are_precise(self):
        self.auto_register = False
        self.runtime._generation = 1
        self.runtime._tunnel_state = 'starting'
        with mock.patch.object(relay.time, 'monotonic', side_effect=[0, 0, 46]):
            self.runtime._start_worker(1, 12345, config(), self.on_ready, self.on_failure)
        self.assertEqual(self.errors, ['TUNNEL_TIMEOUT'])
        self.assert_no_sessions()
        self.errors.clear()
        self.runtime._tunnel_state = 'starting'
        self.verified.return_value = False
        self.runtime._start_worker(1, 12345, config(), self.on_ready, self.on_failure)
        self.assertEqual(self.errors, ['CONNECTOR_MISSING'])

    def test_acl_failure_happens_before_secret_write(self):
        seen = []
        def fail(path, is_dir=False):
            path = Path(path)
            if not is_dir:
                self.assertEqual(path.stat().st_size, 0)
                seen.append(path)
                raise PermissionError('do-not-report-this')
        with mock.patch.object(relay, '_set_protected_dacl', side_effect=fail):
            self.start()
            self.assertTrue(self.failed.wait(3))
            self.runtime._start_thread.join(3)
        self.assertTrue(seen)
        self.spawn.assert_not_called()
        self.assertEqual(self.errors, ['TUNNEL_START_FAILED'])
        self.assert_no_sessions()

    def test_stop_invalidates_generation_and_late_ready(self):
        self.auto_register = False
        self.start()
        self.assertTrue(self.spawned.wait(3))
        self.assertFalse(self.ready.wait(.1))
        generation = self.runtime._generation
        self.runtime.stop()
        self.process.chunks.put(login() + registered(self.proxy_name))
        self.runtime._fail(generation, 'TUNNEL_EXITED', self.on_failure)
        self.assertFalse(self.failed.is_set())
        self.assertFalse(self.ready.is_set())
        self.assert_no_sessions()
        self.runtime.shutdown()
        self.start()
        self.assertTrue(self.failed.wait(3))
        self.assertEqual(self.errors, ['START_CANCELLED'])
        self.assertEqual(self.spawn.call_count, 1)

    def test_busy_and_invalid_inputs_do_not_spawn(self):
        self.wait_ready()
        self.start()
        self.assertTrue(self.failed.wait(3))
        self.assertEqual(self.errors, ['CONNECTOR_BUSY'])
        self.assertEqual(self.spawn.call_count, 1)
        self.runtime.stop()
        self.failed.clear()
        self.runtime.start(True, config(), self.on_ready, self.on_failure)
        self.assertTrue(self.failed.wait(3))
        self.assertEqual(self.errors[-1], 'TUNNEL_START_FAILED')
        self.failed.clear()
        self.runtime.start(12345, {}, self.on_ready, self.on_failure)
        self.assertTrue(self.failed.wait(3))
        self.assertEqual(self.errors[-1], 'RELAY_CONFIG_INVALID')
        self.assertEqual(self.spawn.call_count, 1)

    def test_callback_can_stop_its_own_session(self):
        def ready(origin):
            self.runtime.stop()
            self.on_ready(origin)
        self.runtime.start(12345, config(), ready, self.on_failure)
        self.assertTrue(self.ready.wait(3))
        self.runtime._start_thread.join(3)
        self.assertEqual(self.runtime.status()['tunnel']['state'], 'off')
        self.assert_no_sessions()

    def test_consent_is_exact_and_unsupported_is_controlled(self):
        for consent, version in ((1, relay.CONSENT_VERSION),
                                 (True, 'old'), (False, relay.CONSENT_VERSION)):
            self.assertEqual(self.runtime.install(consent, version)['connector']['error_code'],
                             'CONSENT_REQUIRED')
        with mock.patch.object(relay, '_supported_windows', return_value=False):
            result = self.runtime.install(True, relay.CONSENT_VERSION)
            self.assertEqual(result['connector']['error_code'], 'CONNECTOR_UNSUPPORTED')


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = relay.RelayRuntime(self.temp.name)
        self.addCleanup(self.runtime.shutdown)
        self.runtime._check_directories(create=True)
        self.exe = b'fixed-test-executable-never-run'
        self.license = b'fixed-test-license\n'
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (('CONNECTOR_SIZE', len(self.exe)),
                            ('CONNECTOR_SHA256', hashlib.sha256(self.exe).hexdigest()),
                            ('LICENSE_SIZE', len(self.license)),
                            ('LICENSE_SHA256', hashlib.sha256(self.license).hexdigest())):
            self.stack.enter_context(mock.patch.object(relay, name, value))
        self.stack.enter_context(mock.patch.object(relay, '_supported_windows', return_value=True))
        self.stack.enter_context(mock.patch.object(relay, '_read_packaged_license',
                                                   return_value=self.license))
        self.archive = self.make_archive()
        self.pin_archive(self.archive)
        self.download = self.stack.enter_context(mock.patch.object(relay, '_download',
                                                                  side_effect=self.write_download))

    def make_archive(self, duplicate=False, traversal=False, symlink=False):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            if symlink:
                info = zipfile.ZipInfo(relay.EXE_MEMBER)
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                archive.writestr(info, self.exe)
            else:
                archive.writestr(relay.EXE_MEMBER, self.exe)
            archive.writestr(relay.LICENSE_MEMBER, self.license)
            archive.writestr('frp_0.67.0_windows_amd64/frps.exe', b'ignored')
            if duplicate:
                archive.writestr(relay.EXE_MEMBER, self.exe)
            if traversal:
                archive.writestr('../../should-not-exist', b'ignored')
        return data.getvalue()

    def pin_archive(self, value):
        self.stack.enter_context(mock.patch.object(relay, 'ARCHIVE_SIZE', len(value)))
        self.stack.enter_context(mock.patch.object(relay, 'ARCHIVE_SHA256',
                                                   hashlib.sha256(value).hexdigest()))

    def write_download(self, destination, deadline, cancelled):
        self.assertFalse(cancelled())
        with destination.open('xb') as stream:
            stream.write(self.archive)

    def install_worker(self):
        self.runtime._install_generation = 1
        self.runtime._connector_state = 'installing'
        self.runtime._install_worker(1)

    def assert_clean(self):
        self.assertEqual(list(self.runtime._root.glob('*.part')), [])

    def test_fixed_members_atomic_install_and_full_start_verification(self):
        self.archive = self.make_archive(traversal=True)
        self.pin_archive(self.archive)
        self.install_worker()
        self.assertEqual(self.runtime.status()['connector']['state'], 'installed')
        self.assertEqual(self.runtime._exe.read_bytes(), self.exe)
        self.assertEqual(self.runtime._license.read_bytes(), self.license)
        self.assertEqual(sorted(path.name for path in self.runtime._root.iterdir()),
                         ['LICENSE', 'frpc.exe'])
        self.assertFalse((Path(self.temp.name) / 'should-not-exist').exists())
        self.assert_clean()
        self.runtime._exe.write_bytes(b'x' * len(self.exe))
        self.assertFalse(self.runtime._verified(full=True))
        self.assertEqual(self.runtime.status()['connector']['state'], 'missing')

    def test_archive_digest_mismatch_and_bad_members_do_not_install(self):
        self.archive = b'not-a-zip'
        self.install_worker()
        self.assertEqual(self.runtime.status()['connector']['error_code'],
                         'CONNECTOR_HASH_MISMATCH')
        self.assertFalse(self.runtime._exe.exists())
        self.assert_clean()
        for kwargs in ({'duplicate': True}, {'symlink': True}):
            with self.subTest(kwargs=kwargs):
                self.archive = self.make_archive(**kwargs)
                self.pin_archive(self.archive)
                self.install_worker()
                self.assertEqual(self.runtime.status()['connector']['error_code'],
                                 'CONNECTOR_HASH_MISMATCH')
                self.assertFalse(self.runtime._exe.exists())
                self.assert_clean()

    def test_exe_hash_mismatch_no_publish(self):
        with mock.patch.object(relay, 'CONNECTOR_SHA256', '0' * 64):
            self.install_worker()
        self.assertFalse(self.runtime._exe.exists())
        self.assertEqual(self.runtime.status()['connector']['error_code'],
                         'CONNECTOR_HASH_MISMATCH')
        self.assert_clean()

    def test_cancel_install_never_publishes(self):
        def cancelled_download(destination, deadline, cancelled):
            self.write_download(destination, deadline, cancelled)
            self.runtime.stop()
        self.download.side_effect = cancelled_download
        self.install_worker()
        self.assertFalse(self.runtime._exe.exists())
        self.assertEqual(self.runtime.status()['connector']['error_code'], 'START_CANCELLED')
        self.assert_clean()

    def test_async_install_and_idempotent_reuse(self):
        result = self.runtime.install(True, relay.CONSENT_VERSION)
        self.assertEqual(result['connector']['state'], 'installing')
        self.runtime._install_thread.join(3)
        self.assertFalse(self.runtime._install_thread.is_alive())
        self.assertEqual(self.runtime.status()['connector']['state'], 'installed')
        again = self.runtime.install(True, relay.CONSENT_VERSION)
        self.assertEqual(again['connector']['state'], 'installed')
        self.assertEqual(self.download.call_count, 1)
        self.assert_clean()

    def test_acl_failure_before_download(self):
        with mock.patch.object(relay, '_set_protected_dacl', side_effect=PermissionError):
            self.install_worker()
        self.download.assert_not_called()
        self.assertEqual(self.runtime.status()['connector']['error_code'],
                         'CONNECTOR_INSTALL_FAILED')
        self.assert_clean()

    def test_symlink_and_hardlink_files_rejected(self):
        source = Path(self.temp.name) / 'source'
        source.write_bytes(self.exe)
        try:
            self.runtime._exe.symlink_to(source)
        except OSError:
            pass
        else:
            with self.assertRaises(OSError):
                relay._safe_metadata(self.runtime._exe)
            self.runtime._exe.unlink()
        try:
            os.link(str(source), str(self.runtime._exe))
        except OSError:
            self.skipTest('Hard links unavailable')
        with self.assertRaises(OSError):
            relay._safe_metadata(self.runtime._exe)
        self.assertFalse(self.runtime._verified(full=True))


class PackageTests(unittest.TestCase):
    def test_pinned_packaged_license_bytes(self):
        data = relay._read_packaged_license()
        self.assertEqual(len(data), 11358)
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         'c6596eb7be8581c18be736c846fb9173b69eccf6ef94c5135893ec56bd92ba08')

    def test_download_whitelist(self):
        self.assertTrue(relay._allowed_download_url(relay.ARCHIVE_URL))
        self.assertTrue(relay._allowed_download_url(
            'https://release-assets.githubusercontent.com/path?signature=fixed'))
        for url in ('http://github.com/fatedier/frp/file',
                    relay.ARCHIVE_URL + '?unexpected=1',
                    relay.ARCHIVE_URL + '#fragment',
                    relay.ARCHIVE_URL.replace('v0.67.0', 'v0.68.0'),
                    'https://github.com/other/project/releases/download/file',
                    'https://release-assets.githubusercontent.com.attacker.test/file',
                    'https://user:secret@release-assets.githubusercontent.com/file',
                    'https://release-assets.githubusercontent.com:444/file'):
            self.assertFalse(relay._allowed_download_url(url), url)
        redirects = relay._OfficialRedirects(float('inf'))
        with self.assertRaises(relay.urllib.error.URLError):
            redirects.redirect_request(None, None, 302, '', {}, 'https://attacker.test/file')

    def test_packaged_root_is_realpath_resolved(self):
        source = Path(os.path.abspath(relay.__file__))
        expected = Path(os.path.realpath(str(source))).parent.parent
        self.assertEqual(relay._PACKAGED_ROOT, expected)
        self.assertEqual(Path(os.path.realpath(str(relay._PACKAGED_ROOT))), expected)
        self.assertEqual(Path(os.path.realpath(str(relay._PACKAGED_LICENSE))).parent.parent.parent,
                         expected)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction')
    def test_packaged_license_follows_entry_junction_after_resolution(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            junction = Path(temp) / 'plugin-entry'
            command = 'mklink /J "{}" "{}"'.format(junction, root)
            subprocess.run(command, shell=True, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with mock.patch.object(relay, '_PACKAGED_ROOT', junction):
                    data = relay._read_packaged_license()
                self.assertEqual(hashlib.sha256(data).hexdigest(), relay.LICENSE_SHA256)
                self.assertEqual(Path(os.path.realpath(str(junction))), root)
            finally:
                os.rmdir(junction)

    def test_runtime_is_outside_plugin_tree_and_frps_template_has_no_dashboard(self):
        with self.assertRaisesRegex(ValueError, '^RELAY_CONFIG_INVALID$'):
            relay.RelayRuntime(relay._PACKAGED_ROOT)
        root = Path(__file__).resolve().parents[1]
        text = (root / 'deploy/frp/frps.toml.example').read_text()
        for line in ('bindPort = 7000', 'allowPorts = [{ single = 6000 }]',
                     'transport.tls.force = true', 'transport.tcpMux = false',
                     'webServer.port = 0', 'auth.method = "token"'):
            self.assertIn(line, text)
        for value in ('vhostHTTPPort', 'kcpBindPort', 'quicBindPort', 'sshTunnelGateway'):
            self.assertNotIn(value, text)
        self.assertIn('REPLACE_WITH_A_RANDOM_TOKEN', text)


if __name__ == '__main__':
    unittest.main()
