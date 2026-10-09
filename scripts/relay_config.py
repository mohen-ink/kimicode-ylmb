# -*- coding: utf-8 -*-
"""
Kimi Code 用量面板 · 手机中继（relay）配置持久化（纯标准库，Python>=3.8）

配置文件 <KIMI_HOME>/usage-dashboard/relay.json，字段：
  {host, tunnel_port, public_port, token}

- 读侧（worker / daemon 共用）：sanitize() 把任意外部输入收敛成一份规范化
  配置（host 去空白小写、端口限 1-65535、token 限长）；load() 对不存在/
  损坏/含非法字段的文件一律回退默认值——绝不因坏配置拒绝启动（fail-open
  到内置默认，与「未配置」等价）。字段单独非法（如端口越界）只回退该字段。
- 写侧（daemon）：validate_*() 严格校验用户输入，error() 抛出的中文错误
  文案经 daemon 校验后才落盘；token 永不回显明文（读取只给 token_set）。
- 原子写入 safe_write()：先写 .tmp 再 os.replace；失败静默（与 service.py
  现有持久化语义一致——写失败不阻断主流程，但 API 会如实回报）。

任何进程都可用本模块读配置；worker 侧 RelayClient 构造参数由调用方从
load() 的结果拆出，本模块不感知 mobile_relay。
"""
import json
import os
import time

# ---------------- 默认值（未配置回退；host 空串 = 未配置） ----------------
DEFAULTS = {
    'host': '',            # '' = 未配置；须在面板「中继服务器配置」填入服务器地址
    'tunnel_port': 48213,
    'public_port': 47961,
    'token': '',           # '' = 本机未配置；由 relay.json / KIMI_RELAY_TOKEN 提供
}

HOST_MAX = 253
TOKEN_MAX = 256

_RELAY_FILENAME = 'relay.json'


def config_path(kimi_home):
    """KIMI_HOME → relay.json 路径。"""
    return os.path.join(kimi_home, 'usage-dashboard', _RELAY_FILENAME)


def _is_port(v):
    """1-65535 整数；bool 不是端口。"""
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535


def _clean_host(v):
    """host：非空 hostname/IP，≤253 字符；合法返回小写去空白值，否则 ''。"""
    if not isinstance(v, str):
        return ''
    s = v.strip().lower()
    if not s or len(s) > HOST_MAX:
        return ''
    # 只允许 hostname/IPv4/IPv6 字面量字符：字母数字点横线冒号方括号
    for ch in s:
        if not (ch.isalnum() or ch in '.-_:[]'):
            return ''
    if ' ' in s or '\t' in s:
        return ''
    return s


def _clean_token(v):
    """token：限长去首尾空白；None/非 str → ''。"""
    if not isinstance(v, str):
        return ''
    s = v.strip()
    if len(s) > TOKEN_MAX:
        return ''
    return s


def _clean_port(v, default):
    """端口字段：合法 int 1-65535 原样；否则回退 default。"""
    if _is_port(v):
        return int(v)
    try:
        # 容忍 JSON 里的数字字符串（手编辑友好）
        n = int(str(v).strip())
        if 1 <= n <= 65535:
            return n
    except Exception:
        pass
    return int(default)


def sanitize(d):
    """把任意 dict 收敛成规范化配置 dict（未知键丢弃，非法字段回退默认）。

    返回 {'host','tunnel_port','public_port','token'}；token 保留原始大小写
    （密钥区分大小写），只做限长。
    """
    out = dict(DEFAULTS)
    if not isinstance(d, dict):
        return out
    h = _clean_host(d.get('host'))
    if h:
        out['host'] = h
    out['tunnel_port'] = _clean_port(d.get('tunnel_port'), DEFAULTS['tunnel_port'])
    out['public_port'] = _clean_port(d.get('public_port'), DEFAULTS['public_port'])
    out['token'] = _clean_token(d.get('token'))
    return out


def load(kimi_home):
    """读 relay.json；不存在/损坏/非 dict → 全部默认值。字段级回退见 sanitize。"""
    try:
        with open(config_path(kimi_home), encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        d = {}
    return sanitize(d)


def safe_write(path, text):
    """原子写：先写 <path>.tmp 再 os.replace；失败静默（同 service.safe_write）。"""
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


def save(kimi_home, cfg):
    """校验后落盘。cfg 经 sanitize 收敛；返回落盘的规范化 dict。"""
    cfg = sanitize(cfg)
    safe_write(config_path(kimi_home), json.dumps(cfg, ensure_ascii=False, indent=2))
    return cfg


def redacted(cfg):
    """API 回显用：永不回显 token 明文，只给 token_set。"""
    cfg = sanitize(cfg)
    return {
        'host': cfg['host'],
        'tunnel_port': cfg['tunnel_port'],
        'public_port': cfg['public_port'],
        'token_set': bool(cfg['token']),
    }


def public_origin(cfg):
    """规范化配置 → 公网 origin（'http://<host>:<public_port>'）。"""
    cfg = sanitize(cfg)
    return 'http://%s:%d' % (cfg['host'], cfg['public_port'])


def public_host_header(cfg):
    """规范化配置 → 'host:public_port'（桥 Host/Origin 校验用）。"""
    cfg = sanitize(cfg)
    return '%s:%d' % (cfg['host'], cfg['public_port'])
