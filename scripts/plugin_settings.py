# -*- coding: utf-8 -*-
import json
import os


def settings_path(kimi_home=None):
    home = (kimi_home or os.environ.get('KIMI_CODE_HOME') or
            os.environ.get('KIMI_HOME') or os.path.join(os.path.expanduser('~'), '.kimi-code'))
    return os.path.join(home, 'usage-dashboard', 'settings.json')


def load_settings(kimi_home=None):
    try:
        with open(settings_path(kimi_home), encoding='utf-8') as source:
            data = json.load(source)
    except FileNotFoundError:
        return {'widgets_enabled': True}
    except (OSError, ValueError, UnicodeError):
        return {'widgets_enabled': False}
    enabled = data.get('widgets_enabled', True) if isinstance(data, dict) else False
    return {'widgets_enabled': enabled if type(enabled) is bool else False}


def widgets_enabled(kimi_home=None):
    return load_settings(kimi_home)['widgets_enabled']