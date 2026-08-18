# Android Performance Agent

> V0.2 开发中（当前稳定基线：V0.1.2）

使用 Python 3.12 + DeepSeek 的 Android 性能分析 Agent，通过 LLM Tool Calling 自主选择并调用工具。

当前提供：

- `inspect_project`
- `gradle_build`
- `search_project_text`
- `read_project_file`
- `adb_devices`
- Android module 类型识别
- DeepSeek Agent Loop

`adb_devices` 可以检查本机 ADB 环境，以及 Android 真机和模拟器的连接状态，
为后续 Macrobenchmark 自动执行做准备。Macrobenchmark 尚未实现。

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

V0.2 正在按阶段开发。目前只加入了 ADB 设备发现，尚未实现 adb_install、应用启动、Macrobenchmark 或 Perfetto。
项目不使用 RAG、LangChain 或 LangGraph。
