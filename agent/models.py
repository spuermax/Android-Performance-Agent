from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolEvent:
    step: int
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


@dataclass
class AgentRunState:
    task: str
    project_path: str
    max_steps: int
    step_count: int = 0
    tool_events: list[ToolEvent] = field(default_factory=list)

    @property
    def can_continue(self) -> bool:
        return self.step_count < self.max_steps
