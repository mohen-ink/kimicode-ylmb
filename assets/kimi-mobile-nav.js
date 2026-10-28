/**
 * kimi-mobile-nav.js — 手机端 SPA 交互补丁（由插件桥注入到手机 WebView 的 SPA 页面）
 *
 * 生效范围：仅手机页面。桥在注入本脚本前会先写 window.__kimiMobileBridge = true；
 * 桌面 SPA 也会加载本文件（service.py 的 REMOTE_ASSETS），但不会激活补丁——
 * 返回键钩子（§1）桌面不需要，滚动修护（§2）与插队按钮（§3）只在 PHONE 下安装，
 * 避免在桌面误加悬浮按钮或拦截触摸。
 *
 * 能力：
 *   §1 window.KimiNav.back() —— 供 Android App 返回键逐层返回（多级语义）
 *   §2 聊天滚动修护 —— 阻止 SPA 的 onTouchmovePassive 打断手势惯性（详见 §2 注释）
 *   §3 移动端「插队/立即发送」触摸按钮 —— 复用 SPA 队列首条的 q-send 动作
 *   §4 布局修正 —— 附件名截断不溢出气泡；会话切换抽屉拉高到顶栏下；
 *      SPA 浮层打开时隐藏插件「面板」悬浮按钮
 *   §5 「已完成」入口 —— 会话切换抽屉里补 PC 侧栏的已完成（已归档）会话列表
 */
(function () {
  'use strict';
  if (window.__kimiMobileNav) return;
  window.__kimiMobileNav = true;

  // 桥注入前先置位；桌面加载时为 undefined → 仅定义 §1，不安装 §2/§3
  var PHONE = window.__kimiMobileBridge === true;

  /* ------------------------------------------------------------------ */
  /* 通用小工具                                                          */
  /* ------------------------------------------------------------------ */

  function shown(el) {
    if (!el || !el.isConnected) return false;
    if (el.hasAttribute('hidden')) return false;
    var cs = window.getComputedStyle(el);
    if (!cs || cs.display === 'none' || cs.visibility === 'hidden') return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function firstVisible(sel) {
    var list = document.querySelectorAll(sel);
    for (var i = 0; i < list.length; i++) {
      if (shown(list[i])) return list[i];
    }
    return null;
  }

  function lastVisible(sel) {
    var list = document.querySelectorAll(sel);
    for (var i = list.length - 1; i >= 0; i--) {
      if (shown(list[i])) return list[i];
    }
    return null;
  }

  // 找到可见目标并点击；成功消化返回 true
  function clickSel(sel, last) {
    var el = last ? lastVisible(sel) : firstVisible(sel);
    if (!el) return false;
    try { el.click(); } catch (e) { return false; }
    return true;
  }

  function dispatchEscape(target) {
    try {
      var ev = new KeyboardEvent('keydown', {
        key: 'Escape', code: 'Escape', bubbles: true, cancelable: true
      });
      (target || document).dispatchEvent(ev);
      return true;
    } catch (e) {
      return false;
    }
  }

  // 覆盖层自身 mousedown（Dialog/Lightbox 的遮罩关闭走 target===currentTarget）
  function mousedownSelf(el) {
    try {
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
      return true;
    } catch (e) {
      return false;
    }
  }

  // 读 Pinia「kimi.sessions」（唯一经 window 可达的官方运行时入口，见 kimi-model-tabs.js
  // 的同一路径）：用于判断"当前是否正打开某个会话"，决定 §1 最后一级能否走会话内返回。
  // 这里用鸭子类型而非 `instanceof Map`，避免容器/领域（realm）差异带来的误判。
  function piniaStore(id) {
    try {
      var app = document.querySelector('#app').__vue_app__;
      var pinia = app && app.config && app.config.globalProperties &&
        app.config.globalProperties.$pinia;
      var stores = pinia && pinia._s;
      return stores && typeof stores.get === 'function' ? (stores.get(id) || null) : null;
    } catch (e) {
      return null;
    }
  }

  function activeSessionId() {
    var sessions = piniaStore('kimi.sessions');
    return sessions && typeof sessions.activeSessionId === 'string'
      ? sessions.activeSessionId : '';
  }

  // 桌面端「关闭会话视图」= 全局快捷键 mod+w（非 mac 即 Ctrl+W）。
  // 手机 WebView 的 navigator.platform 不是 Mac，故 mod 落到 ctrlKey。
  // 这一步只是"最后一级"的尽力而为：仅当确实打开了会话时才派发。
  function sendCloseSessionView() {
    try {
      var ev = new KeyboardEvent('keydown', {
        key: 'w', code: 'KeyW',
        ctrlKey: true, metaKey: false, altKey: false, shiftKey: false,
        bubbles: true, cancelable: true
      });
      window.dispatchEvent(ev);
      return true;
    } catch (e) {
      return false;
    }
  }

  /* ------------------------------------------------------------------ */
  /* §1 返回键多级语义                                                    */
  /* ------------------------------------------------------------------ */
  // 层级从"最上层"到"最下层"，命中即消化并返回 true；全部落空才 return false，
  // 交回 App 走 WebView history / 回首页。
  // 现成的 KimiNav.back（如桌面插件自带的 kimi-phone-mcp.js）先行链式调用，
  // 保证不覆盖既有语义。
  var previousBack = (window.KimiNav && typeof window.KimiNav.back === 'function')
    ? window.KimiNav.back : null;

  function closeUsageOverlay() {
    var ov = document.getElementById('kuOv');
    if (!ov || ov.hidden) return false;
    // 用量面板：点遮罩等于其内部 close()（会恢复「面板」按钮可见）
    if (clickSel('#kuOv .ku-mask')) return true;
    ov.hidden = true;
    return true;
  }

  function closePhoneMcpOverlay() {
    var mcp = document.getElementById('ku-phone-mcp-overlay');
    if (mcp && mcp.classList.contains('open')) {
      mcp.classList.remove('open');
      return true;
    }
    return false;
  }

  function closeTopDialog() {
    if (!firstVisible('.ui-dialog__overlay')) return false;
    // 优先关闭按钮（始终有效，不依赖栈顶/Escape 条件）
    if (clickSel('.ui-dialog__overlay .ui-dialog__close', true)) return true;
    // 无关闭按钮（hideClose）时退化为点遮罩自身
    var overlay = lastVisible('.ui-dialog__overlay');
    if (overlay && mousedownSelf(overlay)) return true;
    return dispatchEscape(window);
  }

  function closeMediaLightbox() {
    if (!firstVisible('.media-lightbox')) return false;
    if (clickSel('.media-lightbox-close', true)) return true;
    var box = lastVisible('.media-lightbox');
    if (box && mousedownSelf(box)) return true;
    return dispatchEscape(window);
  }

  function closeSheet() {
    if (!firstVisible('.sheet-root')) return false;
    // BottomSheet：scrim / 抓手按钮都绑定了关闭
    if (clickSel('.sheet-root .sheet-scrim')) return true;
    return clickSel('.sheet-root .sheet-grab');
  }

  function closeRightPanel() {
    // 右侧面板（含子代理详情 / diff / providers 等 tab）在手机上被 .ptb-hide 隐藏
    // （.ptb-hide 在 :not(.mobile) 下才 display:none，移动端可见）
    return clickSel('.ptb-hide');
  }

  function closeSessionAdmin() {
    return clickSel('.session-admin .sa-back');
  }

  window.KimiNav = window.KimiNav || {};
  // 供同页其他脚本（如插件手机面板）取当前会话：SPA 的 activeSessionId 只在
  // Pinia 内部，面板独立渲染时拿不到，这里统一暴露一个只读入口。
  window.KimiNav.activeSession = activeSessionId;
  window.KimiNav.back = function () {
    // 0. 先让已有钩子处理（如桌面插件自带的手机 MCP 弹层）
    if (previousBack) {
      try { if (previousBack() === true) return true; } catch (e) { /* 忽略既有钩子异常 */ }
    }
    // 1. 本插件自有弹层
    if (closeUsageOverlay()) return true;
    if (closeDoneOverlay()) return true;
    if (closePhoneMcpOverlay()) return true;
    // 2. SPA 浮层（由下到上按"最可能在最上层"的顺序尝试）
    if (closeMediaLightbox()) return true;
    if (closeTopDialog()) return true;
    if (closeSheet()) return true;
    if (closeRightPanel()) return true;
    if (closeSessionAdmin()) return true;
    // 3. SPA 会话内返回：仅当确实打开了会话，才派发 closeSessionView
    if (activeSessionId()) return sendCloseSessionView();
    // 4. 落空，交回 App
    return false;
  };
  window.KimiNav.__mobile = true;   // 标记：手机端补丁已就位

  /* ------------------------------------------------------------------ */
  /* §2 聊天滚动修护                                                     */
  /* ------------------------------------------------------------------ */
  // 现象：手机上聊天区上/下滑顿挫、上不去、伴随抖动。
  // 成因：SPA 的 ConversationPane 在滚动容器 .panes.chat-scroll 上挂了
  //   onTouchmovePassive: ha —— 单指竖向滑动时会把 isFollowing(吸底) 置 false
  //   并调用 $a() → Ir()，而 Ir() 会执行一次 `scrollTo({top: 当前 scrollTop})`。
  //   Chrome 借这次程序化滚动**取消正在进行的手势惯性**，于是滑动被反复"掐断"，
  //   既抖又难脱离吸底区（粘滞阈值 h0=80px）。
  // 该处理来自 SPA 编译产物；只对聊天容器的原地写回和手指离开底部时的
  //   抢吸底写入做限定过滤，保留浏览器原生触摸滚动与惯性。

  // §4 布局修正样式（随 §2 的 style 一起注入）：
  //   - 已发送气泡与输入区上传预览共用附件 pill；手机端限制两处文件名宽度，
  //     避免长文件名撑开聊天区或输入区形成横向滚动。
  //   - 会话切换抽屉（BottomSheet 内含 .view-tabs/.mlist）高度随内容，只占半屏；
  //     给明确高度，让顶边贴到顶栏（50px + safe-top）下方。
  //   - SPA 浮层打开时隐藏插件「面板」悬浮按钮（它 z-index 10001 会压住浮层）。
  var LAYOUT_CSS = [
    'html,body{overflow:hidden;overscroll-behavior-y:contain;width:100%;max-width:100%;overflow-x:hidden}',
    '.app-shell{overscroll-behavior-y:contain;width:100%;max-width:100%;overflow-x:hidden}',
    '.panes,.panes.chat-scroll,.chat,.ph{overflow-x:hidden;max-width:100%;min-width:0}',
    '.cin-wrap,.input-row,.ph,.ph .ProseMirror,.u-turn,.u-bub,.u-atts,.composer-media-rail{min-width:0;max-width:100%}',
    // 附件 pill 与文件名：不限具体容器层级，一律限宽 + 省略号。
    // 上传预览与已发送气泡共用同一套类名，长文件名不会撑破气泡/输入区。
    '.attachment-pill,.mention-pill,.quote-pill{max-width:100%;min-width:0;overflow:hidden;vertical-align:bottom}',
    '.attachment-pill-icon,.mention-pill-icon,.quote-pill-icon{flex:none}',
    '.attachment-pill-name,.mention-pill-name{max-width:100%;min-width:0;flex:0 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '.u-bub .u-atts{display:flex;flex-wrap:wrap;gap:6px;overflow:hidden;max-width:100%}',
    '.u-bub .u-atts>.attachment-pill,.u-bub .u-atts>.mention-pill{flex:0 1 auto;max-width:100%}',
    '.u-bub .u-atts .attachment-pill-name,.u-bub .u-atts .mention-pill-name{max-width:100%}',
    '.ph .ProseMirror{overflow-wrap:anywhere;max-width:100%;min-width:0}',
    '.ph .ProseMirror .attachment-pill,.ph .ProseMirror .mention-pill{max-width:100%}',
    '.ph .ProseMirror .attachment-pill-name,.ph .ProseMirror .mention-pill-name{max-width:100%}',
    '.sheet-root .sheet-panel.ku-switcher-sheet{height:calc(var(--app-height,100dvh) - 50px - var(--safe-top,0px));max-height:calc(var(--app-height,100dvh) - 50px - var(--safe-top,0px))}',
    '#app{max-width:100%;overflow-x:clip}',
    'body.ku-spa-overlay #kuUsageBtn{visibility:hidden!important;pointer-events:none!important}',
    'body.ku-spa-overlay #ku-steer-btn{display:none!important}'
  ].join('');

  function installScrollFix() {
    try {
      var style = document.createElement('style');
      style.id = 'ku-mobile-nav-style';
      style.textContent = '.panes.chat-scroll{overscroll-behavior-y:contain}' + LAYOUT_CSS +
        '.u-bub table{display:block;max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}' +
        '.u-bub pre{max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}';
      (document.head || document.documentElement).appendChild(style);
    } catch (e) { /* 样式失败不影响手势修正 */ }

    var UNPIN_GAP = 32, PIN_GAP = 48;
    function gapToBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight; }
    function isChatPane(el) { return !!(el.matches && el.matches('.panes.chat-scroll')); }
    document.addEventListener('scroll', function(e) {
      var t = e.target;
      if (!isChatPane(t)) return;
      var gap = gapToBottom(t);
      if (gap < PIN_GAP) t._kuUnpinned = false;
      else if (gap > UNPIN_GAP) t._kuUnpinned = true;
    }, { passive: true, capture: true });
    function skipWrite(el, top) {
      if (!isChatPane(el) || typeof top !== 'number') return false;
      if (el._kuUnpinned && gapToBottom(el) <= PIN_GAP) el._kuUnpinned = false;
      if (Math.abs(top - el.scrollTop) <= 1) return true;
      var down = top > el.scrollTop;
      if (el._kuUnpinned && down) return true;
      var gapAfter = el.scrollHeight - top - el.clientHeight;
      if (gapAfter >= UNPIN_GAP) el._kuUnpinned = true;
      else if (gapAfter <= PIN_GAP && !down) el._kuUnpinned = false;
      return false;
    }
    try {
      var origScrollTo = Element.prototype.scrollTo;
      if (typeof origScrollTo === 'function') {
        Element.prototype.scrollTo = function(arg, y) {
          var top = (typeof arg === 'object' && arg !== null) ? arg.top : y;
          if (skipWrite(this, top)) return;
          return (typeof arg === 'object' && arg !== null)
            ? origScrollTo.call(this, arg) : origScrollTo.call(this, arg, y);
        };
      }
      var desc = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
      if (desc && desc.configurable && desc.get && desc.set) {
        Object.defineProperty(Element.prototype, 'scrollTop', {
          configurable: true, enumerable: desc.enumerable,
          get: function() { return desc.get.call(this); },
          set: function(top) { if (!skipWrite(this, top)) desc.set.call(this, top); }
        });
      }
    } catch (e) { /* unavailable on this WebView */ }
    lockHorizontal();
  }

  var HSCROLL_OK = '.composer-media-rail,.ku-tabs,.ku-period,.ku-ticks,.ku-chart,.media-lightbox,' +
    '.media-thumb,.view-tabs,[style*="overflow-x"]';
  function allowHScroll(el) {
    for (var n = el; n && n.nodeType === 1 && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      if (n.matches && n.matches(HSCROLL_OK)) return true;
      if (n.scrollWidth - n.clientWidth <= 4) continue;
      var cs;
      try { cs = window.getComputedStyle(n); } catch (e) { cs = null; }
      if (cs && (cs.overflowX === 'auto' || cs.overflowX === 'scroll')) return true;
    }
    return false;
  }
  function lockHorizontal() {
    function reset() {
      if (window.scrollX !== 0) window.scrollTo(0, window.scrollY);
      if (document.documentElement.scrollLeft !== 0) document.documentElement.scrollLeft = 0;
      if (document.body && document.body.scrollLeft !== 0) document.body.scrollLeft = 0;
    }
    document.addEventListener('scroll', function (e) {
      var t = e.target;
      if (!t || t === document || t === document.documentElement || t === document.body) { reset(); return; }
      if (t.scrollLeft && !allowHScroll(t)) t.scrollLeft = 0;
    }, { passive: true, capture: true });
    window.addEventListener('resize', reset);
    if (window.visualViewport) window.visualViewport.addEventListener('resize', reset);
    reset();
  }

  /* ------------------------------------------------------------------ */
  /* §3 移动端「插队/立即发送」触摸按钮                                   */
  /* ------------------------------------------------------------------ */
  // SPA 队列面板首条自带 q-send 按钮（emit('steerQueued', 0)），但它在
  // .panes.chat-scroll 内容流里，手机上常被滚动位置挡住、够不到。
  // 这里补一个悬浮触摸按钮：当"存在排队 prompt"时出现，点击即触发同一个 q-send。
  // 说明：不直接 fetch /prompts:steer——排队项是本地的，尚未拿到服务端 prompt_id，
  // 直接调接口拿不到可用 id；复用 SPA 自身的 q-send 才是正确路径。
  var steerBtn = null;
  var steerTimer = null;

  function steerOnce() {
    var btn = document.querySelector('.q-stack .q-turn .q-send');
    if (btn) { btn.click(); return true; }
    return false;
  }

  function ensureSteerButton() {
    if (steerBtn && steerBtn.isConnected) return steerBtn;
    steerBtn = document.createElement('button');
    steerBtn.id = 'ku-steer-btn';
    steerBtn.type = 'button';
    steerBtn.textContent = '插队';
    steerBtn.title = '立即发送到当前回合';
    steerBtn.setAttribute('aria-label', '立即发送到当前回合');
    steerBtn.style.cssText = [
      'position:fixed',
      'right:max(12px,env(safe-area-inset-right))',
      'bottom:calc(env(safe-area-inset-bottom) + 104px)',
      'z-index:9000',
      'display:none',
      'min-height:44px',
      'padding:10px 14px',
      'border-radius:99px',
      'background:#405eac',
      'color:#fff',
      'border:1px solid #87a5f5',
      'font:14px system-ui',
      'box-shadow:0 6px 20px rgba(0,0,0,.35)'
    ].join(';');
    steerBtn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      steerOnce();
    });
    document.body.appendChild(steerBtn);
    return steerBtn;
  }

  function syncSteerButton() {
    var btn = ensureSteerButton();
    if (!btn) return;
    var show = !!document.querySelector('.q-stack .q-turn .q-send');
    btn.style.display = show ? 'block' : 'none';
  }

  function scheduleSync() {
    if (steerTimer !== null) return;
    steerTimer = window.setTimeout(function () {
      steerTimer = null;
      try { syncSteerButton(); } catch (e) { /* noop */ }
      try { syncLayout(); } catch (e) { /* noop */ }
    }, 200);
  }

  /* ------------------------------------------------------------------ */
  /* §4 布局修正（与 §3 共用 MutationObserver 的 200ms 合并调度）          */
  /* ------------------------------------------------------------------ */
  function switcherPanels() {
    var out = [];
    var list = document.querySelectorAll('.sheet-root .sheet-panel');
    for (var i = 0; i < list.length; i++) {
      if (list[i].querySelector('.view-tabs, .mlist')) out.push(list[i]);
    }
    return out;
  }

  function syncLayout() {
    var panels = switcherPanels();
    for (var i = 0; i < panels.length; i++) {
      if (!panels[i].classList.contains('ku-switcher-sheet')) {
        panels[i].classList.add('ku-switcher-sheet');
      }
      ensureDoneEntry(panels[i]);
    }
    var overlay = !!(firstVisible('.sheet-root') || firstVisible('.ui-dialog__overlay') ||
      firstVisible('.media-lightbox') || isDoneOpen());
    if (document.body.classList.contains('ku-spa-overlay') !== overlay) {
      document.body.classList.toggle('ku-spa-overlay', overlay);
    }
  }

  /* ------------------------------------------------------------------ */
  /* §5 「已完成」入口                                                     */
  /* ------------------------------------------------------------------ */
  // PC 侧栏「已完成」分组 = 已归档会话，数据在 Pinia「kimi.sessionList」：
  //   doneSessions / doneSessionsHasMore / doneSessionsLoadingMore，
  //   ensureDoneSessions() 首次拉取，loadMoreDoneSessions() 翻页。
  // 打开会话不直接调内部 selectSession（不在 store 上），而是走 SPA 自己的
  // 路由约定：pushState 到 /sessions/<id> 再派发 popstate，由 SPA 的
  // onSessionRoutePopState 把会话拉进列表并选中（与浏览器前进/后退同一路径）。
  var doneOv = null;
  var doneBusy = false;

  function isDoneOpen() {
    return !!(doneOv && doneOv.isConnected && !doneOv.hidden);
  }

  function closeDoneOverlay() {
    if (!isDoneOpen()) return false;
    doneOv.hidden = true;
    scheduleSync();
    return true;
  }

  function ensureDoneEntry(panel) {
    if (panel.querySelector('#ku-done-entry')) return;
    var ref = panel.querySelector('.newrow.secondary') || panel.querySelector('.newrow');
    if (!ref || !ref.parentNode) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'ku-done-entry';
    btn.className = 'newrow secondary';
    for (var i = 0; i < ref.attributes.length; i++) {
      var a = ref.attributes[i];
      if (a.name.indexOf('data-v-') === 0) btn.setAttribute(a.name, '');
    }
    btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ' +
      'aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.7 2.7L16 9.8"/></svg>';
    btn.appendChild(document.createTextNode(' 已完成'));
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      closeSheet();
      openDoneOverlay();
    });
    ref.parentNode.insertBefore(btn, ref.nextSibling);
  }

  var DONE_CSS = [
    '#ku-done-ov{position:fixed;inset:0;z-index:10002;display:flex;flex-direction:column;justify-content:flex-end}',
    '#ku-done-ov[hidden]{display:none}',
    '#ku-done-ov .kd-mask{position:absolute;inset:0;background:rgba(0,0,0,.45)}',
    '#ku-done-ov .kd-sheet{position:relative;display:flex;flex-direction:column;min-height:0;',
    'height:calc(var(--app-height,100dvh) - 50px - var(--safe-top,0px));',
    'background:var(--color-surface-raised,#fff);color:var(--color-text,#1a1a1a);',
    'border-radius:16px 16px 0 0;box-shadow:0 -8px 30px rgba(0,0,0,.25);font-family:var(--font-ui,system-ui)}',
    '#ku-done-ov .kd-head{display:flex;align-items:center;gap:8px;padding:12px 12px 10px 16px;',
    'border-bottom:1px solid var(--color-line,rgba(0,0,0,.1))}',
    '#ku-done-ov .kd-title{flex:1;font-size:16px;font-weight:600}',
    '#ku-done-ov .kd-btn{min-height:44px;min-width:44px;padding:0 14px;border-radius:10px;',
    'border:1px solid var(--color-line,rgba(0,0,0,.12));background:transparent;color:inherit;font:inherit;font-size:14px}',
    '#ku-done-ov .kd-body{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;',
    '-webkit-overflow-scrolling:touch;padding:6px 8px max(16px,env(safe-area-inset-bottom))}',
    '#ku-done-ov .kd-row{display:block;width:100%;text-align:left;min-height:56px;padding:10px 12px;',
    'border:0;border-radius:10px;background:transparent;color:inherit;font:inherit}',
    '#ku-done-ov .kd-row:active{background:var(--color-surface-sunken,rgba(0,0,0,.06))}',
    '#ku-done-ov .kd-row.active{background:var(--color-hover,rgba(64,94,172,.1))}',
    '#ku-done-ov .kd-name{display:block;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '#ku-done-ov .kd-meta{display:block;margin-top:3px;font-size:12px;color:var(--color-text-muted,#888);',
    'overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
    '#ku-done-ov .kd-tip{padding:28px 12px;text-align:center;color:var(--color-text-muted,#888);font-size:14px}',
    '#ku-done-ov .kd-more{display:block;width:100%;margin-top:6px}'
  ].join('');

  function ensureDoneOverlay() {
    if (doneOv && doneOv.isConnected) return doneOv;
    if (!document.getElementById('ku-done-style')) {
      var st = document.createElement('style');
      st.id = 'ku-done-style';
      st.textContent = DONE_CSS;
      (document.head || document.documentElement).appendChild(st);
    }
    doneOv = document.createElement('div');
    doneOv.id = 'ku-done-ov';
    doneOv.hidden = true;
    doneOv.innerHTML = '<div class="kd-mask"></div><div class="kd-sheet" role="dialog" aria-label="已完成">' +
      '<div class="kd-head"><span class="kd-title">已完成</span>' +
      '<button type="button" class="kd-btn" data-kd="refresh">刷新</button>' +
      '<button type="button" class="kd-btn" data-kd="close" aria-label="关闭">关闭</button></div>' +
      '<div class="kd-body"></div></div>';
    doneOv.querySelector('.kd-mask').addEventListener('click', closeDoneOverlay);
    doneOv.querySelector('[data-kd=close]').addEventListener('click', closeDoneOverlay);
    doneOv.querySelector('[data-kd=refresh]').addEventListener('click', function () {
      loadDone(true);
    });
    doneOv.querySelector('.kd-body').addEventListener('click', onDoneBodyClick);
    document.body.appendChild(doneOv);
    return doneOv;
  }

  function openDoneOverlay() {
    ensureDoneOverlay().hidden = false;
    scheduleSync();
    loadDone(false);
  }

  function tip(text) {
    var d = document.createElement('div');
    d.className = 'kd-tip';
    d.textContent = text;
    return d;
  }

  function fmtTime(v) {
    var t = new Date(v).getTime();
    if (!isFinite(t)) return '';
    var diff = Math.max(0, Date.now() - t) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + ' 天前';
    var d = new Date(t);
    return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' +
      ('0' + d.getDate()).slice(-2);
  }

  function renderDone(errText) {
    if (!isDoneOpen()) return;
    var body = doneOv.querySelector('.kd-body');
    var store = piniaStore('kimi.sessionList');
    body.textContent = '';
    if (!store) { body.appendChild(tip('无法读取已完成会话')); return; }
    var list = Array.isArray(store.doneSessions) ? store.doneSessions : [];
    var active = activeSessionId();
    for (var i = 0; i < list.length; i++) {
      var s = list[i];
      if (!s || typeof s.id !== 'string') continue;
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'kd-row' + (s.id === active ? ' active' : '');
      row.setAttribute('data-sid', s.id);
      var name = document.createElement('span');
      name.className = 'kd-name';
      name.textContent = s.title || s.lastPrompt || s.id.slice(0, 12);
      var meta = document.createElement('span');
      meta.className = 'kd-meta';
      meta.textContent = [s.workspaceName, fmtTime(s.updatedAt)].filter(Boolean).join(' · ');
      row.appendChild(name);
      row.appendChild(meta);
      body.appendChild(row);
    }
    if (errText) body.appendChild(tip(errText));
    else if (doneBusy && !list.length) body.appendChild(tip('加载中…'));
    else if (!list.length) body.appendChild(tip('还没有已完成的会话'));
    if (store.doneSessionsHasMore && list.length) {
      var more = document.createElement('button');
      more.type = 'button';
      more.className = 'kd-btn kd-more';
      more.setAttribute('data-kd', 'more');
      more.disabled = doneBusy || !!store.doneSessionsLoadingMore;
      more.textContent = more.disabled ? '加载中…' : '加载更多';
      body.appendChild(more);
    }
  }

  function runDone(fn) {
    if (doneBusy) return;
    doneBusy = true;
    renderDone();
    var done = function (err) {
      doneBusy = false;
      renderDone(err ? '加载失败，请点刷新重试' : '');
    };
    try {
      Promise.resolve(fn()).then(function () { done(false); }, function () { done(true); });
    } catch (e) {
      done(true);
    }
  }

  function loadDone(force) {
    var store = piniaStore('kimi.sessionList');
    if (!store || typeof store.ensureDoneSessions !== 'function') { renderDone(); return; }
    if (force) {
      // ensureDoneSessions 只在未播种时拉取；刷新时清掉播种标记重拉第一页
      try { store.doneSessionsSeeded = false; } catch (e) { /* 只读则保持现状 */ }
    }
    runDone(function () { return store.ensureDoneSessions(); });
  }

  function openSessionRoute(id) {
    var path = '/sessions/' + encodeURIComponent(id);
    try {
      var q = new URLSearchParams(window.location.search), keep = new URLSearchParams();
      if (q.get('rc') !== null) keep.set('rc', q.get('rc'));
      if (q.get('from') !== null) keep.set('from', q.get('from'));
      var qs = keep.toString();
      window.history.pushState(null, '', qs ? path + '?' + qs : path);
      window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
      return true;
    } catch (e) {
      return false;
    }
  }

  function onDoneBodyClick(e) {
    var t = e.target && e.target.closest ? e.target.closest('[data-sid],[data-kd=more]') : null;
    if (!t) return;
    e.preventDefault();
    if (t.getAttribute('data-kd') === 'more') {
      var store = piniaStore('kimi.sessionList');
      if (store && typeof store.loadMoreDoneSessions === 'function') {
        runDone(function () { return store.loadMoreDoneSessions(); });
      }
      return;
    }
    var sid = t.getAttribute('data-sid');
    if (sid && sid !== activeSessionId()) openSessionRoute(sid);
    closeDoneOverlay();
  }

  function installSteerButton() {
    try {
      ensureSteerButton();
      // 只观察 body 的子节点增删（不看 attributes/characterData），并以 200ms 合并，
      // 避免在流式输出时给手机叠加高频重排
      var mo = new MutationObserver(scheduleSync);
      mo.observe(document.body, { childList: true, subtree: true });
      scheduleSync();
    } catch (e) { /* 安装失败即静默降级，不影响原生队列面板 */ }
  }

  if (PHONE) {
    installScrollFix();
    installSteerButton();
  }
})();
