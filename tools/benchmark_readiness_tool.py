from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tools.adb_tool import AdbDevicesTool
from tools.base import BaseTool, ToolError


class InspectBenchmarkReadinessTool(BaseTool):
    name = "inspect_benchmark_readiness"
    description = (
        "检查显式指定的 Android 设备上实际安装的目标 APK 是否满足 Standalone "
        "Macrobenchmark 条件，包括 debuggable、profileable by shell 和 "
        "ProfileInstaller；只检查设备与已安装 APK，不读取或修改用户工程。"
    )

    DEVICE_TIMEOUT_SECONDS = 15
    ADB_TIMEOUT_SECONDS = 30
    APK_PULL_TIMEOUT_SECONDS = 120
    AAPT_TIMEOUT_SECONDS = 30
    SERIAL_PATTERN = re.compile(r"[A-Za-z0-9._:-]+")
    PACKAGE_PATTERN = re.compile(
        r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
    )
    DEVICE_PROPERTIES = {
        "manufacturer": "ro.product.manufacturer",
        "model": "ro.product.model",
        "android_version": "ro.build.version.release",
        "sdk": "ro.build.version.sdk",
        "abi": "ro.product.cpu.abi",
    }

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "serial": {
                    "type": "string",
                    "description": "adb_devices 返回的目标设备 serial。",
                },
                "package_name": {
                    "type": "string",
                    "description": "设备上已经安装的真实 applicationId/package name。",
                },
            },
            "required": ["serial", "package_name"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        serial = arguments.get("serial")
        package_name = arguments.get("package_name")
        if not isinstance(serial, str) or not self.SERIAL_PATTERN.fullmatch(
            serial.strip()
        ):
            raise ToolError("serial 格式不合法")
        if not isinstance(package_name, str) or not self.PACKAGE_PATTERN.fullmatch(
            package_name.strip()
        ):
            raise ToolError("package_name 格式不合法")
        serial = serial.strip()
        package_name = package_name.strip()

        adb_path = shutil.which("adb")
        if adb_path is None:
            return self._result(
                serial=serial,
                package_name=package_name,
                error_type="ADB_NOT_FOUND",
                summary="未找到 adb，请安装 Android SDK Platform-Tools 并配置 PATH。",
            )

        device_error = self._check_device(adb_path, serial)
        if device_error is not None:
            error_type, summary = device_error
            return self._result(
                serial=serial,
                package_name=package_name,
                error_type=error_type,
                summary=summary,
            )

        device_context = self._device_context(adb_path, serial)
        package_path = self._installed_base_apk(adb_path, serial, package_name)
        if package_path["error_type"] is not None:
            return self._result(
                serial=serial,
                package_name=package_name,
                device_context=device_context,
                error_type=package_path["error_type"],
                summary=package_path["summary"],
            )

        aapt2_path = self._find_aapt2()
        if aapt2_path is None:
            return self._result(
                serial=serial,
                package_name=package_name,
                installed=True,
                apk_path_on_device=package_path["path"],
                device_context=device_context,
                error_type="AAPT2_NOT_FOUND",
                summary=(
                    "已找到目标 APK，但本机缺少 aapt2，无法检查实际安装 APK 的 Manifest。"
                ),
            )

        manifest_result = self._inspect_installed_manifest(
            adb_path=adb_path,
            aapt2_path=aapt2_path,
            serial=serial,
            apk_path_on_device=package_path["path"],
        )
        if manifest_result["error_type"] is not None:
            return self._result(
                serial=serial,
                package_name=package_name,
                installed=True,
                apk_path_on_device=package_path["path"],
                device_context=device_context,
                error_type=manifest_result["error_type"],
                summary=manifest_result["summary"],
            )

        debuggable = manifest_result["debuggable"]
        profileable = manifest_result["profileable"]
        profileable_shell = manifest_result["profileable_shell"]
        profileinstaller_available = manifest_result["profileinstaller_available"]
        blocking_reasons = self._blocking_reasons(
            debuggable=debuggable,
            profileable_shell=profileable_shell,
            profileinstaller_available=profileinstaller_available,
            startup_mode="COLD",
            compilation_mode="DEFAULT",
            device_context=device_context,
        )
        warnings: list[str] = []
        if not profileinstaller_available:
            warnings.append(
                "目标 APK 未检测到 ProfileInstaller receiver；当前 COLD/DEFAULT "
                "在非 root 设备上的 shader cache reset 可能无法可靠执行。"
            )

        if blocking_reasons:
            error_type = blocking_reasons[0]
            summary = self._blocking_summary(blocking_reasons)
        else:
            error_type = None
            summary = "目标 APK 已安装，并满足当前 Standalone COLD Macrobenchmark 条件。"

        return self._result(
            serial=serial,
            package_name=package_name,
            installed=True,
            debuggable=debuggable,
            profileable=profileable,
            profileable_shell=profileable_shell,
            profileinstaller_available=profileinstaller_available,
            benchmark_ready=not blocking_reasons,
            blocking_reasons=blocking_reasons,
            warnings=warnings,
            apk_path_on_device=package_path["path"],
            device_context=device_context,
            error_type=error_type,
            summary=summary,
        )

    def _check_device(
        self,
        adb_path: str,
        serial: str,
    ) -> tuple[str, str] | None:
        try:
            completed = subprocess.run(
                [adb_path, "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=self.DEVICE_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return "ADB_COMMAND_FAILED", "检查 ADB 设备列表超时。"
        except OSError as exc:
            return "ADB_COMMAND_FAILED", f"无法检查 ADB 设备：{type(exc).__name__}。"
        if completed.returncode != 0:
            return "ADB_COMMAND_FAILED", "adb devices -l 执行失败。"

        devices = AdbDevicesTool._parse_devices(completed.stdout)
        matched = next(
            (device for device in devices if device["serial"] == serial),
            None,
        )
        if matched is None:
            return "DEVICE_NOT_FOUND", "指定 serial 不在当前 ADB 设备列表中。"
        state = matched["state"]
        if state == "unauthorized":
            return "DEVICE_UNAUTHORIZED", "设备尚未授权当前电脑进行 ADB 调试。"
        if state == "offline":
            return "DEVICE_OFFLINE", "设备当前处于 offline 状态。"
        if state != "device":
            return "DEVICE_NOT_FOUND", f"设备状态不可用：{state}。"
        return None

    def _installed_base_apk(
        self,
        adb_path: str,
        serial: str,
        package_name: str,
    ) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                [adb_path, "-s", serial, "shell", "pm", "path", package_name],
                capture_output=True,
                text=True,
                timeout=self.ADB_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "path": None,
                "error_type": "ADB_COMMAND_FAILED",
                "summary": "检查目标 package 安装路径超时。",
            }
        except OSError as exc:
            return {
                "path": None,
                "error_type": "ADB_COMMAND_FAILED",
                "summary": f"无法检查目标 package：{type(exc).__name__}。",
            }
        paths = [
            line.removeprefix("package:").strip()
            for line in (completed.stdout or "").splitlines()
            if line.strip().startswith("package:")
        ]
        if completed.returncode != 0 or not paths:
            return {
                "path": None,
                "error_type": "PACKAGE_NOT_INSTALLED",
                "summary": "指定 package 未安装在目标设备上。",
            }
        base_apk = next((path for path in paths if path.endswith("/base.apk")), paths[0])
        return {"path": base_apk, "error_type": None, "summary": ""}

    def _inspect_installed_manifest(
        self,
        *,
        adb_path: str,
        aapt2_path: Path,
        serial: str,
        apk_path_on_device: str,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="android-performance-readiness-") as tmp:
            local_apk = Path(tmp) / "target.apk"
            try:
                pull = subprocess.run(
                    [adb_path, "-s", serial, "pull", apk_path_on_device, str(local_apk)],
                    capture_output=True,
                    text=True,
                    timeout=self.APK_PULL_TIMEOUT_SECONDS,
                    shell=False,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                return {
                    "error_type": "ADB_COMMAND_FAILED",
                    "summary": f"无法拉取设备上的目标 APK：{type(exc).__name__}。",
                }
            if pull.returncode != 0:
                return {
                    "error_type": "ADB_COMMAND_FAILED",
                    "summary": "adb pull 目标 APK 失败。",
                }

            try:
                dump = subprocess.run(
                    [
                        str(aapt2_path),
                        "dump",
                        "xmltree",
                        str(local_apk),
                        "--file",
                        "AndroidManifest.xml",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.AAPT_TIMEOUT_SECONDS,
                    shell=False,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                return {
                    "error_type": "APK_INSPECTION_FAILED",
                    "summary": f"无法解析目标 APK Manifest：{type(exc).__name__}。",
                }
            if dump.returncode != 0:
                return {
                    "error_type": "APK_INSPECTION_FAILED",
                    "summary": "aapt2 无法解析目标 APK Manifest。",
                }

        manifest = dump.stdout or ""
        profileable, profileable_shell = self._profileable_state(manifest)
        return {
            "debuggable": bool(
                re.search(r"android:debuggable[^\n]*=true", manifest)
            ),
            "profileable": profileable,
            "profileable_shell": profileable_shell,
            "profileinstaller_available": (
                "androidx.profileinstaller.ProfileInstallReceiver" in manifest
            ),
            "error_type": None,
            "summary": "",
        }

    @staticmethod
    def _profileable_state(manifest: str) -> tuple[bool, bool]:
        lines = manifest.splitlines()
        for index, line in enumerate(lines):
            stripped = line.lstrip()
            if not stripped.startswith("E: profileable"):
                continue
            indent = len(line) - len(stripped)
            shell_enabled = False
            for child in lines[index + 1:]:
                child_stripped = child.lstrip()
                child_indent = len(child) - len(child_stripped)
                if child_stripped.startswith("E:") and child_indent <= indent:
                    break
                if "android:shell" in child and re.search(r"=true(?:\s|$)", child):
                    shell_enabled = True
            return True, shell_enabled
        return False, False

    @classmethod
    def _blocking_reasons(
        cls,
        *,
        debuggable: bool,
        profileable_shell: bool,
        profileinstaller_available: bool,
        startup_mode: str,
        compilation_mode: str,
        device_context: dict[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        if debuggable:
            reasons.append("TARGET_DEBUGGABLE")
        if not profileable_shell:
            reasons.append("TARGET_NOT_PROFILEABLE")
        if cls._profileinstaller_required(
            startup_mode=startup_mode,
            compilation_mode=compilation_mode,
            device_context=device_context,
        ) and not profileinstaller_available:
            reasons.append("PROFILER_INSTALLER_NOT_FOUND")
        return reasons

    @staticmethod
    def _profileinstaller_required(
        *,
        startup_mode: str,
        compilation_mode: str,
        device_context: dict[str, Any],
    ) -> bool:
        # V0.2.7 only supports COLD + DEFAULT. Keep this policy isolated so later
        # modes/API-level rules can change without redefining readiness globally.
        _ = device_context
        return startup_mode == "COLD" and compilation_mode == "DEFAULT"

    @classmethod
    def _device_context(cls, adb_path: str, serial: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field, prop in cls.DEVICE_PROPERTIES.items():
            try:
                completed = subprocess.run(
                    [adb_path, "-s", serial, "shell", "getprop", prop],
                    capture_output=True,
                    text=True,
                    timeout=cls.DEVICE_TIMEOUT_SECONDS,
                    shell=False,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if completed.returncode != 0:
                continue
            value = (completed.stdout or "").strip()
            if not value:
                continue
            if field == "sdk":
                try:
                    result[field] = int(value)
                except ValueError:
                    continue
            else:
                result[field] = value
        return result

    @staticmethod
    def _find_aapt2() -> Path | None:
        direct = shutil.which("aapt2")
        if direct:
            return Path(direct).resolve()
        roots = [
            os.environ.get("ANDROID_HOME"),
            os.environ.get("ANDROID_SDK_ROOT"),
        ]
        candidates: list[Path] = []
        for raw_root in roots:
            if not raw_root:
                continue
            build_tools = Path(raw_root).expanduser() / "build-tools"
            candidates.extend(
                path for path in build_tools.glob("*/aapt2") if path.is_file()
            )
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda path: InspectBenchmarkReadinessTool._version_key(
                path.parent.name
            ),
        )

    @staticmethod
    def _version_key(value: str) -> tuple[tuple[int, Any], ...]:
        parts = re.split(r"([0-9]+)", value)
        return tuple(
            (1, int(part)) if part.isdigit() else (0, part.lower())
            for part in parts
            if part
        )

    @staticmethod
    def _blocking_summary(reasons: list[str]) -> str:
        labels = {
            "TARGET_DEBUGGABLE": "目标 APK 是 debuggable 构建",
            "TARGET_NOT_PROFILEABLE": "目标 APK 未启用 profileable by shell",
            "PROFILER_INSTALLER_NOT_FOUND": "目标 APK 未检测到 ProfileInstaller receiver",
        }
        return "目标 APK 暂不满足 Standalone Macrobenchmark 条件：" + "；".join(
            labels.get(reason, reason) for reason in reasons
        ) + "。"

    @staticmethod
    def _result(
        *,
        serial: str,
        package_name: str,
        installed: bool = False,
        debuggable: bool | None = None,
        profileable: bool | None = None,
        profileable_shell: bool | None = None,
        profileinstaller_available: bool | None = None,
        benchmark_ready: bool = False,
        blocking_reasons: list[str] | None = None,
        warnings: list[str] | None = None,
        apk_path_on_device: str | None = None,
        device_context: dict[str, Any] | None = None,
        error_type: str | None,
        summary: str,
    ) -> dict[str, Any]:
        return {
            "success": benchmark_ready,
            "serial": serial,
            "package_name": package_name,
            "installed": installed,
            "debuggable": debuggable,
            "profileable": profileable,
            "profileable_shell": profileable_shell,
            "profileinstaller_available": profileinstaller_available,
            "benchmark_ready": benchmark_ready,
            "blocking_reasons": blocking_reasons or [],
            "warnings": warnings or [],
            "apk_path_on_device": apk_path_on_device,
            "device_context": device_context or {},
            "error_type": error_type,
            "summary": summary,
        }
