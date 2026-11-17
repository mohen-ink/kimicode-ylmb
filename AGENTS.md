# AGENTS.md

## 编程规范

- 不要擅自进行构建、编译、运行测试或编写测试文件，只允许静态检查。
- 不要臆想用户需求，有不确定的地方需要询问用户。
- 只允许派 explorer subagent。

## 同步规范

把仓库改动同步到已安装插件目录 `~/.kimi-code/plugins/managed/kimi-code-usage`。安装目录是运行树，仓库工作区的改动不会自动生效。

1. 覆盖（跳过本机生成物，与 `scripts/updater.py` 的 `KEEP` 白名单一致）：

   ```bash
   cd /d/Git/kimicode-ylmb
   MSYS_NO_PATHCONV=1 robocopy "D:\Git\kimicode-ylmb" "C:\Users\mohen\.kimi-code\plugins\managed\kimi-code-usage" \
     /E /XF "*.log" "kimi-usage.json" "desktop_path.txt" \
     /XD ".git" "__pycache__" ".sync-backup-*" ".pre-merge-backup-*"
   rm -rf ~/.kimi-code/plugins/managed/kimi-code-usage/scripts/__pycache__
   ```

   `__pycache__` 必须清，否则旧字节码可能遮蔽新代码。

2. 按改动类型生效：
   - 只改 `assets/*.js`：无需额外操作，service 每 2 秒自动复制到 `desktop-dist/assets` 并重写 `index.html` 的 `?v=`，客户端重载页面即可。
   - 改了 `scripts/*.py`：必须在安装目录执行 `py -3 scripts/service.py --restart`；hook 心跳、`/plugins reload`、新会话都不会替换已装入内存的服务。不能从仓库工作区重启。
   - 改了 `mobile_worker.py`：先升 `WORKER_VERSION`（`mobile_worker.py:63`），再在手机浮层点「停止」让旧 worker 退出后重新开启；只重启用量服务不会升级存活 worker。
   - 改了 `kimi.plugin.json`、`commands/`、`skills/`、`SYSTEM.md`：`/plugins reload` 或新会话生效。

3. 验证：

   ```bash
   # 服务版本与进程
   curl --noproxy '*' -s http://127.0.0.1:39281/api/status
   # 资产是否已进 desktop-dist（<资产名> 换成本次改动的文件名，如 kimi-model-tabs.js）
   A=<资产名>
   diff --strip-trailing-cr "assets/$A" ~/AppData/Local/Programs/"Kimi Code"/resources/desktop-dist/assets/"$A"
   grep -o "$A?v=[a-f0-9]*" ~/AppData/Local/Programs/"Kimi Code"/resources/desktop-dist/index.html
   ```

   依次应为：`version` 与 `kimi.plugin.json` 一致、`diff` 无输出、`?v=` 等于该文件 md5 前 12 位。只改了 Python 服务时跳过后两条资产检查，改看 `/api/status` 的 `pid` 是否已变化。

4. 约束：
   - 新增资产文件须先登记到 `scripts/service.py` 的 `REMOTE_ASSETS`（或 `sync_widget_asset()` 的显式元组），否则不会进入 `desktop-dist`。
   - 不要手改 `desktop-dist` 内的副本和 `index.html` 注入，会被 service 覆盖回去。
   - 中继相关（`relay_config.py`、`mobile_relay.py`、`relay_server.py`）须本机与 VPS 同步升级，协议不兼容会直接连不上。

## 提交规范

- commit message 使用中文。
- commit 只做本地提交，不推送远端；用户明确要求推送时才 `git push`。
- 版本号维护（用户要求推 tag 时执行）：
  - 版本号唯一来源：`kimi.plugin.json` 的 `version` 字段（如 `3.4.6` → `3.4.7`）。只改这一处，项目其他文件不存版本号。
  - 默认小版本 +1（第三位）；用户明确指定版本号或要求升大版本（次版本/主版本）时按用户要求，不套默认。
  - CHANGELOG（`CHANGELOG.md`）必须同步补全：在文件顶部插入新版本小节，格式为 `## v<新版本号> · <发布日期>`（如 `## v3.4.7 · 2026-10-14`），日期用当天实际日期；分组标题用 `###`（如「新增：」「变更：」「修复：」），条目用 `-`。小节内容概括本次改动，按分组归类；没有对应分组的就不写该分组。
  - 完整提交顺序：改版本号 → 补 CHANGELOG → commit → `git push` 推送远端 → `git tag vX.Y.Z`（带 `v` 前缀，如 `v3.4.7`）→ `git push origin vX.Y.Z` 推 tag。
