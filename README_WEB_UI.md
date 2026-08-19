# Web UI V0.1

复制到 Android-Performance-Agent 项目根目录：

- web_app.py
- web.sh
- web/index.html

运行：

chmod +x web.sh
./web.sh

打开：http://127.0.0.1:8765

Web UI 只负责交互与日志展示，实际执行仍是：
Browser -> Local Python Server -> run.sh -> AndroidPerformanceAgent -> ToolRegistry -> Tools

安全边界：

- 服务只监听 `127.0.0.1`。
- POST API 校验 localhost Host/Origin 和页面启动时生成的 CSRF token。
- 同一时间只允许一个 Agent 进程。
- 请求体、任务长度和 `max_steps` 都有边界检查。
- 停止或关闭 Web UI 时会终止 Agent 进程组。
