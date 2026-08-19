from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.agent import AndroidPerformanceAgent
from llm.client import DeepSeekResponsesClient
from tools.adb_tool import AdbDevicesTool, AdbInstallTool, AdbLaunchAppTool
from tools.app_target_tool import InspectAppTargetTool
from tools.benchmark_readiness_tool import InspectBenchmarkReadinessTool
from tools.file_tool import ReadProjectFileTool
from tools.gradle_tool import GradleBuildTool
from tools.macrobenchmark_tool import RunMacrobenchmarkTool
from tools.project_tool import InspectProjectTool
from tools.registry import ToolRegistry
from tools.search_tool import SearchProjectTextTool
from tools.standalone_macrobenchmark_tool import RunStandaloneMacrobenchmarkTool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Android Performance Agent V0.2.7 - minimal Tool Calling agent."
    )
    parser.add_argument("project_path", help="Path to the Android Gradle project.")
    parser.add_argument(
        "--task",
        default="检查这个 Android 项目是否具备继续进行启动性能分析的条件。",
        help="Natural-language task for the agent.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=15,
        help="Maximum number of model/tool loop steps.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Hide intermediate tool execution logs.",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    project_path = Path(args.project_path).expanduser().resolve()

    registry = ToolRegistry()
    registry.register(InspectProjectTool(allowed_project_path=project_path))
    registry.register(GradleBuildTool(allowed_project_path=project_path))
    registry.register(SearchProjectTextTool(allowed_project_path=project_path))
    registry.register(ReadProjectFileTool(allowed_project_path=project_path))
    registry.register(AdbDevicesTool(allowed_project_path=project_path))
    registry.register(InspectAppTargetTool(allowed_project_path=project_path))
    registry.register(AdbInstallTool(allowed_project_path=project_path))
    registry.register(AdbLaunchAppTool(allowed_project_path=project_path))
    registry.register(RunMacrobenchmarkTool(allowed_project_path=project_path))
    registry.register(InspectBenchmarkReadinessTool(allowed_project_path=project_path))
    registry.register(RunStandaloneMacrobenchmarkTool(allowed_project_path=project_path))

    try:
        llm = DeepSeekResponsesClient.from_env()
    except ValueError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        print("请复制 .env.example 为 .env，并配置 DEEPSEEK_API_KEY。", file=sys.stderr)
        return 2

    agent = AndroidPerformanceAgent(
        llm=llm,
        tools=registry,
        project_path=project_path,
        max_steps=args.max_steps,
        verbose=not args.quiet,
    )

    try:
        answer = agent.run(args.task)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\n[Agent 运行失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("\n========== 最终结论 ==========")
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
