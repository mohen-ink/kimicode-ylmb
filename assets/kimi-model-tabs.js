/**
 * kimi-model-tabs.js — 发送框模型快捷菜单增强（v2）
 *
 * 复刻官方 composer 模型小菜单的布局：分组模型行 + 思考档位行 + 缓存提示。
 * 与官方的差异仅两处：
 *   1. 顶部增加供应商标签条（去掉「全部」，初始定位到当前模型的供应商）
 *   2. 去掉「更多模型…」行（官方该入口指向全屏 ModelPicker，注入端无触发通道）
 *
 * 定位：面板底边贴住 model-pill 顶边（bottom 锚定），切换供应商后高度变化不动底边。
 *
 * 数据与行为全部走官方运行时：
 *   - 模型/收藏：Pinia「kimi.models」（models/starredModelIds/toggleStarModel）
 *   - 会话/乐观更新：Pinia「kimi.sessions」（sessions/activeSessionId/updateSession）
 *   - 思考档位：Pinia「kimi.mode」（thinking/thinkingBySession，写法与官方 setThinking 一致）
 *   - 服务端持久化：__kimiAppClientDeps.current.api().updateSession()
 *   - 默认模型联动：Pinia「kimi.config」（updateConfig）
 *
 * 安全阀：任一前提不满足（Vue/Pinia 不可达、store 结构不符、api 缺失、DOM 变更）
 * 时放弃接管，原生 overlay 菜单照常工作。
 */
(function () {
  'use strict';
  if (window.__kimiModelTabs) return;
  var TAG = '[kimi-model-tabs]';

  function log() {
    try { console.debug.apply(console, [TAG].concat([].slice.call(arguments))); } catch (e) { /* noop */ }
  }

  /* ------------------------------------------------------------------ */
  /* 运行时探测                                                          */
  /* ------------------------------------------------------------------ */

  function findPinia() {
    try {
      var el = document.querySelector('#app');
      var app = el && el.__vue_app__;
      var pinia = app && app.config && app.config.globalProperties && app.config.globalProperties.$pinia;
      if (pinia && pinia._s instanceof Map) return pinia;
    } catch (e) { /* noop */ }
    return null;
  }

  function probeRuntime() {
    var pinia = findPinia();
    if (!pinia) return null;
    try {
      var models = pinia._s.get('kimi.models');
      var sessions = pinia._s.get('kimi.sessions');
      var config = pinia._s.get('kimi.config');
      var mode = pinia._s.get('kimi.mode');
      if (!models || !Array.isArray(models.models) || typeof models.toggleStarModel !== 'function') return null;
      if (!sessions || !Array.isArray(sessions.sessions) || typeof sessions.updateSession !== 'function' || !('activeSessionId' in sessions)) return null;
      if (!config || typeof config.updateConfig !== 'function') return null;
      if (!mode || !('thinking' in mode) || !('thinkingBySession' in mode)) return null;
      var deps = window.__kimiAppClientDeps && window.__kimiAppClientDeps.current;
      var api = deps && typeof deps.api === 'function' ? deps.api() : null;
      if (!api || typeof api.updateSession !== 'function') return null;
      return { pinia: pinia, modelsStore: models, sessionsStore: sessions, configStore: config, modeStore: mode, api: api };
    } catch (e) {
      log('probe failed', e);
      return null;
    }
  }

  /* ------------------------------------------------------------------ */
  /* 派生逻辑（照抄官方 Nv/TW/Dv/Vp/nC/Il 的语义）                        */
  /* ------------------------------------------------------------------ */

  var KIMI_MANAGED = 'managed:kimi-code';
  var CACHE_NOTE = '提示：切换模型或思考程度会使已有的提示词缓存失效。建议新建会话，避免额外的 token 消耗。';

  /* 官方图标（icons-tdlSp8of.js 原样 path）：
     star=Mc/ri-star-fill, star-outline=kc/ri-star-line, check=tn/kimi-check */
  var SVG_STAR = '<svg viewBox="0 0 24 24" width="1.2em" height="1.2em"><path fill="currentColor" d="m12 18.26l-7.053 3.948l1.575-7.928L.588 8.792l8.027-.952L12 .5l3.385 7.34l8.027.952l-5.934 5.488l1.575 7.928z"/></svg>';
  var SVG_STAR_OUTLINE = '<svg viewBox="0 0 24 24" width="1.2em" height="1.2em"><path fill="currentColor" d="m12 18.26l-7.053 3.948l1.575-7.928L.588 8.792l8.027-.952L12 .5l3.385 7.34l8.027.952l-5.934 5.488l1.575 7.928zm0-2.292l4.247 2.377l-.948-4.773l3.573-3.305l-4.833-.573l-2.038-4.419l-2.039 4.42l-4.833.572l3.573 3.305l-.948 4.773z"/></svg>';
  var SVG_CHECK = '<svg viewBox="0 0 24 24" width="1.2em" height="1.2em" fill="none"><path fill-rule="evenodd" clip-rule="evenodd" fill="currentColor" d="M11.3912 16.7134C11.743 17.0627 12.3111 17.0617 12.6617 16.7111L19.6364 9.73641C19.9878 9.38494 19.9878 8.81509 19.6364 8.46362C19.2849 8.11215 18.7151 8.11215 18.3636 8.46362L12.023 14.8042L5.63407 8.46132C5.28133 8.11112 4.71149 8.11318 4.36129 8.46592C4.0111 8.81872 4.01316 9.38856 4.36578 9.73876L11.3912 16.7134Z"/></svg>';

  function providerLabel(p) { return p === KIMI_MANAGED ? 'Kimi 订阅' : p; }

  function effortsOf(m) { return (m && m.supportEfforts) || []; }

  function thinkingMode(m) {
    if (m === undefined) return 'toggle';
    var caps = m.capabilities || [];
    if (caps.indexOf('always_thinking') >= 0) return 'always-on';
    if (caps.indexOf('thinking') >= 0 || m.adaptiveThinking === true) return 'toggle';
    return 'unsupported';
  }

  function midOf(arr) { return arr[Math.floor(arr.length / 2)]; }

  function defaultEffort(m) {
    if (thinkingMode(m) === 'unsupported') return 'off';
    var eff = effortsOf(m);
    if (eff.length > 0) return (m && m.defaultEffort) || midOf(eff);
    return 'on';
  }

  // 官方 Nv：模型可选的思考档位
  function effortChoices(m) {
    var eff = effortsOf(m), mode = thinkingMode(m);
    if (eff.length > 0) return mode === 'always-on' ? eff.slice() : ['off'].concat(eff);
    if (mode === 'always-on') return ['on'];
    if (mode === 'unsupported') return ['off'];
    return ['on', 'off'];
  }

  // 官方 nC：把 UI 档位归一成存储值
  function normalizeEffort(m, v) {
    if (v === 'off') return 'off';
    if (v === 'on') return defaultEffort(m);
    return v;
  }

  function effortLabel(v) { return v.length === 0 ? v : v.charAt(0).toUpperCase() + v.slice(1); }

  function currentModelId(rt) {
    var sid = rt.sessionsStore.activeSessionId;
    var sess = sid && rt.sessionsStore.sessions.find(function (x) { return x.id === sid; });
    var raw = sess && sess.model !== '' ? sess.model : rt.modelsStore.draftModel;
    return raw || null;
  }

  function modelById(rt, id) {
    return (rt.modelsStore.models || []).find(function (m) { return m.id === id || m.model === id; });
  }

  function currentEffort(rt, m) {
    var sid = rt.sessionsStore.activeSessionId;
    var cur = sid ? rt.modeStore.thinkingBySession[sid] : rt.modeStore.thinking;
    // 官方 Yl：当前值不在档位集合里则不显示（undefined 等），这里同样返回 null
    return effortChoices(m).indexOf(cur) >= 0 ? cur : null;
  }

  function setEffort(rt, m, v) {
    var val = normalizeEffort(m, v);
    var mode = rt.modeStore;
    var sid = rt.sessionsStore.activeSessionId;
    try {
      if (sid !== null && sid !== undefined) {
        mode.thinkingBySession = Object.assign({}, mode.thinkingBySession, (function () { var o = {}; o[sid] = val; return o; }()));
      } else {
        mode.thinking = val;
        mode.draftThinkingExplicit = true;
      }
    } catch (e) { log('setEffort failed', e); }
  }

  /* ------------------------------------------------------------------ */
  /* 切换模型（复刻官方 ci.setModel：乐观更新 + 失败回滚 + 默认模型联动）  */
  /* ------------------------------------------------------------------ */

  function selectModel(rt, modelId) {
    var st = rt.sessionsStore;
    var sid = st.activeSessionId;
    if (!sid) {
      try { rt.modelsStore.setDraftModel(modelId); } catch (e) { log('setDraftModel failed', e); }
      return;
    }
    var before = st.sessions.find(function (x) { return x.id === sid; });
    var prevModel = before ? before.model : undefined;
    try { st.updateSession(sid, function (x) { return Object.assign({}, x, { model: modelId }); }); }
    catch (e) { log('optimistic update failed', e); return; }
    Promise.resolve()
      .then(function () { return rt.api.updateSession(sid, { model: modelId }); })
      .then(function () {
        var def = rt.configStore.config && rt.configStore.config.defaultModel;
        if (def !== modelId) {
          try { rt.configStore.updateConfig({ defaultModel: modelId }); } catch (e) { log('updateConfig failed', e); }
        }
      })
      .catch(function (err) {
        log('updateSession failed, rollback', err);
        try {
          st.updateSession(sid, function (x) {
            return Object.assign({}, x, { model: prevModel !== undefined ? prevModel : x.model });
          });
        } catch (e) { /* noop */ }
      });
  }

  /* ------------------------------------------------------------------ */
  /* 面板 UI                                                             */
  /* ------------------------------------------------------------------ */

  /* 以下样式逐条移植自官方产物：
     - 菜单容器/行：icons-DUtaWENm.css 的 .ui-menu / .ui-menu-item（data-v-ad586d89/c52eb4dd）
     - 思考 chips：.ui-seg--xs（data-v-89d04b87，SegmentedControl xs 档）
     - 分隔线：.ui-menu-sep；提示语：.overlay-menu-copy（browserOverlay css）
     令牌与官方同源（--menu-pad、--menu-item-padding 系列等，
     桌面端页面里已有定义；backup 值取自 wght--9wnPkiH.css 的实际解析值） */
  var CSS = [
    '.kmt-panel{position:fixed;z-index:2147483000;display:none;flex-direction:column;width:360px;max-height:520px;',
    'padding:var(--menu-pad,3.5px);background:var(--color-menu-bg,rgba(41,41,41,.95));',
    '-webkit-backdrop-filter:var(--p-menu-backdrop,blur(20px));backdrop-filter:var(--p-menu-backdrop,blur(20px));',
    'border:var(--p-hairline,1px) solid var(--color-line,rgba(255,255,255,.1));border-radius:var(--radius-lg,12px);',
    'box-shadow:var(--shadow-menu,0 6px 18px rgba(0,0,0,.2));overflow:hidden;',
    'font-family:var(--font-ui,system-ui);color:var(--color-text,#eee);user-select:none}',
    '.kmt-panel.kmt-open{display:flex}',
    /* 供应商标签条：官方 ModelPicker chip-strip/chip 样式（data-v-565a5940，高度 28→24 同 seg-sm） */
    '.kmt-tabs{display:flex;gap:var(--space-1,4px);flex-wrap:wrap;padding:2px 4px 4px}',
    '.kmt-tab{flex:none;height:24px;padding:0 var(--space-2,8px);border:none;border-radius:var(--radius-full,999px);',
    'background:transparent;color:var(--color-text-muted,#999);font-family:var(--font-ui,system-ui);font-size:var(--text-sm,13px);',
    'cursor:pointer;transition:background var(--duration-base,.16s),color var(--duration-base,.16s)}',
    '.kmt-tab:hover{background:var(--color-hover,rgba(255,255,255,.07));color:var(--color-text,#fff)}',
    '.kmt-tab.kmt-active{background:var(--color-selected,rgba(255,255,255,.13));color:var(--color-text,#fff);font-weight:var(--weight-medium,500)}',
    '.kmt-tab:focus-visible{outline:none;box-shadow:var(--p-focus-ring,0 0 0 2px rgba(23,131,255,.5))}',
    /* 模型列表：ui-menu-item 原样（padding 5px 9px + is-active=hover 底色） */
    '.kmt-list{flex:1;overflow-y:auto;min-height:0}',
    '.kmt-row{display:flex;align-items:center;gap:var(--menu-row-gap-icon,7px);width:100%;',
    'padding:var(--menu-item-padding-block,5px) var(--menu-item-padding-inline,9px);border:none;border-radius:var(--radius-menu-item,calc(var(--radius-lg,12px) - var(--menu-pad,3.5px)));',
    'background:transparent;color:var(--color-text,#eee);font-family:var(--font-ui,system-ui);font-size:var(--text-sm,13px);',
    'font-weight:var(--weight-option-label,475);line-height:var(--leading-tight,1.25);text-align:left;cursor:pointer;',
    'transition:background var(--duration-base,.16s),color var(--duration-base,.16s)}',
    '.kmt-row:hover{background:var(--color-hover,rgba(255,255,255,.07));color:var(--color-text-strong,#fff)}',
    '.kmt-row.is-active{background:var(--color-hover,rgba(255,255,255,.07));color:var(--color-text,#fff)}',
    '.kmt-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.kmt-check{flex:none;color:var(--color-accent,#1783ff)}',
    '.kmt-star{display:grid;place-items:center;width:16px;height:16px;border:none;padding:0;background:transparent;',
    'cursor:pointer;color:var(--color-text-faint,#888);flex:none;transition:color var(--duration-base,.16s)}',
    '.kmt-star svg{display:block;width:14px;height:14px}',
    '.kmt-star{visibility:hidden;opacity:0}',
    '.kmt-row:hover .kmt-star,.kmt-star.kmt-on{visibility:visible;opacity:1}',
    '.kmt-star.kmt-on{color:var(--color-warning,#a9610a)}',
    '.kmt-star:hover{color:var(--color-text,#fff)}',
    /* 思考行：overlay-menu-choices + ui-seg--xs 原样 */
    '.kmt-think{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-2,8px) 1rem;padding:var(--space-1,4px) var(--menu-item-padding-inline,9px)}',
    '.kmt-think-label{display:inline-flex;align-items:center;gap:var(--space-2,8px);font-size:var(--text-sm,13px);',
    'font-weight:var(--weight-option-label,475);line-height:var(--leading-tight,1.25)}',
    '.kmt-chips{position:relative;display:inline-flex;gap:2px;padding:2px;background:var(--color-surface-sunken,#121212);',
    'border:var(--p-hairline,1px) solid var(--color-line,rgba(255,255,255,.1));border-radius:var(--radius-md,8px)}',
    '.kmt-chip{position:relative;z-index:1;display:inline-flex;align-items:center;border:none;border-radius:var(--radius-sm,6px);',
    'background:transparent;color:var(--color-text-muted,#999);font-family:var(--font-ui,system-ui);font-weight:var(--weight-medium,500);',
    'cursor:pointer;line-height:var(--leading-tight,1.25);height:20px;padding:0 var(--space-2,8px);font-size:var(--text-xs,12px)}',
    '.kmt-chip:hover{color:var(--color-text,#fff)}',
    '.kmt-chip.kmt-active{color:var(--color-text,#fff);background:var(--color-surface-raised,#2c2c2c);box-shadow:var(--shadow-sm,0 1px 2px rgba(0,0,0,.22))}',
    '.kmt-chip:focus-visible{outline:none;box-shadow:var(--p-focus-ring,0 0 0 2px rgba(23,131,255,.5))}',
    /* 提示语：overlay-menu-copy 原样 */
    '.kmt-foot{padding:var(--space-2,8px) var(--menu-item-padding-inline,9px);color:var(--color-text-muted,#999);',
    'font-size:var(--text-sm,13px);line-height:var(--leading-normal,1.5);white-space:normal}',
    /* 分隔线：ui-menu-sep 原样 */
    '.kmt-sep{height:1px;margin:4px 0;background:var(--color-line,rgba(255,255,255,.1));flex:none}'
  ].join('');

  function ensurePanel() {
    var panel = document.getElementById('kmt-panel');
    if (panel) return panel;
    var style = document.createElement('style');
    style.id = 'kmt-style';
    style.textContent = CSS;
    document.head.appendChild(style);
    panel = document.createElement('div');
    panel.id = 'kmt-panel';
    panel.className = 'kmt-panel';
    panel.innerHTML =
      '<div class="kmt-tabs" role="tablist"></div>' +
      '<div class="kmt-list" role="listbox"></div>' +
      '<div class="kmt-body"></div>';
    document.body.appendChild(panel);
    panel.__state = { tab: null };
    panel.addEventListener('mousedown', function (e) { e.stopPropagation(); });
    return panel;
  }

  function closePanel() {
    var panel = document.getElementById('kmt-panel');
    if (panel) panel.classList.remove('kmt-open');
  }

  function panelOpen() {
    var panel = document.getElementById('kmt-panel');
    return !!panel && panel.classList.contains('kmt-open');
  }

  function renderPanel(rt, anchor) {
    var panel = ensurePanel();
    var st = panel.__state;
    var models = rt.modelsStore.models || [];
    var curId = currentModelId(rt) || '';
    var curModel = modelById(rt, curId);

    // 供应商标签条：初始 = 当前模型的供应商；没有「全部」
    var providers = [];
    var seen = {};
    models.forEach(function (m) { if (m.provider && !seen[m.provider]) { seen[m.provider] = 1; providers.push(m.provider); } });
    if (!st.tab || seen[st.tab] === undefined) {
      st.tab = (curModel && curModel.provider) || providers[0] || null;
    }
    var tabs = panel.querySelector('.kmt-tabs');
    tabs.textContent = '';
    providers.forEach(function (p) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'kmt-tab' + (st.tab === p ? ' kmt-active' : '');
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', st.tab === p ? 'true' : 'false');
      b.textContent = providerLabel(p);
      b.addEventListener('click', function () { st.tab = p; renderPanel(rt, anchor); });
      tabs.appendChild(b);
    });

    // 模型列表（图1 样式：仅名称 + 当前勾选，悬停星标）
    var list = panel.querySelector('.kmt-list');
    list.textContent = '';
    var starred = rt.modelsStore.starredModelIds instanceof Set
      ? rt.modelsStore.starredModelIds : new Set(rt.modelsStore.starredModelIds || []);
    var shown = models.filter(function (m) { return m.provider === st.tab; });
    shown.sort(function (a, b) { return (starred.has(b.id) ? 1 : 0) - (starred.has(a.id) ? 1 : 0); });
    if (shown.length === 0) {
      var empty = document.createElement('div');
      empty.className = 'kmt-note';
      empty.textContent = '该供应商暂无可用模型';
      list.appendChild(empty);
    }
    shown.forEach(function (m) {
      var row = document.createElement('div');
      // 官方 ui-menu-item 选中态 = is-active（hover 同底色）
      row.className = 'kmt-row' + (m.id === curId ? ' is-active' : '');
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', m.id === curId ? 'true' : 'false');
      var star = document.createElement('button');
      star.type = 'button';
      star.className = 'kmt-star' + (starred.has(m.id) ? ' kmt-on' : '');
      star.title = starred.has(m.id) ? '取消收藏' : '添加到收藏';
      star.innerHTML = SVG_STAR_OUTLINE;
      if (starred.has(m.id)) star.innerHTML = SVG_STAR;
      star.addEventListener('click', function (e) {
        e.stopPropagation();
        try { rt.modelsStore.toggleStarModel(m.id); } catch (err) { log('toggleStar failed', err); }
        renderPanel(rt, anchor);
      });
      row.appendChild(star);
      var name = document.createElement('span');
      name.className = 'kmt-name';
      name.textContent = m.displayName || m.model || m.id;
      row.appendChild(name);
      if (m.id === curId) {
        var chk = document.createElement('span');
        chk.className = 'kmt-check';
        chk.innerHTML = SVG_CHECK;
        row.appendChild(chk);
      }
      row.addEventListener('click', function () { closePanel(); selectModel(rt, m.id); });
      list.appendChild(row);
    });

    // body：思考档位行（官方 overlay-menu-choices 结构）+ 分隔线 + 缓存提示
    var body = panel.querySelector('.kmt-body');
    body.textContent = '';
    var sep1 = document.createElement('div');
    sep1.className = 'kmt-sep';
    body.appendChild(sep1);
    var refModel = curModel || (shown[0] || null);
    var choices = refModel ? effortChoices(refModel) : [];
    if (refModel && choices.length > 1 && thinkingMode(refModel) !== 'unsupported') {
      // 官方结构：.overlay-menu-choices > span.choice-label + .ui-seg.ui-seg--xs > button.ui-seg__item
      var think = document.createElement('div');
      think.className = 'kmt-think';
      var lb = document.createElement('span');
      lb.className = 'kmt-think-label';
      lb.textContent = '思考';
      think.appendChild(lb);
      var seg = document.createElement('span');
      seg.className = 'kmt-chips';
      var curEff = currentEffort(rt, refModel);
      choices.forEach(function (v) {
        var c = document.createElement('button');
        c.type = 'button';
        c.className = 'kmt-chip' + (curEff === v ? ' kmt-active' : '');
        c.textContent = effortLabel(v);
        c.addEventListener('click', function () {
          setEffort(rt, refModel, v);
          renderPanel(rt, anchor);
        });
        seg.appendChild(c);
      });
      think.appendChild(seg);
      body.appendChild(think);
    } else if (refModel) {
      var note1 = document.createElement('div');
      note1.className = 'kmt-foot';
      note1.textContent = '思考 · ' + (thinkingMode(refModel) === 'unsupported' ? '不支持' : effortLabel(choices[0] || 'on'));
      body.appendChild(note1);
    }

    var sep2 = document.createElement('div');
    sep2.className = 'kmt-sep';
    body.appendChild(sep2);

    // 缓存提示（官方 cacheNote 原文）
    var foot = document.createElement('div');
    foot.className = 'kmt-foot';
    foot.textContent = CACHE_NOTE;
    body.appendChild(foot);

    // 定位：底边贴住锚点顶边（bottom 锚定，高度变化不动底边）
    panel.classList.add('kmt-open');
    var r = anchor.getBoundingClientRect();
    panel.style.top = 'auto';
    panel.style.bottom = Math.max(8, window.innerHeight - r.top + 6) + 'px';
    var left = r.right - panel.offsetWidth;
    panel.style.left = Math.max(8, left) + 'px';
  }

  function openPanel(rt, anchor) {
    var panel = ensurePanel();
    panel.__state.tab = null;   // 每次打开重置到当前模型的供应商
    renderPanel(rt, anchor);
  }

  // 外点 / 失焦 / 缩放关闭
  document.addEventListener('mousedown', function (e) {
    if (!panelOpen()) return;
    if (e.target.closest && e.target.closest('.kmt-panel')) return;
    closePanel();
  }, true);
  window.addEventListener('blur', closePanel);
  window.addEventListener('resize', closePanel);

  /* ------------------------------------------------------------------ */
  /* 入口劫持                                                            */
  /* ------------------------------------------------------------------ */

  function hijack(rt) {
    document.addEventListener('click', function (e) {
      var pill = e.target.closest && e.target.closest('.model-pill');
      if (!pill) return;
      if (!(rt.modelsStore.models || []).length) return;
      if (pill.classList.contains('login-pill')) return;
      e.preventDefault();
      e.stopPropagation();
      if (panelOpen()) { closePanel(); return; }
      openPanel(rt, pill);
    }, true);
    log('hijack installed');
  }

  /* ------------------------------------------------------------------ */
  /* 启动                                                                */
  /* ------------------------------------------------------------------ */

  var tries = 0;
  function boot() {
    var rt = probeRuntime();
    if (rt) {
      try {
        hijack(rt);
        window.__kimiModelTabs = {
          runtime: rt,
          open: function () { var p = document.querySelector('.model-pill'); if (p) openPanel(rt, p); },
          selectModel: function (id) { selectModel(rt, id); }
        };
      } catch (e) { log('hijack failed', e); }
      return;
    }
    if (++tries > 60) { log('give up after 60 tries — native menu stays'); return; }
    setTimeout(boot, 500);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
