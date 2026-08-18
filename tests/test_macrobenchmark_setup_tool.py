from __future__ import annotations

from pathlib import Path

import pytest

from tools.base import ToolError
from tools.macrobenchmark_setup_tool import SetupMacrobenchmarkTool
from tools.macrobenchmark_tool import RunMacrobenchmarkTool


ANDROID_NS = "http://schemas.android.com/apk/res/android"


def create_project(
    root: Path,
    *,
    kotlin_dsl: bool = False,
    existing_profileable: bool = False,
    existing_benchmark_build_type: bool = False,
) -> tuple[SetupMacrobenchmarkTool, dict, dict[str, Path]]:
    (root / "gradlew").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    settings_name = "settings.gradle.kts" if kotlin_dsl else "settings.gradle"
    settings = root / settings_name
    settings.write_text(
        'include(":app")\n' if kotlin_dsl else "include ':app'\n",
        encoding="utf-8",
    )
    app = root / "app"
    app.mkdir()
    build_name = "build.gradle.kts" if kotlin_dsl else "build.gradle"
    build = app / build_name
    if kotlin_dsl:
        benchmark_type = (
            '''
        create("benchmark") {
            initWith(getByName("release"))
            matchingFallbacks += listOf("release")
            isDebuggable = false
        }
'''
            if existing_benchmark_build_type
            else ""
        )
        build.write_text(
            f'''plugins {{ id("com.android.application") }}
android {{
    namespace = "com.example.code"
    compileSdk = 35
    defaultConfig {{ applicationId = "com.example.app" }}
    buildTypes {{
        getByName("release") {{ isMinifyEnabled = true }}
{benchmark_type}    }}
}}
''',
            encoding="utf-8",
        )
    else:
        benchmark_type = (
            """
        benchmark {
            initWith release
            matchingFallbacks = ['release']
            debuggable false
        }
"""
            if existing_benchmark_build_type
            else ""
        )
        build.write_text(
            f'''plugins {{ id 'com.android.application' }}
android {{
    namespace 'com.example.code'
    compileSdk 35
    defaultConfig {{ applicationId 'com.example.app' }}
    buildTypes {{
        release {{ minifyEnabled true }}
{benchmark_type}    }}
}}
''',
            encoding="utf-8",
        )
    manifest = app / "src" / "main" / "AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    profileable = (
        '        <profileable android:shell="true" />\n'
        if existing_profileable
        else ""
    )
    manifest.write_text(
        f'''<manifest xmlns:android="{ANDROID_NS}">
    <application android:theme="@style/AppTheme">
{profileable}        <activity android:name=".MainActivity" />
    </application>
</manifest>
''',
        encoding="utf-8",
    )
    arguments = {
        "project_path": str(root),
        "application_module": None,
        "application_id": "com.example.app",
        "benchmark_module": None,
    }
    return (
        SetupMacrobenchmarkTool(allowed_project_path=root),
        arguments,
        {"settings": settings, "build": build, "manifest": manifest},
    )


def test_setup_macrobenchmark_creates_groovy_module(tmp_path: Path) -> None:
    tool, arguments, files = create_project(tmp_path)

    result = tool.execute(arguments)

    benchmark_build = tmp_path / "benchmark" / "build.gradle"
    benchmark_test = (
        tmp_path
        / "benchmark"
        / "src"
        / "main"
        / "java"
        / "com"
        / "androidperformance"
        / "benchmark"
        / "StartupBenchmark.java"
    )
    assert result["success"] is True
    assert result["changed"] is True
    assert result["application_module"] == "app"
    assert result["validation_task"] == ":benchmark:assembleBenchmark"
    assert benchmark_build.is_file()
    assert benchmark_test.is_file()
    benchmark_source = benchmark_test.read_text(encoding="utf-8")
    assert "scope.startActivityAndWait(intent -> Unit.INSTANCE);" in benchmark_source
    assert "scope.startActivityAndWait();" not in benchmark_source
    assert "id 'com.android.test'" in benchmark_build.read_text(encoding="utf-8")
    assert "benchmark-macro-junit4:1.4.1" in benchmark_build.read_text(
        encoding="utf-8"
    )
    assert "matchingFallbacks = ['release']" in benchmark_build.read_text(
        encoding="utf-8"
    )
    assert "include ':benchmark'" in files["settings"].read_text(encoding="utf-8")
    assert "debuggable false" in files["build"].read_text(encoding="utf-8")
    assert 'profileable android:shell="true"' in files["manifest"].read_text(
        encoding="utf-8"
    )


def test_setup_macrobenchmark_supports_kotlin_dsl(tmp_path: Path) -> None:
    tool, arguments, files = create_project(tmp_path, kotlin_dsl=True)

    result = tool.execute(arguments)

    benchmark_build = tmp_path / "benchmark" / "build.gradle.kts"
    assert result["success"] is True
    assert benchmark_build.is_file()
    assert 'id("com.android.test")' in benchmark_build.read_text(encoding="utf-8")
    assert 'targetProjectPath = ":app"' in benchmark_build.read_text(
        encoding="utf-8"
    )
    assert 'create("benchmark")' in benchmark_build.read_text(encoding="utf-8")
    assert 'matchingFallbacks += listOf("release")' in benchmark_build.read_text(
        encoding="utf-8"
    )
    assert 'include(":benchmark")' in files["settings"].read_text(
        encoding="utf-8"
    )
    assert 'create("benchmark")' in files["build"].read_text(encoding="utf-8")


def test_setup_macrobenchmark_is_idempotent(tmp_path: Path) -> None:
    tool, arguments, files = create_project(tmp_path)

    first = tool.execute(arguments)
    settings_after_first = files["settings"].read_text(encoding="utf-8")
    build_after_first = files["build"].read_text(encoding="utf-8")
    manifest_after_first = files["manifest"].read_text(encoding="utf-8")
    second = tool.execute(arguments)

    assert first["changed"] is True
    assert second["success"] is True
    assert second["already_configured"] is True
    assert second["changed"] is False
    assert files["settings"].read_text(encoding="utf-8") == settings_after_first
    assert files["build"].read_text(encoding="utf-8") == build_after_first
    assert files["manifest"].read_text(encoding="utf-8") == manifest_after_first


def test_setup_macrobenchmark_does_not_duplicate_profileable(
    tmp_path: Path,
) -> None:
    tool, arguments, files = create_project(
        tmp_path,
        existing_profileable=True,
    )

    result = tool.execute(arguments)

    assert result["success"] is True
    assert files["manifest"].read_text(encoding="utf-8").count("<profileable") == 1


def test_setup_macrobenchmark_reuses_valid_benchmark_build_type(
    tmp_path: Path,
) -> None:
    tool, arguments, files = create_project(
        tmp_path,
        existing_benchmark_build_type=True,
    )

    result = tool.execute(arguments)

    assert result["success"] is True
    assert files["build"].read_text(encoding="utf-8").count("benchmark {") == 1


def test_setup_macrobenchmark_requires_target_for_multiple_apps(
    tmp_path: Path,
) -> None:
    tool, arguments, _ = create_project(tmp_path)
    second = tmp_path / "edusoho"
    second.mkdir()
    (second / "build.gradle").write_text(
        "plugins { id 'com.android.application' }\n",
        encoding="utf-8",
    )
    settings = tmp_path / "settings.gradle"
    settings.write_text(
        "include ':app', ':edusoho'\n",
        encoding="utf-8",
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "MULTIPLE_APPLICATION_MODULES"
    assert result["candidates"] == ["app", "edusoho"]


def test_setup_macrobenchmark_requires_resolved_application_id(
    tmp_path: Path,
) -> None:
    tool, arguments, _ = create_project(tmp_path)
    arguments["application_id"] = None

    result = tool.execute(arguments)

    assert result["error_type"] == "APPLICATION_ID_UNRESOLVED"


def test_setup_macrobenchmark_rejects_project_outside_allowed_path(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    tool = SetupMacrobenchmarkTool(allowed_project_path=allowed)

    with pytest.raises(ToolError, match="拒绝访问"):
        tool.execute(
            {
                "project_path": str(other),
                "application_module": "app",
                "application_id": "com.example.app",
                "benchmark_module": "benchmark",
            }
        )


def test_setup_macrobenchmark_rolls_back_all_changes_on_write_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments, files = create_project(tmp_path)
    originals = {
        name: path.read_text(encoding="utf-8") for name, path in files.items()
    }
    original_write = tool._write_text
    failed = False

    def fail_on_app_build(path: Path, text: str) -> None:
        nonlocal failed
        if path == files["build"] and not failed:
            failed = True
            raise OSError("simulated write failure")
        original_write(path, text)

    monkeypatch.setattr(tool, "_write_text", fail_on_app_build)

    result = tool.execute(arguments)

    assert result["error_type"] == "WRITE_FAILED"
    assert not (tmp_path / "benchmark").exists()
    assert files["settings"].read_text(encoding="utf-8") == originals["settings"]
    assert files["build"].read_text(encoding="utf-8") == originals["build"]
    assert files["manifest"].read_text(encoding="utf-8") == originals["manifest"]


def test_generated_test_class_is_exactly_discoverable_by_runner(
    tmp_path: Path,
) -> None:
    tool, arguments, _ = create_project(tmp_path)

    result = tool.execute(arguments)
    benchmark_module = tmp_path / result["benchmark_module"]
    found = RunMacrobenchmarkTool._find_test_file(
        tmp_path,
        benchmark_module,
        result["test_class"],
    )

    assert found is not None
    assert found.name == "StartupBenchmark.java"
    assert RunMacrobenchmarkTool._contains_test_method(
        found.read_text(encoding="utf-8"),
        result["test_method"],
    )


def test_setup_macrobenchmark_skips_existing_benchmark(tmp_path: Path) -> None:
    tool, arguments, _ = create_project(tmp_path)
    settings = tmp_path / "settings.gradle"
    settings.write_text("include ':app', ':benchmark'\n", encoding="utf-8")
    benchmark = tmp_path / "benchmark"
    benchmark.mkdir()
    (benchmark / "build.gradle").write_text(
        "plugins { id 'com.android.test' }\n",
        encoding="utf-8",
    )
    arguments["application_module"] = None
    arguments["application_id"] = None

    result = tool.execute(arguments)

    assert result["success"] is True
    assert result["already_configured"] is True
    assert result["changed"] is False
    assert result["existing_benchmark_modules"] == ["benchmark"]


def test_setup_macrobenchmark_rejects_conflicting_build_type(
    tmp_path: Path,
) -> None:
    tool, arguments, files = create_project(tmp_path)
    build_text = files["build"].read_text(encoding="utf-8")
    build_text = build_text.replace(
        "release { minifyEnabled true }",
        """release { minifyEnabled true }
        benchmark {
            matchingFallbacks = ['release']
            debuggable true
        }""",
    )
    files["build"].write_text(build_text, encoding="utf-8")

    result = tool.execute(arguments)

    assert result["error_type"] == "BENCHMARK_BUILD_TYPE_CONFLICT"
    assert not (tmp_path / "benchmark").exists()
