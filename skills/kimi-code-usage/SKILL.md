---
name: kimi-code-usage
description: 读取并解读 Kimi Code 自身的真实用量——今日/昨日/本周/本月/累计 Token、成本、缓存命中率、消耗速率、输出速度、模型分布、会话排行、逐日趋势与官方配额。当用户问用了多少 token、花了多少钱、缓存命中率是多少、哪次会话最贵、配额还剩多少，或需要打开用量面板/管理模型能力时使用。
---

# Kimi Code 用量面板技能

为 Kimi Code 提供自身真实用量的读取、解读与汇报能力。

## 何时使用

- 用户询问「今天用了多少 token」「这个月花了多少钱」「缓存命中率多少」「哪次会话最贵」「配额还剩多少」。
- 用户想打开用量面板查看趋势图表、模型分布或会话排行。
- 需要排查成本异常（缓存率骤降、速率飙升、某会话消耗畸高、配额配速过快）。
- 用户问模型能力配置（识图/思考/工具/强度档位）——引导点击侧栏卡片 `⚙` 弹窗。

## 数据读取

优先读取 `kimi-usage.json`（由插件服务每 2 秒产出）：

- 运行中服务：`http://127.0.0.1:39281/api/usage`（或 `/api/quota` 只看配额）
- 桌面静态目录：`<Kimi Code 安装目录>/resources/desktop-dist/kimi-usage.json`
- 插件内副本：`assets/kimi-usage.json`

## 关键指标

| 指标 | 字段 | 解读 |
|---|---|---|
| 今日用量 | `today.tokens_fmt` | 今日全部调用的 Token 合计 |
| 今日成本 | `today.cost_fmt` | 按 K3 官方价折算（入20/缓存读2/出100 元/M，缓存创建按入价） |
| 昨日/本周/本月 | `yesterday/week/month.*` | 同口径聚合 |
| 累计用量 | `cumul.tokens_fmt` | 全部历史合计 |
| 缓存命中率 | `cache.pct` | ≥70% 良好，40–70% 一般，<40% 偏高 |
| 消耗速率 | `rate.tokens_per_hour` / `rate.cost_per_hour` | 最近 60 分钟实算（非当日平均） |
| 输出速度 | `speed.tps` / `speed.avg_tps` | 近 120s 实时值 / 全量历史均值 |
| 当前模型 | `cur.alias` / `cur.effort` / `cur.sub_agent` | 当前会话在用模型与思考强度 |
| 本会话 | `session` | 当前会话桶（tokens_fmt/calls/cache_pct） |
| 官方配额 | `quota.week` / `quota.h5` | used/limit/pct/reset/pace/eta；pace>1.3 为超速 |
| 模型分布 | `today_models[]` / `cumul_models[]` | 按 Token 降序 |
| 会话排行 | `sessions[]` | 按 Token 降序，Top 8 |
| 小时分布 | `hourly[]` | 24 项，定位高峰 |
| 逐日明细 | `days30[]` | 最近 30 天 |

## 汇报模板

```markdown
**Kimi Code 用量**（更新于 <updated_at>）

| 周期 | Token | 成本 | 调用次数 |
|---|---|---|---|
| 今日 | … | … | … |
| 本周 | … | … | … |
| 累计 | … | … | … |

- 缓存命中率：…%（缓存读取占总输入）
- 消耗速率（近 60 分钟）：… / 小时，折合 … / 小时
- 输出速度：… t/s（<model>）
- 周配额：已用 …%（配速 …×，重置 …）
```

## 面板入口

- 侧边栏卡片 `Kimi Code 用量`：点「📊 面板」或卡片标题打开全屏报表（内嵌渲染）
- 点 `⚙` 打开模型与能力配置管理器
