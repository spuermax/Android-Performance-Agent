from __future__ import annotations

from pathlib import Path

import pytest

from tools.base import ToolError
from tools.gradle_tool import GradleBuildTool


def test_gradle_tool_reports_missing_wrapper(tmp_path: Path) -> None:
    tool = GradleBuildTool(allowed_project_path=tmp_path)
    result = tool.execute(
        {
            "project_path": str(tmp_path),
            "task": "assembleDebug",
        }
    )

    assert result["success"] is False
    assert result["error_type"] == "GRADLEW_NOT_FOUND"


def test_gradle_tool_rejects_shell_injection_like_task(tmp_path: Path) -> None:
    (tmp_path / "gradlew").write_text("#!/usr/bin/env sh\n", encoding="utf-8")

    tool = GradleBuildTool(allowed_project_path=tmp_path)

    with pytest.raises(ToolError):
        tool.execute(
            {
                "project_path": str(tmp_path),
                "task": "assembleDebug; rm -rf /",
            }
        )
