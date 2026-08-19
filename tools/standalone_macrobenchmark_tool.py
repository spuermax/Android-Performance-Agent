from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError
from tools.benchmark_readiness_tool import InspectBenchmarkReadinessTool
from tools.macrobenchmark_tool import RunMacrobenchmarkTool


class RunStandaloneMacrobenchmarkTool(BaseTool):
    name = "run_standalone_macrobenchmark"
    description = (
        "使用 Android Performance Agent 自带、与用户工程分离的 self-instrumenting "
        "Harness，对显式设备上已经安装的目标 package 执行 COLD Startup "
        "Macrobenchmark，返回本轮 Benchmark JSON 中的 TTID/TTFD 和 Perfetto Trace。"
        "该 Tool 不修改用户工程，也不会 suppress 可靠性错误。"
    )

    MEASUREMENT_METHOD = "STANDALONE_MACROBENCHMARK"
    HARNESS_PACKAGE = "com.androidperformance.standalone"
    TEST_CLASS = f"{HARNESS_PACKAGE}.StandaloneStartupBenchmark"
    TEST_METHOD = "startup"
    RUNNER = "androidx.test.runner.AndroidJUnitRunner"
    BUILD_TIMEOUT_SECONDS = 600
    INSTALL_TIMEOUT_SECONDS = 120
    ADB_TIMEOUT_SECONDS = 30
    INSTRUMENTATION_TIMEOUT_SECONDS = 600
    PULL_TIMEOUT_SECONDS = 180
    SERIAL_PATTERN = InspectBenchmarkReadinessTool.SERIAL_PATTERN
    PACKAGE_PATTERN = InspectBenchmarkReadinessTool.PACKAGE_PATTERN

    def __init__(
        self,
        allowed_project_path: Path,
        *,
        harness_root: Path | None = None,
        results_root: Path | None = None,
        readiness_tool: InspectBenchmarkReadinessTool | None = None,
    ) -> None:
        super().__init__(allowed_project_path=allowed_project_path)
        repository_root = Path(__file__).resolve().parents[1]
        self.harness_root = (
            harness_root
            if harness_root is not None
            else repository_root / "harness" / "standalone-macrobenchmark"
        ).resolve()
        self.results_root = (
            results_root
            if results_root is not None
            else repository_root / "harness" / "results" / "tool-runs"
        ).resolve()
        self.readiness_tool = readiness_tool or InspectBenchmarkReadinessTool(
            allowed_project_path=allowed_project_path
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "serial": {
                    "type": "string",
                    "description": "adb_devices 返回的目标设备 serial。",
                },
                "target_package": {
                    "type": "string",
                    "description": "设备上已经安装且 readiness 通过的目标 package。",
                },
                "iterations": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Startup Macrobenchmark 重复次数，建议默认 5。",
                },
                "startup_mode": {
                    "type": "string",
                    "enum": ["COLD"],
                    "description": "V0.2.7 仅支持 COLD。",
                },
            },
            "required": ["serial", "target_package", "iterations", "startup_mode"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        serial = arguments.get("serial")
        target_package = arguments.get("target_package")
        iterations = arguments.get("iterations")
        startup_mode = arguments.get("startup_mode")
        if not isinstance(serial, str) or not self.SERIAL_PATTERN.fullmatch(
            serial.strip()
        ):
            raise ToolError("serial 格式不合法")
        if not isinstance(target_package, str) or not self.PACKAGE_PATTERN.fullmatch(
            target_package.strip()
        ):
            raise ToolError("target_package 格式不合法")
        if (
            isinstance(iterations, bool)
            or not isinstance(iterations, int)
            or iterations < 1
            or iterations > 100
        ):
            raise ToolError("iterations 必须是 1 到 100 的整数")
        if startup_mode != "COLD":
            raise ToolError("V0.2.7 的 startup_mode 仅支持 COLD")
        serial = serial.strip()
        target_package = target_package.strip()

        started = time.monotonic()
        readiness = self.readiness_tool.execute(
            {"serial": serial, "package_name": target_package}
        )
        if not readiness.get("benchmark_ready"):
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                duration_ms=self._duration_ms(started),
                error_type=readiness.get("error_type") or "BENCHMARK_NOT_READY",
                summary="目标 APK readiness 检查未通过，未执行 Standalone Macrobenchmark。",
                readiness=readiness,
            )

        adb_path = shutil.which("adb")
        if adb_path is None:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                duration_ms=self._duration_ms(started),
                error_type="ADB_NOT_FOUND",
                summary="未找到 adb，请安装 Android SDK Platform-Tools 并配置 PATH。",
                readiness=readiness,
            )

        harness_error = self._validate_harness()
        if harness_error is not None:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                duration_ms=self._duration_ms(started),
                error_type="HARNESS_NOT_FOUND",
                summary=harness_error,
                readiness=readiness,
            )

        run_id = self._new_run_id()
        result_dir = self.results_root / run_id
        try:
            result_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                duration_ms=self._duration_ms(started),
                error_type="RESULT_DIRECTORY_FAILED",
                summary=f"无法创建本轮唯一结果目录：{type(exc).__name__}。",
                readiness=readiness,
            )

        build = self._build_harness(result_dir)
        if build["error_type"] is not None:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type=build["error_type"],
                summary=build["summary"],
                readiness=readiness,
                important_logs=build["important_logs"],
            )

        harness_apk = (
            self.harness_root
            / "benchmark"
            / "build"
            / "outputs"
            / "apk"
            / "debug"
            / "benchmark-debug.apk"
        )
        if not harness_apk.is_file():
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type="HARNESS_APK_NOT_FOUND",
                summary="Harness 构建成功，但没有找到预期的 Benchmark APK。",
                readiness=readiness,
            )

        install = self._run_command(
            [adb_path, "-s", serial, "install", "-r", str(harness_apk)],
            timeout=self.INSTALL_TIMEOUT_SECONDS,
        )
        self._write_log(result_dir / "install.log", install)
        if install["exception"] is not None or install["returncode"] != 0 or (
            "success" not in install["stdout"].lower()
        ):
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type="HARNESS_INSTALL_FAILED",
                summary="Standalone Harness APK 安装失败。",
                readiness=readiness,
                important_logs=self._important_logs(install["raw"]),
            )

        remote_results = f"/sdcard/Download/android-performance-agent-{run_id}"
        mkdir = self._run_command(
            [adb_path, "-s", serial, "shell", "mkdir", "-p", remote_results],
            timeout=self.ADB_TIMEOUT_SECONDS,
        )
        if mkdir["exception"] is not None or mkdir["returncode"] != 0:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type="ADB_COMMAND_FAILED",
                summary="无法在设备上创建本轮 Benchmark 输出目录。",
                readiness=readiness,
                important_logs=self._important_logs(mkdir["raw"]),
            )

        instrumentation_command = [
            adb_path,
            "-s",
            serial,
            "shell",
            "am",
            "instrument",
            "-w",
            "-r",
            "-e",
            "class",
            self.TEST_CLASS,
            "-e",
            "targetPackage",
            target_package,
            "-e",
            "iterations",
            str(iterations),
            "-e",
            "additionalTestOutputDir",
            remote_results,
            f"{self.HARNESS_PACKAGE}/{self.RUNNER}",
        ]
        instrumentation = self._run_command(
            instrumentation_command,
            timeout=self.INSTRUMENTATION_TIMEOUT_SECONDS,
        )
        self._write_log(result_dir / "instrumentation.log", instrumentation)

        device_output = result_dir / "device-output"
        pull = self._run_command(
            [adb_path, "-s", serial, "pull", remote_results, str(device_output)],
            timeout=self.PULL_TIMEOUT_SECONDS,
        )
        self._write_log(result_dir / "pull.log", pull)

        if not self._instrumentation_succeeded(instrumentation):
            error_type = self._classify_instrumentation_failure(instrumentation["raw"])
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type=error_type,
                summary=self._failure_summary(error_type),
                readiness=readiness,
                important_logs=self._important_logs(instrumentation["raw"]),
            )
        if pull["exception"] is not None or pull["returncode"] != 0:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type="BENCHMARK_OUTPUT_PULL_FAILED",
                summary="Macrobenchmark 执行成功，但拉取本轮结果文件失败。",
                readiness=readiness,
                important_logs=self._important_logs(pull["raw"]),
            )

        json_files = sorted(device_output.rglob("*benchmarkData.json"))
        trace_files = sorted(device_output.rglob("*.perfetto-trace"))
        if not json_files:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type="BENCHMARK_JSON_NOT_FOUND",
                summary="Instrumentation 成功，但本轮唯一结果目录中没有 Benchmark JSON。",
                readiness=readiness,
                trace_files=trace_files,
            )

        parsed = self._find_benchmark_result(json_files)
        if parsed is None:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type="BENCHMARK_RESULT_NOT_FOUND",
                summary="本轮 Benchmark JSON 中没有找到 Standalone Startup 测试结果。",
                readiness=readiness,
                trace_files=trace_files,
            )

        json_path, root_data, benchmark = parsed
        metrics = benchmark.get("metrics")
        if not isinstance(metrics, dict) or "timeToInitialDisplayMs" not in metrics:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type="STARTUP_METRIC_NOT_FOUND",
                summary="Benchmark JSON 中没有 timeToInitialDisplayMs，未获得 TTID。",
                readiness=readiness,
                benchmark_json_path=json_path,
                trace_files=trace_files,
            )

        ttid_ms = RunMacrobenchmarkTool._parse_metric(
            "timeToInitialDisplayMs", metrics.get("timeToInitialDisplayMs")
        )
        if ttid_ms is None:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type="STARTUP_METRIC_INVALID",
                summary="Benchmark JSON 中的 TTID 指标结构不完整。",
                readiness=readiness,
                benchmark_json_path=json_path,
                trace_files=trace_files,
            )
        if not trace_files:
            return self._error_result(
                serial=serial,
                target_package=target_package,
                iterations=iterations,
                startup_mode=startup_mode,
                run_id=run_id,
                result_dir=result_dir,
                duration_ms=self._duration_ms(started),
                error_type="PERFETTO_TRACE_NOT_FOUND",
                summary="Benchmark JSON 已生成，但本轮没有收集到 Perfetto Trace。",
                readiness=readiness,
                benchmark_json_path=json_path,
            )

        ttfd_ms = RunMacrobenchmarkTool._parse_metric(
            "timeToFullDisplayMs", metrics.get("timeToFullDisplayMs")
        )
        ttfd_available = ttfd_ms is not None
        summary = (
            "Standalone Macrobenchmark 成功，已从本轮 JSON 获得 TTID 和 TTFD。"
            if ttfd_available
            else "Standalone Macrobenchmark 成功，已获得 TTID；本轮 JSON 没有 TTFD。"
        )
        warnings = list(readiness.get("warnings") or [])
        if len(trace_files) != len(ttid_ms["runs"]):
            warnings.append(
                "本轮 Perfetto Trace 数量与 TTID runs 数量不同，请检查 instrumentation 日志。"
            )
        return {
            "success": True,
            "measurement_method": self.MEASUREMENT_METHOD,
            "serial": serial,
            "target_package": target_package,
            "startup_mode": startup_mode,
            "compilation_mode": "DEFAULT",
            "requested_iterations": iterations,
            "ttid_ms": ttid_ms,
            "ttfd_available": ttfd_available,
            "ttfd_ms": ttfd_ms,
            "run_count": len(ttid_ms["runs"]),
            "repeat_iterations": benchmark.get("repeatIterations"),
            "warmup_iterations": benchmark.get("warmupIterations"),
            "benchmark_json_path": str(json_path.resolve()),
            "trace_files": [str(path.resolve()) for path in trace_files],
            "device_context": RunMacrobenchmarkTool._device_context(root_data),
            "run_id": run_id,
            "result_dir": str(result_dir),
            "remote_result_dir": remote_results,
            "duration_ms": self._duration_ms(started),
            "readiness": readiness,
            "warnings": warnings,
            "error_type": None,
            "summary": summary,
            "important_logs": [],
        }

    def _validate_harness(self) -> str | None:
        required = (
            self.harness_root / "gradlew",
            self.harness_root / "settings.gradle.kts",
            self.harness_root / "benchmark" / "build.gradle.kts",
            self.harness_root
            / "benchmark"
            / "src"
            / "main"
            / "java"
            / "com"
            / "androidperformance"
            / "standalone"
            / "StandaloneStartupBenchmark.java",
            self.harness_root / "harness-host" / "build.gradle.kts",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            return "Standalone Harness 文件不完整：" + "，".join(missing)
        return None

    def _build_harness(self, result_dir: Path) -> dict[str, Any]:
        gradlew = self.harness_root / "gradlew"
        if gradlew.stat().st_mode & 0o111:
            command = [str(gradlew)]
        else:
            command = ["bash", str(gradlew)]
        command.extend(
            ["--no-daemon", ":benchmark:assembleDebug", "--console=plain"]
        )
        completed = self._run_command(
            command,
            timeout=self.BUILD_TIMEOUT_SECONDS,
            cwd=self.harness_root,
            env=os.environ.copy(),
        )
        self._write_log(result_dir / "build.log", completed)
        if completed["exception"] == "TimeoutExpired":
            return {
                "error_type": "HARNESS_BUILD_TIMEOUT",
                "summary": (
                    f"Harness 构建超过 {self.BUILD_TIMEOUT_SECONDS} 秒，结果未知。"
                ),
                "important_logs": self._important_logs(completed["raw"]),
            }
        if completed["exception"] is not None or completed["returncode"] != 0:
            return {
                "error_type": "HARNESS_BUILD_FAILED",
                "summary": "Standalone Harness 构建失败。",
                "important_logs": self._important_logs(completed["raw"]),
            }
        return {"error_type": None, "summary": "", "important_logs": []}

    @staticmethod
    def _run_command(
        command: list[str],
        *,
        timeout: int,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            return {
                "returncode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "raw": (stdout + "\n" + stderr).strip(),
                "exception": None,
            }
        except subprocess.TimeoutExpired as exc:
            stdout = RunStandaloneMacrobenchmarkTool._safe_decode(exc.stdout)
            stderr = RunStandaloneMacrobenchmarkTool._safe_decode(exc.stderr)
            return {
                "returncode": None,
                "stdout": stdout,
                "stderr": stderr,
                "raw": (stdout + "\n" + stderr).strip(),
                "exception": "TimeoutExpired",
            }
        except OSError as exc:
            return {
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "raw": f"{type(exc).__name__}: {exc}",
                "exception": type(exc).__name__,
            }

    @staticmethod
    def _write_log(path: Path, command_result: dict[str, Any]) -> None:
        path.write_text(command_result["raw"] + "\n", encoding="utf-8")

    @classmethod
    def _find_benchmark_result(
        cls,
        json_files: list[Path],
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
                if benchmark.get("className") != cls.TEST_CLASS:
                    continue
                if not RunMacrobenchmarkTool._benchmark_name_matches(
                    benchmark.get("name"), cls.TEST_CLASS, cls.TEST_METHOD
                ):
                    continue
                return json_path, root_data, benchmark
        return None

    @staticmethod
    def _instrumentation_succeeded(result: dict[str, Any]) -> bool:
        raw = result["raw"]
        if result["exception"] is not None or result["returncode"] != 0:
            return False
        failure_markers = (
            "FAILURES!!!",
            "INSTRUMENTATION_FAILED",
            "INSTRUMENTATION_ABORTED",
            "Process crashed",
        )
        return (
            "INSTRUMENTATION_CODE: -1" in raw
            and "OK (" in raw
            and not any(marker.lower() in raw.lower() for marker in failure_markers)
        )

    @staticmethod
    def _classify_instrumentation_failure(raw: str) -> str:
        classified = RunMacrobenchmarkTool._classify_failure(raw)
        if classified == "MACROBENCHMARK_FAILED" and "PROFILERINSTALLER" in raw.upper():
            return "PROFILER_INSTALLER_NOT_FOUND"
        return classified

    @staticmethod
    def _failure_summary(error_type: str) -> str:
        if error_type == "PROFILER_INSTALLER_NOT_FOUND":
            return "Macrobenchmark 无法使用目标 App 的 ProfileInstaller hook。"
        return RunMacrobenchmarkTool._failure_summary(error_type)

    @staticmethod
    def _important_logs(raw: str) -> list[str]:
        selected = RunMacrobenchmarkTool._important_logs(raw)
        if selected:
            return selected
        return [line.strip()[:500] for line in raw.splitlines() if line.strip()][-20:]

    @staticmethod
    def _safe_decode(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _duration_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _new_run_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"

    @classmethod
    def _error_result(
        cls,
        *,
        serial: str,
        target_package: str,
        iterations: int,
        startup_mode: str,
        error_type: str,
        summary: str,
        duration_ms: int,
        readiness: dict[str, Any] | None = None,
        run_id: str | None = None,
        result_dir: Path | None = None,
        benchmark_json_path: Path | None = None,
        trace_files: list[Path] | None = None,
        important_logs: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "measurement_method": cls.MEASUREMENT_METHOD,
            "serial": serial,
            "target_package": target_package,
            "startup_mode": startup_mode,
            "compilation_mode": "DEFAULT",
            "requested_iterations": iterations,
            "ttid_ms": None,
            "ttfd_available": False,
            "ttfd_ms": None,
            "run_count": 0,
            "repeat_iterations": None,
            "warmup_iterations": None,
            "benchmark_json_path": (
                str(benchmark_json_path.resolve()) if benchmark_json_path else None
            ),
            "trace_files": [str(path.resolve()) for path in trace_files or []],
            "device_context": (readiness or {}).get("device_context", {}),
            "run_id": run_id,
            "result_dir": str(result_dir) if result_dir else None,
            "duration_ms": duration_ms,
            "readiness": readiness or {},
            "warnings": list((readiness or {}).get("warnings") or []),
            "error_type": error_type,
            "summary": summary,
            "important_logs": important_logs or [],
        }
