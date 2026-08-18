from pathlib import Path
import pytest

from tools.base import ToolError
from tools.file_tool import ReadProjectFileTool


def test_read_project_file_reads_lines(tmp_path: Path) -> None:
    (tmp_path / "settings.gradle").write_text(
        "line1\nline2\nline3\n",
        encoding="utf-8",
    )

    tool = ReadProjectFileTool(allowed_project_path=tmp_path)
    result = tool.execute({
        "project_path": str(tmp_path),
        "relative_path": "settings.gradle",
        "start_line": 2,
        "end_line": 3,
    })

    assert result["success"] is True
    assert [x["text"] for x in result["content"]] == ["line2", "line3"]


def test_read_project_file_rejects_escape(tmp_path: Path) -> None:
    tool = ReadProjectFileTool(allowed_project_path=tmp_path)

    with pytest.raises(ToolError):
        tool.execute({
            "project_path": str(tmp_path),
            "relative_path": "../outside.txt",
            "start_line": 1,
            "end_line": 10,
        })
