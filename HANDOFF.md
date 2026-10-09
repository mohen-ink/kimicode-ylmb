# Kimi Code 用量面板 — 部署与运维说明

当前版本见 [CHANGELOG.md](CHANGELOG.md)；面向用户的安装与功能说明见 [README.md](README.md)。手机远程连接为**预览功能**。

本文面向部署/协作维护：说明运行边界、更新保护、回滚步骤与安全提示。

## 1. 功能概览

- **用量面板**：侧栏实时记录每次模型调用用量，含模型能力管理（识图 / 深度思考 / 工具调用 / 强度档位），与官方配额监控并列。
- **模式滑杆**：侧栏顶部「模式」滑杆，省钱单干 → 主模型 + 1~3 个挂件（子代理）切换；档位存于 `~/.kimi-code/usage-dashboard/modes.json`，可编辑 2~6 档。
- **手机远程连接（预览）**：Cloudflare Quick Tunnel 或自建 Python 中继，扫码配对后在手机浏览器查看会话与只读用量页。
- **自更新**：面板点「更新」检查，弹窗确认后下载 main 分支源码包并重启服务；本地预览形态（`localPreview` / 版本带 `-local`）拦截 GitHub 自更新。

## 2. 部署边界

- **源码**：仓库工作区。修改不会影响正在运行的用量服务。
- **已安装目录**：托管安装目录（形如 `~/.kimi-code/plugins/managed/kimi-code-usage`）。用量服务从该目录加载代码。
- **重启要求**：Python 服务代码在启动时装入内存，`/plugins reload`、新会话与 hook 心跳都不会替换运行中的服务；改动 `scripts/*.py` 后须在实际安装目录执行 `python scripts/service.py --restart`。仅改文档或样式文件无需重启。
- **手机桥**：运行在独立 worker 进程中。用量后台重启不中断已开启的手机连接（同版本 worker 被重连接管）；但桥/worker 代码变更需先「停止」手机连接让旧 worker 退出，再重新开启。
- **桌面注入资产**：由后台按内容摘要自动同步到桌面资源目录，不需要手工复制。
- **组件目录**：`KIMI_HOME/usage-dashboard/runtime/cloudflared/2026.9.3` 位于插件树外，插件更新不覆盖。

## 3. 回滚（保留用户数据）

回滚只回退插件文件，不动用户数据：

1. 若手机连接已开启，先在浮层点「停止」（撤销配对与隧道，worker 退出）。
2. 用目标版本的同名文件覆盖已安装目录（或重新 `/plugins install` 目标目录）。
3. 在实际安装目录执行 `python scripts/service.py --restart`。
4. 验证：`curl --noproxy '*' -s http://127.0.0.1:39281/api/status` 返回预期版本与进程号。
5. 需要手机连接时重新开启并重新扫码（旧隧道地址与配对会话随停止失效，属设计语义，不是数据丢失）。

保留项：用量采集状态文件、价格配置、忽略项记录、桌面路径记录、`KIMI_HOME/usage-dashboard/runtime/`（组件与 worker 状态目录）、桌面 `config.toml` 及其时间戳备份。

## 4. 更新与预览保护

- 面板不会自动更新。点「更新」按钮才检查，有新版本弹窗显示发布时间与内容，**需点击确认**才下载并重启服务；也可按发布说明手动覆盖安装。
- 本地预览形态（清单带 `localPreview` / 版本带 `-local`）会拦截 GitHub 自更新：检查返回 `blocked: true` / `reason: "local_preview"`，应用返回 HTTP 409；更新器自身也有同样的纵深防御（缺少核心运行文件或移动端运行时只给一半时拒绝写入，退出码 5/6）。
- 退出预览形态需按发布说明安装正式版本。

## 5. 手机连接的安全提示

- **配对链接就是当前电脑 Agent 会话的控制授权**：手机可通过会话提示驱动 Agent 执行命令、读写文件。请勿分享二维码或链接。
- 手机不能直连终端或机器级管理接口；全局配置写仅限窄白名单键。
- 外网流量经 Cloudflare TLS 终止，Cloudflare 可见转发内容；**不是 P2P，也不是端到端加密**。
- 自建中继为**明文传输**（`ws://` / `http://`），配对码、Cookie、会话与文件内容经 VPS 时不可保密；`relay_server.py` 未传 `--token` 时不校验注册，仓库不内置 token 默认值。仅在自控服务器、临时联调场景使用，部署见 [自建中继搭建教程](docs/自建中继服务器-搭建教程.md)。
- Quick Tunnel 使用临时随机地址，停止或重开后可能变化，**不承诺永久链接或可用性 SLA**。

## 6. 发版

- 推 `v*` tag 触发 `.github/workflows/release.yml`：校验 tag 与 `kimi.plugin.json` `version` 一致 → `git archive` 打 zip + sha256 → `gh release create`。
- 面板内更新器下载的是 main 分支源码包，不依赖 Release 资产。
- 版本号只在 main 上递增；协作者请从最新 main 拉分支、提交前先 `git pull --rebase`，避免各自发同号版本。
