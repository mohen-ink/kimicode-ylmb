# Kimi Code 用量面板 — 更新交接

基线版本：上游仓库 `ziyiclouds-blip/kimicode-ylmb` **v3.1.0**（HEAD `36cc13af`）

本次改动集中在侧栏小组件（`assets/kimi-usage-widget.js`）与一处注入防御补丁（`scripts/service.py`），其余文件与上游一致。

---

## 变更内容

### 1. `assets/kimi-usage-widget.js` — 侧栏卡片重排 + hover 详情弹层 + 跳动修复 + 拉伸适配

| 区块 | 改动 |
|---|---|
| 模型行 `#ku-cur-row` | 名称（加粗，可收缩省略）+ 右侧次要灰色小字 `.ku-cur-meta`（思考档位 · 子代理，10px，靠右，超长省略）；不再使用徽章，工具能力等完整信息放在 hover 名称弹出的浮层里 |
| 会话行 `#ku-sess-row` | 行内显示（短 key / tokens / 缓存 / 成本）；短 key = 去掉 `session_` 前缀后取前 8 位，与 tokens 间留 8px；hover key 弹浮层（完整 ID / Token / 调用 / 缓存 / 成本 / 最近活动） |
| 模型列表 `.ku-model-item` | 去掉内嵌第二行 `.ku-m-detail`；hover `.ku-m-name` 弹浮层（模型 / Token / 次数 / 缓存 / 成本） |
| 详情弹层 `.ku-pop` | 新增 `kuBindHoverPop(el, buildRows, title)`：滑入 90ms 弹出、滑出 140ms 自动关，无 ✕ 按钮、无需点击；浮层自身 hover 不消失，`Esc` 可关 |
| hover 监听只绑一次 | `kuBindHoverPop` 把构建函数/标题挂在元素上（`el._kuPopBuild` / `el._kuPopTitle`），以 `el._kuPopBound` 标记只注册一次 `mouseenter`/`mouseleave`，避免每次刷新叠加监听；浮层已对同一锚点打开时只更新内容，不删除重建 |
| 数据签名跳过重渲染 | `processData` / `fetchData` 用 `kuDataChanged(u)` 比较去掉 `updated_at`/`time` 后的 JSON 签名（及在线状态），未变化则跳过 `updateDOM()` / `kupRenderAll()`；`models` 数据同样只在变化时刷新。sparkline、最小化行、面板 KPI 卡片/指标条也按 HTML 缓存比对，内容相同不重写 |
| 跳动修复 | `renderModelsList` 行数不变时就地更新 `textContent`（不再每 2s `innerHTML` 重建 DOM）；`data-mode` 区分 `empty` / `list`；`.ku-min-model` 固定 `min-height`，空态显示淡色 `--` 占位而不是隐藏 |
| 刷新后最小化栏模型名 | 最小化栏第二行（今日用量最高的模型 · tokens · 缓存 · 成本）的 HTML 缓存到 `localStorage`（`kimi-usage-min-model-html`），刷新后 `createCard` 直接用缓存/现有数据预填，数据到达后再替换，不再出现空白/加载占位；`attachWidget` 新建卡片时重置 `kuLastSig`，保证侧栏重建后的新卡片一定会被完整渲染一次 |
| 面板表格抖动 | `kupRenderModels` / `kupRenderSessions` / `kupRenderDaily` 同侧栏列表思路：表头只建一次、行数不变就地更新 `td`；`table-layout:fixed` + `td text-overflow:ellipsis` 让列宽稳定不随内容跳变 |
| 摘要条 | `.ku-summary-strip` 改 `flex-wrap:wrap`，窄窗口换行；`.ku-min-left` 可收缩省略 |
| 拉伸适配 | `.ku-row-val` 改 `flex:1`、`.ku-row-cost` 改 `margin-left:auto` 右对齐；模型列表列改 `minmax(Npx, auto)` 自适应；缓存条 `max-width:40%` 弹性；sparkline 柱改 `flex:1` 均分宽度；宽侧栏下不再左侧扎堆右侧空 |
| 死代码清理 | 删除 `ku-pop-trigger` / `ku-cur-badge` / `ku-cur-effort` / `ku-cur-tools` / `ku-cur-sub` / `ku-cur-info` / `ku-sess-info` / `ku-pop-close` / `ku-m-detail` / `ku-cur-detail` / `ku-tag*` / `ku-cur-badges` / `ku-cur-main` / `ku-cur-line1` 相关残留 |

> hover 触发只绑在名称元素上（`#ku-cur-model` / `#ku-sess-key` / `.ku-m-name`），行内其他位置不触发。

### 2. `scripts/service.py` — 根路径注入防御补丁（+3 行）

上游 `_INJECT_RE` 只清 `/assets/kimi-*.js`，存在旧版残留的根路径注入 `/kimi-usage-widget.js`（非 `/assets/` 前缀）时会与新版卡片争用同一 DOM 容器。

```python
_LEGACY_ROOT_INJECT_RE = re.compile(r'\s*<script src="/kimi-[a-z-]*(?:data|widget)\.js[^"]*"></script>\s*')
# 在两处 _INJECT_RE.sub 处叠一层：
new_html = _LEGACY_ROOT_INJECT_RE.sub('\n', _INJECT_RE.sub('\n', html))
```

### 3. 未改动文件

`scanner.py` / `updater.py` / `bootstrap.cmd` / `sync-usage.cmd` / `kimi.plugin.json` / `README.md` / `SYSTEM.md` / `commands/*` / `skills/*` / `dashboard/*` —— 与上游 v3.1.0 一致。

---

## 验证

- 语法：括号栈扫描通过（`check_syntax.py`）
- 服务：`GET /api/status` → `{"version":"3.1.0","status":"ok"}`

---

## 已知残留 / 非缺陷

- `GET /api/quota` 返回空对象（上游数据源本身无配额数据，非本次改动引入）
- `.ku-pop` 浮层在 <270px 极窄窗口贴边（已兜底 `Math.max(6, …)`）

---

## PR 建议

合入上游只需两个文件：

1. `assets/kimi-usage-widget.js` — 侧栏布局 + hover 弹层 + 跳动修复 + 拉伸适配（主体）
2. `scripts/service.py` — `_LEGACY_ROOT_INJECT_RE` 补丁（可选，防御旧版根路径残留）

标题建议：`fix: sidebar layout overflow, hover detail popover, list re-render flicker, responsive width`
