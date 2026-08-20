from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError

ASSEMBLE_TASK_PATTERN = re.compile(
    r"^\s*(assemble([A-Z][A-Za-z0-9]*))\b(?:\s+-\s+(.*))?$"
)
EXCLUDED_ASSEMBLE_SUFFIXES = (
    "AndroidTest",
    "UnitTest",
    "TestFixtures",
    "Sources",
    "Javadoc",
)
AGGREGATE_DESCRIPTION_PATTERN = re.compile(
    r"\bmain\s+outputs?\s+for\s+all\b.*\bvariants?\b",
    re.IGNORECASE,
)
CONCRETE_DESCRIPTION_PATTERN = re.compile(
    r"\bmain\s+output\s+for\s+variant\b",
    re.IGNORECASE,
)


def concrete_assemble_tasks(
    task_suffixes: dict[str, str],
    descriptions: dict[str, str | None] | None = None,
) -> set[str]:
    """Filter AGP aggregate assemble tasks from concrete APK variants."""
    descriptions = descriptions or {}
    normalized = {
        task_name: suffix.casefold()
        for task_name, suffix in task_suffixes.items()
    }
    concrete: set[str] = set()
    for task_name, suffix in normalized.items():
        description = descriptions.get(task_name) or ""
        if AGGREGATE_DESCRIPTION_PATTERN.search(description):
            continue
        if CONCRETE_DESCRIPTION_PATTERN.search(description):
            concrete.add(task_name)
            continue
        structurally_aggregate = any(
            suffix != other
            and (other.startswith(suffix) or other.endswith(suffix))
            for other in normalized.values()
        )
        if not structurally_aggregate:
            concrete.add(task_name)
    return concrete


def parse_concrete_assemble_tasks(
    raw: str,
) -> dict[str, tuple[str, str | None]]:
    """Parse Gradle task lines and return only concrete APK variants."""
    parsed: dict[str, tuple[str, str | None]] = {}
    for line in raw.splitlines():
        match = ASSEMBLE_TASK_PATTERN.match(line)
        if match is None:
            continue
        task_name = match.group(1)
        suffix = match.group(2)
        if any(suffix.endswith(excluded) for excluded in EXCLUDED_ASSEMBLE_SUFFIXES):
            continue
        parsed[task_name] = (suffix, match.group(3))

    suffixes = {task_name: value[0] for task_name, value in parsed.items()}
    descriptions = {task_name: value[1] for task_name, value in parsed.items()}
    concrete_names = concrete_assemble_tasks(suffixes, descriptions)
    return {
        task_name: parsed[task_name]
        for task_name in concrete_names
    }


class InspectBuildVariantsTool(BaseTool):
    """Discover real application assemble tasks without modifying the project."""

    name = "inspect_build_variants"
    description = (
        "通过目标 application module 的真实 Gradle tasks 枚举可构建 Variant，"
        "并按 Benchmark/Release-like 优先、Debug 最后进行排序。"
        "该 Tool 只读，不修改用户工程，也不会根据设备品牌猜 Product Flavor。"
    )

    DEFAULT_TIMEOUT_SECONDS = 180
    MODULE_PATTERN = re.compile(r"[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*")

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "用户指定的 Android 项目绝对路径。",
                },
                "module": {
                    "type": "string",
                    "description": (
                        "inspect_project / inspect_app_target 确认的 application module，"
                        "例如 app、edusoho 或 feature:demo。"
                    ),
                },
            },
            "required": ["project_path", "module"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_path = arguments.get("project_path")
        module = arguments.get("module")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("project_path 必须是非空字符串")
        if not isinstance(module, str) or not self.MODULE_PATTERN.fullmatch(module.strip()):
            raise ToolError("module 格式不合法")

        project = self.validate_project_path(raw_path)
        module = module.strip().strip(":")
        module_dir = (project / Path(*module.split(":"))).resolve()
        try:
            module_dir.relative_to(project)
        except ValueError as exc:
            raise ToolError("module 超出项目目录") from exc
        if not module_dir.is_dir():
            return self._failure(
                module,
                "MODULE_NOT_FOUND",
                "目标 module 目录不存在。",
            )

        build_file = self._find_build_file(module_dir)
        if build_file is None:
            return self._failure(
                module,
                "BUILD_FILE_NOT_FOUND",
                "目标 module 没有 build.gradle(.kts)。",
            )
        build_text = build_file.read_text(encoding="utf-8", errors="replace")
        application_plugin_declared = "com.android.application" in build_text

        gradlew = project / "gradlew"
        if not gradlew.is_file():
            return self._failure(
                module,
                "GRADLEW_NOT_FOUND",
                "项目中没有找到 gradlew。",
            )

        module_task = f":{module}:tasks"
        if gradlew.stat().st_mode & 0o111:
            command = [str(gradlew), module_task, "--all", "--console=plain"]
        else:
            command = ["bash", str(gradlew), module_task, "--all", "--console=plain"]

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
        except subprocess.TimeoutExpired:
            return self._failure(
                module,
                "GRADLE_TASK_DISCOVERY_TIMEOUT",
                f"Gradle Variant 枚举超过 {self.DEFAULT_TIMEOUT_SECONDS} 秒。",
            )
        except OSError as exc:
            return self._failure(
                module,
                "GRADLE_TASK_DISCOVERY_FAILED",
                f"无法执行 Gradle：{type(exc).__name__}。",
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        raw = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        if completed.returncode != 0:
            return {
                **self._failure(
                    module,
                    "GRADLE_TASK_DISCOVERY_FAILED",
                    "无法通过 Gradle tasks 枚举目标 module 的 Variant。",
                ),
                "exit_code": completed.returncode,
                "duration_ms": duration_ms,
                "important_logs": self._important_lines(raw),
            }

        variants = self._parse_variants(raw, module, build_text)
        if not variants:
            return {
                **self._failure(
                    module,
                    "NO_ASSEMBLE_VARIANTS_FOUND",
                    "Gradle tasks 执行成功，但没有发现可用 assemble Variant。",
                ),
                "exit_code": completed.returncode,
                "duration_ms": duration_ms,
                "important_logs": self._important_lines(raw),
            }

        return {
            "success": True,
            "module": module,
            "build_file": str(build_file.relative_to(project)),
            "application_plugin_declared": application_plugin_declared,
            "exit_code": completed.returncode,
            "duration_ms": duration_ms,
            "candidate_count": len(variants),
            "variants": variants,
            "recommended_candidates": [
                item["assemble_task"]
                for item in variants
                if item["formal_benchmark_candidate"]
            ],
            "selection_policy": (
                "benchmark build type first, then release-like/non-debug candidates, "
                "debug last; device manufacturer is never used as a flavor selector"
            ),
            "error_type": None,
            "summary": (
                f"从真实 Gradle tasks 发现 {len(variants)} 个 application assemble Variant；"
                "已按 Benchmark/Release-like 优先、Debug 最后排序。"
            ),
        }

    @classmethod
    def _parse_variants(
        cls,
        raw: str,
        module: str,
        build_text: str,
    ) -> list[dict[str, Any]]:
        matches = parse_concrete_assemble_tasks(raw)
        by_task: dict[str, dict[str, Any]] = {}
        for task_name, (suffix, _description) in matches.items():
            build_type = cls._guess_build_type(suffix)
            flavor = cls._guess_flavor(suffix, build_type)
            priority = cls._priority(build_type)
            formal_candidate = build_type in {"benchmark", "release"}
            by_task[task_name] = {
                "variant_name": suffix[0].lower() + suffix[1:],
                "assemble_task": f":{module}:{task_name}",
                "build_type": build_type,
                "flavor": flavor,
                "priority": priority,
                "formal_benchmark_candidate": formal_candidate,
                "debuggable_guess": True if build_type == "debug" else (
                    False if formal_candidate else None
                ),
                "signing_config_declared": cls._signing_config_declared(
                    build_text,
                    build_type,
                ),
                "metadata_confidence": "medium",
                "selection_reason": cls._selection_reason(build_type),
            }
        return sorted(
            by_task.values(),
            key=lambda item: (int(item["priority"]), str(item["variant_name"]).lower()),
        )

    @staticmethod
    def _guess_build_type(suffix: str) -> str:
        lower = suffix.lower()
        if lower.endswith("benchmark"):
            return "benchmark"
        if lower.endswith("release"):
            return "release"
        if lower.endswith("debug"):
            return "debug"
        return "unknown"

    @staticmethod
    def _guess_flavor(suffix: str, build_type: str) -> str | None:
        if build_type == "unknown":
            return None
        if len(suffix) <= len(build_type):
            return None
        prefix = suffix[: -len(build_type)]
        return prefix[0].lower() + prefix[1:] if prefix else None

    @staticmethod
    def _priority(build_type: str) -> int:
        return {
            "benchmark": 0,
            "release": 10,
            "unknown": 50,
            "debug": 100,
        }[build_type]

    @staticmethod
    def _selection_reason(build_type: str) -> str:
        if build_type == "benchmark":
            return "显式 Benchmark build type，最高优先级。"
        if build_type == "release":
            return "Release-like，优先用于正式 Macrobenchmark readiness。"
        if build_type == "debug":
            return "Debug 仅用于构建/安装验证，不作为正式性能测量首选。"
        return "无法从 task 名可靠识别 build type，放在 Release-like 之后、Debug 之前。"

    @classmethod
    def _signing_config_declared(cls, build_text: str, build_type: str) -> bool | None:
        if build_type == "unknown":
            return None
        block = cls._extract_named_block(build_text, build_type)
        if block is None:
            return None
        return "signingConfig" in block or "signingConfig =" in block

    @staticmethod
    def _extract_named_block(text: str, name: str) -> str | None:
        pattern = re.compile(rf"\b{re.escape(name)}\s*\{{")
        match = pattern.search(text)
        if match is None:
            return None
        start = match.start()
        brace = text.find("{", match.start(), match.end() + 1)
        if brace < 0:
            return None
        depth = 0
        for index in range(brace, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        return None

    @staticmethod
    def _find_build_file(module_dir: Path) -> Path | None:
        for filename in ("build.gradle.kts", "build.gradle"):
            candidate = module_dir / filename
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _important_lines(raw: str) -> list[str]:
        if not raw:
            return []
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        selected = [
            line
            for line in lines
            if any(
                token in line.lower()
                for token in (
                    "failure:",
                    "what went wrong",
                    "exception",
                    "error",
                    "build failed",
                )
            )
        ]
        return (selected or lines[-10:])[-20:]

    @staticmethod
    def _failure(module: str, error_type: str, summary: str) -> dict[str, Any]:
        return {
            "success": False,
            "module": module,
            "candidate_count": 0,
            "variants": [],
            "recommended_candidates": [],
            "error_type": error_type,
            "summary": summary,
        }
