---
name: models
description: 新增和编辑模型与供应商，管理能力、强度档位与默认模型
---

请帮助用户管理 Kimi Code 的模型与供应商配置。

操作入口：
- 侧边栏 `Kimi Code 用量` 卡片 → `⚙` 按钮，选择「模型」或「供应商」，新增或编辑后点击「保存」。供应商名称和模型别名编辑时不可改，托管供应商只读。
- 「新增模型」选择供应商后手动点击「探测模型列表」，未添加行的 `+ / −` 选择新增/取消选择，已添加行显示禁用的 `−`；右侧 SVG 铅笔暂存详情，最后「保存更改」统一写入。删除按钮位于点击齿轮后的「模型」首页卡片，确认后立即删除并保存，不在探测列表中删除。新模型默认识图/思考/工具全开、上下文 1000000、五档思考和 high；详情可选 256k（256000）/1M（1000000）预设或自定义，已有配置不被覆盖。这些本地声明不代表上游一定支持。密钥默认保留，替换或清除须明确选择，不读取或回显真实密钥。
- 支持 OpenAI/OpenAI Responses/Kimi、Anthropic、Google GenAI 列表探测；Vertex/OAuth 或无列表接口时使用手动添加。探测只使用保存的供应商地址与后台鉴权，支持服务进程的 api_key_env 及合法 extra_headers，不跟随重定向、不关闭 TLS 验证。请求、分页和响应体有上限，截断会明确提示。
- 托管模型及被默认模型、子代理模型池、模式预设或恢复快照引用的模型禁止删除，先解除引用。配置中已有但上游没返回的模型仍可管理；同上游多个本地别名分别操作。
- 或直接调用本地服务 API（127.0.0.1:39281）；管理请求携带 `X-Kimi-Usage-Control: 1`，POST 使用 JSON：
  - GET `/api/model-manager` —— 脱敏编辑视图与 `version/default_model`；密钥仅返回 `has_api_key`，模型含 `delete_protected/delete_reason`
  - POST `/api/model-manager/catalog` `{version, provider}` —— 仅手动触发探测；返回 `{success,version,provider,models:[{id,display_name}],message}`，不保存配置或验证模型能力
  - POST `/api/model-manager/batch` `{version,provider,upserts,removes}` —— 一次原子保存当前供应商的模型增删改；`upserts` 为 `[{original_alias:null|string,model:{alias,provider,model,display_name,max_context_size,capabilities,support_efforts,default_effort}}]`，`removes` 为模型别名数组；不支持 `set_default`，删除保护或任一参数失败则整批拒绝
  - POST `/api/model-manager/provider` `{version, original_name, provider}` —— 新增（`original_name:null`）或编辑供应商；`provider` 含 `name/type/base_url/key_action`，`key_action` 为 `keep/replace/clear`，仅替换时传 `api_key`
  - POST `/api/model-manager/model` `{version, original_alias, model, set_default}` —— 新增（`original_alias:null`）或编辑模型；`model` 含 `alias/provider/model/display_name/max_context_size/capabilities/support_efforts/default_effort`，未声明默认强度用 `null`
  - GET `/api/data` —— 列出全部模型与能力，含 `effective_efforts` / `effective_effort`（主 Agent 实际生效档）/ `effort_issues` / `effort_audit` 思考强度审计
  - POST `/api/toggle-capability` `{alias, capability, enabled}` —— 开关 image_in/thinking/tool_use
  - POST `/api/update-model` `{alias, updates}` —— 如 `{"default_effort":"max"}`；档位值须为 low/medium/high/xhigh/max，且 default 必须在 support_efforts 内，否则 400
  - POST `/api/set-default` `{alias}` —— 设为默认模型
  - POST `/api/add-model` `{alias, provider, model, ...}` —— 新增模型
  - POST `/api/auto-enable-all` —— 一键全开所有模型能力（只补能力标签，不再臆造 support_efforts）

所有配置写入先解析整份 TOML、在内存应用修改，再全量序列化并原子替换，不追加配置段，不生成 `config.toml` 自动备份，不依赖 Kimi CLI 或外部配置校验。保留未编辑字段的值及嵌套配置，不保留注释和原始排版。结构编辑需要 `tomllib`（Python ≥3.11）或环境已有的 `toml`；解析或序列化无法保留语义时拒绝保存。新接口必须携带读取时的 `version`，冲突返回 HTTP 409，需重新载入并重新编辑，不能强制覆盖。保存后用户在会话内输入 `/reload` 加载；设默认不等于立即切换当前会话。

本版不提供供应商删除、重命名、供应商复制、原始 TOML 编辑或备份恢复界面。

参数：$ARGUMENTS
