from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError
from tools.project_tool import InspectProjectTool


class SetupMacrobenchmarkTool(BaseTool):
    name = "setup_macrobenchmark"
    description = (
        "为尚未配置 Macrobenchmark 的普通 Android application 项目创建标准启动测试环境。"
        "该 Tool 会安全修改 settings、目标 Application build 文件和 Manifest，并创建"
        "独立 com.android.test module；不执行 Benchmark、不分析 Perfetto、不修改业务源码。"
    )

    DEFAULT_BENCHMARK_MODULE = "benchmark"
    BENCHMARK_NAMESPACE = "com.androidperformance.benchmark"
    TEST_CLASS = "com.androidperformance.benchmark.StartupBenchmark"
    TEST_METHOD = "startup"
    ITERATIONS = 5
    APPLICATION_ID_PATTERN = re.compile(
        r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
    )
    MODULE_PATTERN = re.compile(
        r":?[A-Za-z0-9_.-]+(?:(?::|/)[A-Za-z0-9_.-]+)*"
    )
    ANDROID_SHELL = "{http://schemas.android.com/apk/res/android}shell"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "用户指定的 Android 项目绝对路径。",
                },
                "application_module": {
                    "type": ["string", "null"],
                    "description": (
                        "目标 Android application module；只有一个时可传 null，"
                        "存在多个时必须明确指定。"
                    ),
                },
                "application_id": {
                    "type": ["string", "null"],
                    "description": "inspect_app_target 确认的真实 applicationId。",
                },
                "benchmark_module": {
                    "type": ["string", "null"],
                    "description": "要创建的 module，传 null 时默认为 benchmark。",
                },
            },
            "required": [
                "project_path",
                "application_module",
                "application_id",
                "benchmark_module",
            ],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_project = arguments.get("project_path")
        requested_app_module = arguments.get("application_module")
        application_id = arguments.get("application_id")
        raw_benchmark_module = arguments.get("benchmark_module")

        if not isinstance(raw_project, str) or not raw_project.strip():
            raise ToolError("project_path 必须是非空字符串")
        if requested_app_module is not None and (
            not isinstance(requested_app_module, str)
            or not self.MODULE_PATTERN.fullmatch(requested_app_module)
        ):
            raise ToolError("application_module 格式不合法")
        if raw_benchmark_module is not None and (
            not isinstance(raw_benchmark_module, str)
            or not self.MODULE_PATTERN.fullmatch(raw_benchmark_module)
        ):
            raise ToolError("benchmark_module 格式不合法")
        if application_id is not None and (
            not isinstance(application_id, str)
            or not self.APPLICATION_ID_PATTERN.fullmatch(application_id)
        ):
            raise ToolError("application_id 格式不合法")

        project = self.validate_project_path(raw_project)
        benchmark_module = self._normalize_module(
            raw_benchmark_module or self.DEFAULT_BENCHMARK_MODULE
        )
        self._reject_traversal(benchmark_module, "benchmark_module")
        if requested_app_module is not None:
            requested_app_module = self._normalize_module(requested_app_module)
            self._reject_traversal(requested_app_module, "application_module")

        gradlew = project / "gradlew"
        if not gradlew.is_file():
            return self._error_result(
                application_module=requested_app_module,
                application_id=application_id,
                benchmark_module=benchmark_module,
                error_type="GRADLEW_NOT_FOUND",
                summary="项目中没有找到 Gradle Wrapper: gradlew。",
            )

        settings_file = self._first_safe_file(
            project,
            project / "settings.gradle.kts",
            project / "settings.gradle",
        )
        if settings_file is None:
            return self._error_result(
                application_module=requested_app_module,
                application_id=application_id,
                benchmark_module=benchmark_module,
                error_type="SETTINGS_MODIFICATION_UNSAFE",
                summary="未找到可安全修改的 settings.gradle/settings.gradle.kts。",
            )

        settings_text = self._read_text(settings_file)
        inspector = InspectProjectTool(allowed_project_path=project)
        modules = inspector._parse_modules(settings_text)
        module_build_files = self._module_build_files(project, modules)
        existing_benchmarks = sorted(
            module
            for module, build_file in module_build_files.items()
            if self._is_macrobenchmark_module(module, self._read_text(build_file))
        )
        if existing_benchmarks:
            existing = (
                benchmark_module
                if benchmark_module in existing_benchmarks
                else existing_benchmarks[0]
            )
            return self._success_result(
                application_module=requested_app_module,
                application_id=application_id,
                benchmark_module=existing,
                already_configured=True,
                changed=False,
                created_files=[],
                modified_files=[],
                profileable_ready=True,
                benchmark_build_type_ready=True,
                warnings=(
                    [
                        "项目存在多个 Macrobenchmark module，请在运行时明确选择。"
                    ]
                    if len(existing_benchmarks) > 1
                    else []
                ),
                existing_benchmark_modules=existing_benchmarks,
            )

        application_modules = sorted(
            module
            for module, build_file in module_build_files.items()
            if "com.android.application" in self._read_text(build_file)
        )
        if requested_app_module is None:
            if len(application_modules) > 1:
                return self._error_result(
                    application_module=None,
                    application_id=application_id,
                    benchmark_module=benchmark_module,
                    error_type="MULTIPLE_APPLICATION_MODULES",
                    summary=(
                        "项目存在多个 Android application module，"
                        "必须先明确指定测试目标。"
                    ),
                    candidates=application_modules,
                )
            if not application_modules:
                return self._error_result(
                    application_module=None,
                    application_id=application_id,
                    benchmark_module=benchmark_module,
                    error_type="APPLICATION_MODULE_NOT_FOUND",
                    summary="项目中没有检测到 Android application module。",
                )
            application_module = application_modules[0]
        else:
            application_module = requested_app_module
            if application_module not in application_modules:
                return self._error_result(
                    application_module=application_module,
                    application_id=application_id,
                    benchmark_module=benchmark_module,
                    error_type="APPLICATION_MODULE_NOT_FOUND",
                    summary="指定 module 不是已检测到的 Android application module。",
                    candidates=application_modules,
                )

        if application_id is None:
            return self._error_result(
                application_module=application_module,
                application_id=None,
                benchmark_module=benchmark_module,
                error_type="APPLICATION_ID_UNRESOLVED",
                summary=(
                    "applicationId 尚未明确；必须先通过 inspect_app_target 确认，"
                    "不能使用 namespace 猜测。"
                ),
            )

        benchmark_path = (project / benchmark_module).resolve()
        self._ensure_within_project(project, benchmark_path)
        if benchmark_module in modules or benchmark_path.exists():
            return self._error_result(
                application_module=application_module,
                application_id=application_id,
                benchmark_module=benchmark_module,
                error_type="BENCHMARK_MODULE_CONFLICT",
                summary="目标 Benchmark module 名称或目录已被其他配置占用。",
            )

        app_build_file = module_build_files[application_module]
        app_build_text = self._read_text(app_build_file)
        compile_sdk = self._extract_compile_sdk(app_build_text)
        if compile_sdk is None:
            return self._error_result(
                application_module=application_module,
                application_id=application_id,
                benchmark_module=benchmark_module,
                error_type="BUILD_FILE_MODIFICATION_UNSAFE",
                summary="无法从目标 Application build 文件安全确定 compileSdk。",
            )

        generated_kts = settings_file.suffix == ".kts"
        app_kts = app_build_file.suffix == ".kts"
        if not compile_sdk.isdigit() and generated_kts != app_kts:
            return self._error_result(
                application_module=application_module,
                application_id=application_id,
                benchmark_module=benchmark_module,
                error_type="BUILD_FILE_MODIFICATION_UNSAFE",
                summary="动态 compileSdk 跨 Gradle DSL，无法安全生成 Benchmark 配置。",
            )

        app_build_update = self._add_benchmark_build_type(
            app_build_text,
            kotlin_dsl=app_kts,
        )
        if app_build_update["error_type"]:
            return self._error_result(
                application_module=application_module,
                application_id=application_id,
                benchmark_module=benchmark_module,
                error_type=app_build_update["error_type"],
                summary=app_build_update["summary"],
            )

        manifest_file = self._first_safe_file(
            project,
            project
            / application_module
            / "src"
            / "main"
            / "AndroidManifest.xml",
        )
        if manifest_file is None:
            return self._error_result(
                application_module=application_module,
                application_id=application_id,
                benchmark_module=benchmark_module,
                error_type="MANIFEST_MODIFICATION_UNSAFE",
                summary="没有找到可安全修改的 src/main/AndroidManifest.xml。",
            )
        manifest_update = self._add_profileable(self._read_text(manifest_file))
        if manifest_update["error_type"]:
            return self._error_result(
                application_module=application_module,
                application_id=application_id,
                benchmark_module=benchmark_module,
                error_type=manifest_update["error_type"],
                summary=manifest_update["summary"],
            )

        settings_updated = self._add_settings_include(
            settings_text,
            benchmark_module,
            kotlin_dsl=generated_kts,
        )
        benchmark_build_file = benchmark_path / (
            "build.gradle.kts" if generated_kts else "build.gradle"
        )
        test_file = (
            benchmark_path
            / "src"
            / "main"
            / "java"
            / "com"
            / "androidperformance"
            / "benchmark"
            / "StartupBenchmark.java"
        )
        benchmark_build_text = self._benchmark_build_template(
            application_module=application_module,
            compile_sdk=compile_sdk,
            kotlin_dsl=generated_kts,
        )
        test_text = self._startup_benchmark_template(application_id)

        snapshots = {
            settings_file: settings_text,
            app_build_file: app_build_text,
            manifest_file: self._read_text(manifest_file),
        }
        created_files = [str(benchmark_build_file), str(test_file)]
        modified_files: list[str] = []
        try:
            self._write_text(settings_file, settings_updated)
            modified_files.append(str(settings_file))
            self._write_text(app_build_file, app_build_update["text"])
            modified_files.append(str(app_build_file))
            self._write_text(manifest_file, manifest_update["text"])
            modified_files.append(str(manifest_file))
            test_file.parent.mkdir(parents=True, exist_ok=False)
            self._write_text(benchmark_build_file, benchmark_build_text)
            self._write_text(test_file, test_text)
        except Exception as exc:
            rollback_error = self._rollback(snapshots, benchmark_path)
            if rollback_error is not None:
                return self._error_result(
                    application_module=application_module,
                    application_id=application_id,
                    benchmark_module=benchmark_module,
                    error_type="ROLLBACK_FAILED",
                    summary=(
                        f"配置写入失败，且回滚失败：{type(rollback_error).__name__}。"
                    ),
                )
            return self._error_result(
                application_module=application_module,
                application_id=application_id,
                benchmark_module=benchmark_module,
                error_type="WRITE_FAILED",
                summary=f"配置写入失败，已恢复全部原文件：{type(exc).__name__}。",
            )

        return self._success_result(
            application_module=application_module,
            application_id=application_id,
            benchmark_module=benchmark_module,
            already_configured=False,
            changed=True,
            created_files=created_files,
            modified_files=modified_files,
            profileable_ready=True,
            benchmark_build_type_ready=True,
            warnings=[
                "仅支持基础 Groovy/Kotlin DSL；复杂 flavor、Convention Plugin 或自定义 build logic 需要人工确认。"
            ],
            existing_benchmark_modules=[],
        )

    def _module_build_files(
        self,
        project: Path,
        modules: list[str],
    ) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for module in modules:
            module_path = (project / module).resolve()
            try:
                self._ensure_within_project(project, module_path)
            except ToolError:
                continue
            build_file = self._first_safe_file(
                project,
                module_path / "build.gradle.kts",
                module_path / "build.gradle",
            )
            if build_file is not None:
                result[module] = build_file
        return result

    @staticmethod
    def _is_macrobenchmark_module(module: str, build_text: str) -> bool:
        return "com.android.test" in build_text and (
            "benchmark" in module.lower()
            or "benchmark-macro-junit4" in build_text
        )

    @staticmethod
    def _extract_compile_sdk(build_text: str) -> str | None:
        patterns = (
            r"(?m)^\s*compileSdk\s*=\s*([0-9]+|[A-Za-z_][A-Za-z0-9_.]*)",
            r"(?m)^\s*compileSdk(?:Version)?\s+(?:=\s*)?([0-9]+|[A-Za-z_][A-Za-z0-9_.]*)",
        )
        for pattern in patterns:
            match = re.search(pattern, build_text)
            if match:
                return match.group(1)
        return None

    @classmethod
    def _add_benchmark_build_type(
        cls,
        text: str,
        *,
        kotlin_dsl: bool,
    ) -> dict[str, Any]:
        android_span = cls._find_block(text, r"(?m)^\s*android\s*(?:\(\s*\))?\s*{")
        if android_span is None:
            return {
                "text": text,
                "error_type": "BUILD_FILE_MODIFICATION_UNSAFE",
                "summary": "目标 build 文件中没有可安全识别的 android block。",
            }
        android_open, android_close = android_span
        android_body = text[android_open + 1:android_close]
        build_types_relative = cls._find_block(
            android_body,
            r"(?m)^\s*buildTypes\s*(?:\(\s*\))?\s*{",
        )

        if build_types_relative is not None:
            relative_open, relative_close = build_types_relative
            build_types_body = android_body[relative_open + 1:relative_close]
            benchmark_span = cls._find_block(
                build_types_body,
                (
                    r"(?m)^\s*(?:benchmark|create\(\s*[\"']benchmark[\"']\s*\)|"
                    r"getByName\(\s*[\"']benchmark[\"']\s*\))\s*\{"
                ),
            )
            if benchmark_span is not None:
                benchmark_body = build_types_body[
                    benchmark_span[0] + 1:benchmark_span[1]
                ]
                debuggable_false = bool(
                    re.search(
                        r"\b(?:isDebuggable\s*=|debuggable\s*(?:=\s*)?)\s*false\b",
                        benchmark_body,
                    )
                )
                debuggable_true = bool(
                    re.search(
                        r"\b(?:isDebuggable\s*=|debuggable\s*(?:=\s*)?)\s*true\b",
                        benchmark_body,
                    )
                )
                release_fallback = bool(
                    re.search(r"matchingFallbacks[^\n}]*release", benchmark_body)
                )
                if debuggable_false and release_fallback:
                    return {"text": text, "error_type": None, "summary": ""}
                conflict = (
                    "已有 benchmark build type 设置 debuggable=true。"
                    if debuggable_true
                    else "已有 benchmark build type 不满足非调试 release fallback 要求。"
                )
                return {
                    "text": text,
                    "error_type": "BENCHMARK_BUILD_TYPE_CONFLICT",
                    "summary": conflict,
                }

            absolute_close = android_open + 1 + relative_close
            block = cls._build_type_block(kotlin_dsl, indent="        ")
            updated = text[:absolute_close] + "\n" + block + text[absolute_close:]
            return {"text": updated, "error_type": None, "summary": ""}

        build_types = cls._build_types_block(kotlin_dsl, indent="    ")
        updated = text[:android_close] + "\n" + build_types + text[android_close:]
        return {"text": updated, "error_type": None, "summary": ""}

    @staticmethod
    def _build_type_block(kotlin_dsl: bool, indent: str) -> str:
        if kotlin_dsl:
            lines = (
                'create("benchmark") {',
                '    initWith(getByName("release"))',
                '    signingConfig = signingConfigs.getByName("debug")',
                '    matchingFallbacks += listOf("release")',
                "    isDebuggable = false",
                "}",
            )
        else:
            lines = (
                "benchmark {",
                "    initWith release",
                "    signingConfig signingConfigs.debug",
                "    matchingFallbacks = ['release']",
                "    debuggable false",
                "}",
            )
        return "\n".join(indent + line for line in lines) + "\n"

    @classmethod
    def _build_types_block(cls, kotlin_dsl: bool, indent: str) -> str:
        inner = cls._build_type_block(kotlin_dsl, indent + "    ")
        return f"{indent}buildTypes {{\n{inner}{indent}}}\n"

    @classmethod
    def _add_profileable(cls, text: str) -> dict[str, Any]:
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return {
                "text": text,
                "error_type": "MANIFEST_MODIFICATION_UNSAFE",
                "summary": "AndroidManifest.xml 无法安全解析。",
            }
        applications = root.findall("application")
        if len(applications) != 1:
            return {
                "text": text,
                "error_type": "MANIFEST_MODIFICATION_UNSAFE",
                "summary": "Manifest 中没有唯一的 application 节点。",
            }
        profileables = applications[0].findall("profileable")
        if profileables:
            if len(profileables) == 1 and profileables[0].attrib.get(cls.ANDROID_SHELL) == "true":
                return {"text": text, "error_type": None, "summary": ""}
            return {
                "text": text,
                "error_type": "MANIFEST_MODIFICATION_UNSAFE",
                "summary": "Manifest 已存在冲突的 profileable 配置。",
            }
        if "xmlns:android=" not in text:
            return {
                "text": text,
                "error_type": "MANIFEST_MODIFICATION_UNSAFE",
                "summary": "Manifest 缺少 android XML namespace，无法安全插入 profileable。",
            }
        matches = list(re.finditer(r"(?m)^(\s*)</application\s*>", text))
        if len(matches) != 1:
            return {
                "text": text,
                "error_type": "MANIFEST_MODIFICATION_UNSAFE",
                "summary": "无法安全定位 application 结束标签。",
            }
        match = matches[0]
        child_indent = match.group(1) + "    "
        profileable = (
            f'{child_indent}<profileable android:shell="true" />\n'
        )
        return {
            "text": text[:match.start()] + profileable + text[match.start():],
            "error_type": None,
            "summary": "",
        }

    @staticmethod
    def _add_settings_include(
        text: str,
        benchmark_module: str,
        *,
        kotlin_dsl: bool,
    ) -> str:
        gradle_path = ":" + benchmark_module.replace("/", ":")
        statement = (
            f'include("{gradle_path}")'
            if kotlin_dsl
            else f"include '{gradle_path}'"
        )
        return text.rstrip() + "\n\n" + statement + "\n"

    @classmethod
    def _benchmark_build_template(
        cls,
        *,
        application_module: str,
        compile_sdk: str,
        kotlin_dsl: bool,
    ) -> str:
        target_path = ":" + application_module.replace("/", ":")
        if kotlin_dsl:
            return f'''plugins {{
    id("com.android.test")
}}

android {{
    namespace = "{cls.BENCHMARK_NAMESPACE}"
    compileSdk = {compile_sdk}

    targetProjectPath = "{target_path}"
    experimentalProperties["android.experimental.self-instrumenting"] = true

    defaultConfig {{
        minSdk = 23
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }}

    buildTypes {{
        create("benchmark") {{
            isDebuggable = true
            signingConfig = signingConfigs.getByName("debug")
            matchingFallbacks += listOf("release")
        }}
    }}
}}

dependencies {{
    implementation("androidx.benchmark:benchmark-macro-junit4:1.4.1")
    implementation("androidx.test.ext:junit:1.3.0")
    implementation("androidx.test.uiautomator:uiautomator:2.3.0")
}}
'''
        return f'''plugins {{
    id 'com.android.test'
}}

android {{
    namespace '{cls.BENCHMARK_NAMESPACE}'
    compileSdk {compile_sdk}

    targetProjectPath = '{target_path}'
    experimentalProperties["android.experimental.self-instrumenting"] = true

    defaultConfig {{
        minSdk 23
        testInstrumentationRunner 'androidx.test.runner.AndroidJUnitRunner'
    }}

    buildTypes {{
        benchmark {{
            debuggable true
            signingConfig signingConfigs.debug
            matchingFallbacks = ['release']
        }}
    }}
}}

dependencies {{
    implementation 'androidx.benchmark:benchmark-macro-junit4:1.4.1'
    implementation 'androidx.test.ext:junit:1.3.0'
    implementation 'androidx.test.uiautomator:uiautomator:2.3.0'
}}
'''

    @classmethod
    def _startup_benchmark_template(cls, application_id: str) -> str:
        return f'''package {cls.BENCHMARK_NAMESPACE};

import androidx.benchmark.macro.CompilationMode;
import androidx.benchmark.macro.StartupMode;
import androidx.benchmark.macro.StartupTimingMetric;
import androidx.benchmark.macro.junit4.MacrobenchmarkRule;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.filters.LargeTest;

import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.Collections;

import kotlin.Unit;

@LargeTest
@RunWith(AndroidJUnit4.class)
public class StartupBenchmark {{
    @Rule
    public final MacrobenchmarkRule benchmarkRule = new MacrobenchmarkRule();

    @Test
    public void startup() {{
        benchmarkRule.measureRepeated(
                "{application_id}",
                Collections.singletonList(new StartupTimingMetric()),
                CompilationMode.DEFAULT,
                StartupMode.COLD,
                {cls.ITERATIONS},
                scope -> {{
                    scope.pressHome();
                    return Unit.INSTANCE;
                }},
                scope -> {{
                    scope.startActivityAndWait(intent -> Unit.INSTANCE);
                    return Unit.INSTANCE;
                }}
        );
    }}
}}
'''

    @staticmethod
    def _find_block(text: str, pattern: str) -> tuple[int, int] | None:
        match = re.search(pattern, text)
        if match is None:
            return None
        opening = text.find("{", match.start(), match.end())
        if opening < 0:
            return None
        depth = 0
        for index in range(opening, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    return opening, index
        return None

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def _rollback(
        snapshots: dict[Path, str],
        benchmark_path: Path,
    ) -> Exception | None:
        try:
            for path, text in snapshots.items():
                path.write_text(text, encoding="utf-8")
            if benchmark_path.exists():
                shutil.rmtree(benchmark_path)
        except Exception as exc:
            return exc
        return None

    @staticmethod
    def _normalize_module(module: str) -> str:
        return module.strip().lstrip(":").replace(":", "/")

    @staticmethod
    def _reject_traversal(module: str, field: str) -> None:
        if any(part in {".", ".."} for part in Path(module).parts):
            raise ToolError(f"{field} 不允许路径穿越")

    @staticmethod
    def _ensure_within_project(project: Path, candidate: Path) -> None:
        try:
            candidate.relative_to(project)
        except ValueError as exc:
            raise ToolError("拒绝修改当前 Android 项目之外的文件") from exc

    @classmethod
    def _first_safe_file(cls, project: Path, *paths: Path) -> Path | None:
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
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @classmethod
    def _success_result(
        cls,
        *,
        application_module: str | None,
        application_id: str | None,
        benchmark_module: str,
        already_configured: bool,
        changed: bool,
        created_files: list[str],
        modified_files: list[str],
        profileable_ready: bool,
        benchmark_build_type_ready: bool,
        warnings: list[str],
        existing_benchmark_modules: list[str],
    ) -> dict[str, Any]:
        return {
            "success": True,
            "application_module": application_module,
            "application_id": application_id,
            "benchmark_module": benchmark_module,
            "benchmark_namespace": cls.BENCHMARK_NAMESPACE,
            "test_class": cls.TEST_CLASS,
            "test_method": cls.TEST_METHOD,
            "startup_mode": "COLD",
            "iterations": cls.ITERATIONS,
            "created_files": created_files,
            "modified_files": modified_files,
            "already_configured": already_configured,
            "changed": changed,
            "profileable_ready": profileable_ready,
            "benchmark_build_type_ready": benchmark_build_type_ready,
            "validation_task": (
                f":{benchmark_module.replace('/', ':')}:assembleBenchmark"
            ),
            "existing_benchmark_modules": existing_benchmark_modules,
            "warnings": warnings,
            "error_type": None,
            "summary": (
                "项目已存在 Macrobenchmark module，无需重复配置。"
                if already_configured
                else "Macrobenchmark 启动测试环境已创建，请由 Agent 决定是否执行 validation_task。"
            ),
        }

    @classmethod
    def _error_result(
        cls,
        *,
        application_module: str | None,
        application_id: str | None,
        benchmark_module: str,
        error_type: str,
        summary: str,
        candidates: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "application_module": application_module,
            "application_id": application_id,
            "benchmark_module": benchmark_module,
            "benchmark_namespace": cls.BENCHMARK_NAMESPACE,
            "test_class": cls.TEST_CLASS,
            "test_method": cls.TEST_METHOD,
            "startup_mode": "COLD",
            "iterations": cls.ITERATIONS,
            "created_files": [],
            "modified_files": [],
            "already_configured": False,
            "changed": False,
            "profileable_ready": False,
            "benchmark_build_type_ready": False,
            "validation_task": None,
            "existing_benchmark_modules": [],
            "candidates": candidates or [],
            "warnings": [],
            "error_type": error_type,
            "summary": summary,
        }
