from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tools.benchmark_target_tool import PrepareBenchmarkTargetTool


class SequenceTool:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(arguments)
        if not self.results:
            raise AssertionError("unexpected Tool call")
        return self.results.pop(0)


def make_project(tmp_path: Path, module: str = "edusoho") -> Path:
    (tmp_path / "settings.gradle.kts").write_text(
        f'include(":{module}")\n',
        encoding="utf-8",
    )
    module_path = tmp_path / module
    module_path.mkdir(parents=True)
    (module_path / "build.gradle").write_text(
        """
plugins { id 'com.android.application' }
android {
  buildTypes {
    debug { debuggable true }
    release { minifyEnabled true }
    benchmark {
      initWith release
      debuggable false
      signingConfig signingConfigs.release
    }
  }
}
""",
        encoding="utf-8",
    )
    gradlew = tmp_path / "gradlew"
    gradlew.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    gradlew.chmod(0o755)
    return tmp_path


def candidate(
    variant: str,
    *,
    build_type: str,
    debuggable: bool,
    priority: int,
) -> dict[str, Any]:
    return {
        "variant_name": variant,
        "variant": variant,
        "build_type": build_type,
        "flavor": variant[: -len(build_type)].lower() or None,
        "assemble_task": f":edusoho:assemble{variant}",
        "debuggable": debuggable,
        "minify_enabled": build_type == "release",
        "signing_available": build_type == "debug",
        "apk_output_expected": True,
        "priority": priority,
    }


def enumeration(variants: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "variants": variants,
        "error_type": None,
        "summary": "ok",
        "important_logs": [],
    }


def test_enumerates_real_assemble_tasks_and_orders_release_before_debug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    stdout = """
assembleRelease - Assembles main outputs for all Release variants.
assembleDebug - Assembles main outputs for all Debug variants.
assembleXiaomi - Assembles main outputs for all Xiaomi variants.
assembleHuawei - Assembles main outputs for all Huawei variants.
assembleXiaomiDebug - Assembles main output for variant xiaomiDebug
assembleXiaomiRelease - Assembles main output for variant xiaomiRelease
assembleHuaweiDebug - Assembles main output for variant huaweiDebug
assembleHuaweiRelease - Assembles main output for variant huaweiRelease
assembleBenchmark - Assembles Benchmark
assembleXiaomiDebugAndroidTest - Assembles tests
"""

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[1:] == [":edusoho:tasks", "--all", "--console=plain"]
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 600
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("tools.benchmark_target_tool.subprocess.run", fake_run)
    tool = PrepareBenchmarkTargetTool(project)

    result = tool._enumerate_variants(project, "edusoho")

    assert [item["variant"] for item in result["variants"]] == [
        "Benchmark",
        "HuaweiRelease",
        "XiaomiRelease",
        "HuaweiDebug",
        "XiaomiDebug",
    ]
    assert result["variants"][1]["assemble_task"] == (
        ":edusoho:assembleHuaweiRelease"
    )
    assert result["variants"][1]["debuggable"] is False
    assert result["variants"][-1]["debuggable"] is True
    tasks = {item["assemble_task"] for item in result["variants"]}
    assert ":edusoho:assembleRelease" not in tasks
    assert ":edusoho:assembleDebug" not in tasks
    assert ":edusoho:assembleXiaomi" not in tasks
    assert ":edusoho:assembleHuawei" not in tasks


def test_prepare_parser_accepts_tasks_without_descriptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    stdout = "assembleXiaomiRelease\nassembleXiaomiDebug\n"

    monkeypatch.setattr(
        "tools.benchmark_target_tool.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout,
            "",
        ),
    )

    result = PrepareBenchmarkTargetTool(project)._enumerate_variants(
        project,
        "edusoho",
    )

    assert [item["variant"] for item in result["variants"]] == [
        "XiaomiRelease",
        "XiaomiDebug",
    ]


def test_continues_after_build_failure_and_selects_next_ready_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    apk = project / "edusoho" / "build" / "outputs" / "apk" / "xiaomi" / "release" / "app.apk"
    apk.parent.mkdir(parents=True)
    apk.write_bytes(b"apk")
    gradle = SequenceTool(
        [
            {"success": False, "error_type": "COMPILATION_FAILED", "apk_outputs": []},
            {"success": True, "error_type": None, "apk_outputs": [str(apk)]},
        ]
    )
    install = SequenceTool([{"success": True}])
    launch = SequenceTool([{"success": True}])
    readiness = SequenceTool(
        [
            {
                "success": True,
                "benchmark_ready": True,
                "debuggable": False,
                "profileable": True,
                "profileable_shell": True,
                "profileinstaller_available": True,
                "blocking_reasons": [],
            }
        ]
    )
    progress_events: list[dict[str, Any]] = []
    tool = PrepareBenchmarkTargetTool(
        project,
        gradle_tool=gradle,  # type: ignore[arg-type]
        install_tool=install,  # type: ignore[arg-type]
        launch_tool=launch,  # type: ignore[arg-type]
        readiness_tool=readiness,  # type: ignore[arg-type]
        progress_sink=progress_events.append,
    )
    variants = [
        candidate("HuaweiRelease", build_type="release", debuggable=False, priority=1),
        candidate("XiaomiRelease", build_type="release", debuggable=False, priority=2),
        candidate("XiaomiDebug", build_type="debug", debuggable=True, priority=3),
    ]
    monkeypatch.setattr(tool, "_enumerate_variants", lambda *_args: enumeration(variants))
    monkeypatch.setattr(tool, "_apk_signing_available", lambda _path: True)
    monkeypatch.setattr(
        tool,
        "_apk_identity",
        lambda _path: {
            "application_id": "com.edusoho.app",
            "launcher_component": "com.edusoho.app/com.edusoho.MainActivity",
        },
    )

    result = tool.execute(
        {"project_path": str(project), "module": "edusoho", "serial": "device-1"}
    )

    assert result["success"] is True
    assert result["selected_variant"] == "XiaomiRelease"
    assert result["selected_apk"] == str(apk)
    assert result["application_id"] == "com.edusoho.app"
    assert result["candidates_checked"] == 2
    assert result["candidate_results"][0]["status"] == "BUILD_FAILED"
    assert [call["task"] for call in gradle.calls] == [
        ":edusoho:assembleHuaweiRelease",
        ":edusoho:assembleXiaomiRelease",
    ]
    assert "设备品牌无关" in result["selection_reason"]
    assert progress_events[0] == {
        "type": "tool_progress",
        "name": "prepare_benchmark_target",
        "candidate_index": 1,
        "candidate_total": 3,
        "variant": "HuaweiRelease",
        "status": "BUILDING",
        "error_type": None,
    }
    assert any(
        event["candidate_index"] == 1 and event["status"] == "BUILD_FAILED"
        for event in progress_events
    )
    assert any(
        event["candidate_index"] == 2
        and event["variant"] == "XiaomiRelease"
        and event["status"] == "CHECKING_READINESS"
        for event in progress_events
    )
    assert progress_events[-1]["status"] == "BENCHMARK_READY"


def test_tries_every_candidate_before_returning_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    apks = []
    for variant in ("huawei-release", "xiaomi-release", "xiaomi-debug"):
        apk = project / f"{variant}.apk"
        apk.write_bytes(b"apk")
        apks.append(apk)
    gradle = SequenceTool(
        [
            {"success": True, "apk_outputs": [str(apk)], "error_type": None}
            for apk in apks
        ]
    )
    install = SequenceTool([{"success": True}] * 3)
    launch = SequenceTool([{"success": True}] * 3)
    readiness = SequenceTool(
        [
            {
                "benchmark_ready": False,
                "debuggable": False,
                "profileable": False,
                "profileable_shell": False,
                "profileinstaller_available": False,
                "blocking_reasons": ["TARGET_NOT_PROFILEABLE"],
                "error_type": "TARGET_NOT_PROFILEABLE",
            },
            {
                "benchmark_ready": False,
                "debuggable": False,
                "profileable": False,
                "profileable_shell": False,
                "profileinstaller_available": False,
                "blocking_reasons": ["PROFILER_INSTALLER_NOT_FOUND"],
                "error_type": "PROFILER_INSTALLER_NOT_FOUND",
            },
            {
                "benchmark_ready": False,
                "debuggable": True,
                "profileable": False,
                "profileable_shell": False,
                "profileinstaller_available": False,
                "blocking_reasons": ["TARGET_DEBUGGABLE"],
                "error_type": "TARGET_DEBUGGABLE",
            },
        ]
    )
    tool = PrepareBenchmarkTargetTool(
        project,
        gradle_tool=gradle,  # type: ignore[arg-type]
        install_tool=install,  # type: ignore[arg-type]
        launch_tool=launch,  # type: ignore[arg-type]
        readiness_tool=readiness,  # type: ignore[arg-type]
    )
    variants = [
        candidate("HuaweiRelease", build_type="release", debuggable=False, priority=1),
        candidate("XiaomiRelease", build_type="release", debuggable=False, priority=2),
        candidate("XiaomiDebug", build_type="debug", debuggable=True, priority=3),
    ]
    monkeypatch.setattr(tool, "_enumerate_variants", lambda *_args: enumeration(variants))
    monkeypatch.setattr(tool, "_apk_signing_available", lambda _path: True)
    monkeypatch.setattr(
        tool,
        "_apk_identity",
        lambda _path: {
            "application_id": "com.edusoho.app",
            "launcher_component": "com.edusoho.app/com.edusoho.MainActivity",
        },
    )

    result = tool.execute(
        {"project_path": str(project), "module": "edusoho", "serial": "device-1"}
    )

    assert result["success"] is False
    assert result["error_type"] == "NO_BENCHMARK_READY_TARGET"
    assert result["candidates_checked"] == 3
    assert len(gradle.calls) == 3
    assert result["blocking_reasons"] == [
        "TARGET_NOT_PROFILEABLE",
        "PROFILER_INSTALLER_NOT_FOUND",
        "TARGET_DEBUGGABLE",
    ]


def test_unsigned_release_is_recorded_and_next_candidate_is_tried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    unsigned = project / "app-release-unsigned.apk"
    signed = project / "app-debug.apk"
    unsigned.write_bytes(b"apk")
    signed.write_bytes(b"apk")
    gradle = SequenceTool(
        [
            {"success": True, "apk_outputs": [str(unsigned)]},
            {"success": True, "apk_outputs": [str(signed)]},
        ]
    )
    install = SequenceTool([{"success": True}])
    launch = SequenceTool([{"success": True}])
    readiness = SequenceTool(
        [
            {
                "benchmark_ready": True,
                "debuggable": False,
                "profileable": True,
                "profileable_shell": True,
                "profileinstaller_available": True,
                "blocking_reasons": [],
            }
        ]
    )
    tool = PrepareBenchmarkTargetTool(
        project,
        gradle_tool=gradle,  # type: ignore[arg-type]
        install_tool=install,  # type: ignore[arg-type]
        launch_tool=launch,  # type: ignore[arg-type]
        readiness_tool=readiness,  # type: ignore[arg-type]
    )
    variants = [
        candidate("XiaomiRelease", build_type="release", debuggable=False, priority=1),
        candidate("XiaomiDebug", build_type="debug", debuggable=True, priority=2),
    ]
    monkeypatch.setattr(tool, "_enumerate_variants", lambda *_args: enumeration(variants))
    monkeypatch.setattr(tool, "_apk_signing_available", lambda path: "unsigned" not in path.name)
    monkeypatch.setattr(
        tool,
        "_apk_identity",
        lambda _path: {
            "application_id": "com.edusoho.app",
            "launcher_component": "com.edusoho.app/com.edusoho.MainActivity",
        },
    )

    result = tool.execute(
        {"project_path": str(project), "module": "edusoho", "serial": "device-1"}
    )

    assert result["success"] is True
    assert result["selected_variant"] == "XiaomiDebug"
    assert result["candidate_results"][0]["rejection_reason"] == "UNSIGNED_TARGET"
    assert len(install.calls) == 1


def test_rejects_module_that_is_not_an_application(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    tool = PrepareBenchmarkTargetTool(project)

    result = tool.execute(
        {"project_path": str(project), "module": "app", "serial": "device-1"}
    )

    assert result["success"] is False
    assert result["error_type"] == "MODULE_NOT_FOUND"
