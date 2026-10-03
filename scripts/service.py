# -*- coding: utf-8 -*-
"""
Kimi Code 用量面板 · 本地服务（单进程）

职责：
  1. 后台扫描 wire.jsonl → 聚合 今日/昨日/本周/本月/累计/逐日/逐时/会话/模型
  2. 每 2s 产出同源数据文件写进桌面端 desktop-dist：
       assets/kimi-usage-data.js  (window.__KIMI_DATA__，侧栏卡片瞬时加载)
       assets/kimi-usage-widget.js (侧栏卡片本体，自愈看护)
       kimi-usage.json            (面板/Skill/命令可读的报表数据)
  3. 看护 index.html 注入点（桌面端更新覆盖后自动重注入，清理旧套件残留注入）
  4. HTTP API :39281 模型能力管理（config.toml 安全改写：备份+kimi doctor 校验）
  5. --tick      : hook 调用入口——确保服务在跑、跑一次采集、注入检查，然后退出
     --once      : 只采集+写文件，不常驻（无服务时的降级刷新）
     无参数      : 常驻服务

启动链：kimi.plugin.json hooks → scripts/bootstrap.cmd → service.py
（桌面端无需任何开机自启/桌面脚本；服务随会话心跳拉起，随客户端退出自然闲置）
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 39281
PLUGIN_ROOT = os.environ.get('KIMI_PLUGIN_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_SRC = os.path.join(PLUGIN_ROOT, 'assets')
HOME = os.path.expanduser('~')
KIMI_HOME = os.path.join(HOME, '.kimi-code')
CONFIG_PATH = os.path.join(KIMI_HOME, 'config.toml')
CONFIG_NEW_PATH = os.path.join(KIMI_HOME, 'config-new.toml')
STATE_FILE = os.path.join(KIMI_HOME, 'usage-collector-state.json')
LOG_FILE = os.path.join(SCRIPT_DIR, 'service.log')

WIDGET_SRC = os.path.join(ASSETS_SRC, 'kimi-usage-widget.js')
PRICING_PATH = os.path.join(KIMI_HOME, 'usage-dashboard', 'pricing.json')
DISMISS_PATH = os.path.join(KIMI_HOME, 'usage-dashboard', 'dismissed-issues.json')

# 自更新源：GitHub 仓库（改源只需改 UPDATE_REPO）
UPDATE_REPO = 'ziyiclouds-blip/kimicode-ylmb'
UPDATE_BRANCH = 'main'
# 走 api.github.com 读清单：raw.* 按分支缓存较久，刚发布时容易拿到旧版本号
UPDATE_MANIFEST_URL = ('https://api.github.com/repos/%s/contents/kimi.plugin.json?ref=%s'
                       % (UPDATE_REPO, UPDATE_BRANCH))
UPDATE_MANIFEST_RAW_URL = ('https://raw.githubusercontent.com/%s/%s/kimi.plugin.json'
                           % (UPDATE_REPO, UPDATE_BRANCH))
UPDATE_CHANGELOG_URL = ('https://api.github.com/repos/%s/contents/CHANGELOG.md?ref=%s'
                       % (UPDATE_REPO, UPDATE_BRANCH))
UPDATE_CHANGELOG_RAW_URL = ('https://raw.githubusercontent.com/%s/%s/CHANGELOG.md'
                            % (UPDATE_REPO, UPDATE_BRANCH))
UPDATE_ZIP_URL = 'https://codeload.github.com/%s/zip/refs/heads/%s' % (UPDATE_REPO, UPDATE_BRANCH)


def plugin_version():
    try:
        with open(os.path.join(PLUGIN_ROOT, 'kimi.plugin.json'), encoding='utf-8') as f:
            return str(json.load(f).get('version') or '0.0.0')
    except Exception:
        return '0.0.0'


PLUGIN_VERSION = plugin_version()


def load_pricing():
    try:
        with open(PRICING_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_dismissed():
    try:
        with open(DISMISS_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return set(d) if isinstance(d, list) else set()
    except Exception:
        return set()


def save_dismissed(items):
    os.makedirs(os.path.dirname(DISMISS_PATH), exist_ok=True)
    with open(DISMISS_PATH, 'w', encoding='utf-8') as f:
        json.dump(sorted(items), f, ensure_ascii=False)


def save_pricing(alias, entry):
    m = load_pricing()
    if entry is None:
        m.pop(alias, None)
    else:
        m[alias] = entry
    os.makedirs(os.path.dirname(PRICING_PATH), exist_ok=True)
    safe_write(PRICING_PATH, json.dumps(m, ensure_ascii=False, indent=2))

sys.path.insert(0, SCRIPT_DIR)
import scanner  # noqa: E402


def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


# ---------------- desktop-dist 定位 ----------------
def _remember_dist(path):
    """把探测到的 desktop-dist 路径写回 desktop_path.txt，下次直接用。"""
    try:
        with open(os.path.join(SCRIPT_DIR, 'desktop_path.txt'), 'w', encoding='utf-8') as f:
            f.write(path)
    except Exception:
        pass


def _dist_from_running_process():
    """列出正在运行的 Kimi Code.exe，返回其 resources/desktop-dist 候选路径。"""
    out = []
    try:
        q = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-Process -Name 'Kimi Code' -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Path"],
            capture_output=True, text=True, timeout=10)
        for line in (q.stdout or '').splitlines():
            p = line.strip().strip('"')
            if p:
                out.append(os.path.join(os.path.dirname(p), 'resources', 'desktop-dist'))
    except Exception:
        pass
    return out


def get_dist_dir():
    cfg = os.path.join(SCRIPT_DIR, 'desktop_path.txt')
    try:
        if os.path.exists(cfg):
            p = open(cfg, encoding='utf-8').read().strip().strip('"').strip("'")
            if os.path.isdir(p):
                return p
    except Exception:
        pass
    candidates = [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Kimi Code', 'resources', 'desktop-dist'),
        r'C:\Program Files\Kimi Code\resources\desktop-dist',
        r'C:\Program Files (x86)\Kimi Code\resources\desktop-dist',
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    # 进程兜底：非默认盘/便携安装时按正在运行的 Kimi Code.exe 位置推导
    for c in _dist_from_running_process():
        if os.path.isdir(c):
            _remember_dist(c)
            return c
    return ''


DIST_DIR = ''
DIST_ASSETS = ''
INDEX_HTML = ''


def refresh_dist_paths():
    global DIST_DIR, DIST_ASSETS, INDEX_HTML
    DIST_DIR = get_dist_dir()
    DIST_ASSETS = os.path.join(DIST_DIR, 'assets') if DIST_DIR else ''
    INDEX_HTML = os.path.join(DIST_DIR, 'index.html') if DIST_DIR else ''


refresh_dist_paths()

# ---------------- 旧套件迁移 ----------------
LEGACY_DIR = os.path.join(KIMI_HOME, 'model-manager')
LEGACY_TAG = os.path.join(KIMI_HOME, 'usage-dashboard', 'legacy-migrated.flag')


def _kill_legacy_processes():
    """无条件清掉旧 model-manager 的 supervisor/server（幂等，flag 后每次也跑，防 supervisor 复活）。"""
    try:
        subprocess.run([
            'powershell', '-NoProfile', '-Command',
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'model-manager.*(supervisor|server)\\.py' }"
            " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        ], capture_output=True, timeout=20)
    except Exception:
        pass


def kill_port_owner():
    """端口被占且不是我们的服务时，按 PID 杀掉占用者。
    安全阀：占用者进程命令行含本插件 service.py 时绝不杀（防并发 tick 误杀刚拉起的 daemon）。"""
    if port_is_ours():
        return
    try:
        out = subprocess.run(
            ['netstat', '-ano', '-p', 'tcp'],
            capture_output=True, text=True, timeout=15).stdout or ''
        pids = []
        for line in out.splitlines():
            parts = line.split()
            if (len(parts) >= 5 and parts[1].endswith(':%d' % PORT)
                    and parts[3].upper().startswith('LISTEN')):
                pids.append(parts[4])
        for pid in set(pids):
            try:
                q = subprocess.run([
                    'powershell', '-NoProfile', '-Command',
                    "(Get-CimInstance Win32_Process -Filter 'ProcessId=%s').CommandLine" % pid
                ], capture_output=True, text=True, timeout=15)
                cl = (q.stdout or '').strip()
                if 'service.py' in cl and ('kimi-code-usage' in cl or 'kimi-code' in cl):
                    log('skip kill pid %s: looks like our own service (%s)' % (pid, cl[:120]))
                    continue
                subprocess.run(['taskkill', '/PID', pid, '/F'],
                               capture_output=True, timeout=10)
                log('killed foreign listener on %d (pid %s)' % (PORT, pid))
            except Exception:
                pass
    except Exception as e:
        log('kill_port_owner error: %r' % e)


def spawn_lock():
    """拉起 daemon 前互相让行：lock 文件新于 12s 视为有进程正在拉起，直接返回 False。"""
    lock = os.path.join(KIMI_HOME, 'usage-dashboard', 'spawn.lock')
    try:
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        try:
            st = os.stat(lock)
            if time.time() - st.st_mtime < 12:
                return False
        except OSError:
            pass
        with open(lock, 'w') as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True


def migrate_legacy():
    """接管 39281：停掉旧 model-manager 守护/服务、摘除其开机自启与 index.html 旧注入。
    flag 只标记"一次性清理"做过；杀旧进程的动作每次都会跑（supervisor 会复活 server）。"""
    _kill_legacy_processes()
    try:
        if os.path.exists(LEGACY_TAG):
            return
        if not os.path.exists(LEGACY_DIR):
            os.makedirs(os.path.dirname(LEGACY_TAG), exist_ok=True)
            with open(LEGACY_TAG, 'w') as f:
                f.write(datetime.now().isoformat())
            return
        # 摘自启：Startup 目录 + Run 键
        startup = os.path.join(os.environ.get('APPDATA', ''),
                               'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Startup')
        for f in ('KimiModelManager.vbs',):
            p = os.path.join(startup, f)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        subprocess.run(['reg', 'delete', r'HKCU\Software\Microsoft\Windows\CurrentVersion\Run',
                        '/v', 'KimiCodePlugin', '/f'], capture_output=True, timeout=10)
        # 摘除 index.html 中的旧注入标签
        if INDEX_HTML and os.path.exists(INDEX_HTML):
            strip_legacy_injections()
        os.makedirs(os.path.dirname(LEGACY_TAG), exist_ok=True)
        with open(LEGACY_TAG, 'w') as f:
            f.write(datetime.now().isoformat())
        log('legacy model-manager migrated')
    except Exception as e:
        log('migrate_legacy error: %r' % e)


# 注入标签统一由 ensure_injection/_INJECT_RE 管理（widget 带 ?v=mtime 版本号）
_INJECT_RE = re.compile(r'\s*<script src="/assets/kimi-(embedded|usage)-(data|widget)\.js[^"]*"></script>\s*')
# 旧套件残留的根路径注入（/kimi-usage-widget.js 等非 /assets/ 前缀）——会与新版卡片
# 争用同一 DOM 容器且不渲染模型/会话行，必须一并清掉，不能只靠 _INJECT_RE
_LEGACY_ROOT_INJECT_RE = re.compile(r'\s*<script src="/kimi-[a-z-]*(?:data|widget)\.js[^"]*"></script>\s*')
# 无 Python 时注入的"需要 Python"占位卡片——服务起来后要摘掉，否则装了 Python
# 仍残留一行提示
_NEEDPY_INJECT_RE = re.compile(r'\s*<script src="/assets/kimi-usage-needpy\.js[^"]*"></script>\s*')


def strip_legacy_injections():
    try:
        html = io.open(INDEX_HTML, encoding='utf-8').read()
        new_html = _NEEDPY_INJECT_RE.sub('\n',
                  _LEGACY_ROOT_INJECT_RE.sub('\n', _INJECT_RE.sub('\n', html)))
        if new_html != html:
            io.open(INDEX_HTML, 'w', encoding='utf-8').write(new_html)
    except Exception:
        pass


def _widget_tag():
    """带 mtime 版本号的注入标签——浏览器/Electron 会缓存无参数 script URL，
    不加版本号时 widget 更新后用户仍看到旧 UI（如缺价格按钮）。"""
    try:
        v = int(os.path.getmtime(WIDGET_SRC))
    except OSError:
        v = 0
    return '<script src="/assets/kimi-usage-widget.js?v=%d"></script>' % v


def ensure_injection():
    """自愈：index.html 缺注入标签就补；清掉旧注入与带 ?v= 的重复标签。"""
    if not INDEX_HTML or not os.path.exists(INDEX_HTML):
        return
    try:
        html = io.open(INDEX_HTML, encoding='utf-8').read()
        new_html = _NEEDPY_INJECT_RE.sub('\n',
                  _LEGACY_ROOT_INJECT_RE.sub('\n', _INJECT_RE.sub('\n', html)))
        data_tag = '<script src="/assets/kimi-usage-data.js"></script>'
        widget_tag = _widget_tag()
        block = '  %s\n  %s\n' % (data_tag, widget_tag)
        if '</body>' in new_html:
            new_html = new_html.replace('</body>', block + '  </body>')
        else:
            new_html += '\n' + block
        if new_html != html:
            io.open(INDEX_HTML, 'w', encoding='utf-8').write(new_html)
    except Exception:
        pass


def sync_widget_asset():
    """把插件内的侧栏卡片 JS 同步到 desktop-dist/assets（自愈看护）。"""
    if not DIST_ASSETS or not os.path.exists(WIDGET_SRC):
        return
    dest = os.path.join(DIST_ASSETS, 'kimi-usage-widget.js')
    try:
        if (not os.path.exists(dest)
                or os.path.getsize(dest) != os.path.getsize(WIDGET_SRC)
                or _file_digest(dest) != _file_digest(WIDGET_SRC)):
            os.makedirs(DIST_ASSETS, exist_ok=True)
            shutil.copy2(WIDGET_SRC, dest)
    except Exception:
        pass


def _file_digest(path):
    try:
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()[:12]
    except Exception:
        return ''


def widget_digest():
    return _file_digest(WIDGET_SRC)


# ---------------- 数据产出 ----------------
def _fmt_tokens(n):
    n = int(n)
    if n >= 1e9:
        return '%.1fB' % (n / 1e9)
    if n >= 1e6:
        return '%.1fM' % (n / 1e6)
    if n >= 1e3:
        return '%.1fK' % (n / 1e3)
    return str(n)


def _fmt_cost(c):
    if c >= 1000:
        return '¥%.1fk' % (c / 1000.0)
    if c >= 100:
        return '¥%.0f' % c
    if c >= 1:
        return '¥%.1f' % c
    return '¥%.2f' % c


def _bucket_json(b, date='', cost=None):
    tokens = int(b.get('tokens', 0))
    cost = round(cost if cost is not None else b.get('cost', 0.0), 1)
    return {
        'tokens': tokens, 'tokens_fmt': _fmt_tokens(tokens),
        'cost': cost, 'cost_fmt': _fmt_cost(cost),
        'calls': int(b.get('records', 0)),
        'in': int(b.get('input', 0)), 'in_fmt': _fmt_tokens(b.get('input', 0)),
        'out': int(b.get('output', 0)), 'out_fmt': _fmt_tokens(b.get('output', 0)),
        'cache_pct': round(_hit(b) * 100, 1),
        'date': date,
    }


def _hit(b):
    inp = b.get('input', 0)
    return (b.get('cache_read', 0) / inp) if inp else 0.0


def _range_sum(daily, start_key, end_key):
    b = {'tokens': 0, 'cost': 0.0, 'input': 0, 'output': 0,
         'cache_read': 0, 'cache_create': 0, 'records': 0}
    for k, cell in daily.items():
        if start_key <= k <= end_key:
            for f in b:
                b[f] += cell.get(f, 0)
    return b


def _models_in_range(daily_models, start_key, end_key):
    """某日期区间内的按模型聚合桶 {model: bucket}。"""
    acc = {}
    for k, mdict in daily_models.items():
        if not (start_key <= k <= end_key):
            continue
        for m, b in mdict.items():
            a = acc.setdefault(m, {'tokens': 0, 'cost': 0.0, 'input': 0, 'output': 0,
                                   'cache_read': 0, 'cache_create': 0, 'records': 0,
                                   'cache_reported': False})
            for f in a:
                if f == 'cache_reported':
                    a[f] = a[f] or bool(b.get(f))
                else:
                    a[f] += b.get(f, 0)
    return acc


def _buckets_cost(mdict):
    """按当前计价重算一组模型桶的总成本（改价即时生效）。"""
    try:
        return sum(scanner.priced_cost(m, b) for m, b in mdict.items())
    except Exception:
        return sum(b.get('cost', 0.0) for b in mdict.values())


def _model_rows(rows):
    out = []
    for r in rows:
        try:
            cost = scanner.priced_cost(r['model'], r)
        except Exception:
            cost = r.get('cost', 0.0)
        out.append({
            'model': r['model'], 'tokens': r['tokens'], 'tokens_fmt': _fmt_tokens(r['tokens']),
            'calls': r.get('calls', r.get('records', 0)),
            'cache_pct': round(r.get('hit', 0.0) * 100, 1),
            'cache_reported': r.get('cache_reported', True),
            'cost': round(cost, 1), 'cost_fmt': _fmt_cost(cost),
            'input': int(r.get('input', 0)), 'output': int(r.get('output', 0)),
            'in_fmt': _fmt_tokens(r.get('input', 0)), 'out_fmt': _fmt_tokens(r.get('output', 0)),
        })
    return out


def _model_rows_from_buckets(mdict):
    rows = [dict(b, model=m, hit=_hit(b)) for m, b in mdict.items()]
    rows.sort(key=lambda r: -r['tokens'])
    return _model_rows(rows)


def _session_rows(rows):
    out = []
    for r in rows[:8]:
        key = r['key']
        short = key.replace('session_', '')[:8] or key[:8]
        out.append({
            'key': key, 'short': short,
            'tokens': r['tokens'], 'tokens_fmt': _fmt_tokens(r['tokens']),
            'calls': r.get('records', 0), 'cache_pct': round(r['hit'] * 100, 1),
            'cache_reported': r.get('cache_reported', True),
            'cost': round(r['cost'], 1), 'cost_fmt': _fmt_cost(r['cost']),
            'last': r.get('last', 0),
        })
    return out


def _quota_json(official):
    """官方额度 + 配速（已用% ÷ 窗口已过%）。"""
    if not official:
        return None
    now = time.time()
    out = {}

    def fill(name, used, limit, reset):
        if not limit:
            return
        frac_used = used / limit
        frac_elapsed = None
        eta = ''
        try:
            rt = datetime.strptime(reset[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S')
            import time as _t
            reset_ts = _t.mktime(rt.timetuple())
            span = 7 * 86400 if name == 'week' else 300
            start_ts = reset_ts - span
            if start_ts < now < reset_ts:
                frac_elapsed = (now - start_ts) / span
                if frac_used > 0 and frac_elapsed and frac_used / frac_elapsed > 0:
                    eta_ts = now + (1 - frac_used) / (frac_used / (now - start_ts))
                    eta = datetime.fromtimestamp(eta_ts).strftime('%m-%d %H:%M')
        except Exception:
            frac_elapsed = None
        pace = (frac_used / frac_elapsed) if frac_elapsed else None
        out[name] = {
            'used': used, 'limit': limit,
            'pct': round(frac_used * 100, 1),
            'reset': reset,
            'pace': round(pace, 2) if pace is not None else None,
            'eta': eta,
        }

    fill('week', official.get('wk_used'), official.get('wk_limit'), official.get('wk_reset', ''))
    fill('h5', official.get('h5_used'), official.get('h5_limit'), official.get('h5_reset', ''))
    out['ts'] = official.get('ts')
    return out or None


def build_dashboard(snap, official):
    """kimi-usage.json 全量内容（旧 schema 超集）。"""
    now = datetime.now()
    today_key = now.strftime('%Y-%m-%d')
    yday_key = (datetime.fromtimestamp(now.timestamp() - 86400)).strftime('%Y-%m-%d')
    daily = snap['daily'] or {}
    daily_models = snap.get('daily_models') or {}

    def row_bag(rows):
        return {r['model']: r for r in rows}

    # 各周期成本按当前计价从分模型桶重算（改价即时生效）；无分模型数据退回桶内 cost
    ydm = daily_models.get(yday_key) or {}
    today = _bucket_json(snap['today'], today_key,
                         cost=_buckets_cost(row_bag(snap['models_today'])))
    yesterday = _bucket_json(daily.get(yday_key) or {}, yday_key,
                             cost=_buckets_cost(ydm))

    dow = now.isoweekday()          # 1=周一
    wk_start = (datetime.fromtimestamp(now.timestamp() - (dow - 1) * 86400)).strftime('%Y-%m-%d')
    wm = _models_in_range(daily_models, wk_start, today_key)
    week = _bucket_json(_range_sum(daily, wk_start, today_key), wk_start,
                        cost=_buckets_cost(wm))
    mo_start = now.strftime('%Y-%m-01')
    mm = _models_in_range(daily_models, mo_start, today_key)
    month = _bucket_json(_range_sum(daily, mo_start, today_key), mo_start,
                         cost=_buckets_cost(mm))
    cumul = _bucket_json(snap['all'],
                         cost=_buckets_cost(row_bag(snap['models_all'])))

    today['models'] = _model_rows(snap['models_today'])
    yesterday['models'] = _model_rows_from_buckets(ydm)
    week['models'] = _model_rows_from_buckets(wm)
    month['models'] = _model_rows_from_buckets(mm)

    burn = snap['burn']
    cur = snap['cur']
    sess = snap['session'] or {}
    quota = _quota_json(official)

    days30 = []
    for i in range(29, -1, -1):
        d = datetime.fromtimestamp(now.timestamp() - i * 86400)
        k = d.strftime('%Y-%m-%d')
        cell = daily.get(k) or {}
        tk = int(cell.get('tokens', 0))
        dmk = daily_models.get(k)
        dcost = _buckets_cost(dmk) if dmk is not None else cell.get('cost', 0.0)
        days30.append({
            'label': d.strftime('%m-%d'), 'date': k, 'tokens': tk,
            'tokens_fmt': _fmt_tokens(tk), 'calls': int(cell.get('records', 0)),
            'cost': round(dcost, 1),
        })

    hourly_rows = []
    for h, (tok, calls) in enumerate(snap['hourly_today']):
        hourly_rows.append({'hour': h, 'tokens': int(tok), 'calls': int(calls),
                            'tokens_fmt': _fmt_tokens(tok)})

    # 时间序列点：今日按小时、本周按日、本月按日
    ts_today = [{'label': '%02d' % r['hour'], 'tokens': r['tokens'], 'calls': r['calls']}
                for r in hourly_rows]
    ts_week = [{'label': d['label'], 'tokens': d['tokens'], 'calls': d['calls']} for d in days30[-7:]]
    ts_month = [{'label': d['label'], 'tokens': d['tokens'], 'calls': d['calls']} for d in days30]

    header_tokens = today['tokens_fmt']
    return {
        'updated_at': now.strftime('%Y-%m-%d %H:%M:%S'),
        'header': {
            'tokens_fmt': header_tokens, 'cost_fmt': today['cost_fmt'],
            'cache_pct': '%.0f%%' % today['cache_pct'],
            'speed_tps': '%d t/s' % round(snap['tps']),
        },
        'today': today, 'yesterday': yesterday, 'week': week, 'month': month,
        'cumul': cumul,
        'rate': {
            'tokens_per_hour': _fmt_tokens(burn['tokens']) + '/h',
            'cost_per_hour': _fmt_cost(burn['cost']) + '/h',
            'window': '近60分钟',
        },
        'cache': {'pct': cumul['cache_pct'], 'pct_fmt': '%.0f%%' % cumul['cache_pct']},
        'speed': {'tps': '%d t/s' % round(snap['tps']), 'model': snap['tps_model'] or '--',
                  'avg_tps': '%d t/s' % round(snap['avg_tps'])},
        'cur': {
            'alias': cur.get('alias') or cur.get('model') or '--',
            'model': cur.get('model') or '',
            'effort': cur.get('effort') or '',
            'session': cur.get('session') or '',
            'sub_agent': cur.get('sub_agent') or '',
        },
        'session': ({
            'key': sess.get('key', ''), 'tokens_fmt': _fmt_tokens(sess.get('tokens', 0)),
            'calls': sess.get('records', 0), 'cache_pct': round(sess.get('hit', 0) * 100, 1),
            'cache_reported': sess.get('cache_reported', True),
            'cost_fmt': _fmt_cost(sess.get('cost', 0.0)),
        } if sess else None),
        'quota': quota,
        'days30': days30,
        'hourly': hourly_rows,
        'timeseries': {
            'today': {'points': ts_today},
            'week': {'points': ts_week},
            'month': {'points': ts_month},
        },
        'today_models': _model_rows(snap['models_today']),
        'cumul_models': _model_rows(snap['models_all']),
        'sessions': _session_rows(snap['sessions']),
        'footer': {'file_count': snap['files'], 'session_count': len(snap['sessions']),
                   'time': now.strftime('%H:%M:%S')},
    }


def safe_write(path, text):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
        for _ in range(2):
            try:
                os.replace(tmp, path)
                break
            except OSError:
                time.sleep(0.05)
    except Exception:
        pass


SCANNER = None
LATEST_OFFICIAL = None
LATEST_DATA = {}
STATE_DIRTY = {'v': False}


def collect_once():
    """跑一次完整采集+落盘（供 --once / tick / 常驻循环共用）。"""
    global LATEST_DATA
    refresh_dist_paths()
    mdata = get_models_data()
    # 别名→裸 model id 映射喂给 scanner，让按别名存的价格能对上 wire 日志里的 model id
    amap = {}
    for mi in mdata.get('models', []):
        mid = mi.get('model') or ''
        if mid:
            amap.setdefault(mid, []).append(mi.get('alias') or '')
    try:
        scanner.MODEL_ALIASES = amap
    except Exception:
        pass
    if SCANNER is None:
        return
    SCANNER.scan_once()
    snap = SCANNER.snapshot()
    dash = build_dashboard(snap, LATEST_OFFICIAL)
    LATEST_DATA = dash

    if DIST_DIR:
        safe_write(os.path.join(DIST_DIR, 'kimi-usage.json'),
                   json.dumps(dash, ensure_ascii=False))
    # 插件内保留一份副本（Skill/命令可读）
    try:
        safe_write(os.path.join(PLUGIN_ROOT, 'assets', 'kimi-usage.json'),
                   json.dumps(dash, ensure_ascii=False))
    except Exception:
        pass

    if DIST_ASSETS:
        js = ('// Kimi Code Auto-generated Data Cache\n'
              'window.__KIMI_DATA__ = %s;\n'
              'if (typeof window.__KMM_ON_DATA_UPDATE__ === "function") {'
              ' try { window.__KMM_ON_DATA_UPDATE__(window.__KIMI_DATA__); } catch(e) {} }\n'
              % json.dumps({'usage': dash, 'models': mdata,
                            'service': service_alive_flag(),
                            'time': int(time.time() * 1000)}, ensure_ascii=False))
        # 侧栏卡片轮询 embedded-data.js；usage-data.js 为 index.html 注入点，两份同源
        safe_write(os.path.join(DIST_ASSETS, 'kimi-usage-data.js'), js)
        safe_write(os.path.join(DIST_ASSETS, 'kimi-embedded-data.js'), js)

    STATE_DIRTY['v'] = True
    return dash


def service_alive_flag():
    return {'port': PORT, 'pid': os.getpid(),
            'api': 'http://127.0.0.1:%d/api/status' % PORT}


# ---------------- config.toml 读写（模型管理） ----------------
def _load_toml(content):
    try:
        import tomllib  # py3.11+
        return tomllib.loads(content)
    except Exception:
        pass
    try:
        import toml
        return toml.loads(content)
    except Exception:
        return _mini_toml(content)


def _mini_toml(content):
    """极简 TOML 兜底解析：只够 [models.*]/[providers.*] 读字段。"""
    data = {}
    section = []
    for raw in content.split('\n'):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            name = line.strip('[] ')
            parts = [p.strip().strip('"') for p in re.split(r'\.(?=(?:[^"]*"[^"]*")*[^"]*$)', name)]
            section = parts
            d = data
            for p in parts:
                d = d.setdefault(p, {})
            continue
        if '=' in line:
            k, _, v = line.partition('=')
            k = k.strip().strip('"')
            v = v.strip()
            d = data
            for p in section:
                d = d.setdefault(p, {})
            d[k] = _mini_val(v)
    return data


def _mini_val(v):
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    if v.startswith('['):
        inner = v.strip('[] ')
        return [x.strip().strip('"') for x in inner.split(',') if x.strip()]
    if v in ('true', 'false'):
        return v == 'true'
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def get_config_content():
    if not os.path.exists(CONFIG_PATH):
        return ''
    with open(CONFIG_PATH, 'rb') as f:
        return f.read().decode('utf-8', errors='replace')


EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')
EFFORT_KEYS = ('support_efforts', 'default_effort')


def _is_managed(model, provider):
    """托管/目录导入的模型：官方刷新可能改写其顶层 support_efforts / default_effort。"""
    name = str(model.get('provider') or '')
    return name.startswith('managed:') or bool((provider or {}).get('oauth'))


def _effective(m, key):
    """[models.x.overrides] 优先于顶层字段（官方文档：运行时读取有效值）。"""
    ov = m.get('overrides') or {}
    return ov[key] if key in ov else m.get(key)


def analyze_model_effort(alias, m, provider, thinking_cfg):
    """按官方文档规则审计单个模型的思考强度配置，返回 (有效档位, 来源, 有效默认档, 实际生效档, 问题列表)。"""
    ptype = (provider or {}).get('type', '')
    caps = list(_effective(m, 'capabilities') or [])
    sup = list(_effective(m, 'support_efforts') or [])
    dflt = _effective(m, 'default_effort')
    adaptive = bool(_effective(m, 'adaptive_thinking'))
    has_think = 'thinking' in caps or 'always_thinking' in caps
    g_on = thinking_cfg.get('enabled', True) is not False
    g_eff = thinking_cfg.get('effort')
    issues = []

    def add(level, msg, code=None, fix=None, fix_value=None):
        it = {'level': level, 'msg': msg}
        if code:
            it['code'] = code
        if fix:
            it['fix'] = fix
        if fix_value is not None:
            it['fix_value'] = fix_value
        issues.append(it)

    bad = [x for x in sup if x not in EFFORT_LEVELS]
    if bad:
        add('error', 'support_efforts 含非法档位 %s（合法值：%s）' % (bad, '/'.join(EFFORT_LEVELS)))
    if dflt is not None and dflt not in EFFORT_LEVELS:
        add('error', 'default_effort="%s" 不是合法档位（合法值：%s）' % (dflt, '/'.join(EFFORT_LEVELS)))
    if sup and dflt and dflt not in sup:
        _fx = 'high' if 'high' in sup else sup[-1]
        add('error', 'default_effort="%s" 不在 support_efforts %s 内，官方会回退或直接报错' % (dflt, sup),
            'default_not_in_support', '默认档改为 %s' % _fx, _fx)
    if not has_think and (sup or dflt):
        add('warn', '配置了思考档位，但没有 thinking 能力标签，档位不会生效',
            'no_thinking_tag', '开启深度思考')
    if has_think and not sup:
        add('warn', '未声明 support_efforts，面板按官方五档兜底展示；实际是否支持取决于上游',
            'no_support_efforts', '写入五档', list(EFFORT_LEVELS))
    if adaptive and ptype and ptype != 'anthropic':
        add('info', 'adaptive_thinking 仅对 anthropic 类型 provider 生效，当前为 %s，该字段会被忽略' % ptype)

    efforts = sup if sup else (list(EFFORT_LEVELS) if has_think or adaptive else [])
    source = 'config' if sup else 'fallback'

    if not g_on:
        actual = 'off'
        add('info', '全局 [thinking].enabled=false，所有模型强制关闭思考')
    elif g_eff and (not sup or g_eff in sup):
        actual = g_eff
        if dflt and dflt != g_eff:
            add('info', '主 Agent 实际使用全局 [thinking].effort="%s"，会覆盖该模型的 default_effort="%s"（子代理才用模型默认值；会话内手动切换除外）' % (g_eff, dflt))
    else:
        actual = dflt or (sup[len(sup) // 2] if sup else None)
        if g_eff and sup and g_eff not in sup:
            add('info', '全局 [thinking].effort="%s" 不在该模型支持列表内，回退到模型默认档' % g_eff)
    return efforts, source, dflt, actual, issues


def _fix_base_url(base):
    """常见协议拼写错误（htpps:// 等）→ 合法 URL；无法判断则返回 None。"""
    m = re.match(r'^\s*([A-Za-z]+):?/*(.+)$', str(base))
    if not m:
        return None
    scheme = m.group(1).lower()
    if not scheme.startswith('ht'):
        return None
    return '%s://%s' % ('https' if scheme.endswith('s') else 'http', m.group(2).strip())


def update_provider_field_in_text(content, provider, key, value):
    pattern = r'(\[providers\.(?:"%s"|%s)\])(.*?)(?=\n\[|\Z)' % (re.escape(provider), re.escape(provider))
    m = re.search(pattern, content, re.DOTALL)
    if not m:
        return content, False
    body = m.group(2)
    line = _render_toml_kv(key, value)
    if re.search(r'^[ \t]*%s[ \t]*=' % re.escape(key), body, re.MULTILINE):
        body = re.sub(r'^[ \t]*%s[ \t]*=.*$' % re.escape(key), lambda _m: line, body, count=1, flags=re.MULTILINE)
    else:
        body = body.rstrip('\n') + '\n' + line + '\n'
    return content[:m.start()] + m.group(1) + body + content[m.end():], True


def get_models_data():
    content = get_config_content()
    data = _load_toml(content) or {}
    default_model = data.get('default_model', '')
    providers = data.get('providers', {}) or {}
    thinking_cfg = data.get('thinking', {}) or {}
    pricing = load_pricing()
    dismissed = load_dismissed()
    dismissed_n = 0
    models = []
    audit = {'error': 0, 'warn': 0, 'info': 0}
    for alias, m in (data.get('models', {}) or {}).items():
        prov = m.get('provider', '')
        prov_cfg = providers.get(prov, {}) or {}
        caps = _effective(m, 'capabilities') or []
        adaptive = bool(_effective(m, 'adaptive_thinking'))
        efforts, source, dflt, actual, issues = analyze_model_effort(alias, m, prov_cfg, thinking_cfg)
        base = str(prov_cfg.get('base_url') or '')
        if base and not re.match(r'^https?://', base, re.I):
            fixed = _fix_base_url(base)
            bad = {'level': 'error', 'code': 'bad_base_url',
                   'msg': 'provider「%s」的 base_url 协议非法：%s' % (prov, base)}
            if fixed:
                bad['fix'] = '改为 %s' % fixed
                bad['fix_value'] = fixed
            issues.insert(0, bad)
        for it in issues:
            if it['level'] != 'info' and it.get('code') and '%s|%s' % (alias, it['code']) in dismissed:
                it['dismissed'] = True
                dismissed_n += 1
                continue
            audit[it['level']] += 1
        models.append({
            'alias': alias,
            'provider': prov,
            'provider_type': prov_cfg.get('type', ''),
            'model': m.get('model', ''),
            'display_name': _effective(m, 'display_name') or alias,
            'max_context_size': _effective(m, 'max_context_size') or 250000,
            'capabilities': caps,
            'has_image': 'image_in' in caps,
            'has_thinking': 'thinking' in caps or 'always_thinking' in caps,
            'always_thinking': 'always_thinking' in caps,
            'adaptive_thinking': adaptive,
            'has_tools': 'tool_use' in caps,
            'support_efforts': _effective(m, 'support_efforts') or [],
            'effective_efforts': efforts,
            'efforts_source': source,
            'default_effort': dflt or thinking_cfg.get('effort', 'high'),
            'effective_effort': actual,
            'effort_issues': issues,
            'is_default': alias == default_model,
            'pricing': pricing.get(alias) or pricing.get(m.get('model', '')) or None,
        })
    models.sort(key=lambda m: (not m['is_default'], m['alias']))
    return {'default_model': default_model,
            'providers': list(providers.keys()),
            'thinking': thinking_cfg,
            'effort_audit': audit,
            'dismissed_count': dismissed_n,
            'models': models}


def safe_apply_config(new_content):
    """写 config-new.toml → kimi doctor 校验 → 时间戳备份 → 原子替换。"""
    with open(CONFIG_NEW_PATH, 'wb') as f:
        f.write(new_content.encode('utf-8'))
    kimi = shutil.which('kimi') or shutil.which('kimi.cmd') or 'kimi'
    try:
        res = subprocess.run([kimi, 'doctor', 'config', CONFIG_NEW_PATH],
                             capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            try:
                os.remove(CONFIG_NEW_PATH)
            except OSError:
                pass
            return False, '校验失败：\n%s\n%s' % (res.stdout, res.stderr)
    except FileNotFoundError:
        # kimi CLI 不在 PATH：跳过校验但保留备份
        pass
    except Exception as e:
        try:
            os.remove(CONFIG_NEW_PATH)
        except OSError:
            pass
        return False, '校验异常：%r' % e

    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    try:
        if os.path.exists(CONFIG_PATH):
            shutil.copy2(CONFIG_PATH, CONFIG_PATH + '.' + ts + '.bak')
        shutil.move(CONFIG_NEW_PATH, CONFIG_PATH)
    except Exception as e:
        return False, '替换失败：%r' % e
    collect_once()
    return True, '配置已校验并应用，会话内 /reload 生效'


def _render_toml_kv(k, v):
    if isinstance(v, bool):
        return '%s = %s' % (k, str(v).lower())
    if isinstance(v, int):
        return '%s = %d' % (k, v)
    if isinstance(v, list):
        return '%s = [ %s ]' % (k, ', '.join('"%s"' % x for x in v))
    return '%s = "%s"' % (k, v)


def update_model_in_text(content, alias, updates, sub=''):
    """改写 [models.<alias>] 的字段；sub='overrides' 时改写 [models.<alias>.overrides]（不存在则新建）。"""
    escaped = re.escape(alias)
    suffix = r'\.' + re.escape(sub) if sub else ''
    pattern = r'(\[models\.(?:"%s"|%s)%s\])(.*?)(?=\n\[|\Z)' % (escaped, escaped, suffix)
    m = re.search(pattern, content, re.DOTALL)
    if not m and sub:
        main = re.search(r'(\[models\.(?:"%s"|%s)\])(.*?)(?=\n\[|\Z)' % (escaped, escaped), content, re.DOTALL)
        if not main:
            return content, False
        kvs = ''.join(_render_toml_kv(k, v) + '\n' for k, v in updates.items())
        block = '[models."%s".%s]\n%s' % (alias, sub, kvs)
        return content[:main.end()].rstrip('\n') + '\n\n' + block + content[main.end():], True
    if not m:
        return content, False
    header, body = m.group(1), m.group(2)
    lines = body.split('\n')
    new_lines = []
    handled = set()
    render = _render_toml_kv

    for line in lines:
        s = line.strip()
        if '=' in s and not s.startswith('#'):
            k = s.split('=')[0].strip()
            if k in updates:
                new_lines.append(render(k, updates[k]))
                handled.add(k)
                continue
        new_lines.append(line)
    tail = []
    while new_lines and not new_lines[-1].strip():
        tail.append(new_lines.pop())
    for k, v in updates.items():
        if k not in handled:
            new_lines.append(render(k, v))
    new_lines.extend(tail)
    new_content = content[:m.start()] + header + '\n'.join(new_lines) + content[m.end():]
    return new_content, True


def update_model_effort_in_text(content, alias, updates):
    """思考档位写入：校验合法性与 default ∈ support；托管模型写 overrides 固定。返回 (新内容, 错误信息)。"""
    data = _load_toml(content) or {}
    m = (data.get('models', {}) or {}).get(alias)
    if m is None:
        return content, '模型 %s 不存在' % alias
    sup_new = updates.get('support_efforts')
    dflt_new = updates.get('default_effort')
    if sup_new is not None and (not isinstance(sup_new, list) or any(x not in EFFORT_LEVELS for x in sup_new)):
        return content, 'support_efforts 只能包含：%s' % '/'.join(EFFORT_LEVELS)
    if dflt_new is not None and dflt_new not in EFFORT_LEVELS:
        return content, 'default_effort 只能是：%s' % '/'.join(EFFORT_LEVELS)
    sup = sup_new if sup_new is not None else (_effective(m, 'support_efforts') or [])
    dflt = dflt_new if dflt_new is not None else _effective(m, 'default_effort')
    if sup and dflt and dflt not in sup:
        return content, 'default_effort "%s" 不在 support_efforts %s 内' % (dflt, sup)
    provider = (data.get('providers', {}) or {}).get(m.get('provider'), {}) or {}
    rest = {k: v for k, v in updates.items() if k not in EFFORT_KEYS}
    eff = {k: v for k, v in updates.items() if k in EFFORT_KEYS}
    new = content
    if rest:
        new, _ = update_model_in_text(new, alias, rest)
    if eff:
        new, _ = update_model_in_text(new, alias, eff, sub='overrides' if _is_managed(m, provider) else '')
    return new, None


def apply_issue_fix(content, model, issue):
    """对单个 (模型, 问题) 套用修复，返回 (新内容, 错误信息)。"""
    code, alias = issue.get('code'), model['alias']
    if code == 'bad_base_url':
        if not issue.get('fix_value'):
            return content, '无法自动判断正确地址，请手动修改 config.toml 中 provider「%s」的 base_url' % model['provider']
        new, ok = update_provider_field_in_text(content, model['provider'], 'base_url', issue['fix_value'])
        return (new, None) if ok else (content, '未找到 provider「%s」' % model['provider'])
    if code == 'no_thinking_tag':
        caps = list(model['capabilities'])
        if 'thinking' not in caps:
            caps.append('thinking')
        new, ok = update_model_in_text(content, alias, {'capabilities': caps})
        return (new, None) if ok else (content, '模型 %s 不存在' % alias)
    if code == 'no_support_efforts':
        return update_model_effort_in_text(content, alias, {'support_efforts': list(EFFORT_LEVELS)})
    if code == 'default_not_in_support':
        return update_model_effort_in_text(content, alias, {'default_effort': issue['fix_value']})
    return content, '该问题没有自动修复方案'


def _global_effort_note(alias, level):
    """全局 [thinking].effort 会覆盖主 Agent 的模型默认档，保存后提示实际生效值。"""
    try:
        m = next((x for x in get_models_data()['models'] if x['alias'] == alias), None)
        if m and m.get('effective_effort') and m['effective_effort'] != level:
            return '注意：主 Agent 实际使用 %s（被全局 [thinking].effort 覆盖），%s 仅对子代理等场景生效' % (m['effective_effort'], level)
    except Exception:
        pass
    return ''


def set_default_model_in_text(content, alias):
    if re.search(r'^default_model\s*=', content, re.MULTILINE):
        return re.sub(r'^default_model\s*=.*$',
                      'default_model = "%s"' % alias, content, flags=re.MULTILINE)
    return 'default_model = "%s"\n' % alias + content


def auto_enable_all_in_text(content):
    data = _load_toml(content) or {}
    cur = content
    for alias, m in (data.get('models', {}) or {}).items():
        caps = list(m.get('capabilities', []) or [])
        changed = False
        for cap in ('tool_use', 'thinking', 'image_in'):
            if cap not in caps:
                caps.append(cap)
                changed = True
        if changed:
            cur, _ = update_model_in_text(cur, alias, {'capabilities': caps})
    return cur


# ---------------- 自更新 ----------------
_UPDATE_CACHE = {'t': 0.0, 'data': None}


def _semver(v):
    parts = []
    for x in re.split(r'[^\d]+', str(v or '')):
        if x.isdigit():
            parts.append(int(x))
    return tuple(parts or [0])


def parse_changelog(text, version):
    """取出 CHANGELOG.md 中 `## v<version> · 日期` 一节，返回 (日期, 条目列表)。"""
    date, items, on = '', [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r'^##\s+v?([\d.]+)\s*(?:[·\-|]\s*(\S+))?', line)
        if m:
            if on:
                break
            on = _semver(m.group(1)) == _semver(version)
            if on:
                date = m.group(2) or ''
            continue
        if not on:
            continue
        if line.startswith('### '):
            items.append({'t': 'h', 'text': line[4:].strip()})
        elif line.startswith('- '):
            items.append({'t': 'li', 'text': line[2:].strip()})
        elif line.strip():
            items.append({'t': 'p', 'text': line.strip()})
    return date, items


def fetch_release_notes(version):
    for url in (UPDATE_CHANGELOG_URL, UPDATE_CHANGELOG_RAW_URL):
        try:
            req = urllib.request.Request(url,
                                         headers={'User-Agent': 'kimi-code-usage/%s' % PLUGIN_VERSION,
                                                  'Accept': 'application/vnd.github.raw+json'})
            with urllib.request.urlopen(req, timeout=8) as r:
                return parse_changelog(r.read().decode('utf-8'), version)
        except Exception:
            continue
    return '', []


def check_update(force=False):
    """对比 GitHub 清单版本，5 分钟内存缓存。返回 {current, latest, update, error}。"""
    now = time.time()
    if not force and _UPDATE_CACHE['data'] is not None and now - _UPDATE_CACHE['t'] < 300:
        return _UPDATE_CACHE['data']
    out = {'current': PLUGIN_VERSION, 'latest': None, 'update': False,
           'repo': 'https://github.com/%s' % UPDATE_REPO, 'error': None}
    errors = []
    for url in (UPDATE_MANIFEST_URL, UPDATE_MANIFEST_RAW_URL):
        try:
            req = urllib.request.Request(url,
                                         headers={'User-Agent': 'kimi-code-usage/%s' % PLUGIN_VERSION,
                                                  'Accept': 'application/vnd.github.raw+json'})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode('utf-8'))
            latest = str(d.get('version') or '')
            if not latest:
                raise ValueError('清单缺少 version 字段')
            out['latest'] = latest
            out['update'] = _semver(latest) > _semver(PLUGIN_VERSION)
            out['released'], out['notes'] = fetch_release_notes(latest) if out['update'] else ('', [])
            out['error'] = None
            _UPDATE_CACHE['t'] = now
            _UPDATE_CACHE['data'] = out
            return out
        except Exception as e:
            errors.append('%r' % e)
    out['error'] = ' / '.join(errors)
    return out


def apply_update():
    """下载新版 zip → 拉起 updater.py 守护进程 → 安排本进程退出（updater 会重新拉起）。"""
    try:
        req = urllib.request.Request(UPDATE_ZIP_URL,
                                     headers={'User-Agent': 'kimi-code-usage/%s' % PLUGIN_VERSION})
        fd, zip_path = tempfile.mkstemp(prefix='kimi-usage-update-', suffix='.zip')
        try:
            with os.fdopen(fd, 'wb') as f, urllib.request.urlopen(req, timeout=30) as r:
                shutil.copyfileobj(r, f)
        except Exception:
            try:
                os.remove(zip_path)
            except OSError:
                pass
            raise
        py = sys.executable
        cand = os.path.join(os.path.dirname(py), 'pythonw.exe')
        if os.path.exists(cand):
            py = cand
        flags = 0
        if os.name == 'nt':
            flags = getattr(subprocess, 'DETACHED_PROCESS', 0x00000008) | getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
        subprocess.Popen([py, os.path.join(SCRIPT_DIR, 'updater.py'), PLUGIN_ROOT, zip_path],
                         cwd=PLUGIN_ROOT, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
        log('updater spawned, scheduling self-exit for update')
        # 让 HTTP 响应先发出去再退出
        threading.Timer(0.8, os._exit, args=(0,)).start()
        return True, '更新包已下载，服务正在重启…'
    except Exception as e:
        log('apply_update failed: %r' % e)
        return False, '更新失败：%r' % e


# ---------------- HTTP API ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Private-Network', 'true')

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/api/status', '/api/health'):
            self._json({'status': 'ok', 'name': 'kimi-code-usage', 'port': PORT,
                        'pid': os.getpid(), 'version': PLUGIN_VERSION,
                        'widget': widget_digest()})
        elif path == '/api/update/check':
            self._json(check_update(force='force' in self.path))
        elif path in ('/api/usage', '/api/dashboard'):
            self._json(LATEST_DATA or collect_once() or {})
        elif path == '/api/quota':
            self._json({'quota': (LATEST_DATA or {}).get('quota'),
                        'official': LATEST_OFFICIAL})
        elif path == '/api/data':
            self._json(get_models_data())
        elif path == '/api/reload':
            self._json({'success': True, 'data': collect_once() or {}})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode('utf-8'))
        except Exception:
            req = {}
        try:
            if path == '/api/set-default':
                alias = req.get('alias', '')
                if not alias:
                    return self._json({'success': False, 'message': '缺少 alias'}, 400)
                ok, msg = safe_apply_config(set_default_model_in_text(get_config_content(), alias))
                return self._json({'success': ok, 'message': msg}, 200 if ok else 400)

            if path == '/api/toggle-capability':
                alias, cap = req.get('alias', ''), req.get('capability', '')
                enabled = bool(req.get('enabled', True))
                if not alias or not cap:
                    return self._json({'success': False, 'message': '缺少参数'}, 400)
                data = get_models_data()
                target = next((m for m in data['models'] if m['alias'] == alias), None)
                if not target:
                    return self._json({'success': False, 'message': '模型 %s 不存在' % alias}, 400)
                caps = list(target['capabilities'])
                if enabled and cap not in caps:
                    caps.append(cap)
                elif not enabled and cap in caps:
                    caps.remove(cap)
                new_content, _ = update_model_in_text(get_config_content(), alias, {'capabilities': caps})
                ok, msg = safe_apply_config(new_content)
                return self._json({'success': ok, 'message': msg}, 200 if ok else 400)

            if path == '/api/update-model':
                alias, updates = req.get('alias', ''), req.get('updates', {})
                if not alias or not updates:
                    return self._json({'success': False, 'message': '缺少参数'}, 400)
                new_content, err = update_model_effort_in_text(get_config_content(), alias, updates)
                if err:
                    return self._json({'success': False, 'message': err}, 400)
                ok, msg = safe_apply_config(new_content)
                note = _global_effort_note(alias, updates['default_effort']) if ok and updates.get('default_effort') else ''
                return self._json({'success': ok, 'message': msg, 'note': note}, 200 if ok else 400)

            if path == '/api/add-model':
                alias = req.get('alias', '').strip()
                provider = req.get('provider', '').strip()
                model_id = req.get('model', '').strip()
                if not alias or not provider or not model_id:
                    return self._json({'success': False, 'message': 'alias/provider/model 必填'}, 400)
                efforts = req.get('support_efforts')
                effort_lines = ''
                if efforts:
                    if (not isinstance(efforts, list)
                            or any(e not in EFFORT_LEVELS for e in efforts)):
                        return self._json({'success': False,
                                           'message': 'support_efforts 只能包含 %s' % '/'.join(EFFORT_LEVELS)}, 400)
                    dflt = req.get('default_effort') or efforts[0]
                    if dflt not in efforts:
                        return self._json({'success': False,
                                           'message': 'default_effort 不在 support_efforts 内'}, 400)
                    effort_lines = ('support_efforts = [ %s ]\ndefault_effort = "%s"\n'
                                    % (', '.join('"%s"' % e for e in efforts), dflt))
                block = ('\n[models."%s"]\nprovider = "%s"\nmodel = "%s"\n'
                         'max_context_size = %d\ncapabilities = [ "tool_use", "thinking", "image_in" ]\n'
                         'display_name = "%s"\n%s'
                         % (alias, provider, model_id,
                            int(req.get('max_context_size', 250000)),
                            req.get('display_name', '').strip() or alias, effort_lines))
                ok, msg = safe_apply_config(get_config_content() + block)
                return self._json({'success': ok, 'message': msg}, 200 if ok else 400)

            if path == '/api/set-price':
                alias = (req.get('alias') or '').strip()
                mode = (req.get('mode') or '').strip()
                if not alias:
                    return self._json({'success': False, 'message': '缺少 alias'}, 400)
                if mode not in ('volume', 'per_call', ''):
                    return self._json({'success': False, 'message': "mode 需为 volume / per_call / ''(重置)"}, 400)
                if not mode:
                    save_pricing(alias, None)
                    collect_once()
                    return self._json({'success': True, 'message': '%s 已恢复默认计价' % alias})
                if mode == 'per_call':
                    try:
                        price = float(req.get('price'))
                    except (TypeError, ValueError):
                        return self._json({'success': False, 'message': 'price 必须是数字'}, 400)
                    save_pricing(alias, {'mode': 'per_call', 'price': price})
                    collect_once()
                    return self._json({'success': True, 'message': '%s 按次计价 ¥%g/次' % (alias, price)})
                entry = {'mode': 'volume'}
                for k in ('input', 'output', 'cache_hit', 'cache_write'):
                    v = req.get(k)
                    if v is not None and v != '':
                        try:
                            entry[k] = float(v)
                        except (TypeError, ValueError):
                            return self._json({'success': False, 'message': '%s 必须是数字' % k}, 400)
                save_pricing(alias, entry)
                collect_once()
                return self._json({'success': True, 'message': '%s 按量计价已保存' % alias})

            if path == '/api/issue-action':
                action = req.get('action', '')
                alias, code = req.get('alias', ''), req.get('code', '')
                if action in ('ignore', 'unignore'):
                    cur = load_dismissed()
                    key = '%s|%s' % (alias, code)
                    (cur.add if action == 'ignore' else cur.discard)(key)
                    save_dismissed(cur)
                    return self._json({'success': True, 'message': '已忽略' if action == 'ignore' else '已恢复提示'})
                if action == 'unignore_all':
                    save_dismissed(set())
                    return self._json({'success': True, 'message': '已恢复全部提示'})
                data = get_models_data()
                content = get_config_content()
                done, errs = 0, []
                for mdl in data['models']:
                    for it in mdl['effort_issues']:
                        if it.get('dismissed') or not it.get('code') or it['level'] == 'info':
                            continue
                        if action == 'fix':
                            if mdl['alias'] != alias or it['code'] != code:
                                continue
                        elif action == 'fix_all':
                            if it['code'] == 'no_support_efforts' or not it.get('fix'):
                                continue
                        else:
                            return self._json({'success': False, 'message': '未知 action'}, 400)
                        content, err = apply_issue_fix(content, mdl, it)
                        if err:
                            errs.append(err)
                        else:
                            done += 1
                if errs and not done:
                    return self._json({'success': False, 'message': '；'.join(sorted(set(errs)))}, 400)
                if not done:
                    return self._json({'success': True, 'message': '没有需要修复的项'})
                ok, msg = safe_apply_config(content)
                if ok and errs:
                    msg += '（另有未处理：%s）' % '；'.join(sorted(set(errs)))
                return self._json({'success': ok, 'message': msg if not ok else '已修复 %d 项，会话内 /reload 生效' % done}, 200 if ok else 400)

            if path == '/api/auto-enable-all':
                ok, msg = safe_apply_config(auto_enable_all_in_text(get_config_content()))
                return self._json({'success': ok, 'message': msg}, 200 if ok else 400)

            if path == '/api/update/apply':
                chk = check_update(force=True)
                if chk.get('error'):
                    return self._json({'success': False, 'message': '检查更新失败：%s' % chk['error']}, 502)
                if not chk.get('update'):
                    return self._json({'success': True, 'message': '已是最新版本 v%s' % PLUGIN_VERSION,
                                       'current': PLUGIN_VERSION})
                ok, msg = apply_update()
                d = {'success': ok, 'message': msg, 'current': PLUGIN_VERSION, 'latest': chk.get('latest')}
                return self._json(d, 200 if ok else 500)
        except Exception as e:
            return self._json({'success': False, 'message': '处理异常：%r' % e}, 500)

        self.send_response(404)
        self.end_headers()


# ---------------- 状态持久化 ----------------
def load_state():
    try:
        st = json.load(open(STATE_FILE, encoding='utf-8'))
    except Exception:
        return
    if not SCANNER or not SCANNER.import_state(st):
        return
    # 一致性自愈：历史污染（被杀 daemon 的半扫档）表现为
    # offsets 已标大量文件但 daily/sessions 聚合远小于 all → 清档重扫
    try:
        all_r = int((st.get('all') or {}).get('records') or 0)
        daily_r = sum(int(b.get('records') or 0) for b in (st.get('daily') or {}).values())
        offsets_n = len(st.get('offsets') or {})
        if offsets_n > 50 and all_r > 500 and daily_r < all_r * 0.5:
            log('state inconsistent (all=%d daily=%d offsets=%d) -> full rescan' % (all_r, daily_r, offsets_n))
            SCANNER.reset()
            return
    except Exception:
        pass
    log('state restored (v%d)' % st.get('version', 0))


def save_state():
    if not SCANNER or not STATE_DIRTY['v']:
        return
    try:
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(SCANNER.export_state(), f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
        STATE_DIRTY['v'] = False
    except Exception:
        pass
    # 顺带清理旧版采集器的遗留文件
    for stale in ('credentials.json', 'quota-current.json'):
        p = os.path.join(KIMI_HOME, stale)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


# ---------------- 主循环 / 启动 ----------------
def _poll_official_loop():
    global LATEST_OFFICIAL
    while True:
        try:
            LATEST_OFFICIAL = scanner.fetch_official()
        except Exception:
            pass
        time.sleep(300)


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen(url, timeout=3):
    """绕过系统代理直连本机（代理可能劫持 127.0.0.1 导致探活失败→重复拉起）。"""
    return _NO_PROXY.open(url, timeout=timeout)


def port_is_ours():
    for _ in range(2):
        try:
            with _urlopen('http://127.0.0.1:%d/api/status' % PORT, timeout=4) as r:
                d = json.loads(r.read().decode())
                return d.get('name') == 'kimi-code-usage'
        except Exception:
            time.sleep(0.3)
    return False


def spawn_daemon():
    """静默拉起常驻服务进程（pythonw 后台）。"""
    py = sys.executable
    # Microsoft Store 存根（WindowsApps）不能拉起后台服务
    if 'WindowsApps' in py.replace('/', '\\'):
        py = ''
    # 优先 pythonw.exe 免窗口
    cand = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    if not py and os.path.exists(cand):
        py = cand
    for alt in (r'C:\Program Files\python\pythonw.exe', r'C:\Program Files\Python313\pythonw.exe',
                r'C:\Program Files\Python312\pythonw.exe', r'C:\Program Files\Python311\pythonw.exe'):
        if not os.path.exists(py) or 'WindowsApps' in py:
            if os.path.exists(alt):
                py = alt
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0x00000008) | getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
    try:
        subprocess.Popen([py, os.path.join(SCRIPT_DIR, 'service.py')],
                         cwd=PLUGIN_ROOT, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
        log('daemon spawned via %s' % py)
        return True
    except Exception as e:
        log('spawn_daemon failed: %r' % e)
        return False


def tick():
    """hook 入口：服务不在就拉起；服务在就触发一次采集刷新。"""
    if port_is_ours():
        try:
            _urlopen('http://127.0.0.1:%d/api/reload' % PORT, timeout=3).read()
        except Exception:
            pass
        return 0
    # 端口空着或被旧套件/僵尸进程占着：迁移旧套件，杀掉占用者后拉起新服务
    migrate_legacy()
    kill_port_owner()
    time.sleep(0.6)
    if port_is_ours():
        return 0                 # 期间另一个 tick 已经把服务拉起来了
    if spawn_lock() and spawn_daemon():
        # 等服务起来完成首次注入即可返回；失败时降级为一次性采集
        for _ in range(40):
            if port_is_ours():
                return 0
            time.sleep(0.25)
    return run_once()


def run_once():
    """降级：无常驻服务，跑一次采集+注入+落盘。"""
    global SCANNER
    if SCANNER is None:
        SCANNER = scanner.UsageScanner()
        load_state()
    migrate_legacy()
    kill_port_owner()
    sync_widget_asset()
    ensure_injection()
    collect_once()
    save_state()
    return 0


class _HTTPServer(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 允许两个进程同时 LISTEN 同一端口——关掉它防双绑
    allow_reuse_address = False


def run_server():
    global SCANNER
    if port_is_ours():
        log('another daemon already serving on %d, exiting' % PORT)
        return
    migrate_legacy()
    SCANNER = scanner.UsageScanner()
    load_state()
    # 扫描由主循环 collect_once 驱动，不再另起后台线程（双线程曾并发扫文件导致重复计数）
    threading.Thread(target=_poll_official_loop, daemon=True).start()

    httpd = None
    try:
        httpd = _HTTPServer(('127.0.0.1', PORT), Handler)
    except OSError as e:
        # 端口被占：若不是我们的服务，迁移旧套件并按 PID 清场后再绑一次
        if not port_is_ours():
            migrate_legacy()
            kill_port_owner()
            try:
                httpd = _HTTPServer(('127.0.0.1', PORT), Handler)
            except OSError:
                log('port %d unavailable: %r' % (PORT, e))
                httpd = None
    if httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        log('service listening on 127.0.0.1:%d' % PORT)
    else:
        # 没绑到端口还继续跑只会攒脏 state（无法提供 API）——退出交给下次 tick 重拉
        save_state()
        log('exiting: no HTTP listener bound')
        return

    n = 0
    while True:
        try:
            sync_widget_asset()
            ensure_injection()
            collect_once()
            n += 1
            if n % 15 == 0:
                save_state()
        except Exception as e:
            log('loop error: %r' % e)
        time.sleep(2)


if __name__ == '__main__':
    try:
        if '--tick' in sys.argv:
            sys.exit(tick())
        if '--once' in sys.argv:
            sys.exit(run_once())
        run_server()
    except KeyboardInterrupt:
        save_state()
    except Exception as e:
        log('fatal: %r' % e)
        try:
            save_state()
        except Exception:
            pass
        sys.exit(1)
