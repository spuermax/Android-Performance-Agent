from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.agent import AndroidPerformanceAgent
from llm.base import LLMResponse, ToolCall
from tools.base import BaseTool
from tools.registry import ToolRegistry


class FakeTool(BaseTool):
    name = "fake_tool"
    description = "fake"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "value": 42}


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def create_response(self, *, instructions: str, input_items: list[Any], tools: list[dict[str, Any]]) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(text="", output_items=[], tool_calls=[ToolCall(call_id="call-1", name="fake_tool", arguments={})])
        return LLMResponse(text="done", output_items=[], tool_calls=[])


def test_agent_emits_structured_events(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(FakeTool(allowed_project_path=tmp_path))
    events: list[dict[str, Any]] = []
    agent = AndroidPerformanceAgent(llm=FakeLLM(), tools=registry, project_path=tmp_path, max_steps=3, verbose=False, event_sink=events.append)  # type: ignore[arg-type]
    assert agent.run("test") == "done"
    assert [event["type"] for event in events] == ["run_started", "tool_started", "tool_result", "final"]
    assert events[2]["result"]["value"] == 42


def test_event_sink_failure_does_not_break_agent(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(FakeTool(allowed_project_path=tmp_path))
    def broken_sink(_event: dict[str, Any]) -> None:
        raise RuntimeError("ui unavailable")
    agent = AndroidPerformanceAgent(llm=FakeLLM(), tools=registry, project_path=tmp_path, max_steps=3, verbose=False, event_sink=broken_sink)  # type: ignore[arg-type]
    assert agent.run("test") == "done"
