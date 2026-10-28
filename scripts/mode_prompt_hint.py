# -*- coding: utf-8 -*-
import json
import os
import re
import sys
import urllib.request

from plugin_settings import widgets_enabled

SESSION = re.compile(r'^session_[A-Za-z0-9_-]{8,100}$')
ALIAS = re.compile(r'^[A-Za-z0-9_.:/+-]{1,120}$')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def _kimi_home():
    home = os.environ.get('KIMI_CODE_HOME') or os.environ.get('KIMI_HOME')
    if home and os.path.isdir(home):
        return home
    default = os.path.join(os.path.expanduser('~'), '.kimi-code')
    return default if os.path.isdir(default) else ''


def main():
    if not widgets_enabled():
        return
    print('挂件功能已开启：档位挂件是可选模型池，不是任务数或并发实例上限。'
          '独立工作项按需在同一轮并发派送多个 Agent，或用 AgentSwarm 多 items；同一模型可复用。'
          '本对话选档后仅使用该档白名单且已在宿主目录注册的挂件；默认挂件符合时可省略 model。'
          '锁定时禁止 model=primary、与主模型相同的挂件、fork 或恢复模型未知的子代理；单干档不派件。'
          '简单任务、先后依赖或冲突写入保持单干或串行，结果齐后由主代理汇总。'
          '此指引仅在挂件开关开启期间适用，关闭后不再使用历史挂件指引。')
    try:
        payload = json.load(sys.stdin)
        sid = payload.get('session_id', payload.get('sessionId'))
        home = _kimi_home()
        if not isinstance(sid, str) or not SESSION.fullmatch(sid) or not home:
            return
        modes_path = os.path.join(home, 'usage-dashboard', 'modes.json')
        if not os.path.exists(modes_path):
            return
        with open(modes_path, encoding='utf-8') as source:
            data = json.load(source)
        import mode_policy
        chosen = mode_policy.effective(data, sid)
        if chosen is None:
            return
        wanted = chosen.get('subagents') or []
        if not isinstance(wanted, list) or any(not isinstance(a, str) or not ALIAS.fullmatch(a) for a in wanted):
            return
        with urllib.request.urlopen('http://127.0.0.1:39281/api/modes', timeout=1.5) as response:
            current = json.loads(response.read(65536).decode('utf-8'))['current']
        pool = current.get('pool') or []
        if not isinstance(pool, list):
            return
        locked = chosen.get('locked') is True
        main_model = chosen.get('main') if locked else None
        available = [a for a in wanted if a in pool and not (locked and a == main_model)]
        if not wanted:
            print('本对话档位为单干：不要派发 Agent、AgentSwarm 或 TowerSpawn。')
        elif not available or current.get('agents_disabled') or locked and current.get('force'):
            print('本对话没有可安全派送的挂件：可能与主模型相同、未加入宿主目录或被全局强制模型覆盖。不要使用主模型冒充挂件。')
        else:
            names = ', '.join(available)
            print(('本对话已锁定：禁止 model=primary、fork 或恢复模型未知的子代理。' if locked else '')
                  + '本对话可用挂件模型：' + names + '。这是一组可用模型，不是实例数量上限。'
                  '当任务包含独立工作项时，在同一轮按需并发派送多个 Agent，或用 AgentSwarm 多 items；'
                  '允许多个子代理实例重复选用同一个可用模型。不要仅因当前档位只有一种挂件模型就串行执行。'
                  '不改变其它对话的模型。')
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return


if __name__ == '__main__':
    main()
