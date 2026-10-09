# -*- coding: utf-8 -*-
"""
Kimi Code 用量面板 · 自更新助手

由 service.py 在收到 /api/update/apply 后拉起：
  1. 等待旧服务退出（让出 39281 端口）
  2. 解压下载好的更新包，覆盖插件目录（跳过本机生成物：日志/用量数据/机器路径配置）
  3. 重新拉起 service.py 常驻服务

用法: updater.py <plugin_root> <zip_path>
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

PORT = 39281
# 更新时绝不覆盖的本机生成物（相对插件根）
KEEP = {
    'scripts/desktop_path.txt',
    'scripts/service.log',
    'scripts/updater.log',
    'assets/kimi-usage.json',
}
# 同步删除是"上游已移除该文件"的判据，必须只按本机白名单放行、不看压缩包里有什么。
# 自 3.3.3 起上游正式包**已经分发**手机运行时（mobile_*.py、kimi-mobile-api.js、
# assets/vendor/ 的连接器许可证与二维码库）：它们在包里时走正常覆盖，不经此白名单。
# 白名单仍然保留——updater.py 是"旧版本 + 新版本"两侧共同信任的引导程序，删除判据
# 只能来自这份写死的本机约定；一旦按包内容反推"已移除"，遇到不含该组文件的包就会
# 把完整运行树静默删掉，worker、隧道、手机接口随之失效且无法由更新包恢复。
LOCAL_ONLY_KEEP = frozenset((
    'assets/kimi-mobile-api.js',
    'assets/kimi-remote-api.js',
    'assets/kimi-remote-qr.js',
    'assets/kimi-remote-widget.js',
    'assets/kimi-remote-widget-live.css',
    'scripts/mobile_bridge.py',
    'scripts/mobile_tunnel.py',
    'scripts/mobile_relay.py',
    'scripts/mobile_usage.py',
    'scripts/mobile_worker.py',
    'scripts/mobile_credentials.py',
    'scripts/mobile_security.py',
    # relay_config 被 service.py 顶层 import——若包不含手机运行时（"partial" 为空、
    # 完整性闸门放行）就会被同步删除，导致用量服务直接起不来，故必须保护。
    'scripts/relay_config.py',
))
LOCAL_ONLY_KEEP_PREFIX = ('docs/', 'assets/vendor/')
# 不完整更新包（缺核心运行文件）一律拒绝：宁可回到旧版本，也不留下半套运行树。
PACKAGE_REQUIRED = frozenset((
    'kimi.plugin.json',
    'scripts/service.py',
    'scripts/bootstrap.cmd',
    'scripts/scanner.py',
    'scripts/updater.py',
    'assets/kimi-usage-widget.js',
))
# 移动端运行时是成组交付的：3.3.3 起上游正式包已含这一组，但若只给了一部分——
# 例如只更新了 bridge 而没给 worker——那就是不完整包，覆盖后同步删除会清掉剩下的
# 运行时文件，把插件变成不可启动状态，故必须整体拒绝。
# 3.4.0 起中继改用自建 Python 中继，组内相应去掉 frp 许可证、加入 relay_config.py
# （service.py 顶层 import）与 relay_server.py（VPS 侧随包分发）。
MOBILE_RUNTIME_GROUP = frozenset((
    'scripts/mobile_bridge.py',
    'scripts/mobile_tunnel.py',
    'scripts/mobile_relay.py',
    'scripts/mobile_usage.py',
    'scripts/mobile_worker.py',
    'scripts/mobile_credentials.py',
    'scripts/mobile_security.py',
    'scripts/relay_config.py',
    'scripts/relay_server.py',
    'assets/kimi-mobile-api.js',
))
# 退出码：3 = 旧服务未退出，4 = 更新包损坏/应用失败，5 = 本地预览自保护，6 = 更新包不完整
GUARD_LOCAL_PREVIEW = 5
PACKAGE_INCOMPLETE = 6


class IncompletePackage(ValueError):
    """更新包缺少运行必需文件；必须在使用前拒绝，不能留半套运行树。"""


def log(msg):
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'updater.log')
        with open(p, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


def wait_port_free(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        try:
            s.settimeout(1)
            s.connect(('127.0.0.1', PORT))
            s.close()
            time.sleep(0.5)
        except OSError:
            try:
                s.close()
            except Exception:
                pass
            return True
    return False


def find_root(extract_dir):
    """zip 顶层通常是 <repo>-<branch>/ 或 <repo>-<branch>/plugin/，找到含 kimi.plugin.json 的那层。"""
    for base, _dirs, files in os.walk(extract_dir):
        if 'kimi.plugin.json' in files:
            return base
    return extract_dir


def sync_delete_allowed(rel):
    """同步删除白名单：只清上游确实移除过的普通文件。

    被白名单挡下的文件即便不在更新包里也保留（内容原样）。白名单按本机约定写成
    常量，不看压缩包里有什么 —— updater.py 是"旧版本 + 新版本"两侧共同信任的
    引导程序，删除判据一旦改由包内容反推，缺该组文件的包（例如仍按 3.3.3 之前
    形态构建的旧包）就会把本地运行时静默删掉。
    """
    if rel in KEEP or rel in LOCAL_ONLY_KEEP:
        return False
    if rel.startswith('.git'):
        return False
    for prefix in LOCAL_ONLY_KEEP_PREFIX:
        if rel.startswith(prefix):
            return False
    return True


def apply_zip(zip_path, plugin_root):
    tmp = tempfile.mkdtemp(prefix='kimi-usage-update-')
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        src = find_root(tmp)
        remote_files = {}
        for base, _dirs, files in os.walk(src):
            rel = os.path.relpath(base, src)
            for fn in files:
                r = fn if rel == '.' else os.path.join(rel, fn).replace('\\', '/')
                if r not in KEEP:
                    remote_files[r] = os.path.join(base, fn)
        # 完整性闸门：先看清包里有什么，再决定动不动本地文件。
        # 缺文件或只给半个移动端运行时的"半套包"会先覆盖再用同步删除清掉剩下的
        # 运行时文件，把插件变成不可启动状态 —— 故此处直接拒绝，不写任何文件。
        present = set(remote_files)
        missing = sorted(PACKAGE_REQUIRED - present)
        if missing:
            raise IncompletePackage('missing: %s' % ', '.join(missing))
        partial = MOBILE_RUNTIME_GROUP & present
        if partial and partial != MOBILE_RUNTIME_GROUP:
            raise IncompletePackage('partial mobile runtime: %s'
                                    % ', '.join(sorted(MOBILE_RUNTIME_GROUP - partial)))
        copied = 0
        for r, path in remote_files.items():
            dst = os.path.join(plugin_root, r)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(path, dst)
            copied += 1
        # 同步删除：远端已移除的文件本地也清掉（保留本机生成物、本地专属运行时与 .git 元数据）
        removed = 0
        for base, _dirs, files in os.walk(plugin_root):
            if '.git' in base.replace('\\', '/').split('/'):
                continue
            rel = os.path.relpath(base, plugin_root)
            for fn in files:
                r = fn if rel == '.' else os.path.join(rel, fn).replace('\\', '/')
                if r in remote_files or not sync_delete_allowed(r):
                    continue
                try:
                    os.remove(os.path.join(base, fn))
                    removed += 1
                except OSError:
                    pass
        # 清掉 pyc 缓存，防新代码被旧字节码遮蔽
        shutil.rmtree(os.path.join(plugin_root, 'scripts', '__pycache__'), ignore_errors=True)
        log('applied %d files, removed %d stale (from %s)' % (copied, removed, src))
        return copied
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def service_offline():
    """端口空闲才说明旧服务确实退了。

    拒绝路径（本地预览、半套包）上游服务可能还活着 —— service.py 拉起 updater 后约
    0.8s 才退出。此时无条件 relaunch 会多出一个实例抢 39281 端口，反而把原本健康的
    服务挤掉，所以拒绝路径统一先探端口。
    """
    if wait_port_free(timeout=3):
        return True
    log('old service still listening; skip relaunch')
    return False


def relaunch(plugin_root):
    py = sys.executable
    cand = os.path.join(os.path.dirname(py), 'pythonw.exe')
    if os.path.exists(cand):
        py = cand
    for alt in (r'C:\Program Files\python\pythonw.exe', r'C:\Program Files\Python313\pythonw.exe',
                r'C:\Program Files\Python312\pythonw.exe', r'C:\Program Files\Python311\pythonw.exe'):
        if not os.path.exists(py) or 'Microsoft\\WindowsApps' in py:
            if os.path.exists(alt):
                py = alt
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0x00000008) | getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
    svc = os.path.join(plugin_root, 'scripts', 'service.py')
    subprocess.Popen([py, svc], cwd=plugin_root, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, close_fds=True)
    log('service relaunched via %s' % py)


def read_manifest(plugin_root):
    """读插件清单；None 表示缺失/损坏/不是对象。"""
    try:
        with open(os.path.join(plugin_root, 'kimi.plugin.json'), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def local_build_blocked(plugin_root):
    """updater 自保护的纵深防御层：本地预览构建一律拒绝自更新。

    调用方 service.py 已有同样的拦截，但 updater.py 是被"旧版本 + 新版本"两侧
    共同信任的独立进程，一旦手工拉起或调用方被替换/回退，就会直接覆盖运行树。
    故这里必须自己 fail-closed：清单损坏、缺失、无版本号、带 -local 后缀或
    localPreview 标记，都视为本地预览，宁可不动手也不误更新。
    """
    d = read_manifest(plugin_root)
    if not d:
        return 'manifest unavailable'
    version = str(d.get('version') or '')
    if not version:
        return 'manifest without version'
    if d.get('localPreview') is True:
        return 'localPreview'
    if '-local' in version:
        return 'local version %s' % version
    return None


def main():
    if len(sys.argv) < 3:
        return 2
    plugin_root, zip_path = sys.argv[1], sys.argv[2]
    log('update start: root=%s zip=%s' % (plugin_root, zip_path))
    local = local_build_blocked(plugin_root)
    if local:
        # fail closed：一个文件都不动；旧服务若已退出则拉回，仍占端口就让它继续跑
        log('local preview build (%s); refusing self-update' % local)
        if service_offline():
            relaunch(plugin_root)
        return GUARD_LOCAL_PREVIEW
    if not wait_port_free():
        log('old service did not exit in time; aborting')
        return 3
    try:
        apply_zip(zip_path, plugin_root)
    except IncompletePackage as e:
        log('refused incomplete update package: %s' % e)
        if service_offline():
            relaunch(plugin_root)
        return PACKAGE_INCOMPLETE
    except Exception as e:
        log('apply failed: %r' % e)
        relaunch(plugin_root)   # 失败也要把旧服务拉回来
        return 4
    relaunch(plugin_root)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    log('update done')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log('fatal: %r' % e)
        sys.exit(1)
