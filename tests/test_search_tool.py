from pathlib import Path
from tools.search_tool import SearchProjectTextTool


def test_search_project_text_finds_match(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "build.gradle").write_text(
        "plugins {\n    id 'io.github.androidinsight'\n}\n",
        encoding="utf-8",
    )

    tool = SearchProjectTextTool(allowed_project_path=tmp_path)
    result = tool.execute({
        "project_path": str(tmp_path),
        "query": "io.github.androidinsight",
        "max_results": 20,
    })

    assert result["success"] is True
    assert result["match_count"] == 1
    assert result["matches"][0]["file"] == "app/build.gradle"
    assert result["matches"][0]["line"] == 2
