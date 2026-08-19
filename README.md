# Android Performance Agent

> 基于 LLM Tool Calling 的 Android 启动性能分析 Agent
> 自动完成：项目检查 → 构建 → 设备检测 → 安装 → Macrobenchmark → Perfetto → 优化候选 → 源码定位

Android Performance Agent 是一个面向 Android 启动性能分析的本地 Agent 工具。

你只需要提供一个 Android 项目路径，Agent 会根据真实 Tool Result 自主决定下一步，并自动完成启动性能分析流程。

当前版本聚焦：

**Measure → Analyze → Plan → Locate**

不自动修改业务代码，不自动提交 Git。

---

## 为什么做这个项目

传统 Android 启动性能分析通常需要手动完成：

- 检查工程与构建环境
- 找 application module / applicationId / Launcher Activity
- 构建 APK
- 连接 Android 真机
- 安装并启动 App
- 配置和运行 Macrobenchmark
- 找 Benchmark JSON
- 找 Perfetto Trace
- 使用 Perfetto / Trace Processor 分析
- 判断 Binder / I/O / CPU / 首帧等瓶颈
- 再回到源码中寻找可能的对应位置

Android Performance Agent 将这些步骤串成一个完整流程：

**Android Project → Agent → Measure → Analyze → Optimization Candidates → Source Localization**

目标不是“让 AI 猜优化建议”，而是让 AI 基于真实性能证据进行分析。

---

## 核心能力

当前支持：

- 自动识别 Android / Gradle 项目
- 自动识别 application module
- 自动识别 benchmark module
- 自动识别 applicationId
- 自动识别 Launcher Activity
- 自动执行 Gradle Build
- 自动检测 ADB 设备
- 自动安装 APK
- 自动验证 App 是否能正常启动
- 自动运行已有 AndroidX Macrobenchmark
- 普通 Android 项目支持 Standalone Macrobenchmark Harness
- 自动读取 Benchmark JSON
- 获取真实 TTID / TTFD
- 自动收集 Perfetto Trace
- 使用 Perfetto Trace Processor SQL 分析启动 Trace
- 分析 First Frame / Binder / I/O / CPU / GC / Application / Activity 等启动阶段
- 自动生成有证据的 Optimization Candidates
- 自动定位可能相关的 Java / Kotlin / Manifest 源码位置
- 输出 HIGH / MEDIUM / LOW Source Match Confidence
- 无法可靠关联源码时返回 `unresolved`
- 提供本地 Web UI 展示完整分析过程

---

## 工作流程

完整分析链路：

`Android Project`

→ `Inspect`

→ `Build`

→ `Device`

→ `Install`

→ `Launch`

→ `Measure`

→ `Analyze`

→ `Plan`

→ `Locate`

### Measure

通过 Macrobenchmark 获取：

- TTID
- TTFD（如果 App 支持）
- Benchmark JSON
- Perfetto Trace

### Analyze

使用 Perfetto Trace Processor SQL 提取：

- App Startup 区间
- First Frame
- Binder
- Main Thread I/O
- Main Thread Running / Runnable
- GC
- Application / Activity Startup Stage
- Dex / Class Loading
- Top Bottlenecks

### Plan

基于真实 Perfetto 结果生成优化候选。

### Locate

尝试将性能证据映射到：

- Java
- Kotlin
- AndroidManifest.xml
- 文件路径
- 行号
- Symbol
- Confidence

无法可靠定位时返回：

`unresolved`

---

## Evidence First

Android Performance Agent 的核心原则是：

**性能结论必须来自真实 Tool Result。**

规则包括：

- 没有 Benchmark，不报告 TTID / TTFD
- TTID / TTFD 必须来自 Benchmark JSON
- 没有 Perfetto Trace，不生成 Trace 级性能结论
- LLM 不直接读取 Perfetto 二进制文件
- Raw Slice 可能嵌套或重叠，不能直接累加
- Bottleneck 排名使用 exclusive startup attribution
- Source Match HIGH 不等于 Performance Severity HIGH
- LOW / MEDIUM 只能作为源码候选
- 找不到可靠源码映射时返回 `unresolved`
- Trace health warning 必须保留并披露
- 不根据常见 Android API 或生命周期名称猜测根因

Perfetto 分析链路：

`Perfetto Trace`

→ `Trace Processor SQL`

→ `Structured Tool Result`

→ `LLM`

---

## Standalone Macrobenchmark

普通 Android 项目通常没有 Macrobenchmark module。

Android Performance Agent 内置 Standalone Macrobenchmark Harness，可以在：

**不修改目标项目源码**

**不修改 settings.gradle**

**不修改业务 Gradle**

**不修改 AndroidManifest.xml**

**不引用目标项目源码**

的情况下，对已经安装的 App 进行启动性能测量。

Harness 通过运行时 `targetPackage` 指定目标应用。

### PoC 结果

真实物理设备验证：

- Internal Macrobenchmark median：`308.227 ms`
- Standalone Macrobenchmark median：`308.734 ms`
- 差异：`+0.164%`
- 4 rounds
- 20 iterations
- 20 Perfetto traces
- 目标项目源码修改：`0`

因此普通 Android 项目也可以直接进入启动性能 Measure 阶段。

---

# 环境要求

| 环境 | 要求 |
|---|---|
| OS | macOS / Linux 推荐 |
| Python | **3.12** |
| Java | **JDK 17 推荐** |
| Android SDK | 必须 |
| ADB | 必须 |
| Android Device | 推荐真实设备 |
| USB Debugging | 必须 |
| Perfetto Trace Processor | Analyze 阶段必须 |
| DeepSeek API Key | 必须 |
| Android Studio | 非强制，但推荐 |

### Python 3.12

检查：

`python3.12 --version`

项目的 `setup.sh` 会自动创建 `.venv`、升级 pip 并安装 `requirements.txt`。

### Java

推荐使用 JDK 17。

检查：

`java -version`

### Android SDK / ADB

推荐安装 Android Studio，并通过 Android Studio 安装 Android SDK、Platform Tools、Build Tools。

检查：

`adb version`

连接设备：

`adb devices`

如果显示 `unauthorized`，请在手机上确认 USB 调试授权。

部分 Xiaomi / MIUI 设备还需要额外开启“开发者选项 → USB 安装”，否则可能出现：

`INSTALL_FAILED_USER_RESTRICTED`

### Perfetto Trace Processor

完整 Analyze 阶段需要：

`trace_processor`

或：

`trace_processor_shell`

如果不在 PATH，可配置：

`TRACE_PROCESSOR_SHELL=/path/to/trace_processor`

### DeepSeek API Key

复制配置：

`cp .env.example .env`

然后编辑 `.env`：

`DEEPSEEK_API_KEY=your_api_key`

`.env` 不应提交到 Git 仓库。

---

# Quick Start

### 1. Clone

`git clone https://github.com/spuermax/Android-Performance-Agent.git`

`cd Android-Performance-Agent`

### 2. 初始化环境

如果脚本没有执行权限：

`chmod +x setup.sh run.sh web.sh test.sh`

然后：

`./setup.sh`

### 3. 配置 DeepSeek

`cp .env.example .env`

填写：

`DEEPSEEK_API_KEY`

### 4. 连接 Android 设备

`adb devices`

确保至少有一台设备状态为：

`device`

正式性能测试推荐使用真实物理设备。

### 5. 启动 Web UI

`./web.sh`

浏览器默认打开：

`http://127.0.0.1:8765`

在页面中填写 Android 项目路径，例如：

`/Users/yourname/projects/MyAndroidApp`

选择：

**完整启动分析**

然后点击：

**开始执行**

---

# Web UI

当前 Web UI 使用结构化 Agent Event，不依赖从终端日志中猜测性能数据。

页面主要包含：

- Project
- Device
- Measure
- Analyze
- Plan
- Locate
- Final Result
- Agent Log

结构：

`Agent`

→ `tool_started`

→ `tool_result`

→ Structured Event

→ Python Web Backend

→ Dashboard

---

# CLI 使用

也可以直接使用命令行：

`./run.sh "/path/to/android/project"`

指定任务：

`./run.sh "/path/to/android/project" --task "执行完整启动性能分析" --max-steps 15`

运行测试：

`./test.sh`

---

# 真实设备运行结果

项目已经在真实 Android 物理设备上完成完整 E2E 验证。

测试环境：

- Device：Xiaomi M2102K1AC
- Android：14
- SDK：34
- ABI：arm64-v8a
- Target：`com.sample.redex`

Macrobenchmark：

- Startup Mode：COLD
- Runs：5
- TTID Min：`272.1 ms`
- TTID Median：`284.8 ms`
- TTID Max：`499.9 ms`
- TTFD：本轮不可用

Perfetto Startup：

- Startup Duration：`289.996 ms`
- First Frame：`68.8 ms`
- Binder：`41.9 ms`
- Main Thread I/O：`37.8 ms`
- Launch Delay：`30.8 ms`
- Main Thread Running：`25.9 ms`
- GC overlap：`0 ms`

Source Localization：

- `MainActivity`：HIGH Source Match
- `MainActivity#onCreate / setContentView`：MEDIUM Candidate
- `SampleApplication`：MEDIUM / LOW Candidate
- Binder：unresolved
- Main Thread I/O：unresolved
- CPU Scheduling：unresolved

该轮完整执行：

**Inspect → Build → Device → Install → Launch → Measure → Analyze → Plan → Locate**

全部成功。

---

# 主要 Tools

当前 Tool Registry 包含：

- `inspect_project`
- `gradle_build`
- `search_project_text`
- `read_project_file`
- `adb_devices`
- `inspect_app_target`
- `adb_install`
- `adb_launch_app`
- `inspect_benchmark_readiness`
- `run_macrobenchmark`
- `run_standalone_macrobenchmark`
- `analyze_perfetto_trace`
- `generate_startup_optimization_plan`
- `locate_startup_bottleneck_source`

Tool 负责真实事实提取。

LLM 负责决定下一步、解释 Tool Result、组织最终分析结论。

项目不是固定 Workflow，也不依赖 LangChain / LangGraph。

---

# 架构

`Browser / CLI`

→ `AndroidPerformanceAgent`

→ `LLM`

→ `Tool Registry`

→ `Android / Gradle / ADB / Macrobenchmark / Perfetto Tools`

→ `Structured Tool Result`

→ `LLM`

→ `Final Report`

---

# 当前能力边界

当前版本聚焦：

**Android Startup Performance**

当前不做：

- 自动修改业务代码
- 自动提交 Git
- 自动创建 PR
- 自动声称某个候选源码一定是根因
- 在没有 Benchmark 时生成启动耗时
- 在没有 Trace 时生成 Perfetto 级性能结论

其他限制：

- TTFD 依赖 App 是否提供 fully drawn 信号
- Product Flavor 支持仍有限
- Binder / I/O 不一定能可靠定位具体业务源码
- Trace Processor health warning 会保留并披露
- 正式性能数据推荐在物理设备上采集

---

# Roadmap

已完成：

- Project Inspection
- Gradle Build
- ADB Device Detection
- APK Install / Launch Verification
- Existing Macrobenchmark
- Standalone Macrobenchmark Harness
- Benchmark JSON Parsing
- TTID / TTFD
- Perfetto Trace Processor Analysis
- Evidence-based Optimization Plan
- Startup Source Localization
- Structured Web UI

后续方向：

- 更好的 Binder Source Attribution
- 更好的 Main Thread I/O Source Attribution
- 多轮 Benchmark 对比
- Before / After Report
- 报告导出
- 更多 Android 项目结构兼容
- Memory / Jank 等其他性能分析模块

---

# 项目原则

这个项目不会把：

> “AI 觉得这里可能慢”

当作性能结论。

它更关注：

> “真实 Benchmark 和 Perfetto 证据告诉我们哪里值得继续调查。”

因此：

**No Measurement → No Performance Claim**

**No Reliable Source Evidence → Unresolved**

---

## License

请根据仓库最终选择的开源协议补充 License。
