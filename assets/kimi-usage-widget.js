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
    .kmm-toolbar { padding: 0 22px 14px; display: flex; gap: 10px; align-items: center; }
    .kmm-search {
      flex: 1; min-width: 0; box-sizing: border-box;
      background: var(--color-surface-sunken, #121212);
      border: 1px solid transparent; color: var(--color-text, #e2e8f0);
      border-radius: 9px; padding: 8px 12px; font-size: 12.5px; outline: none; font-family: inherit;
    }
    .kmm-search:focus { border-color: var(--color-accent, #1a88ff); }
    .kmm-tbtn {
      padding: 8px 14px; border-radius: 9px; font-size: 12.5px; font-weight: 600; cursor: pointer;
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 14%, transparent);
      background: transparent; color: var(--color-text, #e2e8f0); font-family: inherit; white-space: nowrap;
    }
    .kmm-tbtn:hover { border-color: var(--color-accent, #1a88ff); color: var(--color-accent, #1a88ff); }
    .kmm-tbtn.primary { background: var(--color-accent, #1a88ff); border-color: var(--color-accent, #1a88ff); color: #fff; }
    .kmm-tbtn.primary:hover { filter: brightness(1.08); color: #fff; }
    .kmm-modal-body {
      padding: 14px 22px 18px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
      border-top: 1px solid color-mix(in srgb, var(--color-text, #fff) 7%, transparent);
    }
    .kmm-foot {
      padding: 10px 22px; font-size: 11.5px; color: var(--color-text-faint, #94a3b8);
      display: flex; justify-content: space-between; gap: 12px;
      border-top: 1px solid color-mix(in srgb, var(--color-text, #fff) 7%, transparent);
    }
    .kmm-card {
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 9%, transparent);
      border-radius: 12px;
      padding: 13px 16px 12px;
      background: var(--color-surface-raised, #292929);
      transition: border-color .15s, box-shadow .15s;
    }
    .kmm-card:hover { box-shadow: 0 2px 10px rgba(0,0,0,.06); }
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
    .kmm-acts { display: flex; gap: 4px; flex-shrink: 0; }
    .kmm-link {
      background: transparent; border: none; cursor: pointer; font-size: 12px; font-family: inherit;
      color: var(--color-text-secondary, #64748b); padding: 3px 8px; border-radius: 7px;
    }
    .kmm-link:hover { background: color-mix(in srgb, var(--color-text, #fff) 7%, transparent); color: var(--color-text, #e2e8f0); }
    .kmm-link.accent { color: var(--color-accent, #1a88ff); }
    .kmm-meta { margin-top: 3px; font-size: 11.5px; color: var(--color-text-faint, #94a3b8); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .kmm-ctl { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-top: 11px; }
    .kmm-chips { display: flex; gap: 6px; }
    .kmm-chip {
      display: inline-flex; align-items: center; cursor: pointer; user-select: none;
      padding: 4px 12px; border-radius: 999px; font-size: 12px; font-weight: 500;
      border: 1px solid color-mix(in srgb, var(--color-text, #fff) 14%, transparent);
      color: var(--color-text-faint, #94a3b8); background: transparent; transition: all .15s;
    }
    .kmm-chip input { display: none; }
    .kmm-chip.on {
      color: var(--color-accent, #1a88ff); font-weight: 600;
      border-color: color-mix(in srgb, var(--color-accent, #1a88ff) 45%, transparent);
      background: color-mix(in srgb, var(--color-accent, #1a88ff) 10%, transparent);
    }
    .kmm-chip.lock { cursor: not-allowed; opacity: .75; }
    .kmm-eff { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .kmm-eff-label { font-size: 11.5px; color: var(--color-text-faint, #94a3b8); }
    .kmm-seg {
      display: inline-flex; padding: 2px; border-radius: 9px; gap: 1px;
      background: var(--color-surface-sunken, #121212);
    }
    .kmm-seg button {
      border: none; background: transparent; cursor: pointer; font-family: inherit;
      padding: 3px 10px; border-radius: 7px; font-size: 11.5px; font-weight: 500;
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
    #kud-cancel { background: var(--color-surface-sunken, #eef0f2); color: var(--color-text-secondary, #64748b); }
    #kud-ok { background: var(--color-accent, #1a88ff); color: #fff; }
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
      z-index: 100001;
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
      if (!e.target.closest('#ku-min-panel-btn') && !e.target.closest('#ku-min-gear-btn')) {
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
    fetch(API_BASE + '/api/update/check', { cache: 'no-store' })
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
        '<div class="kud-ver">当前版本 v<span id="kud-cur"></span><br>最新版本 <b>v<span id="kud-new"></span></b></div>' +
        '<div class="kud-tip">更新后后台服务会自动重启，几秒钟后恢复。</div>' +
        '<div class="kud-foot"><button id="kud-cancel">稍后</button><button id="kud-ok">立即更新</button></div>' +
        '</div>';
      document.body.appendChild(o);
      o.onclick = function(e) { if (e.target === o) kuLayerHide(o, 'visible'); };
      o.querySelector('#kud-cancel').onclick = function() { kuLayerHide(o, 'visible'); };
    }
    o.querySelector('#kud-cur').textContent = info.current;
    o.querySelector('#kud-new').textContent = info.latest;
    o.querySelector('#kud-ok').onclick = function() { kuLayerHide(o, 'visible'); onOk(); };
    kuLayerShow(o, 'visible');
  }

  function kupApplyUpdate() {
    kupUpdating = true;
    kupUpdateBadge();
    fetch(API_BASE + '/api/update/apply', { method: 'POST' })
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
    fetch(API_BASE + '/api/update/check?force=1', { cache: 'no-store' })
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

  /* ================================================================
   * 模型管理弹窗（移植自 kimi-embedded-widget.js，kmm- 前缀）
   * ================================================================ */
  function ensureModelOverlay() {
    if (kmmOverlayEl) return kmmOverlayEl;
    var overlay = document.createElement('div');
    overlay.id = KMM_OVERLAY_ID;
    overlay.innerHTML = `
      <div id="kmm-modal">
        <div class="kmm-modal-header">
          <div class="kmm-modal-title">
            <h2>模型与能力</h2>
            <span class="kmm-def-badge" id="kmm-modal-default-badge">默认：加载中</span>
          </div>
          <button class="kmm-x" id="kmm-modal-close-btn" title="关闭">✕</button>
        </div>
        <div class="kmm-toolbar">
          <input class="kmm-search" id="kmm-modal-filter" placeholder="搜索模型、渠道…">
          <button class="kmm-tbtn" id="kmm-modal-refresh" title="重新读取 config.toml">刷新</button>
          <button class="kmm-tbtn primary" id="kmm-modal-auto-all" title="为所有模型补全识图 / 思考 / 工具调用能力标签">一键补全能力</button>
        </div>
        <div class="kmm-modal-body" id="kmm-modal-cards"></div>
        <div class="kmm-foot">
          <span>修改会自动备份并校验，在会话中输入 /reload 立即生效</span>
          <span>端口 39281</span>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    kmmOverlayEl = overlay;

    var $o = function(id) { return overlay.querySelector('#' + id); };
    $o('kmm-modal-close-btn').onclick = closeModelModal;
    overlay.onclick = function(e) { if (e.target === overlay) closeModelModal(); };
    $o('kmm-modal-refresh').onclick = fetchModels;
    $o('kmm-modal-filter').oninput = kmmRenderModalCards;
    $o('kmm-modal-auto-all').onclick = async function() {
      if (!confirm('确定要为全部模型补全【识图 + 深度思考 + 工具调用】能力标签吗？（不会改动思考强度档位）')) return;
      try {
        const res = await fetch(`${API_BASE}/api/auto-enable-all`, { method: 'POST' });
        const data = await res.json();
        if (data.success) {
          kmmToast('🎉 全部模型能力已全开并校验通过！');
          fetchModels();
        } else kmmToast(data.message, true);
      } catch (e) { kmmToast('执行失败: 后台服务未响应', true); }
    };
    return overlay;
  }

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

  function openModelModal() {
    var overlay = ensureModelOverlay();
    kuLayerShow(overlay, 'visible');
    isModelModalOpen = true;
    // 卡片渲染走空闲回调，overlay 先淡入
    kuScheduleIdle(function() { kmmRenderModalCards(); });
    fetchModels();
    if (!apiAlive) {
      kmmToast('⚠️ 后台服务离线：当前为缓存数据只读展示', true);
    }
  }
  function closeModelModal() {
    if (kmmOverlayEl) kuLayerHide(kmmOverlayEl, 'visible');
    isModelModalOpen = false;
  }

  function fetchModels() {
    // 优先本地服务 API，失败回退 __KIMI_DATA__ 缓存
    return fetch(`${API_BASE}/api/data`)
      .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(d) {
        modelsData = d;
        apiAlive = true;
        if (isModelModalOpen) kmmRenderModalCards();
      })
      .catch(function() {
        apiAlive = false;
        if (window.__KIMI_DATA__ && window.__KIMI_DATA__.models) {
          modelsData = window.__KIMI_DATA__.models;
          if (isModelModalOpen) kmmRenderModalCards();
        }
      });
  }

  function kmmRenderModalCards() {
    if (!modelsData || !kmmOverlayEl) return;
    var $o = function(id) { return kmmOverlayEl.querySelector('#' + id); };
    var badge = $o('kmm-modal-default-badge');
    if (badge) {
      var def = modelsData.default_model || '';
      var defM = (modelsData.models || []).find(function(m) { return m.alias === def; });
      badge.innerHTML = '默认：<b>' + esc((defM && defM.display_name) || def || '--') + '</b>';
    }
    var container = $o('kmm-modal-cards');
    if (!container) return;
    var filterEl = $o('kmm-modal-filter');
    var kw = filterEl ? filterEl.value.trim().toLowerCase() : '';
    container.innerHTML = '';
    var au = modelsData.effort_audit || {};
    if (au.error || au.warn) {
      var sum = document.createElement('div');
      sum.style.cssText = 'font-size:12px;padding:8px 12px;border-radius:8px;margin-bottom:8px;border:1px solid color-mix(in srgb,var(--color-warning,#d29922) 40%,transparent);background:color-mix(in srgb,var(--color-warning,#d29922) 10%,transparent);color:var(--color-text,#e5e7eb);';
      sum.textContent = '思考强度检查：' + (au.error ? au.error + ' 个错误' : '') + (au.error && au.warn ? '、' : '') + (au.warn ? au.warn + ' 个警告' : '') + '，详见各模型卡片';
      container.appendChild(sum);
    }

    var lastUsed = (state.cur && state.cur.alias && state.cur.alias !== '--') ? state.cur.alias : '';
    var list = (modelsData.models || []).slice().sort(function(a, b) {
      var sa = (a.alias === lastUsed ? 0 : a.is_default ? 1 : 2);
      var sb = (b.alias === lastUsed ? 0 : b.is_default ? 1 : 2);
      return sa - sb;
    });

    var shown = 0;
    list.forEach(function(m) {
      var hay = (m.alias + ' ' + (m.display_name || '') + ' ' + m.model + ' ' + m.provider).toLowerCase();
      if (kw && hay.indexOf(kw) < 0) return;
      shown++;
      var isCur = (m.alias === lastUsed && lastUsed);
      var card = document.createElement('div');
      card.className = 'kmm-card ' + ((m.is_default || isCur) ? 'is-default' : '');
      var ctxK = Math.round((m.max_context_size || 0) / 1024);
      // 档位优先级：服务端 effective_efforts（已合并 overrides）> support_efforts > 官方五档兜底
      var rawEfforts = (m.effective_efforts && m.effective_efforts.length) ? m.effective_efforts
                     : (m.support_efforts && m.support_efforts.length) ? m.support_efforts
                     : (m.has_thinking ? ['low', 'medium', 'high', 'xhigh', 'max'] : null);
      var efforts = rawEfforts ? rawEfforts.filter(function(l) { return l !== 'none'; }) : null;
      if (efforts && !efforts.length) efforts = null;
      var effOutOfList = !!(efforts && m.default_effort && efforts.indexOf(m.default_effort) < 0);
      var selTitle = (m.efforts_source === 'config' ? '档位来自本机配置' : '档位为默认兜底（config.toml 未声明 support_efforts）');
      var issues = m.effort_issues || [];
      var infoTitle = issues.filter(function(it) { return it.level === 'info'; }).map(function(it) { return it.msg; }).join('\n');
      var actualHtml = (m.effective_effort && m.has_thinking)
        ? '<span class="kmm-note" title="' + esc(infoTitle || '主 Agent 实际使用的思考档位') + '">实际生效 ' + esc(m.effective_effort) + (infoTitle ? ' ⓘ' : '') + '</span>'
        : '';
      var alwaysOn = !!m.always_thinking;
      var a = JSON.stringify(m.alias);
      var warnHtml = issues.filter(function(it) { return it.level !== 'info'; }).map(function(it) {
        var c = it.level === 'error' ? 'var(--color-danger,#f85149)' : 'var(--color-warning,#d29922)';
        return '<div class="kmm-warn" style="color:' + c + ';">' + (it.level === 'error' ? '✖ ' : '⚠ ') + esc(it.msg) + '</div>';
      }).join('');
      var chip = function(cap, label, on, locked, tip) {
        return '<label class="kmm-chip' + (on ? ' on' : '') + (locked ? ' lock' : '') + '"' + (tip ? ' title="' + esc(tip) + '"' : '') + '>'
          + '<input type="checkbox" ' + (on ? 'checked ' : '') + (locked ? 'disabled ' : '')
          + "onchange='window.__KMM_TOGGLE(" + a + ", \"" + cap + "\", this.checked)'>" + label + '</label>';
      };
      var segHtml;
      if (efforts) {
        segHtml = '<div class="kmm-seg" title="' + esc(selTitle) + '">'
          + efforts.map(function(lvl) {
              var on = m.default_effort === lvl;
              return '<button class="' + (on ? 'on' : '') + '" ' + (on ? '' : "onclick='window.__KMM_EFFORT(" + a + ", \"" + lvl + "\")'") + '>' + lvl + '</button>';
            }).join('')
          + '</div>';
      } else {
        segHtml = '<span class="kmm-note">该模型未开启思考</span>';
      }
      card.innerHTML =
        '<div class="kmm-top">'
        + '<div class="kmm-name"><strong>' + esc(m.display_name || m.alias) + '</strong>'
        + (isCur ? '<span class="kmm-tag cur">使用中</span>' : '')
        + (m.is_default ? '<span class="kmm-tag def">默认</span>' : '')
        + (alwaysOn ? '<span class="kmm-tag on" title="always_thinking=true：思考常开，不可关闭">思考常开</span>' : '')
        + (!alwaysOn && m.adaptive_thinking ? '<span class="kmm-tag ad" title="adaptive_thinking=true：自适应思考">自适应</span>' : '')
        + '</div>'
        + '<div class="kmm-acts">'
        + '<button class="kmm-link" title="' + (m.pricing ? '自定义计价中' : '未设置，按默认单价估算') + "\" onclick='window.__KMM_PRICE(" + a + ")'>"
        + (m.pricing ? (m.pricing.mode === 'per_call' ? '按次计价' : '按量计价') : '设置价格') + '</button>'
        + (!m.is_default ? "<button class=\"kmm-link accent\" onclick='window.__KMM_SET_DEFAULT(" + a + ")'>设为默认</button>" : '')
        + '</div></div>'
        + '<div class="kmm-meta">' + esc(m.provider) + ' · ' + esc(m.model) + ' · ' + ctxK + 'k 上下文</div>'
        + '<div class="kmm-ctl">'
        + '<div class="kmm-chips">'
        + chip('image_in', '识图', !!m.has_image, false, '')
        + chip('thinking', '思考', !!(m.has_thinking || alwaysOn), alwaysOn, alwaysOn ? '思考常开，不可关闭' : '')
        + chip('tool_use', '工具', !!m.has_tools, false, '')
        + '</div>'
        + '<div class="kmm-eff"><span class="kmm-eff-label">思考强度</span>' + segHtml
        + (effOutOfList ? '<span class="kmm-warn" style="margin:0;color:var(--color-warning,#d29922);">⚠ 当前 ' + esc(m.default_effort) + ' 不在列表</span>' : '')
        + actualHtml
        + '</div></div>'
        + warnHtml;
      container.appendChild(card);
    });
    if (shown === 0) {
      var empty = document.createElement('div');
      empty.style.cssText = 'text-align:center;color:var(--color-text-faint,#64748b);font-size:12px;padding:18px 0;';
      empty.textContent = '没有匹配的模型';
      container.appendChild(empty);
    }
  }

  // 全局操作回调（供弹窗内联 onclick/onchange 调用）
  window.__KMM_SET_DEFAULT = async function(alias) {
    try {
      const res = await fetch(`${API_BASE}/api/set-default`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ alias: alias })
      });
      const data = await res.json();
      if (data.success) {
        kmmToast(`已设 ${alias} 为默认模型`);
        fetchModels();
      } else kmmToast(data.message, true);
    } catch (e) { kmmToast('操作失败: 后台服务未响应', true); }
  };

  window.__KMM_TOGGLE = async function(alias, capability, enabled) {
    try {
      const res = await fetch(`${API_BASE}/api/toggle-capability`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ alias: alias, capability: capability, enabled: enabled })
      });
      const data = await res.json();
      if (data.success) {
        kmmToast(`已${enabled ? '开启' : '关闭'} ${capability}`);
        fetchModels();
      } else kmmToast(data.message, true);
    } catch (e) { kmmToast('保存失败: 后台服务未响应', true); }
  };

  window.__KMM_EFFORT = async function(alias, defaultEffort) {
    try {
      const res = await fetch(`${API_BASE}/api/update-model`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ alias: alias, updates: { default_effort: defaultEffort } })
      });
      const data = await res.json();
      if (data.success) {
        kmmToast(data.note ? `已设 ${defaultEffort}。${data.note}` : `思考强度已设为 ${defaultEffort}`);
        fetchModels();
      } else kmmToast(data.message, true);
    } catch (e) { kmmToast('更新失败: 后台服务未响应', true); }
  };

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
    document.body.appendChild(o);
    kprOverlayEl = o;
    var $q = function(id) { return o.querySelector('#' + id); };
    $q('kpr-cancel').onclick = function() { kuLayerHide(o, 'visible'); };
    o.onclick = function(e) { if (e.target === o) kuLayerHide(o, 'visible'); };
    $q('kpr-mode-volume').onclick = function() { kprSetMode('volume'); };
    $q('kpr-mode-call').onclick = function() { kprSetMode('per_call'); };
    $q('kpr-save').onclick = function() { kprSave(false); };
    $q('kpr-reset').onclick = function() { kprSave(true); };
    return o;
  }

  function kprSetMode(m) {
    kprMode = m;
    var o = kprOverlayEl;
    o.querySelector('#kpr-mode-volume').classList.toggle('active', m === 'volume');
    o.querySelector('#kpr-mode-call').classList.toggle('active', m === 'per_call');
    o.querySelector('#kpr-volume-fields').style.display = (m === 'volume') ? '' : 'none';
    o.querySelector('#kpr-call-fields').style.display = (m === 'per_call') ? '' : 'none';
  }

  window.__KMM_PRICE = function(alias) {
    var o = kprEnsureOverlay();
    kprAlias = alias;
    o.querySelector('#kpr-alias').textContent = alias;
    var m = (modelsData && (modelsData.models || []).find(function(x) { return x.alias === alias; })) || {};
    var p = m.pricing || null;
    o.querySelector('#kpr-input').value = (p && p.input != null) ? p.input : 20;
    o.querySelector('#kpr-output').value = (p && p.output != null) ? p.output : 100;
    o.querySelector('#kpr-cache-hit').value = (p && p.cache_hit != null) ? p.cache_hit : 2;
    o.querySelector('#kpr-cache-write').value = (p && p.cache_write != null) ? p.cache_write : '';
    o.querySelector('#kpr-price').value = (p && p.price != null) ? p.price : 0;
    kprSetMode(p ? p.mode : 'volume');
    kuLayerShow(o, 'visible');
    if (!apiAlive) kmmToast('⚠️ 后台服务离线，无法保存', true);
  };

  function kprSave(reset) {
    var body = reset ? { alias: kprAlias, mode: '' }
      : kprMode === 'per_call'
        ? { alias: kprAlias, mode: 'per_call', price: parseFloat(kprOverlayEl.querySelector('#kpr-price').value) || 0 }
        : {
            alias: kprAlias, mode: 'volume',
            input: parseFloat(kprOverlayEl.querySelector('#kpr-input').value) || 0,
            output: parseFloat(kprOverlayEl.querySelector('#kpr-output').value) || 0,
            cache_hit: parseFloat(kprOverlayEl.querySelector('#kpr-cache-hit').value) || 0,
            cache_write: kprOverlayEl.querySelector('#kpr-cache-write').value === '' ? null : parseFloat(kprOverlayEl.querySelector('#kpr-cache-write').value)
          };
    fetch(`${API_BASE}/api/set-price`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (data.success) {
        kmmToast(reset ? '已恢复默认计价' : '价格已保存');
        kuLayerHide(kprOverlayEl, 'visible');
        fetchModels();
      } else kmmToast(data.message || '保存失败', true);
    }).catch(function() { kmmToast('保存失败: 后台服务未响应', true); });
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
    fetch(`${API_BASE}/api/status`, { cache: 'no-store' })
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
