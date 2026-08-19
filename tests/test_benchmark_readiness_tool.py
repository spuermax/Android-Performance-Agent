from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.base import ToolError
from tools.benchmark_readiness_tool import InspectBenchmarkReadinessTool


ADB_PATH = "/opt/android-sdk/platform-tools/adb"
AAPT2_PATH = Path("/opt/android-sdk/build-tools/35.0.0/aapt2")


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


def manifest_dump(
    *,
    debuggable: bool = False,
    profileable: bool = True,
    profileable_shell: bool = True,
    profileinstaller: bool = True,
) -> str:
    lines = [
        "E: manifest",
        '  A: package="com.example.app"',
        "  E: application",
    ]
    if debuggable:
        lines.append("    A: android:debuggable=true")
    if profileable:
        lines.append("    E: profileable")
        lines.append(
            f"      A: android:shell={'true' if profileable_shell else 'false'}"
        )
    if profileinstaller:
        lines.extend(
            [
                "    E: receiver",
                '      A: android:name="androidx.profileinstaller.ProfileInstallReceiver"',
            ]
        )
    return "\n".join(lines) + "\n"


def create_tool(tmp_path: Path) -> InspectBenchmarkReadinessTool:
    return InspectBenchmarkReadinessTool(allowed_project_path=tmp_path)


def arguments() -> dict[str, str]:
    return {"serial": "device-1", "package_name": "com.example.app"}


def mock_ready_device(monkeypatch, manifest: str, *, package_installed: bool = True):
    monkeypatch.setattr(
        "tools.benchmark_readiness_tool.shutil.which",
        lambda name: ADB_PATH if name == "adb" else None,
    )
    monkeypatch.setattr(
        InspectBenchmarkReadinessTool,
        "_find_aapt2",
        staticmethod(lambda: AAPT2_PATH),
    )
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command == [ADB_PATH, "devices", "-l"]:
            return completed("List of devices attached\ndevice-1 device\n")
        if "getprop" in command:
            values = {
                "ro.product.manufacturer": "Google",
                "ro.product.model": "Pixel 8",
                "ro.build.version.release": "14",
                "ro.build.version.sdk": "34",
                "ro.product.cpu.abi": "arm64-v8a",
            }
            return completed(values[command[-1]] + "\n")
        if command[:6] == [
            ADB_PATH,
            "-s",
            "device-1",
            "shell",
            "pm",
            "path",
        ]:
            if package_installed:
                return completed("package:/data/app/com.example.app/base.apk\n")
            return completed(returncode=1, stderr="Unknown package")
        if command[:4] == [ADB_PATH, "-s", "device-1", "pull"]:
            return completed("1 file pulled")
        if command[0] == str(AAPT2_PATH):
            return completed(manifest)
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("tools.benchmark_readiness_tool.subprocess.run", fake_run)
    return calls


def test_readiness_reports_adb_not_found(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("tools.benchmark_readiness_tool.shutil.which", lambda _: None)

    result = create_tool(tmp_path).execute(arguments())

    assert result["error_type"] == "ADB_NOT_FOUND"
    assert result["benchmark_ready"] is False


@pytest.mark.parametrize(
    ("state", "error_type"),
    [("offline", "DEVICE_OFFLINE"), ("unauthorized", "DEVICE_UNAUTHORIZED")],
)
def test_readiness_rejects_unavailable_device(
    monkeypatch,
    tmp_path: Path,
    state: str,
    error_type: str,
) -> None:
    monkeypatch.setattr(
        "tools.benchmark_readiness_tool.shutil.which",
        lambda _: ADB_PATH,
    )
    monkeypatch.setattr(
        "tools.benchmark_readiness_tool.subprocess.run",
        lambda *args, **kwargs: completed(
            f"List of devices attached\ndevice-1 {state}\n"
        ),
    )

    result = create_tool(tmp_path).execute(arguments())

    assert result["error_type"] == error_type


def test_readiness_reports_package_not_installed(monkeypatch, tmp_path: Path) -> None:
    mock_ready_device(
        monkeypatch,
        manifest_dump(),
        package_installed=False,
    )

    result = create_tool(tmp_path).execute(arguments())

    assert result["error_type"] == "PACKAGE_NOT_INSTALLED"
    assert result["installed"] is False


def test_readiness_blocks_debuggable_target(monkeypatch, tmp_path: Path) -> None:
    calls = mock_ready_device(
        monkeypatch,
        manifest_dump(debuggable=True),
    )

    result = create_tool(tmp_path).execute(arguments())

    assert result["debuggable"] is True
    assert result["benchmark_ready"] is False
    assert "TARGET_DEBUGGABLE" in result["blocking_reasons"]
    assert all(kwargs["shell"] is False for _, kwargs in calls)


def test_readiness_blocks_target_not_profileable(monkeypatch, tmp_path: Path) -> None:
    mock_ready_device(
        monkeypatch,
        manifest_dump(profileable=False),
    )

    result = create_tool(tmp_path).execute(arguments())

    assert result["profileable"] is False
    assert result["profileable_shell"] is False
    assert result["error_type"] == "TARGET_NOT_PROFILEABLE"


def test_readiness_detects_profileinstaller_as_separate_capability(
    monkeypatch,
    tmp_path: Path,
) -> None:
    mock_ready_device(
        monkeypatch,
        manifest_dump(profileinstaller=False),
    )

    result = create_tool(tmp_path).execute(arguments())

    assert result["profileable_shell"] is True
    assert result["profileinstaller_available"] is False
    assert result["error_type"] == "PROFILER_INSTALLER_NOT_FOUND"
    assert result["warnings"]


def test_readiness_accepts_actual_installed_apk_properties(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls = mock_ready_device(monkeypatch, manifest_dump())

    result = create_tool(tmp_path).execute(arguments())

    assert result["success"] is True
    assert result["installed"] is True
    assert result["debuggable"] is False
    assert result["profileable"] is True
    assert result["profileable_shell"] is True
    assert result["profileinstaller_available"] is True
    assert result["benchmark_ready"] is True
    assert result["blocking_reasons"] == []
    assert result["device_context"] == {
        "manufacturer": "Google",
        "model": "Pixel 8",
        "android_version": "14",
        "sdk": 34,
        "abi": "arm64-v8a",
    }
    assert any(command[:4] == [ADB_PATH, "-s", "device-1", "pull"] for command, _ in calls)


def test_readiness_validates_package_name(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="package_name"):
        create_tool(tmp_path).execute(
            {"serial": "device-1", "package_name": "not a package"}
        )
