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
6. 模型与供应商配置问题引导用户点击侧栏卡片的 `⚙`。删除按钮位于点击齿轮后的「模型」首页卡片，确认后立即删除并保存。点击「新增模型」，选择供应商并手动探测列表；未添加行的 `+ / −` 只用于选择新增/取消选择，已添加行显示禁用的 `−`，不在探测列表中删除。右侧 SVG 铅笔编辑详情，上下文可选 256k/1M 或自定义。新模型默认识图/思考/工具全开、1M 上下文、五档思考与 high；这些只是本地声明，不代表上游实际支持。批量草稿点击「保存更改」才写文件，取消不写；首页删除需二次确认；托管或被默认/子代理/模式预设和恢复快照引用的模型禁止删除。手动添加、能力、强度、价格和设默认入口保留。供应商密钥默认保留，不读取或回显旧密钥；探测鉴权仅在后台。保存解析完整 TOML 并全量序列化后原子替换，不追加配置段；保留未编辑字段的值，不保留注释或原排版，不生成模型配置备份，不依赖 Kimi CLI；会话内 `/reload` 加载。配置冲突需重新载入，不能强行覆盖；供应商删除、重命名、复制与原始 TOML 编辑不在本版范围。
