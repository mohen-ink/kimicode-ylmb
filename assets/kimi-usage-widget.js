/**
 * Kimi Code 用量监控 · 一体化自包含组件 v3.0
 * ---------------------------------------------------------------
 * 三源合并：
 *   1) assets/kimi-usage-widget.js          —— 侧栏常驻卡片（基底，样式体系/折叠记忆/挂载结构全保留）
 *   2) assets/kimi-usage-panel-src.html     —— 全屏用量报表（原 iframe 方案改为内嵌渲染，kup- 前缀）
 *   3) core/kimi-embedded-widget.js         —— 模型管理弹窗（kmm- 前缀，__KMM_* 回调与 API 操作）
 *
 * 数据源：window.__KIMI_DATA__ = { usage, models, service, time }
 *   由 /assets/kimi-usage-data.js 注入，重写后回调 window.__KMM_ON_DATA_UPDATE__。
 *   兜底：fetch('/kimi-usage.json')。
 * 管理 API：http://127.0.0.1:39281
 */
(function() {
  if (window.__KIMI_USAGE_WIDGET_INIT__) return;
  window.__KIMI_USAGE_WIDGET_INIT__ = true;

  const CARD_ID = 'kimi-usage-card';
  const PANEL_MODAL_ID = 'kimi-usage-panel-modal';
  const KMM_OVERLAY_ID = 'kmm-overlay';
  const STORAGE_COLLAPSED_KEY = 'kimi-usage-collapsed-state';
  const API_BASE = 'http://127.0.0.1:39281';
  const STALE_MS = 15000;
  // 39281 控制面要求自定义头做本机写守卫；所有 API_BASE 请求统一走此封装，
  // 不拦原生 fetch，也不影响 /kimi-usage.json 静态数据兜底
  const API_HEADERS = { 'X-Kimi-Usage-Control': '1' };
  function kapiFetch(url, opts) {
    opts = opts || {};
    var h = {};
    for (var k in API_HEADERS) h[k] = API_HEADERS[k];
    if (opts.headers) for (var k2 in opts.headers) h[k2] = opts.headers[k2];
    opts.headers = h;
    return fetch(url, opts);
  }

  let activeTab = 'today';
  let isCollapsed = false;
  try {
    isCollapsed = localStorage.getItem(STORAGE_COLLAPSED_KEY) === 'true';
  } catch (e) {}
  const STORAGE_MIN_MODEL_KEY = 'kimi-usage-min-model-html';
  let kuLastMinHtml = '';
  try { kuLastMinHtml = localStorage.getItem(STORAGE_MIN_MODEL_KEY) || ''; } catch (e) {}

  function kuMinModelHtml() {
    const rows = (state.today_models && state.today_models.length)
      ? state.today_models
      : (state.cumul_models || []);
    const top = rows[0];
    if (!top || !top.model || top.model === '--') return '';
    const name = String(top.model).split('/').pop();
    const cp = kupCacheTxt(top);
    return esc(name) + ' · <b>' + esc(top.tokens_fmt || fmtTok(top.tokens))
      + '</b> <span class="c">' + esc(cp) + '</span>'
      + ' <span class="m">' + esc(top.cost_fmt || '--') + '</span>';
  }

  let state = {
    updated_at: '--',
    header: { tokens_fmt: '--', cost_fmt: '--', cache_pct: '--', speed_tps: '--' },
    today: { tokens_fmt: '--', cost: 0, cost_fmt: '--', calls: 0, in_fmt: '--', out_fmt: '--', cache_pct: 0 },
    yesterday: { tokens_fmt: '--', cost_fmt: '--', calls: 0 },
    week: { tokens_fmt: '--', cost_fmt: '--', calls: 0 },
    month: { tokens_fmt: '--', cost_fmt: '--', calls: 0 },
    cumul: { tokens_fmt: '--', cost: 0, cost_fmt: '--', calls: 0 },
    rate: { tokens_per_hour: '--', cost_per_hour: '--', window: '近60分钟' },
    cache: { pct: 0, pct_fmt: '--' },
    speed: { tps: '--', model: '--', avg_tps: '--' },
    cur: { alias: '', model: '', effort: '', session: '', sub_agent: '' },
    session: null,
    quota: null,
    days30: [],
    hourly: [],
    timeseries: null,
    today_models: [],
    cumul_models: [],
    sessions: [],
    footer: { file_count: 0, session_count: 0, time: '--' }
  };
  let modelsData = null;
  let serviceOnline = false;
  let lastDataTime = 0;   // __KIMI_DATA__.time (epoch ms) 或 JSON updated_at
  let apiAlive = false;   // /api/status 探活结果
  let kmmOverlayEl = null;
  let isModelModalOpen = false;
  let kupWavePeriod = 'week';
  let kupModelTab = 'today';
  let kupTheme = 'dark';
  let kupChecking = false;
  let kupUpdating = false;
  let kupUninstalling = false;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function shortName(alias) {
    alias = String(alias || '');
    if (!alias || alias === '--') return '--';
    return alias.split('/').pop();
  }
  function parseTimeMs(v) {
    if (!v) return 0;
    if (typeof v === 'number') return v;
    var n = Number(v);
    if (n > 1e9) return n;
    var t = Date.parse(String(v).replace(' ', 'T'));
    return isNaN(t) ? 0 : t;
  }
  function serviceTitle() {
    if (serviceOnline) return '后台服务运行中（端口 39281）· 数据实时推送';
    return '数据已过期（服务未运行）';
  }
  function applyServiceFlag() {
    const card = document.getElementById(CARD_ID);
    if (!card) return;
    card.querySelectorAll('.ku-dot').forEach(d => {
      d.classList.toggle('ku-dot-off', !serviceOnline);
      d.title = serviceTitle();
    });
  }

  /* ---------- 弹层开合动画（opacity/transform 渐进，关闭延迟隐藏） ---------- */
  const KU_ANIM_MS = 190;
  function kuLayerShow(el, cls) {
    if (!el) return;
    clearTimeout(el._kuHideTimer);
    el.classList.add(cls);                       // display:flex 立即响应
    el.classList.remove('ku-anim-in');
    void el.offsetWidth;                         // 强制回流，确保过渡生效
    requestAnimationFrame(function() { el.classList.add('ku-anim-in'); });
  }
  function kuLayerHide(el, cls) {
    if (!el || !el.classList.contains(cls)) return;
    el.classList.remove('ku-anim-in');           // 先播关闭动画
    clearTimeout(el._kuHideTimer);
    el._kuHideTimer = setTimeout(function() { el.classList.remove(cls); }, KU_ANIM_MS);
  }
  // 空闲时调度重渲染：面板先出现，内容异步填充
  function kuScheduleIdle(fn) {
    var done = false;
    var run = function() {
      if (done) return; done = true;
      try { fn(); } catch (e) {}
    };
    if (typeof window.requestIdleCallback === 'function') {
      try { window.requestIdleCallback(run, { timeout: 400 }); } catch (e) { run(); }
    } else {
      requestAnimationFrame(function() { setTimeout(run, 0); });
    }
    setTimeout(run, 450); // 兜底，确保一定会执行
  }

  /* ---------- 侧栏详情小弹层（模型 / 会话 完整信息，hover 触发） ---------- */
  var kuPopEl = null;
  var kuPopShowT = null;
  var kuPopHideT = null;
  function kuClosePop() {
    clearTimeout(kuPopShowT);
    if (kuPopEl) { try { kuPopEl.remove(); } catch (e) {} kuPopEl = null; }
  }
  function kuScheduleClosePop() {
    clearTimeout(kuPopHideT);
    kuPopHideT = setTimeout(kuClosePop, 140);
  }
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && kuPopEl) kuClosePop();
  }, true);
  function kuOpenPop(anchor, title, rowsHtml) {
    if (!anchor || !document.contains(anchor)) { kuClosePop(); return; }
    if (kuPopEl && kuPopEl._anchor === anchor && document.body.contains(kuPopEl)) {
      var html = '<div class="ku-pop-head"><span>' + esc(title) + '</span></div>' + rowsHtml;
      if (kuPopEl._html !== html) { kuPopEl.innerHTML = html; kuPopEl._html = html; }
      return;
    }
    kuClosePop();
    var pop = document.createElement('div');
    pop.className = 'ku-pop';
    pop._anchor = anchor;
    pop._html = '<div class="ku-pop-head"><span>' + esc(title) + '</span></div>' + rowsHtml;
    pop.innerHTML = pop._html;
    pop.addEventListener('mouseenter', function() { clearTimeout(kuPopHideT); });
    pop.addEventListener('mouseleave', kuScheduleClosePop);
    document.body.appendChild(pop);
    var r = anchor.getBoundingClientRect();
    var pw = Math.min(252, window.innerWidth - 12);
    var x = Math.max(6, Math.min(r.left, window.innerWidth - pw - 6));
    var y = r.bottom + 6;
    pop.style.width = pw + 'px';
    var ph = pop.offsetHeight || 120;
    if (y + ph > window.innerHeight - 6) y = Math.max(6, r.top - ph - 6);
    pop.style.left = x + 'px';
    pop.style.top = y + 'px';
    kuPopEl = pop;
  }
  function kuBindHoverPop(el, buildRows, title) {
    if (!el) return;
    el._kuPopBuild = buildRows;
    el._kuPopTitle = title || '详情';
    if (kuPopEl && kuPopEl._anchor === el) kuOpenPop(el, el._kuPopTitle, buildRows());
    if (el._kuPopBound) return;
    el._kuPopBound = true;
    el.addEventListener('mouseenter', function() {
      clearTimeout(kuPopHideT);
      clearTimeout(kuPopShowT);
      kuPopShowT = setTimeout(function() {
        kuOpenPop(el, el._kuPopTitle, el._kuPopBuild());
      }, 90);
    });
    el.addEventListener('mouseleave', function() {
      clearTimeout(kuPopShowT);
      kuScheduleClosePop();
    });
  }
  function kuPopRow(k, v) {
    return '<div class="ku-pop-row"><span class="ku-pop-k">' + esc(k) + '</span><span class="ku-pop-v">' + esc(v) + '</span></div>';
  }
  // 当前模型完整信息弹层
  function kuCurModelDetail(alias) {
    var cur = state.cur || {};
    var m = (modelsData && modelsData.models || []).find(function(x) { return x.alias === alias; }) || {};
    var effs = (m.effective_efforts && m.effective_efforts.length) ? m.effective_efforts
             : (m.support_efforts && m.support_efforts.length) ? m.support_efforts : null;
    var rows =
      kuPopRow('别名', alias) +
      (m.provider ? kuPopRow('提供方', m.provider) : '') +
      (m.model ? kuPopRow('模型', m.model) : '') +
      (m.display_name ? kuPopRow('显示名', m.display_name) : '') +
      kuPopRow('思考档位', cur.effort || m.default_effort || '--') +
      (effs ? kuPopRow('可用档位', effs.join(' / ')) : '') +
      kuPopRow('工具调用', m.has_tools ? '支持' : '不支持') +
      (m.has_thinking != null ? kuPopRow('深度思考', (m.has_thinking || m.always_thinking) ? '开启' : '关闭') : '') +
      (m.max_context_size ? kuPopRow('上下文', Math.round(m.max_context_size / 1024) + 'k') : '') +
      (cur.session ? kuPopRow('会话', cur.session) : '') +
      (cur.sub_agent ? kuPopRow('子代理', cur.sub_agent) : '');
    return rows;
  }

  /* ================================================================
   * 样式：卡片(ku-) + 报表(kup-) + 模型管理(kmm-)
   * ================================================================ */
  const style = document.createElement('style');
  style.id = 'kimi-usage-widget-styles';
  style.textContent = `
    /* ========== 1. 侧栏卡片 (ku-) ========== */
    #${CARD_ID} {
      margin: 6px 8px 6px 8px;
      padding: 11px 13px 15px 13px;
      background: color-mix(in srgb, var(--color-text, #000) 2.5%, var(--color-sidebar-bg, #fff));
      border: 1px solid color-mix(in srgb, var(--color-text, #000) 9%, transparent);
      border-radius: 11px;
      font-size: 11.5px;
      line-height: 1.45;
      color: var(--color-text, #1e293b);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
      box-sizing: border-box;
      user-select: none;
      transition: all 0.2s ease;
      position: relative;
      z-index: 2;
    }
    #${CARD_ID}:hover {
      border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 35%, transparent);
      box-shadow: 0 4px 16px rgba(0,0,0,0.06);
    }
    .ku-expanded-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 7px;
    }
    .ku-title-group {
      display: flex;
      align-items: center;
      gap: 6px;
      font-weight: 700;
      font-size: 12.5px;
      color: var(--color-text, #0f172a);
      flex: 1;
      min-width: 0;
      cursor: pointer;
    }
    .ku-title-group:hover .ku-title-text { color: var(--color-accent, #1a88ff); }
    .ku-dot {
      color: var(--color-accent, #1a88ff);
      font-size: 14px;
      line-height: 1;
      flex-shrink: 0;
      transition: color 0.3s;
    }
    .ku-dot-off { color: #9ca3af !important; }
    .ku-title-text {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      transition: color 0.15s;
    }
    .ku-header-actions {
      display: flex;
      align-items: center;
      gap: 5px;
      flex-shrink: 0;
    }
    .ku-btn-panel {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      height: 22px;
      padding: 0 8px;
      border-radius: 6px;
      border: 1px solid color-mix(in srgb, var(--color-accent, #1a88ff) 30%, transparent);
      background: color-mix(in srgb, var(--color-accent, #1a88ff) 8%, transparent);
      color: var(--color-accent, #1a88ff);
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
      font-family: inherit;
      transition: all 0.15s ease;
    }
    .ku-btn-panel:hover {
      background: var(--color-accent, #1a88ff);
      color: #fff;
      border-color: var(--color-accent, #1a88ff);
      transform: translateY(-0.5px);
      box-shadow: 0 2px 8px color-mix(in srgb, var(--color-accent, #1a88ff) 30%, transparent);
    }
    .ku-btn-panel:active { transform: translateY(0); }
    .ku-btn-collapse {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 22px;
      height: 22px;
      border-radius: 5px;
      border: 1px solid color-mix(in srgb, var(--color-text, #000) 12%, transparent);
      background: color-mix(in srgb, var(--color-text, #000) 3%, transparent);
      color: var(--color-text, #334155);
      font-size: 13px;
      font-weight: 700;
      line-height: 1;
      cursor: pointer;
      font-family: inherit;
      transition: all 0.15s ease;
    }
    .ku-btn-collapse:hover {
      background: color-mix(in srgb, var(--color-text, #000) 8%, transparent);
      color: var(--color-text, #0f172a);
      border-color: color-mix(in srgb, var(--color-text, #000) 25%, transparent);
    }
    .ku-summary-strip {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 5px;
      margin-bottom: 9px;
      padding-bottom: 7px;
      border-bottom: 1px solid color-mix(in srgb, var(--color-text, #000) 6%, transparent);
    }
    .ku-strip-pill {
      font-size: 10px;
      font-variant-numeric: tabular-nums;
      padding: 2px 6px;
      border-radius: 4px;
      background: color-mix(in srgb, var(--color-text, #000) 4%, transparent);
      color: var(--color-text-secondary, #64748b);
    }
    .ku-strip-pill b { color: var(--color-text, #0f172a); font-weight: 700; }
    .ku-strip-cache { background: color-mix(in srgb, var(--color-success, #3fb950) 12%, transparent); color: var(--color-success, #059669); }
    .ku-strip-cache b { color: var(--color-success, #059669); }
    .ku-minimized-bar {
      display: none;
      flex-direction: column;
      align-items: stretch;
      gap: 3px;
      cursor: pointer;
      white-space: nowrap;
    }
    .ku-min-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      white-space: nowrap;
    }
    .ku-min-model {
      display: block;
      font-size: 10.5px;
      font-weight: 600;
      color: var(--color-text-secondary, #64748b);
      font-variant-numeric: tabular-nums;
      overflow: hidden;
      text-overflow: ellipsis;
      padding-left: 12px;
      min-height: 14px;
      line-height: 14px;
    }
    .ku-min-model .ku-min-ph { opacity: 0.5; font-weight: 500; }
    .ku-min-model b {
      color: var(--color-text, #0f172a);
      font-weight: 700;
    }
    .ku-min-model .c { color: var(--color-success, #059669); font-weight: 700; }
    .ku-min-model .m { color: var(--color-accent, #1a88ff); font-weight: 700; }
    .ku-min-left {
      display: flex;
      align-items: center;
      gap: 5px;
      font-weight: 700;
      font-size: 11.5px;
      color: var(--color-text, #0f172a);
      flex: 0 1 auto;
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .ku-min-center {
      display: flex;
      align-items: center;
      gap: 4px;
      flex-shrink: 0;
      white-space: nowrap;
    }
    .ku-min-pill {
      font-size: 10px;
      font-weight: 700;
      padding: 1px 6px;
      border-radius: 6px;
      background: color-mix(in srgb, var(--color-accent, #1a88ff) 12%, transparent);
      color: var(--color-accent, #1a88ff);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }
    .ku-min-cache { background: color-mix(in srgb, var(--color-success, #3fb950) 14%, transparent); color: var(--color-success, #059669); }
    .ku-min-actions {
      display: flex;
      align-items: center;
      gap: 4px;
      flex-shrink: 0;
      white-space: nowrap;
    }
    .ku-min-action-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 20px;
      padding: 0 5px;
      border-radius: 4px;
      border: 1px solid color-mix(in srgb, var(--color-text, #000) 12%, transparent);
      background: color-mix(in srgb, var(--color-text, #000) 4%, transparent);
      color: var(--color-text, #334155);
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.15s;
      white-space: nowrap;
    }
    .ku-min-action-btn:hover { background: var(--color-accent, #1a88ff); color: #fff; border-color: var(--color-accent, #1a88ff); }
    .ku-min-btn-plus { width: 20px; padding: 0; font-size: 13px; line-height: 1; }

    .ku-card.is-collapsed,
    #${CARD_ID}.is-collapsed {
      padding: 8px 11px;
      background: color-mix(in srgb, var(--color-text, #000) 2%, var(--color-sidebar-bg, #fff));
    }
    .ku-card.is-collapsed .ku-expanded-header,
    .ku-card.is-collapsed .ku-summary-strip,
    .ku-card.is-collapsed .ku-expanded-body,
    #${CARD_ID}.is-collapsed .ku-expanded-header,
    #${CARD_ID}.is-collapsed .ku-summary-strip,
    #${CARD_ID}.is-collapsed .ku-expanded-body {
      display: none !important;
    }
    .ku-card.is-collapsed .ku-minimized-bar,
    #${CARD_ID}.is-collapsed .ku-minimized-bar { display: flex !important; }

    .ku-expanded-body { display: block; }
    .ku-row {
      display: flex;
      align-items: baseline;
      margin-bottom: 4px;
      font-size: 11.5px;
    }
    .ku-row-label {
      width: 32px;
      font-weight: 600;
      color: var(--color-text, #334155);
      flex-shrink: 0;
    }
    .ku-row-val {
      font-weight: 700;
      color: var(--color-text, #0f172a);
      font-variant-numeric: tabular-nums;
      min-width: 60px;
      flex: 1;
    }
    .ku-row-cost {
      font-weight: 600;
      color: var(--color-text, #334155);
      font-variant-numeric: tabular-nums;
      margin-left: auto;
      text-align: right;
    }
    .ku-row-cache { align-items: center; }
    .ku-cache-label { color: var(--color-success, #3fb950) !important; }
    .ku-cache-bar-wrap {
      width: 72px;
      max-width: 40%;
      height: 7px;
      background: color-mix(in srgb, var(--color-text, #000) 8%, transparent);
      border-radius: 3px;
      overflow: hidden;
      margin-right: 12px;
      flex-shrink: 0;
    }
    .ku-cache-bar-fill {
      height: 100%;
      background: var(--color-success, #3fb950);
      border-radius: 3px;
      transition: width 0.3s ease;
    }
    .ku-cache-pct { color: var(--color-success, #3fb950); font-weight: 700; font-variant-numeric: tabular-nums; }
    .ku-row-model {
      font-weight: 600;
      color: var(--color-text, #0f172a);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    /* 当前模型行：名称（可收缩省略）+ 右侧次要信息 */
    #ku-cur-row { min-width: 0; }
    #ku-cur-row .ku-row-model { flex: 0 1 auto; min-width: 0; }
    .ku-cur-meta {
      margin-left: auto;
      padding-left: 8px;
      font-size: 10px;
      color: var(--color-text-secondary, #64748b);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
      flex: 0 1 auto;
    }
    .ku-cur-meta:empty { display: none; }
    /* 详情弹层只在名称上 hover 触发 */
    #ku-cur-model { cursor: default; }
    #ku-cur-model:hover { color: var(--color-accent, #1a88ff); }
    .ku-model-item:focus-visible { outline: 1px solid var(--color-accent, #1a88ff); outline-offset: 1px; border-radius: 3px; }
    .ku-m-name { cursor: default; }
    .ku-m-name:hover { color: var(--color-accent, #1a88ff); }
    #ku-sess-key { cursor: default; flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
    #ku-sess-key:hover { color: var(--color-accent, #1a88ff); }
    /* 会话行三段式：key 可收缩 / tokens 弹性 / 缓存+成本右对齐 */
    #ku-sess-key { margin-left: 0; text-align: left; color: var(--color-text-secondary, #64748b); font-weight: 500; }
    #ku-sess-tokens { flex: 1; min-width: 0; margin-left: 8px; }
    #ku-sess-cache { flex-shrink: 0; margin-left: 6px; }
    #ku-sess-cost { flex-shrink: 0; margin-left: 6px; }
    .ku-meta-calls {
      font-size: 11px;
      color: var(--color-text-secondary, #64748b);
      margin: 6px 0 7px 0;
      font-variant-numeric: tabular-nums;
    }
    .ku-sparkline-row {
      display: flex;
      align-items: center;
      font-size: 11px;
      color: var(--color-text-secondary, #64748b);
      margin-bottom: 9px;
      gap: 6px;
    }
    .ku-sparkline-label {
      width: 38px;
      font-weight: 500;
      color: var(--color-text, #334155);
      flex-shrink: 0;
    }
    .ku-sparkline-date { font-size: 10px; font-variant-numeric: tabular-nums; }
    .ku-sparkline-bars {
      display: flex;
      align-items: flex-end;
      gap: 2.5px;
      height: 14px;
      padding: 0 2px;
      flex: 1;
      min-width: 0;
    }
    .ku-sparkline-bar {
      flex: 1;
      min-width: 3px;
      background: color-mix(in srgb, var(--color-text, #000) 30%, #334155);
      border-radius: 1px;
      min-height: 2px;
    }
    .ku-sparkline-bar.active { background: var(--color-accent, #1a88ff); }
    .ku-divider {
      height: 1px;
      background: color-mix(in srgb, var(--color-text, #000) 8%, transparent);
      margin: 10px 0 8px 0;
    }
    .ku-models-tabs { display: flex; gap: 10px; margin-bottom: 7px; }
    .ku-tab-btn {
      background: transparent;
      border: none;
      padding: 1px 4px;
      font-size: 11.5px;
      cursor: pointer;
      color: var(--color-text-secondary, #64748b);
      font-family: inherit;
      transition: color 0.15s;
    }
    .ku-tab-btn.active { color: var(--color-accent, #1a88ff); font-weight: 700; }
    .ku-models-list {
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding: 2px 0 4px 0;
    }
    .ku-model-item {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(52px, auto) minmax(36px, auto) minmax(48px, auto);
      column-gap: 8px;
      align-items: center;
      font-size: 11px;
      font-variant-numeric: tabular-nums;
      line-height: 1.4;
      padding: 2px 4px;
      margin: 0 -4px;
      border-radius: 5px;
    }
    .ku-model-item:hover { background: color-mix(in srgb, var(--color-text, #000) 5%, transparent); }
    .ku-m-name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--color-text, #1e293b);
      font-weight: 500;
    }
    .ku-m-tokens { text-align: right; color: var(--color-text, #0f172a); font-weight: 600; }
    .ku-m-cache { text-align: right; color: var(--color-text-secondary, #64748b); }
    .ku-m-cost { text-align: right; color: var(--color-text, #334155); font-weight: 600; }
    .ku-footer {
      font-size: 10px;
      color: var(--color-text-secondary, #94a3b8);
      margin-top: 10px;
      padding-top: 7px;
      border-top: 1px solid color-mix(in srgb, var(--color-text, #000) 5%, transparent);
      font-variant-numeric: tabular-nums;
    }
    /* 侧栏详情小弹层（模型 / 会话 完整信息） */
    .ku-pop {
      position: fixed;
      z-index: 99998;
      width: 252px;
      max-height: 280px;
      overflow-y: auto;
      padding: 9px 11px 10px;
      border-radius: 10px;
      border: 1px solid color-mix(in srgb, var(--color-text, #000) 14%, transparent);
      background: var(--color-bg, var(--color-sidebar-bg, #ffffff));
      color: var(--color-text, #1e293b);
      box-shadow: 0 10px 32px rgba(0,0,0,.28);
      font-size: 11px;
      line-height: 1.55;
      animation: ku-pop-in .15s ease;
      user-select: text;
    }
    @keyframes ku-pop-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
    .ku-pop-head {
      font-weight: 700; font-size: 11.5px; margin-bottom: 5px;
      padding-bottom: 5px;
      border-bottom: 1px solid color-mix(in srgb, var(--color-text, #000) 8%, transparent);
    }
    .ku-pop-row { display: flex; gap: 8px; margin: 3px 0; align-items: baseline; }
    .ku-pop-k { flex-shrink: 0; width: 58px; color: var(--color-text-secondary, #94a3b8); font-size: 10.5px; }
    .ku-pop-v { flex: 1; min-width: 0; overflow-wrap: anywhere; word-break: break-all; font-weight: 500; }
`;
  style.textContent += `
    /* ========== 2. 全屏报表弹层 (kup-) ========== */
    #${PANEL_MODAL_ID} {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.72);
      backdrop-filter: blur(6px);
      z-index: 99999;
      align-items: center;
      justify-content: center;
      padding: 20px;
      box-sizing: border-box;
      opacity: 0;
      transition: opacity 0.19s ease;
    }
    #${PANEL_MODAL_ID}.active { display: flex; }
    #${PANEL_MODAL_ID}.ku-anim-in { opacity: 1; }
    #${PANEL_MODAL_ID}.ku-anim-in .kup-card {
      transform: none;
      transition: transform 0.2s cubic-bezier(0.25, 0.6, 0.3, 1) 0.02s, opacity 0.17s ease 0.02s;
      opacity: 1;
    }
    #${PANEL_MODAL_ID} .kup-card {
      transform: translateY(14px) scale(0.985);
      opacity: 0;
      transition: transform 0.16s ease-in, opacity 0.14s ease-in;
    }
    .kup-card {
      width: 96vw;
      max-width: 1400px;
      height: 94vh;
      border-radius: 16px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      background: var(--color-bg, #121212);
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 10%, transparent);
      box-shadow: 0 24px 70px rgba(0, 0, 0, 0.5);
    }
    .kup-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 20px;
      border-bottom: 1px solid color-mix(in srgb, var(--color-text, #fff) 8%, transparent);
      background: var(--color-surface, #1f1f1f);
      color: var(--color-text, #f1f5f9);
      flex: none;
    }
    .kup-head-title {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 14.5px;
      font-weight: 700;
    }
    .kup-head-dot {
      display: inline-block;
      width: 8px; height: 8px;
      border-radius: 50%;
      background: var(--color-success, #3fb950);
      flex-shrink: 0;
    }
    .kup-head-dot.off { background: var(--color-text-faint, #64748b); }
    .kup-head-tools { display: flex; align-items: center; gap: 10px; }
    .kup-btn {
      padding: 5px 13px;
      border-radius: 7px;
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 16%, transparent);
      background: var(--color-surface-raised, #292929);
      color: var(--color-text, #f1f5f9);
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      font-family: inherit;
      transition: all 0.15s;
    }
    .kup-btn:hover { border-color: var(--color-accent, #1a88ff); color: var(--color-accent, #1a88ff); }
    #kup-uninstall-btn { border-color: color-mix(in srgb, var(--color-danger, #f85149) 45%, transparent); color: var(--color-danger, #f85149); }
    #kup-uninstall-btn:hover { border-color: var(--color-danger, #f85149); color: var(--color-danger, #f85149); }
    .kup-btn:disabled { opacity: .5; cursor: wait; }
    .kup-close {
      border: none;
      background: transparent;
      color: var(--color-text-faint, #94a3b8);
      font-size: 24px;
      line-height: 1;
      cursor: pointer;
      padding: 2px 6px;
      border-radius: 5px;
    }
    .kup-close:hover { color: var(--color-danger, #f85149); }
    .kup-shell {
      flex: 1;
      overflow-y: auto;
      padding: 20px 24px 40px;
      background: var(--kup-bg);
      color: var(--kup-text);
      font-size: 13px;
      line-height: 1.5;
    }
    .kup-shell[data-theme="dark"] {
      --kup-bg:#121212; --kup-card:#1f1f1f; --kup-card2:#292929; --kup-border:rgba(255,255,255,.08); --kup-border2:rgba(255,255,255,.16);
      --kup-text:rgba(255,255,255,.9); --kup-muted:rgba(255,255,255,.62); --kup-faint:rgba(255,255,255,.42);
      --kup-accent:#1a88ff; --kup-accent2:#3d7bff; --kup-cyan:#22b8cf;
      --kup-green:#3fb950; --kup-amber:#d29922; --kup-red:#f85149;
      --kup-track:rgba(255,255,255,.07); --kup-shadow:0 8px 30px rgba(0,0,0,.45);
    }
    .kup-shell[data-theme="light"] {
      --kup-bg:#f5f5f5; --kup-card:#ffffff; --kup-card2:#f5f5f5; --kup-border:rgba(0,0,0,.1); --kup-border2:rgba(0,0,0,.2);
      --kup-text:rgba(0,0,0,.88); --kup-muted:rgba(0,0,0,.62); --kup-faint:rgba(0,0,0,.45);
      --kup-accent:#1783ff; --kup-accent2:#4d7fe8; --kup-cyan:#0e8599;
      --kup-green:#0e7a38; --kup-amber:#b58708; --kup-red:#c0392b;
      --kup-track:rgba(0,0,0,.06); --kup-shadow:0 4px 20px rgba(0,0,0,.08);
    }
    .kup-shell * { box-sizing: border-box; }
    .kup-app { max-width: 1360px; margin: 0 auto; }
    .kup-topbar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:14px; margin-bottom:20px; }
    .kup-brand { display:flex; align-items:center; gap:12px; }
    .kup-brand-mark {
      width:36px; height:36px; border-radius:10px;
      background:linear-gradient(135deg,var(--kup-accent2),var(--kup-cyan));
      display:flex; align-items:center; justify-content:center;
      color:#fff; font-weight:800; font-size:16px;
      box-shadow:0 4px 16px color-mix(in srgb,var(--kup-accent2) 35%,transparent);
    }
    .kup-brand h1 { font-size:18px; margin:0; font-weight:700; letter-spacing:.3px; }
    .kup-brand p { margin:2px 0 0; font-size:12px; color:var(--kup-muted); }
    .kup-topbar-right { display:flex; align-items:center; gap:12px; }
    .kup-shell #kup-stamp { color:var(--kup-muted); font-size:12.5px; font-variant-numeric:tabular-nums; }
    .kup-shell .kup-btn {
      border:1px solid var(--kup-border2); background:var(--kup-card); color:var(--kup-text);
      padding:7px 15px; border-radius:9px; font-size:12.5px;
    }
    .kup-shell .kup-btn:hover { border-color:var(--kup-accent); color:var(--kup-accent); background:var(--kup-card); }
    .kup-btn-primary {
      background:linear-gradient(135deg,var(--kup-accent),var(--kup-accent2)) !important;
      color:#fff !important; border-color:transparent !important;
      box-shadow:0 4px 16px color-mix(in srgb,var(--kup-accent) 35%,transparent);
    }
    .kup-btn-primary:hover { opacity:.92; transform:translateY(-1px); }
    .kup-shell #kup-err {
      display:none; background:color-mix(in srgb,var(--kup-red) 10%,transparent); border:1px solid color-mix(in srgb,var(--kup-red) 30%,transparent);
      color:var(--kup-red); padding:11px 16px; border-radius:10px; margin-bottom:16px; font-size:13px;
    }
    .kup-kpis-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:16px; }
    @media(max-width:1100px){ .kup-kpis-grid{grid-template-columns:repeat(2,1fr)} }
    @media(max-width:580px){ .kup-kpis-grid{grid-template-columns:1fr} }
    .kup-kpi-card {
      background:linear-gradient(180deg,var(--kup-card2),var(--kup-card));
      border:1px solid var(--kup-border); border-radius:14px;
      padding:16px 18px; position:relative; overflow:hidden;
      box-shadow:var(--kup-shadow); transition:border-color .15s,transform .15s;
    }
    .kup-kpi-card:hover { border-color:var(--kup-border2); transform:translateY(-1px); }
    .kup-kpi-card::after {
      content:""; position:absolute; left:0; top:0; bottom:0; width:3.5px;
      background:linear-gradient(180deg,var(--kup-accent),var(--kup-accent2));
    }
    .kup-kpi-card.c-yesterday::after { background:linear-gradient(180deg,var(--kup-cyan),var(--kup-accent)); }
    .kup-kpi-card.c-week::after { background:linear-gradient(180deg,var(--kup-accent2),var(--kup-cyan)); }
    .kup-kpi-card.c-month::after { background:linear-gradient(180deg,var(--kup-green),var(--kup-cyan)); }
    .kup-kpi-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
    .kup-kpi-title { font-size:12.5px; font-weight:600; color:var(--kup-muted); }
    .kup-kpi-date {
      font-size:11px; font-weight:500; padding:2px 9px; border-radius:12px;
      background:color-mix(in srgb,var(--kup-accent) 10%,transparent); color:var(--kup-accent);
      border:1px solid color-mix(in srgb,var(--kup-accent) 20%,transparent); font-variant-numeric:tabular-nums;
    }
    .kup-kpi-tokens {
      font-size:27px; font-weight:800; letter-spacing:.3px;
      font-variant-numeric:tabular-nums; color:var(--kup-text); margin-bottom:12px;
    }
    .kup-kpi-tokens small { font-size:14px; font-weight:600; color:var(--kup-muted); margin-left:4px; }
    .kup-kpi-meta-grid {
      display:grid; grid-template-columns:repeat(3,1fr); gap:8px;
      font-size:11.5px; padding-top:10px; border-top:1px solid var(--kup-border);
    }
    .kup-kpi-meta-item span { display:block; color:var(--kup-faint); font-size:10.5px; margin-bottom:2px; }
    .kup-kpi-meta-item b { color:var(--kup-text); font-variant-numeric:tabular-nums; font-size:13px; }
    .kup-kpi-sub-text {
      margin-top:8px; font-size:11px; color:var(--kup-muted);
      display:flex; justify-content:space-between; align-items:center;
    }
    .kup-sub-metrics {
      display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin-bottom:16px;
    }
    .kup-sub-pill {
      background:var(--kup-card); border:1px solid var(--kup-border); border-radius:11px;
      padding:10px 14px; display:flex; align-items:center; justify-content:space-between;
      box-shadow:var(--kup-shadow);
    }
    .kup-sub-pill-left span { display:block; font-size:11px; color:var(--kup-muted); }
    .kup-sub-pill-left b { font-size:15px; font-weight:700; color:var(--kup-text); font-variant-numeric:tabular-nums; }
    .kup-sub-pill-right { text-align:right; font-size:11.5px; color:var(--kup-faint); }
    .kup-pcard {
      background:var(--kup-card); border:1px solid var(--kup-border); border-radius:14px;
      margin-bottom:16px; box-shadow:var(--kup-shadow); overflow:hidden;
    }
    .kup-card-head {
      display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;
      padding:14px 18px; border-bottom:1px solid var(--kup-border);
    }
    .kup-card-head h2 { font-size:14.5px; margin:0; font-weight:700; }
    .kup-card-sub { font-size:12px; color:var(--kup-muted); margin-top:2px; }
    .kup-tabs { display:flex; gap:4px; background:var(--kup-track); padding:3px; border-radius:8px; }
    .kup-tab {
      padding:5px 14px; border-radius:6px; font-size:12px; font-weight:600;
      color:var(--kup-muted); background:transparent; border:none; cursor:pointer;
      font-family:inherit; transition:all .15s;
    }
    .kup-tab.active { background:var(--kup-card); color:var(--kup-accent); box-shadow:var(--kup-shadow); }
    .kup-card-body { padding:16px 18px; }
    .kup-shell #kup-wave-wrap { position:relative; width:100%; height:220px; }
    .kup-shell #kup-wave-chart { width:100%; height:100%; overflow:visible; }
    .kup-shell #kup-wave-tip {
      position:absolute; pointer-events:none; background:var(--kup-card2); border:1px solid var(--kup-border2);
      border-radius:10px; padding:10px 14px; color:var(--kup-text);
      box-shadow:0 12px 32px rgba(0,0,0,.45); display:none; z-index:30;
      white-space:nowrap; line-height:1.5; min-width:140px;
    }
    .kup-wt-title { font-size:13px; font-weight:700; color:var(--kup-accent); margin-bottom:6px; text-align:left; }
    .kup-wt-row { display:flex; align-items:center; justify-content:space-between; gap:12px; font-size:12px; margin-bottom:2px; }
    .kup-wt-label { color:var(--kup-text); font-weight:500; }
    .kup-wt-val { color:var(--kup-accent); font-weight:700; font-variant-numeric:tabular-nums; }
    .kup-chart-legend {
      display:flex; align-items:center; gap:18px; font-size:11.5px; color:var(--kup-muted); margin-bottom:8px;
    }
    .kup-chart-legend span { display:inline-flex; align-items:center; gap:5px; }
    .kup-dot-token { width:8px; height:8px; border-radius:50%; background:var(--kup-accent2); }
    .kup-dot-call { width:8px; height:8px; border-radius:2px; background:var(--kup-cyan); opacity:.7; }
    .kup-shell table { width:100%; border-collapse:collapse; font-size:12.5px; }
    .kup-shell th, .kup-shell td {
      padding:10px 14px; text-align:right; border-bottom:1px solid var(--kup-border);
      font-variant-numeric:tabular-nums; white-space:nowrap;
    }
    .kup-shell th:first-child, .kup-shell td:first-child { text-align:left; }
    .kup-shell th { font-size:11.5px; font-weight:600; color:var(--kup-muted); background:var(--kup-track); }
    .kup-shell tbody tr:last-child td { border-bottom:none; }
    .kup-shell tbody tr:hover { background:color-mix(in srgb,var(--kup-accent) 6%,transparent); }
    .kup-mono { font-variant-numeric:tabular-nums; }
    .kup-tag {
      display:inline-block; padding:2px 7px; border-radius:5px; font-size:11px; font-weight:600;
      background:color-mix(in srgb,var(--kup-green) 14%,transparent); color:var(--kup-green);
    }
    .kup-tag.warn { background:color-mix(in srgb,var(--kup-amber) 16%,transparent); color:var(--kup-amber); }
    .kup-tag.low { background:color-mix(in srgb,var(--kup-red) 14%,transparent); color:var(--kup-red); }
    .kup-model-cell { display:flex; align-items:center; gap:10px; }
    .kup-model-cell .bar {
      flex:1; height:6px; background:var(--kup-track); border-radius:3px; overflow:hidden;
      min-width:80px; display:block;
    }
    .kup-model-cell .bar i {
      display:block; height:100%;
      background:linear-gradient(90deg,var(--kup-accent2),var(--kup-cyan)); border-radius:3px;
    }
    .kup-model-cell .pct {
      font-size:11px; color:var(--kup-muted); width:38px; text-align:right; font-variant-numeric:tabular-nums;
    }
    .kup-model-name {
      font-weight:600; color:var(--kup-text); max-width:200px; overflow:hidden; text-overflow:ellipsis;
    }
    .kup-empty { padding:28px; text-align:center; color:var(--kup-muted); font-size:12.5px; }
    .kup-grid2 { display:grid; grid-template-columns:1.2fr 1fr; gap:16px; }
    @media(max-width:960px){ .kup-grid2{grid-template-columns:1fr} }
    .kup-scroll { max-height:360px; overflow:auto; }
    .kup-scroll table { table-layout: fixed; width: 100%; }
    .kup-scroll td { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    /* 面板打开时的骨架占位（异步渲染前立即给出反馈） */
    #${PANEL_MODAL_ID} .kup-shell { position: relative; }
    .kup-skel-overlay {
      position: absolute; inset: 0; z-index: 5;
      background: var(--kup-bg, transparent);
      padding: 20px 24px;
    }
    .kup-skel { padding: 14px 18px; }
    .kup-skel-row {
      height: 13px; border-radius: 6px; margin: 8px 0;
      background: linear-gradient(90deg, var(--kup-track) 25%, color-mix(in srgb, var(--kup-text) 14%, transparent) 50%, var(--kup-track) 75%);
      background-size: 200% 100%;
      animation: kup-skel-shimmer 1.15s linear infinite;
    }
    @keyframes kup-skel-shimmer { from { background-position: 200% 0; } to { background-position: -200% 0; } }
`;
  style.textContent += `
    /* ========== 3. 模型管理弹窗 (kmm-) ========== */
    #${KMM_OVERLAY_ID} {
      position: fixed;
      top: 0; left: 0; width: 100vw; height: 100vh;
      background: rgba(0, 0, 0, 0.65);
      backdrop-filter: blur(4px);
      z-index: 99999;
      display: none;
      align-items: center;
      justify-content: center;
      opacity: 0;
      transition: opacity 0.18s ease;
    }
    #${KMM_OVERLAY_ID}.visible { display: flex; }
    #${KMM_OVERLAY_ID}.ku-anim-in { opacity: 1; }
    #${KMM_OVERLAY_ID}.ku-anim-in #kmm-modal {
      transform: none;
      opacity: 1;
      transition: transform 0.19s cubic-bezier(0.25, 0.6, 0.3, 1) 0.02s, opacity 0.16s ease 0.02s;
    }
    #kmm-modal {
      width: 640px;
      max-width: 90vw;
      max-height: 86vh;
      transform: translateY(12px) scale(0.975);
      opacity: 0;
      transition: transform 0.15s ease-in, opacity 0.13s ease-in;
      background: var(--color-surface, #1f1f1f);
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 10%, transparent);
      border-radius: 16px;
      display: flex;
      flex-direction: column;
      box-shadow: 0 16px 40px rgba(0,0,0,0.4);
      color: var(--color-text, #e2e8f0);
      font-family: system-ui, -apple-system, sans-serif;
      overflow: hidden;
    }
    .kmm-modal-header {
      padding: 18px 22px 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .kmm-modal-title { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
    .kmm-modal-title h2 { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: .2px; }
    .kmm-def-badge { font-size: 11.5px; color: var(--color-text-secondary, #94a3b8); }
    .kmm-def-badge b { color: var(--color-accent, #1a88ff); font-weight: 600; }
    .kmm-x {
      width: 28px; height: 28px; border-radius: 8px; border: none; background: transparent;
      color: var(--color-text-faint, #94a3b8); font-size: 15px; cursor: pointer; line-height: 1;
    }
    .kmm-x:hover { background: color-mix(in srgb, var(--color-text, #fff) 8%, transparent); color: var(--color-text, #fff); }
    .kmm-toolbar { padding: 0 22px 14px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    #kmm-modal [hidden], #ksk-box [hidden] { display: none !important; }
    #kmm-modal .kmm-tabs { padding: 0 22px 12px; display: flex; gap: 8px; }
    #kmm-modal .kmm-tabs .on { border-color: var(--color-accent, #1a88ff); color: var(--color-accent, #1a88ff); }
    #kmm-manager-status { padding: 0 22px 12px; font-size: 12px; color: var(--color-warning, #d29922); overflow-wrap: anywhere; }
    #kmm-modal button:disabled, #ksk-box button:disabled { opacity: .45; cursor: not-allowed; }
    #kmm-modal .kmm-editor { min-height: 0; }
    #kmm-modal .kmm-editor h3 { margin: 0; font-size: 15px; }
    #kmm-modal .kmm-editor-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    #kmm-modal .kmm-field, #ksk-box .kmm-field { display: flex; flex-direction: column; gap: 5px; min-width: 0; font-size: 12px; }
    #kmm-modal .kmm-field input, #kmm-modal .kmm-field select,
    #ksk-box .kmm-field input, #ksk-box .kmm-field select, #ksk-box .kmm-field textarea {
      width: 100%; min-width: 0; box-sizing: border-box; padding: 7px 9px; border-radius: 6px;
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 12%, transparent);
      background: var(--color-surface-sunken, #121212); color: var(--color-text, #e2e8f0); font: inherit;
      transition: border-color .15s, box-shadow .15s;
    }
    #kmm-modal .kmm-field input:focus, #kmm-modal .kmm-field select:focus,
    #ksk-box .kmm-field input:focus, #ksk-box .kmm-field select:focus,
    #ksk-box .kmm-field textarea:focus { outline: none; border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 70%, transparent); box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-accent, #1a88ff) 12%, transparent); }
    #kmm-modal .kmm-field input:disabled, #kmm-modal .kmm-field select:disabled,
    #ksk-box .kmm-field input:disabled, #ksk-box .kmm-field select:disabled { opacity: .6; }
    #kmm-modal .kmm-editor-checks, #ksk-box .kmm-editor-checks { display: flex; flex-wrap: wrap; gap: 8px 14px; font-size: 12px; }
    #kmm-modal .kmm-editor-checks label, #ksk-box .kmm-editor-checks label { display: inline-flex; gap: 5px; align-items: center; overflow-wrap: anywhere; }
    #kmm-modal .kmm-editor-actions { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
    #kmm-editor-note, #kmm-editor-error, #kmm-editor-status { font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
    #kmm-editor-note { color: var(--color-text-secondary, #94a3b8); }
    #kmm-editor-error, #kmm-editor-status { color: var(--color-warning, #d29922); }
    #kmm-modal .kmm-provider-name { overflow-wrap: anywhere; }
    #ksk-box .kmm-note { font-size: 11.5px; color: var(--color-text-faint, #94a3b8); }
    #kmm-batch { display: flex; flex-direction: column; min-height: 0; flex: 1; border-top: 1px solid color-mix(in srgb, var(--color-text, #fff) 7%, transparent); }
    .kmm-batch-head { display: flex; flex-wrap: wrap; align-items: end; gap: 6px; padding: 11px 16px 8px; }
    .kmm-batch-head .kmm-field { flex: 1; min-width: 140px; }
    .kmm-batch-head .kmm-search { flex-basis: 100%; }
    .kmm-batch-filters { display: flex; flex-wrap: wrap; gap: 4px; padding: 0 16px 9px; }
    .kmm-batch-filters .kmm-tbtn { padding: 4px 8px; font-size: 11px; font-weight: 500; border-color: transparent; }
    .kmm-batch-filters .kmm-tbtn.on { color: var(--color-accent, #1a88ff); border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 35%, transparent); background: color-mix(in srgb, var(--color-accent, #1a88ff) 10%, transparent); }
    .kmm-batch-note { padding: 0 16px 8px; color: var(--color-text-secondary, #94a3b8); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
    #kmm-batch-status { color: var(--color-warning, #d29922); }
    .kmm-batch-list { overflow-y: auto; min-height: 100px; max-height: 44vh; padding: 0 16px 10px; display: flex; flex-direction: column; gap: 5px; }
    .kmm-batch-row { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid color-mix(in srgb, var(--color-text, #fff) 9%, transparent); border-radius: 8px; background: var(--color-surface-raised, #292929); }
    .kmm-batch-row.pending { border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 45%, transparent); }
    .kmm-batch-row.removing { border-color: color-mix(in srgb, var(--color-warning, #d29922) 40%, transparent); }
    .kmm-batch-info { flex: 1; min-width: 0; }
    .kmm-batch-title { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; font-size: 12.5px; font-weight: 600; overflow-wrap: anywhere; }
    .kmm-batch-meta, .kmm-batch-reason { margin-top: 4px; font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; color: var(--color-text-secondary, #94a3b8); }
    .kmm-batch-reason { color: var(--color-warning, #d29922); }
    .kmm-batch-actions { display: flex; flex-shrink: 0; gap: 6px; }
    .kmm-row-btn { width: 30px; height: 30px; display: inline-flex; align-items: center; justify-content: center; padding: 0; border-radius: 8px; border: 1px solid color-mix(in srgb, var(--color-text, #fff) 14%, transparent); background: transparent; color: var(--color-text, #e2e8f0); font: inherit; font-size: 19px; cursor: pointer; }
    .kmm-row-btn:hover { color: var(--color-accent, #1a88ff); border-color: var(--color-accent, #1a88ff); }
    .kmm-row-btn.selected { background: color-mix(in srgb, var(--color-accent, #1a88ff) 10%, transparent); }
    .kmm-row-btn svg { width: 14px; height: 14px; }
    .kmm-batch-footer { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; padding: 12px 22px; border-top: 1px solid color-mix(in srgb, var(--color-text, #fff) 7%, transparent); }
    #kmm-batch-count { flex: 1; font-size: 12px; min-width: 180px; color: var(--color-text-secondary, #94a3b8); }
    .kmm-context-presets { display: flex; gap: 6px; flex-wrap: wrap; }
    .kmm-context-presets .kmm-tbtn { padding: 4px 8px; font-size: 11px; }
    #kmm-modal {
      --kmm-bg: var(--color-surface, #1f1f1f);
      --kmm-text: var(--color-text, #e2e8f0);
      --kmm-muted: var(--color-text-secondary, #9ca3af);
      --kmm-accent: var(--color-accent, #1a88ff);
      --kmm-border: color-mix(in srgb, var(--kmm-text) 9%, transparent);
      --kmm-soft: color-mix(in srgb, var(--kmm-text) 3%, var(--kmm-bg));
      --kmm-input: color-mix(in srgb, var(--kmm-text) 2%, var(--kmm-bg));
      width: 1040px; max-width: calc(100vw - 48px); height: min(780px, 88vh); max-height: calc(100dvh - 48px);
      position: relative; isolation: isolate; border-radius: 18px; font-size: 13px; line-height: 1.55;
      border-color: var(--kmm-border); box-shadow: 0 24px 80px #0005;
    }
    #kmm-modal *, #kmm-modal *::before, #kmm-modal *::after { box-sizing: border-box; }
    #kmm-modal :is(button, input, select, textarea) { font-family: inherit; }
    #kmm-modal :is(button, summary, input, select, textarea):focus-visible { outline: 2px solid var(--kmm-accent); outline-offset: 3px; }
    #kmm-modal :is(input, select, textarea) { accent-color: var(--kmm-accent); }
    #kmm-modal :is(input[type="checkbox"], input[type="radio"]) { width: 15px; height: 15px; margin: 0; flex: 0 0 auto; }
    #kmm-modal .kmm-modal-header { min-height: 72px; padding: 18px 24px; border-bottom: 1px solid var(--kmm-border); }
    #kmm-modal .kmm-modal-title { min-width: 0; gap: 18px; align-items: center; flex-wrap: nowrap; }
    #kmm-modal .kmm-modal-title h2 { font-size: 18px; font-weight: 650; letter-spacing: 0; flex-shrink: 0; }
    #kmm-modal .kmm-def-badge { max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--kmm-muted); font-size: 12px; }
    #kmm-modal .kmm-x { width: 32px; height: 32px; flex-shrink: 0; }
    #kmm-modal .kmm-workspace { display: flex; flex: 1; min-height: 0; }
    #kmm-modal .kmm-tabs { width: 164px; flex: 0 0 164px; padding: 18px 12px; flex-direction: column; border-right: 1px solid var(--kmm-border); background: var(--kmm-soft); gap: 5px; }
    #kmm-modal .kmm-nav { display: flex; align-items: center; gap: 10px; min-height: 42px; padding: 10px 12px; border: 0; border-radius: 9px; background: transparent; text-align: left; color: var(--kmm-muted); cursor: pointer; font: inherit; }
    #kmm-modal .kmm-nav:hover { background: color-mix(in srgb, var(--kmm-text) 5%, transparent); color: var(--kmm-text); }
    #kmm-modal .kmm-nav.on { background: color-mix(in srgb, var(--kmm-accent) 10%, transparent); color: var(--kmm-accent); font-weight: 600; }
    #kmm-modal .kmm-nav svg { width: 17px; height: 17px; flex-shrink: 0; }
    #kmm-modal .kmm-nav-count { margin-left: auto; font-size: 11px; font-variant-numeric: tabular-nums; opacity: .75; }
    #kmm-modal .kmm-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
    #kmm-modal .kmm-page-head { padding: 24px 28px 18px; }
    #kmm-modal .kmm-page-head h3 { margin: 0; font-size: 20px; font-weight: 650; }
    #kmm-modal .kmm-page-head p { margin: 5px 0 0; color: var(--kmm-muted); font-size: 12px; }
    #kmm-modal .kmm-toolbar { padding: 0 28px 18px; flex-wrap: nowrap; gap: 10px; }
    #kmm-modal .kmm-search { min-height: 38px; padding: 9px 12px; border-radius: 8px; background: var(--kmm-input); border-color: var(--kmm-border); font-size: 12px; }
    #kmm-modal .kmm-toolbar .kmm-search { max-width: 400px; margin-right: auto; }
    #kmm-modal .kmm-tbtn { min-height: 36px; padding: 8px 13px; border-radius: 8px; border-color: var(--kmm-border); font-size: 12px; font-weight: 550; line-height: 1.5; }
    #kmm-modal .kmm-tbtn.primary { border-color: var(--kmm-accent); background: var(--kmm-accent); color: #fff; }
    #kmm-modal button:disabled { opacity: .42; cursor: not-allowed; filter: none; }
    #kmm-modal .kmm-content { display: flex; flex: 1; min-height: 0; }
    #kmm-modal .kmm-lists { flex: 1; min-width: 0; display: flex; flex-direction: column; }
    #kmm-modal .kmm-lists > .kmm-modal-body { flex: 1; min-height: 0; padding: 0 28px 24px; border: 0; gap: 10px; }
    #kmm-modal.has-detail :is(.kmm-page-head, .kmm-toolbar, .kmm-lists) { display: none; }
    #kmm-modal :is(#kmm-detail, #kmm-detail.wide) { width: 100%; flex: 1; min-width: 0; display: flex; flex-direction: column; border: 0; background: transparent; }
    #kmm-modal .kmm-detail-head { padding: 20px 28px 18px; display: flex; align-items: center; flex-wrap: wrap; gap: 12px; border-bottom: 1px solid var(--kmm-border); }
    #kmm-modal #kmm-detail-back { padding: 5px 0; color: var(--kmm-muted); font-size: 12px; }
    #kmm-modal #kmm-detail-title { flex: 1; min-width: 0; margin: 0; font-size: 16px; font-weight: 600; }
    #kmm-modal #kmm-dirty { padding: 3px 8px; border-radius: 6px; background: color-mix(in srgb, var(--color-warning, #d29922) 9%, transparent); color: var(--color-warning, #d29922); font-size: 11px; }
    #kmm-modal #kmm-detail > .kmm-modal-body { flex: 1; min-height: 0; padding: 24px 28px 0; border: 0; gap: 18px; }
    #kmm-modal .kmm-editor > h3 { margin: 0; font-size: 15px; font-weight: 600; overflow-wrap: anywhere; }
    #kmm-modal .kmm-editor-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
    #kmm-modal .kmm-field { gap: 8px; font-size: 12px; color: var(--kmm-muted); line-height: 1.5; }
    #kmm-modal .kmm-field :is(input:not([type="checkbox"]):not([type="radio"]), select, textarea) { min-height: 39px; padding: 10px 12px; border-radius: 8px; border-color: var(--kmm-border); background: var(--kmm-input); color: var(--kmm-text); font-size: 13px; }
    #kmm-modal .kmm-field textarea { resize: vertical; min-height: 110px; line-height: 1.7; }
    #kmm-modal .kmm-field-wide { grid-column: 1 / -1; }
    #kmm-modal .kmm-section { display: flex; flex-direction: column; flex-shrink: 0; gap: 18px; padding: 20px; border: 1px solid var(--kmm-border); border-radius: 12px; }
    #kmm-modal .kmm-section h4 { margin: 0; color: var(--kmm-text); font-size: 13px; font-weight: 600; }
    #kmm-modal .kmm-advanced-content { display: flex; flex-direction: column; gap: 20px; }
    #kmm-modal .kmm-inline-action { align-self: flex-start; }
    #kmm-modal .kmm-batch-alternatives { display: flex; align-items: center; gap: 4px; padding-bottom: 3px; }
    #kmm-modal :is(.kmm-toggle, .kmm-acts > .kmm-editor-checks) { display: inline-flex; align-items: center; align-self: flex-start; flex-wrap: wrap; gap: 8px; min-height: 30px; color: var(--kmm-muted); font-size: 12px; cursor: pointer; }
    #kmm-modal .kmm-toggle small { color: var(--kmm-muted); font-size: 11px; }
    #kmm-modal :is(.kmm-toggle, .kmm-acts > .kmm-editor-checks) input[type="checkbox"] { appearance: none; position: relative; width: 30px; height: 18px; border: 1px solid color-mix(in srgb, var(--kmm-text) 16%, transparent); border-radius: 20px; background: color-mix(in srgb, var(--kmm-text) 15%, transparent); cursor: pointer; transition: background .15s, border-color .15s; }
    #kmm-modal :is(.kmm-toggle, .kmm-acts > .kmm-editor-checks) input[type="checkbox"]::before { content: ''; position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; border-radius: 50%; background: #fff; box-shadow: 0 1px 3px #0002; transition: transform .15s; }
    #kmm-modal :is(.kmm-toggle, .kmm-acts > .kmm-editor-checks) input[type="checkbox"]:checked { background: var(--kmm-accent); border-color: var(--kmm-accent); }
    #kmm-modal :is(.kmm-toggle, .kmm-acts > .kmm-editor-checks) input[type="checkbox"]:checked::before { transform: translateX(12px); }
    #kmm-modal :is(.kmm-toggle, .kmm-acts > .kmm-editor-checks) input:disabled { opacity: .45; cursor: not-allowed; }
    #kmm-modal .kmm-tag.ad { background: var(--kmm-soft); color: var(--kmm-muted); }
    #kmm-modal .kmm-audit { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 11px 14px; border: 1px solid var(--kmm-border); border-radius: 9px; color: var(--kmm-muted); font-size: 12px; }
    #kmm-modal .kmm-audit > span { flex: 1; }
    #kmm-modal .kmm-audit.is-warning { border-color: color-mix(in srgb, var(--color-warning, #d29922) 25%, transparent); background: color-mix(in srgb, var(--color-warning, #d29922) 5%, transparent); }
    #kmm-modal .kmm-warn { color: var(--color-warning, #d29922); }
    #kmm-modal .kmm-warn summary { cursor: pointer; }
    #kmm-modal .kmm-lists > .kmm-modal-body > .kmm-note:only-child { display: flex; flex: 1; align-items: center; justify-content: center; min-height: 180px; padding: 32px; text-align: center; }
    #kmm-modal :is(.kmm-error, #kpr-error) { font-size: 12px; line-height: 1.8; overflow-wrap: anywhere; color: var(--kmm-muted); }
    #kmm-modal .kmm-error:not(:empty) { padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--color-danger, #f85149) 20%, transparent); border-radius: 8px; color: var(--color-danger, #f85149); }
    #kmm-modal .kmm-error:empty { display: none; }
    #kmm-modal #ksk-overlay #ksk-box { display: flex; flex-direction: column; flex: 0 0 auto; gap: 20px; min-height: 100%; overflow: visible; }
    #kmm-modal .kmm-main { min-height: 0; }
    #kmm-modal .kmm-card { flex-shrink: 0; }
    #kmm-modal .kmm-card-description { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; white-space: normal; overflow-wrap: anywhere; }
    #kmm-modal .kmm-skill-description { margin: 0; color: var(--kmm-muted); font-size: 13px; line-height: 1.8; }
    #kmm-modal .kmm-file-location { flex-shrink: 0; font-size: 11px; overflow-wrap: anywhere; }
    #kmm-modal .kmm-file-location summary { cursor: pointer; margin-bottom: 8px; }
    #kmm-modal .kmm-file-location code { color: var(--kmm-muted); font-size: 11px; }
    @media (max-height: 700px) {
      #kmm-modal.has-detail #kmm-batch { display: block; overflow-y: auto; }
      #kmm-modal.has-detail #kmm-detail .kmm-batch-list { max-height: 260px; }
      #kmm-modal.has-detail #kmm-detail .kmm-batch-footer { position: sticky; bottom: 0; }
    }
    #kmm-modal .kmm-editor-checks { gap: 10px; align-items: center; }
    #kmm-modal .kmm-editor-checks label { gap: 8px; padding: 9px 12px; border: 1px solid var(--kmm-border); border-radius: 8px; font-size: 12px; }
    #kmm-modal .kmm-editor-checks label:has(input:checked) { border-color: color-mix(in srgb, var(--kmm-accent) 30%, transparent); background: color-mix(in srgb, var(--kmm-accent) 5%, transparent); }
    #kmm-modal .kmm-editor-actions { position: sticky; bottom: 0; padding: 16px 0; margin-top: auto; display: flex; gap: 10px; justify-content: flex-end; flex-wrap: wrap; border-top: 1px solid var(--kmm-border); background: var(--kmm-bg); z-index: 2; }
    #kmm-modal .kmm-editor-actions > .kmm-link:first-child { margin-right: auto; }
    #kmm-modal :is(#kmm-editor-note, #kmm-editor-status) { font-size: 12px; color: var(--kmm-muted); line-height: 1.7; }
    #kmm-modal :is(#kmm-editor-error, #kmm-editor-status, #kmm-provider-key-note):empty { display: none; }
    #kmm-modal #kmm-editor-error:not(:empty) { padding: 10px 12px; border-radius: 8px; background: color-mix(in srgb, var(--color-danger, #f85149) 7%, transparent); color: var(--color-danger, #f85149); }
    #kmm-modal .kmm-editor > details { padding: 14px 16px; border: 1px solid var(--kmm-border); border-radius: 10px; }
    #kmm-modal .kmm-editor > details > summary { cursor: pointer; color: var(--kmm-muted); font-size: 12px; }
    #kmm-modal .kmm-editor > details[open] > summary { margin-bottom: 16px; color: var(--kmm-text); }
    #kmm-modal .kmm-card { border: 1px solid var(--kmm-border); border-radius: 11px; padding: 15px 17px; background: transparent; box-shadow: none; }
    #kmm-modal .kmm-card:hover { border-color: color-mix(in srgb, var(--kmm-text) 17%, transparent); background: var(--kmm-soft); box-shadow: none; }
    #kmm-modal .kmm-card.is-default { border-color: color-mix(in srgb, var(--kmm-accent) 26%, transparent); background: color-mix(in srgb, var(--kmm-accent) 3%, var(--kmm-bg)); }
    #kmm-modal .kmm-top { align-items: flex-start; gap: 16px; }
    #kmm-modal .kmm-name { gap: 7px; min-width: 0; }
    #kmm-modal .kmm-name strong { font-size: 14px; font-weight: 600; overflow-wrap: anywhere; }
    #kmm-modal .kmm-acts { gap: 5px; align-items: center; }
    #kmm-modal .kmm-link { padding: 5px 8px; font-size: 12px; line-height: 1.5; }
    #kmm-modal .kmm-acts > .kmm-link.accent { color: var(--kmm-muted); }
    #kmm-modal .kmm-acts > .kmm-link.accent:hover { color: var(--kmm-accent); }
    #kmm-modal .kmm-meta { margin-top: 5px; font-size: 12px; color: var(--kmm-muted); line-height: 1.6; }
    #kmm-modal .kmm-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 5px 0; margin-top: 9px; color: var(--kmm-muted); font-size: 11px; line-height: 1.5; }
    #kmm-modal .kmm-summary span { padding: 0; background: none; border-radius: 0; }
    #kmm-modal .kmm-summary span + span::before { content: '·'; padding: 0 9px; opacity: .55; }
    #kmm-modal .kmm-tag { padding: 2px 7px; border-radius: 5px; font-size: 10px; font-weight: 500; line-height: 1.5; }
    #kmm-modal .kmm-tag.cur { background: color-mix(in srgb, var(--kmm-accent) 11%, transparent); color: var(--kmm-accent); }
    #kmm-modal .kmm-tag.def { background: color-mix(in srgb, var(--kmm-text) 6%, transparent); color: var(--kmm-muted); }
    #kmm-modal .kmm-note { color: var(--kmm-muted); font-size: 12px; line-height: 1.7; }
    #kmm-modal .kmm-empty { flex: 1; display: flex; flex-direction: column; justify-content: center; align-items: center; gap: 8px; min-height: 180px; padding: 32px; text-align: center; color: var(--kmm-muted); }
    #kmm-modal .kmm-empty strong { color: var(--kmm-text); font-size: 14px; font-weight: 550; }
    #kmm-modal .kmm-empty p { max-width: 340px; margin: 0; font-size: 12px; line-height: 1.8; }
    #kmm-modal .kmm-item-menu { position: relative; }
    #kmm-modal .kmm-item-menu > summary { display: flex; align-items: center; justify-content: center; width: 30px; height: 30px; list-style: none; cursor: pointer; border-radius: 7px; font-size: 18px; color: var(--kmm-muted); }
    #kmm-modal .kmm-item-menu > summary:hover { background: color-mix(in srgb, var(--kmm-text) 6%, transparent); }
    #kmm-modal .kmm-item-menu > summary::-webkit-details-marker { display: none; }
    #kmm-modal .kmm-item-menu[open] { z-index: 4; }
    #kmm-modal .kmm-menu { position: absolute; right: 0; top: 34px; width: 230px; max-width: 70vw; padding: 5px; border: 1px solid var(--kmm-border); background: var(--color-surface-raised, #292929); box-shadow: 0 8px 24px #0004; border-radius: 10px; }
    #kmm-modal .kmm-menu button { display: block; width: 100%; padding: 9px 10px; text-align: left; border-radius: 6px; white-space: normal; }
    #kmm-modal .kmm-menu-reason { display: block; padding: 0 10px 9px; font-size: 11px; line-height: 1.6; color: var(--kmm-muted); }
    #kmm-tools { position: relative; }
    #kmm-tools-menu { z-index: 5; top: 44px; }
    #kmm-modal #kmm-manager-status { margin: 14px 28px; padding: 10px 12px; border: 1px solid var(--kmm-border); border-radius: 8px; background: var(--kmm-soft); color: var(--kmm-muted); font-size: 12px; line-height: 1.7; }
    #kmm-modal #kmm-manager-status.is-warning { border-color: color-mix(in srgb, var(--color-warning, #d29922) 22%, transparent); color: var(--color-warning, #d29922); }
    #kmm-status-action { margin-left: 6px; }
    #kmm-modal #kmm-batch { border: 0; overflow: hidden; }
    #kmm-modal #kmm-detail .kmm-batch-head { padding: 22px 28px 16px; gap: 12px; }
    #kmm-modal .kmm-batch-head .kmm-field { flex: 1; min-width: 180px; }
    #kmm-modal .kmm-batch-head .kmm-search { flex-basis: 100%; margin-top: 4px; }
    #kmm-modal #kmm-detail .kmm-batch-filters { flex-wrap: nowrap; overflow-x: auto; flex-shrink: 0; gap: 5px; padding: 0 28px 16px; }
    #kmm-modal .kmm-batch-filters .kmm-tbtn { flex-shrink: 0; min-height: 30px; padding: 5px 10px; border-color: transparent; font-size: 12px; font-weight: 500; color: var(--kmm-muted); }
    #kmm-modal .kmm-batch-filters .kmm-tbtn.on { color: var(--kmm-accent); background: color-mix(in srgb, var(--kmm-accent) 9%, transparent); }
    #kmm-modal #kmm-detail .kmm-batch-note { padding: 0 28px 12px; font-size: 11px; line-height: 1.7; }
    #kmm-modal #kmm-batch-status { color: var(--kmm-muted); }
    #kmm-modal #kmm-batch-status.is-warning { color: var(--color-warning, #d29922); }
    #kmm-modal #kmm-batch-status:empty { display: none; }
    #kmm-modal #kmm-detail .kmm-batch-list { flex: 1; min-height: 100px; max-height: none; padding: 0 28px 20px; gap: 6px; }
    #kmm-modal .kmm-batch-row { padding: 12px 14px; gap: 12px; border-color: var(--kmm-border); border-radius: 9px; background: transparent; }
    #kmm-modal .kmm-batch-row:has(input) { cursor: pointer; }
    #kmm-modal .kmm-batch-row:hover { background: var(--kmm-soft); }
    #kmm-modal .kmm-batch-row.pending { border-color: color-mix(in srgb, var(--kmm-accent) 36%, transparent); background: color-mix(in srgb, var(--kmm-accent) 5%, transparent); }
    #kmm-modal .kmm-batch-title { justify-content: space-between; font-size: 13px; font-weight: 550; }
    #kmm-modal .kmm-batch-meta { font-size: 11px; }
    #kmm-modal #kmm-detail .kmm-batch-footer { flex-shrink: 0; margin-top: auto; padding: 16px 28px; border-color: var(--kmm-border); gap: 10px; background: var(--kmm-bg); }
    #kmm-modal #kmm-batch-count { min-width: 0; }
    #kmm-modal .kmm-context-presets { margin-top: 1px; }
    #kmm-modal .kmm-context-presets .kmm-tbtn { min-height: 26px; padding: 3px 10px; font-size: 11px; }
    #kmm-modal .kmm-key-actions { display: flex; gap: 6px; flex-wrap: wrap; }
    #kmm-modal .kmm-key-actions label { display: inline-flex; align-items: center; gap: 7px; padding: 9px 12px; border: 1px solid var(--kmm-border); border-radius: 8px; color: var(--kmm-muted); font-size: 12px; }
    #kmm-modal .kmm-key-actions label:has(input:checked) { color: var(--kmm-accent); border-color: color-mix(in srgb, var(--kmm-accent) 30%, transparent); background: color-mix(in srgb, var(--kmm-accent) 5%, transparent); }
    #kmm-modal :is(#ksk-overlay, #kpr-overlay, #ksync-overlay) { position: static; inset: auto; z-index: auto; background: transparent; backdrop-filter: none; padding: 0; flex: 1; min-height: 0; overflow: auto; opacity: 1; display: flex; }
    #kmm-modal :is(#ksk-box, #kpr-modal, #ksync-modal) { width: 100%; max-width: none; max-height: none; border: 0; border-radius: 0; box-shadow: none; margin: 0; padding: 24px 28px; box-sizing: border-box; transform: none; opacity: 1; flex: 1; min-height: 0; background: transparent; color: var(--kmm-text); }
    #kmm-modal :is(#kpr-modal, #ksync-modal) { display: flex; flex-direction: column; gap: 16px; }
    #kmm-modal :is(.kpr-kicker, .ksync-kicker) { display: none; }
    #kmm-modal :is(#kpr-modal, #ksync-modal) h3 { margin: 0; font-size: 16px; overflow-wrap: anywhere; }
    #kmm-modal :is(.kpr-model, .ksync-meta, .kpr-hint) { margin: 0; color: var(--kmm-muted); font-size: 12px; line-height: 1.7; }
    #kmm-modal .kpr-label { margin: 0 0 8px; font-size: 12px; font-weight: 550; }
    #kmm-modal .kpr-seg { padding: 4px; gap: 4px; background: var(--kmm-soft); border: 1px solid var(--kmm-border); border-radius: 10px; }
    #kmm-modal .kpr-seg button { border: 0; border-radius: 6px; padding: 9px 10px; font-size: 12px; }
    #kmm-modal .kpr-seg button.active { background: var(--kmm-bg); color: var(--kmm-accent); box-shadow: 0 1px 4px #0001; }
    #kmm-modal .kpr-grid { gap: 18px; margin: 0; }
    #kmm-modal .kpr-field input { padding: 11px 12px; border-color: var(--kmm-border); background: var(--kmm-input); color: var(--kmm-text); font-size: 13px; }
    #kmm-modal .kpr-field label { font-size: 12px; color: var(--kmm-muted); font-weight: 500; }
    #kmm-modal :is(.kpr-foot, .ksync-foot) { position: sticky; bottom: -24px; flex-shrink: 0; margin-top: auto; padding: 16px 0; border-color: var(--kmm-border); background: var(--kmm-bg); gap: 10px; flex-wrap: wrap; }
    #kmm-modal :is(.kpr-foot, .ksync-foot) button { padding: 9px 16px; border-radius: 8px; font-size: 12px; }
    #kmm-modal .ksync-rate { flex-wrap: wrap; gap: 10px; margin: 0; font-size: 12px; font-weight: 500; }
    #kmm-modal .ksync-rate input { background: var(--kmm-input); border-color: var(--kmm-border); color: var(--kmm-text); }
    #kmm-modal .ksync-list { min-height: 140px; margin: 0; border-color: var(--kmm-border); }
    #kmm-modal .ksync-row { padding: 12px; gap: 10px; font-size: 12px; border-color: var(--kmm-border); }
    #kmm-modal .ksync-row .ksync-alias { min-width: 0; flex: 1; }
    #kmm-modal .ksync-row .ksync-match { flex: 1; }
    #kmm-modal #ksk-body { min-height: 260px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; line-height: 1.8; }
    #kmm-modal #ksk-box pre { padding: 18px; margin: 0; border: 1px solid var(--kmm-border); border-radius: 10px; background: var(--kmm-input); color: var(--kmm-text); font-size: 12px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
    #kmm-modal #ksk-box h3 { margin: 0; font-size: 16px; overflow-wrap: anywhere; }
    #kmm-modal #kmm-confirm { position: absolute; inset: 0; z-index: 10; background: #0007; display: flex; align-items: center; justify-content: center; padding: 24px; }
    #kmm-modal .kmm-confirm-box { width: 400px; max-width: 100%; padding: 26px; border: 1px solid var(--kmm-border); border-radius: 14px; background: var(--kmm-bg); box-shadow: 0 12px 48px #0006; }
    #kmm-modal #kmm-confirm-title { margin: 0 0 12px; font-size: 17px; font-weight: 600; }
    #kmm-modal #kmm-confirm-message { white-space: pre-line; overflow-wrap: anywhere; font-size: 13px; line-height: 1.8; margin-bottom: 22px; color: var(--kmm-muted); }
    #kmm-modal .kmm-confirm-box .kmm-editor-actions { position: static; border: 0; padding: 0; }
    #kmm-modal .kmm-help { line-height: 1.9; font-size: 13px; padding-bottom: 28px !important; }
    #kmm-modal .kmm-help h3 { margin: 8px 0 0; padding-top: 16px; font-size: 15px; font-weight: 600; border-top: 1px solid var(--kmm-border); }
    #kmm-modal .kmm-help h3:first-child { padding-top: 0; border: 0; margin-top: 0; }
    #kmm-modal .kmm-help p { margin: 0; color: var(--kmm-muted); }
    #kmm-modal .kmm-help code { padding: 2px 6px; border-radius: 4px; background: var(--kmm-soft); color: var(--kmm-text); }
    #kmm-modal .kmm-foot { min-height: 44px; padding: 10px 24px; align-items: center; border-color: var(--kmm-border); font-size: 11px; color: var(--kmm-muted); }
    #kmm-modal :is(.kmm-modal-body, .kmm-batch-list, .kmm-batch-filters, #ksk-overlay, #kpr-overlay, #ksync-overlay) { scrollbar-width: thin; scrollbar-color: color-mix(in srgb, var(--kmm-text) 15%, transparent) transparent; }
    @media (max-width: 800px) {
      #kmm-modal { max-width: calc(100vw - 24px); height: 90vh; max-height: calc(100dvh - 24px); }
      #kmm-modal .kmm-tabs { width: 140px; flex-basis: 140px; padding: 14px 8px; }
      #kmm-modal .kmm-nav { gap: 8px; padding: 10px; }
      #kmm-modal .kmm-editor-grid { grid-template-columns: 1fr; }
      #kmm-modal .kmm-page-head { padding: 22px 20px 16px; }
      #kmm-modal .kmm-toolbar { padding: 0 20px 16px; }
      #kmm-modal .kmm-lists > .kmm-modal-body { padding: 0 20px 20px; }
      #kmm-modal .kmm-top { flex-wrap: wrap; gap: 8px; }
      #kmm-modal .kmm-acts { margin-left: auto; }
    }
    @media (max-width: 600px) {
      #kmm-modal { max-width: calc(100vw - 16px); height: 94vh; max-height: calc(100dvh - 16px); border-radius: 12px; }
      #kmm-modal .kmm-modal-header { min-height: 60px; padding: 14px 16px; }
      #kmm-modal .kmm-modal-title { gap: 12px; }
      #kmm-modal .kmm-def-badge { max-width: 180px; font-size: 11px; }
      #kmm-modal .kmm-workspace { flex-direction: column; }
      #kmm-modal .kmm-tabs { flex: 0 0 auto; width: 100%; padding: 8px; flex-direction: row; overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--kmm-border); gap: 4px; }
      #kmm-modal .kmm-nav { flex: 1 0 auto; justify-content: center; min-height: 36px; padding: 8px 10px; font-size: 12px; gap: 6px; }
      #kmm-modal .kmm-nav svg { display: none; }
      #kmm-modal .kmm-nav-count { margin-left: 0; font-size: 10px; }
      #kmm-modal .kmm-page-head { padding: 18px 16px 14px; }
      #kmm-modal .kmm-page-head h3 { font-size: 18px; }
      #kmm-modal .kmm-toolbar { padding: 0 16px 14px; flex-wrap: wrap; gap: 8px; }
      #kmm-modal .kmm-toolbar .kmm-search { flex: 1 1 140px; max-width: none; }
      #kmm-modal .kmm-lists > .kmm-modal-body { padding: 0 16px 16px; }
      #kmm-modal .kmm-card { padding: 13px; }
      #kmm-modal .kmm-name strong { font-size: 13px; }
      #kmm-modal .kmm-detail-head { padding: 14px 16px; gap: 10px; }
      #kmm-modal #kmm-detail-title { font-size: 14px; }
      #kmm-modal #kmm-detail > .kmm-modal-body { padding: 18px 16px 0; }
      #kmm-modal #kmm-detail :is(.kmm-batch-head, #ksk-box, #kpr-modal, #ksync-modal) { padding: 18px 16px; }
      #kmm-modal .kmm-batch-head .kmm-field { min-width: 100%; }
      #kmm-modal .kmm-batch-head .kmm-search { flex-basis: 100%; }
      #kmm-modal #kmm-detail :is(.kmm-batch-filters, .kmm-batch-note) { padding-left: 16px; padding-right: 16px; }
      #kmm-modal #kmm-detail .kmm-batch-list { padding: 0 16px 16px; }
      #kmm-modal #kmm-detail .kmm-batch-footer { padding: 12px 16px; }
      #kmm-modal .kpr-grid { grid-template-columns: 1fr; }
      #kmm-modal :is(.kpr-foot, .ksync-foot) { bottom: -18px; }
      #kmm-modal .ksync-row { flex-wrap: wrap; }
      #kmm-modal .ksync-row .ksync-match { flex-basis: 60%; }
      #kmm-modal .kmm-foot { padding: 8px 16px; }
      #kmm-modal .kmm-foot > span { display: none; }
      #kmm-modal .kmm-foot > button { margin-left: auto; }
      #kmm-modal .kmm-summary { gap: 5px 10px; }
      #kmm-modal .kmm-summary span + span::before { content: none; }
    }
    .kmm-search {
      flex: 1; min-width: 0; box-sizing: border-box;
      background: var(--color-surface-sunken, #121212);
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 11%, transparent); color: var(--color-text, #e2e8f0);
      border-radius: 7px; padding: 7px 10px; font-size: 12px; outline: none; font-family: inherit;
    }
    .kmm-search:focus { border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 70%, transparent); box-shadow: 0 0 0 2px color-mix(in srgb, var(--color-accent, #1a88ff) 12%, transparent); }
    .kmm-tbtn {
      padding: 7px 11px; border-radius: 7px; font-size: 12px; font-weight: 600; cursor: pointer;
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 13%, transparent);
      background: transparent; color: var(--color-text, #e2e8f0); font-family: inherit; white-space: nowrap;
      transition: border-color .15s, background .15s, color .15s;
    }
    .kmm-tbtn:hover { border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 70%, transparent); color: var(--color-accent, #1a88ff); background: color-mix(in srgb, var(--color-accent, #1a88ff) 6%, transparent); }
    .kmm-tbtn.primary { background: var(--color-accent, #1a88ff); border-color: var(--color-accent, #1a88ff); color: #fff; }
    .kmm-tbtn.primary:hover { filter: brightness(1.08); color: #fff; background: var(--color-accent, #1a88ff); }
    .kmm-modal-body {
      padding: 12px 16px 14px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 8px;
      border-top: 1px solid color-mix(in srgb, var(--color-text, #fff) 7%, transparent);
    }
    .kmm-foot {
      padding: 8px 16px; font-size: 11px; color: var(--color-text-faint, #94a3b8);
      display: flex; justify-content: space-between; gap: 10px;
      border-top: 1px solid color-mix(in srgb, var(--color-text, #fff) 7%, transparent);
    }
    .kmm-card {
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 9%, transparent);
      border-radius: 9px;
      padding: 10px 12px;
      background: var(--color-surface-raised, #292929);
      transition: border-color .15s, box-shadow .15s;
    }
    .kmm-card:hover { border-color: color-mix(in srgb, var(--color-text, #fff) 16%, transparent); box-shadow: 0 1px 6px rgba(0,0,0,.05); }
    .kmm-card.is-default {
      border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 55%, transparent);
      background: color-mix(in srgb, var(--color-accent, #1a88ff) 5%, var(--color-surface-raised, #292929));
    }
    .kmm-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .kmm-name { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; min-width: 0; }
    .kmm-name strong { font-size: 14px; font-weight: 700; }
    .kmm-tag { font-size: 10.5px; padding: 1px 7px; border-radius: 999px; font-weight: 600; white-space: nowrap; }
    .kmm-tag.cur { background: var(--color-accent, #1a88ff); color: #fff; }
    .kmm-tag.def { background: color-mix(in srgb, var(--color-warning, #d29922) 22%, transparent); color: var(--color-warning, #b8860b); }
    .kmm-tag.on  { background: color-mix(in srgb, var(--color-success, #3fb950) 16%, transparent); color: var(--color-success, #2da44e); }
    .kmm-tag.ad  { background: color-mix(in srgb, var(--color-accent, #1a88ff) 14%, transparent); color: var(--color-accent, #1a88ff); }
    .kmm-acts { display: flex; gap: 2px; flex-shrink: 0; }
    .kmm-link {
      background: transparent; border: none; cursor: pointer; font-size: 11.5px; font-family: inherit;
      color: var(--color-text-secondary, #64748b); padding: 3px 6px; border-radius: 5px;
    }
    .kmm-link:hover { background: color-mix(in srgb, var(--color-text, #fff) 7%, transparent); color: var(--color-text, #e2e8f0); }
    .kmm-link.accent { color: var(--color-accent, #1a88ff); }
    .kmm-link.danger { color: var(--color-danger, #f85149); }
    .kmm-meta { margin-top: 3px; font-size: 11.5px; color: var(--color-text-faint, #94a3b8); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .kmm-ctl { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
    .kmm-chips { display: flex; gap: 4px; }
    .kmm-chip {
      display: inline-flex; align-items: center; cursor: pointer; user-select: none;
      padding: 3px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 500;
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 13%, transparent);
      color: var(--color-text-faint, #94a3b8); background: transparent; transition: all .15s;
    }
    .kmm-chip input { display: none; }
    .kmm-chip.on {
      color: var(--color-accent, #1a88ff); font-weight: 600;
      border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 45%, transparent);
      background: color-mix(in srgb, var(--color-accent, #1a88ff) 10%, transparent);
    }
    .kmm-chip.lock { cursor: not-allowed; opacity: .75; }
    .kmm-eff { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
    .kmm-eff-label { font-size: 11px; color: var(--color-text-faint, #94a3b8); }
    .kmm-seg {
      display: inline-flex; padding: 2px; border-radius: 7px; gap: 1px;
      background: var(--color-surface-sunken, #121212);
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 8%, transparent);
    }
    .kmm-seg button {
      border: none; background: transparent; cursor: pointer; font-family: inherit;
      padding: 3px 8px; border-radius: 5px; font-size: 11px; font-weight: 500;
      color: var(--color-text-secondary, #64748b);
    }
    .kmm-seg button:hover { color: var(--color-text, #e2e8f0); }
    .kmm-seg button.on { background: var(--color-accent, #1a88ff); color: #fff; font-weight: 600; cursor: default; }
    .kmm-warn { margin-top: 8px; font-size: 11.5px; line-height: 1.5; }
    .kmm-note { font-size: 11px; color: var(--color-text-faint, #94a3b8); cursor: help; }
    #kpr-overlay {
      position: fixed; inset: 0; z-index: 100002;
      background: rgba(0,0,0,.45); backdrop-filter: blur(3px);
      display: none; align-items: center; justify-content: center;
      font-family: system-ui, -apple-system, sans-serif;
      opacity: 0; transition: opacity 0.17s ease;
    }
    #kpr-overlay.visible { display: flex; }
    #kpr-overlay.ku-anim-in { opacity: 1; }
    #kpr-overlay.ku-anim-in #kpr-modal {
      transform: none; opacity: 1;
      transition: transform 0.18s cubic-bezier(0.25, 0.6, 0.3, 1) 0.02s, opacity 0.15s ease 0.02s;
    }
    #kpr-modal {
      width: 460px; max-height: 86vh; overflow-y: auto;
      transform: translateY(10px) scale(0.98); opacity: 0;
      transition: transform 0.14s ease-in, opacity 0.12s ease-in;
      background: var(--color-surface-raised, #ffffff);
      color: var(--color-text, #1f2329);
      border-radius: 14px; box-shadow: 0 24px 64px rgba(0,0,0,.35);
      padding: 26px 28px 22px;
    }
    #kpr-modal .kpr-kicker { font-size: 11px; letter-spacing: .12em; color: var(--color-text-faint, #94a3b8); font-weight: 600; }
    #kpr-modal h3 { margin: 4px 0 0; font-size: 20px; font-weight: 700; }
    #kpr-modal .kpr-model { margin: 14px 0 6px; font-size: 12px; color: var(--color-text-secondary, #64748b); }
    #kpr-modal .kpr-model b { color: var(--color-text, #1f2329); font-size: 13px; }
    .kpr-label { font-size: 13px; font-weight: 600; margin: 14px 0 8px; }
    .kpr-seg { display: flex; gap: 8px; }
    .kpr-seg button {
      flex: 1; padding: 9px 0; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer;
      border: 1px solid color-mix(in srgb, var(--color-text, #000) 14%, transparent);
      background: transparent; color: var(--color-text-secondary, #64748b);
    }
    .kpr-seg button.active {
      background: var(--color-accent, #1a88ff); border-color: var(--color-accent, #1a88ff); color: #fff;
    }
    .kpr-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 4px; }
    .kpr-field label { display: block; font-size: 12px; font-weight: 600; margin-bottom: 5px; }
    .kpr-field input {
      width: 100%; box-sizing: border-box; padding: 9px 12px; font-size: 14px;
      border-radius: 8px; border: 1px solid color-mix(in srgb, var(--color-text, #000) 16%, transparent);
      background: var(--color-surface-sunken, #f5f6f7); color: var(--color-text, #1f2329); outline: none;
    }
    .kpr-field input:focus { border-color: var(--color-accent, #1a88ff); }
    .kpr-hint { font-size: 11.5px; color: var(--color-text-faint, #94a3b8); margin-top: 5px; }
    .kpr-foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 22px; padding-top: 16px; border-top: 1px solid color-mix(in srgb, var(--color-text, #000) 8%, transparent); }
    .kpr-foot button { padding: 9px 22px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; }
    .kpr-cancel { background: var(--color-surface-sunken, #eef0f2); border: none; color: var(--color-text-secondary, #64748b); }
    .kpr-save { background: var(--color-accent, #1a88ff); border: none; color: #fff; }
    .kpr-reset { background: none; border: none; color: var(--color-text-faint, #94a3b8); margin-right: auto; font-weight: 500; }
    #ksync-overlay {
      position: fixed; inset: 0; z-index: 100003;
      background: rgba(0,0,0,.45); backdrop-filter: blur(3px);
      display: none; align-items: center; justify-content: center;
      font-family: system-ui, -apple-system, sans-serif;
      opacity: 0; transition: opacity 0.17s ease;
    }
    #ksync-overlay.visible { display: flex; }
    #ksync-overlay.ku-anim-in { opacity: 1; }
    #ksync-overlay.ku-anim-in #ksync-modal {
      transform: none; opacity: 1;
      transition: transform 0.18s cubic-bezier(0.25, 0.6, 0.3, 1) 0.02s, opacity 0.15s ease 0.02s;
    }
    #ksync-modal {
      width: 680px; max-height: 88vh; display: flex; flex-direction: column;
      transform: translateY(10px) scale(0.98); opacity: 0;
      transition: transform 0.14s ease-in, opacity 0.12s ease-in;
      background: var(--color-surface-raised, #ffffff);
      color: var(--color-text, #1f2329);
      border-radius: 14px; box-shadow: 0 24px 64px rgba(0,0,0,.35);
      padding: 22px 24px 18px;
    }
    #ksync-modal .ksync-kicker { font-size: 11px; letter-spacing: .12em; color: var(--color-text-faint, #94a3b8); font-weight: 600; }
    #ksync-modal h3 { margin: 4px 0 0; font-size: 18px; font-weight: 700; }
    .ksync-meta { margin-top: 6px; font-size: 12px; color: var(--color-text-secondary, #64748b); }
    .ksync-rate { display: flex; align-items: center; gap: 8px; margin-top: 10px; font-size: 12.5px; font-weight: 600; }
    .ksync-rate input {
      width: 90px; padding: 6px 10px; font-size: 13px; border-radius: 7px;
      border: 1px solid color-mix(in srgb, var(--color-text, #000) 16%, transparent);
      background: var(--color-surface-sunken, #f5f6f7); color: var(--color-text, #1f2329); outline: none;
    }
    .ksync-rate button {
      padding: 6px 14px; border-radius: 7px; font-size: 12.5px; font-weight: 600; cursor: pointer;
      background: var(--color-surface-sunken, #eef0f2); border: none; color: var(--color-text-secondary, #64748b);
    }
    .ksync-rate button:disabled { opacity: .55; cursor: default; }
    .ksync-list {
      margin-top: 12px; overflow-y: auto; min-height: 120px; flex: 1;
      border: 1px solid color-mix(in srgb, var(--color-text, #000) 10%, transparent); border-radius: 10px;
    }
    .ksync-row { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-bottom: 1px solid color-mix(in srgb, var(--color-text, #000) 6%, transparent); font-size: 12.5px; }
    .ksync-row:last-child { border-bottom: none; }
    .ksync-row.unmatched { opacity: .55; }
    .ksync-row input[type="checkbox"] { width: 15px; height: 15px; flex: none; cursor: pointer; }
    .ksync-row .ksync-alias { font-weight: 600; min-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ksync-row .ksync-match { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--color-text-secondary, #64748b); }
    .ksync-row .ksync-cost { flex: none; font-variant-numeric: tabular-nums; color: var(--color-text-secondary, #64748b); }
    .ksync-row .ksync-badge { flex: none; font-size: 10.5px; padding: 2px 7px; border-radius: 999px; background: color-mix(in srgb, #16a34a 12%, transparent); color: #16a34a; font-weight: 600; }
    .ksync-row .ksync-badge.fuzzy { background: color-mix(in srgb, #d97706 12%, transparent); color: #d97706; }
    .ksync-row .ksync-badge.none { background: color-mix(in srgb, var(--color-text, #000) 8%, transparent); color: var(--color-text-faint, #94a3b8); }
    .ksync-empty { padding: 28px 16px; text-align: center; font-size: 12.5px; color: var(--color-text-faint, #94a3b8); }
    .ksync-foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; padding-top: 14px; border-top: 1px solid color-mix(in srgb, var(--color-text, #000) 8%, transparent); }
    .ksync-foot button { padding: 9px 22px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; }
    .ksync-cancel { background: var(--color-surface-sunken, #eef0f2); border: none; color: var(--color-text-secondary, #64748b); }
    .ksync-apply { background: var(--color-accent, #1a88ff); border: none; color: #fff; }
    .ksync-apply:disabled { opacity: .55; cursor: default; }
    body.kud-has-update #kup-update-btn, body.kud-has-update #ku-panel-btn, body.kud-has-update #ku-min-panel-btn { position: relative; }
    body.kud-has-update #kup-update-btn::after, body.kud-has-update #ku-panel-btn::after, body.kud-has-update #ku-min-panel-btn::after {
      content: ''; position: absolute; top: -3px; right: -3px; width: 8px; height: 8px;
      border-radius: 50%; background: #ef4444; box-shadow: 0 0 0 2px var(--color-surface-raised, #fff);
    }
    #kup-mute-btn { opacity: .75; }
    #kud-overlay {
      position: fixed; inset: 0; z-index: 100003;
      background: rgba(0,0,0,.45); backdrop-filter: blur(3px);
      display: none; align-items: center; justify-content: center;
      font-family: system-ui, -apple-system, sans-serif;
      opacity: 0; transition: opacity 0.17s ease;
    }
    #kud-overlay.visible { display: flex; }
    #kud-overlay.ku-anim-in { opacity: 1; }
    #kud-modal {
      width: 380px; max-width: 90vw; box-sizing: border-box;
      background: var(--color-surface-raised, #ffffff); color: var(--color-text, #1f2329);
      border-radius: 14px; box-shadow: 0 24px 64px rgba(0,0,0,.35); padding: 24px 26px 20px;
    }
    #kud-modal h3 { margin: 0 0 10px; font-size: 17px; font-weight: 700; }
    #kud-modal .kud-ver { font-size: 13px; color: var(--color-text-secondary, #64748b); line-height: 1.7; }
    #kud-modal .kud-ver b { color: var(--color-accent, #1a88ff); font-size: 15px; }
    #kud-modal .kud-tip { font-size: 11.5px; color: var(--color-text-faint, #94a3b8); margin-top: 8px; }
    #kud-modal .kud-foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
    #kud-modal .kud-foot button { padding: 8px 20px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; }
    #kud-notes { margin-top: 12px; max-height: 38vh; overflow-y: auto; padding: 10px 12px; border-radius: 10px;
      background: var(--color-surface-sunken, #eef0f2); font-size: 12px; line-height: 1.65; color: var(--color-text-secondary, #64748b); }
    #kud-notes .kud-n-h { margin-top: 8px; font-weight: 700; color: var(--color-text, #1f2329); }
    #kud-notes .kud-n-h:first-child { margin-top: 0; }
    #kud-notes .kud-n-li { padding-left: 2px; }
    #kud-notes .kud-n-p { margin-bottom: 4px; }
    #kud-modal { width: 440px; }
    #kud-cancel { background: var(--color-surface-sunken, #eef0f2); color: var(--color-text-secondary, #64748b); }
    #kud-ok { background: var(--color-accent, #1a88ff); color: #fff; }
    .ku-mode-row {
      margin: 0 0 9px; padding: 9px 10px 8px; border-radius: 10px;
      background: color-mix(in srgb, var(--color-accent, #1a88ff) 4.5%, transparent);
      border: 1px solid color-mix(in srgb, var(--color-accent, #1a88ff) 14%, transparent);
      transition: border-color .15s ease, box-shadow .15s ease;
    }
    .ku-mode-row:hover { border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 30%, transparent); }
    .ku-mode-top { display: flex; align-items: center; gap: 7px; }
    .ku-mode-top .ku-row-label { font-size: 10px; font-weight: 700; color: var(--color-text-faint, #94a3b8); text-transform: uppercase; letter-spacing: .4px; width: auto; }
    .ku-mode-scope { display: flex; gap: 4px; flex-shrink: 0; }
    .ku-mode-scope button {
      border: 1px solid color-mix(in srgb, var(--color-text-faint, #94a3b8) 28%, transparent);
      background: transparent; cursor: pointer; font-size: 10px; font-weight: 600; padding: 2px 7px;
      border-radius: 6px; color: var(--color-text-faint, #94a3b8); font-family: inherit; line-height: 1.5;
      transition: all .15s ease;
    }
    .ku-mode-scope button:hover { color: var(--color-accent, #1a88ff); }
    .ku-mode-scope button.on { color: #fff; background: var(--color-accent, #1a88ff); border-color: var(--color-accent, #1a88ff); }
    .ku-mode-scope button:disabled { opacity: .5; cursor: wait; }
    .ku-mode-name { flex: 1; min-width: 0; font-size: 12.5px; font-weight: 700; color: var(--color-text, #0f172a); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ku-mode-edit {
      border: 1px solid color-mix(in srgb, var(--color-accent, #1a88ff) 28%, transparent);
      background: color-mix(in srgb, var(--color-accent, #1a88ff) 7%, transparent);
      cursor: pointer; font-size: 10.5px; font-weight: 600; padding: 2px 8px; border-radius: 6px;
      color: var(--color-accent, #1a88ff); font-family: inherit; line-height: 1.5;
      transition: all .15s ease;
    }
    .ku-mode-edit:hover { background: var(--color-accent, #1a88ff); color: #fff; border-color: var(--color-accent, #1a88ff); }
    .ku-mode-lock { border: 1px solid color-mix(in srgb, var(--color-text-faint, #94a3b8) 30%, transparent);
      background: transparent; cursor: pointer; font-size: 10.5px; font-weight: 600; padding: 2px 7px;
      border-radius: 6px; color: var(--color-text-faint, #94a3b8); font-family: inherit; line-height: 1.5;
      transition: all .15s ease; flex-shrink: 0; }
    .ku-mode-lock:hover { color: var(--color-accent, #1a88ff); border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 40%, transparent);
      background: color-mix(in srgb, var(--color-accent, #1a88ff) 7%, transparent); }
    .ku-mode-lock.on { color: var(--color-warning, #d29922); border-color: color-mix(in srgb, var(--color-warning, #d29922) 45%, transparent);
      background: color-mix(in srgb, var(--color-warning, #d29922) 12%, transparent); }
    .ku-mode-lock.on:hover { background: color-mix(in srgb, var(--color-warning, #d29922) 20%, transparent); }
    .ku-mode-lock:disabled { opacity: .5; cursor: wait; }
    .ku-mode-lock-badge { display: inline-block; font-size: 10px; color: var(--color-warning, #d29922);
      background: color-mix(in srgb, var(--color-warning, #d29922) 12%, transparent);
      padding: 1px 6px; border-radius: 5px; margin-left: 5px; vertical-align: 1px; font-weight: 600; }
    .ku-host-model-locked { opacity: .45 !important; pointer-events: none !important; cursor: not-allowed !important; filter: grayscale(.4); }
    /* 只置灰视觉、不锁事件的容器命中：包住可交互控件时用这条，绝不冻输入/发送 */
    .ku-host-model-locked-v { opacity: .5 !important; filter: grayscale(.35); }
    /* 命中元素若意外包住输入框，仍放行内部可编辑子元素，避免整框被冻住 */
    .ku-host-model-locked textarea, .ku-host-model-locked input,
    .ku-host-model-locked [contenteditable="true"] { pointer-events: auto !important; opacity: 1 !important; filter: none !important; cursor: text !important; }
    .ku-mode-range { width: 100%; margin: 8px 0 0; accent-color: var(--color-accent, #1a88ff); cursor: pointer; -webkit-appearance: none; appearance: none; height: 20px; background: transparent; }
    .ku-mode-range::-webkit-slider-runnable-track { height: 5px; border-radius: 3px; background: color-mix(in srgb, var(--color-accent, #1a88ff) 18%, color-mix(in srgb, var(--color-text,#000) 7%, transparent)); }
    .ku-mode-range::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 17px; height: 17px; margin-top: -6px; border-radius: 50%; background: var(--color-accent, #1a88ff); border: 2.5px solid var(--color-surface, #fff); box-shadow: 0 1px 6px rgba(0,0,0,.28), 0 0 0 0 color-mix(in srgb, var(--color-accent, #1a88ff) 0%, transparent); transition: transform .15s ease, box-shadow .2s ease; }
    .ku-mode-range.ku-snapping::-webkit-slider-thumb { transition: transform .16s cubic-bezier(.2,.9,.25,1.4), box-shadow .2s ease; }
    .ku-mode-range:not(:disabled):hover::-webkit-slider-thumb { transform: scale(1.12); box-shadow: 0 1px 6px rgba(0,0,0,.28), 0 0 0 5px color-mix(in srgb, var(--color-accent, #1a88ff) 14%, transparent); }
    .ku-mode-range:not(:disabled):active::-webkit-slider-thumb { transform: scale(1.2); box-shadow: 0 1px 6px rgba(0,0,0,.3), 0 0 0 8px color-mix(in srgb, var(--color-accent, #1a88ff) 18%, transparent); }
    .ku-mode-range::-moz-range-track { height: 5px; border-radius: 3px; background: color-mix(in srgb, var(--color-accent, #1a88ff) 18%, color-mix(in srgb, var(--color-text,#000) 7%, transparent)); }
    .ku-mode-range::-moz-range-thumb { width: 17px; height: 17px; border-radius: 50%; background: var(--color-accent, #1a88ff); border: 2.5px solid var(--color-surface, #fff); box-shadow: 0 1px 6px rgba(0,0,0,.28); transition: transform .15s ease, box-shadow .2s ease; }
    .ku-mode-range:not(:disabled):hover::-moz-range-thumb { transform: scale(1.12); box-shadow: 0 1px 6px rgba(0,0,0,.28), 0 0 0 5px color-mix(in srgb, var(--color-accent, #1a88ff) 14%, transparent); }
    .ku-mode-range:disabled { cursor: wait; }
    .ku-mode-range:disabled::-webkit-slider-thumb { opacity: .55; }
    .ku-mode-range:disabled::-moz-range-thumb { opacity: .55; }
    .ku-mode-ticks { display: flex; justify-content: space-between; gap: 2px; font-size: 10.5px; color: var(--color-text-faint, #94a3b8); margin-top: 4px; padding: 0 1px; }
    .ku-mode-ticks span { flex: 1; text-align: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; padding: 2px 3px; border-radius: 5px; transition: background .12s ease, color .12s ease, font-weight .12s; }
    .ku-mode-ticks span:hover { background: color-mix(in srgb, var(--color-text, #000) 5%, transparent); color: var(--color-text-secondary, #64748b); }
    .ku-mode-ticks span.on { background: color-mix(in srgb, var(--color-accent, #1a88ff) 13%, transparent); color: var(--color-accent, #1a88ff); font-weight: 700; }
    .ku-mode-desc { font-size: 10.5px; line-height: 1.5; color: var(--color-text-secondary, #64748b); margin-top: 6px; padding-left: 7px; border-left: 2px solid color-mix(in srgb, var(--color-accent, #1a88ff) 30%, transparent); word-break: break-all; }
    .ku-mode-desc b { color: var(--color-text, #0f172a); font-weight: 600; }
    .ku-mode-desc .warn { color: var(--color-warning, #d29922); }
    #kmd-overlay {
      position: fixed; inset: 0; z-index: 100002;
      background: rgba(0,0,0,.45); backdrop-filter: blur(3px);
      display: none; align-items: center; justify-content: center;
      font-family: system-ui, -apple-system, sans-serif;
      opacity: 0; transition: opacity 0.17s ease;
    }
    #kmd-overlay.visible { display: flex; opacity: 1; }
    #kmd-overlay.ku-anim-in { opacity: 1; }
    #kmd-modal {
      width: 620px; max-width: 92vw; max-height: 86vh; display: flex; flex-direction: column;
      background: var(--color-surface, #1f1f1f); color: var(--color-text, #e2e8f0);
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 10%, transparent);
      border-radius: 14px; box-shadow: 0 24px 64px rgba(0,0,0,.35); overflow: hidden;
    }
    .kmd-head { padding: 16px 20px 10px; display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
    .kmd-head h3 { margin: 0; font-size: 16px; font-weight: 700; }
    .kmd-head .kmd-sub { font-size: 11.5px; color: var(--color-text-faint, #94a3b8); }
    .kmd-body { padding: 14px 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px;
      border-top: 1px solid color-mix(in srgb, var(--color-text, #fff) 7%, transparent); }
    .kmd-card { border: 1px solid color-mix(in srgb, var(--color-text, #fff) 9%, transparent); border-radius: 11px;
      padding: 11px 13px; background: var(--color-surface-raised, #292929);
      transition: border-color .15s ease, box-shadow .15s ease, background .15s ease; }
    .kmd-card.on { border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 55%, transparent);
      background: color-mix(in srgb, var(--color-accent, #1a88ff) 5%, var(--color-surface-raised, #292929));
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-accent, #1a88ff) 10%, transparent); }
    .kmd-line { display: flex; gap: 8px; align-items: center; margin-top: 7px; flex-wrap: wrap; }
    .kmd-line:first-child { margin-top: 0; }
    .kmd-line label { font-size: 11px; width: 40px; flex-shrink: 0; color: var(--color-text-faint, #94a3b8); font-weight: 600; }
    .kmd-card input[type=text], .kmd-card select {
      flex: 1; min-width: 120px; box-sizing: border-box; padding: 6px 9px; font-size: 12px; font-family: inherit;
      border-radius: 7px; border: 1px solid color-mix(in srgb, var(--color-text, #fff) 14%, transparent);
      background: var(--color-surface-sunken, #121212); color: var(--color-text, #e2e8f0); outline: none;
      transition: border-color .12s ease, box-shadow .12s ease;
    }
    .kmd-card input[type=text]:focus, .kmd-card select:focus {
      border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 55%, transparent);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-accent, #1a88ff) 15%, transparent); }
    .kmd-card select.kmd-sub-sel { flex: 1; min-width: 0; }
    .kmd-idx { font-size: 10px; font-weight: 700; color: var(--color-accent, #1a88ff); flex-shrink: 0;
      width: 40px; text-align: center; box-sizing: border-box;
      padding: 3px 0; border-radius: 5px; background: color-mix(in srgb, var(--color-accent, #1a88ff) 12%, transparent);
      letter-spacing: .3px; }
    .kmd-acts { display: flex; gap: 2px; margin-left: auto; flex-shrink: 0; }
    .kmd-acts .kmm-link { padding: 3px 6px; font-size: 11px; }
    .kmd-acts .kmm-link[data-act=del] { color: var(--color-warning, #d29922); margin-left: 6px; }
    .kmd-acts .kmm-link[data-act=del]:hover { background: color-mix(in srgb, var(--color-warning, #d29922) 12%, transparent); color: var(--color-warning, #d29922); }
    .kmd-foot { padding: 12px 20px; display: flex; gap: 8px; align-items: center;
      border-top: 1px solid color-mix(in srgb, var(--color-text, #fff) 7%, transparent); }
    .kmd-foot .kmd-tip { flex: 1; font-size: 11px; color: var(--color-text-faint, #94a3b8); }
    #kmm-toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: var(--color-success, #3fb950);
      color: #fff;
      padding: 8px 16px;
      border-radius: 8px;
      box-shadow: 0 4px 14px rgba(0,0,0,0.3);
      font-size: 12px;
      font-weight: 500;
      z-index: 100010;
      display: none;
      font-family: system-ui, -apple-system, sans-serif;
    }
`;
  document.head.appendChild(style);

  /* ================================================================
   * 侧栏卡片
   * ================================================================ */
  function updateDOM() {
    // 列表重建后旧 anchor 已脱离 DOM，孤儿弹层一并清掉
    if (kuPopEl && kuPopEl._anchor && !document.contains(kuPopEl._anchor)) kuClosePop();
    const card = document.getElementById(CARD_ID);
    if (!card) return;

    // 状态条
    const sToday = card.querySelector('#ku-strip-today');
    if (sToday) sToday.textContent = state.today.tokens_fmt || '--';
    const sCache = card.querySelector('#ku-strip-cache');
    if (sCache) sCache.textContent = (state.today.cache_pct || state.cache.pct || 0) + '%';
    const sCost = card.querySelector('#ku-strip-cost');
    if (sCost) sCost.textContent = state.today.cost_fmt || '--';

    // 最小化徽章
    const minTokens = card.querySelector('#ku-min-tokens');
    if (minTokens) minTokens.textContent = state.today.tokens_fmt || '--';
    const minCache = card.querySelector('#ku-min-cache');
    if (minCache) minCache.textContent = (state.today.cache_pct || state.cache.pct || 0) + '%';
    // 第二行：今日用量最高的模型（名称 · tokens · 缓存率 · 成本）
    const minModel = card.querySelector('#ku-min-model');
    if (minModel) {
      const fresh = kuMinModelHtml();
      if (fresh && fresh !== kuLastMinHtml) {
        kuLastMinHtml = fresh;
        try { localStorage.setItem(STORAGE_MIN_MODEL_KEY, fresh); } catch (err) {}
      }
      const minHtml = fresh || kuLastMinHtml || '<span class="ku-min-ph">--</span>';
      if (minModel._html !== minHtml) { minModel.innerHTML = minHtml; minModel._html = minHtml; }
    }

    // 今日 / 累计 / 速率
    const tdVal = card.querySelector('#ku-today-val');
    if (tdVal) tdVal.textContent = state.today.tokens_fmt || '--';
    const tdCost = card.querySelector('#ku-today-cost');
    if (tdCost) tdCost.textContent = state.today.cost_fmt || `¥${state.today.cost || 0}`;
    const cmVal = card.querySelector('#ku-cumul-val');
    if (cmVal) cmVal.textContent = state.cumul.tokens_fmt || '--';
    const cmCost = card.querySelector('#ku-cumul-cost');
    if (cmCost) cmCost.textContent = state.cumul.cost_fmt || `¥${state.cumul.cost || 0}`;
    const rtVal = card.querySelector('#ku-rate-val');
    if (rtVal) rtVal.textContent = state.rate.tokens_per_hour || '--';
    const rtCost = card.querySelector('#ku-rate-cost');
    if (rtCost) rtCost.textContent = state.rate.cost_per_hour || '--';

    // 缓存 / 速度
    const cBar = card.querySelector('#ku-cache-bar');
    if (cBar) cBar.style.width = `${state.cache.pct || 0}%`;
    const cPct = card.querySelector('#ku-cache-pct');
    if (cPct) cPct.textContent = state.cache.pct_fmt || '--';
    const spTps = card.querySelector('#ku-speed-tps');
    if (spTps) spTps.textContent = state.speed.tps || '--';
    const spModel = card.querySelector('#ku-speed-model');
    if (spModel) spModel.textContent = shortName(state.speed.model);

    // 当前模型行（usage.cur + models 能力徽章；过长别名降入第二行详情，ℹ 徽标弹出完整信息）
    const curRow = card.querySelector('#ku-cur-row');
    if (curRow) {
      const cur = state.cur || {};
      const alias = cur.alias && cur.alias !== '--' ? cur.alias : (modelsData && modelsData.default_model) || '';
      const cfg = alias && modelsData && modelsData.models
        ? modelsData.models.find(m => m.alias === alias) : null;
      if (!alias) {
        curRow.style.display = 'none';
      } else {
        curRow.style.display = '';
        const nameEl = card.querySelector('#ku-cur-model');
        if (nameEl) {
          nameEl.textContent = shortName(alias);
          nameEl.title = alias;
        }
        // 主行：名称 + 右侧次要信息（思考档位 · 子代理），工具等完整信息在悬停浮层
        const metaEl = card.querySelector('#ku-cur-meta');
        if (metaEl) {
          const meta = [cur.effort, cur.sub_agent].filter(Boolean).join(' · ');
          if (metaEl.textContent !== meta) metaEl.textContent = meta;
        }
        // 滑入名称弹完整信息，滑出即关
        const nameEl2 = card.querySelector('#ku-cur-model');
        kuBindHoverPop(nameEl2, function() { return kuCurModelDetail(alias); }, '当前模型');
      }
    }

    // 配额行（usage.quota 为 null 时整行隐藏）
    const quotaRow = card.querySelector('#ku-quota-row');
    if (quotaRow) {
      const q = state.quota;
      if (!q || !q.week) {
        quotaRow.style.display = 'none';
      } else {
        quotaRow.style.display = '';
        const w = q.week;
        const pctEl = card.querySelector('#ku-quota-pct');
        const overPace = (w.pace != null && w.pace > 1.3);
        if (pctEl) {
          pctEl.textContent = (w.pct != null ? w.pct : '--') + '%';
          pctEl.style.color = overPace ? 'var(--color-danger, #f85149)' : '';
          pctEl.title = overPace
            ? `配速过快，预计 ${w.eta || '--'} 耗尽`
            : `本周额度 ${w.used}/${w.limit}，重置 ${w.reset || '--'}${w.pace != null ? ' · 配速 ' + w.pace : ''}`;
        }
      }
    }

    // 本会话行（usage.session 为 null 时隐藏）
    const sessRow = card.querySelector('#ku-sess-row');
    if (sessRow) {
      const s = state.session;
      if (!s) {
        sessRow.style.display = 'none';
      } else {
        sessRow.style.display = '';
        const keyEl = card.querySelector('#ku-sess-key');
        const sKey = String(s.key || '');
        if (keyEl) {
          const shortKey = sKey.replace(/^session_/, '').slice(0, 8) || '--';
          if (keyEl.textContent !== shortKey) keyEl.textContent = shortKey;
          keyEl.title = `会话 ${sKey} · ${s.cost_fmt || ''}`;
        }
        const sTok = card.querySelector('#ku-sess-tokens');
        if (sTok) sTok.textContent = s.tokens_fmt || '--';
        const sCache = card.querySelector('#ku-sess-cache');
        if (sCache) sCache.textContent = kupCacheTxt(s);
        const sCost = card.querySelector('#ku-sess-cost');
        if (sCost) sCost.textContent = s.cost_fmt || '--';
        // 只在会话 key 上滑入弹详情
        kuBindHoverPop(keyEl, function() {
          return kuPopRow('会话 ID', sKey || '--') +
            kuPopRow('Token', s.tokens_fmt || '--') +
            kuPopRow('调用次数', (s.calls != null ? s.calls : '--') + ' 次') +
            kuPopRow('缓存率', kupCacheTxt(s)) +
            kuPopRow('估算成本', s.cost_fmt || '--') +
            (s.last ? kuPopRow('最近活动', s.last) : '');
        }, '会话详情');
      }
    }

    // 今日调用
    const metaCalls = card.querySelector('#ku-meta-calls');
    if (metaCalls) {
      metaCalls.textContent = `今日调用 ${state.today.calls || 0} 次 · 入 ${state.today.in_fmt || '0'} / 出 ${state.today.out_fmt || '0'}`;
    }

    // 近7天 Sparkline（days30 末 7 天）
    const sparkWrap = card.querySelector('#ku-sparkline-bars');
    const sparkStart = card.querySelector('#ku-sparkline-start');
    const sparkEnd = card.querySelector('#ku-sparkline-end');
    const days = (state.days30 || []).slice(-7);
    if (sparkWrap && days.length > 0) {
      sparkStart.textContent = days[0].label;
      sparkEnd.textContent = days[days.length - 1].label;
      const maxTok = Math.max.apply(null, days.map(d => d.tokens || 0).concat([1]));
      const sparkHtml = days.map((d, idx) => {
        const h = Math.max(2, Math.round(((d.tokens || 0) / maxTok) * 14));
        const isLast = (idx === days.length - 1);
        return `<div class="ku-sparkline-bar ${isLast ? 'active' : ''}" style="height:${h}px" title="${d.label}: ${(d.tokens || 0).toLocaleString()} tokens"></div>`;
      }).join('');
      if (sparkWrap._html !== sparkHtml) { sparkWrap.innerHTML = sparkHtml; sparkWrap._html = sparkHtml; }
    }

    // 模型列表 (今日 vs 累计)
    renderModelsList(card);

    // Footer
    const ft = card.querySelector('#ku-footer');
    if (ft && state.footer) {
      ft.textContent = `wire.jsonl · ${state.footer.file_count || 0} 文件 · ${state.footer.time || '--'}`;
    }

    applyServiceFlag();
  }

  function renderModelsList(card) {
    const listEl = card.querySelector('#ku-models-list');
    if (!listEl) return;
    const models = (activeTab === 'today' ? state.today_models : state.cumul_models) || [];
    const top5 = models.slice(0, 5);
    if (!top5.length) {
      if (listEl.getAttribute('data-mode') !== 'empty') {
        listEl.innerHTML = '<div style="color:var(--color-text-secondary,#8a97ab);font-size:10px;padding:4px 0;">暂无模型调用数据</div>';
        listEl.setAttribute('data-mode', 'empty');
      }
      return;
    }
    var items = listEl.querySelectorAll('.ku-model-item');
    if (listEl.getAttribute('data-mode') === 'list' && items.length === top5.length) {
      // 行数不变 → 就地更新文本，不重建 DOM，避免跳动和 hover 浮层丢失
      items.forEach(function(item, i) {
        var m = top5[i];
        var full = m.model || m.raw_name || m.name || '';
        item.title = full;
        item.setAttribute('data-i', i);
        var nameEl = item.querySelector('.ku-m-name');
        var tokEl = item.querySelector('.ku-m-tokens');
        var cacheEl = item.querySelector('.ku-m-cache');
        var costEl = item.querySelector('.ku-m-cost');
        if (nameEl) { var nm = m.name || (full ? full.split('/').pop() : '--'); if (nameEl.textContent !== nm) nameEl.textContent = nm; }
        if (tokEl) { var t = m.tokens_fmt || '--'; if (tokEl.textContent !== t) tokEl.textContent = t; }
        if (cacheEl) { var c = kupCacheTxt(m); if (cacheEl.textContent !== c) cacheEl.textContent = c; }
        if (costEl) { var co = m.cost_fmt || '--'; if (costEl.textContent !== co) costEl.textContent = co; }
      });
      return;
    }
    // 行数变化或首次渲染 → 重建
    listEl.setAttribute('data-mode', 'list');
    listEl.innerHTML = top5.map(function(m, i) {
      const full = m.model || m.raw_name || m.name || '';
      const name = m.name || (full ? full.split('/').pop() : '--');
      return `<div class="ku-model-item" data-i="${i}" title="${esc(full)}">
        <span class="ku-m-name">${esc(name)}</span>
        <span class="ku-m-tokens">${esc(m.tokens_fmt)}</span>
        <span class="ku-m-cache">${kupCacheTxt(m)}</span>
        <span class="ku-m-cost">${esc(m.cost_fmt)}</span>
      </div>`;
    }).join('');
    // 只在模型名称上滑入弹详情，滑出即关（闭包实时读列表，就地更新后仍正确）
    listEl.querySelectorAll('.ku-model-item').forEach(function(item) {
      var nameEl = item.querySelector('.ku-m-name');
      kuBindHoverPop(nameEl, function() {
        var list = (activeTab === 'today' ? state.today_models : state.cumul_models) || [];
        var m = list[Number(item.getAttribute('data-i') || 0)] || {};
        return kuPopRow('模型', m.model || m.raw_name || m.name || '--') +
          kuPopRow('Token', m.tokens_fmt || '--') +
          kuPopRow('调用次数', (m.calls != null ? m.calls : '--') + ' 次') +
          kuPopRow('缓存率', kupCacheTxt(m)) +
          kuPopRow('估算成本', m.cost_fmt || '--');
      }, '模型调用明细');
    });
  }

  function createCard() {
    const el = document.createElement('div');
    el.id = CARD_ID;
    el.className = 'ku-card';
    if (isCollapsed) el.classList.add('is-collapsed');

    el.innerHTML = `
      <div class="ku-expanded-header">
        <div class="ku-title-group" id="ku-title-group" title="点击打开 Kimi Code 用量面板">
          <span class="ku-dot">•</span>
          <span class="ku-title-text">Kimi 用量</span>
        </div>
        <div class="ku-header-actions">
          <button class="ku-btn-panel" id="ku-remote-btn" title="手机远程连接（扫码 / 复制链接）">手机</button>
          <button class="ku-btn-panel" id="ku-gear-btn" title="模型与能力配置管理器">⚙</button>
          <button class="ku-btn-panel" id="ku-panel-btn" title="打开全屏用量仪表盘">
            <span>📊</span>
            <span>面板</span>
          </button>
          <button class="ku-btn-collapse" id="ku-collapse-btn" title="收起 / 最小化">-</button>
        </div>
      </div>

      <div class="ku-summary-strip">
        <span class="ku-strip-pill ku-strip-tokens">今日 <b id="ku-strip-today">${state.today.tokens_fmt || '--'}</b></span>
        <span class="ku-strip-pill ku-strip-cache">缓存 <b id="ku-strip-cache">${(state.today.cache_pct || state.cache.pct || 0)}%</b></span>
        <span class="ku-strip-pill ku-strip-cost"><b id="ku-strip-cost">${state.today.cost_fmt || '--'}</b></span>
      </div>

      <div class="ku-expanded-body">
        <div class="ku-mode-row" id="ku-mode-row" style="display:none">
          <div class="ku-mode-top">
            <span class="ku-row-label">模式</span>
            <span class="ku-mode-name" id="ku-mode-name">--</span>
            <button class="ku-mode-lock" id="ku-mode-lock" title="锁定当前档：发送框模型被固定为本档主模型，后端守护自动改回">锁定</button>
            <button class="ku-mode-edit" id="ku-mode-edit" title="自定义各档主模型与挂件">编辑</button>
          </div>
          <div class="ku-mode-scope" id="ku-mode-scope" title="全局：改所有对话；本对话：只改当前这一个对话">
            <button type="button" data-scope="global">全局</button>
            <button type="button" data-scope="session">本对话</button>
          </div>
          <input type="range" class="ku-mode-range" id="ku-mode-range" min="0" max="3" step="0.02" value="0" title="拖动切换干活模式，松手生效">
          <div class="ku-mode-ticks" id="ku-mode-ticks"></div>
          <div class="ku-mode-desc" id="ku-mode-desc">加载中…</div>
        </div>
        <div class="ku-row">
          <span class="ku-row-label">今日</span>
          <span class="ku-row-val" id="ku-today-val">--</span>
          <span class="ku-row-cost" id="ku-today-cost">--</span>
        </div>
        <div class="ku-row">
          <span class="ku-row-label">累计</span>
          <span class="ku-row-val" id="ku-cumul-val">--</span>
          <span class="ku-row-cost" id="ku-cumul-cost">--</span>
        </div>
        <div class="ku-row">
          <span class="ku-row-label">速率</span>
          <span class="ku-row-val" id="ku-rate-val">--</span>
          <span class="ku-row-cost" id="ku-rate-cost">--</span>
        </div>

        <div class="ku-row" id="ku-cur-row" title="当前会话使用的模型，滑入名称查看详情">
          <span class="ku-row-label">模型</span>
          <span class="ku-row-model" id="ku-cur-model">--</span>
          <span class="ku-cur-meta" id="ku-cur-meta"></span>
        </div>

        <div class="ku-row" id="ku-quota-row" style="display:none">
          <span class="ku-row-label">配额</span>
          <span class="ku-row-val" id="ku-quota-pct">--</span>
          <span class="ku-row-cost">周配额</span>
        </div>

        <div class="ku-row" id="ku-sess-row" style="display:none" title="当前会话用量，滑入查看详情">
          <span class="ku-row-label">会话</span>
          <span class="ku-row-cost" id="ku-sess-key">--</span>
          <span class="ku-row-val" id="ku-sess-tokens">--</span>
          <span class="ku-row-cost" id="ku-sess-cache">--</span>
          <span class="ku-row-cost" id="ku-sess-cost">--</span>
        </div>

        <div class="ku-row ku-row-cache">
          <span class="ku-row-label ku-cache-label">缓存</span>
          <div class="ku-cache-bar-wrap">
            <div class="ku-cache-bar-fill" id="ku-cache-bar" style="width: 0%;"></div>
          </div>
          <span class="ku-cache-pct" id="ku-cache-pct">--</span>
        </div>

        <div class="ku-row">
          <span class="ku-row-label">速度</span>
          <span class="ku-row-val" id="ku-speed-tps">--</span>
          <span class="ku-row-model" id="ku-speed-model">--</span>
        </div>

        <div class="ku-meta-calls" id="ku-meta-calls">
          今日调用 -- 次 · 入 -- / 出 --
        </div>

        <div class="ku-sparkline-row">
          <span class="ku-sparkline-label">近7天</span>
          <span class="ku-sparkline-date" id="ku-sparkline-start">--</span>
          <div class="ku-sparkline-bars" id="ku-sparkline-bars"></div>
          <span class="ku-sparkline-date" id="ku-sparkline-end">--</span>
        </div>

        <div class="ku-divider"></div>

        <div class="ku-models-tabs">
          <button class="ku-tab-btn active" id="ku-tab-today">[今日]</button>
          <button class="ku-tab-btn" id="ku-tab-cumul">[累计]</button>
        </div>
        <div class="ku-models-list" id="ku-models-list"></div>
        <div class="ku-footer" id="ku-footer">wire.jsonl · 0 文件 · --</div>
      </div>

      <div class="ku-minimized-bar" id="ku-min-bar" title="点击展开用量卡片">
        <div class="ku-min-top">
          <div class="ku-min-left">
            <span class="ku-dot">•</span>
            <span>Kimi Code 用量</span>
          </div>
          <div class="ku-min-center">
            <span class="ku-min-pill" id="ku-min-tokens">${state.today.tokens_fmt || '--'}</span>
            <span class="ku-min-pill ku-min-cache" id="ku-min-cache">${(state.today.cache_pct || state.cache.pct || 0)}%</span>
          </div>
          <div class="ku-min-actions">
            <button class="ku-min-action-btn" id="ku-min-remote-btn" title="手机远程连接">手机</button>
            <button class="ku-min-action-btn" id="ku-min-gear-btn" title="模型管理">⚙</button>
            <button class="ku-min-action-btn" id="ku-min-panel-btn" title="查看全尺寸面板">📊</button>
            <button class="ku-min-action-btn ku-min-btn-plus" id="ku-min-expand-btn" title="展开">+</button>
          </div>
        </div>
        <div class="ku-min-model" id="ku-min-model">${kuMinModelHtml() || kuLastMinHtml || '<span class="ku-min-ph">--</span>'}</div>
      </div>
    `;

    el.querySelector('#ku-collapse-btn').onclick = (e) => {
      e.stopPropagation();
      isCollapsed = true;
      el.classList.add('is-collapsed');
      try { localStorage.setItem(STORAGE_COLLAPSED_KEY, 'true'); } catch (err) {}
    };

    function expandCard() {
      isCollapsed = false;
      el.classList.remove('is-collapsed');
      try { localStorage.setItem(STORAGE_COLLAPSED_KEY, 'false'); } catch (err) {}
    }
    el.querySelector('#ku-min-bar').onclick = (e) => {
      if (!e.target.closest('#ku-min-panel-btn') && !e.target.closest('#ku-min-gear-btn')
          && !e.target.closest('#ku-min-remote-btn')) {
        expandCard();
      }
    };
    el.querySelector('#ku-min-expand-btn').onclick = (e) => {
      e.stopPropagation();
      expandCard();
    };

    el.querySelector('#ku-panel-btn').onclick = (e) => { e.stopPropagation(); openPanel(); };
    el.querySelector('#ku-title-group').onclick = (e) => { e.stopPropagation(); openPanel(); };
    el.querySelector('#ku-min-panel-btn').onclick = (e) => { e.stopPropagation(); openPanel(); };
    el.querySelector('#ku-gear-btn').onclick = (e) => { e.stopPropagation(); openModelModal(); };
    el.querySelector('#ku-min-gear-btn').onclick = (e) => { e.stopPropagation(); openModelModal(); };
    function openRemoteOverlay() {
      var RW = window.KimiRemoteWidget;
      if (RW && typeof RW.open === 'function') { RW.open(); return; }
      kmmToast('远程连接模块未加载（缺少 kimi-remote-widget.js）', true);
    }
    kmdBindCard(el);
    el.querySelector('#ku-remote-btn').onclick = (e) => { e.stopPropagation(); openRemoteOverlay(); };
    el.querySelector('#ku-min-remote-btn').onclick = (e) => { e.stopPropagation(); openRemoteOverlay(); };

    const tabToday = el.querySelector('#ku-tab-today');
    const tabCumul = el.querySelector('#ku-tab-cumul');
    tabToday.onclick = (e) => {
      e.stopPropagation();
      activeTab = 'today';
      tabToday.classList.add('active');
      tabCumul.classList.remove('active');
      renderModelsList(el);
    };
    tabCumul.onclick = (e) => {
      e.stopPropagation();
      activeTab = 'cumul';
      tabCumul.classList.add('active');
      tabToday.classList.remove('active');
      renderModelsList(el);
    };

    return el;
  }

  // 挂载组件到侧栏正上方
  function attachWidget() {
    if (document.getElementById(CARD_ID)) return;
    const footer = document.querySelector('.side-footer') || document.querySelector('[class*="side-footer"]');
    if (footer && footer.parentNode) {
      const widget = createCard();
      footer.parentNode.insertBefore(widget, footer);
      kuLastSig = '';
      updateDOM();
    }
  }

  /* ================================================================
   * 全屏报表（移植自 kimi-usage-panel-src.html，kup- 前缀，无 iframe）
   * ================================================================ */
  function kup$(id) { return document.getElementById(id); }
  function kupFmt(n) { return (n || 0).toLocaleString('zh-CN'); }
  function kupFmtK(n) {
    n = n || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
  }
  function kupNiceMax(v) {
    if (v <= 0) return 10;
    var exp = Math.pow(10, Math.floor(Math.log10(v)));
    var f = v / exp;
    var nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return nf * exp;
  }
  function kupRank(pct) {
    if (pct == null || isNaN(pct)) return '';
    if (pct >= 70) return 'kup-tag';
    if (pct >= 40) return 'kup-tag warn';
    return 'kup-tag low';
  }
  // 上游不回传缓存字段（如反代渠道）时显示 —，而不是误报 0%
  function kupCacheTxt(m) {
    if (m && m.cache_reported === false) return '—';
    return (m && m.cache_pct != null) ? m.cache_pct + '%' : '--';
  }
  function kupShowErr(msg) {
    var b = kup$('kup-err');
    if (b) { b.style.display = 'block'; b.textContent = '加载用量数据失败：' + msg; }
  }

  function kupRenderCoreKPIs() {
    var t = state.today || {};
    var y = state.yesterday || {};
    var w = state.week || {};
    var m = state.month || {};
    var el = kup$('kup-core-kpis');
    if (!el) return;
    var cards = [
      {
        cls: 'c-today', label: '今日目前',
        date: t.date || new Date().toISOString().slice(0, 10),
        tokens: t.tokens_fmt || '0', calls: t.calls || 0,
        in: t.in_fmt || '0', out: t.out_fmt || '0',
        sub: '成本 ' + (t.cost_fmt || '¥0') + ' · 缓存 ' + (t.cache_pct || 0) + '%'
      },
      {
        cls: 'c-yesterday', label: '昨日汇总', date: y.date || '--',
        tokens: y.tokens_fmt || '0', calls: y.calls || 0,
        in: y.in_fmt || '0', out: y.out_fmt || '0',
        sub: '成本 ' + (y.cost_fmt || '¥0') + ' · 缓存 ' + (y.cache_pct || 0) + '%'
      },
      {
        cls: 'c-week', label: '本周累计', date: w.date || '--',
        tokens: w.tokens_fmt || '0', calls: w.calls || 0,
        in: w.in_fmt || '0', out: w.out_fmt || '0',
        sub: '成本 ' + (w.cost_fmt || '¥0') + ' · 缓存 ' + (w.cache_pct || 0) + '%'
      },
      {
        cls: 'c-month', label: '本月累计', date: m.date || '--',
        tokens: m.tokens_fmt || '0', calls: m.calls || 0,
        in: m.in_fmt || '0', out: m.out_fmt || '0',
        sub: '成本 ' + (m.cost_fmt || '¥0') + ' · 缓存 ' + (m.cache_pct || 0) + '%'
      }
    ];
    kupSetHtml(el, cards.map(function(c) {
      return '<div class="kup-kpi-card ' + c.cls + '">' +
        '<div class="kup-kpi-head">' +
          '<span class="kup-kpi-title">' + c.label + '</span>' +
          '<span class="kup-kpi-date">' + esc(c.date) + '</span>' +
        '</div>' +
        '<div class="kup-kpi-tokens">' + c.tokens + ' <small>tokens</small></div>' +
        '<div class="kup-kpi-meta-grid">' +
          '<div class="kup-kpi-meta-item"><span>成功调用</span><b>' + kupFmt(c.calls) + '</b></div>' +
          '<div class="kup-kpi-meta-item"><span>输入</span><b>' + esc(c.in) + '</b></div>' +
          '<div class="kup-kpi-meta-item"><span>输出</span><b>' + esc(c.out) + '</b></div>' +
        '</div>' +
        '<div class="kup-kpi-sub-text"><span>' + esc(c.sub) + '</span></div>' +
      '</div>';
    }).join(''));
  }

  function kupSetHtml(el, html) {
    if (el._html === html && el.innerHTML) return;
    el.innerHTML = html;
    el._html = html;
  }

  function kupRenderSubMetrics() {
    var el = kup$('kup-sub-metrics');
    if (!el) return;
    var ca = state.cache || {}, r = state.rate || {}, sp = state.speed || {}, c = state.cumul || {};
    var pills = [
      { label: '缓存命中率', val: (ca.pct_fmt || '--'), sub: '缓存读取占总输入比' },
      { label: '今日消耗速率', val: (r.tokens_per_hour || '--'), sub: (r.cost_per_hour || '--') + (r.window ? ' · ' + r.window : '') },
      { label: '当前吞吐速度', val: (sp.tps || '--'), sub: '活跃模型 ' + shortName(sp.model) },
      { label: '全量累计总量', val: (c.tokens_fmt || '--'), sub: '成本 ' + (c.cost_fmt || '--') + ' · ' + kupFmt(c.calls) + ' 次调用' }
    ];
    if (state.quota && state.quota.week) {
      pills.push({
        label: '周配额已用',
        val: (state.quota.week.pct != null ? state.quota.week.pct : '--') + '%',
        sub: '重置 ' + (state.quota.week.reset || '--')
      });
    }
    if (state.quota && state.quota.h5) {
      pills.push({
        label: '5小时配额已用',
        val: (state.quota.h5.pct != null ? state.quota.h5.pct : '--') + '%',
        sub: '重置 ' + (state.quota.h5.reset || '--')
      });
    }
    kupSetHtml(el, pills.map(function(p) {
      return '<div class="kup-sub-pill">' +
        '<div class="kup-sub-pill-left"><span>' + p.label + '</span><b>' + esc(p.val) + '</b></div>' +
        '<div class="kup-sub-pill-right">' + esc(p.sub) + '</div>' +
      '</div>';
    }).join(''));
  }

  function kupSelectWavePeriod(p) {
    if (!p) return;
    kupWavePeriod = p;
    kupDrawWaveChart();
  }

  function kupDrawWaveChart() {
    var modal = document.getElementById(PANEL_MODAL_ID);
    if (!modal || !modal.classList.contains('active')) return;

    modal.querySelectorAll('#kup-wave-tabs .kup-tab').forEach(function(t) {
      t.classList.toggle('active', t.getAttribute('data-period') === kupWavePeriod);
    });

    var tsData = state.timeseries ? state.timeseries[kupWavePeriod] : null;
    var pts = (tsData && tsData.points && tsData.points.length > 0) ? tsData.points : null;
    if (!pts) {
      if (kupWavePeriod === 'today') {
        pts = (state.hourly || []).map(function(h) {
          return { label: ('0' + h.hour).slice(-2), tokens: h.tokens, calls: h.calls };
        });
      } else if (kupWavePeriod === 'month') {
        pts = (state.days30 || []).map(function(d) {
          return { label: d.label, tokens: d.tokens, calls: d.calls };
        });
      } else {
        pts = (state.days30 || []).slice(-7).map(function(d) {
          return { label: d.label, tokens: d.tokens, calls: d.calls };
        });
      }
    }

    var wrap = kup$('kup-wave-wrap');
    var svg = kup$('kup-wave-chart');
    if (!svg || !wrap) return;

    var subLabels = {
      today: '今日 24 小时逐小时 Token 消耗与调用次数 · 今日总消耗: ' + (state.today ? state.today.tokens_fmt : '--'),
      week: '本周 7 天逐日 Token 消耗与调用次数 · 本周总消耗: ' + (state.week ? state.week.tokens_fmt : '--'),
      month: '本月 30 天逐日 Token 消耗与调用次数 · 本月总消耗: ' + (state.month ? state.month.tokens_fmt : '--')
    };
    var waveSub = kup$('kup-wave-sub');
    if (waveSub) waveSub.textContent = subLabels[kupWavePeriod] || '按时间聚合的总 Token 消耗';

    var W = wrap.clientWidth || 920;
    var H = 220;
    var padL = 54, padR = 24, padT = 24, padB = 35;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('width', '100%');
    svg.setAttribute('height', '100%');
    svg.innerHTML = '';

    if (!pts || !pts.length) {
      svg.innerHTML = '<text x="' + (W / 2) + '" y="' + (H / 2) + '" text-anchor="middle" style="fill:var(--kup-faint)" font-size="12">暂无时序数据</text>';
      return;
    }

    var maxTokens = 0, maxCalls = 0;
    pts.forEach(function(p) {
      if ((p.tokens || 0) > maxTokens) maxTokens = p.tokens;
      if ((p.calls || 0) > maxCalls) maxCalls = p.calls;
    });
    var yMax = kupNiceMax(maxTokens);
    var iw = (W - padL - padR) / Math.max(pts.length - 1, 1);
    var bw = Math.min(Math.max(6, iw * 0.52), 40);

    function X(i) { return padL + i * iw; }
    function Y(v) {
      var rawY = padT + (H - padT - padB) * (1 - (v || 0) / yMax);
      return Math.max(padT, Math.min(H - padB, rawY));
    }

    // 刻度线与刻度值
    var ticks = 4;
    for (var i = 0; i <= ticks; i++) {
      var val = yMax * i / ticks;
      var y = padT + (H - padT - padB) * (1 - i / ticks);
      var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', padL); line.setAttribute('y1', y);
      line.setAttribute('x2', W - padR); line.setAttribute('y2', y);
      line.style.stroke = 'var(--kup-track)';
      line.setAttribute('stroke-dasharray', '4 5');
      svg.appendChild(line);
      var tx = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      tx.setAttribute('x', padL - 8); tx.setAttribute('y', y + 4);
      tx.setAttribute('text-anchor', 'end'); tx.style.fill = 'var(--kup-faint)';
      tx.setAttribute('font-size', '10.5'); tx.setAttribute('font-variant-numeric', 'tabular-nums');
      tx.textContent = kupFmtK(val);
      svg.appendChild(tx);
    }

    // X 轴标签
    var step = pts.length > 14 ? Math.ceil(pts.length / 8) : 1;
    pts.forEach(function(p, i) {
      if (i % step !== 0 && i !== pts.length - 1) return;
      var tx = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      tx.setAttribute('x', X(i)); tx.setAttribute('y', H - padB + 20);
      tx.setAttribute('text-anchor', 'middle'); tx.style.fill = 'var(--kup-faint)';
      tx.setAttribute('font-size', '10.5'); tx.setAttribute('font-variant-numeric', 'tabular-nums');
      tx.textContent = p.label;
      svg.appendChild(tx);
    });

    // 调用次数浅蓝柱形（开方缩放）
    if (maxCalls > 0) {
      var barH = (H - padT - padB) * 0.40;
      pts.forEach(function(p, i) {
        var calls = p.calls || 0;
        if (calls > 0) {
          var bh = Math.max(4, Math.pow(calls / maxCalls, 0.62) * barH);
          var rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
          rect.setAttribute('x', X(i) - bw / 2);
          rect.setAttribute('y', H - padB - bh);
          rect.setAttribute('width', bw);
          rect.setAttribute('height', bh);
          rect.setAttribute('rx', '2.5');
          rect.style.fill = 'var(--kup-cyan)';
          rect.setAttribute('fill-opacity', '0.35');
          svg.appendChild(rect);
        }
      });
    }

    // 平滑三次样条曲线
    function getBezierCurve(points) {
      if (points.length <= 1) return 'M ' + X(0) + ' ' + Y(points[0].tokens);
      var d = 'M ' + X(0) + ' ' + Y(points[0].tokens);
      for (var i = 0; i < points.length - 1; i++) {
        var p0 = i > 0 ? points[i - 1] : points[i];
        var p1 = points[i];
        var p2 = points[i + 1];
        var p3 = i < points.length - 2 ? points[i + 2] : p2;
        var cp1x = X(i) + (X(i + 1) - X(i > 0 ? i - 1 : i)) / 5.5;
        var cp1y = Y(p1.tokens) + (Y(p2.tokens) - Y(p0.tokens)) / 5.5;
        var cp2x = X(i + 1) - (X(i < points.length - 2 ? i + 2 : i + 1) - X(i)) / 5.5;
        var cp2y = Y(p2.tokens) - (Y(p3.tokens) - Y(p1.tokens)) / 5.5;
        cp1y = Math.max(padT, Math.min(H - padB, cp1y));
        cp2y = Math.max(padT, Math.min(H - padB, cp2y));
        d += ' C ' + cp1x + ' ' + cp1y + ', ' + cp2x + ' ' + cp2y + ', ' + X(i + 1) + ' ' + Y(p2.tokens);
      }
      return d;
    }

    var curveD = getBezierCurve(pts);
    var areaD = curveD + ' L ' + X(pts.length - 1) + ' ' + (H - padB) + ' L ' + X(0) + ' ' + (H - padB) + ' Z';

    var defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    defs.innerHTML = '<linearGradient id="kup-waveGrad" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="#1a88ff" stop-opacity="0.38"/>' +
      '<stop offset="100%" stop-color="#1a88ff" stop-opacity="0.0"/>' +
      '</linearGradient>';
    svg.appendChild(defs);

    var areaPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    areaPath.setAttribute('d', areaD);
    areaPath.setAttribute('fill', 'url(#kup-waveGrad)');
    svg.appendChild(areaPath);

    var linePath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    linePath.setAttribute('d', curveD);
    linePath.setAttribute('fill', 'none');
    linePath.style.stroke = 'var(--kup-accent2)';
    linePath.setAttribute('stroke-width', '2.6');
    linePath.setAttribute('stroke-linecap', 'round');
    linePath.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(linePath);

    // 数据点白底圆点
    pts.forEach(function(p, i) {
      var dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      dot.setAttribute('cx', X(i));
      dot.setAttribute('cy', Y(p.tokens));
      dot.setAttribute('r', '4');
      dot.style.fill = 'var(--kup-card)';
      dot.style.stroke = 'var(--kup-accent2)';
      dot.setAttribute('stroke-width', '2');
      svg.appendChild(dot);
    });

    // 悬停交互区
    var tip = kup$('kup-wave-tip');
    pts.forEach(function(p, i) {
      var hit = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
      hit.setAttribute('x', X(i) - iw / 2);
      hit.setAttribute('y', padT);
      hit.setAttribute('width', Math.max(12, iw));
      hit.setAttribute('height', H - padT - padB);
      hit.setAttribute('fill', 'transparent');
      hit.style.cursor = 'pointer';
      hit.addEventListener('mouseenter', function() {
        if (!tip) return;
        tip.style.display = 'block';
        tip.innerHTML =
          '<div class="kup-wt-title">' + esc(p.label) + '</div>' +
          '<div class="kup-wt-row"><span class="kup-wt-label">总 Token:</span><b class="kup-wt-val">' + kupFmt(p.tokens) + '</b></div>' +
          '<div class="kup-wt-row"><span class="kup-wt-label">调用次数:</span><b class="kup-wt-val">' + kupFmt(p.calls) + ' 次</b></div>';
        var tipW = tip.offsetWidth || 150;
        var lx = X(i) + 12;
        if (lx + tipW > W) lx = X(i) - tipW - 12;
        tip.style.left = Math.max(8, lx) + 'px';
        tip.style.top = Math.max(10, Y(p.tokens) - 45) + 'px';
      });
      hit.addEventListener('mouseleave', function() {
        if (tip) tip.style.display = 'none';
      });
      svg.appendChild(hit);
    });
  }

  function kupSelectModelTab(m) {
    if (!m) return;
    kupModelTab = m;
    kupRenderModels();
  }

  function kupRenderModels() {
    var modal = document.getElementById(PANEL_MODAL_ID);
    if (!modal) return;
    modal.querySelectorAll('#kup-model-tabs .kup-tab').forEach(function(t) {
      t.classList.toggle('active', t.getAttribute('data-m') === kupModelTab);
    });
    var rows = [];
    if (kupModelTab === 'today') rows = (state.today && state.today.models && state.today.models.length)
      ? state.today.models : (state.today_models || []);
    else if (kupModelTab === 'yesterday') rows = (state.yesterday && state.yesterday.models) || [];
    else if (kupModelTab === 'week') rows = (state.week && state.week.models) || [];
    else rows = state.cumul_models || [];

    var tabLabels = { today: '今日各模型消耗', yesterday: '昨日各模型消耗', week: '本周各模型消耗', cumul: '全量累计各模型消耗' };
    var sub = kup$('kup-models-sub');
    if (sub) sub.textContent = tabLabels[kupModelTab] || '模型调用统计';
    var table = kup$('kup-models-table');
    if (!table) return;
    if (!rows || !rows.length) {
      if (table.getAttribute('data-mode') !== 'empty') {
        table.innerHTML = '<tbody><tr><td class="kup-empty">该时段暂无模型调用数据</td></tr></tbody>';
        table.setAttribute('data-mode', 'empty');
      }
      return;
    }
    var maxT = 1;
    rows.forEach(function(m) { if ((m.tokens || 0) > maxT) maxT = m.tokens; });
    var bodyRows = rows.map(function(m) {
      var pct = maxT > 0 ? ((m.tokens || 0) / maxT * 100) : 0;
      var full = m.model || m.raw_name || m.name || '';
      var name = m.name || (full ? full.split('/').pop() : '--');
      var cp = (m.cache_reported === false) ? null
        : ((m.cache_pct != null) ? m.cache_pct : null);
      return {
        name: name, full: full, pct: pct, calls: kupFmt(m.calls),
        in: m.in_fmt || m.input_fmt || (m.input != null ? kupFmtK(m.input) : '--'),
        out: m.out_fmt || m.output_fmt || (m.output != null ? kupFmtK(m.output) : '--'),
        cpCls: kupRank(cp), cpTxt: kupCacheTxt(m),
        tokens: m.tokens_fmt || kupFmt(m.tokens)
      };
    });
    var tbody = table.querySelector('tbody');
    var trs = tbody ? tbody.querySelectorAll('tr') : [];
    var rebuild = !tbody || table.getAttribute('data-mode') !== 'list' || trs.length !== bodyRows.length;
    if (rebuild) {
      var head = '<thead><tr><th>模型</th><th>占比</th><th>调用</th><th>输入 Token</th><th>输出 Token</th><th>缓存率</th><th>总 Token</th></tr></thead>';
      var body = '<tbody>' + bodyRows.map(function(r) {
        return '<tr>' +
          '<td><div class="kup-model-cell"><span class="kup-model-name" title="' + esc(r.full) + '">' + esc(r.name) + '</span></div></td>' +
          '<td><div class="kup-model-cell"><span class="bar"><i style="width:' + r.pct.toFixed(1) + '%"></i></span><span class="pct">' + r.pct.toFixed(1) + '%</span></div></td>' +
          '<td class="kup-mono">' + r.calls + '</td>' +
          '<td class="kup-mono">' + esc(r.in) + '</td>' +
          '<td class="kup-mono">' + esc(r.out) + '</td>' +
          '<td><span class="' + r.cpCls + '">' + r.cpTxt + '</span></td>' +
          '<td class="kup-mono" style="font-weight:700;color:var(--kup-accent2)">' + esc(r.tokens) + '</td>' +
        '</tr>';
      }).join('') + '</tbody>';
      table.innerHTML = head + body;
      table.setAttribute('data-mode', 'list');
      return;
    }
    // 就地更新
    trs.forEach(function(tr, i) {
      var r = bodyRows[i];
      var tds = tr.querySelectorAll('td');
      if (tds.length < 7) return;
      var nameEl = tds[0].querySelector('.kup-model-name');
      if (nameEl && nameEl.textContent !== r.name) { nameEl.textContent = r.name; nameEl.title = r.full; }
      var barI = tds[1].querySelector('.bar i');
      var pctEl = tds[1].querySelector('.pct');
      var pctTxt = r.pct.toFixed(1) + '%';
      if (barI) barI.style.width = pctTxt;
      if (pctEl && pctEl.textContent !== pctTxt) pctEl.textContent = pctTxt;
      if (tds[2].textContent !== r.calls) tds[2].textContent = r.calls;
      if (tds[3].textContent !== r.in) tds[3].textContent = r.in;
      if (tds[4].textContent !== r.out) tds[4].textContent = r.out;
      var sp = tds[5].querySelector('span');
      if (sp && (sp.textContent !== r.cpTxt || sp.className !== r.cpCls)) { sp.textContent = r.cpTxt; sp.className = r.cpCls; }
      if (tds[6].textContent !== r.tokens) tds[6].textContent = r.tokens;
    });
  }

  function kupRenderSessions() {
    var table = kup$('kup-sessions-table');
    if (!table) return;
    var rows = state.sessions || [];
    if (!rows.length) {
      if (table.getAttribute('data-mode') !== 'empty') {
        table.innerHTML = '<tbody><tr><td class="kup-empty">暂无会话数据</td></tr></tbody>';
        table.setAttribute('data-mode', 'empty');
      }
      return;
    }
    // 表头只建一次；行数不变就地更新 td 文本，避免重建导致列宽/滚动位置抖动
    var bodyRows = rows.map(function(s) {
      var cp = (s.cache_reported === false) ? null : parseInt(s.cache_pct, 10);
      var key = s.key || s.id || '';
      var short = s.short || s.short_id || (key.length > 8 ? key.slice(0, 8) : key);
      return {
        key: key, short: short, tokens: s.tokens_fmt || '--', calls: kupFmt(s.calls),
        cp: cp, cpCls: kupRank(cp), cpTxt: kupCacheTxt(s),
        cost: s.cost_fmt || '--', last: s.last || '--'
      };
    });
    var tbody = table.querySelector('tbody');
    var trs = tbody ? tbody.querySelectorAll('tr') : [];
    var rebuild = !tbody || table.getAttribute('data-mode') !== 'list' || trs.length !== bodyRows.length;
    if (rebuild) {
      var head = '<thead><tr><th>会话</th><th>Token</th><th>调用</th><th>缓存率</th><th>估算成本</th><th>最近活动</th></tr></thead>';
      var body = '<tbody>' + bodyRows.map(function(r) {
        return '<tr>' +
          '<td class="kup-mono" title="' + esc(r.key) + '">' + esc(r.short) + '</td>' +
          '<td class="kup-mono" style="font-weight:600">' + esc(r.tokens) + '</td>' +
          '<td class="kup-mono">' + r.calls + '</td>' +
          '<td><span class="' + r.cpCls + '">' + r.cpTxt + '</span></td>' +
          '<td class="kup-mono">' + esc(r.cost) + '</td>' +
          '<td class="kup-mono" style="color:var(--kup-muted)">' + esc(r.last) + '</td>' +
        '</tr>';
      }).join('') + '</tbody>';
      table.innerHTML = head + body;
      table.setAttribute('data-mode', 'list');
      return;
    }
    // 就地更新
    trs.forEach(function(tr, i) {
      var r = bodyRows[i];
      var tds = tr.querySelectorAll('td');
      if (tds.length < 6) return;
      if (tds[0].textContent !== r.short) { tds[0].textContent = r.short; tds[0].title = r.key; }
      if (tds[1].textContent !== r.tokens) tds[1].textContent = r.tokens;
      if (tds[2].textContent !== r.calls) tds[2].textContent = r.calls;
      var sp = tds[3].querySelector('span');
      if (sp && (sp.textContent !== r.cpTxt || sp.className !== r.cpCls)) { sp.textContent = r.cpTxt; sp.className = r.cpCls; }
      if (tds[4].textContent !== r.cost) tds[4].textContent = r.cost;
      if (tds[5].textContent !== r.last) tds[5].textContent = r.last;
    });
  }

  function kupRenderDaily() {
    var table = kup$('kup-daily-table');
    if (!table) return;
    var rows = (state.days30 || []).slice().reverse();
    if (!rows.length) {
      if (table.getAttribute('data-mode') !== 'empty') {
        table.innerHTML = '<tbody><tr><td class="kup-empty">暂无每日记录</td></tr></tbody>';
        table.setAttribute('data-mode', 'empty');
      }
      return;
    }
    var bodyRows = rows.map(function(d) {
      return {
        date: d.date, tokens: d.tokens_fmt || kupFmt(d.tokens), calls: kupFmt(d.calls),
        cost: d.cost_fmt || ('¥' + (d.cost || 0))
      };
    });
    var tbody = table.querySelector('tbody');
    var trs = tbody ? tbody.querySelectorAll('tr') : [];
    var rebuild = !tbody || table.getAttribute('data-mode') !== 'list' || trs.length !== bodyRows.length;
    if (rebuild) {
      var head = '<thead><tr><th>日期</th><th>总 Token</th><th>调用次数</th><th>估算成本</th></tr></thead>';
      var body = '<tbody>' + bodyRows.map(function(r) {
        return '<tr>' +
          '<td class="kup-mono">' + esc(r.date) + '</td>' +
          '<td class="kup-mono" style="font-weight:600">' + esc(r.tokens) + '</td>' +
          '<td class="kup-mono">' + r.calls + '</td>' +
          '<td class="kup-mono">' + esc(r.cost) + '</td>' +
        '</tr>';
      }).join('') + '</tbody>';
      table.innerHTML = head + body;
      table.setAttribute('data-mode', 'list');
      return;
    }
    trs.forEach(function(tr, i) {
      var r = bodyRows[i];
      var tds = tr.querySelectorAll('td');
      if (tds.length < 4) return;
      if (tds[0].textContent !== r.date) tds[0].textContent = r.date;
      if (tds[1].textContent !== r.tokens) tds[1].textContent = r.tokens;
      if (tds[2].textContent !== r.calls) tds[2].textContent = r.calls;
      if (tds[3].textContent !== r.cost) tds[3].textContent = r.cost;
    });
  }

  function kupRenderAll() {
    var modal = document.getElementById(PANEL_MODAL_ID);
    if (!modal || !modal.classList.contains('active')) return;
    kupRenderCoreKPIs();
    kupRenderSubMetrics();
    kupDrawWaveChart();
    kupRenderModels();
    kupRenderSessions();
    kupRenderDaily();
    var stamp = kup$('kup-stamp');
    if (stamp && state.updated_at) {
      stamp.textContent = '北京时间 ' + state.updated_at + ' · ' +
        ((state.footer && state.footer.file_count) || 0) + ' 个日志文件';
    }
    var dot = modal.querySelector('.kup-head-dot');
    if (dot) {
      dot.classList.toggle('off', !serviceOnline);
      dot.title = serviceTitle();
    }
  }

  function kupApplyTheme(t) {
    kupTheme = (t === 'light') ? 'light' : 'dark';
    var shell = document.querySelector('#' + PANEL_MODAL_ID + ' .kup-shell');
    if (shell) shell.setAttribute('data-theme', kupTheme);
    try { localStorage.setItem('kimi-usage-theme', kupTheme); } catch (e) {}
  }
  function kupInitTheme() {
    var saved = null;
    try { saved = localStorage.getItem('kimi-usage-theme'); } catch (e) {}
    if (saved === 'dark' || saved === 'light') { kupApplyTheme(saved); return; }
    var host = null;
    try { host = document.documentElement.getAttribute('data-color-scheme'); } catch (e) {}
    kupApplyTheme(host === 'light' ? 'light' : 'dark');
  }

  var kupSettings = null;
  var kupSettingsBusy = false;
  var kupSettingsEpoch = 0;
  var kupSettingsPending = null;

  function kupWidgetsEnabled() {
    return kupSettings ? kupSettings.widgets_enabled : !!(kmdData && kmdData.widgets_enabled !== false);
  }

  function kupRenderSettings() {
    var button = document.getElementById('kup-widgets-btn');
    if (!button) return;
    var enabled = kupSettings ? kupSettings.widgets_enabled : true;
    button.disabled = kupSettingsBusy || !kupSettings;
    button.textContent = '挂件：' + (kupSettingsBusy ? '保存中…' : !kupSettings ? '加载中…' : enabled ? '开' : '关');
    button.setAttribute('aria-pressed', String(enabled));
    button.title = enabled ? '点击关闭挂件：桌面与手机隐藏模式栏，并停用挂件提示、调用守卫和锁档；保留已有档位' : '点击开启挂件，恢复模式栏及挂件相关功能';
    button.onclick = kupToggleWidgets;
  }

  function kupApplySettings(data) {
    if (!data || typeof data.widgets_enabled !== 'boolean') throw new Error('挂件设置响应无效');
    var changed = !kupSettings || kupSettings.widgets_enabled !== data.widgets_enabled;
    if (changed) kupSettingsEpoch++;
    kupSettings = { widgets_enabled: data.widgets_enabled };
    kupRenderSettings();
    if (!data.widgets_enabled) {
      kmdDraft = null;
      kmdDragging = false;
      if (kmdSnapRaf) { cancelAnimationFrame(kmdSnapRaf); kmdSnapRaf = 0; }
      if (kmdOverlayEl && kmdOverlayEl.classList.contains('visible')) kuLayerHide(kmdOverlayEl, 'visible');
    }
    kmdRender();
    kmdHostLockWatch();
    kmdHostLockApply();
    return changed;
  }

  function kupFetchSettings() {
    if (kupSettingsBusy || kupSettingsPending) return kupSettingsPending || Promise.resolve(kupSettings);
    var epoch = kupSettingsEpoch;
    kupSettingsPending = kapiFetch(API_BASE + '/api/settings', { cache: 'no-store' })
      .then(function(r) { if (!r.ok) throw new Error('读取挂件设置失败'); return r.json(); })
      .then(function(data) {
        if (epoch !== kupSettingsEpoch) return kupSettings;
        var changed = kupApplySettings(data);
        if (changed && data.widgets_enabled) return kmdFetch(true);
        return data;
      }).catch(function() {
        if (!kupSettings) {
          var button = document.getElementById('kup-widgets-btn');
          if (button) { button.textContent = '挂件：读取失败'; button.title = '读取挂件设置失败，请刷新页面后重试'; }
        }
      }).finally(function() { kupSettingsPending = null; });
    return kupSettingsPending;
  }

  function kupToggleWidgets() {
    if (!kupSettings || kupSettingsBusy) return;
    var enabled = !kupSettings.widgets_enabled;
    kupSettingsBusy = true;
    kupSettingsEpoch++;
    kupRenderSettings();
    kapiFetch(API_BASE + '/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ widgets_enabled: enabled }), cache: 'no-store'
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (!data || !data.success) throw new Error((data && data.message) || '挂件设置保存失败');
      kupApplySettings(data.data);
      kmmToast(data.message || '设置已保存');
      if (enabled) return kmdFetch(true);
    }).catch(function(error) {
      kmmToast(error.message || '保存失败：后台服务未响应', true);
    }).finally(function() {
      kupSettingsBusy = false;
      kupRenderSettings();
      kupFetchSettings();
    });
  }

  function openPanel() {
    var modal = document.getElementById(PANEL_MODAL_ID);
    if (!modal) {
      modal = document.createElement('div');
      modal.id = PANEL_MODAL_ID;
      modal.innerHTML = `
        <div class="kup-card">
          <div class="kup-head">
            <div class="kup-head-title">
              <span class="kup-head-dot" title="服务状态"></span>
              <span>⚡ Kimi Code 用量面板</span>
            </div>
            <div class="kup-head-tools">
              <button class="kup-btn" id="kup-update-btn">更新</button>
              <button class="kup-btn" id="kup-uninstall-btn">卸载</button>
              <button class="kup-btn" id="kup-widgets-btn" aria-pressed="true" disabled>挂件：加载中…</button>
              <button class="kup-btn" id="kup-mute-btn"></button>
              <button class="kup-btn" id="kup-theme-btn">切换主题</button>
              <button class="kup-btn kup-btn-primary" id="kup-refresh-btn">刷新</button>
              <button class="kup-close" id="kup-close">&times;</button>
            </div>
          </div>
          <div class="kup-shell" data-theme="dark">
            <div class="kup-app" id="kup-app">
              <header class="kup-topbar">
                <div class="kup-brand">
                  <div class="kup-brand-mark">⚡</div>
                  <div>
                    <h1>Kimi Code 用量面板</h1>
                    <p>UTC+8 · 本地真实会话与模型调用数据 · 毫秒级实时落盘</p>
                  </div>
                </div>
                <div class="kup-topbar-right">
                  <span id="kup-stamp">--</span>
                </div>
              </header>
              <div id="kup-err"></div>
              <section class="kup-kpis-grid" id="kup-core-kpis"></section>
              <section class="kup-sub-metrics" id="kup-sub-metrics"></section>
              <section class="kup-pcard">
                <div class="kup-card-head">
                  <div>
                    <h2>Token 消耗趋势</h2>
                    <div class="kup-card-sub" id="kup-wave-sub">按时间聚合的总 Token 消耗</div>
                  </div>
                  <div class="kup-tabs" id="kup-wave-tabs">
                    <button class="kup-tab" data-period="today">今日</button>
                    <button class="kup-tab active" data-period="week">本周</button>
                    <button class="kup-tab" data-period="month">本月</button>
                  </div>
                </div>
                <div class="kup-card-body">
                  <div class="kup-chart-legend">
                    <span><i class="kup-dot-token"></i> 总 Token 消耗 (波浪条)</span>
                    <span><i class="kup-dot-call"></i> 调用次数 (柱形)</span>
                  </div>
                  <div id="kup-wave-wrap">
                    <div id="kup-wave-tip"></div>
                    <svg id="kup-wave-chart"></svg>
                  </div>
                </div>
              </section>
              <div class="kup-grid2">
                <section class="kup-pcard">
                  <div class="kup-card-head">
                    <div>
                      <h2>模型调用统计</h2>
                      <div class="kup-card-sub" id="kup-models-sub">各模型调用量、输入/输出 Token 及占比</div>
                    </div>
                    <div class="kup-tabs" id="kup-model-tabs">
                      <button class="kup-tab active" data-m="today">今日</button>
                      <button class="kup-tab" data-m="yesterday">昨日</button>
                      <button class="kup-tab" data-m="week">本周</button>
                      <button class="kup-tab" data-m="cumul">累计</button>
                    </div>
                  </div>
                  <div class="kup-card-body" style="padding:0">
                    <div class="kup-scroll"><table id="kup-models-table"></table></div>
                  </div>
                </section>
                <section class="kup-pcard">
                  <div class="kup-card-head">
                    <div>
                      <h2>会话分布</h2>
                      <div class="kup-card-sub">按 Token 消耗降序 · Top 8</div>
                    </div>
                  </div>
                  <div class="kup-card-body" style="padding:0">
                    <div class="kup-scroll"><table id="kup-sessions-table"></table></div>
                  </div>
                </section>
              </div>
              <section class="kup-pcard">
                <div class="kup-card-head">
                  <div>
                    <h2>每日明细</h2>
                    <div class="kup-card-sub">最近 30 天逐日 Token 消耗、调用次数与成本</div>
                  </div>
                </div>
                <div class="kup-card-body" style="padding:0">
                  <div class="kup-scroll" style="max-height:360px"><table id="kup-daily-table"></table></div>
                </div>
              </section>
            </div>
          </div>
        </div>
      `;
      document.body.appendChild(modal);

      modal.querySelector('#kup-close').onclick = closePanel;
      modal.onclick = (e) => { if (e.target === modal) closePanel(); };
      modal.querySelector('#kup-theme-btn').onclick = () => {
        kupApplyTheme(kupTheme === 'dark' ? 'light' : 'dark');
      };
      modal.querySelector('#kup-refresh-btn').onclick = () => {
        reloadDataScript();
        fetchData();
        probeService();
      };
      modal.querySelector('#kup-update-btn').onclick = kupUpdateClick;
      modal.querySelector('#kup-uninstall-btn').onclick = kupUninstallClick;
      modal.querySelector('#kup-mute-btn').onclick = kupToggleMute;
      kupMuteRender();
      modal.querySelectorAll('#kup-wave-tabs .kup-tab').forEach(function(t) {
        t.onclick = function() { kupSelectWavePeriod(t.getAttribute('data-period')); };
      });
      modal.querySelectorAll('#kup-model-tabs .kup-tab').forEach(function(t) {
        t.onclick = function() { kupSelectModelTab(t.getAttribute('data-m')); };
      });
      kupInitTheme();
    }
    kuLayerShow(modal, 'active');
    kupRenderSettings();
    kupFetchSettings();
    // 骨架占位：首次打开在重渲染完成前覆盖一层 shimmer，DOM/事件保持原样
    var app = modal.querySelector('#kup-app');
    var shell = modal.querySelector('.kup-shell');
    if (app && shell && !app._kuRendered && !modal.querySelector('.kup-skel-overlay')) {
      var skel = document.createElement('div');
      skel.className = 'kup-skel-overlay';
      skel.innerHTML = '<div class="kup-skel">' +
        '<div class="kup-skel-row" style="width:42%"></div>' +
        '<div class="kup-skel-row" style="width:86%"></div>' +
        '<div class="kup-skel-row" style="width:64%"></div>' +
        '<div class="kup-skel-row" style="width:74%"></div>' +
        '<div class="kup-skel-row" style="width:55%"></div></div>';
      shell.appendChild(skel);
    }
    // 重渲染放到下一帧 / 空闲时，让面板动画先起来（点击即时响应）
    kuScheduleIdle(function() {
      try {
        kupRenderAll();
        if (app) app._kuRendered = true;
      } finally {
        // 渲染异常也必须摘掉骨架层，否则白板永久盖住面板
        var sk = modal.querySelector('.kup-skel-overlay');
        if (sk) sk.remove();
      }
    });
  }

  function closePanel() {
    var modal = document.getElementById(PANEL_MODAL_ID);
    kuLayerHide(modal, 'active');
  }

  /* ================================================================
   * 自更新：仅在点击「更新」后检查 GitHub；有新版本才弹窗，已是最新不弹窗
   * ================================================================ */
  function kupUpdateBadge(text) {
    var btn = document.getElementById('kup-update-btn');
    if (!btn) return;
    btn.textContent = text || (kupUpdating ? '更新中…' : '更新');
  }

  var KUP_LAST_KEY = 'kimi-usage-upd-last';
  var KUP_FOUND_KEY = 'kimi-usage-upd-found';
  var KUP_MUTE_KEY = 'kimi-usage-upd-mute';
  function kupLs(k, v) {
    try {
      if (v === undefined) return localStorage.getItem(k);
      if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v);
    } catch (e) {}
    return null;
  }
  function kupMuted() { return kupLs(KUP_MUTE_KEY) === '1'; }
  function kupMuteRender() {
    var b = document.getElementById('kup-mute-btn');
    if (!b) return;
    b.textContent = kupMuted() ? '更新提醒：关' : '更新提醒：开';
    b.title = kupMuted() ? '已关闭每日更新提醒，点击重新开启' : '每日检查一次更新并在按钮上显示红点；点击关闭提醒';
  }
  function kupDot(on) { document.body.classList.toggle('kud-has-update', !!on); }
  function kupToggleMute() {
    var m = !kupMuted();
    kupLs(KUP_MUTE_KEY, m ? '1' : null);
    kupMuteRender();
    if (m) { kupDot(false); kmmToast('已关闭更新提醒，不会再显示红点（仍可手动点「更新」检查）'); }
    else { kmmToast('已开启更新提醒'); kupDailyCheck(true); }
  }
  // 每日提醒：24 小时内最多静默检查一次，只亮红点，不弹窗
  function kupDailyCheck(force) {
    if (kupMuted()) { kupDot(false); return; }
    var found = kupLs(KUP_FOUND_KEY);
    var last = parseInt(kupLs(KUP_LAST_KEY) || '0', 10) || 0;
    if (!force && Date.now() - last < 24 * 3600 * 1000) { kupDot(!!found); return; }
    kapiFetch(API_BASE + '/api/update/check', { cache: 'no-store' })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d || d.error) return;
        kupLs(KUP_LAST_KEY, String(Date.now()));
        kupLs(KUP_FOUND_KEY, d.update ? d.latest : null);
        kupDot(!!d.update && !kupMuted());
      })
      .catch(function() {});
  }

  function kupUpdateDialog(info, onOk) {
    var o = document.getElementById('kud-overlay');
    if (!o) {
      o = document.createElement('div');
      o.id = 'kud-overlay';
      o.innerHTML = '<div id="kud-modal">' +
        '<h3>发现新版本</h3>' +
        '<div class="kud-ver">当前版本 v<span id="kud-cur"></span><br>最新版本 <b>v<span id="kud-new"></span></b><span id="kud-date" style="margin-left:8px;font-size:11.5px;color:var(--color-text-faint,#94a3b8);"></span></div>' +
        '<div id="kud-notes"></div>' +
        '<div class="kud-tip">更新后后台服务会自动重启，几秒钟后恢复。</div>' +
        '<div class="kud-foot"><button id="kud-cancel">稍后</button><button id="kud-ok">立即更新</button></div>' +
        '</div>';
      document.body.appendChild(o);
      o.onclick = function(e) { if (e.target === o) kuLayerHide(o, 'visible'); };
      o.querySelector('#kud-cancel').onclick = function() { kuLayerHide(o, 'visible'); };
    }
    o.querySelector('#kud-cur').textContent = info.current;
    o.querySelector('#kud-new').textContent = info.latest;
    o.querySelector('#kud-date').textContent = info.released ? '发布于 ' + info.released : '';
    var nb = o.querySelector('#kud-notes');
    nb.innerHTML = '';
    (info.notes || []).forEach(function(n) {
      var d = document.createElement('div');
      d.className = 'kud-n-' + n.t;
      d.textContent = (n.t === 'li' ? '• ' : '') + n.text;
      nb.appendChild(d);
    });
    nb.style.display = (info.notes && info.notes.length) ? 'block' : 'none';
    o.querySelector('#kud-ok').onclick = function() { kuLayerHide(o, 'visible'); onOk(); };
    kuLayerShow(o, 'visible');
  }

  function kupApplyUpdate() {
    kupUpdating = true;
    kupUpdateBadge();
    kapiFetch(API_BASE + '/api/update/apply', { method: 'POST' })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.success && d.latest) {
          kmmToast('正在更新到 v' + d.latest + '，服务重启中…');
        } else {
          kmmToast(d.message || '更新失败', !d.success);
          kupUpdating = false;
          kupUpdateBadge();
        }
      })
      .catch(function() {
        // 服务重启时连接被重置属正常——更新大概率已在进行
        kmmToast('服务重启中，稍后自动恢复');
      });
    // 重启后旧页面状态失效：到点复位按钮
    setTimeout(function() { kupUpdating = false; kupUpdateBadge(); }, 20000);
  }

  function kupUpdateClick() {
    if (kupUpdating || kupChecking) return;
    kupChecking = true;
    kupUpdateBadge('检查中…');
    kapiFetch(API_BASE + '/api/update/check?force=1', { cache: 'no-store' })
      .then(function(r) { return r.json(); })
      .then(function(d) {
        kupChecking = false;
        kupUpdateBadge();
        if (d.error) { kmmToast('检查更新失败：' + d.error, true); return; }
        kupLs(KUP_LAST_KEY, String(Date.now()));
        kupLs(KUP_FOUND_KEY, d.update ? d.latest : null);
        kupDot(!!d.update && !kupMuted());
        if (d.update) { kupUpdateDialog(d, kupApplyUpdate); return; }
        kupUpdateBadge('已是最新 ✓');
        setTimeout(function() { if (!kupUpdating && !kupChecking) kupUpdateBadge(); }, 2000);
      })
      .catch(function() {
        kupChecking = false;
        kupUpdateBadge();
        kmmToast('检查更新失败：后台服务未响应', true);
      });
  }

  function kupUninstallClick() {
    if (kupUninstalling) return;
    if (!confirm('确定卸载 Kimi Code 用量面板？\n\n会打开一个命令行窗口：结束所有插件进程、清理侧边栏注入、删除用量数据目录和插件目录。重启 Kimi Code 后侧栏卡片消失。')) return;
    kupUninstalling = true;
    var remote = window.KimiRemoteWidget;
    if (remote && typeof remote.close === 'function') remote.close();
    var button = document.getElementById('kup-uninstall-btn');
    if (button) { button.disabled = true; button.textContent = '卸载中…'; }
    kapiFetch(API_BASE + '/api/plugin/uninstall', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
      cache: 'no-store'
    }).then(function(r) {
      return r.json().then(function(d) {
        if (!r.ok || !d || d.success !== true) {
          throw new Error((d && d.message) || '卸载脚本启动失败。');
        }
        return d;
      });
    }).then(function(d) {
      alert((d && d.message ? d.message : '卸载清理窗口已打开。')
        + '\n\n请在新打开的窗口中查看进度；结束后重启 Kimi Code。');
    }).catch(function(e) {
      kupUninstalling = false;
      if (button) { button.disabled = false; button.textContent = '卸载'; }
      alert((e && e.message ? e.message : '卸载失败。') + '\n\n也可以手动运行插件目录下 scripts\\uninstall.cmd。');
    });
  }

  /* ================================================================
   * 模型管理弹窗（移植自 kimi-embedded-widget.js，kmm- 前缀）
   * ================================================================ */
  function ensureModelOverlay() {
    if (kmmOverlayEl) return kmmOverlayEl;
    var overlay = document.createElement('div');
    overlay.id = KMM_OVERLAY_ID;
    overlay.innerHTML = `
      <div id="kmm-modal" role="dialog" aria-modal="true" aria-labelledby="kmm-settings-title" tabindex="-1">
        <div class="kmm-modal-header">
          <div class="kmm-modal-title"><h2 id="kmm-settings-title">设置</h2>
            <span class="kmm-def-badge" id="kmm-modal-default-badge">默认：加载中</span>
          </div>
          <button class="kmm-x" id="kmm-modal-close-btn" aria-label="关闭设置">✕</button>
        </div>
        <div class="kmm-workspace">
          <nav class="kmm-tabs" role="tablist" aria-label="设置板块" aria-orientation="vertical">
            <button class="kmm-nav on" id="kmm-tab-models" role="tab" aria-selected="true" aria-controls="kmm-modal-cards"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4m14-6h4m-4 6h4"/><rect x="9" y="9" width="6" height="6" rx="1"/></svg><span>模型</span><span class="kmm-nav-count" id="kmm-count-models">—</span></button>
            <button class="kmm-nav" id="kmm-tab-providers" role="tab" aria-selected="false" aria-controls="kmm-provider-cards"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3v5m8-5v5M6 8h12v3a6 6 0 0 1-12 0Zm6 9v4"/></svg><span>供应商</span><span class="kmm-nav-count" id="kmm-count-providers">—</span></button>
            <button class="kmm-nav" id="kmm-tab-mcp" role="tab" aria-selected="false" aria-controls="kmm-mcp-cards"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="3" width="16" height="7" rx="2"/><rect x="4" y="14" width="16" height="7" rx="2"/><path d="M8 6.5h.01M8 17.5h.01M12 7h4m-4 10h4"/></svg><span>MCP 服务器</span><span class="kmm-nav-count" id="kmm-count-mcp">—</span></button>
            <button class="kmm-nav" id="kmm-tab-skills" role="tab" aria-selected="false" aria-controls="kmm-skill-cards"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 3 2.8 6.2L21 12l-6.2 2.8L12 21l-2.8-6.2L3 12l6.2-2.8Z"/></svg><span>技能</span><span class="kmm-nav-count" id="kmm-count-skills">—</span></button>
          </nav>
          <main class="kmm-main">
            <div class="kmm-page-head"><h3 id="kmm-page-title">模型</h3><p id="kmm-page-description">管理模型连接、能力与计价。</p></div>
            <div class="kmm-toolbar" id="kmm-list-toolbar">
              <input class="kmm-search" id="kmm-modal-filter" placeholder="搜索模型…" aria-label="搜索当前板块">
              <button class="kmm-tbtn primary" id="kmm-manager-add">新增模型</button>
              <div id="kmm-tools">
                <button class="kmm-tbtn" id="kmm-tools-button" aria-label="更多操作" aria-expanded="false">⋯</button>
                <div class="kmm-menu" id="kmm-tools-menu" hidden>
                  <button class="kmm-link" id="kmm-modal-refresh">刷新列表</button>
                  <button class="kmm-link" id="kmm-modal-auto-all">批量设置能力…</button>
                  <button class="kmm-link" id="kmm-sync-prices">同步参考价格…</button>
                  <button class="kmm-link" id="kmm-fix-all">修复可确定的问题…</button>
                </div>
              </div>
            </div>
            <div id="kmm-manager-status" role="status" aria-live="polite" hidden><span id="kmm-status-text"></span><button class="kmm-link accent" id="kmm-status-action" hidden>重新连接</button></div>
            <div class="kmm-content">
              <div class="kmm-lists">
                <div class="kmm-modal-body" id="kmm-modal-cards" role="tabpanel" aria-labelledby="kmm-tab-models"></div>
                <div class="kmm-modal-body" id="kmm-provider-cards" role="tabpanel" aria-labelledby="kmm-tab-providers" hidden></div>
                <div class="kmm-modal-body" id="kmm-mcp-cards" role="tabpanel" aria-labelledby="kmm-tab-mcp" hidden></div>
                <div class="kmm-modal-body" id="kmm-skill-cards" role="tabpanel" aria-labelledby="kmm-tab-skills" hidden></div>
              </div>
              <section id="kmm-detail" aria-labelledby="kmm-detail-title" hidden>
                <div class="kmm-detail-head"><button class="kmm-link" id="kmm-detail-back">← 返回</button><h3 id="kmm-detail-title">详情</h3><span id="kmm-dirty" hidden>未保存</span><button class="kmm-link" id="kmm-detail-expand" aria-label="展开编辑区" hidden>展开</button></div>
                <form class="kmm-modal-body kmm-editor" id="kmm-editor" hidden autocomplete="off"></form>
                <div class="kmm-modal-body kmm-help" id="kmm-help" hidden></div>
              </section>
            </div>
          </main>
        </div>
        <div class="kmm-foot"><span>列表操作自动保存 · 表单点击保存后提交</span><button class="kmm-link" id="kmm-help-button">设置帮助</button></div>
        <div id="kmm-confirm" role="alertdialog" aria-modal="true" aria-labelledby="kmm-confirm-title" aria-describedby="kmm-confirm-message" hidden>
          <div class="kmm-confirm-box"><h3 id="kmm-confirm-title">确认操作</h3><div id="kmm-confirm-message"></div><div class="kmm-editor-actions"><button class="kmm-tbtn" id="kmm-confirm-cancel">取消</button><button class="kmm-tbtn primary" id="kmm-confirm-ok">确认</button></div></div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    kmmOverlayEl = overlay;

    var $o = function(id) { return overlay.querySelector('#' + id); };
    $o('kmm-modal-close-btn').onclick = closeModelModal;
    overlay.onclick = function(e) { if (e.target === overlay) closeModelModal(); };
    $o('kmm-modal-refresh').onclick = function() { kmmRefreshCurrent(); kmmCloseTools(); };
    $o('kmm-modal-filter').oninput = function() {
      kmmQueries[kmmManagerTab] = this.value;
      kmmRenderModalCards(); kmmRenderProviders(); mcpRenderCards(); skillRenderCards();
    };
    $o('kmm-tools-button').onclick = function() {
      var menu = $o('kmm-tools-menu'); menu.hidden = !menu.hidden;
      this.setAttribute('aria-expanded', String(!menu.hidden));
    };
    $o('kmm-fix-all').onclick = function() { kmmCloseTools(); window.__KMM_ISSUE('', '', 'fix_all'); };
    $o('kmm-detail-back').onclick = async function() { if (await kmmDiscardAll()) kmmRenderManagerUI(); };
    $o('kmm-detail-expand').onclick = function() {
      var wide = $o('kmm-detail').classList.toggle('wide'); this.textContent = wide ? '收起' : '展开';
    };
    $o('kmm-help-button').onclick = async function() {
      if (!await kmmDiscardAll()) return;
      kmmDetailMode = 'help';
      $o('kmm-help').innerHTML = '<h3>保存与生效</h3><p>列表上的独立操作自动保存。详情中的修改点击「保存」后提交，取消不会写入。</p>'
        + '<p>模型与供应商配置保存后，在会话中执行 <code>/reload</code> 加载；MCP 配置在新会话或重连后加载。设为默认不会切换当前对话。</p>'
        + '<h3>模型能力与价格</h3><p>能力、上下文和思考档位是本地配置声明，不代表上游实际支持。新模型默认开启三项能力、1M 上下文和五档思考，默认 high；请按供应商说明调整。</p>'
        + '<p>价格仅用于用量估算，不影响供应商实际收费。公开目录价格可能与转售渠道价格不同，应用前请检查匹配结果。</p>'
        + '<h3>配置与安全</h3><p>已有密钥不回显；不更换或移除就会保留。托管供应商只读，部分模型被默认设置或模式引用时不能删除。供应商卡片「⋯」可删除无模型引用的非托管供应商，需先删除或迁移关联模型。</p>'
        + '<p>保存会保留未编辑字段的值，但重写配置排版与注释，不生成备份。外部修改导致冲突时不会强行覆盖。</p>'
        + '<p>技能的「自动调用」关闭后，仍可通过技能命令手动调用。仅用户级技能可编辑。删除技能会移除其整个目录。</p>';
      kmmRenderManagerUI(); kmmFocusDetail();
    };
    $o('kmm-status-action').onclick = kmmRefreshCurrent;
    overlay.addEventListener('input', kmmUpdateDirty);
    overlay.addEventListener('change', kmmUpdateDirty);
    overlay.addEventListener('click', function(e) {
      if (!e.target.closest('#kmm-tools')) kmmCloseTools();
      overlay.querySelectorAll('.kmm-item-menu[open]').forEach(function(menu) { if (!menu.contains(e.target)) menu.open = false; });
    });
    $o('kmm-tab-models').onclick = function() { kmmSwitchTab('models'); };
    $o('kmm-tab-providers').onclick = function() { kmmSwitchTab('providers'); };
    $o('kmm-tab-mcp').onclick = function() { kmmSwitchTab('mcp'); };
    $o('kmm-tab-skills').onclick = function() { kmmSwitchTab('skills'); };
    $o('kmm-manager-add').onclick = function() {
      if (kmmManagerTab === 'providers') kmmOpenEditor('provider', null);
      else if (kmmManagerTab === 'mcp') mcpOpenEditor(null);
      else if (kmmManagerTab === 'skills') skillNew();
      else kmmOpenBatch();
    };
    overlay.addEventListener('keydown', kmmKeyboard);
    $o('kmm-modal-auto-all').onclick = async function() {
      kmmCloseTools();
      if (kmmBusy() || kmmDraft || kmmBatch || !apiAlive) return;
      if (!await kmmConfirm('将为所有模型开启识图、思考、工具调用的本地能力声明。此操作不验证上游支持，也不修改思考档位。', '批量设置', '批量设置模型能力？')) return;
      kmmQuickBusy = true; kmmRenderManagerUI();
      try {
        const res = await kapiFetch(`${API_BASE}/api/auto-enable-all`, { method: 'POST' });
        const data = await res.json();
        if (data.success) { kmmToast('能力配置已保存，请在会话中 /reload 加载'); await kmmRefreshAll(); }
        else kmmToast(data.message, true);
      } catch (e) { kmmToast('执行失败：后台服务未响应', true); }
      finally { kmmQuickBusy = false; kmmRenderManagerUI(); }
    };
    $o('kmm-sync-prices').onclick = function() { ksyncOpen(); };
    return overlay;
  }

  var kmmManagerData = null;
  var kmmManagerOnline = false;
  var kmmManagerMessage = '正在读取模型管理配置…';
  var kmmManagerTab = 'models';
  var kmmManagerRequest = null;
  var kmmDraft = null;
  var kmmBatch = null;
  var kmmEditorReloading = false;
  var kmmDeleting = null;
  var kmmSyncBusy = false;
  var kmmDefaultEfforts = ['low', 'medium', 'high', 'xhigh', 'max'];

  var kmmQueries = { models: '', providers: '', mcp: '', skills: '' };
  var kmmDetailMode = '';
  var kmmConfirmResolve = null;
  var kmmReturnToAdd = false;
  var kmmDetailOrigin = null;
  var kmmModalOrigin = null;
  var kmmQuickBusy = false;

  function kmmEl(id) { return kmmOverlayEl && kmmOverlayEl.querySelector('#' + id); }
  function kmmCloseTools() {
    if (!kmmOverlayEl) return;
    kmmEl('kmm-tools-menu').hidden = true;
    kmmEl('kmm-tools-button').setAttribute('aria-expanded', 'false');
  }
  function kmmRefreshCurrent() {
    if (kmmManagerTab === 'mcp') return mcpFetch();
    if (kmmManagerTab === 'skills') return skillFetch();
    return kmmRefreshAll();
  }
  function kmmConfirm(message, acceptLabel, title, cancelLabel) {
    if (kmmConfirmResolve) return Promise.resolve(false);
    var previous = document.activeElement;
    kmmEl('kmm-confirm-title').textContent = title || '确认操作';
    kmmEl('kmm-confirm-message').textContent = message;
    kmmEl('kmm-confirm-ok').textContent = acceptLabel || '确认';
    kmmEl('kmm-confirm-cancel').textContent = cancelLabel || '取消';
    kmmEl('kmm-confirm').hidden = false;
    ['.kmm-modal-header', '.kmm-workspace', '.kmm-foot'].forEach(function(selector) { kmmEl('kmm-modal').querySelector(selector).inert = true; });
    return new Promise(function(resolve) {
      kmmConfirmResolve = function(accepted) {
        kmmEl('kmm-confirm').hidden = true; kmmConfirmResolve = null;
        ['.kmm-modal-header', '.kmm-workspace', '.kmm-foot'].forEach(function(selector) { kmmEl('kmm-modal').querySelector(selector).inert = false; });
        if (previous && previous.isConnected) previous.focus();
        resolve(accepted);
      };
      kmmEl('kmm-confirm-ok').onclick = function() { kmmConfirmResolve(true); };
      kmmEl('kmm-confirm-cancel').onclick = function() { kmmConfirmResolve(false); };
      kmmEl('kmm-confirm-cancel').focus();
    });
  }
  function skillCapture() {
    var d = skillDetail;
    if (!d || !d.editing || d.loading) return;
    var name = document.getElementById('ksk-name'), desc = document.getElementById('ksk-desc');
    var body = document.getElementById('ksk-body'), disabled = document.getElementById('ksk-disabled');
    if (name) d.name = name.value;
    if (desc) d.description = desc.value;
    if (body) d.body = body.value;
    if (disabled) d.disabled = disabled.checked;
  }
  function skillEditValue() {
    return skillDetail ? JSON.stringify({ name: skillDetail.name == null ? skillDetail.item.name : skillDetail.name,
      description: skillDetail.description || '', body: skillDetail.body || '', disabled: !!skillDetail.disabled }) : '';
  }
  function kmmChanged() {
    skillCapture();
    return kmmDraftChanged() || mcpDraftChanged() || kmmBatchChanged()
      || !!(skillDetail && skillDetail.editing && !skillDetail.loading && !skillDetail.detailError && skillDetail.initial !== skillEditValue())
      || !!(kmmDetailMode === 'price' && kprInitial !== kprSnapshot());
  }
  function kmmUpdateDirty() {
    if (kmmOverlayEl) kmmEl('kmm-dirty').hidden = !kmmChanged();
  }
  function kmmBusy() {
    return !!(kmmDeleting || kmmQuickBusy || mcpBusy || skillBusy || kmmSyncBusy || kmmEditorReloading
      || (kmmDraft && kmmDraft.saving) || (mcpDraft && mcpDraft.saving) || (skillDetail && skillDetail.saving)
      || (kmmBatch && (kmmBatch.saving || kmmBatch.reloading)));
  }
  function kmmFocusDetail() {
    if (!kmmDetailOrigin) kmmDetailOrigin = document.activeElement;
    var focus = Array.from(kmmEl('kmm-detail').querySelectorAll('input:not(:disabled), textarea:not(:disabled), select:not(:disabled)')).find(function(el) { return el.getClientRects().length; });
    (focus || kmmEl('kmm-detail-back')).focus();
  }
  function kmmRestoreFocus() {
    var target = kmmDetailOrigin; kmmDetailOrigin = null;
    if (target && target.isConnected) target.focus();
    else if (kmmOverlayEl) kmmEl('kmm-manager-add').focus();
  }
  function kmmMountDetail(node, mode, title) {
    kmmDetailMode = mode;
    kmmEl('kmm-detail').appendChild(node);
    node.hidden = false;
    kmmEl('kmm-detail-title').textContent = title;
    kmmRenderManagerUI(); kmmFocusDetail();
  }
  function kmmKeyboard(e) {
    if (e.key === 'Escape') {
      e.preventDefault(); e.stopPropagation();
      if (kmmConfirmResolve) kmmConfirmResolve(false);
      else if (!kmmEl('kmm-tools-menu').hidden) kmmCloseTools();
      else if (!kmmEl('kmm-detail').hidden) kmmDiscardAll().then(function(ok) { if (ok) kmmRenderManagerUI(); });
      else closeModelModal();
      return;
    }
    if (e.key !== 'Tab') return;
    var root = kmmConfirmResolve ? kmmEl('kmm-confirm') : kmmEl('kmm-modal');
    var items = Array.from(root.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]'))
      .filter(function(el) { return el.getClientRects().length && (!kmmConfirmResolve || root.contains(el)); });
    if (!items.length) return;
    var first = items[0], last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
  }
  function kmmStrings(values) {
    return Array.isArray(values) ? values.filter(function(v, i) { return typeof v === 'string' && values.indexOf(v) === i; }) : [];
  }
  function kmmOrderedEfforts(values) {
    return kmmStrings(values).sort(function(a, b) {
      var ai = kmmDefaultEfforts.indexOf(a), bi = kmmDefaultEfforts.indexOf(b);
      return (ai < 0 ? kmmDefaultEfforts.length : ai) - (bi < 0 ? kmmDefaultEfforts.length : bi);
    });
  }
  function kmmNormalizeAlias(alias, provider, previousProvider) {
    var value = String(alias || '').trim(), prefix = String(provider || '').trim() + '/';
    if (!value || !provider) return value;
    if (previousProvider && previousProvider !== provider && value.indexOf(previousProvider + '/') === 0) {
      value = value.slice(previousProvider.length + 1);
    }
    return value.indexOf(prefix) === 0 ? value : prefix + value;
  }
  function kmmManagerWritable() { return !!(kmmManagerOnline && kmmManagerData && kmmManagerData.editable); }
  function kmmSafeMessage(message) {
    var text = typeof message === 'string' ? message : '';
    var key = kmmDraft && kmmDraft.kind === 'provider' && kmmDraft.value.api_key;
    return key ? text.split(key).join('[密钥已隐藏]') : text;
  }
  function kmmFetchManager() {
    if (kmmManagerRequest) return kmmManagerRequest;
    kmmManagerRequest = kapiFetch(API_BASE + '/api/model-manager', { cache: 'no-store' })
      .then(function(r) { if (!r.ok) throw new Error('unavailable'); return r.json(); })
      .then(function(d) {
        if (!d || typeof d.version !== 'string' || typeof d.editable !== 'boolean' || !Array.isArray(d.providers)
            || !Array.isArray(d.models) || !Array.isArray(d.provider_types) || !Array.isArray(d.effort_levels)) throw new Error('invalid');
        kmmManagerData = {
          version: d.version, editable: d.editable, message: kmmSafeMessage(d.message), default_model: String(d.default_model || ''),
          providers: d.providers.map(function(p) {
            return { name: String(p.name || ''), type: String(p.type || ''), base_url: String(p.base_url || ''), has_api_key: !!p.has_api_key, managed: !!p.managed,
              delete_protected: !!p.delete_protected, delete_reason: typeof p.delete_reason === 'string' ? p.delete_reason : '' };
          }),
          models: d.models.map(function(m) {
            return { alias: String(m.alias || ''), provider: String(m.provider || ''), model: String(m.model || ''), display_name: String(m.display_name || ''),
              max_context_size: m.max_context_size, capabilities: kmmStrings(m.capabilities), support_efforts: kmmStrings(m.support_efforts),
              default_effort: typeof m.default_effort === 'string' ? m.default_effort : null, managed: !!m.managed,
              delete_protected: !!m.delete_protected, delete_reason: typeof m.delete_reason === 'string' ? m.delete_reason : '' };
          }),
          provider_types: kmmStrings(d.provider_types), effort_levels: kmmStrings(d.effort_levels)
        };
        kmmManagerOnline = true;
        kmmManagerMessage = d.editable ? kmmManagerData.message : (kmmManagerData.message || '配置解析器不可用，当前只读，不能保存。');
        return kmmManagerData;
      }).catch(function() {
        kmmManagerOnline = false;
        kmmManagerMessage = '模型管理服务离线或接口不可用，当前只读，不能保存。请启动或更新后台服务后刷新。';
        return null;
      }).then(function(d) {
        kmmManagerRequest = null;
        if (kmmBatch && !kmmDraft) kmmRenderBatchRows();
        if (!kmmDraft && !kmmBatch && !kmmDetailMode && !skillDetail) { kmmRenderModalCards(); kmmRenderProviders(); }
        kmmRenderManagerUI();
        return d;
      });
    return kmmManagerRequest;
  }
  function kmmRefreshAll() {
    var manager = Promise.resolve(kmmManagerRequest).then(function() { return kmmFetchManager(); });
    var modes = kmdFetch(true).then(function() {
      if (!kmdData || !kmdOverlayEl || !kmdOverlayEl.classList.contains('visible')) return;
      kmdOverlayEl.querySelectorAll('select[data-f="main"], select[data-f="sub"]').forEach(function(select) {
        select.innerHTML = kmdOptions(select.value, select.getAttribute('data-f') === 'sub');
      });
    });
    return Promise.all([fetchModels(), manager, modes]);
  }
  function kmmRenderEmpty(container, title, description) {
    var empty = document.createElement('div');
    empty.className = 'kmm-empty';
    empty.innerHTML = '<strong>' + esc(title) + '</strong><p>' + esc(description || '') + '</p>';
    container.appendChild(empty);
  }
  function kmmRenderManagerUI() {
    if (!kmmOverlayEl) return;
    var editing = !!kmmDraft || !!mcpDraft, batching = !!kmmBatch;
    var detail = editing || batching || !!skillDetail || !!kmmDetailMode, busy = kmmBusy();
    kmmEl('kmm-modal').classList.toggle('has-detail', detail);
    kmmEl('kmm-detail').hidden = !detail;
    kmmEl('kmm-editor').hidden = !editing;
    kmmEl('kmm-help').hidden = kmmDetailMode !== 'help';
    if (batching) kmmRenderBatch();
    if (kmmEl('kmm-batch')) kmmEl('kmm-batch').hidden = !batching || editing;
    var labels = { models: '模型', providers: '供应商', mcp: 'MCP 服务器', skills: '技能' };
    var descriptions = { models: '管理可用模型，配置能力、上下文与费用估算。', providers: '集中管理 API 连接与认证信息，已有密钥不会回显。',
      mcp: '连接外部工具与服务，保存后在新会话或重连时加载。', skills: '管理技能指令与自动调用，仅用户级技能可编辑。' };
    var ids = { models: 'kmm-modal-cards', providers: 'kmm-provider-cards', mcp: 'kmm-mcp-cards', skills: 'kmm-skill-cards' };
    var counts = { models: kmmManagerData && kmmManagerData.models.length, providers: kmmManagerData && kmmManagerData.providers.length,
      mcp: mcpData && mcpData.servers.length, skills: skillData && skillData.skills.length };
    kmmEl('kmm-page-title').textContent = labels[kmmManagerTab];
    kmmEl('kmm-page-description').textContent = descriptions[kmmManagerTab];
    Object.keys(labels).forEach(function(tab) {
      var btn = kmmEl('kmm-tab-' + tab), list = kmmEl(ids[tab]);
      btn.classList.toggle('on', kmmManagerTab === tab);
      btn.setAttribute('aria-selected', String(kmmManagerTab === tab)); btn.disabled = busy;
      kmmEl('kmm-count-' + tab).textContent = counts[tab] == null ? '—' : counts[tab];
      list.hidden = kmmManagerTab !== tab;
      if (detail || busy) list.querySelectorAll('.kmm-item-menu[open]').forEach(function(menu) { menu.open = false; });
      list.inert = detail || busy;
    });
    kmmEl('kmm-modal-close-btn').disabled = busy;
    kmmEl('kmm-detail-back').disabled = busy;
    kmmEl('kmm-modal-refresh').disabled = busy || detail;
    var add = kmmEl('kmm-manager-add');
    add.textContent = '新增' + labels[kmmManagerTab];
    add.disabled = busy || detail || !apiAlive || (kmmManagerTab === 'mcp' ? !mcpWritable() : kmmManagerTab === 'skills' ? !skillData : !kmmManagerWritable());
    ['kmm-modal-auto-all', 'kmm-sync-prices', 'kmm-fix-all'].forEach(function(id) {
      kmmEl(id).hidden = kmmManagerTab !== 'models'; kmmEl(id).disabled = busy || detail || !apiAlive || !kmmManagerWritable();
    });
    kmmEl('kmm-tools-button').disabled = busy || detail;
    var filter = kmmEl('kmm-modal-filter'); filter.disabled = detail;
    filter.placeholder = '搜索' + labels[kmmManagerTab] + '…';
    filter.setAttribute('aria-label', '搜索' + labels[kmmManagerTab]);
    var message = kmmManagerTab === 'mcp' ? (mcpData ? mcpData.message : mcpRequest ? '正在读取 MCP 服务器…' : '无法连接 MCP 管理服务。')
      : kmmManagerTab === 'skills' ? (!skillData ? skillRequest ? '正在读取技能…' : '无法读取技能列表。' : '') : kmmManagerMessage;
    var offline = kmmManagerTab === 'mcp' ? !mcpData && !mcpRequest : kmmManagerTab === 'skills' ? !skillData && !skillRequest : !kmmManagerOnline && !kmmManagerRequest;
    var readonly = kmmManagerTab === 'mcp' ? mcpData && !mcpData.editable : kmmManagerTab !== 'skills' && kmmManagerData && !kmmManagerData.editable;
    kmmEl('kmm-status-text').textContent = message || '';
    kmmEl('kmm-manager-status').hidden = !message;
    kmmEl('kmm-manager-status').classList.toggle('is-warning', !!(offline || readonly));
    kmmEl('kmm-status-action').hidden = !offline;
    kmmEl('kmm-status-action').disabled = busy;
    var badge = kmmEl('kmm-modal-default-badge'); badge.hidden = detail || kmmManagerTab !== 'models';
    if (kmmManagerData) {
      var def = kmmManagerData.default_model, model = kmmManagerData.models.find(function(m) { return m.alias === def; });
      badge.textContent = '默认模型 · ' + ((model && model.display_name) || def || '未设置');
      badge.title = badge.textContent;
    }
    var title = editing ? (kmmDraft ? (kmmDraft.original === null ? '新增' : '编辑') + (kmmDraft.kind === 'provider' ? '供应商' : '模型') : (mcpDraft.original === null ? '新增' : '编辑') + ' MCP 服务器')
      : batching ? '新增模型' : skillDetail ? skillDetail.creating ? '新增技能' : skillDetail.editing ? '编辑技能' : '技能详情' : kmmDetailMode === 'help' ? '设置帮助' : kmmDetailMode === 'price' ? '模型计价' : '同步参考价格';
    kmmEl('kmm-detail-title').textContent = title;
    kmmEl('kmm-detail-back').textContent = '← ' + labels[kmmManagerTab];
    kmmEl('kmm-detail-expand').hidden = true;
    if (kmmDraft) kmmUpdateEditorState();
    kmmUpdateDirty();
  }
  function kmmDraftChanged() { return !!(kmmDraft && JSON.stringify(kmmDraft.value) !== kmmDraft.initial); }
  function mcpDraftChanged() { return !!(mcpDraft && JSON.stringify(mcpDraft.value) !== mcpDraft.initial); }
  function kmmClearEditor() {
    if (kmmDraft && kmmDraft.kind === 'provider') kmmDraft.value.api_key = '';
    mcpDraft = null;
    var editor = kmmEl('kmm-editor');
    if (editor) {
      var key = editor.querySelector('#kmm-provider-api-key');
      if (key) key.value = '';
      editor.innerHTML = '';
      editor.onsubmit = null;
    }
    kmmDraft = null;
  }
  async function kmmDiscardAll() {
    if (kmmBusy() || kmmConfirmResolve) return false;
    if (kmmChanged() && !await kmmConfirm('修改尚未保存。离开后将丢弃这些修改。', '放弃修改', '离开编辑？', '继续编辑')) return false;
    kmmClearEditor(); kmmClearBatch();
    skillCloseDetail(true);
    if (kprOverlayEl) { kprOverlayEl.hidden = true; kprOverlayEl.classList.remove('visible'); }
    if (ksyncOverlayEl) { ksyncOverlayEl.hidden = true; ksyncOverlayEl.classList.remove('visible'); }
    kmmDetailMode = ''; kmmReturnToAdd = false;
    kmmEl('kmm-help').hidden = true; kmmEl('kmm-detail').classList.remove('wide');
    kmmRenderManagerUI();
    if (kmmDetailOrigin) kmmRestoreFocus();
    return true;
  }
  async function kmmSwitchTab(tab) {
    if (tab === kmmManagerTab) return;
    if (!await kmmDiscardAll()) return;
    kmmManagerTab = tab;
    kmmEl('kmm-modal-filter').value = kmmQueries[tab];
    kmmRenderManagerUI(); kmmRenderModalCards(); kmmRenderProviders();
    if (tab === 'mcp') { mcpRenderCards(); if (!mcpData) mcpFetch(); }
    if (tab === 'skills') { skillRenderCards(); if (!skillData) skillFetch(); }
  }
  function kmmModelValue(item, provider, id, displayName) {
    return { alias: item ? item.alias : kmmNormalizeAlias(id, provider), provider: item ? item.provider : provider, model: item ? item.model : id || '',
      display_name: item ? item.display_name : '', max_context_size: String(item ? item.max_context_size : 1000000),
      capabilities: item ? item.capabilities.slice() : ['image_in', 'thinking', 'tool_use'],
      support_efforts: item ? kmmOrderedEfforts(item.support_efforts) : kmmDefaultEfforts.slice(), default_effort: item ? item.default_effort : 'high' };
  }
  function kmmModelPayload(value, original) {
    var payload = { alias: original === null ? kmmNormalizeAlias(value.alias, value.provider) : original, provider: value.provider, model: value.model,
      max_context_size: Number(value.max_context_size), capabilities: value.capabilities.slice(),
      support_efforts: kmmOrderedEfforts(value.support_efforts), default_effort: value.default_effort };
    if (value.display_name.trim()) payload.display_name = value.display_name.trim();
    else if (original !== null) payload.display_name = null;
    return payload;
  }
  function kmmModelError(value) {
    if (!value.alias.trim() || !value.provider || !value.model.trim()) return '请输入模型 alias、供应商和上游模型 ID。';
    if (!Number.isSafeInteger(Number(value.max_context_size)) || Number(value.max_context_size) <= 0) return '上下文必须是大于 0 的整数。';
    if (value.default_effort !== null && value.support_efforts.indexOf(value.default_effort) < 0) return '默认思考强度必须在所选档位列表中。';
    return '';
  }
  function kmmBatchOperations(b) {
    var upserts = b.rows.filter(function(row) { return !row.original && row.selected; }).map(function(row) {
      return { original_alias: null, model: kmmModelPayload(row.value, null) };
    });
    return { upserts: upserts, removes: [], added: upserts.length };
  }
  function kmmBatchChanged() {
    return !!(kmmBatch && kmmBatch.rows.some(function(row) { return !row.original && row.selected; }));
  }
  function kmmClearBatch() {
    kmmBatch = null;
    var el = kmmEl('kmm-batch');
    if (el) { el.innerHTML = ''; el._kmmBatch = null; el.hidden = true; }
  }
  var kmmBatchCategories = ['全部', 'GPT', 'Claude', 'Gemini', 'Qwen', 'GLM', 'Deepseek', 'Kimi', '其他'];
  function kmmBatchCategoryMatches(model, category) {
    if (category === '全部') return true;
    var id = model.toLowerCase();
    if (category === '其他') return !kmmBatchCategories.slice(1, -1).some(function(name) { return id.indexOf(name.toLowerCase()) >= 0; });
    return id.indexOf(category.toLowerCase()) >= 0;
  }
  function kmmCreateBatch(provider) {
    var data = kmmManagerData;
    kmmBatch = { version: data.version, provider: provider, providers: data.providers.map(function(p) { return p.name; }),
      aliases: data.models.map(function(m) { return m.alias; }), rows: [], query: '', category: '全部', loading: false, saving: false, reloading: false,
      conflict: false, error: '', message: provider ? '选择供应商后，点击「获取模型列表」。也可以手动填写模型 ID。' : '先建立供应商连接，再选择要添加的模型。', catalogLoaded: false };
    data.models.filter(function(m) { return m.provider === provider; }).forEach(function(m) {
      var value = kmmModelValue(m);
      kmmBatch.rows.push({ original: m, value: value, selected: false, catalog: false });
    });
  }
  async function kmmOpenBatch(provider) {
    if (!kmmManagerWritable() || !await kmmDiscardAll()) return;
    if (!kmmManagerData.providers.some(function(p) { return p.name === provider; })) provider = kmmManagerData.providers.length ? kmmManagerData.providers[0].name : '';
    kmmCreateBatch(provider);
    kmmManagerTab = 'models'; kmmEl('kmm-modal-filter').value = kmmQueries.models;
    kmmRenderManagerUI(); kmmRenderBatchRows(); kmmFocusDetail();
  }
  function kmmUniqueAlias(b, id) {
    var taken = b.aliases.concat(b.rows.map(function(row) {
      return row.original ? row.original.alias : kmmNormalizeAlias(row.value.alias, b.provider);
    }));
    var root = kmmNormalizeAlias(id, b.provider), alias = root, n = 2;
    while (taken.indexOf(alias) >= 0) alias = root + '-' + n++;
    return alias;
  }
  function kmmModelDeleteReason(m) {
    if (!m) return '模型管理配置中没有此模型，请刷新后重试。';
    var current = kmmManagerData && kmmManagerData.models.find(function(x) { return x.alias === m.alias; });
    if (m.managed || (current && current.managed)) return '官方托管模型由系统自动管理，不能删除。';
    if ((kmmManagerData && kmmManagerData.default_model === m.alias)) return '当前默认模型不能删除，请先更换默认模型。';
    if ((current && current.delete_protected) || m.delete_protected) return (current && current.delete_reason) || m.delete_reason || '该模型被配置引用，请先解除引用。';
    return '';
  }
  async function kmmDeleteModel(alias) {
    if (kmmDeleting || kmmDraft || kmmBatch || kmmEditorReloading || !kmmManagerWritable()) return;
    var operation = { alias: alias };
    kmmDeleting = operation;
    kmmRenderManagerUI(); kmmRenderModalCards();
    try {
      await Promise.resolve(kmmManagerRequest);
      var snapshot = await kmmFetchManager();
      if (kmmDeleting !== operation || kmmDraft || kmmBatch || !isModelModalOpen) return;
      if (!snapshot || !snapshot.editable) { kmmToast(kmmManagerMessage || '当前只读，不能删除。', true); return; }
      var model = snapshot.models.find(function(m) { return m.alias === alias; });
      var reason = kmmModelDeleteReason(model);
      if (reason) { kmmToast(reason, true); return; }
      if (!await kmmConfirm('将删除模型「' + alias + '」和它的本地覆盖配置，此操作不能撤销。', '删除模型', '删除此模型？')) return;
      var response = await kapiFetch(API_BASE + '/api/model-manager/batch', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: snapshot.version, provider: model.provider, upserts: [], removes: [alias] }) });
      var data = await response.json();
      if (response.status === 409 || data.code === 'CONFIG_CONFLICT') {
        kmmToast(kmmSafeMessage(data.message) || '配置已变化，删除未执行；请刷新后重新确认，不会强制覆盖。', true);
        await kmmFetchManager();
      } else if (response.ok && data.success === true && typeof data.version === 'string') {
        kmmToast('模型已删除并原子保存，会话中 /reload 生效');
        await kmmRefreshAll();
      } else {
        kmmToast(kmmSafeMessage(data.message) || '删除失败，配置未保存。', true);
        if (response.status === 503) { kmmManagerOnline = false; kmmManagerMessage = '配置写入不可用，请刷新后重试。'; }
      }
    } catch (e) {
      kmmToast('删除失败：后台服务未响应或返回异常，请刷新确认配置状态后重试。', true);
      kmmManagerOnline = false; kmmManagerMessage = '后台服务不可用，不能保存；请刷新后重试。';
    } finally {
      if (kmmDeleting === operation) kmmDeleting = null;
      kmmRenderManagerUI(); kmmRenderModalCards(); kmmRenderProviders();
    }
  }
  function kmmRenderBatch() {
    var b = kmmBatch;
    if (!b) return;
    var el = kmmEl('kmm-batch');
    if (!el) {
      el = document.createElement('section'); el.id = 'kmm-batch';
      kmmEl('kmm-editor').before(el);
    }
    if (el._kmmBatch !== b) {
      el._kmmBatch = b;
      el.innerHTML = '<div class="kmm-batch-head"><label class="kmm-field">供应商<select id="kmm-batch-provider">'
        + kmmEditorOptions(b.providers, b.provider, b.providers.length ? null : '尚无供应商') + '</select></label>'
        + '<button type="button" class="kmm-tbtn" id="kmm-batch-probe">获取模型列表</button>'
        + '<div class="kmm-batch-alternatives"><button type="button" class="kmm-link" id="kmm-batch-new-provider">新建供应商</button>'
        + '<button type="button" class="kmm-link" id="kmm-batch-manual">手动添加</button></div>'
        + '<input class="kmm-search" id="kmm-batch-search" placeholder="搜索模型名称或 ID…" aria-label="搜索供应商模型"></div>'
        + '<div class="kmm-batch-filters" role="group" aria-label="模型分类">' + kmmBatchCategories.map(function(category) {
          return '<button type="button" class="kmm-tbtn' + (b.category === category ? ' on' : '') + '" data-kmm-category="' + esc(category)
            + '" aria-pressed="' + String(b.category === category) + '">' + esc(category) + '</button>';
        }).join('') + '</div>'
        + '<div class="kmm-batch-note">默认启用识图、思考、工具调用，1M 上下文，强度 high。仅为本地声明，请按上游支持调整。</div>'
        + '<div class="kmm-batch-note" id="kmm-batch-status" role="status" aria-live="polite"></div>'
        + '<div class="kmm-batch-list" id="kmm-batch-list"></div><div class="kmm-batch-footer"><span id="kmm-batch-count"></span>'
        + '<button type="button" class="kmm-link" id="kmm-batch-reload" hidden>载入最新配置</button><button type="button" class="kmm-tbtn" id="kmm-batch-cancel">取消</button>'
        + '<button type="button" class="kmm-tbtn primary" id="kmm-batch-save">添加模型</button></div>';
      el.querySelectorAll('[data-kmm-category]').forEach(function(button) {
        button.onclick = function() {
          if (kmmBatch !== b || b.saving || b.reloading) return;
          b.category = button.getAttribute('data-kmm-category');
          el.querySelectorAll('[data-kmm-category]').forEach(function(choice) {
            var active = choice.getAttribute('data-kmm-category') === b.category;
            choice.classList.toggle('on', active); choice.setAttribute('aria-pressed', String(active));
          });
          kmmEl('kmm-batch-list').scrollTop = 0;
          kmmRenderBatchRows(); kmmRenderBatch();
        };
      });
      kmmEl('kmm-batch-provider').onchange = async function() {
        if (kmmBatch !== b || b.saving || b.reloading) return;
        var provider = this.value;
        if (kmmBatchChanged() && !await kmmConfirm('切换供应商将取消当前选择的模型。', '切换供应商', '取消当前选择？', '保留选择')) { this.value = b.provider; return; }
        if (kmmBatch !== b) return;
        kmmCreateBatch(provider); kmmRenderManagerUI(); kmmRenderBatchRows();
      };
      kmmEl('kmm-batch-search').oninput = function() { if (kmmBatch === b) { b.query = this.value; kmmRenderBatchRows(); } };
      kmmEl('kmm-batch-probe').onclick = kmmProbeCatalog;
      kmmEl('kmm-batch-new-provider').onclick = async function() {
        if (!await kmmDiscardAll()) return;
        await kmmOpenEditor('provider', null); kmmReturnToAdd = true;
      };
      kmmEl('kmm-batch-manual').onclick = async function() {
        var provider = b.provider;
        if (!await kmmDiscardAll()) return;
        await kmmOpenEditor('model', null);
        if (kmmDraft) { kmmDraft.value.provider = provider; kmmDraft.initial = JSON.stringify(kmmDraft.value); kmmRenderEditor(); }
      };
      kmmEl('kmm-batch-save').onclick = kmmSaveBatch;
      kmmEl('kmm-batch-reload').onclick = kmmReloadBatch;
      kmmEl('kmm-batch-cancel').onclick = async function() { await kmmDiscardAll(); };
      kmmRenderBatchRows();
    }
    var operations = kmmBatchOperations(b), busy = b.saving || b.reloading;
    kmmEl('kmm-batch-count').textContent = '已选择 ' + operations.added + ' 个模型';
    var status = kmmEl('kmm-batch-status');
    status.textContent = b.error || (busy ? (b.saving ? '正在添加模型…' : '正在载入配置…')
      : b.conflict ? '配置已被其他操作修改，当前选择仍保留。请载入最新配置后重新选择。'
      : !kmmManagerWritable() ? kmmManagerMessage : b.loading ? '正在获取供应商模型列表…' : b.message);
    status.classList.toggle('is-warning', !!(b.error || b.conflict || !kmmManagerWritable()));
    kmmEl('kmm-batch-provider').disabled = busy || b.loading || !b.providers.length;
    kmmEl('kmm-batch-new-provider').disabled = busy || b.loading;
    kmmEl('kmm-batch-search').disabled = busy;
    kmmEl('kmm-batch-probe').disabled = busy || b.loading || !b.provider || b.conflict || !kmmManagerWritable();
    kmmEl('kmm-batch-probe').textContent = b.loading ? '获取中…' : '获取模型列表';
    kmmEl('kmm-batch-manual').disabled = busy || b.loading || !b.provider || b.conflict || !kmmManagerWritable();
    kmmEl('kmm-batch-save').disabled = busy || b.loading || b.conflict || !kmmManagerWritable() || !operations.added;
    kmmEl('kmm-batch-save').textContent = b.saving ? '添加中…' : operations.added ? '添加 ' + operations.added + ' 个模型' : '添加模型';
    kmmEl('kmm-batch-reload').hidden = !b.conflict;
    kmmEl('kmm-batch-reload').disabled = busy;
    kmmEl('kmm-batch-cancel').disabled = busy;
    kmmEl('kmm-batch').querySelectorAll('[data-kmm-category]').forEach(function(btn) { btn.disabled = busy; });
    kmmEl('kmm-batch-list').querySelectorAll('input').forEach(function(input) {
      input.disabled = busy || b.loading || b.conflict || !kmmManagerWritable();
    });
    kmmUpdateDirty();
  }
  function kmmRenderBatchRows() {
    var b = kmmBatch, list = kmmEl('kmm-batch-list');
    if (!b || !list || kmmEl('kmm-batch')._kmmBatch !== b) return;
    var scrollTop = list.scrollTop, kw = b.query.trim().toLowerCase();
    list.innerHTML = '';
    var rows = b.rows.filter(function(row) { return !row.original; }).concat(b.rows.filter(function(row) { return !!row.original; }));
    rows.forEach(function(row) {
      var v = row.value;
      if (!kmmBatchCategoryMatches(v.model, b.category)) return;
      if (kw && (v.alias + ' ' + v.model + ' ' + v.display_name).toLowerCase().indexOf(kw) < 0) return;
      var name = v.display_name && v.display_name !== v.alias ? v.display_name : v.model || v.alias;
      var div = document.createElement(row.original ? 'div' : 'label');
      div.className = 'kmm-batch-row' + (row.selected ? ' pending' : '');
      div.innerHTML = (row.original ? '' : '<input type="checkbox" aria-label="' + esc('选择 ' + v.model) + '"' + (row.selected ? ' checked' : '') + '>')
        + '<div class="kmm-batch-info"><div class="kmm-batch-title"><span title="' + esc(v.model) + '">' + esc(name) + '</span>'
        + (row.original ? '<span class="kmm-tag def">已添加</span>' : '') + '</div>'
        + (name !== v.model ? '<div class="kmm-batch-meta">' + esc(v.model) + '</div>' : '')
        + (row.original && b.catalogLoaded && !row.catalog ? '<div class="kmm-batch-meta">仅本地配置 · 本次列表未返回</div>' : '') + '</div>';
      var toggle = div.querySelector('input');
      if (toggle) {
        toggle.disabled = b.saving || b.loading || b.reloading || b.conflict || !kmmManagerWritable();
        toggle.onchange = function() {
          if (kmmBatch !== b || b.saving || b.loading || b.reloading || b.conflict) return;
          row.selected = toggle.checked; b.error = ''; div.classList.toggle('pending', row.selected); kmmRenderBatch();
        };
      }
      list.appendChild(div);
    });
    if (!list.children.length) {
      var filtered = kw || b.category !== '全部';
      kmmRenderEmpty(list, filtered ? '没有匹配的模型' : b.catalogLoaded ? '没有可显示的模型' : '从供应商获取模型',
        filtered ? '换个关键词或切换分类试试。' : b.catalogLoaded ? '上游未返回模型，可以重试或手动添加。' : '选择供应商，点击「获取模型列表」；也可以手动填写模型 ID。');
    }
    list.scrollTop = scrollTop;
  }
  function kmmProbeCatalog() {
    var b = kmmBatch;
    if (!b || b.loading || b.saving || b.reloading || b.conflict || !kmmManagerWritable()) return;
    b.loading = true; b.error = ''; kmmRenderManagerUI();
    kapiFetch(API_BASE + '/api/model-manager/catalog', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: b.version, provider: b.provider }) })
      .then(function(r) { return r.json().then(function(data) { return { status: r.status, ok: r.ok, data: data }; }); })
      .then(function(result) {
        if (kmmBatch !== b || !isModelModalOpen) return;
        var data = result.data || {};
        if (result.status === 409 || data.code === 'CONFIG_CONFLICT') { b.conflict = true; b.error = '配置已变化，当前选择仍保留，请载入最新配置。'; return; }
        if (!result.ok || data.success !== true) { b.error = kmmSafeMessage(data.message) || '获取失败，可重试或手动添加。'; return; }
        if (data.version !== b.version || data.provider !== b.provider || !Array.isArray(data.models)
            || data.models.some(function(m) { return !m || typeof m.id !== 'string' || !m.id.trim() || typeof m.display_name !== 'string'; })) {
          b.error = '返回的模型列表异常，当前选择仍保留。'; return;
        }
        b.rows.forEach(function(row) { row.catalog = false; });
        var seen = [];
        data.models.forEach(function(m) {
          if (seen.indexOf(m.id) >= 0) return;
          seen.push(m.id);
          var matching = b.rows.filter(function(row) { return row.value.model === m.id; });
          if (matching.length) { matching.forEach(function(row) { row.catalog = true; }); return; }
          var value = kmmModelValue(null, b.provider, m.id, m.display_name);
          value.alias = kmmUniqueAlias(b, m.id);
          b.rows.push({ original: null, value: value, selected: false, catalog: true });
        });
        b.catalogLoaded = true; b.message = '获取到 ' + seen.length + ' 个模型。勾选后点击底部添加。';
      }).catch(function() {
        if (kmmBatch === b && isModelModalOpen) b.error = '获取失败，当前选择仍保留；可以重试或手动添加。';
      }).then(function() {
        if (kmmBatch !== b || !isModelModalOpen) return;
        b.loading = false; kmmRenderBatchRows(); kmmRenderManagerUI();
      });
  }
  async function kmmReloadBatch() {
    var b = kmmBatch;
    if (!b || b.saving || b.reloading) return;
    if (kmmBatchChanged() && !await kmmConfirm('载入最新配置后需要重新选择模型。当前尚未添加任何模型。', '载入最新配置', '重新选择模型？', '保留选择')) return;
    if (kmmBatch !== b) return;
    b.reloading = true; kmmRenderManagerUI();
    var data = await kmmFetchManager();
    if (kmmBatch !== b || !isModelModalOpen) return;
    b.reloading = false;
    if (!data) { b.error = '载入失败，当前选择仍保留。'; kmmRenderManagerUI(); return; }
    var provider = data.providers.some(function(p) { return p.name === b.provider; }) ? b.provider : data.providers.length ? data.providers[0].name : '';
    kmmCreateBatch(provider); kmmRenderManagerUI(); kmmRenderBatchRows();
  }
  async function kmmSaveBatch() {
    var b = kmmBatch;
    if (!b || kmmDraft || b.loading || b.saving || b.reloading || b.conflict || !kmmManagerWritable()) return;
    var operations = kmmBatchOperations(b), error = '', aliases = [];
    b.rows.filter(function(row) { return !row.original && row.selected; }).forEach(function(row) {
      error = error || kmmModelError(row.value);
      var alias = row.value.alias.trim();
      if (aliases.indexOf(alias) >= 0 || b.aliases.indexOf(alias) >= 0) error = error || '模型别名已存在：' + alias;
      aliases.push(alias);
    });
    if (error) { b.error = error; kmmRenderBatch(); return; }
    if (!operations.added) return;
    b.saving = true; b.error = ''; kmmRenderManagerUI();
    try {
      var response = await kapiFetch(API_BASE + '/api/model-manager/batch', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: b.version, provider: b.provider, upserts: operations.upserts, removes: [] }) });
      var data = await response.json();
      if (kmmBatch !== b) return;
      if (response.status === 409 || data.code === 'CONFIG_CONFLICT') { b.conflict = true; b.error = '配置已变化，当前选择仍保留。请载入最新配置后重新选择。'; }
      else if (response.ok && data.success === true) {
        kmmClearBatch(); kmmToast('已添加 ' + operations.added + ' 个模型，请在会话中 /reload 加载');
        kmmQueries.models = ''; kmmEl('kmm-modal-filter').value = '';
        await kmmRefreshAll(); kmmRenderModalCards(); kmmRestoreFocus();
        var card = Array.from(kmmEl('kmm-modal-cards').querySelectorAll('[data-model-alias]')).find(function(el) { return aliases.indexOf(el.getAttribute('data-model-alias')) >= 0; });
        if (card) { card.scrollIntoView({ block: 'nearest' }); card.animate([{ outline: '2px solid var(--color-accent, #1a88ff)' }, { outline: '2px solid transparent' }], { duration: 2200 }); }
      } else b.error = kmmSafeMessage(data.message) || '添加失败，当前选择仍保留。';
    } catch (e) { b.error = '未能确认添加结果。选择仍保留，请载入最新配置后核对。'; }
    finally { b.saving = false; kmmRenderManagerUI(); }
  }
  function kmmProviderDeleteReason(p) {
    if (!p) return '供应商不存在，请刷新后重试。';
    if (p.managed) return '托管/OAuth 供应商由官方管理，不能删除。';
    if (p.delete_protected) return p.delete_reason || '此供应商不能删除，请刷新后重试。';
    var count = kmmManagerData.models.filter(function(m) { return m.provider === p.name; }).length;
    return count ? '仍有 ' + count + ' 个模型引用此供应商，请先删除或迁移这些模型。' : '';
  }
  async function kmmDeleteProvider(name) {
    if (kmmBusy() || !kmmEl('kmm-detail').hidden || !kmmManagerWritable()) return;
    var operation = { provider: name };
    kmmDeleting = operation;
    kmmRenderManagerUI(); kmmRenderProviders();
    try {
      await Promise.resolve(kmmManagerRequest);
      var snapshot = await kmmFetchManager();
      if (kmmDeleting !== operation || kmmDraft || kmmBatch || !isModelModalOpen) return;
      if (!snapshot || !snapshot.editable) { kmmToast(kmmManagerMessage || '当前只读，不能删除。', true); return; }
      var provider = snapshot.providers.find(function(p) { return p.name === name; });
      var reason = kmmProviderDeleteReason(provider);
      if (reason) { kmmToast(reason, true); return; }
      if (!await kmmConfirm('将删除供应商「' + name + '」及其本地连接和认证配置，此操作不能撤销。不会删除系统环境变量。', '删除供应商', '删除此供应商？')) return;
      var response = await kapiFetch(API_BASE + '/api/model-manager/provider/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: snapshot.version, name: name }) });
      var data = await response.json();
      if (response.status === 409 || data.code === 'CONFIG_CONFLICT') {
        kmmToast(kmmSafeMessage(data.message) || '配置已变化，删除未执行；请刷新后重新确认，不会强制覆盖。', true);
        await kmmFetchManager();
      } else if (response.ok && data.success === true && typeof data.version === 'string') {
        kmmToast('供应商已删除并原子保存，会话中 /reload 生效');
        await kmmRefreshAll();
      } else {
        kmmToast(kmmSafeMessage(data.message) || '删除失败，配置未保存。', true);
        if (response.status === 503) { kmmManagerOnline = false; kmmManagerMessage = '配置写入不可用，请刷新后重试。'; }
      }
    } catch (e) {
      kmmToast('删除失败：后台服务未响应或返回异常，请刷新确认配置状态后重试。', true);
      kmmManagerOnline = false; kmmManagerMessage = '后台服务不可用，不能保存；请刷新后重试。';
    } finally {
      if (kmmDeleting === operation) kmmDeleting = null;
      kmmRenderManagerUI(); kmmRenderModalCards(); kmmRenderProviders();
    }
  }
  function kmmRenderProviders() {
    var container = kmmEl('kmm-provider-cards');
    if (!container || kmmDraft || kmmBatch || kmmManagerTab !== 'providers') return;
    container.innerHTML = '';
    if (!kmmManagerData) {
      kmmRenderEmpty(container, kmmManagerRequest ? '正在读取供应商…' : '暂时无法读取供应商', '请稍候，或重新连接管理服务。');
      return;
    }
    var kw = kmmQueries.providers.trim().toLowerCase();
    var providers = kmmManagerData.providers, busy = kmmBusy();
    providers.forEach(function(p) {
      if (kw && (p.name + ' ' + p.type + ' ' + p.base_url).toLowerCase().indexOf(kw) < 0) return;
      var card = document.createElement('div'); card.className = 'kmm-card';
      var deleteReason = kmmProviderDeleteReason(p);
      card.innerHTML = '<div class="kmm-top"><div class="kmm-name kmm-provider-name"><strong>' + esc(p.name) + '</strong>'
        + (p.managed ? '<span class="kmm-tag def">托管 · 只读</span>' : '') + '</div>'
        + '<div class="kmm-acts"><button class="kmm-link accent" data-kmm-provider-edit>' + (p.managed ? '查看' : '编辑') + '</button>'
        + '<button class="kmm-link" data-kmm-provider-add>添加模型</button>'
        + '<details class="kmm-item-menu"><summary aria-label="' + esc(p.name + ' 的更多操作') + '">⋯</summary><div class="kmm-menu">'
        + '<button class="kmm-link danger" data-kmm-provider-delete' + (deleteReason ? ' disabled' : '') + '>删除供应商</button>'
        + (deleteReason ? '<span class="kmm-menu-reason">' + esc(deleteReason) + '</span>' : '') + '</div></details></div></div>'
        + '<div class="kmm-meta" title="' + esc(p.base_url || '默认地址') + '">' + esc(p.base_url || '使用默认 API 地址') + '</div>'
        + '<div class="kmm-summary"><span>' + esc(p.type) + '</span><span>密钥' + (p.has_api_key ? '已配置' : '未配置') + '</span><span>'
        + kmmManagerData.models.filter(function(m) { return m.provider === p.name; }).length + ' 个模型</span></div>';
      var edit = card.querySelector('[data-kmm-provider-edit]');
      edit.disabled = busy || !kmmManagerOnline;
      edit.onclick = function() { if (!kmmBusy()) kmmOpenEditor('provider', p.name); };
      card.querySelector('[data-kmm-provider-add]').disabled = busy || !kmmManagerWritable();
      card.querySelector('[data-kmm-provider-add]').onclick = function() { if (!kmmBusy()) kmmOpenBatch(p.name); };
      var remove = card.querySelector('[data-kmm-provider-delete]');
      remove.disabled = !!deleteReason || busy || !kmmManagerWritable();
      remove.onclick = function() {
        card.querySelector('.kmm-item-menu').open = false;
        kmmDeleteProvider(p.name);
      };
      container.appendChild(card);
    });
    if (!container.children.length) kmmRenderEmpty(container, kw ? '没有匹配的供应商' : '还没有供应商', kw ? '试试供应商名称、协议或 API 地址。' : '点击「新增供应商」建立连接，然后添加模型。');
  }
  async function kmmOpenEditor(kind, original) {
    if (!kmmManagerOnline || !kmmManagerData || !await kmmDiscardAll()) return;
    if (original === null && !kmmManagerWritable()) return;
    if (kind === 'model' && original === null && !kmmManagerData.providers.length) {
      kmmManagerTab = 'providers'; kmmRenderManagerUI(); kmmRenderProviders();
      kmmToast('请先新增供应商，再新增模型'); return;
    }
    var item = original === null ? null : (kind === 'provider' ? kmmManagerData.providers : kmmManagerData.models).find(function(x) {
      return (kind === 'provider' ? x.name : x.alias) === original;
    });
    if (original !== null && !item) { kmmToast('配置已变化，请刷新后重新编辑', true); return; }
    var value = kind === 'provider'
      ? { name: item ? item.name : '', type: item ? item.type : (kmmManagerData.provider_types[0] || ''), base_url: item ? item.base_url : '', key_action: 'keep', api_key: '' }
      : kmmModelValue(item, kmmManagerData.providers.length ? kmmManagerData.providers[0].name : '', '', '');
    if (kind === 'model') value.set_default = false;
    kmmDraft = { kind: kind, original: original, version: kmmManagerData.version, managed: !!(item && item.managed), hasKey: !!(item && item.has_api_key),
      value: value, initial: JSON.stringify(value), saving: false, conflict: false, error: '',
      providers: kmmManagerData.providers.map(function(p) { return p.name; }), providerTypes: kmmManagerData.provider_types.slice(),
      effortLevels: kmmStrings(kmmDefaultEfforts.concat(kmmManagerData.effort_levels)) };
    kmmManagerTab = kind === 'provider' ? 'providers' : 'models';
    kmmRenderEditor(); kmmRenderManagerUI();
  }
  function kmmEditorOptions(values, selected, emptyLabel) {
    var choices = kmmStrings(values);
    if (selected && choices.indexOf(selected) < 0) choices.push(selected);
    return (emptyLabel ? '<option value="">' + esc(emptyLabel) + '</option>' : '') + choices.map(function(v) {
      return '<option value="' + esc(v) + '"' + (v === selected ? ' selected' : '') + '>' + esc(v) + '</option>';
    }).join('');
  }
  function kmmRenderEditor() {
    var d = kmmDraft, editor = kmmEl('kmm-editor');
    if (!d || !editor) return;
    var v = d.value;
    var field = function(label, name, type, fixed, id) {
      return '<label class="kmm-field">' + esc(label) + '<input id="' + (id || 'kmm-editor-' + name.replace(/_/g, '-')) + '" data-kmm-field="' + name
        + '" type="' + (type || 'text') + '" value="' + esc(v[name]) + '"' + (fixed ? ' disabled' : '')
        + (type === 'number' ? ' min="1" step="1" required' : '') + '></label>';
    };
    var select = function(label, name, values, fixed, emptyLabel, id) {
      return '<label class="kmm-field">' + esc(label) + '<select id="' + (id || 'kmm-editor-' + name.replace(/_/g, '-')) + '" data-kmm-field="' + name + '"'
        + (fixed ? ' disabled' : '') + '>' + kmmEditorOptions(values, v[name], emptyLabel) + '</select></label>';
    };
    var html = d.original ? '<h3>' + esc(d.original) + '</h3>' : '';
    if (d.kind === 'provider') {
      html += '<div id="kmm-editor-note">' + (d.managed ? '此供应商由系统管理，仅可查看。' : '已有密钥不回显；不更换或移除就会保留。') + '</div>'
        + '<section class="kmm-section"><h4>连接信息</h4><div class="kmm-editor-grid">'
        + field('供应商名称', 'name', 'text', d.original !== null)
        + select('接口协议', 'type', d.providerTypes, d.managed)
        + '<div class="kmm-field-wide">' + field('API 地址（留空使用默认地址）', 'base_url', 'text', d.managed) + '</div></div></section>'
        + '<section class="kmm-section"><h4>认证信息</h4><div class="kmm-field">API 密钥 · ' + (d.hasKey ? '已配置' : '未配置') + '<div class="kmm-key-actions">'
        + [{ value: 'keep', label: '保留现有' }, { value: 'replace', label: d.hasKey ? '更换密钥' : '填写密钥' }, { value: 'clear', label: '移除密钥' }].map(function(action) {
          return '<label><input type="radio" name="kmm-key-action" data-kmm-key-action value="' + action.value + '"' + (v.key_action === action.value ? ' checked' : '') + '>' + action.label + '</label>';
        }).join('') + '</div></div>'
        + '<label class="kmm-field" id="kmm-provider-key-field" hidden>新 API 密钥<input id="kmm-provider-api-key" data-kmm-field="api_key" type="password" autocomplete="new-password" spellcheck="false"></label>'
        + '<div class="kmm-note" id="kmm-provider-key-note"></div></section>';
    } else {
      html += '<div id="kmm-editor-note">能力与思考档位仅为本地声明，请按上游实际支持配置。已有别名不可修改。</div>'
        + '<section class="kmm-section"><h4>模型信息</h4><div class="kmm-editor-grid">'
        + field('显示名称（可留空）', 'display_name')
        + select('供应商', 'provider', d.providers, d.managed)
        + field('上游模型 ID', 'model', 'text', d.managed)
        + field('本地别名（自动加供应商前缀）', 'alias', 'text', d.original !== null) + '</div></section>'
        + '<section class="kmm-section"><h4>能力与推理</h4><div class="kmm-editor-grid">'
        + '<div class="kmm-field">' + field('上下文长度（Token）', 'max_context_size', 'number')
        + '<div class="kmm-context-presets"><button type="button" class="kmm-tbtn" data-kmm-context="256000">256k</button>'
        + '<button type="button" class="kmm-tbtn" data-kmm-context="1000000">1M</button></div></div>'
        + select('默认思考强度', 'default_effort', kmmOrderedEfforts(v.support_efforts), false, '不指定') + '</div>';
      var caps = kmmStrings(['image_in', 'thinking', 'tool_use'].concat(v.capabilities));
      var capLabels = { image_in: '识图', thinking: '思考', tool_use: '工具调用' };
      html += '<div class="kmm-editor-checks" role="group" aria-label="模型能力">' + caps.map(function(cap) {
        return '<label><input type="checkbox" data-kmm-cap="' + esc(cap) + '"' + (v.capabilities.indexOf(cap) >= 0 ? ' checked' : '') + '>' + esc(capLabels[cap] || cap) + '</label>';
      }).join('') + '</div></section><details><summary>高级设置 · 支持的思考档位</summary><div class="kmm-editor-checks">'
        + kmmOrderedEfforts(d.effortLevels.concat(v.support_efforts)).map(function(level) {
          return '<label><input type="checkbox" data-kmm-effort="' + esc(level) + '"' + (v.support_efforts.indexOf(level) >= 0 ? ' checked' : '') + '>' + esc(level) + '</label>';
        }).join('') + '</div></details>'
        + '<label class="kmm-toggle"><input id="kmm-editor-set-default" type="checkbox" data-kmm-field="set_default"' + (v.set_default ? ' checked' : '') + '><span>保存后设为默认模型</span><small>不切换当前对话</small></label>';
      if (d.original !== null) html += '<button type="button" class="kmm-link accent kmm-inline-action" id="kmm-editor-pricing">计价设置 →</button>';
    }
    editor.innerHTML = html + '<div id="kmm-editor-status" role="status" aria-live="polite"></div><div id="kmm-editor-error" role="alert"></div>'
      + '<div class="kmm-editor-actions"><button type="button" class="kmm-link" id="kmm-editor-reload" hidden>载入最新配置</button>'
      + '<button type="button" class="kmm-tbtn" id="kmm-editor-cancel">取消</button><button type="submit" class="kmm-tbtn primary" id="kmm-editor-save">保存</button></div>';
    var suggestAlias = function() { return v.model.trim() ? kmmNormalizeAlias(v.model.trim(), v.provider) : ''; };
    editor.querySelectorAll('[data-kmm-key-action]').forEach(function(input) {
      input.onchange = function() {
        if (d.saving || input.disabled) return;
        v.key_action = input.value;
        if (v.key_action !== 'replace') { v.api_key = ''; kmmEl('kmm-provider-api-key').value = ''; }
        kmmUpdateEditorState();
      };
    });
    if (kmmEl('kmm-editor-pricing')) kmmEl('kmm-editor-pricing').onclick = function() { window.__KMM_PRICE(d.original); };
    if (d.kind === 'model' && d.original === null) d.aliasSuggested = kmmNormalizeAlias(v.model, v.provider);
    editor.querySelectorAll('[data-kmm-field]').forEach(function(input) {
      var update = function() {
        if (!kmmDraft || kmmDraft !== d || d.saving || kmmEditorReloading || input.disabled) return;
        var name = input.getAttribute('data-kmm-field'), previousProvider = v.provider;
        var autoAlias = !v.alias || !v.alias.trim() || kmmNormalizeAlias(v.alias, v.provider) === d.aliasSuggested;
        v[name] = input.type === 'checkbox' ? input.checked : name === 'default_effort' ? (input.value || null) : input.value;
        if (d.kind === 'model' && d.original === null) {
          if (name === 'model' && autoAlias) { d.aliasSuggested = suggestAlias(); v.alias = d.aliasSuggested; }
          if (name === 'provider') {
            v.alias = kmmNormalizeAlias(v.alias, v.provider, previousProvider);
            d.aliasSuggested = kmmNormalizeAlias(d.aliasSuggested, v.provider, previousProvider);
            if (autoAlias) { d.aliasSuggested = suggestAlias(); v.alias = d.aliasSuggested; }
          }
          if (name === 'model' || name === 'provider') kmmEl('kmm-editor-alias').value = v.alias;
        }
        if (name === 'key_action' && v.key_action !== 'replace') { v.api_key = ''; kmmEl('kmm-provider-api-key').value = ''; }
        kmmUpdateEditorState();
      };
      input.addEventListener('input', update); input.addEventListener('change', update);
      if (d.kind === 'model' && d.original === null && input.getAttribute('data-kmm-field') === 'alias') {
        input.addEventListener('blur', function() {
          if (kmmDraft !== d || d.saving || kmmEditorReloading || input.disabled) return;
          v.alias = kmmNormalizeAlias(input.value, v.provider);
          if (!v.alias && v.model.trim()) { d.aliasSuggested = suggestAlias(); v.alias = d.aliasSuggested; }
          input.value = v.alias; kmmUpdateEditorState();
        });
      }
    });
    editor.querySelectorAll('[data-kmm-context]').forEach(function(button) {
      button.onclick = function() {
        if (kmmDraft !== d || d.saving || kmmEditorReloading || !kmmManagerWritable()) return;
        v.max_context_size = button.getAttribute('data-kmm-context'); kmmEl('kmm-editor-max-context-size').value = v.max_context_size; kmmUpdateEditorState();
      };
    });
    editor.querySelectorAll('[data-kmm-cap], [data-kmm-effort]').forEach(function(input) {
      input.onchange = function() {
        if (kmmDraft !== d || d.saving || kmmEditorReloading || input.disabled) return;
        var cap = input.hasAttribute('data-kmm-cap');
        var selector = cap ? '[data-kmm-cap]' : '[data-kmm-effort]';
        v[cap ? 'capabilities' : 'support_efforts'] = Array.from(editor.querySelectorAll(selector)).filter(function(el) { return el.checked; })
          .map(function(el) { return el.getAttribute(cap ? 'data-kmm-cap' : 'data-kmm-effort'); });
        if (!cap) {
          if (v.support_efforts.indexOf(v.default_effort) < 0) v.default_effort = null;
          kmmEl('kmm-editor-default-effort').innerHTML = kmmEditorOptions(v.support_efforts, v.default_effort, '不指定');
        }
        kmmUpdateEditorState();
      };
    });
    editor.onsubmit = function(e) { e.preventDefault(); kmmSaveEditor(); };
    kmmEl('kmm-editor-cancel').onclick = async function() { await kmmDiscardAll(); };
    kmmEl('kmm-editor-reload').onclick = kmmReloadEditor;
    kmmUpdateEditorState();
    kmmRenderManagerUI(); kmmFocusDetail();
  }
  function kmmUpdateEditorState() {
    var d = kmmDraft;
    if (!d) return;
    var readonly = d.kind === 'provider' && d.managed, busy = d.saving || kmmEditorReloading;
    var unavailable = !kmmManagerWritable(), conflict = d.conflict;
    kmmEl('kmm-editor-save').hidden = readonly;
    kmmEl('kmm-editor-save').disabled = busy || readonly || unavailable || conflict || (d.original !== null && !kmmDraftChanged());
    kmmEl('kmm-editor-save').textContent = d.saving ? '保存中…' : kmmReturnToAdd ? '保存并继续添加模型' : '保存';
    kmmEl('kmm-editor-cancel').disabled = busy;
    kmmEl('kmm-editor-reload').hidden = !conflict && (!kmmManagerData || kmmManagerData.version === d.version);
    kmmEl('kmm-editor-reload').disabled = busy;
    kmmEl('kmm-editor-error').textContent = d.error;
    kmmEl('kmm-editor-status').textContent = busy ? '正在保存…' : readonly ? '托管供应商仅可查看。'
      : unavailable ? (kmmManagerMessage || '当前无法保存。') : conflict ? '配置已被其他操作修改，输入已保留。载入最新配置前会再次确认。'
      : '保存后，在会话中执行 /reload 加载。';
    kmmEl('kmm-editor').querySelectorAll('input, select').forEach(function(input) {
      var name = input.getAttribute('data-kmm-field');
      var fixed = readonly || (d.original !== null && (name === 'name' || name === 'alias'))
        || (d.kind === 'model' && d.managed && (name === 'provider' || name === 'model'));
      input.disabled = busy || fixed || unavailable;
    });
    kmmEl('kmm-editor').querySelectorAll('[data-kmm-context]').forEach(function(button) { button.disabled = busy || unavailable; });
    if (d.kind === 'provider') {
      kmmEl('kmm-provider-key-field').hidden = d.value.key_action !== 'replace';
      kmmEl('kmm-provider-api-key').disabled = busy || readonly || unavailable || d.value.key_action !== 'replace';
      kmmEl('kmm-provider-key-note').textContent = d.value.key_action === 'clear' ? '保存后将移除密钥，使用此供应商的模型可能无法调用。'
        : d.value.key_action === 'replace' ? '只保存本次填写的新密钥，已有密钥不会展示。' : '现有认证信息保持不变。';
    }
    kmmUpdateDirty();
  }
  async function kmmReloadEditor() {
    var d = kmmDraft;
    if (!d || d.saving || kmmEditorReloading) return;
    if (kmmDraftChanged() && !await kmmConfirm('载入最新配置会放弃当前未保存的修改，包括新填写的密钥。', '载入最新配置', '放弃当前修改？', '继续编辑')) return;
    if (kmmDraft !== d) return;
    kmmEditorReloading = true; kmmRenderManagerUI();
    var data = await kmmFetchManager(); kmmEditorReloading = false;
    if (kmmDraft !== d || !isModelModalOpen) return;
    if (!data) { d.error = '未能载入，当前输入仍保留。'; kmmRenderManagerUI(); return; }
    var kind = d.kind, original = d.original;
    kmmClearEditor(); await kmmOpenEditor(kind, original); kmmRenderManagerUI();
  }
  async function kmmSaveEditor() {
    var d = kmmDraft;
    if (!d || kmmConfirmResolve || d.saving || kmmEditorReloading || d.conflict || !kmmManagerWritable() || (d.kind === 'provider' && d.managed)) return;
    var v = d.value, body = { version: d.version }, error = '';
    if (d.kind === 'provider') {
      if (!v.name.trim() || !v.type) error = '请输入供应商名称并选择协议类型。';
      else if (v.key_action === 'replace' && !v.api_key.trim()) error = 'replace 必须输入新的 API key。';
      body.original_name = d.original;
      body.provider = { name: d.original === null ? v.name.trim() : d.original, type: v.type, base_url: v.base_url.trim(), key_action: v.key_action };
      if (v.key_action === 'replace') body.provider.api_key = v.api_key;
    } else {
      if (d.original === null) {
        v.alias = kmmNormalizeAlias(v.alias || v.model, v.provider);
        kmmEl('kmm-editor-alias').value = v.alias;
      }
      error = kmmModelError(v);
      if (d.original === null && kmmManagerData.models.some(function(m) { return m.alias === v.alias; })) {
        error = error || '模型别名已存在，请换一个唯一别名。';
      }
      body.original_alias = d.original;
      body.model = kmmModelPayload(v, d.original);
      if (!d.managed) body.model.model = v.model.trim();
      if (v.set_default) body.set_default = true;
    }
    if (error) { d.error = error; kmmUpdateEditorState(); return; }
    if (d.kind === 'provider' && v.key_action === 'clear' && !await kmmConfirm('移除密钥后，此供应商的模型可能无法调用。', '保存并移除密钥', '移除 API 密钥？')) return;
    if (kmmDraft !== d) return;
    d.error = ''; d.saving = true; kmmRenderManagerUI();
    try {
      var response = await kapiFetch(API_BASE + '/api/model-manager/' + d.kind, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      var data = await response.json();
      if (kmmDraft !== d) return;
      if (response.status === 409 || data.code === 'CONFIG_CONFLICT') {
        d.conflict = true; d.error = kmmSafeMessage(data.message) || '配置已变化，输入仍保留。';
      } else if (response.ok && data.success === true) {
        var continueAdd = kmmReturnToAdd, provider = v.name;
        kmmReturnToAdd = false; kmmClearEditor();
        kmmToast('配置已保存，请在会话中 /reload 加载');
        await kmmRefreshAll();
        if (continueAdd && isModelModalOpen) await kmmOpenBatch(provider);
        else kmmRestoreFocus();
      } else d.error = kmmSafeMessage(data.message) || '保存失败，输入仍保留。';
    } catch (e) {
      if (kmmDraft === d) d.error = '未能确认保存结果。输入已保留，请重新连接并载入最新配置后核对。';
    } finally {
      if (body.provider) body.provider.api_key = '';
      if (kmmDraft === d) d.saving = false;
      kmmRenderManagerUI();
      if (!kmmDraft) { kmmRenderModalCards(); kmmRenderProviders(); }
    }
  }
  setInterval(function() {
    if (!document.hidden && isModelModalOpen && !(kmmDraft && kmmDraft.saving)
        && !(kmmBatch && (kmmBatch.saving || kmmBatch.reloading)) && !kmmEditorReloading) kmmFetchManager();
  }, 15000);

  function kmmToast(msg, isErr) {
    var toast = document.getElementById('kmm-toast');
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'kmm-toast';
      document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.style.background = isErr ? 'var(--color-danger, #f85149)' : 'var(--color-success, #3fb950)';
    toast.style.display = 'block';
    setTimeout(function() { toast.style.display = 'none'; }, 3000);
  }

  /* ================================================================
   * 模式滑杆（kmd- 前缀）：从单模型省钱档到「主模型 + 1~3 个挂件子代理」
   * ================================================================ */
  var kmdData = null;
  var kmdBusy = false;
  var kmdOverlayEl = null;
  var kmdDraft = null;
  var kmdLastFetch = 0;
  var kmdLastSid = null;
  var kmdLastScope = null;

  function kmdModelName(alias) {
    var m = kmdData && (kmdData.models || []).find(function(x) { return x.alias === alias; });
    return (m && m.display_name && m.display_name !== alias) ? m.display_name : shortName(alias);
  }
  function kmdPriceTxt(alias) {
    var m = kmdData && (kmdData.models || []).find(function(x) { return x.alias === alias; });
    var p = m && m.pricing;
    if (!p) return '';
    if (p.mode === 'per_call') return '¥' + p.price + '/次';
    if (p.input != null) return p.input + '/' + p.output;
    return '';
  }
  function kmdModeDesc(m) {
    if (!m) return '';
    var s = '主 <b>' + esc(kmdModelName(m.main)) + '</b>';
    var pt = kmdPriceTxt(m.main);
    if (pt) s += ' <span title="输入/输出 每百万 Token">(' + esc(pt) + ')</span>';
    var subs = m.subagents || [];
    if (!subs.length) s += ' · 单干，不派子代理';
    else s += ' · 挂件 ' + subs.length + '：' + subs.map(function(a) { return esc(kmdModelName(a)); }).join('、');
    if (m.scene) s += '<br>' + esc(m.scene);
    return s;
  }

  function kmdScope() {
    return (kmdData && kmdData.scope === 'session') ? 'session' : 'global';
  }
  function kmdActiveSessionId() {
    try {
      var app = document.querySelector('#app').__vue_app__;
      var pinia = app.config.globalProperties.$pinia;
      var sessions = pinia._s.get('kimi.sessions');
      var sid = sessions && sessions.activeSessionId;
      if (typeof sid === 'string' && /^session_[A-Za-z0-9_-]{8,100}$/.test(sid)) return sid;
    } catch (e) { /* no active session */ }
    return '';
  }
  function kmdShownIndex() {
    var modes = (kmdData && kmdData.modes) || [];
    if (kmdScope() === 'session' && typeof kmdData.session_active === 'number'
        && kmdData.session_active >= 0 && kmdData.session_active < modes.length) {
      return kmdData.session_active;
    }
    return kmdData.active;
  }

  function kmdRenderScope(card) {
    var wrap = card.querySelector('#ku-mode-scope');
    if (!wrap) return;
    var scope = kmdScope();
    wrap.querySelectorAll('button').forEach(function(b) {
      b.classList.toggle('on', b.getAttribute('data-scope') === scope);
      b.disabled = kmdBusy || (b.getAttribute('data-scope') === 'session' && !kmdActiveSessionId());
      b.title = b.getAttribute('data-scope') === 'session'
        ? (kmdActiveSessionId() ? '只改当前对话，其它对话不受影响' : '当前没有打开的对话')
        : '改全局默认：所有对话一起变';
    });
  }

  function kmdRender(card) {
    card = card || document.getElementById(CARD_ID);
    if (!card) return;
    var enabled = kupWidgetsEnabled();
    var row = card.querySelector('#ku-mode-row');
    if (row) row.style.display = enabled ? '' : 'none';
    if (!enabled) { kmdRenderLock(card); return; }
    if (!kmdData) return;
    var modes = kmdData.modes || [];
    var range = card.querySelector('#ku-mode-range');
    var nameEl = card.querySelector('#ku-mode-name');
    var desc = card.querySelector('#ku-mode-desc');
    var ticks = card.querySelector('#ku-mode-ticks');
    if (!range || !modes.length) return;
    range.max = String(modes.length - 1);
    var act = kmdShownIndex();
    // act==null（编辑后当前 config 不再匹配任何档）时，滑块别停在失效
    // 位置：优先回到「最近一次应用」的档（last_applied.index），那才是
    // 用户最后明确选过的档位；都没有再退回 range.value。
    var fallback = (kmdData.last_applied && typeof kmdData.last_applied.index === 'number'
                    && kmdData.last_applied.index < modes.length)
                   ? kmdData.last_applied.index : Math.round(Number(range.value || 0));
    var shown = (act == null) ? fallback : act;
    if (!kmdBusy && !kmdDragging && document.activeElement !== range) range.value = String(shown);
    ticks.innerHTML = modes.map(function(m, i) {
      // 高亮跟滑块实际位置（shown）走：act==null 时 shown 是 last_applied，
      // 那档也该高亮，避免「滑到某档却没选中态」的割裂。
      return '<span data-i="' + i + '" class="' + (i === shown ? 'on' : '') + '" title="' + esc(m.name) + '">' + esc(m.name) + '</span>';
    }).join('');
    ticks.querySelectorAll('span').forEach(function(sp) {
      sp.onclick = function(e) { e.stopPropagation(); kmdApply(Number(sp.getAttribute('data-i'))); };
    });
    if (act == null) {
      var fb = modes[shown];
      nameEl.textContent = (fb ? ((shown + 1) + '/' + modes.length + ' · ' + fb.name) : '自定义')
        + '（配置已改动·未匹配）';
      var c = kmdData.current || {};
      desc.innerHTML = '当前主 <b>' + esc(kmdModelName(c.default_model)) + '</b> · 子代理池 '
        + (c.agents_disabled ? '已禁用' : ((c.pool || []).length + ' 个'))
        + (kmdData.has_snapshot ? ' · <span class="warn">可在「编辑」里恢复切换前配置</span>' : '');
    } else {
      nameEl.textContent = (act + 1) + '/' + modes.length + ' · ' + modes[act].name;
      desc.innerHTML = kmdModeDesc(modes[act]);
    }
    var policy = kmdScope() === 'session' ? kmdData.session_policy : kmdData.global_mode;
    if (policy && act == null) {
      nameEl.textContent = policy.name + '（已应用快照）';
      desc.innerHTML = kmdModeDesc(policy);
    }
    if (kmdScope() === 'session') {
      if (!kmdActiveSessionId()) {
        desc.innerHTML = '<span class="warn">未打开对话：本对话档位不可用，先打开一个对话。</span>';
      } else {
        var note = document.createElement('div');
        note.className = 'ku-scope-note';
        note.style.cssText = 'color:var(--color-text-faint,#94a3b8);font-size:10px;margin-top:2px;';
        note.textContent = '选档自动同步挂件目录；实际可派范围按本对话白名单限制，其它对话主模型不变。';
        desc.appendChild(note);
        var missing = kmdData.session_unavailable_subagents || [];
        if (missing.length || (kmdData.current && kmdData.current.agents_disabled)) {
          var warning = document.createElement('div');
          warning.className = 'warn';
          warning.textContent = missing.length ? '目录待同步：' + missing.join('、') + '。重新点击当前档位自动修复。' : '宿主已全局禁用子代理工具。';
          desc.appendChild(warning);
        }
      }
    }
    if (kmdData.session_independent && kmdData.session_id === kmdActiveSessionId()) {
      var tip = document.createElement('div');
      tip.className = 'warn';
      tip.textContent = '本对话使用独立档位' + (kmdData.session_lock ? '（已锁定）' : '') + '，全局切档不会覆盖它。';
      var follow = document.createElement('button');
      follow.type = 'button'; follow.className = 'kmm-tbtn';
      follow.textContent = '本对话跟随全局'; follow.disabled = kmdBusy;
      follow.onclick = function(e) { e.stopPropagation(); kmdFollowGlobal(); };
      desc.appendChild(tip); desc.appendChild(follow);
    }
    kmdRenderScope(card);
    kmdRenderLock(card);
  }

  var kmdDragging = false;
  var kmdSnapRaf = 0;

  function kmdPreview(card, i) {
    if (!kmdData) return;
    var modes = kmdData.modes || [];
    var m = modes[Math.round(i)];
    if (!m) return;
    var dragging = kmdDragging ? '（松手切换）' : '';
    card.querySelector('#ku-mode-name').textContent = (Math.round(i) + 1) + '/' + modes.length + ' · ' + m.name + dragging;
    card.querySelector('#ku-mode-desc').innerHTML = kmdModeDesc(m);
    var ticks = card.querySelector('#ku-mode-ticks');
    if (ticks) ticks.querySelectorAll('span').forEach(function(sp) {
      sp.classList.toggle('on', Number(sp.getAttribute('data-i')) === Math.round(i));
    });
  }

  function kmdSnapTo(range, target, done) {
    if (kmdSnapRaf) cancelAnimationFrame(kmdSnapRaf);
    var from = Number(range.value), to = Number(target);
    if (from === to) { if (done) done(); return; }
    range.classList.add('ku-snapping');
    var t0 = performance.now(), dur = 170;
    function tick(t) {
      var p = Math.min(1, (t - t0) / dur);
      var e = 1 - Math.pow(1 - p, 3);
      range.value = String(from + (to - from) * e);
      if (p < 1) { kmdSnapRaf = requestAnimationFrame(tick); return; }
      kmdSnapRaf = 0; range.classList.remove('ku-snapping');
      range.value = String(to);
      if (done) done();
    }
    kmdSnapRaf = requestAnimationFrame(tick);
  }

  function kmdFetch(force) {
    var sid = kmdActiveSessionId();
    var scope = kmdScope();
    // 切对话后必须重新取：会话档位与锁状态都跟 session 绑定
    var stale = force || Date.now() - kmdLastFetch >= 15000 || sid !== kmdLastSid || scope !== kmdLastScope;
    if (!stale) { kmdRender(); return Promise.resolve(kmdData); }
    kmdLastFetch = Date.now();
    kmdLastSid = sid;
    kmdLastScope = scope;
    var q = sid ? ('?session_id=' + encodeURIComponent(sid)) : '';
    var epoch = kupSettingsEpoch;
    return kapiFetch(API_BASE + '/api/modes' + q, { cache: 'no-store' })
      .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(d) {
        if (epoch !== kupSettingsEpoch || sid !== kmdActiveSessionId()) return kmdData;
        kmdData = d;
        if ((!kupSettings || d.widgets_enabled === false) && typeof d.widgets_enabled === 'boolean') kupApplySettings(d);
        kmdRender(); kmdHostLockApply(); kmdHostLockWatch();
        if (kupWidgetsEnabled() && kmdOverlayEl && kmdOverlayEl.classList.contains('visible') && !kmdDraft) kmdRenderEditor();
        return d;
      })
      .catch(function() {
        var desc = document.querySelector('#' + CARD_ID + ' #ku-mode-desc');
        if (desc && !kmdData) desc.textContent = '后台服务离线，模式滑杆不可用';
      });
  }

  function kmdPost(path, body) {
    return kapiFetch(API_BASE + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
    }).then(function(r) { return r.json(); });
  }

  function kmdTargetBody(index) {
    var body = { index: index, scope: kmdScope() };
    var sid = kmdActiveSessionId();
    if (sid) body.session_id = sid;
    return body;
  }

  function kmdApply(i) {
    if (kmdBusy || !kmdData || !kupWidgetsEnabled()) return;
    i = Math.round(i);
    var scope = kmdScope(), sid = kmdActiveSessionId(), epoch = kupSettingsEpoch;
    if (scope === 'session' && !sid) { kmmToast('先打开一个对话再选本对话档位', true); return; }
    var target = (kmdData.modes || [])[i];
    if (!target) return;
    var entry = scope === 'session' ? kmdData.session_lock : kmdData.locked;
    var tip = '切换至「' + target.name + '」？';
    if (scope === 'global' && kmdData.session_independent) tip += '\n当前对话仍用独立档位，不会改变。要同步请点“本对话跟随全局”。';
    if (entry && entry.locked) tip += '\n保持锁定，并更新锁定档位，无需手动解锁。';
    if ((scope === 'global' && kmdData.session_independent || entry && entry.locked) && !confirm(tip)) { kmdRender(); return; }
    var card = document.getElementById(CARD_ID);
    var range = card && card.querySelector('#ku-mode-range');
    var prevActive = kmdData.active, prevSession = kmdData.session_active;
    kmdBusy = true;
    if (scope === 'session') kmdData.session_active = i;
    else kmdData.active = i;
    if (range) range.disabled = true;
    if (range && kmdDragging) { kmdDragging = false; kmdSnapTo(range, i, null); }
    else if (range) range.value = String(i);
    kmdRender();
    kmdPost('/api/modes/apply', kmdTargetBody(i)).then(function(d) {
      if (epoch !== kupSettingsEpoch || sid !== kmdActiveSessionId()) return;
      if (d.success) {
        if (d.data) kmdData = d.data;
        kmmToast(d.message || '已切换');
      } else {
        kmdData.active = prevActive; kmdData.session_active = prevSession;
        kmmToast(d.message || '切换失败', true);
      }
    }).catch(function() {
      if (epoch !== kupSettingsEpoch || sid !== kmdActiveSessionId()) return;
      kmdData.active = prevActive; kmdData.session_active = prevSession;
      kmmToast('切换失败：后台服务未响应', true);
    }).finally(function() {
      kmdBusy = false;
      if (range) range.disabled = !kupWidgetsEnabled();
      kmdFetch(true);
      if (typeof fetchModels === 'function') kmmRefreshAll();
    });
  }

  function kmdSetScope(scope) {
    if (!kmdData || kmdBusy || !kupWidgetsEnabled() || scope === kmdScope()) return;
    if (scope === 'session' && !kmdActiveSessionId()) { kmmToast('先打开一个对话', true); return; }
    if (scope === 'global' && kmdData.session_independent && !confirm('仅打开全局设置，不会解除本对话独立档位或锁定。要让当前对话同步，请点击“本对话跟随全局”。')) return;
    var epoch = kupSettingsEpoch, sid = kmdActiveSessionId();
    kmdBusy = true; kmdRender();
    kmdPost('/api/modes/scope', { scope: scope }).then(function(d) {
      if (epoch !== kupSettingsEpoch || sid !== kmdActiveSessionId()) return;
      if (d.success) {
        kmdData.scope = scope;
        kmmToast(d.message || '已切换');
      } else kmmToast(d.message || '切换失败', true);
    }).catch(function() { kmmToast('切换失败：后台服务未响应', true); })
      .finally(function() { kmdBusy = false; kmdFetch(true); });
  }

  /* ---- 档位锁定 ---- */
  var kmdLockBusy = false;
  var kmdHostObs = null;

  function kmdFollowGlobal() {
    var sid = kmdActiveSessionId(), epoch = kupSettingsEpoch;
    if (kmdBusy || !sid || !kupWidgetsEnabled()) return;
    if (!confirm('解除本对话独立档位和锁定，并立即跟随全局？其它独立对话不变。')) return;
    kmdBusy = true; kmdRender();
    kmdPost('/api/modes/follow-global', { session_id: sid }).then(function(d) {
      if (epoch !== kupSettingsEpoch || sid !== kmdActiveSessionId()) return;
      if (d.data) kmdData = d.data;
      kmmToast(d.message || (d.success ? '本对话已跟随全局' : '切换失败'), !d.success);
    }).catch(function() { kmmToast('切换失败：后台服务未响应', true); })
      .finally(function() { kmdBusy = false; kmdFetch(true); });
  }
  function kmdIsLocked() {
    if (!kupWidgetsEnabled() || !kmdData) return false;
    var sid = kmdActiveSessionId();
    if (!sid) return !!(kmdData.locked && kmdData.locked.locked);
    return kmdData.session_id === sid && kmdData.session_locked === true;
  }
  // 当前作用域自己那把锁：本对话只看本对话锁，全局只看全局锁。
  // 不能拿「生效锁」（可能来自全局）去决定会话解锁按钮，否则会去解一把不存在的锁。
  function kmdScopeLocked() {
    if (!kupWidgetsEnabled() || !kmdData) return false;
    return kmdScope() === 'session'
      ? !!(kmdData.session_lock && kmdData.session_lock.locked)
      : !!(kmdData.locked && kmdData.locked.locked);
  }
  function kmdLockedIndex() {
    var index = kmdData && (kmdScope() === 'session' ? kmdData.session_active : kmdData.active);
    return typeof index === 'number' ? index : null;
  }

  function kmdHostLockApply() {
    var locked = kmdIsLocked();
    if (!locked) {
      document.querySelectorAll('[data-ku-lock]').forEach(function(n) {
        n.removeAttribute('data-ku-lock');
        n.classList.remove('ku-host-model-locked', 'ku-host-model-locked-v');
        if (n.tagName === 'BUTTON' || n.tagName === 'SELECT') n.disabled = false;
        n.removeAttribute('title');
      });
      return 0;
    }
    var hit = 0;
    // 只锁「模型切换器」叶子控件，绝不碰输入区容器/祖先——
    // 之前给容器加 pointer-events:none 会把里面的 textarea 也冻结，导致整框打不了字。
    var sels = [
      '[role="combobox"]', '[aria-haspopup="listbox"]',
      'button[class*="model" i]', 'button[class*="selector" i]',
      '[class*="model-select" i]', '[class*="modelSelect" i]',
      '[class*="chat-model" i]'
    ];
    var cand = [];
    sels.forEach(function(s) {
      try { document.querySelectorAll(s).forEach(function(n) { cand.push(n); }); } catch (e) {}
    });
    // 兜底：文本像「模型名 · Medium/High」的叶子可点元素（不含 input/textarea 后代、
    // 不是大容器），在底部输入区附近。
    if (!cand.length) {
      var pool = document.querySelectorAll('button, [role="button"], [class*="trigger" i]');
      pool.forEach(function(n) {
        if (n.querySelector('textarea,input,[contenteditable="true"]')) return;
        if (n.children.length > 4) return;              // 大容器不锁，只锁叶子
        var t = (n.textContent || '').trim();
        if (t && t.length < 40 && /medium|high|low|xhigh|max|opus|gpt|claude|swe|mimo|kimi|deepseek/i.test(t)
            && n.getBoundingClientRect().top > window.innerHeight * 0.5) {
          cand.push(n);
        }
      });
    }
    cand.forEach(function(n) {
      if (n.closest('#' + CARD_ID + ',#kmd-overlay,#kmm-overlay,.ku-sheet')) {
        if (n.hasAttribute('data-ku-lock')) {
          n.removeAttribute('data-ku-lock');
          n.classList.remove('ku-host-model-locked', 'ku-host-model-locked-v');
          if (n.tagName === 'BUTTON' || n.tagName === 'SELECT') n.disabled = false;
          n.removeAttribute('title');
        }
        return;
      }
      // 安全闸：命中元素若包住/就是输入框，跳过，绝不锁。
      if (n.tagName === 'TEXTAREA' || n.tagName === 'INPUT'
          || n.isContentEditable
          || n.querySelector('textarea,input[type="text"],[contenteditable="true"]')) return;
      // 含其它可交互控件（按钮/输入/链接）的容器只置灰视觉、不锁事件，
      // 免得误伤同容器内的发送/附件按钮或输入框。
      var hasInteractive = !!n.querySelector('button,input,select,a,[contenteditable="true"],textarea');
      if (locked) {
        if (!n.hasAttribute('data-ku-lock')) { n.setAttribute('data-ku-lock', '1'); hit++; }
        if (hasInteractive) n.classList.add('ku-host-model-locked-v');
        else n.classList.add('ku-host-model-locked');
        if (n.tagName === 'BUTTON' || n.tagName === 'SELECT') n.disabled = true;
        n.setAttribute('title', '已锁定到档位主模型，发送框不能切换（在用量卡片解锁）');
      } else if (n.hasAttribute('data-ku-lock')) {
        n.removeAttribute('data-ku-lock');
        n.classList.remove('ku-host-model-locked');
        n.classList.remove('ku-host-model-locked-v');
        if (n.tagName === 'BUTTON' || n.tagName === 'SELECT') n.disabled = false;
        n.removeAttribute('title');
      }
    });
    return hit;
  }

  function kmdHostLockWatch() {
    if (kmdHostObs) { kmdHostObs.disconnect(); kmdHostObs = null; }
    if (!kmdIsLocked()) return;
    // SPA 会重挂 DOM，观察整棵 body，命中后重设禁用
    kmdHostObs = new MutationObserver(function() { kmdHostLockApply(); });
    kmdHostObs.observe(document.body, { childList: true, subtree: true });
  }

  function kmdRenderLock(card) {
    card = card || document.getElementById(CARD_ID);
    if (!card) return;
    var btn = card.querySelector('#ku-mode-lock');
    if (!btn) return;
    var nameEl = card.querySelector('#ku-mode-name');
    var desc = card.querySelector('#ku-mode-desc');
    if (!kmdData || !kupWidgetsEnabled()) {
      btn.classList.remove('on');
      btn.disabled = true;
      btn.innerHTML = '🔓';
      btn.title = '挂件已关闭';
      if (nameEl) {
        var staleBadge = nameEl.querySelector('.ku-mode-lock-badge');
        if (staleBadge) staleBadge.remove();
      }
      if (desc) {
        var staleNote = desc.querySelector('.ku-lock-note');
        if (staleNote) staleNote.remove();
      }
      return;
    }
    var scopeNow = kmdScope();
    var locked = kmdScopeLocked();
    var li = kmdLockedIndex();
    btn.classList.toggle('on', locked);
    btn.disabled = kmdLockBusy;
    btn.innerHTML = locked ? '🔒' : '🔓';
    btn.title = locked
      ? ('已锁定第 ' + (li + 1) + ' 档「' + ((kmdData.modes || [])[li] || {}).name + '」：发送框模型固定，点击解锁')
      : (scopeNow === 'session'
          ? '锁定本对话：只固定当前对话的主模型，其它对话不受影响'
          : '锁定当前档：发送框模型被固定为本档主模型，后端守护自动改回');
    if (nameEl && locked) {
      // 名字后挂锁标，但不重复加
      if (!nameEl.querySelector('.ku-mode-lock-badge')) {
        var b = document.createElement('span');
        b.className = 'ku-mode-lock-badge';
        b.textContent = '锁';
        nameEl.appendChild(b);
      }
    } else if (nameEl) {
      var old = nameEl.querySelector('.ku-mode-lock-badge');
      if (old) old.remove();
    }
    if (desc && locked && !desc.querySelector('.ku-lock-note')) {
      var n = document.createElement('div');
      n.className = 'ku-lock-note';
      n.style.cssText = 'color:var(--color-warning,#d29922);font-size:10px;margin-top:2px;';
      n.textContent = (kmdData.session_lock && !(kmdData.locked && kmdData.locked.locked))
        ? '已锁定本对话：只固定当前对话的主模型，其它对话不受影响'
        : '已锁定：发送框请用档位主模型（后端会自动改回偏离的切换）';
      desc.appendChild(n);
    } else if (desc && !locked) {
      var on = desc.querySelector('.ku-lock-note');
      if (on) on.remove();
    }
  }

  function kmdToggleLock() {
    if (kmdLockBusy || !kmdData || !kupWidgetsEnabled()) return;
    var scope = kmdScope();
    var locked = kmdScopeLocked();
    var idx = locked ? null : (kmdShownIndex() != null ? kmdShownIndex()
                             : (kmdData.last_applied && kmdData.last_applied.index));
    if (!locked && (idx == null || idx < 0)) { kmmToast('先选一档再锁定', true); return; }
    if (!locked && scope === 'session' && !kmdActiveSessionId()) { kmmToast('先打开一个对话', true); return; }
    kmdLockBusy = true;
    kmdRenderLock();
    var epoch = kupSettingsEpoch, sid = kmdActiveSessionId();
    var body = locked
      ? { scope: scope, session_id: sid || null }
      : kmdTargetBody(idx);
    var call = locked ? kmdPost('/api/modes/unlock', body) : kmdPost('/api/modes/lock', body);
    call.then(function(d) {
      if (epoch !== kupSettingsEpoch || sid !== kmdActiveSessionId()) return;
      if (d.success) {
        if (d.data) kmdData = d.data;
        kmmToast(d.message || (locked ? '已解锁' : '已锁定'));
      } else {
        kmmToast(d.message || '操作失败', true);
      }
    }).catch(function() { kmmToast('操作失败：后台服务未响应', true); })
      .finally(function() {
        kmdLockBusy = false;
        kmdRender();
        kmdRenderLock();
        kmdHostLockApply();
        kmdHostLockWatch();
      });
  }


  function kmdBindCard(el) {
    var range = el.querySelector('#ku-mode-range');
    if (!range) return;
    ['click', 'mousedown', 'pointerdown'].forEach(function(t) {
      el.querySelector('#ku-mode-row').addEventListener(t, function(e) { e.stopPropagation(); });
    });
    var kmdStartDrag = function() { kmdDragging = true; };
    var kmdEndDrag = function() { kmdDragging = false; };
    range.addEventListener('pointerdown', kmdStartDrag);
    range.addEventListener('pointerup', kmdEndDrag);
    range.addEventListener('pointercancel', kmdEndDrag);
    range.addEventListener('blur', kmdEndDrag);
    range.oninput = function() { kmdPreview(el, Number(range.value)); };
    range.onchange = function() {
      var i = Math.round(Number(range.value));
      kmdDragging = false;
      if (kmdData && i === kmdShownIndex()) { kmdSnapTo(range, i, function() { kmdRender(); }); return; }
      kmdSnapTo(range, i, function() { kmdApply(i); });
    };
    el.querySelector('#ku-mode-edit').onclick = function(e) { e.stopPropagation(); kmdOpenEditor(); };
    el.querySelector('#ku-mode-lock').onclick = function(e) { e.stopPropagation(); kmdToggleLock(); };
    el.querySelectorAll('#ku-mode-scope button').forEach(function(b) {
      b.onclick = function(e) { e.stopPropagation(); kmdSetScope(b.getAttribute('data-scope')); };
    });
    if (kmdData) { kmdRender(el); kmdRenderLock(el); kmdHostLockApply(); kmdHostLockWatch(); }
    kmdFetch(true).then(function() { kmdRenderLock(); kmdHostLockApply(); kmdHostLockWatch(); });
  }

  function kmdEnsureOverlay() {
    if (kmdOverlayEl) return kmdOverlayEl;
    var o = document.createElement('div');
    o.id = 'kmd-overlay';
    o.innerHTML = '<div id="kmd-modal">'
      + '<div class="kmd-head"><div><h3>模式档位</h3><div class="kmd-sub">滑杆从左到右：单模型省钱 → 主模型 + 挂件（子代理）。每档可自定义主模型与 0~3 个挂件。</div></div>'
      + '<button class="kmm-x" id="kmd-x" title="关闭">✕</button></div>'
      + '<div class="kmd-body" id="kmd-body"></div>'
      + '<div class="kmd-foot"><span class="kmd-tip" id="kmd-tip"></span>'
      + '<button class="kmm-tbtn" id="kmd-restore" title="把 default_model / [secondary_model] / [tools] 恢复成第一次用滑杆之前的样子">恢复切换前</button>'
      + '<button class="kmm-tbtn" id="kmd-reset">恢复预设</button>'
      + '<button class="kmm-tbtn" id="kmd-add">+ 加一档</button>'
      + '<button class="kmm-tbtn primary" id="kmd-save">保存</button></div>'
      + '</div>';
    document.body.appendChild(o);
    kmdOverlayEl = o;
    var close = function() { kmdDraft = null; kuLayerHide(o, 'visible'); };
    o.onclick = function(e) { if (e.target === o) close(); };
    o.querySelector('#kmd-x').onclick = close;
    o.querySelector('#kmd-add').onclick = function() {
      kmdCollect();
      if (kmdDraft.length >= (kmdData.max || 6)) { kmmToast('最多 ' + (kmdData.max || 6) + ' 档', true); return; }
      var last = kmdDraft[kmdDraft.length - 1] || {};
      kmdDraft.push({ name: '档位 ' + (kmdDraft.length + 1), scene: '', main: last.main || '', subagents: (last.subagents || []).slice(), effort: '' });
      kmdRenderEditor();
    };
    o.querySelector('#kmd-save').onclick = function() {
      kmdCollect();
      var btn = o.querySelector('#kmd-save');
      btn.disabled = true;
      kmdPost('/api/modes/save', { modes: kmdDraft }).then(function(d) {
        if (d.success) {
          kmdData = d.data; kmdDraft = null;
          kmmToast(d.message || '已保存');
          // 保存后立即刷新滑块：不等关弹窗延迟，先清拖拽/焦点态再把
          // 滑块吸附到 active 档——否则 range 仍停在编辑前位置，与
          // detect_active_mode 反推出的高亮档不一致，看起来像「没选中」。
          setTimeout(function() {
            kuLayerHide(o, 'visible');
            var card = document.getElementById(CARD_ID);
            var range = card && card.querySelector('#ku-mode-range');
            kmdBusy = false; kmdDragging = false;
            if (range) { range.blur(); }
            kmdRender();
            var nowIndex = kmdShownIndex();
            if (range && kmdData && nowIndex != null) {
              kmdSnapTo(range, nowIndex, null);
            }
          }, 350);
        } else {
          kmmToast(d.message || '保存失败', true);
        }
      }).catch(function() { kmmToast('保存失败：后台服务未响应', true); })
        .then(function() { btn.disabled = false; });
    };
    o.querySelector('#kmd-reset').onclick = function() {
      if (!confirm('把档位列表恢复为内置 4 档预设？（不会改动 config.toml）')) return;
      kmdPost('/api/modes/reset', {}).then(function(d) {
        kmmToast(d.message, !d.success);
        if (d.success) { kmdData = d.data; kmdDraft = null; kmdRender(); kmdRenderEditor(); }
      });
    };
    o.querySelector('#kmd-restore').onclick = function() {
      if (!confirm('把 config.toml 的主模型 / 子代理池 / [tools] 恢复成第一次用滑杆之前的样子？')) return;
      kmdPost('/api/modes/restore', {}).then(function(d) {
        kmmToast(d.message, !d.success);
        if (d.data) { kmdData = d.data; kmdDraft = null; kmdRender(); kmdRenderEditor(); }
      }).catch(function() { kmmToast('恢复失败：后台服务未响应', true); });
    };
    return o;
  }

  function kmdOptions(sel, allowEmpty) {
    var h = allowEmpty ? '<option value="">（无）</option>' : '';
    var found = !sel;
    (kmdData.models || []).forEach(function(m) {
      if (m.alias === sel) found = true;
      var pt = kmdPriceTxt(m.alias);
      var label = m.alias + (m.display_name && m.display_name !== m.alias ? ' · ' + m.display_name : '') + (pt ? '  [' + pt + ']' : '') + (m.has_tools ? '' : '  (无工具调用)');
      h += '<option value="' + esc(m.alias) + '"' + (m.alias === sel ? ' selected' : '') + '>' + esc(label) + '</option>';
    });
    if (!found) h += '<option value="' + esc(sel) + '" selected>' + esc(sel) + '（config 中不存在）</option>';
    return h;
  }

  function kmdRenderEditor() {
    if (!kmdOverlayEl || !kmdData) return;
    if (!kmdDraft) kmdDraft = JSON.parse(JSON.stringify(kmdData.modes || []));
    var body = kmdOverlayEl.querySelector('#kmd-body');
    var max = kmdData.subs_max || 3;
    body.innerHTML = kmdDraft.map(function(m, i) {
      var subs = '';
      for (var k = 0; k < max; k++) {
        subs += '<select class="kmd-sub-sel" data-f="sub" data-k="' + k + '">' + kmdOptions((m.subagents || [])[k] || '', true) + '</select>';
      }
      return '<div class="kmd-card' + (i === kmdShownIndex() ? ' on' : '') + '" data-i="' + i + '">'
        + '<div class="kmd-line"><span class="kmd-idx">第 ' + (i + 1) + ' 档</span>'
        + '<input type="text" data-f="name" maxlength="20" placeholder="档名" value="' + esc(m.name) + '">'
        + '<span class="kmd-acts">'
        + '<button class="kmm-link" data-act="up" title="上移">↑</button><button class="kmm-link" data-act="down" title="下移">↓</button>'
        + '<button class="kmm-link" data-act="del" title="删除这一档">删除</button></span></div>'
        + '<div class="kmd-line"><label>场景</label><input type="text" data-f="scene" maxlength="120" placeholder="适合什么活" value="' + esc(m.scene || '') + '"></div>'
        + '<div class="kmd-line"><label>主模型</label><select data-f="main">' + kmdOptions(m.main, false) + '</select></div>'
        + '<div class="kmd-line"><label>挂件</label>' + subs + '</div>'
        + '<div class="kmd-line"><label>思考</label><select data-f="effort">'
        + ['', 'on', 'off', 'low', 'medium', 'high', 'xhigh', 'max'].map(function(e) {
            return '<option value="' + e + '"' + ((m.effort || '') === e ? ' selected' : '') + '>' + (e ? '子代理 ' + e : '子代理沿用现有') + '</option>';
          }).join('')
        + '</select></div></div>';
    }).join('');
    body.querySelectorAll('[data-act]').forEach(function(b) {
      b.onclick = function() {
        kmdCollect();
        var i = Number(b.closest('.kmd-card').getAttribute('data-i'));
        var act = b.getAttribute('data-act');
        if (act === 'del') {
          if (kmdDraft.length <= (kmdData.min || 2)) { kmmToast('至少保留 ' + (kmdData.min || 2) + ' 档', true); return; }
          kmdDraft.splice(i, 1);
        } else {
          var j = act === 'up' ? i - 1 : i + 1;
          if (j < 0 || j >= kmdDraft.length) return;
          var t = kmdDraft[i]; kmdDraft[i] = kmdDraft[j]; kmdDraft[j] = t;
        }
        kmdRenderEditor();
      };
    });
    var tip = [];
    if (kmdData.last_applied) tip.push('上次切换：' + kmdData.last_applied.name + ' @ ' + kmdData.last_applied.time);
    if ((kmdData.undefined_pool || []).length) tip.push('⚠ 现有子代理池含未定义模型：' + kmdData.undefined_pool.join('、'));
    tip.push('第 1 个挂件即子代理默认模型；0 个挂件 = 禁用 Agent/AgentSwarm。切换后会话内 /reload 生效');
    kmdOverlayEl.querySelector('#kmd-tip').textContent = tip.join(' · ');
    kmdOverlayEl.querySelector('#kmd-restore').disabled = !kmdData.has_snapshot;
    kmdOverlayEl.querySelector('#kmd-restore').style.opacity = kmdData.has_snapshot ? '' : '.45';
  }

  function kmdCollect() {
    if (!kmdOverlayEl || !kmdDraft) return;
    kmdOverlayEl.querySelectorAll('.kmd-card').forEach(function(c) {
      var i = Number(c.getAttribute('data-i'));
      var m = kmdDraft[i];
      if (!m) return;
      m.name = c.querySelector('[data-f=name]').value.trim();
      m.scene = c.querySelector('[data-f=scene]').value.trim();
      m.main = c.querySelector('[data-f=main]').value;
      m.effort = c.querySelector('[data-f=effort]').value;
      var subs = [];
      c.querySelectorAll('[data-f=sub]').forEach(function(s) { if (s.value && subs.indexOf(s.value) < 0) subs.push(s.value); });
      m.subagents = subs;
    });
  }

  function kmdOpenEditor() {
    if (!kupWidgetsEnabled()) { kmmToast('挂件已关闭，请先在面板设置中开启', true); return; }
    var o = kmdEnsureOverlay();
    kmdDraft = null;
    kuLayerShow(o, 'visible');
    if (kmdData) kmdRenderEditor();
    kmdFetch(true).then(function() { if (kupWidgetsEnabled() && !kmdDraft) kmdRenderEditor(); });
  }

  setInterval(function() {
    if (document.hidden || kmdBusy || !document.getElementById(CARD_ID) || !kupWidgetsEnabled()) return;
    kmdFetch(false);
  }, 20000);
  setInterval(function() {
    if (!document.hidden) kupFetchSettings();
  }, 3000);
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden) kupFetchSettings();
  });
  kupFetchSettings();

  function openModelModal() {
    kmmModalOrigin = document.activeElement;
    var overlay = ensureModelOverlay();
    kuLayerShow(overlay, 'visible'); isModelModalOpen = true;
    kmmRenderManagerUI(); kmmEl('kmm-modal').focus();
    kuScheduleIdle(function() { kmmRenderModalCards(); kmmRenderProviders(); });
    kmmRefreshAll(); mcpFetch(); skillFetch();
  }
  async function closeModelModal() {
    if (!await kmmDiscardAll()) return;
    if (kmmOverlayEl) kuLayerHide(kmmOverlayEl, 'visible');
    isModelModalOpen = false;
    if (kmmModalOrigin && kmmModalOrigin.isConnected) kmmModalOrigin.focus();
  }

  function fetchModels() {
    // 优先本地服务 API，失败回退 __KIMI_DATA__ 缓存
    return kapiFetch(`${API_BASE}/api/data`)
      .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(d) {
        modelsData = d;
        apiAlive = true;
        if (isModelModalOpen) kmmRenderModalCards();
      })
      .catch(function() {
        apiAlive = false;
        if (window.__KIMI_DATA__ && window.__KIMI_DATA__.models) modelsData = window.__KIMI_DATA__.models;
        if (isModelModalOpen) kmmRenderModalCards();
      }).then(function() { if (isModelModalOpen) kmmRenderManagerUI(); });
  }

  function kmmRenderModalCards() {
    if (!kmmOverlayEl) return;
    var container = kmmEl('kmm-modal-cards');
    if (!container || kmmDraft || kmmBatch || kmmManagerTab !== 'models') return;
    var kw = kmmQueries.models.trim().toLowerCase();
    container.innerHTML = '';
    if (!modelsData) {
      kmmRenderEmpty(container, '暂时没有模型数据', '请稍候，或在更多操作中刷新列表。');
      return;
    }
    var badge = kmmEl('kmm-modal-default-badge');
    var def = kmmManagerData ? kmmManagerData.default_model : modelsData.default_model || '';
    var defM = (kmmManagerData ? kmmManagerData.models : modelsData.models || []).find(function(m) { return m.alias === def; });
    badge.textContent = '默认模型 · ' + ((defM && defM.display_name) || def || '未设置');
    badge.title = badge.textContent;
    var au = modelsData.effort_audit || {}, ign = modelsData.dismissed_count || 0, fixable = 0;
    (modelsData.models || []).forEach(function(m) {
      (m.effort_issues || []).forEach(function(it) {
        if (!it.dismissed && it.level !== 'info' && it.fix && it.code !== 'no_support_efforts') fixable++;
      });
    });
    if (au.error || au.warn || ign) {
      var sum = document.createElement('div');
      sum.className = 'kmm-audit' + (au.error || au.warn ? ' is-warning' : '');
      var parts = [];
      if (au.error) parts.push(au.error + ' 个错误');
      if (au.warn) parts.push(au.warn + ' 个提醒');
      var txt = document.createElement('span');
      txt.textContent = parts.length ? '配置检查 · ' + parts.join('，') : '配置检查通过';
      sum.appendChild(txt);
      var mk = function(label, fn) {
        var bt = document.createElement('button');
        bt.className = 'kmm-link accent'; bt.textContent = label;
        bt.disabled = !apiAlive || !kmmManagerWritable(); bt.onclick = fn;
        sum.appendChild(bt);
      };
      if (fixable) mk('修复 ' + fixable + ' 项', function() { window.__KMM_ISSUE('', '', 'fix_all'); });
      if (ign) mk('恢复已忽略的 ' + ign + ' 项', function() { window.__KMM_ISSUE('', '', 'unignore_all'); });
      container.appendChild(sum);
    }
    var lastUsed = state.cur && state.cur.alias && state.cur.alias !== '--' ? state.cur.alias : '';
    var list = (modelsData.models || []).slice().sort(function(a, b) {
      return (a.alias === lastUsed ? 0 : a.is_default ? 1 : 2) - (b.alias === lastUsed ? 0 : b.is_default ? 1 : 2);
    });
    var shown = 0;
    list.forEach(function(m) {
      var hay = (m.alias + ' ' + (m.display_name || '') + ' ' + m.model + ' ' + m.provider).toLowerCase();
      if (kw && hay.indexOf(kw) < 0) return;
      shown++;
      var isCur = m.alias === lastUsed && lastUsed;
      var card = document.createElement('div');
      card.className = 'kmm-card' + (m.is_default || isCur ? ' is-default' : '');
      var editableModel = kmmManagerData && kmmManagerData.models.find(function(x) { return x.alias === m.alias; });
      var deleteReason = kmmModelDeleteReason(editableModel);
      var capabilities = [m.has_image ? '识图' : '', m.has_thinking || m.always_thinking ? '思考' : '', m.has_tools ? '工具调用' : ''].filter(Boolean);
      var issues = (m.effort_issues || []).filter(function(it) { return it.level !== 'info' && !it.dismissed; });
      var name = m.display_name && m.display_name !== m.alias ? m.display_name : m.model || m.alias;
      var meta = [m.provider];
      if (m.model && m.model !== name) meta.push(m.model);
      if (m.alias !== m.provider + '/' + m.model && m.alias !== name) meta.push('别名 ' + m.alias);
      card.setAttribute('data-model-alias', m.alias);
      card.innerHTML = '<div class="kmm-top"><div class="kmm-name"><strong title="' + esc(m.alias) + '">' + esc(name) + '</strong>'
        + (isCur ? '<span class="kmm-tag cur">使用中</span>' : '') + (m.is_default ? '<span class="kmm-tag def">默认</span>' : '')
        + (editableModel && editableModel.managed ? '<span class="kmm-tag def">托管</span>' : '') + '</div><div class="kmm-acts">'
        + (editableModel ? '<button class="kmm-link accent" data-kmm-action="edit">编辑</button>' : '')
        + '<details class="kmm-item-menu"><summary aria-label="' + esc(m.alias + ' 的更多操作') + '">⋯</summary><div class="kmm-menu">'
        + '<button class="kmm-link" data-kmm-action="price">计价设置</button>'
        + (!m.is_default ? '<button class="kmm-link" data-kmm-action="default">设为默认模型</button><span class="kmm-menu-reason">不切换当前对话</span>' : '')
        + '<button class="kmm-link danger" data-kmm-action="delete"' + (deleteReason ? ' disabled' : '') + '>删除模型</button>'
        + (deleteReason ? '<span class="kmm-menu-reason">' + esc(deleteReason) + '</span>' : '') + '</div></details></div></div>'
        + '<div class="kmm-meta" title="' + esc(meta.filter(Boolean).join(' · ')) + '">' + esc(meta.filter(Boolean).join(' · ')) + '</div>'
        + '<div class="kmm-summary"><span>' + esc(capabilities.join(' / ') || '未声明能力') + '</span>'
        + '<span>' + fmtTok(m.max_context_size || 0) + ' 上下文</span><span>思考 ' + esc(m.default_effort || '未指定') + '</span>'
        + (m.pricing ? '<span>' + (m.pricing.mode === 'per_call' ? '按次计价' : '自定义单价') + '</span>' : '') + '</div>'
        + (issues.length ? '<details class="kmm-warn"><summary>' + issues.length + ' 项配置提醒</summary>' + issues.map(function(it) {
          return '<div class="kmm-warn">' + esc(it.msg)
            + (it.fix ? '<button class="kmm-link accent" data-kmm-action="issue" data-code="' + esc(it.code) + '" data-issue-action="fix">' + esc(it.fix) + '</button>' : '')
            + '<button class="kmm-link" data-kmm-action="issue" data-code="' + esc(it.code) + '" data-issue-action="ignore">忽略</button></div>';
        }).join('') + '</details>' : '');
      card.querySelectorAll('[data-kmm-action]').forEach(function(btn) {
        var action = btn.getAttribute('data-kmm-action');
        btn.disabled = btn.disabled || (action === 'edit' ? !kmmManagerOnline : action === 'price' ? !apiAlive : !kmmManagerWritable());
        btn.onclick = function() {
          if (kmmBusy() || !kmmEl('kmm-detail').hidden) return;
          card.querySelectorAll('.kmm-item-menu').forEach(function(menu) { menu.open = false; });
          if (action === 'edit') kmmOpenEditor('model', m.alias);
          else if (action === 'delete') kmmDeleteModel(m.alias);
          else if (action === 'price') window.__KMM_PRICE(m.alias);
          else if (action === 'default') window.__KMM_SET_DEFAULT(m.alias);
          else if (action === 'issue') window.__KMM_ISSUE(m.alias, btn.getAttribute('data-code'), btn.getAttribute('data-issue-action'));
        };
      });
      container.appendChild(card);
    });
    if (!shown) kmmRenderEmpty(container, kw ? '没有匹配的模型' : '还没有模型', kw ? '试试模型名称、供应商或上游 ID。' : '点击「新增模型」，从供应商获取列表或手动添加。');
  }

  window.__KMM_SET_DEFAULT = async function(alias) {
    if (kmmBusy() || !kmmEl('kmm-detail').hidden || !kmmManagerWritable()) return;
    var model = kmmManagerData.models.find(function(m) { return m.alias === alias; });
    if (!model) return;
    kmmQuickBusy = true; kmmRenderManagerUI();
    try {
      var response = await kapiFetch(API_BASE + '/api/model-manager/model', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: kmmManagerData.version, original_alias: alias, model: kmmModelPayload(kmmModelValue(model), alias), set_default: true }) });
      var data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.message || '未能保存默认模型');
      kmmToast('默认模型已保存；当前对话不变，新会话或 /reload 后加载'); await kmmRefreshAll();
    } catch (e) { kmmToast(e.message || '操作失败，请刷新后重试。', true); }
    finally { kmmQuickBusy = false; kmmRenderManagerUI(); }
  };
  window.__KMM_ISSUE = async function(alias, code, action) {
    if (kmmBusy() || !kmmEl('kmm-detail').hidden) return;
    if (action === 'fix_all' && !await kmmConfirm('将修复可确定的协议、思考标签和默认档位问题，并保存配置。', '修复并保存', '修复配置问题？')) return;
    if (action === 'fix' && code === 'no_support_efforts' && !await kmmConfirm('将声明 low、medium、high、xhigh、max 五档思考。上游不支持的档位会调用失败。', '写入五档声明', '声明支持的思考档位？')) return;
    kmmQuickBusy = true; kmmRenderManagerUI();
    try {
      var response = await kapiFetch(API_BASE + '/api/issue-action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: action, alias: alias, code: code }) });
      var data = await response.json(); kmmToast(data.message, !data.success);
      if (data.success) await kmmRefreshAll();
    } catch (e) { kmmToast('操作失败：后台服务未响应', true); }
    finally { kmmQuickBusy = false; kmmRenderManagerUI(); }
  };

  /* ================================================================
   * MCP 服务器管理（mcp- 前缀）
   * 数据源：GET /api/mcp，写：POST /api/mcp/{upsert,delete,toggle}
   * 所有写入都带 version，服务端比对不一致返回 409，前端提示刷新。
   * ================================================================ */
  var mcpData = null;
  var mcpRequest = null;
  var mcpDraft = null;
  var mcpBusy = false;

  function mcpWritable() {
    return !!(mcpData && mcpData.editable && apiAlive);
  }
  function mcpServer(name) {
    if (!mcpData) return null;
    return mcpData.servers.find(function(s) { return s.name === name; }) || null;
  }
  function mcpFetch() {
    if (mcpRequest) return mcpRequest;
    mcpRequest = kapiFetch(API_BASE + '/api/mcp', { cache: 'no-store' })
      .then(function(r) { if (!r.ok) throw new Error('unavailable'); return r.json(); })
      .then(function(d) {
        if (!d || typeof d.editable !== 'boolean' || !Array.isArray(d.servers)) throw new Error('invalid');
        mcpData = {
          version: String(d.version || ''), editable: d.editable,
          message: typeof d.message === 'string' ? d.message : '',
          servers: d.servers.map(function(s) {
            return {
              name: String(s.name || ''), transport: String(s.transport || ''),
              enabled: !!s.enabled, deferred: !!s.deferred,
              command: String(s.command || ''), args: Array.isArray(s.args) ? s.args.slice() : [],
              cwd: String(s.cwd || ''), url: String(s.url || ''), auth: String(s.auth || ''),
              bearer_token_env: String(s.bearer_token_env || ''),
              startupTimeoutMs: s.startupTimeoutMs, toolTimeoutMs: s.toolTimeoutMs,
              enabled_tools: Array.isArray(s.enabled_tools) ? s.enabled_tools.slice() : [],
              disabled_tools: Array.isArray(s.disabled_tools) ? s.disabled_tools.slice() : [],
              env_keys: Array.isArray(s.env_keys) ? s.env_keys.slice() : [],
              header_keys: Array.isArray(s.header_keys) ? s.header_keys.slice() : [],
              valid: s.valid !== false, error: typeof s.error === 'string' ? s.error : ''
            };
          })
        };
        return mcpData;
      })
      .catch(function() {
        mcpData = null;
        return null;
      })
      .then(function(d) {
        mcpRequest = null;
        if (kmmOverlayEl) { kmmRenderManagerUI(); mcpRenderCards(); }
        return d;
      });
    return mcpRequest;
  }
  function mcpSend(path, payload) {
    return kapiFetch(API_BASE + '/api/mcp/' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function(r) {
      return r.json().then(function(d) { return { status: r.status, data: d }; })
        .catch(function() { return { status: r.status, data: {} }; });
    });
  }
  function mcpDescribe(s) {
    if (s.transport === 'stdio') {
      return [s.command].concat(s.args || []).join(' ').slice(0, 160);
    }
    return s.url || '';
  }
  function mcpRenderCards() {
    var container = kmmEl('kmm-mcp-cards');
    if (!container || kmmManagerTab !== 'mcp' || mcpDraft) return;
    container.innerHTML = '';
    if (!mcpData) {
      kmmRenderEmpty(container, mcpRequest ? '正在读取 MCP 服务器…' : '暂时无法读取 MCP 配置', '请稍候，或重新连接管理服务。');
      return;
    }
    if (!mcpData.servers.length) {
      kmmRenderEmpty(container, '还没有 MCP 服务器', mcpData.editable ? '点击「新增 MCP 服务器」，连接外部工具与服务。' : '当前配置只读，没有可显示的连接。');
      return;
    }
    var kw = kmmQueries.mcp.trim().toLowerCase();
    mcpData.servers.forEach(function(s) {
      if (kw && (s.name + ' ' + s.transport + ' ' + mcpDescribe(s)).toLowerCase().indexOf(kw) < 0) return;
      var card = document.createElement('div'); card.className = 'kmm-card';
      card.innerHTML = '<div class="kmm-top"><div class="kmm-name"><strong>' + esc(s.name) + '</strong>'
        + '<span class="kmm-tag def">' + esc(s.transport) + '</span></div><div class="kmm-acts">'
        + '<label class="kmm-toggle"><input type="checkbox" data-mcp-toggle aria-label="' + esc('启用 ' + s.name) + '"' + (s.enabled ? ' checked' : '') + '><span>启用</span></label>'
        + '<button class="kmm-link accent" data-mcp-act="edit">编辑</button>'
        + '<details class="kmm-item-menu"><summary aria-label="' + esc(s.name + ' 的更多操作') + '">⋯</summary><div class="kmm-menu"><button class="kmm-link danger" data-mcp-act="delete">删除服务器</button></div></details></div></div>'
        + '<div class="kmm-meta" title="' + esc(mcpDescribe(s)) + '">' + esc(mcpDescribe(s) || '未配置连接方式') + '</div>'
        + '<div class="kmm-summary"><span>' + (s.enabled ? '已启用' : '已停用') + '</span><span>' + (s.deferred ? '使用时加载' : '会话启动时加载') + '</span></div>'
        + (s.valid ? '' : '<div class="kmm-warn">' + esc(s.error) + '</div>');
      card.querySelectorAll('button, input').forEach(function(el) { el.disabled = !mcpWritable(); });
      card.querySelector('[data-mcp-toggle]').onchange = function() { mcpToggle(s.name, this.checked); };
      card.querySelectorAll('[data-mcp-act]').forEach(function(btn) {
        btn.onclick = function() { if (btn.getAttribute('data-mcp-act') === 'edit') mcpOpenEditor(s.name); else mcpDelete(s.name); };
      });
      container.appendChild(card);
    });
    if (!container.children.length) kmmRenderEmpty(container, '没有匹配的 MCP 服务器', '试试连接名称、传输方式或服务地址。');
    kmmRenderManagerUI();
  }
  async function mcpToggle(name, enabled) {
    if (kmmBusy() || !mcpWritable() || !kmmEl('kmm-detail').hidden) return;
    mcpBusy = true; kmmRenderManagerUI();
    try {
      var res = await mcpSend('toggle', { version: mcpData.version, name: name, enabled: enabled });
      if (!res.data.success) throw new Error(res.data.code === 'CONFIG_CONFLICT' ? '配置已变化，本次未保存，请核对刷新后的状态。' : res.data.message || '保存失败');
      kmmToast((enabled ? '启用' : '停用') + '状态已保存，新会话或重连后加载');
    } catch (e) { kmmToast(e.message || '操作失败，正在核对状态。', true); }
    finally { await mcpFetch(); mcpBusy = false; mcpRenderCards(); kmmRenderManagerUI(); }
  }
  async function mcpDelete(name) {
    if (kmmBusy() || !mcpWritable() || !kmmEl('kmm-detail').hidden) return;
    if (!await kmmConfirm('将删除 MCP 服务器「' + name + '」的连接配置，此操作不能撤销，不会卸载它的程序。', '删除服务器', '删除此连接？')) return;
    mcpBusy = true; kmmRenderManagerUI();
    try {
      var res = await mcpSend('delete', { version: mcpData.version, name: name });
      if (!res.data.success) throw new Error(res.data.message || '删除失败');
      kmmToast('连接配置已删除，新会话或重连后加载');
    } catch (e) { kmmToast(e.message || '删除失败，请核对配置。', true); }
    finally { await mcpFetch(); mcpBusy = false; mcpRenderCards(); kmmRenderManagerUI(); }
  }
  function mcpDraftValue(s) {
    var argsText = s ? (s.args || []).join('\n') : '';
    return {
      name: s ? s.name : '', transport: s ? (s.transport || 'stdio') : 'stdio',
      command: s ? s.command : '', args: argsText,
      cwd: s ? s.cwd : '', url: s ? s.url : '', auth: s ? s.auth : '',
      bearer_token_env: s ? s.bearer_token_env : '',
      startup_timeout: s && s.startupTimeoutMs != null ? String(s.startupTimeoutMs) : '',
      tool_timeout: s && s.toolTimeoutMs != null ? String(s.toolTimeoutMs) : '',
      enabled_tools: s ? (s.enabled_tools || []).join('\n') : '',
      disabled_tools: s ? (s.disabled_tools || []).join('\n') : '',
      enabled: s ? s.enabled : true, deferred: s ? s.deferred : false
    };
  }
  async function mcpOpenEditor(name) {
    if (!mcpData || !await kmmDiscardAll()) return;
    var s = name === null ? null : mcpServer(name);
    if (name !== null && !s) { kmmToast('配置已变化，请刷新后重试', true); return; }
    if (name === null && !mcpWritable()) return;
    mcpDraft = { original: name, value: mcpDraftValue(s), version: mcpData.version,
      initial: JSON.stringify(mcpDraftValue(s)), saving: false, conflict: false, error: '' };
    mcpRenderEditor(); kmmRenderManagerUI(); kmmFocusDetail();
  }
  async function mcpCloseEditor() { await kmmDiscardAll(); }
  function mcpLines(text) {
    return String(text || '').split('\n').map(function(v) { return v.trim(); }).filter(function(v) { return v; });
  }
  function mcpBuildPayload(v) {
    var body = { name: v.name.trim(), transport: v.transport };
    if (v.transport === 'stdio') {
      body.command = v.command.trim();
      var args = mcpLines(v.args);
      if (args.length) body.args = args;
      if (v.cwd.trim()) body.cwd = v.cwd.trim();
    } else {
      body.url = v.url.trim();
      if (v.auth) body.auth = v.auth;
      if (v.bearer_token_env.trim()) body.bearerTokenEnvVar = v.bearer_token_env.trim();
    }
    if (v.startup_timeout.trim()) body.startupTimeoutMs = Number(v.startup_timeout);
    if (v.tool_timeout.trim()) body.toolTimeoutMs = Number(v.tool_timeout);
    var on = mcpLines(v.enabled_tools), off = mcpLines(v.disabled_tools);
    if (on.length) body.enabledTools = on;
    if (off.length) body.disabledTools = off;
    body.enabled = !!v.enabled;
    if (v.deferred) body.deferred = true;
    return body;
  }
  function mcpValidate(v) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(v.name.trim())) return '名称只能包含字母、数字、点、下划线和连字符，且以字母或数字开头。';
    if (v.transport === 'stdio' && !v.command.trim()) return '请填写启动命令。';
    if (v.transport !== 'stdio') {
      if (!/^https?:\/\//i.test(v.url.trim())) return '请填写 http(s) 开头的地址。';
      if (v.bearer_token_env.trim() && v.auth === 'oauth') return 'OAuth 与静态 Bearer 变量只能二选一。';
    }
    var limits = [['startup_timeout', '启动超时'], ['tool_timeout', '工具超时']];
    for (var i = 0; i < limits.length; i++) {
      var text = v[limits[i][0]].trim();
      if (!text) continue;
      var num = Number(text);
      if (!Number.isInteger(num) || num < 1 || num > 2147483647) return limits[i][1] + '必须是 1~2147483647 的整数。';
    }
    return '';
  }
  function mcpRenderEditor() {
    var d = mcpDraft, editor = kmmEl('kmm-editor');
    if (!d || !editor) return;
    var v = d.value;
    var field = function(label, name, type, hint) {
      return '<label class="kmm-field">' + esc(label) + '<input id="mcp-f-' + name.replace(/_/g, '-')
        + '" data-mcp-field="' + name + '" type="' + (type || 'text') + '" value="' + esc(v[name]) + '">'
        + (hint ? '<span class="kmm-note">' + esc(hint) + '</span>' : '') + '</label>';
    };
    var area = function(label, name, hint) {
      return '<label class="kmm-field">' + esc(label) + '<textarea id="mcp-f-' + name.replace(/_/g, '-')
        + '" data-mcp-field="' + name + '" rows="4" spellcheck="false">' + esc(v[name]) + '</textarea>'
        + (hint ? '<span class="kmm-note">' + esc(hint) + '</span>' : '') + '</label>';
    };
    var html = (d.original ? '<h3>' + esc(d.original) + '</h3>' : '')
      + '<div id="kmm-editor-note">保存至 ~/.kimi-code/mcp.json，不保留原文件排版。已有 env 与 headers 密钥原样保留，不在面板回显。</div>'
      + '<section class="kmm-section"><h4>连接信息</h4><div class="kmm-editor-grid">'
      + field('名称', 'name', 'text', d.original === null ? '' : '创建后不可改名')
      + '<label class="kmm-field">传输方式<select id="mcp-f-transport" data-mcp-field="transport">'
      + ['stdio', 'http', 'sse'].map(function(t) {
          return '<option value="' + t + '"' + (v.transport === t ? ' selected' : '') + '>' + t + '</option>';
        }).join('') + '</select></label>';
    if (v.transport === 'stdio') {
      html += field('启动命令', 'command', 'text', '例如 npx 或 cmd.exe')
        + field('工作目录（可留空）', 'cwd')
        + '<div class="kmm-field-wide">' + area('启动参数', 'args', '每行一个参数；含空格的参数直接整行填写。') + '</div>';
    } else {
      html += '<div class="kmm-field-wide">' + field('服务地址', 'url', 'text', '以 http:// 或 https:// 开头') + '</div>'
        + '<label class="kmm-field">鉴权方式<select id="mcp-f-auth" data-mcp-field="auth">'
        + '<option value=""' + (v.auth ? '' : ' selected') + '>无</option>'
        + '<option value="oauth"' + (v.auth === 'oauth' ? ' selected' : '') + '>OAuth</option></select></label>'
        + field('Bearer 环境变量名（可留空）', 'bearer_token_env', 'text', '从环境变量读取密钥，不写入配置；与 OAuth 二选一。');
    }
    html += '</div></section><label class="kmm-toggle"><input type="checkbox" data-mcp-field="enabled"' + (v.enabled ? ' checked' : '') + '><span>启用此服务器</span></label>'
      + '<details' + (d.advanced ? ' open' : '') + ' id="mcp-advanced"><summary>高级设置 · 超时与工具权限</summary><div class="kmm-advanced-content"><div class="kmm-editor-grid">'
      + field('启动超时（毫秒）', 'startup_timeout', 'number', '留空使用默认值')
      + field('工具调用超时（毫秒）', 'tool_timeout', 'number', '留空使用默认值')
      + area('允许的工具', 'enabled_tools', '每行一个名称，留空允许全部。')
      + area('禁用的工具', 'disabled_tools', '每行一个名称。') + '</div>'
      + '<label class="kmm-toggle"><input type="checkbox" data-mcp-field="deferred"' + (v.deferred ? ' checked' : '') + '><span>使用时再加载工具清单</span></label></div></details>'
      + '<div id="kmm-editor-status" role="status" aria-live="polite"></div><div id="kmm-editor-error" role="alert"></div>'
      + '<div class="kmm-editor-actions"><button type="button" class="kmm-link" id="mcp-editor-reload" hidden>载入最新配置</button>'
      + '<button type="button" class="kmm-tbtn" id="mcp-editor-cancel">取消</button><button type="submit" class="kmm-tbtn primary" id="mcp-editor-save">保存</button></div>';
    editor.innerHTML = html;
    editor.querySelector('#mcp-advanced').ontoggle = function() { d.advanced = this.open; };
    editor.querySelectorAll('[data-mcp-field]').forEach(function(input) {
      var update = function() {
        if (mcpDraft !== d || d.saving || input.disabled) return;
        var key = input.getAttribute('data-mcp-field');
        v[key] = input.type === 'checkbox' ? input.checked : input.value;
        if (key === 'transport') mcpRenderEditor();
        mcpUpdateEditorState();
      };
      input.addEventListener('input', update); input.addEventListener('change', update);
    });
    editor.querySelector('#mcp-editor-cancel').onclick = mcpCloseEditor;
    editor.querySelector('#mcp-editor-reload').onclick = async function() {
      if (mcpDraftChanged() && !await kmmConfirm('载入最新配置将放弃当前未保存的修改。', '载入最新配置', '放弃当前修改？', '继续编辑')) return;
      var data = await mcpFetch();
      if (!data || mcpDraft !== d) return;
      kmmClearEditor(); await mcpOpenEditor(d.original);
    };
    editor.onsubmit = function(e) { e.preventDefault(); mcpSave(); };
    mcpUpdateEditorState();
  }
  function mcpUpdateEditorState() {
    var d = mcpDraft; if (!d) return;
    kmmEl('kmm-editor').querySelectorAll('input, select, textarea').forEach(function(el) {
      el.disabled = d.saving || !mcpWritable() || (el.getAttribute('data-mcp-field') === 'name' && d.original !== null);
    });
    kmmEl('mcp-editor-save').disabled = d.saving || d.conflict || !mcpWritable() || (d.original !== null && !mcpDraftChanged());
    kmmEl('mcp-editor-save').textContent = d.saving ? '保存中…' : '保存';
    kmmEl('mcp-editor-cancel').disabled = d.saving;
    kmmEl('mcp-editor-reload').hidden = !d.conflict;
    kmmEl('mcp-editor-reload').disabled = d.saving;
    kmmEl('kmm-editor-error').textContent = d.error;
    kmmEl('kmm-editor-status').textContent = d.saving ? '正在保存…' : d.conflict ? '配置已被其他操作修改，输入仍保留。' : '保存后在新会话或重连后加载。';
    kmmUpdateDirty();
  }
  async function mcpSave() {
    var d = mcpDraft;
    if (!d || d.saving || d.conflict || !mcpWritable()) return;
    d.error = mcpValidate(d.value);
    if (d.error) { mcpUpdateEditorState(); return; }
    d.saving = true; mcpUpdateEditorState(); kmmRenderManagerUI();
    try {
      var res = await mcpSend('upsert', { version: d.version, original_name: d.original, server: mcpBuildPayload(d.value) });
      if (mcpDraft !== d) return;
      if (res.data.success) {
        kmmToast('已保存，新会话或重连后加载'); kmmClearEditor();
        await mcpFetch(); kmmRestoreFocus();
      } else {
        d.conflict = res.status === 409 || res.data.code === 'CONFIG_CONFLICT';
        d.error = res.data.message || '保存失败，输入仍保留。';
      }
    } catch (e) { d.error = '未能确认保存结果，输入仍保留。请重新连接后核对。'; }
    finally { d.saving = false; if (mcpDraft === d) mcpUpdateEditorState(); kmmRenderManagerUI(); }
  }

  /* ================================================================
   * Skill 管理（skill- 前缀）
   * 数据源：GET /api/skills，正文：GET /api/skill?key=…
   * ================================================================ */
  var skillData = null;
  var skillRequest = null;
  var skillDetail = null;

  function skillFetch() {
    if (skillRequest) return skillRequest;
    skillRequest = kapiFetch(API_BASE + '/api/skills', { cache: 'no-store' })
      .then(function(r) { if (!r.ok) throw new Error('unavailable'); return r.json(); })
      .then(function(d) {
        if (!d || !Array.isArray(d.skills)) throw new Error('invalid');
        skillData = d;
        return d;
      })
      .catch(function() { skillData = null; return null; })
      .then(function(d) {
        skillRequest = null;
        if (kmmOverlayEl) { kmmRenderManagerUI(); skillRenderCards(); }
        return d;
      });
    return skillRequest;
  }
  var SKILL_SOURCE_LABEL = { user: '用户级', project: '项目级', plugin: '插件' };
  function skillRenderCards() {
    var container = kmmEl('kmm-skill-cards');
    if (!container || kmmManagerTab !== 'skills') return;
    container.innerHTML = '';
    if (!skillData) {
      kmmRenderEmpty(container, skillRequest ? '正在读取技能…' : '暂时无法读取技能', '请稍候，或重新连接管理服务。');
      return;
    }
    var head = document.createElement('div'); head.className = 'kmm-note';
    var counts = skillData.counts || {}, parts = [];
    ['user', 'project', 'plugin'].forEach(function(k) {
      if (counts[k]) parts.push(SKILL_SOURCE_LABEL[k] + ' ' + counts[k]);
    });
    head.textContent = parts.join(' · ');
    if (parts.length) container.appendChild(head);
    var kw = kmmQueries.skills.trim().toLowerCase(), shown = 0;
    skillData.skills.forEach(function(s) {
      if (kw && (s.name + ' ' + s.description + ' ' + s.source + ' ' + (SKILL_SOURCE_LABEL[s.source] || '')).toLowerCase().indexOf(kw) < 0) return;
      shown++;
      var card = document.createElement('div'); card.className = 'kmm-card';
      var acts = '<button class="kmm-link accent" data-skill-act="view">' + (s.writable ? '详情' : '查看') + '</button>';
      if (s.writable) acts = '<label class="kmm-toggle"><input type="checkbox" data-skill-toggle aria-label="' + esc('允许 ' + s.name + ' 自动调用') + '"' + (s.disabled ? '' : ' checked') + '><span>自动调用</span></label>'
        + '<button class="kmm-link accent" data-skill-act="edit">编辑</button>'
        + '<details class="kmm-item-menu"><summary aria-label="' + esc(s.name + ' 的更多操作') + '">⋯</summary><div class="kmm-menu"><button class="kmm-link" data-skill-act="view">查看内容</button><button class="kmm-link danger" data-skill-act="delete">删除技能及目录</button></div></details>';
      card.innerHTML = '<div class="kmm-top"><div class="kmm-name"><strong>' + esc(s.name) + '</strong><span class="kmm-tag def">' + esc(SKILL_SOURCE_LABEL[s.source] || s.source) + '</span></div>'
        + '<div class="kmm-acts">' + acts + '</div></div><div class="kmm-meta kmm-card-description" title="' + esc(s.description || '') + '">' + esc(s.description || '无描述') + '</div>'
        + '<div class="kmm-summary"><span>' + (s.disabled ? '自动调用关闭 · 仍可手动触发' : '允许自动调用') + '</span>'
        + (!s.writable ? '<span>仅可查看</span>' : '') + '</div>'
        + (s.valid ? '' : '<div class="kmm-warn">' + esc(s.error) + '</div>');
      card.querySelectorAll('button, input').forEach(function(el) { el.disabled = !apiAlive; });
      if (card.querySelector('[data-skill-toggle]')) card.querySelector('[data-skill-toggle]').onchange = function() { skillToggle(s, !this.checked); };
      card.querySelectorAll('[data-skill-act]').forEach(function(btn) {
        btn.onclick = function() {
          var act = btn.getAttribute('data-skill-act');
          if (act === 'view') skillOpen(s); else if (act === 'edit') skillEdit(s); else skillDelete(s);
        };
      });
      container.appendChild(card);
    });
    if (!shown) kmmRenderEmpty(container, kw ? '没有匹配的技能' : '还没有技能', kw ? '试试技能名称、用途描述或来源。' : '点击「新增技能」创建指令；关闭自动调用后仍可手动触发。');
    kmmRenderManagerUI();
  }
  var skillBusy = false;
  function skillSend(action, payload) {
    return kapiFetch(API_BASE + '/api/skills/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function(r) {
      return r.json().then(function(d) { return { status: r.status, data: d }; })
        .catch(function() { return { status: r.status, data: {} }; });
    });
  }
  async function skillToggle(item, disabled) {
    if (kmmBusy() || !item.writable || !kmmEl('kmm-detail').hidden) return;
    skillBusy = true; kmmRenderManagerUI();
    try {
      var res = await skillSend('toggle', { key: item.key, disabled: disabled });
      if (!res.data.success) throw new Error(res.data.message || '操作失败');
      kmmToast(res.data.message || (disabled ? '自动调用已关闭，仍可手动触发' : '自动调用已开启'));
    } catch (e) { kmmToast(e.message || '操作失败，请核对状态。', true); }
    finally { await skillFetch(); skillBusy = false; skillRenderCards(); kmmRenderManagerUI(); }
  }
  async function skillDelete(item) {
    if (kmmBusy() || !item.writable || !kmmEl('kmm-detail').hidden) return;
    if (!await kmmConfirm('将删除技能「' + item.name + '」的整个目录，包括其中的脚本和资源，此操作不能撤销。\n' + item.dir, '删除整个目录', '删除此技能？')) return;
    skillBusy = true; kmmRenderManagerUI();
    try {
      var res = await skillSend('delete', { key: item.key });
      if (!res.data.success) throw new Error(res.data.message || '删除失败');
      kmmToast(res.data.message || '技能已删除');
    } catch (e) { kmmToast(e.message || '删除失败，请核对状态。', true); }
    finally { await skillFetch(); skillBusy = false; skillRenderCards(); kmmRenderManagerUI(); }
  }
  async function skillOpen(item, editing) {
    if (!item || !item.key || !await kmmDiscardAll()) return;
    var d = { item: item, name: item.name, body: '', description: item.description, loading: true,
      error: '', editing: !!editing, saving: false, writable: false };
    skillDetail = d; skillRenderDetail();
    try {
      var response = await kapiFetch(API_BASE + '/api/skill?key=' + encodeURIComponent(item.key), { cache: 'no-store' });
      var data = await response.json();
      if (skillDetail !== d) return;
      if (!response.ok || typeof data.body !== 'string') throw new Error(data.message || data.error || '无法读取技能');
      d.body = data.body; d.description = data.description || ''; d.disabled = !!data.disabled;
      d.writable = !!data.writable; d.initial = skillEditValue();
    } catch (e) { d.error = '读取失败，请重新打开详情重试。'; d.detailError = true; }
    if (skillDetail !== d) return;
    d.loading = false; skillRenderDetail(); kmmFocusDetail();
  }
  function skillEdit(item) { return skillOpen(item, true); }
  async function skillNew() {
    if (!skillData || !await kmmDiscardAll()) return;
    skillDetail = { item: { key: '', name: '', source: 'user', path: '', dir: '~/.kimi-code/skills' }, name: '',
      body: '', description: '', loading: false, error: '', editing: true, saving: false, creating: true, writable: true, disabled: false };
    skillDetail.initial = skillEditValue(); skillRenderDetail(); kmmFocusDetail();
  }
  function skillRenderDetail() {
    if (!skillDetail || !kmmOverlayEl) return;
    var d = skillDetail, item = d.item, existing = document.getElementById('ksk-overlay');
    if (!existing) { existing = document.createElement('div'); existing.id = 'ksk-overlay'; kmmEl('kmm-detail').appendChild(existing); }
    var html = '<div id="ksk-box"><div class="kmm-top">'
      + (d.creating ? '<h3>技能配置</h3>' : '<h3>' + esc(item.name) + '</h3>')
      + '<span class="kmm-tag def">' + esc(SKILL_SOURCE_LABEL[item.source] || item.source) + '</span></div>';
    var actions = '';
    if (d.loading) html += '<p class="kmm-note" role="status">正在读取技能内容…</p>';
    else if (d.detailError) html += '<p class="kmm-error" role="alert">' + esc(d.error) + '</p>';
    else if (d.editing && d.writable) {
      html += '<section class="kmm-section"><h4>基本信息</h4>'
        + '<label class="kmm-field">名称<input id="ksk-name" value="' + esc(d.name) + '"' + (d.creating ? '' : ' disabled') + '><span class="kmm-note">创建后不可改名。</span></label>'
        + '<label class="kmm-field">用途描述<input id="ksk-desc" value="' + esc(d.description) + '"></label></section>'
        + '<label class="kmm-field kmm-skill-body">技能指令（Markdown）<textarea id="ksk-body" rows="14" spellcheck="false">' + esc(d.body) + '</textarea></label>'
        + '<label class="kmm-toggle"><input type="checkbox" id="ksk-disabled"' + (d.disabled ? ' checked' : '') + '><span>关闭自动调用</span><small>仍可手动触发</small></label>'
        + '<div id="ksk-error" role="alert" class="kmm-error">' + esc(d.error) + '</div>';
      actions = '<div class="kmm-editor-actions"><button type="button" class="kmm-tbtn" id="ksk-cancel">取消</button><button type="button" class="kmm-tbtn primary" id="ksk-save">' + (d.saving ? '保存中…' : '保存') + '</button></div>';
    } else {
      html += '<p class="kmm-skill-description">' + esc(d.description || '无描述') + '</p>'
        + '<div class="kmm-note">' + (d.disabled ? '自动调用已关闭，仍可手动触发。' : '允许模型自动调用。') + (!d.writable ? ' 此来源仅可查看。' : '') + '</div>'
        + '<pre>' + esc(d.body || '无正文') + '</pre>';
      if (d.writable) actions = '<div class="kmm-editor-actions"><button type="button" class="kmm-tbtn primary" id="ksk-edit">编辑技能</button></div>';
    }
    existing.innerHTML = html + '<details class="kmm-note kmm-file-location"><summary>文件位置</summary><code>' + esc(item.path || item.dir) + '</code></details>' + actions + '</div>';
    if (existing.querySelector('#ksk-edit')) existing.querySelector('#ksk-edit').onclick = function() { d.editing = true; d.initial = skillEditValue(); skillRenderDetail(); kmmFocusDetail(); };
    if (existing.querySelector('#ksk-cancel')) existing.querySelector('#ksk-cancel').onclick = async function() { await kmmDiscardAll(); };
    if (existing.querySelector('#ksk-save')) existing.querySelector('#ksk-save').onclick = skillSave;
    existing.querySelectorAll('input, textarea, button').forEach(function(el) { if (d.saving) el.disabled = true; });
    existing.oninput = function() { skillCapture(); kmmUpdateDirty(); };
    existing.onchange = existing.oninput;
    kmmRenderManagerUI();
  }
  async function skillSave() {
    var d = skillDetail;
    if (!d || d.saving || !d.editing || !d.writable) return;
    skillCapture();
    var payload = { key: d.creating ? '' : d.item.key, name: d.name.trim(), description: d.description.trim(), body: d.body, disabled: !!d.disabled };
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(payload.name)) d.error = '名称请使用字母、数字、点、下划线或连字符，以字母或数字开头，最多 64 个字符。';
    else if (!payload.description) d.error = '请填写用途描述。'; else d.error = '';
    if (d.error) { skillRenderDetail(); return; }
    d.saving = true; skillRenderDetail();
    try {
      var res = await skillSend('save', payload);
      if (skillDetail !== d) return;
      if (res.data.success) {
        d.saving = false; skillCloseDetail(true); kmmToast(res.data.message || '技能已保存');
        await skillFetch(); kmmRestoreFocus();
      } else d.error = res.data.message || '保存失败，输入仍保留。';
    } catch (e) { d.error = '未能确认保存结果，输入仍保留。请重新连接后核对。'; }
    finally { d.saving = false; if (skillDetail === d) skillRenderDetail(); kmmRenderManagerUI(); }
  }
  async function skillCloseDetail(force) {
    if (force !== true) { await kmmDiscardAll(); return; }
    skillDetail = null;
    var el = document.getElementById('ksk-overlay'); if (el) el.remove();
  }

  /* ================================================================
   * 价格设置弹窗（kpr- 前缀，按量 / 按次 两种计价）
   * ================================================================ */
  var kprOverlayEl = null;
  var kprAlias = '';
  var kprMode = 'volume';

  function kprEnsureOverlay() {
    if (kprOverlayEl) return kprOverlayEl;
    var o = document.createElement('div');
    o.id = 'kpr-overlay';
    o.innerHTML = `
      <div id="kpr-modal">
        <div class="kpr-kicker">USAGE PRICING</div>
        <h3>设置模型单价</h3>
        <div class="kpr-model">模型 · <b id="kpr-alias">--</b></div>
        <div class="kpr-label">计价方式</div>
        <div class="kpr-seg">
          <button id="kpr-mode-volume" class="active">按量（每百万 Token）</button>
          <button id="kpr-mode-call">按次（每次调用）</button>
        </div>
        <div id="kpr-volume-fields">
          <div class="kpr-grid" style="margin-top:12px;">
            <div class="kpr-field"><label>输入 Token 单价</label><input id="kpr-input" type="number" min="0" step="any" value="0"><div class="kpr-hint">¥ / 1M tokens</div></div>
            <div class="kpr-field"><label>输出 Token 单价</label><input id="kpr-output" type="number" min="0" step="any" value="0"><div class="kpr-hint">¥ / 1M tokens</div></div>
            <div class="kpr-field"><label>缓存命中单价</label><input id="kpr-cache-hit" type="number" min="0" step="any" value="0"><div class="kpr-hint">¥ / 1M tokens</div></div>
            <div class="kpr-field"><label>缓存写入单价</label><input id="kpr-cache-write" type="number" min="0" step="any" value="0"><div class="kpr-hint">¥ / 1M tokens，留空按输入价</div></div>
          </div>
        </div>
        <div id="kpr-call-fields" style="display:none;">
          <div class="kpr-field" style="margin-top:12px;"><label>每次调用价格</label><input id="kpr-price" type="number" min="0" step="any" value="0"><div class="kpr-hint">¥ / 次，与该次 Token 数无关</div></div>
        </div>
        <div class="kpr-foot">
          <button class="kpr-reset" id="kpr-reset">恢复默认</button>
          <button class="kpr-cancel" id="kpr-cancel">取消</button>
          <button class="kpr-save" id="kpr-save">保存</button>
        </div>
      </div>`;
    kmmEl('kmm-detail').appendChild(o); o.hidden = true;
    kprOverlayEl = o;
    var $q = function(id) { return o.querySelector('#' + id); };
    var error = document.createElement('div'); error.id = 'kpr-error'; error.setAttribute('role', 'alert'); $q('kpr-modal').appendChild(error);
    $q('kpr-cancel').onclick = async function() { await kmmDiscardAll(); };
    $q('kpr-mode-volume').onclick = function() { kprResetPending = false; kprSetMode('volume'); kmmUpdateDirty(); };
    $q('kpr-mode-call').onclick = function() { kprResetPending = false; kprSetMode('per_call'); kmmUpdateDirty(); };
    $q('kpr-save').onclick = kprSave;
    $q('kpr-reset').textContent = '使用默认计价';
    $q('kpr-reset').onclick = function() { kprResetPending = true; error.textContent = '点击保存后恢复默认计价。'; kmmUpdateDirty(); };
    o.oninput = function() { kprResetPending = false; kmmUpdateDirty(); };
    return o;
  }
  var kprInitial = '', kprResetPending = false;
  function kprSnapshot() {
    return kprOverlayEl ? JSON.stringify([kprMode, kprResetPending].concat(Array.from(kprOverlayEl.querySelectorAll('input')).map(function(el) { return el.value; }))) : '';
  }
  function kprSetMode(m) {
    kprMode = m;
    var o = kprOverlayEl;
    o.querySelector('#kpr-mode-volume').classList.toggle('active', m === 'volume');
    o.querySelector('#kpr-mode-call').classList.toggle('active', m === 'per_call');
    o.querySelector('#kpr-volume-fields').style.display = (m === 'volume') ? '' : 'none';
    o.querySelector('#kpr-call-fields').style.display = (m === 'per_call') ? '' : 'none';
  }
  window.__KMM_PRICE = async function(alias) {
    if (!await kmmDiscardAll()) return;
    var o = kprEnsureOverlay(); kprAlias = alias; kprResetPending = false;
    o.querySelector('#kpr-alias').textContent = alias;
    var m = (modelsData && (modelsData.models || []).find(function(x) { return x.alias === alias; })) || {}, p = m.pricing || null;
    o.querySelector('#kpr-input').value = p && p.input != null ? p.input : 20;
    o.querySelector('#kpr-output').value = p && p.output != null ? p.output : 100;
    o.querySelector('#kpr-cache-hit').value = p && p.cache_hit != null ? p.cache_hit : 2;
    o.querySelector('#kpr-cache-write').value = p && p.cache_write != null ? p.cache_write : '';
    o.querySelector('#kpr-price').value = p && p.price != null ? p.price : 0;
    o.querySelector('#kpr-error').textContent = '仅用于面板费用估算，不改变供应商实际收费。';
    kprSetMode(p ? p.mode : 'volume'); kprInitial = kprSnapshot();
    kmmMountDetail(o, 'price', '模型计价');
  };
  async function kprSave() {
    if (kmmBusy() || !apiAlive) return;
    var o = kprOverlayEl, body = { alias: kprAlias, mode: kprResetPending ? '' : kprMode };
    var fields = kprMode === 'per_call' ? { price: 'kpr-price' } : { input: 'kpr-input', output: 'kpr-output', cache_hit: 'kpr-cache-hit', cache_write: 'kpr-cache-write' };
    if (!kprResetPending) {
      for (var key of Object.keys(fields)) {
        var input = o.querySelector('#' + fields[key]), raw = input.value;
        if (key === 'cache_write' && raw === '') { body[key] = null; continue; }
        if (raw === '' || !Number.isFinite(Number(raw)) || Number(raw) < 0) { o.querySelector('#kpr-error').textContent = '请填写不小于 0 的有效价格。'; input.focus(); return; }
        body[key] = Number(raw);
      }
    }
    kmmQuickBusy = true; kmmRenderManagerUI();
    o.querySelectorAll('button, input').forEach(function(el) { el.disabled = true; });
    try {
      var response = await kapiFetch(API_BASE + '/api/set-price', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      var data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.message || '保存失败');
      o.hidden = true; kmmDetailMode = ''; kmmToast(kprResetPending ? '已恢复默认计价' : '计价已保存');
      await kmmRefreshAll(); kmmRestoreFocus();
    } catch (e) { o.querySelector('#kpr-error').textContent = e.message || '保存失败，输入仍保留。'; }
    finally { kmmQuickBusy = false; o.querySelectorAll('button, input').forEach(function(el) { el.disabled = false; }); kmmRenderManagerUI(); }
  }

  /* ================================================================
   * models.dev 价格同步弹窗（ksync- 前缀）
   * ================================================================ */
  var ksyncOverlayEl = null;
  var ksyncRows = [];

  function ksyncEnsureOverlay() {
    if (ksyncOverlayEl) return ksyncOverlayEl;
    var o = document.createElement('div');
    o.id = 'ksync-overlay';
    o.innerHTML = `
      <div id="ksync-modal">
        <div class="ksync-kicker">MODELS.DEV PRICING</div>
        <h3>从 models.dev 同步价格</h3>
        <div class="ksync-meta" id="ksync-meta">正在拉取 models.dev 目录并匹配本机模型…</div>
        <div class="ksync-rate">
          <span>汇率 USD→CNY</span>
          <input id="ksync-rate" type="number" min="0.01" step="any" value="7.1">
          <button id="ksync-rematch">重新匹配</button>
          <span class="ksync-hint" style="font-weight:400;color:var(--color-text-faint,#94a3b8);">改汇率后点「重新匹配」重算价格</span>
        </div>
        <div class="ksync-list" id="ksync-list"></div>
        <div class="ksync-foot">
          <button class="ksync-cancel" id="ksync-cancel">取消</button>
          <button class="ksync-apply" id="ksync-apply" disabled>写入选中价格</button>
        </div>
      </div>`;
    kmmEl('kmm-detail').appendChild(o); o.hidden = true;
    ksyncOverlayEl = o;
    o.querySelector('#ksync-cancel').onclick = async function() { await kmmDiscardAll(); };
    o.querySelector('#ksync-rematch').onclick = function() {
      var rate = parseFloat(o.querySelector('#ksync-rate').value);
      if (!(rate > 0)) { kmmToast('汇率需大于 0', true); return; }
      ksyncLoad(rate);
    };
    o.querySelector('#ksync-apply').onclick = ksyncApply;
    return o;
  }

  function ksyncRenderList() {
    var o = ksyncOverlayEl;
    var list = o.querySelector('#ksync-list');
    if (!ksyncRows.length) {
      list.innerHTML = '<div class="ksync-empty">没有可匹配的模型（先在 config.toml 中配置模型）</div>';
      o.querySelector('#ksync-apply').disabled = true;
      return;
    }
    list.innerHTML = ksyncRows.map(function(r, i) {
      var badge = r.matched_id
        ? '<span class="ksync-badge' + (r.exact ? '' : ' fuzzy') + '">' + (r.exact ? '精确' : '模糊 ' + Math.round(r.score * 100) + '%') + '</span>'
        : '<span class="ksync-badge none">未匹配</span>';
      var match = r.matched_id ? (r.provider + ' / ' + r.matched_id) : '目录中没有相似模型';
      var cost = r.cost ? ('¥' + r.cost.input + ' / ¥' + r.cost.output + ' / ¥' + r.cost.cache_hit) : '—';
      var row = '<label class="ksync-row' + (r.matched_id ? '' : ' unmatched') + '">'
        + '<input type="checkbox" data-i="' + i + '"' + (r.selected ? ' checked' : '') + (r.matched_id ? '' : ' disabled') + '>'
        + '<span class="ksync-alias" title="' + r.alias.replace(/"/g, '&quot;') + '">' + r.alias + '</span>'
        + badge
        + '<span class="ksync-match" title="' + match.replace(/"/g, '&quot;') + '">' + match + '</span>'
        + '<span class="ksync-cost" title="输入 / 输出 / 缓存命中（¥ per 1M）">' + cost + '</span>'
        + '</label>';
      return row;
    }).join('');
    list.querySelectorAll('input[type="checkbox"]').forEach(function(cb) {
      cb.onchange = function() {
        ksyncRows[parseInt(cb.getAttribute('data-i'), 10)].selected = cb.checked;
        ksyncUpdateApply();
      };
    });
    ksyncUpdateApply();
  }

  function ksyncUpdateApply() {
    var n = ksyncRows.filter(function(r) { return r.selected && r.matched_id; }).length;
    var apply = ksyncOverlayEl.querySelector('#ksync-apply');
    apply.disabled = n === 0;
    apply.textContent = n ? '写入选中价格（' + n + '）' : '写入选中价格';
  }

  var ksyncGeneration = 0, ksyncMatchedRate = 0;
  async function ksyncLoad(rate) {
    var o = ksyncOverlayEl, generation = ++ksyncGeneration;
    ksyncRows = []; ksyncMatchedRate = 0;
    o.querySelector('#ksync-apply').disabled = true;
    o.querySelector('#ksync-rematch').disabled = true;
    o.querySelector('#ksync-list').textContent = '正在获取参考价格并匹配模型…';
    try {
      var response = await kapiFetch(API_BASE + '/api/models-dev/match', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(rate ? { rate: rate } : {}) });
      var data = await response.json();
      if (generation !== ksyncGeneration || kmmDetailMode !== 'sync') return;
      if (!response.ok || !data.success) throw new Error(data.message || '匹配失败');
      ksyncRows = (data.rows || []).map(function(row) { row.selected = !!row.matched_id && !!row.exact; return row; });
      ksyncMatchedRate = Number(o.querySelector('#ksync-rate').value);
      o.querySelector('#ksync-meta').textContent = '已匹配 ' + ksyncRows.filter(function(row) { return row.matched_id; }).length + ' 个模型。默认仅选精确匹配，请核对参考价与实际收费。';
      ksyncRenderList();
    } catch (e) {
      if (generation === ksyncGeneration && kmmDetailMode === 'sync') o.querySelector('#ksync-list').textContent = e.message || '获取失败，请重试。';
    } finally { if (generation === ksyncGeneration) o.querySelector('#ksync-rematch').disabled = false; }
  }

  async function ksyncOpen() {
    kmmCloseTools();
    if (!apiAlive || !await kmmDiscardAll()) return;
    ksyncEnsureOverlay(); ksyncRows = [];
    ksyncOverlayEl.querySelector('#ksync-rate').value = 7.1;
    kmmMountDetail(ksyncOverlayEl, 'sync', '同步参考价格'); ksyncLoad(0);
  }
  async function ksyncApply() {
    if (kmmBusy()) return;
    if (Number(ksyncOverlayEl.querySelector('#ksync-rate').value) !== ksyncMatchedRate) { kmmToast('汇率已修改，请先重新匹配价格。', true); return; }
    var o = ksyncOverlayEl, rows = ksyncRows.filter(function(r) { return r.selected && r.matched_id; })
      .map(function(r) { return { alias: r.alias, selected: true, cost: r.cost }; });
    if (!rows.length || !await kmmConfirm('将更新 ' + rows.length + ' 个模型的估算价格。公开目录是参考价，可能与供应商实际收费不同。', '应用价格', '应用选中的参考价格？')) return;
    kmmSyncBusy = true; kmmRenderManagerUI();
    o.querySelectorAll('button, input').forEach(function(el) { el.disabled = true; });
    try {
      var response = await kapiFetch(API_BASE + '/api/models-dev/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rows: rows }) });
      var data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.message || '应用失败');
      o.hidden = true; kmmDetailMode = ''; kmmToast(data.message || '参考价格已保存'); await kmmRefreshAll(); kmmRestoreFocus();
    } catch (e) { kmmToast(e.message || '应用失败，请重试。', true); }
    finally {
      kmmSyncBusy = false;
      o.querySelectorAll('button, input').forEach(function(el) { el.disabled = false; });
      ksyncRenderList(); kmmRenderManagerUI();
    }
  }

  /* ================================================================
   * 数据流：__KIMI_DATA__ 推送 + script 轮询 + fetch 兜底
   * ================================================================ */
  function fmtTok(n) {
    n = Number(n) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(Math.round(n));
  }
  function fmtCost(n) {
    n = Number(n) || 0;
    if (n >= 1000) return '¥' + (n / 1000).toFixed(1) + 'k';
    if (n >= 100) return '¥' + Math.round(n);
    return '¥' + n.toFixed(2);
  }
  function pad2(n) { return ('0' + n).slice(-2); }
  function fmtDate(ms) {
    var dt = new Date(ms);
    return dt.getFullYear() + '-' + pad2(dt.getMonth() + 1) + '-' + pad2(dt.getDate());
  }
  function fmtDateTime(ms) {
    var dt = new Date(ms);
    return fmtDate(ms) + ' ' + pad2(dt.getHours()) + ':' + pad2(dt.getMinutes()) + ':' + pad2(dt.getSeconds());
  }

  // 把 embedded-data.js 的平铺 schema 归一化为面板 schema；
  // 已是面板 schema 则原样返回；两种都不是返回 null（丢弃，绝不让坏数据进 state）。
  function normalizeUsage(u, env) {
    if (!u || typeof u !== 'object') return null;
    if (u.today && typeof u.today.tokens_fmt === 'string') return u;
    var isFlat = Array.isArray(u.daily) || u.burn || u.files_count != null;
    if (!isFlat) return null;

    var timeMs = parseTimeMs(env && env.time) || parseTimeMs(u.updated_ms) || Date.now();
    var daily = Array.isArray(u.daily) ? u.daily : [];
    var todayStr = fmtDate(timeMs);
    var days30 = daily.map(function(d) {
      var dateStr = fmtDate(d.day || 0);
      return { label: dateStr.slice(5), date: dateStr, tokens: d.tokens || 0, tokens_fmt: fmtTok(d.tokens), calls: d.records || 0, cost: d.cost || 0 };
    });
    var todayRow = u.today || {};
    var allRow = u.all || {};
    var burn = u.burn || {};
    var yestStr = fmtDate(timeMs - 86400000);
    var yestRaw = null;
    daily.forEach(function(d) { if (fmtDate(d.day || 0) === yestStr) yestRaw = d; });
    var weekStart = new Date(timeMs);
    weekStart.setHours(0, 0, 0, 0);
    weekStart.setDate(weekStart.getDate() - ((weekStart.getDay() + 6) % 7));
    var monthStart = fmtDate(timeMs).slice(0, 7) + '-01';
    var weekStartStr = fmtDate(weekStart.getTime());
    function aggSince(startStr) {
      var t = 0, c = 0, r = 0, i = 0, o = 0, cr = 0;
      daily.forEach(function(d) {
        var ds = fmtDate(d.day || 0);
        if (ds >= startStr && ds <= todayStr) {
          t += d.tokens || 0; c += d.cost || 0; r += d.records || 0;
          i += d.input || 0; o += d.output || 0; cr += 0;
        }
      });
      return { tokens: t, cost: c, records: r, input: i, output: o };
    }
    var wk = aggSince(weekStartStr);
    var mo = aggSince(monthStart);
    var cachePct = (u.cache_pct != null ? u.cache_pct : 0);
    function period(row, dateStr, calls) {
      return {
        tokens_fmt: fmtTok(row.tokens), cost_fmt: fmtCost(row.cost), calls: calls || 0,
        in_fmt: fmtTok(row.input), out_fmt: fmtTok(row.output), cache_pct: cachePct, date: dateStr
      };
    }
    // 按日分模型聚合：昨日 / 本周（daily_models: {day_ms_str: [rows]}）
    var dailyModels = (u.daily_models && typeof u.daily_models === 'object') ? u.daily_models : {};
    function modelsOnDay(dateStr) {
      var out = [];
      Object.keys(dailyModels).forEach(function(k) {
        if (fmtDate(parseInt(k, 10) || 0) === dateStr) out = out.concat(dailyModels[k] || []);
      });
      return out;
    }
    function modelsSince(startStr) {
      var byModel = {};
      Object.keys(dailyModels).forEach(function(k) {
        var ds = fmtDate(parseInt(k, 10) || 0);
        if (ds < startStr || ds > todayStr) return;
        (dailyModels[k] || []).forEach(function(m) {
          var key = m.model || '?';
          var acc = byModel[key] || (byModel[key] = { model: key, tokens: 0, input: 0, output: 0, calls: 0, cost: 0, cache_read: 0, cache_reported: false });
          acc.tokens += m.tokens || 0; acc.input += m.input || 0; acc.output += m.output || 0;
          acc.calls += (m.records != null ? m.records : m.calls) || 0; acc.cost += m.cost || 0;
          acc.cache_read += m.cache_read || 0;
          if (m.cache_reported) acc.cache_reported = true;
        });
      });
      var rows = Object.keys(byModel).map(function(k) {
        var a = byModel[k];
        a.hit = a.input > 0 ? a.cache_read / a.input : 0;
        a.cache_pct = Math.round(a.hit * 1000) / 10;
        return a;
      });
      rows.sort(function(a, b) { return b.tokens - a.tokens; });
      return rows;
    }
    var hourlyRows = (Array.isArray(u.hourly) ? u.hourly : []).map(function(h) {
      return { hour: h.hour || 0, tokens: h.tokens || 0, calls: h.records || 0, tokens_fmt: fmtTok(h.tokens) };
    });
    var sessionRows = (Array.isArray(u.sessions) ? u.sessions : []).map(function(s) {
      var key = s.key || '';
      return {
        key: key, short: key.replace(/^session_/, '').slice(0, 8),
        tokens: s.tokens || 0, tokens_fmt: fmtTok(s.tokens), calls: s.records || 0,
        cache_pct: Math.round((s.hit || 0) * 1000) / 10,
        cache_reported: (s.cache_reported !== false),
        cost: s.cost || 0, cost_fmt: fmtCost(s.cost),
        last: s.last ? fmtDateTime(s.last).slice(5) : '--'
      };
    }).slice(0, 8);
    function mapModels(list) {
      return (Array.isArray(list) ? list : []).map(function(m) {
        return {
          model: m.model || '--', tokens: m.tokens || 0, tokens_fmt: fmtTok(m.tokens),
          calls: (m.calls != null ? m.calls : m.records) || 0,
          cache_pct: (m.cache_pct != null ? m.cache_pct : (m.hit != null ? Math.round(m.hit * 1000) / 10 : 0)),
          cache_reported: (m.cache_reported !== false),
          cost: m.cost || 0, cost_fmt: fmtCost(m.cost),
          input: m.input || 0, output: m.output || 0,
          in_fmt: (m.in_fmt != null) ? m.in_fmt : fmtTok(m.input),
          out_fmt: (m.out_fmt != null) ? m.out_fmt : fmtTok(m.output)
        };
      });
    }
    var tps = (u.tps != null ? u.tps : 0);
    return {
      updated_at: (timeMs ? fmtDateTime(timeMs) : (u.updated_at || '--')),
      header: { tokens_fmt: fmtTok(todayRow.tokens), cost_fmt: fmtCost(todayRow.cost), cache_pct: Math.round(cachePct) + '%', speed_tps: Math.round(tps) + ' t/s' },
      today: period(todayRow, todayStr, todayRow.records),
      yesterday: (function() {
        var p = period(yestRaw || {}, yestStr, (yestRaw && yestRaw.records) || 0);
        p.models = mapModels(modelsOnDay(yestStr));
        return p;
      })(),
      week: (function() {
        var p = period(wk, weekStartStr, wk.records);
        p.models = mapModels(modelsSince(weekStartStr));
        return p;
      })(),
      month: (function() {
        var p = period(mo, monthStart, mo.records);
        p.models = mapModels(modelsSince(monthStart));
        return p;
      })(),
      cumul: period(allRow, '', allRow.records || 0),
      rate: { tokens_per_hour: fmtTok(burn.token_rate_hour) + '/h', cost_per_hour: fmtCost(burn.cost_rate_hour) + '/h', window: '近60分钟' },
      cache: { pct: cachePct, pct_fmt: Math.round(cachePct) + '%' },
      speed: { tps: Math.round(tps) + ' t/s', model: u.tps_model || '--', avg_tps: '--' },
      cur: { alias: (env && env.models && env.models.default_model) || u.tps_model || '--', model: '', effort: '', session: '', sub_agent: '' },
      session: null,
      quota: null,
      days30: days30,
      hourly: hourlyRows,
      timeseries: null,
      today_models: mapModels(u.models_today),
      cumul_models: mapModels(u.models_all),
      sessions: sessionRows,
      footer: { file_count: u.files_count || 0, session_count: u.sessions_total || sessionRows.length, time: u.updated_at || '' }
    };
  }

  function processData(d) {
    if (!d) return;
    if (d.usage && !d.usage.error) {
      var u = normalizeUsage(d.usage, d);
      if (u) {
        state = u;
        lastDataTime = parseTimeMs(d.time) || parseTimeMs(u.updated_at);
        serviceOnline = (lastDataTime > 0 && (Date.now() - lastDataTime) <= STALE_MS) || apiAlive;
        if (kuDataChanged(u)) {
          updateDOM();
          kupRenderAll();
        }
      }
    } else if (d.usage && d.usage.error) {
      serviceOnline = false;
      applyServiceFlag();
    }
    if (d.models) {
      var mSig;
      try { mSig = JSON.stringify(d.models); } catch (e) { mSig = null; }
      if (mSig !== null && mSig !== kuLastModelsSig) {
        kuLastModelsSig = mSig;
        modelsData = d.models;
        if (isModelModalOpen) kmmRenderModalCards();
        updateDOM();
      }
    }
  }

  // 数据签名：忽略每次写盘都会变的时间戳，内容未变则跳过重渲染（避免周期性抖动）
  var kuLastSig = '';
  var kuLastModelsSig = '';
  var kuLastOnline = null;
  function kuDataChanged(u) {
    var sig;
    try {
      sig = JSON.stringify(u, function(k, v) {
        return (k === 'updated_at' || k === 'time') ? undefined : v;
      });
    } catch (e) { return true; }
    var changed = sig !== kuLastSig || kuLastOnline !== serviceOnline;
    kuLastSig = sig;
    kuLastOnline = serviceOnline;
    return changed;
  }

  // 数据文件重写后的实时回调（链式：不覆盖其他 widget 的回调）
  var kuPrevDataUpdate = window.__KMM_ON_DATA_UPDATE__;
  window.__KMM_ON_DATA_UPDATE__ = function(data) {
    processData(data);
    if (typeof kuPrevDataUpdate === 'function') {
      try { kuPrevDataUpdate(data); } catch (e) {}
    }
  };

  // 每 2s 重载数据脚本（同源 script 注入，无 CSP 问题）
  function reloadDataScript() {
    var old = document.getElementById('ku-dynamic-data-script');
    if (old) old.remove();
    var s = document.createElement('script');
    s.id = 'ku-dynamic-data-script';
    s.src = '/assets/kimi-embedded-data.js?t=' + Date.now();
    document.head.appendChild(s);
  }

  // fetch /kimi-usage.json 兜底
  async function fetchData() {
    try {
      const res = await fetch('/kimi-usage.json?t=' + Date.now(), { cache: 'no-store' });
      if (res.ok) {
        const d = await res.json();
        var u = normalizeUsage(d, null);
        if (u) {
          var t = parseTimeMs(u.updated_at) || parseTimeMs(d.time);
          // 仅在比内存数据更新时采纳
          if (t > 0 && (!lastDataTime || t > lastDataTime)) {
            state = u;
            lastDataTime = t;
            if (!serviceOnline) {
              serviceOnline = lastDataTime > 0 && (Date.now() - lastDataTime) <= STALE_MS;
            }
            if (kuDataChanged(u)) {
              updateDOM();
              kupRenderAll();
            }
          }
        }
      }
    } catch (e) {
      // 兜底失败则依赖 __KIMI_DATA__ 缓存
    }
  }

  // 热更新提示：插件文件变化后提示刷新界面（不打断、不自动刷新）
  var widgetDigest = '';
  function showReloadBanner() {
    if (document.getElementById('kud-reload')) return;
    var b = document.createElement('div');
    b.id = 'kud-reload';
    b.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:100004;background:#1d4ed8;color:#fff;'
      + 'padding:10px 14px;border-radius:10px;font:13px/1.4 system-ui,sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.25);'
      + 'display:flex;gap:10px;align-items:center';
    b.innerHTML = '<span>用量面板已更新</span>'
      + '<button style="border:0;border-radius:6px;padding:3px 10px;cursor:pointer;background:#fff;color:#1d4ed8">刷新界面</button>'
      + '<button style="border:0;background:transparent;color:#fff;cursor:pointer;opacity:.8">稍后</button>';
    var btns = b.getElementsByTagName('button');
    btns[0].onclick = function() { location.reload(); };
    btns[1].onclick = function() { b.remove(); };
    document.body.appendChild(b);
  }

  // 探活：/api/status
  function probeService() {
    kapiFetch(`${API_BASE}/api/status`, { cache: 'no-store' })
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(d) {
        apiAlive = !!(d && (d.status === 'ok' || d.name));
        if (apiAlive && d.version && kupLs('kimi-usage-upd-ver') !== d.version) {
          kupLs('kimi-usage-upd-ver', d.version);
          kupLs(KUP_FOUND_KEY, null); kupLs(KUP_LAST_KEY, null); kupDot(false);
        }
        if (apiAlive && d.widget) {
          if (!widgetDigest) widgetDigest = d.widget;
          else if (d.widget !== widgetDigest) showReloadBanner();
        }
        if (apiAlive && !serviceOnline) { serviceOnline = true; applyServiceFlag(); }
      })
      .catch(function() { apiAlive = false; });
  }

  // 鲜活性心跳：数据超过 15s 未更新则标灰
  function heartbeatCheck() {
    var stale = !(lastDataTime > 0 && (Date.now() - lastDataTime) <= STALE_MS);
    var online = !stale || apiAlive;
    if (online !== serviceOnline) {
      serviceOnline = online;
      applyServiceFlag();
    }
  }

  /* ================================================================
   * 初始化
   * ================================================================ */
  // 立即渲染同步已有的推送数据
  if (window.__KIMI_DATA__) {
    try { processData(window.__KIMI_DATA__); } catch (e) {}
  }

  // DOM 变动监听（侧栏重建时自愈重挂）
  var observer = new MutationObserver(function() {
    attachWidget();
  });

  function init() {
    observer.observe(document.body, { childList: true, subtree: true });
    attachWidget();
    if (window.__KIMI_DATA__) updateDOM();
    reloadDataScript();
    fetchData();
    probeService();
    setTimeout(function() { kupDailyCheck(false); }, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // 2s 数据轮询 + fetch 兜底 + 3s 心跳 + 10s 探活
  setInterval(reloadDataScript, 2000);
  setInterval(fetchData, 2000);
  setInterval(heartbeatCheck, 3000);
  setInterval(probeService, 10000);
  setInterval(function() { kupDailyCheck(false); }, 3600 * 1000);

  console.log('Kimi Code 用量一体化组件 v3.1 已初始化');
})();
