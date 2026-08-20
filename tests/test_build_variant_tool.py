from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.build_variant_tool import InspectBuildVariantsTool


def make_project(tmp_path: Path) -> None:
    gradlew = tmp_path / "gradlew"
    gradlew.write_text("#!/bin/sh\n", encoding="utf-8")
    gradlew.chmod(0o755)
    module = tmp_path / "edusoho"
    module.mkdir()
    (module / "build.gradle").write_text(
        """
        apply plugin: 'com.android.application'
        android {
          buildTypes {
            debug { signingConfig signingConfigs.debugConfig }
            release { minifyEnabled true }
          }
        }
        """,
        encoding="utf-8",
    )


def test_discovers_real_assemble_tasks_and_prioritizes_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_project(tmp_path)
    output = """
assembleRelease - Assembles all release variants
assembleDebug - Assembles all debug variants
assembleHuawei - Assembles all variants for product flavor huawei
assembleXiaomi - Assembles all variants for product flavor xiaomi
assembleHuaweiDebug - Assembles main outputs for variant huaweiDebug
assembleHuaweiRelease - Assembles main outputs for variant huaweiRelease
assembleXiaomiDebug - Assembles main outputs for variant xiaomiDebug
assembleXiaomiRelease - Assembles main outputs for variant xiaomiRelease
assembleXiaomiDebugAndroidTest - Assembles tests
"""

    monkeypatch.setattr(
        "tools.build_variant_tool.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )
    tool = InspectBuildVariantsTool(allowed_project_path=tmp_path)
    result = tool.execute({"project_path": str(tmp_path), "module": "edusoho"})

    assert result["success"] is True
    tasks = [item["assemble_task"] for item in result["variants"]]
    assert tasks[:2] == [
        ":edusoho:assembleHuaweiRelease",
        ":edusoho:assembleXiaomiRelease",
    ]
    assert tasks[-2:] == [
        ":edusoho:assembleHuaweiDebug",
        ":edusoho:assembleXiaomiDebug",
    ]
    assert all("AndroidTest" not in task for task in tasks)
    assert ":edusoho:assembleRelease" not in tasks
    assert ":edusoho:assembleDebug" not in tasks
    assert ":edusoho:assembleHuawei" not in tasks
    assert ":edusoho:assembleXiaomi" not in tasks
    assert result["variants"][0]["formal_benchmark_candidate"] is True
    assert result["variants"][0]["signing_config_declared"] is False


def test_does_not_choose_flavor_from_device_brand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_project(tmp_path)
    output = """
assembleOppoRelease - Assembles main outputs for variant oppoRelease
assembleXiaomiRelease - Assembles main outputs for variant xiaomiRelease
"""
    monkeypatch.setattr(
        "tools.build_variant_tool.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )
    tool = InspectBuildVariantsTool(allowed_project_path=tmp_path)
    result = tool.execute({"project_path": str(tmp_path), "module": "edusoho"})

    # Stable lexical order among equal-priority release candidates; no device input exists.
    assert [item["flavor"] for item in result["variants"]] == ["oppo", "xiaomi"]



def test_plugin_detection_is_best_effort_for_alias_based_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gradlew = tmp_path / "gradlew"
    gradlew.write_text("#!/bin/sh\n", encoding="utf-8")
    gradlew.chmod(0o755)
    module = tmp_path / "app"
    module.mkdir()
    (module / "build.gradle.kts").write_text(
        'plugins { alias(libs.plugins.android.application) }\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "tools.build_variant_tool.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="assembleRelease - Assembles main outputs for variant release\n",
            stderr="",
        ),
    )
    tool = InspectBuildVariantsTool(allowed_project_path=tmp_path)
    result = tool.execute({"project_path": str(tmp_path), "module": "app"})
    assert result["success"] is True
    assert result["application_plugin_declared"] is False
    assert result["variants"][0]["assemble_task"] == ":app:assembleRelease"


def test_keeps_concrete_release_beside_custom_pre_release_build_type() -> None:
    raw = """
assembleRelease - Assembles main output for variant release
assemblePreRelease - Assembles main output for variant preRelease
"""

    variants = InspectBuildVariantsTool._parse_variants(raw, "app", "")

    assert {item["assemble_task"] for item in variants} == {
        ":app:assembleRelease",
        ":app:assemblePreRelease",
    }
