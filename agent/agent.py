from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.context import build_agent_instructions, load_startup_skill
from agent.models import AgentRunState, ToolEvent
from llm.base import BaseLLMClient
from tools.registry import ToolRegistry


class AndroidPerformanceAgent:
    """
    Minimal Agent Loop:
      User Goal
        -> LLM decides whether to call a tool
        -> application executes tool
        -> tool result is returned to LLM
        -> LLM decides next action
        -> final answer

    注意：这里没有把 inspect_project -> gradle_build 写死。
    Tool 的选择由模型通过 Function/Tool Calling 动态决定。
    """

    def __init__(
        self,
        llm: BaseLLMClient,
        tools: ToolRegistry,
        project_path: Path,
        max_steps: int = 6,
        verbose: bool = True,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps 必须大于 0")

        self.llm = llm
        self.tools = tools
        self.project_path = project_path.resolve()
        self.max_steps = max_steps
        self.verbose = verbose

        startup_skill = load_startup_skill()
        self.instructions = build_agent_instructions(
            project_path=self.project_path,
            startup_skill=startup_skill,
        )

    def run(self, task: str) -> str:
        state = AgentRunState(
            task=task,
            project_path=str(self.project_path),
            max_steps=self.max_steps,
        )

        # Responses API 的 input 是一个持续累积的上下文列表。
        input_items: list[Any] = [
            {
                "role": "user",
                "content": (
                    f"目标项目：{self.project_path}\n"
                    f"任务：{task}\n"
                    "请自行判断下一步，需要真实信息时调用工具。"
                ),
            }
        ]

        while state.can_continue:
            state.step_count += 1

            response = self.llm.create_response(
                instructions=self.instructions,
                input_items=input_items,
                tools=self.tools.schemas(),
            )

            # 官方 Responses API 的工具调用流程要求保留模型输出，
            # 再把 function_call_output 追加到下一轮输入。
            input_items.extend(response.output_items)

            if not response.tool_calls:
                final_text = (response.text or "").strip()
                if final_text:
                    return final_text
                return "Agent 已停止，但模型没有返回最终文本。"

            for tool_call in response.tool_calls:
                if self.verbose:
                    print(
                        f"[Agent step {state.step_count}] "
                        f"调用 Tool: {tool_call.name} "
                        f"{json.dumps(tool_call.arguments, ensure_ascii=False)}"
                    )

                result = self.tools.execute(
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                )

                state.tool_events.append(
                    ToolEvent(
                        step=state.step_count,
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                        result=result,
                    )
                )

                if self.verbose:
                    print(
                        "[Tool Result] "
                        + json.dumps(result, ensure_ascii=False, indent=2)
                    )

                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_call.call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )

        return (
            f"Agent 达到最大执行步数 {self.max_steps}，已停止以避免无限循环。"
            "请查看上方 Tool Result，或提高 --max-steps 后重试。"
        )
