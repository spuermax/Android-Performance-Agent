# Web UI V0.2

V0.2 在 V0.1 本地 Web Shell 基础上增加结构化性能控制台。

## 运行

先按项目根目录 README 完成环境配置，然后执行：

```bash
./web.sh
```

浏览器默认打开 `http://127.0.0.1:8765`。

## 数据链路

Web UI 继续复用原有 Agent 架构：

```text
Browser
→ Local Python Server
→ run.sh --event-stream
→ AndroidPerformanceAgent
→ ToolRegistry
→ Tools
```

`event_stream` 只是 Agent 的可选结构化输出，不改变 Tool Calling 决策过程。普通
CLI 不传 `--event-stream` 时行为保持不变。

结构化控制台展示：

- Project：目标 module、package 和 Launcher Activity。
- Device：实际连接或执行测量的设备信息。
- Measure：Benchmark JSON 中的 TTID Min / Median / Max / Runs。
- Analyze：Perfetto Startup 区间、Trace Health 和 Top Bottlenecks。
- Plan：基于真实 Perfetto evidence 生成的优化候选。
- Locate：源码文件、行号、symbol、confidence 和 unresolved 项。
- 最终结论与原始 Agent 日志分别展示。

核心实现：

- Agent 新增可选 `event_sink`。
- `main.py --event-stream` 输出固定前缀 JSON 事件。
- Web 后端只消费机器事件，不从最终 LLM 文本里提取性能指标。
- 主动 Stop 显示 `Stopped`。

## 安全边界

- 服务只监听 `127.0.0.1`。
- POST API 校验 localhost Host/Origin 和页面启动时生成的 CSRF token。
- 同一时间只允许一个 Agent 进程。
- 请求体、项目路径、任务类型/长度和 `max_steps` 均有边界检查。
- 停止、异常或关闭 Web UI 时会终止 Agent 进程组。
- Agent 输出按 UTF-8 解码，异常字节使用替换字符，不中断日志读取。

## V0.2 验收标准

1. CLI 默认行为不变。
2. Web 页面指标来自真实 Tool Result。
3. TTID/Perfetto/Source Location 可以结构化展示。
4. LOW/MEDIUM/unresolved 原语义保持不变。
5. 原始 Agent Log 仍保留。
