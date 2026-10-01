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
    """zip 顶层通常是 <repo>-<branch>/，找到含 kimi.plugin.json 的那层。"""
    for base, _dirs, files in os.walk(extract_dir):
        if 'kimi.plugin.json' in files:
            return base
    return extract_dir


def apply_zip(zip_path, plugin_root):
    tmp = tempfile.mkdtemp(prefix='kimi-usage-update-')
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        src = find_root(tmp)
        copied = 0
        remote_files = set()
        for base, _dirs, files in os.walk(src):
            rel = os.path.relpath(base, src)
            for fn in files:
                r = fn if rel == '.' else os.path.join(rel, fn).replace('\\', '/')
                if r in KEEP:
                    continue
                remote_files.add(r)
                dst = os.path.join(plugin_root, r)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(base, fn), dst)
                copied += 1
        # 同步删除：远端已移除的文件本地也清掉（保留本机生成物与 .git 元数据）
        removed = 0
        for base, _dirs, files in os.walk(plugin_root):
            if '.git' in base.replace('\\', '/').split('/'):
                continue
            rel = os.path.relpath(base, plugin_root)
            for fn in files:
                r = fn if rel == '.' else os.path.join(rel, fn).replace('\\', '/')
                if r in KEEP or r in remote_files or r.startswith('.git'):
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


def main():
    if len(sys.argv) < 3:
        return 2
    plugin_root, zip_path = sys.argv[1], sys.argv[2]
    log('update start: root=%s zip=%s' % (plugin_root, zip_path))
    if not wait_port_free():
        log('old service did not exit in time; aborting')
        return 3
    try:
        apply_zip(zip_path, plugin_root)
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
