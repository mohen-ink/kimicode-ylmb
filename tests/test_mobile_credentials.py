import concurrent.futures
import contextlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mobile_credentials as credentials
import mobile_bridge as bridge
import mobile_security as security
import mobile_worker as worker
import updater


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / 'server.token'

    def assert_clean(self):
        self.assertEqual(list(self.home.glob('.server-token-*.tmp')), [])

    def test_create_and_reuse(self):
        token = credentials.ensure_server_token(self.home)
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        before = self.path.stat()
        self.assertEqual(credentials.ensure_server_token(self.home), token)
        after = self.path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))
        self.assert_clean()

    def test_existing_not_changed(self):
        token = credentials.ensure_server_token(self.home)
        with mock.patch.object(credentials.secrets, 'token_urlsafe') as generate:
            self.assertEqual(credentials.ensure_server_token(self.home), token)
        generate.assert_not_called()

    def test_read_missing_does_not_create(self):
        self.assertIsNone(credentials.read_server_token(self.home))
        self.assertFalse(self.path.exists())

    def test_concurrent_creation(self):
        barrier = threading.Barrier(8)
        def create():
            barrier.wait()
            return credentials.ensure_server_token(self.home)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(lambda _: create(), range(8)))
        self.assertEqual(len(set(tokens)), 1)
        self.assert_clean()

    def test_bad_existing_files_not_overwritten(self):
        for raw in (b'', b'\xff', b'x' * 4097, b'bad\r\ntoken', b'hello world'):
            with self.subTest(raw_length=len(raw)):
                self.path.write_bytes(raw)
                os.chmod(self.path, 0o600)
                with self.assertRaisesRegex(credentials.CredentialUnavailable,
                                            '^SERVER_TOKEN_UNAVAILABLE$'):
                    credentials.ensure_server_token(self.home)
                self.assertEqual(self.path.read_bytes(), raw)
        self.assert_clean()

    def test_directory_rejected(self):
        self.path.mkdir()
        with self.assertRaises(credentials.CredentialUnavailable):
            credentials.ensure_server_token(self.home)
        self.assertTrue(self.path.is_dir())

    def test_unreadable_not_recreated(self):
        credentials.ensure_server_token(self.home)
        with mock.patch.object(credentials.os, 'open', side_effect=PermissionError):
            with self.assertRaises(credentials.CredentialUnavailable):
                credentials.ensure_server_token(self.home)
        self.assert_clean()

    def test_acl_failure_never_publishes_secret(self):
        def fail(path, _is_dir):
            self.assertEqual(Path(path).stat().st_size, 0)
            raise PermissionError('sensitive-path')
        with mock.patch.object(credentials, '_set_protected_dacl', side_effect=fail):
            with self.assertRaisesRegex(credentials.CredentialUnavailable,
                                        '^SERVER_TOKEN_UNAVAILABLE$'):
                credentials.ensure_server_token(self.home)
        self.assertFalse(self.path.exists())
        self.assert_clean()

    def test_publish_failure_cleanup(self):
        with mock.patch.object(credentials.os, 'link', side_effect=OSError):
            with self.assertRaises(credentials.CredentialUnavailable):
                credentials.ensure_server_token(self.home)
        self.assertFalse(self.path.exists())
        self.assert_clean()

    def test_symlink_rejected(self):
        target = self.home / 'target'
        target.write_bytes(b'leave-me')
        try:
            self.path.symlink_to(target)
        except OSError as exc:
            self.skipTest('Symlink permission unavailable: ' + str(exc.winerror))
        with self.assertRaises(credentials.CredentialUnavailable):
            credentials.ensure_server_token(self.home)
        self.assertEqual(target.read_bytes(), b'leave-me')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction')
    def test_junction_home(self):
        with tempfile.TemporaryDirectory() as root:
            entry = Path(root) / 'home'
            command = 'mklink /J "{}" "{}"'.format(entry, self.home)
            subprocess.run(command, shell=True, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertEqual(credentials.ensure_server_token(entry),
                                 credentials.read_server_token(self.home))
            finally:
                os.rmdir(entry)

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_protected_acl(self):
        credentials.ensure_server_token(self.home)
        adv = __import__('ctypes').WinDLL('advapi32')
        ctypes = __import__('ctypes')
        ptr = ctypes.c_void_p
        adv.GetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_int,
            ctypes.c_ulong, ptr, ptr, ptr, ptr, ctypes.POINTER(ptr)]
        adv.GetNamedSecurityInfoW.restype = ctypes.c_ulong
        adv.GetSecurityDescriptorControl.argtypes = [ptr,
            ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ulong)]
        adv.GetSecurityDescriptorControl.restype = ctypes.c_int
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ptr, ctypes.c_ulong, ctypes.c_ulong, ctypes.POINTER(ptr), ptr]
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = ctypes.c_int
        kernel = ctypes.WinDLL('kernel32')
        kernel.LocalFree.argtypes = [ptr]
        kernel.LocalFree.restype = ptr
        sd, text = ptr(), ptr()
        try:
            self.assertEqual(adv.GetNamedSecurityInfoW(str(self.path), 1, 4,
                             None, None, None, None, ctypes.byref(sd)), 0)
            control, revision = ctypes.c_ushort(), ctypes.c_ulong()
            self.assertTrue(adv.GetSecurityDescriptorControl(sd,
                            ctypes.byref(control), ctypes.byref(revision)))
            self.assertTrue(control.value & 0x1000)
            self.assertTrue(adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                            sd, 1, 4, ctypes.byref(text), None))
            sddl = ctypes.wstring_at(text)
            expected = 'D:P(A;;FA;;;%s)(A;;FA;;;SY)' % security._current_user_sid()
            other = ptr()
            adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
                ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ptr), ptr]
            adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
            canonical = ptr()
            try:
                self.assertTrue(adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                                expected, 1, ctypes.byref(other), None))
                self.assertTrue(adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                                other, 1, 4, ctypes.byref(canonical), None))
                self.assertEqual(sddl.replace('D:PAI', 'D:P'),
                                 ctypes.wstring_at(canonical))
            finally:
                if canonical.value:
                    kernel.LocalFree(canonical)
                if other.value:
                    kernel.LocalFree(other)
            self.assertIn(';;;SY)', sddl)
            self.assertEqual(sddl.count('(A;'), 2)
        finally:
            if text.value:
                kernel.LocalFree(text)
            if sd.value:
                kernel.LocalFree(sd)


class OwnerServer(ThreadingHTTPServer):
    daemon_threads = True


class OwnerHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def do_GET(self):
        if self.path == '/api/v1/healthz':
            status, body = 200, b'{}'
        else:
            self.server.auth_called.set()
            if self.server.on_auth:
                self.server.on_auth()
            try:
                token = credentials.read_server_token(self.server.home)
            except credentials.CredentialUnavailable:
                token = None
            valid = token and self.headers.get('Authorization') == 'Bearer ' + token
            status = self.server.auth_status if valid else 401
            body = b'[]'
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.owner = OwnerServer(('127.0.0.1', 0), OwnerHandler)
        self.owner.home = self.home
        self.owner.auth_status = 200
        self.owner.auth_called = threading.Event()
        self.owner.on_auth = None
        self.origin = 'http://127.0.0.1:%d' % self.owner.server_port
        threading.Thread(target=self.owner.serve_forever, daemon=True).start()
        self.addCleanup(self.owner.server_close)
        self.addCleanup(self.owner.shutdown)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        inst = {'host': '127.0.0.1', 'port': self.owner.server_port,
                'pid': os.getpid(), 'server_id': 'test-instance'}
        for name, value in (('_load_instances', [inst]), ('_pid_alive', True),
                            ('_pid_is_desktop', True), ('_pid_creation_ticks', 123),
                            ('_tcp_listener_pid', os.getpid()),
                            ('local_lan_addresses', ['192.168.1.2'])):
            self.stack.enter_context(mock.patch.object(bridge, name, return_value=value))
        self.mgr = bridge.MobileBridgeManager(str(self.home))
        self.addCleanup(self.mgr.shutdown)
        self.mgr._connector = None
        def bind(_address):
            server = bridge._LanHTTPServer(('127.0.0.1', 0), bridge._LanHandler, self.mgr)
            return server, server.server_port
        self.bind = self.stack.enter_context(mock.patch.object(self.mgr, '_bind',
                                                              side_effect=bind))

    def start(self):
        return self.mgr.start(self.origin, '192.168.1.2')

    def assert_off(self, code):
        state = self.mgr.status()
        self.assertEqual(state['state'], 'off')
        self.assertEqual(state['tunnel']['error_code'], code)
        self.assertNotIn('url', state)
        self.assertIsNone(self.mgr._server)
        self.bind.assert_not_called()

    def test_first_start_pair_and_real_proxy_call(self):
        status = self.start()
        self.assertEqual(status['state'], 'on')
        self.assertTrue(self.owner.auth_called.is_set())
        token = credentials.read_server_token(self.home)
        self.assertNotIn(token, json.dumps(status))
        pair = status['url'].split('#pair=')[1]
        conn = http.client.HTTPConnection('127.0.0.1', status['port'], timeout=3)
        try:
            host = '192.168.1.2:%d' % status['port']
            conn.request('POST', '/mobile/pair/exchange', json.dumps({'token': pair}),
                         headers={'Host': host, 'Origin': 'http://' + host,
                                  'Content-Type': 'application/json'})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            cookie = response.getheader('Set-Cookie').split(';', 1)[0]
            sid = cookie.split('=', 1)[1]
            self.assertNotIn(token, response.read().decode('utf-8'))
            conn.request('GET', '/api/v1/workspaces', headers={
                'Host': host, 'Cookie': cookie})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'[]')
        finally:
            conn.close()
        self.mgr.stop()
        self.assertFalse(self.mgr.session_valid(sid))
        self.assertEqual(self.start()['state'], 'on')
        self.assertEqual(credentials.read_server_token(self.home), token)

    def test_status_does_not_create(self):
        self.mgr.status()
        self.assertFalse((self.home / 'server.token').exists())

    def test_offline_owner_does_not_create(self):
        with mock.patch.object(bridge, '_pid_alive', return_value=False):
            with self.assertRaisesRegex(bridge.MobileBridgeError, '^OWNER_LOST$'):
                self.start()
        self.assertFalse((self.home / 'server.token').exists())
        self.assert_off('OWNER_LOST')

    def test_auth_rejection_no_listener(self):
        for code in (401, 403):
            with self.subTest(status=code):
                self.owner.auth_status = code
                with self.assertRaisesRegex(bridge.MobileBridgeError, '^OWNER_AUTH_FAILED$'):
                    self.start()
                self.assert_off('OWNER_AUTH_FAILED')

    def test_other_auth_status_no_listener(self):
        for code in (302, 404, 500):
            with self.subTest(status=code):
                self.owner.auth_status = code
                with self.assertRaisesRegex(bridge.MobileBridgeError,
                                            '^OWNER_AUTH_CHECK_FAILED$'):
                    self.start()
                self.assert_off('OWNER_AUTH_CHECK_FAILED')

    def test_auth_timeout_no_listener(self):
        with mock.patch.object(bridge.http.client.HTTPConnection, 'request',
                               side_effect=TimeoutError):
            with self.assertRaisesRegex(bridge.MobileBridgeError, '^OWNER_AUTH_CHECK_FAILED$'):
                bridge._verify_owner_auth('127.0.0.1', self.owner.server_port, 'not-secret')
        with mock.patch.object(bridge, '_verify_owner_auth', side_effect=
                               bridge.MobileBridgeError('OWNER_AUTH_CHECK_FAILED')):
            with self.assertRaisesRegex(bridge.MobileBridgeError, '^OWNER_AUTH_CHECK_FAILED$'):
                self.start()
        self.assert_off('OWNER_AUTH_CHECK_FAILED')

    def test_empty_token_has_precise_error(self):
        (self.home / 'server.token').touch(mode=0o600)
        with self.assertRaisesRegex(bridge.MobileBridgeError, '^SERVER_TOKEN_UNAVAILABLE$'):
            self.start()
        self.assert_off('SERVER_TOKEN_UNAVAILABLE')

    def test_identity_change_before_auth(self):
        with mock.patch.object(bridge, '_owner_matches', side_effect=[True, False]):
            with self.assertRaisesRegex(bridge.MobileBridgeError, '^OWNER_LOST$'):
                self.start()
        self.assertFalse(self.owner.auth_called.is_set())
        self.assert_off('OWNER_LOST')

    def test_identity_change_after_auth(self):
        with mock.patch.object(bridge, '_owner_matches', side_effect=[True, True, False]):
            with self.assertRaisesRegex(bridge.MobileBridgeError, '^OWNER_LOST$'):
                self.start()
        self.assert_off('OWNER_LOST')

    def test_stop_during_auth(self):
        self.owner.on_auth = self.mgr.stop
        with self.assertRaisesRegex(bridge.MobileBridgeError, '^START_CANCELLED$'):
            self.start()
        self.assertEqual(self.mgr.status()['state'], 'off')
        self.assertIsNone(self.mgr._server)
        self.bind.assert_not_called()

    def test_internet_failure_does_not_start_tunnel(self):
        connector = mock.Mock()
        self.mgr._connector = connector
        with mock.patch.object(self.mgr, '_connector_status',
                               return_value=({'state': 'installed'}, {})):
            self.owner.auth_status = 401
            with self.assertRaisesRegex(bridge.MobileBridgeError, '^OWNER_AUTH_FAILED$'):
                self.mgr.start(self.origin, mode='internet', relay_consent=True,
                               consent_version=bridge.RELAY_CONSENT_VERSION)
        connector.start.assert_not_called()
        self.assert_off('OWNER_AUTH_FAILED')

    def test_worker_http_control_preserves_auth_error(self):
        server = worker._WorkerHTTPServer(('127.0.0.1', 0), worker._WorkerHandler)
        server.manager = self.mgr
        server.secret = 'test-ipc-secret'
        server.last_ipc = 0
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.owner.auth_status = 401
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            body = json.dumps({'owner_origin': self.origin, 'mode': 'lan',
                               'address': '192.168.1.2'})
            conn.request('POST', '/api/mobile/start', body, headers={
                worker.WORKER_HEADER: server.secret, 'Content-Type': 'application/json'})
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            result = json.loads(response.read())
            self.assertEqual(result['error'], 'OWNER_AUTH_FAILED')
            self.assertNotIn(credentials.read_server_token(self.home), json.dumps(result))
        finally:
            conn.close()
        self.assert_off('OWNER_AUTH_FAILED')


class ContractTests(unittest.TestCase):
    def test_error_contracts_and_package(self):
        root = Path(__file__).resolve().parents[1]
        import ast
        module = ast.parse((root / 'scripts/service.py').read_text(encoding='utf-8'))
        values = next(node.value.args[0] for node in module.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == '_MOBILE_ERROR_CODES'
                              for t in node.targets))
        service_codes = frozenset(ast.literal_eval(values))
        self.assertEqual(service_codes, bridge._MOBILE_ERROR_CODES)
        for code in ('SERVER_TOKEN_UNAVAILABLE', 'OWNER_AUTH_FAILED', 'OWNER_AUTH_CHECK_FAILED'):
            self.assertIn(code, service_codes)
            for path in ('assets/kimi-mobile-api.js', 'assets/kimi-remote-widget.js'):
                self.assertIn(code + ':', (root / path).read_text(encoding='utf-8'))
        for name in ('mobile_credentials.py', 'mobile_security.py'):
            self.assertIn('scripts/' + name, updater.MOBILE_RUNTIME_GROUP)
            self.assertFalse(updater.sync_delete_allowed('scripts/' + name))
        self.assertNotEqual(worker.WORKER_VERSION, '3.3.6')

    def test_worker_acl_failure_is_controlled(self):
        with mock.patch.object(worker, '_shared_set_protected_dacl', side_effect=PermissionError):
            with self.assertRaisesRegex(bridge.MobileBridgeError, 'worker ACL 设置失败'):
                worker._ensure_file_acl('unused')


if __name__ == '__main__':
    unittest.main()
