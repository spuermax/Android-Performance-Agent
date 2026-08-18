from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import call

import pytest

from tools.adb_tool import AdbDevicesTool, AdbInstallTool, AdbLaunchAppTool
from tools.base import ToolError


ADB_PATH = "/opt/android-sdk/platform-tools/adb"


def completed(
    stdout: str = "",
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def create_tool(tmp_path: Path) -> AdbDevicesTool:
    return AdbDevicesTool(allowed_project_path=tmp_path)


def test_adb_not_found(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: None)

    result = create_tool(tmp_path).execute({})

    assert result["success"] is False
    assert result["adb_available"] is False
    assert result["error_type"] == "ADB_NOT_FOUND"
    assert result["devices"] == []


def test_adb_available_without_devices(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: completed("List of devices attached\n\n"),
    )

    result = create_tool(tmp_path).execute({})

    assert result["success"] is False
    assert result["error_type"] == "NO_DEVICE"
    assert result["device_count"] == 0
    assert result["ready_device_count"] == 0


def test_ready_device_includes_properties(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    outputs = iter(
        [
            completed(
                "List of devices attached\n"
                "emulator-5554 device product:sdk_gphone64 model:Pixel_8\n"
            ),
            completed("Google\n"),
            completed("Pixel 8\n"),
            completed("14\n"),
            completed("34\n"),
            completed("arm64-v8a\n"),
        ]
    )
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: next(outputs),
    )

    result = create_tool(tmp_path).execute({})

    assert result["success"] is True
    assert result["ready_device_count"] == 1
    assert result["devices"] == [
        {
            "serial": "emulator-5554",
            "state": "device",
            "manufacturer": "Google",
            "model": "Pixel 8",
            "android_version": "14",
            "sdk": 34,
            "abi": "arm64-v8a",
        }
    ]


def test_unauthorized_device_is_returned(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: completed(
            "List of devices attached\nR58M123 unauthorized usb:1-1\n"
        ),
    )

    result = create_tool(tmp_path).execute({})

    assert result["error_type"] == "NO_READY_DEVICE"
    assert result["devices"] == [{"serial": "R58M123", "state": "unauthorized"}]
    assert result["ready_device_count"] == 0


def test_offline_device_is_not_ready(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: completed(
            "List of devices attached\nemulator-5556 offline\n"
        ),
    )

    result = create_tool(tmp_path).execute({})

    assert result["error_type"] == "NO_READY_DEVICE"
    assert result["devices"][0]["state"] == "offline"
    assert result["ready_device_count"] == 0


def test_multiple_devices_are_all_returned_without_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    run_calls = []

    def fake_run(command, **kwargs):
        run_calls.append(call(command, **kwargs))
        if command == [ADB_PATH, "devices", "-l"]:
            return completed(
                "List of devices attached\n"
                "phone-1 device usb:1-1\n"
                "emulator-5554 device product:sdk_gphone64\n"
                "phone-2 unauthorized usb:1-2\n"
            )
        property_name = command[-1]
        values = {
            "ro.product.manufacturer": "Google",
            "ro.product.model": "Pixel",
            "ro.build.version.release": "14",
            "ro.build.version.sdk": "34",
            "ro.product.cpu.abi": "arm64-v8a",
        }
        return completed(values[property_name])

    monkeypatch.setattr("tools.adb_tool.subprocess.run", fake_run)

    result = create_tool(tmp_path).execute({})

    assert result["device_count"] == 3
    assert result["ready_device_count"] == 2
    assert [device["serial"] for device in result["devices"]] == [
        "phone-1",
        "emulator-5554",
        "phone-2",
    ]
    assert "selected_device" not in result
    assert all(record.kwargs["shell"] is False for record in run_calls)
    assert all(record.kwargs["timeout"] == 15 for record in run_calls)


def test_adb_command_failure_is_structured(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: completed(returncode=1),
    )

    result = create_tool(tmp_path).execute({})

    assert result["success"] is False
    assert result["error_type"] == "ADB_COMMAND_FAILED"
    assert result["devices"] == []


def test_failed_getprop_sets_only_that_field_to_none(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)

    def fake_run(command, **kwargs):
        if command == [ADB_PATH, "devices", "-l"]:
            return completed("List of devices attached\nphone-1 device\n")
        if command[-1] == "ro.product.model":
            return completed(returncode=1)
        values = {
            "ro.product.manufacturer": "Google",
            "ro.build.version.release": "15",
            "ro.build.version.sdk": "35",
            "ro.product.cpu.abi": "arm64-v8a",
        }
        return completed(values[command[-1]])

    monkeypatch.setattr("tools.adb_tool.subprocess.run", fake_run)

    result = create_tool(tmp_path).execute({})

    assert result["success"] is True
    assert result["devices"][0]["model"] is None
    assert result["devices"][0]["manufacturer"] == "Google"


def create_install_tool(project: Path) -> tuple[AdbInstallTool, Path, dict]:
    apk = project / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    apk.parent.mkdir(parents=True)
    apk.write_bytes(b"test apk")
    tool = AdbInstallTool(allowed_project_path=project)
    arguments = {
        "project_path": str(project),
        "serial": "emulator-5554",
        "apk_path": str(apk),
    }
    return tool, apk, arguments


def test_adb_install_reports_adb_not_found(monkeypatch, tmp_path: Path) -> None:
    tool, _, arguments = create_install_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: None)

    result = tool.execute(arguments)

    assert result["error_type"] == "ADB_NOT_FOUND"


def test_adb_install_rejects_unknown_serial(monkeypatch, tmp_path: Path) -> None:
    tool, _, arguments = create_install_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: completed(
            "List of devices attached\nother-device device\n"
        ),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "DEVICE_NOT_FOUND"


def test_adb_install_rejects_unauthorized_device(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, _, arguments = create_install_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: completed(
            "List of devices attached\nemulator-5554 unauthorized\n"
        ),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "DEVICE_UNAUTHORIZED"


def test_adb_install_rejects_offline_device(monkeypatch, tmp_path: Path) -> None:
    tool, _, arguments = create_install_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: completed(
            "List of devices attached\nemulator-5554 offline\n"
        ),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "DEVICE_OFFLINE"


def test_adb_install_reports_missing_apk(monkeypatch, tmp_path: Path) -> None:
    tool = AdbInstallTool(allowed_project_path=tmp_path)
    missing = tmp_path / "app" / "missing.apk"

    result = tool.execute(
        {
            "project_path": str(tmp_path),
            "serial": "emulator-5554",
            "apk_path": str(missing),
        }
    )

    assert result["error_type"] == "APK_NOT_FOUND"


def test_adb_install_rejects_apk_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside_apk = tmp_path / "outside.apk"
    outside_apk.write_bytes(b"test apk")
    tool = AdbInstallTool(allowed_project_path=project)

    with pytest.raises(ToolError, match="项目目录之外"):
        tool.execute(
            {
                "project_path": str(project),
                "serial": "emulator-5554",
                "apk_path": str(outside_apk),
            }
        )


def test_adb_install_success_uses_fixed_safe_commands(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, apk, arguments = create_install_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(call(command, **kwargs))
        if command == [ADB_PATH, "devices", "-l"]:
            return completed(
                "List of devices attached\nemulator-5554 device product:sdk\n"
            )
        return completed("Performing Streamed Install\nSuccess\n")

    monkeypatch.setattr("tools.adb_tool.subprocess.run", fake_run)

    result = tool.execute(arguments)

    assert result["success"] is True
    assert result["error_type"] is None
    assert result["exit_code"] == 0
    assert result["important_logs"] == ["Success"]
    assert calls[1].args[0] == [
        ADB_PATH,
        "-s",
        "emulator-5554",
        "install",
        "-r",
        str(apk),
    ]
    assert all(record.kwargs["shell"] is False for record in calls)
    assert calls[0].kwargs["timeout"] == 15
    assert calls[1].kwargs["timeout"] == 120


@pytest.mark.parametrize(
    "adb_error",
    [
        "INSTALL_FAILED_USER_RESTRICTED",
        "INSTALL_FAILED_UPDATE_INCOMPATIBLE",
        "INSTALL_FAILED_VERSION_DOWNGRADE",
        "INSTALL_FAILED_NO_MATCHING_ABIS",
    ],
)
def test_adb_install_classifies_common_failures(
    monkeypatch,
    tmp_path: Path,
    adb_error: str,
) -> None:
    tool, _, arguments = create_install_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    responses = iter(
        [
            completed(
                "List of devices attached\nemulator-5554 device\n"
            ),
            completed(
                returncode=1,
                stderr=f"Failure [{adb_error}]\n",
            ),
        ]
    )
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: next(responses),
    )

    result = tool.execute(arguments)

    assert result["success"] is False
    assert result["error_type"] == adb_error
    assert result["important_logs"] == [f"Failure [{adb_error}]"]


def test_adb_install_timeout_is_structured(monkeypatch, tmp_path: Path) -> None:
    tool, apk, arguments = create_install_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(call(command, **kwargs))
        if command == [ADB_PATH, "devices", "-l"]:
            return completed(
                "List of devices attached\nemulator-5554 device\n"
            )
        raise subprocess.TimeoutExpired(
            command,
            timeout=120,
            output="Performing Streamed Install\n",
        )

    monkeypatch.setattr("tools.adb_tool.subprocess.run", fake_run)

    result = tool.execute(arguments)

    assert result["success"] is False
    assert result["error_type"] == "ADB_INSTALL_TIMEOUT"
    assert result["apk_path"] == str(apk)
    assert result["serial"] == "emulator-5554"
    assert all(record.kwargs["shell"] is False for record in calls)


def create_launch_tool(tmp_path: Path) -> tuple[AdbLaunchAppTool, dict]:
    return AdbLaunchAppTool(allowed_project_path=tmp_path), {
        "serial": "emulator-5554",
        "application_id": "com.example.app",
        "launcher_component": "com.example.app/.MainActivity",
    }


def test_adb_launch_reports_adb_not_found(monkeypatch, tmp_path: Path) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: None)

    result = tool.execute(arguments)

    assert result["error_type"] == "ADB_NOT_FOUND"
    assert result["installed"] is False


def test_adb_launch_rejects_unknown_serial(monkeypatch, tmp_path: Path) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: completed(
            "List of devices attached\nother-device device\n"
        ),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "DEVICE_NOT_FOUND"


@pytest.mark.parametrize(
    ("state", "expected_error"),
    [
        ("unauthorized", "DEVICE_UNAUTHORIZED"),
        ("offline", "DEVICE_OFFLINE"),
    ],
)
def test_adb_launch_rejects_unavailable_device_states(
    monkeypatch,
    tmp_path: Path,
    state: str,
    expected_error: str,
) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: completed(
            f"List of devices attached\nemulator-5554 {state}\n"
        ),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == expected_error


def test_adb_launch_reports_package_not_installed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    responses = iter(
        [
            completed("List of devices attached\nemulator-5554 device\n"),
            completed(returncode=1),
        ]
    )
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: next(responses),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "PACKAGE_NOT_INSTALLED"
    assert result["installed"] is False
    assert result["activity_started"] is False


def test_adb_launch_rejects_component_package_mismatch_before_adb(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    arguments["launcher_component"] = "com.other.app/.MainActivity"

    def fail_if_called(*args, **kwargs):
        raise AssertionError("package mismatch must not execute adb")

    monkeypatch.setattr("tools.adb_tool.subprocess.run", fail_if_called)

    result = tool.execute(arguments)

    assert result["error_type"] == "COMPONENT_PACKAGE_MISMATCH"


def test_adb_launch_success_verifies_process_and_ignores_timings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(call(command, **kwargs))
        if command == [ADB_PATH, "devices", "-l"]:
            return completed("List of devices attached\nemulator-5554 device\n")
        if "pm" in command:
            return completed("package:/data/app/example/base.apk\n")
        if "am" in command:
            return completed(
                "Status: ok\n"
                "Activity: com.example.app/.MainActivity\n"
                "ThisTime: 100\nTotalTime: 120\nWaitTime: 130\n"
            )
        return completed("30592 30593\n")

    monkeypatch.setattr("tools.adb_tool.subprocess.run", fake_run)

    result = tool.execute(arguments)

    assert result["success"] is True
    assert result["installed"] is True
    assert result["activity_started"] is True
    assert result["process_running"] is True
    assert result["pids"] == [30592, 30593]
    assert result["important_logs"] == [
        "Status: ok",
        "Activity: com.example.app/.MainActivity",
    ]
    assert "startup_time" not in result
    assert "ttid" not in result
    assert all(record.kwargs["shell"] is False for record in calls)
    assert [record.kwargs["timeout"] for record in calls] == [15, 15, 30, 15]


def test_adb_launch_classifies_missing_activity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    responses = iter(
        [
            completed("List of devices attached\nemulator-5554 device\n"),
            completed("package:/data/app/example/base.apk\n"),
            completed(
                returncode=1,
                stderr=(
                    "Error type 3\n"
                    "Error: Activity class com.example.app/.Missing "
                    "does not exist.\n"
                ),
            ),
        ]
    )
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: next(responses),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "ACTIVITY_NOT_FOUND"
    assert result["installed"] is True
    assert result["activity_started"] is False


def test_adb_launch_classifies_permission_denial(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    responses = iter(
        [
            completed("List of devices attached\nemulator-5554 device\n"),
            completed("package:/data/app/example/base.apk\n"),
            completed(returncode=1, stderr="Permission Denial: not exported\n"),
        ]
    )
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: next(responses),
    )

    result = tool.execute(arguments)

    assert result["error_type"] == "ACTIVITY_PERMISSION_DENIED"


def test_adb_launch_timeout_is_structured(monkeypatch, tmp_path: Path) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)

    def fake_run(command, **kwargs):
        if command == [ADB_PATH, "devices", "-l"]:
            return completed("List of devices attached\nemulator-5554 device\n")
        if "pm" in command:
            return completed("package:/data/app/example/base.apk\n")
        raise subprocess.TimeoutExpired(command, timeout=30)

    monkeypatch.setattr("tools.adb_tool.subprocess.run", fake_run)

    result = tool.execute(arguments)

    assert result["error_type"] == "ADB_LAUNCH_TIMEOUT"
    assert result["installed"] is True
    assert result["activity_started"] is False


def test_adb_launch_fails_when_pidof_is_empty(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    monkeypatch.setattr("tools.adb_tool.shutil.which", lambda _: ADB_PATH)
    responses = iter(
        [
            completed("List of devices attached\nemulator-5554 device\n"),
            completed("package:/data/app/example/base.apk\n"),
            completed(
                "Status: ok\nActivity: com.example.app/.MainActivity\n"
            ),
            completed(returncode=1),
        ]
    )
    monkeypatch.setattr(
        "tools.adb_tool.subprocess.run",
        lambda *args, **kwargs: next(responses),
    )

    result = tool.execute(arguments)

    assert result["success"] is False
    assert result["installed"] is True
    assert result["activity_started"] is True
    assert result["process_running"] is False
    assert result["pids"] == []
    assert result["error_type"] == "LAUNCH_VERIFICATION_FAILED"


@pytest.mark.parametrize(
    ("field", "malicious_value"),
    [
        ("application_id", "com.example.app;rm -rf /"),
        ("application_id", "com.example.app && id"),
        ("launcher_component", "com.example.app/.MainActivity|id"),
        ("launcher_component", "com.example.app/.MainActivity`id`"),
    ],
)
def test_adb_launch_rejects_malicious_identifiers(
    tmp_path: Path,
    field: str,
    malicious_value: str,
) -> None:
    tool, arguments = create_launch_tool(tmp_path)
    arguments[field] = malicious_value

    with pytest.raises(ToolError):
        tool.execute(arguments)
