from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.adb_tool import AdbDevicesTool
from tools.base import BaseTool, ToolError


class RunMacrobenchmarkTool(BaseTool):
    name = "run_macrobenchmark"
    description = (
        "在显式指定的 Android 设备上运行项目中已经存在的 Macrobenchmark "
        "启动测试，读取 AndroidX Benchmark 本轮生成的 JSON 和 Perfetto Trace，"
        "返回真实 TTID/TTFD 指标。该 Tool 不创建测试、不修改项目、不分析 "
        "Perfetto，也不会 suppress 不可靠测量环境的 Benchmark 错误。"
    )

    DEFAULT_TIMEOUT_SECONDS = 600
    DEVICE_CHECK_TIMEOUT_SECONDS = 15
    MODULE_PATTERN = re.compile(
        r":?[A-Za-z0-9_.-]+(?:(?::|/)[A-Za-z0-9_.-]+)*"
    )
    CLASS_PATTERN = re.compile(
        r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    )
    METHOD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "用户指定的 Android 项目绝对路径。",
                },
                "benchmark_module": {
                    "type": "string",
                    "description": "已有 Macrobenchmark module，例如 benchmark。",
                },
                "serial": {
                    "type": "string",
                    "description": "adb_devices 返回的目标设备 serial。",
                },
                "test_class": {
                    "type": "string",
                    "description": "Macrobenchmark 测试类的完整类名。",
                },
                "test_method": {
                    "type": ["string", "null"],
                    "description": "可选的测试方法名；不指定时传 null。",
                },
            },
            "required": [
                "project_path",
                "benchmark_module",
                "serial",
                "test_class",
                "test_method",
            ],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_project = arguments.get("project_path")
        raw_module = arguments.get("benchmark_module")
        serial = arguments.get("serial")
        test_class = arguments.get("test_class")
        test_method = arguments.get("test_method")

        if not isinstance(raw_project, str) or not raw_project.strip():
            raise ToolError("project_path 必须是非空字符串")
        if not isinstance(raw_module, str) or not self.MODULE_PATTERN.fullmatch(
            raw_module
        ):
            raise ToolError("benchmark_module 格式不合法")
        if not isinstance(serial, str) or not serial.strip():
            raise ToolError("serial 必须是非空字符串")
        if not isinstance(test_class, str) or not self.CLASS_PATTERN.fullmatch(
            test_class
        ):
            raise ToolError("test_class 必须是合法的完整类名")
        if test_method is not None and (
            not isinstance(test_method, str)
            or not self.METHOD_PATTERN.fullmatch(test_method)
        ):
            raise ToolError("test_method 格式不合法")

        project = self.validate_project_path(raw_project)
        benchmark_module = self._normalize_module(raw_module)
        if any(
            part in {".", ".."} for part in Path(benchmark_module).parts
        ):
            raise ToolError("benchmark_module 不允许路径穿越")
        serial = serial.strip()
        test_selector = (
            f"{test_class}#{test_method}" if test_method else test_class
        )

        gradlew = project / "gradlew"
        if not gradlew.is_file():
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                error_type="GRADLEW_NOT_FOUND",
                summary="项目中没有找到 Gradle Wrapper: gradlew。",
            )

        module_path = (project / benchmark_module).resolve()
        self._ensure_within_project(project, module_path)
        build_file = self._first_safe_file(
            project,
            module_path / "build.gradle.kts",
            module_path / "build.gradle",
        )
        if not module_path.is_dir() or build_file is None:
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                error_type="BENCHMARK_MODULE_NOT_FOUND",
                summary="指定的 Benchmark module 不存在或缺少 Gradle 构建文件。",
            )

        test_file = self._find_test_file(
            project,
            module_path,
            test_class,
        )
        if test_file is None:
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                error_type="BENCHMARK_TEST_NOT_FOUND",
                summary="在 benchmark module 的 src/androidTest 中没有找到指定测试类。",
            )

        test_text = test_file.read_text(encoding="utf-8", errors="replace")
        if test_method and not self._contains_test_method(test_text, test_method):
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                error_type="BENCHMARK_TEST_METHOD_NOT_FOUND",
                summary="Benchmark 测试文件中没有找到指定测试方法。",
            )
        if "MacrobenchmarkRule" not in test_text or "StartupTimingMetric" not in test_text:
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                error_type="STARTUP_BENCHMARK_NOT_DETECTED",
                summary="指定测试未检测到 MacrobenchmarkRule 和 StartupTimingMetric。",
            )

        adb_path = shutil.which("adb")
        if adb_path is None:
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                error_type="ADB_NOT_FOUND",
                summary="未找到 adb，请安装 Android SDK Platform-Tools 并配置 PATH。",
            )

        device_error = self._check_device(adb_path, serial)
        if device_error is not None:
            error_type, summary = device_error
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                error_type=error_type,
                summary=summary,
            )

        output_root = (
            module_path
            / "build"
            / "outputs"
            / "connected_android_test_additional_output"
        )
        before_json = self._file_snapshot(output_root, "*benchmarkData.json")
        before_traces = self._file_snapshot(output_root, "*.perfetto-trace")
        started_wall_ns = time.time_ns()

        task = f":{benchmark_module.replace('/', ':')}:connectedCheck"
        selector_argument = (
            "-Pandroid.testInstrumentationRunnerArguments.class="
            f"{test_selector}"
        )
        if gradlew.stat().st_mode & 0o111:
            command = [str(gradlew), task, selector_argument]
        else:
            command = ["bash", str(gradlew), task, selector_argument]
        command.extend(["--console=plain", "--stacktrace"])

        child_env = os.environ.copy()
        child_env["ANDROID_SERIAL"] = serial
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=project,
                capture_output=True,
                text=True,
                timeout=self.DEFAULT_TIMEOUT_SECONDS,
                shell=False,
                env=child_env,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            raw = self._combine_output(
                self._safe_decode(exc.stdout),
                self._safe_decode(exc.stderr),
            )
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                duration_ms=duration_ms,
                error_type="MACROBENCHMARK_TIMEOUT",
                summary=(
                    f"Macrobenchmark 执行超过 {self.DEFAULT_TIMEOUT_SECONDS} 秒，已终止。"
                ),
                important_logs=self._important_logs(raw),
            )
        except OSError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                duration_ms=duration_ms,
                error_type="MACROBENCHMARK_FAILED",
                summary=f"无法执行 Macrobenchmark：{type(exc).__name__}。",
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        raw = self._combine_output(completed.stdout, completed.stderr)
        if completed.returncode != 0:
            error_type = self._classify_failure(raw)
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                duration_ms=duration_ms,
                error_type=error_type,
                summary=self._failure_summary(error_type),
                important_logs=self._important_logs(raw),
            )

        json_files = self._fresh_files(
            project,
            output_root,
            "*benchmarkData.json",
            before_json,
            started_wall_ns,
        )
        trace_files = self._fresh_files(
            project,
            output_root,
            "*.perfetto-trace",
            before_traces,
            started_wall_ns,
        )
        if not json_files:
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                duration_ms=duration_ms,
                error_type="BENCHMARK_JSON_NOT_FOUND",
                summary="Macrobenchmark 执行成功，但没有找到本轮新生成或更新的 Benchmark JSON。",
                trace_files=[str(path) for path in trace_files],
            )

        parsed = self._find_benchmark_result(
            json_files,
            test_class=test_class,
            test_method=test_method,
        )
        if parsed is None:
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                duration_ms=duration_ms,
                error_type="BENCHMARK_RESULT_NOT_FOUND",
                summary="本轮 Benchmark JSON 中没有找到与指定测试匹配的结果。",
                trace_files=[str(path) for path in trace_files],
            )

        json_path, root_data, benchmark = parsed
        metrics = benchmark.get("metrics")
        if not isinstance(metrics, dict) or "timeToInitialDisplayMs" not in metrics:
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                duration_ms=duration_ms,
                error_type="STARTUP_METRIC_NOT_FOUND",
                summary="Benchmark JSON 中没有 timeToInitialDisplayMs，未获得 TTID。",
                benchmark_json_path=str(json_path),
                trace_files=[str(path) for path in trace_files],
            )

        ttid_ms = self._parse_metric(
            "timeToInitialDisplayMs",
            metrics.get("timeToInitialDisplayMs"),
        )
        if ttid_ms is None:
            return self._error_result(
                benchmark_module=benchmark_module,
                serial=serial,
                test_class=test_class,
                test_method=test_method,
                test_selector=test_selector,
                duration_ms=duration_ms,
                error_type="STARTUP_METRIC_INVALID",
                summary="Benchmark JSON 中的 TTID 指标结构不完整。",
                benchmark_json_path=str(json_path),
                trace_files=[str(path) for path in trace_files],
            )

        ttfd_ms = self._parse_metric(
            "timeToFullDisplayMs",
            metrics.get("timeToFullDisplayMs"),
        )
        ttfd_available = ttfd_ms is not None
        summary = (
            "Macrobenchmark 执行成功，已从 Benchmark JSON 获得真实 TTID 和 TTFD。"
            if ttfd_available
            else "Macrobenchmark 执行成功，已获得 TTID；本次 Benchmark JSON 没有 TTFD。"
        )
        return {
            "success": True,
            "benchmark_module": benchmark_module,
            "serial": serial,
            "test_class": test_class,
            "test_method": test_method,
            "test_selector": test_selector,
            "ttid_ms": ttid_ms,
            "ttfd_available": ttfd_available,
            "ttfd_ms": ttfd_ms,
            "run_count": len(ttid_ms["runs"]),
            "repeat_iterations": benchmark.get("repeatIterations"),
            "warmup_iterations": benchmark.get("warmupIterations"),
            "benchmark_json_path": str(json_path),
            "trace_files": [str(path) for path in trace_files],
            "device_context": self._device_context(root_data),
            "duration_ms": duration_ms,
            "error_type": None,
            "summary": summary,
            "important_logs": [],
        }

    def _check_device(
        self,
        adb_path: str,
        serial: str,
    ) -> tuple[str, str] | None:
        try:
            completed = subprocess.run(
                [adb_path, "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=self.DEVICE_CHECK_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return "ADB_COMMAND_FAILED", "检查 ADB 设备列表超时。"
        except OSError as exc:
            return (
                "ADB_COMMAND_FAILED",
                f"无法检查 ADB 设备列表：{type(exc).__name__}。",
            )

        if completed.returncode != 0:
            return "ADB_COMMAND_FAILED", "adb devices -l 执行失败。"
        devices = AdbDevicesTool._parse_devices(completed.stdout)
        matched = next(
            (device for device in devices if device["serial"] == serial),
            None,
        )
        if matched is None:
            return "DEVICE_NOT_FOUND", "指定 serial 不在当前 ADB 设备列表中。"
        if matched["state"] == "unauthorized":
            return "DEVICE_UNAUTHORIZED", "设备尚未授权当前电脑进行 ADB 调试。"
        if matched["state"] == "offline":
            return "DEVICE_OFFLINE", "设备当前处于 offline 状态。"
        if matched["state"] != "device":
            return "DEVICE_NOT_READY", f"设备当前状态不可用于 Benchmark：{matched['state']}。"
        return None

    @classmethod
    def _find_test_file(
        cls,
        project: Path,
        module_path: Path,
        test_class: str,
    ) -> Path | None:
        class_parts = test_class.split(".")
        source_roots = (
            module_path / "src" / "main" / "java",
            module_path / "src" / "main" / "kotlin",
            module_path / "src" / "androidTest" / "java",
            module_path / "src" / "androidTest" / "kotlin",
        )
        for source_root in source_roots:
            for extension in (".kt", ".java"):
                candidate = source_root.joinpath(
                    *class_parts[:-1],
                    class_parts[-1] + extension,
                )
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(project)
                except ValueError:
                    continue
                return resolved
        return None

    @staticmethod
    def _contains_test_method(test_text: str, method: str) -> bool:
        return bool(
            re.search(rf"\bfun\s+{re.escape(method)}\s*\(", test_text)
            or re.search(
                rf"(?m)^\s*(?:(?:public|protected|private|static|final)\s+)*"
                rf"[A-Za-z_$][A-Za-z0-9_$<>,.?\[\]]*\s+"
                rf"{re.escape(method)}\s*\(",
                test_text,
            )
        )

    @staticmethod
    def _file_snapshot(root: Path, pattern: str) -> dict[Path, tuple[int, int]]:
        if not root.is_dir():
            return {}
        snapshot: dict[Path, tuple[int, int]] = {}
        for path in root.rglob(pattern):
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[path.resolve()] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    @classmethod
    def _fresh_files(
        cls,
        project: Path,
        root: Path,
        pattern: str,
        before: dict[Path, tuple[int, int]],
        started_wall_ns: int,
    ) -> list[Path]:
        if not root.is_dir():
            return []
        fresh: list[Path] = []
        for path in root.rglob(pattern):
            resolved = path.resolve()
            try:
                resolved.relative_to(project)
                stat = resolved.stat()
            except (ValueError, OSError):
                continue
            signature = (stat.st_mtime_ns, stat.st_size)
            previous = before.get(resolved)
            changed = previous is None or previous != signature
            generated_after_start = stat.st_mtime_ns >= started_wall_ns
            if changed and generated_after_start:
                fresh.append(resolved)
        return sorted(fresh, key=lambda item: item.stat().st_mtime_ns, reverse=True)

    @classmethod
    def _find_benchmark_result(
        cls,
        json_files: list[Path],
        *,
        test_class: str,
        test_method: str | None,
    ) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
        for json_path in json_files:
            try:
                root_data = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(root_data, dict):
                continue
            benchmarks = root_data.get("benchmarks")
            if not isinstance(benchmarks, list):
                continue
            for benchmark in benchmarks:
                if not isinstance(benchmark, dict):
                    continue
                if benchmark.get("className") != test_class:
                    continue
                if test_method and not cls._benchmark_name_matches(
                    benchmark.get("name"),
                    test_class,
                    test_method,
                ):
                    continue
                return json_path, root_data, benchmark
        return None

    @staticmethod
    def _benchmark_name_matches(
        name: Any,
        test_class: str,
        test_method: str,
    ) -> bool:
        if not isinstance(name, str):
            return False
        return (
            name == test_method
            or name.startswith(f"{test_method}[")
            or name == f"{test_class}#{test_method}"
            or name == f"{test_class}.{test_method}"
        )

    @staticmethod
    def _parse_metric(metric_name: str, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        minimum = value.get("minimum")
        median = value.get("median")
        maximum = value.get("maximum")
        runs = value.get("runs")
        if not all(isinstance(item, (int, float)) for item in (minimum, median, maximum)):
            return None
        if not isinstance(runs, list) or not all(
            isinstance(item, (int, float)) for item in runs
        ):
            return None
        return {
            "metric_name": metric_name,
            "minimum": float(minimum),
            "median": float(median),
            "maximum": float(maximum),
            "runs": [float(item) for item in runs],
        }

    @staticmethod
    def _device_context(root_data: dict[str, Any]) -> dict[str, Any]:
        context = root_data.get("context")
        if not isinstance(context, dict):
            return {}

        build = context.get("build")
        if not isinstance(build, dict):
            build = {}
        version = build.get("version")
        if not isinstance(version, dict):
            version = {}

        result: dict[str, Any] = {}
        for key in ("brand", "model", "device"):
            value = build.get(key)
            if value is None:
                value = context.get(key)
            if value is not None:
                result[key] = value

        sdk = version.get("sdk")
        if sdk is None:
            sdk = context.get("sdk")
        if sdk is not None:
            result["sdk"] = sdk

        for key in ("cpuCoreCount", "cpuMaxFreqHz"):
            if key in context:
                result[key] = context[key]
        return result

    @staticmethod
    def _classify_failure(raw: str) -> str:
        upper = raw.upper()
        if "DEBUGGABLE" in upper:
            return "BENCHMARK_DEBUGGABLE"
        if "EMULATOR" in upper:
            return "BENCHMARK_EMULATOR"
        if "LOW-BATTERY" in upper or "LOW_BATTERY" in upper:
            return "BENCHMARK_LOW_BATTERY"
        if "NOT-PROFILEABLE" in upper or "NOT_PROFILEABLE" in upper:
            return "BENCHMARK_NOT_PROFILEABLE"
        lower = raw.lower()
        if (
            "there were failing tests" in lower
            or "tests failed" in lower
            or "test failed" in lower
            or "assertionerror" in lower
        ):
            return "BENCHMARK_TEST_FAILED"
        if (
            "compilation failed" in lower
            or "could not compile" in lower
            or "compiledebug" in lower
        ):
            return "BENCHMARK_BUILD_FAILED"
        return "MACROBENCHMARK_FAILED"

    @staticmethod
    def _failure_summary(error_type: str) -> str:
        summaries = {
            "BENCHMARK_DEBUGGABLE": (
                "Macrobenchmark 拒绝测量 debuggable 目标；未 suppress 此可靠性错误。"
            ),
            "BENCHMARK_EMULATOR": (
                "Macrobenchmark 检测到模拟器；模拟器不适合产生代表真实设备的性能数据。"
            ),
            "BENCHMARK_LOW_BATTERY": (
                "设备电量过低，Macrobenchmark 已阻止不可靠测量。"
            ),
            "BENCHMARK_NOT_PROFILEABLE": (
                "目标应用不满足 profileable 要求，Macrobenchmark 已阻止测量。"
            ),
            "BENCHMARK_TEST_FAILED": "Macrobenchmark 测试执行失败。",
            "BENCHMARK_BUILD_FAILED": "Benchmark module 构建失败。",
            "MACROBENCHMARK_FAILED": "Macrobenchmark 执行失败，请查看关键日志。",
        }
        return summaries.get(error_type, "Macrobenchmark 执行失败。")

    @staticmethod
    def _important_logs(raw: str) -> list[str]:
        patterns = (
            "DEBUGGABLE",
            "EMULATOR",
            "LOW-BATTERY",
            "LOW_BATTERY",
            "NOT-PROFILEABLE",
            "NOT_PROFILEABLE",
            "FAILURE:",
            "FAILED",
            "Exception",
            "Error",
        )
        selected: list[str] = []
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if line and any(pattern.lower() in line.lower() for pattern in patterns):
                selected.append(line[:500])
        return selected[-40:]

    @staticmethod
    def _normalize_module(module: str) -> str:
        return module.strip().lstrip(":").replace(":", "/")

    @staticmethod
    def _ensure_within_project(project: Path, candidate: Path) -> None:
        try:
            candidate.relative_to(project)
        except ValueError as exc:
            raise ToolError("拒绝访问当前 Android 项目之外的 Benchmark module") from exc

    @classmethod
    def _first_safe_file(
        cls,
        project: Path,
        *paths: Path,
    ) -> Path | None:
        for path in paths:
            if not path.is_file():
                continue
            resolved = path.resolve()
            try:
                cls._ensure_within_project(project, resolved)
            except ToolError:
                continue
            return resolved
        return None

    @staticmethod
    def _safe_decode(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _combine_output(stdout: str | None, stderr: str | None) -> str:
        return ((stdout or "") + "\n" + (stderr or "")).strip()

    @staticmethod
    def _error_result(
        *,
        benchmark_module: str,
        serial: str,
        test_class: str,
        test_method: str | None,
        test_selector: str,
        error_type: str,
        summary: str,
        duration_ms: int = 0,
        benchmark_json_path: str | None = None,
        trace_files: list[str] | None = None,
        important_logs: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "benchmark_module": benchmark_module,
            "serial": serial,
            "test_class": test_class,
            "test_method": test_method,
            "test_selector": test_selector,
            "ttid_ms": None,
            "ttfd_available": False,
            "ttfd_ms": None,
            "run_count": 0,
            "repeat_iterations": None,
            "warmup_iterations": None,
            "benchmark_json_path": benchmark_json_path,
            "trace_files": trace_files or [],
            "device_context": {},
            "duration_ms": duration_ms,
            "error_type": error_type,
            "summary": summary,
            "important_logs": important_logs or [],
        }
