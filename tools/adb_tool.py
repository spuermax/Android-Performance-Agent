from __future__ import annotations

import shutil
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError


class AdbDevicesTool(BaseTool):
    name = "adb_devices"
    description = (
        "检查本机 ADB 环境以及当前连接的 Android 真机和模拟器，"
        "返回所有设备的连接状态与可用设备的基础系统信息，"
        "为后续真机启动性能测试和 Macrobenchmark 做准备。"
    )

    DEFAULT_TIMEOUT_SECONDS = 15
    DEVICE_PROPERTIES = {
        "manufacturer": "ro.product.manufacturer",
        "model": "ro.product.model",
        "android_version": "ro.build.version.release",
        "sdk": "ro.build.version.sdk",
        "abi": "ro.product.cpu.abi",
    }

    def __init__(self, allowed_project_path: Path) -> None:
        # 保持统一的 Tool 构造接口；本 Tool 不访问项目目录。
        super().__init__(allowed_project_path=allowed_project_path)

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments:
            raise ToolError("adb_devices 不接受参数")

        adb_path = shutil.which("adb")
        if adb_path is None:
            return self._result(
                success=False,
                adb_available=False,
                adb_path=None,
                devices=[],
                error_type="ADB_NOT_FOUND",
                summary="未找到 adb，请安装 Android SDK Platform-Tools 并配置 PATH。",
            )

        try:
            completed = subprocess.run(
                [adb_path, "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=self.DEFAULT_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return self._command_failed_result(
                adb_path,
                f"adb devices -l 执行超过 {self.DEFAULT_TIMEOUT_SECONDS} 秒。",
            )
        except OSError as exc:
            return self._command_failed_result(
                adb_path,
                f"无法执行 adb devices -l：{type(exc).__name__}。",
            )

        if completed.returncode != 0:
            return self._command_failed_result(
                adb_path,
                f"adb devices -l 执行失败，退出码为 {completed.returncode}。",
            )

        devices = self._parse_devices(completed.stdout)
        for device in devices:
            if device["state"] == "device":
                self._add_device_properties(adb_path, device)

        ready_device_count = sum(
            device["state"] == "device" for device in devices
        )
        if not devices:
            return self._result(
                success=False,
                adb_available=True,
                adb_path=adb_path,
                devices=[],
                error_type="NO_DEVICE",
                summary="ADB 可用，但没有发现 Android 设备。",
            )

        if ready_device_count == 0:
            return self._result(
                success=False,
                adb_available=True,
                adb_path=adb_path,
                devices=devices,
                error_type="NO_READY_DEVICE",
                summary=self._no_ready_device_summary(devices),
            )

        device_count = len(devices)
        if device_count == ready_device_count:
            summary = f"发现 {ready_device_count} 台可用 Android 设备。"
        else:
            summary = (
                f"发现 {device_count} 台 Android 设备，"
                f"其中 {ready_device_count} 台可用。"
            )

        return self._result(
            success=True,
            adb_available=True,
            adb_path=adb_path,
            devices=devices,
            error_type=None,
            summary=summary,
        )

    @staticmethod
    def _parse_devices(stdout: str | None) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []

        for raw_line in (stdout or "").splitlines():
            line = raw_line.strip()
            if (
                not line
                or line.startswith("List of devices attached")
                or line.startswith("*")
            ):
                continue

            parts = line.split()
            if len(parts) < 2:
                continue

            devices.append({"serial": parts[0], "state": parts[1]})

        return devices

    def _add_device_properties(
        self,
        adb_path: str,
        device: dict[str, Any],
    ) -> None:
        serial = device["serial"]
        for field, property_name in self.DEVICE_PROPERTIES.items():
            value = self._get_property(adb_path, serial, property_name)
            if field == "sdk":
                try:
                    device[field] = int(value) if value is not None else None
                except ValueError:
                    device[field] = None
            else:
                device[field] = value

    def _get_property(
        self,
        adb_path: str,
        serial: str,
        property_name: str,
    ) -> str | None:
        try:
            completed = subprocess.run(
                [
                    adb_path,
                    "-s",
                    serial,
                    "shell",
                    "getprop",
                    property_name,
                ],
                capture_output=True,
                text=True,
                timeout=self.DEFAULT_TIMEOUT_SECONDS,
                shell=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

        if completed.returncode != 0:
            return None

        value = (completed.stdout or "").strip()
        return value or None

    @staticmethod
    def _no_ready_device_summary(devices: list[dict[str, Any]]) -> str:
        unauthorized_count = sum(
            device["state"] == "unauthorized" for device in devices
        )
        offline_count = sum(device["state"] == "offline" for device in devices)
        details: list[str] = []
        if unauthorized_count:
            details.append(f"{unauthorized_count} 台 unauthorized")
        if offline_count:
            details.append(f"{offline_count} 台 offline")

        if details:
            return "发现 Android 设备，但没有可用设备：" + "，".join(details) + "。"
        return "发现 Android 设备，但没有处于 device 状态的可用设备。"

    @classmethod
    def _command_failed_result(
        cls,
        adb_path: str,
        summary: str,
    ) -> dict[str, Any]:
        return cls._result(
            success=False,
            adb_available=True,
            adb_path=adb_path,
            devices=[],
            error_type="ADB_COMMAND_FAILED",
            summary=summary,
        )

    @staticmethod
    def _result(
        *,
        success: bool,
        adb_available: bool,
        adb_path: str | None,
        devices: list[dict[str, Any]],
        error_type: str | None,
        summary: str,
    ) -> dict[str, Any]:
        return {
            "success": success,
            "adb_available": adb_available,
            "adb_path": adb_path,
            "device_count": len(devices),
            "ready_device_count": sum(
                device["state"] == "device" for device in devices
            ),
            "devices": devices,
            "error_type": error_type,
            "summary": summary,
        }


class AdbInstallTool(BaseTool):
    name = "adb_install"
    description = (
        "将已经构建好的 APK 安装到显式指定的 Android 设备，"
        "用于后续启动性能测试和 Macrobenchmark。"
        "必须提供 adb_devices 返回的真实设备 serial；本 Tool 不自动选择设备，"
        "不执行 Gradle Build，也不接受任意 ADB 命令。"
    )

    DEVICE_CHECK_TIMEOUT_SECONDS = 15
    INSTALL_TIMEOUT_SECONDS = 120

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "用户指定的 Android 项目绝对路径。",
                },
                "serial": {
                    "type": "string",
                    "description": "adb_devices 返回的目标设备 serial。",
                },
                "apk_path": {
                    "type": "string",
                    "description": "当前 Android 项目目录内已构建 APK 的路径。",
                },
            },
            "required": ["project_path", "serial", "apk_path"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_project_path = arguments.get("project_path")
        serial = arguments.get("serial")
        raw_apk_path = arguments.get("apk_path")

        if not isinstance(raw_project_path, str) or not raw_project_path.strip():
            raise ToolError("project_path 必须是非空字符串")
        if not isinstance(serial, str) or not serial.strip():
            raise ToolError("serial 必须是非空字符串")
        if not isinstance(raw_apk_path, str) or not raw_apk_path.strip():
            raise ToolError("apk_path 必须是非空字符串")

        project = self.validate_project_path(raw_project_path)
        serial = serial.strip()
        apk_path = Path(raw_apk_path).expanduser().resolve()
        try:
            apk_path.relative_to(project)
        except ValueError as exc:
            raise ToolError("拒绝安装当前 Android 项目目录之外的 APK") from exc

        if apk_path.suffix.lower() != ".apk":
            raise ToolError("apk_path 必须指向 .apk 文件")
        if not apk_path.exists():
            return self._result(
                serial=serial,
                apk_path=apk_path,
                error_type="APK_NOT_FOUND",
                summary="指定的 APK 文件不存在。",
            )
        if not apk_path.is_file():
            return self._result(
                serial=serial,
                apk_path=apk_path,
                error_type="APK_NOT_FILE",
                summary="指定的 APK 路径不是普通文件。",
            )

        adb_path = shutil.which("adb")
        if adb_path is None:
            return self._result(
                serial=serial,
                apk_path=apk_path,
                error_type="ADB_NOT_FOUND",
                summary="未找到 adb，请安装 Android SDK Platform-Tools 并配置 PATH。",
            )

        device_check = self._check_device(adb_path, serial)
        if device_check is not None:
            error_type, summary = device_check
            return self._result(
                serial=serial,
                apk_path=apk_path,
                error_type=error_type,
                summary=summary,
            )

        command = [adb_path, "-s", serial, "install", "-r", str(apk_path)]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.INSTALL_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            raw = self._combine_output(
                self._safe_decode(exc.stdout),
                self._safe_decode(exc.stderr),
            )
            return self._result(
                serial=serial,
                apk_path=apk_path,
                duration_ms=duration_ms,
                error_type="ADB_INSTALL_TIMEOUT",
                summary=(
                    f"ADB 安装超过 {self.INSTALL_TIMEOUT_SECONDS} 秒，已终止。"
                ),
                important_logs=self._important_logs(raw),
            )
        except OSError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return self._result(
                serial=serial,
                apk_path=apk_path,
                duration_ms=duration_ms,
                error_type="ADB_INSTALL_FAILED",
                summary=f"无法执行 ADB 安装：{type(exc).__name__}。",
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        raw = self._combine_output(completed.stdout, completed.stderr)
        install_succeeded = completed.returncode == 0 and any(
            line.strip() == "Success" for line in raw.splitlines()
        )
        if install_succeeded:
            return self._result(
                success=True,
                serial=serial,
                apk_path=apk_path,
                duration_ms=duration_ms,
                exit_code=completed.returncode,
                error_type=None,
                summary="APK 已成功安装到设备。",
                important_logs=self._important_logs(raw),
            )

        error_type = self._classify_install_error(raw)
        return self._result(
            serial=serial,
            apk_path=apk_path,
            duration_ms=duration_ms,
            exit_code=completed.returncode,
            error_type=error_type,
            summary=self._failure_summary(error_type),
            important_logs=self._important_logs(raw),
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
                timeout=self.DEVICE_CHECK_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return "ADB_COMMAND_FAILED", "检查 ADB 设备列表超时。"
        except OSError as exc:
            return (
                "ADB_COMMAND_FAILED",
                f"无法检查 ADB 设备列表：{type(exc).__name__}。",
            )

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
            return "DEVICE_NOT_READY", f"设备当前状态不可用于安装：{state}。"
        return None

    @staticmethod
    def _classify_install_error(raw: str) -> str:
        error_types = (
            "INSTALL_FAILED_USER_RESTRICTED",
            "INSTALL_FAILED_VERSION_DOWNGRADE",
            "INSTALL_FAILED_UPDATE_INCOMPATIBLE",
            "INSTALL_FAILED_INSUFFICIENT_STORAGE",
            "INSTALL_FAILED_OLDER_SDK",
            "INSTALL_FAILED_NO_MATCHING_ABIS",
        )
        for error_type in error_types:
            if error_type in raw:
                return error_type
        if "INSTALL_PARSE_FAILED_" in raw:
            return "INSTALL_PARSE_FAILED"
        return "ADB_INSTALL_FAILED"

    @staticmethod
    def _failure_summary(error_type: str) -> str:
        summaries = {
            "INSTALL_FAILED_USER_RESTRICTED": (
                "设备拒绝通过 ADB 安装 APK；请在设备上允许 USB 安装，"
                "MIUI/HyperOS 设备还需检查 USB 安装权限。"
            ),
            "INSTALL_FAILED_VERSION_DOWNGRADE": "APK 版本低于设备上的已安装版本。",
            "INSTALL_FAILED_UPDATE_INCOMPATIBLE": (
                "APK 与设备上已安装应用的签名不一致。"
            ),
            "INSTALL_FAILED_INSUFFICIENT_STORAGE": "设备存储空间不足。",
            "INSTALL_FAILED_OLDER_SDK": "APK 要求的 Android SDK 高于设备版本。",
            "INSTALL_FAILED_NO_MATCHING_ABIS": "APK 不支持设备的 CPU ABI。",
            "INSTALL_PARSE_FAILED": "设备无法解析 APK。",
            "ADB_INSTALL_FAILED": "ADB 安装失败，请查看裁剪后的关键日志。",
        }
        return summaries.get(error_type, "ADB 安装失败。")

    @staticmethod
    def _safe_decode(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _combine_output(stdout: str | None, stderr: str | None) -> str:
        return ((stdout or "") + "\n" + (stderr or "")).strip()

    @staticmethod
    def _important_logs(raw: str) -> list[str]:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        selected = [
            line
            for line in lines
            if (
                line == "Success"
                or "INSTALL_" in line
                or "Failure" in line
                or "failed" in line.lower()
                or "error" in line.lower()
            )
        ]
        if not selected:
            selected = lines[-5:]
        return [line[:500] for line in selected[-10:]]

    @staticmethod
    def _result(
        *,
        serial: str,
        apk_path: Path,
        error_type: str | None,
        summary: str,
        success: bool = False,
        duration_ms: int = 0,
        exit_code: int | None = None,
        important_logs: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": success,
            "serial": serial,
            "apk_path": str(apk_path),
            "duration_ms": duration_ms,
            "exit_code": exit_code,
            "error_type": error_type,
            "summary": summary,
            "important_logs": important_logs or [],
        }


class AdbLaunchAppTool(BaseTool):
    name = "adb_launch_app"
    description = (
        "在显式指定的 Android 设备上启动已经安装的 App，并验证 Launcher "
        "Component 和 App 进程是否有效。这个 Tool 只用于启动可用性验证，"
        "不执行 force-stop，也不提供 TTID、TTFD 或正式启动性能结论。"
    )

    DEVICE_CHECK_TIMEOUT_SECONDS = 15
    PACKAGE_CHECK_TIMEOUT_SECONDS = 15
    LAUNCH_TIMEOUT_SECONDS = 30
    PROCESS_CHECK_TIMEOUT_SECONDS = 15
    PROCESS_CHECK_ATTEMPTS = 3
    PROCESS_CHECK_RETRY_DELAY_SECONDS = 0.4
    APPLICATION_ID_PATTERN = re.compile(
        r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
    )
    CLASS_NAME_PATTERN = re.compile(
        r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "serial": {
                    "type": "string",
                    "description": "adb_devices 返回的目标设备 serial。",
                },
                "application_id": {
                    "type": "string",
                    "description": "已经安装到设备的 Android applicationId。",
                },
                "launcher_component": {
                    "type": "string",
                    "description": (
                        "inspect_app_target 返回的 Launcher Component，"
                        "格式为 package/class。"
                    ),
                },
            },
            "required": ["serial", "application_id", "launcher_component"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        serial = arguments.get("serial")
        application_id = arguments.get("application_id")
        launcher_component = arguments.get("launcher_component")

        if not isinstance(serial, str) or not serial.strip():
            raise ToolError("serial 必须是非空字符串")
        if not isinstance(application_id, str) or not self.APPLICATION_ID_PATTERN.fullmatch(
            application_id
        ):
            raise ToolError("application_id 格式不合法")
        if not isinstance(launcher_component, str):
            raise ToolError("launcher_component 必须是字符串")

        component_package = self._validate_component(launcher_component)
        serial = serial.strip()
        if component_package != application_id:
            return self._result(
                serial=serial,
                application_id=application_id,
                launcher_component=launcher_component,
                error_type="COMPONENT_PACKAGE_MISMATCH",
                summary="Launcher Component 的 package 与 application_id 不一致。",
            )

        adb_path = shutil.which("adb")
        if adb_path is None:
            return self._result(
                serial=serial,
                application_id=application_id,
                launcher_component=launcher_component,
                error_type="ADB_NOT_FOUND",
                summary="未找到 adb，请安装 Android SDK Platform-Tools 并配置 PATH。",
            )

        device_check = self._check_device(adb_path, serial)
        if device_check is not None:
            error_type, summary = device_check
            return self._result(
                serial=serial,
                application_id=application_id,
                launcher_component=launcher_component,
                error_type=error_type,
                summary=summary,
            )

        package_check = self._check_package(adb_path, serial, application_id)
        if package_check is not None:
            error_type, summary = package_check
            return self._result(
                serial=serial,
                application_id=application_id,
                launcher_component=launcher_component,
                error_type=error_type,
                summary=summary,
            )

        launch_command = [
            adb_path,
            "-s",
            serial,
            "shell",
            "am",
            "start",
            "-W",
            "-n",
            launcher_component,
        ]
        try:
            completed = subprocess.run(
                launch_command,
                capture_output=True,
                text=True,
                timeout=self.LAUNCH_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raw = AdbInstallTool._combine_output(
                AdbInstallTool._safe_decode(exc.stdout),
                AdbInstallTool._safe_decode(exc.stderr),
            )
            return self._result(
                serial=serial,
                application_id=application_id,
                launcher_component=launcher_component,
                installed=True,
                error_type="ADB_LAUNCH_TIMEOUT",
                summary=(
                    f"App 启动命令超过 {self.LAUNCH_TIMEOUT_SECONDS} 秒，已终止。"
                ),
                important_logs=self._launch_logs(raw),
            )
        except OSError as exc:
            return self._result(
                serial=serial,
                application_id=application_id,
                launcher_component=launcher_component,
                installed=True,
                error_type="ADB_LAUNCH_FAILED",
                summary=f"无法执行 App 启动命令：{type(exc).__name__}。",
            )

        raw = AdbInstallTool._combine_output(completed.stdout, completed.stderr)
        launch_error = self._classify_launch_error(raw)
        if launch_error is not None:
            return self._result(
                serial=serial,
                application_id=application_id,
                launcher_component=launcher_component,
                installed=launch_error != "PACKAGE_NOT_INSTALLED",
                error_type=launch_error,
                summary=self._launch_failure_summary(launch_error),
                important_logs=self._launch_logs(raw),
            )

        activity_started = (
            completed.returncode == 0
            and "Status: ok" in raw
            and any(
                line.strip().startswith("Activity:")
                for line in raw.splitlines()
            )
        )
        if not activity_started:
            return self._result(
                serial=serial,
                application_id=application_id,
                launcher_component=launcher_component,
                installed=True,
                error_type="ADB_LAUNCH_FAILED",
                summary="ADB 未返回可确认 Activity 成功启动的状态。",
                important_logs=self._launch_logs(raw),
            )

        pids = self._get_pids(adb_path, serial, application_id)
        if not pids:
            return self._result(
                serial=serial,
                application_id=application_id,
                launcher_component=launcher_component,
                installed=True,
                activity_started=True,
                error_type="LAUNCH_VERIFICATION_FAILED",
                summary=(
                    "Activity 启动命令执行成功，但目标进程未保持存活，"
                    "可能启动后退出或崩溃，需要 logcat 进一步确认。"
                ),
                important_logs=self._launch_logs(raw),
            )

        return self._result(
            success=True,
            serial=serial,
            application_id=application_id,
            launcher_component=launcher_component,
            installed=True,
            activity_started=True,
            process_running=True,
            pids=pids,
            error_type=None,
            summary="App 已在指定设备上成功启动，并检测到目标应用进程。",
            important_logs=self._launch_logs(raw),
        )

    @classmethod
    def _validate_component(cls, component: str) -> str:
        if component.count("/") != 1:
            raise ToolError("launcher_component 必须使用 package/class 格式")
        package_name, class_name = component.split("/", 1)
        if not cls.APPLICATION_ID_PATTERN.fullmatch(package_name):
            raise ToolError("launcher_component 的 package 格式不合法")

        normalized_class = class_name[1:] if class_name.startswith(".") else class_name
        if (
            not normalized_class
            or not cls.CLASS_NAME_PATTERN.fullmatch(normalized_class)
            or (not class_name.startswith(".") and "." not in class_name)
        ):
            raise ToolError("launcher_component 的 Activity 类名格式不合法")
        return package_name

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
                timeout=self.DEVICE_CHECK_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return "ADB_COMMAND_FAILED", "检查 ADB 设备列表超时。"
        except OSError as exc:
            return (
                "ADB_COMMAND_FAILED",
                f"无法检查 ADB 设备列表：{type(exc).__name__}。",
            )

        if completed.returncode != 0:
            return "ADB_COMMAND_FAILED", "adb devices -l 执行失败。"
        devices = AdbDevicesTool._parse_devices(completed.stdout)
        matched = next(
            (device for device in devices if device["serial"] == serial),
            None,
        )
        if matched is None:
            return "DEVICE_NOT_FOUND", "指定 serial 不在当前 ADB 设备列表中。"
        if matched["state"] == "unauthorized":
            return "DEVICE_UNAUTHORIZED", "设备尚未授权当前电脑进行 ADB 调试。"
        if matched["state"] == "offline":
            return "DEVICE_OFFLINE", "设备当前处于 offline 状态。"
        if matched["state"] != "device":
            return (
                "DEVICE_NOT_READY",
                f"设备当前状态不可用于启动：{matched['state']}。",
            )
        return None

    def _check_package(
        self,
        adb_path: str,
        serial: str,
        application_id: str,
    ) -> tuple[str, str] | None:
        try:
            completed = subprocess.run(
                [
                    adb_path,
                    "-s",
                    serial,
                    "shell",
                    "pm",
                    "path",
                    application_id,
                ],
                capture_output=True,
                text=True,
                timeout=self.PACKAGE_CHECK_TIMEOUT_SECONDS,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return "ADB_COMMAND_FAILED", "检查目标应用安装状态超时。"
        except OSError as exc:
            return (
                "ADB_COMMAND_FAILED",
                f"无法检查目标应用安装状态：{type(exc).__name__}。",
            )

        package_paths = [
            line.strip()
            for line in (completed.stdout or "").splitlines()
            if line.strip().startswith("package:")
        ]
        if completed.returncode != 0 or not package_paths:
            return "PACKAGE_NOT_INSTALLED", "目标应用尚未安装到指定设备。"
        return None

    def _get_pids(
        self,
        adb_path: str,
        serial: str,
        application_id: str,
    ) -> list[int]:
        for attempt in range(self.PROCESS_CHECK_ATTEMPTS):
            try:
                completed = subprocess.run(
                    [
                        adb_path,
                        "-s",
                        serial,
                        "shell",
                        "pidof",
                        application_id,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self.PROCESS_CHECK_TIMEOUT_SECONDS,
                    shell=False,
                )
            except (subprocess.TimeoutExpired, OSError):
                pids: list[int] = []
            else:
                pids = (
                    [
                        int(value)
                        for value in (completed.stdout or "").split()
                        if value.isdigit() and int(value) > 0
                    ]
                    if completed.returncode == 0
                    else []
                )

            if pids:
                return pids
            if attempt < self.PROCESS_CHECK_ATTEMPTS - 1:
                time.sleep(self.PROCESS_CHECK_RETRY_DELAY_SECONDS)
        return []

    @staticmethod
    def _classify_launch_error(raw: str) -> str | None:
        lower = raw.lower()
        if "permission denial" in lower:
            return "ACTIVITY_PERMISSION_DENIED"
        if "package" in lower and (
            "not installed" in lower or "not found" in lower
        ):
            return "PACKAGE_NOT_INSTALLED"
        if (
            "error type 3" in lower
            or ("activity class" in lower and "does not exist" in lower)
            or "unable to resolve intent" in lower
        ):
            return "ACTIVITY_NOT_FOUND"
        return None

    @staticmethod
    def _launch_failure_summary(error_type: str) -> str:
        summaries = {
            "ACTIVITY_NOT_FOUND": (
                "Launcher Component 无法启动，请重新检查 App Target。"
            ),
            "ACTIVITY_PERMISSION_DENIED": "设备拒绝启动目标 Activity。",
            "PACKAGE_NOT_INSTALLED": "目标应用尚未安装到指定设备。",
        }
        return summaries.get(error_type, "ADB 启动失败。")

    @staticmethod
    def _launch_logs(raw: str) -> list[str]:
        selected: list[str] = []
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            lower = line.lower()
            if not line or line.startswith(("ThisTime:", "TotalTime:", "WaitTime:")):
                continue
            if (
                line.startswith(("Status:", "Activity:", "Error:"))
                or "permission denial" in lower
                or "does not exist" in lower
                or "unable to resolve intent" in lower
                or "error type" in lower
            ):
                selected.append(line[:500])
        return selected[-10:]

    @staticmethod
    def _result(
        *,
        serial: str,
        application_id: str,
        launcher_component: str,
        error_type: str | None,
        summary: str,
        success: bool = False,
        installed: bool = False,
        activity_started: bool = False,
        process_running: bool = False,
        pids: list[int] | None = None,
        important_logs: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": success,
            "serial": serial,
            "application_id": application_id,
            "launcher_component": launcher_component,
            "installed": installed,
            "activity_started": activity_started,
            "process_running": process_running,
            "pids": pids or [],
            "error_type": error_type,
            "summary": summary,
            "important_logs": important_logs or [],
        }
