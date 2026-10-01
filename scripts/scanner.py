# -*- coding: utf-8 -*-
"""
wire.jsonl 增量扫描器：聚合 Kimi Code 会话的 token 用量 / 缓存命中 / 生成速度。

数据源：
  ~/.kimi-code/sessions/*/*/agents/*/wire.jsonl
    - {"type":"usage.record","model":..., "usage":{inputOther,output,inputCacheRead,inputCacheCreation}, "time":ms}
    - {"type":"context.append_loop_event","event":{"type":"step.end","usage":{...},"llmStreamDurationMs":ms}, "time":ms}
    - {"type":"llm.request","modelAlias":...,"thinkingEffort":..., "time":ms}
  官方额度：GET <base_url>/usages（本地 OAuth Bearer，端点跟随 config.toml）

定价（Kimi K3 官方价，元/百万 token）：输入 20、缓存读 2、输出 100；缓存创建按标准输入价。

对外提供 UsageScanner.snapshot()（线程安全）、fetch_official()
与 export_state()/import_state()（跨重启保留历史聚合与字节偏移）。
"""
import json
import os
import glob
import threading
import time
from collections import deque, defaultdict

HOME = os.path.expanduser('~/.kimi-code')
SESSIONS = os.path.join(HOME, 'sessions')
CONFIG_FILE = os.path.join(HOME, 'config.toml')
CRED_DIR = os.path.join(HOME, 'credentials')
CRED_FILE = os.path.join(CRED_DIR, 'kimi-code.json')
USAGES_URL = 'https://api.kimi.com/coding/v1/usages'

PRICE_INPUT = 20.0
PRICE_OUTPUT = 100.0
PRICE_CACHE_READ = 2.0          # 缓存创建按输入价计入

STATE_VERSION = 4
MAX_DAYS = 400
MAX_SESSIONS = 200

# 自定义计价（元/百万 token 或 元/次）：
#   ~/.kimi-code/usage-dashboard/pricing.json  （主，本服务写入）
#   ~/.kimi-code/model-manager/pricing.json    （兼容旧套件，合并读取，新文件优先）
# {"<alias或model>": {"mode":"volume","input":20,"output":100,"cache_hit":2,"cache_write":20}}
# {"<alias或model>": {"mode":"per_call","price":0.05}}
PRICING_FILES = [
    os.path.join(HOME, 'usage-dashboard', 'pricing.json'),
    os.path.join(HOME, 'model-manager', 'pricing.json'),
]
_PRICING = {'map': {}, 'mtimes': {}}
_PRICING_LOCK = threading.Lock()
# wire 日志里的 model 是裸 id（如 devin/swe-2），价格常按别名存；
# 由 service.py 每轮写入 {裸model_id: [别名,...]} 供反查。
MODEL_ALIASES = {}


def _load_pricing():
    mtimes = {}
    for p in PRICING_FILES:
        try:
            mtimes[p] = os.path.getmtime(p)
        except OSError:
            mtimes[p] = -1.0
    with _PRICING_LOCK:
        if mtimes == _PRICING['mtimes']:
            return
        m = {}
        for p in reversed(PRICING_FILES):      # 先旧后新，新文件覆盖
            if mtimes.get(p, -1) < 0:
                continue
            try:
                d = json.load(open(p, encoding='utf-8'))
                if isinstance(d, dict):
                    m.update(d)
            except Exception:
                pass
        _PRICING['map'] = m
        _PRICING['mtimes'] = mtimes


def _price_for(model):
    """按别名或裸 model id 找自定义价格；未配置返回 None。"""
    _load_pricing()
    m = _PRICING['map']
    if not m:
        return None
    key = str(model or '')
    p = m.get(key)
    if p is None:
        for a in MODEL_ALIASES.get(key, ()):
            p = m.get(a)
            if p is not None:
                break
    if p is None and '/' in key:
        p = m.get(key.split('/')[-1]) or m.get(key.split('/', 1)[-1])
    return p


def _cost_of(model, u):
    """单条 usage 的成本：自定义计价优先，否则默认 K3 价。"""
    p = _price_for(model)
    if p and p.get('mode') == 'per_call':
        return float(p.get('price') or 0)
    if p and p.get('mode') == 'volume':
        return (u.get('inputOther', 0) * float(p.get('input', PRICE_INPUT))
                + u.get('output', 0) * float(p.get('output', PRICE_OUTPUT))
                + u.get('inputCacheRead', 0) * float(p.get('cache_hit', PRICE_CACHE_READ))
                + u.get('inputCacheCreation', 0) * float(p.get('cache_write', p.get('input', PRICE_INPUT)))) / 1e6
    return _cost(u)


def priced_cost(model, b):
    """对一个聚合桶按当前计价重算成本（改价即时生效，无需重扫）。"""
    p = _price_for(model)
    if p and p.get('mode') == 'per_call':
        return float(p.get('price') or 0) * b.get('records', 0)
    if p and p.get('mode') == 'volume':
        pin = float(p.get('input', PRICE_INPUT))
        pout = float(p.get('output', PRICE_OUTPUT))
        pcr = float(p.get('cache_hit', PRICE_CACHE_READ))
        pcw = p.get('cache_write')
        pcw = float(pcw) if pcw is not None else pin
        other = b.get('input', 0) - b.get('cache_read', 0) - b.get('cache_create', 0)
        return (max(other, 0) * pin + b.get('output', 0) * pout
                + b.get('cache_read', 0) * pcr + b.get('cache_create', 0) * pcw) / 1e6
    return float(b.get('cost', 0.0))


def _today_start_ms(now_ms=None):
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    lt = time.localtime(now_ms / 1000.0)
    t0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return int(t0 * 1000)


def _day_key(t_ms):
    return time.strftime('%Y-%m-%d', time.localtime(t_ms / 1000.0))


def _cost(u):
    return (u.get('inputOther', 0) * PRICE_INPUT
            + u.get('output', 0) * PRICE_OUTPUT
            + u.get('inputCacheRead', 0) * PRICE_CACHE_READ
            + u.get('inputCacheCreation', 0) * PRICE_INPUT) / 1e6


def _total(u):
    return (u.get('inputOther', 0) + u.get('output', 0)
            + u.get('inputCacheRead', 0) + u.get('inputCacheCreation', 0))


def _input(u):
    return u.get('inputOther', 0) + u.get('inputCacheRead', 0) + u.get('inputCacheCreation', 0)


def _new_bucket():
    return {'tokens': 0, 'cost': 0.0, 'input': 0, 'output': 0,
            'cache_read': 0, 'cache_create': 0, 'records': 0}


def _add(bucket, u, cost=None):
    bucket['tokens'] += _total(u)
    bucket['cost'] += _cost(u) if cost is None else cost
    bucket['input'] += _input(u)
    bucket['output'] += u.get('output', 0)
    bucket['cache_read'] += u.get('inputCacheRead', 0)
    bucket['cache_create'] += u.get('inputCacheCreation', 0)
    bucket['records'] += 1


def _hit(b):
    return (b['cache_read'] / b['input']) if b.get('input') else 0.0


class _Agg(object):
    """累计 + 今日 + 分模型 + 按日 + 逐小时 桶。"""

    def __init__(self):
        self.day = _today_start_ms()
        self.all = _new_bucket()
        self.today = _new_bucket()
        self.models_all = defaultdict(_new_bucket)
        self.models_today = defaultdict(_new_bucket)
        self.daily = {}                 # day_key 'YYYY-MM-DD' -> bucket
        self.daily_models = {}          # day_key 'YYYY-MM-DD' -> {model: bucket}
        self.hourly = defaultdict(dict)  # day_key -> {hour_int: [tokens, calls]}

    def rollover(self):
        d = _today_start_ms()
        if d != self.day:
            self.day = d
            self.today = _new_bucket()
            self.models_today = defaultdict(_new_bucket)

    def add(self, model, u, t_ms, cost=None):
        self.rollover()
        if cost is None:
            cost = _cost_of(model, u)
        _add(self.all, u, cost)
        _add(self.models_all[model], u, cost)
        if t_ms > 0:
            dk = _day_key(t_ms)
            _add(self.daily.setdefault(dk, _new_bucket()), u, cost)
            _add(self.daily_models.setdefault(dk, {}).setdefault(model, _new_bucket()), u, cost)
            hh = int(time.strftime('%H', time.localtime(t_ms / 1000.0)))
            cell = self.hourly[dk].setdefault(hh, [0, 0])
            cell[0] += _total(u)
            cell[1] += 1
            if len(self.daily) > MAX_DAYS:
                for old in sorted(self.daily.keys())[:-MAX_DAYS]:
                    self.daily.pop(old, None)
                    self.hourly.pop(old, None)
                    self.daily_models.pop(old, None)
        if t_ms >= self.day:
            _add(self.today, u, cost)
            _add(self.models_today[model], u, cost)


class UsageScanner(object):
    """后台增量扫描所有 wire.jsonl（字节偏移续扫）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._agg = _Agg()
        self._offsets = {}          # path -> [size, mtime, offset]
        self._step_events = deque(maxlen=600)   # (t_ms, model, output, stream_ms)
        self._usage_events = deque(maxlen=20000)  # (t_ms, tokens, cost)
        self._stop = threading.Event()
        self._scan_lock = threading.Lock()   # scan_once 串行化：后台线程与 collect_once 不得并发扫同一文件
        self._thread = None
        self._last_model = None
        self._cur = {'model': '', 'alias': '', 'effort': '', 'time': 0,
                     'session': '', 'sess_model': '', 'sub_agent': ''}
        self._sess = {}            # session key -> bucket + last
        self._speed_out = 0        # 全量输出 token（历史均值用）
        self._speed_dur = 0        # 全量流式时长 ms
        self.first_scan_done = threading.Event()
        self.files_seen = 0

    # ---------------- 扫描 ----------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def reset(self):
        """清空聚合与偏移，下次 scan_once 全量重扫。"""
        with self._lock:
            self._agg = _Agg()
            self._offsets = {}
            self._sess = {}
            self._usage_events.clear()
            self._step_events.clear()
            self._speed_out = 0
            self._speed_dur = 0
            self._cur = {'model': '', 'alias': '', 'effort': '', 'time': 0,
                         'session': '', 'sess_model': '', 'sub_agent': ''}

    def _run(self):
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            self._stop.wait(2.0)

    def scan_once(self):
        with self._scan_lock:
            self._scan_once_locked()

    def _scan_once_locked(self):
        paths = glob.glob(os.path.join(SESSIONS, '*', '*', 'agents', '*', 'wire.jsonl'))
        self.files_seen = len(paths)
        for p in paths:
            try:
                self._scan_file(p)
            except Exception:
                continue
        self.first_scan_done.set()

    def _scan_file(self, path):
        try:
            st = os.stat(path)
        except OSError:
            self._offsets.pop(path, None)
            return
        size, mtime = st.st_size, st.st_mtime
        prev = self._offsets.get(path)
        offset = 0
        if prev and prev[0] == size and prev[1] == mtime:
            return                      # 未变化
        if prev:
            offset = prev[2]
            if size < offset:           # 截断/重建，从头读
                offset = 0
        with open(path, 'rb') as f:
            f.seek(offset)
            data = f.read()
            new_offset = f.tell()
        self._offsets[path] = [size, mtime, new_offset]
        if not data:
            return
        # <wd>/<session>/agents/<agent>/wire.jsonl → 会话目录名 + agent 目录名
        agent_dir = os.path.basename(os.path.dirname(path))
        sess = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))
        for line in data.split(b'\n'):
            if b'usage.record' in line:
                self._parse_usage(line, sess)
            elif b'"llm.request"' in line:
                self._parse_request(line, agent_dir)
            elif b'llmStreamDurationMs' in line:
                self._parse_step(line)

    # ---------------- 解析 ----------------
    def _parse_usage(self, line, sess=None):
        try:
            rec = json.loads(line)
        except ValueError:
            return
        if rec.get('type') != 'usage.record':
            return
        u = rec.get('usage') or {}
        model = str(rec.get('model') or '?')
        t = rec.get('time') or 0
        cost = _cost_of(model, u)
        with self._lock:
            self._last_model = model
            self._agg.add(model, u, t, cost)
            if t > 0:
                self._usage_events.append((t, _total(u), cost))
            if sess:
                b = self._sess.setdefault(sess, _new_bucket())
                _add(b, u, cost)
                if t > b.get('last', 0):
                    b['last'] = t
                    self._cur['session'] = sess
                    self._cur['sess_model'] = model
                if len(self._sess) > MAX_SESSIONS:
                    for old in sorted(self._sess, key=lambda k: self._sess[k].get('last', 0))[:-MAX_SESSIONS]:
                        self._sess.pop(old, None)

    def _parse_request(self, line, agent_dir=''):
        try:
            rec = json.loads(line)
        except ValueError:
            return
        if rec.get('type') != 'llm.request':
            return
        t = rec.get('time') or 0
        with self._lock:
            cur = self._cur
            if t >= cur.get('time', 0):
                cur['time'] = t
                cur['model'] = str(rec.get('model') or cur.get('model') or '?')
                if rec.get('modelAlias'):
                    cur['alias'] = str(rec['modelAlias'])
                elif cur['model']:
                    cur['alias'] = cur['model']
                if rec.get('thinkingEffort'):
                    cur['effort'] = str(rec['thinkingEffort'])
                cur['sub_agent'] = '' if agent_dir in ('main', '') else agent_dir

    def _parse_step(self, line):
        try:
            rec = json.loads(line)
        except ValueError:
            return
        ev = rec.get('event') or {}
        if ev.get('type') != 'step.end':
            return
        u = ev.get('usage') or {}
        out = u.get('output', 0)
        dur = ev.get('llmStreamDurationMs') or 0
        t = rec.get('time') or 0
        if out <= 0 or dur <= 0:
            return
        model = rec.get('model') or ev.get('model') or self._last_model or '?'
        with self._lock:
            self._step_events.append((t, str(model), out, dur))
            self._speed_out += out
            self._speed_dur += dur

    # ---------------- 快照 ----------------
    def snapshot(self):
        with self._lock:
            self._agg.rollover()
            agg = self._agg
            evs = list(self._step_events)
            uevs = list(self._usage_events)
            cur = dict(self._cur)
            sess_map = {k: dict(v) for k, v in self._sess.items()}
            daily = {k: dict(v) for k, v in agg.daily.items()}
            daily_models = {k: {m: dict(b) for m, b in v.items()}
                            for k, v in agg.daily_models.items()}
            hourly = {k: dict(v) for k, v in agg.hourly.items()}
            speed_out, speed_dur = self._speed_out, self._speed_dur
            files = self.files_seen

        now_ms = int(time.time() * 1000)
        day0 = _today_start_ms(now_ms)

        # 实时 TPS：最近 120s 内 step.end，Σoutput/Σstream
        recent = [e for e in evs if e[0] >= now_ms - 120000]
        tps = 0.0
        tps_model = ''
        if recent:
            out = sum(e[2] for e in recent)
            dur = sum(e[3] for e in recent)
            tps = (out * 1000.0 / dur) if dur > 0 else 0.0
            tps_model = recent[-1][1]
        elif evs:
            e = evs[-1]
            tps = (e[2] * 1000.0 / e[3]) if e[3] > 0 else 0.0
            tps_model = e[1]

        avg_tps = (speed_out * 1000.0 / speed_dur) if speed_dur > 0 else 0.0

        def model_rows(mdict):
            rows = [{'model': m, 'tokens': b['tokens'], 'cost': b['cost'],
                     'input': b['input'], 'output': b['output'],
                     'cache_read': b['cache_read'], 'cache_create': b['cache_create'],
                     'records': b['records'], 'calls': b['records'],
                     'hit': _hit(b)} for m, b in mdict.items()]
            rows.sort(key=lambda r: -r['tokens'])
            return rows

        sess_rows = []
        for k, b in sess_map.items():
            r = dict(b)
            r['key'] = k
            r['hit'] = _hit(b)
            sess_rows.append(r)
        sess_rows.sort(key=lambda r: -r['tokens'])

        sess_key = cur.get('session')
        sess_b = sess_map.get(sess_key) if sess_key else None
        session = (dict(sess_b, key=sess_key, hit=_hit(sess_b))
                   if sess_b else None)

        today_key = _day_key(now_ms)
        hourly_today = hourly.get(today_key) or {}

        return {
            'ts': now_ms,
            'day_start': day0,
            'files': files,
            'cur': cur,
            'session': session,
            'sessions': sess_rows,
            'today': dict(agg.today, hit=_hit(agg.today)),
            'all': dict(agg.all, hit=_hit(agg.all)),
            'models_today': model_rows(agg.models_today),
            'models_all': model_rows(agg.models_all),
            'tps': tps,
            'tps_model': tps_model,
            'avg_tps': avg_tps,
            'daily': daily,           # day_key -> bucket
            'daily_models': daily_models,  # day_key -> {model: bucket}
            'hourly': hourly,         # day_key -> {hour: [tokens, calls]}
            'hourly_today': [hourly_today.get(h, [0, 0]) for h in range(24)],
            'burn': self._burn(uevs, now_ms),
        }

    def _burn(self, uevs, now_ms):
        """最近 60 分钟实算消耗速率：tokens/h、¥/h。"""
        win = [e for e in uevs if e[0] >= now_ms - 3600000]
        if not win:
            return {'tokens': 0, 'cost': 0.0}
        return {'tokens': sum(e[1] for e in win),
                'cost': sum(e[2] for e in win)}

    # ---------------- 持久化 ----------------
    def export_state(self):
        with self._lock:
            now_ms = int(time.time() * 1000)
            return {
                'version': STATE_VERSION,
                'saved_at': now_ms,
                'offsets': dict(self._offsets),
                'all': self._agg.all,
                'today': self._agg.today,
                'today_key': _day_key(self._agg.day),
                'models_all': dict(self._agg.models_all),
                'models_today': dict(self._agg.models_today),
                'daily': self._agg.daily,
                'daily_models': {dk: dict(v) for dk, v in self._agg.daily_models.items()},
                'hourly': {k: dict(v) for k, v in self._agg.hourly.items()},
                'sessions': self._sess,
                'speed': {'out': self._speed_out, 'dur': self._speed_dur},
                'cur': dict(self._cur),
                'last_model': self._last_model,
                # 速率窗口：只留近 60 分钟用量事件与近 5 分钟 step 事件
                'usage_events': [e for e in self._usage_events if e[0] >= now_ms - 3600000],
                'step_events': [e for e in self._step_events if e[0] >= now_ms - 300000],
            }

    def import_state(self, st):
        if not isinstance(st, dict) or st.get('version') != STATE_VERSION:
            return False
        with self._lock:
            self._offsets.update(st.get('offsets') or {})
            if st.get('all'):
                self._agg.all.update({k: v for k, v in st['all'].items() if k in self._agg.all})
            if st.get('today_key') == _day_key(int(time.time() * 1000)):
                if st.get('today'):
                    self._agg.today.update({k: v for k, v in st['today'].items() if k in self._agg.today})
                for m, b in (st.get('models_today') or {}).items():
                    nb = _new_bucket()
                    nb.update({k: v for k, v in b.items() if k in nb})
                    self._agg.models_today[m] = nb
            for m, b in (st.get('models_all') or {}).items():
                nb = _new_bucket()
                nb.update({k: v for k, v in b.items() if k in nb})
                self._agg.models_all[m] = nb
            for dk, b in (st.get('daily') or {}).items():
                nb = _new_bucket()
                nb.update({k: v for k, v in b.items() if k in nb})
                self._agg.daily[dk] = nb
            for dk, mdict in (st.get('daily_models') or {}).items():
                for m, b in mdict.items():
                    nb = _new_bucket()
                    nb.update({k: v for k, v in b.items() if k in nb})
                    self._agg.daily_models.setdefault(dk, {})[m] = nb
            for dk, hmap in (st.get('hourly') or {}).items():
                for h, cell in hmap.items():
                    self._agg.hourly[dk][int(h)] = list(cell)
            for sk, b in (st.get('sessions') or {}).items():
                nb = _new_bucket()
                nb.update({k: v for k, v in b.items() if k in nb or k == 'last'})
                self._sess[sk] = nb
            sp = st.get('speed') or {}
            self._speed_out = int(sp.get('out') or 0)
            self._speed_dur = int(sp.get('dur') or 0)
            if st.get('cur'):
                self._cur.update(st['cur'])
            if st.get('last_model'):
                self._last_model = st['last_model']
            for e in st.get('usage_events') or []:
                self._usage_events.append(tuple(e))
            for e in st.get('step_events') or []:
                self._step_events.append(tuple(e))
        return True


# ---------------- 官方额度 ----------------
def resolve_official_endpoint():
    """凭据槽位 + usages 端点跟随 config.toml 的当前登录环境。"""
    name, url = None, None
    try:
        section = ''
        with open(CONFIG_FILE, encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.strip()
                if line.startswith('['):
                    section = line.strip('[] ')
                elif '=' in line and not line.startswith('#'):
                    k, _, v = line.partition('=')
                    v = v.strip().strip('"')
                    if section == 'providers."managed:kimi-code"' and k.strip() == 'base_url' and v:
                        url = v.rstrip('/') + '/usages'
                    elif section == 'providers."managed:kimi-code".oauth' and k.strip() == 'key' and v:
                        name = v.split('/')[-1]
    except OSError:
        pass
    cred = os.path.join(CRED_DIR, name + '.json') if name else CRED_FILE
    return cred, (url or USAGES_URL)


def fetch_official():
    """返回 {'wk_used','wk_limit','wk_reset','h5_used','h5_limit','h5_reset','ts'} 或 None。"""
    import urllib.request
    try:
        cred_file, usages_url = resolve_official_endpoint()
        cred = json.load(open(cred_file))
        if cred.get('expires_at', 0) < time.time() + 10:
            return None
        req = urllib.request.Request(usages_url, headers={
            'Authorization': 'Bearer ' + cred['access_token'],
            'Accept': 'application/json',
            'User-Agent': 'kimi-code-cli'})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode())
        out = {'ts': time.time()}
        wk = d.get('usage') or {}
        if wk.get('limit'):
            out['wk_used'] = float(wk.get('used', 0))
            out['wk_limit'] = float(wk['limit'])
            out['wk_reset'] = wk.get('resetTime', '')
        for item in d.get('limits') or []:
            w = item.get('window') or {}
            if w.get('duration') == 300 and w.get('timeUnit') == 'TIME_UNIT_MINUTE':
                det = item.get('detail') or {}
                if det.get('limit'):
                    out['h5_used'] = float(det.get('used', 0))
                    out['h5_limit'] = float(det['limit'])
                    out['h5_reset'] = det.get('resetTime', '')
        return out if len(out) > 1 else None
    except Exception:
        return None


# ---------------- 格式化 ----------------
def fmt_tokens(n):
    n = int(n)
    if n >= 1000000:
        return '%.1fM' % (n / 1e6)
    if n >= 1000:
        return '%.1fK' % (n / 1e3)
    return str(n)


def fmt_money(x):
    if x >= 100:
        return '¥%.0f' % x
    if x >= 1:
        return '¥%.1f' % x
    return '¥%.2f' % x


def fmt_pct(x):
    return '%.0f%%' % (x * 100.0)


if __name__ == '__main__':
    sc = UsageScanner()
    sc.scan_once()
    snap = sc.snapshot()
    print('files:', snap['files'])
    print('today:', {k: snap['today'][k] for k in ('tokens', 'cost', 'hit', 'records')})
    print('all  :', {k: snap['all'][k] for k in ('tokens', 'cost', 'hit', 'records')})
    print('tps  : %.1f (%s) avg %.1f' % (snap['tps'], snap['tps_model'], snap['avg_tps']))
    print('sessions:', len(snap['sessions']))
    for r in snap['models_today'][:5]:
        print('  model today:', r['model'], fmt_tokens(r['tokens']), fmt_pct(r['hit']))
    print('official:', fetch_official())
