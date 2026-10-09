from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')

JS_BASE = r'''
const assert = require('assert').strict;
const path = require('path');
const root = process.argv[1];
const api = require(path.join(root, 'assets/kimi-mobile-api.js'));
const certificate = '-----BEGIN CERTIFICATE-----\nQUJDRA==\n-----END CERTIFICATE-----\n';
const config = () => ({server_ip: '8.8.8.8', server_port: 7000, remote_port: 6000,
  token: 'test-auth-token-'.repeat(3), ca_cert: certificate});
const off = (mode = 'relay') => ({enabled: false, state: 'off', mode, device_count: 0,
  connector: {state: 'missing', version: '2026.9.3'},
  relay_connector: {state: 'installed', version: '0.61.1'}, tunnel: {state: 'off'}});
const ready = (port = 6000) => Object.assign(off(), {enabled: true, state: 'on',
  tunnel: {state: 'ready'}, public_origin: 'http://8.8.8.8:' + port,
  pair_state: 'available', expires_at: Date.now() / 1000 + 600,
  url: 'http://8.8.8.8:' + port + '/mobile/pair#pair=Pair_Token_123456'});
function client(reply = off(), http = 200) {
  const calls = [];
  const win = {location: {protocol: 'http:', origin: 'http://127.0.0.1:12345', search: ''}};
  const instance = api.create({window: win, fetch: async (url, init) => {
    calls.push({url, init, body: init.body ? JSON.parse(init.body) : undefined});
    return {status: http, url, text: async () => JSON.stringify(reply)};
  }});
  return {instance, calls};
}
const rejectCode = (code) => (err) => {
  assert.equal(err.code, code);
  assert(!err.message.includes(config().token));
  assert(!err.message.includes('BEGIN CERTIFICATE'));
  return true;
};
'''

JS_DOM = r'''
const fs = require('fs');
const vm = require('vm');
class Element {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.doc = doc; this.children = [];
    this.parentNode = null; this.className = ''; this.style = {}; this.attributes = {};
    this.listeners = {}; this.value = ''; this.checked = false; this.disabled = false;
    this._text = ''; this.id = '';
  }
  get classList() {
    const el = this;
    const entries = () => el.className.split(/\s+/).filter(Boolean);
    return {contains: v => entries().includes(v),
      add: v => {if (!entries().includes(v)) el.className = entries().concat(v).join(' ');},
      remove: v => {el.className = entries().filter(x => x !== v).join(' ');},
      toggle: (v, force) => {
        const on = force === undefined ? !entries().includes(v) : force;
        el.className = entries().filter(x => x !== v).concat(on ? [v] : []).join(' ');
        return on;
      }};
  }
  appendChild(el) {el.parentNode = this; this.children.push(el); return el;}
  removeChild(el) {this.children = this.children.filter(x => x !== el); el.parentNode = null;}
  get firstChild() {return this.children[0] || null;}
  set textContent(text) {this._text = String(text); this.children.forEach(x => x.parentNode = null); this.children = [];}
  get textContent() {return this._text + this.children.map(x => x.textContent).join('');}
  set innerHTML(text) {this.textContent = text;}
  setAttribute(k, v) {this.attributes[k] = String(v);}
  getAttribute(k) {return this.attributes[k];}
  addEventListener(name, fn) {(this.listeners[name] ||= []).push(fn);}
  removeEventListener(name, fn) {this.listeners[name] = (this.listeners[name] || []).filter(x => x !== fn);}
  dispatch(name, extra = {}) {
    const e = Object.assign({target: this, stopPropagation() {}, preventDefault() {}}, extra);
    (this.listeners[name] || []).forEach(fn => fn(e));
  }
  querySelector(sel) {
    const wanted = sel.slice(1);
    for (const child of this.children) {
      if (child.id === wanted) return child;
      const nested = child.querySelector(sel); if (nested) return nested;
    }
    return null;
  }
  focus() {this.doc.activeElement = this;}
}
function dom() {
  const document = {createElement(tag) {return new Element(tag, document);},
    createTextNode(text) {const el = new Element('text', document); el.textContent = text; return el;},
    getElementById(id) {return this.body.querySelector('#' + id) || this.head.querySelector('#' + id);},
    contains(el) {while (el) {if (el === this.body || el === this.head) return true; el = el.parentNode;} return false;},
    addEventListener() {}, removeEventListener() {}};
  document.body = new Element('body', document); document.head = new Element('head', document);
  document.activeElement = document.body;
  return document;
}
const flush = async () => {for (let i = 0; i < 24; i++) await Promise.resolve();};
'''


@unittest.skipUnless(NODE, 'Node is required for JavaScript contract tests')
class RelayFrontendTests(unittest.TestCase):
    def run_js(self, source, dom=False):
        script = JS_BASE + (JS_DOM if dom else '') + '\n(async () => {\n' + source + r'''
})().catch(err => {console.error(err); process.exitCode = 1;});
'''
        result = subprocess.run([NODE, '-e', script, str(ROOT)], cwd=ROOT,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_relay_payload_and_legacy_payloads(self):
        self.run_js(r'''
const {instance, calls} = client();
await instance.installConnector(true, 'relay');
assert.deepEqual(calls[0].body, {mode: 'relay', consent: true, consent_version: 'frp-tcp-http-v1'});
await instance.setEnabled(true, undefined, 'relay', true, config());
assert.deepEqual(calls[1].body, {owner_origin: 'http://127.0.0.1:12345', mode: 'relay',
  relay_consent: true, consent_version: 'frp-tcp-http-v1', relay_config: config()});
await instance.installConnector(true);
assert.deepEqual(calls[2].body, {consent: true, consent_version: 'cloudflare-quick-2026-09-v1'});
await instance.installConnector(true, 'internet');
assert.deepEqual(calls[3].body, calls[2].body);
await instance.setEnabled(true, undefined, 'internet', true);
assert.deepEqual(calls[4].body, {owner_origin: 'http://127.0.0.1:12345', mode: 'internet',
  relay_consent: true, consent_version: 'cloudflare-quick-2026-09-v1'});
await instance.setEnabled(true, '192.168.1.2', 'lan');
assert.deepEqual(calls[5].body, {owner_origin: 'http://127.0.0.1:12345', mode: 'lan', address: '192.168.1.2'});
await instance.setEnabled(false, undefined, 'relay', true, config());
assert.deepEqual(calls[6].body, {});
for (const call of calls) {
  assert(call.url.startsWith('http://127.0.0.1:39281/api/mobile/'));
  assert(!call.url.includes(config().token));
  assert.equal(call.init.credentials, 'omit'); assert.equal(call.init.redirect, 'error');
  assert.equal(call.init.headers['X-Kimi-Mobile-Control'], '1');
}
''')

    def test_invalid_config_and_consent_never_send(self):
        self.run_js(r'''
const {instance, calls} = client();
for (const server_ip of ['0.1.2.3', '10.1.2.3', '100.64.0.1', '100.127.255.255',
  '127.0.0.1', '169.254.1.1', '172.16.0.1', '172.31.255.255', '192.168.0.1',
  '192.0.0.1', '192.0.2.1', '192.88.99.1', '198.18.0.1', '198.19.255.255',
  '198.51.100.1', '203.0.113.1', '224.0.0.1', '255.255.255.255',
  '008.8.8.8', '8.8.8', '134744072', '0x08080808', '8.8.8.8 ', '[::1]', 'example.com']) {
  await assert.rejects(instance.setEnabled(true, undefined, 'relay', true,
    Object.assign(config(), {server_ip})), rejectCode('RELAY_CONFIG_INVALID'));
}
for (const patch of [{server_port: 0}, {remote_port: 65536}, {server_port: '7000'},
  {server_port: 1.1}, {remote_port: 7000}, {token: 'x'.repeat(31)}, {token: 'x'.repeat(513)},
  {token: 'x'.repeat(32) + '\n'}, {token: 'x'.repeat(32) + ' '}, {token: 'x'.repeat(32) + '\x7f'},
  {token: 'x'.repeat(32) + '中'}, {ca_cert: ''}, {ca_cert: '-----BEGIN PRIVATE KEY-----\nQUJDRA==\n-----END PRIVATE KEY-----'},
  {ca_cert: [certificate, certificate.replace('QUJDRA==', 'QUJDREU='), certificate.replace('QUJDRA==', 'QUJDREVG'),
    certificate.replace('QUJDRA==', 'QUJDREVGRw=='), certificate.replace('QUJDRA==', 'QUJDREVGR0g=')].join('\n')},
  {ca_cert: certificate + certificate}, {ca_cert: certificate + 'not-pem'},
  {ca_cert: 'x'.repeat(65537)}, {ca_cert: certificate.replace('QUJDRA==', 'QU=JDRA=')},
  {ca_cert: certificate, extra: true}, {ca_cert: certificate, server_name: 'unexpected'}]) {
  await assert.rejects(instance.setEnabled(true, undefined, 'relay', true,
    Object.assign(config(), patch)), rejectCode('RELAY_CONFIG_INVALID'));
}
await assert.rejects(instance.setEnabled(true, undefined, 'relay', false, config()), rejectCode('CONSENT_REQUIRED'));
await assert.rejects(instance.installConnector(false, 'relay'), rejectCode('CONSENT_REQUIRED'));
await assert.rejects(instance.installConnector(true, 'unknown'), rejectCode('MOBILE_BAD_ARG'));
await assert.rejects(instance.setEnabled(true, '192.168.1.2', 'relay', true, config()), rejectCode('MOBILE_BAD_ARG'));
assert.equal(calls.length, 0);
for (const n of [1, 65535]) {
  assert.equal(api.validateRelayConfig(Object.assign(config(), {remote_port: n})).remote_port, n);
}
assert.equal(api.validateRelayConfig(Object.assign(config(), {ca_cert: certificate.replace(/\n/g, '\r\n')})).ca_cert, certificate);
const wide = 'QUJD'.repeat(12000);
const wideCert = '-----BEGIN CERTIFICATE-----\n' + wide + '\n-----END CERTIFICATE-----';
assert.equal(api.validateRelayConfig(Object.assign(config(), {ca_cert: wideCert})).ca_cert, wideCert + '\n');
const chain = [certificate.trim(), certificate.replace('QUJDRA==', 'QUJDREU=').trim()].join(' \t\n');
const normalizedChain = [certificate.trim(), certificate.replace('QUJDRA==', 'QUJDREU=').trim()].join('\n') + '\n';
assert.equal(api.validateRelayConfig(Object.assign(config(), {ca_cert: chain})).ca_cert,
  normalizedChain);
await assert.rejects(instance.setEnabled(true, undefined, 'relay', true,
  Object.assign(config(), {extra: true})), rejectCode('RELAY_CONFIG_INVALID'));
''')

    def test_pair_url_binding_keeps_cf_and_lan_rules(self):
        self.run_js(r'''
for (const port of [1, 80, 6000, 65535]) {
  const status = ready(port);
  assert.equal(api.validateRemoteURL(status.url, status), status.url);
}
const s = ready();
for (const url of [s.url.replace('http:', 'https:'), s.url.replace('8.8.8.8', '1.1.1.1'),
  s.url.replace(':6000', ':6001'), s.url.replace('8.8.8.8', '008.8.8.8'),
  s.url.replace('8.8.8.8', 'user:auth@8.8.8.8'), s.url.replace('/mobile/pair', '/x/../mobile/pair'),
  s.url.replace('#pair=', '?token=auth#pair='), s.url + '&token=auth', s.url + '#extra',
  s.url.replace('/mobile/pair', '/mobile/%70air'), s.url.replace('http://', 'http:\\'),
  s.url.replace('#pair=', '#token=')]) {
  assert.throws(() => api.validateRemoteURL(url, s), rejectCode('MOBILE_URL_REJECTED'));
}
assert.throws(() => api.validateRemoteURL(s.url), rejectCode('MOBILE_URL_REJECTED'));
for (const patch of [{enabled: false}, {state: 'starting'}, {tunnel: {state: 'starting'}},
  {public_origin: 'http://10.1.2.3:6000'}, {public_origin: 'http://8.8.8.8:6000/'},
  {public_origin: 'http://8.8.8.8:06000'}, {public_origin: 'http://8.8.8.8:6000?x=y'},
  {pair_state: 'used'}, {expires_at: 1}]) {
  assert.throws(() => api.validateRemoteURL(s.url, Object.assign({}, s, patch)), rejectCode('MOBILE_URL_REJECTED'));
}
const cf = Object.assign(ready(), {mode: 'internet', public_origin: 'https://safe-name.trycloudflare.com',
  url: 'https://safe-name.trycloudflare.com/mobile/pair#pair=Pair_Token_123456'});
assert.equal(api.validateRemoteURL(cf.url, cf), cf.url);
assert.throws(() => api.validateRemoteURL(s.url, cf), rejectCode('MOBILE_URL_REJECTED'));
assert.throws(() => api.validateRemoteURL(cf.url.replace('safe-name.trycloudflare.com', 'example.com'),
  Object.assign({}, cf, {public_origin: 'https://example.com'})), rejectCode('MOBILE_URL_REJECTED'));
const lan = {mode: 'lan', address: '192.168.1.2', port: 39282};
assert.equal(api.validateRemoteURL('http://192.168.1.2:39282/mobile/pair#pair=Pair_Token_123456', lan),
  'http://192.168.1.2:39282/mobile/pair#pair=Pair_Token_123456');
''')

    def test_status_projection_compatibility_and_redaction(self):
        self.run_js(r'''
const old = off('internet'); delete old.relay_connector;
const legacy = await client(old).instance.status(); assert(!('relay_connector' in legacy));
const raw = Object.assign(ready(), {relay_config: config(), token: config().token,
  connector: {state: 'failed', version: '2026.9.3', error_code: 'CONNECTOR_HASH_MISMATCH'},
  relay_connector: {state: 'installed', version: '9.2.3-beta', token: config().token}});
const result = await client(raw).instance.status();
assert.equal(result.relay_connector.version, '9.2.3-beta'); assert.equal(result.url, raw.url);
assert(!result.error); assert(!JSON.stringify(result).includes(config().token));
assert(!('relay_config' in result));
for (const patch of [{relay_connector: {state: 'broken', version: '0.61.1'}},
  {relay_connector: {state: 'installed', version: 'x'.repeat(65)}},
  {relay_connector: {state: 'installed', version: 'http://evil'}},
  {relay_connector: {state: 'installed', version: '0.61.1\n'}},
  {relay_connector: {state: 'installed', version: '0.61.1', error_code: 'arbitrary-secret'}},
  {connector: {state: 'installed', version: 'other-version'}},
  {public_origin: undefined}, {public_origin: 'https://8.8.8.8:6000'},
  {public_origin: 'http://8.8.8.8:6000\n'},
  {public_origin: 'http://127.0.0.1:6000'}, {url: raw.url + '?auth=secret'}]) {
  await assert.rejects(client(Object.assign({}, ready(), patch)).instance.status(), rejectCode('MOBILE_BAD_RESPONSE'));
}
const expired = await client(Object.assign(ready(), {expires_at: 1})).instance.status();
assert.equal(expired.pair_state, 'expired'); assert(!expired.url);
const {instance, calls} = client({error_code: 'RELAY_CONFIG_INVALID', error: config().token}, 400);
await assert.rejects(instance.setEnabled(true, undefined, 'relay', true, config()), rejectCode('RELAY_CONFIG_INVALID'));
assert.equal(calls.length, 1);
''')

    def test_widget_relay_install_start_reopen_rotate_stop_and_focus(self):
        self.run_js(r'''
const document = dom(); const timers = new Map(); let nextTimer = 1;
let state = off(); state.relay_connector.state = 'missing';
state.connector = {state: 'failed', version: '2026.9.3', error_code: 'CONNECTOR_HASH_MISMATCH'};
const calls = []; const storage = new Map([['kur-mode', 'relay']]); let resolveStart;
const mobile = {status: async () => state,
  installConnector: async (...args) => {calls.push(['install', ...args]);
    state = Object.assign({}, state, {relay_connector: {state: 'installed', version: '0.61.1'}}); return state;},
  setEnabled: (...args) => {
    calls.push(['enabled', ...args.slice(0, 4), args[4] ? JSON.parse(JSON.stringify(args[4])) : undefined]);
    if (args[0]) return new Promise(resolve => {resolveStart = resolve;});
    state = off(); return Promise.resolve(state);
  },
  rotatePair: async () => {calls.push(['rotate']); state = Object.assign({}, state,
    {url: state.public_origin + '/mobile/pair#pair=New_Pair_Token_123456'}); return state;}};
const window = {KimiMobileAPI: Object.assign({}, api, {create: () => mobile}),
  localStorage: {getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v)},
  navigator: {}, location: {protocol: 'http:', origin: 'http://127.0.0.1:12345'}};
vm.runInNewContext(fs.readFileSync(path.join(root, 'assets/kimi-remote-widget.js'), 'utf8'),
  {window, document, Promise, setTimeout: (fn, ms) => {const id = nextTimer++; timers.set(id, {fn, ms}); return id;},
    clearTimeout: id => timers.delete(id)});
const el = id => document.getElementById(id);
const click = id => {assert(!el(id).disabled, id + ' disabled'); el(id).dispatch('click');};
const check = id => {el(id).checked = true; el(id).dispatch('change');};
const fill = (id, value) => {el(id).value = value; el(id).dispatch('input');};
window.KimiRemoteWidget.open(); await flush();
assert.equal(el('kur-mode-relay').getAttribute('aria-pressed'), 'true');
assert.equal(el('kur-frp-server-port').value, '7000'); assert.equal(el('kur-frp-remote-port').value, '6000');
assert.equal(el('kur-frp-token').type, 'password'); assert(el('kur-enable').disabled);
assert.equal(document.activeElement, el('kur-x'));
check('kur-frp-download-consent'); click('kur-frp-install'); await flush();
assert.deepEqual(calls[0], ['install', true, 'relay']);
assert(el('kur-frp-install').classList.contains('kur-hidden'));
fill('kur-frp-server-ip', '8.8.8.8'); fill('kur-frp-token', config().token); fill('kur-frp-ca-cert', certificate);
check('kur-frp-consent'); assert(!el('kur-enable').disabled);
el('kur-frp-token').focus();
el('kur-overlay').dispatch('keydown', {key: 'Tab', shiftKey: false});
assert.equal(document.activeElement, el('kur-frp-token'));
click('kur-enable'); el('kur-enable').dispatch('click'); await flush();
assert.equal(calls.filter(c => c[0] === 'enabled' && c[1]).length, 1);
assert.equal(calls[1][3], 'relay'); assert.equal(calls[1][4], true); assert.deepEqual(calls[1][5], config());
assert.equal(el('kur-frp-token').value, ''); assert.equal(el('kur-frp-ca-cert').value, '');
assert(el('kur-frp-server-ip').disabled); assert(el('kur-mode-lan').disabled);
window.KimiRemoteWidget.close(); window.KimiRemoteWidget.open(); await flush();
assert(el('kur-mode-internet').disabled);
state = ready(); state.connector = {state: 'failed', version: '2026.9.3', error_code: 'CONNECTOR_HASH_MISMATCH'};
resolveStart(state); await flush();
assert.equal(el('kur-link').value, state.url); assert.equal(el('kur-frp-server-ip').value, '8.8.8.8');
assert.equal(el('kur-frp-remote-port').value, '6000'); assert(el('kur-frp-token').disabled);
assert(!el('kur-msg').textContent.includes('完整性')); assert(!el('kur-tunnel-diag').classList.contains('kur-show'));
click('kur-rotate'); await flush(); assert(el('kur-link').value.includes('New_Pair_Token'));
assert.equal(document.activeElement, el('kur-x'));
click('kur-disable'); await flush();
assert.equal(el('kur-frp-token').value, ''); assert.equal(el('kur-frp-ca-cert').value, '');
assert.equal(el('kur-link').value, ''); assert(!el('kur-frp-server-ip').disabled);
click('kur-mode-lan'); assert.equal(storage.get('kur-mode'), 'lan');
click('kur-mode-relay'); assert.equal(storage.get('kur-mode'), 'relay');
fill('kur-frp-token', config().token); fill('kur-frp-ca-cert', certificate);
window.KimiRemoteWidget.close(); assert.equal(el('kur-frp-token').value, ''); assert.equal(el('kur-frp-ca-cert').value, '');
assert.deepEqual([...storage.keys()], ['kur-mode']); assert(!JSON.stringify([...storage.values()]).includes(config().token));
''', dom=True)


if __name__ == '__main__':
    unittest.main()
