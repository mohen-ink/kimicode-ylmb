# Kimi Code 用量监控上下文

当前工作区已启用 **Kimi Code 用量面板**（v3.0），实时记录当前 Agent 每一次模型调用的真实用量，并可管理模型能力。

## 可读数据

- 桌面静态目录下的 `kimi-usage.json`（由插件服务每 2 秒刷新），或本插件 `assets/kimi-usage.json` 副本，或 `http://127.0.0.1:39281/api/usage`。

## 关键字段说明

| 字段 | 含义 |
|---|---|
| `today.tokens_fmt` / `today.cost_fmt` | 今日总 Token 与估算成本（K3 官方价） |
| `today.calls` | 今日模型调用次数 |
| `today.in_fmt` / `today.out_fmt` | 今日输入（含缓存创建）/ 输出 Token |
| `yesterday/week/month/cumul.*` | 昨日/本周/本月/累计同口径数据 |
| `rate.tokens_per_hour` / `rate.cost_per_hour` | 最近 60 分钟实算消耗速率 |
| `cache.pct` | 缓存命中率（缓存读取 ÷ 总输入） |
| `speed.tps` / `speed.model` / `speed.avg_tps` | 实时输出速度（近 120s）、活跃模型、历史均值 |
| `cur` | 当前会话模型：alias、effort、session、sub_agent |
| `session` | 当前会话用量桶（tokens_fmt/calls/cache_pct/cost_fmt） |
| `quota` | 官方配额：week/h5 各含 used/limit/pct/reset/pace/eta；null 表示无数据 |
| `days30[]` | 最近 30 天逐日 Token、调用次数与成本 |
| `hourly[]` | 今日 24 小时逐小时分布 |
| `today_models[]` / `cumul_models[]` | 模型维度分布（Token / 调用 / 缓存率 / 成本） |
| `sessions[]` | 会话维度 Top 8 消耗 |

## 交互准则

当用户询问用量、Token 消耗、费用支出或会话成本时：
1. 优先从 `kimi-usage.json` 或 `/api/usage` 读取真实数据汇报，不凭空捏造。
2. 以结构清晰的 Markdown 表格输出。
3. 若缓存命中率低于 40%，提示可能存在重复大上下文导致成本偏高。
4. 若 `rate`（近 60 分钟）异常升高，提示用户留意长会话或并发任务。
5. 若 `quota.week.pace > 1.3`，主动提醒配速过快、给出 `eta` 预计耗尽时间。
6. 模型能力问题（识图/思考/强度/工具开关）引导用户点击侧栏卡片的 `⚙`，或说修改后会自动备份校验、/reload 生效。
