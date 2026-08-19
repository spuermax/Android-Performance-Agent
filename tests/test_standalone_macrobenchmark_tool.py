from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.base import ToolError
from tools.standalone_macrobenchmark_tool import RunStandaloneMacrobenchmarkTool


ADB_PATH = "/opt/android-sdk/platform-tools/adb"


def completed(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def create_harness(root: Path) -> Path:
    harness = root / "standalone-macrobenchmark"
    (harness / "gradlew").parent.mkdir(parents=True)
    (harness / "gradlew").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    (harness / "settings.gradle.kts").write_text(
        'include(":benchmark", ":harness-host")\n', encoding="utf-8"
    )
    (harness / "benchmark" / "build.gradle.kts").parent.mkdir()
    (harness / "benchmark" / "build.gradle.kts").write_text(
        'plugins { id("com.android.test") }\n', encoding="utf-8"
    )
    source = (
        harness
        / "benchmark"
        / "src"
        / "main"
        / "java"
        / "com"
        / "androidperformance"
        / "standalone"
        / "StandaloneStartupBenchmark.java"
    )
    source.parent.mkdir(parents=True)
    source.write_text("class StandaloneStartupBenchmark {}\n", encoding="utf-8")
    host_build = harness / "harness-host" / "build.gradle.kts"
    host_build.parent.mkdir()
    host_build.write_text(
        'plugins { id("com.android.application") }\n', encoding="utf-8"
    )
    return harness


def benchmark_data(*, include_ttid: bool = True, include_ttfd: bool = False) -> dict:
    metrics = {}
    if include_ttid:
        metrics["timeToInitialDisplayMs"] = {
            "minimum": 300.0,
            "median": 320.0,
            "maximum": 350.0,
            "runs": [300.0, 320.0, 350.0],
        }
    if include_ttfd:
        metrics["timeToFullDisplayMs"] = {
            "minimum": 500.0,
            "median": 550.0,
            "maximum": 600.0,
            "runs": [500.0, 550.0, 600.0],
        }
    return {
        "context": {
            "build": {
                "brand": "Google",
                "model": "Pixel 9",
                "device": "tokay",
                "version": {"sdk": 35},
            },
            "cpuCoreCount": 8,
            "cpuMaxFreqHz": 3100000000,
        },
        "benchmarks": [
            {
                "name": "startup",
                "className": RunStandaloneMacrobenchmarkTool.TEST_CLASS,
                "metrics": metrics,
                "warmupIterations": 0,
                "repeatIterations": 3,
            }
        ],
    }


def ready_result() -> dict:
    return {
        "success": True,
        "serial": "device-1",
        "package_name": "com.example.app",
        "installed": True,
        "debuggable": False,
        "profileable": True,
        "profileable_shell": True,
        "profileinstaller_available": True,
        "benchmark_ready": True,
        "blocking_reasons": [],
        "warnings": [],
        "device_context": {"model": "Pixel 9", "sdk": 35},
        "error_type": None,
        "summary": "ready",
    }


def arguments() -> dict:
    return {
        "serial": "device-1",
        "target_package": "com.example.app",
        "iterations": 3,
        "startup_mode": "COLD",
    }


def create_tool(tmp_path: Path, *, with_harness: bool = True):
    project = tmp_path / "project"
    project.mkdir()
    harness = (
        create_harness(tmp_path / "agent")
        if with_harness
        else tmp_path / "agent" / "missing-harness"
    )
    results = tmp_path / "results"
    tool = RunStandaloneMacrobenchmarkTool(
        allowed_project_path=project,
        harness_root=harness,
        results_root=results,
    )
    tool.readiness_tool.execute = lambda _: ready_result()
    return tool, harness, results


def mock_run(
    monkeypatch,
    tool: RunStandaloneMacrobenchmarkTool,
    harness: Path,
    *,
    build_returncode: int = 0,
    instrumentation_success: bool = True,
    json_data: dict | None = None,
    write_json: bool = True,
    write_trace: bool = True,
):
    monkeypatch.setattr(
        "tools.standalone_macrobenchmark_tool.shutil.which",
        lambda name: ADB_PATH if name == "adb" else None,
    )
    monkeypatch.setattr(tool, "_new_run_id", lambda: "run-1")
    calls: list[tuple[list[str], dict]] = []

    def fake_subprocess_run(command, **kwargs):
        calls.append((command, kwargs))
        if ":benchmark:assembleDebug" in command:
            if build_returncode == 0:
                apk = (
                    harness
                    / "benchmark"
                    / "build"
                    / "outputs"
                    / "apk"
                    / "debug"
                    / "benchmark-debug.apk"
                )
                apk.parent.mkdir(parents=True, exist_ok=True)
                apk.write_bytes(b"harness apk")
                return completed("BUILD SUCCESSFUL")
            return completed("FAILURE: Build failed", returncode=build_returncode)
        if command[:4] == [ADB_PATH, "-s", "device-1", "install"]:
            return completed("Success\n")
        if command[:7] == [
            ADB_PATH,
            "-s",
            "device-1",
            "shell",
            "mkdir",
            "-p",
            command[-1],
        ]:
            return completed()
        if "instrument" in command:
            if instrumentation_success:
                return completed(
                    "OK (1 test)\nINSTRUMENTATION_CODE: -1\n"
                )
            return completed(
                "FAILURES!!!\njava.lang.AssertionError\nINSTRUMENTATION_CODE: 0\n"
            )
        if command[:4] == [ADB_PATH, "-s", "device-1", "pull"]:
            output = Path(command[-1])
            output.mkdir(parents=True, exist_ok=True)
            if write_json:
                data = json_data if json_data is not None else benchmark_data()
                (output / "standalone-benchmarkData.json").write_text(
                    json.dumps(data), encoding="utf-8"
                )
            if write_trace:
                (output / "startup_iter000.perfetto-trace").write_bytes(b"trace")
            return completed("files pulled")
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(
        "tools.standalone_macrobenchmark_tool.subprocess.run",
        fake_subprocess_run,
    )
    return calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("serial", "bad serial"),
        ("target_package", "not-a-package"),
        ("iterations", 0),
        ("startup_mode", "WARM"),
    ],
)
def test_standalone_validates_parameters(
    tmp_path: Path,
    field: str,
    value,
) -> None:
    tool, _, _ = create_tool(tmp_path)
    supplied = arguments()
    supplied[field] = value

    with pytest.raises(ToolError):
        tool.execute(supplied)


def test_standalone_stops_when_readiness_blocks(tmp_path: Path) -> None:
    tool, _, _ = create_tool(tmp_path)
    blocked = ready_result()
    blocked.update(
        {
            "success": False,
            "benchmark_ready": False,
            "blocking_reasons": ["TARGET_DEBUGGABLE"],
            "error_type": "TARGET_DEBUGGABLE",
        }
    )
    tool.readiness_tool.execute = lambda _: blocked

    result = tool.execute(arguments())

    assert result["error_type"] == "TARGET_DEBUGGABLE"
    assert result["readiness"]["blocking_reasons"] == ["TARGET_DEBUGGABLE"]


def test_standalone_reports_missing_harness(tmp_path: Path) -> None:
    tool, _, _ = create_tool(tmp_path, with_harness=False)

    result = tool.execute(arguments())

    assert result["error_type"] == "HARNESS_NOT_FOUND"


def test_standalone_reports_harness_build_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, harness, _ = create_tool(tmp_path)
    mock_run(monkeypatch, tool, harness, build_returncode=1)

    result = tool.execute(arguments())

    assert result["error_type"] == "HARNESS_BUILD_FAILED"
    assert result["important_logs"]


def test_standalone_reports_instrumentation_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, harness, _ = create_tool(tmp_path)
    mock_run(monkeypatch, tool, harness, instrumentation_success=False)

    result = tool.execute(arguments())

    assert result["error_type"] == "BENCHMARK_TEST_FAILED"
    assert result["benchmark_json_path"] is None


def test_standalone_reports_missing_json(monkeypatch, tmp_path: Path) -> None:
    tool, harness, _ = create_tool(tmp_path)
    mock_run(monkeypatch, tool, harness, write_json=False)

    result = tool.execute(arguments())

    assert result["error_type"] == "BENCHMARK_JSON_NOT_FOUND"
    assert len(result["trace_files"]) == 1


def test_standalone_reports_missing_ttid(monkeypatch, tmp_path: Path) -> None:
    tool, harness, _ = create_tool(tmp_path)
    mock_run(
        monkeypatch,
        tool,
        harness,
        json_data=benchmark_data(include_ttid=False),
    )

    result = tool.execute(arguments())

    assert result["error_type"] == "STARTUP_METRIC_NOT_FOUND"


def test_standalone_returns_ttid_without_guessing_ttfd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, harness, _ = create_tool(tmp_path)
    calls = mock_run(monkeypatch, tool, harness)

    result = tool.execute(arguments())

    assert result["success"] is True
    assert result["measurement_method"] == "STANDALONE_MACROBENCHMARK"
    assert result["ttid_ms"] == {
        "metric_name": "timeToInitialDisplayMs",
        "minimum": 300.0,
        "median": 320.0,
        "maximum": 350.0,
        "runs": [300.0, 320.0, 350.0],
    }
    assert result["ttfd_available"] is False
    assert result["ttfd_ms"] is None
    assert result["repeat_iterations"] == 3
    assert result["run_count"] == 3
    assert len(result["trace_files"]) == 1
    assert result["device_context"]["model"] == "Pixel 9"
    assert all(kwargs["shell"] is False for _, kwargs in calls)
    assert not any("suppressErrors" in str(command) for command, _ in calls)


def test_standalone_returns_ttfd_when_json_contains_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, harness, _ = create_tool(tmp_path)
    mock_run(
        monkeypatch,
        tool,
        harness,
        json_data=benchmark_data(include_ttfd=True),
    )

    result = tool.execute(arguments())

    assert result["success"] is True
    assert result["ttfd_available"] is True
    assert result["ttfd_ms"]["median"] == 550.0


def test_standalone_unique_result_dir_ignores_old_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, harness, results = create_tool(tmp_path)
    old = results / "old-run" / "device-output"
    old.mkdir(parents=True)
    old_json = benchmark_data()
    old_json["benchmarks"][0]["metrics"]["timeToInitialDisplayMs"]["median"] = 9999
    (old / "old-benchmarkData.json").write_text(
        json.dumps(old_json), encoding="utf-8"
    )
    mock_run(monkeypatch, tool, harness)

    result = tool.execute(arguments())

    assert result["success"] is True
    assert result["ttid_ms"]["median"] == 320.0
    assert "/run-1/" in result["benchmark_json_path"]
    assert "/old-run/" not in result["benchmark_json_path"]


def test_standalone_requires_perfetto_trace(monkeypatch, tmp_path: Path) -> None:
    tool, harness, _ = create_tool(tmp_path)
    mock_run(monkeypatch, tool, harness, write_trace=False)

    result = tool.execute(arguments())

    assert result["error_type"] == "PERFETTO_TRACE_NOT_FOUND"
