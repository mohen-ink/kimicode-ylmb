/**
 * Kimi Remote Widget · 手机远程连接浮层 v3（自包含模块，kur- 前缀）
 * ---------------------------------------------------------------
 * 顶部三段切换块：内网（LAN，明文 HTTP，绑定所选内网网卡）/ CF 隧道（Cloudflare
 * Quick Tunnel）/ 中继服务器（frp，手机段明文 HTTP）。连接开启或在途期间模式锁定为当前
 * 运行模式，需先停止才能切换；关闭状态下切换只保存模式偏好，不持久化中继配置。
 * 外部契约（由先加载的脚本提供，缺失时给出明确提示，不拖垮宿主）：
 *   window.KimiMobileAPI.create({window?, fetch?, timeoutMs?})
 *     -> { status(), installConnector(consent, mode?), setEnabled(boolean, address?, mode?, relayConsent?, relayConfig?), rotatePair?() }
 *   window.KimiMobileAPI.validateRemoteURL(text, status) -> 可信 LAN / Internet / Relay pair url 或 throw
 *   window.KimiMobileAPI.validateRelayConfig(config) -> 已校验的中继配置或 throw
 *   window.KimiRemoteAPI.create(...)（可选）-> { status(): Promise<{enabled,state}> } 仅用于探测旧版官方中继
 *   window.KimiRemoteQR.toSVG(text) -> SVG 字符串
 * 本模块导出：window.KimiRemoteWidget.open()
 *
 * 轮询到旧版官方中继仍在运行时，只显示横幅与“停止”入口，绝不自动停止或代为切换。
 * 开启/安装不依赖官方中继状态；官方查询独立落地、独立重绘，不阻塞桥状态轮询。
 * 关闭浮层不会停止连接或释放在途请求锁；POST 不重试，结束后用 GET 对账。
 * gen/seq 丢弃跨开关旧响应，轮询不抢焦点。
 */
(function() {
  'use strict';
  if (window.KimiRemoteWidget) return;

  var OVERLAY_ID = 'kur-overlay';
  var STYLE_ID = 'kur-style';
  var POLL_MS = 1500;
  var STATES = { off: '未开启', starting: '正在开启…', on: '已开启', stopping: '正在停止…' };

  var overlayEl = null;
  var isOpen = false;
  var gen = 0;
  var seq = 0;
  var inflight = null;
  var wantPoll = false;
  var wantRefresh = false;
  var pollTimer = null;
  var api = null;
  var mobileApi = null;
  var MODE_KEY = 'kur-mode';
  var lanStatus = null;   // 三种模式共用的移动桥状态
  var selMode = loadMode();   // 关闭状态下的所选模式：'lan' | 'internet' | 'relay'
  var selAddr = '';           // 内网模式所选网卡地址
  var lanAddrKey = null;      // 上次渲染到下拉框的网卡列表指纹，变化才重建 DOM，避免轮询时收起下拉
  var offStatus = null;   // 官方中继状态（仅探测用，不影响桥可用性）
  var lanErr = '';
  var offErr = '';
  var offReq = null;      // 独立在途的官方状态查询
  var curState = '';      // 最近一次已应用的外网状态
  var linkOk = false;     // 当前展示的链接经校验且已被最近一次刷新确认
  var msgSrc = '';        // 当前消息来源：'req'=请求/状态错误 'post'=POST 落地错误（对账 GET 不得清除）'local'=复制反馈
  var postError = '';     // 最近一次 POST 的安全错误文案：显式成功/对应状态达成前保留，新失败保持供诊断
  var postKind = '';      // 产生 postError 的动作：start/stop/install/rotate/official（决定谁能清除它）
  var lastFocus = null;
  var docKeyHandler = null;
  var overlayKeyHandler = null;

  /* ---------------- 样式（kur- 前缀，只注入一次） ---------------- */
  var CSS = '' +
    '#kur-overlay { position: fixed; inset: 0; z-index: 100010; display: none; align-items: center; justify-content: center; padding: 16px; background: rgba(8, 12, 20, 0.5); backdrop-filter: blur(3px); font-family: system-ui, -apple-system, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif; box-sizing: border-box; }' +
    '#kur-overlay.kur-open { display: flex; }' +
    '#kur-overlay * { box-sizing: border-box; }' +
    '#kur-overlay .kur-hidden { display: none !important; }' +
    '.kur-dialog { width: 100%; max-width: 392px; max-height: calc(100vh - 32px); overflow-y: auto; border-radius: 16px; background: var(--color-surface, var(--color-bg, #ffffff)); color: var(--color-text, #1e293b); border: 1px solid color-mix(in srgb, var(--color-text, #000) 10%, transparent); box-shadow: 0 16px 40px rgba(0,0,0,0.35); }' +
    '.kur-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 18px 22px 14px; }' +
    '.kur-titles { min-width: 0; }' +
    '.kur-title { font-size: 17px; font-weight: 700; letter-spacing: .2px; line-height: 1.3; color: var(--color-text, #0f172a); }' +
    '.kur-x { width: 28px; height: 28px; border-radius: 8px; border: none; background: transparent; color: var(--color-text-faint, var(--color-text-muted, #64748b)); font-size: 15px; line-height: 1; cursor: pointer; font-family: inherit; flex-shrink: 0; }' +
    '.kur-x:hover { background: color-mix(in srgb, var(--color-text, #000) 8%, transparent); color: var(--color-text, #0f172a); }' +
    '.kur-body { display: flex; flex-direction: column; gap: 12px; padding: 0 22px 20px; }' +
    '.kur-statuscard { padding: 12px 14px; border-radius: 12px; background: var(--color-surface-sunken, color-mix(in srgb, var(--color-text, #000) 4%, transparent)); }' +
    '.kur-status { display: flex; align-items: center; gap: 10px; font-size: 14px; font-weight: 600; line-height: 1.4; }' +
    '.kur-dot { width: 10px; height: 10px; border-radius: 50%; background: #9ca3af; flex-shrink: 0; }' +
    '.kur-dot.kur-on { background: var(--color-success, #3fb950); box-shadow: 0 0 0 4px color-mix(in srgb, var(--color-success, #3fb950) 22%, transparent); }' +
    '.kur-dot.kur-mid { background: var(--color-warning, #d97706); box-shadow: 0 0 0 4px color-mix(in srgb, var(--color-warning, #d97706) 22%, transparent); }' +
    '.kur-device { margin-top: 4px; padding-left: 20px; font-size: 12px; color: var(--color-text-muted, #64748b); word-break: break-all; }' +
    '.kur-legacy, .kur-worker, .kur-tdiag { display: none; font-size: 12px; line-height: 1.6; padding: 10px 12px; border-radius: 10px; word-break: break-all; }' +
    '.kur-legacy { background: color-mix(in srgb, var(--color-warning, #d97706) 12%, transparent); color: var(--color-warning, #b45309); }' +
    '.kur-worker { background: color-mix(in srgb, var(--color-accent, #1a88ff) 8%, transparent); color: var(--color-text-muted, #64748b); }' +
    '.kur-tdiag { background: color-mix(in srgb, var(--color-warning, #d97706) 10%, transparent); color: var(--color-warning, #b45309); }' +
    '.kur-legacy.kur-show, .kur-worker.kur-show, .kur-tdiag.kur-show { display: block; }' +
    '.kur-legacy-stop { margin-top: 8px; }' +
    '.kur-tdiag-detail { display: block; margin-top: 4px; font-size: 11.5px; color: var(--color-text-muted, #64748b); }' +
    '.kur-internet { display: flex; flex-direction: column; gap: 10px; }' +
    '.kur-connector { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 2px; font-size: 12.5px; font-weight: 600; color: var(--color-text-muted, #64748b); }' +
    '.kur-consent { display: flex; align-items: flex-start; gap: 10px; padding: 12px 14px; border-radius: 12px; cursor: pointer; font-size: 12px; line-height: 1.65; color: var(--color-text-muted, #64748b); border: 1px solid color-mix(in srgb, var(--color-text, #000) 10%, transparent); background: var(--color-surface-raised, transparent); transition: border-color .15s, background .15s; }' +
    '.kur-consent input { flex-shrink: 0; width: 15px; height: 15px; margin: 3px 0 0; accent-color: var(--color-accent, #1a88ff); cursor: pointer; }' +
    '.kur-consent:has(input:checked) { border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 50%, transparent); background: color-mix(in srgb, var(--color-accent, #1a88ff) 6%, var(--color-surface-raised, transparent)); color: var(--color-text, #1e293b); }' +
    '.kur-qr { display: none; justify-content: center; padding: 16px; border-radius: 12px; background: var(--color-surface-sunken, color-mix(in srgb, var(--color-text, #000) 4%, transparent)); }' +
    '.kur-qr.kur-show { display: flex; }' +
    '.kur-qrbox { width: 200px; height: 200px; padding: 10px; border-radius: 12px; background: #fff; display: flex; align-items: center; justify-content: center; box-shadow: 0 1px 4px rgba(0,0,0,0.12); }' +
    '.kur-qrbox svg { width: 100%; height: 100%; display: block; }' +
    '.kur-qr-fb { font-size: 12px; line-height: 1.6; color: var(--color-text-muted, #64748b); text-align: center; padding: 12px 6px; }' +
    '.kur-linkwrap { display: none; }' +
    '.kur-linkwrap.kur-show { display: block; }' +
    '.kur-pairhint { display: block; margin-bottom: 8px; font-size: 12px; color: var(--color-warning, #b45309); }' +
    '.kur-link { display: block; width: 100%; padding: 9px 12px; border-radius: 10px; border: 1px solid color-mix(in srgb, var(--color-text, #000) 12%, transparent); background: var(--color-surface-sunken, transparent); color: var(--color-text-muted, #475569); font-size: 12px; line-height: 1.5; font-family: inherit; user-select: text; -webkit-user-select: text; cursor: text; word-break: break-all; resize: none; outline: none; }' +
    '.kur-link:focus { border-color: var(--color-accent, #1a88ff); }' +
    '.kur-copyrow { display: flex; justify-content: space-between; gap: 8px; margin-top: 8px; }' +
    '.kur-btn-mini { flex: 1; height: 32px; padding: 0 12px; border-radius: 9px; font-size: 12.5px; font-weight: 600; font-family: inherit; cursor: pointer; border: 1px solid color-mix(in srgb, var(--color-text, #000) 14%, transparent); background: transparent; color: var(--color-text, #1e293b); transition: all .15s; }' +
    '.kur-btn-mini:not(:disabled):hover { border-color: var(--color-accent, #1a88ff); color: var(--color-accent, #1a88ff); }' +
    '.kur-btn-mini:disabled { opacity: 0.45; cursor: not-allowed; }' +
    '.kur-copyrow #kur-copy { background: var(--color-accent, #1a88ff); border-color: var(--color-accent, #1a88ff); color: #fff; }' +
    '.kur-copyrow #kur-copy:not(:disabled):hover { filter: brightness(1.08); color: #fff; }' +
    '#kur-install { width: 100%; flex: none; }' +
    '.kur-actions { display: flex; flex-direction: column; gap: 8px; }' +
    '.kur-btn { width: 100%; height: 42px; border-radius: 10px; font-size: 14px; font-weight: 600; letter-spacing: .3px; cursor: pointer; font-family: inherit; transition: all .15s; }' +
    '.kur-btn:disabled { opacity: 0.45; cursor: not-allowed; }' +
    '.kur-btn-on { border: 1px solid var(--color-accent, #1a88ff); background: var(--color-accent, #1a88ff); color: #fff; }' +
    '.kur-btn-on:not(:disabled):hover { filter: brightness(1.08); }' +
    '.kur-btn-off { height: 38px; font-size: 13px; border: 1px solid color-mix(in srgb, var(--color-danger, #f85149) 45%, transparent); background: transparent; color: var(--color-danger, #f85149); }' +
    '.kur-btn-off:not(:disabled):hover { background: color-mix(in srgb, var(--color-danger, #f85149) 10%, transparent); }' +
    '.kur-msg { display: none; font-size: 12.5px; line-height: 1.6; padding: 10px 12px; border-radius: 10px; word-break: break-all; user-select: text; -webkit-user-select: text; }' +
    '.kur-msg.kur-show { display: block; }' +
    '.kur-msg-err { background: color-mix(in srgb, var(--color-danger, #f85149) 10%, transparent); color: var(--color-danger, #f85149); }' +
    '.kur-msg-ok { background: color-mix(in srgb, var(--color-success, #3fb950) 12%, transparent); color: var(--color-success, #059669); }' +
    '.kur-msg-warn { background: color-mix(in srgb, var(--color-warning, #d97706) 12%, transparent); color: var(--color-warning, #b45309); }' +
    '.kur-fold { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 10px 2px 0; border: none; border-top: 1px solid color-mix(in srgb, var(--color-text, #000) 8%, transparent); background: transparent; color: var(--color-text-muted, #64748b); font-size: 12px; font-family: inherit; cursor: pointer; }' +
    '#kur-overlay button:focus-visible, #kur-overlay .kur-consent:has(input:focus-visible) { outline: 2px solid color-mix(in srgb, var(--color-accent, #1a88ff) 70%, transparent); outline-offset: 1px; }' +
    '.kur-fold:hover { color: var(--color-text, #1e293b); }' +
    '.kur-fold-arrow { display: inline-block; transition: transform .15s; }' +
    '.kur-fold[aria-expanded="true"] .kur-fold-arrow { transform: rotate(180deg); }' +
    '.kur-note { margin-top: -4px; font-size: 12px; line-height: 1.75; color: var(--color-text-muted, #64748b); }' +
    '.kur-modes { display: flex; gap: 4px; padding: 3px; margin: 0 22px 14px; border-radius: 11px; background: var(--color-surface-sunken, color-mix(in srgb, var(--color-text, #000) 6%, transparent)); }' +
    '.kur-mode { flex: 1; min-width: 0; height: 32px; padding: 0 6px; border: none; border-radius: 8px; background: transparent; color: var(--color-text-muted, #64748b); font-size: 12.5px; font-weight: 600; font-family: inherit; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; transition: background .15s, color .15s; }' +
    '.kur-mode:not(:disabled):not(.kur-mode-on):hover { color: var(--color-text, #1e293b); }' +
    '.kur-mode.kur-mode-on { background: var(--color-accent, #1a88ff); color: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.2); }' +
    '.kur-mode:disabled { cursor: not-allowed; }' +
    '.kur-mode:disabled:not(.kur-mode-on) { opacity: 0.45; }' +
    '.kur-modehint { display: none; margin: -8px 22px 12px; font-size: 12px; line-height: 1.5; color: var(--color-text-faint, var(--color-text-muted, #64748b)); }' +
    '.kur-modehint.kur-show { display: block; }' +
    '.kur-lan, .kur-relay { display: flex; flex-direction: column; gap: 10px; }' +
    '.kur-lan-row { display: flex; flex-direction: column; gap: 6px; font-size: 12.5px; font-weight: 600; color: var(--color-text-muted, #64748b); }' +
    '.kur-select { width: 100%; height: 36px; padding: 0 10px; border-radius: 10px; border: 1px solid color-mix(in srgb, var(--color-text, #000) 14%, transparent); background: var(--color-surface-sunken, transparent); color: var(--color-text, #1e293b); font-size: 13px; font-family: inherit; outline: none; }' +
    '.kur-select:focus { border-color: var(--color-accent, #1a88ff); }' +
    '.kur-select:disabled { opacity: 0.6; cursor: not-allowed; }' +
    '.kur-lan-empty { padding: 10px 12px; border-radius: 10px; font-size: 12px; line-height: 1.6; background: color-mix(in srgb, var(--color-warning, #d97706) 12%, transparent); color: var(--color-warning, #b45309); }' +
    '@media (max-width: 380px) { .kur-head { padding: 16px 16px 12px; } .kur-body { padding: 0 16px 16px; } .kur-modes { margin: 0 16px 12px; } .kur-modehint { margin: -6px 16px 10px; } .kur-qrbox { width: 180px; height: 180px; } }';

  var DOWNLOAD_NOTICE = '我同意从 Cloudflare 官方 GitHub 下载固定版本 cloudflared 2026.9.3（约 55 MB，55,366,080 字节），并校验 SHA-256；安装组件不会开启连接。';
  var RELAY_NOTICE = '我同意本次通过 Cloudflare Quick Tunnel 中转。Cloudflare 可处理传输内容，此方式不是端到端加密（E2E），也不是 P2P；临时通道没有可靠性保证，电脑须保持开机，不依赖个人 VPS。';
  var LAN_NOTICE = '我了解内网模式使用明文 HTTP，同一网络内的其他设备可能窃听或篡改流量；仅在自己控制的可信 Wi-Fi 下使用。';
  var FRP_DOWNLOAD_NOTICE = '我同意从 frp 官方 GitHub 下载插件固定版本的 frpc，并校验 SHA-256；这是独立于 cloudflared 的组件，安装不会开启连接。';
  var FRP_HTTP_NOTICE = '我明确同意本次以明文 HTTP 连接中继服务器：手机到服务器的流量（包括配对授权）可能被窃听或篡改，仅用于联调，不用于敏感操作。电脑到服务器的 frp 通道仍使用 TLS 并校验服务器身份，但无法保护手机 HTTP 段。';
  var NOTE_RELAY = '中继服务器第一阶段仅支持 HTTP，仅用于联调。手机到服务器的流量及配对授权可能被窃听、篡改，请勿用于敏感操作。' +
    '服务器无需域名：填写规范公网 IPv4、frps 控制端口与不同的手机访问端口，并在服务器防火墙放行相应端口。' +
    '电脑到服务器的 frp 通道使用 TLS；CA 必须为签发服务器证书的可信公开证书，服务器证书应包含该 IP 的 SAN，以便校验身份。不要粘贴私钥。' +
    'frp 认证 token 须与服务器一致，它不是手机配对码，不会出现在手机链接或二维码里。配置不保存在浏览器存储中，停止或关闭浮层后清空 token 和 CA。' +
    '运行中配置锁定；恢复运行状态只显示公开 IP 和访问端口。链接/二维码含一次性控制授权，请勿分享；手机端可执行命令、操作电脑文件。' +
    '配对码约 10 分钟有效、仅可使用一次；生成新二维码不会断开已连接设备。电脑须保持开机，关闭此窗口不会停止连接。';
  var NOTE_LAN = '内网模式：不经过 Cloudflare，也无需下载任何组件。手机与电脑必须连接同一个可信 Wi-Fi，用系统浏览器扫描二维码或打开链接。' +
    '桥只绑定你选择的那块内网网卡（RFC1918 私有地址），不会监听其他网卡；端口从 39282 起自动选取空闲端口。' +
    '流量是明文 HTTP，不是加密连接；链接/二维码含一次性会话控制授权，请勿分享；手机端可通过 Agent 执行命令、操作电脑文件。' +
    '配对二维码约 10 分钟有效、仅可使用一次；连接租约闲置约 24 小时、累计最长 7 天后自动失效。' +
    '首次开启时 Windows 防火墙可能弹出放行提示，需允许“专用网络”；若手机仍打不开，请检查防火墙或路由器的客户端隔离设置。' +
    '重启本机服务（守护进程）不会中断已建立的连接；退出桌面端或在此停止连接会撤销全部配对并断开设备。关闭本窗口不会停止连接。';
  var NOTE_INTERNET = '外网模式：手机无需同 WiFi，用系统浏览器扫描二维码或打开链接。' +
    '链接/二维码含一次性会话控制授权，请勿分享；手机端可通过 Agent 执行命令、操作电脑文件。' +
    '配对二维码约 10 分钟有效、仅可使用一次；连接租约闲置约 24 小时、累计最长 7 天后自动失效。' +
    '重启本机服务（守护进程）不会中断已建立的连接；退出桌面端或在此停止连接会撤销全部配对并断开设备。' +
    '外网通道进程异常退出后，旧链接立即不可用，请停止后重新开启以获取新链接。' +
    '组件仅在明确同意并点击安装后下载，中转仅在本次明确同意并点击开启后启动。' +
    '关闭本窗口不会停止连接。';

  function ensureStyle(doc) {
    if (doc.getElementById(STYLE_ID)) return;
    var s = doc.createElement('style');
    s.id = STYLE_ID;
    s.textContent = CSS;
    doc.head.appendChild(s);
  }

  /* ---------------- DOM 构建 ---------------- */
  function $(sel) { return overlayEl ? overlayEl.querySelector(sel) : null; }

  function buildOverlay(doc) {
    var o = doc.createElement('div');
    o.id = OVERLAY_ID;
    o.setAttribute('role', 'dialog');
    o.setAttribute('aria-modal', 'true');
    o.setAttribute('aria-label', '手机连接');

    var d = doc.createElement('div');
    d.className = 'kur-dialog';

    var head = doc.createElement('div');
    head.className = 'kur-head';
    var titles = doc.createElement('div');
    titles.className = 'kur-titles';
    var title = doc.createElement('div');
    title.className = 'kur-title';
    title.textContent = '手机连接';
    titles.appendChild(title);
    var x = doc.createElement('button');
    x.id = 'kur-x';
    x.className = 'kur-x';
    x.type = 'button';
    x.title = '关闭';
    x.setAttribute('aria-label', '关闭');
    x.textContent = '✕';
    head.appendChild(titles);
    head.appendChild(x);
    d.appendChild(head);

    var modes = doc.createElement('div');
    modes.className = 'kur-modes';
    modes.id = 'kur-modes';
    modes.setAttribute('role', 'group');
    modes.setAttribute('aria-label', '连接模式');
    [['lan', '内网'], ['internet', 'CF 隧道'], ['relay', '中继服务器']].forEach(function(m) {
      var mb = doc.createElement('button');
      mb.id = 'kur-mode-' + m[0];
      mb.className = 'kur-mode';
      mb.type = 'button';
      mb.textContent = m[1];
      mb.setAttribute('aria-pressed', 'false');
      modes.appendChild(mb);
    });
    d.appendChild(modes);
    var modeHint = doc.createElement('div');
    modeHint.className = 'kur-modehint';
    modeHint.id = 'kur-modehint';
    modeHint.setAttribute('aria-live', 'polite');
    d.appendChild(modeHint);

    var b = doc.createElement('div');
    b.className = 'kur-body';

    var card = doc.createElement('div');
    card.className = 'kur-statuscard';
    var status = doc.createElement('div');
    status.className = 'kur-status';
    status.setAttribute('aria-live', 'polite');
    var dot = doc.createElement('span');
    dot.className = 'kur-dot';
    dot.id = 'kur-dot';
    var st = doc.createElement('span');
    st.id = 'kur-state';
    st.textContent = '查询中…';
    status.appendChild(dot);
    status.appendChild(st);
    card.appendChild(status);
    var dev = doc.createElement('div');
    dev.className = 'kur-device';
    dev.id = 'kur-device';
    dev.setAttribute('aria-live', 'polite');
    card.appendChild(dev);
    b.appendChild(card);

    var legacy = doc.createElement('div');
    legacy.className = 'kur-legacy';
    legacy.id = 'kur-legacy';
    legacy.setAttribute('aria-live', 'polite');
    legacy.setAttribute('aria-hidden', 'true');
    var legacyText = doc.createElement('span');
    legacyText.id = 'kur-legacy-text';
    legacy.appendChild(legacyText);
    legacy.appendChild(doc.createElement('br'));
    var legacyStop = doc.createElement('button');
    legacyStop.id = 'kur-legacy-stop';
    legacyStop.className = 'kur-btn-mini kur-legacy-stop';
    legacyStop.type = 'button';
    legacyStop.textContent = '停止官方中继';
    legacyStop.disabled = true;
    legacy.appendChild(legacyStop);
    b.appendChild(legacy);

    var worker = doc.createElement('div');
    worker.className = 'kur-worker';
    worker.id = 'kur-worker';
    worker.setAttribute('aria-live', 'polite');
    worker.setAttribute('aria-hidden', 'true');
    b.appendChild(worker);

    var tdiag = doc.createElement('div');
    tdiag.className = 'kur-tdiag';
    tdiag.id = 'kur-tunnel-diag';
    tdiag.setAttribute('aria-live', 'polite');
    tdiag.setAttribute('aria-hidden', 'true');
    var tdiagText = doc.createElement('span');
    tdiagText.id = 'kur-tunnel-diag-text';
    var tdiagDetail = doc.createElement('span');
    tdiagDetail.className = 'kur-tdiag-detail';
    tdiagDetail.id = 'kur-tunnel-diag-detail';
    tdiag.appendChild(tdiagText);
    tdiag.appendChild(tdiagDetail);
    b.appendChild(tdiag);

    var consentRow = function(host, id, text) {
      var label = doc.createElement('label');
      label.className = 'kur-consent';
      label.id = id + '-row';
      var input = doc.createElement('input');
      input.id = id;
      input.type = 'checkbox';
      input.checked = false;
      label.appendChild(input);
      var notice = doc.createElement('span');
      notice.textContent = text;
      label.appendChild(notice);
      host.appendChild(label);
    };

    var lan = doc.createElement('div');
    lan.id = 'kur-lan';
    lan.className = 'kur-lan';
    var lanRow = doc.createElement('label');
    lanRow.className = 'kur-lan-row';
    lanRow.appendChild(doc.createTextNode('内网网卡地址'));
    var lanSel = doc.createElement('select');
    lanSel.id = 'kur-lan-addr';
    lanSel.className = 'kur-select';
    lanSel.setAttribute('aria-label', '内网网卡地址');
    lanRow.appendChild(lanSel);
    lan.appendChild(lanRow);
    var lanEmpty = doc.createElement('div');
    lanEmpty.id = 'kur-lan-empty';
    lanEmpty.className = 'kur-lan-empty kur-hidden';
    lanEmpty.textContent = '未检测到可用的内网 IPv4 地址（10.x / 172.16–31.x / 192.168.x）。请确认电脑已连接 Wi-Fi 或有线网络。';
    lan.appendChild(lanEmpty);
    consentRow(lan, 'kur-lan-consent', LAN_NOTICE);
    b.appendChild(lan);

    var relayPanel = doc.createElement('div');
    relayPanel.id = 'kur-relay';
    relayPanel.className = 'kur-relay';
    var relayConnector = doc.createElement('div');
    relayConnector.id = 'kur-frp-connector';
    relayConnector.className = 'kur-connector';
    relayConnector.setAttribute('aria-live', 'polite');
    relayPanel.appendChild(relayConnector);
    consentRow(relayPanel, 'kur-frp-download-consent', FRP_DOWNLOAD_NOTICE);
    var frpInstall = doc.createElement('button');
    frpInstall.id = 'kur-frp-install';
    frpInstall.type = 'button';
    frpInstall.className = 'kur-btn-mini';
    frpInstall.textContent = '仅安装 frp 组件';
    frpInstall.disabled = true;
    relayPanel.appendChild(frpInstall);
    [['server-ip', '服务器公网 IPv4', 'text', '', '例如 8.8.8.8'],
      ['server-port', '控制端口', 'number', '7000', '7000'],
      ['remote-port', '手机访问端口', 'number', '6000', '6000'],
      ['token', 'frp 认证 token（不是手机配对码）', 'password', '', '32–512 位无空白 ASCII'],
      ['ca-cert', '服务器 CA 证书（PEM，仅粘贴公开证书）', 'textarea', '', '-----BEGIN CERTIFICATE-----']
    ].forEach(function(f) {
      var row = doc.createElement('label');
      row.className = 'kur-lan-row';
      row.appendChild(doc.createTextNode(f[1]));
      var input = doc.createElement(f[2] === 'textarea' ? 'textarea' : 'input');
      input.id = 'kur-frp-' + f[0];
      input.className = f[2] === 'textarea' ? 'kur-link' : 'kur-select';
      if (f[2] === 'textarea') { input.rows = 5; input.maxLength = 65536; }
      else input.type = f[2];
      if (f[2] === 'number') { input.min = '1'; input.max = '65535'; input.step = '1'; }
      if (f[0] === 'token') input.maxLength = 512;
      if (f[0] === 'server-ip') input.maxLength = 15;
      input.value = f[3];
      input.placeholder = f[4];
      input.setAttribute('autocomplete', 'off');
      input.setAttribute('spellcheck', 'false');
      input.setAttribute('aria-label', f[1]);
      row.appendChild(input);
      relayPanel.appendChild(row);
    });
    consentRow(relayPanel, 'kur-frp-consent', FRP_HTTP_NOTICE);
    b.appendChild(relayPanel);

    var internet = doc.createElement('div');
    internet.id = 'kur-internet';
    internet.className = 'kur-internet';
    var connector = doc.createElement('div');
    connector.id = 'kur-connector';
    connector.className = 'kur-connector';
    connector.setAttribute('aria-live', 'polite');
    internet.appendChild(connector);
    consentRow(internet, 'kur-download-consent', DOWNLOAD_NOTICE);
    var install = doc.createElement('button');
    install.id = 'kur-install';
    install.type = 'button';
    install.className = 'kur-btn-mini';
    install.textContent = '仅安装组件（约 55 MB）';
    install.disabled = true;
    internet.appendChild(install);
    consentRow(internet, 'kur-relay-consent', RELAY_NOTICE);
    b.appendChild(internet);

    var qr = doc.createElement('div');
    qr.className = 'kur-qr';
    qr.id = 'kur-qr';
    var qrbox = doc.createElement('div');
    qrbox.className = 'kur-qrbox';
    qrbox.id = 'kur-qrbox';
    qrbox.setAttribute('aria-label', '远程连接二维码');
    qr.appendChild(qrbox);
    b.appendChild(qr);

    var linkwrap = doc.createElement('div');
    linkwrap.className = 'kur-linkwrap';
    linkwrap.id = 'kur-linkwrap';
    linkwrap.setAttribute('aria-hidden', 'true');
    var pairHint = doc.createElement('span');
    pairHint.className = 'kur-pairhint';
    pairHint.textContent = '链接含一次性控制授权，请勿分享';
    linkwrap.appendChild(pairHint);
    var link = doc.createElement('textarea');
    link.className = 'kur-link';
    link.id = 'kur-link';
    link.readOnly = true;
    link.rows = 2;
    link.setAttribute('aria-label', '远程连接地址');
    var copyrow = doc.createElement('div');
    copyrow.className = 'kur-copyrow';
    var rotate = doc.createElement('button');
    rotate.id = 'kur-rotate';
    rotate.className = 'kur-btn-mini';
    rotate.type = 'button';
    rotate.textContent = '生成新二维码';
    rotate.disabled = true;
    var copy = doc.createElement('button');
    copy.id = 'kur-copy';
    copy.className = 'kur-btn-mini';
    copy.type = 'button';
    copy.textContent = '复制链接';
    copy.disabled = true;
    copyrow.appendChild(rotate);
    copyrow.appendChild(copy);
    linkwrap.appendChild(link);
    linkwrap.appendChild(copyrow);
    b.appendChild(linkwrap);

    var actions = doc.createElement('div');
    actions.className = 'kur-actions';
    var onBtn = doc.createElement('button');
    onBtn.id = 'kur-enable';
    onBtn.className = 'kur-btn kur-btn-on';
    onBtn.type = 'button';
    onBtn.textContent = '开启外网连接';
    var offBtn = doc.createElement('button');
    offBtn.id = 'kur-disable';
    offBtn.className = 'kur-btn kur-btn-off';
    offBtn.type = 'button';
    offBtn.textContent = '停止连接';
    actions.appendChild(onBtn);
    actions.appendChild(offBtn);
    b.appendChild(actions);

    var msg = doc.createElement('div');
    msg.className = 'kur-msg';
    msg.id = 'kur-msg';
    msg.setAttribute('aria-live', 'polite');
    msg.setAttribute('aria-hidden', 'true');
    b.appendChild(msg);

    var fold = doc.createElement('button');
    fold.id = 'kur-note-toggle';
    fold.className = 'kur-fold';
    fold.type = 'button';
    fold.setAttribute('aria-expanded', 'false');
    fold.setAttribute('aria-controls', 'kur-note');
    var foldLabel = doc.createElement('span');
    foldLabel.textContent = '使用须知与安全说明';
    var foldArrow = doc.createElement('span');
    foldArrow.className = 'kur-fold-arrow';
    foldArrow.setAttribute('aria-hidden', 'true');
    foldArrow.textContent = '▾';
    fold.appendChild(foldLabel);
    fold.appendChild(foldArrow);
    b.appendChild(fold);

    var note = doc.createElement('div');
    note.className = 'kur-note kur-hidden';
    note.id = 'kur-note';
    note.textContent = NOTE_INTERNET;
    b.appendChild(note);

    d.appendChild(b);
    o.appendChild(d);
    return o;
  }

  /* ---------------- UI 辅助 ---------------- */
  // kind: true/'err' 错误 · 'warn' 提醒（非错误，如一次性配对码已用）· false/'ok' 正常
  function showMsg(text, kind, src) {
    var m = $('#kur-msg');
    if (!m) return;
    msgSrc = text ? (src || 'req') : '';
    m.textContent = text || '';
    m.classList.toggle('kur-show', !!text);
    m.classList.toggle('kur-msg-err', kind === true || kind === 'err');
    m.classList.toggle('kur-msg-warn', kind === 'warn');
    m.classList.toggle('kur-msg-ok', !kind || kind === 'ok');
    m.setAttribute('aria-hidden', text ? 'false' : 'true');
  }

  // 可见性判断：真实浏览器用 getClientRects/getComputedStyle；
  // 无布局环境退回祖先链上的 display/显隐类检查
  function isShown(el) {
    if (!el) return false;
    try { if (el.getClientRects && el.getClientRects().length === 0) return false; } catch (e) {}
    try {
      var cs = window.getComputedStyle && window.getComputedStyle(el);
      if (cs && (cs.display === 'none' || cs.visibility === 'hidden')) return false;
    } catch (e) {}
    var n = el;
    while (n && n.parentNode) {
      var st = n.style;
      if (st && (st.display === 'none' || st.visibility === 'hidden')) return false;
      var cl = n.classList;
      if (cl) {
        if (cl.contains('kur-hidden')) return false;
        if ((cl.contains('kur-linkwrap') || cl.contains('kur-qr') || cl.contains('kur-legacy'))
            && !cl.contains('kur-show')) return false;
        if (n.id === OVERLAY_ID && !cl.contains('kur-open')) return false;
      }
      n = n.parentNode;
    }
    return true;
  }

  function focusables() {
    var ids = ['#kur-x', '#kur-mode-lan', '#kur-mode-internet', '#kur-mode-relay', '#kur-legacy-stop',
               '#kur-lan-addr', '#kur-lan-consent', '#kur-frp-download-consent', '#kur-frp-install',
               '#kur-frp-server-ip', '#kur-frp-server-port', '#kur-frp-remote-port', '#kur-frp-token',
               '#kur-frp-ca-cert', '#kur-frp-consent', '#kur-download-consent', '#kur-install', '#kur-relay-consent',
               '#kur-link', '#kur-rotate', '#kur-copy', '#kur-enable', '#kur-disable', '#kur-note-toggle'];
    var out = [];
    for (var i = 0; i < ids.length; i++) {
      var el = $(ids[i]);
      if (!el || el.disabled) continue;
      if ((ids[i] === '#kur-link' || ids[i] === '#kur-copy') && !linkOk) continue;
      if (!isShown(el)) continue;
      out.push(el);
    }
    return out;
  }

  function trapTab(e) {
    var els = focusables();
    if (!els.length) { e.preventDefault(); return; }
    var first = els[0];
    var last = els[els.length - 1];
    var cur = document.activeElement;
    var inside = false;
    for (var i = 0; i < els.length; i++) if (els[i] === cur) { inside = true; break; }
    if (e.shiftKey) {
      if (!inside || cur === first) { e.preventDefault(); last.focus(); }
    } else if (!inside || cur === last) {
      e.preventDefault(); first.focus();
    }
  }

  function lanConfirmedOff() {
    return !!mobileApi && !!lanStatus && lanStatus.state === 'off' && lanStatus.enabled === false;
  }
  function legacyOfficialActive() {
    return !!offStatus && offStatus.enabled === true && offStatus.state !== 'off';
  }
  function bridgeBusy() {
    return !!lanStatus && (lanStatus.state !== 'off' || lanStatus.enabled);
  }
  function startBlockedByLegacy() { return legacyOfficialActive(); }

  /* ---------------- 模式 ---------------- */
  function loadMode() {
    try {
      var v = window.localStorage && window.localStorage.getItem(MODE_KEY);
      if (v === 'lan' || v === 'internet' || v === 'relay') return v;
    } catch (e) { /* 存储不可用：用默认 */ }
    return 'internet';
  }
  function saveMode(m) {
    try { if (window.localStorage) window.localStorage.setItem(MODE_KEY, m); } catch (e) { /* 忽略 */ }
  }
  function curMode() {
    if (bridgeBusy() && MODE_NAMES[lanStatus.mode]) return lanStatus.mode;
    return selMode;
  }
  function modeLocked() {
    return !lanStatus || bridgeBusy() || (!!inflight && inflight.kind === 'POST');
  }
  function lanAddresses() {
    return (lanStatus && Array.isArray(lanStatus.addresses)) ? lanStatus.addresses : [];
  }
  function lanAddrValid() {
    return !!selAddr && lanAddresses().indexOf(selAddr) >= 0;
  }
  function checked(id) { var el = $(id); return !!el && el.checked === true; }
  function requestLocked(allowRefresh) {
    return !!inflight && !(allowRefresh && inflight.kind === 'GET');
  }
  function inDialog(el) {
    while (el) {
      if (el === overlayEl) return true;
      el = el.parentNode;
    }
    return false;
  }
  function clearRelaySecrets() {
    ['#kur-frp-token', '#kur-frp-ca-cert'].forEach(function(id) {
      var el = $(id);
      if (el) el.value = '';
    });
  }
  function readRelayConfig() {
    var validate = window.KimiMobileAPI && window.KimiMobileAPI.validateRelayConfig;
    if (typeof validate !== 'function') return null;
    var value = function(id) { var el = $('#kur-frp-' + id); return el ? el.value : ''; };
    var port = function(id) { var raw = value(id); return /^[1-9][0-9]{0,4}$/.test(raw) ? +raw : NaN; };
    try {
      return validate({ server_ip: value('server-ip'), server_port: port('server-port'),
        remote_port: port('remote-port'), token: value('token'), ca_cert: value('ca-cert') });
    } catch (e) { return null; }
  }
  function currentConnector() {
    if (!lanStatus) return null;
    return curMode() === 'relay' ? (lanStatus.relay_connector || { state: 'missing' }) : lanStatus.connector;
  }
  function canStart(allowRefresh) {
    if (requestLocked(allowRefresh) || !lanConfirmedOff() || startBlockedByLegacy()) return false;
    var mode = curMode();
    if (mode === 'lan') return lanAddrValid() && checked('#kur-lan-consent');
    var connector = currentConnector();
    if (!connector || connector.state !== 'installed') return false;
    if (mode === 'relay') return checked('#kur-frp-consent') && !!readRelayConfig();
    return mode === 'internet' && checked('#kur-relay-consent');
  }
  function canInstall(allowRefresh) {
    var mode = curMode();
    var connector = currentConnector();
    return !requestLocked(allowRefresh) && !!mobileApi && (mode === 'internet' || mode === 'relay')
      && typeof mobileApi.installConnector === 'function' && lanConfirmedOff() && !startBlockedByLegacy()
      && !!connector && (connector.state === 'missing' || connector.state === 'failed')
      && checked(mode === 'relay' ? '#kur-frp-download-consent' : '#kur-download-consent');
  }
  function canRotate(allowRefresh) {
    if (requestLocked(allowRefresh) || !mobileApi || typeof mobileApi.rotatePair !== 'function') return false;
    if (!lanStatus || !lanStatus.enabled || lanStatus.state !== 'on') return false;
    if (lanStatus.mode === 'lan') return true;
    if (lanStatus.mode !== 'internet' && lanStatus.mode !== 'relay') return false;
    return !!lanStatus.tunnel && lanStatus.tunnel.state === 'ready';
  }

  function connectorInstalled() {
    return !!lanStatus && !!lanStatus.connector && lanStatus.connector.state === 'installed';
  }

  function setButtons() {
    var previousFocus = document.activeElement;
    var hadDialogFocus = isOpen && inDialog(previousFocus);
    var locked = requestLocked(true);
    var on = $('#kur-enable');
    var off = $('#kur-disable');
    if (on) {
      on.disabled = !mobileApi || !canStart(true);
      on.setAttribute('aria-disabled', on.disabled || !!inflight ? 'true' : 'false');
    }
    if (off) {
      // 停止是幂等的安全动作：GET 失败（lanStatus 未知）时仍必须可发出；
      // 仅在已确认 off 时禁用
      off.disabled = locked || !mobileApi || (!!lanStatus && lanStatus.state === 'off');
      off.setAttribute('aria-disabled', off.disabled || !!inflight ? 'true' : 'false');
    }
    // 已知开启，或状态查询失败（疑似仍有实例在跑）时都提供停止入口
    var showStop = (!!lanStatus && lanStatus.state !== 'off') || (!lanStatus && !!lanErr);
    if (on) {
      on.classList.toggle('kur-hidden', showStop);
      on.textContent = curMode() === 'lan' ? '开启内网连接' : (curMode() === 'relay' ? '开启中继连接（HTTP）' : '开启外网连接');
    }
    if (off) off.classList.toggle('kur-hidden', !showStop);
    var rotate = $('#kur-rotate');
    if (rotate) {
      rotate.disabled = !canRotate(true);
      rotate.setAttribute('aria-disabled', rotate.disabled || !!inflight ? 'true' : 'false');
    }
    var copy = $('#kur-copy');
    if (copy) copy.disabled = !linkOk;
    var install = $('#kur-install');
    if (install) {
      install.disabled = !canInstall(true);
      install.setAttribute('aria-disabled', install.disabled || !!inflight ? 'true' : 'false');
    }
    renderModeUi();
    var connector = lanStatus && lanStatus.connector;
    var connectorEl = $('#kur-connector');
    var names = { missing: '未安装', installing: '正在安装…', installed: '已安装（2026.9.3）', failed: '安装失败' };
    if (connectorEl) connectorEl.textContent = '外网组件：' + (connector ? names[connector.state] || '状态未知' : '状态待确认');
    // 组件已安装时隐藏下载同意行与安装按钮（而非永久 removeChild）：
    // 状态回落到 missing/failed 时必须能重新显示，绝不把入口从 DOM 删死
    var installed = connectorInstalled();
    var downloadRow = $('#kur-download-consent-row');
    if (downloadRow) downloadRow.classList.toggle('kur-hidden', installed);
    if (install) install.classList.toggle('kur-hidden', installed);
    var download = $('#kur-download-consent');
    if (download) download.disabled = installed || locked || !connector || connector.state === 'installing';
    var relay = $('#kur-relay-consent');
    if (relay) relay.disabled = locked || !lanConfirmedOff() || startBlockedByLegacy();
    var lanConsent = $('#kur-lan-consent');
    if (lanConsent) lanConsent.disabled = locked || !lanConfirmedOff() || startBlockedByLegacy();
    var lanSel = $('#kur-lan-addr');
    if (lanSel) lanSel.disabled = locked || !lanConfirmedOff() || startBlockedByLegacy() || !lanAddresses().length;
    var frp = lanStatus && (lanStatus.relay_connector || { state: 'missing' });
    var frpInstalled = !!frp && frp.state === 'installed';
    var frpEl = $('#kur-frp-connector');
    var frpNames = { missing: '未安装', installing: '正在安装…', installed: '已安装', failed: '安装失败' };
    if (frpEl) frpEl.textContent = 'frp 组件：' + (frp ? (frpNames[frp.state] || '状态未知')
      + (frp.version ? '（' + frp.version + '）' : '') : '状态待确认');
    var frpInstall = $('#kur-frp-install');
    if (frpInstall) {
      frpInstall.disabled = curMode() !== 'relay' || !canInstall(true);
      frpInstall.setAttribute('aria-disabled', frpInstall.disabled || !!inflight ? 'true' : 'false');
      frpInstall.classList.toggle('kur-hidden', frpInstalled);
    }
    var frpRow = $('#kur-frp-download-consent-row');
    if (frpRow) frpRow.classList.toggle('kur-hidden', frpInstalled);
    var frpDownload = $('#kur-frp-download-consent');
    if (frpDownload) frpDownload.disabled = frpInstalled || locked || !frp || frp.state === 'installing';
    var frpLocked = locked || !lanConfirmedOff() || startBlockedByLegacy();
    ['server-ip', 'server-port', 'remote-port', 'token', 'ca-cert', 'consent'].forEach(function(id) {
      var el = $('#kur-frp-' + id);
      if (el) el.disabled = frpLocked;
    });
    renderLegacyBanner();
    if (hadDialogFocus && document.activeElement === document.body) {
      var targets = focusables();
      if (targets.length) targets[0].focus();
    }
  }

  var MODE_NAMES = { lan: '内网', internet: 'CF 隧道', relay: '中继服务器' };

  function lanAddrsKey() { return lanAddresses().join('|'); }

  // 重建网卡下拉：仅在网卡列表变化时重建，避免轮询把用户正在展开的下拉收起；
  // 保留用户已选地址，未选时仅有一块网卡才自动选中（多网卡不预选，防止选到虚拟网卡）
  function renderLanAddrs() {
    var sel = $('#kur-lan-addr');
    var empty = $('#kur-lan-empty');
    if (!sel) return;
    var addrs = lanAddresses();
    var key = lanStatus ? lanAddrsKey() : null;
    if (key !== lanAddrKey) {
      lanAddrKey = key;
      if (selAddr && addrs.indexOf(selAddr) < 0) selAddr = '';
      if (!selAddr && addrs.length === 1) selAddr = addrs[0];
      while (sel.firstChild) sel.removeChild(sel.firstChild);
      if (addrs.length !== 1) {
        var ph = document.createElement('option');
        ph.value = '';
        ph.textContent = addrs.length ? '请选择网卡地址' : '无可用地址';
        sel.appendChild(ph);
      }
      addrs.forEach(function(a) {
        var o = document.createElement('option');
        o.value = a;
        o.textContent = a;
        sel.appendChild(o);
      });
    }
    sel.value = selAddr;
    if (empty) empty.classList.toggle('kur-hidden', !lanStatus || addrs.length > 0 || lanStatus.state !== 'off');
  }

  function renderModeUi() {
    var mode = curMode();
    var locked = modeLocked();
    ['lan', 'internet', 'relay'].forEach(function(m) {
      var btn = $('#kur-mode-' + m);
      if (!btn) return;
      var on = m === mode;
      btn.classList.toggle('kur-mode-on', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.disabled = locked && !on;
      btn.setAttribute('aria-disabled', btn.disabled ? 'true' : 'false');
    });
    var panels = { lan: '#kur-lan', internet: '#kur-internet', relay: '#kur-relay' };
    Object.keys(panels).forEach(function(m) {
      var p = $(panels[m]);
      if (p) p.classList.toggle('kur-hidden', m !== mode);
    });
    var hint = $('#kur-modehint');
    if (hint) {
      var text = '';
      if (!lanStatus) text = '';
      else if (bridgeBusy()) text = '当前为' + (MODE_NAMES[mode] || '') + '模式。如需切换，请先停止连接。';
      hint.textContent = text;
      hint.classList.toggle('kur-show', !!text);
    }
    var note = $('#kur-note');
    if (note) {
      var noteText = mode === 'lan' ? NOTE_LAN : (mode === 'internet' ? NOTE_INTERNET : NOTE_RELAY);
      if (note.textContent !== noteText) note.textContent = noteText;
    }
    renderLanAddrs();
  }

  function selectMode(m) {
    if (!isOpen || modeLocked() || m === selMode) return;
    selMode = m;
    saveMode(m);
    clearRelaySecrets();
    var consent = $('#kur-frp-consent');
    if (consent) consent.checked = false;
    hideLink();
    showMsg('', false);
    postError = '';
    postKind = '';
    setButtons();
    if (lanStatus) renderStatus(lanStatus);
  }

  // 官方中继仍在运行：横幅 + 显式停止入口，绝不自动停止或代切
  function renderLegacyBanner() {
    var banner = $('#kur-legacy');
    if (!banner) return;
    var textEl = $('#kur-legacy-text');
    var stopBtn = $('#kur-legacy-stop');
    var text = '';
    var stopShown = false;
    if (legacyOfficialActive()) {
      text = '检测到官方中继远程连接仍在运行。为避免两条通道同时在线，' +
        '请先显式停止该连接，再开启手机连接。';
      stopShown = true;
    }
    if (textEl) textEl.textContent = text;
    banner.classList.toggle('kur-show', !!text);
    banner.setAttribute('aria-hidden', text ? 'false' : 'true');
    if (stopBtn) {
      stopBtn.style.display = stopShown ? '' : 'none';
      stopBtn.disabled = !stopShown || !api || !!inflight;
      stopBtn.setAttribute('aria-disabled', stopBtn.disabled ? 'true' : 'false');
    }
  }

  function validUrlFrom(status) {
    if ((inflight && inflight.stopping) || !status || status.state !== 'on' || !status.url) return '';
    if (!status.enabled) return '';
    if (status.mode === 'internet' || status.mode === 'relay') {
      if (!status.tunnel || status.tunnel.state !== 'ready') return '';
    } else if (status.mode !== 'lan') return '';
    var MV = window.KimiMobileAPI && window.KimiMobileAPI.validateRemoteURL;
    if (typeof MV !== 'function') return '';
    try { return MV(status.url, status); } catch (e) { return ''; }
  }

  function renderQr(url) {
    var wrap = $('#kur-qr');
    var box = $('#kur-qrbox');
    if (!wrap || !box) return;
    if (!url) { wrap.classList.remove('kur-show'); box.innerHTML = ''; return; }
    var Q = window.KimiRemoteQR;
    var svg = '';
    if (Q && typeof Q.toSVG === 'function') {
      try { svg = Q.toSVG(url); } catch (e) { svg = ''; }
    }
    if (svg) {
      box.innerHTML = svg;
    } else {
      // 二维码不可用：链接仍然可选可复制
      box.innerHTML = '';
      var fb = document.createElement('div');
      fb.className = 'kur-qr-fb';
      fb.textContent = '二维码不可用，请复制上方链接到手机浏览器打开。';
      box.appendChild(fb);
    }
    wrap.classList.add('kur-show');
  }

  function hideLink(skipButtons) {
    var linkwrap = $('#kur-linkwrap');
    var moveFocus = false;
    if (isOpen && isShown(linkwrap)) {
      var active = document.activeElement;
      while (active && active !== overlayEl) {
        if (active === linkwrap) { moveFocus = true; break; }
        active = active.parentNode;
      }
    }
    linkOk = false;
    var link = $('#kur-link');
    if (linkwrap) {
      linkwrap.classList.remove('kur-show');
      linkwrap.setAttribute('aria-hidden', 'true');
    }
    if (link) link.value = '';
    renderQr('');
    if (!skipButtons) {
      setButtons();
      if (moveFocus) {
        var targets = focusables();
        if (targets.length) targets[0].focus();
      }
    }
  }

  function safeError(e) {
    var code = e && e.code;
    var messages = {
      CONNECTOR_MISSING: '请先安装外网连接组件。',
      CONNECTOR_INSTALL_FAILED: '组件安装失败，请确认后再试。',
      CONNECTOR_HASH_MISMATCH: '组件完整性校验失败，无法使用。',
      CONNECTOR_UNSUPPORTED: '当前系统不支持此外网组件。',
      CONNECTOR_BUSY: '组件正在处理其他操作，请稍候。',
      CONSENT_REQUIRED: '请分别明确同意组件下载和本次中转风险。',
      RELAY_CONFIG_INVALID: '中继配置无效：请检查公网 IPv4、不同的端口、32–512 位无空白 ASCII token 和 PEM CA 证书。',
      TUNNEL_START_FAILED: '外网通道启动失败，请停止后再试。',
      TUNNEL_TIMEOUT: '外网通道启动超时，请检查网络。',
      TUNNEL_EXITED: '外网通道已断开，请停止后重新开启。',
      OWNER_LOST: '桌面端服务已断开，连接已撤销。',
      SERVER_TOKEN_UNAVAILABLE: '手机连接凭据创建或读取失败，请检查目录权限。',
      OWNER_AUTH_FAILED: '桌面端拒绝手机连接凭据，请停止后重试或升级桌面端。',
      OWNER_AUTH_CHECK_FAILED: '无法验证桌面端认证，请稍后重试。',
      START_CANCELLED: '连接启动已取消。',
      // worker 启动阶段码（与 mobile_worker stage 表一致，固定中文文案）
      WORKER_STATE_DIR_UNAVAILABLE: '手机连接助手工作目录不可用，请检查插件安装。',
      WORKER_STARTUP_BUSY: '有其他启动操作正在进行，请稍候再试。',
      WORKER_LOCK_HELD: '已有手机连接实例在运行但身份无法核实，请稍候再试。',
      WORKER_SPAWN_DENIED: '系统拒绝以独立进程启动手机连接助手。',
      WORKER_SPAWN_FAILED: '无法启动手机连接独立进程，请稍后重试。',
      WORKER_CHILD_EXITED: '手机连接独立进程启动后立即退出，请稍后重试。',
      WORKER_BOOT_TIMEOUT: '手机连接独立进程启动超时，请稍后重试。',
      WORKER_VERSION_MISMATCH: '手机连接助手版本与当前插件不一致，请更新后重试。',
      MOBILE_TIMEOUT: '请求超时，正在通过状态查询确认结果；不会自动重试操作。',
      MOBILE_NETWORK: '无法连接本机插件服务，请检查服务状态。',
      MOBILE_BAD_RESPONSE: '服务状态格式异常，已隐藏连接地址。',
      MOBILE_UNSUPPORTED: '插件服务不支持手机连接，请升级后重试。',
      MOBILE_AUTH: '控制请求被拒绝，请从桌面端面板操作。',
      REMOTE_AUTH: '官方接口未授权，请在 Kimi Code 中登录后重新打开面板。',
      REMOTE_UNSUPPORTED: '当前桌面端不支持官方远程接口，请升级后重试。',
      REMOTE_TIMEOUT: '官方接口请求超时，正在查询状态确认结果。',
      REMOTE_NETWORK: '无法连接官方接口的本机桌面端服务。',
      REMOTE_BAD_RESPONSE: '官方接口状态格式异常，无法确认连接状态。',
      REMOTE_HTTP: '官方接口请求未成功，请检查桌面端服务。',
      REMOTE_API_ERROR: '官方远程操作未成功，请等待状态查询。',
      REMOTE_REDIRECT: '官方接口响应来源异常，已拒绝重定向。',
      REMOTE_BASE_INVALID: '无法确认官方接口的本机服务地址。',
      REMOTE_BASE_REJECTED: '官方接口服务来源不受信任，已阻止调用。',
      REMOTE_ENV: '当前环境无法调用官方接口，请重新打开桌面面板。',
      REMOTE_URL_REJECTED: '官方连接地址不受信任，已隐藏。',
      REMOTE_BAD_ARG: '官方接口操作参数无效，已拒绝请求。'
    };
    return typeof code === 'string' && Object.prototype.hasOwnProperty.call(messages, code)
      ? messages[code] : '连接请求未成功，请等待状态查询后再操作。';
  }

  // 隧道重连诊断：仅渲染服务端已投影的 connector_diagnostics，只读、纯数字；
  // 不做整体健康推断，不触发任何动作（绝不停用/换码/新建通道）
  function renderTunnelDiag(s) {
    var box = $('#kur-tunnel-diag');
    if (!box) return;
    var textEl = $('#kur-tunnel-diag-text');
    var detailEl = $('#kur-tunnel-diag-detail');
    var dg = s && s.connector_diagnostics;
    var reconnecting = !!dg && dg.transport_state === 'reconnecting';
    var label = reconnecting ? '隧道正在重连，短暂中断可能影响上传' : '';
    if (textEl) textEl.textContent = label;
    var parts = [];
    if (reconnecting && dg.counts) {
      if (typeof dg.counts.origin_request_failed === 'number') parts.push('源站请求失败 ' + dg.counts.origin_request_failed + ' 次');
      if (typeof dg.counts.connection_unregistered === 'number') parts.push('断连 ' + dg.counts.connection_unregistered + ' 次');
      if (typeof dg.counts.connection_retrying === 'number') parts.push('重试 ' + dg.counts.connection_retrying + ' 次');
      if (typeof dg.active_conn_count === 'number') parts.push('当前在线通道 ' + dg.active_conn_count + '/4');
    }
    var detail = parts.join(' · ');
    if (detailEl) {
      detailEl.textContent = detail;
      detailEl.style.display = detail ? '' : 'none';
    }
    // 容器同步整行文本（真实浏览器由子节点自然组成；此处同时保证无布局环境可读）
    box.textContent = reconnecting ? label + (detail ? ' ' + detail : '') : '';
    box.classList.toggle('kur-show', reconnecting);
    box.setAttribute('aria-hidden', reconnecting ? 'false' : 'true');
  }

  // POST 失败文案只在对应动作被**正面确认**后清除：start→状态 on、install→组件
  // installed。简单 GET 返回 off（无 error）不清除——那可能是真实失败仍待用户读取；
  // rotate/official 等无正面确认的动作也一律保留，新失败始终保留供诊断。
  function clearPostIfConfirmed(s) {
    if (msgSrc !== 'post' || !postError) return;
    s = s || {};
    var st = STATES[s.state] ? s.state : '';
    var ok = (postKind === 'start' && st === 'on')
      || (postKind === 'install' && !!s.connector && s.connector.state === 'installed')
      || (postKind === 'install-relay' && !!s.relay_connector && s.relay_connector.state === 'installed');
    if (!ok) return;
    postError = '';
    postKind = '';
    showMsg('', false);
  }

  function renderStatus(s) {
    s = s || {};
    var st = STATES[s.state] ? s.state : '';
    if (st === 'off' && curState && curState !== 'off') clearRelaySecrets();
    curState = st;
    if (st && st !== 'off' && MODE_NAMES[s.mode]) selMode = s.mode;
    if (s.mode === 'relay' && st === 'on' && s.public_origin) {
      var endpoint = /^http:\/\/([0-9.]+):([1-9][0-9]{0,4})$/.exec(s.public_origin);
      if (endpoint) {
        var ip = $('#kur-frp-server-ip');
        var port = $('#kur-frp-remote-port');
        if (ip) ip.value = endpoint[1];
        if (port) port.value = endpoint[2];
      }
    }
    var url = validUrlFrom(s);
    var onNoUrl = st === 'on' && !url;
    var pairUsed = onNoUrl && s.pair_state === 'used';
    var pairExpired = onNoUrl && s.pair_state === 'expired';
    var tunnelFailed = s.mode === curMode() && s.mode !== 'lan' && s.tunnel && s.tunnel.state === 'failed';
    var stateEl = $('#kur-state');
    var dot = $('#kur-dot');
    var stateText = STATES[st] || '状态待确认';
    if (onNoUrl) {
      stateText = tunnelFailed ? '外网通道已断开' : (pairUsed ? '已开启（配对码已使用）'
        : (pairExpired ? '已开启（配对码已过期）' : '已开启（无可用连接地址）'));
    }
    if (stateEl) stateEl.textContent = stateText;
    if (dot) {
      var healthy = st === 'on' && !tunnelFailed && (!onNoUrl || pairUsed);
      dot.classList.toggle('kur-on', healthy);
      dot.classList.toggle('kur-mid', !healthy && (st === 'starting' || st === 'stopping' || onNoUrl));
    }
    var dev = $('#kur-device');
    if (dev) {
      var parts = [];
      if (s.device_name) parts.push(s.device_name);
      if (s.device_id) parts.push('ID ' + s.device_id);
      if (st === 'on' && typeof s.device_count === 'number') parts.push('已连设备 ' + s.device_count + ' 台');
      dev.textContent = parts.join(' · ');
      dev.style.display = parts.length ? '' : 'none';
    }
    if (url) {
      linkOk = true;
      var linkwrap = $('#kur-linkwrap');
      var link = $('#kur-link');
      if (linkwrap) {
        linkwrap.classList.add('kur-show');
        linkwrap.setAttribute('aria-hidden', 'false');
      }
      if (link) link.value = url;
      renderQr(url);
    } else {
      hideLink(true);
    }
    var worker = $('#kur-worker');
    if (worker) {
      var notice = typeof s.worker_notice === 'string' ? s.worker_notice : '';
      worker.textContent = notice;
      worker.classList.toggle('kur-show', !!notice);
      worker.setAttribute('aria-hidden', notice ? 'false' : 'true');
    }
    renderTunnelDiag(s.mode === 'internet' && curMode() === 'internet' ? s : null);
    clearPostIfConfirmed(s);
    var mode = curMode();
    var sameMode = s.mode === mode;
    var connector = mode === 'relay' ? s.relay_connector : (mode === 'internet' ? s.connector : null);
    var topError = sameMode || !/^(CONNECTOR_|TUNNEL_|RELAY_)/.test(s.error_code || '') ? s.error_code : '';
    var errorCode = topError || (sameMode && mode !== 'lan' && s.tunnel && s.tunnel.error_code)
      || (connector && connector.error_code);
    var priority = msgSrc === 'post';   // POST 落地错误优先于轮询级提示，不被对账 GET 覆盖
    if (errorCode) showMsg(priority ? postError : safeError({ code: errorCode }), true, priority ? 'post' : 'req');
    else if (tunnelFailed) {
      showMsg(priority ? postError : '外网通道进程已退出，旧链接已不可用。请停止后重新开启，以生成新的链接和二维码。', true, priority ? 'post' : 'req');
    } else if (pairUsed) {
      showMsg(priority ? postError : '配对码已使用。如需连接另一台手机，点击“生成新二维码”即可换发新码（不会断开已连设备）。', priority ? true : 'warn', priority ? 'post' : 'req');
    } else if (pairExpired) {
      showMsg(priority ? postError : '配对码已过期。点击“生成新二维码”即可换发新码（约 10 分钟有效、仅可使用一次）。', priority ? true : 'warn', priority ? 'post' : 'req');
    } else if (onNoUrl) showMsg(priority ? postError : '连接已开启，但暂无可用连接地址。', true, priority ? 'post' : 'req');
    else if (priority) showMsg(postError, true, 'post');
    else if (msgSrc === 'req') showMsg('', false);
    setButtons();
  }

  /* ---------------- API 调用 ---------------- */
  function ensureApis() {
    if (!api) {
      offErr = '';
      var A = window.KimiRemoteAPI;
      if (A && typeof A.create === 'function') {
        try {
          api = A.create({ window: window, fetch: window.fetch && window.fetch.bind(window) });
        } catch (e) {
          api = null;
          offErr = '官方中继状态查询不可用';
        }
      }
    }
    if (!mobileApi) {
      lanStatus = null;
      lanErr = '模块不可用，请确认移动连接脚本完整加载。';
      var M = window.KimiMobileAPI;
      if (M && typeof M.create === 'function') {
        try {
          mobileApi = M.create({ window: window, fetch: window.fetch && window.fetch.bind(window) });
          if (mobileApi) lanErr = '';
        } catch (e) {
          mobileApi = null;
        }
      }
    }
    return mobileApi;
  }

  function renderFailure(req) {
    if (req.gen !== gen || !isOpen || req.seq !== seq) return;
    lanStatus = null;
    curState = '';
    linkOk = false;
    try {
      hideLink();
      showMsg('显示状态未成功，正在重新查询；请勿重复操作。', true, 'req');
    } catch (e) { /* DOM 故障不得阻断锁释放与后续 GET */ }
  }

  function reqDone(req) {
    if (inflight !== req) return;
    inflight = null;
    try { setButtons(); } catch (e) { renderFailure(req); }
    if (wantRefresh || (isOpen && wantPoll && req.kind === 'POST')) {
      wantRefresh = false;
      pollOnce();
      return;
    }
    schedulePoll();
  }

  function cancelPollTimer() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function wrap(run) {
    return Promise.resolve().then(run).then(
      function(s) { return { status: s }; },
      function(e) { return { error: safeError(e) }; });
  }

  // 官方中继状态仅用于“旧版仍在运行”的提示探测：独立于外网轮询，
  // 有自己的完成与重绘路径，绝不 Promise.all 阻塞外网状态落地。
  function probeOfficial() {
    if (!api || !isOpen || offReq) return;
    var req = { gen: gen };
    offReq = req;
    wrap(function() { return api.status(); }).then(function(r) {
      if (offReq !== req) return;
      offReq = null;
      if (req.gen !== gen || !isOpen) return;
      if (r.status) { offStatus = r.status; offErr = ''; }
      else { offStatus = null; offErr = r.error || '查询失败'; }
      try { setButtons(); } catch (e) { /* 官方探测失败不影响外网 */ }
    });
  }

  function pollOnce() {
    if (!isOpen || inflight || !mobileApi) return;
    cancelPollTimer();
    probeOfficial();
    var req = { kind: 'GET', gen: gen, seq: ++seq };
    inflight = req;
    Promise.resolve().then(function() {
      setButtons();
      return wrap(function() { return mobileApi.status(); });
    }).then(function(r) {
      if (r.status) {
        if (req.gen !== gen || !isOpen) return;
        if (req.seq < seq) return;
        seq = req.seq;                 // 与 POST 同号落地：先记序号再渲染，避免被当陈旧
        lanStatus = r.status;
        lanErr = '';
        renderStatus(lanStatus);
      } else {
        if (req.gen !== gen || !isOpen || req.seq !== seq) return;
        seq = req.seq;
        lanStatus = null;
        lanErr = r.error || '查询失败';
        renderFailure(req);
        if (isOpen && req.gen === gen) showMsg('状态查询失败：' + lanErr, true, 'req');
      }
    }).catch(function() { renderFailure(req); })
      .then(function() { reqDone(req); }, function() { reqDone(req); });
  }

  function schedulePoll() {
    if (pollTimer || !isOpen || !wantPoll || inflight) return;
    pollTimer = setTimeout(function() {
      pollTimer = null;
      pollOnce();
    }, POLL_MS);
  }

  function startPolling() {
    wantPoll = true;
    schedulePoll();
  }
  function stopPolling() {
    wantPoll = false;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    // 不动 inflight/offReq：停表不结束在途请求
  }

  // official=true：操作官方中继（仅旧版停止入口），结果簿记到 offStatus，不动外网状态/链接
  function runPost(operation, stopping, official, kind) {
    cancelPollTimer();
    postError = '';
    postKind = kind || '';
    if (msgSrc === 'post') showMsg('', false);
    var req = { kind: 'POST', gen: gen, seq: ++seq, stopping: stopping === true };
    inflight = req;
    if (req.stopping && !official) {
      curState = '';
      try {
        hideLink();
        var st = $('#kur-state');
        if (st) st.textContent = '正在停止（状态待确认）';
      } catch (e) { renderFailure(req); }
    }
    Promise.resolve().then(function() {
      setButtons();
      return operation();
    }).then(function(s) {
      if (req.gen !== gen || !isOpen) return;
      if (req.seq < seq) return;
      seq = req.seq;
      if (official) {
        offStatus = s;
        offErr = '';
        setButtons();
      } else {
        lanStatus = s;
        lanErr = '';
        renderStatus(s);
      }
    }).catch(function(e) {
      if (req.gen !== gen || !isOpen || req.seq !== seq) return;
      // POST 落地错误保留到重试或新的显式动作：对账 GET 不得清除，绝不显示假成功
      postError = safeError(e);
      if (official) {
        offErr = '状态未确认';
        setButtons();
      } else {
        lanStatus = null;
        lanErr = '状态未确认';
        curState = '';
        hideLink();
        var st = $('#kur-state');
        if (st) st.textContent = '状态待确认';
        var d = $('#kur-dot');
        if (d) { d.classList.remove('kur-on'); d.classList.remove('kur-mid'); }
      }
      showMsg(postError, true, 'post');
    }).then(function() { reqDone(req); }, function() { renderFailure(req); reqDone(req); });
  }

  function setEnabled(on) {
    if (!isOpen || inflight) return;
    ensureApis();
    // 停止（on=false）是幂等安全动作：状态未知（GET 失败）时也放行；
    // 仅在已确认 off 时拦截
    if (!mobileApi || (on && !canStart()) || (!on && !!lanStatus && lanStatus.state === 'off')) return;
    if (!on) {
      clearRelaySecrets();
      runPost(function() { return mobileApi.setEnabled(false); }, true, false, 'stop');
      return;
    }
    if (curMode() === 'lan') {
      var addr = selAddr;
      runPost(function() { return mobileApi.setEnabled(true, addr, 'lan'); }, false, false, 'start');
      var lanConsent = $('#kur-lan-consent');
      if (lanConsent) lanConsent.checked = false;
      return;
    }
    if (curMode() === 'relay') {
      var config = readRelayConfig();
      if (!config) { showMsg(safeError({ code: 'RELAY_CONFIG_INVALID' }), true, 'req'); return; }
      var frpConsent = checked('#kur-frp-consent');
      runPost(function() {
        try { return mobileApi.setEnabled(true, undefined, 'relay', frpConsent, config); }
        finally {
          config.token = '';
          config.ca_cert = '';
          config = null;
          clearRelaySecrets();
        }
      }, false, false, 'start');
      var frp = $('#kur-frp-consent');
      if (frp) frp.checked = false;
      return;
    }
    var relayConsent = checked('#kur-relay-consent');
    runPost(function() {
      return mobileApi.setEnabled(true, undefined, 'internet', relayConsent);
    }, false, false, 'start');
    var relay = $('#kur-relay-consent');
    if (relay) relay.checked = false;
  }

  function installConnector() {
    if (!isOpen || !canInstall()) return;
    var mode = curMode();
    runPost(function() { return mode === 'relay' ? mobileApi.installConnector(true, 'relay')
      : mobileApi.installConnector(true); }, false, false, mode === 'relay' ? 'install-relay' : 'install');
    var consent = $(mode === 'relay' ? '#kur-frp-download-consent' : '#kur-download-consent');
    if (consent) consent.checked = false;
  }

  // 更换配对码：仅重发一次性配对码，不重建通道、不断开已连设备；POST 锁内只允许一次
  // 失败时旧链接原样保留（旧 token 未变），错误文案保留到重试/新动作
  function rotatePair() {
    if (!isOpen || !canRotate()) return;
    runPost(function() { return mobileApi.rotatePair(); }, false, false, 'rotate');
  }

  // 旧版官方中继的显式停止：共享 POST 锁与错误/对账语义，但不触碰外网状态与链接
  function stopOfficial() {
    if (!isOpen || inflight || !api || !legacyOfficialActive()) return;
    runPost(function() { return api.setEnabled(false); }, false, true, 'official');
  }

  function copyLink() {
    var link = $('#kur-link');
    var url = link && link.value;
    if (!url || !linkOk) { showMsg('暂无可复制的连接地址。', true, 'req'); return; }
    var expires = lanStatus && lanStatus.expires_at;
    var ms = expires > 1e12 ? expires : expires * 1000;
    if (!lanStatus || validUrlFrom(lanStatus) !== url || (expires !== undefined && ms <= Date.now())) {
      hideLink();
      showMsg('配对链接已失效，请等待状态查询或点击“更换配对码”。', 'warn', 'req');
      return;
    }
    var myGen = gen;
    // 复制途中浮层关闭/链接刷新换掉都算失效
    var live = function() { return myGen === gen && isOpen && linkOk && link.value === url; };
    var done = function() {
      if (!live()) return;
      showMsg('链接已复制。该链接含配对授权，请勿分享；粘贴到手机系统浏览器打开。', false, 'local');
    };
    var manual = function() {
      if (!live()) return;
      try { link.focus(); link.select(); link.setSelectionRange(0, link.value.length); } catch (e) {}
      showMsg('复制失败：请长按或全选上方地址手动复制。', true, 'local');
    };
    var fallback = function() {
      if (!live()) return;
      try {
        link.focus(); link.select();
        if (document.execCommand && document.execCommand('copy')) done();
        else manual();
      } catch (e) { manual(); }
    };
    var nav = window.navigator;
    if (nav && nav.clipboard && typeof nav.clipboard.writeText === 'function') {
      Promise.resolve().then(function() {
        if (!live()) return;
        return nav.clipboard.writeText(url);
      }).then(done, fallback);
      return;
    }
    fallback();
  }

  function toggleNote(force) {
    var note = $('#kur-note');
    var btn = $('#kur-note-toggle');
    if (!note || !btn) return;
    var open = typeof force === 'boolean' ? force : note.classList.contains('kur-hidden');
    note.classList.toggle('kur-hidden', !open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  /* ---------------- 开合 ---------------- */
  function open() {
    if (isOpen && overlayEl && overlayEl.parentNode) return;
    gen++;
    seq = 0;
    wantRefresh = false;
    curState = '';
    linkOk = false;
    // inflight/busy 不重置：在途 POST 的锁跨开关存活
    try { lastFocus = document.activeElement; } catch (e) { lastFocus = null; }

    ensureStyle(document);
    if (!overlayEl || !overlayEl.parentNode) {
      overlayEl = buildOverlay(document);
      document.body.appendChild(overlayEl);
      overlayEl.addEventListener('click', function(e) { if (e.target === overlayEl) close(); });
      overlayKeyHandler = function(e) {
        if (e && (e.key === 'Tab' || e.keyCode === 9)) trapTab(e);
      };
      overlayEl.addEventListener('keydown', overlayKeyHandler);
      var bind = function(id, fn) {
        var el = overlayEl.querySelector(id);
        if (el) el.addEventListener('click', function(e) { e.stopPropagation(); fn(); });
      };
      bind('#kur-x', close);
      bind('#kur-install', installConnector);
      bind('#kur-frp-install', installConnector);
      ['server-ip', 'server-port', 'remote-port', 'token', 'ca-cert'].forEach(function(id) {
        var el = $('#kur-frp-' + id);
        if (el) el.addEventListener('input', setButtons);
      });
      bind('#kur-enable', function() { setEnabled(true); });
      bind('#kur-disable', function() { setEnabled(false); });
      bind('#kur-copy', copyLink);
      bind('#kur-rotate', rotatePair);
      bind('#kur-legacy-stop', stopOfficial);
      bind('#kur-note-toggle', toggleNote);
      ['lan', 'internet', 'relay'].forEach(function(m) {
        bind('#kur-mode-' + m, function() { selectMode(m); });
      });
      ['#kur-download-consent', '#kur-relay-consent', '#kur-lan-consent', '#kur-frp-download-consent', '#kur-frp-consent'].forEach(function(id) {
        var el = $(id);
        if (el) el.addEventListener('change', setButtons);
      });
      var addrSel = $('#kur-lan-addr');
      if (addrSel) addrSel.addEventListener('change', function() {
        selAddr = addrSel.value;
        setButtons();
      });
    }
    isOpen = true;
    toggleNote(false);
    overlayEl.classList.add('kur-open');
    ['#kur-download-consent', '#kur-relay-consent', '#kur-lan-consent', '#kur-frp-download-consent', '#kur-frp-consent'].forEach(function(id) {
      var el = $(id);
      if (el) el.checked = false;
    });
    selMode = loadMode();
    lanAddrKey = null;

    if (!docKeyHandler) {
      docKeyHandler = function(e) {
        if (e && (e.key === 'Escape' || e.keyCode === 27)) close();
      };
      document.addEventListener('keydown', docKeyHandler);
    }

    // 焦点进对话框（aria-modal 需要焦点容器配合）
    try {
      var x = $('#kur-x');
      if (x && typeof x.focus === 'function') x.focus();
    } catch (e) {}

    if (!ensureApis()) {
      renderStatus({});
      showMsg(lanErr || '移动连接模块不可用。', true, 'req');
      setButtons();
    } else {
      lanStatus = null;
      offStatus = null;
      showMsg('', false);
      var st = $('#kur-state');
      if (st) st.textContent = '查询中…';
      var d = $('#kur-dot');
      if (d) { d.classList.remove('kur-on'); d.classList.remove('kur-mid'); }
      hideLink();
      setButtons();
      if (inflight) wantRefresh = true;   // 在途请求属旧 gen：落地后立即补当前 gen 的 GET
      else pollOnce();
      startPolling();
    }
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    gen++;                    // 丢弃所有在途响应
    wantRefresh = false;
    stopPolling();
    clearRelaySecrets();
    if (overlayEl) overlayEl.classList.remove('kur-open');
    if (docKeyHandler) {
      document.removeEventListener('keydown', docKeyHandler);
      docKeyHandler = null;
    }
    var f = lastFocus;
    lastFocus = null;
    try {
      if (f && typeof f.focus === 'function' && document.contains && document.contains(f)) f.focus();
    } catch (e) {}
  }

  window.KimiRemoteWidget = { open: open, close: close };
})();
