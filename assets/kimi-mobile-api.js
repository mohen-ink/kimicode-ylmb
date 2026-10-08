/**
 * 手机连接控制面适配层（window.KimiMobileAPI）。
 * 控制面与 owner 仅信任本机回环；公网源绝不成为控制源。
 * LAN 配对严格绑定地址/端口，Internet 配对严格绑定当前 ready tunnel 的公网源。
 * 所有 POST 均不重试，响应仅下发已校验字段与有限安全错误文案。
 */
(function() {
  'use strict';

  var CONTROL_BASE = 'http://127.0.0.1:39281';
  var STATUS_PATH = '/api/mobile/status';
  var START_PATH = '/api/mobile/start';
  var STOP_PATH = '/api/mobile/stop';
  var CONTROL_HEADER = 'X-Kimi-Mobile-Control';
  var ORIGIN_PARAM = 'kimi_origin';
  var ORIGIN_STORAGE_KEY = 'kimi-desktop-server-origin';
  var DEFAULT_GET_TIMEOUT_MS = 10000;
  var DEFAULT_POST_TIMEOUT_MS = 30000;
  var DEFAULT_START_TIMEOUT_MS = 95000;
  var DEFAULT_INSTALL_TIMEOUT_MS = 245000;
  var MIN_TIMEOUT_MS = 500;
  var MAX_TIMEOUT_MS = 300000;
  var MAX_DEVICE_COUNT = 10000;
  var VALID_STATES = ['off', 'starting', 'on', 'stopping'];
  var MODES = ['lan', 'internet'];
  var CONNECTOR_STATES = ['missing', 'installing', 'installed', 'failed'];
  var TUNNEL_STATES = ['off', 'starting', 'ready', 'failed'];
  var PAIR_STATES = ['missing', 'available', 'used', 'expired'];
  var CONNECTOR_VERSION = '2026.9.3';
  var CONSENT_VERSION = 'cloudflare-quick-2026-09-v1';
  var INSTALL_PATH = '/api/mobile/connector/install';
  var PAIR_ROTATE_PATH = '/api/mobile/pair/rotate';
  var IPV4_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
  var PAIR_PATH = '/mobile/pair';
  var PAIR_TOKEN_RE = /^[A-Za-z0-9_-]{8,256}$/;
  var PUBLIC_ORIGIN_RE = /^https:\/\/[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com$/;
  var ERROR_MESSAGES = {
    CONNECTOR_MISSING: '请先安装外网连接组件。',
    CONNECTOR_INSTALL_FAILED: '连接组件安装失败，请重新确认后再试。',
    CONNECTOR_HASH_MISMATCH: '连接组件完整性校验失败，无法使用。',
    CONNECTOR_UNSUPPORTED: '当前系统不支持此外网连接组件。',
    CONNECTOR_BUSY: '连接组件正在处理其他操作，请稍候。',
    CONSENT_REQUIRED: '请分别明确同意组件下载和本次 Cloudflare 中转。',
    TUNNEL_START_FAILED: '外网通道启动失败，请停止后再试。',
    TUNNEL_TIMEOUT: '外网通道启动超时，请检查网络后重试。',
    TUNNEL_EXITED: '外网通道已断开，请停止后重新开启。',
    OWNER_LOST: '桌面端服务已断开，连接已撤销。',
    START_CANCELLED: '连接启动已取消。',
    WORKER_STATE_DIR_UNAVAILABLE: '手机连接助手工作目录不可用，请检查插件安装。',
    WORKER_STARTUP_BUSY: '有其他启动操作正在进行，请稍候再试。',
    WORKER_LOCK_HELD: '已有手机连接实例在运行但身份无法核实，请稍候再试。',
    WORKER_SPAWN_DENIED: '系统拒绝以独立进程启动手机连接助手。',
    WORKER_SPAWN_FAILED: '无法启动手机连接独立进程，请稍后重试。',
    WORKER_CHILD_EXITED: '手机连接独立进程启动后立即退出，请稍后重试。',
    WORKER_BOOT_TIMEOUT: '手机连接独立进程启动超时，请稍后重试。',
    WORKER_VERSION_MISMATCH: '手机连接助手版本与当前插件不一致，请更新后重试。'
  };

  var apiErrors = typeof WeakSet === 'function' ? new WeakSet()
    : (typeof Set === 'function' ? new Set() : null);

  function apiErr(message, code) {
    var e = new Error(message);
    e.code = code || 'MOBILE_API_ERROR';
    if (apiErrors) apiErrors.add(e);
    return e;
  }

  function isApiErr(err) {
    return apiErrors !== null && !!err && (typeof err === 'object' || typeof err === 'function')
      && apiErrors.has(err);
  }

  function parseIPv4(host) {
    var m = IPV4_RE.exec(String(host || '').toLowerCase());
    if (!m) return null;
    var p = [+m[1], +m[2], +m[3], +m[4]];
    for (var i = 0; i < 4; i++) {
      if (!(p[i] >= 0 && p[i] <= 255)) return null;
    }
    return p;
  }

  function isLoopbackHost(host) {
    host = String(host || '').toLowerCase();
    if (host === 'localhost' || host.slice(-11) === '.localhost') return true;
    if (host === '[::1]' || host === '::1') return true;
    var p = parseIPv4(host);
    return !!p && p[0] === 127;
  }

  // RFC1918 私网 IPv4：10.0.0.0/8、172.16.0.0/12、192.168.0.0/16
  function isRFC1918(host) {
    var p = parseIPv4(host);
    if (!p) return false;
    if (p[0] === 10) return true;
    if (p[0] === 172 && p[1] >= 16 && p[1] <= 31) return true;
    if (p[0] === 192 && p[1] === 168) return true;
    return false;
  }

  function isObj(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }

  function errorMessage(code) {
    return typeof code === 'string' && Object.prototype.hasOwnProperty.call(ERROR_MESSAGES, code)
      ? ERROR_MESSAGES[code] : '';
  }

  function badStatus() {
    throw apiErr('服务返回的手机连接状态格式异常', 'MOBILE_BAD_RESPONSE');
  }

  function validPublicOrigin(origin) {
    return typeof origin === 'string' && PUBLIC_ORIGIN_RE.test(origin) && !/\s/.test(origin);
  }

  // owner_origin：http + 回环 host，无 userinfo/路径/查询/锚点
  function normalizeOwnerOrigin(raw, source) {
    if (typeof raw !== 'string' || raw.trim() === '') {
      throw apiErr(source + ' 提供的桌面端服务地址无效（空值），请重新打开面板', 'MOBILE_OWNER_INVALID');
    }
    var u;
    try { u = new URL(raw); } catch (e) {
      throw apiErr(source + ' 提供的桌面端服务地址无法解析，请重新打开面板', 'MOBILE_OWNER_INVALID');
    }
    if (u.protocol !== 'http:') {
      // 与 backend _parse_owner_origin 一致：owner 桌面端服务恒为本机 http 回环，
      // https://127.0.0.1:* 永远不可能是合法 owner，拒绝比放行更安全
      throw apiErr('桌面端服务地址协议不受支持，仅允许 http', 'MOBILE_OWNER_REJECTED');
    }
    if (u.username !== '' || u.password !== '') {
      throw apiErr('桌面端服务地址不允许携带用户凭证', 'MOBILE_OWNER_REJECTED');
    }
    if (!isLoopbackHost(u.hostname)) {
      throw apiErr('桌面端服务地址必须是本机回环地址（127.0.0.1/localhost/::1）', 'MOBILE_OWNER_REJECTED');
    }
    var path = u.pathname;
    if (path !== '/' && path !== '') {
      throw apiErr('桌面端服务地址不允许携带额外路径', 'MOBILE_OWNER_REJECTED');
    }
    if (u.search !== '' || u.hash !== '') {
      throw apiErr('桌面端服务地址不允许携带查询参数或锚点', 'MOBILE_OWNER_REJECTED');
    }
    return u.protocol + '//' + u.host;
  }

  function discoverOwner(win) {
    var search = '';
    try { search = win.location && win.location.search; } catch (e) { /* keep '' */ }
    var qv = null;
    if (typeof search === 'string' && search.length > 1) {
      try { qv = new URLSearchParams(search).get(ORIGIN_PARAM); } catch (e) {
        throw apiErr('页面参数 ' + ORIGIN_PARAM + ' 校验失败', 'MOBILE_OWNER_INVALID');
      }
    }
    var sv = null, svSeen = false;
    try {
      var ss = win.sessionStorage;
      if (ss && typeof ss.getItem === 'function') {
        sv = ss.getItem(ORIGIN_STORAGE_KEY);
        svSeen = sv !== null && sv !== undefined;
      }
    } catch (e) { /* 存储不可读视为未发现 */ }
    return { query: qv, session: svSeen ? sv : null };
  }

  // 返回当前桌面的 owner origin（http 回环）。
  // http 页面：仅允许与本页完全一致的回环源（原生页面），冲突地址一律拒绝；
  // https 页面同样无法过 normalizeOwnerOrigin 的 http-only 校验（页面即被拒）。
  // app:// 等非 http 渲染器：query → sessionStorage 精确发现，缺发现即拒绝。
  function resolveOwner(win) {
    var proto = '';
    try { proto = String(win.location && win.location.protocol || '').toLowerCase(); } catch (e) { /* keep '' */ }
    var found = discoverOwner(win);
    if (proto === 'http:' || proto === 'https:') {
      var page = normalizeOwnerOrigin(win.location.origin || '', '页面来源');
      if (found.query !== null) {
        var oq = normalizeOwnerOrigin(found.query, '页面参数 ' + ORIGIN_PARAM);
        if (oq !== page) {
          throw apiErr('服务地址与本页来源不一致，已阻止跨来源调用', 'MOBILE_OWNER_REJECTED');
        }
      }
      if (found.session !== null) {
        var os = normalizeOwnerOrigin(found.session, '会话存储的服务地址');
        if (os !== page) {
          throw apiErr('服务地址与本页来源不一致，已阻止跨来源调用', 'MOBILE_OWNER_REJECTED');
        }
      }
      return page;
    }
    if (found.query !== null) return normalizeOwnerOrigin(found.query, '页面参数 ' + ORIGIN_PARAM);
    if (found.session !== null) return normalizeOwnerOrigin(found.session, '会话存储的服务地址');
    throw apiErr('无法确认当前桌面端服务地址，请从 Kimi Code 桌面端重新打开本面板', 'MOBILE_ENV');
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
        reject(apiErr(label + '请求超时（' + timeoutMs + 'ms），请检查插件服务是否正常运行', 'MOBILE_TIMEOUT'));
      }, timeoutMs);
      init.signal = ctrl.signal;
      init.redirect = 'error';
      init.credentials = 'omit';

      function fail(err) {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        if (timedOut || (err && (err.name === 'AbortError' || err.name === 'TimeoutError'))) {
          reject(apiErr(label + '请求超时（' + timeoutMs + 'ms），请检查插件服务是否正常运行', 'MOBILE_TIMEOUT'));
        } else if (isApiErr(err)) {
          reject(err);
        } else {
          reject(apiErr(label + '网络请求失败，请确认插件服务已启动后重试', 'MOBILE_NETWORK'));
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
        fail(apiErr('fetch 实现未返回 Promise', 'MOBILE_NETWORK'));
        return;
      }
      promise.then(function(res) {
        if (res && res.redirected === true) {
          fail(apiErr('局域网连接接口不允许重定向', 'MOBILE_REDIRECT'));
          return;
        }
        if (res && typeof res.url === 'string' && res.url !== '' && res.url !== url) {
          fail(apiErr('响应地址与请求地址不一致，已丢弃', 'MOBILE_REDIRECT'));
          return;
        }
        var status = res && typeof res.status === 'number' ? res.status : 0;
        var textP;
        try {
          textP = (res && typeof res.text === 'function')
            ? res.text()
            : Promise.reject(apiErr('响应对象不支持读取内容', 'MOBILE_BAD_RESPONSE'));
        } catch (e) { fail(e); return; }
        Promise.resolve(textP).then(function(text) {
          done({ http: status, body: text });
        }, fail);
      }, fail);
    });
  }

  function isPort(n) {
    return typeof n === 'number' && isFinite(n)
      && n >= 1 && n <= 65535 && Math.floor(n) === n;
  }

  function validateRemoteURL(text, status) {
    var reject = function() { throw apiErr('配对链接格式或状态绑定不受信任', 'MOBILE_URL_REJECTED'); };
    if (typeof text !== 'string' || text === '' || text !== text.trim()) return reject();
    var u;
    try { u = new URL(text); } catch (e) { return reject(); }
    if (u.username || u.password || u.pathname !== PAIR_PATH || u.search
        || u.hash.slice(0, 6) !== '#pair=' || !PAIR_TOKEN_RE.test(u.hash.slice(6))) return reject();
    if (isObj(status) && status.mode === 'internet') {
      if (status.enabled !== true || status.state !== 'on' || !isObj(status.tunnel)
          || status.tunnel.state !== 'ready' || !validPublicOrigin(status.public_origin)
          || u.protocol !== 'https:' || u.port || u.origin !== status.public_origin
          || text !== status.public_origin + PAIR_PATH + u.hash) return reject();
    } else {
      if (u.protocol !== 'http:' || !isRFC1918(u.hostname) || !u.port
          || text !== u.origin + PAIR_PATH + u.hash) return reject();
      if (status !== undefined && status !== null) {
        if (!isObj(status) || (status.mode !== undefined && status.mode !== 'lan')
            || !isRFC1918(status.address) || !isPort(status.port)
            || u.hostname !== status.address || u.port !== String(status.port)) return reject();
      }
    }
    if (isObj(status)) {
      if (status.pair_state !== undefined && status.pair_state !== 'available') return reject();
      if (status.expires_at !== undefined) {
        var ms = status.expires_at > 1e12 ? status.expires_at : status.expires_at * 1000;
        if (typeof status.expires_at !== 'number' || !isFinite(ms) || ms <= Date.now()) return reject();
      }
    }
    return text;
  }

  // 隧道诊断（可选）：只投影固定枚举/有界计数，单项非法则整体省略该诊断，
  // 绝不因诊断问题拒绝整个状态，也绝不原样透传（原始行/IP/URL/凭据不可进入）。
  var DIAG_COUNT_KEYS = ['connection_registered', 'connection_unregistered',
    'connection_retrying', 'origin_request_failed'];
  var DIAG_TRANSPORT_STATES = ['unknown', 'ready', 'reconnecting'];
  var DIAG_RING_MAX = 8;

  function diagEvent(e) {
    if (!isObj(e) || DIAG_COUNT_KEYS.indexOf(e.kind) < 0) return null;
    if (typeof e.time !== 'number' || !isFinite(e.time) || e.time < 0) return null;
    return { kind: e.kind, time: e.time };
  }

  function toDiagnostics(dg) {
    if (!isObj(dg) || !isObj(dg.counts)) return null;
    var counts = {};
    for (var i = 0; i < DIAG_COUNT_KEYS.length; i++) {
      var v = dg.counts[DIAG_COUNT_KEYS[i]];
      if (typeof v !== 'number' || !isFinite(v) || v < 0 || v > 65535
          || Math.floor(v) !== v) return null;
      counts[DIAG_COUNT_KEYS[i]] = v;
    }
    if (DIAG_TRANSPORT_STATES.indexOf(dg.transport_state) < 0) return null;
    if (!Array.isArray(dg.active_conn_indices) || dg.active_conn_indices.length > 4) return null;
    var indices = [];
    for (var j = 0; j < dg.active_conn_indices.length; j++) {
      var idx = dg.active_conn_indices[j];
      if (typeof idx !== 'number' || !isFinite(idx) || Math.floor(idx) !== idx
          || idx < 0 || idx > 3 || indices.indexOf(idx) >= 0) return null;
      indices.push(idx);
    }
    if (typeof dg.active_conn_count !== 'number' || !isFinite(dg.active_conn_count)
        || Math.floor(dg.active_conn_count) !== dg.active_conn_count
        || dg.active_conn_count !== indices.length) return null;
    var last = null;
    if (dg.last_event !== null && dg.last_event !== undefined) {
      last = diagEvent(dg.last_event);
      if (last === null) return null;
    }
    if (!Array.isArray(dg.recent_events)) return null;
    var ring = [];
    for (var k = 0; k < dg.recent_events.length; k++) {
      var ev = diagEvent(dg.recent_events[k]);
      if (ev === null) return null;
      ring.push(ev);
    }
    if (ring.length > DIAG_RING_MAX) ring = ring.slice(ring.length - DIAG_RING_MAX);
    return {
      counts: counts,
      transport_state: dg.transport_state,
      active_conn_indices: indices,
      active_conn_count: dg.active_conn_count,
      last_event: last,
      recent_events: ring
    };
  }

  function toStatus(d) {
    if (!isObj(d) || typeof d.enabled !== 'boolean' || VALID_STATES.indexOf(d.state) < 0
        || MODES.indexOf(d.mode) < 0 || typeof d.device_count !== 'number'
        || !isFinite(d.device_count) || d.device_count < 0 || d.device_count > MAX_DEVICE_COUNT
        || Math.floor(d.device_count) !== d.device_count
        || (d.state === 'off' && d.enabled) || (d.state === 'on' && !d.enabled)) badStatus();
    if (!isObj(d.connector) || CONNECTOR_STATES.indexOf(d.connector.state) < 0
        || d.connector.version !== CONNECTOR_VERSION || !isObj(d.tunnel)
        || TUNNEL_STATES.indexOf(d.tunnel.state) < 0) badStatus();
    var status = {
      enabled: d.enabled, state: d.state, mode: d.mode, device_count: d.device_count,
      connector: { state: d.connector.state, version: CONNECTOR_VERSION },
      tunnel: { state: d.tunnel.state }
    };
    var sources = [d, d.connector, d.tunnel];
    var targets = [status, status.connector, status.tunnel];
    for (var k = 0; k < sources.length; k++) {
      if (sources[k].error_code !== undefined) {
        if (!errorMessage(sources[k].error_code)) badStatus();
        targets[k].error_code = sources[k].error_code;
        status.error = errorMessage(sources[k].error_code);
      }
    }
    if (d.owner_origin !== undefined && d.owner_origin !== '') {
      try { status.owner_origin = normalizeOwnerOrigin(d.owner_origin, '服务返回的'); }
      catch (e) { badStatus(); }
    }
    if (d.addresses !== undefined) {
      if (!Array.isArray(d.addresses) || d.addresses.length > 256) badStatus();
      status.addresses = [];
      for (var i = 0; i < d.addresses.length; i++) {
        if (typeof d.addresses[i] !== 'string' || !isRFC1918(d.addresses[i])) badStatus();
        if (status.addresses.indexOf(d.addresses[i]) < 0) status.addresses.push(d.addresses[i]);
      }
    }
    if (d.address !== undefined && d.address !== '') {
      if (typeof d.address !== 'string' || !isRFC1918(d.address)) badStatus();
      status.address = d.address;
    }
    if (d.port !== undefined && d.port !== 0) {
      if (!isPort(d.port)) badStatus();
      status.port = d.port;
    }
    if (d.pair_state !== undefined) {
      if (PAIR_STATES.indexOf(d.pair_state) < 0) badStatus();
      status.pair_state = d.pair_state;
    }
    if (d.public_origin !== undefined) {
      if (d.mode !== 'internet' || !d.enabled || d.state !== 'on'
          || d.tunnel.state !== 'ready' || !validPublicOrigin(d.public_origin)) badStatus();
      status.public_origin = d.public_origin;
    }
    if (d.expires_at !== undefined) {
      if (typeof d.expires_at !== 'number' || !isFinite(d.expires_at) || d.expires_at <= 0) badStatus();
    }
    if (d.url !== undefined) {
      if (typeof d.url !== 'string' || !d.url || !d.enabled || d.state !== 'on'
          || d.expires_at === undefined
          || (d.mode === 'internet' && (d.tunnel.state !== 'ready' || !status.public_origin))
          || (d.pair_state !== undefined && d.pair_state !== 'available')) badStatus();
      var ms = d.expires_at > 1e12 ? d.expires_at : d.expires_at * 1000;
      var checked;
      try { checked = validateRemoteURL(d.url, status); } catch (e) { badStatus(); }
      if (ms > Date.now()) {
        status.url = checked;
        status.expires_at = d.expires_at;
      } else {
        status.pair_state = 'expired';
      }
    } else if (d.expires_at !== undefined) {
      badStatus();
    }
    // 服务侧可选提示（如助手版本过旧/已崩溃）：仅放行有界纯文本，
    // 拒绝 URL/路径/配对串/控制字符，绝不成为链接、脚本或凭据载体
    if (d.worker_notice !== undefined) {
      if (typeof d.worker_notice !== 'string' || d.worker_notice.length === 0
          || d.worker_notice.length > 200 || /[\r\n\t\0]/.test(d.worker_notice)
          || /(https?:\/\/|\/[A-Za-z0-9_.-]|[A-Za-z]:\\|\\\\)/.test(d.worker_notice)
          || /#[A-Za-z0-9_-]{4,}/.test(d.worker_notice)
          || /^[A-Za-z0-9_-]{8,256}$/.test(d.worker_notice)) badStatus();
      status.worker_notice = d.worker_notice;
    }
    if (d.connector_diagnostics !== undefined && d.connector_diagnostics !== null) {
      var projected = toDiagnostics(d.connector_diagnostics);
      if (projected !== null) status.connector_diagnostics = projected;
    }
    return status;
  }

  function call(fetchImpl, timeoutMs, method, path, body, label) {
    var url = CONTROL_BASE + path;
    var init = {
      method: method,
      headers: { 'Accept': 'application/json' },
      cache: 'no-store',
      redirect: 'error',
      credentials: 'omit'
    };
    init.headers[CONTROL_HEADER] = '1';
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    return doFetch(fetchImpl, url, init, timeoutMs, label).then(function(out) {
      if (out.http >= 200 && out.http < 300) {
        var okBody;
        try { okBody = JSON.parse(out.body); } catch (e) {
          throw apiErr('服务返回内容不是合法 JSON', 'MOBILE_BAD_RESPONSE');
        }
        return toStatus(okBody);
      }
      var code = '';
      try {
        var errBody = JSON.parse(out.body);
        if (isObj(errBody) && errorMessage(errBody.error_code)) code = errBody.error_code;
      } catch (e) { /* 安全文案不使用服务端自由文本 */ }
      if (code) throw apiErr(errorMessage(code), code);
      if (out.http === 404) {
        throw apiErr('当前插件服务不支持手机连接接口，请升级插件后重试', 'MOBILE_UNSUPPORTED');
      }
      if (out.http === 401 || out.http === 403) {
        throw apiErr('手机控制请求被拒绝，请从 Kimi Code 桌面端面板操作', 'MOBILE_AUTH');
      }
      throw apiErr('手机连接请求未成功，请查询状态后再操作', 'MOBILE_HTTP');
    });
  }

  function create(opts) {
    opts = opts || {};
    var win = opts.window || (typeof window !== 'undefined' ? window : undefined);
    var fetchImpl = opts.fetch || (typeof fetch === 'function' ? fetch : undefined);
    if (!win || !win.location) {
      throw apiErr('当前环境缺少 window.location，无法定位桌面端服务', 'MOBILE_ENV');
    }
    if (typeof fetchImpl !== 'function') {
      throw apiErr('当前环境不支持 fetch，无法调用局域网连接接口', 'MOBILE_ENV');
    }
    // 建 client 时即解析 owner：无法确认可信桌面端服务地址就不发任何请求
    var ownerOrigin = resolveOwner(win);

    var override = opts.timeoutMs;
    var hasOverride = typeof override === 'number' && isFinite(override);
    var clamp = function(ms) {
      if (ms < MIN_TIMEOUT_MS) return MIN_TIMEOUT_MS;
      if (ms > MAX_TIMEOUT_MS) return MAX_TIMEOUT_MS;
      return ms;
    };
    // 显式 override 对所有请求统一生效（呼叫方自行决定预算）；默认按接口
    // 分配——仅 start/install 拉长到能覆盖 worker 侧在途时长，其余维持短预算
    var getTimeoutMs = clamp(hasOverride ? override : DEFAULT_GET_TIMEOUT_MS);
    var postTimeoutMs = clamp(hasOverride ? override : DEFAULT_POST_TIMEOUT_MS);
    var startTimeoutMs = clamp(hasOverride ? override : DEFAULT_START_TIMEOUT_MS);
    var installTimeoutMs = clamp(hasOverride ? override : DEFAULT_INSTALL_TIMEOUT_MS);

    return {
      status: function() {
        return call(fetchImpl, getTimeoutMs, 'GET', STATUS_PATH, undefined, '查询手机连接状态');
      },
      installConnector: function(consent) {
        if (consent !== true) return Promise.reject(apiErr(errorMessage('CONSENT_REQUIRED'), 'CONSENT_REQUIRED'));
        return call(fetchImpl, installTimeoutMs, 'POST', INSTALL_PATH,
          { consent: true, consent_version: CONSENT_VERSION }, '安装连接组件');
      },
      setEnabled: function(enabled, address, mode, relayConsent) {
        if (typeof enabled !== 'boolean') {
          return Promise.reject(apiErr('setEnabled 参数必须是布尔值', 'MOBILE_BAD_ARG'));
        }
        if (!enabled) {
          return call(fetchImpl, postTimeoutMs, 'POST', STOP_PATH, {}, '停止手机连接');
        }
        if (mode !== undefined && MODES.indexOf(mode) < 0) {
          return Promise.reject(apiErr('手机连接模式不受支持', 'MOBILE_BAD_ARG'));
        }
        var body = { owner_origin: ownerOrigin };
        if (mode !== undefined) body.mode = mode;
        if (mode === 'internet') {
          if (address !== undefined && address !== null && address !== '') {
            return Promise.reject(apiErr('外网模式不允许指定绑定地址', 'MOBILE_BAD_ARG'));
          }
          if (relayConsent !== true) return Promise.reject(apiErr(errorMessage('CONSENT_REQUIRED'), 'CONSENT_REQUIRED'));
          body.relay_consent = true;
          body.consent_version = CONSENT_VERSION;
        } else if (address !== undefined && address !== null && address !== '') {
          if (typeof address !== 'string' || !isRFC1918(address)) {
            return Promise.reject(apiErr('网卡地址必须是本机局域网 IPv4 地址', 'MOBILE_BAD_ARG'));
          }
          body.address = address;
        }
        return call(fetchImpl, startTimeoutMs, 'POST', START_PATH, body, '开启手机连接');
      },
      rotatePair: function() {
        return call(fetchImpl, postTimeoutMs, 'POST', PAIR_ROTATE_PATH, {}, '更换配对码');
      }
    };
  }

  var api = {
    create: create,
    validateRemoteURL: validateRemoteURL
  };

  var root = typeof globalThis !== 'undefined' ? globalThis : this;
  root.KimiMobileAPI = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
