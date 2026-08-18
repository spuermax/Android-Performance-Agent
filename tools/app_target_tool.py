from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError
from tools.project_tool import InspectProjectTool


class InspectAppTargetTool(BaseTool):
    name = "inspect_app_target"
    description = (
        "识别 Android 项目中后续 Macrobenchmark 要测试的真实 Application Target。"
        "检查 application module，静态提取显式 applicationId 和 namespace，"
        "并从主 Manifest 解析 MAIN + LAUNCHER Activity 或 activity-alias。"
        "如果存在多个 application module，必须由用户明确指定 module。"
    )

    DEFAULT_VARIANT = "debug"
    ANDROID_NAME = "{http://schemas.android.com/apk/res/android}name"
    ANDROID_TARGET_ACTIVITY = (
        "{http://schemas.android.com/apk/res/android}targetActivity"
    )

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
                        "可选的 Android application module，例如 app 或 mobile/app。"
                        "存在多个 application module 时必须指定。"
                    ),
                },
            },
            "required": ["project_path"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_path = arguments.get("project_path")
        requested_module = arguments.get("module")

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("project_path 必须是非空字符串")
        if requested_module is not None and (
            not isinstance(requested_module, str) or not requested_module.strip()
        ):
            raise ToolError("module 必须是非空字符串")

        project = self.validate_project_path(raw_path)
        application_modules = self._find_application_modules(project)

        if not application_modules:
            return self._empty_result(
                error_type="NO_APPLICATION_MODULE",
                summary="项目中没有检测到 Android application module。",
                candidates=[],
            )

        if requested_module is None:
            if len(application_modules) > 1:
                return self._empty_result(
                    error_type="MULTIPLE_APPLICATION_MODULES",
                    summary=(
                        "检测到多个 Android application module，"
                        "请明确指定 module，Tool 不会自动猜测。"
                    ),
                    candidates=application_modules,
                )
            module = application_modules[0]
        else:
            module = self._normalize_module(requested_module)
            if module not in application_modules:
                return self._empty_result(
                    error_type="MODULE_NOT_FOUND",
                    summary=(
                        f"指定的 module '{module}' 不是已检测到的 "
                        "Android application module。"
                    ),
                    candidates=application_modules,
                )

        module_path = (project / module).resolve()
        self._ensure_within_project(project, module_path)
        build_file = self._safe_first_existing(
            project,
            module_path / "build.gradle.kts",
            module_path / "build.gradle",
        )
        build_text = self._read_text(build_file)
        application_id = self._extract_application_id(build_text)
        namespace = self._extract_namespace(build_text)

        manifest_candidate = (
            module_path / "src" / "main" / "AndroidManifest.xml"
        )
        manifest_path = self._safe_first_existing(project, manifest_candidate)
        launcher_activity: str | None = None
        launcher_component: str | None = None
        manifest_error: str | None = None

        if manifest_path is None:
            manifest_error = "MANIFEST_NOT_FOUND"
        else:
            launcher_activity, component_class = self._parse_launcher(
                manifest_path,
                namespace=namespace,
            )
            if launcher_activity is None or component_class is None:
                manifest_error = "LAUNCHER_ACTIVITY_NOT_FOUND"
            elif application_id is not None:
                launcher_component = f"{application_id}/{component_class}"

        unresolved: list[str] = []
        if application_id is None:
            unresolved.append("application_id")
        if manifest_error == "MANIFEST_NOT_FOUND":
            unresolved.append("manifest")
        elif manifest_error == "LAUNCHER_ACTIVITY_NOT_FOUND":
            unresolved.append("launcher_activity")
        elif launcher_component is None:
            unresolved.append("launcher_component")

        if application_id is None:
            error_type = "APPLICATION_ID_UNRESOLVED"
            summary = (
                "已识别 Application module，但 applicationId 无法通过静态分析确定。"
                "它可能来自变量、product flavor 或外部 Gradle 脚本；"
                "后续需要通过构建产物或 ADB 进一步确认。"
            )
        elif manifest_error == "MANIFEST_NOT_FOUND":
            error_type = manifest_error
            summary = (
                "已识别 Application module 和 applicationId，"
                "但没有找到 src/main/AndroidManifest.xml。"
            )
        elif manifest_error == "LAUNCHER_ACTIVITY_NOT_FOUND":
            error_type = manifest_error
            summary = (
                "已识别 Application module 和 applicationId，"
                "但无法从主 Manifest 可靠解析 MAIN + LAUNCHER Activity。"
                "后续可通过构建产物或 ADB 进一步确认。"
            )
        else:
            error_type = None
            summary = (
                f"已识别 debug 默认分析目标：{launcher_component}。"
                "当前 V0.2 尚未完整解析 product flavors。"
            )

        success = error_type is None
        confidence = "high" if success else "low"
        return {
            "success": success,
            "module": module,
            "module_path": str(module_path),
            "application_id": application_id,
            "namespace": namespace,
            "launcher_activity": launcher_activity,
            "launcher_component": launcher_component,
            "manifest_path": str(manifest_path) if manifest_path else None,
            "variant": self.DEFAULT_VARIANT,
            "variant_note": (
                "debug 是当前 V0.2 的默认静态分析 variant；"
                "尚未完整支持 product flavors。"
            ),
            "confidence": confidence,
            "best_effort": not success,
            "unresolved": unresolved,
            "error_type": error_type,
            "summary": summary,
        }

    def _find_application_modules(self, project: Path) -> list[str]:
        settings_file = self._safe_first_existing(
            project,
            project / "settings.gradle.kts",
            project / "settings.gradle",
        )
        settings_text = self._read_text(settings_file)
        inspector = InspectProjectTool(allowed_project_path=project)
        modules = inspector._parse_modules(settings_text)

        application_modules: list[str] = []
        for module in modules:
            module_path = (project / module).resolve()
            try:
                self._ensure_within_project(project, module_path)
            except ToolError:
                continue

            build_files = (
                self._safe_first_existing(project, path)
                for path in (
                    module_path / "build.gradle.kts",
                    module_path / "build.gradle",
                )
            )
            build_text = "\n".join(
                self._read_text(path) for path in build_files
            )
            if "com.android.application" in build_text:
                application_modules.append(module)

        return sorted(application_modules)

    @classmethod
    def _extract_application_id(cls, build_text: str) -> str | None:
        build_text = cls._strip_gradle_comments(build_text)
        default_config = cls._extract_block(build_text, "defaultConfig")
        if default_config is None:
            return None
        application_id = cls._extract_literal_assignment(
            default_config,
            "applicationId",
        )
        if application_id is None:
            return None

        product_flavors = cls._extract_block(build_text, "productFlavors")
        if product_flavors and re.search(
            r"\b(?:applicationId|applicationIdSuffix)\b",
            product_flavors,
        ):
            return None
        return application_id

    @classmethod
    def _extract_namespace(cls, build_text: str) -> str | None:
        return cls._extract_literal_assignment(
            cls._strip_gradle_comments(build_text),
            "namespace",
        )

    @staticmethod
    def _strip_gradle_comments(text: str) -> str:
        without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        return re.sub(r"(?m)//.*$", "", without_blocks)

    @staticmethod
    def _extract_literal_assignment(text: str, key: str) -> str | None:
        patterns = (
            rf"(?m)^\s*{re.escape(key)}\s*=\s*[\"']([A-Za-z0-9_.]+)[\"']",
            rf"(?m)^\s*{re.escape(key)}\s+[\"']([A-Za-z0-9_.]+)[\"']",
            rf"(?m)^\s*{re.escape(key)}\s*\(\s*[\"']([A-Za-z0-9_.]+)[\"']\s*\)",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_block(text: str, block_name: str) -> str | None:
        match = re.search(
            rf"\b{re.escape(block_name)}\s*(?:\(\s*\))?\s*\{{",
            text,
        )
        if not match:
            return None

        opening_brace = text.find("{", match.start())
        depth = 0
        for index in range(opening_brace, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    return text[opening_brace + 1:index]
        return None

    @classmethod
    def _parse_launcher(
        cls,
        manifest_path: Path,
        *,
        namespace: str | None,
    ) -> tuple[str | None, str | None]:
        try:
            root = ET.parse(manifest_path).getroot()
        except (ET.ParseError, OSError):
            return None, None

        manifest_package = root.attrib.get("package")
        class_package = namespace or manifest_package
        application = root.find("application")
        if application is None:
            return None, None

        for element_name in ("activity", "activity-alias"):
            for element in application.findall(element_name):
                if not cls._has_launcher_intent_filter(element):
                    continue

                raw_component = element.attrib.get(cls.ANDROID_NAME)
                component_class = cls._resolve_class_name(
                    raw_component,
                    class_package,
                )
                if component_class is None:
                    return None, None

                if element_name == "activity-alias":
                    raw_activity = element.attrib.get(cls.ANDROID_TARGET_ACTIVITY)
                    launcher_activity = cls._resolve_class_name(
                        raw_activity,
                        class_package,
                    )
                else:
                    launcher_activity = component_class

                if launcher_activity is None:
                    return None, None
                return launcher_activity, component_class

        return None, None

    @classmethod
    def _has_launcher_intent_filter(cls, element: ET.Element) -> bool:
        for intent_filter in element.findall("intent-filter"):
            actions = {
                action.attrib.get(cls.ANDROID_NAME)
                for action in intent_filter.findall("action")
            }
            categories = {
                category.attrib.get(cls.ANDROID_NAME)
                for category in intent_filter.findall("category")
            }
            if (
                "android.intent.action.MAIN" in actions
                and "android.intent.category.LAUNCHER" in categories
            ):
                return True
        return False

    @staticmethod
    def _resolve_class_name(
        raw_name: str | None,
        class_package: str | None,
    ) -> str | None:
        if raw_name is None or not raw_name.strip():
            return None
        name = raw_name.strip()
        if name.startswith("."):
            return f"{class_package}{name}" if class_package else None
        if "." not in name:
            return f"{class_package}.{name}" if class_package else None
        return name

    @staticmethod
    def _normalize_module(module: str) -> str:
        return module.strip().lstrip(":").replace(":", "/")

    @staticmethod
    def _ensure_within_project(project: Path, candidate: Path) -> None:
        try:
            candidate.relative_to(project)
        except ValueError as exc:
            raise ToolError("拒绝访问目标项目之外的 module") from exc

    @classmethod
    def _safe_first_existing(
        cls,
        project: Path,
        *paths: Path,
    ) -> Path | None:
        for path in paths:
            if path.is_file():
                resolved = path.resolve()
                try:
                    cls._ensure_within_project(project, resolved)
                except ToolError:
                    continue
                return resolved
        return None

    @staticmethod
    def _read_text(path: Path | None) -> str:
        if path is None or not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @classmethod
    def _empty_result(
        cls,
        *,
        error_type: str,
        summary: str,
        candidates: list[str],
    ) -> dict[str, Any]:
        return {
            "success": False,
            "module": None,
            "module_path": None,
            "application_id": None,
            "namespace": None,
            "launcher_activity": None,
            "launcher_component": None,
            "manifest_path": None,
            "variant": cls.DEFAULT_VARIANT,
            "variant_note": (
                "debug 是当前 V0.2 的默认静态分析 variant；"
                "尚未完整支持 product flavors。"
            ),
            "confidence": "none",
            "best_effort": False,
            "unresolved": [],
            "candidates": candidates,
            "error_type": error_type,
            "summary": summary,
        }
