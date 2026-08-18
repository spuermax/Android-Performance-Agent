from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError


class InspectProjectTool(BaseTool):
    name = "inspect_project"
    description = (
        "检查指定 Android/Gradle 项目的基础工程环境。"
        "返回项目有效性、Gradle Wrapper、Java、modules、benchmark module、"
        "AndroidX、gradle.properties 和部分 AGP 信息。"
        "在判断项目是否可构建或可进入性能分析之前优先使用。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "用户指定的 Android 项目绝对路径。",
                }
            },
            "required": ["project_path"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_path = arguments.get("project_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("project_path 必须是非空字符串")

        project = self.validate_project_path(raw_path)

        result: dict[str, Any] = {
            "success": True,
            "project_path": str(project),
            "project_exists": project.exists(),
            "is_directory": project.is_dir(),
        }

        if not project.exists() or not project.is_dir():
            result.update(
                {
                    "is_gradle_project": False,
                    "is_android_project": False,
                    "blocking_issues": ["目标路径不存在或不是目录"],
                }
            )
            return result

        settings_file = self._first_existing(
            project / "settings.gradle.kts",
            project / "settings.gradle",
        )
        gradlew = project / "gradlew"
        wrapper_properties = (
            project
            / "gradle"
            / "wrapper"
            / "gradle-wrapper.properties"
        )
        gradle_properties = project / "gradle.properties"

        settings_text = self._read_text(settings_file) if settings_file else ""
        modules = self._parse_modules(settings_text)
        module_types = self._detect_android_module_types(project, modules)
        android_modules = sorted(module_types.keys())
        application_modules = sorted(
            module
            for module, module_type in module_types.items()
            if module_type == "application"
        )

        wrapper_version = self._parse_gradle_wrapper_version(wrapper_properties)
        java = self._java_version()
        androidx_enabled = self._read_property(
            gradle_properties, "android.useAndroidX"
        )
        agp_version = self._detect_agp_version(project)

        benchmark_modules = [
            module
            for module in modules
            if "benchmark" in module.lower()
            or "macrobenchmark" in module.lower()
        ]

        blocking_issues: list[str] = []
        if not settings_file:
            blocking_issues.append("缺少 settings.gradle/settings.gradle.kts")
        if not gradlew.exists():
            blocking_issues.append("缺少 Gradle Wrapper: gradlew")
        if not wrapper_properties.exists():
            blocking_issues.append("缺少 gradle-wrapper.properties")
        if not android_modules:
            blocking_issues.append("未检测到 Android Application/Library 插件模块")

        result.update(
            {
                "is_gradle_project": bool(settings_file and gradlew.exists()),
                "is_android_project": bool(android_modules),
                "settings_file": str(settings_file) if settings_file else None,
                "gradlew_exists": gradlew.exists(),
                "gradle_wrapper_version": wrapper_version,
                "java": java,
                "modules": modules,
                "android_modules": android_modules,
                "module_types": module_types,
                "application_modules": application_modules,
                "primary_application_module": (
                    application_modules[0]
                    if len(application_modules) == 1
                    else None
                ),
                "benchmark_modules": benchmark_modules,
                "has_benchmark_module": bool(benchmark_modules),
                "gradle_properties_exists": gradle_properties.exists(),
                "androidx_enabled": androidx_enabled,
                "agp_version_best_effort": agp_version,
                "blocking_issues": blocking_issues,
            }
        )
        return result

    @staticmethod
    def _first_existing(*paths: Path) -> Path | None:
        for path in paths:
            if path.exists():
                return path
        return None

    @staticmethod
    def _read_text(path: Path | None) -> str:
        if path is None or not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _parse_modules(self, settings_text: str) -> list[str]:
        modules: set[str] = set()

        # Kotlin DSL: include(":app", ":benchmark")
        for match in re.finditer(r"\binclude\s*\((.*?)\)", settings_text, re.S):
            body = match.group(1)
            for quoted in re.findall(r"""["']([^"']+)["']""", body):
                modules.add(self._normalize_module(quoted))

        # Groovy DSL: include ':app', ':lib'
        for match in re.finditer(
            r"(?m)^\s*include\s+(.+?)\s*$",
            settings_text,
        ):
            body = match.group(1)
            for quoted in re.findall(r"""["']([^"']+)["']""", body):
                modules.add(self._normalize_module(quoted))

        return sorted(module for module in modules if module)

    @staticmethod
    def _normalize_module(module: str) -> str:
        return module.strip().lstrip(":").replace(":", "/")

    def _detect_android_module_types(
        self,
        project: Path,
        modules: list[str],
    ) -> dict[str, str]:
        detected: dict[str, str] = {}

        plugin_markers = (
            ("com.android.application", "application"),
            ("com.android.library", "library"),
            ("com.android.test", "test"),
            ("com.android.dynamic-feature", "dynamic-feature"),
        )

        for module in modules:
            module_dir = project / module
            candidates = [
                module_dir / "build.gradle.kts",
                module_dir / "build.gradle",
            ]
            build_text = "\n".join(
                self._read_text(path) for path in candidates
            )

            for marker, module_type in plugin_markers:
                if marker in build_text:
                    detected[module] = module_type
                    break

        return detected

    @staticmethod
    def _parse_gradle_wrapper_version(path: Path) -> str | None:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(
            r"distributionUrl=.*gradle-([0-9][0-9A-Za-z.\-+]*)-(?:bin|all)\.zip",
            text,
        )
        return match.group(1) if match else None

    @staticmethod
    def _java_version() -> dict[str, Any]:
        try:
            completed = subprocess.run(
                ["java", "-version"],
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
        except FileNotFoundError:
            return {
                "available": False,
                "version": None,
                "raw": "java command not found",
            }
        except subprocess.TimeoutExpired:
            return {
                "available": False,
                "version": None,
                "raw": "java -version timed out",
            }

        raw = ((completed.stderr or "") + "\n" + (completed.stdout or "")).strip()
        version_match = re.search(r'version\s+"([^"]+)"', raw)
        if not version_match:
            version_match = re.search(r"openjdk\s+([0-9][^\s]*)", raw, re.I)

        return {
            "available": completed.returncode == 0,
            "version": version_match.group(1) if version_match else None,
            "raw": raw[:500],
        }

    @staticmethod
    def _read_property(path: Path, key: str) -> bool | str | None:
        if not path.exists():
            return None
        for raw_line in path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            current_key, value = line.split("=", 1)
            if current_key.strip() == key:
                normalized = value.strip().lower()
                if normalized == "true":
                    return True
                if normalized == "false":
                    return False
                return value.strip()
        return None

    def _detect_agp_version(self, project: Path) -> str | None:
        candidates = [
            project / "build.gradle.kts",
            project / "build.gradle",
            project / "gradle" / "libs.versions.toml",
        ]
        text = "\n".join(self._read_text(path) for path in candidates)

        patterns = [
            r"""com\.android\.tools\.build:gradle:([0-9][0-9A-Za-z.\-+]*)""",
            r"""id\s*\(\s*["']com\.android\.application["']\s*\)\s*version\s*["']([^"']+)["']""",
            r"""id\s+["']com\.android\.application["']\s+version\s+["']([^"']+)["']""",
            r"""(?m)^\s*agp\s*=\s*["']([^"']+)["']""",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None
