from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError


class AnalyzePerfettoTraceTool(BaseTool):
    name = "analyze_perfetto_trace"
    description = (
        "使用官方 Perfetto Trace Processor SQL 分析单个 Android 冷启动 "
        ".perfetto-trace，提取真实 App Startup 区间、主线程长 Slice、Binder、"
        "I/O、GC、CPU 调度和首帧阶段，并返回结构化事实供 Agent 解释。"
        "不会让 LLM 读取原始二进制 Trace，也不会修改目标项目。"
    )

    TRACE_PROCESSOR_TIMEOUT_SECONDS = 120
    LONG_MAIN_THREAD_SLICE_MS = 5.0
    MAX_LONG_SLICES = 20
    MAX_DETAIL_SLICES = 10
    MAX_BOTTLENECKS = 8
    TRACE_SUFFIXES = {".perfetto-trace", ".pftrace"}
    PACKAGE_PATTERN = re.compile(
        r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "trace_file": {
                    "type": "string",
                    "description": (
                        "run_macrobenchmark 或 run_standalone_macrobenchmark "
                        "返回的单个 .perfetto-trace 文件绝对路径。"
                    ),
                },
                "package_name": {
                    "type": "string",
                    "description": (
                        "Macrobenchmark Tool 返回的目标 package/applicationId。"
                        "Trace Processor SQL 必须只分析该 package 的 Startup。"
                    ),
                },
            },
            "required": ["trace_file", "package_name"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_trace_file = arguments.get("trace_file")
        package_name = arguments.get("package_name")
        if not isinstance(raw_trace_file, str) or not raw_trace_file.strip():
            raise ToolError("trace_file 必须是非空字符串")
        if not isinstance(package_name, str) or not self.PACKAGE_PATTERN.fullmatch(
            package_name.strip()
        ):
            raise ToolError("package_name 格式不合法")
        package_name = package_name.strip()

        trace_file = Path(raw_trace_file).expanduser().resolve()
        self._validate_trace_path(trace_file)
        if trace_file.suffix.lower() not in self.TRACE_SUFFIXES:
            raise ToolError("trace_file 必须是 .perfetto-trace 或 .pftrace 文件")
        if not trace_file.exists():
            return self._empty_result(
                trace_file=trace_file,
                package_name=package_name,
                error_type="TRACE_FILE_NOT_FOUND",
                summary="指定的 Perfetto Trace 文件不存在。",
            )
        if not trace_file.is_file():
            return self._empty_result(
                trace_file=trace_file,
                package_name=package_name,
                error_type="TRACE_FILE_NOT_FOUND",
                summary="指定的 Perfetto Trace 路径不是普通文件。",
            )

        trace_processor = self._find_trace_processor()
        if trace_processor is None:
            return self._empty_result(
                trace_file=trace_file,
                package_name=package_name,
                error_type="TRACE_PROCESSOR_NOT_FOUND",
                summary=(
                    "未找到官方 trace_processor_shell/trace_processor；"
                    "请安装 Perfetto Trace Processor 并配置 PATH 或 "
                    "TRACE_PROCESSOR_SHELL。"
                ),
            )

        started = time.monotonic()
        query = self._run_query(
            trace_processor,
            trace_file,
            self._analysis_sql(package_name),
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        if query["exception"] == "TimeoutExpired":
            return self._empty_result(
                trace_file=trace_file,
                package_name=package_name,
                trace_processor_path=trace_processor,
                duration_ms=duration_ms,
                error_type="TRACE_PROCESSOR_TIMEOUT",
                summary=(
                    f"Trace Processor SQL 超过 "
                    f"{self.TRACE_PROCESSOR_TIMEOUT_SECONDS} 秒，结果未知。"
                ),
                important_logs=self._important_logs(query["stderr"]),
            )
        if query["exception"] is not None or query["returncode"] != 0:
            return self._empty_result(
                trace_file=trace_file,
                package_name=package_name,
                trace_processor_path=trace_processor,
                duration_ms=duration_ms,
                error_type="TRACE_PROCESSOR_SQL_FAILED",
                summary="Trace Processor 无法完成 Android Startup SQL 查询。",
                important_logs=self._important_logs(query["stderr"]),
            )

        try:
            rows = self._parse_csv(query["stdout"])
        except (csv.Error, ValueError) as exc:
            return self._empty_result(
                trace_file=trace_file,
                package_name=package_name,
                trace_processor_path=trace_processor,
                duration_ms=duration_ms,
                error_type="TRACE_PROCESSOR_OUTPUT_INVALID",
                summary=f"Trace Processor 输出无法结构化解析：{type(exc).__name__}。",
                important_logs=self._important_logs(query["stderr"]),
            )

        metadata_rows = self._rows(rows, "trace_meta")
        total_startups = (
            self._int(metadata_rows[0].get("rank")) or 0
            if metadata_rows
            else 0
        )
        target_startups = (
            self._int(metadata_rows[0].get("event_count")) or 0
            if metadata_rows
            else 0
        )
        startup_rows = self._rows(rows, "startup")
        if target_startups > 1:
            return self._empty_result(
                trace_file=trace_file,
                package_name=package_name,
                trace_processor_path=trace_processor,
                duration_ms=duration_ms,
                error_type="MULTIPLE_TARGET_STARTUPS",
                summary=(
                    f"Trace 中检测到 {target_startups} 个目标 package "
                    f"{package_name} 的 Startup，Tool 不会自动选择。"
                ),
                important_logs=self._important_logs(query["stderr"]),
            )
        if not startup_rows:
            error_type = (
                "ANDROID_STARTUP_NOT_FOUND"
                if total_startups == 0
                else "TARGET_STARTUP_NOT_FOUND"
            )
            summary = (
                "Trace 中没有检测到 Android App Startup 区间。"
                if total_startups == 0
                else f"Trace 中没有检测到目标 package {package_name} 的 Startup。"
            )
            return self._empty_result(
                trace_file=trace_file,
                package_name=package_name,
                trace_processor_path=trace_processor,
                duration_ms=duration_ms,
                error_type=error_type,
                summary=summary,
                important_logs=self._important_logs(query["stderr"]),
            )

        return self._structured_result(
            trace_file=trace_file,
            requested_package=package_name,
            trace_processor_path=trace_processor,
            duration_ms=duration_ms,
            rows=rows,
            important_logs=self._important_logs(query["stderr"]),
        )

    def _validate_trace_path(self, trace_file: Path) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        allowed_roots = (
            self.allowed_project_path,
            (repository_root / "harness" / "results").resolve(),
        )
        for root in allowed_roots:
            try:
                trace_file.relative_to(root)
                return
            except ValueError:
                continue
        raise ToolError(
            "拒绝访问目标项目或 Agent Harness 结果目录之外的 Trace 文件。"
        )

    @staticmethod
    def _find_trace_processor() -> Path | None:
        configured = os.environ.get("TRACE_PROCESSOR_SHELL")
        if configured:
            candidate = Path(configured).expanduser().resolve()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        for command in ("trace_processor_shell", "trace_processor"):
            found = shutil.which(command)
            if found:
                return Path(found).resolve()
        return None

    @classmethod
    def _run_query(
        cls,
        trace_processor: Path,
        trace_file: Path,
        sql: str,
    ) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                [str(trace_processor), "query", str(trace_file), sql],
                capture_output=True,
                text=True,
                timeout=cls.TRACE_PROCESSOR_TIMEOUT_SECONDS,
                shell=False,
            )
            return {
                "returncode": completed.returncode,
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
                "exception": None,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "returncode": None,
                "stdout": cls._safe_decode(exc.stdout),
                "stderr": cls._safe_decode(exc.stderr),
                "exception": "TimeoutExpired",
            }
        except OSError as exc:
            return {
                "returncode": None,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}",
                "exception": type(exc).__name__,
            }

    @classmethod
    def _analysis_sql(cls, package_name: str) -> str:
        threshold_ns = int(cls.LONG_MAIN_THREAD_SLICE_MS * 1_000_000)
        package_literal = cls._sql_string_literal(package_name)
        return f"""
INCLUDE PERFETTO MODULE android.startup.startups;
INCLUDE PERFETTO MODULE android.startup.startup_breakdowns;
INCLUDE PERFETTO MODULE android.garbage_collection;

WITH
startup_counts AS (
  SELECT COUNT(*) AS total_count,
         SUM(CASE WHEN package = {package_literal} THEN 1 ELSE 0 END)
           AS target_count
  FROM android_startups
),
selected_startup AS (
  SELECT startup_id, ts, dur, package, startup_type
  FROM android_startups
  WHERE package = {package_literal}
    AND (SELECT target_count FROM startup_counts) = 1
  ORDER BY ts, startup_id
  LIMIT 1
),
main_thread AS (
  SELECT t.startup_id, t.tid, t.utid, t.pid, t.thread_name
  FROM android_startup_threads t
  JOIN selected_startup s USING(startup_id)
  WHERE t.is_main_thread
  ORDER BY t.tid
  LIMIT 1
),
breakdown AS (
  SELECT b.reason AS name, SUM(b.dur) / 1e6 AS duration_ms,
         COUNT(*) AS event_count
  FROM android_startup_opinionated_breakdown b
  JOIN selected_startup s USING(startup_id)
  GROUP BY b.reason
),
long_slices AS (
  SELECT a.slice_name AS name, a.slice_dur / 1e6 AS duration_ms,
         a.tid, a.thread_name,
         ROW_NUMBER() OVER (ORDER BY a.slice_dur DESC, a.slice_id) AS rank
  FROM android_thread_slices_for_all_startups a
  JOIN selected_startup s USING(startup_id)
  WHERE a.is_main_thread AND a.slice_dur >= {threshold_ns}
),
binder_slices AS (
  SELECT a.slice_name AS name, a.slice_dur / 1e6 AS duration_ms,
         a.tid, a.thread_name,
         ROW_NUMBER() OVER (ORDER BY a.slice_dur DESC, a.slice_id) AS rank
  FROM android_thread_slices_for_all_startups a
  JOIN selected_startup s USING(startup_id)
  WHERE a.is_main_thread AND LOWER(a.slice_name) GLOB 'binder*'
),
initialization_slices AS (
  SELECT a.slice_name AS name, a.slice_dur / 1e6 AS duration_ms,
         a.tid, a.thread_name,
         CASE
           WHEN LOWER(a.slice_name) LIKE '%provider%' THEN 'content_provider'
           ELSE 'application'
         END AS kind,
         ROW_NUMBER() OVER (
           PARTITION BY CASE
             WHEN LOWER(a.slice_name) LIKE '%provider%' THEN 'content_provider'
             ELSE 'application'
           END
           ORDER BY a.slice_dur DESC, a.slice_id
         ) AS rank
  FROM android_thread_slices_for_all_startups a
  JOIN selected_startup s USING(startup_id)
  WHERE a.is_main_thread AND (
    LOWER(a.slice_name) LIKE '%application%oncreate%'
    OR LOWER(a.slice_name) = 'bindapplication'
    OR LOWER(a.slice_name) LIKE '%provider%'
  )
),
gc_events AS (
  -- gc_dur is GC wall duration. This computes interval overlap, not STW pause.
  SELECT g.gc_type AS name,
         (MIN(g.gc_ts + g.gc_dur, s.ts + s.dur) - MAX(g.gc_ts, s.ts)) / 1e6
           AS duration_ms,
         g.tid, g.process_name,
         ROW_NUMBER() OVER (ORDER BY g.gc_dur DESC, g.gc_id) AS rank
  FROM android_garbage_collection_events g
  JOIN android_startup_processes p ON p.upid = g.upid
  JOIN selected_startup s ON s.startup_id = p.startup_id
  WHERE g.gc_ts < s.ts + s.dur AND g.gc_ts + g.gc_dur > s.ts
),
process_cpu AS (
  SELECT SUM(
           MIN(sc.ts + sc.dur, s.ts + s.dur) - MAX(sc.ts, s.ts)
         ) / 1e6 AS duration_ms,
         COUNT(*) AS event_count
  FROM sched_slice sc
  JOIN android_startup_threads t ON t.utid = sc.utid
  JOIN selected_startup s ON s.startup_id = t.startup_id
  WHERE sc.ts < s.ts + s.dur AND sc.ts + sc.dur > s.ts
)
SELECT * FROM (
  SELECT 'trace_meta' AS section, {package_literal} AS name,
         NULL AS duration_ms, c.target_count AS event_count,
         NULL AS tid, NULL AS value, c.total_count AS rank
  FROM startup_counts c
  UNION ALL
  SELECT 'startup' AS section, s.package AS name, s.dur / 1e6 AS duration_ms,
         (SELECT target_count FROM startup_counts) AS event_count,
         NULL AS tid, s.startup_type AS value, s.startup_id AS rank
  FROM selected_startup s
  UNION ALL
  SELECT 'main_thread', m.thread_name, NULL, m.pid, m.tid,
         CAST(m.utid AS STRING), 0
  FROM main_thread m
  UNION ALL
  SELECT 'breakdown', b.name, b.duration_ms, b.event_count, NULL, NULL,
         ROW_NUMBER() OVER (ORDER BY b.duration_ms DESC, b.name)
  FROM breakdown b
  UNION ALL
  SELECT 'long_main_slice', l.name, l.duration_ms, 1, l.tid,
         l.thread_name, l.rank
  FROM long_slices l WHERE l.rank <= {cls.MAX_LONG_SLICES}
  UNION ALL
  SELECT 'binder_slice', b.name, b.duration_ms, 1, b.tid,
         b.thread_name, b.rank
  FROM binder_slices b WHERE b.rank <= {cls.MAX_DETAIL_SLICES}
  UNION ALL
  SELECT 'gc_event', g.name, g.duration_ms, 1, g.tid,
         g.process_name, g.rank
  FROM gc_events g WHERE g.rank <= {cls.MAX_DETAIL_SLICES}
  UNION ALL
  SELECT 'process_cpu', 'app_process_running', p.duration_ms, p.event_count,
         NULL, NULL, 0
  FROM process_cpu p
  UNION ALL
  SELECT i.kind, i.name, i.duration_ms, 1, i.tid, i.thread_name, i.rank
  FROM initialization_slices i WHERE i.rank <= {cls.MAX_DETAIL_SLICES}
)
ORDER BY section, rank;
""".strip()

    @staticmethod
    def _parse_csv(output: str) -> list[dict[str, str | None]]:
        if not output.strip():
            raise ValueError("empty Trace Processor output")
        reader = csv.DictReader(io.StringIO(output.lstrip()))
        expected = {
            "section",
            "name",
            "duration_ms",
            "event_count",
            "tid",
            "value",
            "rank",
        }
        if reader.fieldnames is None or set(reader.fieldnames) != expected:
            raise ValueError("unexpected Trace Processor CSV columns")
        return [dict(row) for row in reader]

    @staticmethod
    def _rows(
        rows: list[dict[str, str | None]],
        section: str,
    ) -> list[dict[str, str | None]]:
        return [row for row in rows if row.get("section") == section]

    @classmethod
    def _structured_result(
        cls,
        *,
        trace_file: Path,
        requested_package: str,
        trace_processor_path: Path,
        duration_ms: int,
        rows: list[dict[str, str | None]],
        important_logs: list[str],
    ) -> dict[str, Any]:
        startup = cls._rows(rows, "startup")[0]
        startup_duration_ms = cls._float(startup.get("duration_ms")) or 0.0
        metadata = cls._rows(rows, "trace_meta")[0]
        startup_count = cls._int(metadata.get("rank")) or 0
        target_startup_count = cls._int(metadata.get("event_count")) or 0
        main_rows = cls._rows(rows, "main_thread")
        main = main_rows[0] if main_rows else {}
        breakdown_rows = cls._rows(rows, "breakdown")
        breakdown = {
            row.get("name") or "unknown": {
                "duration_ms": cls._float(row.get("duration_ms")) or 0.0,
                "event_count": cls._int(row.get("event_count")) or 0,
            }
            for row in breakdown_rows
        }

        long_slices = cls._slice_rows(cls._rows(rows, "long_main_slice"))
        binder_slices = cls._slice_rows(cls._rows(rows, "binder_slice"))
        gc_events = cls._gc_rows(cls._rows(rows, "gc_event"))
        app_init = cls._slice_rows(cls._rows(rows, "application"))
        provider_init = cls._slice_rows(cls._rows(rows, "content_provider"))
        process_cpu_rows = cls._rows(rows, "process_cpu")
        process_cpu_ms = (
            cls._float(process_cpu_rows[0].get("duration_ms")) or 0.0
            if process_cpu_rows
            else 0.0
        )

        running_ms = cls._breakdown_total(breakdown, "Running")
        runnable_ms = cls._breakdown_total(breakdown, "R", "R+")
        io_ms = cls._breakdown_total(breakdown, "io")
        binder_ms = cls._breakdown_total(breakdown, "binder")
        gc_total_ms = sum(event["wall_overlap_ms"] for event in gc_events)
        startup_stages = cls._startup_stages(breakdown)
        top_bottlenecks = cls._top_bottlenecks(
            breakdown_rows,
            startup_duration_ms,
        )
        bind_application_ms = cls._breakdown_total(
            breakdown,
            "bind_application",
        )
        class_level_on_create = any(
            "oncreate" in str(item.get("name", "")).lower()
            and str(item.get("name", "")).lower() != "bindapplication"
            for item in app_init
        )
        return {
            "success": True,
            "trace_file": str(trace_file),
            "trace_processor_path": str(trace_processor_path),
            "package_name": requested_package,
            "startup_type": startup.get("value"),
            "startup_duration_ms": startup_duration_ms,
            "startup_count_in_trace": startup_count,
            "target_startup_count": target_startup_count,
            "main_thread": {
                "name": main.get("name"),
                "tid": cls._int(main.get("tid")),
                "pid": cls._int(main.get("event_count")),
                "running_ms": running_ms,
                "runnable_ms": runnable_ms,
                "io_blocked_ms": io_ms,
                "long_slice_threshold_ms": cls.LONG_MAIN_THREAD_SLICE_MS,
                "long_slice_count": len(long_slices),
            },
            "long_main_thread_slices": long_slices,
            "long_main_thread_slices_semantics": {
                "data_source": "android_thread_slices_for_all_startups",
                "duration_kind": "raw_inclusive_slice_duration",
                "may_overlap_or_nest": True,
                "additive": False,
                "usage": "source_localization_only",
            },
            "binder": {
                "total_blocking_ms": binder_ms,
                "event_count": cls._breakdown_count(breakdown, "binder"),
                "top_slices": binder_slices,
            },
            "io": {
                "total_blocking_ms": io_ms,
                "event_count": cls._breakdown_count(breakdown, "io"),
            },
            "gc": {
                "total_wall_overlap_ms": round(gc_total_ms, 6),
                "event_count": len(gc_events),
                "events": gc_events,
            },
            "cpu": {
                "main_thread_running_ms": running_ms,
                "main_thread_runnable_ms": runnable_ms,
                "app_process_running_ms": round(process_cpu_ms, 6),
                "average_running_cores": round(
                    process_cpu_ms / startup_duration_ms, 6
                ) if startup_duration_ms > 0 else None,
            },
            "startup_stages": startup_stages,
            "application_initialization": {
                "detected": bool(app_init),
                "slices": app_init,
                "exclusive_bind_application_ms": bind_application_ms,
                "class_level_on_create_detected": class_level_on_create,
                "attribution": (
                    "bindApplication 是 Framework App binding 父路径；"
                    "其 raw duration 不等于业务 Application.onCreate 耗时。"
                ),
            },
            "content_provider_initialization": {
                "detected": bool(provider_init),
                "slices": provider_init,
            },
            "top_bottlenecks": top_bottlenecks,
            "top_bottlenecks_semantics": {
                "data_source": "android_startup_opinionated_breakdown",
                "duration_kind": "exclusive_startup_attribution",
                "mutually_exclusive": True,
                "usage": "bottleneck_ranking",
            },
            "warnings": [
                "Raw 主线程 Slice 可能嵌套或重叠；禁止累加，"
                "也不得作为相互独立的启动瓶颈排名。"
            ],
            "analysis_duration_ms": duration_ms,
            "error_type": None,
            "summary": (
                f"已从 Perfetto Trace 提取 {requested_package} 的 "
                f"{startup.get('value')} 启动事实，启动区间 "
                f"{startup_duration_ms:.3f} ms。"
            ),
            "important_logs": important_logs,
        }

    @staticmethod
    def _slice_rows(
        rows: list[dict[str, str | None]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "name": row.get("name"),
                    "duration_ms": round(
                        AnalyzePerfettoTraceTool._float(row.get("duration_ms"))
                        or 0.0,
                        6,
                    ),
                    "tid": AnalyzePerfettoTraceTool._int(row.get("tid")),
                    "thread_name": row.get("value"),
                }
            )
        return result

    @staticmethod
    def _gc_rows(
        rows: list[dict[str, str | None]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "gc_type": row.get("name"),
                "wall_overlap_ms": round(
                    AnalyzePerfettoTraceTool._float(row.get("duration_ms"))
                    or 0.0,
                    6,
                ),
                "tid": AnalyzePerfettoTraceTool._int(row.get("tid")),
                "process_name": row.get("value"),
            }
            for row in rows
        ]

    @staticmethod
    def _sql_string_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @classmethod
    def _top_bottlenecks(
        cls,
        breakdown_rows: list[dict[str, str | None]],
        startup_duration_ms: float,
    ) -> list[dict[str, Any]]:
        labels = {
            "binder": "Binder 调用",
            "io": "I/O 等待",
            "Running": "主线程 CPU 运行",
            "R": "主线程可运行等待调度",
            "R+": "主线程抢占/可运行等待",
            "choreographer_do_frame": "首帧 Choreographer exclusive 归因",
            "bind_application": "App binding 启动路径（非业务 onCreate 独占耗时）",
            "activity_start": "Activity 创建",
            "activity_resume": "Activity Resume",
            "inflate": "布局 Inflate",
            "open_dex_files_from_oat": "Dex/OAT 加载",
            "verify_class": "Class Verification",
            "art_lock_contention": "ART 锁竞争",
            "launch_delay": "系统 Launch Delay",
        }
        result: list[dict[str, Any]] = []
        sorted_rows = sorted(
            breakdown_rows,
            key=lambda row: cls._float(row.get("duration_ms")) or 0.0,
            reverse=True,
        )
        for row in sorted_rows[: cls.MAX_BOTTLENECKS]:
            reason = row.get("name") or "unknown"
            metric_ms = cls._float(row.get("duration_ms")) or 0.0
            result.append(
                {
                    "reason": reason,
                    "label": labels.get(reason, reason),
                    "duration_ms": round(metric_ms, 6),
                    "percentage_of_startup": round(
                        metric_ms * 100 / startup_duration_ms,
                        3,
                    ) if startup_duration_ms > 0 else None,
                    "event_count": cls._int(row.get("event_count")) or 0,
                    "duration_kind": "exclusive_startup_attribution",
                }
            )
        return result

    @classmethod
    def _startup_stages(
        cls,
        breakdown: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        stage_reasons = (
            "launch_delay",
            "bind_application",
            "activity_start",
            "activity_resume",
            "inflate",
            "choreographer_do_frame",
            "open_dex_files_from_oat",
            "resources_manager_get_resources",
            "verify_class",
        )
        return [
            {
                "stage": reason,
                "duration_ms": round(breakdown[reason]["duration_ms"], 6),
                "event_count": breakdown[reason]["event_count"],
                "duration_kind": "exclusive_startup_attribution",
            }
            for reason in stage_reasons
            if reason in breakdown
        ]

    @staticmethod
    def _breakdown_total(
        breakdown: dict[str, dict[str, Any]],
        *reasons: str,
    ) -> float:
        return round(
            sum(
                float(breakdown.get(reason, {}).get("duration_ms", 0.0))
                for reason in reasons
            ),
            6,
        )

    @staticmethod
    def _breakdown_count(
        breakdown: dict[str, dict[str, Any]],
        reason: str,
    ) -> int:
        return int(breakdown.get(reason, {}).get("event_count", 0))

    @staticmethod
    def _float(value: str | None) -> float | None:
        if value in (None, "", "[NULL]"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int(value: str | None) -> int | None:
        if value in (None, "", "[NULL]"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_decode(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _important_logs(raw: str) -> list[str]:
        selected = [
            line.strip()[:500]
            for line in raw.splitlines()
            if line.strip()
            and (
                "error" in line.lower()
                or "warning" in line.lower()
                or "health" in line.lower()
                or "Trace loaded" in line
            )
        ]
        return selected[-10:]

    @staticmethod
    def _empty_result(
        *,
        trace_file: Path,
        package_name: str,
        error_type: str,
        summary: str,
        trace_processor_path: Path | None = None,
        duration_ms: int = 0,
        important_logs: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "trace_file": str(trace_file),
            "trace_processor_path": (
                str(trace_processor_path) if trace_processor_path else None
            ),
            "package_name": package_name,
            "startup_type": None,
            "startup_duration_ms": None,
            "startup_count_in_trace": 0,
            "target_startup_count": 0,
            "main_thread": None,
            "long_main_thread_slices": [],
            "long_main_thread_slices_semantics": None,
            "binder": None,
            "io": None,
            "gc": None,
            "cpu": None,
            "startup_stages": [],
            "application_initialization": None,
            "content_provider_initialization": None,
            "top_bottlenecks": [],
            "top_bottlenecks_semantics": None,
            "warnings": [],
            "analysis_duration_ms": duration_ms,
            "error_type": error_type,
            "summary": summary,
            "important_logs": important_logs or [],
        }
