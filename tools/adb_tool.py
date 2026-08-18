from __future__ import annotations

import shutil
import subprocess
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
