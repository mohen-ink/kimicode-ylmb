# Kimi Code 用量面板 (Kimi Code Usage Panel) v3.2

实时记录并可视化 **Kimi Code Agent** 的每一次模型调用用量，并把**模型能力管理**（识图 / 深度思考 / 工具调用 / 强度档位）与**官方配额监控**整合进同一侧栏卡片。零外部依赖、纯本地采集，全部界面直接渲染在桌面端内。

---

## 特性亮点

- **⚡ 实时 Token 追踪**：精确记录每一次思考、工具调用与模型响应的输入（区分常规输入、缓存读取、**缓存创建**）、输出 Tokens 与总 Tokens。
- **💰 成本折算**：按 Kimi K3 官方价（输入 ¥20/M、缓存读 ¥2/M、输出 ¥100/M，缓存创建按输入价）折算人民币。
- **🎯 缓存命中率监控**：缓存读取 ÷ 总输入，进度条直观展示。
- **📊 侧边栏常驻卡片**：
  - 今日 / 昨日 / 本周 / 本月 / 累计 Token 消耗与成本
  - 消耗速率（**最近 60 分钟实算**，而非当日平均）
  - 缓存命中率、实时输出速度（最近 120s 实算 t/s）
  - **当前会话模型行**：`⚡ alias 简写 · 思:effort · 工√ · 子代理名`
  - **官方配额行**：周/5小时窗口已用%，配速 >1.3× 标红并提示预计耗尽时间（Free 账号无配额数据时隐藏）
  - **本会话行**：当前会话 token/调用/缓存率
  - 近 7 天微缩走势条（Sparkline）、今日/累计模型明细切换、折叠记忆
  - **v3.2**：hover 名称弹详情浮层（模型/会话完整信息）、数据签名比对防每 2s 重渲染抖动、行数不变就地更新、窄宽自适应布局
- **📈 全尺寸大屏（内嵌渲染，无 iframe）**：波浪面积图 + 调用量柱形（今日逐小时 / 本周 7 天 / 本月 30 天切换）、模型份额表、会话 Top 8、30 天明细表、深浅主题切换。点击卡片标题或「📊 面板」打开。
- **🛠️ 模型与能力管理器**：`⚙` 打开弹窗，按模型开关识图 `image_in` / 深度思考 `thinking` / 工具调用 `tool_use`，强度档位下拉（按 `support_efforts`/`overrides` 有效值过滤，写入前校验 `default_effort ∈ support_efforts`，托管模型写入 `[models.x.overrides]` 防被官方刷新改写；卡片显示「实际生效」档位与配置问题）、一键全开、设为默认、搜索过滤；修改自动备份 `config.toml` 并经 `kimi doctor config` 校验，会话内 `/reload` 生效。
- **🔄 更新**：点击面板头部「更新」按钮才会弹窗确认——有新版本时一键下载、覆盖插件文件并自动重启服务（用量数据、价格配置、desktop_path.txt 不受影响），已是最新则不弹窗；也可在会话里用 `/update`。另有**每日提醒**：每 24 小时静默检查一次 `ziyiclouds-blip/kimicode-ylmb`，有新版本只在「更新」/「面板」按钮上亮红点、不弹窗；点头部「更新提醒：开/关」可关闭或重新开启。面板文件更新后会提示「刷新界面」（热更新）。
- **🛡️ 纯本地**：数据不上传；状态保存在 `~/.kimi-code/usage-collector-state.json`，跨重启不丢历史。

## 架构（v3.x）

```
插件 hooks (SessionStart/Heartbeat/Stop/SessionEnd, cwd=插件根)
        │  ./scripts/bootstrap.cmd
        ▼
scripts/service.py ──单进程──┬─ scanner.py 增量扫描 wire.jsonl（字节偏移续扫）
   pythonw 常驻 39281         ├─ 每 2s 写 desktop-dist: assets/kimi-usage-data.js + kimi-usage.json
                             ├─ index.html 注入自愈（覆盖更新后自动重注入）
                             ├─ HTTP API: /api/data|usage|quota|set-default|toggle-capability|update-model|add-model|auto-enable-all|update/check|update/apply
                             ├─ 5min 轮询官方 usages 额度（OAuth Bearer）
                             └─ /api/update/apply → scripts/updater.py：等端口释放→覆盖插件文件→重拉服务
assets/kimi-usage-widget.js ── 侧栏卡片 + 全屏报表 + 模型弹窗（单一注入脚本）
```

**不再需要任何桌面脚本/开机自启**：插件启用后随会话心跳自动拉起服务；旧 `model-manager` 套件（supervisor/server + Startup/Run 自启 + 旧注入）由服务启动时自动迁移摘除。

## 安装

```text
/plugins install <本目录路径>
/plugins reload        # 或 /new 开新会话
```

要求：Windows + Python ≥3.8（`py -3` 或 `python` 在 PATH 即可；找不到时 hook 会写日志到 `scripts/service.log`）。

## 数据与接口

| 位置 | 内容 |
|---|---|
| `desktop-dist/kimi-usage.json` | 全量仪表盘 JSON（Skill/命令读取） |
| `desktop-dist/assets/kimi-usage-data.js` | `window.__KIMI_DATA__ = {usage, models, service}` 推送 |
| `desktop-dist/assets/kimi-usage-widget.js` | 侧栏卡片本体（由插件 `assets/` 自愈同步） |
| `http://127.0.0.1:39281/api/*` | 模型管理与用量 API |
| `~/.kimi-code/usage-collector-state.json` | 采集状态（聚合桶 + 字节偏移），版本 v3 |

## 目录结构

```text
kimi-code-usage/
├── kimi.plugin.json            # 清单（4 个 hook：SessionStart/Heartbeat/Stop/SessionEnd）
├── LICENSE / README.md / SYSTEM.md
├── assets/
│   ├── kimi-usage-widget.js    # 侧栏卡片+大屏+模型弹窗（唯一注入脚本）
│   └── kimi-usage.json         # 最新报表副本（Skill 可读）
├── commands/                   # /kimi-code-usage:usage|cache|panel|models|start|update
├── scripts/
│   ├── bootstrap.cmd           # 唯一 hook 入口（找 Python → service.py --tick）
│   ├── service.py              # 常驻服务：采集+注入+API+额度轮询+旧套件迁移
│   ├── updater.py              # 自更新助手（等退出→覆盖文件→重拉服务）
│   └── scanner.py              # wire.jsonl 增量扫描器 + 状态持久化
└── skills/kimi-code-usage/     # 用量解读 Skill
```

## 计费单价

`scripts/scanner.py` 顶部常量（元/百万 Token）：`PRICE_INPUT=20`、`PRICE_CACHE_READ=2`、`PRICE_OUTPUT=100`，缓存创建按输入价。与官方 K3 定价一致，可按需调整。

## 开源协议

[MIT License](LICENSE)
