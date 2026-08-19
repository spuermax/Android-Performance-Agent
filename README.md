# Android Performance Agent

> V0.5 开发中

使用 Python 3.12 + DeepSeek 的 Android 性能分析 Agent，通过 LLM Tool Calling 自主选择并调用工具。

当前提供：

- `inspect_project`
- `gradle_build`
- `search_project_text`
- `read_project_file`
- `adb_devices`
- `inspect_app_target`
- `adb_install`
- `adb_launch_app`
- `run_macrobenchmark`
- `inspect_benchmark_readiness`
- `run_standalone_macrobenchmark`
- `analyze_perfetto_trace`
- `generate_startup_optimization_plan`
- `locate_startup_bottleneck_source`
- Android module 类型识别
- DeepSeek Agent Loop

`adb_devices` 可以检查本机 ADB 环境，以及 Android 真机和模拟器的连接状态，
为 Macrobenchmark 自动执行做准备。

`inspect_app_target` 可以识别后续 Macrobenchmark 要测试的 application module、
显式 applicationId 和 Launcher Activity。当前默认静态分析 `debug` variant；
product flavors 仍可能需要后续通过构建产物或 ADB 确认。

`adb_install` 可以把项目目录内已经构建好的 APK 安装到显式指定的在线设备。
它不会自动选择设备或触发 Build；`gradle_build` 在 assemble 成功后会返回匹配的
`apk_outputs`，存在多个 APK 时全部返回，由 Agent 决定下一步并要求明确目标。

`adb_launch_app` 会在显式指定的设备上验证 package 已安装、Launcher Component
可以启动且目标进程存在。它只做 Launch Verification；不会 force-stop，也不会把
`am start -W` 的时间字段作为 TTID、TTFD 或正式启动性能数据。

`run_macrobenchmark` 可以运行项目中已经存在的 AndroidX Macrobenchmark Startup
Test，并只从本轮生成的 Benchmark JSON 读取真实 TTID/TTFD，同时定位对应的
Perfetto trace 文件。Gradle Console 仅用于错误诊断，不作为性能指标来源。
Tool 不会自动 suppress DEBUGGABLE、EMULATOR、LOW-BATTERY 或 NOT-PROFILEABLE，
也不会创建或修改 Benchmark 测试。

`inspect_benchmark_readiness` 会检查指定设备上实际安装的目标 APK 是否为
non-debuggable、是否支持 profileable by shell，以及当前 COLD Macrobenchmark 所需的
ProfileInstaller 条件。它检查的是设备中的 APK，不根据源码或 Gradle 配置猜测。

对于本身没有 Macrobenchmark module 的普通 Android 项目，
`run_standalone_macrobenchmark` 可以使用 Agent 自带的独立 self-instrumenting Harness，
通过运行时 `targetPackage` 测量已经安装的目标 App。Harness 不引用目标项目源码，
不会修改目标项目的 settings、build 文件、Manifest 或业务代码。正式 TTID/TTFD
仍只读取本轮 Benchmark JSON，并同时收集本轮 Perfetto trace。

`analyze_perfetto_trace` 使用官方 Perfetto Trace Processor SQL 分析单个启动
Trace，返回 App Startup 区间、主线程长 Slice、Binder、I/O、GC、CPU
调度、首帧阶段和排序后的瓶颈事实。Tool 不会让 LLM 直接读取原始
二进制 Trace，也不在 Tool 内生成优化建议。运行前需要在 `PATH` 中安装
`trace_processor_shell`/`trace_processor`，或配置 `TRACE_PROCESSOR_SHELL`。
Tool 必须同时接收 Macrobenchmark 返回的目标 `package_name`，SQL 只分析
该 package 的 Startup；找不到或出现多个目标 Startup 时会停止，不会猜测。
GC 输出的 `total_wall_overlap_ms` 是 GC wall duration 与启动区间的重叠，
不表示 Stop-The-World pause。

`generate_startup_optimization_plan` 只消费成功的 `analyze_perfetto_trace`
结构化结果，根据 Application/Provider、I/O、Binder、GC wall overlap、
CPU、主线程长 Slice 和 Dex/Class 等真实证据生成带优先级的优化候选。
统一最低证据阈值为 `impact_ms >= 3` 或占 Startup `>= 1%`；
未达阈值时不会生成泛化建议。Tool 不修改代码、不生成
Baseline/Startup Profile，也不执行重新测量。

`locate_startup_bottleneck_source` 根据 V0.4 优化候选携带的真实 evidence，
在指定 application module 内定位可能相关的 Java/Kotlin 源码与 Manifest 配置。
结果包含文件、行号、符号、置信度和关联原因；无法可靠关联时返回 `unresolved`，
不会因为项目中存在 `Application.onCreate` 或常见 API 就断言它是瓶颈。
Tool 复用受项目路径约束的文本搜索与文件读取能力，只读且不访问项目目录外文件。

## 最简单的使用流程

需要预先安装 Python 3.12。

第一次运行：

```bash
./setup.sh
```

根据示例创建配置，并填写自己的 DeepSeek API Key：

```bash
cp .env.example .env
```

`.env` 已加入 `.gitignore`，禁止提交 API Key。

以后运行：

```bash
./run.sh "/Android项目路径"
```

执行全部单元测试：

```bash
./test.sh
```

也可以透传 `main.py` 的其他参数：

```bash
./run.sh "/Android项目路径" --task "检查构建失败原因" --max-steps 6
```

## 架构说明

本项目保持 Tool-Using Agent 架构，不是固定 Workflow。每一轮由 LLM 根据用户目标、上下文和 Tool 描述决定调用哪个 Tool，或直接给出最终结论。

所有项目读取和构建工具都绑定最初指定的 Android 项目目录，不能越界访问其他路径。Gradle 输出会在 Tool Layer 中清洗和分类，再交给 LLM 判断，以减少噪音和上下文开销。

V0.5 在 Perfetto Startup 事实和结构化优化候选之上增加源码位置候选。
Tool 负责事实提取、证据映射和只读源码定位，最终解释与方案取舍仍由 LLM 决定；
本版本不会自动修改或提交业务代码，也不执行 Before/After。
项目不使用 RAG、LangChain 或 LangGraph。
