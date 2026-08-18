from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError


class GradleBuildTool(BaseTool):
    name = "gradle_build"
    description = (
        "在用户指定的 Android 项目中，通过项目自身 Gradle Wrapper 执行一个 Gradle Task。"
        "用于真实验证项目是否可构建。"
        "返回成功状态、退出码、耗时、错误分类、经过裁剪的重要日志，"
        "并在 assemble 成功后返回匹配当前 module/variant 的全部 APK 路径。"
    )

    DEFAULT_TIMEOUT_SECONDS = 600

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "用户指定的 Android 项目绝对路径。",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "要执行的单个 Gradle task，例如 assembleDebug、"
                        "assembleRelease 或 :app:assembleDebug。"
                    ),
                },
            },
            "required": ["project_path", "task"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_path = arguments.get("project_path")
        task = arguments.get("task")

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("project_path 必须是非空字符串")
        if not isinstance(task, str) or not task.strip():
            raise ToolError("task 必须是非空字符串")

        task = task.strip()
        if not re.fullmatch(r"[A-Za-z0-9_:.\-]+", task):
            raise ToolError(
                "Gradle task 包含不允许的字符。"
                "仅允许字母、数字、下划线、冒号、点和连字符。"
            )

        project = self.validate_project_path(raw_path)
        if not project.exists() or not project.is_dir():
            raise ToolError("项目路径不存在或不是目录")

        gradlew = project / "gradlew"
        if not gradlew.exists():
            return {
                "success": False,
                "task": task,
                "timeout_seconds": self.DEFAULT_TIMEOUT_SECONDS,
                "exit_code": None,
                "duration_ms": 0,
                "error_type": "GRADLEW_NOT_FOUND",
                "summary": "项目中没有找到 gradlew。",
                "important_logs": [],
                "apk_outputs": [],
            }

        if gradlew.stat().st_mode & 0o111:
            command = [str(gradlew), task, "--console=plain", "--stacktrace"]
        else:
            command = [
                "bash",
                str(gradlew),
                task,
                "--console=plain",
                "--stacktrace",
            ]

        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=project,
                capture_output=True,
                text=True,
                timeout=self.DEFAULT_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            raw = self._combine_output(
                self._safe_decode(exc.stdout),
                self._safe_decode(exc.stderr),
            )
            return {
                "success": False,
                "task": task,
                "timeout_seconds": self.DEFAULT_TIMEOUT_SECONDS,
                "exit_code": None,
                "duration_ms": duration_ms,
                "error_type": "BUILD_TIMEOUT",
                "summary": (
                    f"Gradle 执行超过 {self.DEFAULT_TIMEOUT_SECONDS} 秒，已终止。"
                ),
                "important_logs": self._important_lines(raw),
                "apk_outputs": [],
            }

        duration_ms = int((time.monotonic() - started) * 1000)
        raw = self._combine_output(completed.stdout, completed.stderr)
        success = completed.returncode == 0

        error_type = None if success else self._classify_error(raw)
        summary = (
            "Gradle task 执行成功。"
            if success
            else self._build_failure_summary(error_type, raw)
        )

        return {
            "success": success,
            "task": task,
            "timeout_seconds": self.DEFAULT_TIMEOUT_SECONDS,
            "exit_code": completed.returncode,
            "duration_ms": duration_ms,
            "error_type": error_type,
            "summary": summary,
            "important_logs": self._important_lines(raw),
            "apk_outputs": (
                self._find_apk_outputs(project, task) if success else []
            ),
        }

    @staticmethod
    def _find_apk_outputs(project: Path, task: str) -> list[str]:
        task_parts = task.strip(":").split(":")
        task_name = task_parts[-1]
        variant_match = re.fullmatch(r"assemble([A-Z][A-Za-z0-9]*)", task_name)
        if variant_match is None:
            return []

        expected_variant = variant_match.group(1).lower()
        expected_module = Path(*task_parts[:-1]) if len(task_parts) > 1 else None
        outputs: list[str] = []

        for candidate in project.rglob("*.apk"):
            resolved = candidate.resolve()
            try:
                relative = resolved.relative_to(project)
            except ValueError:
                continue

            parts = relative.parts
            build_index = next(
                (
                    index
                    for index in range(len(parts) - 2)
                    if parts[index:index + 3] == (
                        "build",
                        "outputs",
                        "apk",
                    )
                ),
                None,
            )
            if build_index is None:
                continue

            module_path = Path(*parts[:build_index])
            if expected_module is not None and module_path != expected_module:
                continue
            if not GradleBuildTool._is_application_module(
                project,
                module_path,
            ):
                continue

            variant_parts = parts[build_index + 3:-1]
            actual_variant = "".join(variant_parts).lower()
            if actual_variant != expected_variant:
                continue
            outputs.append(str(resolved))

        return sorted(outputs)

    @staticmethod
    def _is_application_module(project: Path, module_path: Path) -> bool:
        module_dir = (project / module_path).resolve()
        try:
            module_dir.relative_to(project)
        except ValueError:
            return False

        build_texts: list[str] = []
        for filename in ("build.gradle.kts", "build.gradle"):
            candidate = (module_dir / filename).resolve()
            try:
                candidate.relative_to(project)
            except ValueError:
                continue
            if candidate.is_file():
                build_texts.append(
                    candidate.read_text(encoding="utf-8", errors="replace")
                )
        return "com.android.application" in "\n".join(build_texts)

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
    def _classify_error(raw: str) -> str:
        lower = raw.lower()

        if (
            "incompatible gradle jvm" in lower
            or "incompatible with the gradle jvm version" in lower
            or "supports java versions between" in lower
            or "unsupported class file major version" in lower
        ):
            return "JAVA_GRADLE_INCOMPATIBLE"

        if (
            "android.useandroidx" in lower
            and "not enabled" in lower
        ):
            return "ANDROIDX_DISABLED"

        if (
            "could not resolve all" in lower
            or "could not find " in lower
            or "could not resolve " in lower
        ):
            return "DEPENDENCY_RESOLUTION_FAILED"

        if (
            "compilation failed" in lower
            or "compiledebugjavawithjavac" in lower
            or "compilereleasejavawithjavac" in lower
            or "error:" in lower
            or "错误:" in raw
        ):
            return "COMPILATION_FAILED"

        if "sdk location not found" in lower:
            return "ANDROID_SDK_NOT_FOUND"

        return "GRADLE_BUILD_FAILED"

    @staticmethod
    def _build_failure_summary(error_type: str, raw: str) -> str:
        mapping = {
            "JAVA_GRADLE_INCOMPATIBLE": "Java 与 Gradle/JVM 版本兼容性异常。",
            "ANDROIDX_DISABLED": "项目依赖 AndroidX，但 android.useAndroidX 未正确开启。",
            "DEPENDENCY_RESOLUTION_FAILED": "Gradle 依赖解析失败。",
            "COMPILATION_FAILED": "源码或资源编译失败。",
            "ANDROID_SDK_NOT_FOUND": "未找到 Android SDK 配置。",
            "GRADLE_BUILD_FAILED": "Gradle 构建失败，需查看关键日志。",
        }
        return mapping.get(error_type, "Gradle 构建失败。")

    @staticmethod
    def _important_lines(raw: str) -> list[str]:
        if not raw:
            return []

        lines = [line.rstrip() for line in raw.splitlines()]
        patterns = (
            "FAILURE:",
            "* What went wrong:",
            "Execution failed for task",
            "Could not resolve",
            "Could not find ",
            "Could not determine",
            "incompatible",
            "android.useAndroidX",
            "SDK location not found",
            "error:",
            "错误:",
            "Exception",
            "BUILD FAILED",
            "BUILD SUCCESSFUL",
        )

        selected: list[str] = []
        seen: set[str] = set()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if any(pattern.lower() in stripped.lower() for pattern in patterns):
                if stripped not in seen:
                    selected.append(stripped)
                    seen.add(stripped)

        # 如果没有匹配到重点，保留末尾少量日志，避免把几千行直接塞给模型。
        if not selected:
            selected = [line.strip() for line in lines[-20:] if line.strip()]

        return selected[-40:]
