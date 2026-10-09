# -*- coding: utf-8 -*-
"""显式开启手机连接时补建 server.token；不覆盖，不把秘密写进诊断。"""
import os
import secrets
import stat
import tempfile

from mobile_security import _set_protected_dacl

_MAX_BYTES = 4096


class CredentialUnavailable(Exception):
    def __init__(self):
        super().__init__('SERVER_TOKEN_UNAVAILABLE')


def _identity(info):
    return info.st_dev, info.st_ino


def _regular(info):
    return (stat.S_ISREG(info.st_mode)
            and not getattr(info, 'st_file_attributes', 0) & 0x400)


def _home(kimi_home):
    # 主目录 junction 是受支持的入口；凭据文件自身的链接不是。
    home = os.path.realpath(kimi_home)
    info = os.lstat(home)
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise CredentialUnavailable()
    return home, _identity(info)


def _check_home(home, identity):
    info = os.lstat(home)
    if (not stat.S_ISDIR(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or _identity(info) != identity):
        raise CredentialUnavailable()


def _read(path):
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    if not _regular(before):
        raise CredentialUnavailable()
    if os.name != 'nt' and (before.st_mode & 0o077 or before.st_uid != os.getuid()):
        raise CredentialUnavailable()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as f:
        opened = os.fstat(f.fileno())
        if not _regular(opened) or _identity(opened) != _identity(before):
            raise CredentialUnavailable()
        raw = f.read(_MAX_BYTES + 1)
    after = os.lstat(path)
    if (not _regular(after) or _identity(after) != _identity(opened)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or len(raw) > _MAX_BYTES):
        raise CredentialUnavailable()
    token = raw.decode('utf-8', 'strict').strip()
    # Bearer 必须是单行 ASCII；保留旧 token 的内容/格式，不强制新格式。
    if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise CredentialUnavailable()
    return token


def read_server_token(kimi_home):
    try:
        home, identity = _home(kimi_home)
        token = _read(os.path.join(home, 'server.token'))
        _check_home(home, identity)
        return token
    except (OSError, UnicodeError, ValueError):
        raise CredentialUnavailable() from None


def ensure_server_token(kimi_home):
    """只在缺失时创建。原子硬链接发布确保任何竞态下都不覆盖现有凭据。"""
    tmp = None
    try:
        home, identity = _home(kimi_home)
        path = os.path.join(home, 'server.token')
        token = _read(path)
        _check_home(home, identity)
        if token is not None:
            return token
        fd, tmp = tempfile.mkstemp(prefix='.server-token-', suffix='.tmp', dir=home)
        with os.fdopen(fd, 'wb') as f:
            # 空临时文件先限制权限，随后才写入秘密。
            _set_protected_dacl(tmp, False)
            f.write(secrets.token_urlsafe(32).encode('ascii'))
            f.flush()
            os.fsync(f.fileno())
        _check_home(home, identity)
        try:
            os.link(tmp, path)  # 不支持原子无覆盖发布的文件系统 fail closed。
        except FileExistsError:
            pass  # 另一进程胜出；只使用其已发布的 token。
        token = _read(path)
        _check_home(home, identity)
        if token is None:
            raise CredentialUnavailable()
        return token
    except (OSError, UnicodeError, ValueError):
        raise CredentialUnavailable() from None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            except OSError:
                raise CredentialUnavailable() from None
