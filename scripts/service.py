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
     --restart   : 核实旧 daemon 为本插件 service.py 后停止并拉起新代码（升级后手动换版本用）
     --once      : 只采集+写文件，不常驻（无服务时的降级刷新）
     无参数      : 常驻服务

启动链：kimi.plugin.json hooks → scripts/bootstrap.cmd → service.py
（桌面端无需任何开机自启/桌面脚本；服务随会话心跳拉起，随客户端退出自然闲置）
"""
import atexit
import hashlib
import io
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
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
# 远程控制（本机 QR/链接手机遥控）可选资产，固定顺序即注入顺序：
# vendor 依赖最先，remote-qr/api 次之，mobile-api 须在 remote-widget 之前，
# remote-widget 再次，usage-widget 永远最后
REMOTE_ASSETS = (
    'vendor/qrcodegen.js',
    'kimi-remote-qr.js',
    'kimi-remote-api.js',
    'kimi-mobile-api.js',
    'kimi-remote-widget.js',
)
REMOTE_STYLE_ASSETS = ('kimi-remote-widget-live.css',)
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

LOCAL_PREVIEW_MSG = '本地预览已禁用 GitHub 自更新，请手动安装确认后的正式版本。'


def _local_manifest():
    try:
        with open(os.path.join(PLUGIN_ROOT, 'kimi.plugin.json'), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _manifest_is_local(d):
    return d.get('localPreview') is True or '-local' in str(d.get('version') or '')


def _is_local_preview():
    """check_update 拦截条件：清单明确标记本地预览（清单损坏不拦查询，只拦 apply）。"""
    d = _local_manifest()
    return bool(d) and _manifest_is_local(d)


def _apply_update_blocked():
    """apply 一律 fail closed：本地预览标记或清单不可读/损坏都拒绝自更新。"""
    d = _local_manifest()
    return not d or _manifest_is_local(d)


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


def _python_exe_ok(exe):
    """argv[0] 是否确认为 python/pythonw 直跑（与 strict restart 同一判别口径）。"""
    name = os.path.basename(str(exe or '')).lower()
    return (name in ('python.exe', 'pythonw.exe')
            or bool(re.match(r'^python3(\.\d+)*\.exe$', name))
            or os.path.normcase(str(exe or '')) == os.path.normcase(sys.executable))


def _is_legacy_daemon_argv(argv):
    """argv 是否精确为 python 直接运行 LEGACY_DIR 下 server.py/supervisor.py。
    -c、其他文件副本、路径当参数等一律不匹配。"""
    if len(argv) != 2 or not _python_exe_ok(argv[0]):
        return False
    script = argv[1]
    if (os.path.basename(script) not in ('server.py', 'supervisor.py')
            or not os.path.isabs(script)):
        return False
    return (os.path.normcase(os.path.dirname(os.path.abspath(script)))
            == os.path.normcase(os.path.abspath(LEGACY_DIR)))


def _legacy_daemon_pids():
    """枚举 Win32_Process，只返回通过 _is_legacy_daemon_argv 严格核实的 PID。"""
    out = []
    try:
        q = subprocess.run([
            'powershell', '-NoProfile', '-Command',
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'python.*\\.py' }"
            " | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        ], capture_output=True, text=True, timeout=20)
        rows = json.loads(q.stdout or '[]')
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows if isinstance(rows, list) else []:
            try:
                pid = int(row.get('ProcessId') or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if pid <= 0 or pid == os.getpid():
                continue
            if _is_legacy_daemon_argv(_parse_cmdline(str(row.get('CommandLine') or '').strip())):
                out.append(pid)
    except Exception:
        pass
    return out


def _kill_legacy_processes():
    """只停经严格 argv 核实为 LEGACY_DIR\\server.py|supervisor.py 直跑的旧 daemon（幂等，flag 后每次也跑）。
    非匹配进程（其他副本、-c、路径当参数等）一概不碰。"""
    for pid in _legacy_daemon_pids():
        try:
            subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                           capture_output=True, timeout=10)
            log('killed legacy model-manager daemon pid %d' % pid)
        except Exception:
            pass


def kill_port_owner():
    """端口被占且 API 无响应时，只杀经 _daemon_pid_verified 核实为本插件 service.py 的
    残留 daemon（如挂死进程）；陌生占用者一律不杀，只记录冲突日志。"""
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
                if _daemon_pid_verified(pid):
                    subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                                   capture_output=True, timeout=10)
                    log('killed stale own daemon on %d (pid %s)' % (PORT, pid))
                else:
                    log('port %d held by foreign pid %s; refusing to kill' % (PORT, pid))
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


# 注入标签统一由 ensure_injection/_INJECT_RE 管理（widget 带 ?v=内容摘要版本号）
_INJECT_RE = re.compile(r'\s*<script src="/assets/kimi-(embedded|usage)-(data|widget)\.js[^"]*"></script>\s*')
# 旧套件残留的根路径注入（/kimi-usage-widget.js 等非 /assets/ 前缀）——会与新版卡片
# 争用同一 DOM 容器且不渲染模型/会话行，必须一并清掉，不能只靠 _INJECT_RE
_LEGACY_ROOT_INJECT_RE = re.compile(r'\s*<script src="/kimi-[a-z-]*(?:data|widget)\.js[^"]*"></script>\s*')
# 无 Python 时注入的"需要 Python"占位卡片——服务起来后要摘掉，否则装了 Python
# 仍残留一行提示
_NEEDPY_INJECT_RE = re.compile(r'\s*<script src="/assets/kimi-usage-needpy\.js[^"]*"></script>\s*')
# 远程控制可选资产的托管注入标签（点名清理，不碰任何其他 script 标签）：
# 源文件被移除/未发布时旧标签必须一并摘掉，避免 404 脚本残留
_REMOTE_INJECT_RE = re.compile(
    r'\s*<script src="/assets/(?:vendor/qrcodegen|kimi-remote-(?:qr|api|widget)|kimi-mobile-api)\.js[^"]*"></script>\s*')


def strip_legacy_injections():
    try:
        html = io.open(INDEX_HTML, encoding='utf-8').read()
        new_html = _NEEDPY_INJECT_RE.sub('\n',
                  _REMOTE_INJECT_RE.sub('\n',
                  _LEGACY_ROOT_INJECT_RE.sub('\n', _INJECT_RE.sub('\n', html))))
        if new_html != html:
            io.open(INDEX_HTML, 'w', encoding='utf-8').write(new_html)
    except Exception:
        pass


def _widget_tag():
    """带内容摘要版本号的注入标签——浏览器/Electron 会缓存无参数 script URL，
    不加版本号时 widget 更新后用户仍看到旧 UI（如缺价格按钮）。
    用文件内容 digest 而非 mtime：同一秒内改内容版本号也会变，防同秒旧缓存。"""
    return '<script src="/assets/kimi-usage-widget.js?v=%s"></script>' % (
        _file_digest(WIDGET_SRC) or '0')


def _remote_tags():
    # 仅当源/目标文件都存在且内容摘要一致才注入——同步失败时摘标签而不是
    # 给用户发一份挂了新 ?v 的旧代码
    tags = []
    for rel in REMOTE_ASSETS:
        src = os.path.join(ASSETS_SRC, rel.replace('/', os.sep))
        dest = os.path.join(DIST_ASSETS, rel.replace('/', os.sep)) if DIST_ASSETS else ''
        src_d = _file_digest(src)
        if not src_d:
            continue
        dest_d = _file_digest(dest) if dest else ''
        if src_d != dest_d:
            log('remote asset not injected (sync incomplete): %s' % rel)
            continue
        tags.append('<script src="/assets/%s?v=%s"></script>' % (rel, src_d))
    return tags


def ensure_injection():
    """自愈：index.html 缺注入标签就补；清掉旧注入、远程标签与带 ?v= 的重复标签。"""
    if not INDEX_HTML or not os.path.exists(INDEX_HTML):
        return
    try:
        html = io.open(INDEX_HTML, encoding='utf-8').read()
        new_html = _NEEDPY_INJECT_RE.sub('\n',
                  _REMOTE_INJECT_RE.sub('\n',
                  _LEGACY_ROOT_INJECT_RE.sub('\n', _INJECT_RE.sub('\n', html))))
        data_tag = '<script src="/assets/kimi-usage-data.js"></script>'
        tags = [data_tag] + _remote_tags() + [_widget_tag()]
        block = ''.join('  %s\n' % t for t in tags)
        if '</body>' in new_html:
            new_html = new_html.replace('</body>', block + '  </body>')
        else:
            new_html += '\n' + block
        if new_html != html:
            io.open(INDEX_HTML, 'w', encoding='utf-8').write(new_html)
    except Exception:
        pass


def sync_widget_asset():
    """把插件内的侧栏卡片 JS 与可选 JS/CSS 同步到 desktop-dist/assets（自愈看护）。"""
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
    for rel in REMOTE_ASSETS + REMOTE_STYLE_ASSETS:
        src = os.path.join(ASSETS_SRC, rel.replace('/', os.sep))
        if not os.path.exists(src):
            continue                # 可选资产未发布：跳过，不影响既有功能
        dst = os.path.join(DIST_ASSETS, rel.replace('/', os.sep))
        try:
            if (not os.path.exists(dst)
                    or os.path.getsize(dst) != os.path.getsize(src)
                    or _file_digest(dst) != _file_digest(src)):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        except Exception as e:
            log('remote asset sync failed: %s (%r)' % (rel, e))


def _file_digest(path):
    try:
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()[:12]
    except Exception:
        return ''


def _remote_style_loader():
    if not DIST_ASSETS:
        return ''
    rel = REMOTE_STYLE_ASSETS[0]
    src_d = _file_digest(os.path.join(ASSETS_SRC, rel))
    if not src_d or src_d != _file_digest(os.path.join(DIST_ASSETS, rel)):
        return ''
    href = json.dumps('/assets/kimi-remote-widget-live.css?v=' + src_d)
    return ('''(function () {
  if (typeof document === "undefined" || !document || !document.head) return;
  var head = document.head;
  var href = %s;
  var readyId = "ku-remote-live-style";
  var pendingId = "ku-remote-live-style-pending";
  var ready = document.getElementById(readyId);
  var pending = document.getElementById(pendingId);
  if (pending && pending.getAttribute("href") !== href) {
    if (pending.parentNode) pending.parentNode.removeChild(pending);
    pending = null;
  }
  if ((ready && ready.getAttribute("href") === href) ||
      (pending && pending.getAttribute("href") === href)) return;
  var link = document.createElement("link");
  link.id = pendingId;
  link.rel = "stylesheet";
  link.href = href;
  link.onload = function () {
    if (document.getElementById(pendingId) !== link || link.parentNode !== head) return;
    var old = document.getElementById(readyId);
    if (old && old.parentNode) old.parentNode.removeChild(old);
    link.id = readyId;
    link.onload = link.onerror = null;
  };
  link.onerror = function () {
    if (document.getElementById(pendingId) !== link || link.parentNode !== head) return;
    head.removeChild(link);
    link.onload = link.onerror = null;
  };
  head.appendChild(link);
})();
''' % href)


def widget_digest():
    # 联合摘要：源内容 + 目标(dest)就绪状态 + index.html 注入现状。
    # 同步失败/恢复、注入缺失/补回都会改变指纹，渲染端据此提示重载。
    h = hashlib.md5()
    rels = [('kimi-usage-widget.js',)] + [(rel,) for rel in REMOTE_ASSETS]
    for (rel,) in rels:
        src = WIDGET_SRC if rel == 'kimi-usage-widget.js' else os.path.join(
            ASSETS_SRC, rel.replace('/', os.sep))
        dst = os.path.join(DIST_ASSETS, rel.replace('/', os.sep)) if DIST_ASSETS else ''
        h.update(rel.encode('utf-8'))
        try:
            with open(src, 'rb') as f:
                h.update(f.read())
        except Exception:
            h.update(b'\x00missing\x00')
        h.update(b'|dst=' + (_file_digest(dst) or 'none').encode('utf-8'))
    try:
        html = io.open(INDEX_HTML, encoding='utf-8').read() if INDEX_HTML else ''
    except Exception:
        html = ''
    for name in ['kimi-usage-data.js'] + [r for (r,) in rels]:
        h.update(('|inj:%s=%d' % (name, 'src="/assets/%s' % name in html)).encode('utf-8'))
    return h.hexdigest()[:12]


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
        js += _remote_style_loader()
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
    """白名单内单字段改写（键/值类型校验失败直接返回 False）。"""
    if _valid_field_updates({key: value}, _PROVIDER_FIELD_TYPES, 'provider') is not None:
        return content, False
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


# TOML 安全序列化：字符串一律经 JSON 转义（与 TOML basic string 兼容：
# 引号/反斜杠/换行/控制字符全部被转义，无法注入表头或键值）。
def _toml_string(v):
    return json.dumps(str(v), ensure_ascii=False)


def _toml_qkey(v):
    """表头内 quoted key：models."<v>" —— 引号/反斜杠/控制字符安全。"""
    return json.dumps(str(v), ensure_ascii=False)


_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]{0,63}$')

# 可写字段白名单：未知字段一律拒写，只保留受支持操作。
_MODEL_FIELD_TYPES = {
    'capabilities': list, 'support_efforts': list, 'default_effort': str,
    'provider': str, 'model': str, 'display_name': str, 'max_context_size': int,
    'adaptive_thinking': bool,
}
_MODEL_SUB_WHITELIST = frozenset({'', 'overrides'})
_PROVIDER_FIELD_TYPES = {'base_url': str, 'type': str}


def _valid_field_updates(updates, allowed, label):
    """updates 必须是非空 dict，键在白名单内且符合 key 语法，值类型匹配。
    返回 None 通过，否则中文错误信息。"""
    if not isinstance(updates, dict) or not updates:
        return '%s更新必须是非空字段字典' % label
    for k, v in updates.items():
        if not isinstance(k, str) or not _KEY_RE.match(k) or k not in allowed:
            return '不支持的%s字段：%r' % (label, k)
        t = allowed[k]
        if t is int and isinstance(v, bool):
            return '字段 %s 类型错误（bool 不是 int）' % k
        if t is not list and not isinstance(v, t):
            return '字段 %s 类型错误（需要 %s）' % (k, t.__name__)
        if t is list and (not isinstance(v, list) or any(not isinstance(x, str) for x in v)):
            return '字段 %s 必须是字符串数组' % k
    return None


def _render_toml_kv(k, v):
    if isinstance(v, bool):
        return '%s = %s' % (k, str(v).lower())
    if isinstance(v, int):
        return '%s = %d' % (k, v)
    if isinstance(v, list):
        return '%s = [ %s ]' % (k, ', '.join(_toml_string(x) for x in v))
    return '%s = %s' % (k, _toml_string(v))


def update_model_in_text(content, alias, updates, sub=''):
    """改写 [models.<alias>] 的字段；sub='overrides' 时改写 [models.<alias>.overrides]（不存在则新建）。
    未知字段/非法值/非法子表直接拒写（返回原样 + False）。"""
    if sub not in _MODEL_SUB_WHITELIST:
        return content, False
    if _valid_field_updates(updates, _MODEL_FIELD_TYPES, '模型') is not None:
        return content, False
    escaped = re.escape(alias)
    suffix = r'\.' + re.escape(sub) if sub else ''
    pattern = r'(\[models\.(?:"%s"|%s)%s\])(.*?)(?=\n\[|\Z)' % (escaped, escaped, suffix)
    m = re.search(pattern, content, re.DOTALL)
    if not m and sub:
        main = re.search(r'(\[models\.(?:"%s"|%s)\])(.*?)(?=\n\[|\Z)' % (escaped, escaped), content, re.DOTALL)
        if not main:
            return content, False
        kvs = ''.join(_render_toml_kv(k, v) + '\n' for k, v in updates.items())
        block = '[models.%s.%s]\n%s' % (_toml_qkey(alias), sub, kvs)
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
    err = _valid_field_updates(updates, _MODEL_FIELD_TYPES, '模型')
    if err:
        return content, err
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
    line = 'default_model = %s' % _toml_string(alias)
    if re.search(r'^default_model\s*=', content, re.MULTILINE):
        # 替换串含 \ 会被 re.sub 按转义解释，必须走 lambda
        return re.sub(r'^default_model\s*=.*$',
                      lambda _m: line, content, flags=re.MULTILINE)
    return line + '\n' + content


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
    if _is_local_preview():      # 置于缓存与网络之前，force 也不能绕过
        return {'current': PLUGIN_VERSION, 'latest': None, 'update': False,
                'blocked': True, 'reason': 'local_preview', 'error': LOCAL_PREVIEW_MSG}
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
    """下载新版 zip → 拉起 updater.py → detach 手机 worker 后有界退出（updater 会重新拉起 daemon）。

    退出路径只 detach：DETACHED worker 及其中的桥会话/隧道/配对继续存活，
    新 daemon 经 worker.json 记录重连接管；显式停止手机连接只能走 /api/mobile/stop。
    """
    if _apply_update_blocked():  # 先于下载/tempfile/守护进程拉起；清单损坏也 fail closed
        return False, LOCAL_PREVIEW_MSG
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
        _mobile_begin_shutdown()
        threading.Timer(0.8, _exit_after_shutdown, args=(0,)).start()
        return True, '更新包已下载，服务正在重启…'
    except Exception as e:
        log('apply_update failed: %r' % e)
        return False, '更新失败：%r' % e


# ---------------- 手机连接控制面（独立 mobile worker 进程） ----------------
# /api/mobile* 在 legacy 路由/body 读取之前分流：本层校验 ORIGINAL 请求的
# loopback peer / 精确 Host / 关键单值头 / TE / CL / Origin 后，把已核实的
# method+path+body 交给 MobileWorkerClient——独立 detached worker 进程持有
# MobileBridgeManager+ConnectorRuntime，daemon 退出/重启不吊销手机会话
# （begin_shutdown/shutdown 仅 detach，绝不 stop worker），同版本 worker 经
# 状态记录重连接管。worker 不可用时 fail closed（无放行头），绝不回退 legacy，
# 也绝不回退进程内 bridge。status/stop 不隐式拉起进程；start/install 显式确保。
_MOBILE_PREFIX = '/api/mobile'
_MOBILE = {'mgr': None, 'tried': False}
_MOBILE_LOCK = threading.Lock()
_MOBILE_STATE_LOCK = threading.Lock()
_MOBILE_CLOSING = threading.Event()
_MOBILE_CONTROL_HEADER = 'X-Kimi-Mobile-Control'
_MOBILE_TRUSTED_APP_ORIGIN = 'app://renderer'
_MOBILE_MAX_BODY = 8192
_MOBILE_ERROR_CODES = frozenset((
    'CONNECTOR_MISSING', 'CONNECTOR_INSTALL_FAILED', 'CONNECTOR_HASH_MISMATCH',
    'CONNECTOR_UNSUPPORTED', 'CONNECTOR_BUSY', 'CONSENT_REQUIRED',
    'TUNNEL_START_FAILED', 'TUNNEL_TIMEOUT', 'TUNNEL_EXITED', 'OWNER_LOST',
    'START_CANCELLED',
    # mobile_worker spawn/ensure 阶段码（与 mobile_bridge._MOBILE_ERROR_CODES
    # 同集合）：只透出固定码，不含 stage/stderr/路径/secret 等诊断细节。
    'WORKER_STATE_DIR_UNAVAILABLE', 'WORKER_STARTUP_BUSY', 'WORKER_LOCK_HELD',
    'WORKER_SPAWN_DENIED', 'WORKER_SPAWN_FAILED', 'WORKER_CHILD_EXITED',
    'WORKER_BOOT_TIMEOUT', 'WORKER_VERSION_MISMATCH',
))


def _mobile_manager():
    """惰性构造 worker 客户端；只传 kimi_home，永久关闭后不再构造。"""
    with _MOBILE_STATE_LOCK:
        if _MOBILE_CLOSING.is_set():
            return None
        if _MOBILE['mgr'] is not None:
            return _MOBILE['mgr']
    with _MOBILE_LOCK:
        if _MOBILE_CLOSING.is_set():
            return None
        if _MOBILE['mgr'] is None:
            try:
                from mobile_worker import MobileWorkerClient
                _MOBILE['mgr'] = MobileWorkerClient(KIMI_HOME)
                log('mobile worker client ready (lazy, no worker spawn)')
            except Exception as e:
                if not _MOBILE['tried']:
                    log('mobile worker client unavailable: %r' % e)
                _MOBILE['tried'] = True
    return None if _MOBILE_CLOSING.is_set() else _MOBILE['mgr']


def _mobile_begin_shutdown():
    with _MOBILE_STATE_LOCK:
        mgr = _MOBILE['mgr']
        try:
            if mgr is not None:
                mgr.begin_shutdown()     # detach ONLY：worker 继续存活
        finally:
            _MOBILE_CLOSING.set()
        return mgr


def _mobile_fail_closed(handler):
    """worker 客户端不可用/异常时对 /api/mobile* 的统一拒答：
    无 Access-Control-Allow-Origin 等放行头，不泄露内部细节。"""
    body = json.dumps({'error': '手机连接功能暂不可用'},
                      ensure_ascii=False).encode('utf-8')
    try:
        handler.send_response(503)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        handler.wfile.write(body)
    except Exception:
        pass


def _mobile_send(handler, code, obj, origin=''):
    body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    handler.send_response(code)
    if origin:
        handler.send_header('Access-Control-Allow-Origin', origin)
        handler.send_header('Vary', 'Origin')
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Connection', 'close')
    handler.end_headers()
    handler.wfile.write(body)


def _mobile_error(handler, origin, code, msg):
    handler.close_connection = True
    payload = {'error': msg}
    if msg in _MOBILE_ERROR_CODES:
        payload['error_code'] = msg
    _mobile_send(handler, code, payload, origin)


def _mobile_read_body(handler):
    """控制面小 JSON：CL 已在外层校验为单值纯数字；这里有界读取。"""
    cls = handler.headers.get_all('Content-Length') or []
    n = int(cls[0]) if cls else 0
    if n > _MOBILE_MAX_BODY:
        raise ValueError('请求体过大')
    if n == 0:
        return {}
    try:
        handler.connection.settimeout(15.0)
        raw = handler.rfile.read(n)
    except Exception:
        raise ValueError('请求体不完整')
    if len(raw) != n:
        raise ValueError('请求体不完整')
    try:
        body = json.loads(raw.decode('utf-8'))
    except Exception:
        raise ValueError('请求体不是合法 JSON')
    if not isinstance(body, dict):
        raise ValueError('请求体格式错误')
    return body


def _mobile_dispatch(handler):
    """/api/mobile* 前置分流。返回 True 表示请求已终结（含 fail-closed 拒答）。

    先在 ORIGINAL 请求上校验：头解析缺陷、Host/Origin/CL/TE/控制头单值、
    TE 拒、CL 严格数字、loopback peer、精确 Host（本服务端口）、可信 Origin、
    自定义控制头——全部通过才把已核实的 method+path+body 交给 worker 客户端
    （客户端内部以 IPC secret + 重写 Host 访问 worker loopback 控制口）。
    任何一步失败或客户端异常都拒答，绝不回退 legacy。"""
    path = handler.path.split('?')[0]
    if not (path == _MOBILE_PREFIX or path.startswith(_MOBILE_PREFIX + '/')):
        return False
    # --- 原始请求基线校验 ---
    if getattr(handler.headers, 'defects', None):
        _mobile_error(handler, '', 400, '请求头不合法')
        return True
    for h in ('Host', 'Origin', 'Content-Length', 'Transfer-Encoding',
              _MOBILE_CONTROL_HEADER, 'Content-Type', 'Sec-Fetch-Site'):
        if len(handler.headers.get_all(h) or []) > 1:
            _mobile_error(handler, '', 400, '请求头不合法')
            return True
    if handler.headers.get_all('Transfer-Encoding'):
        _mobile_error(handler, '', 400, '请求体格式不被支持')
        return True
    _cls = handler.headers.get_all('Content-Length') or []
    if _cls and not _cls[0].isdigit():
        _mobile_error(handler, '', 400, '请求头不合法')
        return True
    try:
        peer_ok = ipaddress.ip_address(
            handler.client_address[0]).is_loopback
    except Exception:
        peer_ok = False
    if not peer_ok:
        _mobile_error(handler, '', 403, '仅允许本机访问')
        return True
    host_hdr = (handler.headers.get('Host') or '').strip().lower()
    if host_hdr not in _allowed_hosts():
        _mobile_error(handler, '', 403, 'Host 不被允许')
        return True
    origin = (handler.headers.get('Origin') or '').strip()
    method = handler.command
    # 可信 app origin（app://renderer）的 cross-site fetch metadata 属正常
    # （app:// 与 http://loopback 跨 scheme）；其余 cross-site 一律拒
    if ((handler.headers.get('Sec-Fetch-Site') or '').lower() == 'cross-site'
            and origin != _MOBILE_TRUSTED_APP_ORIGIN):
        _mobile_error(handler, '', 403, '跨站请求被拒绝')
        return True
    mgr = None if _MOBILE_CLOSING.is_set() else _mobile_manager()
    if mgr is None or _MOBILE_CLOSING.is_set():
        _mobile_fail_closed(handler)
        return True
    try:
        trusted = set(mgr._trusted_control_origins())
    except Exception:
        trusted = set()
    trusted.add(_MOBILE_TRUSTED_APP_ORIGIN)
    if method == 'OPTIONS':
        if not origin or origin not in trusted:
            _mobile_error(handler, '', 403, 'Origin 不被允许')
            return True
        handler.send_response(204)
        handler.send_header('Access-Control-Allow-Origin', origin)
        handler.send_header('Vary', 'Origin')
        handler.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        handler.send_header('Access-Control-Allow-Headers',
                            '%s, Content-Type' % _MOBILE_CONTROL_HEADER)
        handler.send_header('Access-Control-Allow-Private-Network', 'true')
        handler.send_header('Access-Control-Max-Age', '300')
        handler.send_header('Content-Length', '0')
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        return True
    if origin and origin not in trusted:
        _mobile_error(handler, '', 403, 'Origin 不被允许')
        return True
    if (handler.headers.get(_MOBILE_CONTROL_HEADER) or '') != '1':
        _mobile_error(handler, origin, 403, '缺少控制头')
        return True
    # --- 已核实请求映射到 worker 客户端 ---
    try:
        if method == 'GET' and path == _MOBILE_PREFIX + '/status':
            _mobile_send(handler, 200, mgr.status(), origin)
        elif method == 'POST' and path == _MOBILE_PREFIX + '/connector/install':
            body = _mobile_read_body(handler)
            if set(body) - {'consent', 'consent_version'}:
                raise ValueError('请求参数不被允许')
            _mobile_send(handler, 200, mgr.install_connector(
                body.get('consent'), body.get('consent_version')), origin)
        elif method == 'POST' and path == _MOBILE_PREFIX + '/start':
            body = _mobile_read_body(handler)
            mode = body.get('mode', 'lan')
            allowed = ({'owner_origin', 'mode', 'relay_consent', 'consent_version'}
                       if mode == 'internet' else {'owner_origin', 'mode', 'address'})
            if set(body) - allowed:
                raise ValueError('请求参数不被允许')
            _mobile_send(handler, 200, mgr.start(
                body.get('owner_origin'), body.get('address'), mode,
                body.get('relay_consent', False), body.get('consent_version')),
                origin)
        elif method == 'POST' and path == _MOBILE_PREFIX + '/stop':
            if _mobile_read_body(handler):
                raise ValueError('请求参数不被允许')
            _mobile_send(handler, 200, mgr.stop(), origin)
        elif method == 'POST' and path == _MOBILE_PREFIX + '/pair/rotate':
            if _mobile_read_body(handler):
                raise ValueError('请求参数不被允许')
            _mobile_send(handler, 200, mgr.pair_rotate(), origin)
        else:
            _mobile_error(handler, origin, 404, '接口不存在')
    except ValueError as e:
        _mobile_error(handler, origin, 400, str(e))
    except Exception as e:
        msg = str(e)
        if msg in _MOBILE_ERROR_CODES:
            _mobile_error(handler, origin, 400, msg)
        elif type(e).__name__ == 'MobileBridgeError':
            _mobile_error(handler, origin, 400, msg[:160])
        else:
            log('mobile control error: %r' % e)
            _mobile_fail_closed(handler)
    return True


def _mobile_stop():
    """永久关闭手机控制面：仅 detach——独立 worker 与其中的桥/隧道/配对
    会话全部继续存活，下个 daemon 实例经状态记录重连接管。显式停止手机
    连接只能走 /api/mobile/stop。"""
    try:
        mgr = _mobile_begin_shutdown()
        if mgr is not None:
            mgr.shutdown()               # MobileWorkerClient.shutdown = detach
    except Exception as e:
        log('mobile shutdown error: %r' % e)


def _exit_after_shutdown(code=0, timeout=3.0):
    done = threading.Event()

    def cleanup():
        try:
            _mobile_stop()
        finally:
            done.set()

    try:
        _mobile_begin_shutdown()
        threading.Thread(target=cleanup, daemon=True).start()
        done.wait(timeout)
    finally:
        os._exit(code)


atexit.register(_mobile_stop)


# ---------------- legacy 控制面守卫 ----------------
# 所有方法：loopback peer + 严格 Host（loopback:PORT 精确端口，防 DNS rebinding）
# + 单值 Host/Origin/控制头 + headers.defects 拒绝。
# 读：无 Origin（本机程序）或可信 Origin 放行，其余拒。写：再要求
# X-Kimi-Usage-Control:1（跨站无法伪造）。可信 Origin = 静态集（app://renderer、
# 本服务自身 loopback）+ mobile worker（独立进程）对存活桌面 owner 实例的
# 动态核实（owner 退出/端口复用即不再可信）。CORS 只回显可信 Origin，无 *。
_CONTROL_HEADER = 'X-Kimi-Usage-Control'
_SINGLE_HEADERS = ('Host', 'Origin', _CONTROL_HEADER, 'Sec-Fetch-Site')
_TRUSTED_ORIGINS = frozenset({
    'app://renderer',
    'http://127.0.0.1:%d' % PORT,
    'http://localhost:%d' % PORT,
})


def _allowed_hosts():
    return {'127.0.0.1:%d' % PORT, 'localhost:%d' % PORT,
            '[::1]:%d' % PORT, '::1:%d' % PORT}


# ---------------- HTTP API ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = 'kimi-code-usage'
    sys_version = ''

    def version_string(self):
        return self.server_version

    def log_message(self, *a):
        pass

    def _loopback_peer(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except Exception:
            return False

    def _strict_host_ok(self):
        hosts = self.headers.get_all('Host') or []
        return len(hosts) == 1 and hosts[0].strip().lower() in _allowed_hosts()

    def _origin_ok(self, origin):
        """可信 Origin：静态集 + mobile worker 对存活桌面 owner 实例的动态核实。"""
        if origin in _TRUSTED_ORIGINS:
            return True
        mgr = _mobile_manager()
        if mgr is None or not origin:
            return False
        try:
            trusted = mgr._trusted_control_origins()
        except Exception:
            return False
        return origin in trusted

    def _deny(self, code=403):
        self.close_connection = True
        body = json.dumps({'error': '来源不可信，已拒绝'},
                          ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _drain_body(self, cap=65536):
        """超限 body 的有界排空：≤cap 字节、≤1s 超时，随后强制关闭连接。
        不排空时 Windows 上未读请求体会触发 RST 使错误响应丢失；排空为尽力而为。"""
        self.close_connection = True
        try:
            self.connection.settimeout(1.0)
            remaining = cap
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 8192))
                if not chunk:
                    break
                remaining -= len(chunk)
        except Exception:
            pass

    def _guard_common(self):
        if not self._loopback_peer() or not self._strict_host_ok():
            return self._deny()
        if getattr(self.headers, 'defects', None):
            return self._deny()
        for name in _SINGLE_HEADERS:
            vals = self.headers.get_all(name) or []
            if len(vals) > 1:
                return self._deny()
        self._torigin = (self.headers.get('Origin') or '').strip()
        return True

    def _guard_read(self):
        """读守卫：无 Origin（本机程序）/可信 Origin 放行；foreign/null Origin、
        cross-site 抓取一律拒。"""
        if not self._guard_common():
            return False
        origin = self._torigin
        if self._origin_ok(origin):
            return True
        sfs = (self.headers.get('Sec-Fetch-Site') or '').strip().lower()
        if sfs == 'cross-site':
            return self._deny()
        if not origin:
            return True
        return self._deny()

    def _guard_write(self):
        """写守卫：在读守卫之上再要求自定义控制头，Origin 若存在必须可信。"""
        if not self._guard_common():
            return False
        if (self.headers.get(_CONTROL_HEADER) or '').strip() != '1':
            return self._deny()
        origin = self._torigin
        if not origin:
            return True
        if self._origin_ok(origin):
            return True
        return self._deny()

    def _cors(self, origin=''):
        # 仅对可信 Origin 回显精确值，不发 ACAO:*
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Vary', 'Origin')

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        origin = getattr(self, '_torigin', '')
        self._cors(origin if origin and self._origin_ok(origin) else '')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        if _mobile_dispatch(self):
            return
        if not self._guard_common():
            return
        origin = self._torigin
        if not self._origin_ok(origin):
            return self._deny()
        self.send_response(200)
        self._cors(origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        # 固定允许头，不回显 Access-Control-Request-Headers
        self.send_header('Access-Control-Allow-Headers',
                         'Content-Type, X-Kimi-Usage-Control')
        self.send_header('Access-Control-Max-Age', '600')
        self.end_headers()

    def do_GET(self):
        if _mobile_dispatch(self):
            return
        if not self._guard_read():
            return
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

    def _method_not_allowed(self):
        """非 GET/POST/OPTIONS 方法：/api/mobile* 先分流（worker 客户端自答），
        其余统一 405 JSON（HEAD 不写 body）。"""
        if _mobile_dispatch(self):
            return
        self.close_connection = True
        body = json.dumps({'error': '方法不被支持'},
                          ensure_ascii=False).encode('utf-8')
        self.send_response(405)
        self.send_header('Allow', 'GET, POST, OPTIONS')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def __getattr__(self, name):
        # BaseHTTPRequestHandler 以 hasattr(self, 'do_<METHOD>') 判支持——
        # 拦截全部未定义 do_*（PUT/DELETE/PATCH/HEAD 及其他扩展动词），统一 405。
        if name.startswith('do_'):
            return self._method_not_allowed
        raise AttributeError(name)

    def do_POST(self):
        if _mobile_dispatch(self):
            return
        if not self._guard_write():
            return
        path = self.path.split('?')[0]
        # HTTP framing：TE 一律拒（防请求走私），CL 必须单值纯数字且有界
        if self.headers.get_all('Transfer-Encoding'):
            return self._deny(400)
        cls = self.headers.get_all('Content-Length') or []
        if len(cls) != 1 or not cls[0].strip().isdigit():
            return self._deny(400)
        body_len = int(cls[0].strip())
        if body_len > 262144:
            self._drain_body()
            return self._json({'success': False, 'message': '请求体过大'}, 413)
        try:
            req = json.loads(self.rfile.read(body_len).decode('utf-8'))
            if not isinstance(req, dict):
                req = {}
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
                alias = str(req.get('alias') or '').strip()
                provider = str(req.get('provider') or '').strip()
                model_id = str(req.get('model') or '').strip()
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
                    effort_lines = ('support_efforts = [ %s ]\ndefault_effort = %s\n'
                                    % (', '.join(_toml_string(e) for e in efforts), _toml_string(dflt)))
                try:
                    max_ctx = int(req.get('max_context_size', 250000))
                except (TypeError, ValueError):
                    return self._json({'success': False, 'message': 'max_context_size 必须是整数'}, 400)
                block = ('\n[models.%s]\nprovider = %s\nmodel = %s\n'
                         'max_context_size = %d\ncapabilities = [ "tool_use", "thinking", "image_in" ]\n'
                         'display_name = %s\n%s'
                         % (_toml_qkey(alias), _toml_string(provider), _toml_string(model_id),
                            max_ctx,
                            _toml_string(str(req.get('display_name') or '').strip() or alias), effort_lines))
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
                if _apply_update_blocked():
                    return self._json({'success': False, 'code': 'LOCAL_PREVIEW_UPDATE_BLOCKED',
                                       'message': LOCAL_PREVIEW_MSG}, 409)
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
    # 优先同目录的 pythonw.exe 免窗口；存根场景 cand 也是存根，os.path.exists
    # 为真但 for 循环里的 WindowsApps 检查会把它换掉
    cand = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    if not py and os.path.exists(cand):
        py = cand
    elif py and os.path.exists(cand):
        py = cand
    for alt in (r'C:\Program Files\python\pythonw.exe', r'C:\Program Files\Python313\pythonw.exe',
                r'C:\Program Files\Python312\pythonw.exe', r'C:\Program Files\Python311\pythonw.exe'):
        if not py or not os.path.exists(py) or 'WindowsApps' in py:
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


def _daemon_status():
    """活着的用量面板服务 status 字典；查不到返回 None。"""
    for _ in range(2):
        try:
            with _urlopen('http://127.0.0.1:%d/api/status' % PORT, timeout=4) as r:
                d = json.loads(r.read().decode())
                if d.get('name') == 'kimi-code-usage':
                    return d
        except Exception:
            time.sleep(0.3)
    return None


def _daemon_pid():
    try:
        return int((_daemon_status() or {}).get('pid'))
    except (TypeError, ValueError):
        return None


def _parse_cmdline(cl):
    # Windows 用 CommandLineToArgvW 保证与进程真实 argv 一致（引号/空格不会误判）
    if os.name == 'nt':
        try:
            import ctypes
            f = ctypes.windll.shell32.CommandLineToArgvW
            f.restype = ctypes.POINTER(ctypes.c_wchar_p)
            f.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
            n = ctypes.c_int()
            p = f(cl, ctypes.byref(n))
            return [p[i] for i in range(n.value)]
        except Exception:
            return []
    try:
        return shlex.split(cl)
    except Exception:
        return []


def _daemon_pid_verified(pid):
    """严格核实该 PID 就是"当前 service.py 由 Python 直跑"的 daemon，才允许停止。"""
    try:
        pid = int(pid)
        if pid <= 0 or pid == os.getpid():
            return False
        q = subprocess.run([
            'powershell', '-NoProfile', '-Command',
            "(Get-CimInstance Win32_Process -Filter 'ProcessId=%s').CommandLine" % pid
        ], capture_output=True, text=True, timeout=15)
        argv = _parse_cmdline((q.stdout or '').strip())
        if len(argv) != 2:
            return False
        exe = os.path.basename(argv[0]).lower()
        exe_ok = (exe in ('python.exe', 'pythonw.exe')
                  or bool(re.match(r'^python3(\.\d+)*\.exe$', exe))
                  or os.path.normcase(argv[0]) == os.path.normcase(sys.executable))
        script_ok = (os.path.isabs(argv[1])
                     and os.path.normcase(os.path.abspath(argv[1]))
                     == os.path.normcase(os.path.abspath(__file__)))
        return exe_ok and script_ok
    except Exception:
        return False


def _port_free():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', PORT))
            return True
    except OSError:
        return False


def _wait_spawn_lock():
    """有界等待 spawn.lock 过期（锁本身 12s 失效）。拿不到就放弃——绝不先杀后抢。"""
    for _ in range(15):
        if spawn_lock():
            return True
        time.sleep(1.0)
    return False


def restart_service():
    """--restart：先拿到 spawn_lock，再核实占用者确为本插件 daemon 才停止；
    等端口释放后走既有 spawn_daemon 拉起当前代码，最后以 name+version 确认。"""
    refresh_dist_paths()
    st = _daemon_status()
    pid = st.get('pid') if st else None
    if pid is not None and not _daemon_pid_verified(pid):
        log('restart refused: pid %s not verified as our daemon' % pid)
        print('restart refused: port %d 响应了用量面板 API，但进程无法核实为当前'
              ' service.py，不会自动结束。请手动结束后重试。' % PORT)
        return 2
    # 先取得 spawn 权再等锁内操作；若期间有别的 tick 在拉起，宁可拒绝也不误杀
    if not _wait_spawn_lock():
        print('restart refused: 另一个拉起操作进行中（spawn.lock 未释放），未做任何改动。')
        return 2
    try:
        if pid is not None:
            st2 = _daemon_status()
            pid2 = st2.get('pid') if st2 else None
            if pid2 is None:
                # 等锁期间旧 daemon 已退出：仍要确认端口没被外来进程抢走
                if not _port_free():
                    print('restart refused: 旧 daemon 退出后端口被其他进程占用，未自动处理。')
                    return 2
            elif not _daemon_pid_verified(pid2):
                print('restart refused: 等锁期间端口易主，未核实，未做任何改动。')
                return 2
            else:
                subprocess.run(['taskkill', '/PID', str(pid2), '/F'],
                               capture_output=True, timeout=10)
                for _ in range(40):
                    if _port_free():
                        break
                    time.sleep(0.25)
                else:
                    print('restart failed: 端口 %d 未释放，请稍后重试。' % PORT)
                    return 2
                log('old daemon (pid %s) stopped for restart' % pid2)
        elif not _port_free():
            print('restart refused: 端口 %d 被其他进程占用，未自动处理。' % PORT)
            return 2
        if spawn_daemon():
            for _ in range(40):
                st3 = _daemon_status()
                if st3 and st3.get('version') == PLUGIN_VERSION:
                    print('restarted: service v%s is running on 127.0.0.1:%d'
                          % (st3['version'], PORT))
                    return 0
                time.sleep(0.25)
        print('restart failed: 新服务未能在限定时间内就绪，请查看 scripts/service.log')
        return 1
    finally:
        lock = os.path.join(KIMI_HOME, 'usage-dashboard', 'spawn.lock')
        try:
            # 只释放自己刚拿到的锁（内容为我们写入的 pid），不动他人锁
            if open(lock).read().strip() == str(os.getpid()):
                os.remove(lock)
        except Exception:
            pass


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
        if '--restart' in sys.argv:
            sys.exit(restart_service())
        if '--once' in sys.argv:
            sys.exit(run_once())
        run_server()
    except KeyboardInterrupt:
        _mobile_stop()
        save_state()
    except Exception as e:
        log('fatal: %r' % e)
        _mobile_stop()
        try:
            save_state()
        except Exception:
            pass
        sys.exit(1)
