# -*- coding: utf-8 -*-
import json
import os
import re
import sys
import urllib.request

from plugin_settings import widgets_enabled

SESSION_ID = re.compile(r'^session_[A-Za-z0-9_-]{8,100}$')


def deny(reason):
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': reason,
    }}, ensure_ascii=True))


def _kimi_home():
    home = os.environ.get('KIMI_CODE_HOME') or os.environ.get('KIMI_HOME')
    if home and os.path.isdir(home):
        return home
    default = os.path.join(os.path.expanduser('~'), '.kimi-code')
    return default if os.path.isdir(default) else ''


def main():
    if not widgets_enabled():
        return
    try:
        call = json.load(sys.stdin)
    except (OSError, ValueError):
        deny('无法读取子代理调用信息；本对话挂件限制无法核对，请勿派送。')
        return
    if not isinstance(call, dict):
        deny('子代理调用信息格式异常，请勿派送。')
        return
    tool = call.get('tool_name', call.get('toolName'))
    if tool not in ('Agent', 'AgentSwarm', 'TowerSpawn'):
        return
    sid = call.get('session_id', call.get('sessionId'))
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        return              # 会话标识认不出：fail-open，不当成全禁止派件的理由
    home = _kimi_home()
    if not home:
        deny('无法读取档位配置，不允许绕过本对话挂件限制。')
        return
    path = os.path.join(home, 'usage-dashboard', 'modes.json')
    if not os.path.exists(path):
        return              # 未安装/未选档：没有本对话策略，放行
    try:
        with open(path, encoding='utf-8') as source:
            data = json.load(source)
    except (OSError, ValueError):
        deny('无法读取本对话档位，不允许派送挂件。')
        return
    if not isinstance(data, dict):
        deny('档位数据格式异常，不允许派送挂件。')
        return
    record = data.get('session_modes') or {}
    lock = data.get('session_locks') or {}
    if not isinstance(record, dict) or not isinstance(lock, dict):
        deny('本对话挂件配置格式异常，不允许派送。')
        return
    locked = isinstance(lock.get(sid), dict) and lock[sid].get('locked') is True
    selected = lock.get(sid) or record.get(sid)
    if selected is None:
        return
    index = selected.get('index') if isinstance(selected, dict) else selected
    modes = data.get('modes')
    if type(index) is not int or not isinstance(modes, list) or not 0 <= index < len(modes):
        deny('本对话档位无效，不能确认挂件权限。')
        return
    mode = modes[index]
    allowed = (selected.get('subagents') if isinstance(selected, dict) and
               selected.get('locked') is True else mode.get('subagents') if isinstance(mode, dict) else None)
    if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
        deny('本对话挂件白名单无效，不能派送。')
        return
    if not allowed:
        deny('本对话选择单干档，禁止派送子代理。')
        return
    params = call.get('tool_input', call.get('toolInput'))
    if not isinstance(params, dict):
        deny('无法确认子代理调用参数，请勿派送。')
        return
    if locked and (params.get('fork') is True or params.get('resume')
                   or params.get('resume_agent_ids')):
        deny('本对话已锁定，不能恢复或继承模型未知的子代理；请新建挂件并选择锁定档位的模型。')
        return
    pool = []
    secondary_default = ''
    forced = False
    try:
        with urllib.request.urlopen('http://127.0.0.1:39281/api/modes', timeout=1.5) as response:
            live = json.loads(response.read(65536).decode('utf-8'))
        current = live['current']
        if isinstance(current.get('pool'), list):
            pool = current['pool']
        if isinstance(current.get('secondary_default'), str):
            secondary_default = current['secondary_default']
        forced = current.get('force') is True
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        pass                # 服务不可达：fail-open，跳过宿主目录核对，白名单仍生效
    requested = params.get('model')
    if locked and forced:
        deny('本对话已锁定，但宿主强制使用全局挂件模型；不能安全派送子代理。')
        return
    if tool == 'TowerSpawn' and params.get('kind') == 'reviewer':
        if locked or mode.get('main') not in allowed:
            deny('本对话不允许主模型作为审查挂件。')
        return
    if requested == 'primary':
        if locked or mode.get('main') not in allowed:
            deny('本对话已锁定，禁止使用主模型作为子代理；请选择允许的挂件模型。')
        return
    model = requested or secondary_default
    if locked and model and model == selected.get('main'):
        deny('本对话已锁定，挂件模型与主模型相同；禁止把主模型当子代理。')
        return
    if not model:
        return              # 未指定模型且服务不可达：交给宿主默认行为
    if model not in allowed:
        deny('本对话仅允许挂件 ' + '、'.join(allowed) + '；请选择其中一个模型。')
    elif pool and model not in pool:
        deny('宿主挂件目录尚未包含 ' + model + '，本对话无法派送该模型；不能仅靠会话档位补全全局目录。')


if __name__ == '__main__':
    main()
