from __future__ import annotations

from typing import Any

from tools.base import BaseTool, ToolError
from tools.redaction import redact_value


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool 已注册: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.openai_schema() for tool in self._tools.values()]

    @staticmethod
    def _safe_result(value: dict[str, Any]) -> dict[str, Any]:
        redacted = redact_value(value)
        if isinstance(redacted, dict):
            return redacted
        return {
            "success": False,
            "error_type": "INVALID_TOOL_RESULT",
            "message": "Tool Result 不是对象。",
        }

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return self._safe_result({
                "success": False,
                "error_type": "UNKNOWN_TOOL",
                "message": f"未知 Tool: {name}",
            })

        if "_invalid_json" in arguments:
            return self._safe_result({
                "success": False,
                "error_type": "INVALID_ARGUMENTS",
                "message": "模型生成的 Tool 参数不是合法 JSON。",
                "raw_arguments": arguments["_invalid_json"],
            })

        try:
            return self._safe_result(tool.execute(arguments))
        except ToolError as exc:
            return self._safe_result({
                "success": False,
                "error_type": "TOOL_VALIDATION_ERROR",
                "message": str(exc),
            })
        except Exception as exc:
            return self._safe_result({
                "success": False,
                "error_type": "TOOL_RUNTIME_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
            })
