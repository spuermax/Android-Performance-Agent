# Android Startup Analysis Skill

## 目标

以真实测量为基础分析 Android 启动性能，最终形成：

Measure → Analyze → Optimize → Verify

## 标准流程

1. 检查项目环境与 Android/Gradle 工程有效性。
2. 确认项目至少能成功构建目标 Variant。
3. 确认测试设备与测试环境。
4. 使用 Macrobenchmark 获取 TTID / TTFD 等启动指标。
5. 获取启动过程 Perfetto Trace。
6. 分析主线程启动关键路径。
7. 重点排查：
   - Application 初始化
   - ContentProvider 自动初始化
   - 第三方 SDK 初始化
   - 主线程 I/O
   - Class Loading
   - Binder 调用
   - GC
   - 首帧绘制
8. 基于证据给出优化建议。
9. 修改后重新执行 Macrobenchmark。
10. 对比优化前后结果，确认收益和回归风险。

## V0.1 能力边界

当前只有：

- inspect_project
- gradle_build

因此 V0.1 只能回答：

“这个项目目前是否具备继续进入启动性能测量阶段的工程条件？”

不能在没有 Macrobenchmark / Perfetto 数据的情况下判断应用“启动快或慢”。

## 决策原则

- 优先真实证据，不凭经验猜测当前项目数据。
- 构建失败是性能测试的前置阻塞问题。
- 非 Android 项目应立即停止性能分析。
- 没有 benchmark module 不等价于项目无法继续，只表示后续可能需要创建/配置 Macrobenchmark 模块。
