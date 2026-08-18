from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    output_items: list[Any]
    tool_calls: list[ToolCall]


class BaseLLMClient(ABC):
    @abstractmethod
    def create_response(
        self,
        *,
        instructions: str,
        input_items: list[Any],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        raise NotImplementedError
