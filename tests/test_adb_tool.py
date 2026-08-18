from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import call

from tools.adb_tool import AdbDevicesTool


ADB_PATH = "/opt/android-sdk/platform-tools/adb"


def completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr="",
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
