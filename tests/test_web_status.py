from web_status import (
    apply_benchmark_readiness_result,
    finish_dashboard_sections,
    resolve_final_status,
)


def dashboard() -> dict:
    return {
        name: {"status": "pending", "summary": None}
        for name in ("project", "device", "measure", "analyze", "plan", "locate")
    }


def test_readiness_failure_marks_measure_blocked() -> None:
    d = dashboard()
    apply_benchmark_readiness_result(
        d,
        {
            "success": False,
            "benchmark_ready": False,
            "summary": "TARGET_DEBUGGABLE",
        },
    )
    assert d["measure"]["status"] == "blocked"
    assert resolve_final_status(
        stopped=False,
        reached_max_steps=False,
        returncode=0,
        dashboard=d,
    ) == "blocked"


def test_readiness_success_keeps_measure_pending_until_real_measure() -> None:
    d = dashboard()
    apply_benchmark_readiness_result(
        d,
        {
            "success": True,
            "benchmark_ready": True,
            "summary": "ready",
        },
    )
    assert d["measure"]["status"] == "pending"


def test_blocked_measure_skips_downstream_pipeline() -> None:
    d = dashboard()
    d["project"]["status"] = "success"
    d["device"]["status"] = "success"
    d["measure"]["status"] = "blocked"
    finish_dashboard_sections(d, "blocked")
    assert d["measure"]["status"] == "blocked"
    assert d["analyze"]["status"] == "skipped"
    assert d["plan"]["status"] == "skipped"
    assert d["locate"]["status"] == "skipped"


def test_completed_only_when_no_blocker_and_process_succeeds() -> None:
    d = dashboard()
    d["project"]["status"] = "success"
    assert resolve_final_status(
        stopped=False,
        reached_max_steps=False,
        returncode=0,
        dashboard=d,
    ) == "completed"
