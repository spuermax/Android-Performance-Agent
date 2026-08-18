from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import call

import pytest

from tools.base import ToolError
from tools.macrobenchmark_tool import RunMacrobenchmarkTool


ADB_PATH = "/opt/android-sdk/platform-tools/adb"
TEST_CLASS = "com.example.benchmark.ExampleStartupBenchmark"


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


def create_benchmark_project(
    root: Path,
    *,
    with_gradlew: bool = True,
    with_module: bool = True,
    with_test: bool = True,
    test_text: str | None = None,
) -> tuple[RunMacrobenchmarkTool, dict, Path]:
    if with_gradlew:
        (root / "gradlew").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    module = root / "benchmark"
    if with_module:
        module.mkdir()
        (module / "build.gradle.kts").write_text(
            'plugins { id("com.android.test") }\n',
            encoding="utf-8",
        )
    if with_module and with_test:
        test_file = (
            module
            / "src"
            / "androidTest"
            / "java"
            / "com"
            / "example"
            / "benchmark"
            / "ExampleStartupBenchmark.kt"
        )
        test_file.parent.mkdir(parents=True)
        test_file.write_text(
            test_text
            or """class ExampleStartupBenchmark {
    val rule = MacrobenchmarkRule()
    fun startup() {
        val metrics = listOf(StartupTimingMetric())
    }
}
""",
            encoding="utf-8",
        )
    arguments = {
        "project_path": str(root),
        "benchmark_module": "benchmark",
        "serial": "device-1",
        "test_class": TEST_CLASS,
        "test_method": "startup",
    }
    return RunMacrobenchmarkTool(allowed_project_path=root), arguments, module


def benchmark_data(*, include_ttid: bool = True, include_ttfd: bool = False) -> dict:
    metrics = {}
    if include_ttid:
        metrics["timeToInitialDisplayMs"] = {
            "minimum": 324.7,
            "median": 340.2,
            "maximum": 387.0,
            "runs": [324.7, 340.2, 387.0],
        }
    if include_ttfd:
        metrics["timeToFullDisplayMs"] = {
            "minimum": 500.0,
            "median": 550.5,
            "maximum": 600.0,
            "runs": [500.0, 550.5, 600.0],
        }
    return {
        "context": {
            "brand": "Google",
            "model": "Pixel 8",
            "sdk": 34,
            "cpuCoreCount": 8,
        },
        "benchmarks": [
            {
                "name": "startup",
                "className": TEST_CLASS,
                "metrics": metrics,
                "warmupIterations": 0,
                "repeatIterations": 3,
            }
        ],
    }


def benchmark_data_with_official_context() -> dict:
    data = benchmark_data()
    data["context"] = {
        "build": {
            "brand": "Google",
            "model": "Pixel 9 Pro",
            "device": "komodo",
            "version": {"sdk": 35},
        },
        "cpuCoreCount": 8,
        "cpuMaxFreqHz": 3200000000,
    }
    return data


def output_root(module: Path) -> Path:
    return module / "build" / "outputs" / "connected_android_test_additional_output"


def write_benchmark_json(module: Path, data: dict, name: str = "benchmarkData.json") -> Path:
    path = output_root(module) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def mock_successful_run(monkeypatch, module: Path, callback=None):
    monkeypatch.setattr("tools.macrobenchmark_tool.shutil.which", lambda _: ADB_PATH)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(call(command, **kwargs))
        if command == [ADB_PATH, "devices", "-l"]:
            return completed("List of devices attached\ndevice-1 device\n")
        if callback:
            callback()
        return completed(
            "BUILD SUCCESSFUL\n"
            "timeToInitialDisplayMs median 9999.0\n"
        )

    monkeypatch.setattr("tools.macrobenchmark_tool.subprocess.run", fake_run)
    return calls


def test_macrobenchmark_reports_missing_gradlew(tmp_path: Path) -> None:
    tool, arguments, _ = create_benchmark_project(
        tmp_path,
        with_gradlew=False,
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "GRADLEW_NOT_FOUND"


def test_macrobenchmark_reports_missing_module(tmp_path: Path) -> None:
    tool, arguments, _ = create_benchmark_project(
        tmp_path,
        with_module=False,
        with_test=False,
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "BENCHMARK_MODULE_NOT_FOUND"


def test_macrobenchmark_reports_missing_test(tmp_path: Path) -> None:
    tool, arguments, _ = create_benchmark_project(tmp_path, with_test=False)

    result = tool.execute(arguments)

    assert result["error_type"] == "BENCHMARK_TEST_NOT_FOUND"


@pytest.mark.parametrize(
    ("source_root", "extension"),
    [
        ("src/main/java", ".java"),
        ("src/main/kotlin", ".kt"),
        ("src/androidTest/java", ".java"),
        ("src/androidTest/kotlin", ".kt"),
    ],
)
def test_macrobenchmark_finds_test_by_exact_class_path_in_supported_sources(
    tmp_path: Path,
    source_root: str,
    extension: str,
) -> None:
    tool, _, module = create_benchmark_project(tmp_path, with_test=False)
    exact_file = (
        module
        / source_root
        / "com"
        / "example"
        / "benchmark"
        / f"ExampleStartupBenchmark{extension}"
    )
    exact_file.parent.mkdir(parents=True)
    exact_file.write_text("class ExampleStartupBenchmark {}\n", encoding="utf-8")
    decoy = module / source_root / "wrong" / exact_file.name
    decoy.parent.mkdir(parents=True)
    decoy.write_text("class ExampleStartupBenchmark {}\n", encoding="utf-8")

    found = tool._find_test_file(tmp_path, module, TEST_CLASS)

    assert found == exact_file.resolve()
    exact_file.unlink()
    assert tool._find_test_file(tmp_path, module, TEST_CLASS) is None


def test_macrobenchmark_reports_adb_not_found(monkeypatch, tmp_path: Path) -> None:
    tool, arguments, _ = create_benchmark_project(tmp_path)
    monkeypatch.setattr("tools.macrobenchmark_tool.shutil.which", lambda _: None)

    result = tool.execute(arguments)

    assert result["error_type"] == "ADB_NOT_FOUND"


def test_macrobenchmark_rejects_unknown_serial(monkeypatch, tmp_path: Path) -> None:
    tool, arguments, _ = create_benchmark_project(tmp_path)
    monkeypatch.setattr("tools.macrobenchmark_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.macrobenchmark_tool.subprocess.run",
        lambda *args, **kwargs: completed(
            "List of devices attached\nother-device device\n"
        ),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "DEVICE_NOT_FOUND"


@pytest.mark.parametrize(
    ("state", "expected_error"),
    [
        ("unauthorized", "DEVICE_UNAUTHORIZED"),
        ("offline", "DEVICE_OFFLINE"),
    ],
)
def test_macrobenchmark_rejects_unavailable_device(
    monkeypatch,
    tmp_path: Path,
    state: str,
    expected_error: str,
) -> None:
    tool, arguments, _ = create_benchmark_project(tmp_path)
    monkeypatch.setattr("tools.macrobenchmark_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.macrobenchmark_tool.subprocess.run",
        lambda *args, **kwargs: completed(
            f"List of devices attached\ndevice-1 {state}\n"
        ),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == expected_error


def test_macrobenchmark_reads_ttid_only_from_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments, module = create_benchmark_project(tmp_path)
    calls = mock_successful_run(
        monkeypatch,
        module,
        lambda: write_benchmark_json(module, benchmark_data()),
    )

    result = tool.execute(arguments)

    assert result["success"] is True
    assert result["ttid_ms"] == {
        "metric_name": "timeToInitialDisplayMs",
        "minimum": 324.7,
        "median": 340.2,
        "maximum": 387.0,
        "runs": [324.7, 340.2, 387.0],
    }
    assert result["ttid_ms"]["median"] != 9999.0
    assert result["repeat_iterations"] == 3
    assert result["warmup_iterations"] == 0
    assert result["run_count"] == 3
    assert result["device_context"] == {
        "brand": "Google",
        "model": "Pixel 8",
        "sdk": 34,
        "cpuCoreCount": 8,
    }
    assert calls[1].kwargs["shell"] is False
    assert calls[1].kwargs["env"]["ANDROID_SERIAL"] == "device-1"
    assert not any("suppressErrors" in str(value) for value in calls[1].args[0])


def test_macrobenchmark_reads_ttfd_from_json(monkeypatch, tmp_path: Path) -> None:
    tool, arguments, module = create_benchmark_project(tmp_path)
    mock_successful_run(
        monkeypatch,
        module,
        lambda: write_benchmark_json(
            module,
            benchmark_data(include_ttfd=True),
        ),
    )

    result = tool.execute(arguments)

    assert result["success"] is True
    assert result["ttfd_available"] is True
    assert result["ttfd_ms"]["metric_name"] == "timeToFullDisplayMs"
    assert result["ttfd_ms"]["median"] == 550.5


def test_macrobenchmark_reads_official_nested_device_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments, module = create_benchmark_project(tmp_path)
    mock_successful_run(
        monkeypatch,
        module,
        lambda: write_benchmark_json(
            module,
            benchmark_data_with_official_context(),
        ),
    )

    result = tool.execute(arguments)

    assert result["success"] is True
    assert result["device_context"] == {
        "brand": "Google",
        "model": "Pixel 9 Pro",
        "device": "komodo",
        "sdk": 35,
        "cpuCoreCount": 8,
        "cpuMaxFreqHz": 3200000000,
    }


def test_macrobenchmark_succeeds_without_ttfd(monkeypatch, tmp_path: Path) -> None:
    tool, arguments, module = create_benchmark_project(tmp_path)
    mock_successful_run(
        monkeypatch,
        module,
        lambda: write_benchmark_json(module, benchmark_data()),
    )

    result = tool.execute(arguments)

    assert result["success"] is True
    assert result["ttfd_available"] is False
    assert result["ttfd_ms"] is None


def test_macrobenchmark_reports_missing_ttid_metric(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments, module = create_benchmark_project(tmp_path)
    mock_successful_run(
        monkeypatch,
        module,
        lambda: write_benchmark_json(
            module,
            benchmark_data(include_ttid=False, include_ttfd=True),
        ),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "STARTUP_METRIC_NOT_FOUND"
    assert result["ttid_ms"] is None


def test_macrobenchmark_reports_missing_fresh_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments, module = create_benchmark_project(tmp_path)
    mock_successful_run(monkeypatch, module)

    result = tool.execute(arguments)

    assert result["error_type"] == "BENCHMARK_JSON_NOT_FOUND"


def test_macrobenchmark_ignores_old_json_and_uses_new_json(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments, module = create_benchmark_project(tmp_path)
    old_data = benchmark_data()
    old_data["benchmarks"][0]["metrics"]["timeToInitialDisplayMs"]["median"] = 999.0
    old_path = write_benchmark_json(module, old_data, "old-benchmarkData.json")
    mock_successful_run(
        monkeypatch,
        module,
        lambda: write_benchmark_json(
            module,
            benchmark_data(),
            "new-benchmarkData.json",
        ),
    )

    result = tool.execute(arguments)

    assert result["success"] is True
    assert result["ttid_ms"]["median"] == 340.2
    assert result["benchmark_json_path"].endswith("new-benchmarkData.json")
    assert result["benchmark_json_path"] != str(old_path)


def test_macrobenchmark_returns_all_fresh_traces(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments, module = create_benchmark_project(tmp_path)

    def create_outputs() -> None:
        write_benchmark_json(module, benchmark_data())
        trace_root = output_root(module)
        (trace_root / "iter000.perfetto-trace").write_bytes(b"trace 0")
        (trace_root / "nested").mkdir()
        (trace_root / "nested" / "iter001.perfetto-trace").write_bytes(b"trace 1")

    mock_successful_run(monkeypatch, module, create_outputs)

    result = tool.execute(arguments)

    assert len(result["trace_files"]) == 2
    assert all(path.endswith(".perfetto-trace") for path in result["trace_files"])


def test_macrobenchmark_timeout_is_structured(monkeypatch, tmp_path: Path) -> None:
    tool, arguments, _ = create_benchmark_project(tmp_path)
    monkeypatch.setattr("tools.macrobenchmark_tool.shutil.which", lambda _: ADB_PATH)

    def fake_run(command, **kwargs):
        if command == [ADB_PATH, "devices", "-l"]:
            return completed("List of devices attached\ndevice-1 device\n")
        raise subprocess.TimeoutExpired(command, timeout=600)

    monkeypatch.setattr("tools.macrobenchmark_tool.subprocess.run", fake_run)

    result = tool.execute(arguments)

    assert result["error_type"] == "MACROBENCHMARK_TIMEOUT"
    assert result["test_selector"] == f"{TEST_CLASS}#startup"


@pytest.mark.parametrize(
    ("console_error", "expected_error"),
    [
        ("DEBUGGABLE", "BENCHMARK_DEBUGGABLE"),
        ("EMULATOR", "BENCHMARK_EMULATOR"),
        ("LOW-BATTERY", "BENCHMARK_LOW_BATTERY"),
        ("NOT-PROFILEABLE", "BENCHMARK_NOT_PROFILEABLE"),
    ],
)
def test_macrobenchmark_does_not_suppress_environment_errors(
    monkeypatch,
    tmp_path: Path,
    console_error: str,
    expected_error: str,
) -> None:
    tool, arguments, _ = create_benchmark_project(tmp_path)
    monkeypatch.setattr("tools.macrobenchmark_tool.shutil.which", lambda _: ADB_PATH)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(call(command, **kwargs))
        if command == [ADB_PATH, "devices", "-l"]:
            return completed("List of devices attached\ndevice-1 device\n")
        return completed(returncode=1, stderr=f"ERROR: {console_error}\n")

    monkeypatch.setattr("tools.macrobenchmark_tool.subprocess.run", fake_run)

    result = tool.execute(arguments)

    assert result["error_type"] == expected_error
    gradle_command = calls[1].args[0]
    assert not any("suppress" in str(value).lower() for value in gradle_command)


@pytest.mark.parametrize(
    ("field", "malicious_value"),
    [
        ("benchmark_module", "../../outside"),
        ("benchmark_module", "benchmark/../other"),
        ("benchmark_module", "benchmark;id"),
        ("test_class", "com.example.Benchmark;id"),
        ("test_method", "startup && id"),
    ],
)
def test_macrobenchmark_rejects_malicious_parameters(
    tmp_path: Path,
    field: str,
    malicious_value: str,
) -> None:
    tool, arguments, _ = create_benchmark_project(tmp_path)
    arguments[field] = malicious_value

    with pytest.raises(ToolError):
        tool.execute(arguments)


def test_macrobenchmark_reports_missing_method(tmp_path: Path) -> None:
    tool, arguments, _ = create_benchmark_project(tmp_path)
    arguments["test_method"] = "missing"

    result = tool.execute(arguments)

    assert result["error_type"] == "BENCHMARK_TEST_METHOD_NOT_FOUND"


def test_macrobenchmark_requires_startup_metric_in_test_source(tmp_path: Path) -> None:
    tool, arguments, _ = create_benchmark_project(
        tmp_path,
        test_text="""class ExampleStartupBenchmark {
    val rule = MacrobenchmarkRule()
    fun startup() = Unit
}
""",
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "STARTUP_BENCHMARK_NOT_DETECTED"
