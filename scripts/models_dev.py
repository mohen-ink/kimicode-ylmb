# -*- coding: utf-8 -*-
"""models.dev 价格同步：拉取公开目录，模糊匹配本机模型并换算写入 pricing.json。

api.json 结构（约 5 MiB，社区维护）：
  {"<provider>": {"name": ..., "models": {"<id>": {"name": ..., "cost": {
      "input": USD/1M, "output": USD/1M, "cache_read": USD/1M, "cache_write": USD/1M}}}}}

官方价格：目录混有大量转售商/聚合器（deepinfra、openrouter、nano-gpt 等，
价格常有折扣或加价），只取 OFFICIAL_PROVIDERS 白名单内第一方厂商的条目；
云平台供应商（alibaba、volcengine 等官方条目）会代挂别家模型按平台牌价
计，因此每个厂商还限定自家模型族前缀，族外条目一律跳过；input/output
全为 0 的套餐/赠送价同样跳过。

匹配（models_dev_match / models_dev_apply）：
  候选 = 白名单供应商下的模型，key 为模型 id 规范化（小写、剥冒号后缀
  如 kimi-k2.7-code:thinking、剥日期后缀 -20250901、只取最后一个 / 之后
  的部分）。本机侧同样只取 alias / model id 最后一个 / 之后的部分参与：
    1) 候选 key 精确或前缀匹配
    2) token 序列 difflib 相似度，阈值 0.72，同分时短 id 优先
  型号守卫：两侧 id 中的数字序列必须完全一致（deepseek-v4.1-flash 与
  deepseek-v4-flash 数字不符，直接不匹配）；日期后缀先剥掉再比较。
  展示行保留来源供应商（如 moonshotai / kimi-k3）供人工核对。

换算只做 USD→CNY 乘法（缓存读缺省按输入价补齐）。models_dev_apply
按弹窗勾选生成增量，models_dev_save 合并后由 service.save_pricing
的原子安全写落盘。
"""
import difflib
import json
import re
import threading
import time
import urllib.request

MAX_BYTES = 16 * 1024 * 1024
TIMEOUT = 30
CACHE_TTL = 12 * 3600
DEFAULT_RATE = 7.1
MATCH_THRESHOLD = 0.72

# 第一方厂商白名单（api.json 中转售商条目的价格不代表官方牌价，一律不取）。
# 值 = 该厂商自家模型族前缀（规范化后按前缀匹配）；None = 目录内全部条目都算
# 官方（这类厂商在目录里只挂自家模型）。云平台（alibaba、volcengine 等）会
# 代挂别家模型按平台牌价计，必须用前缀限定只留自家模型族。
OFFICIAL_PROVIDERS = {
    'ai21': None, 'alibaba': ('qwen', 'qvq', 'qwq', 'tongyi'), 'alibaba-cn': ('qwen', 'qvq', 'qwq', 'tongyi'),
    'anthropic': None, 'arcee': None, 'cerebras': None, 'cohere': None,
    'deepseek': ('deepseek',), 'google': ('gemini', 'gemma', 'imagen', 'lyria', 'nano-banana', 'veo', 'vidu'),
    'longcat': None, 'meta': None, 'minimax': ('minimax', 'abab'), 'minimax-cn': ('minimax', 'abab'),
    'mistral': ('mistral', 'codestral', 'magistral', 'ministral', 'pixtral', 'voxtral', 'devstral'),
    'moonshotai': ('kimi', 'moonshot'), 'moonshotai-cn': ('kimi', 'moonshot'),
    'nvidia': None, 'openai': ('gpt', 'o1', 'o3', 'o4', 'chatgpt', 'text-embedding', 'dall-e', 'whisper'),
    'perplexity': ('sonar', 'r1-1776'), 'sensenova': ('sensechat', 'sensechirp', 'senserun', 'sensetime'),
    'stepfun': ('step',), 'stepfun-ai': ('step',), 'upstage': ('solar', 'document'),
    'volcengine': ('doubao', 'seed', 'skylark'), 'xai': ('grok',), 'xiaomi': ('mimo',),
    'zai': ('glm', 'cogview', 'cogvideo', 'cogagent'), 'zhipuai': ('glm', 'cogview', 'cogvideo', 'cogagent'),
}

_API_URL = 'https://models.dev/api.json'
_LOCK = threading.Lock()
_CACHE = {'at': 0.0, 'providers': []}

_TRAILING_DATE = re.compile(r'-[0-9]{6,8}$')


def _version_seq(text):
    """id 中的数字序列（4.1 → [4, 1]），用于型号守卫。"""
    return re.findall(r'[0-9]+', _norm_key(text))


def _norm_key(text):
    s = str(text or '').lower()
    s = s.rsplit('/', 1)[-1]
    s = _TRAILING_DATE.sub('', s.split(':', 1)[0])
    return re.sub(r'\s+', '-', s)


def _split(text):
    return [part for part in re.split(r'[^0-9a-z]+', str(text or '').lower()) if part]


def _token_similarity(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ra, rb = difflib.SequenceMatcher(None, a, b).ratio(), \
        difflib.SequenceMatcher(None, b, a).ratio()
    ra += 0.001 * (len(b) - len(a))
    rb += 0.001 * (len(a) - len(b))
    return max(ra, rb)


def _candidate_keys(model_id):
    nid = _norm_key(model_id)
    keys = [model_id, nid]
    seen, out = set(), []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _official_model(provider_id, model_id):
    """厂商白名单 + 自家模型族前缀双过滤；返回 True 表示可视为官方牌价。"""
    prefixes = OFFICIAL_PROVIDERS.get(provider_id)
    if prefixes is None:
        return provider_id in OFFICIAL_PROVIDERS
    nid = _norm_key(model_id)
    return any(nid == prefix or nid.startswith(prefix + '-') or nid.startswith(prefix + '.')
               or nid.startswith(prefix + '_') for prefix in prefixes)


def _fetch(opener):
    request = urllib.request.Request(_API_URL, headers={
        'accept': 'application/json', 'accept-encoding': 'identity',
        'user-agent': 'kimi-code-usage-pricing-sync'})
    with opener.open(request, timeout=TIMEOUT) as response:
        if getattr(response, 'status', 200) != 200:
            raise RuntimeError('HTTP %s' % getattr(response, 'status', '?'))
        chunks, size = [], 0
        while size <= MAX_BYTES:
            chunk = response.read1(min(262144, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    raw = b''.join(chunks)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('响应为空或超过 %d MiB' % (MAX_BYTES // 1048576))
    payload = json.loads(raw.decode('utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('api.json 结构异常')
    providers = []
    for provider_id, provider in payload.items():
        if not isinstance(provider, dict) or provider_id not in OFFICIAL_PROVIDERS:
            continue
        models = provider.get('models')
        if not isinstance(models, dict):
            continue
        entries = []
        for model_id, model in models.items():
            if not isinstance(model, dict) or not _official_model(provider_id, model_id):
                continue
            cost = model.get('cost')
            if not isinstance(cost, dict):
                continue
            input_price = cost.get('input')
            output_price = cost.get('output')
            if not isinstance(input_price, (int, float)) or not isinstance(output_price, (int, float)):
                continue
            if input_price <= 0 and output_price <= 0:
                continue  # 套餐/赠送条目没有牌价意义
            entries.append({
                'id': str(model_id),
                'name': str(model.get('name') or model_id),
                'input': float(input_price), 'output': float(output_price),
                'cache_read': cost.get('cache_read') if isinstance(cost.get('cache_read'), (int, float)) else None,
                'cache_write': cost.get('cache_write') if isinstance(cost.get('cache_write'), (int, float)) else None,
            })
        if entries:
            providers.append({'id': str(provider_id),
                              'name': str(provider.get('name') or provider_id),
                              'models': entries})
    if not providers:
        raise ValueError('api.json 中没有带价格的模型')
    return providers


def providers_cached():
    """带 12h 内存缓存的目录；拉取失败时回退上次成功结果。"""
    with _LOCK:
        if _CACHE['providers'] and time.monotonic() - _CACHE['at'] < CACHE_TTL:
            return _CACHE['providers'], True
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        providers = _fetch(opener)
    except Exception:
        with _LOCK:
            if _CACHE['providers']:
                return _CACHE['providers'], False
            raise
    with _LOCK:
        _CACHE['providers'] = providers
        _CACHE['at'] = time.monotonic()
    return providers, True


def _index(providers):
    exact, fuzzy = {}, []
    for provider in providers:
        for model in provider['models']:
            for key in _candidate_keys(model['id']):
                exact.setdefault(key, (provider['id'], model))
            fuzzy.append((provider['id'], model['id'], model))
            # 官方条目的 id 可能不带版本号（DeepSeek 官方把 V4.1 Flash 挂成
            # deepseek-flash），name 里有完整型号，一并做精确 key；再补一个
            # 非字母数字折叠为连字符的变体（DeepSeek-V4.1-Flash ↔ deepseek-v4-1-flash）
            for name_key in (model['name'].lower(), re.sub(r'[^0-9a-z]+', '-', model['name'].lower())):
                for key in _candidate_keys(name_key):
                    exact.setdefault(key, (provider['id'], model))
    return exact, fuzzy


def _best_match(index_exact, index_fuzzy, value):
    probe = _norm_key(value)
    tokens = _split(probe)
    versions = _version_seq(probe)
    for key in _candidate_keys(value):
        hit = index_exact.get(key)
        if hit:
            return hit, 1.0, True
        if '.' in key:
            for prefix, hit in index_exact.items():
                if len(prefix) >= len(key) and prefix.startswith(key):
                    return hit, 0.95, True
    best, best_score = None, 0.0
    for provider_id, model_id, model in index_fuzzy:
        cand = _norm_key(model_id)
        if _version_seq(cand) != versions:
            continue  # 型号数字不符（v4.1 vs v4 等），不是同一个模型
        score = _token_similarity(tokens, _split(cand))
        if score > best_score + 1e-9:
            best, best_score = (provider_id, model), score
    if best is not None and best_score >= MATCH_THRESHOLD:
        return best, best_score, False
    return None, best_score, False


def _converted(model, rate):
    rate = float(rate) if rate and float(rate) > 0 else DEFAULT_RATE
    input_price = float(model['input']) * rate
    cache_read = model.get('cache_read')
    entry = {'mode': 'volume', 'input': round(input_price, 4),
             'output': round(float(model['output']) * rate, 4),
             'cache_hit': round(float(cache_read) * rate, 4) if cache_read is not None else round(input_price, 4)}
    cache_write = model.get('cache_write')
    if cache_write is not None:
        entry['cache_write'] = round(float(cache_write) * rate, 4)
    return entry


def models_dev_match(models, rate=None):
    """对 get_models_data() 的 models 数组逐个匹配，返回供弹窗展示的行。"""
    rate = float(rate) if rate is not None and float(rate) > 0 else DEFAULT_RATE
    providers, from_cache = providers_cached()
    exact, fuzzy = _index(providers)
    rows = []
    for m in models:
        alias = str(m.get('alias') or '')
        model_id = str(m.get('model') or '')
        # 匹配只看模型 id 本身（最后一个 / 之后），供应商前缀不参与
        probe = model_id.rsplit('/', 1)[-1] or alias.rsplit('/', 1)[-1]
        if not probe:
            rows.append({'alias': alias, 'model': model_id, 'matched_id': None,
                         'provider': None, 'score': 0.0, 'exact': False,
                         'cost': None, 'rate': rate})
            continue
        hit, score, is_exact = _best_match(exact, fuzzy, probe)
        if hit is None:
            hit, score, is_exact = _best_match(exact, fuzzy, alias)
        if hit is None and '-' in probe:
            # 本地别名常见「原名-序号」后缀（kimi-k3-1 → kimi-k3）；点分版本号
            # 不受影响（4.1 是一个 token，不会被剥掉）
            hit, score, is_exact = _best_match(exact, fuzzy, probe.rsplit('-', 1)[0])
        if hit is None:
            rows.append({'alias': alias, 'model': model_id, 'matched_id': None,
                         'provider': None, 'score': round(score, 3), 'exact': False,
                         'cost': None, 'rate': rate})
            continue
        hit_provider, model = hit
        rows.append({'alias': alias, 'model': model_id, 'matched_id': model['id'],
                     'provider': hit_provider, 'score': round(score, 3), 'exact': is_exact,
                     'cost': _converted(model, rate), 'rate': rate})
    return {'from_cache': from_cache, 'providers': len(providers),
            'models': sum(len(p['models']) for p in providers), 'rows': rows}


def models_dev_apply(rows, existing):
    """按弹窗勾选结果生成 pricing 增量；返回 (增量 dict, 变更别名列表)。

    选中行以 models.dev 价格为准覆盖同名键；按次计价的模型跳过保护。
    写入由调用方合并后一次性落盘。
    """
    delta, changed = {}, []
    for row in rows or []:
        if not row.get('selected'):
            continue
        alias = str(row.get('alias') or '').strip()
        cost = row.get('cost') if isinstance(row.get('cost'), dict) else None
        if not alias or not cost:
            continue
        current = existing.get(alias) if isinstance(existing, dict) else None
        if isinstance(current, dict) and current.get('mode') == 'per_call':
            continue
        delta[alias] = dict(cost)
        changed.append(alias)
    return delta, changed


def models_dev_save(delta, save_one):
    """逐条写入增量（save_one(alias, entry) 即 service.save_pricing 的原子落盘）。"""
    for alias, entry in delta.items():
        save_one(alias, entry)
    return sorted(delta)
