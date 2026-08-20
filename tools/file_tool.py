from __future__ import annotations

from typing import Any

from tools.base import BaseTool, ToolError
from tools.redaction import redact_text


class ReadProjectFileTool(BaseTool):
    name = "read_project_file"
    description = (
        "读取当前 Android 项目内的文本文件，可按行号范围读取。"
        "适合查看 settings.gradle、build.gradle、AndroidManifest.xml、"
        "gradle.properties、Kotlin/Java 源码等。敏感凭据会在返回前自动脱敏。"
    )

    MAX_LINES = 250

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "用户指定的 Android 项目绝对路径。",
                },
                "relative_path": {
                    "type": "string",
                    "description": "项目根目录内的相对文件路径。",
                },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "起始行号，从 1 开始。",
                },
                "end_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "结束行号，包含该行。",
                },
            },
            "required": ["project_path", "relative_path", "start_line", "end_line"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_path = arguments.get("project_path")
        relative_path = arguments.get("relative_path")
        start_line = arguments.get("start_line")
        end_line = arguments.get("end_line")

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("project_path 必须是非空字符串")
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ToolError("relative_path 必须是非空字符串")
        if not isinstance(start_line, int) or start_line < 1:
            raise ToolError("start_line 必须 >= 1")
        if not isinstance(end_line, int) or end_line < start_line:
            raise ToolError("end_line 必须 >= start_line")
        if end_line - start_line + 1 > self.MAX_LINES:
            raise ToolError(f"单次最多读取 {self.MAX_LINES} 行")

        project = self.validate_project_path(raw_path)
        candidate = (project / relative_path).resolve()

        try:
            candidate.relative_to(project)
        except ValueError as exc:
            raise ToolError("拒绝读取项目目录之外的文件") from exc

        if not candidate.exists():
            return {
                "success": False,
                "error_type": "FILE_NOT_FOUND",
                "relative_path": relative_path,
            }
        if not candidate.is_file():
            return {
                "success": False,
                "error_type": "NOT_A_FILE",
                "relative_path": relative_path,
            }

        lines = candidate.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()

        selected = lines[start_line - 1:end_line]
        return {
            "success": True,
            "relative_path": relative_path,
            "total_lines": len(lines),
            "start_line": start_line,
            "end_line": min(end_line, len(lines)),
            "content": [
                {"line": start_line + i, "text": redact_text(line)}
                for i, line in enumerate(selected)
            ],
        }
