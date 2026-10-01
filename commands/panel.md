---
name: panel
description: 打开 Kimi Code 用量面板查看趋势图与分布明细
---

请帮助用户打开 **Kimi Code 用量面板**。

该面板（内嵌渲染于桌面端，无 iframe）包含：
- 四大核心周期卡片：今日目前、昨日汇总、本周累计、本月累计
- Token 消耗趋势：今日 24 小时 / 本周 7 天 / 本月 30 天平滑波浪面积图与调用量柱形
- 模型调用统计：带彩色份额占比条、输入/输出 Token、调用量与成本
- 会话分布排行：Top 8 会话用量
- 30 天每日明细表与官方配额指示（有配额数据时）

打开方式：
- 侧边栏 `Kimi Code 用量` 卡片 → 点击右上角「📊 面板」按钮或卡片标题
- 读取 `desktop-dist/kimi-usage.json` 或 `http://127.0.0.1:39281/api/usage` 以 Markdown 呈现相同报表

参数：$ARGUMENTS
