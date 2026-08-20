from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from tools.redaction import redact_text
from web_status import (
    apply_benchmark_readiness_result,
    finish_dashboard_sections,
    resolve_final_status,
)

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
RUN_SH = ROOT / "run.sh"
HOST = "127.0.0.1"
PORT = int(os.environ.get("ANDROID_PERF_WEB_PORT", "8765"))
MAX_REQUEST_BYTES = 64 * 1024
MAX_TASK_CHARS = 20_000
MAX_LOG_LINES = 4_000
STOP_GRACE_SECONDS = 3.0
FORCE_STOP_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
CSRF_TOKEN_PLACEHOLDER = "__ANDROID_PERF_CSRF_TOKEN__"
CSRF_TOKEN = secrets.token_urlsafe(32)
EVENT_PREFIX = "__APA_EVENT__ "

lock = threading.Lock()
shutdown_event = threading.Event()


def empty_dashboard(project_path: str = "") -> dict[str, Any]:
    return {
        "project": {
            "status": "pending",
            "target_confirmed": False,
            "path": project_path,
            "module": None,
            "application_id": None,
            "launcher_activity": None,
            "launcher_component": None,
            "summary": None,
        },
        "device": {
            "status": "pending",
            "serial": None,
            "manufacturer": None,
            "model": None,
            "android_version": None,
            "sdk": None,
            "abi": None,
            "summary": None,
        },
        "measure": {
            "status": "pending",
            "selected_variant": None,
            "selected_apk": None,
            "candidates_discovered": None,
            "candidates_checked": None,
            "candidate_results": [],
            "candidate_progress": None,
            "ttid_ms": None,
            "ttfd_available": False,
            "ttfd_ms": None,
            "run_count": None,
            "trace_files": [],
            "summary": None,
        },
        "analyze": {
            "status": "pending",
            "startup_duration_ms": None,
            "startup_type": None,
            "trace_health": None,
            "trace_health_issues": [],
            "top_bottlenecks": [],
            "summary": None,
        },
        "plan": {
            "status": "pending",
            "recommendations": [],
            "priority_order": [],
            "summary": None,
        },
        "locate": {
            "status": "pending",
            "matches": [],
            "unresolved": [],
            "summary": None,
        },
        "final_text": None,
    }


state: dict[str, Any] = {
    "running": False,
    "status": "idle",
    "blocked": False,
    "stop_requested": False,
    "reached_max_steps": False,
    "pid": None,
    "returncode": None,
    "project_path": "",
    "task": "",
    "current_tool": None,
    "logs": [],
    "dashboard": empty_dashboard(),
}
process: subprocess.Popen[str] | None = None
agent_thread: threading.Thread | None = None


class RequestValidationError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def add_log(line: str) -> None:
    with lock:
        state["logs"].append(redact_text(line.rstrip("\n")))
        state["logs"] = state["logs"][-MAX_LOG_LINES:]


def reserve_agent_run(project_path: str, task: str) -> bool:
    with lock:
        if state["running"]:
            return False
        state.update(
            {
                "running": True,
                "status": "running",
                "blocked": False,
                "stop_requested": False,
                "reached_max_steps": False,
                "pid": None,
                "returncode": None,
                "project_path": project_path,
                "task": task,
                "current_tool": None,
                "logs": [],
                "dashboard": empty_dashboard(project_path),
            }
        )
        return True


def reset_reserved_run(returncode: int = -1) -> None:
    global agent_thread
    with lock:
        agent_thread = None
        state["running"] = False
        state["status"] = "failed"
        state["pid"] = None
        state["returncode"] = returncode


def validate_run_payload(body: Any) -> tuple[str, str, int]:
    if not isinstance(body, dict):
        raise RequestValidationError("JSON 请求体必须是对象")

    project_path = str(body.get("project_path") or "").strip()
    raw_task = body.get("task")
    if not project_path:
        raise RequestValidationError("项目路径不能为空")
    if len(project_path) > 4_096:
        raise RequestValidationError("项目路径过长")
    project = Path(project_path).expanduser()
    if not project.is_dir():
        raise RequestValidationError("项目目录不存在")
    if not isinstance(raw_task, str):
        raise RequestValidationError("任务必须是字符串")
    task = raw_task.strip()
    if not task:
        raise RequestValidationError("任务不能为空")
    if len(task) > MAX_TASK_CHARS:
        raise RequestValidationError(f"任务最多允许 {MAX_TASK_CHARS} 个字符")

    raw_max_steps = body.get("max_steps", 30)
    if isinstance(raw_max_steps, bool):
        raise RequestValidationError("max_steps 必须是 1-50 的整数")
    try:
        max_steps = int(raw_max_steps)
    except (TypeError, ValueError) as exc:
        raise RequestValidationError("max_steps 必须是 1-50 的整数") from exc
    if isinstance(raw_max_steps, float) and not raw_max_steps.is_integer():
        raise RequestValidationError("max_steps 必须是 1-50 的整数")
    if not 1 <= max_steps <= 50:
        raise RequestValidationError("max_steps 必须在 1-50 之间")

    return str(project.resolve()), task, max_steps


def allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def request_is_authorized(
    headers: Mapping[str, str],
    *,
    port: int,
    require_token: bool,
) -> bool:
    host = headers.get("Host", "").lower()
    if host not in allowed_hosts(port):
        return False
    origin = headers.get("Origin")
    if origin:
        allowed_origins = {f"http://{value}" for value in allowed_hosts(port)}
        if origin.lower() not in allowed_origins:
            return False
    if require_token:
        provided = headers.get("X-CSRF-Token", "")
        if not provided or not secrets.compare_digest(provided, CSRF_TOKEN):
            return False
    return True


def index_html() -> bytes:
    content = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    if CSRF_TOKEN_PLACEHOLDER not in content:
        raise RuntimeError("Web UI 缺少 CSRF token placeholder")
    return content.replace(CSRF_TOKEN_PLACEHOLDER, CSRF_TOKEN).encode("utf-8")


def signal_process_group(child: subprocess.Popen[str], sig: signal.Signals) -> bool:
    if child.poll() is not None:
        return False
    try:
        if os.name == "nt":
            child.terminate() if sig == signal.SIGTERM else child.kill()
        else:
            os.killpg(child.pid, sig)
        return True
    except ProcessLookupError:
        return False


def escalate_stop(child: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if child.poll() is None:
        signal_process_group(child, FORCE_STOP_SIGNAL)
        add_log("[Web UI] Agent 未在宽限期内退出，已强制停止。")


def request_agent_stop() -> tuple[bool, str]:
    with lock:
        child = process
        starting = bool(state["running"] and child is None)
    if child is None or child.poll() is not None:
        if starting:
            return False, "Agent 正在启动，请稍后重试"
        return False, "当前没有运行中的 Agent"
    if not signal_process_group(child, signal.SIGTERM):
        return False, "Agent 已结束"
    with lock:
        state["stop_requested"] = True
        state["status"] = "stopping"
    add_log("[Web UI] 已请求停止 Agent。")
    threading.Thread(target=escalate_stop, args=(child,), daemon=True).start()
    return True, ""


def _section_status(result: dict[str, Any]) -> str:
    return "success" if result.get("success") is True else "failed"


def finish_running_dashboard_sections(
    dashboard: dict[str, Any],
    final_status: str,
) -> None:
    """Backward-compatible wrapper for V0.5.2 pipeline finalization."""
    finish_dashboard_sections(dashboard, final_status)


def _first_ready_device(result: dict[str, Any]) -> dict[str, Any] | None:
    devices = result.get("devices")
    if not isinstance(devices, list):
        return None
    for device in devices:
        if isinstance(device, dict) and device.get("state") == "device":
            return device
    return None


def _apply_tool_result(name: str, result: dict[str, Any]) -> None:
    with lock:
        dashboard = state["dashboard"]

        if name == "inspect_project":
            dashboard["project"]["status"] = _section_status(result)
            dashboard["project"]["summary"] = result.get("summary")
            return

        if name == "inspect_app_target":
            section = dashboard["project"]
            success = result.get("success") is True
            if success:
                section.update(
                    {
                        "status": "success",
                        "target_confirmed": True,
                        "module": result.get("module"),
                        "application_id": result.get("application_id"),
                        "launcher_activity": result.get("launcher_activity"),
                        "launcher_component": result.get("launcher_component"),
                        "summary": result.get("summary"),
                    }
                )
            elif not section.get("target_confirmed"):
                section.update(
                    {
                        "status": "failed",
                        "module": result.get("module"),
                        "application_id": result.get("application_id"),
                        "launcher_activity": result.get("launcher_activity"),
                        "launcher_component": result.get("launcher_component"),
                        "summary": result.get("summary"),
                    }
                )
            else:
                section["status"] = "success"
            return

        if name == "adb_devices":
            section = dashboard["device"]
            ready = _first_ready_device(result)
            section["status"] = _section_status(result)
            section["summary"] = result.get("summary")
            if ready is not None:
                for key in (
                    "serial",
                    "manufacturer",
                    "model",
                    "android_version",
                    "sdk",
                    "abi",
                ):
                    section[key] = ready.get(key)
            return

        if name == "inspect_build_variants":
            section = dashboard["measure"]
            success = result.get("success") is True
            section.update(
                {
                    "status": "pending" if success else "blocked",
                    "candidates_discovered": result.get("candidate_count"),
                    "candidate_results": result.get("variants") or [],
                    "summary": result.get("summary"),
                }
            )
            state["blocked"] = not success
            if not success:
                finish_dashboard_sections(dashboard, "blocked")
            return

        if name == "prepare_benchmark_target":
            section = dashboard["measure"]
            success = (
                result.get("success") is True
                and result.get("benchmark_ready") is not False
            )
            section.update(
                {
                    "status": "pending" if success else "blocked",
                    "selected_variant": result.get("selected_variant"),
                    "selected_apk": result.get("selected_apk"),
                    "candidates_discovered": result.get("candidates_discovered"),
                    "candidates_checked": result.get("candidates_checked"),
                    "candidate_results": result.get("candidate_results") or [],
                    "summary": result.get("summary"),
                }
            )
            state["blocked"] = not success
            if success:
                dashboard["project"].update(
                    {
                        "module": result.get("module")
                        or dashboard["project"].get("module"),
                        "application_id": result.get("application_id")
                        or dashboard["project"].get("application_id"),
                        "launcher_component": result.get("launcher_component")
                        or dashboard["project"].get("launcher_component"),
                        "target_confirmed": True,
                    }
                )
                for section_name in ("analyze", "plan", "locate"):
                    if dashboard[section_name].get("status") == "skipped":
                        dashboard[section_name]["status"] = "pending"
            else:
                finish_dashboard_sections(dashboard, "blocked")
            return

        if name == "inspect_benchmark_readiness":
            apply_benchmark_readiness_result(dashboard, result)
            return

        if name in {"run_macrobenchmark", "run_standalone_macrobenchmark"}:
            section = dashboard["measure"]
            section.update(
                {
                    "status": _section_status(result),
                    "ttid_ms": result.get("ttid_ms"),
                    "ttfd_available": bool(result.get("ttfd_available")),
                    "ttfd_ms": result.get("ttfd_ms"),
                    "run_count": result.get("run_count"),
                    "trace_files": result.get("trace_files") or [],
                    "summary": result.get("summary"),
                }
            )
            device = dashboard["device"]
            if result.get("serial") is not None:
                device["serial"] = result.get("serial")
            device_context = result.get("device_context")
            if isinstance(device_context, dict):
                manufacturer = (
                    device_context.get("manufacturer") or device_context.get("brand")
                )
                if manufacturer is not None:
                    device["manufacturer"] = manufacturer
                for key in ("model", "sdk"):
                    if device_context.get(key) is not None:
                        device[key] = device_context.get(key)
            return

        if name == "analyze_perfetto_trace":
            dashboard["analyze"].update(
                {
                    "status": _section_status(result),
                    "startup_duration_ms": result.get("startup_duration_ms"),
                    "startup_type": result.get("startup_type"),
                    "trace_health": result.get("trace_health"),
                    "trace_health_issues": result.get("trace_health_issues") or [],
                    "top_bottlenecks": result.get("top_bottlenecks") or [],
                    "summary": result.get("summary"),
                }
            )
            return

        if name == "generate_startup_optimization_plan":
            dashboard["plan"].update(
                {
                    "status": _section_status(result),
                    "recommendations": result.get("recommendations") or [],
                    "priority_order": result.get("priority_order") or [],
                    "summary": result.get("summary"),
                }
            )
            return

        if name == "locate_startup_bottleneck_source":
            dashboard["locate"].update(
                {
                    "status": _section_status(result),
                    "matches": result.get("matches") or [],
                    "unresolved": result.get("unresolved") or [],
                    "summary": result.get("summary"),
                }
            )


def apply_agent_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    if event_type == "tool_started":
        name = event.get("name")
        with lock:
            state["current_tool"] = name
            if isinstance(name, str):
                stage = {
                    "inspect_project": "project",
                    "inspect_app_target": "project",
                    "adb_devices": "device",
                    "inspect_build_variants": "measure",
                    "prepare_benchmark_target": "measure",
                    "inspect_benchmark_readiness": "measure",
                    "run_macrobenchmark": "measure",
                    "run_standalone_macrobenchmark": "measure",
                    "analyze_perfetto_trace": "analyze",
                    "generate_startup_optimization_plan": "plan",
                    "locate_startup_bottleneck_source": "locate",
                }.get(name)
                if stage is not None:
                    state["dashboard"][stage]["status"] = "running"
                    if name == "prepare_benchmark_target":
                        state["dashboard"][stage]["candidate_progress"] = None
        return

    if event_type == "tool_result":
        name = event.get("name")
        result = event.get("result")
        if isinstance(name, str) and isinstance(result, dict):
            _apply_tool_result(name, result)
        return

    if event_type == "tool_progress":
        if event.get("name") == "prepare_benchmark_target":
            with lock:
                progress = {
                    key: event.get(key)
                    for key in (
                        "candidate_index",
                        "candidate_total",
                        "variant",
                        "status",
                        "error_type",
                    )
                }
                section = state["dashboard"]["measure"]
                section["candidate_progress"] = progress
                section["status"] = "running"
                summary = (
                    f"{progress['candidate_index']} / {progress['candidate_total']} - "
                    f"{progress['variant']} - {progress['status']}"
                )
                if progress.get("error_type"):
                    summary += f" - {progress['error_type']}"
                section["summary"] = summary
                state["current_tool"] = f"prepare_benchmark_target · {summary}"
        return

    if event_type == "final":
        with lock:
            state["dashboard"]["final_text"] = event.get("text")
            state["reached_max_steps"] = event.get("reached_max_steps") is True
        return

    if event_type == "run_failed":
        with lock:
            state["status"] = "failed"


def handle_process_line(line: str) -> None:
    stripped = line.rstrip("\n")
    if stripped.startswith(EVENT_PREFIX):
        payload = stripped[len(EVENT_PREFIX) :]
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            add_log("[Web UI] 无法解析 Agent event：" + payload)
            return
        if isinstance(event, dict):
            apply_agent_event(event)
        return
    add_log(stripped)


def run_agent(project_path: str, task: str, max_steps: int) -> None:
    global agent_thread, process
    child: subprocess.Popen[str] | None = None
    code = -1
    cmd = [
        str(RUN_SH),
        project_path,
        "--task",
        task,
        "--max-steps",
        str(max_steps),
        "--event-stream",
    ]
    add_log("$ " + " ".join(repr(value) for value in cmd))
    try:
        if shutdown_event.is_set():
            raise RuntimeError("Web UI 正在关闭，取消启动 Agent")
        child = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        with lock:
            process = child
            state["pid"] = child.pid
        if shutdown_event.is_set():
            signal_process_group(child, signal.SIGTERM)
        if child.stdout is not None:
            for line in child.stdout:
                handle_process_line(line)
        code = child.wait()
        add_log(f"[Web UI] Agent 进程结束，returncode={code}")
    except Exception as exc:
        add_log(f"[Web UI Error] {type(exc).__name__}: {exc}")
        if child is not None and child.poll() is None:
            signal_process_group(child, signal.SIGTERM)
            escalate_stop(child)
    finally:
        with lock:
            stopped = bool(state["stop_requested"])
            reached_max_steps = bool(state["reached_max_steps"])
            if process is child:
                process = None
            if agent_thread is threading.current_thread():
                agent_thread = None
            state["running"] = False
            state["returncode"] = code
            state["pid"] = None
            state["current_tool"] = None
            final_status = resolve_final_status(
                stopped=stopped,
                reached_max_steps=reached_max_steps,
                returncode=code,
                dashboard=state["dashboard"],
            )
            state["status"] = final_status
            finish_running_dashboard_sections(state["dashboard"], final_status)


def shutdown_agent() -> None:
    shutdown_event.set()
    with lock:
        child = process
        worker = agent_thread
    if child is not None and child.poll() is None:
        signal_process_group(child, signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if child.poll() is None:
            signal_process_group(child, FORCE_STOP_SIGNAL)
    if worker is not None and worker is not threading.current_thread():
        worker.join(timeout=STOP_GRACE_SECONDS + 0.5)
    with lock:
        late_child = process
    if late_child is not None and late_child.poll() is None:
        signal_process_group(late_child, FORCE_STOP_SIGNAL)


def add_security_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    )


def send_json(
    handler: BaseHTTPRequestHandler,
    data: dict[str, Any],
    status: int = 200,
) -> None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    add_security_headers(handler)
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        return

    def _authorized(self, *, require_token: bool = False) -> bool:
        port = int(self.server.server_address[1])
        return request_is_authorized(
            self.headers,
            port=port,
            require_token=require_token,
        )

    def _request_path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type.lower() != "application/json":
            raise RequestValidationError("Content-Type 必须是 application/json", 415)
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise RequestValidationError("Content-Length 不合法") from exc
        if length < 0:
            raise RequestValidationError("Content-Length 不合法")
        if length > MAX_REQUEST_BYTES:
            raise RequestValidationError("请求体过大", 413)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestValidationError("JSON 格式错误") from exc
        if not isinstance(body, dict):
            raise RequestValidationError("JSON 请求体必须是对象")
        return body

    def do_GET(self) -> None:
        if not self._authorized():
            send_json(self, {"ok": False, "error": "拒绝访问"}, 403)
            return
        path = self._request_path()
        if path == "/":
            raw = index_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            add_security_headers(self)
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/status":
            with lock:
                data = dict(state)
                data["logs"] = list(state["logs"])
                data["dashboard"] = json.loads(
                    json.dumps(state["dashboard"], ensure_ascii=False)
                )
            send_json(self, data)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        global agent_thread
        if not self._authorized(require_token=True):
            send_json(self, {"ok": False, "error": "请求来源或 CSRF token 无效"}, 403)
            return
        try:
            body = self._read_json()
        except RequestValidationError as exc:
            send_json(self, {"ok": False, "error": str(exc)}, exc.status)
            return

        path = self._request_path()
        if path == "/api/run":
            try:
                project_path, task, max_steps = validate_run_payload(body)
            except RequestValidationError as exc:
                send_json(self, {"ok": False, "error": str(exc)}, exc.status)
                return
            if not reserve_agent_run(project_path, task):
                send_json(self, {"ok": False, "error": "已有 Agent 正在运行"}, 409)
                return
            worker = threading.Thread(
                target=run_agent,
                args=(project_path, task, max_steps),
                daemon=True,
                name="android-performance-agent",
            )
            with lock:
                agent_thread = worker
            try:
                worker.start()
            except RuntimeError as exc:
                reset_reserved_run()
                send_json(self, {"ok": False, "error": f"无法启动 Agent：{exc}"}, 500)
                return
            send_json(self, {"ok": True})
            return

        if path == "/api/stop":
            stopped, error = request_agent_stop()
            if not stopped:
                send_json(self, {"ok": False, "error": error}, 409)
                return
            send_json(self, {"ok": True})
            return
        self.send_error(404)


def main() -> None:
    if not RUN_SH.is_file():
        raise SystemExit("请把 web_app.py 放到 Android-Performance-Agent 项目根目录。")
    if not (WEB_ROOT / "index.html").is_file():
        raise SystemExit("缺少 web/index.html。")
    shutdown_event.clear()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Android Performance Agent Web UI: {url}")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        shutdown_agent()


if __name__ == "__main__":
    main()
