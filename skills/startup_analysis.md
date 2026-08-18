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
7. 检查项目是否已经存在 Macrobenchmark module。
8. 已存在时查找并运行现有 Startup Benchmark；不存在时先调用 setup_macrobenchmark。
9. setup_macrobenchmark 成功后，由 Agent 决定调用 gradle_build 执行 validation_task。
10. 运行 Macrobenchmark Startup Test。
11. 只从本轮 Benchmark JSON 读取 TTID / TTFD 等正式指标。
12. 定位本轮生成的 Perfetto Trace。
13. 在后续阶段分析主线程启动关键路径。
14. 重点排查：
   - Application 初始化
   - ContentProvider 自动初始化
   - 第三方 SDK 初始化
   - 主线程 I/O
   - Class Loading
   - Binder 调用
   - GC
   - 首帧绘制
15. 基于证据给出优化建议。
16. 修改后重新执行 Macrobenchmark。
17. 对比优化前后结果，确认收益和回归风险。

## V0.2.7 能力边界

当前可使用：

- inspect_project
- gradle_build
- adb_devices
- inspect_app_target
- adb_install
- adb_launch_app
- run_macrobenchmark
- setup_macrobenchmark

这些 Tool 是供 Agent 自主决策的独立能力，不是 Python 固定 Workflow。
当前可以运行已有 Macrobenchmark，并从 Benchmark JSON 获取正式 TTID/TTFD，
同时定位 Perfetto trace 文件；尚不能分析 Perfetto 内容。

不能根据 `am start -W` 的 ThisTime、TotalTime 或 WaitTime 判断应用“启动快或慢”。
- TTID / TTFD 的唯一数据源是 AndroidX Benchmark JSON，不解析 Gradle Console 数字。
- 不 suppress DEBUGGABLE、EMULATOR、LOW-BATTERY、NOT-PROFILEABLE 等可靠性错误。
- `has_benchmark_module=true` 时跳过 setup_macrobenchmark，直接查找现有 Startup Benchmark。
- `has_benchmark_module=false` 时先 inspect_app_target；目标明确后调用 setup_macrobenchmark，再由 Agent 调用 gradle_build 验证返回的 validation_task。
- setup_macrobenchmark 只搭建环境，不在 Tool 内执行 Build、安装、Benchmark 或固定 Workflow。

## 决策原则

- 优先真实证据，不凭经验猜测当前项目数据。
- 同一 Tool、相同参数、相同失败原因，不要无条件立即重试；应先判断环境、参数或前置条件是否发生变化。
- `gradle_build` 返回 `BUILD_TIMEOUT` 时，应表述为“构建超时，结果未知”，不能当作“构建失败”。
- 构建失败是性能测试的前置阻塞问题。
- 非 Android 项目应立即停止性能分析。
- 没有 benchmark module 不等价于项目无法继续，只表示后续可能需要创建/配置 Macrobenchmark 模块。
