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
  4. HTTP API :39281 模型与供应商管理（版本冲突检测、严格解析、档位归一化与原子写入）
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

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

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
UNINSTALL_MARKER = os.path.join(KIMI_HOME, 'usage-dashboard', 'plugin-uninstall.json')
UNINSTALL_LOCK = threading.Lock()
UNINSTALL_BEGIN = threading.Event()
_UNINSTALL_EXIT = threading.Event()

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
import relay_config  # noqa: E402
import model_manager  # noqa: E402
import model_catalog  # noqa: E402

CONFIG_LOCK = threading.RLock()
CONFIG_CONFLICT = '配置已被其他操作更改，请刷新后重试'


def _config_locked(function):
    def locked(*args, **kwargs):
        with CONFIG_LOCK:
            return function(*args, **kwargs)
    return locked


def _config_change(transform):
    with CONFIG_LOCK:
        base = get_config_content()
        return safe_apply_config(transform(base), base_content=base)


@_config_locked
def get_model_manager_data():
    try:
        content = get_config_content()
        result = model_manager.snapshot(content)
        if result['editable']:
            try:
                references = _model_manager_mode_references(model_manager.parse(content))
                for model in result['models']:
                    if not model['delete_protected'] and model['alias'] in references:
                        model.update(delete_protected=True, delete_reason=references[model['alias']])
            except model_manager.ConfigError as error:
                for model in result['models']:
                    if not model['delete_protected']:
                        model.update(delete_protected=True, delete_reason=str(error))
    except Exception:
        result = model_manager.snapshot('')
        result.update(editable=False, message='无法读取配置，拒绝改写')
    return result


def get_model_manager_catalog(req):
    result = {'success': False, 'version': '', 'provider': '', 'models': [], 'message': ''}
    try:
        if not isinstance(req, dict) or set(req) != {'version', 'provider'}:
            raise model_manager.ConfigError('请求参数不被允许或缺少必填字段')
        model_manager._string(req['version'], 'version', True)
        model_manager._string(req['provider'], 'provider', True)
        result.update(version=req['version'], provider=req['provider'])
        with CONFIG_LOCK:
            base = get_config_content()
            result['version'] = model_manager.version(base)
            if req['version'] != result['version']:
                result.update(code='CONFIG_CONFLICT', message=CONFIG_CONFLICT)
                return result, 409
            provider = model_catalog.provider_snapshot(model_manager.parse(base), req['provider'])
        failure = None
        try:
            models, message = model_catalog.fetch_models(req['provider'], provider)
        except model_catalog.CatalogError as error:
            failure = str(error)
            models, message = [], failure
        except Exception:
            failure = '上游模型探测失败，请检查供应商配置；仍可手动添加模型'
            models, message = [], failure
        with CONFIG_LOCK:
            current_version = model_manager.version(get_config_content())
            if current_version != req['version']:
                result.update(version=current_version, code='CONFIG_CONFLICT', message=CONFIG_CONFLICT)
                return result, 409
        result.update(success=failure is None, models=models, message=message)
        return result, 400 if failure else 200
    except model_manager.ConfigError as error:
        result['message'] = str(error)
        return result, 400
    except Exception:
        result['message'] = '无法读取配置或执行模型探测'
        return result, 500


@_config_locked
def save_model_manager(path, req):
    try:
        is_batch = path == '/api/model-manager/batch'
        is_provider = path == '/api/model-manager/provider'
        if is_batch:
            allowed = required = {'version', 'provider', 'upserts', 'removes'}
        else:
            item_key = 'provider' if is_provider else 'model'
            original_key = 'original_name' if is_provider else 'original_alias'
            required = {'version', original_key, item_key}
            allowed = required | (set() if is_provider else {'set_default'})
        if not isinstance(req, dict) or set(req) - allowed or not required <= set(req):
            raise model_manager.ConfigError('请求参数不被允许或缺少必填字段')
        model_manager._string(req['version'], 'version', True)
        base = get_config_content()
        if req['version'] != model_manager.version(base):
            return {'success': False, 'code': 'CONFIG_CONFLICT', 'message': CONFIG_CONFLICT}, 409
        modes_version = None
        if is_batch:
            if not isinstance(req['removes'], list):
                raise model_manager.ConfigError('removes 必须是数组')
            references = None
            if req['removes']:
                modes_version = _model_manager_modes_version()
                references = _model_manager_mode_references(model_manager.parse(base))
            new = model_manager.batch_models(base, req['provider'], req['upserts'], req['removes'], references)
        elif is_provider:
            new = model_manager.upsert_provider(base, req[original_key], req[item_key])
        else:
            new = model_manager.upsert_model(base, req[original_key], req[item_key], req.get('set_default', False))
        if modes_version is not None and _model_manager_modes_version() != modes_version:
            return {'success': False, 'code': 'CONFIG_CONFLICT', 'message': CONFIG_CONFLICT}, 409
        ok, msg = safe_apply_config(new, base_content=base)
        if not ok:
            if msg == CONFIG_CONFLICT:
                return {'success': False, 'code': 'CONFIG_CONFLICT', 'message': msg}, 409
            return {'success': False, 'message': msg}, 400
        return {'success': True, 'message': msg, 'version': model_manager.version(get_config_content())}, 200
    except model_manager.ConfigError as e:
        return {'success': False, 'message': str(e)}, 400
    except Exception:
        return {'success': False, 'message': '模型管理操作失败，配置未改动'}, 500


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
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
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


def uninstall_requested():
    """卸载保护标记存在即视为已卸载（阻止心跳/注入复活）。"""
    try:
        os.lstat(UNINSTALL_MARKER)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return True


def refresh_dist_paths():
    global DIST_DIR, DIST_ASSETS, INDEX_HTML
    if uninstall_requested():
        DIST_DIR = DIST_ASSETS = INDEX_HTML = ''
        return
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
        ], capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW)
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
                           capture_output=True, timeout=10, creationflags=_NO_WINDOW)
            log('killed legacy model-manager daemon pid %d' % pid)
        except Exception:
            pass


def kill_port_owner():
    """端口被占且 API 无响应时，只杀经 _daemon_pid_verified 核实为本插件 service.py 的
    残留 daemon（如挂死进程）；陌生占用者一律不杀，只记录冲突日志。
    有 daemon 正在拉起（spawn.lock 新鲜）时一律不杀——并发的 tick/run_once 会走到这里，
    若把对方刚 bind、/api 尚未就绪的 daemon 当残留杀掉，就会互相清场、谁都不起来。"""
    if port_is_ours():
        return
    if _recent_spawn():
        log('port %d busy but a daemon was just spawned; not killing' % PORT)
        return
    try:
        out = subprocess.run(
            ['netstat', '-ano', '-p', 'tcp'],
            capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW).stdout or ''
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
                                   capture_output=True, timeout=10, creationflags=_NO_WINDOW)
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


def _recent_spawn(window=15.0):
    """spawn.lock 刚被写过 → 有 daemon 正在拉起（bind 完成但 /api 尚未就绪也算）。
    此时绝不能把端口占用者当残留 daemon 杀掉，否则并发 tick 会互相清场、谁都起不来。"""
    lock = os.path.join(KIMI_HOME, 'usage-dashboard', 'spawn.lock')
    try:
        return (time.time() - os.stat(lock).st_mtime) < window
    except OSError:
        return False


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
                        '/v', 'KimiCodePlugin', '/f'], capture_output=True, timeout=10, creationflags=_NO_WINDOW)
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
        import tomllib
    except ImportError:
        try:
            import toml
        except ImportError:
            return _mini_toml(content)
    try:
        return model_manager.parse(content)
    except model_manager.ConfigError:
        return {}


def _mini_toml(content):
    """仅供无严格解析器时读取用量模型摘要，不能用于配置写入。"""
    data, section = {}, []
    for raw in content.split('\n'):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            name = line.strip('[] ')
            section = [p.strip().strip('"') for p in re.split(r'\.(?=(?:[^"]*"[^"]*")*[^"]*$)', name)]
            current = data
            for part in section:
                current = current.setdefault(part, {})
            continue
        if '=' in line:
            key, _, value = line.partition('=')
            current = data
            for part in section:
                current = current.setdefault(part, {})
            current[key.strip().strip('"')] = _mini_val(value.strip())
    return data


def _mini_val(value):
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith('['):
        return [item.strip().strip('"') for item in value.strip('[] ').split(',') if item.strip()]
    if value in ('true', 'false'):
        return value == 'true'
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def get_config_content():
    if not os.path.exists(CONFIG_PATH):
        return ''
    with open(CONFIG_PATH, 'rb') as f:
        return f.read().decode('utf-8')


EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')
EFFORT_KEYS = ('support_efforts', 'default_effort')


def _is_managed(model, provider):
    """托管/目录导入的模型：官方刷新可能改写其顶层 support_efforts / default_effort。"""
    name = str(model.get('provider') or '')
    return name.startswith('managed:') or 'oauth' in (provider or {})


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


def _update_provider_field_data(data, provider, key, value):
    if _valid_field_updates({key: value}, _PROVIDER_FIELD_TYPES, 'provider') is not None:
        return False
    old = model_manager._table(data, ('providers',)).get(provider)
    if not isinstance(old, dict) or model_manager.managed_provider(provider, old):
        return False
    item = {'name': provider, 'type': old.get('type', ''),
            'base_url': old.get('base_url', ''), 'key_action': 'keep'}
    item[key] = value
    model_manager._upsert_provider_data(data, provider, item)
    return True


def update_provider_field_in_text(content, provider, key, value):
    try:
        data = model_manager.parse(content)
        if not _update_provider_field_data(data, provider, key, value):
            return content, False
        return model_manager.dumps(data), True
    except model_manager.ConfigError:
        return content, False


def get_models_data(content=None):
    if content is None:
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


@_config_locked
def safe_apply_config(new_content, *, base_content):
    try:
        if get_config_content() != base_content:
            return False, CONFIG_CONFLICT
        model_manager.parse(base_content)
        data = model_manager.parse(new_content)
        model_manager.normalize_support_efforts_data(data)
        new_content = model_manager.dumps(data)
    except model_manager.ConfigError as e:
        return False, str(e)
    except Exception:
        return False, '无法读取配置，拒绝改写'
    tmp = None
    try:
        from mobile_security import _set_protected_dacl
        directory = os.path.dirname(CONFIG_PATH)
        fd, tmp = tempfile.mkstemp(prefix='.config-write-', suffix='.toml', dir=directory)
        with os.fdopen(fd, 'wb') as f:
            _set_protected_dacl(tmp)
            f.write(new_content.encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        if get_config_content() != base_content:
            return False, CONFIG_CONFLICT
        os.replace(tmp, CONFIG_PATH)
        tmp = None
        STATE_DIRTY['v'] = True
        return True, '配置已保存，会话内 /reload 生效'
    except Exception:
        return False, '配置写入或原子替换失败，拒绝改写'
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]{0,63}$')
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


def update_model_in_text(content, alias, updates, sub=''):
    if sub not in _MODEL_SUB_WHITELIST:
        return content, False
    try:
        return model_manager.update_model(content, alias, updates), True
    except model_manager.ConfigError:
        return content, False


def update_model_effort_in_text(content, alias, updates):
    try:
        return model_manager.update_model(content, alias, updates), None
    except model_manager.ConfigError as e:
        return content, str(e)


def _apply_issue_fix_data(data, model, issue):
    code, alias = issue.get('code'), model['alias']
    try:
        if code == 'bad_base_url':
            if not issue.get('fix_value'):
                return '无法自动判断正确地址，请手动修改 config.toml 中 provider「%s」的 base_url' % model['provider']
            if not _update_provider_field_data(data, model['provider'], 'base_url', issue['fix_value']):
                return '未找到 provider「%s」' % model['provider']
        elif code == 'no_thinking_tag':
            target = model_manager._table(data, ('models',)).get(alias)
            if not isinstance(target, dict):
                return '模型 %s 不存在' % alias
            caps = list(model_manager.effective(target, 'capabilities') or [])
            if 'thinking' not in caps:
                caps.append('thinking')
            model_manager._update_model_data(data, alias, {'capabilities': caps})
        elif code == 'no_support_efforts':
            model_manager._update_model_data(data, alias, {'support_efforts': list(EFFORT_LEVELS)})
        elif code == 'default_not_in_support':
            model_manager._update_model_data(data, alias, {'default_effort': issue['fix_value']})
        else:
            return '该问题没有自动修复方案'
    except model_manager.ConfigError as error:
        return str(error)
    return None


def apply_issue_fix(content, model, issue):
    """对单个 (模型, 问题) 套用修复，返回 (完整 TOML, 错误信息)。"""
    try:
        data = model_manager.parse(content)
        error = _apply_issue_fix_data(data, model, issue)
        if error:
            return content, error
        return model_manager.dumps(data), None
    except model_manager.ConfigError as error:
        return content, str(error)


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
    return model_manager.set_default_model(content, alias)


def auto_enable_all_in_text(content):
    data = model_manager.parse(content)
    models = data.get('models', {})
    if not isinstance(models, dict):
        raise model_manager.ConfigError('models 必须是表')
    for alias, m in models.items():
        caps = list(model_manager.effective(m, 'capabilities') or [])
        changed = False
        for cap in ('tool_use', 'thinking', 'image_in'):
            if cap not in caps:
                caps.append(cap)
                changed = True
        if changed:
            model_manager._update_model_data(data, alias, {'capabilities': caps})
    return model_manager.dumps(data)


def toggle_capability_in_text(content, alias, cap, enabled):
    if cap not in ('tool_use', 'thinking', 'image_in') or type(enabled) is not bool:
        raise model_manager.ConfigError('能力仅支持 tool_use/thinking/image_in，enabled 必须是布尔值')
    data = model_manager.parse(content)
    target = model_manager._table(data, ('models',)).get(alias)
    if not isinstance(target, dict):
        raise model_manager.ConfigError('模型不存在')
    caps = list(model_manager.effective(target, 'capabilities') or [])
    if cap == 'thinking' and not enabled and 'always_thinking' in caps:
        raise model_manager.ConfigError('always_thinking 模型的思考能力不可关闭')
    if enabled and cap not in caps:
        caps.append(cap)
    elif not enabled and cap in caps:
        caps.remove(cap)
    model_manager._update_model_data(data, alias, {'capabilities': caps})
    return model_manager.dumps(data)


@_config_locked
def fix_issues(action, alias, code):
    base = get_config_content()
    report = get_models_data(base)
    if action not in ('fix', 'fix_all'):
        return False, '未知 action'
    try:
        data = model_manager.parse(base)
    except model_manager.ConfigError as error:
        return False, str(error)
    done, errs = 0, []
    for mdl in report['models']:
        for issue in mdl['effort_issues']:
            if issue.get('dismissed') or not issue.get('code') or issue['level'] == 'info':
                continue
            if action == 'fix' and (mdl['alias'] != alias or issue['code'] != code):
                continue
            if action == 'fix_all' and (issue['code'] == 'no_support_efforts' or not issue.get('fix')):
                continue
            err = _apply_issue_fix_data(data, mdl, issue)
            if err:
                errs.append(err)
            else:
                done += 1
    if not done:
        return (False, '；'.join(sorted(set(errs)))) if errs else (True, '没有需要修复的项')
    try:
        content = model_manager.dumps(data)
    except model_manager.ConfigError as error:
        return False, str(error)
    ok, msg = safe_apply_config(content, base_content=base)
    return ok, msg if not ok else '已修复 %d 项，会话内 /reload 生效' % done


# ---------------- 模式滑杆（主模型 + 挂件子代理档位） ----------------
MODES_PATH = os.path.join(KIMI_HOME, 'usage-dashboard', 'modes.json')
MODES_MIN, MODES_MAX, MODE_SUBS_MAX = 2, 6, 3
_AGENT_TOOLS = ('Agent', 'AgentSwarm')
_MODE_EFFORTS = ('', 'on', 'off') + EFFORT_LEVELS
# 空白模板：main/subagents 用 '' 占位，load_modes 时解析成该机器
# config.toml [models] 里的真实别名，不绑定任何具体环境。
DEFAULT_MODES = [
    {'name': '省钱单干', 'scene': '问答、查资料、小改动：最便宜的模型自己干，不派子代理',
     'main': '', 'subagents': [], 'effort': ''},
    {'name': '标准', 'scene': '日常编码：主模型规划 + 1 个挂件落地',
     'main': '', 'subagents': [], 'effort': ''},
    {'name': '协作', 'scene': '跨文件改动、排障：强主模型 + 2 个挂件分工',
     'main': '', 'subagents': [], 'effort': ''},
    {'name': '全力', 'scene': '大任务并行：强主模型 + 3 个挂件（实现 / 长上下文 / 极速）',
     'main': '', 'subagents': [], 'effort': ''},
]


def _resolve_mode_alias(alias, data, want_subs):
    """占位别名 '' → 该机器 config 里的真实别名。
    want_subs=True 时挑一个能作挂件的（有 tool_use）；否则拿 default_model 或第一个模型。"""
    if alias:
        return alias
    models = data.get('models') or {}
    if not models:
        return ''
    if want_subs:
        for a, m in models.items():
            caps = _effective(m, 'capabilities') or []
            if 'tool_use' in caps:
                return a
        return ''
    return data.get('default_model') or sorted(models.keys())[0]


def _normalize_default_modes(modes, data):
    """把 DEFAULT_MODES 里的 '' 占位解析成该机器真实别名；不动已有具体别名。"""
    out = []
    for m in modes:
        m = dict(m)
        m['main'] = _resolve_mode_alias(m.get('main'), data, False)
        m['subagents'] = [_resolve_mode_alias(s, data, True) for s in (m.get('subagents') or [])]
        m['subagents'] = [s for s in m['subagents'] if s]
        out.append(m)
    return out

def _model_manager_modes_bytes():
    try:
        with open(MODES_PATH, 'rb') as f:
            raw = f.read(1024 * 1024 + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise model_manager.ConfigError('无法读取模式预设，删除已禁用') from None
    if len(raw) > 1024 * 1024:
        raise model_manager.ConfigError('模式预设文件过大，删除已禁用')
    return raw


def _model_manager_modes_version():
    raw = _model_manager_modes_bytes()
    return 'missing' if raw is None else hashlib.sha256(raw).hexdigest()


def _model_manager_mode_references(data):
    raw = _model_manager_modes_bytes()
    if raw is None:
        stored = {}
    else:
        try:
            stored = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise model_manager.ConfigError('模式预设或恢复快照损坏，请先修复再删除模型') from None
    if not isinstance(stored, dict):
        raise model_manager.ConfigError('模式预设格式无效，删除已禁用')
    references = {}

    def add(alias, reason):
        if alias is not None and alias != '':
            model_manager._string(alias, '模式模型引用', True)
            references.setdefault(alias, reason)

    modes = stored.get('modes')
    if not isinstance(modes, list) or not MODES_MIN <= len(modes) <= MODES_MAX:
        modes = DEFAULT_MODES
    for mode in modes:
        if not isinstance(mode, dict):
            raise model_manager.ConfigError('模式预设格式无效，删除已禁用')
        main = mode.get('main')
        if main is not None:
            model_manager._string(main, '模式主模型')
        add(_resolve_mode_alias(main, data, False), '被模式滑杆预设引用，请先编辑档位解除引用')
        subs = mode.get('subagents') or []
        if not isinstance(subs, list):
            raise model_manager.ConfigError('模式子代理预设格式无效，删除已禁用')
        for alias in subs:
            model_manager._string(alias, '模式子代理模型')
            add(_resolve_mode_alias(alias, data, True), '被模式子代理预设引用，请先编辑档位解除引用')
    locked = stored.get('locked')
    if locked is not None:
        if not isinstance(locked, dict):
            raise model_manager.ConfigError('模式锁定信息无效，删除已禁用')
        if locked.get('locked'):
            add(locked.get('main'), '被锁定模式引用，请先解锁并解除档位引用')
            subs = locked.get('subagents') or []
            if not isinstance(subs, list):
                raise model_manager.ConfigError('模式锁定信息无效，删除已禁用')
            for alias in subs:
                add(alias, '被锁定模式子代理引用，请先解锁并解除档位引用')
    snap = stored.get('previous_snapshot')
    if snap is not None:
        if not isinstance(snap, dict):
            raise model_manager.ConfigError('模式恢复快照无效，删除已禁用')
        add(snap.get('default_model'), '被模式恢复快照引用，请先恢复或解除快照')
        secondary_text = snap.get('secondary_text') or ''
        if not isinstance(secondary_text, str):
            raise model_manager.ConfigError('模式恢复快照无效，删除已禁用')
        secondary = model_manager.parse(secondary_text)
        if set(secondary) - {'secondary_model'}:
            raise model_manager.ConfigError('模式恢复快照包含非预期配置，删除已禁用')
        for alias in model_manager.config_references(secondary):
            add(alias, '被模式恢复快照的子代理引用，请先恢复或解除快照')
    return references


def load_modes(data=None):
    try:
        with open(MODES_PATH, encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        # 文件整体坏（如尾部某字段双重转义写花）时，尽量从文本里抠出 modes
        # 数组保住用户档位，而不是整档回退默认——之前就是 previous_snapshot
        # 损坏让 load_modes 静默换回 DEFAULT_MODES，用户配的挂件「丢了一个」。
        d = _salvage_modes_file()
    modes = d.get('modes')
    if not isinstance(modes, list) or not (MODES_MIN <= len(modes) <= MODES_MAX):
        d['modes'] = json.loads(json.dumps(DEFAULT_MODES))
    # 空白模板：DEFAULT_MODES 里的 '' 占位在该机器 config 上解析成真实别名
    if data is None:
        data = _load_toml(get_config_content()) or {}
    d['modes'] = _normalize_default_modes(d['modes'], data)
    if not isinstance(d.get('pool_hints'), dict):
        d['pool_hints'] = {}
    return d


def _salvage_modes_file():
    """modes.json 解析失败时的兜底：从原始文本里抠出 "modes" 数组段单独
    json.loads，保住档位/mode 配置；抠不出才返回 {}（外层回退默认）。
    顺带把坏文件备份成 modes.json.corrupt-<ts> 便于排查。"""
    try:
        with open(MODES_PATH, encoding='utf-8', errors='replace') as f:
            raw = f.read()
    except Exception:
        return {}
    try:
        import shutil
        shutil.copyfile(MODES_PATH,
                        MODES_PATH + '.corrupt-' +
                        datetime.now().strftime('%Y%m%d%H%M%S'))
    except Exception:
        pass
    i = raw.find('"modes"')
    if i < 0:
        return {}
    j = raw.find('[', i)
    if j < 0:
        return {}
    # 从 '[' 起配平找数组终点（modes 元素是对象，字符串里不会有裸括号）
    depth, k, in_str, esc = 0, j, False, False
    while k < len(raw):
        c = raw[k]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    break
        k += 1
    try:
        modes = json.loads(raw[j:k + 1])
    except Exception:
        return {}
    if not isinstance(modes, list):
        return {}
    return {'modes': modes}


def save_modes(d):
    os.makedirs(os.path.dirname(MODES_PATH), exist_ok=True)
    safe_write(MODES_PATH, json.dumps(d, ensure_ascii=False, indent=2))


def _defined_aliases(data):
    return set((data.get('models') or {}).keys())


def validate_modes(modes, data):
    """校验档位列表，返回 (规范化列表, 错误)。别名必须存在于 config 的 [models]。"""
    if not isinstance(modes, list) or not (MODES_MIN <= len(modes) <= MODES_MAX):
        return None, '档位数量需在 %d~%d 之间' % (MODES_MIN, MODES_MAX)
    defined = _defined_aliases(data)
    out = []
    for i, m in enumerate(modes):
        if not isinstance(m, dict):
            return None, '第 %d 档格式错误' % (i + 1)
        name = str(m.get('name') or '').strip()[:20] or ('档位 %d' % (i + 1))
        scene = str(m.get('scene') or '').strip()[:120]
        main = str(m.get('main') or '').strip()
        if main not in defined:
            return None, '第 %d 档「%s」主模型 %s 不在 config 的 [models] 中' % (i + 1, name, main or '(空)')
        subs = m.get('subagents') or []
        if not isinstance(subs, list) or len(subs) > MODE_SUBS_MAX:
            return None, '第 %d 档「%s」挂件最多 %d 个' % (i + 1, name, MODE_SUBS_MAX)
        clean = []
        for s in subs:
            s = str(s or '').strip()
            if not s:
                continue
            if s == 'primary':
                return None, '第 %d 档：primary 是保留字，不能作挂件' % (i + 1)
            if s not in defined:
                return None, '第 %d 档「%s」挂件 %s 不在 config 的 [models] 中' % (i + 1, name, s)
            if s in clean:
                return None, '第 %d 档「%s」挂件 %s 重复' % (i + 1, name, s)
            clean.append(s)
        effort = str(m.get('effort') or '').strip()
        if effort not in _MODE_EFFORTS:
            return None, '第 %d 档 effort 只能是 %s' % (i + 1, '/'.join(e for e in _MODE_EFFORTS if e))
        out.append({'name': name, 'scene': scene, 'main': main, 'subagents': clean, 'effort': effort})
    return out, None


def apply_mode_to_text(content, mode, hints, tools_owned):
    """在完整配置对象上应用模式，返回 (完整 TOML, tools_owned 新值)。"""
    data = model_manager.parse(content)
    model_manager._set_default_model_data(data, mode['main'])
    old_sec = data.get('secondary_model', {})
    if isinstance(old_sec, str):
        old_sec = {}
    if not isinstance(old_sec, dict):
        raise model_manager.ConfigError('secondary_model 必须是表或模型别名')
    secondary = dict(old_sec)
    for key in ('default_model', 'force', 'models'):
        secondary.pop(key, None)
    subs = mode['subagents']
    if subs:
        secondary['default_model'] = subs[0]
        secondary['models'] = {alias: hints.get(alias) or alias for alias in subs}
    effort = mode.get('effort') or old_sec.get('default_effort') or ''
    if effort:
        secondary['default_effort'] = effort
    data['secondary_model'] = secondary

    tools = data.get('tools', {})
    if not isinstance(tools, dict):
        raise model_manager.ConfigError('tools 必须是表')
    disabled = tools.get('disabled', [])
    if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled):
        raise model_manager.ConfigError('tools.disabled 必须是字符串数组')
    disabled = list(disabled)
    owned = tools_owned
    if not subs:
        added = [tool for tool in _AGENT_TOOLS if tool not in disabled]
        if added:
            disabled += added
            owned = True
    elif tools_owned:
        disabled = [tool for tool in disabled if tool not in _AGENT_TOOLS]
        owned = False
    if disabled:
        tools['disabled'] = disabled
        data['tools'] = tools
    elif 'disabled' in tools:
        del tools['disabled']
        if not tools:
            data.pop('tools', None)
    return model_manager.dumps(data), owned


def _diff_outside(old, new, keys=('default_model', 'secondary_model', 'tools')):
    """模式改写只允许动 keys；其余解析结果必须完全一致。"""
    a = {k: v for k, v in old.items() if k not in keys}
    b = {k: v for k, v in new.items() if k not in keys}
    return model_manager.semantic_equal(a, b)


def _strict_toml(content):
    try:
        return model_manager.parse(content), None
    except model_manager.ConfigError as e:
        return None, str(e)


def detect_active_mode(modes, data):
    dm = data.get('default_model', '')
    sec = data.get('secondary_model') or {}
    if isinstance(sec, str):
        sec = {'default_model': sec}
    pool = list((sec.get('models') or {}).keys())
    disabled = set(((data.get('tools') or {}).get('disabled')) or [])
    agents_off = all(t in disabled for t in _AGENT_TOOLS)
    for i, m in enumerate(modes):
        if m.get('main') != dm:
            continue
        subs = m.get('subagents') or []
        if not subs:
            if agents_off:
                return i
            continue
        if not agents_off and sorted(pool) == sorted(subs) and sec.get('default_model') == subs[0]:
            return i
    return None


@_config_locked
def get_modes_data():
    content = get_config_content()
    data = _load_toml(content) or {}
    d = load_modes(data)
    sec = data.get('secondary_model') or {}
    if isinstance(sec, str):
        sec = {'default_model': sec}
    hints = d['pool_hints']
    changed = False
    for k, v in (sec.get('models') or {}).items():
        if isinstance(v, str) and v and hints.get(k) != v:
            hints[k] = v
            changed = True
    if changed:
        save_modes(d)
    pricing = load_pricing()
    models = []
    for alias, m in (data.get('models') or {}).items():
        caps = _effective(m, 'capabilities') or []
        models.append({'alias': alias, 'display_name': _effective(m, 'display_name') or alias,
                       'has_tools': 'tool_use' in caps, 'has_image': 'image_in' in caps,
                       'pricing': pricing.get(alias) or pricing.get(m.get('model', '')) or None})
    models.sort(key=lambda x: x['alias'])
    defined = _defined_aliases(data)
    undefined_pool = [k for k in (sec.get('models') or {}) if k not in defined]
    disabled = ((data.get('tools') or {}).get('disabled')) or []
    return {'modes': d['modes'], 'active': detect_active_mode(d['modes'], data),
            'last_applied': d.get('last_applied'),
            'locked': d.get('locked') or {'locked': False},
            'has_snapshot': bool(d.get('previous_snapshot')),
            'current': {'default_model': data.get('default_model', ''),
                        'secondary_default': sec.get('default_model', ''),
                        'pool': list((sec.get('models') or {}).keys()),
                        'agents_disabled': all(t in disabled for t in _AGENT_TOOLS)},
            'undefined_pool': undefined_pool,
            'models': models, 'subs_max': MODE_SUBS_MAX,
            'min': MODES_MIN, 'max': MODES_MAX}


def _snapshot_config(content):
    data = model_manager.parse(content)
    secondary = {key: data[key] for key in ('secondary_model',) if key in data}
    tools = {key: data[key] for key in ('tools',) if key in data}
    return {'default_model': data.get('default_model', ''),
            'secondary_text': model_manager.dumps(secondary),
            'tools_text': model_manager.dumps(tools),
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}


def apply_mode(index):
    ok, result = _apply_mode_config(index)
    if not ok:
        return False, result
    mode, old = result
    n = len(mode['subagents'])
    desc = '单干（已禁用 Agent/AgentSwarm）' if not n else '%d 个挂件' % n
    live = _apply_session_live(mode, old)
    extra = ('；%s' % live) if live else '，会话内 /reload 生效'
    return True, '已切到「%s」：主 %s · %s%s' % (mode['name'], mode['main'], desc, extra)


@_config_locked
def _apply_mode_config(index):
    content = get_config_content()
    old, err = _strict_toml(content)
    if err:
        return False, err
    d = load_modes(old)
    modes, err = validate_modes(d['modes'], old)
    if err:
        return False, err
    if not isinstance(index, int) or not (0 <= index < len(modes)):
        return False, '档位序号无效'
    mode = modes[index]
    hints = dict(d['pool_hints'])
    secondary = old.get('secondary_model', {})
    if isinstance(secondary, str):
        secondary = {}
    if not isinstance(secondary, dict) or not isinstance(secondary.get('models', {}), dict):
        return False, 'secondary_model 及其 models 必须是表或模型别名'
    for k, v in secondary.get('models', {}).items():
        if isinstance(v, str) and v:
            hints[k] = v
    for s in mode['subagents']:
        if not hints.get(s):
            mm = (old.get('models') or {}).get(s) or {}
            hints[s] = _effective(mm, 'display_name') or s
    try:
        new_content, owned = apply_mode_to_text(content, mode, hints, bool(d.get('tools_owned')))
    except model_manager.ConfigError as error:
        return False, str(error)
    new, err = _strict_toml(new_content)
    if err:
        return False, '生成的配置无法解析，已拒绝：%s' % err
    if not _diff_outside(old, new):
        return False, '改写波及了无关配置段，已拒绝（config 未改动）'
    if new.get('default_model') != mode['main']:
        return False, '改写校验失败：default_model 未生效'
    if not d.get('previous_snapshot'):
        d['previous_snapshot'] = _snapshot_config(content)
    ok, msg = safe_apply_config(new_content, base_content=content)
    if not ok:
        return False, msg
    d['pool_hints'] = hints
    d['tools_owned'] = owned
    d['last_applied'] = {'index': index, 'name': mode['name'],
                         'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    save_modes(d)
    return True, (mode, old)


# ---------------- 模式滑杆：活动会话即时同步 ----------------
# apply 成功后尝试让当前会话/发送框立刻换档（不依赖 /reload）：
#   1) POST {server}/api/v1/sessions/{active}/profile  把活动会话的 agent_config.model
#      改成主模型（发送框模型下拉随之切换；新建会话走 default_model 已改）
#   2) POST {server}/api/v1/config  提交 secondary_model，daemon 侧子代理池立即换血
# 失败一律降级：config.toml 已写好，/reload 后完全一致，只在上报文案里提示。
def _server_endpoint():
    """从 ~/.kimi-code/server/instances/*.json 找在跑的桌面端 daemon（heartbeat 最新）。"""
    inst = os.path.join(KIMI_HOME, 'server', 'instances')
    best = None
    now = time.time()
    try:
        for fn in os.listdir(inst):
            if not fn.endswith('.json'):
                continue
            try:
                info = json.loads(io.open(os.path.join(inst, fn), encoding='utf-8').read())
            except Exception:
                continue
            host, port = info.get('host'), info.get('port')
            if not host or not port:
                continue
            hb = float(info.get('heartbeat_at') or info.get('started_at') or 0)
            if best is None or hb > best[0]:
                best = (hb, str(host), int(port))
    except Exception:
        return None
    if not best or now - best[0] / 1000.0 > 120:
        return None
    return 'http://%s:%d' % (best[1], best[2])


def _server_token():
    try:
        tok = io.open(os.path.join(KIMI_HOME, 'server.token'), encoding='utf-8').read().strip()
        return tok or None
    except Exception:
        return None


def _api_json(method, url, token, body=None, timeout=6):
    req = urllib.request.Request(url, method=method)
    req.add_header('Authorization', 'Bearer ' + token)
    data = None
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        data = json.dumps(body).encode('utf-8')
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            d = json.loads(r.read().decode('utf-8', 'replace'))
            return d if isinstance(d, dict) and d.get('code') in (0, '0') else None
    except Exception:
        return None


def _api_data(payload):
    d = payload.get('data')
    return d if isinstance(d, dict) else {}


def _thinking_for_model(model_item, prefer):
    """按桌面端 store 的 thinking 语义映射：off/on/effort；无能力模型 → 'off'。"""
    caps = model_item.get('capabilities') or []
    se = model_item.get('support_efforts') or []
    can_think = 'thinking' in caps or 'always_thinking' in caps or bool(se)
    if not can_think or prefer == 'off':
        return 'off'
    if prefer == 'on' or prefer == '':
        if se:
            return model_item.get('default_effort') or se[len(se) // 2]
        return 'on'
    return prefer if prefer in se else (model_item.get('default_effort') or (se[len(se) // 2] if se else 'on'))


def _apply_session_live(mode, old_data):
    """尽力把档位落到运行中的会话：会话模型 + daemon secondary_model。返回提示文本。"""
    base = _server_endpoint()
    token = _server_token()
    if not base or not token:
        return '后台服务未找到，配置已落盘（/reload 生效）'
    notes = []

    want_sec = mode['subagents']
    cur = _api_data(_api_json('GET', base + '/api/v1/config', token) or {})
    sec = cur.get('secondary_model')
    if isinstance(sec, dict):
        # 只推 daemon 认识的键（model/defaultModel/defaultEffort/models 会被忽略并污染）
        sub = {}
        if want_sec:
            sub['defaultModel'] = want_sec[0]
            sub['models'] = {s: s for s in want_sec}
            if mode.get('effort'):
                sub['defaultEffort'] = mode['effort']
        else:
            sub['models'] = {}
        r = _api_json('POST', base + '/api/v1/config', token, {'secondary_model': sub})
        if r is None:
            notes.append('子代理池未能即时下发（/reload 后生效）')
        else:
            # 校验写回了
            ok = _api_data(_api_json('GET', base + '/api/v1/config', token) or {}).get('secondary_model') or {}
            live = ok.get('models') or {}
            miss = [s for s in want_sec if s not in live]
            extra = [k for k in live if k not in want_sec]
            if not miss and not extra and (not want_sec or ok.get('defaultModel') == want_sec[0]):
                notes.append('子代理池已即时更新')
            else:
                notes.append('子代理池校验偏差（/reload 修正）')
    else:
        notes.append('config 接口异常，子代理池 /reload 后生效')

    sess = _api_data(_api_json('GET', base + '/api/v1/sessions?page_size=1&sort=updated_desc', token) or {})
    items = sess.get('items') or []
    if not items:
        notes.append('无活动会话，发送框将在新会话自动应用')
    else:
        sid = items[0].get('id')
        ok, note = _session_set_model(base, token, sid, mode)
        notes.append(note)
    return '；'.join(notes)


def _session_set_model(base, token, sid, mode):
    """把活动会话的 agent_config.model 改成 mode['main'] 对应的 daemon 模型 id。
    返回 (是否成功, 提示文本)。供 apply 和锁定守护共用。"""
    models = _api_data(_api_json('GET', base + '/api/v1/models', token) or {}).get('items') or []
    mid = mode['main']
    # 发送框要 daemon 的 provider/model id，config 里可能存的是 alias
    mi = next((m for m in models if m.get('model') == mid or m.get('id') == mid), None)
    if mi is None:
        mi = next((m for m in models if (m.get('model') or '').endswith('/' + mid.split('/')[-1])), None)
    if mi is None:
        return False, '发送框模型 %s 不在 daemon 模型表（未切换）' % mid
    send_id = mi.get('id') or mi.get('model')
    thinking = _thinking_for_model(mi, mode.get('effort') or '')
    body = {'agent_config': {'model': send_id, 'thinking': thinking}}
    r = _api_json('POST', base + '/api/v1/sessions/%s/profile' % sid, token, body)
    if r is None:
        return False, '会话模型未即时切换（/reload 或手动选档）'
    return True, '发送框模型已同步为 %s' % (mi.get('display_name') or send_id)


# ---------------- 模式锁定 ----------------
# 锁定 = 切到指定档位 + 守护线程盯活动会话，凡是把会话模型改成档位主模型
# 之外的操作都被改回。挂件仍按档位下发（secondary_model 已在 apply 时写入）。
_LOCK_STATE = threading.Lock()
_LOCK_THREAD = [None]          # 正在跑的守护线程
_LOCK_STOP = threading.Event() # 守护退出信号
_LOCK_GEN = [0]                # 守护代数，防止旧线程写新档


def _lock_daemon_target():
    """取当前活动会话 id + 该会话现在的 model；供守护比对。"""
    base = _server_endpoint()
    token = _server_token()
    if not base or not token:
        return None, None, None
    sess = _api_data(_api_json('GET', base + '/api/v1/sessions?page_size=1&sort=updated_desc', token) or {})
    items = sess.get('items') or []
    if not items:
        return base, token, None
    return base, token, items[0]


def _locked_main_id(base, token, main_alias):
    """把档位 main 别名解析成 daemon 认识的 model id；解析不到返回 None。"""
    models = _api_data(_api_json('GET', base + '/api/v1/models', token) or {}).get('items') or []
    mi = next((m for m in models if m.get('model') == main_alias or m.get('id') == main_alias), None)
    if mi is None:
        mi = next((m for m in models if (m.get('model') or '').endswith('/' + main_alias.split('/')[-1])), None)
    return (mi.get('id') or mi.get('model')) if mi else None


def _lock_guard(gen):
    """每 ~1.5s 检查活动会话模型；偏离锁定档位主模型就改回。
    daemon 失联/无活动会话就静默重试；收到 _LOCK_STOP 或代数变了就退出。"""
    while not _LOCK_STOP.is_set() and _LOCK_GEN[0] == gen:
        time.sleep(1.5)
        if _LOCK_STOP.is_set() or _LOCK_GEN[0] != gen:
            break
        try:
            d = load_modes()
            locked = d.get('locked')
            if not (isinstance(locked, dict) and locked.get('locked')):
                break                       # 已被解锁
            modes = d.get('modes') or []
            li = locked.get('index', -1)
            if not (0 <= li < len(modes)):
                continue                    # 锁定档位已被编辑删掉，继续守到用户解锁
            mode = modes[li]
            base, token, active = _lock_daemon_target()
            if not base or not active:
                continue                    # daemon 没起 / 无会话，下轮再看
            sid = active.get('id')
            cur = ((active.get('agent_config') or {}).get('model') or '')
            want_id = _locked_main_id(base, token, mode['main'])
            if not want_id or cur == want_id:
                continue
            # 活动会话被改成了别的模型 → 改回档位主模型
            ok, _ = _session_set_model(base, token, sid, mode)
            if ok:
                log('mode-lock: session %s model %s -> %s' % (sid, cur, mode['main']))
        except Exception:
            pass                            # 静默重试，守护不能崩


def _start_lock_guard():
    """启动（或重启）守护线程；幂等。"""
    with _LOCK_STATE:
        _LOCK_GEN[0] += 1
        _LOCK_STOP.set()                     # 先让旧线程退出
        _LOCK_STOP.clear()                   # 再为新线程清信号（旧线程读的是自己那代 gen，不受影响）
        t = threading.Thread(target=_lock_guard, args=(_LOCK_GEN[0],), daemon=True)
        _LOCK_THREAD[0] = t
        t.start()


def _stop_lock_guard():
    with _LOCK_STATE:
        _LOCK_GEN[0] += 1
        _LOCK_STOP.set()
        _LOCK_THREAD[0] = None


def lock_mode(index):
    """锁到第 index 档：apply + 记 locked + 起守护。"""
    ok, msg = apply_mode(index)
    if not ok:
        return False, msg
    with CONFIG_LOCK:
        data = _load_toml(get_config_content()) or {}
        d = load_modes(data)
        modes, err = validate_modes(d['modes'], data)
        if err or not isinstance(index, int) or not 0 <= index < len(modes):
            return False, err or '档位序号无效'
        d['locked'] = {'locked': True, 'index': index,
                       'name': modes[index]['name'],
                       'main': modes[index]['main'],
                       'subagents': list(modes[index]['subagents']),
                       'effort': modes[index].get('effort', ''),
                       'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        save_modes(d)
    _start_lock_guard()
    return True, '%s；已锁定' % msg


@_config_locked
def unlock_mode():
    """解锁：停守护 + 清 locked；不改当前模型。"""
    d = load_modes()
    was = d.get('locked')
    d.pop('locked', None)
    save_modes(d)
    _stop_lock_guard()
    name = (was or {}).get('name', '') if isinstance(was, dict) else ''
    return True, '已解锁%s' % (('（原档：%s）' % name) if name else '')


@_config_locked
def restore_mode_snapshot():
    content = get_config_content()
    old, err = _strict_toml(content)
    if err:
        return False, err
    d = load_modes(old)
    snap = d.get('previous_snapshot')
    if not snap:
        return False, '没有可恢复的快照（尚未用滑杆切换过）'
    try:
        if not isinstance(snap, dict):
            raise model_manager.ConfigError('模式恢复快照必须是对象')
        new = model_manager.parse(content)
        if snap.get('default_model'):
            model_manager._set_default_model_data(new, snap['default_model'])
        for key, field in (('secondary_model', 'secondary_text'), ('tools', 'tools_text')):
            saved = model_manager.parse(snap.get(field) or '')
            if set(saved) - {key}:
                raise model_manager.ConfigError('模式恢复快照包含非预期配置')
            if key in saved:
                new[key] = saved[key]
            else:
                new.pop(key, None)
        new_content = model_manager.dumps(new)
    except model_manager.ConfigError as error:
        return False, '快照还原后无法解析或完整序列化，已拒绝：%s' % error
    if not _diff_outside(old, new):
        return False, '还原波及了无关配置段，已拒绝（config 未改动）'
    ok, msg = safe_apply_config(new_content, base_content=content)
    if not ok:
        return False, msg
    d.pop('previous_snapshot', None)
    d['tools_owned'] = False
    d['last_applied'] = None
    # 恢复快照时清锁：锁定已失去参照档位，守护若继续跑会把会话又改回锁档主模型
    if d.pop('locked', None):
        _stop_lock_guard()
    save_modes(d)
    return True, '已恢复到使用滑杆之前的配置（%s 的快照），会话内 /reload 生效' % snap.get('time', '')


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
    'TUNNEL_AUTH_FAILED', 'START_CANCELLED', 'SERVER_TOKEN_UNAVAILABLE',
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


def _relay_config_validate(body):
    """写侧严格校验（返回错误文案或 ''）。token 可留空表示清除/回退默认。

    host：非空 hostname/IP（去空白小写后 ≤253，字母数字点横线冒号方括号）；
    tunnel_port/public_port：1-65535 整数；token：≤256 字符串。"""
    if 'host' in body:
        h = body['host']
        if not isinstance(h, str) or not relay_config._clean_host(h):
            return '中继服务器地址无效'
    for k in ('tunnel_port', 'public_port'):
        if k in body:
            v = body[k]
            ok = isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535
            if not ok:
                try:
                    ok = 1 <= int(str(v).strip()) <= 65535
                except Exception:
                    ok = False
            if not ok:
                return '中继端口需在 1-65535 之间'
    if 'token' in body:
        t = body['token']
        if not isinstance(t, str) or len(t.strip()) > relay_config.TOKEN_MAX:
            return '密钥格式无效'
    return ''


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
        # relay 配置是 daemon 本地文件（usage-dashboard/relay.json），
        # 不经 worker 代理；GET 永不回显 token 明文，POST 只写不落回执。
        if method == 'GET' and path == _MOBILE_PREFIX + '/relay/config':
            _mobile_send(handler, 200,
                         {'relay': relay_config.redacted(relay_config.load(KIMI_HOME))},
                         origin)
        elif method == 'POST' and path == _MOBILE_PREFIX + '/relay/config':
            body = _mobile_read_body(handler)
            if set(body) - {'host', 'tunnel_port', 'public_port', 'token'}:
                raise ValueError('请求参数不被允许')
            # 校验：host 非空 hostname/IP、端口 1-65535、token 限长；
            # 缺省字段继承现有值（便于部分更新）。
            cur = relay_config.load(KIMI_HOME)
            err = _relay_config_validate(body)
            if err:
                raise ValueError(err)
            nxt = dict(cur)
            for k in ('host', 'tunnel_port', 'public_port', 'token'):
                if k in body:
                    nxt[k] = body[k]
            saved = relay_config.save(KIMI_HOME, nxt)
            _mobile_send(handler, 200,
                         {'success': True, 'relay': relay_config.redacted(saved)},
                         origin)
        elif method == 'GET' and path == _MOBILE_PREFIX + '/status':
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
            if mode == 'internet':
                allowed = {'owner_origin', 'mode', 'relay_consent', 'consent_version'}
            elif mode == 'relay':
                allowed = {'owner_origin', 'mode'}
            else:
                allowed = {'owner_origin', 'mode', 'address'}
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
        if self.path.split('?')[0].startswith('/api/model-manager'):
            self.send_header('Cache-Control', 'no-store')
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
        if path == '/api/model-manager':
            if (self.headers.get(_CONTROL_HEADER) or '').strip() != '1':
                return self._deny()
            return self._json(get_model_manager_data())
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
        elif path == '/api/modes':
            self._json(get_modes_data())
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
            if path == '/api/plugin/uninstall':
                if set(req) != {'confirm'} or req.get('confirm') is not True:
                    return self._json({'success': False, 'message': '卸载确认参数不正确'}, 400)
                payload, status = uninstall_plugin()
                try:
                    self._json(payload, status)
                    self.wfile.flush()
                finally:
                    if payload.get('success'):
                        _UNINSTALL_EXIT.set()
                        threading.Timer(0.3, _force_exit_after_uninstall).start()
                return

            if path == '/api/model-manager/catalog':
                payload, status = get_model_manager_catalog(req)
                return self._json(payload, status)

            if path in ('/api/model-manager/provider', '/api/model-manager/model', '/api/model-manager/batch'):
                payload, status = save_model_manager(path, req)
                return self._json(payload, status)

            if path == '/api/set-default':
                ok, msg = _config_change(lambda base: set_default_model_in_text(base, req.get('alias')))
                return self._json({'success': ok, 'message': msg}, 200 if ok else 400)

            if path == '/api/toggle-capability':
                ok, msg = _config_change(lambda base: toggle_capability_in_text(
                    base, req.get('alias'), req.get('capability'), req.get('enabled', True)))
                return self._json({'success': ok, 'message': msg}, 200 if ok else 400)

            if path == '/api/update-model':
                alias, updates = req.get('alias'), req.get('updates')
                ok, msg = _config_change(lambda base: model_manager.update_model(base, alias, updates))
                note = _global_effort_note(alias, updates['default_effort']) if ok and updates.get('default_effort') else ''
                return self._json({'success': ok, 'message': msg, 'note': note}, 200 if ok else 400)

            if path == '/api/add-model':
                item = {key: req[key] for key in ('alias', 'provider', 'model', 'display_name',
                        'max_context_size', 'capabilities', 'support_efforts', 'default_effort') if key in req}
                item.setdefault('display_name', item.get('alias', ''))
                item.setdefault('max_context_size', 1000000)
                item.setdefault('capabilities', ['image_in', 'thinking', 'tool_use'])
                item.setdefault('support_efforts', list(model_manager.EFFORT_LEVELS))
                item.setdefault('default_effort', 'high')
                ok, msg = _config_change(lambda base: model_manager.upsert_model(base, None, item))
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
                ok, msg = fix_issues(action, alias, code)
                return self._json({'success': ok, 'message': msg}, 200 if ok else 400)

            if path == '/api/modes/save':
                with CONFIG_LOCK:
                    data = _load_toml(get_config_content()) or {}
                    modes, err = validate_modes(req.get('modes'), data)
                    if err:
                        return self._json({'success': False, 'message': err}, 400)
                    d = load_modes(data)
                    d['modes'] = modes
                    save_modes(d)
                return self._json({'success': True, 'message': '已保存 %d 个档位' % len(modes),
                                   'data': get_modes_data()})

            if path == '/api/modes/reset':
                with CONFIG_LOCK:
                    data = _load_toml(get_config_content()) or {}
                    d = load_modes(data)
                    d['modes'] = json.loads(json.dumps(DEFAULT_MODES))
                    save_modes(d)
                return self._json({'success': True, 'message': '档位已恢复为内置预设',
                                   'data': get_modes_data()})

            if path == '/api/modes/apply':
                idx = req.get('index')
                if isinstance(idx, bool) or not isinstance(idx, int):
                    return self._json({'success': False, 'message': 'index 必须是整数'}, 400)
                # 锁定时滑到别的档 = 解锁旧档 + 锁新档（locked 状态保持，档位换）
                d0 = load_modes()
                locked = d0.get('locked')
                if isinstance(locked, dict) and locked.get('locked'):
                    ok, msg = lock_mode(idx)
                else:
                    ok, msg = apply_mode(idx)
                return self._json({'success': ok, 'message': msg, 'data': get_modes_data()},
                                  200 if ok else 400)

            if path == '/api/modes/lock':
                idx = req.get('index')
                if isinstance(idx, bool) or not isinstance(idx, int):
                    return self._json({'success': False, 'message': 'index 必须是整数'}, 400)
                ok, msg = lock_mode(idx)
                return self._json({'success': ok, 'message': msg, 'data': get_modes_data()},
                                  200 if ok else 400)

            if path == '/api/modes/unlock':
                ok, msg = unlock_mode()
                return self._json({'success': ok, 'message': msg, 'data': get_modes_data()},
                                  200 if ok else 400)

            if path == '/api/modes/restore':
                ok, msg = restore_mode_snapshot()
                return self._json({'success': ok, 'message': msg, 'data': get_modes_data()},
                                  200 if ok else 400)

            if path == '/api/auto-enable-all':
                ok, msg = _config_change(auto_enable_all_in_text)
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
        except model_manager.ConfigError as e:
            return self._json({'success': False, 'message': str(e)}, 400)
        except Exception:
            return self._json({'success': False, 'message': '处理异常，操作未完成'}, 500)

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
    """本机探活。先用 bind 测试排除空闲端口：部分环境（安全软件拦截回环）下连接未监听
    端口要 ~2s 才回 RST，直接 urlopen 会把每次 tick 拖慢数秒而超过 hook 超时；
    bind 测试不受该延迟影响。_port_free 定义见下方。"""
    if _port_free():
        return False
    for _ in range(2):
        try:
            with _urlopen('http://127.0.0.1:%d/api/status' % PORT, timeout=1) as r:
                d = json.loads(r.read().decode())
                return d.get('name') == 'kimi-code-usage'
        except Exception:
            time.sleep(0.1)
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
    if uninstall_requested() or UNINSTALL_BEGIN.is_set():
        return False
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
        ], capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW)
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
                               capture_output=True, timeout=10, creationflags=_NO_WINDOW)
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
    if uninstall_requested():
        return 0
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
    # 服务重启自愈：modes.json 里还挂着锁就把守护线程拉回来
    try:
        _d = load_modes()
        if isinstance(_d.get('locked'), dict) and _d['locked'].get('locked'):
            _start_lock_guard()
            log('mode-lock guard restored (index=%s)' % _d['locked'].get('index'))
    except Exception:
        pass

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
    while not _UNINSTALL_EXIT.is_set():
        try:
            sync_widget_asset()
            ensure_injection()
            collect_once()
            n += 1
            if n % 15 == 0:
                save_state()
        except Exception as e:
            log('loop error: %r' % e)
        _UNINSTALL_EXIT.wait(2)
    if _UNINSTALL_EXIT.is_set():
        _force_exit_after_uninstall()


def _force_exit_after_uninstall():
    os._exit(0)


def uninstall_plugin():
    """写卸载保护标记、弹出 uninstall.cmd 清理窗口，随后本服务自行退出。"""
    if not UNINSTALL_LOCK.acquire(timeout=1.0):
        return {'success': False, 'message': '另一个卸载操作进行中，请稍后重试'}, 409
    try:
        script = os.path.join(SCRIPT_DIR, 'uninstall.cmd')
        if not os.path.isfile(script):
            raise ValueError('uninstall.cmd missing')
        UNINSTALL_BEGIN.set()
        os.makedirs(os.path.dirname(UNINSTALL_MARKER), exist_ok=True)
        with open(UNINSTALL_MARKER, 'w', encoding='utf-8') as f:
            json.dump({'plugin': 'kimi-code-usage',
                       'requested_at': datetime.now().isoformat()}, f)
        # 新控制台窗口跑卸载脚本：杀进程、清注入、删 usage-dashboard 与插件目录
        flags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0x00000010)
        subprocess.Popen('cmd.exe /c ""%s""' % script, shell=False,
                         cwd=os.environ.get('TEMP', PLUGIN_ROOT),
                         creationflags=flags, close_fds=True)
        _mobile_begin_shutdown()   # detach；worker 由卸载脚本按命令行核实后结束
        return {'success': True,
                'message': '卸载清理窗口已打开，完成后本服务退出；请按窗口提示操作。'}, 202
    except Exception as e:
        log('uninstall launch failed: %r' % e)
        try:
            if os.path.lexists(UNINSTALL_MARKER):
                os.remove(UNINSTALL_MARKER)
        except Exception:
            pass
        UNINSTALL_BEGIN.clear()
        return {'success': False, 'message': '无法启动卸载脚本：%s' % str(e)[:200]}, 500
    finally:
        UNINSTALL_LOCK.release()


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
