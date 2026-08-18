from __future__ import annotations

from pathlib import Path

from tools.project_tool import InspectProjectTool


def create_minimal_android_project(root: Path) -> None:
    (root / "gradle" / "wrapper").mkdir(parents=True)
    (root / "app").mkdir()

    (root / "settings.gradle.kts").write_text(
        'include(":app", ":benchmark")\n',
        encoding="utf-8",
    )
    (root / "gradlew").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    (root / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
        "distributionUrl=https\\://services.gradle.org/distributions/"
        "gradle-8.13-bin.zip\n",
        encoding="utf-8",
    )
    (root / "gradle.properties").write_text(
        "android.useAndroidX=true\n",
        encoding="utf-8",
    )
    (root / "app" / "build.gradle.kts").write_text(
        'plugins { id("com.android.application") }\n',
        encoding="utf-8",
    )


def test_inspect_project_detects_gradle_and_android(tmp_path: Path) -> None:
    create_minimal_android_project(tmp_path)

    tool = InspectProjectTool(allowed_project_path=tmp_path)
    result = tool.execute({"project_path": str(tmp_path)})

    assert result["success"] is True
    assert result["is_gradle_project"] is True
    assert result["is_android_project"] is True
    assert result["gradle_wrapper_version"] == "8.13"
    assert result["androidx_enabled"] is True
    assert "app" in result["modules"]
    assert result["module_types"]["app"] == "application"
    assert result["application_modules"] == ["app"]
    assert result["primary_application_module"] == "app"
    assert "benchmark" in result["benchmark_modules"]


def test_inspect_project_rejects_other_path(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()

    tool = InspectProjectTool(allowed_project_path=allowed)
    result = None
    try:
        result = tool.execute({"project_path": str(other)})
    except Exception as exc:
        assert "拒绝访问" in str(exc)

    assert result is None
