# -*- coding: utf-8 -*-
"""
Kimi Code 用量面板 · 手机连接的本机持久化（纯标准库，Python>=3.8）

文件位于 <KIMI_HOME>/usage-dashboard/，原子写入并尽力收紧 ACL：
  mobile-password.json    {password}         手机登录密码（调试用明文，尽力收紧 ACL）
  mobile-sessions.json    {sessions}         已登录设备会话（sid -> created/expires/pwv）
  mobile-resume.json      {mode, address}    随桌面端自动恢复的连接意图（仅 lan / relay）
  mobile-relay-tid.json   {tunnel_id}        中继隧道 ID（重连时经 X-Relay-Resume 复用）

读取失败 / 损坏 / 字段非法一律当作未配置；写入失败返回 False，不抛异常。
daemon 写密码与恢复意图，worker 写会话与隧道 ID；显式停止后 daemon 清理会话。
"""
import hashlib
import json
import math
import os
import re
import tempfile
import time

PASSWORD_MAX = 128
SESSIONS_MAX = 8
_FILE_MAX_BYTES = 65536
_SID_RE = re.compile(r'[A-Za-z0-9_.~-]{1,256}')
_TID_RE = re.compile(r'[A-Za-z0-9_-]{4,32}')
_ADDR_RE = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}')

_PASSWORD_FILE = 'mobile-password.json'
_SESSIONS_FILE = 'mobile-sessions.json'
_RESUME_FILE = 'mobile-resume.json'
_TID_FILE = 'mobile-relay-tid.json'


def _path(kimi_home, name):
    return os.path.join(kimi_home, 'usage-dashboard', name)


def _load(kimi_home, name):
    try:
        with open(_path(kimi_home, name), 'rb') as f:
            raw = f.read(_FILE_MAX_BYTES + 1)
        if len(raw) > _FILE_MAX_BYTES:
            return None
        data = json.loads(raw.decode('utf-8'))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _save(kimi_home, name, obj):
    path = _path(kimi_home, name)
    tmp = None
    try:
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.mobile-', suffix='.tmp', dir=folder)
        try:
            from mobile_security import _set_protected_dacl
            _set_protected_dacl(tmp, False)
        except Exception:
            pass
        with os.fdopen(fd, 'wb') as f:
            f.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        for _ in range(3):
            try:
                os.replace(tmp, path)
                tmp = None
                return True
            except OSError:
                time.sleep(0.05)
        return False
    except Exception:
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _remove(kimi_home, name):
    try:
        os.remove(_path(kimi_home, name))
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ---------------- 登录密码 ----------------
def password_path(kimi_home):
    return _path(kimi_home, _PASSWORD_FILE)


def password_stamp(kimi_home):
    """文件指纹（变化即视为密码已改）；文件不存在返回 None。"""
    try:
        st = os.stat(password_path(kimi_home))
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def load_password(kimi_home):
    d = _load(kimi_home, _PASSWORD_FILE)
    pw = d.get('password') if d else None
    if not isinstance(pw, str) or not 0 < len(pw) <= PASSWORD_MAX:
        return ''
    try:
        pw.encode('utf-8')
    except UnicodeEncodeError:
        return ''
    return pw


def save_password(kimi_home, password):
    """设置密码；空串 = 清除。任意字符、任意强度，仅限长。"""
    if not isinstance(password, str) or len(password) > PASSWORD_MAX:
        return False
    try:
        password.encode('utf-8')
    except UnicodeEncodeError:
        return False
    if not password:
        return _remove(kimi_home, _PASSWORD_FILE)
    return _save(kimi_home, _PASSWORD_FILE, {'password': password})


def password_version(password):
    """会话绑定的密码版本；密码变化后旧会话随之失效。无密码为空串。"""
    if not password:
        return ''
    return hashlib.sha256(password.encode('utf-8')).hexdigest()[:16]


# ---------------- 设备会话 ----------------
def load_sessions(kimi_home):
    data = _load(kimi_home, _SESSIONS_FILE)
    raw = data.get('sessions') if data else None
    out = {}
    if not isinstance(raw, dict):
        return out
    for sid, rec in raw.items():
        if (not isinstance(sid, str) or not _SID_RE.fullmatch(sid)
                or not isinstance(rec, dict)):
            continue
        created, expires = rec.get('created'), rec.get('expires')
        pwv = rec.get('pwv', '')
        if (not _num(created) or not _num(expires)
                or not isinstance(pwv, str) or len(pwv) > 64):
            continue
        out[sid] = {'created': float(created), 'expires': float(expires), 'pwv': pwv}
    if len(out) > SESSIONS_MAX:
        keep = sorted(out, key=lambda k: out[k]['created'], reverse=True)[:SESSIONS_MAX]
        out = {k: out[k] for k in keep}
    return out


def save_sessions(kimi_home, sessions):
    return _save(kimi_home, _SESSIONS_FILE, {'sessions': sessions})


def clear_sessions(kimi_home):
    return _remove(kimi_home, _SESSIONS_FILE)


# ---------------- 自动恢复意图 ----------------
def load_resume(kimi_home):
    d = _load(kimi_home, _RESUME_FILE)
    if not d:
        return None
    mode = d.get('mode')
    if mode not in ('lan', 'relay'):
        return None
    address = d.get('address', '')
    if mode == 'lan':
        if not isinstance(address, str) or not _ADDR_RE.fullmatch(address):
            return None
    else:
        address = ''
    return {'mode': mode, 'address': address}


def save_resume(kimi_home, mode, address=''):
    if mode not in ('lan', 'relay'):
        return False
    if mode != 'lan' or not isinstance(address, str):
        address = ''
    return _save(kimi_home, _RESUME_FILE, {'mode': mode, 'address': address})


def clear_resume(kimi_home):
    return _remove(kimi_home, _RESUME_FILE)


# ---------------- 中继隧道 ID ----------------
def load_relay_tid(kimi_home):
    d = _load(kimi_home, _TID_FILE)
    tid = d.get('tunnel_id') if d else None
    return tid if isinstance(tid, str) and _TID_RE.fullmatch(tid) else ''


def save_relay_tid(kimi_home, tid):
    if not isinstance(tid, str) or not _TID_RE.fullmatch(tid):
        return False
    return _save(kimi_home, _TID_FILE, {'tunnel_id': tid})


def clear_relay_tid(kimi_home):
    return _remove(kimi_home, _TID_FILE)
