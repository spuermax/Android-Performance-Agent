from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from tools.startup_optimization_tool import GenerateStartupOptimizationPlanTool


def tool(tmp_path: Path) -> GenerateStartupOptimizationPlanTool:
    return GenerateStartupOptimizationPlanTool(allowed_project_path=tmp_path)


def base_analysis() -> dict:
    return {
        "success": True,
        "package_name": "com.example.app",
        "startup_duration_ms": 300.0,
        "application_initialization": {"detected": False, "slices": []},
        "content_provider_initialization": {"detected": False, "slices": []},
        "io": {"total_blocking_ms": 0.0, "event_count": 0},
        "binder": {"total_blocking_ms": 0.0, "event_count": 0, "top_slices": []},
        "gc": {"total_wall_overlap_ms": 0.0, "event_count": 0, "events": []},
        "cpu": {
            "main_thread_running_ms": 0.0,
            "main_thread_runnable_ms": 0.0,
            "app_process_running_ms": 0.0,
        },
        "long_main_thread_slices": [],
        "startup_stages": [],
        "top_bottlenecks": [],
    }


def execute(tmp_path: Path, analysis: dict) -> dict:
    return tool(tmp_path).execute({"perfetto_analysis": analysis})


def categories(result: dict) -> list[str]:
    return [item["category"] for item in result["recommendations"]]


def test_optimization_rejects_invalid_perfetto_result(tmp_path: Path) -> None:
    analysis = base_analysis()
    analysis["success"] = False

    result = execute(tmp_path, analysis)

    assert result["error_type"] == "INVALID_PERFETTO_ANALYSIS"
    assert result["recommendations"] == []


def test_optimization_application_initialization(tmp_path: Path) -> None:
    analysis = base_analysis()
    analysis["application_initialization"] = {
        "detected": True,
        "slices": [{"name": "bindApplication", "duration_ms": 49.951}],
    }
    analysis["startup_stages"] = [
        {"stage": "bind_application", "duration_ms": 19.156, "event_count": 3}
    ]

    result = execute(tmp_path, analysis)

    assert "APPLICATION_INITIALIZATION" in categories(result)
    recommendation = result["recommendations"][0]
    assert recommendation["severity"] == "MEDIUM"
    assert result["bottlenecks"][0]["impact_ms"] == 19.156
    assert "bind_application 19.156 ms" in recommendation["evidence"]
    assert "bindApplication 49.951 ms" in recommendation["evidence"]
    assert "未提供业务 Application.onCreate 类级耗时证据" in (
        recommendation["evidence"]
    )
    assert "不能把该时长归因" in recommendation["reason"]


def test_optimization_does_not_attribute_raw_bind_application_alone(
    tmp_path: Path,
) -> None:
    analysis = base_analysis()
    analysis["application_initialization"] = {
        "detected": True,
        "slices": [{"name": "bindApplication", "duration_ms": 49.951}],
    }

    result = execute(tmp_path, analysis)

    assert "APPLICATION_INITIALIZATION" not in categories(result)


def test_optimization_content_provider(tmp_path: Path) -> None:
    analysis = base_analysis()
    analysis["content_provider_initialization"] = {
        "detected": True,
        "slices": [{"name": "SdkProvider.install", "duration_ms": 8.0}],
    }

    result = execute(tmp_path, analysis)

    assert categories(result) == ["CONTENT_PROVIDER_INITIALIZATION"]
    assert "Auto Init" in result["recommendations"][0]["suggestion"]


def test_optimization_skips_detected_initialization_below_evidence_threshold(
    tmp_path: Path,
) -> None:
    analysis = base_analysis()
    analysis["application_initialization"] = {
        "detected": True,
        "slices": [],
    }
    analysis["content_provider_initialization"] = {
        "detected": True,
        "slices": [{"name": "SmallProvider", "duration_ms": 1.0}],
    }

    result = execute(tmp_path, analysis)

    assert result["recommendations"] == []


def test_optimization_accepts_one_percent_evidence_threshold(
    tmp_path: Path,
) -> None:
    analysis = base_analysis()
    analysis["startup_duration_ms"] = 100.0
    analysis["content_provider_initialization"] = {
        "detected": True,
        "slices": [{"name": "Provider", "duration_ms": 1.0}],
    }

    result = execute(tmp_path, analysis)

    assert categories(result) == ["CONTENT_PROVIDER_INITIALIZATION"]
    assert result["evidence_threshold"]["minimum_impact_ms"] == 3.0
    assert result["evidence_threshold"]["minimum_startup_percentage"] == 1.0


def test_optimization_main_thread_io(tmp_path: Path) -> None:
    analysis = base_analysis()
    analysis["io"] = {"total_blocking_ms": 35.0, "event_count": 12}

    result = execute(tmp_path, analysis)

    recommendation = result["recommendations"][0]
    assert recommendation["category"] == "MAIN_THREAD_IO"
    assert recommendation["severity"] == "HIGH"
    assert "35.000 ms" in recommendation["evidence"]


def test_optimization_binder(tmp_path: Path) -> None:
    analysis = base_analysis()
    analysis["binder"] = {
        "total_blocking_ms": 15.0,
        "event_count": 5,
        "top_slices": [{"name": "binder transaction", "duration_ms": 7.0}],
    }

    result = execute(tmp_path, analysis)

    recommendation = result["recommendations"][0]
    assert recommendation["category"] == "BINDER_IPC"
    assert "binder transaction 7.000 ms" in recommendation["evidence"]


def test_optimization_gc_uses_wall_overlap_without_pause_claim(
    tmp_path: Path,
) -> None:
    analysis = base_analysis()
    analysis["gc"] = {
        "total_wall_overlap_ms": 6.0,
        "event_count": 2,
        "events": [],
    }

    result = execute(tmp_path, analysis)

    recommendation = result["recommendations"][0]
    assert recommendation["category"] == "STARTUP_GC_ALLOCATION"
    assert "wall duration" in recommendation["evidence"]
    assert "不等于 STW pause" in recommendation["evidence"]


def test_optimization_cpu(tmp_path: Path) -> None:
    analysis = base_analysis()
    analysis["cpu"] = {
        "main_thread_running_ms": 55.0,
        "main_thread_runnable_ms": 8.0,
        "app_process_running_ms": 170.0,
    }

    result = execute(tmp_path, analysis)

    recommendation = result["recommendations"][0]
    assert recommendation["category"] == "MAIN_THREAD_CPU_SCHEDULING"
    assert recommendation["severity"] == "HIGH"
    assert "Running 55.000 ms" in recommendation["evidence"]


def test_optimization_long_main_thread_slice(tmp_path: Path) -> None:
    analysis = base_analysis()
    analysis["long_main_thread_slices"] = [
        {"name": "inflate", "duration_ms": 70.0},
        {"name": "bindApplication", "duration_ms": 20.0},
    ]

    result = execute(tmp_path, analysis)

    assert result["recommendations"] == []
    hint = result["source_localization_hints"][0]
    assert hint["category"] == "LONG_MAIN_THREAD_TASK"
    assert hint["ranking_eligible"] is False
    assert "inflate 70.000 ms" in hint["evidence"]
    assert "禁止相加" in hint["evidence"]


def test_optimization_dex_and_class(tmp_path: Path) -> None:
    analysis = base_analysis()
    analysis["startup_stages"] = [
        {"stage": "open_dex_files_from_oat", "duration_ms": 6.0, "event_count": 2},
        {"stage": "verify_class", "duration_ms": 1.0, "event_count": 4},
    ]

    result = execute(tmp_path, analysis)

    recommendation = result["recommendations"][0]
    assert recommendation["category"] == "DEX_CLASS_LOADING"
    assert "Baseline Profile" in recommendation["suggestion"]
    assert "当前版本不生成 Profile" in recommendation["suggestion"]


def test_optimization_skips_dex_class_below_evidence_threshold(
    tmp_path: Path,
) -> None:
    analysis = base_analysis()
    analysis["startup_stages"] = [
        {"stage": "open_dex_files_from_oat", "duration_ms": 1.0, "event_count": 1}
    ]

    result = execute(tmp_path, analysis)

    assert "DEX_CLASS_LOADING" not in categories(result)


def test_optimization_uses_top_bottleneck_when_stage_is_absent(
    tmp_path: Path,
) -> None:
    analysis = base_analysis()
    analysis["top_bottlenecks"] = [
        {
            "reason": "choreographer_do_frame",
            "duration_ms": 55.0,
            "event_count": 9,
        }
    ]
    analysis["long_main_thread_slices"] = [
        {"name": "Choreographer#doFrame", "duration_ms": 105.563},
        {"name": "Choreographer#doFrame resynced", "duration_ms": 105.332},
        {"name": "traversal", "duration_ms": 105.028},
    ]

    result = execute(tmp_path, analysis)

    recommendation = result["recommendations"][0]
    assert recommendation["category"] == "FIRST_FRAME_WORK"
    assert recommendation["severity"] == "HIGH"
    assert "55.000 ms" in recommendation["evidence"]
    assert "105.563 ms" in recommendation["evidence"]
    assert "不与 exclusive breakdown 累加" in recommendation["evidence"]
    assert "LONG_MAIN_THREAD_TASK" not in categories(result)


def test_optimization_sorts_multiple_bottlenecks_by_evidence(
    tmp_path: Path,
) -> None:
    analysis = base_analysis()
    analysis["io"] = {"total_blocking_ms": 40.0, "event_count": 8}
    analysis["binder"] = {
        "total_blocking_ms": 15.0,
        "event_count": 4,
        "top_slices": [],
    }
    analysis["content_provider_initialization"] = {
        "detected": True,
        "slices": [{"name": "SmallProvider", "duration_ms": 1.0}],
    }

    result = execute(tmp_path, analysis)

    assert [item["category"] for item in result["priority_order"]] == [
        "MAIN_THREAD_IO",
        "BINDER_IPC",
    ]
    assert [item["severity"] for item in result["priority_order"]] == [
        "HIGH",
        "MEDIUM",
    ]
    assert len(result["verification_plan"]) == 2


def test_optimization_returns_no_generic_advice_without_evidence(
    tmp_path: Path,
) -> None:
    result = execute(tmp_path, base_analysis())

    assert result["success"] is True
    assert result["bottlenecks"] == []
    assert result["recommendations"] == []
    assert result["priority_order"] == []
    assert result["verification_plan"] == []
    assert "不生成泛化优化建议" in result["summary"]


def test_optimization_does_not_mutate_analysis(tmp_path: Path) -> None:
    analysis = base_analysis()
    original = deepcopy(analysis)

    execute(tmp_path, analysis)

    assert analysis == original
