# -*- coding: utf-8 -*-
"""手机用量数据面：loopback 只读取数 + 真实 schema 白名单投影 + 有限缓存。

固定 GET http://127.0.0.1:39281/api/usage（本机用量面板插件 API），
socket timeout 3s + 总 deadline（防 slow trickle 每次 read 重置续命）、
响应 ≤1MiB。投影匹配 service.build_dashboard 真实 schema：
rate.tokens_per_hour/cost_per_hour、speed.tps/avg_tps 是格式字符串
（成本为 ¥ RMB，不虚构币种）；updated_at 采集时刻透传。严格类型、
非 finite 拒绝、字符串有界且拒绝 URL/路径形态值；未知键、会话 ID、
路径一律丢弃；错误不回显端口/地址/Errno。锁单飞 + 2s 缓存挡并发洪峰；
回源失败在有限 staleTTL 内降级旧缓存（标 _stale + 保留真实采集时刻），
连续失败指数退避，服务停超过上限抛 UsageFetchError（桥回 503）。
本模块只依赖 Python>=3.8 标准库。
"""
import http.client
import json
import math
import re
import threading
import time

USAGE_API_URL = 'http://127.0.0.1:39281/api/usage'
USAGE_API_HOST = '127.0.0.1'
USAGE_API_PORT = 39281
USAGE_API_PATH = '/api/usage'
USAGE_TIMEOUT_SECONDS = 3.0          # socket timeout（每次 IO）
USAGE_TOTAL_DEADLINE = 5.0           # 整个取数（连接+读）累计上限
USAGE_MAX_BODY = 1024 * 1024         # 1 MiB
USAGE_CACHE_SECONDS = 2.0            # 新鲜缓存：挡并发洪峰
USAGE_STALE_TTL_SECONDS = 30.0       # 回源失败可降级的旧缓存上限
USAGE_BACKOFF_MIN_SECONDS = 2.0      # 连续失败退避起点
USAGE_BACKOFF_MAX_SECONDS = 60.0     # 退避上限
_STRING_MAX = 128                    # 单个字符串字段上限
_MODEL_LIST_MAX = 32                 # 模型分布条数上限
# 字符串值不得含 URL/Windows 或 POSIX 绝对路径形态（防敏感污染借 fmt 出口）
_BAD_VALUE_RE = re.compile(
    r'(?i)(https?://|ftp://|\\\\|[a-z]:[/\\]|/(?:home|Users|etc|var|tmp|root)/)')

_lock = threading.Lock()
_cache = {'ts': 0.0, 'data': None}
_fail = {'count': 0, 'next_ok': 0.0}


class UsageFetchError(Exception):
    """取数/投影失败的对外信号；消息不含端口、地址、Errno 等敏感原文。"""


def reset_cache():
    with _lock:
        _cache['ts'] = 0.0
        _cache['data'] = None
        _fail['count'] = 0
        _fail['next_ok'] = 0.0


def _http_get(url, timeout, deadline=None):
    """固定 loopback GET；仅本函数接触网络，测试可整体替换。
    连接与响应头阶段受总 deadline 约束（socket timeout 裁剪到剩余预算，
    慢连接/慢头部不能越过总上限）。返回的响应由 fetch_usage 读完后显式
    close（HTTPResponse.close + 底层 conn.close 释放资源）。"""
    if url != USAGE_API_URL:
        raise UsageFetchError('用量数据接口不被允许')
    conn_timeout = timeout
    if deadline is not None:
        conn_timeout = max(0.05, min(timeout, deadline - time.monotonic()))
    conn = http.client.HTTPConnection(USAGE_API_HOST, USAGE_API_PORT,
                                      timeout=conn_timeout)
    try:
        conn.request('GET', USAGE_API_PATH, headers={'Accept': 'application/json'})
        if deadline is not None and conn.sock is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageFetchError('用量数据读取超时')
            conn.sock.settimeout(min(timeout, remaining))
        resp = conn.getresponse()
        resp._mu_conn = conn      # close 时一并断开底层连接
        return resp
    except UsageFetchError:
        try:
            conn.close()
        except Exception:
            pass
        raise
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise UsageFetchError('用量数据暂时不可用')


def _close_resp(resp):
    conn = getattr(resp, '_mu_conn', None)
    try:
        resp.close()
    except Exception:
        pass
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _read_capped(resp, limit, deadline):
    """分块读取：read1（拿到多少算多少，不为凑满整块聚合等待）；
    每轮把真实 socket timeout 裁剪到 deadline 剩余——单次阻塞绝不会超过
    剩余预算（read(65536) 在真实 HTTPResponse 上会为凑满聚合等待，慢字节
    流可在 deadline 检查之间越界，read1 无此问题）。"""
    chunks = []
    total = 0
    reader = getattr(resp, 'read1', None) or resp.read
    sock = getattr(resp, 'fp', None)
    raw_sock = getattr(getattr(sock, 'raw', None), '_sock', None) if sock else None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageFetchError('用量数据读取超时')
            if raw_sock is not None:
                try:
                    raw_sock.settimeout(min(USAGE_TIMEOUT_SECONDS, remaining))
                except Exception:
                    pass
            chunk = reader(min(65536, limit + 1 - total))
            if not chunk:
                if getattr(resp, 'length', None) not in (0, None):
                    raise UsageFetchError('用量数据不完整')
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise UsageFetchError('用量数据超出大小限制')
    except UsageFetchError:
        raise
    except Exception:
        raise UsageFetchError('用量数据读取失败')
    if time.monotonic() >= deadline:
        raise UsageFetchError('用量数据读取超时')
    return b''.join(chunks)


def _fail_closed(msg):
    raise UsageFetchError(msg)


def _proj_number(v, name):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _fail_closed('用量数据字段类型不合规')
    if not math.isfinite(float(v)):   # NaN/±Infinity 一律拒
        _fail_closed('用量数据含非有限(finite)数值')
    return v


def _proj_string(v, name):
    if not isinstance(v, str) or len(v) > _STRING_MAX:
        _fail_closed('用量数据字段类型不合规')
    if any(ord(c) < 0x20 or c == '\x7f' for c in v):
        _fail_closed('用量数据字段含控制字符')
    if _BAD_VALUE_RE.search(v):
        _fail_closed('用量数据字段含地址/路径形态')
    return v


# 白名单：顶层键 -> {子键: 投影函数}；不在表内的一律丢弃（含会话 ID/路径/URL）
_NUM = _proj_number
_STR = _proj_string
_SUMMARY_KEYS = {'tokens_fmt': _STR, 'cost_fmt': _STR, 'calls': _NUM,
                 'cache_pct': _NUM, 'date': _STR}
_TOP_KEYS = {
    'today': _SUMMARY_KEYS,
    'yesterday': _SUMMARY_KEYS,
    'week': _SUMMARY_KEYS,
    'month': _SUMMARY_KEYS,
    'cumul': _SUMMARY_KEYS,
    # 真实 schema：rate/speed 都是格式字符串（'12.3K/h'、'¥0.5/h'、'18 t/s'）
    'rate': {'tokens_per_hour': _STR, 'cost_per_hour': _STR, 'window': _STR},
    'cache': {'pct': _NUM, 'pct_fmt': _STR},
    'speed': {'tps': _STR, 'model': _STR, 'avg_tps': _STR},
    'cur': {'alias': _STR, 'effort': _STR},
}
_QUOTA_KEYS = {'used': _NUM, 'limit': _NUM, 'pct': _NUM, 'pace': _NUM,
               'reset': _STR, 'eta': _STR}
_MODEL_KEYS = {'model': _STR, 'tokens_fmt': _STR, 'calls': _NUM,
               'cache_pct': _NUM, 'cost_fmt': _STR}


def _proj_dict(obj, keys, name):
    if not isinstance(obj, dict):
        _fail_closed('用量数据结构不合规')
    out = {}
    for k, fn in keys.items():
        if k in obj and obj[k] is not None:
            out[k] = fn(obj[k], '%s.%s' % (name, k))
    return out


def _proj_models(obj, name):
    if not isinstance(obj, list):
        _fail_closed('用量数据结构不合规')
    # 合法大表不拒：只投影前 _MODEL_LIST_MAX 条（页面本就只展示前几名）
    return [_proj_dict(item, _MODEL_KEYS, name)
            for item in obj[:_MODEL_LIST_MAX]]


def _project(raw):
    """原始 JSON -> 白名单投影；任何不合规 fail-closed 整体拒。"""
    if not isinstance(raw, dict):
        _fail_closed('用量数据结构不合规')
    out = {}
    updated = raw.get('updated_at')
    if updated is not None:
        out['updated_at'] = _proj_string(updated, 'updated_at')
    for key, keys in _TOP_KEYS.items():
        if key in raw and raw[key] is not None:
            out[key] = _proj_dict(raw[key], keys, key)
    quota = raw.get('quota')
    if quota is not None:
        if not isinstance(quota, dict):
            _fail_closed('用量数据结构不合规')
        q = {}
        for scope in ('week', 'h5'):
            if scope in quota and quota[scope] is not None:
                q[scope] = _proj_dict(quota[scope], _QUOTA_KEYS,
                                      'quota.%s' % scope)
        out['quota'] = q
    for key in ('today_models', 'cumul_models'):
        if key in raw and raw[key] is not None:
            out[key] = _proj_models(raw[key], key)
    return out


def fetch_usage(transport=None):
    """取白名单投影后的用量数据。锁单飞 + 2s 缓存挡并发洪峰；
    回源失败在 USAGE_STALE_TTL_SECONDS 内降级旧缓存（标 _stale +
    _collected_at 真实采集时刻），连续失败指数退避，服务停超过上限
    抛 UsageFetchError。"""
    getter = transport or _http_get
    with _lock:
        now = time.monotonic()
        if _cache['data'] is not None and now - _cache['ts'] < USAGE_CACHE_SECONDS:
            return _cache['data']
        if now < _fail['next_ok']:
            # 失败退避窗口内：只用仍在 staleTTL 内的旧缓存，绝不回源
            if (_cache['data'] is not None
                    and now - _cache['ts'] < USAGE_STALE_TTL_SECONDS):
                return _stale_copy(_cache['data'])
            raise UsageFetchError('用量数据暂时不可用')
        deadline = now + USAGE_TOTAL_DEADLINE
        try:
            resp = getter(USAGE_API_URL, USAGE_TIMEOUT_SECONDS,
                          deadline=deadline)
            try:
                if getattr(resp, 'status', 0) != 200:
                    _fail_closed('用量数据暂时不可用')
                body = _read_capped(resp, USAGE_MAX_BODY, deadline)
            finally:
                _close_resp(resp)
            try:
                raw = json.loads(body.decode('utf-8'),
                                 parse_constant=lambda s: _fail_closed(
                                     '用量数据含非有限(finite)数值'))
            except UsageFetchError:
                raise
            except Exception:
                _fail_closed('用量数据不是合法 JSON')
            data = _project(raw)
        except UsageFetchError:
            _fail['count'] += 1
            _fail['next_ok'] = time.monotonic() + min(
                USAGE_BACKOFF_MAX_SECONDS,
                USAGE_BACKOFF_MIN_SECONDS * (2 ** min(_fail['count'] - 1, 5)))
            if (_cache['data'] is not None
                    and time.monotonic() - _cache['ts'] < USAGE_STALE_TTL_SECONDS):
                return _stale_copy(_cache['data'])
            raise
        except Exception:
            _fail['count'] += 1
            _fail['next_ok'] = time.monotonic() + min(
                USAGE_BACKOFF_MAX_SECONDS,
                USAGE_BACKOFF_MIN_SECONDS * (2 ** min(_fail['count'] - 1, 5)))
            if (_cache['data'] is not None
                    and time.monotonic() - _cache['ts'] < USAGE_STALE_TTL_SECONDS):
                return _stale_copy(_cache['data'])
            raise UsageFetchError('用量数据暂时不可用')
        _fail['count'] = 0
        _fail['next_ok'] = 0.0
        _cache['ts'] = time.monotonic()
        _cache['data'] = data
        return data


def _stale_copy(data):
    out = dict(data)
    out['_stale'] = True
    out['_collected_at'] = data.get('updated_at', '')
    return out


# ---------------- 用量 UI（整页与 SPA 弹层共用一份 CSS/标记/脚本） ----------------
# 样式选择器与 DOM 查询都限定在容器内：同一片段既能作 /mobile/usage 整页内容，
# 也能注入 SPA 弹层而不与宿主页面互相污染（宿主可能撞名 id/class，故统一用
# data-ku 属性 + ku- 前缀类名做容器内查询）。
# 取数与跳转一律经传入的 root 前缀拼同源路径：relay 下 root='/t/<tid>'
# （保住隧道前缀，不依赖 relay 的 cookie/Referer 兜底）；LAN 下 root=''，
# 退化为原先的根绝对路径，行为不变。
_USAGE_CSS = """\
#kuBox{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#e8e8ec;font-size:14px}
#kuBox *{box-sizing:border-box}
#kuBox h1{font-size:1.05em;font-weight:600;margin:4px 0 12px;flex:1;color:#e8e8ec}
#kuBox h2{font-size:.82em;color:#a8a8b3;font-weight:600;margin:0 0 6px}
#kuBox .ku-top{display:flex;align-items:center;gap:8px}
#kuBox .ku-back{padding:5px 12px;border-radius:999px;background:#17171d;color:#a8a8b3;border:1px solid #33333d;font-size:13px;cursor:pointer;text-decoration:none}
#kuBox .ku-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
@media(min-width:560px){#kuBox .ku-grid{grid-template-columns:repeat(4,1fr)}}
#kuBox .ku-card{background:#17171d;border:1px solid #26262e;border-radius:10px;padding:10px;margin-top:8px}
#kuBox .ku-grid .ku-card{margin-top:0}
#kuBox .ku-big{font-size:1.25em;font-weight:600;margin-top:2px}
#kuBox .ku-sub{color:#8b8b96;font-size:.78em;margin-top:2px}
#kuBox .ku-row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #22222a;font-size:.86em}
#kuBox .ku-row:last-child{border-bottom:none}
#kuBox .ku-k{color:#a8a8b3}
#kuBox .ku-bar{height:6px;background:#22222a;border-radius:3px;overflow:hidden;margin-top:6px}
#kuBox .ku-bar>i{display:block;height:100%;background:#5b8cff;border-radius:3px}
#kuBox .ku-st{color:#8b8b96;font-size:.76em;margin:12px 0 4px;text-align:center}
#kuBox .ku-err{color:#ff7d6b;text-align:center;padding:12px}
#kuBox .ku-stale{color:#e8b56b}
#kuBox .ku-mrow{display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid #22222a;font-size:.84em}
#kuBox .ku-mrow:last-child{border-bottom:none}
#kuBox .ku-mn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%}
"""

_USAGE_BODY = """\
<div class="ku-grid" data-ku="cards"></div>
<div class="ku-card" data-ku="quotaBox"><h2>官方配额</h2><div data-ku="quota"></div></div>
<div class="ku-card"><h2>速率 / 缓存 / 速度</h2><div data-ku="misc"></div></div>
<div class="ku-card"><h2>模型分布（今日）</h2><div data-ku="models"></div></div>
<div class="ku-st" data-ku="status">加载中…</div>
"""

# 挂载函数：host=容器元素（内含 _USAGE_BODY 标记），root=站点根前缀（''或/t/<id>）。
# 返回 {start,stop} 供弹层收起时暂停轮询、展开时续跑；整页场景忽略返回值。
_USAGE_JS = """\
function el(t,c){var e=document.createElement(t);if(c)e.className=c;return e;}
function txt(s){return (typeof s==='string')?s:'';}
function card(label,value,sub){
  var c=el('div','ku-card'),h=el('h2'),v=el('div','ku-big');
  h.textContent=label;v.textContent=value;c.appendChild(h);c.appendChild(v);
  if(sub){var s=el('div','ku-sub');s.textContent=sub;c.appendChild(s);}
  return c;}
function row(k,v){var r=el('div','ku-row'),a=el('span','ku-k'),b=el('span');
  a.textContent=k;b.textContent=v;r.appendChild(a);r.appendChild(b);return r;}
function quotaRow(name,q){
  var wrap=el('div');
  var pct=(typeof q.pct==='number'&&isFinite(q.pct))?Math.max(0,Math.min(100,q.pct)):null;
  wrap.appendChild(row(name,(q.used!=null?q.used:'?')+' / '+(q.limit!=null?q.limit:'?')+(pct!=null?'（'+pct.toFixed(1)+'%）':'')));
  if(pct!=null){var bar=el('div','ku-bar'),i=el('i');i.style.width=pct+'%';bar.appendChild(i);wrap.appendChild(bar);}
  if(typeof q.reset==='string'&&q.reset)wrap.appendChild(row('重置',txt(q.reset)));
  if(typeof q.eta==='string'&&q.eta)wrap.appendChild(row('预计耗尽',txt(q.eta)));
  return wrap;}
function render(host,d){
  function q(k){return host.querySelector('[data-ku="'+k+'"]');}
  function put(k,node){var b=q(k);if(!b)return;while(b.firstChild)b.removeChild(b.firstChild);if(node)b.appendChild(node);}
  var cards=el('div');cards.className='ku-grid';
  [['今日',d.today],['本周',d.week],['本月',d.month],['累计',d.cumul]].forEach(function(p){
    var s=p[1]||{},t=s.tokens_fmt||'—',sub=[];
    if(s.cost_fmt)sub.push(s.cost_fmt);
    if(s.calls!=null)sub.push(s.calls+' 次调用');
    if(typeof s.cache_pct==='number')sub.push('缓存 '+s.cache_pct.toFixed(0)+'%');
    cards.appendChild(card(p[0],String(t),sub.join(' · ')));});
  put('cards',cards);
  var qb=el('div');
  if(d.quota&&d.quota.week)qb.appendChild(quotaRow('每周',d.quota.week));
  if(d.quota&&d.quota.h5)qb.appendChild(quotaRow('5 小时',d.quota.h5));
  put('quota',qb);
  var quotaBox=q('quotaBox');if(quotaBox)quotaBox.style.display=qb.firstChild?'':'none';
  var misc=el('div');
  if(d.cache){if(d.cache.pct_fmt)misc.appendChild(row('缓存命中率',txt(d.cache.pct_fmt)));
    else if(typeof d.cache.pct==='number')misc.appendChild(row('缓存命中率',d.cache.pct.toFixed(1)+'%'));}
  if(d.rate){var w=d.rate.window?('（'+txt(d.rate.window)+'）'):'';
    if(d.rate.tokens_per_hour)misc.appendChild(row('Token 速率'+w,txt(d.rate.tokens_per_hour)));
    if(d.rate.cost_per_hour)misc.appendChild(row('成本速率'+w,txt(d.rate.cost_per_hour)));}
  if(d.speed){if(d.speed.tps)misc.appendChild(row('输出速度',txt(d.speed.tps)+(d.speed.model&&d.speed.model!=='--'?' · '+txt(d.speed.model):'')));
    if(d.speed.avg_tps)misc.appendChild(row('历史均值',txt(d.speed.avg_tps)));}
  put('misc',misc);
  var ms=el('div');
  (Array.isArray(d.today_models)?d.today_models:[]).slice(0,8).forEach(function(m){
    var r=el('div','ku-mrow'),n=el('span','ku-mn'),v=el('span');
    n.textContent=txt(m.model)||'(未知)';
    var bits=[];
    if(m.tokens_fmt)bits.push(m.tokens_fmt);
    if(m.calls!=null)bits.push(m.calls+' 次');
    if(m.cost_fmt)bits.push(m.cost_fmt);
    v.textContent=bits.join(' · ');
    r.appendChild(n);r.appendChild(v);ms.appendChild(r);});
  put('models',ms);
  var st=q('status');if(!st)return;
  if(d._stale){
    st.textContent='数据为缓存，采集于 '+(d._collected_at||d.updated_at||'未知')+'（用量服务暂不可用）';
    st.className='ku-st ku-stale';
  }else{
    st.textContent='采集于 '+(d.updated_at||'未知');
    st.className='ku-st';}}
function kimiUsageMount(host,root){
  var POLL=5000,FETCH_TIMEOUT=5500,timer=null,inflight=null,alive=true;
  function q(k){return host.querySelector('[data-ku="'+k+'"]');}
  function fail(t){var st=q('status');if(st){st.textContent=t;st.className='ku-st ku-err';}}
  function poll(){
    if(!alive||document.hidden||inflight)return;
    var ctrl=new AbortController();
    inflight=ctrl;
    var to=setTimeout(function(){ctrl.abort();},FETCH_TIMEOUT);
    fetch(root+'/mobile/usage/data',{headers:{'Accept':'application/json'},signal:ctrl.signal})
      .then(function(r){
        if(r.status===403){location.replace(root+'/mobile/pair');return null;}
        if(!r.ok)throw new Error('bad');
        return r.json();})
      .then(function(d){if(d)render(host,d);})
      .catch(function(e){if(e&&e.name!=='AbortError')fail('用量数据暂时不可用');else if(e)fail('用量数据请求超时');})
      .finally(function(){clearTimeout(to);inflight=null;});}
  function loop(){poll();if(alive)timer=setTimeout(loop,POLL);}
  function onVis(){
    if(document.hidden){
      if(timer){clearTimeout(timer);timer=null;}
      if(inflight){inflight.abort();inflight=null;}}
    else if(!timer&&alive){loop();}}
  document.addEventListener('visibilitychange',onVis);
  loop();
  return {
    stop:function(){alive=false;
      if(timer){clearTimeout(timer);timer=null;}
      if(inflight){inflight.abort();inflight=null;}
      document.removeEventListener('visibilitychange',onVis);},
    start:function(){if(!alive){alive=true;document.addEventListener('visibilitychange',onVis);}
      if(!timer&&!document.hidden)loop();}};
}
"""

USAGE_PAGE_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Kimi Code · 手机用量</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#101014;color:#e8e8ec;margin:0;padding:12px;font-size:14px}
""" + _USAGE_CSS + """
</style></head><body>
<div id="kuBox"><div class="ku-top"><h1>Kimi Code 用量</h1><button class="ku-back" data-ku-back type="button">返回聊天</button></div>
""" + _USAGE_BODY + """
</div>
<script>
(function(){
'use strict';
""" + _USAGE_JS + """
// 本页路径是 <root>/mobile/usage（relay 下 root=/t/<id>，否则 root=''）；
// 剥掉末段得到 root，取数/回配对/回聊天全部按 root 前缀拼，不丢隧道归属。
var root=location.pathname.replace(/\\/mobile\\/usage\\/?$/,'');
kimiUsageMount(document.getElementById('kuBox'),root);
document.querySelector('[data-ku-back]').addEventListener('click',function(){
  if(window.history.length>1)history.back();else location.replace(root+'/');});
})();
</script></body></html>"""


# SPA 弹层注入片段：入口 <button>（不再跳页）+ 遮罩/底部 sheet + 共享 UI。
# root 从 SPA 当前 URL 的 /t/<tid> 段取（SPA 可能已丢前缀走 cookie 兜底——
# 取不到就退回 ''，与整页同一路径规则）；sheet 内滚动，遮罩/关闭/再点按钮收起。
USAGE_OVERLAY_HTML = """\
<style>
#kuOv{position:fixed;inset:0;z-index:10000}
#kuOv[hidden]{display:none}
#kuOv .ku-mask{position:absolute;inset:0;background:rgba(8,8,12,.6)}
#kuOv .ku-sheet{position:absolute;left:0;right:0;bottom:0;top:6%;background:#101014;border-top:1px solid #33333d;border-radius:14px 14px 0 0;overflow-y:auto;padding:12px}
</style>
<style>
""" + _USAGE_CSS + """
</style>
<div id="kuOv" hidden>
<div class="ku-mask"></div>
<div class="ku-sheet"><div id="kuBox">
<div class="ku-top"><h1>Kimi Code 用量</h1><button class="ku-back" data-ku-back type="button">关闭</button></div>
""" + _USAGE_BODY + """
</div></div></div>
<button id="kuUsageBtn" type="button" style="position:fixed;right:12px;bottom:12px;z-index:10001;padding:7px 14px;border-radius:999px;background:#17171d;color:#a8a8b3;border:1px solid #33333d;font:13px system-ui,sans-serif;opacity:.85;cursor:pointer">用量</button>
<script>
(function(){
'use strict';
""" + _USAGE_JS + """
var m=location.pathname.match(/^(\\/t\\/[A-Za-z0-9_-]{4,32})(?=\\/|$)/);
var root=m?m[1]:'';
var ov=document.getElementById('kuOv'),inst=null;
function open(){ov.hidden=false;
  if(!inst){inst=kimiUsageMount(document.getElementById('kuBox'),root);}else{inst.start();}}
function close(){ov.hidden=true;if(inst)inst.stop();}
document.getElementById('kuUsageBtn').addEventListener('click',function(){
  if(ov.hidden)open();else close();});
ov.querySelector('.ku-mask').addEventListener('click',close);
ov.querySelector('[data-ku-back]').addEventListener('click',close);
})();
</script>"""


__all__ = ['fetch_usage', 'reset_cache', 'UsageFetchError', 'USAGE_PAGE_HTML',
           'USAGE_OVERLAY_HTML',
           'USAGE_API_URL', 'USAGE_TIMEOUT_SECONDS', 'USAGE_TOTAL_DEADLINE',
           'USAGE_MAX_BODY', 'USAGE_CACHE_SECONDS', 'USAGE_STALE_TTL_SECONDS']
