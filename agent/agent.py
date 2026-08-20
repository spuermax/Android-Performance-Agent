from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from agent.context import build_agent_instructions, load_startup_skill
from agent.models import AgentRunState, ToolEvent
from llm.base import BaseLLMClient
from tools.redaction import redact_value
from tools.registry import ToolRegistry

AgentEventSink = Callable[[dict[str, Any]], None]


class AndroidPerformanceAgent:
    """Dynamic LLM Tool Calling loop with optional structured event output.

    Tool selection remains controlled by the model; ``event_sink`` only mirrors
    lifecycle and Tool Result facts for consumers such as the local Web UI.
    """

    def __init__(
        self,
        llm: BaseLLMClient,
        tools: ToolRegistry,
        project_path: Path,
        max_steps: int = 6,
        verbose: bool = True,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps 必须大于 0")

        self.llm = llm
        self.tools = tools
        self.project_path = project_path.resolve()
        self.max_steps = max_steps
        self.verbose = verbose
        self.event_sink = event_sink

        startup_skill = load_startup_skill()
        self.instructions = build_agent_instructions(
            project_path=self.project_path,
            startup_skill=startup_skill,
        )

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self.event_sink is None:
            return
        try:
            safe_payload = redact_value(payload)
            self.event_sink({"type": event_type, **safe_payload})
        except Exception:
            # UI/telemetry failure must never break the Agent core loop.
            return

    def run(self, task: str) -> str:
        state = AgentRunState(
            task=task,
            project_path=str(self.project_path),
            max_steps=self.max_steps,
        )
        self._emit(
            "run_started",
            task=task,
            project_path=str(self.project_path),
            max_steps=self.max_steps,
        )

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

        try:
            while state.can_continue:
                state.step_count += 1

                response = self.llm.create_response(
                    instructions=self.instructions,
                    input_items=input_items,
                    tools=self.tools.schemas(),
                )
                input_items.extend(response.output_items)

                if not response.tool_calls:
                    final_text = (response.text or "").strip()
                    if not final_text:
                        final_text = "Agent 已停止，但模型没有返回最终文本。"
                    self._emit(
                        "final",
                        text=final_text,
                        step_count=state.step_count,
                        reached_max_steps=False,
                    )
                    return final_text

                for tool_call in response.tool_calls:
                    safe_arguments = redact_value(tool_call.arguments)
                    if not isinstance(safe_arguments, dict):
                        safe_arguments = {}

                    self._emit(
                        "tool_started",
                        step=state.step_count,
                        name=tool_call.name,
                        arguments=safe_arguments,
                    )

                    if self.verbose:
                        print(
                            f"[Agent step {state.step_count}] "
                            f"调用 Tool: {tool_call.name} "
                            f"{json.dumps(safe_arguments, ensure_ascii=False)}"
                        )

                    result = self.tools.execute(
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                    )
                    safe_result = redact_value(result)
                    if not isinstance(safe_result, dict):
                        safe_result = {
                            "success": False,
                            "error_type": "INVALID_TOOL_RESULT",
                            "message": "Tool Result 不是对象。",
                        }

                    state.tool_events.append(
                        ToolEvent(
                            step=state.step_count,
                            name=tool_call.name,
                            arguments=safe_arguments,
                            result=safe_result,
                        )
                    )

                    self._emit(
                        "tool_result",
                        step=state.step_count,
                        name=tool_call.name,
                        arguments=safe_arguments,
                        result=safe_result,
                    )

                    if self.verbose:
                        print(
                            "[Tool Result] "
                            + json.dumps(safe_result, ensure_ascii=False, indent=2)
                        )

                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": tool_call.call_id,
                            "output": json.dumps(safe_result, ensure_ascii=False),
                        }
                    )

            final_text = (
                f"Agent 达到最大执行步数 {self.max_steps}，已停止以避免无限循环。"
                "请查看上方 Tool Result，或提高 --max-steps 后重试。"
            )
            self._emit(
                "final",
                text=final_text,
                step_count=state.step_count,
                reached_max_steps=True,
            )
            return final_text
        except Exception as exc:
            self._emit(
                "run_failed",
                error_type=type(exc).__name__,
                message=str(exc),
                step_count=state.step_count,
            )
            raise
