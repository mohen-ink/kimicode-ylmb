# -*- coding: utf-8 -*-


def resolve(data, record):
    if record is None:
        return None
    if isinstance(record, dict) and isinstance(record.get('main'), str) and isinstance(record.get('subagents'), list):
        return dict(record)
    index = record.get('index') if isinstance(record, dict) else record
    modes = data.get('modes') or []
    if type(index) is not int or not 0 <= index < len(modes) or not isinstance(modes[index], dict):
        return None
    result = dict(modes[index])
    result['index'] = index
    result['locked'] = isinstance(record, dict) and record.get('locked') is True
    return result


def session_record(data, sid):
    locks = data.get('session_locks') or {}
    records = data.get('session_modes') or {}
    lock = locks.get(sid) if isinstance(locks, dict) else None
    return lock if isinstance(lock, dict) and lock.get('locked') else records.get(sid) if isinstance(records, dict) else None


def global_record(data):
    lock = data.get('locked')
    return lock if isinstance(lock, dict) and lock.get('locked') else data.get('last_applied')


def effective(data, sid):
    record = session_record(data, sid)
    return resolve(data, record if record is not None else global_record(data))


def independent(data, sid):
    return session_record(data, sid) is not None


def snapshot(mode, index=None):
    value = {key: mode.get(key, '') for key in ('name', 'scene', 'main', 'effort')}
    value['subagents'] = list(mode.get('subagents') or [])
    value['index'] = index if index is not None else mode.get('index')
    if mode.get('locked'):
        value['locked'] = True
    return value


def pin_records(data):
    for field in ('locked', 'last_applied'):
        record = data.get(field)
        value = resolve(data, record)
        if value is not None:
            data[field] = dict(record, **snapshot(value)) if isinstance(record, dict) else snapshot(value)
    for field in ('session_modes', 'session_locks'):
        for sid, record in (data.get(field) or {}).items():
            value = resolve(data, record)
            if value is not None:
                data[field][sid] = dict(record, **snapshot(value)) if isinstance(record, dict) else snapshot(value)


def index_for(data, mode):
    if mode is None:
        return None
    for index, candidate in enumerate(data.get('modes') or []):
        if isinstance(candidate, dict) and all(candidate.get(k, '') == mode.get(k, '') for k in ('name', 'main', 'subagents', 'effort')):
            return index
    return None


def required_subagents(data, extra=None):
    modes = [resolve(data, global_record(data)), extra]
    for sid in set(data.get('session_modes') or {}) | set(data.get('session_locks') or {}):
        modes.append(resolve(data, session_record(data, sid)))
    result = []
    for mode in modes:
        for alias in (mode or {}).get('subagents') or []:
            if alias not in result:
                result.append(alias)
    return result