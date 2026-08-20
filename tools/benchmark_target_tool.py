from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from tools.adb_tool import AdbInstallTool, AdbLaunchAppTool
from tools.app_target_tool import InspectAppTargetTool
from tools.base import BaseTool, ToolError
from tools.benchmark_readiness_tool import InspectBenchmarkReadinessTool
from tools.build_variant_tool import parse_concrete_assemble_tasks
from tools.gradle_tool import GradleBuildTool

ProgressSink = Callable[[dict[str, Any]], None]


class PrepareBenchmarkTargetTool(BaseTool):
    name = "prepare_benchmark_target"
    description = (
        "为普通 Android 项目的 Standalone Macrobenchmark 准备真实目标 APK。"
        "它从指定 application module 的 Gradle 真实 assemble tasks 枚举 Variant，"
        "按 benchmark/release、non-debuggable、debug 的稳定顺序逐个 Build、Install、"
        "Launch 和检查 Benchmark readiness；只有全部候选失败才返回阻塞。"
        "不会根据设备品牌选择 product flavor，也不会修改 Manifest、Gradle、"
        "签名配置或业务源码。"
    )

    ENUMERATION_TIMEOUT_SECONDS = 600
    APK_INSPECTION_TIMEOUT_SECONDS = 30
    MODULE_PATTERN = re.compile(
        r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*"
    )
    def __init__(
        self,
        allowed_project_path: Path,
        *,
        gradle_tool: GradleBuildTool | None = None,
        install_tool: AdbInstallTool | None = None,
        launch_tool: AdbLaunchAppTool | None = None,
        readiness_tool: InspectBenchmarkReadinessTool | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> None:
        super().__init__(allowed_project_path=allowed_project_path)
        self.gradle_tool = gradle_tool or GradleBuildTool(allowed_project_path)
        self.install_tool = install_tool or AdbInstallTool(allowed_project_path)
        self.launch_tool = launch_tool or AdbLaunchAppTool(allowed_project_path)
        self.readiness_tool = readiness_tool or InspectBenchmarkReadinessTool(
            allowed_project_path
        )
        self.progress_sink = progress_sink

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
                    "description": "inspect_app_target 确认的 application module。",
                },
                "serial": {
                    "type": "string",
                    "description": "adb_devices 返回的显式目标设备 serial。",
                },
                "application_id": {
                    "type": "string",
                    "description": (
                        "可选。inspect_app_target 静态解析到的 applicationId；"
                        "构建后优先使用 APK Manifest 中的真实 package。"
                    ),
                },
                "launcher_component": {
                    "type": "string",
                    "description": (
                        "可选。inspect_app_target 静态解析到的 Launcher Component；"
                        "构建后优先使用 APK Manifest 中的真实 launcher。"
                    ),
                },
            },
            "required": [
                "project_path",
                "module",
                "serial",
            ],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project = self._validate_arguments(arguments)
        module = InspectAppTargetTool._normalize_module(arguments["module"])
        serial = arguments["serial"].strip()
        raw_application_id = arguments.get("application_id")
        raw_component = arguments.get("launcher_component")
        fallback_application_id = (
            raw_application_id.strip()
            if isinstance(raw_application_id, str)
            else None
        )
        fallback_component = (
            raw_component.strip() if isinstance(raw_component, str) else None
        )

        app_modules = InspectAppTargetTool(
            allowed_project_path=project
        )._find_application_modules(project)
        if module not in app_modules:
            return self._empty_result(
                module=module,
                error_type="MODULE_NOT_FOUND",
                summary=(
                    f"指定 module '{module}' 不是当前项目检测到的 application module。"
                ),
            )

        enumeration = self._enumerate_variants(project, module)
        if enumeration["error_type"] is not None:
            return self._empty_result(
                module=module,
                error_type=enumeration["error_type"],
                summary=enumeration["summary"],
                important_logs=enumeration["important_logs"],
            )

        variants = enumeration["variants"]
        if not variants:
            return self._empty_result(
                module=module,
                error_type="NO_BUILD_VARIANTS",
                summary=(
                    f"Gradle 没有为 application module '{module}' 暴露可用的 "
                    "assemble Variant task。"
                ),
            )

        candidate_results: list[dict[str, Any]] = []
        all_blocking_reasons: list[str] = []
        candidate_total = len(variants)
        for candidate_index, candidate in enumerate(variants, start=1):
            self._emit_progress(
                candidate_index,
                candidate_total,
                str(candidate["variant"]),
                "BUILDING",
            )
            result = self._evaluate_candidate(
                project=project,
                module=module,
                serial=serial,
                candidate=candidate,
                fallback_application_id=fallback_application_id,
                fallback_component=fallback_component,
                candidate_index=candidate_index,
                candidate_total=candidate_total,
            )
            self._emit_progress(
                candidate_index,
                candidate_total,
                str(candidate["variant"]),
                str(result["status"]),
                error_type=result.get("rejection_reason"),
            )
            candidate_results.append(result)
            for reason in result["blocking_reasons"]:
                if reason not in all_blocking_reasons:
                    all_blocking_reasons.append(reason)
            if result["benchmark_ready"]:
                selection_reason = (
                    "按 benchmark/release、non-debuggable、debug 的固定优先级排序后，"
                    f"选择首个 Build、Install、Launch 与 readiness 均通过的 "
                    f"Variant：{result['variant']}。选择与设备品牌无关。"
                )
                return {
                    "success": True,
                    "module": module,
                    "selected_variant": result["variant"],
                    "selected_apk": result["apk_path"],
                    "application_id": result["application_id"],
                    "launcher_component": result["launcher_component"],
                    "candidates_discovered": len(variants),
                    "candidates_checked": len(candidate_results),
                    "variant_candidates": variants,
                    "candidate_results": candidate_results,
                    "benchmark_ready": True,
                    "blocking_reasons": [],
                    "selection_reason": selection_reason,
                    "error_type": None,
                    "summary": (
                        f"已从 {len(variants)} 个真实 Gradle Variant 中找到 "
                        f"Benchmark-ready Target：{result['variant']}。"
                    ),
                }

        return {
            "success": False,
            "module": module,
            "selected_variant": None,
            "selected_apk": None,
            "application_id": fallback_application_id,
            "launcher_component": fallback_component,
            "candidates_discovered": len(variants),
            "candidates_checked": len(candidate_results),
            "variant_candidates": variants,
            "candidate_results": candidate_results,
            "benchmark_ready": False,
            "blocking_reasons": all_blocking_reasons,
            "selection_reason": None,
            "error_type": "NO_BENCHMARK_READY_TARGET",
            "summary": (
                f"已枚举并尝试 {len(candidate_results)} 个 Variant，"
                "没有找到可构建、可安装且满足 Benchmark readiness 的 Target。"
            ),
        }

    def _validate_arguments(self, arguments: dict[str, Any]) -> Path:
        raw_project = arguments.get("project_path")
        module = arguments.get("module")
        serial = arguments.get("serial")
        application_id = arguments.get("application_id")
        launcher_component = arguments.get("launcher_component")
        if not isinstance(raw_project, str) or not raw_project.strip():
            raise ToolError("project_path 必须是非空字符串")
        if not isinstance(module, str) or not self.MODULE_PATTERN.fullmatch(
            InspectAppTargetTool._normalize_module(module)
        ):
            raise ToolError("module 格式不合法")
        if not isinstance(serial, str) or not InspectBenchmarkReadinessTool.SERIAL_PATTERN.fullmatch(
            serial.strip()
        ):
            raise ToolError("serial 格式不合法")
        if application_id is not None and (
            not isinstance(application_id, str)
            or not InspectBenchmarkReadinessTool.PACKAGE_PATTERN.fullmatch(
                application_id.strip()
            )
        ):
            raise ToolError("application_id 格式不合法")
        if launcher_component is not None and not isinstance(
            launcher_component, str
        ):
            raise ToolError("launcher_component 必须是字符串")
        component_package = None
        if isinstance(launcher_component, str):
            component_package = AdbLaunchAppTool._validate_component(
                launcher_component.strip()
            )
        if (
            component_package is not None
            and isinstance(application_id, str)
            and component_package != application_id.strip()
        ):
            raise ToolError(
                "launcher_component 的 package 与 application_id 不一致"
            )
        project = self.validate_project_path(raw_project)
        if not project.is_dir():
            raise ToolError("项目路径不存在或不是目录")
        return project

    def _enumerate_variants(
        self,
        project: Path,
        module: str,
    ) -> dict[str, Any]:
        gradlew = project / "gradlew"
        if not gradlew.is_file():
            return {
                "variants": [],
                "error_type": "GRADLEW_NOT_FOUND",
                "summary": "项目中没有找到 gradlew。",
                "important_logs": [],
            }
        task = f":{module.replace('/', ':')}:tasks"
        if gradlew.stat().st_mode & 0o111:
            command = [str(gradlew), task, "--all", "--console=plain"]
        else:
            command = [
                "bash",
                str(gradlew),
                task,
                "--all",
                "--console=plain",
            ]
        try:
            completed = subprocess.run(
                command,
                cwd=project,
                capture_output=True,
                text=True,
                timeout=self.ENUMERATION_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raw = GradleBuildTool._combine_output(
                GradleBuildTool._safe_decode(exc.stdout),
                GradleBuildTool._safe_decode(exc.stderr),
            )
            return {
                "variants": [],
                "error_type": "VARIANT_ENUMERATION_TIMEOUT",
                "summary": (
                    f"Gradle Variant 枚举超过 {self.ENUMERATION_TIMEOUT_SECONDS} 秒，"
                    "结果未知。"
                ),
                "important_logs": GradleBuildTool._important_lines(raw),
            }
        except OSError as exc:
            return {
                "variants": [],
                "error_type": "VARIANT_ENUMERATION_FAILED",
                "summary": f"无法执行 Gradle Variant 枚举：{type(exc).__name__}。",
                "important_logs": [],
            }
        raw = GradleBuildTool._combine_output(completed.stdout, completed.stderr)
        if completed.returncode != 0:
            return {
                "variants": [],
                "error_type": "VARIANT_ENUMERATION_FAILED",
                "summary": "Gradle 无法枚举 application module 的 Variant tasks。",
                "important_logs": GradleBuildTool._important_lines(raw),
            }

        task_names = parse_concrete_assemble_tasks(raw)
        build_text = self._module_build_text(project, module)
        variants = []
        module_task_prefix = f":{module.replace('/', ':')}:"
        for task_name in sorted(task_names):
            metadata = self._variant_metadata(task_name, build_text)
            metadata["assemble_task"] = module_task_prefix + task_name
            variants.append(metadata)
        variants.sort(key=self._variant_sort_key)
        for index, variant in enumerate(variants, start=1):
            variant["priority"] = index
        return {
            "variants": variants,
            "error_type": None,
            "summary": f"发现 {len(variants)} 个真实 Gradle assemble Variant。",
            "important_logs": [],
        }

    def _evaluate_candidate(
        self,
        *,
        project: Path,
        module: str,
        serial: str,
        candidate: dict[str, Any],
        fallback_application_id: str | None,
        fallback_component: str | None,
        candidate_index: int,
        candidate_total: int,
    ) -> dict[str, Any]:
        result = {
            **candidate,
            "status": "PENDING",
            "build_success": False,
            "apk_path": None,
            "apk_attempts": [],
            "install_success": False,
            "launch_success": False,
            "application_id": fallback_application_id,
            "launcher_component": fallback_component,
            "profileable": None,
            "profileable_shell": None,
            "profileinstaller_available": None,
            "benchmark_ready": False,
            "blocking_reasons": [],
            "rejection_reason": None,
        }
        build = self.gradle_tool.execute(
            {
                "project_path": str(project),
                "task": candidate["assemble_task"],
            }
        )
        if not build.get("success"):
            reason = build.get("error_type") or "BUILD_FAILED"
            result.update(
                {
                    "status": "BUILD_TIMEOUT" if reason == "BUILD_TIMEOUT" else "BUILD_FAILED",
                    "blocking_reasons": [reason],
                    "rejection_reason": reason,
                }
            )
            return result
        result["build_success"] = True
        self._emit_progress(
            candidate_index,
            candidate_total,
            str(candidate["variant"]),
            "INSPECTING_APK",
        )
        apk_outputs = [
            Path(path).resolve()
            for path in build.get("apk_outputs", [])
            if isinstance(path, str)
        ]
        if not apk_outputs:
            result.update(
                {
                    "status": "APK_OUTPUT_NOT_FOUND",
                    "blocking_reasons": ["APK_OUTPUT_NOT_FOUND"],
                    "rejection_reason": "APK_OUTPUT_NOT_FOUND",
                }
            )
            return result

        for apk_path in self._sort_apk_outputs(apk_outputs):
            attempt: dict[str, Any] = {"apk_path": str(apk_path)}
            result["apk_path"] = str(apk_path)
            signing = self._apk_signing_available(apk_path)
            if signing is not None:
                result["signing_available"] = signing
            if signing is False or "unsigned" in apk_path.name.lower():
                result["signing_available"] = False
                attempt["rejection_reason"] = "UNSIGNED_TARGET"
                result["apk_attempts"].append(attempt)
                continue

            identity = self._apk_identity(apk_path)
            application_id = identity.get("application_id") or fallback_application_id
            launcher_component = identity.get("launcher_component")
            if launcher_component is None and fallback_component is not None:
                component_class = fallback_component.split("/", 1)[1]
                if application_id is not None:
                    launcher_component = f"{application_id}/{component_class}"
            if application_id is None or launcher_component is None:
                attempt["rejection_reason"] = "APK_IDENTITY_UNRESOLVED"
                result["apk_attempts"].append(attempt)
                continue
            attempt["application_id"] = application_id
            attempt["launcher_component"] = launcher_component

            self._emit_progress(
                candidate_index,
                candidate_total,
                str(candidate["variant"]),
                "INSTALLING",
            )
            install = self.install_tool.execute(
                {
                    "project_path": str(project),
                    "serial": serial,
                    "apk_path": str(apk_path),
                }
            )
            attempt["install_success"] = install.get("success") is True
            if not install.get("success"):
                reason = self._install_rejection_reason(install)
                attempt["rejection_reason"] = reason
                result["apk_attempts"].append(attempt)
                continue

            result.update(
                {
                    "apk_path": str(apk_path),
                    "install_success": True,
                    "signing_available": True,
                    "application_id": application_id,
                    "launcher_component": launcher_component,
                }
            )
            self._emit_progress(
                candidate_index,
                candidate_total,
                str(candidate["variant"]),
                "LAUNCHING",
            )
            launch = self.launch_tool.execute(
                {
                    "serial": serial,
                    "application_id": application_id,
                    "launcher_component": launcher_component,
                }
            )
            attempt["launch_success"] = launch.get("success") is True
            if not launch.get("success"):
                reason = launch.get("error_type") or "LAUNCH_FAILED"
                attempt["rejection_reason"] = reason
                result["apk_attempts"].append(attempt)
                continue
            result["launch_success"] = True

            self._emit_progress(
                candidate_index,
                candidate_total,
                str(candidate["variant"]),
                "CHECKING_READINESS",
            )
            readiness = self.readiness_tool.execute(
                {"serial": serial, "package_name": application_id}
            )
            result.update(
                {
                    "debuggable": readiness.get("debuggable"),
                    "profileable": readiness.get("profileable"),
                    "profileable_shell": readiness.get("profileable_shell"),
                    "profileinstaller_available": readiness.get(
                        "profileinstaller_available"
                    ),
                    "benchmark_ready": readiness.get("benchmark_ready") is True,
                    "blocking_reasons": list(
                        readiness.get("blocking_reasons") or []
                    ),
                }
            )
            if result["benchmark_ready"]:
                result.update(
                    {
                        "status": "BENCHMARK_READY",
                        "rejection_reason": None,
                    }
                )
                attempt["benchmark_ready"] = True
                result["apk_attempts"].append(attempt)
                return result
            reason = (
                result["blocking_reasons"][0]
                if result["blocking_reasons"]
                else readiness.get("error_type") or "READINESS_FAILED"
            )
            result.update(
                {
                    "status": "READINESS_FAILED",
                    "blocking_reasons": result["blocking_reasons"] or [reason],
                    "rejection_reason": reason,
                }
            )
            attempt["rejection_reason"] = reason
            result["apk_attempts"].append(attempt)
            return result

        reasons = [
            attempt.get("rejection_reason")
            for attempt in result["apk_attempts"]
            if attempt.get("rejection_reason")
        ]
        unique_reasons = list(dict.fromkeys(reasons)) or ["APK_NOT_INSTALLABLE"]
        result.update(
            {
                "status": unique_reasons[0],
                "blocking_reasons": unique_reasons,
                "rejection_reason": unique_reasons[0],
            }
        )
        return result

    def _emit_progress(
        self,
        candidate_index: int,
        candidate_total: int,
        variant: str,
        status: str,
        *,
        error_type: Any = None,
    ) -> None:
        if self.progress_sink is None:
            return
        try:
            self.progress_sink(
                {
                    "type": "tool_progress",
                    "name": self.name,
                    "candidate_index": candidate_index,
                    "candidate_total": candidate_total,
                    "variant": variant,
                    "status": status,
                    "error_type": error_type,
                }
            )
        except Exception:
            # UI progress must never interrupt deterministic target preparation.
            return

    @classmethod
    def _variant_metadata(
        cls,
        task_name: str,
        build_text: str,
    ) -> dict[str, Any]:
        variant_name = task_name.removeprefix("assemble")
        build_types = cls._build_type_metadata(build_text)
        matched_build_type = next(
            (
                name
                for name in sorted(build_types, key=len, reverse=True)
                if variant_name.lower().endswith(name.lower())
            ),
            None,
        )
        if matched_build_type is None and variant_name.lower() == "benchmark":
            matched_build_type = "benchmark"
        metadata = build_types.get(matched_build_type or "", {})
        flavor_part = (
            variant_name[: -len(matched_build_type)]
            if matched_build_type and len(variant_name) > len(matched_build_type)
            else ""
        )
        flavor = (
            flavor_part[:1].lower() + flavor_part[1:]
            if flavor_part
            else None
        )
        return {
            "variant_name": variant_name,
            "variant": variant_name,
            "build_type": matched_build_type,
            "flavor": flavor,
            "assemble_task": task_name,
            "debuggable": metadata.get("debuggable"),
            "minify_enabled": metadata.get("minify_enabled"),
            "signing_available": metadata.get("signing_available"),
            "apk_output_expected": True,
        }

    @classmethod
    def _build_type_metadata(cls, build_text: str) -> dict[str, dict[str, Any]]:
        metadata: dict[str, dict[str, Any]] = {
            "debug": {
                "debuggable": True,
                "minify_enabled": False,
                "signing_available": True,
            },
            "release": {
                "debuggable": False,
                "minify_enabled": None,
                "signing_available": False,
            },
        }
        stripped = InspectAppTargetTool._strip_gradle_comments(build_text)
        build_types = InspectAppTargetTool._extract_block(stripped, "buildTypes")
        if not build_types:
            return metadata
        for name, body in cls._named_blocks(build_types).items():
            inherited = re.search(
                r"\binitWith\b[^\n]*(debug|release)",
                body,
                re.IGNORECASE,
            )
            base = dict(metadata.get(inherited.group(1).lower(), {})) if inherited else {}
            default = metadata.get(name, {})
            current = {**default, **base}
            debuggable = cls._boolean_property(body, "debuggable")
            minify = cls._boolean_property(body, "minifyEnabled")
            if debuggable is not None:
                current["debuggable"] = debuggable
            if minify is not None:
                current["minify_enabled"] = minify
            if re.search(r"\bsigningConfig\b", body):
                current["signing_available"] = not bool(
                    re.search(r"\bsigningConfig\s*=\s*null\b", body)
                )
            current.setdefault("debuggable", None)
            current.setdefault("minify_enabled", None)
            current.setdefault("signing_available", None)
            metadata[name] = current
        return metadata

    @staticmethod
    def _named_blocks(text: str) -> dict[str, str]:
        pattern = re.compile(
            r"(?:(?:create|getByName|maybeCreate|named)\s*\(\s*[\"']"
            r"(?P<called>[A-Za-z0-9_.-]+)[\"']\s*\)|"
            r"(?P<bare>[A-Za-z][A-Za-z0-9_.-]*))\s*\{"
        )
        blocks: dict[str, str] = {}
        cursor = 0
        while True:
            match = pattern.search(text, cursor)
            if match is None:
                break
            opening = text.find("{", match.start())
            depth = 0
            closing = None
            for index in range(opening, len(text)):
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                    if depth == 0:
                        closing = index
                        break
            if closing is None:
                break
            name = (match.group("called") or match.group("bare")).lower()
            if name not in {"all", "configureeach"}:
                blocks[name] = text[opening + 1 : closing]
            cursor = closing + 1
        return blocks

    @staticmethod
    def _boolean_property(text: str, property_name: str) -> bool | None:
        match = re.search(
            rf"\b(?:is)?{re.escape(property_name)}\s*(?:=|\s)\s*(true|false)\b",
            text,
            re.IGNORECASE,
        )
        if match is None:
            return None
        return match.group(1).lower() == "true"

    @staticmethod
    def _variant_sort_key(candidate: dict[str, Any]) -> tuple[int, str]:
        variant = str(candidate["variant_name"])
        build_type = str(candidate.get("build_type") or "")
        if "benchmark" in variant.lower() or "benchmark" in build_type.lower():
            group = 0
        elif build_type.lower() == "release" or variant.lower().endswith("release"):
            group = 1
        elif candidate.get("debuggable") is False:
            group = 2
        elif candidate.get("debuggable") is True:
            group = 4
        else:
            group = 3
        return group, variant.casefold()

    @staticmethod
    def _sort_apk_outputs(paths: list[Path]) -> list[Path]:
        return sorted(
            paths,
            key=lambda path: (
                "unsigned" in path.name.lower(),
                "universal" not in path.name.lower(),
                path.name.casefold(),
            ),
        )

    def _apk_signing_available(self, apk_path: Path) -> bool | None:
        if "unsigned" in apk_path.name.lower():
            return False
        apksigner = self._find_android_tool("apksigner")
        if apksigner is None:
            return None
        try:
            completed = subprocess.run(
                [str(apksigner), "verify", str(apk_path)],
                capture_output=True,
                text=True,
                timeout=self.APK_INSPECTION_TIMEOUT_SECONDS,
                shell=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        return completed.returncode == 0

    def _apk_identity(self, apk_path: Path) -> dict[str, str | None]:
        aapt2 = self.readiness_tool._find_aapt2()
        if aapt2 is None:
            return {"application_id": None, "launcher_component": None}
        try:
            completed = subprocess.run(
                [str(aapt2), "dump", "badging", str(apk_path)],
                capture_output=True,
                text=True,
                timeout=self.APK_INSPECTION_TIMEOUT_SECONDS,
                shell=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return {"application_id": None, "launcher_component": None}
        if completed.returncode != 0:
            return {"application_id": None, "launcher_component": None}
        raw = completed.stdout or ""
        package = re.search(r"(?m)^package:\s+name='([^']+)'", raw)
        launcher = re.search(
            r"(?m)^launchable-activity:\s+name='([^']+)'",
            raw,
        )
        application_id = package.group(1) if package else None
        launcher_component = (
            f"{application_id}/{launcher.group(1)}"
            if application_id and launcher
            else None
        )
        return {
            "application_id": application_id,
            "launcher_component": launcher_component,
        }

    @staticmethod
    def _install_rejection_reason(install: dict[str, Any]) -> str:
        error_type = str(install.get("error_type") or "ADB_INSTALL_FAILED")
        logs = "\n".join(str(line) for line in install.get("important_logs", []))
        if (
            "NO_CERTIFICATES" in logs
            or "not signed" in logs.lower()
            or "unsigned" in logs.lower()
        ):
            return "UNSIGNED_TARGET"
        return error_type

    @classmethod
    def _find_android_tool(cls, name: str) -> Path | None:
        direct = shutil.which(name)
        if direct:
            return Path(direct)
        sdk = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
        if not sdk:
            return None
        candidates = [
            path
            for path in (Path(sdk) / "build-tools").glob(f"*/{name}")
            if path.is_file()
        ]
        return (
            max(
                candidates,
                key=lambda path: InspectBenchmarkReadinessTool._version_key(
                    path.parent.name
                ),
            )
            if candidates
            else None
        )

    @staticmethod
    def _module_build_text(project: Path, module: str) -> str:
        module_path = project / module
        texts: list[str] = []
        for filename in ("build.gradle.kts", "build.gradle"):
            path = module_path / filename
            if path.is_file():
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(texts)

    @staticmethod
    def _empty_result(
        *,
        module: str | None,
        error_type: str,
        summary: str,
        important_logs: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "module": module,
            "selected_variant": None,
            "selected_apk": None,
            "application_id": None,
            "launcher_component": None,
            "candidates_discovered": 0,
            "candidates_checked": 0,
            "variant_candidates": [],
            "candidate_results": [],
            "benchmark_ready": False,
            "blocking_reasons": [error_type],
            "selection_reason": None,
            "error_type": error_type,
            "summary": summary,
            "important_logs": important_logs or [],
        }
