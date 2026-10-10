# Kimi Code 用量面板 (Kimi Code Usage Panel) v3.4.0

实时记录并可视化 **Kimi Code Agent** 的每一次模型调用用量，把**模型能力管理**（识图 / 深度思考 / 工具调用 / 强度档位）、**模式滑杆**与**官方配额监控**整合进同一侧栏卡片，另附**手机远程连接**（内网 / Cloudflare Quick Tunnel / 自建中继，扫码或复制链接配对，预览功能）。用量采集零外部依赖、纯本地进行；外网连接需要经明确同意安装单独的 Cloudflare 组件，自建中继需自备服务器。界面直接渲染在桌面端内。

> **版本与状态**：本仓库当前发布 **v3.4.0（2026-10-09）**：以自建 Python 中继方案为基底合并回主线，同时纳入模式滑杆、`server.token` 自助补建、手机端会话归档/删除/导出，以及手机聊天 WebSocket 心跳修复；原 frp 中继方案退场，`deploy/frp/` 与 frp 许可证已移除。**手机远程连接为预览功能**。**自建中继为明文传输**（`ws://` / `http://`），配对码、Cookie、会话与文件内容经 VPS 时不可保密，仅在自控服务器、临时联调场景使用。

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
- **🛠️ 模型与供应商管理器**：`⚙` 打开「模型 / 供应商」弹窗，可新增和编辑供应商连接参数、密钥及模型配置；保留能力开关、默认思考强度、价格、设为默认、搜索与配置问题检查。表单点击「保存」才写文件，取消不写；密钥默认保留，不回显已有密钥。写入采用原子替换，不生成模型配置备份，不依赖 Kimi CLI，会话内 `/reload` 加载。配置冲突拒绝覆盖；详见下方「模型与供应商管理」。
- **🔄 更新**：面板**不会自动更新**。点面板头部「更新」按钮才会请求检查——有新版本时弹窗显示发布时间与更新内容（取自 `CHANGELOG.md`），**需你点击确认**才会下载、覆盖插件文件并自动重启服务（用量数据、价格配置、desktop_path.txt 不受影响）；已是最新则不弹窗。会话里也可用 `/update`。另有**每日提醒**：每 24 小时静默检查一次 `ziyiclouds-blip/kimicode-ylmb`，有新版本只在「更新」/「面板」按钮上亮红点、不弹窗；点头部「更新提醒：开/关」可关闭或重新开启。正式版面板文件更新后会提示「刷新界面」。**带本地预览标记的安装形态**（清单 `localPreview: true` 或版本带 `-local`）会拦截 GitHub 自更新（检查返回 `blocked: true` / `reason: "local_preview"`，应用返回 HTTP 409；`scripts/updater.py` 自身也有同样的纵深防御，退出码 5），需要按发布说明手动安装；GitHub 上发布的 `3.3.3` 清单为纯数字版本，不含该标记。CSS-only 自动样式更新只覆盖固定样式文件，不代表通用 JS 热重载。
- **📱 手机远程连接（可选，预览）**：侧栏「手机」浮层顶部是**三段模式切换块**：**内网**（局域网明文 HTTP）、**CF 隧道**（Cloudflare Quick Tunnel）、**中继服务器**（已开放：自备 VPS 跑 `scripts/relay_server.py` 做转发跳板，本机 worker 主动连过去建隧道，手机访问 VPS 固定公网地址；部署见 [自建中继搭建教程](docs/自建中继服务器-搭建教程.md)）。加载状态、打开浮层都不会自动安装组件或开启连接；手机端使用 Kimi 原生手机 SPA，桥只负责配对与受限转发。
  - 明确同意后安装固定版本的 Cloudflare 连接器，再单独同意第三方中继并开启；准备好后生成临时 HTTPS 配对链接与离线二维码，供不在同一 WiFi 的手机浏览器使用。不是 P2P，不使用个人 VPS。
  - **配对码 10 分钟一次性**：配对码只出现在 URL fragment，10 分钟有效、成功兑换一台设备即销毁；「生成新二维码」作废旧码签新码（不影响已连设备）；停止吊销全部配对/会话/隧道。
  - 二维码由 vendored `qrcodegen.js`（Project Nayuki，MIT）离线生成；QR 失败可复制链接。
  - **内网模式**：不经过 Cloudflare、无需下载组件；选择本机一块 RFC1918 内网网卡并确认「明文 HTTP、仅可信 Wi-Fi」后开启，生成 `http://<内网IP>:<端口>/mobile/pair#pair=…` 配对链接与二维码（端口从 39282 起取空闲）。手机须与电脑在同一可信 Wi-Fi。
  - **模式锁定**：桥开启期间切换块锁定为当前运行模式，需先停止才能切换；关闭状态下切换只保存前端偏好（`localStorage: kur-mode`），不发请求。检测到官方中继仍在运行时，浮层只提示并提供「停止官方中继」入口，绝不代为切换。
- **🛡️ 用量采集纯本地**：用量采集仍在本机进行。开启手机连接后，会话流量会经 Cloudflare 第三方中继转发，会话本身不再「纯本地」。

## 模型与供应商管理

侧栏点击 `⚙`，先在「供应商」中新增连接，再点击「新增模型」，选择供应商并手动点击「探测模型列表」。弹窗不会自动探测；模型列表支持搜索及「全部 / GPT / Claude / Gemini / Qwen / GLM / Deepseek / Kimi / 其他」筛选，按上游模型 ID 做大小写不敏感的字符匹配；「其他」为不含以上任一分类名称的模型。已添加的本地模型统一放在列表末尾，配置中已有但上游未返回的模型也会保留显示。新模型别名自动加供应商前缀，例如供应商 `CPA` 的 `devin/claude-opus-5-5` 生成 `CPA/devin/claude-opus-5-5`；上游模型 ID 保持原样，已有别名不改写。齿轮首页卡片标题直接显示实际配置别名，显示名保留在悬停提示；新模型默认显示名也加供应商前缀。

- **新增模型列表**：每行右侧依次为 `+ / −` 和 SVG 铅笔按钮。未添加模型点击 `+` 选择新增，再点击 `−` 取消选择；已添加模型显示禁用的 `−`，此页面不删除已有模型。选择和详情只修改草稿，点击「保存更改」才统一写入。铅笔编辑详情后返回列表，不会单独保存，也不会自动选中未添加模型。同一上游模型的多个本地别名分别显示。
- **删除入口**：点击齿轮后首先显示的「模型」页面，每张已有模型卡片提供删除按钮，确认后立即删除该别名及其本地覆盖配置，采用原子保存，不生成配置备份；取消确认不写文件。不需要先探测供应商，也不在新增列表中删除。
- **默认配置**：新增模型默认开启识图、思考和工具调用，上下文 `1M（1000000）`，支持 `low / medium / high / xhigh / max` 五档，默认 `high`；详情可调整能力和档位，并提供 `256k（256000）/ 1M（1000000）` 上下文预设及自定义输入。已有模型保留原配置。能力与档位只是本地声明，不代表上游实际支持。
- **删除保护**：托管模型、默认模型、子代理模型池或模式滑杆预设/恢复快照引用的模型不能删除，需先解除引用。删除在完整配置对象中进行，保留其它模型及未知配置字段，随后全量序列化保存。
- **探测协议**：支持 OpenAI、OpenAI Responses、Kimi 的 Bearer `/models`，Anthropic 和 Google GenAI 的各自鉴权与列表分页；Vertex/OAuth 暂不支持探测，可使用手动添加。显式 Base URL 应包含服务商要求的版本路径；Kimi 协议必须填写地址，其余支持的协议可使用标准默认地址。不跟随重定向，不尝试其它地址发送密钥。最多 10 页、5000 模型、单页 2 MiB、累计 8 MiB，达到限制会提示列表可能不完整；请求超时 10 秒、总读取预算 45 秒。允许配置的本机及内网服务，明显云元数据目标拒绝访问。
- **供应商**：编辑协议和 API 地址，密钥输入默认遮罩。已有密钥不回显；`keep`（保留）不修改旧值，`replace`（替换）写入新值，`clear`（清除）删除配置中的 `api_key`，不清除环境变量或嵌套认证。已有 `api_key_env` 绑定时拒绝直接替换，需先手动解除绑定。探测可读取服务进程环境中的该变量及合法的 `extra_headers`，不读取秘密文件；这些信息只留后台，不返回前端。托管或 OAuth 供应商只读，不修改官方认证。
- **编辑与保存**：名称与别名编辑时固定，托管模型身份固定，适用的能力修改使用覆盖层。手动添加入口保留，适用于上游不提供列表。表单和批量草稿点击保存才写入，取消不写。现有卡片的能力、强度与设默认快捷操作仍直接保存。保存成功后执行 `/reload`；设默认不等于立即切换当前会话。
- **冲突**：草稿打开后若配置被滑杆、其他会话或外部工具修改，保存会拒绝覆盖，需重新载入并重新编辑。对不共享锁的外部写入只能做版本检测，不承诺绝对并发隔离。
- **写入方式与要求**：保存时解析完整配置，在内存完成增删改，再全量序列化为 TOML 并原子替换，不向旧文本追加配置段。保留未编辑字段的值、未知字段及嵌套配置；注释、原排版和数字写法不会保留，表与字段使用统一格式。序列化后重新解析，类型或语义不一致则拒绝写入。用量采集仍支持 Python ≥3.8；配置编辑需要标准库 `tomllib`（Python ≥3.11）或环境已有的 `toml`，不会自动安装依赖，也不依赖 Kimi CLI 或外部配置校验器。
- **本版边界**：不提供供应商删除、重命名、供应商复制、原始 TOML 编辑或备份恢复界面。保存不生成 config.toml 的 .bak 备份；已有历史备份不会自动删除。模式滑杆的切换前快照仅保留相关配置段，损坏模式预设的排查副本和插件源码同步备份不受此设置影响。

编辑 API 使用现有本地 `39281` 服务，携带 `X-Kimi-Usage-Control: 1`：`GET /api/model-manager` 返回脱敏视图、删除保护和配置版本；`POST /api/model-manager/catalog` 手动探测，`POST /api/model-manager/batch` 批量增删改；原 `/provider` 和 `/model` 单项保存接口保留。版本冲突返回 HTTP 409。密钥不会加入用量接口或 `desktop-dist/assets` 的静态数据脚本；手机端不开放这些管理接口。详细请求格式见 [模型命令](commands/models.md)。

## 手机远程连接（外网，预览功能）

> 手机远程连接为**预览功能**。支持两种接入方式：**Cloudflare Quick Tunnel**（免服务器、临时 HTTPS 地址）与**自建中继服务器**（走自己的 VPS，见 [自建中继搭建教程](docs/自建中继服务器-搭建教程.md)）。

**3.3 预览演进（简）**：v3.3.0 引入手机远程连接试点（官方中继，需付费会员），v3.3.1 增加局域网模式，v3.3.2 扩展为外网 Cloudflare Quick Tunnel，v3.3.3 收口为**外网单一路径**、把手机桥迁入独立 worker 进程，并加入手机只读用量页与一批连接、上传、权限和界面稳定性修复；v3.4.0 进一步以自建 Python 中继方案为基底，移除 frp。

### 使用步骤

> 下列步骤按默认的 **CF 隧道** 模式写。用 **中继服务器** 模式时改为：模式切到「中继服务器」→ 在浮层里填服务器地址 / 隧道端口 / 公网端口 / 连接密钥并保存 → 点「开启中继连接」，部署细节见 [自建中继搭建教程](docs/自建中继服务器-搭建教程.md)。

1. 安装插件（见下文），打开侧栏「手机」浮层。电脑必须能访问 GitHub 官方发布站点和 Cloudflare。
2. 首次使用：勾选下载同意并安装连接组件（固定 `cloudflared 2026.9.3`，Windows AMD64；完整许可证已内置；网络瞬态自动有限重试，最多 3 次 GET 共用 180 秒预算，首次可能需要 2–3 分钟，**安装中勿重复点击**）。装好后校验复用。安装失败先检查到官方发布站点的网络，再显式重试；不要关闭 TLS/安全软件绕过。
3. 勾选第三方中继同意并开启：**Cloudflare 在 TLS 终止处可见经过的会话内容**，并非端到端加密。安装同意不等于中继同意。
4. 等待隧道就绪；只有就绪且配对码有效时才显示链接/二维码。手机用**系统浏览器**扫码或打开复制的链接（微信等内置浏览器请切系统浏览器）。不要把配对链接/二维码交给他人。
5. 配第二台手机：点浮层「生成新二维码」重新扫码（旧码作废，已连设备不受影响）。
6. 撤销：回浮层点「停止」——吊销配对码、已配会话、WebSocket 和隧道。电脑须一直开机且桌面服务运行；关机、桌面 owner 失联或服务异常会中断连接。**关闭浮层不会断开连接**；用量 daemon 重启不中断已开启的手机连接（桥在独立 worker 进程中存活）。

- **手机只读用量查看（桥内转发）**：worker 内 `scripts/mobile_usage.py` 在配对会话中提供只读 `GET /mobile/usage` 与 `GET /mobile/usage/data`——仅已配对设备（SID 同源认证，与聊天面同一会话 Cookie）可访问，后端只从固定 loopback（`127.0.0.1:39281` 本机用量服务）白名单读取既定只读用量数据，不接受手机传入任意 URL/主机。**本机用量服务停止只让手机用量页不可用，不影响已开启的聊天桥**；原生 SPA 内仍不渲染桌面侧栏用量 widget（刻意剥离），这是桥内白名单转发而非 SPA 适配。

**服务限制**：Quick Tunnel 是临时随机 `https://<随机名称>.trycloudflare.com` 地址，停止或重开后地址变化；**不承诺永久 URL**、无 SLA、不支持 SSE（本方案用 WebSocket）。电脑向 Cloudflare 发起出站连接，需网络允许 **7844/UDP（QUIC）或 7844/TCP**；不需要路由器入站端口映射，不自动修改防火墙。

### 配对与安全边界

- **配对 = 控制授权**：手机可查看会话并发 prompt 驱动电脑 Agent（含读写文件、执行命令的会话面能力）。桥只放行精确 method+route 的受限会话面；机器级管理与未知路由默认拒绝；没有手机直连终端、会话归档/删除；全局配置写仅限窄白名单键（`default_model`/`thinking`/`auto_session_title`/`default_plan_mode`）。**非 `/api/` 面为白名单**（`/`、`/index.html`、`/favicon.ico`、形状校验过的 `/assets/**`、`/admin/sessions`、单段 `/sessions/*` 与 `/devices/*`），其余静态路径不转发；`/api/v2/sessions` 为**精确匹配**。**文件上传体上限 32 MiB**（含包装层），上传读取放宽为 idle 120s / 累计 600s，控制面与其它路径仍为 15s 累计。请勿分享链接。
- **配对码一次性**：10 分钟有效，成功兑换一台设备即销毁；浮层「生成新二维码」可在不中断已连设备的情况下签新码。会话 Cookie idle 24h 滑动过期、绝对寿命 7 天，到期失效并切断实时连接；最多 8 台设备。
- **外网经过第三方 TLS 终止**：Cloudflare 可见经隧道转发的内容。不是 P2P；不使用个人 VPS。
- **`server.token` 不出桥**：凭据只由桥运行时读取，用于上游 Authorization；不向手机返回真实凭据（手机端只有无权限的占位值）。日志、错误和状态不包含原始凭据或配对秘密。
- **固定组件而非任意可执行文件**：仅官方 [cloudflare/cloudflared 2026.9.3 Windows AMD64 发布文件](https://github.com/cloudflare/cloudflared/releases/download/2026.9.3/cloudflared-windows-amd64.exe)，大小 `55366080` 字节，SHA256 `f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2`；完整 [Apache-2.0 许可证随插件打包](assets/vendor/cloudflared-2026.9.3-LICENSE)（`11357` 字节，SHA256 `58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd`，安装先校验包内许可证再下载 EXE）。不接受用户传入 URL、命令或外网绑定参数。Python ≥3.8 标准库实现，不新增 pip/npm 依赖。
- **组件存储在插件树外**：`KIMI_HOME/usage-dashboard/runtime/cloudflared/2026.9.3`；插件更新不覆盖此目录。
- **本地预览形态禁止 GitHub 自更新**：带 `localPreview: true` 与 `-local` 后缀的安装形态保留其自更新拦截；检查返回 `blocked: true` / `reason: "local_preview"`，应用返回 HTTP 409。**`scripts/updater.py` 自身也有 `local_build_blocked()` 纵深防御**（清单 `localPreview`/版本 `-local`/清单缺版本一律拒，一个文件都不动，退出码 5），不依赖调用方自觉。GitHub 上发布的 `3.3.3` 清单为纯数字版本、不带该标记。
- **旧版通道**：检测到旧版局域网连接仍在运行时浮层提供「停止连接」入口；检测到官方中继仍在运行时，浮层给出专用按钮**「停止官方中继」**（`#kur-legacy-stop`，调 `KimiRemoteAPI.setEnabled(false)`），同样不代为开关。官方中继是 Kimi Code 官方功能，需付费会员与同一 Kimi 账号。内网（`mode='lan'`）已重新进入 UI 的模式切换块，不再作为"旧版通道"处理。

### 手机控制面接口合同（供集成排查）

全部 `/api/mobile/*` 请求由 service 前置分流给独立 mobile worker（IPC secret，loopback）；worker 内 `mobile_bridge` 自管 loopback、Host、Origin、`X-Kimi-Mobile-Control: 1`、请求体与 CORS 守卫；service 不另建安装路由。安装和开启都是显式 POST：

| 接口 | 请求 |
|---|---|
| `GET /api/mobile/status` | 只读状态，不下载或开启 |
| `POST /api/mobile/connector/install` | `{consent:true, consent_version:"cloudflare-quick-2026-09-v1"}` |
| `POST /api/mobile/start`（内网） | `{owner_origin, mode:"lan", address:"<本机内网IPv4>"}` |
| `POST /api/mobile/start`（外网） | `{owner_origin, mode:"internet", relay_consent:true, consent_version:"cloudflare-quick-2026-09-v1"}` |
| `POST /api/mobile/start`（自建中继） | `{owner_origin, mode:"relay"}`，中继参数从本机 `relay.json` 读取 |
| `GET /api/mobile/relay/config` | 只读，返回 `{host, tunnel_port, public_port, token_set}`，**不回显 token** |
| `POST /api/mobile/relay/config` | 保存中继参数（`host` / `tunnel_port` / `public_port` / `token`），落地 `~/.kimi-code/usage-dashboard/relay.json` |
| `POST /api/mobile/pair/rotate` | `{}`，作废旧配对码并签发新码（不影响已连设备） |
| `POST /api/mobile/stop` | `{}`，幂等撤销 |

**模式滑杆接口**：`GET /api/modes`（读取档位与当前档）、`POST /api/modes/save`、`POST /api/modes/reset`、`POST /api/modes/apply`、`POST /api/modes/restore`。档位存于 `~/.kimi-code/usage-dashboard/modes.json`；切换只改 `config.toml` 的 `default_model`、`[secondary_model]`/`[secondary_model.models]` 与 `[tools].disabled`，越界则拒绝写入。

状态顶层 `state` 为 `off|starting|on|stopping`，并含 `enabled`、`device_count`、`connector` 与 `tunnel`；`connector.state` 为 `missing|installing|installed|failed`、版本 `2026.9.3`，`tunnel.state` 为 `off|starting|ready|failed`。只在外网隧道就绪时有 `public_origin`，只在有效配对时有 `url/expires_at`；若有 `pair_state`，仅 `missing|available|used|expired`。检测到旧版本 worker 仍在运行时状态附 `worker_notice`：当前版本**不接管、不擅杀**，但经同一证明强度核实后返回该 worker 的**实际状态（不伪造 off，使「停止」按钮可用）**并放行**显式 stop**——迁移路径是浮层「停止」按钮（停止后升级），而非让用户手动重启电脑或杀进程。错误只使用有限错误码，不应返回 raw logs、runtime 路径或秘密 URL。worker 不可用、关闭中或控制异常时 fail closed，不回退 legacy API，也不自动开启桥。

服务退出时只对 worker 客户端做 detach（`begin_shutdown()`，不做下载或资源 IO）——独立 worker 与其中的桥/隧道/配对继续存活，同版本新 daemon 经状态记录重连接管；显式停止才吊销配对/WS/桥/连接器并让 worker 退出。worker 侧用只读 `IsProcessInJob` 探测 + `CREATE_BREAKAWAY_FROM_JOB` 隔离（绝不把自身加入 Job、绝不静默继承 daemon 的 Job；不能安全脱离时拒绝启动）；活性凭证是真实 OS 锁（`startup.lock`/`worker.lock`，进程崩溃 OS 自动释放，锁文件残留无害）；同版本新 daemon 经记录 + 进程身份 + 认证 meta 三重核实重连接管，旧版本 worker 只读其实际 status 并放行显式「停止」。更新退出的资源清理等待最多 3 秒，不通过结束主 app 或其他进程实现。

常见失败：`CONSENT_REQUIRED`（未明确同意）、`CONNECTOR_MISSING`（尚未安装）、`CONNECTOR_INSTALL_FAILED` / `CONNECTOR_HASH_MISMATCH`（安装或校验失败）、`CONNECTOR_UNSUPPORTED`（平台不支持）、`CONNECTOR_BUSY`（组件操作进行中）、`TUNNEL_START_FAILED` / `TUNNEL_TIMEOUT` / `TUNNEL_EXITED`（隧道不可用）、`OWNER_LOST`（桌面实例失联）、`START_CANCELLED`（启动被取消）。

## 架构（v3.x）

```
插件 hooks (SessionStart/Heartbeat, cwd=插件根)
        │  ./scripts/bootstrap.cmd
        ▼
scripts/service.py ──单进程──┬─ scanner.py 增量扫描 wire.jsonl（字节偏移续扫）
   pythonw 常驻 39281         ├─ 每 2s 写 desktop-dist: assets/kimi-usage-data.js + kimi-usage.json
                             ├─ index.html 注入自愈（覆盖更新后自动重注入）
                             ├─ model_manager.py：全量 TOML 序列化、批量增删改、脱敏视图与删除保护
                             ├─ model_catalog.py：供应商鉴权、手动列表探测与有界分页
                             ├─ HTTP API: /api/model-manager[ /model | /provider | /catalog | /batch ] + 原有接口
                             ├─ 5min 轮询官方 usages 额度（OAuth Bearer）
                             ├─ /api/mobile/* 前置分流 → MobileWorkerClient（IPC secret，loopback）
                             └─ /api/update/apply → detach worker 后有界退出 → updater.py（预览在 service 与 updater 两侧拦截）
scripts/mobile_worker.py ── 独立 detached worker 进程（daemon 重启只 detach，worker 存活）
        └─ scripts/mobile_bridge.py ── 配对/会话 + 外网受限会话代理 + WS 隧道
        └─ scripts/mobile_usage.py ── 手机只读用量（/mobile/usage[/data]，白名单读固定 loopback 用量数据）
        └─ scripts/mobile_tunnel.py ── 固定 cloudflared 组件 → Cloudflare Quick Tunnel → 手机 HTTPS 浏览器（Kimi 原生手机 SPA）
assets/vendor/qrcodegen.js + kimi-remote-qr.js + kimi-remote-api.js + kimi-mobile-api.js + kimi-remote-widget.js
                        ── 可选手机外网连接（摘要校验注入，缺失自动摘除标签）
assets/kimi-usage-widget.js ── 侧栏卡片 + 全屏报表 + 模型弹窗（注入链最后一个脚本）
```

**不再需要任何桌面脚本/开机自启**：插件启用后随会话心跳自动拉起用量服务；旧 `model-manager` 套件由服务启动时自动迁移摘除。用量服务自动运行不等于手机连接自动开启，手机连接必须由用户显式开启。

## 安装

在 Kimi Code 中安装、重载插件：

```text
/plugins install <本目录路径>
/plugins reload
```

新安装或修改 Python 服务代码后，在 Windows 命令提示符中由用户重启**实际安装目录**的用量服务（若安装位置不同，请替换路径）。仅同步文档或 CSS 等资源无需重启：

```bat
python "%USERPROFILE%\.kimi-code\plugins\managed\kimi-code-usage\scripts\service.py" --restart
```

要求：Windows + Python ≥3.8（`py -3` 或 `python` 在 PATH 即可；找不到时 hook 会写日志到 `scripts/service.log`）。外网固定组件另限 Windows AMD64，不支持的平台应报告 `CONNECTOR_UNSUPPORTED`。

**源码安装必须重启服务**：代码在 daemon 启动时装入内存，`/plugins reload`、`/new` 与 hook 心跳不会替换旧进程代码；手机桥运行在独立 worker 进程中，daemon 重启本身不中断已开启的手机连接（同版本 worker 被重连接管），但桥/worker 代码变更须先「停止」手机连接让旧 worker 退出再开启。重启只允许停止执行当前这份 `service.py` 确切绝对路径的 Python 服务；不要从另一份源码副本重启安装目录的服务，否则所有权检查会拒绝。它不应结束主 Kimi app 或其他进程。重启后用 `curl --noproxy '*' -s http://127.0.0.1:39281/api/status` 确认服务版本与 `pid`；若安装的是带本地预览标记的形态，再用 `/api/update/check` 确认 `blocked: true`、`reason: "local_preview"`（发布源码 `3.3.3` 无此标记）。显式「停止」手机连接会撤销隧道与全部已配会话（外网隧道地址作废），需重新开启并让手机重新扫码配对。

## 数据与接口

| 位置 | 内容 |
|---|---|
| `desktop-dist/kimi-usage.json` | 全量仪表盘 JSON（Skill/命令读取） |
| `desktop-dist/assets/kimi-usage-data.js` | `window.__KIMI_DATA__ = {usage, models, service}` 推送 |
| `desktop-dist/assets/kimi-usage-widget.js` | 侧栏卡片本体（由插件 `assets/` 自愈同步） |
| `http://127.0.0.1:39281/api/*`（除 mobile） | 模型管理与用量 API；仅 loopback，POST 需 `X-Kimi-Usage-Control: 1`，浏览器来源需可信 |
| `http://127.0.0.1:39281/api/mobile/*` | 手机控制面；安全合同由 bridge 自管，需 `X-Kimi-Mobile-Control: 1`，不返回桌面真实凭据 |
| `~/.kimi-code/usage-collector-state.json` | 采集状态（聚合桶 + 字节偏移），版本 v3 |
| `KIMI_HOME/usage-dashboard/runtime/cloudflared/2026.9.3` | 固定外网组件，插件树外，显式同意后才安装 |

## 目录结构

```text
kimi-code-usage/
├── kimi.plugin.json            # 清单（2 个 hook：SessionStart/SessionHeartbeat）
├── LICENSE / README.md / SYSTEM.md
├── assets/
│   ├── kimi-usage-widget.js    # 侧栏卡片+大屏+模型弹窗（注入链最后一个脚本）
│   ├── kimi-remote-widget.js   # 手机浮层（内网 / CF 隧道 / 中继占位 模式切换）
│   ├── kimi-remote-api.js      # 官方 /api/v1/remote-control 适配层（仅探测旧版官方中继）
│   ├── kimi-mobile-api.js      # 手机控制面适配层（/api/mobile/*）
│   ├── kimi-remote-qr.js       # QR→SVG 桥接（依赖 vendor/qrcodegen.js）
│   ├── vendor/qrcodegen.js     # Project Nayuki qrcodegen（MIT，离线二维码）
│   ├── vendor/cloudflared-2026.9.3-LICENSE  # cloudflared Apache-2.0 完整原文（固定摘要）
│   └── kimi-usage.json         # 最新报表副本（Skill 可读）
├── commands/                   # /kimi-code-usage:usage|cache|panel|models|start|update
├── docs/                       # 自建中继服务器搭建教程
├── scripts/
│   ├── bootstrap.cmd           # 唯一 hook 入口（找 Python → service.py --tick）
│   ├── service.py              # 采集+注入+API+/api/mobile 前置分流（退出只 detach worker）
│   ├── model_manager.py        # 模型/供应商脱敏视图、批量编辑、删除保护与语义校验
│   ├── model_catalog.py        # 后台供应商鉴权、手动模型列表探测与有界分页
│   ├── mobile_worker.py        # 独立 worker 进程 + daemon 侧 IPC 客户端（protocol=1）
│   ├── mobile_bridge.py        # 配对/会话 + 外网受限代理 + WS 隧道
│   ├── mobile_usage.py         # 手机只读用量（/mobile/usage[/data]，SID 同源认证，白名单读固定 loopback）
│   ├── mobile_tunnel.py        # 固定版本连接器（cloudflared）安装与隧道生命周期管理
│   ├── updater.py              # 自更新助手（自带本地预览纵深防御 + 完整性闸门，退出码 5/6）
│   └── scanner.py              # wire.jsonl 增量扫描器 + 状态持久化
└── skills/kimi-code-usage/     # 用量解读 Skill
```

## 计费单价

`scripts/scanner.py` 顶部常量（元/百万 Token）：`PRICE_INPUT=20`、`PRICE_CACHE_READ=2`、`PRICE_OUTPUT=100`，缓存创建按输入价。与官方 K3 定价一致，可按需调整。

## 开源协议

插件采用 [MIT License](LICENSE)。离线二维码库为 Project Nayuki MIT；单独下载的 **cloudflared 组件采用 Apache-2.0**，不属于插件的 MIT 授权。组件的源码与许可证见 [Cloudflare 官方仓库](https://github.com/cloudflare/cloudflared)。
