# Android Performance Agent

> V0.2.7 开发中

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
- `setup_macrobenchmark`
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

`setup_macrobenchmark` 可以为没有 Macrobenchmark 的普通 Android Application
创建基础启动测试环境，包括独立 `com.android.test` module、COLD Startup Benchmark、
非调试 benchmark build type 和 profileable 配置。它只负责搭建并返回
`validation_task`，不会在内部执行 Build 或 Benchmark。

当前版本只安全支持基础 Groovy/Kotlin Gradle DSL。复杂 Product Flavor、动态
applicationId、Convention Plugin、高度自定义 build logic 或非标准多工程结构会返回
阻塞错误，不会猜测或强行修改。

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

V0.2.7 已能为普通 Android Application 创建基础 Macrobenchmark 启动测试环境，也能执行现有 Startup Test、从 Benchmark JSON 读取真实 TTID/TTFD，并定位对应 Perfetto trace。当前只收集 trace 文件，不分析 Perfetto；性能判断与后续决策仍交给 LLM。
项目不使用 RAG、LangChain 或 LangGraph。
