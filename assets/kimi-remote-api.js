/**
 * 手机远程控制 · 官方接口适配层（window.KimiRemoteAPI）
 *
 *   GET/POST {base}/api/v1/remote-control
 *   envelope {code, msg, data, request_id}
 *   data = {enabled, state:'off'|'starting'|'on'|'stopping', url?, device_id?, device_name?, error?}
 *
 * 安全约束（勿放宽）：
 * - 基址仅限回环 http/https，无 userinfo/query/hash，路径 "/"（"/v1" 归一化）。
 * - http(s) 浏览器页：API 基址必须等于本页 location.origin，kimi_origin / 会话存储
 *   中的冲突地址一律拒绝（不发请求）；app:// 渲染器走 query → sessionStorage 发现。
 * - 首个请求绝不带凭据；浏览器页仅允许向与本页完全一致的基址回环使用
 *   'kimi-web.server-credential'（{version:1, credential, expiresAt 未过期}）；
 *   app:// 走 Electron 自动注入，永不读取凭据。
 * - 所有对外字符串（错误消息、device_name、error、url）均按当次请求实际使用的
 *   凭据做字面脱敏；凭据不写日志/返回值/持久化；除 401 外不重试，
 *   网络错误与超时不做盲重试；拒绝跟随任何重定向。
 */
(function() {
  'use strict';

  var ORIGIN_PARAM = 'kimi_origin';
  var ORIGIN_STORAGE_KEY = 'kimi-desktop-server-origin';
  var CREDENTIAL_KEY = 'kimi-web.server-credential';
  var API_PATH = '/api/v1/remote-control';
  var DEFAULT_GET_TIMEOUT_MS = 10000;
  var DEFAULT_POST_TIMEOUT_MS = 60000;
  var MIN_TIMEOUT_MS = 500;
  var MAX_TIMEOUT_MS = 60000;
  var MAX_MSG_LEN = 200;
  var MAX_DEVICE_NAME_LEN = 128;
  var VALID_STATES = ['off', 'starting', 'on', 'stopping'];
  var RELAY_HOSTS = { 'code-rc.kimi.com': true, 'code-rc.kimi.ai': true };
  var CRED_KEY_SRC = 'token|secret|password|passwd|pwd|credential|api[-_]?key|auth|authorization|bearer|access[-_]?token|refresh[-_]?token|key|ticket|st';
  var CREDENTIAL_PARAM_SCRUB_RE = new RegExp('([?&](?:' + CRED_KEY_SRC + ')=)[^&#\\s]*', 'gi');
  var BEARER_SCRUB_RE = /(Bearer|Basic)\s+\S+/gi;
  var DEVICE_ID_RE = /^[A-Za-z0-9_-]{1,256}$/;
  var ALLOWED_URL_PARAMS = { 'rc': '1', 'from': 'kimi_code_cli' };
  var hasOwn = Object.prototype.hasOwnProperty;

  var rcErrors = typeof WeakSet === 'function' ? new WeakSet()
    : (typeof Set === 'function' ? new Set() : null);

  function rcErr(message, code) {
    var e = new Error(message);
    e.code = code || 'REMOTE_API_ERROR';
    if (rcErrors) rcErrors.add(e);
    return e;
  }

  function isRcErr(err) {
    return rcErrors !== null && !!err && (typeof err === 'object' || typeof err === 'function')
      && rcErrors.has(err);
  }

  function isLoopbackHost(host) {
    host = String(host || '').toLowerCase();
    if (host === 'localhost' || host.slice(-11) === '.localhost') return true;
    if (host === '[::1]' || host === '::1') return true;
    var m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
    if (m) {
      var a = +m[1], b = +m[2], c = +m[3], d = +m[4];
      if (a <= 255 && b <= 255 && c <= 255 && d <= 255 && a === 127) return true;
    }
    return false;
  }

  function normalizeBase(raw, source) {
    if (typeof raw !== 'string' || raw.trim() === '') {
      throw rcErr(source + ' 提供的服务地址无效（空值），请重新打开面板', 'REMOTE_BASE_INVALID');
    }
    var u;
    try { u = new URL(raw); } catch (e) {
      throw rcErr(source + ' 提供的服务地址无法解析，请重新打开面板', 'REMOTE_BASE_INVALID');
    }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') {
      throw rcErr('服务地址协议不受支持，仅允许 http/https', 'REMOTE_BASE_REJECTED');
    }
    if (u.username !== '' || u.password !== '') {
      throw rcErr('服务地址不允许携带用户凭证', 'REMOTE_BASE_REJECTED');
    }
    if (!isLoopbackHost(u.hostname)) {
      throw rcErr('服务地址必须是本机回环地址（127.0.0.1/localhost/::1）', 'REMOTE_BASE_REJECTED');
    }
    var path = u.pathname;
    if (path === '/v1' || path === '/v1/') path = '/';
    if (path !== '/' && path !== '') {
      throw rcErr('服务地址不允许携带额外路径', 'REMOTE_BASE_REJECTED');
    }
    if (u.search !== '' || u.hash !== '') {
      throw rcErr('服务地址不允许携带查询参数或锚点', 'REMOTE_BASE_REJECTED');
    }
    return u.protocol + '//' + u.host;
  }

  function overrideBase(raw, source, pageBase) {
    var o = normalizeBase(raw, source);
    if (o !== pageBase) {
      throw rcErr('服务地址与本页来源不一致，已阻止跨来源调用', 'REMOTE_BASE_REJECTED');
    }
    return o;
  }

  function discoverBase(win) {
    var search = '';
    try { search = win.location && win.location.search; } catch (e) { /* keep '' */ }
    var qv = null;
    if (typeof search === 'string' && search.length > 1) {
      try { qv = new URLSearchParams(search).get(ORIGIN_PARAM); } catch (e) {
        throw rcErr('页面参数 kimi_origin 校验失败', 'REMOTE_BASE_INVALID');
      }
    }
    var sv = null, svSeen = false;
    try {
      var ss = win.sessionStorage;
      if (ss && typeof ss.getItem === 'function') {
        sv = ss.getItem(ORIGIN_STORAGE_KEY);
        svSeen = sv !== null && sv !== undefined;
      }
    } catch (e) {
      if (e && e.isRemoteApiError) throw e;
    }
    return { query: qv, session: svSeen ? sv : null };
  }

  // 返回 {base, allowCredential}；allowCredential 仅当页面自身为 http(s) 回环源。
  function resolveBase(win) {
    var proto = '';
    try { proto = String(win.location && win.location.protocol || '').toLowerCase(); } catch (e) { /* keep '' */ }
    var isWebPage = proto === 'http:' || proto === 'https:';
    var found = discoverBase(win);
    if (isWebPage) {
      var pageBase = normalizeBase(win.location.origin || '', '页面来源');
      if (found.query !== null) return { base: overrideBase(found.query, '页面参数 kimi_origin', pageBase), allowCredential: true };
      if (found.session !== null) return { base: overrideBase(found.session, '会话存储的服务地址', pageBase), allowCredential: true };
      return { base: pageBase, allowCredential: true };
    }
    if (found.query !== null) return { base: normalizeBase(found.query, '页面参数 kimi_origin'), allowCredential: false };
    if (found.session !== null) return { base: normalizeBase(found.session, '会话存储的服务地址'), allowCredential: false };
    return { base: normalizeBase(win.location && win.location.origin || '', '页面来源'), allowCredential: false };
  }

  function parseCredential(raw) {
    if (typeof raw !== 'string' || raw === '') return null;
    var obj;
    try { obj = JSON.parse(raw); } catch (e) { return null; }
    if (!obj || typeof obj !== 'object') return null;
    if (obj.version !== 1) return null;
    if (typeof obj.credential !== 'string' || obj.credential.trim() === '') return null;
    if (typeof obj.expiresAt !== 'number' || !isFinite(obj.expiresAt)) return null;
    if (obj.expiresAt <= Date.now()) return null;
    return obj.credential;
  }

  function readStoredCredential(win) {
    var sources = [];
    try { if (win.sessionStorage) sources.push(win.sessionStorage); } catch (e) { /* skip */ }
    try { if (win.localStorage) sources.push(win.localStorage); } catch (e) { /* skip */ }
    for (var i = 0; i < sources.length; i++) {
      var raw;
      try { raw = sources[i].getItem(CREDENTIAL_KEY); } catch (e) { continue; }
      var cred = parseCredential(raw);
      if (cred !== null) return cred;
    }
    return null;
  }

  // 对服务端返回/异常上抛的字符串统一脱敏：通用凭据模式 + 当次使用的具体凭据
  // 的字面量与其 percent 编码形式（先容忍式解码再匹配）。
  function scrubMsg(msg, secret) {
    var s = String(msg === undefined || msg === null ? '' : msg);
    if (typeof secret === 'string' && secret !== '') {
      s = s.split(secret).join('***');
      try {
        var enc = encodeURIComponent(secret);
        if (enc !== secret) s = s.split(enc).join('***');
      } catch (e) { /* keep */ }
      var dec = tolerantDecode(s);
      if (dec !== s) s = dec.split(secret).join('***');
    }
    s = s.replace(BEARER_SCRUB_RE, '$1 ***');
    s = s.replace(CREDENTIAL_PARAM_SCRUB_RE, '$1***');
    if (s.length > MAX_MSG_LEN) s = s.slice(0, MAX_MSG_LEN) + '…';
    return s.trim();
  }

  // 容忍式 percent 解码：非法序列原样保留；用于先还原编码凭据再脱敏。
  function tolerantDecode(s) {
    return String(s).replace(/%[0-9A-Fa-f]{2}/g, function(h) {
      return String.fromCharCode(parseInt(h.slice(1), 16));
    });
  }

  function hasSecretForm(s, secret) {
    if (typeof secret !== 'string' || secret === '') return false;
    s = String(s);
    if (s.indexOf(secret) >= 0) return true;
    try {
      var enc = encodeURIComponent(secret);
      if (enc !== secret && s.indexOf(enc) >= 0) return true;
    } catch (e) { /* keep */ }
    if (tolerantDecode(s).indexOf(secret) >= 0) return true;
    return false;
  }

  function isObj(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }

  function parseEnvelope(bodyText) {
    var body;
    try { body = JSON.parse(bodyText); } catch (e) {
      throw rcErr('服务返回内容不是合法 JSON', 'REMOTE_BAD_RESPONSE');
    }
    if (!isObj(body) || typeof body.code !== 'number'
        || typeof body.msg !== 'string' || typeof body.request_id !== 'string') {
      throw rcErr('服务返回包络格式异常', 'REMOTE_BAD_RESPONSE');
    }
    return body;
  }

  function toStatus(env, secret) {
    if (env.code !== 0) {
      var m = scrubMsg(env.msg, secret) || '未知错误';
      throw rcErr('远程控制操作失败：' + m, 'REMOTE_API_ERROR');
    }
    var d = env.data;
    if (!isObj(d) || typeof d.enabled !== 'boolean' || VALID_STATES.indexOf(d.state) < 0) {
      throw rcErr('服务返回的远程控制状态格式异常', 'REMOTE_BAD_RESPONSE');
    }
    var status = { enabled: d.enabled, state: d.state };
    if (typeof d.url === 'string' && d.url !== '') {
      // 含本次凭据（或其编码形式）的链接绝不回传，避免把秘密改写进可分享 URL。
      if (!hasSecretForm(d.url, secret)) {
        try { status.url = validateRemoteURL(d.url); } catch (e) { /* 不可信链接不下发 */ }
      }
    }
    if (typeof d.device_id === 'string' && d.device_id !== '') {
      var did = scrubMsg(d.device_id, secret);
      if (DEVICE_ID_RE.test(did)) status.device_id = did;
    }
    if (typeof d.device_name === 'string' && d.device_name !== '') {
      var dn = scrubMsg(d.device_name, secret);
      if (dn.length > MAX_DEVICE_NAME_LEN) dn = dn.slice(0, MAX_DEVICE_NAME_LEN) + '…';
      if (dn !== '') status.device_name = dn;
    }
    if (typeof d.error === 'string' && d.error !== '') status.error = scrubMsg(d.error, secret);
    return status;
  }

  function validateRemoteURL(text) {
    if (typeof text !== 'string' || text.trim() === '') {
      throw rcErr('远程链接为空', 'REMOTE_URL_REJECTED');
    }
    var u;
    try { u = new URL(text.trim()); } catch (e) {
      throw rcErr('远程链接无法解析', 'REMOTE_URL_REJECTED');
    }
    if (u.protocol !== 'https:') {
      throw rcErr('远程链接必须使用 https', 'REMOTE_URL_REJECTED');
    }
    if (u.username !== '' || u.password !== '') {
      throw rcErr('远程链接不允许携带用户凭证', 'REMOTE_URL_REJECTED');
    }
    if (u.hash !== '') {
      throw rcErr('远程链接不允许携带锚点', 'REMOTE_URL_REJECTED');
    }
    if (u.port !== '') {
      throw rcErr('远程链接不允许自定义端口', 'REMOTE_URL_REJECTED');
    }
    if (u.hostname !== u.hostname.toLowerCase() || !hasOwn.call(RELAY_HOSTS, u.hostname)) {
      throw rcErr('远程链接域名不受信任（仅支持官方中继 code-rc.kimi.com / code-rc.kimi.ai）',
        'REMOTE_URL_REJECTED');
    }
    var segs = u.pathname.split('/');
    var pathOk = segs[0] === '' && segs[1] === 'devices' && segs[2] !== ''
      && (segs.length === 3 || (segs.length === 4 && segs[3] === ''));
    if (!pathOk) {
      throw rcErr('远程链接路径不符合官方格式 /devices/<设备ID>/', 'REMOTE_URL_REJECTED');
    }
    if (!DEVICE_ID_RE.test(segs[2])) {
      throw rcErr('远程链接中的设备 ID 格式不合法', 'REMOTE_URL_REJECTED');
    }
    // 仅允许官方链接的固定查询参数（rc=1, from=kimi_code_cli，各至多一次），
    // 其余任意名称一律拒绝——未知参数可能携带编码凭据。
    var params;
    try { params = new URLSearchParams(u.search); } catch (e) {
      throw rcErr('远程链接查询参数无法解析', 'REMOTE_URL_REJECTED');
    }
    var seen = {};
    var badParam = false;
    params.forEach(function(value, name) {
      var expect = ALLOWED_URL_PARAMS[name];
      if (expect === undefined || expect !== value || seen[name]) badParam = true;
      seen[name] = true;
    });
    if (badParam) {
      throw rcErr('远程链接查询参数不符合官方格式', 'REMOTE_URL_REJECTED');
    }
    return u.toString();
  }

  function doFetch(fetchImpl, url, init, timeoutMs, label) {
    return new Promise(function(resolve, reject) {
      var ctrl = new AbortController();
      var timedOut = false;
      var finished = false;
      var timer = setTimeout(function() {
        timedOut = true;
        try { ctrl.abort(); } catch (e) { /* fetch 可能忽略信号，race 仍保证超时 */ }
        finished = true;
        reject(rcErr(label + '请求超时（' + timeoutMs + 'ms），请检查 Kimi Code 是否正常运行', 'REMOTE_TIMEOUT'));
      }, timeoutMs);
      init.signal = ctrl.signal;
      init.redirect = 'error';

      function fail(err) {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        if (timedOut || (err && (err.name === 'AbortError' || err.name === 'TimeoutError'))) {
          reject(rcErr(label + '请求超时（' + timeoutMs + 'ms），请检查 Kimi Code 是否正常运行', 'REMOTE_TIMEOUT'));
        } else if (isRcErr(err)) {
          reject(err);
        } else {
          reject(rcErr(label + '网络请求失败，请确认 Kimi Code 服务已启动后重试', 'REMOTE_NETWORK'));
        }
      }
      function done(out) {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        resolve(out);
      }

      var promise;
      try {
        promise = fetchImpl(url, init);
      } catch (e) { fail(e); return; }
      if (!promise || typeof promise.then !== 'function') {
        fail(rcErr('fetch 实现未返回 Promise', 'REMOTE_NETWORK'));
        return;
      }
      promise.then(function(res) {
        if (res && res.redirected === true) {
          fail(rcErr('远程控制接口不允许重定向', 'REMOTE_REDIRECT'));
          return;
        }
        if (res && typeof res.url === 'string' && res.url !== '' && res.url !== url) {
          fail(rcErr('响应地址与请求地址不一致，已丢弃', 'REMOTE_REDIRECT'));
          return;
        }
        var status = res && typeof res.status === 'number' ? res.status : 0;
        var textP;
        try {
          textP = (res && typeof res.text === 'function')
            ? res.text()
            : Promise.reject(rcErr('响应对象不支持读取内容', 'REMOTE_BAD_RESPONSE'));
        } catch (e) { fail(e); return; }
        Promise.resolve(textP).then(function(text) {
          done({ http: status, body: text });
        }, fail);
      }, fail);
    });
  }

  function request(win, fetchImpl, timeoutMs, base, method, body, label, allowCredential) {
    var url = base + API_PATH;
    var secret = null;
    var init = {
      method: method,
      headers: { 'Accept': 'application/json' },
      cache: 'no-store',
      redirect: 'error'
    };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    return doFetch(fetchImpl, url, init, timeoutMs, label).then(function(out) {
      if (out.http === 401) {
        if (!allowCredential) {
          throw rcErr('未授权访问（401），请重新登录 Kimi Code 后再试', 'REMOTE_AUTH');
        }
        secret = readStoredCredential(win);
        if (!secret) {
          throw rcErr('未授权访问（401），请先在 Kimi Code 中登录或重新打开本面板', 'REMOTE_AUTH');
        }
        var init2 = {
          method: method,
          headers: {
            'Accept': 'application/json',
            'Authorization': 'Bearer ' + secret
          },
          cache: 'no-store',
          redirect: 'error'
        };
        if (body !== undefined) {
          init2.headers['Content-Type'] = 'application/json';
          init2.body = JSON.stringify(body);
        }
        return doFetch(fetchImpl, url, init2, timeoutMs, label).then(function(out2) {
          if (out2.http === 401) {
            throw rcErr('凭据已失效，请重新登录 Kimi Code 后再试', 'REMOTE_AUTH');
          }
          return out2;
        });
      }
      return out;
    }).then(function(out) {
      if (out.http === 404) {
        throw rcErr('当前 Kimi Code 版本不支持远程控制接口，请升级后重试', 'REMOTE_UNSUPPORTED');
      }
      if (out.http >= 500) {
        throw rcErr('服务内部错误（HTTP ' + out.http + '），请稍后重试', 'REMOTE_HTTP');
      }
      if (out.http < 200 || out.http >= 300) {
        var detail = '';
        try { detail = scrubMsg(JSON.parse(out.body).msg, secret); } catch (e) { /* keep '' */ }
        throw rcErr(label + '请求被拒绝（HTTP ' + out.http + '）' +
          (detail ? '：' + detail : ''), 'REMOTE_HTTP');
      }
      return toStatus(parseEnvelope(out.body), secret);
    });
  }

  function create(opts) {
    opts = opts || {};
    var win = opts.window || (typeof window !== 'undefined' ? window : undefined);
    var fetchImpl = opts.fetch || (typeof fetch === 'function' ? fetch : undefined);
    if (!win || !win.location) {
      throw rcErr('当前环境缺少 window.location，无法定位 Kimi Code 服务', 'REMOTE_ENV');
    }
    if (typeof fetchImpl !== 'function') {
      throw rcErr('当前环境不支持 fetch，无法调用远程控制接口', 'REMOTE_ENV');
    }
    var override = opts.timeoutMs;
    var hasOverride = typeof override === 'number' && isFinite(override);
    var clamp = function(ms) {
      if (ms < MIN_TIMEOUT_MS) return MIN_TIMEOUT_MS;
      if (ms > MAX_TIMEOUT_MS) return MAX_TIMEOUT_MS;
      return ms;
    };
    var getTimeoutMs = clamp(hasOverride ? override : DEFAULT_GET_TIMEOUT_MS);
    var postTimeoutMs = clamp(hasOverride ? override : DEFAULT_POST_TIMEOUT_MS);

    var cached = null;
    function resolved() {
      if (cached === null) cached = resolveBase(win);
      return cached;
    }

    function call(method, body, label, timeoutMs) {
      var r;
      try { r = resolved(); } catch (e) { return Promise.reject(e); }
      return request(win, fetchImpl, timeoutMs, r.base, method, body, label, r.allowCredential);
    }

    return {
      status: function() {
        return call('GET', undefined, '查询远程控制状态', getTimeoutMs);
      },
      setEnabled: function(enabled) {
        if (typeof enabled !== 'boolean') {
          return Promise.reject(rcErr('setEnabled 参数必须是布尔值', 'REMOTE_BAD_ARG'));
        }
        return call('POST', { enabled: enabled }, '设置远程控制', postTimeoutMs);
      }
    };
  }

  var api = {
    create: create,
    validateRemoteURL: validateRemoteURL
  };

  var root = typeof globalThis !== 'undefined' ? globalThis : this;
  root.KimiRemoteAPI = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
