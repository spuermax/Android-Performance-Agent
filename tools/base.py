from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class ToolError(Exception):
    pass


class BaseTool(ABC):
    name: str
    description: str

    def __init__(self, allowed_project_path: Path) -> None:
        self.allowed_project_path = allowed_project_path.expanduser().resolve()

    @property
    @abstractmethod
    def parameters_schema(self) -> dict[str, Any]:
        raise NotImplementedError

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters_schema,
            "strict": True,
        }

    def validate_project_path(self, project_path: str) -> Path:
        candidate = Path(project_path).expanduser().resolve()
        if candidate != self.allowed_project_path:
            raise ToolError(
                "拒绝访问目标项目之外的路径。"
                f" allowed={self.allowed_project_path}, requested={candidate}"
            )
        return candidate

    @abstractmethod
    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
