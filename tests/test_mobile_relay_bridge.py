import ast
import contextlib
import io
import json
from email.message import Message
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mobile_bridge as bridge
import mobile_relay as relay
import mobile_tunnel as tunnel
import mobile_worker as worker


OWNER_ORIGIN = 'http://127.0.0.1:43123'
PUBLIC_ORIGIN = 'http://8.8.8.8:18080'
FRP_TOKEN = 'relay-secret-' + 'X' * 40
OWNER_TOKEN = 'owner-secret-' + 'Y' * 40
CA = '-----BEGIN CERTIFICATE-----\nAQID\n-----END CERTIFICATE-----\n'


def config():
    return {'server_ip': '8.8.8.8', 'server_port': 7000,
            'remote_port': 18080, 'token': FRP_TOKEN, 'ca_cert': CA}


def start_body():
    return {'owner_origin': OWNER_ORIGIN, 'mode': 'relay',
            'relay_consent': True, 'consent_version': relay.CONSENT_VERSION,
            'relay_config': config()}


def headers(values):
    out = Message()
    for key, value in values.items():
        out[key] = value
    return out


def service_functions(*names):
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'service.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    selected = ast.Module(body=[node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])
    namespace = {'json': json, 'ipaddress': bridge.ipaddress,
                 'threading': threading, '_MOBILE_PREFIX': '/api/mobile',
                 '_MOBILE_CONTROL_HEADER': 'X-Kimi-Mobile-Control',
                 '_MOBILE_TRUSTED_APP_ORIGIN': 'app://renderer',
                 '_MOBILE_MAX_BODY': 8192, '_MOBILE_ERROR_CODES': bridge._MOBILE_ERROR_CODES,
                 '_MOBILE_CLOSING': threading.Event(), 'log': mock.Mock()}
    exec(compile(selected, str(path), 'exec'), namespace)
    return namespace


class RelayBridgeTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.cf = mock.Mock()
        self.cf.status.return_value = {'connector': {'state': 'installed', 'version': '2026.9.3'}}
        self.frp = mock.Mock()
        self.frp.status.return_value = {'connector': {'state': 'installed', 'version': relay.RELAY_VERSION}}
        self.frp.install.return_value = self.frp.status.return_value
        self.cf.install.return_value = self.cf.status.return_value
        self.stack.enter_context(mock.patch.object(tunnel, 'ConnectorRuntime', return_value=self.cf))
        self.stack.enter_context(mock.patch.object(relay, 'RelayRuntime', return_value=self.frp))
        ssl_context = self.stack.enter_context(mock.patch.object(relay.ssl, 'SSLContext'))
        ssl_context.return_value.cert_store_stats.return_value = {'x509': 1, 'x509_ca': 1}
        inst = {'host': '127.0.0.1', 'port': 43123, 'pid': 99, 'server_id': 'owner-id'}
        for name, value in (('_load_instances', [inst]), ('_pid_alive', True),
                            ('_pid_is_desktop', True), ('_pid_creation_ticks', 123),
                            ('_tcp_listener_pid', 99), ('_owner_responds', True),
                            ('_owner_matches', True), ('local_lan_addresses', ['192.168.1.2']),
                            ('_ensure_server_token', OWNER_TOKEN)):
            self.stack.enter_context(mock.patch.object(bridge, name, return_value=value))
        self.auth = self.stack.enter_context(mock.patch.object(bridge, '_verify_owner_auth'))
        self.stack.enter_context(mock.patch.object(bridge.threading, 'Thread'))
        self.mgr = bridge.MobileBridgeManager('unused-test-home')
        self.server = mock.Mock()
        self.bind = self.stack.enter_context(mock.patch.object(self.mgr, '_bind',
            return_value=(self.server, 39282)))
        self.owner_check = self.stack.enter_context(mock.patch.object(self.mgr,
            '_owner_still_mine', return_value=True))
        self.ready = self.failure = None
        def runtime_start(port, supplied, ready, failure):
            self.assertEqual(port, 39282)
            self.assertEqual(supplied, config())
            self.ready, self.failure = ready, failure
        self.frp.start.side_effect = runtime_start
        self.addCleanup(self.mgr.shutdown)

    def start(self):
        return self.mgr.start(OWNER_ORIGIN, mode='relay', relay_consent=True,
                              consent_version=relay.CONSENT_VERSION, relay_config=config())

    def make_handler(self, method='GET', path='/mobile/pair', extra=None, peer='127.0.0.1'):
        handler = object.__new__(bridge._LanHandler)
        handler.server = types.SimpleNamespace(manager=self.mgr)
        handler._generation = self.mgr._generation
        handler.command, handler.path = method, path
        handler.client_address = (peer, 1234)
        values = {'Host': PUBLIC_ORIGIN.split('://')[1]}
        if extra:
            values.update(extra)
        handler.headers = headers(values)
        handler.wfile = io.BytesIO()
        handler._err = mock.Mock()
        handler._send_status = mock.Mock()
        handler.send_header = mock.Mock()
        handler._end_headers = mock.Mock()
        return handler

    def pair(self):
        token = next(iter(self.mgr._pair_tokens))
        return self.mgr.exchange_pair_token('relay', token, self.mgr._generation)

    def test_ready_callback_is_required_and_secrets_are_not_published(self):
        state = self.start()
        self.assertEqual(state['state'], 'starting')
        self.assertEqual(state['tunnel'], {'state': 'starting'})
        self.assertNotIn('url', state)
        self.assertNotIn('public_origin', state)
        self.assertIsNone(self.mgr.request_context(self.mgr._generation))
        self.bind.assert_called_once_with('127.0.0.1')
        self.cf.start.assert_not_called()
        self.auth.assert_called_once_with('127.0.0.1', 43123, OWNER_TOKEN)
        self.ready(PUBLIC_ORIGIN)
        state = self.mgr.status()
        self.assertEqual(state['state'], 'on')
        self.assertEqual(state['public_origin'], PUBLIC_ORIGIN)
        self.assertTrue(state['url'].startswith(PUBLIC_ORIGIN + '/mobile/pair#pair='))
        self.assertEqual(state['connector']['version'], '2026.9.3')
        self.assertEqual(state['relay_connector']['version'], relay.RELAY_VERSION)
        for secret in (FRP_TOKEN, OWNER_TOKEN, CA):
            self.assertNotIn(secret, json.dumps(state))
        self.assertFalse(any('config' in key for key in self.mgr.__dict__))
        self.assertNotIn(FRP_TOKEN, [value for value in self.mgr.__dict__.values()
                                    if isinstance(value, str)])

    def test_ready_rechecks_owner_and_exact_public_origin(self):
        for origin, owner_ok, code in ((PUBLIC_ORIGIN, False, 'OWNER_LOST'),
                ('http://8.8.4.4:18080', True, 'TUNNEL_START_FAILED'),
                (PUBLIC_ORIGIN + '/?token=' + FRP_TOKEN, True, 'TUNNEL_START_FAILED')):
            with self.subTest(code=code, owner_ok=owner_ok):
                self.start()
                self.owner_check.return_value = owner_ok
                self.ready(origin)
                status = self.mgr.status()
                self.assertEqual(status['state'], 'off')
                self.assertEqual(status['tunnel']['error_code'], code)
                self.assertNotIn('url', status)
                self.assertNotIn(FRP_TOKEN, json.dumps(status))
                self.owner_check.return_value = True

    def test_stop_during_start_and_late_callbacks_do_not_revive(self):
        self.start()
        ready, failure = self.ready, self.failure
        self.mgr.stop()
        ready(PUBLIC_ORIGIN)
        failure('TUNNEL_EXITED')
        self.assertEqual(self.mgr.status()['state'], 'off')
        self.assertEqual(self.mgr._pair_tokens, {})
        self.frp.stop.assert_called()
        self.server.shutdown.assert_called()
        self.server.server_close.assert_called()

    def test_process_exit_revokes_sessions_pair_ws_and_sockets(self):
        self.start()
        self.ready(PUBLIC_ORIGIN)
        sid = self.pair()
        old_generation = self.mgr._generation
        self.assertTrue(self.mgr.ws_acquire(sid, old_generation))
        sock = mock.Mock()
        self.mgr.track_socket(sock, generation=old_generation)
        self.failure('TUNNEL_EXITED')
        self.assertFalse(self.mgr.session_valid(sid, old_generation))
        self.assertFalse(self.mgr.generation_valid(old_generation))
        self.assertEqual(self.mgr._sessions, {})
        self.assertEqual(self.mgr._pair_tokens, {})
        self.assertEqual(self.mgr._ws_total, 0)
        self.assertIsNone(self.mgr._server_token)
        sock.shutdown.assert_called_once()
        sock.close.assert_called_once()
        self.frp.stop.assert_called()
        self.assertEqual(self.mgr.status()['tunnel']['error_code'], 'TUNNEL_EXITED')

    def test_owner_loss_on_authorized_request_revokes_relay(self):
        self.start()
        self.ready(PUBLIC_ORIGIN)
        sid = self.pair()
        self.owner_check.return_value = False
        self.assertIsNone(self.mgr.upstream_snapshot(self.mgr._generation))
        self.assertFalse(self.mgr.session_valid(sid))
        self.assertEqual(self.mgr.status()['tunnel']['error_code'], 'OWNER_LOST')
        self.frp.stop.assert_called()

    def test_auth_failure_never_starts_relay(self):
        self.auth.side_effect = bridge.MobileBridgeError('OWNER_AUTH_FAILED')
        with self.assertRaisesRegex(bridge.MobileBridgeError, '^OWNER_AUTH_FAILED$'):
            self.start()
        self.frp.start.assert_not_called()
        self.bind.assert_not_called()

    def test_invalid_relay_parameters_have_no_start_side_effects(self):
        for field, value in (('server_ip', '127.0.0.1'), ('remote_port', True),
                             ('server_port', 18080), ('token', 'short'),
                             ('ca_cert', 'not-pem')):
            supplied = config()
            supplied[field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(bridge.MobileBridgeError, '^RELAY_CONFIG_INVALID$'):
                    self.mgr.start(OWNER_ORIGIN, mode='relay', relay_consent=True,
                        consent_version=relay.CONSENT_VERSION, relay_config=supplied)
        self.assertEqual(self.mgr.status()['state'], 'off')
        self.bind.assert_not_called()
        self.frp.start.assert_not_called()
        self.auth.assert_not_called()

    def test_http_relay_cookie_and_same_origin_guards(self):
        self.start()
        self.ready(PUBLIC_ORIGIN)
        token = next(iter(self.mgr._pair_tokens))
        handler = self.make_handler('POST', '/mobile/pair/exchange', {'Origin': PUBLIC_ORIGIN})
        self.assertTrue(handler._request_ok())
        handler._read_body = mock.Mock(return_value=json.dumps({'token': token}).encode())
        handler._pair_exchange()
        cookie = next(args[1] for args, _ in handler.send_header.call_args_list
                      if args[0] == 'Set-Cookie')
        for flag in ('HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(flag, cookie)
        self.assertNotIn('Secure', cookie)
        self.assertNotIn(FRP_TOKEN, handler.wfile.getvalue().decode())
        for supplied in ({'Origin': 'https://8.8.8.8:18080'}, {'Origin': 'null'},
                         {'Host': '8.8.8.8:80', 'Origin': PUBLIC_ORIGIN},
                         {'Origin': PUBLIC_ORIGIN + '/'}, {}):
            bad = self.make_handler('POST', '/mobile/pair/exchange', supplied)
            self.assertFalse(bad._request_ok())
            self.assertEqual(bad._err.call_args.args[0], 403)
        ws = self.make_handler('GET', '/api/v1/ws', {'Connection': 'Upgrade'})
        self.assertFalse(ws._request_ok())
        read = self.make_handler(extra={'Origin': 'http://evil.example'})
        self.assertFalse(read._request_ok())

    def test_loopback_only_and_forwarded_headers_are_rejected(self):
        self.start()
        self.ready(PUBLIC_ORIGIN)
        self.assertFalse(self.make_handler(peer='192.168.1.3')._request_ok())
        for name in ('CF-Connecting-IP', 'X-Forwarded-For', 'X-Forwarded-Host',
                     'Forwarded', 'X-Real-IP'):
            with self.subTest(header=name):
                handler = self.make_handler(extra={name: '1.1.1.1'})
                self.assertFalse(handler._request_ok())
                self.assertEqual(handler._err.call_args.args[0], 400)
        handler = self.make_handler()
        self.assertTrue(handler._request_ok())
        self.assertEqual(handler._client_ip, 'relay')

    def test_anonymous_aggregate_and_session_buckets_are_separate(self):
        self.start()
        self.ready(PUBLIC_ORIGIN)
        generation = self.mgr._generation
        with mock.patch.object(bridge.time, 'monotonic', return_value=100.0):
            for index in range(int(bridge.INTERNET_RATE_BURST)):
                self.assertTrue(self.mgr.internet_rate_ok(generation, str(index), 'forged'))
            self.assertFalse(self.mgr.internet_rate_ok(generation, 'another', 'forged-again'))
            self.assertEqual(set(self.mgr._rate_buckets), {'global'})
            sid = self.pair()
            for _ in range(int(bridge.PAIRED_SID_RATE_BURST)):
                self.assertTrue(self.mgr.internet_rate_ok(generation, 'relay', sid))
            self.assertFalse(self.mgr.internet_rate_ok(generation, 'relay', sid))
            self.mgr._issue_pair_token_locked()
            sid2 = self.pair()
            self.assertTrue(self.mgr.internet_rate_ok(generation, 'relay', sid2))
            self.assertIn('paired:global', self.mgr._rate_buckets)
            self.assertFalse(self.mgr.internet_rate_ok(generation, 'relay'))

    def test_runtime_status_and_errors_are_strictly_projected(self):
        self.frp.status.return_value = {'connector': {'state': 'installed',
            'version': relay.RELAY_VERSION, 'token': FRP_TOKEN, 'config': config()},
            'public_origin': FRP_TOKEN, 'diagnostics': {'log': FRP_TOKEN}}
        state = self.mgr.status()
        self.assertEqual(state['relay_connector'], {'state': 'installed', 'version': relay.RELAY_VERSION})
        self.assertNotIn(FRP_TOKEN, json.dumps(state))
        self.frp.status.return_value['connector']['error_code'] = FRP_TOKEN
        self.assertEqual(self.mgr.status()['relay_connector']['error_code'], 'CONNECTOR_INSTALL_FAILED')
        self.frp.status.return_value = {'connector': {'state': 'installed', 'version': relay.RELAY_VERSION}}
        self.frp.start.side_effect = RuntimeError(FRP_TOKEN)
        with self.assertRaisesRegex(bridge.MobileBridgeError, '^TUNNEL_START_FAILED$'):
            self.start()
        self.assertNotIn(FRP_TOKEN, json.dumps(self.mgr.status()))

    def test_install_mode_routes_to_selected_runtime(self):
        self.mgr.install_connector(True, relay.CONSENT_VERSION, 'relay')
        self.frp.install.assert_called_once_with(True, relay.CONSENT_VERSION)
        self.cf.install.assert_not_called()
        self.mgr.install_connector(True, bridge.RELAY_CONSENT_VERSION)
        self.cf.install.assert_called_once_with(True, bridge.RELAY_CONSENT_VERSION)
        with self.assertRaisesRegex(bridge.MobileBridgeError, '^CONSENT_REQUIRED$'):
            self.mgr.install_connector(True, bridge.RELAY_CONSENT_VERSION, 'relay')

    def test_proxy_uses_only_owner_credential_and_filters_outbound_secret(self):
        self.start()
        self.ready(PUBLIC_ORIGIN)
        sid = self.pair()
        for payload, status in ((b'[]', 200), (OWNER_TOKEN.encode(), 502)):
            with self.subTest(status=status):
                handler = self.make_handler(path='/api/v1/workspaces',
                    extra={'Cookie': bridge.SESSION_COOKIE + '=' + sid})
                self.assertTrue(handler._request_ok())
                conn, response = mock.Mock(), mock.Mock()
                response.headers = Message()
                response.status, response.length = 200, 0
                response.getheaders.return_value = [('Content-Type', 'application/json')]
                response.read1.side_effect = [payload, b'']
                conn.getresponse.return_value = response
                with mock.patch.object(bridge.http.client, 'HTTPConnection', return_value=conn):
                    handler._proxy_http('/api/v1/workspaces')
                conn.putheader.assert_any_call('Authorization', 'Bearer ' + OWNER_TOKEN)
                self.assertNotIn(FRP_TOKEN, str(conn.putheader.call_args_list))
                if status == 200:
                    handler._send_status.assert_called_with(200)
                    self.assertEqual(handler.wfile.getvalue(), b'[]')
                else:
                    self.assertEqual(handler._err.call_args.args[0], 502)
                    self.assertNotIn(OWNER_TOKEN.encode(), handler.wfile.getvalue())

    def test_cf_secure_cookie_and_lan_behavior_are_preserved(self):
        self.cf.start.side_effect = lambda port, ready, failed: ready('https://test.trycloudflare.com')
        state = self.mgr.start(OWNER_ORIGIN, mode='internet', relay_consent=True,
                              consent_version=bridge.RELAY_CONSENT_VERSION)
        self.assertEqual(state['public_origin'], 'https://test.trycloudflare.com')
        handler = self.make_handler('POST', '/mobile/pair/exchange', {
            'Host': 'test.trycloudflare.com', 'Origin': 'https://test.trycloudflare.com'})
        self.assertTrue(handler._request_ok())
        pair = next(iter(self.mgr._pair_tokens))
        handler._read_body = mock.Mock(return_value=json.dumps({'token': pair}).encode())
        handler._pair_exchange()
        self.assertTrue(any(args[0] == 'Set-Cookie' and '; Secure' in args[1]
                            for args, _ in handler.send_header.call_args_list))
        self.mgr.stop()
        state = self.mgr.start(OWNER_ORIGIN, '192.168.1.2')
        self.assertEqual(state['mode'], 'lan')
        self.assertEqual(state['tunnel'], {'state': 'off'})
        self.assertTrue(state['url'].startswith('http://192.168.1.2:39282/'))
        self.bind.assert_called_with('192.168.1.2')

    def test_upload_and_ws_allowlists_are_unchanged(self):
        handler = self.make_handler('POST', '/api/v1/files')
        self.assertTrue(handler._api_allowed('POST', '/api/v1/files'))
        self.assertEqual(handler._body_limits('/api/v1/files'),
                         (bridge.UPLOAD_BODY_IDLE_SECONDS, bridge.UPLOAD_BODY_MAX_SECONDS))
        for path in ('/api/v1/shutdown', '/api/v1/debug', '/api/v1/files/x/delete',
                     '/api/v1/providers', '/api/mobile/start'):
            self.assertFalse(handler._api_allowed('POST', path))
        self.assertIn('subscribe', bridge._WS_ALLOWED_TYPES)
        self.assertNotIn('terminal_input', bridge._WS_ALLOWED_TYPES)

    def test_session_archive_delete_export_are_allowed_but_stay_narrow(self):
        # 放行：单会话 :action 的 archive/delete，导出的 POST 子路径，
        # 以及 v2 精确批量归档/恢复。
        handler = self.make_handler('POST', '/api/v1/sessions/x')
        for path in ('/api/v1/sessions/s1:delete', '/api/v1/sessions/s1:archive',
                     '/api/v1/sessions/s1/export',
                     '/api/v2/sessions:archive', '/api/v2/sessions:restore'):
            self.assertTrue(handler._api_allowed('POST', path), path)
        # 只放 POST：归档/删除/导出都不接受其它方法。
        for method in ('GET', 'PUT', 'DELETE', 'PATCH'):
            for path in ('/api/v1/sessions/s1:delete', '/api/v1/sessions/s1:archive',
                         '/api/v1/sessions/s1/export',
                         '/api/v2/sessions:archive', '/api/v2/sessions:restore'):
                self.assertFalse(handler._api_allowed(method, path), (method, path))
        # 未放行的近邻路径必须仍然拒绝（含 v2 其它写面与子资源）。
        for path in ('/api/v2/sessions', '/api/v2/sessions:delete',
                     '/api/v2/sessions/s1:archive', '/api/v2/sessions:s1/archive',
                     '/api/v1/sessions/s1:purge', '/api/v1/sessions/s1/export/extra',
                     '/api/v1/providers:refresh', '/api/v1/shutdown',
                     '/api/v1/authority', '/api/v1/fs:read'):
            self.assertFalse(handler._api_allowed('POST', path), path)


class RelayControlTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        ssl_context = self.stack.enter_context(mock.patch.object(relay.ssl, 'SSLContext'))
        ssl_context.return_value.cert_store_stats.return_value = {'x509': 1, 'x509_ca': 1}
        self.client = worker.MobileWorkerClient('unused-test-home')
        self.invoke = self.stack.enter_context(mock.patch.object(self.client, '_invoke',
                                                                 return_value={'state': 'starting'}))

    def test_start_and_install_contracts(self):
        self.client.start(*bridge._start_body_params(start_body()))
        self.assertEqual(self.invoke.call_args.args[:3],
                         ('POST', '/api/mobile/start', start_body()))
        self.assertTrue(self.invoke.call_args.kwargs['ensure'])
        self.client.install_connector(True, relay.CONSENT_VERSION, 'relay')
        self.assertEqual(self.invoke.call_args.args[2],
                         {'mode': 'relay', 'consent': True, 'consent_version': relay.CONSENT_VERSION})
        self.client.install_connector(True, bridge.RELAY_CONSENT_VERSION)
        self.assertEqual(self.invoke.call_args.args[2]['mode'], 'internet')

    def test_bad_params_fail_before_spawn_or_ipc(self):
        for update in ({'relay_config': None}, {'relay_consent': False},
                       {'consent_version': bridge.RELAY_CONSENT_VERSION},
                       {'owner_origin': 'http://8.8.8.8:43123'}):
            body = start_body()
            body.update(update)
            with self.subTest(update=tuple(update)):
                with self.assertRaises(bridge.MobileBridgeError):
                    self.client.start(body['owner_origin'], mode='relay',
                        relay_consent=body['relay_consent'], consent_version=body['consent_version'],
                        relay_config=body['relay_config'])
        with self.assertRaises(bridge.MobileBridgeError):
            self.client.install_connector(False, relay.CONSENT_VERSION, 'relay')
        self.invoke.assert_not_called()

    def test_exact_schema_and_mode_specific_consent(self):
        self.assertEqual(bridge._start_body_params(start_body())[-1], config())
        for key in start_body():
            body = start_body()
            del body[key]
            with self.subTest(missing=key):
                with self.assertRaises(bridge.MobileBridgeError):
                    bridge._start_body_params(body)
        for extra in ({'address': '127.0.0.1'}, {'token': FRP_TOKEN}):
            with self.assertRaises(bridge.MobileBridgeError):
                bridge._start_body_params(dict(start_body(), **extra))
        self.assertEqual(bridge._install_body_params({'consent': True,
            'consent_version': bridge.RELAY_CONSENT_VERSION})[-1], 'internet')
        with self.assertRaises(bridge.MobileBridgeError):
            bridge._install_body_params({'mode': 'lan', 'consent': True,
                                          'consent_version': relay.CONSENT_VERSION})

    def test_worker_and_service_dispatch_use_same_contract(self):
        for path, body, method in (('/api/mobile/start', start_body(), 'start'),
                ('/api/mobile/connector/install', {'mode': 'relay', 'consent': True,
                                                  'consent_version': relay.CONSENT_VERSION}, 'install_connector')):
            with self.subTest(path=path):
                mgr = mock.Mock()
                wh = object.__new__(worker._WorkerHandler)
                wh.server = types.SimpleNamespace(manager=mgr)
                wh.path, wh.command = path, 'POST'
                wh._guards_ok = mock.Mock(return_value=True)
                wh._body = mock.Mock(return_value=body)
                wh._json, wh._fail = mock.Mock(), mock.Mock()
                wh._dispatch()
                expected = bridge._start_body_params(body) if method == 'start' else bridge._install_body_params(body)
                getattr(mgr, method).assert_called_once_with(*expected)
                wh._fail.assert_not_called()
                mgr.reset_mock()
                mgr._trusted_control_origins.return_value = {'app://renderer'}
                ns = service_functions('_mobile_dispatch')
                ns.update({'_mobile_manager': lambda: mgr,
                           '_allowed_hosts': lambda: {'127.0.0.1:39281'},
                           '_mobile_read_body': lambda handler: body,
                           '_mobile_send': mock.Mock(), '_mobile_error': mock.Mock(),
                           '_mobile_fail_closed': mock.Mock()})
                sh = types.SimpleNamespace(path=path, command='POST',
                    client_address=('127.0.0.1', 1234), headers=headers({
                        'Host': '127.0.0.1:39281', 'Origin': 'app://renderer',
                        'X-Kimi-Mobile-Control': '1'}))
                self.assertTrue(ns['_mobile_dispatch'](sh))
                getattr(mgr, method).assert_called_once_with(*expected)
                ns['_mobile_error'].assert_not_called()
                mgr.reset_mock()
                mgr._control_origin_ok.return_value = (True, 'app://renderer')
                mgr._control_body.return_value = body
                sh.server = types.SimpleNamespace(server_port=39281)
                self.assertTrue(bridge.MobileBridgeManager.handle_control(mgr, sh))
                getattr(mgr, method).assert_called_once_with(*expected)
                mgr._control_error.assert_not_called()

    def test_all_control_readers_reject_duplicate_keys_and_keep_legacy_limit(self):
        ns = service_functions('_mobile_read_body')
        raws = [b'{"mode":"relay","mode":"lan"}',
                json.dumps({'mode': 'internet', 'padding': 'x' * 9000}).encode()]
        for raw in raws:
            with self.subTest(length=len(raw)):
                h = types.SimpleNamespace(path='/api/mobile/start',
                    headers=headers({'Content-Length': str(len(raw))}),
                    connection=mock.Mock(), rfile=io.BytesIO(raw))
                with self.assertRaises(ValueError):
                    ns['_mobile_read_body'](h)
                wh = object.__new__(worker._WorkerHandler)
                wh.path, wh.headers, wh.connection = h.path, h.headers, h.connection
                wh.rfile = io.BufferedReader(io.BytesIO(raw))
                with self.assertRaises(bridge.MobileBridgeError):
                    wh._body()
                with mock.patch.object(bridge, '_read_with_deadline', return_value=raw):
                    with self.assertRaises(bridge.MobileBridgeError):
                        bridge.MobileBridgeManager._control_body(None, h)
        raw = json.dumps(dict(start_body(), relay_config=dict(config(), ca_cert=CA + ' ' * 9000))).encode()
        h = types.SimpleNamespace(path='/api/mobile/start',
            headers=headers({'Content-Length': str(len(raw))}),
            connection=mock.Mock(), rfile=io.BytesIO(raw))
        self.assertEqual(ns['_mobile_read_body'](h)['mode'], 'relay')

    def test_detach_does_not_stop_and_old_worker_can_be_explicitly_stopped(self):
        with mock.patch.object(self.client, '_call') as call:
            self.client._worker = (39283, 'ipc-secret', worker.WORKER_VERSION)
            self.client.begin_shutdown()
            self.assertIsNone(self.client._worker)
            call.assert_not_called()
        client = worker.MobileWorkerClient('unused-test-home')
        with mock.patch.object(client, '_attach', return_value=None), \
                mock.patch.object(client, '_old_worker_handle', return_value=(39283, 'old-secret')), \
                mock.patch.object(client, '_local_off_status', return_value={'state': 'off'}), \
                mock.patch.object(client, '_call', return_value={}) as call:
            self.assertEqual(client.stop(), {'state': 'off'})
            self.assertEqual([item.args[3] for item in call.call_args_list],
                             ['/api/mobile/stop', '/api/mobile/internal/stop-worker'])
        self.assertEqual(worker.WORKER_VERSION, '3.3.8')

    def test_local_off_status_includes_relay_install_state_without_spawn(self):
        frp, cf = mock.Mock(), mock.Mock()
        frp.status.return_value = {'connector': {'state': 'installed', 'version': relay.RELAY_VERSION,
                                                'token': FRP_TOKEN}}
        cf.status.return_value = {'connector': {'state': 'missing', 'version': '2026.9.3'}}
        with mock.patch.object(relay, 'RelayRuntime', return_value=frp), \
                mock.patch.object(tunnel, 'ConnectorRuntime', return_value=cf), \
                mock.patch.object(worker, 'local_lan_addresses', return_value=[]):
            status = self.client._local_off_status()
        self.assertEqual(status['relay_connector'], {'state': 'installed', 'version': relay.RELAY_VERSION})
        self.assertEqual(status['tunnel'], {'state': 'off'})
        self.assertNotIn(FRP_TOKEN, json.dumps(status))
        self.invoke.assert_not_called()


if __name__ == '__main__':
    unittest.main()
