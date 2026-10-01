检查并应用插件自更新（从 GitHub 仓库拉取最新版本）。

执行步骤：
1. `curl --noproxy '*' -s http://127.0.0.1:39281/api/update/check` 查看当前版本与远端最新版本；
2. 若有新版本且用户确认，执行 `curl --noproxy '*' -s -X POST http://127.0.0.1:39281/api/update/apply`；
   服务会自动下载更新包、覆盖插件文件并重启，约 5-10 秒恢复；
3. 重启后再次调用 `/api/update/check` 或 `/api/status` 确认版本号已更新。

注意：服务离线时先执行 /start 拉起后台；网络无法访问 GitHub 时检查代理后重试。
更新只覆盖插件文件，不会动用量数据、价格配置与 desktop_path.txt。
