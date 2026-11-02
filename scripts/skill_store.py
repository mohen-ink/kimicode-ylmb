# -*- coding: utf-8 -*-
"""Skill 只读扫描：按 Kimi Code 的目录优先级发现 SKILL.md，解析 frontmatter 并汇报来源。

扫描根（与原生实现一致，按覆盖优先级从高到低）：
  用户级  ~/.kimi-code/skills, ~/.agents/skills
  项目级  <root>/.kimi-code/skills, <root>/.agents/skills   （<root> = 向上找到 .git 的目录）
插件自带 skills/ 目录作为第三种来源单列，由调用方传入。
"""
import hashlib
import os
import re
import shutil
import tempfile

MAX_DEPTH = 8
MAX_SKILLS = 400
FRONTMATTER_MAX = 65536
BODY_PREVIEW = 20000

_NAME_RE = re.compile(r'^\s*name\s*:\s*(.+?)\s*$', re.MULTILINE)
_DESC_RE = re.compile(r'^\s*description\s*:\s*(.+?)\s*$', re.MULTILINE)
# 官方字段，三种写法都认（与原生 iYe 映射一致）
DISABLE_RE = re.compile(r'^\s*(?:disable-model-invocation|disable_model_invocation)\s*:\s*true\s*$',
                        re.MULTILINE | re.IGNORECASE)
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


class SkillPathError(ValueError):
    pass


def _normalize_display(text):
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1]
    return value.strip()


def parse_frontmatter(text):
    """解析 YAML frontmatter 的 name / description 与 disable-model-invocation。

    返回 (metadata, body, error)。缺 frontmatter 时 metadata 为空、error 有值——
    与原生实现一致：SKILL.md 必须以 --- 开头。header 保留原文，供回写时不动其它字段。
    """
    stripped = text.lstrip('\ufeff')
    lines = stripped.split('\n')
    if not lines or lines[0].strip() != '---':
        return {}, stripped.strip(), '缺少 YAML frontmatter（文件必须以 --- 开头）'
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == '---':
            end = index
            break
    if end is None:
        return {}, stripped.strip(), 'frontmatter 没有闭合的 ---'
    header = '\n'.join(lines[1:end])
    body = '\n'.join(lines[end + 1:]).strip()
    metadata = {'header': header}
    name = _NAME_RE.search(header)
    if name:
        metadata['name'] = _normalize_display(name.group(1))
    description = _DESC_RE.search(header)
    if description:
        metadata['description'] = _normalize_display(description.group(1))
    metadata['disabled'] = DISABLE_RE.search(header) is not None
    error = ''
    if not metadata.get('name'):
        error = 'frontmatter 缺少必填字段 name'
    elif not metadata.get('description'):
        error = 'frontmatter 缺少必填字段 description'
    return metadata, body, error


def _is_skill_dir(entries):
    return 'SKILL.md' in entries


def _scan_root(root, source, seen, out, depth=0):
    if depth > MAX_DEPTH or len(out) >= MAX_SKILLS:
        return
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return
    listing = set(entries)
    if _is_skill_dir(listing):
        _record(root, os.path.join(root, 'SKILL.md'), os.path.basename(root), source, seen, out)
        return
    for entry in entries:
        if entry == 'node_modules' or entry.startswith('.'):
            continue
        child = os.path.join(root, entry)
        if not os.path.isdir(child):
            continue
        if _is_skill_dir(_safe_listing(child)):
            _record(child, os.path.join(child, 'SKILL.md'), entry, source, seen, out)
        else:
            _scan_root(child, source, seen, out, depth + 1)


def _safe_listing(path):
    try:
        return set(os.listdir(path))
    except OSError:
        return set()


def _record(directory, skill_md, dir_name, source, seen, out):
    if len(out) >= MAX_SKILLS:
        return
    try:
        real = os.path.realpath(directory).replace('\\', '/')
    except OSError:
        real = directory.replace('\\', '/')
    if real in seen:
        return
    seen.add(real)
    key = hashlib.sha256(real.encode('utf-8')).hexdigest()[:16]
    try:
        size = os.path.getsize(skill_md)
        with open(skill_md, 'rb') as handle:
            raw = handle.read(FRONTMATTER_MAX)
        text = raw.decode('utf-8', 'replace')
    except OSError:
        out.append({'name': dir_name, 'description': '', 'dir': directory, 'path': skill_md,
                    'key': key, 'source': source, 'valid': False, 'error': '无法读取 SKILL.md',
                    'disabled': False, 'writable': False})
        return
    metadata, _body, error = parse_frontmatter(text)
    out.append({'name': metadata.get('name') or dir_name,
                'description': metadata.get('description') or '',
                'dir': directory, 'path': skill_md, 'key': key, 'source': source,
                'valid': not error, 'error': error, 'size': size,
                'disabled': bool(metadata.get('disabled')),
                'writable': source == 'user'})


def _project_root(start):
    current = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(current, '.git')):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(start)
        current = parent


def _roots(kimi_home, workspace):
    home = (kimi_home or os.environ.get('KIMI_CODE_HOME') or
            os.environ.get('KIMI_HOME') or os.path.join(os.path.expanduser('~'), '.kimi-code'))
    agents_home = os.path.join(os.path.expanduser('~'), '.agents')
    roots = [
        (os.path.join(home, 'skills'), 'user', '用户级 · ~/.kimi-code/skills'),
        (os.path.join(agents_home, 'skills'), 'user', '用户级 · ~/.agents/skills'),
    ]
    if workspace:
        root = _project_root(workspace)
        roots.append((os.path.join(root, '.kimi-code', 'skills'), 'project', '项目级 · .kimi-code/skills'))
        roots.append((os.path.join(root, '.agents', 'skills'), 'project', '项目级 · .agents/skills'))
    return roots


def scan(kimi_home=None, workspace=None, plugin_skills=None):
    """发现所有 skill。plugin_skills 是插件自带 skills 目录的绝对路径（可选）。"""
    seen, out, scanned = set(), [], []
    for path, source, label in _roots(kimi_home, workspace):
        if os.path.isdir(path):
            scanned.append({'path': path, 'source': source, 'label': label})
            _scan_root(path, source, seen, out)
    if plugin_skills and os.path.isdir(plugin_skills):
        scanned.append({'path': plugin_skills, 'source': 'plugin', 'label': '插件自带'})
        _scan_root(plugin_skills, 'plugin', seen, out)
    out.sort(key=lambda item: (item['source'] != 'user', item['source'] != 'project', item['name'].lower()))
    counts = {'user': 0, 'project': 0, 'plugin': 0}
    for item in out:
        counts[item['source']] = counts.get(item['source'], 0) + 1
    return {'skills': out, 'roots': scanned, 'counts': counts,
            'truncated': len(out) >= MAX_SKILLS}


def read_body(key, kimi_home=None, workspace=None, plugin_skills=None, limit=BODY_PREVIEW):
    """按扫描得到的稳定 key 读取 SKILL.md 正文，不信任前端传入的路径。"""
    target = _locate(key, kimi_home, workspace, plugin_skills)
    real = os.path.realpath(target['path'])
    if os.path.basename(real) != 'SKILL.md':
        raise SkillPathError('只能读取 SKILL.md')
    try:
        with open(real, 'rb') as handle:
            text = handle.read(FRONTMATTER_MAX).decode('utf-8', 'replace')
    except OSError:
        raise SkillPathError('无法读取该文件')
    metadata, body, error = parse_frontmatter(text)
    return {'name': metadata.get('name') or target['name'],
            'description': metadata.get('description') or '',
            'source': target['source'],
            'disabled': bool(metadata.get('disabled')),
            'writable': target['source'] == 'user',
            'body': body[:limit], 'error': error, 'truncated': len(body) > limit}


def _locate(key, kimi_home=None, workspace=None, plugin_skills=None):
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{16}', key):
        raise SkillPathError('无效的 skill 标识')
    found = scan(kimi_home, workspace, plugin_skills)
    target = next((item for item in found['skills'] if item.get('key') == key), None)
    if target is None:
        raise SkillPathError('该 skill 已不存在，请刷新后重试')
    return target


def user_roots(kimi_home=None):
    """可写的用户级 skill 目录（插件与项目级一律只读）。"""
    home = (kimi_home or os.environ.get('KIMI_CODE_HOME') or
            os.environ.get('KIMI_HOME') or os.path.join(os.path.expanduser('~'), '.kimi-code'))
    return [os.path.join(home, 'skills'),
            os.path.join(os.path.expanduser('~'), '.agents', 'skills')]


def _yaml_value(text):
    """写回 frontmatter 的单行标量：含特殊字符时用双引号并转义。"""
    value = str(text).replace('\r', ' ').replace('\n', ' ').strip()
    if value and not re.search(r'[:#\'"\[\]{}&*!|>%@`]', value) and value[0] not in '-?' \
            and value == value.strip():
        return value
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _set_header_field(header, name, value):
    """在 frontmatter 文本里设置或移除一个布尔字段，其余行原样保留。"""
    lines = header.split('\n') if header else []
    pattern = re.compile(r'^\s*' + re.escape(name) + r'\s*:', re.IGNORECASE)
    kept = [line for line in lines if not pattern.match(line)]
    if value is not None:
        kept.append('%s: %s' % (name, 'true' if value else 'false'))
    while kept and not kept[-1].strip():
        kept.pop()
    return '\n'.join(kept)


def compose(name, description, body, header='', disabled=False):
    header = _set_header_field(header or '', 'name', None)
    header = _set_header_field(header, 'description', None)
    header = _set_header_field(header, 'disable-model-invocation', None)
    header = _set_header_field(header, 'disable_model_invocation', None)
    lines = ['---', 'name: ' + _yaml_value(name), 'description: ' + _yaml_value(description)]
    for line in (header.split('\n') if header else []):
        if line.strip():
            lines.append(line)
    lines.append('disable-model-invocation: true' if disabled else 'disable-model-invocation: false')
    lines.append('---')
    content = '\n'.join(lines) + '\n\n' + str(body).strip() + '\n'
    metadata, _b, error = parse_frontmatter(content)
    if error:
        raise SkillPathError('生成的内容无法被 Kimi 解析：' + error)
    return content


def _write_atomic(path, content):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = None
    try:
        from mobile_security import _set_protected_dacl
        fd, tmp = tempfile.mkstemp(prefix='.skill-write-', suffix='.md', dir=directory)
        with os.fdopen(fd, 'wb') as handle:
            _set_protected_dacl(tmp)
            handle.write(content.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def save(key, name, description, body, disabled, kimi_home=None, workspace=None, plugin_skills=None):
    """新增（key 为空）或编辑现有 skill。只允许写用户级目录。"""
    if not isinstance(name, str) or not NAME_RE.match(name.strip()):
        raise SkillPathError('名称只能包含字母、数字、点、下划线和连字符，且以字母或数字开头')
    if not isinstance(description, str) or not description.strip():
        raise SkillPathError('描述不能为空')
    if len(description) > 1000:
        raise SkillPathError('描述过长（上限 1000 字符）')
    if not isinstance(body, str):
        raise SkillPathError('正文必须是文本')
    if len(body) > 200000:
        raise SkillPathError('正文过长（上限 200000 字符）')
    if type(disabled) is not bool:
        raise SkillPathError('禁用状态必须是布尔值')
    name = name.strip()
    if key:
        target = _locate(key, kimi_home, workspace, plugin_skills)
        if target['source'] != 'user':
            raise SkillPathError('只有用户级 skill 可以编辑，插件与项目级只读')
        real = os.path.realpath(target['path'])
        try:
            with open(real, 'rb') as handle:
                current = handle.read(FRONTMATTER_MAX).decode('utf-8', 'replace')
        except OSError:
            raise SkillPathError('无法读取该文件')
        metadata, _old_body, _error = parse_frontmatter(current)
        content = compose(name, description, body, metadata.get('header', ''), disabled)
        _write_atomic(real, content)
        return {'message': '已保存，会话内 /reload 生效'}
    root = user_roots(kimi_home)[0]
    directory = os.path.join(root, name)
    target_md = os.path.join(directory, 'SKILL.md')
    if os.path.exists(target_md):
        raise SkillPathError('同名 skill 已存在：%s' % name)
    if os.path.exists(directory) and os.listdir(directory):
        raise SkillPathError('目录 %s 已存在且非空' % name)
    content = compose(name, description, body, '', disabled)
    _write_atomic(target_md, content)
    return {'message': '已创建 %s，会话内 /reload 生效' % name}


def delete(key, kimi_home=None, workspace=None, plugin_skills=None):
    """删除用户级 skill 的整个目录，不可撤销。"""
    target = _locate(key, kimi_home, workspace, plugin_skills)
    if target['source'] != 'user':
        raise SkillPathError('只有用户级 skill 可以删除，插件与项目级只读')
    real = os.path.realpath(target['dir'])
    roots = [os.path.realpath(p) for p in user_roots(kimi_home)]
    inside = any(real != root and real.startswith(root + os.sep) for root in roots)
    if not inside:
        raise SkillPathError('该目录不在用户级 skill 根内，拒绝删除')
    if os.path.basename(os.path.realpath(target['path'])) != 'SKILL.md':
        raise SkillPathError('目标不是 SKILL.md，拒绝删除')
    try:
        shutil.rmtree(real)
    except OSError:
        raise SkillPathError('删除失败，可能有文件被占用')
    return {'message': '已删除 %s' % target['name']}


def set_disabled(key, disabled, kimi_home=None, workspace=None, plugin_skills=None):
    """切换 disable-model-invocation，正文与其它 frontmatter 字段保持不变。"""
    if type(disabled) is not bool:
        raise SkillPathError('禁用状态必须是布尔值')
    target = _locate(key, kimi_home, workspace, plugin_skills)
    if target['source'] != 'user':
        raise SkillPathError('只有用户级 skill 可以启用或禁用，插件与项目级只读')
    real = os.path.realpath(target['path'])
    try:
        with open(real, 'rb') as handle:
            current = handle.read(FRONTMATTER_MAX).decode('utf-8', 'replace')
    except OSError:
        raise SkillPathError('无法读取该文件')
    metadata, body, error = parse_frontmatter(current)
    if error:
        raise SkillPathError('该 SKILL.md 无法解析，请先修复：' + error)
    content = compose(metadata.get('name') or target['name'],
                      metadata.get('description') or '',
                      body, metadata.get('header', ''), disabled)
    _write_atomic(real, content)
    return {'message': ('已禁用（模型不再自动调用，仍可 /skill:%s 手动使用）' % target['name'])
            if disabled else '已启用', 'disabled': disabled}


def public(item, home=None, prefixes=None):
    """把绝对路径脱敏：home 前缀换成 ~，其余调用方给的根换成可读标签。"""
    row = dict(item)
    rules = []
    if home:
        rules.append((home, '~'))
    for prefix, label in (prefixes or []):
        if prefix:
            rules.append((prefix, label))
    for key in ('dir', 'path'):
        value = row.get(key) or ''
        if not value:
            continue
        for prefix, label in rules:
            if value == prefix or value.startswith(prefix.rstrip('\\/') + os.sep) \
                    or value.startswith(prefix.rstrip('\\/') + '/'):
                rest = value[len(prefix.rstrip('\\/')):].replace('\\', '/').lstrip('/')
                row[key] = label + (('/' + rest) if rest else '')
                break
    return row
