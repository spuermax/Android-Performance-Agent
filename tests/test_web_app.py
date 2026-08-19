from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import web_app


@pytest.fixture(autouse=True)
def reset_web_state() -> None:
    with web_app.lock:
        web_app.state.update(
            {
                "running": False,
                "status": "idle",
                "stop_requested": False,
                "reached_max_steps": False,
                "pid": None,
                "returncode": None,
                "project_path": "",
                "task": "",
                "current_tool": None,
                "logs": [],
                "dashboard": web_app.empty_dashboard(),
            }
        )
        web_app.process = None
        web_app.agent_thread = None
    web_app.shutdown_event.clear()
    yield
    web_app.shutdown_event.clear()


def test_validate_run_payload(tmp_path: Path) -> None:
    project, task, max_steps = web_app.validate_run_payload(
        {
            "project_path": str(tmp_path),
            "task": "分析启动性能",
            "max_steps": 15,
        }
    )

    assert project == str(tmp_path.resolve())
    assert task == "分析启动性能"
    assert max_steps == 15


@pytest.mark.parametrize("value", ["", "   ", None])
def test_validate_run_payload_rejects_empty_project_path(value: object) -> None:
    with pytest.raises(web_app.RequestValidationError, match="项目路径不能为空"):
        web_app.validate_run_payload(
            {
                "project_path": value,
                "task": "test",
                "max_steps": 15,
            }
        )


@pytest.mark.parametrize("value", [None, "", " ", False])
def test_validate_run_payload_rejects_invalid_task(
    tmp_path: Path,
    value: object,
) -> None:
    with pytest.raises(web_app.RequestValidationError, match="任务"):
        web_app.validate_run_payload(
            {
                "project_path": str(tmp_path),
                "task": value,
                "max_steps": 15,
            }
        )


@pytest.mark.parametrize("value", ["abc", True, 1.5, 0, 51])
def test_validate_run_payload_rejects_invalid_max_steps(
    tmp_path: Path,
    value: object,
) -> None:
    with pytest.raises(web_app.RequestValidationError, match="max_steps"):
        web_app.validate_run_payload(
            {
                "project_path": str(tmp_path),
                "task": "test",
                "max_steps": value,
            }
        )


def test_agent_slot_reservation_is_atomic() -> None:
    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(
            executor.map(
                lambda index: web_app.reserve_agent_run(
                    f"/project/{index}",
                    f"task-{index}",
                ),
                range(32),
            )
        )

    assert results.count(True) == 1
    assert results.count(False) == 31
    assert web_app.state["running"] is True


def test_request_authorization_requires_local_origin_and_csrf_token() -> None:
    valid = {
        "Host": "127.0.0.1:8765",
        "Origin": "http://127.0.0.1:8765",
        "X-CSRF-Token": web_app.CSRF_TOKEN,
    }

    assert web_app.request_is_authorized(
        valid,
        port=8765,
        require_token=True,
    )
    assert not web_app.request_is_authorized(
        {**valid, "Origin": "https://attacker.example"},
        port=8765,
        require_token=True,
    )
    assert not web_app.request_is_authorized(
        {**valid, "Host": "attacker.example"},
        port=8765,
        require_token=True,
    )
    assert not web_app.request_is_authorized(
        {**valid, "X-CSRF-Token": "wrong"},
        port=8765,
        require_token=True,
    )


def test_index_html_injects_csrf_token() -> None:
    html = web_app.index_html().decode("utf-8")

    assert web_app.CSRF_TOKEN in html
    assert web_app.CSRF_TOKEN_PLACEHOLDER not in html


def test_stop_reports_agent_starting_state() -> None:
    assert web_app.reserve_agent_run("/project", "task")

    stopped, error = web_app.request_agent_stop()

    assert stopped is False
    assert "正在启动" in error


def test_log_buffer_is_bounded() -> None:
    for index in range(web_app.MAX_LOG_LINES + 10):
        web_app.add_log(str(index))

    assert len(web_app.state["logs"]) == web_app.MAX_LOG_LINES
    assert web_app.state["logs"][0] == "10"


def test_reset_reserved_run_clears_worker_reference() -> None:
    worker = object()
    with web_app.lock:
        web_app.agent_thread = worker  # type: ignore[assignment]
        web_app.state["running"] = True

    web_app.reset_reserved_run(returncode=-1)

    assert web_app.agent_thread is None
    assert web_app.state["running"] is False
    assert web_app.state["returncode"] == -1


def test_shutdown_signals_active_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        pid = 1234

        def __init__(self) -> None:
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else -15

    child = FakeProcess()
    signals: list[object] = []

    def fake_signal(target: object, sig: object) -> bool:
        assert target is child
        signals.append(sig)
        child.running = False
        return True

    monkeypatch.setattr(web_app, "signal_process_group", fake_signal)
    with web_app.lock:
        web_app.process = child  # type: ignore[assignment]
        web_app.state["running"] = True

    web_app.shutdown_agent()

    assert signals == [web_app.signal.SIGTERM]
    assert web_app.shutdown_event.is_set()


def test_run_agent_terminates_process_group_when_log_reading_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenStdout:
        def __iter__(self) -> object:
            raise UnicodeError("broken output")

    class FakeProcess:
        pid = 1234
        stdout = BrokenStdout()

        def __init__(self) -> None:
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else -15

    child = FakeProcess()
    popen_kwargs: dict[str, object] = {}
    signals: list[object] = []

    def fake_popen(*_args: object, **kwargs: object) -> FakeProcess:
        popen_kwargs.update(kwargs)
        return child

    def fake_signal(target: object, sig: object) -> bool:
        assert target is child
        signals.append(sig)
        child.running = False
        return True

    monkeypatch.setattr(web_app.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(web_app, "signal_process_group", fake_signal)

    web_app.run_agent("/project", "task", 15)

    assert signals == [web_app.signal.SIGTERM]
    assert popen_kwargs["encoding"] == "utf-8"
    assert popen_kwargs["errors"] == "replace"
    assert web_app.process is None
    assert web_app.state["running"] is False
    assert web_app.state["returncode"] == -1


def test_run_agent_reports_requested_stop_as_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 1234
        stdout: list[str] = []

        def poll(self) -> int:
            return -15

        def wait(self) -> int:
            return -15

    monkeypatch.setattr(web_app.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    with web_app.lock:
        web_app.state["running"] = True
        web_app.state["stop_requested"] = True
        web_app.state["dashboard"]["measure"]["status"] = "running"

    web_app.run_agent("/project", "task", 15)

    assert web_app.state["status"] == "stopped"
    assert web_app.state["returncode"] == -15
    assert web_app.state["dashboard"]["measure"]["status"] == "stopped"


def test_run_agent_reports_max_steps_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 1234
        stdout: list[str] = []

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(web_app.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    with web_app.lock:
        web_app.state["running"] = True
        web_app.state["reached_max_steps"] = True

    web_app.run_agent("/project", "task", 15)

    assert web_app.state["status"] == "incomplete"
    assert web_app.state["returncode"] == 0


def test_run_agent_marks_running_section_failed_on_process_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 1234
        stdout: list[str] = []

        def poll(self) -> int:
            return 1

        def wait(self) -> int:
            return 1

    monkeypatch.setattr(web_app.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    with web_app.lock:
        web_app.state["running"] = True
        web_app.state["dashboard"]["analyze"]["status"] = "running"

    web_app.run_agent("/project", "task", 15)

    assert web_app.state["status"] == "failed"
    assert web_app.state["dashboard"]["analyze"]["status"] == "failed"


@pytest.mark.parametrize(
    ("final_status", "expected_section_status"),
    [("stopped", "stopped"), ("failed", "failed")],
)
def test_finish_running_dashboard_sections(
    final_status: str,
    expected_section_status: str,
) -> None:
    dashboard = web_app.empty_dashboard()
    dashboard["measure"]["status"] = "running"
    dashboard["project"]["status"] = "success"

    web_app.finish_running_dashboard_sections(dashboard, final_status)

    assert dashboard["measure"]["status"] == expected_section_status
    assert dashboard["project"]["status"] == "success"
