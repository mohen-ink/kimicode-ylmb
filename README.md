# Kimi Code 用量面板 (Kimi Code Usage Panel) v3.3.3

实时记录并可视化 **Kimi Code Agent** 的每一次模型调用用量，把**模型能力管理**（识图 / 深度思考 / 工具调用 / 强度档位）与**官方配额监控**整合进同一侧栏卡片，另附**手机外网远程连接**（Cloudflare Quick Tunnel，扫码/复制链接配对，预览功能）。用量采集零外部依赖、纯本地进行；外网连接需要经明确同意安装单独的 Cloudflare 组件。界面直接渲染在桌面端内。

> **版本与状态**：本仓库当前发布 **v3.3.3（2026-10-08）**，已在 GitHub 标为 **Pre-release**。**手机远程连接仍是预览功能，尚未完成全部真机与公网验收**，请按预览对待。本页描述目标行为与已核实事实，不把「状态接口正常」「隔离测试通过」写成整体验收完成。

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
- **🛠️ 模型与能力管理器**：`⚙` 打开弹窗，按模型开关识图 `image_in` / 深度思考 `thinking` / 工具调用 `tool_use`，默认强度分段按钮（按 `support_efforts`/`overrides` 有效值过滤，写入前校验 `default_effort ∈ support_efforts`，托管模型写入 `[models.x.overrides]` 防被官方刷新改写；卡片显示配置问题）、一键全开、设为默认、搜索过滤；修改自动备份 `config.toml` 并经 `kimi doctor config` 校验，会话内 `/reload` 生效。
- **🔄 更新**：面板**不会自动更新**。点面板头部「更新」按钮才会请求检查——有新版本时弹窗显示发布时间与更新内容（取自 `CHANGELOG.md`），**需你点击确认**才会下载、覆盖插件文件并自动重启服务（用量数据、价格配置、desktop_path.txt 不受影响）；已是最新则不弹窗。会话里也可用 `/update`。另有**每日提醒**：每 24 小时静默检查一次 `ziyiclouds-blip/kimicode-ylmb`，有新版本只在「更新」/「面板」按钮上亮红点、不弹窗；点头部「更新提醒：开/关」可关闭或重新开启。正式版面板文件更新后会提示「刷新界面」。**带本地预览标记的安装形态**（清单 `localPreview: true` 或版本带 `-local`）会拦截 GitHub 自更新（检查返回 `blocked: true` / `reason: "local_preview"`，应用返回 HTTP 409；`scripts/updater.py` 自身也有同样的纵深防御，退出码 5），需要按发布说明手动安装；GitHub 上发布的 `3.3.3` 清单为纯数字版本，不含该标记。CSS-only 自动样式更新只覆盖固定样式文件，不代表通用 JS 热重载。
- **📱 手机远程连接（可选，预览）**：侧栏「手机」浮层顶部是**三段模式切换块**：**内网**（局域网明文 HTTP）、**CF 隧道**（Cloudflare Quick Tunnel）、**中继服务器**（占位，尚未开放）。加载状态、打开浮层都不会自动安装组件或开启连接；手机端使用 Kimi 原生手机 SPA，桥只负责配对与受限转发。
  - 明确同意后安装固定版本的 Cloudflare 连接器，再单独同意第三方中继并开启；准备好后生成临时 HTTPS 配对链接与离线二维码，供不在同一 WiFi 的手机浏览器使用。不是 P2P，不使用个人 VPS。
  - **配对码 10 分钟一次性**：配对码只出现在 URL fragment，10 分钟有效、成功兑换一台设备即销毁；「生成新二维码」作废旧码签新码（不影响已连设备）；停止吊销全部配对/会话/隧道。
  - 二维码由 vendored `qrcodegen.js`（Project Nayuki，MIT）离线生成；QR 失败可复制链接。
  - **内网模式**：不经过 Cloudflare、无需下载组件；选择本机一块 RFC1918 内网网卡并确认「明文 HTTP、仅可信 Wi-Fi」后开启，生成 `http://<内网IP>:<端口>/mobile/pair#pair=…` 配对链接与二维码（端口从 39282 起取空闲）。手机须与电脑在同一可信 Wi-Fi。
  - **模式锁定**：桥开启期间切换块锁定为当前运行模式，需先停止才能切换；关闭状态下切换只保存前端偏好（`localStorage: kur-mode`），不发请求。检测到官方中继仍在运行时，浮层只提示并提供「停止官方中继」入口，绝不代为切换。
- **🛡️ 用量采集纯本地**：用量采集仍在本机进行。开启手机连接后，会话流量会经 Cloudflare 第三方中继转发，会话本身不再「纯本地」。

## 手机远程连接（外网，预览功能）

> **状态（2026-10-08）**：手机外网连接随 **v3.3.3 源码发布**，但仍是**预览功能，尚未完成全部真机与公网验收**。本段描述目标行为与已核实事实；隔离测试计数、状态接口返回 200，都不等于整体验收完成。

**已核实**：固定版本连接器（cloudflared 2026.9.3，Windows AMD64）的真实标准安装通过（约 154 秒）；runtime EXE 与随包 LICENSE 的固定大小、SHA256 复验正确；受控 `--version` 退出码 0，返回 `cloudflared version 2026.9.3 (built 2026-09-24T08:31 UTC)`。安装不等于开启隧道，`--version` 不等于真实隧道生命周期验收。

**未验收**：公网端到端（真实手机经临时公网地址完成配对→会话→消息往返）、实体手机摄像头扫码、真实桌面界面的实际交互与剪贴板、公网上传链路（含蜂窝上行）、真实隧道完整生命周期、真实桌面生命周期接管等——完整未验收清单见 [docs/手机远程连接-最终工作文档-20261007.md](docs/手机远程连接-最终工作文档-20261007.md) §7。用户曾自报公网连通，用户报告不替代执行者验收。

**测试口径**：维护期间有一次定点复跑 **82 项执行通过**，另有指定维护用例 **36 项复跑通过**。两者都是**执行计数**，不能相加当作项目测试总数，也不代表全项目去重测试全绿。发布隔离回归已迁移旧合同，54 个检查项通过（33 个 Python 套件、9 个 Node 套件、12 项静态检查）；Python 记录 540 个用例，其中 2 个因符号链接权限跳过，另有 1 个被正向回归取代的历史观察用例未运行；Node 77 条断言通过。以上为执行计数，包含继承导致的重复，不是去重总数，也不替代公网真机验收。

**3.3 预览演进（简）**：v3.3.0 引入手机远程连接试点（官方中继，需付费会员），v3.3.1 增加局域网模式，v3.3.2 扩展为外网 Cloudflare Quick Tunnel，v3.3.3 收口为**外网单一路径**、把手机桥迁入独立 worker 进程，并加入手机只读用量页与一批连接、上传、权限和界面稳定性修复。各阶段的过程记录不再逐条列在本页，需要时见 [CHANGELOG.md](CHANGELOG.md) 与上述三份手机文档。

完整项目说明见 [docs/手机远程连接-工作文档.md](docs/手机远程连接-工作文档.md)，合并与部署回滚见 [docs/手机远程连接-合并文档.md](docs/手机远程连接-合并文档.md)。

### 使用步骤

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
| `POST /api/mobile/start`（外网） | `{owner_origin, mode:"internet", relay_consent:true, consent_version:"cloudflare-quick-2026-09-v1"}` |
| `POST /api/mobile/pair/rotate` | `{}`，作废旧配对码并签发新码（不影响已连设备） |
| `POST /api/mobile/stop` | `{}`，幂等撤销 |

状态顶层 `state` 为 `off|starting|on|stopping`，并含 `enabled`、`device_count`、`connector` 与 `tunnel`；`connector.state` 为 `missing|installing|installed|failed`、版本 `2026.9.3`，`tunnel.state` 为 `off|starting|ready|failed`。只在外网隧道就绪时有 `public_origin`，只在有效配对时有 `url/expires_at`；若有 `pair_state`，仅 `missing|available|used|expired`。检测到旧版本 worker 仍在运行时状态附 `worker_notice`：当前版本**不接管、不擅杀**，但经同一证明强度核实后返回该 worker 的**实际状态（不伪造 off，使「停止」按钮可用）**并放行**显式 stop**——迁移路径是浮层「停止」按钮（停止后升级），而非让用户手动重启电脑或杀进程。错误只使用有限错误码，不应返回 raw logs、runtime 路径或秘密 URL。worker 不可用、关闭中或控制异常时 fail closed，不回退 legacy API，也不自动开启桥。

服务退出时只对 worker 客户端做 detach（`begin_shutdown()`，不做下载或资源 IO）——独立 worker 与其中的桥/隧道/配对继续存活，同版本新 daemon 经状态记录重连接管；显式停止才吊销配对/WS/桥/连接器并让 worker 退出。worker 侧用只读 `IsProcessInJob` 探测 + `CREATE_BREAKAWAY_FROM_JOB` 隔离（绝不把自身加入 Job、绝不静默继承 daemon 的 Job；不能安全脱离时拒绝启动）；活性凭证是真实 OS 锁（`startup.lock`/`worker.lock`，进程崩溃 OS 自动释放，锁文件残留无害）；同版本新 daemon 经记录 + 进程身份 + 认证 meta 三重核实重连接管，旧版本 worker 只读其实际 status 并放行显式「停止」。更新退出的资源清理等待最多 3 秒，不通过结束主 app 或其他进程实现。

常见失败：`CONSENT_REQUIRED`（未明确同意）、`CONNECTOR_MISSING`（尚未安装）、`CONNECTOR_INSTALL_FAILED` / `CONNECTOR_HASH_MISMATCH`（安装或校验失败）、`CONNECTOR_UNSUPPORTED`（平台不支持）、`CONNECTOR_BUSY`（组件操作进行中）、`TUNNEL_START_FAILED` / `TUNNEL_TIMEOUT` / `TUNNEL_EXITED`（隧道不可用）、`OWNER_LOST`（桌面实例失联）、`START_CANCELLED`（启动被取消）。

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
├── kimi.plugin.json            # 清单（4 个 hook：SessionStart/Heartbeat/Stop/SessionEnd）
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
├── docs/                       # 手机远程连接工作文档、合并文档与最终工作文档
├── scripts/
│   ├── bootstrap.cmd           # 唯一 hook 入口（找 Python → service.py --tick）
│   ├── service.py              # 采集+注入+API+/api/mobile 前置分流（退出只 detach worker）
│   ├── mobile_worker.py        # 独立 worker 进程 + daemon 侧 IPC 客户端（protocol=1）
│   ├── mobile_bridge.py        # 配对/会话 + 外网受限代理 + WS 隧道（公网待验收）
│   ├── mobile_usage.py         # 手机只读用量（/mobile/usage[/data]，SID 同源认证，白名单读固定 loopback）
│   ├── mobile_tunnel.py        # 固定组件安装及 --version 已验；真实隧道生命周期待验收
│   ├── updater.py              # 自更新助手（自带本地预览纵深防御 + 完整性闸门，退出码 5/6）
│   └── scanner.py              # wire.jsonl 增量扫描器 + 状态持久化
└── skills/kimi-code-usage/     # 用量解读 Skill
```

## 计费单价

`scripts/scanner.py` 顶部常量（元/百万 Token）：`PRICE_INPUT=20`、`PRICE_CACHE_READ=2`、`PRICE_OUTPUT=100`，缓存创建按输入价。与官方 K3 定价一致，可按需调整。

## 开源协议

插件采用 [MIT License](LICENSE)。离线二维码库为 Project Nayuki MIT；单独下载的 **cloudflared 组件采用 Apache-2.0**，不属于插件的 MIT 授权。组件的源码与许可证见 [Cloudflare 官方仓库](https://github.com/cloudflare/cloudflared)。
