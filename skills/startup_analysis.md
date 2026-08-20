# Android Startup Analysis Skill

## 目标

以真实测量为基础分析 Android 启动性能，最终形成：

Measure → Analyze → Optimize → Verify

## 标准决策阶段

1. 检查项目环境与 Android/Gradle 工程有效性。
2. 识别真实 Application Target 和 Launcher Component。
3. 确认测试设备在线且已授权。
4. 如果项目存在可用的 Macrobenchmark Startup Test，优先调用 `run_macrobenchmark`。
5. 如果项目没有可用 Macrobenchmark module，不修改工程；对真实 application module 调用 `inspect_build_variants`。
6. `inspect_build_variants` 只根据真实 Gradle assemble tasks 枚举 Candidate，并按 Benchmark/Release-like 优先、Debug 最后排序；设备 manufacturer 不能作为 flavor 选择依据。
7. 对候选 Target 依次执行：`gradle_build` → `adb_install` → `adb_launch_app` → `inspect_benchmark_readiness`。
8. 单个 Candidate 构建/安装/readiness 失败时记录原因并继续后续候选；只有候选合理穷尽且无一 readiness 通过，才判定 Measure BLOCKED。
9. Debug Variant 只用于构建/安装/启动验证，不用于正式 Macrobenchmark。
10. readiness 通过后调用 `run_standalone_macrobenchmark`，从本轮 Benchmark JSON 读取 TTID/TTFD，并取得 Perfetto Trace。
11. 调用 `analyze_perfetto_trace` 提取主线程、Binder、I/O、GC、CPU 与首帧事实。
12. 调用 `generate_startup_optimization_plan` 将 Perfetto 事实映射为结构化优化候选。
13. 调用 `locate_startup_bottleneck_source`，将有真实 evidence 的优化候选关联到目标 module 中的候选源码位置。
14. 重点排查 Application 初始化、ContentProvider 自动初始化、第三方 SDK 初始化、主线程 I/O、Class Loading、Binder、GC 与首帧绘制。
15. 由 LLM 根据性能候选、源码候选及置信度给出最终建议。

## V0.5.2 能力边界

当前新增：

- `inspect_build_variants`：只读枚举真实 Gradle assemble Variant，并给出确定性的候选排序。
- Benchmark Target Selection：优先尝试 Benchmark/Release-like Variant；单个 Candidate 失败不再立即终止整个 Measure。
- Secret Redaction：项目文件、Tool Result、Agent Log 与 Web event 中的常见密码、token、API key、username 等在进入模型/UI 前脱敏。
- Web blocked 状态：readiness 阻塞时显示 Blocked，后续 Analyze/Plan/Locate 标为 Skipped，而不是错误显示 Completed。

V0.5.2 仍然不自动修改 Manifest，不添加 `<profileable android:shell="true" />`，不添加 ProfileInstaller，不修改 signingConfig，不创建 benchmark module，不修改业务源码。

## 决策原则

- 优先真实证据，不凭经验猜测当前项目数据。
- 有多个 application module 时先识别真实 Target，再枚举该 Target 的 Variant。
- 不根据 Xiaomi/Huawei/Oppo 等设备品牌自动选择同名 Product Flavor。
- `inspect_build_variants` 的 build type / signing 字段属于 best-effort metadata；最终是否可正式测量必须以“实际构建 + 实际安装 APK + `inspect_benchmark_readiness`”为准。
- 同一 Candidate、相同参数、相同失败原因，不要无条件立即重试；应继续下一个候选或报告真实阻塞。
- `gradle_build` 返回 `BUILD_TIMEOUT` 时，应表述为“构建超时，结果未知”，不能当作“构建失败”。
- 有项目内 Macrobenchmark 时优先 `run_macrobenchmark`；没有时走 Standalone Harness，不创建 benchmark module。
- readiness 失败不得 suppress DEBUGGABLE、NOT-PROFILEABLE 等可靠性错误；但单个 Candidate readiness 失败不等于整个项目无可测 Target。
- TTID / TTFD 的唯一数据源是 AndroidX Benchmark JSON，不解析 `am start -W` 数字。
- Perfetto 事实必须来自 Trace Processor SQL，不根据文件名、Gradle Console 或 LLM 猜测。
- 瓶颈排名只使用 `android_startup_opinionated_breakdown` exclusive 归因。
- `bindApplication` raw 父 Slice 不等于业务 `Application.onCreate`。
- `trace_health=WARNING` 不直接等于分析失败，但最终报告必须披露 `trace_health_issues`。
- 优化候选最低证据阈值为 `impact_ms >= 3` 或占 Startup `>= 1%`。
- 找不到明确源码关联时返回 `unresolved`，不得猜测。
