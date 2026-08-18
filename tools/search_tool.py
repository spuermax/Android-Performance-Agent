from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError


class SearchProjectTextTool(BaseTool):
    name = "search_project_text"
    description = (
        "在当前 Android 项目范围内搜索文本。"
        "适合定位 Gradle Plugin ID、applicationId、Manifest 配置、类名、方法名等。"
        "自动跳过 build、.gradle、.git 等生成目录。"
    )

    SKIP_DIRS = {
        ".git",
        ".gradle",
        ".idea",
        ".venv",
        "build",
        "captures",
        "node_modules",
        "out",
    }

    TEXT_SUFFIXES = {
        ".gradle", ".kts", ".properties", ".xml", ".kt", ".java",
        ".toml", ".txt", ".md", ".json", ".yaml", ".yml", ".pro",
    }

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "用户指定的 Android 项目绝对路径。",
                },
                "query": {
                    "type": "string",
                    "description": "要搜索的文本，例如 io.github.androidinsight。",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "最多返回多少条匹配结果。",
                },
            },
            "required": ["project_path", "query", "max_results"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_path = arguments.get("project_path")
        query = arguments.get("query")
        max_results = arguments.get("max_results")

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("project_path 必须是非空字符串")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query 必须是非空字符串")
        if not isinstance(max_results, int) or not 1 <= max_results <= 100:
            raise ToolError("max_results 必须在 1-100 之间")

        project = self.validate_project_path(raw_path)

        matches: list[dict[str, Any]] = []
        scanned_files = 0

        for root, dirs, files in os.walk(project):
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
            root_path = Path(root)

            for filename in files:
                path = root_path / filename
                if (
                    path.suffix.lower() not in self.TEXT_SUFFIXES
                    and filename not in {
                        "gradlew",
                        "settings.gradle",
                        "build.gradle",
                        "gradle.properties",
                    }
                ):
                    continue

                scanned_files += 1

                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                for line_no, line in enumerate(content.splitlines(), start=1):
                    if query.lower() not in line.lower():
                        continue

                    matches.append(
                        {
                            "file": str(path.relative_to(project)),
                            "line": line_no,
                            "text": line.strip()[:500],
                        }
                    )

                    if len(matches) >= max_results:
                        return {
                            "success": True,
                            "query": query,
                            "match_count": len(matches),
                            "truncated": True,
                            "scanned_files": scanned_files,
                            "matches": matches,
                        }

        return {
            "success": True,
            "query": query,
            "match_count": len(matches),
            "truncated": False,
            "scanned_files": scanned_files,
            "matches": matches,
        }
