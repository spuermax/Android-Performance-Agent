from pathlib import Path

import pytest

from tools.base import ToolError
from tools.startup_source_locator_tool import LocateStartupBottleneckSourceTool


def write_project(project: Path) -> None:
    (project / "app/src/main/java/com/example/app").mkdir(parents=True)
    (project / "app/src/main/AndroidManifest.xml").write_text(
        """<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <application android:name=".ExampleApp">
        <provider android:name=".StartupProvider" android:authorities="com.example.app.startup" />
    </application>
</manifest>
""",
        encoding="utf-8",
    )


def plan(*recommendations: tuple[str, str]) -> dict:
    return {
        "success": True,
        "package_name": "com.example.app",
        "recommendations": [
            {"category": category, "evidence": evidence}
            for category, evidence in recommendations
        ],
    }


def execute(project: Path, optimization_plan: dict) -> dict:
    tool = LocateStartupBottleneckSourceTool(allowed_project_path=project)
    return tool.execute(
        {
            "optimization_plan": optimization_plan,
            "project_path": str(project),
            "target_module": ":app",
            "package_name": "com.example.app",
        }
    )


def categories(result: dict) -> set[str]:
    return {match["category"] for match in result["matches"]}


def test_locates_application_source_from_real_evidence(tmp_path: Path) -> None:
    write_project(tmp_path)
    (tmp_path / "app/src/main/java/com/example/app/ExampleApp.kt").write_text(
        """package com.example.app
class ExampleApp : Application() {
    override fun onCreate() {
        super.onCreate()
    }
}
""",
        encoding="utf-8",
    )

    result = execute(
        tmp_path,
        plan(("APPLICATION_INITIALIZATION", "ExampleApp 18.000 ms")),
    )

    assert result["success"] is True
    assert "APPLICATION_INITIALIZATION" in categories(result)
    exact = [match for match in result["matches"] if match["confidence"] == "HIGH"]
    assert any(match["symbol"] == "ExampleApp" for match in exact)
    assert all(match["evidence"] == "ExampleApp 18.000 ms" for match in result["matches"])


def test_manifest_application_without_class_evidence_is_low_confidence(
    tmp_path: Path,
) -> None:
    write_project(tmp_path)

    result = execute(
        tmp_path,
        plan(
            (
                "APPLICATION_INITIALIZATION",
                "exclusive bind_application 19.156 ms; raw bindApplication 49.951 ms",
            )
        ),
    )

    manifest = next(
        match for match in result["matches"]
        if match["file_path"].endswith("AndroidManifest.xml")
    )
    assert manifest["confidence"] == "LOW"
    assert "只证明它参与启动" in manifest["reason"]


def test_manifest_application_lifecycle_without_class_name_is_medium(
    tmp_path: Path,
) -> None:
    write_project(tmp_path)

    result = execute(
        tmp_path,
        plan(("APPLICATION_INITIALIZATION", "Application.onCreate 8.000 ms")),
    )

    manifest = next(
        match for match in result["matches"]
        if match["file_path"].endswith("AndroidManifest.xml")
    )
    assert manifest["confidence"] == "MEDIUM"


def test_locates_content_provider_source(tmp_path: Path) -> None:
    write_project(tmp_path)
    (tmp_path / "app/src/main/java/com/example/app/StartupProvider.kt").write_text(
        """package com.example.app
class StartupProvider : ContentProvider() {
    override fun onCreate(): Boolean = true
}
""",
        encoding="utf-8",
    )

    result = execute(
        tmp_path,
        plan(("CONTENT_PROVIDER_INITIALIZATION", "StartupProvider 8.000 ms")),
    )

    assert "CONTENT_PROVIDER_INITIALIZATION" in categories(result)
    assert any(match["symbol"] == "StartupProvider" for match in result["matches"])


def test_locates_manifest_provider(tmp_path: Path) -> None:
    write_project(tmp_path)

    result = execute(
        tmp_path,
        plan(("CONTENT_PROVIDER_INITIALIZATION", "StartupProvider 6.000 ms")),
    )

    manifest = [
        match for match in result["matches"]
        if match["file_path"].endswith("AndroidManifest.xml")
    ]
    assert manifest
    assert manifest[0]["symbol"] == ".StartupProvider"
    assert manifest[0]["confidence"] == "HIGH"


def test_locates_sdk_initialization_named_by_evidence(tmp_path: Path) -> None:
    write_project(tmp_path)
    (tmp_path / "app/src/main/java/com/example/app/ExampleApp.kt").write_text(
        """package com.example.app
class ExampleApp : Application() {
    override fun onCreate() {
        AnalyticsSdk.init(this)
    }
}
""",
        encoding="utf-8",
    )

    result = execute(
        tmp_path,
        plan(("APPLICATION_INITIALIZATION", "AnalyticsSdk 12.000 ms")),
    )

    sdk_matches = [
        match for match in result["matches"]
        if match["file_path"].endswith("ExampleApp.kt") and match["line"] == 4
    ]
    assert sdk_matches
    assert sdk_matches[0]["confidence"] == "HIGH"


def test_locates_main_thread_io_call(tmp_path: Path) -> None:
    write_project(tmp_path)
    (tmp_path / "app/src/main/java/com/example/app/ExampleApp.kt").write_text(
        """package com.example.app
class ExampleApp : Application() {
    override fun onCreate() {
        getSharedPreferences("startup", MODE_PRIVATE)
    }
}
""",
        encoding="utf-8",
    )

    result = execute(
        tmp_path,
        plan(("MAIN_THREAD_IO", "启动区间主线程 I/O blocking 31.000 ms")),
    )

    io_matches = [
        match for match in result["matches"]
        if match["category"] == "MAIN_THREAD_IO"
    ]
    assert io_matches
    assert any("getSharedPreferences" in match["reason"] for match in io_matches)
    assert all(match["confidence"] in {"MEDIUM", "LOW"} for match in io_matches)


def test_returns_multiple_categories_and_matches(tmp_path: Path) -> None:
    write_project(tmp_path)
    (tmp_path / "app/src/main/java/com/example/app/MainActivity.kt").write_text(
        """package com.example.app
class MainActivity : Activity() {
    override fun onCreate(state: Bundle?) {
        getSystemService(ACTIVITY_SERVICE)
        setContentView(R.layout.main)
    }
}
""",
        encoding="utf-8",
    )

    result = execute(
        tmp_path,
        plan(
            ("BINDER_IPC", "启动区间 Binder blocking 15.000 ms"),
            ("FIRST_FRAME_WORK", "choreographer_do_frame 20.000 ms"),
        ),
    )

    assert {"BINDER_IPC", "FIRST_FRAME_WORK"}.issubset(categories(result))
    assert len(result["matches"]) >= 2


def test_deduplicates_same_symbol_and_collects_matched_lines(tmp_path: Path) -> None:
    write_project(tmp_path)
    (tmp_path / "app/src/main/java/com/example/app/MainActivity.kt").write_text(
        """package com.example.app
class MainActivity : Activity() {
    override fun onCreate(state: Bundle?) {
        layoutInflater.inflate(R.layout.main, null)
    }
}
""",
        encoding="utf-8",
    )

    result = execute(
        tmp_path,
        plan(("LONG_MAIN_THREAD_TASK", "Raw main-thread Slice 20.000 ms")),
    )

    on_create = [
        match for match in result["matches"]
        if match["file_path"].endswith("MainActivity.kt")
        and match["symbol"] == "onCreate"
    ]
    assert len(on_create) == 1
    assert on_create[0]["matched_lines"] == [3, 4]


def test_symbol_deduplication_keeps_highest_confidence(tmp_path: Path) -> None:
    tool = LocateStartupBottleneckSourceTool(allowed_project_path=tmp_path)

    result = tool._deduplicate_and_sort(
        [
            {
                "category": "FIRST_FRAME_WORK",
                "file_path": "app/src/main/MainActivity.kt",
                "line": 17,
                "symbol": "onCreate",
                "confidence": "LOW",
            },
            {
                "category": "FIRST_FRAME_WORK",
                "file_path": "app/src/main/MainActivity.kt",
                "line": 18,
                "symbol": "onCreate",
                "confidence": "HIGH",
            },
        ]
    )

    assert len(result) == 1
    assert result[0]["confidence"] == "HIGH"
    assert result[0]["line"] == 18
    assert result[0]["matched_lines"] == [17, 18]


def test_locator_consumes_raw_slice_localization_hints(tmp_path: Path) -> None:
    write_project(tmp_path)
    (tmp_path / "app/src/main/java/com/example/app/MainActivity.kt").write_text(
        "class MainActivity { fun onCreate() = Unit }",
        encoding="utf-8",
    )
    optimization_plan = plan()
    optimization_plan["source_localization_hints"] = [
        {
            "category": "LONG_MAIN_THREAD_TASK",
            "evidence": "Raw Slice MainActivity.onCreate；仅用于源码定位。",
        }
    ]

    result = execute(tmp_path, optimization_plan)

    assert "LONG_MAIN_THREAD_TASK" in categories(result)


def test_unresolved_when_evidence_has_no_reliable_source_match(tmp_path: Path) -> None:
    write_project(tmp_path)

    result = execute(
        tmp_path,
        plan(("DEX_CLASS_LOADING", "open_dex_files_from_oat 7.000 ms")),
    )

    assert result["matches"] == []
    assert result["unresolved"][0]["category"] == "DEX_CLASS_LOADING"
    assert "不根据通用命名猜测" in result["unresolved"][0]["reason"]


def test_rejects_project_path_outside_allowed_project(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    write_project(allowed)
    write_project(outside)
    tool = LocateStartupBottleneckSourceTool(allowed_project_path=allowed)

    with pytest.raises(ToolError, match="拒绝访问"):
        tool.execute(
            {
                "optimization_plan": plan(
                    ("APPLICATION_INITIALIZATION", "ExampleApp 8.000 ms")
                ),
                "project_path": str(outside),
                "target_module": "app",
                "package_name": "com.example.app",
            }
        )


def test_rejects_module_traversal(tmp_path: Path) -> None:
    write_project(tmp_path)
    tool = LocateStartupBottleneckSourceTool(allowed_project_path=tmp_path)

    with pytest.raises(ToolError, match="target_module"):
        tool.execute(
            {
                "optimization_plan": plan(
                    ("APPLICATION_INITIALIZATION", "ExampleApp 8.000 ms")
                ),
                "project_path": str(tmp_path),
                "target_module": "../outside",
                "package_name": "com.example.app",
            }
        )


def test_does_not_follow_source_symlink_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    write_project(project)
    outside = tmp_path / "SecretSdk.kt"
    outside.write_text("class SecretSdk { fun init() = Unit }", encoding="utf-8")
    (project / "app/src/main/java/com/example/app/SecretSdk.kt").symlink_to(outside)

    result = execute(
        project,
        plan(("APPLICATION_INITIALIZATION", "SecretSdk 10.000 ms")),
    )

    assert all("SecretSdk.kt" not in match["file_path"] for match in result["matches"])


def test_rejects_plan_for_different_package(tmp_path: Path) -> None:
    write_project(tmp_path)
    wrong_plan = plan(("APPLICATION_INITIALIZATION", "ExampleApp 8.000 ms"))
    wrong_plan["package_name"] = "com.other.app"

    result = execute(tmp_path, wrong_plan)

    assert result["success"] is False
    assert result["error_type"] == "PACKAGE_MISMATCH"
    assert result["matches"] == []
