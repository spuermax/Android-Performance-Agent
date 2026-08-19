from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.base import ToolError
from tools.perfetto_analysis_tool import AnalyzePerfettoTraceTool


PROCESSOR = Path("/opt/perfetto/trace_processor_shell")
CSV_HEADER = (
    '"section","name","duration_ms","event_count","tid","value","rank"\n'
)


def create_trace(tmp_path: Path) -> Path:
    trace = tmp_path / "startup.perfetto-trace"
    trace.write_bytes(b"perfetto trace")
    return trace


def create_tool(tmp_path: Path) -> AnalyzePerfettoTraceTool:
    return AnalyzePerfettoTraceTool(allowed_project_path=tmp_path)


def arguments(trace: Path, package_name: str = "com.example.app") -> dict[str, str]:
    return {"trace_file": str(trace), "package_name": package_name}


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


def normal_csv() -> str:
    return CSV_HEADER + """\
"trace_meta","com.example.app",,1,"[NULL]","[NULL]",1
"startup","com.example.app",308.5,1,"[NULL]","cold",51
"main_thread","com.example.app",,"1234",1234,"735",0
"breakdown","choreographer_do_frame",90.0,10,"[NULL]","[NULL]",1
"breakdown","binder",40.0,8,"[NULL]","[NULL]",2
"breakdown","io",30.0,6,"[NULL]","[NULL]",3
"breakdown","Running",50.0,12,"[NULL]","[NULL]",4
"breakdown","R",5.0,2,"[NULL]","[NULL]",5
"long_main_slice","bindApplication",35.0,1,1234,"com.example.app",1
"binder_slice","binder transaction",8.0,1,1234,"com.example.app",1
"gc_event","young concurrent copying",4.0,1,1250,"com.example.app",1
"process_cpu","app_process_running",180.0,40,"[NULL]","[NULL]",0
"application","bindApplication",35.0,1,1234,"com.example.app",1
"content_provider","installProvider",2.0,1,1234,"com.example.app",1
"""


def mock_processor(monkeypatch, stdout: str, *, returncode: int = 0):
    monkeypatch.setattr(
        AnalyzePerfettoTraceTool,
        "_find_trace_processor",
        staticmethod(lambda: PROCESSOR),
    )
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return completed(stdout, returncode, "SQL error" if returncode else "")

    monkeypatch.setattr(
        "tools.perfetto_analysis_tool.subprocess.run",
        fake_run,
    )
    return calls


def test_perfetto_reports_missing_trace(tmp_path: Path) -> None:
    result = create_tool(tmp_path).execute(
        arguments(tmp_path / "missing.perfetto-trace")
    )

    assert result["error_type"] == "TRACE_FILE_NOT_FOUND"


def test_perfetto_reports_missing_trace_processor(
    monkeypatch,
    tmp_path: Path,
) -> None:
    trace = create_trace(tmp_path)
    monkeypatch.setattr(
        AnalyzePerfettoTraceTool,
        "_find_trace_processor",
        staticmethod(lambda: None),
    )

    result = create_tool(tmp_path).execute(arguments(trace))

    assert result["error_type"] == "TRACE_PROCESSOR_NOT_FOUND"


def test_perfetto_reports_sql_failure(monkeypatch, tmp_path: Path) -> None:
    trace = create_trace(tmp_path)
    mock_processor(monkeypatch, "", returncode=1)

    result = create_tool(tmp_path).execute(arguments(trace))

    assert result["error_type"] == "TRACE_PROCESSOR_SQL_FAILED"
    assert result["important_logs"] == ["SQL error"]


def test_perfetto_reports_non_android_startup_trace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    trace = create_trace(tmp_path)
    mock_processor(monkeypatch, CSV_HEADER)

    result = create_tool(tmp_path).execute(arguments(trace))

    assert result["error_type"] == "ANDROID_STARTUP_NOT_FOUND"


def test_perfetto_parses_startup_and_structured_bottlenecks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    trace = create_trace(tmp_path)
    calls = mock_processor(monkeypatch, normal_csv())

    result = create_tool(tmp_path).execute(arguments(trace))

    assert result["success"] is True
    assert result["package_name"] == "com.example.app"
    assert result["startup_type"] == "cold"
    assert result["startup_duration_ms"] == 308.5
    assert result["main_thread"]["tid"] == 1234
    assert result["main_thread"]["running_ms"] == 50.0
    assert result["binder"]["total_blocking_ms"] == 40.0
    assert result["binder"]["top_slices"][0]["duration_ms"] == 8.0
    assert result["io"]["total_blocking_ms"] == 30.0
    assert result["gc"]["event_count"] == 1
    assert result["gc"]["total_wall_overlap_ms"] == 4.0
    assert result["gc"]["events"][0]["wall_overlap_ms"] == 4.0
    assert result["cpu"]["app_process_running_ms"] == 180.0
    assert result["application_initialization"]["detected"] is True
    assert result["content_provider_initialization"]["detected"] is True
    assert result["top_bottlenecks"][0]["reason"] == "choreographer_do_frame"
    assert calls[0][0][0] == str(PROCESSOR)
    assert calls[0][0][1] == "query"
    assert "android.startup.startups" in calls[0][0][3]
    assert "WHERE package = 'com.example.app'" in calls[0][0][3]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 120


def test_perfetto_rejects_trace_outside_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.perfetto-trace"
    outside.write_bytes(b"trace")

    with pytest.raises(ToolError, match="\u62d2\u7edd\u8bbf\u95ee"):
        create_tool(allowed).execute(arguments(outside))


def test_perfetto_validates_trace_suffix(tmp_path: Path) -> None:
    invalid = tmp_path / "trace.bin"
    invalid.write_bytes(b"trace")

    with pytest.raises(ToolError, match="perfetto-trace"):
        create_tool(tmp_path).execute(arguments(invalid))


def test_perfetto_reports_target_startup_not_found(
    monkeypatch,
    tmp_path: Path,
) -> None:
    trace = create_trace(tmp_path)
    output = CSV_HEADER + (
        '"trace_meta","com.example.app",,0,"[NULL]","[NULL]",2\n'
    )
    mock_processor(monkeypatch, output)

    result = create_tool(tmp_path).execute(arguments(trace))

    assert result["error_type"] == "TARGET_STARTUP_NOT_FOUND"
    assert result["package_name"] == "com.example.app"


def test_perfetto_rejects_multiple_target_startups(
    monkeypatch,
    tmp_path: Path,
) -> None:
    trace = create_trace(tmp_path)
    output = CSV_HEADER + """\
"trace_meta","com.example.app",,2,"[NULL]","[NULL]",2
"""
    mock_processor(monkeypatch, output)

    result = create_tool(tmp_path).execute(arguments(trace))

    assert result["error_type"] == "MULTIPLE_TARGET_STARTUPS"


def test_perfetto_validates_package_name(tmp_path: Path) -> None:
    trace = create_trace(tmp_path)

    with pytest.raises(ToolError, match="package_name"):
        create_tool(tmp_path).execute(arguments(trace, "not a package"))
