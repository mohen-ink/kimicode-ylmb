# Kimi Code 用量面板 (Kimi Code Usage Panel) v3.3.8

实时记录并可视化 **Kimi Code Agent** 的每一次模型调用用量，把**模型能力管理**（识图 / 深度思考 / 工具调用 / 强度档位）与**官方配额监控**整合进同一侧栏卡片，另附**手机远程连接**（内网 / CF 隧道 / 用户自配 frp 中继，扫码或复制链接配对，预览功能）。用量采集零外部依赖、纯本地进行；CF 与 frp 组件均需明确同意后单独安装。界面直接渲染在桌面端内。

> **版本与状态**：当前实现预览版本为 **v3.3.8（2026-10-09）**：保留内网与 CF 隧道，新增用户自配 frp TCP 中继；本版放开手机端会话**归档 / 删除 / 导出**。不表示已发布或已完成部署验收。v3.3.3 起的历史发布在 GitHub 标为 **Pre-release**。**手机远程连接仍是预览功能，真机蜂窝链路尚未验收**。v3.3.7 的证书规范化测试期望与更新器许可证漏项已修订，本地复验 93 个单元用例通过；v3.3.8 在此之上改动桥白名单与 worker 版本，本地复验 94 个单元用例通过。Python 3.13 编译、清单 JSON 与版本检查、frp 许可证摘要及 `git diff --check` 通过。上述结果不替代真实 frp 安装、服务器部署或公网真机验收。

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
- **📱 手机远程连接（可选，预览）**：侧栏「手机」浮层提供**内网 / CF 隧道 / 中继服务器**三种模式。打开浮层、读取状态、选择模式均不会安装组件或开启连接；手机使用 Kimi 原生手机 SPA，桥负责配对与受限转发。
  - 内网无需组件；CF 需单独安装 cloudflared；中继需用户已有服务器、自行部署 frps，再明确同意安装固定版本 frpc。三种模式均非 P2P。
  - **配对码 10 分钟一次性**：配对码位于 URL fragment，成功兑换一台设备即销毁；「生成新二维码」不影响已连设备。二维码离线生成，失败可复制链接；停止撤销全部授权。
  - **模式锁定**：桥运行期间须先停止才能切换；关闭状态仅保存 `localStorage: kur-mode` 偏好，**不保存 frp token**。官方中继仍在运行时只提示并提供「停止官方中继」，绝不代为切换。
- **🛡️ 用量采集纯本地**：采集仍在本机进行；手机连接的会话流量按所选模式经过局域网、Cloudflare 或用户服务器，不应称会话「纯本地」。

## 手机远程连接（三模式，预览功能）

| 模式 / API 值 | 路径与手机地址 | 前置条件与安全边界 |
|---|---|---|
| 内网 / `lan` | 手机 → 电脑 RFC1918 内网地址，`http://<内网IP>:<端口>` | 同一可信 Wi-Fi；无需组件；明文 HTTP，不用于不可信网络 |
| CF 隧道 / `internet` | 手机 → Cloudflare → cloudflared → 本机桥，临时 `https://<随机名>.trycloudflare.com` | 官方固定组件；Cloudflare 可见 TLS 终止后的内容；非端到端加密 |
| 中继服务器 / `relay` | 手机 → 用户 frps → frpc → 本机桥，`http://<公网IPv4>:<访问端口>` | 用户自配服务器；电脑到服务器强制 TLS、公开 CA 验证与 IP SAN 匹配、token 认证；**手机到服务器仍为明文 HTTP** |

> **中继第一阶段只用于显式同意的临时联调**：HTTP 会话、配对兑换、Cookie 与消息可被窃听或篡改。frpc→frps 的 TLS **不保护手机→服务器这一段**；自建服务器不等于安全公网，更不是端到端加密。第一阶段不需要域名、Nginx 或手机 HTTPS，不将它描述为正式安全部署。示例控制端口 `7000`、手机访问端口 `6000`，不是强制端口。

**历史已核实（CF，非本轮）**：cloudflared 2026.9.3 Windows AMD64 的标准安装约 154 秒完成，EXE 与 LICENSE 固定大小和摘要复验正确；受控 `--version` 退出码 0。历史隔离回归的 82、36、54 检查项以及 Python 540/Node 77 等为不同执行口径、包含继承重复，不能相加算去重总数。安装、版本输出与隔离记录均不证明真实隧道生命周期或公网真机验收。

**本轮未验收**：frp 安装与服务端部署、实际插件前端呈现及 worker 升级接管、真实界面扫码/剪贴板、蜂窝配对→会话→消息→上传、断线撤销与 worker 生命周期。本地复验、安装文件同步及本机服务检查结果见页首；用户已选择暂不部署。用户历史自报连通不替代执行者验收。完整历史清单见 [最终工作文档 §7](docs/手机远程连接-最终工作文档-20261007.md)。

**版本演进**：v3.3.3 的「外网单一路径」是历史口径；v3.3.5/3.3.6 已恢复内网与 CF 模式，v3.3.7 新增自配 frp 中继，v3.3.8 放开手机端会话归档/删除/导出。具体服务器准备、接口与排障见 [工作文档](docs/手机远程连接-工作文档.md)，历史合并/回滚见 [合并文档](docs/手机远程连接-合并文档.md)。

### 使用步骤

1. 安装插件，打开「手机」浮层；若已有连接，先显式停止，再选模式。
2. 按模式准备并分别确认风险：
   - **内网**：选择本机 RFC1918 网卡，确认「明文 HTTP、仅可信 Wi-Fi」后开启。端口从 `39282` 起取空闲，手机连同一可信 Wi-Fi。
   - **CF 隧道**：电脑须能访问官方 GitHub 发布站点与 Cloudflare。先勾选下载同意安装固定 `cloudflared 2026.9.3`（首次可能 2–3 分钟，勿重复点击；最多 3 次 GET 共用 180 秒预算），再单独同意第三方中继并开启；安装同意不等于中继同意。
   - **中继服务器**：先按[工作文档 §5.2](docs/手机远程连接-工作文档.md#52-服务器准备与-frps-部署用户自行执行)部署同版本 frps，准备公网 IPv4、控制端口、访问端口、token 与公开 CA 证书。勾选下载同意安装固定 frp 客户端；表单填入配置，token 只在密码框输入，CA 可粘贴公开 PEM。单独明确同意明文公网 HTTP 临时联调后开启。**不要在聊天中粘贴真实 token 或任何私钥。**
3. 等待连接就绪且配对码有效，再用手机**系统浏览器**扫码或打开链接；不要分享二维码/配对链接。relay 地址为 `http://IP:remote_port/mobile/pair#pair=…`。
4. 第二台手机须点「生成新二维码」，旧码作废但已连设备不受影响。
5. 点「停止」撤销全部配对、会话、WebSocket、桥与客户端，relay 临时配置同步删除。relay 断线也撤销授权，**必须显式重开并重新配对**，不会自动恢复旧会话。

电脑须一直开机且目标桌面实例可用。关闭浮层不停止连接；daemon 退出/重启只 detach，独立 worker 保留已有连接。手机只读 `GET /mobile/usage[/data]` 使用同一已配会话，仅白名单读取 `127.0.0.1:39281`；用量服务停止只使该页不可用，不影响仍存活的聊天桥。

**网络限制**：CF 需出站 `7844/UDP`（QUIC）或 `7844/TCP`，地址临时变化、无 SLA、不支持 SSE（本方案用 WebSocket）。relay 电脑向用户 frps 控制端口出站连接，服务器需用户手动开放控制/访问 TCP 端口并限制测试来源；不需电脑路由器入站映射。插件不自动改防火墙或部署服务器。

### 配对与安全边界

- **配对 = 控制授权**：手机可查看会话并发 prompt 驱动电脑 Agent（含读写文件、执行命令的会话面能力）。桥只放行精确 method+route 的受限会话面；机器级管理与未知路由默认拒绝；没有手机直连终端；会话级**归档 / 删除 / 导出已放行**（含 v2 批量归档与恢复，均仅接受 POST）；全局配置写仅限窄白名单键（`default_model`/`thinking`/`auto_session_title`/`default_plan_mode`）。**非 `/api/` 面为白名单**（`/`、`/index.html`、`/favicon.ico`、形状校验过的 `/assets/**`、`/admin/sessions`、单段 `/sessions/*` 与 `/devices/*`），其余静态路径不转发；`/api/v2/sessions` 为**精确匹配**。**文件上传体上限 32 MiB**（含包装层），上传读取放宽为 idle 120s / 累计 600s，控制面与其它路径仍为 15s 累计。请勿分享链接。
- **配对码一次性**：10 分钟有效，成功兑换一台设备即销毁；浮层「生成新二维码」可在不中断已连设备的情况下签新码。会话 Cookie idle 24h 滑动过期、绝对寿命 7 天，到期失效并切断实时连接；最多 8 台设备。
- **分段安全边界**：CF 在 Cloudflare 处终止 TLS；relay 在用户 frps 处终止电脑侧 TLS，但手机侧明文可被窃听篡改。两者均非端到端加密。relay 桥**只绑定 `127.0.0.1` 的实际分配端口**，frpc TCP 转发该端口，不公开本机桥监听。
- **凭据分离**：桌面 `server.token` 只在桥内用于上游 Authorization，手机仅得到无权限占位值。frp token 是另一份秘密：仅密码输入、worker 内存及 ACL 收紧的临时配置中存在，不进 URL、argv、日志、status 或 localStorage；停止删除临时配置。CA 可粘贴的是**公开证书，不是 CA 私钥或服务器私钥**。
- **固定组件而非任意可执行文件**：CF 使用官方 [cloudflared 2026.9.3 Windows AMD64](https://github.com/cloudflare/cloudflared/releases/download/2026.9.3/cloudflared-windows-amd64.exe)，大小 `55366080` 字节、SHA256 `f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2`；包内 [Apache-2.0 LICENSE](assets/vendor/cloudflared-2026.9.3-LICENSE) 为 `11357` 字节、SHA256 `58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd`。frp 使用官方固定 Windows AMD64 版本，**以 `scripts/mobile_relay.py` 的 `RELAY_VERSION` 为准**，ZIP、解包 EXE、包内 `assets/vendor/frp-<version>-LICENSE` 均按代码固定大小与 SHA256 校验，不凭空填写未定版本/摘要。安装先校验随包许可证，不接受任意下载 URL 或命令；Python ≥3.8 标准库，不新增 pip/npm 依赖。
- **组件位于插件树外**：CF 在 `KIMI_HOME/usage-dashboard/runtime/cloudflared/2026.9.3`，frp 在 `KIMI_HOME/usage-dashboard/runtime/frp/<version>`；插件更新不覆盖这些目录。
- **本地预览自更新闸门不变**：`localPreview: true` 或版本 `-local` 的检查返回 `blocked: true` / `reason: "local_preview"`，应用 HTTP 409；updater 自身仍做纵深防御（清单缺版本也拒绝，退出码 5）。历史 GitHub `3.3.3` 不带该标记。
- **旧版通道**：旧 worker 只读其真实状态并允许显式停止，不接管、不擅杀、不伪造 off；官方中继需付费会员与同一账号，只提供「停止官方中继」入口。内网现为正常模式，不再称旧版通道。

### 手机控制面接口合同（供集成排查）

全部 `/api/mobile/*` 由 service 前置分流给独立 worker（loopback、IPC secret）。bridge 管理 Host、Origin、`X-Kimi-Mobile-Control: 1`、请求体与 CORS 守卫。以下为对象字段示意，不是含真实凭据的命令；安装/开启都是显式 POST：

| 接口 | 请求 |
|---|---|
| `GET /api/mobile/status` | 只读；不下载或开启 |
| `POST /api/mobile/connector/install`（默认 CF，旧参数不变） | `{consent:true, consent_version:"cloudflare-quick-2026-09-v1"}` |
| `POST /api/mobile/connector/install`（frp） | `{mode:"relay", consent:true, consent_version:"frp-tcp-http-v1"}` |
| `POST /api/mobile/start`（CF） | `{owner_origin, mode:"internet", relay_consent:true, consent_version:"cloudflare-quick-2026-09-v1"}` |
| `POST /api/mobile/start`（relay） | `{owner_origin, mode:"relay", relay_consent:true, consent_version:"frp-tcp-http-v1", relay_config:{server_ip, server_port, remote_port, token, ca_cert}}` |
| `POST /api/mobile/pair/rotate` | `{}`；旧码作废，已连设备不受影响 |
| `POST /api/mobile/stop` | `{}`；幂等撤销所有模式的授权与资源 |

`state` 仍为 `off|starting|on|stopping`。旧 `connector` **始终代表 CF**（版本 `2026.9.3`），新增 `relay_connector` 代表 frp；组件状态为 `missing|installing|installed|failed`。`tunnel` 为当前模式的通用连接状态（`off|starting|ready|failed`），不把 relay 状态塞入 CF `connector`。relay 就绪时 `public_origin=http://IP:remote_port`；有效配对才有 `url/expires_at`，`pair_state` 若出现仅为 `missing|available|used|expired`。状态与有限错误码不得泄露 token、原始日志、运行目录或秘密链接；worker/control 异常时 fail closed，不回退旧接口或自动开启。

daemon 退出只对客户端 `begin_shutdown()` / detach，不撤销 worker 内桥/配对/连接；同版本 daemon 经记录、进程身份、认证 meta 三重核实重连。worker 用 OS 锁证明活性，并通过 `IsProcessInJob` + `CREATE_BREAKAWAY_FROM_JOB` 脱离 daemon Job，不能安全脱离时拒绝启动；**frpc 客户端归 worker 自有 Job 管理**，不归 daemon Job。显式 stop 或 relay 断线撤销授权、回收客户端并删除临时配置；断线后须用户显式重开。更新退出有界等待，不结束主 app 或无关进程。

既有有限错误码包括 `CONSENT_REQUIRED`、`CONNECTOR_MISSING`、`CONNECTOR_INSTALL_FAILED`、`CONNECTOR_HASH_MISMATCH`、`CONNECTOR_UNSUPPORTED`、`CONNECTOR_BUSY`、`TUNNEL_START_FAILED`、`TUNNEL_TIMEOUT`、`TUNNEL_EXITED`、`OWNER_LOST`、`START_CANCELLED`。relay 配置/证书/端口诊断请见工作文档；不通过关闭 TLS 或跳过证书校验排障。

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
scripts/mobile_worker.py ── 独立 detached worker（daemon 只 detach；客户端由 worker 管理）
        ├─ scripts/mobile_bridge.py ── 配对/会话 + 受限 HTTP/WS 代理；lan → 可信 Wi-Fi 手机
        ├─ scripts/mobile_usage.py ── 手机只读用量（白名单读固定 loopback）
        ├─ scripts/mobile_tunnel.py ── cloudflared → Cloudflare → 手机 HTTPS
        └─ scripts/mobile_relay.py ── frpc → 强制 TLS/CA/IP SAN → 用户 frps → 手机明文 HTTP
assets/vendor/qrcodegen.js + kimi-remote-qr.js + kimi-remote-api.js + kimi-mobile-api.js + kimi-remote-widget.js
                        ── 可选手机三模式连接（摘要校验注入，缺失自动摘除标签）
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
| `KIMI_HOME/usage-dashboard/runtime/cloudflared/2026.9.3` | 固定 CF 组件，插件树外，显式同意后安装 |
| `KIMI_HOME/usage-dashboard/runtime/frp/<version>` | 固定 frp 组件，版本以 `RELAY_VERSION` 为准，显式同意后安装 |

## 目录结构

```text
kimi-code-usage/
├── kimi.plugin.json            # 清单（4 个 hook：SessionStart/Heartbeat/Stop/SessionEnd）
├── LICENSE / README.md / SYSTEM.md
├── assets/
│   ├── kimi-usage-widget.js    # 侧栏卡片+大屏+模型弹窗（注入链最后一个脚本）
│   ├── kimi-remote-widget.js   # 手机浮层（内网 / CF 隧道 / frp 中继）
│   ├── kimi-remote-api.js      # 官方 /api/v1/remote-control 适配层（仅探测旧版官方中继）
│   ├── kimi-mobile-api.js      # 手机控制面适配层（/api/mobile/*）
│   ├── kimi-remote-qr.js       # QR→SVG 桥接（依赖 vendor/qrcodegen.js）
│   ├── vendor/qrcodegen.js     # Project Nayuki qrcodegen（MIT，离线二维码）
│   ├── vendor/cloudflared-2026.9.3-LICENSE  # cloudflared Apache-2.0 原文（固定摘要）
│   ├── vendor/frp-<version>-LICENSE       # frp Apache-2.0 原文（固定版本/摘要）
│   └── kimi-usage.json         # 最新报表副本（Skill 可读）
├── commands/                   # /kimi-code-usage:usage|cache|panel|models|start|update
├── docs/                       # 手机远程连接工作文档、合并文档与最终工作文档
├── deploy/frp/frps.toml.example # 用户自行部署服务器的配置模板
├── scripts/
│   ├── bootstrap.cmd           # 唯一 hook 入口（找 Python → service.py --tick）
│   ├── service.py              # 采集+注入+API+/api/mobile 前置分流（退出只 detach worker）
│   ├── mobile_worker.py        # 独立 worker 进程 + daemon 侧 IPC 客户端（protocol=1）
│   ├── mobile_bridge.py        # 配对/会话 + 三模式受限代理 + WS（公网待验收）
│   ├── mobile_usage.py         # 手机只读用量（SID 同源认证，白名单读固定 loopback）
│   ├── mobile_tunnel.py        # CF 固定组件（历史安装/版本已核实，真实生命周期待验收）
│   ├── mobile_relay.py         # frp 固定安装/校验、强制 TLS、配置与客户端生命周期
│   ├── updater.py              # 自更新助手（本地预览纵深防御 + 完整性闸门，退出码 5/6）
│   └── scanner.py              # wire.jsonl 增量扫描器 + 状态持久化
└── skills/kimi-code-usage/     # 用量解读 Skill
```

## 计费单价

`scripts/scanner.py` 顶部常量（元/百万 Token）：`PRICE_INPUT=20`、`PRICE_CACHE_READ=2`、`PRICE_OUTPUT=100`，缓存创建按输入价。与官方 K3 定价一致，可按需调整。

## 开源协议

插件采用 [MIT License](LICENSE)。离线二维码库为 Project Nayuki MIT；单独下载的 **cloudflared 与 frp 组件采用 Apache-2.0**，不属于插件的 MIT 授权。源码与许可证见 [Cloudflare 官方仓库](https://github.com/cloudflare/cloudflared)与 [frp 官方仓库](https://github.com/fatedier/frp)，完整许可证随插件打包；frp 许可证文件名中的版本以代码 `RELAY_VERSION` 为准。
