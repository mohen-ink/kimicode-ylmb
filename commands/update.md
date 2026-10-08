检查并应用插件自更新（从 GitHub 仓库拉取最新版本）。

执行步骤：
1. `curl --noproxy '*' -s http://127.0.0.1:39281/api/update/check` 查看当前版本与远端最新版本；
2. 若有新版本且用户确认，执行：
   `curl --noproxy '*' -s -X POST -H 'X-Kimi-Usage-Control: 1' -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:39281/api/update/apply`；
   服务会自动下载更新包、覆盖插件文件并重启，约 5-10 秒恢复；
3. 重启后再次调用 `/api/update/check` 或 `/api/status` 确认版本号已更新。

接口访问合同：所有 GET 读接口（`/api/status`、`/api/update/check` 等）对无 Origin 的 CLI 调用无需任何头即可用，但带外站 Origin 的浏览器请求（foreign/null Origin、`Sec-Fetch-Site: cross-site`）一律被拒；**所有 POST 写接口必须携带 `X-Kimi-Usage-Control: 1`**（跨站浏览器无法伪造，会触发预检被拒），缺头会收到 403。服务仅接受 loopback peer 与严格 Host（防 DNS rebinding），不再回 `Access-Control-Allow-Origin: *`。

注意：服务离线时先执行 /start 拉起后台；网络无法访问 GitHub 时检查代理后重试。
更新只覆盖插件文件，不会动用量数据、价格配置与 desktop_path.txt。

v3.3.3 使用纯数字发布版本，清单不带本地预览标记；旧用户可检查并确认更新。更新弹窗读取 `main` 分支 `CHANGELOG.md` 中对应版本的日期与公告，GitHub 的 Pre-release 标记不会隔离此更新源；手机功能仍为预览，请先阅读公告。

源码方式安装/更新时，`/plugins reload`、新会话与 hook 心跳不会替换已加载的 Python 服务代码。须在**实际安装的插件根目录**执行 `python scripts/service.py --restart`，再用 `/api/status` 确认版本。桥/worker 代码变更须先显式停止手机连接让旧 worker 退出，再重新开启并配对；只重启用量服务不会升级存活的旧 worker。不要从另一份源码副本重启安装目录的服务，也不要关闭主客户端代替后台重启。

## 本地预览保护

若清单 `localPreview: true` 或版本号含 `-local`（如 `v3.3.1-local`）：
- `/api/update/check` 一律返回 `blocked: true`、`reason: "local_preview"`，`force` 也无法绕过；
- `/api/update/apply` 直接拒绝（HTTP 409 `LOCAL_PREVIEW_UPDATE_BLOCKED`），清单损坏时 apply 同样拒绝。
这是刻意设计：预览构建尚未发布，不允许被 GitHub 正式版覆盖。
- 要退出预览：手动安装包含该版本的正式发行包；
- **不要**手动运行 `scripts/updater.py`——即便 2026-10-07 起它自身已加 `local_build_blocked()` 纵深防御（`localPreview: true`、版本含 `-local`、清单缺版本一律拒且一个文件都不动，退出码 5），手工调用仍无意义且可能造成插件目录被卸载/重装流程以外的状态变更。
- `scripts/updater.py` 现另有完整性闸门：缺核心运行文件、或移动端运行时只给一半（如只更新 bridge 不更新 worker）时返回退出码 6 且不写任何文件；同步删除只清 `LOCAL_ONLY_KEEP` 白名单外的普通文件，不再按「包里没有」反推删除 `mobile_*.py`、`kimi-mobile-api.js`、`docs/`、`assets/vendor/`。
