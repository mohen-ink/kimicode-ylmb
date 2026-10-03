---
name: models
description: 管理模型能力：识图、深度思考、工具调用、强度档位、默认模型
---

请帮助用户管理 Kimi Code 的模型能力配置。

操作入口：
- 侧边栏 `Kimi Code 用量` 卡片 → `⚙` 按钮打开「模型与能力配置管理器」
- 或直接调用本地服务 API（127.0.0.1:39281）：
  - GET `/api/data` —— 列出全部模型与能力，含 `effective_efforts` / `effective_effort`（主 Agent 实际生效档）/ `effort_issues` / `effort_audit` 思考强度审计
  - POST `/api/toggle-capability` `{alias, capability, enabled}` —— 开关 image_in/thinking/tool_use
  - POST `/api/update-model` `{alias, updates}` —— 如 `{"default_effort":"max"}`；档位值须为 low/medium/high/xhigh/max，且 default 必须在 support_efforts 内，否则 400
  - POST `/api/set-default` `{alias}` —— 设为默认模型
  - POST `/api/add-model` `{alias, provider, model, ...}` —— 新增模型
  - POST `/api/auto-enable-all` —— 一键全开所有模型能力（只补能力标签，不再臆造 support_efforts）

所有修改会自动备份 `config.toml` 并经 `kimi doctor config` 校验，用户在会话内输入 `/reload` 立即应用。

参数：$ARGUMENTS
