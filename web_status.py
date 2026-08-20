from __future__ import annotations

from typing import Any

PIPELINE_SECTIONS = ("project", "device", "measure", "analyze", "plan", "locate")


def apply_benchmark_readiness_result(
    dashboard: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Reflect readiness without pretending a benchmark has already run."""
    section = dashboard.get("measure")
    if not isinstance(section, dict):
        return

    ready = result.get("success") is True and result.get("benchmark_ready") is True
    section["status"] = "pending" if ready else "blocked"
    section["summary"] = result.get("summary")


def resolve_final_status(
    *,
    stopped: bool,
    reached_max_steps: bool,
    returncode: int,
    dashboard: dict[str, Any],
) -> str:
    if stopped:
        return "stopped"
    if reached_max_steps:
        return "incomplete"
    if returncode != 0:
        return "failed"
    if any(
        isinstance(dashboard.get(name), dict)
        and dashboard[name].get("status") == "blocked"
        for name in PIPELINE_SECTIONS
    ):
        return "blocked"
    return "completed"


def finish_dashboard_sections(
    dashboard: dict[str, Any],
    final_status: str,
) -> None:
    """Resolve in-flight/downstream sections after the Agent can no longer run."""
    if final_status in {"stopped", "failed"}:
        for section_name in PIPELINE_SECTIONS:
            section = dashboard.get(section_name)
            if isinstance(section, dict) and section.get("status") == "running":
                section["status"] = final_status
        return

    if final_status != "blocked":
        return

    blocked_seen = False
    for section_name in PIPELINE_SECTIONS:
        section = dashboard.get(section_name)
        if not isinstance(section, dict):
            continue
        status = section.get("status")
        if status == "blocked":
            blocked_seen = True
            continue
        if not blocked_seen and status == "running":
            section["status"] = "blocked"
            blocked_seen = True
            continue
        if blocked_seen and status in {"pending", "running"}:
            section["status"] = "skipped"
