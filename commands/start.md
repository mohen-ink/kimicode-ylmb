---
name: start
description: 启动/自愈 Kimi Code 用量面板后台服务（采集+注入+39281 API）
---

请帮助用户拉起 **Kimi Code 用量面板** 后台服务。

执行步骤（用 Bash 工具）：

1. 运行插件入口脚本（等价于 hook 触发，幂等）：

   ```bash
   cmd //c "$USERPROFILE/.kimi-code/plugins/managed/kimi-code-usage/scripts/bootstrap.cmd"
   ```

   该脚本会找到本机 Python 并执行 `service.py --tick`（拉起常驻服务或触发一次刷新）。

2. 验证服务已接管：访问 `http://127.0.0.1:39281/api/status`，应返回 `{"status":"ok","name":"kimi-code-usage", ...}`。注意绕过系统代理（curl 加 `--noproxy '*'` 或 urllib 用空 ProxyHandler）。

3. 向用户报告：端口、pid、以及面板数据文件 `desktop-dist/assets/kimi-usage-data.js` 的更新时间。

说明：服务平时随会话 hook（SessionStart/Heartbeat）自动拉起，本命令只是手动兜底。参数：$ARGUMENTS
