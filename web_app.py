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

lock = threading.Lock()
shutdown_event = threading.Event()
state: dict[str, Any] = {
    "running": False,
    "pid": None,
    "returncode": None,
    "project_path": "",
    "task": "",
    "logs": [],
}
process: subprocess.Popen[str] | None = None
agent_thread: threading.Thread | None = None


class RequestValidationError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def add_log(line: str) -> None:
    with lock:
        state["logs"].append(line.rstrip("\n"))
        state["logs"] = state["logs"][-MAX_LOG_LINES:]


def reserve_agent_run(project_path: str, task: str) -> bool:
    """Atomically reserve the single Agent slot before starting its thread."""
    with lock:
        if state["running"]:
            return False
        state.update(
            {
                "running": True,
                "pid": None,
                "returncode": None,
                "project_path": project_path,
                "task": task,
                "logs": [],
            }
        )
        return True


def reset_reserved_run(returncode: int = -1) -> None:
    global agent_thread
    with lock:
        agent_thread = None
        state["running"] = False
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

    raw_max_steps = body.get("max_steps", 15)
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
    add_log("[Web UI] 已请求停止 Agent。")
    threading.Thread(target=escalate_stop, args=(child,), daemon=True).start()
    return True, ""


def run_agent(project_path: str, task: str, max_steps: int) -> None:
    global agent_thread, process
    child: subprocess.Popen[str] | None = None
    code = -1
    cmd = [str(RUN_SH), project_path, "--task", task, "--max-steps", str(max_steps)]
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
                add_log(line)
        code = child.wait()
        add_log(f"[Web UI] Agent 进程结束，returncode={code}")
    except Exception as exc:
        add_log(f"[Web UI Error] {type(exc).__name__}: {exc}")
        if child is not None and child.poll() is None:
            signal_process_group(child, signal.SIGTERM)
            escalate_stop(child)
    finally:
        with lock:
            if process is child:
                process = None
            if agent_thread is threading.current_thread():
                agent_thread = None
            state["running"] = False
            state["returncode"] = code
            state["pid"] = None


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
