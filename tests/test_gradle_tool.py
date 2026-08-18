from __future__ import annotations

import subprocess
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
    assert result["timeout_seconds"] == 600


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


def test_gradle_build_returns_all_matching_apk_outputs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    gradlew = tmp_path / "gradlew"
    gradlew.write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "build.gradle.kts").write_text(
        'plugins { id("com.android.application") }\n',
        encoding="utf-8",
    )
    debug_dir = tmp_path / "app" / "build" / "outputs" / "apk" / "debug"
    release_dir = tmp_path / "app" / "build" / "outputs" / "apk" / "release"
    debug_dir.mkdir(parents=True)
    release_dir.mkdir(parents=True)
    first_apk = debug_dir / "app-arm64-v8a-debug.apk"
    second_apk = debug_dir / "app-x86_64-debug.apk"
    first_apk.write_bytes(b"apk")
    second_apk.write_bytes(b"apk")
    (release_dir / "app-release.apk").write_bytes(b"apk")
    monkeypatch.setattr(
        "tools.gradle_tool.subprocess.run",
        lambda *args, **kwargs: type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "BUILD SUCCESSFUL", "stderr": ""},
        )(),
    )

    result = GradleBuildTool(allowed_project_path=tmp_path).execute(
        {
            "project_path": str(tmp_path),
            "task": ":app:assembleDebug",
        }
    )

    assert result["success"] is True
    assert result["apk_outputs"] == sorted(
        [str(first_apk.resolve()), str(second_apk.resolve())]
    )
    assert result["timeout_seconds"] == 600


def test_gradle_build_uses_600_second_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "gradlew").write_text(
        "#!/usr/bin/env sh\n",
        encoding="utf-8",
    )
    observed = {}

    def fake_run(command, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, timeout=kwargs["timeout"])

    monkeypatch.setattr("tools.gradle_tool.subprocess.run", fake_run)

    result = GradleBuildTool(allowed_project_path=tmp_path).execute(
        {
            "project_path": str(tmp_path),
            "task": "assembleDebug",
        }
    )

    assert observed["timeout"] == 600
    assert result["success"] is False
    assert result["error_type"] == "BUILD_TIMEOUT"
    assert result["timeout_seconds"] == 600
    assert "600 秒" in result["summary"]
