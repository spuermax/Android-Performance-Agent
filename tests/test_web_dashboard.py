from __future__ import annotations

import web_app


def reset() -> None:
    with web_app.lock:
        web_app.state["dashboard"] = web_app.empty_dashboard("/project")
        web_app.state["current_tool"] = None
        web_app.state["status"] = "running"
        web_app.state["reached_max_steps"] = False
        web_app.state["blocked"] = False
        web_app.state["logs"] = []


def test_dashboard_collects_measure_device_and_source_results() -> None:
    reset()
    web_app.apply_agent_event({"type": "tool_started", "name": "run_macrobenchmark", "step": 1})
    web_app.apply_agent_event({"type": "tool_result", "name": "run_macrobenchmark", "step": 1, "result": {"success": True, "serial": "device-2", "ttid_ms": {"minimum": 280.4, "median": 297.8, "maximum": 341.9, "runs": [280.4, 297.8, 341.9]}, "ttfd_available": False, "ttfd_ms": None, "run_count": 3, "trace_files": ["/project/a.perfetto-trace"], "device_context": {"brand": "Xiaomi", "model": "M2102K1AC", "sdk": 34}, "summary": "ok"}})
    web_app.apply_agent_event({"type": "tool_result", "name": "locate_startup_bottleneck_source", "result": {"success": True, "matches": [{"category": "FIRST_FRAME_WORK", "file_path": "app/MainActivity.kt", "line": 20, "symbol": "onCreate", "confidence": "MEDIUM"}], "unresolved": ["BINDER_IPC"], "summary": "located"}})
    dashboard = web_app.state["dashboard"]
    assert dashboard["measure"]["status"] == "success"
    assert dashboard["measure"]["ttid_ms"]["median"] == 297.8
    assert dashboard["device"]["serial"] == "device-2"
    assert dashboard["device"]["manufacturer"] == "Xiaomi"
    assert dashboard["device"]["model"] == "M2102K1AC"
    assert dashboard["device"]["sdk"] == 34
    assert dashboard["locate"]["matches"][0]["confidence"] == "MEDIUM"
    assert dashboard["locate"]["unresolved"] == ["BINDER_IPC"]


def test_machine_event_line_is_not_added_to_human_log() -> None:
    reset()
    payload = web_app.EVENT_PREFIX + '{"type":"final","text":"done","step_count":2,"reached_max_steps":false}'
    web_app.handle_process_line(payload)
    assert web_app.state["dashboard"]["final_text"] == "done"
    assert web_app.state["reached_max_steps"] is False
    assert web_app.state["logs"] == []


def test_human_log_is_redacted_before_reaching_dashboard() -> None:
    reset()

    web_app.add_log(
        "password=secret repo=https://alice:token@example.com/maven"
    )

    log = web_app.state["logs"][0]
    assert "secret" not in log
    assert "alice" not in log
    assert "token" not in log


def test_final_event_preserves_max_steps_state() -> None:
    reset()

    web_app.apply_agent_event(
        {
            "type": "final",
            "text": "达到最大执行步数",
            "step_count": 15,
            "reached_max_steps": True,
        }
    )

    assert web_app.state["dashboard"]["final_text"] == "达到最大执行步数"
    assert web_app.state["reached_max_steps"] is True


def test_target_preparation_progress_is_visible_in_measure() -> None:
    reset()

    web_app.apply_agent_event(
        {
            "type": "tool_progress",
            "name": "prepare_benchmark_target",
            "candidate_index": 7,
            "candidate_total": 20,
            "variant": "OppoRelease",
            "status": "READINESS_FAILED",
            "error_type": "TARGET_NOT_PROFILEABLE",
        }
    )

    measure = web_app.state["dashboard"]["measure"]
    assert measure["status"] == "running"
    assert measure["candidate_progress"]["variant"] == "OppoRelease"
    assert measure["summary"] == (
        "7 / 20 - OppoRelease - READINESS_FAILED - TARGET_NOT_PROFILEABLE"
    )
    assert "7 / 20" in web_app.state["current_tool"]


def test_failed_app_candidate_does_not_replace_confirmed_target() -> None:
    reset()
    web_app.apply_agent_event(
        {
            "type": "tool_result",
            "name": "inspect_app_target",
            "result": {
                "success": True,
                "module": "edusoho",
                "application_id": "com.example.edusoho",
                "launcher_activity": "com.example.edusoho.MainActivity",
                "summary": "confirmed",
            },
        }
    )
    web_app.apply_agent_event(
        {"type": "tool_started", "name": "inspect_app_target"}
    )
    web_app.apply_agent_event(
        {
            "type": "tool_result",
            "name": "inspect_app_target",
            "result": {
                "success": False,
                "module": "app",
                "application_id": None,
                "error_type": "MODULE_NOT_FOUND",
                "summary": "app failed",
            },
        }
    )

    project = web_app.state["dashboard"]["project"]
    assert project["status"] == "success"
    assert project["module"] == "edusoho"
    assert project["application_id"] == "com.example.edusoho"
    assert project["summary"] == "confirmed"


def test_dashboard_collects_project_device_analysis_and_plan_results() -> None:
    reset()
    web_app.apply_agent_event(
        {
            "type": "tool_result",
            "name": "inspect_app_target",
            "result": {
                "success": True,
                "module": "app",
                "application_id": "com.sample.redex",
                "launcher_activity": "com.sample.redex.MainActivity",
                "summary": "target found",
            },
        }
    )
    web_app.apply_agent_event(
        {
            "type": "tool_result",
            "name": "adb_devices",
            "result": {
                "success": True,
                "devices": [
                    {
                        "serial": "f91e097e",
                        "state": "device",
                        "manufacturer": "Xiaomi",
                        "model": "M2102K1AC",
                        "android_version": "14",
                        "sdk": 34,
                        "abi": "arm64-v8a",
                    }
                ],
                "summary": "one device",
            },
        }
    )
    bottleneck = {
        "reason": "choreographer_do_frame",
        "label": "首帧 Choreographer exclusive 归因",
        "duration_ms": 91.405,
        "percentage_of_startup": 29.4,
    }
    web_app.apply_agent_event(
        {
            "type": "tool_result",
            "name": "analyze_perfetto_trace",
            "result": {
                "success": True,
                "startup_duration_ms": 308.0,
                "startup_type": "cold",
                "trace_health": "OK",
                "top_bottlenecks": [bottleneck],
                "summary": "analyzed",
            },
        }
    )
    recommendation = {
        "category": "FIRST_FRAME_WORK",
        "severity": "HIGH",
        "evidence": "91.405 ms",
        "suggestion": "检查首帧工作",
    }
    web_app.apply_agent_event(
        {
            "type": "tool_result",
            "name": "generate_startup_optimization_plan",
            "result": {
                "success": True,
                "recommendations": [recommendation],
                "priority_order": [],
                "summary": "planned",
            },
        }
    )

    dashboard = web_app.state["dashboard"]
    assert dashboard["project"]["application_id"] == "com.sample.redex"
    assert dashboard["device"]["model"] == "M2102K1AC"
    assert dashboard["analyze"]["top_bottlenecks"] == [bottleneck]
    assert dashboard["plan"]["recommendations"] == [recommendation]


def test_dashboard_html_uses_real_perfetto_bottleneck_fields() -> None:
    html = web_app.index_html().decode("utf-8")

    assert "item.label||item.reason" in html
    assert "item.percentage_of_startup" in html
    assert "Incomplete · Max Steps Reached" in html
    assert ".step.stopped" in html
    assert 'blocked:"Blocked"' in html
    assert ".step.skipped" in html


def test_failed_target_preparation_blocks_measure_and_skips_downstream() -> None:
    reset()

    web_app.apply_agent_event(
        {"type": "tool_started", "name": "prepare_benchmark_target"}
    )
    web_app.apply_agent_event(
        {
            "type": "tool_result",
            "name": "prepare_benchmark_target",
            "result": {
                "success": False,
                "selected_variant": None,
                "selected_apk": None,
                "candidates_checked": 3,
                "candidate_results": [],
                "error_type": "NO_BENCHMARK_READY_TARGET",
                "summary": "所有候选均不满足 readiness。",
            },
        }
    )

    dashboard = web_app.state["dashboard"]
    assert web_app.state["blocked"] is True
    assert dashboard["measure"]["status"] == "blocked"
    assert dashboard["analyze"]["status"] == "skipped"
    assert dashboard["plan"]["status"] == "skipped"
    assert dashboard["locate"]["status"] == "skipped"


def test_successful_target_preparation_records_selection_and_unblocks() -> None:
    reset()
    with web_app.lock:
        web_app.state["blocked"] = True
        web_app.state["dashboard"]["analyze"]["status"] = "skipped"
        web_app.state["dashboard"]["plan"]["status"] = "skipped"
        web_app.state["dashboard"]["locate"]["status"] = "skipped"

    web_app.apply_agent_event(
        {
            "type": "tool_result",
            "name": "prepare_benchmark_target",
            "result": {
                "success": True,
                "selected_variant": "XiaomiRelease",
                "selected_apk": "/project/app-release.apk",
                "candidates_checked": 2,
                "candidate_results": [{"variant": "HuaweiRelease"}],
                "summary": "ready",
            },
        }
    )

    dashboard = web_app.state["dashboard"]
    assert web_app.state["blocked"] is False
    assert dashboard["measure"]["status"] == "pending"
    assert dashboard["measure"]["selected_variant"] == "XiaomiRelease"
    assert dashboard["measure"]["candidates_checked"] == 2
    assert dashboard["analyze"]["status"] == "pending"
