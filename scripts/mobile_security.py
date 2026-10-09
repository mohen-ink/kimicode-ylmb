# -*- coding: utf-8 -*-
"""手机运行时共享的最小权限设置；不依赖 bridge/worker，不输出凭据。"""
import ctypes
import os
import re

_SID_RE = re.compile(r'S-\d+(?:-\d+)+\Z')


def _current_user_sid():
    """从当前进程 token 取真实 SID；独立 DLL 对象及完整句柄签名。"""
    if os.name != 'nt':
        return None
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ptr = ctypes.c_void_p
    kernel.GetCurrentProcess.restype = ptr
    kernel.CloseHandle.argtypes = [ptr]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ptr]
    kernel.LocalFree.restype = ptr
    adv.OpenProcessToken.argtypes = [ptr, ctypes.c_ulong, ctypes.POINTER(ptr)]
    adv.OpenProcessToken.restype = ctypes.c_int
    adv.GetTokenInformation.argtypes = [ptr, ctypes.c_int, ptr, ctypes.c_ulong,
                                       ctypes.POINTER(ctypes.c_ulong)]
    adv.GetTokenInformation.restype = ctypes.c_int
    adv.ConvertSidToStringSidW.argtypes = [ptr, ctypes.POINTER(ptr)]
    adv.ConvertSidToStringSidW.restype = ctypes.c_int
    token = ptr()
    try:
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008,
                                    ctypes.byref(token)):
            return None
        n = ctypes.c_ulong()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(n))
        if not n.value:
            return None
        buf = ctypes.create_string_buffer(n.value)
        if not adv.GetTokenInformation(token, 1, buf, n, ctypes.byref(n)):
            return None
        sid = ctypes.cast(buf, ctypes.POINTER(ptr)).contents
        text = ptr()
        if not sid or not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        try:
            return ctypes.wstring_at(text)
        finally:
            kernel.LocalFree(text)
    except (OSError, ValueError):
        return None
    finally:
        if token.value:
            kernel.CloseHandle(token)


def _acl_user():
    if not os.environ.get('USERNAME'):
        raise PermissionError('ACL 授权主体不可用')
    sid = _current_user_sid()
    if not sid or not _SID_RE.fullmatch(sid):
        raise PermissionError('ACL 授权主体不可用')
    return '*' + sid


def _acl_sid():
    return _acl_user()[1:]


def _set_protected_dacl(path, is_dir=False):
    """一次替换 DACL：真实用户 SID + SYSTEM，断继承；失败不降级。"""
    if os.name != 'nt':
        os.chmod(path, 0o700 if is_dir else 0o600)
        return
    sid = _acl_sid()
    flags = 'OICI' if is_dir else ''
    sddl = 'D:P(A;%s;FA;;;%s)(A;%s;FA;;;SY)' % (flags, sid, flags)
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    ptr = ctypes.c_void_p
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ptr), ptr]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    adv.GetSecurityDescriptorDacl.argtypes = [ptr, ctypes.POINTER(ctypes.c_int),
                                             ctypes.POINTER(ptr),
                                             ctypes.POINTER(ctypes.c_int)]
    adv.GetSecurityDescriptorDacl.restype = ctypes.c_int
    adv.SetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_int,
                                        ctypes.c_ulong, ptr, ptr, ptr, ptr]
    adv.SetNamedSecurityInfoW.restype = ctypes.c_ulong
    kernel.LocalFree.argtypes = [ptr]
    kernel.LocalFree.restype = ptr
    sd = ptr()
    try:
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(sd), None) or not sd.value:
            raise PermissionError('ACL 设置失败')
        present, defaulted, dacl = ctypes.c_int(), ctypes.c_int(), ptr()
        if (not adv.GetSecurityDescriptorDacl(sd, ctypes.byref(present),
                                             ctypes.byref(dacl),
                                             ctypes.byref(defaulted))
                or not present.value or not dacl.value):
            raise PermissionError('ACL 设置失败')
        if adv.SetNamedSecurityInfoW(path, 1, 0x80000004,
                                    None, None, dacl, None) != 0:
            raise PermissionError('ACL 设置失败')
    finally:
        if sd.value:
            kernel.LocalFree(sd)
