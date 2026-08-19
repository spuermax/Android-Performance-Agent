# Android Startup Analysis Skill

## 目标

以真实测量为基础分析 Android 启动性能，最终形成：

Measure → Analyze → Optimize → Verify

## 标准决策阶段

1. 检查项目环境与 Android/Gradle 工程有效性。
2. 确认项目至少能成功构建目标 Variant。
3. 确认测试设备在线且已授权。
4. 识别明确的 Application Target 和 Launcher Component。
5. 安装已构建 APK 到显式指定的设备。
6. 验证 Package、Launcher Activity 和 App 进程可以正常启动。
7. 如果项目存在可用的 Macrobenchmark Startup Test，优先调用 run_macrobenchmark。
8. 如果项目没有可用的 Macrobenchmark，不修改项目；先调用 inspect_benchmark_readiness。
9. readiness 通过后，Agent 可决定调用 run_standalone_macrobenchmark；未通过则报告真实阻塞。
10. 只从本轮 Benchmark JSON 读取 TTID / TTFD 等正式指标。
11. 定位本轮生成的 Perfetto Trace。
12. 调用 analyze_perfetto_trace 提取主线程、Binder、I/O、GC、CPU 与首帧事实。
13. 调用 generate_startup_optimization_plan 将 Perfetto 事实映射为结构化优化候选。
14. 调用 locate_startup_bottleneck_source，将有真实 evidence 的优化候选关联到目标 module 中的候选源码位置。
15. 重点排查：
   - Application 初始化
   - ContentProvider 自动初始化
   - 第三方 SDK 初始化
   - 主线程 I/O
   - Class Loading
   - Binder 调用
   - GC
   - 首帧绘制
16. 由 LLM 根据性能候选、源码候选及置信度给出最终建议。

## V0.5.1 能力边界

当前可使用：

- inspect_project
- gradle_build
- adb_devices
- inspect_app_target
- adb_install
- adb_launch_app
- run_macrobenchmark
- inspect_benchmark_readiness
- run_standalone_macrobenchmark
- analyze_perfetto_trace
- generate_startup_optimization_plan
- locate_startup_bottleneck_source

这些 Tool 是供 Agent 自主决策的独立能力，不是 Python 固定 Workflow。
当前可以运行已有 Macrobenchmark，也可以对 readiness 通过的已安装 APK 使用 Agent
自带 Standalone Harness。两种方式都从 Benchmark JSON 获取正式 TTID/TTFD，
并可将单个 Perfetto trace 交给 `analyze_perfetto_trace` 提取启动性能事实。
`analyze_perfetto_trace` 不会生成优化结论，解释和建议仍由 LLM 完成。
V0.5.1 可将 Perfetto 事实映射为结构化优化候选，并只读定位相关源码候选，但不修改代码、
不生成 Profile、不执行重新 Measure 或 Before/After。

不能根据 `am start -W` 的 ThisTime、TotalTime 或 WaitTime 判断应用“启动快或慢”。
- TTID / TTFD 的唯一数据源是 AndroidX Benchmark JSON，不解析 Gradle Console 数字。
- 不 suppress DEBUGGABLE、EMULATOR、LOW-BATTERY、NOT-PROFILEABLE 等可靠性错误。

## 决策原则

- 优先真实证据，不凭经验猜测当前项目数据。
- 同一 Tool、相同参数、相同失败原因，不要无条件立即重试；应先判断环境、参数或前置条件是否发生变化。
- `gradle_build` 返回 `BUILD_TIMEOUT` 时，应表述为“构建超时，结果未知”，不能当作“构建失败”。
- 构建失败是性能测试的前置阻塞问题。
- 非 Android 项目应立即停止性能分析。
- 有可用的项目内 Macrobenchmark Startup Test 时，优先调用 `run_macrobenchmark`。
- 没有项目内 Macrobenchmark 时，不得创建 module 或修改用户工程；先调用 `inspect_benchmark_readiness`，通过后再由 Agent 决定是否调用 `run_standalone_macrobenchmark`。
- readiness 未通过时，明确报告设备上实际 APK 的阻塞原因，不 suppress 可靠性错误。
- Macrobenchmark 返回 Trace 后，可由 Agent 决定调用 `analyze_perfetto_trace`；必须传入 Macrobenchmark 目标 `package_name`，不得由 Tool 猜测 Startup 目标，也不得将该步骤写死为 Python Workflow。
- Perfetto 事实必须来自 Trace Processor SQL，不根据文件名、Gradle Console 或 LLM 猜测。
- 瓶颈排名只使用 `android_startup_opinionated_breakdown` 的 exclusive 归因。Raw main-thread Slice 可能嵌套或重叠，只能用于源码定位，禁止相加或作为独立瓶颈排名。
- `bindApplication` raw 父 Slice 不等于业务 `Application.onCreate` 耗时；应同时报告 exclusive `bind_application`，没有类级 Slice 时不得把耗时归因给自定义 Application 类。
- Raw 源码定位 hint 应同时保留最长 Slice 和包含 package/class/method 标识符的 Slice，不得仅按时长截断而丢失强源码 evidence。
- `trace_health=WARNING` 不直接等于分析失败，但最终报告必须披露结构化 `trace_health_issues`。
- GC `total_wall_overlap_ms` 只表示 GC wall duration 与 Startup 区间的重叠，不得解释为 Stop-The-World pause。
- `generate_startup_optimization_plan` 只能消费成功的 `analyze_perfetto_trace` Result，建议必须携带实际 evidence 和优先级。
- 所有优化候选的统一最低证据阈值为 `impact_ms >= 3` 或占 Startup `>= 1%`；未达阈值时，不生成“异步初始化”等泛化建议。
- `locate_startup_bottleneck_source` 只能消费成功的结构化启动优化计划，并要求计划 package 与目标 package 一致。
- 源码定位必须基于优化候选中的真实 evidence；普通 API 或生命周期方法的存在性只能形成带置信度的候选，不能直接证明瓶颈。
- 源码候选按 `category + file_path + symbol` 聚合；同一 symbol 的多次文本命中放入 `matched_lines`，不重复输出。
- Trace 明确包含类名且与 Manifest/Source 一致时可为 HIGH；只有 `bindApplication` 且 Manifest 注册自定义 Application 时只能为 LOW；明确有 Application lifecycle 但无类名时可为 MEDIUM。
- 通用生命周期方法不能跨类别自动获得 MEDIUM：I/O、Binder、Long Task 只有普通 `onCreate` 命中时仍为 LOW；必须有类/方法级 evidence 或类别直接语义关系才能提高置信度。
- 找不到明确关联时返回 `unresolved`，不得猜测；所有读取必须限制在指定项目和 target module 内。
- V0.5.1 不自动修改或提交代码，不生成 Baseline Profile/Startup Profile，不执行重新 Measure 或 Before/After。
