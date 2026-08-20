from __future__ import annotations

from pathlib import Path

import web_app
from tools.base import BaseTool
from tools.registry import ToolRegistry


class SecretTool(BaseTool):
    name = "secret_tool"
    description = "test"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    def execute(self, arguments: dict) -> dict:
        return {
            "success": True,
            "password": "do-not-leak",
            "log": "repo=https://alice:secret@example.com/maven",
        }


def _reset_dashboard() -> None:
    with web_app.lock:
        web_app.state["dashboard"] = web_app.empty_dashboard("/project")
        web_app.state["blocked"] = False
        web_app.state["status"] = "running"
        web_app.state["current_tool"] = None


def test_main_registers_prepare_benchmark_target() -> None:
    root = Path(__file__).resolve().parents[1]
    main_text = (root / "main.py").read_text(encoding="utf-8")

    assert "from tools.benchmark_target_tool import PrepareBenchmarkTargetTool" in main_text
    assert "registry.register(PrepareBenchmarkTargetTool(" in main_text


def test_variant_discovery_failure_is_a_measure_blocker() -> None:
    _reset_dashboard()

    web_app.apply_agent_event(
        {"type": "tool_started", "name": "inspect_build_variants"}
    )
    web_app.apply_agent_event(
        {
            "type": "tool_result",
            "name": "inspect_build_variants",
            "result": {
                "success": False,
                "error_type": "GRADLE_TASK_DISCOVERY_FAILED",
                "candidate_count": 0,
                "variants": [],
                "summary": "variant discovery failed",
            },
        }
    )

    dashboard = web_app.state["dashboard"]
    assert web_app.state["blocked"] is True
    assert dashboard["measure"]["status"] == "blocked"
    assert dashboard["analyze"]["status"] == "skipped"
    assert dashboard["plan"]["status"] == "skipped"
    assert dashboard["locate"]["status"] == "skipped"


def test_target_preparation_success_recovers_from_temporary_block() -> None:
    _reset_dashboard()
    with web_app.lock:
        web_app.state["blocked"] = True
        web_app.state["dashboard"]["measure"]["status"] = "blocked"
        web_app.state["dashboard"]["analyze"]["status"] = "skipped"
        web_app.state["dashboard"]["plan"]["status"] = "skipped"
        web_app.state["dashboard"]["locate"]["status"] = "skipped"

    web_app.apply_agent_event(
        {
            "type": "tool_result",
            "name": "prepare_benchmark_target",
            "result": {
                "success": True,
                "benchmark_ready": True,
                "module": "edusoho",
                "selected_variant": "Release",
                "selected_apk": "/project/edusoho-release.apk",
                "application_id": "com.example.app",
                "launcher_component": "com.example.app/.MainActivity",
                "candidates_discovered": 3,
                "candidates_checked": 2,
                "candidate_results": [],
                "summary": "ready",
            },
        }
    )

    dashboard = web_app.state["dashboard"]
    assert web_app.state["blocked"] is False
    assert dashboard["measure"]["status"] == "pending"
    assert dashboard["measure"]["selected_variant"] == "Release"
    assert dashboard["project"]["application_id"] == "com.example.app"
    assert dashboard["analyze"]["status"] == "pending"
    assert dashboard["plan"]["status"] == "pending"
    assert dashboard["locate"]["status"] == "pending"


def test_registry_uses_canonical_tools_redaction() -> None:
    registry = ToolRegistry()
    registry.register(SecretTool(allowed_project_path=Path.cwd()))

    result = registry.execute("secret_tool", {})

    assert result["password"] == "***REDACTED***"
    assert "alice" not in result["log"]
    assert "secret" not in result["log"]
