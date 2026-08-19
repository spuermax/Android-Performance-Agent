from __future__ import annotations

import re
from typing import Any

from tools.base import BaseTool, ToolError


class GenerateStartupOptimizationPlanTool(BaseTool):
    name = "generate_startup_optimization_plan"
    description = (
        "将 analyze_perfetto_trace 的成功结构化结果转换为有证据的"
        "Android 启动优化候选、优先级和验证计划。Tool 不读取原始 "
        "Trace、不修改代码、不执行优化或重新测量；最终解释仍由 LLM 完成。"
    )

    SEVERITY_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    MIN_EVIDENCE_IMPACT_MS = 3.0
    MIN_EVIDENCE_STARTUP_PERCENTAGE = 1.0
    RAW_HINT_TOP_SLICES = 5
    RAW_HINT_MAX_SLICES = 10

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "perfetto_analysis": {
                    "type": "object",
                    "description": (
                        "analyze_perfetto_trace 返回的完整成功 Tool Result。"
                    ),
                }
            },
            "required": ["perfetto_analysis"],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        analysis = arguments.get("perfetto_analysis")
        if not isinstance(analysis, dict):
            raise ToolError("perfetto_analysis 必须是对象")

        validation_error = self._validation_error(analysis)
        if validation_error is not None:
            return self._empty_result(
                error_type="INVALID_PERFETTO_ANALYSIS",
                summary=validation_error,
            )

        startup_duration_ms = self._number(analysis.get("startup_duration_ms"))
        package_name = str(analysis["package_name"])
        candidates: list[dict[str, Any]] = []

        self._application_candidate(
            candidates,
            analysis,
            startup_duration_ms,
        )
        self._provider_candidate(candidates, analysis, startup_duration_ms)
        self._io_candidate(candidates, analysis, startup_duration_ms)
        self._binder_candidate(candidates, analysis, startup_duration_ms)
        self._gc_candidate(candidates, analysis, startup_duration_ms)
        self._cpu_candidate(candidates, analysis, startup_duration_ms)
        self._dex_class_candidate(candidates, analysis, startup_duration_ms)
        self._first_frame_candidate(candidates, analysis, startup_duration_ms)
        source_localization_hints = self._raw_slice_localization_hints(analysis)

        ordered = sorted(
            candidates,
            key=lambda item: (
                -self.SEVERITY_ORDER[item["severity"]],
                -item["impact_ms"],
                item["category"],
            ),
        )
        bottlenecks = [self._bottleneck(item) for item in ordered]
        recommendations = [self._recommendation(item) for item in ordered]
        priority_order = [
            {
                "priority": index,
                "category": item["category"],
                "severity": item["severity"],
                "evidence": item["evidence"],
            }
            for index, item in enumerate(ordered, start=1)
        ]
        verification_plan = [
            {
                "order": index,
                "category": item["category"],
                "verification": item["verification"],
            }
            for index, item in enumerate(ordered, start=1)
        ]

        if not ordered:
            summary = (
                f"{package_name} 的当前 Perfetto 分析没有达到已定义证据阈值的"
                "启动瓶颈，Tool 不生成泛化优化建议。"
            )
        else:
            summary = (
                f"已根据 {package_name} 的 Perfetto 事实生成 "
                f"{len(ordered)} 类启动优化候选，建议按证据优先级验证。"
            )

        return {
            "success": True,
            "package_name": package_name,
            "startup_duration_ms": startup_duration_ms,
            "bottlenecks": bottlenecks,
            "recommendations": recommendations,
            "priority_order": priority_order,
            "verification_plan": verification_plan,
            "source_localization_hints": source_localization_hints,
            "evidence_threshold": {
                "minimum_impact_ms": self.MIN_EVIDENCE_IMPACT_MS,
                "minimum_startup_percentage": (
                    self.MIN_EVIDENCE_STARTUP_PERCENTAGE
                ),
                "rule": "impact_ms >= 3 OR percentage_of_startup >= 1%",
            },
            "error_type": None,
            "summary": summary,
        }

    @classmethod
    def _validation_error(cls, analysis: dict[str, Any]) -> str | None:
        if analysis.get("success") is not True:
            return "perfetto_analysis 不是成功的 Trace 分析结果。"
        package_name = analysis.get("package_name")
        if not isinstance(package_name, str) or not package_name.strip():
            return "perfetto_analysis 缺少 package_name。"
        duration = cls._number(analysis.get("startup_duration_ms"))
        if duration <= 0:
            return "perfetto_analysis 缺少有效 startup_duration_ms。"
        required_objects = (
            "application_initialization",
            "content_provider_initialization",
            "io",
            "binder",
            "gc",
            "cpu",
        )
        if any(not isinstance(analysis.get(field), dict) for field in required_objects):
            return "perfetto_analysis 缺少必要的结构化性能字段。"
        if not isinstance(analysis.get("long_main_thread_slices"), list):
            return "perfetto_analysis 缺少 long_main_thread_slices。"
        return None

    @classmethod
    def _application_candidate(
        cls,
        output: list[dict[str, Any]],
        analysis: dict[str, Any],
        startup_ms: float,
    ) -> None:
        data = analysis["application_initialization"]
        if data.get("detected") is not True:
            return
        slices = cls._valid_slices(data.get("slices"))
        class_slices = [
            item
            for item in slices
            if "oncreate" in item["name"].lower()
            and item["name"].lower() != "bindapplication"
        ]
        exclusive = cls._reason_metric(analysis, "bind_application")
        if exclusive is not None:
            impact_ms = exclusive["duration_ms"]
            raw_bind = [
                item for item in slices
                if item["name"].lower() == "bindapplication"
            ]
            raw_detail = cls._slice_evidence("Raw bindApplication 父 Slice", raw_bind)
            class_note = (
                cls._slice_evidence("类级 Application.onCreate Slice", class_slices)
                if class_slices
                else "当前 Trace 未提供业务 Application.onCreate 类级耗时证据。"
            )
            evidence = (
                f"启动 exclusive breakdown: bind_application "
                f"{impact_ms:.3f} ms；{raw_detail} {class_note} "
                "Raw 父 Slice 可能包含嵌套工作，不与 exclusive 时长累加。"
            )
        elif class_slices:
            impact_ms = max(item["duration_ms"] for item in class_slices)
            evidence = (
                cls._slice_evidence("类级 Application.onCreate raw Slice", class_slices)
                + "该 raw 时长可能包含嵌套工作，仅作定位证据。"
            )
        else:
            return
        if not cls._meets_min_evidence(impact_ms, startup_ms):
            return
        severity = cls._severity(impact_ms, startup_ms, high_ms=30, medium_ms=10)
        cls._append(
            output,
            category="APPLICATION_INITIALIZATION",
            severity=severity,
            impact_ms=impact_ms,
            evidence=evidence,
            reason=(
                "证据指向 Framework App binding/Application 启动路径；"
                "除非 Trace 明确给出类级 Slice，否则不能把该时长归因给业务 "
                "Application.onCreate。"
            ),
            suggestion=(
                "盘点 Application 中的同步任务：延迟非必要初始化、"
                "改为按需初始化，并将不影响首帧的第三方 SDK 延后。"
            ),
            expected_impact="减少首帧前 App binding/Application 启动路径中的业务工作。",
            verification=(
                "修改后重新执行相同 Macrobenchmark，对比 TTID 与 "
                "bind_application/Application Slice 时长。"
            ),
        )

    @classmethod
    def _provider_candidate(
        cls,
        output: list[dict[str, Any]],
        analysis: dict[str, Any],
        startup_ms: float,
    ) -> None:
        data = analysis["content_provider_initialization"]
        if data.get("detected") is not True:
            return
        slices = cls._valid_slices(data.get("slices"))
        impact_ms = sum(item["duration_ms"] for item in slices)
        if not cls._meets_min_evidence(impact_ms, startup_ms):
            return
        severity = cls._severity(impact_ms, startup_ms, high_ms=20, medium_ms=5)
        cls._append(
            output,
            category="CONTENT_PROVIDER_INITIALIZATION",
            severity=severity,
            impact_ms=impact_ms,
            evidence=cls._slice_evidence("ContentProvider Slice", slices),
            reason="ContentProvider 会在 Application.onCreate 前自动初始化。",
            suggestion=(
                "检查第三方 SDK Auto Init Provider；仅在业务允许且依赖官方"
                "支持时禁用非必要自动初始化，并改为延迟或按需初始化。"
            ),
            expected_impact="减少 Application 启动前的 Provider 同步工作。",
            verification=(
                "修改后重新执行 Macrobenchmark，确认目标 Provider Slice "
                "减少且 App 功能与 SDK 初始化时序正常。"
            ),
        )

    @classmethod
    def _io_candidate(
        cls,
        output: list[dict[str, Any]],
        analysis: dict[str, Any],
        startup_ms: float,
    ) -> None:
        data = analysis["io"]
        impact_ms = cls._number(data.get("total_blocking_ms"))
        if not cls._meets_min_evidence(impact_ms, startup_ms):
            return
        cls._append(
            output,
            category="MAIN_THREAD_IO",
            severity=cls._severity(impact_ms, startup_ms, 30, 10),
            impact_ms=impact_ms,
            evidence=(
                f"启动区间主线程 I/O blocking {impact_ms:.3f} ms，"
                f"{cls._integer(data.get('event_count'))} 个区间。"
            ),
            reason="主线程等待 I/O 会直接延长启动关键路径。",
            suggestion=(
                "定位对应长 Slice，将非必要文件读取移出主线程，"
                "延后数据库初始化，并减少首帧前 SharedPreferences/配置读取。"
            ),
            expected_impact="降低主线程 I/O 阻塞和启动等待时间。",
            verification=(
                "重新执行 Macrobenchmark，对比 TTID 与 "
                "io.total_blocking_ms，并确认数据时序正确。"
            ),
        )

    @classmethod
    def _binder_candidate(
        cls,
        output: list[dict[str, Any]],
        analysis: dict[str, Any],
        startup_ms: float,
    ) -> None:
        data = analysis["binder"]
        impact_ms = cls._number(data.get("total_blocking_ms"))
        if not cls._meets_min_evidence(impact_ms, startup_ms):
            return
        top_slices = cls._valid_slices(data.get("top_slices"))
        detail = cls._slice_evidence("最长 Binder Slice", top_slices[:3])
        cls._append(
            output,
            category="BINDER_IPC",
            severity=cls._severity(impact_ms, startup_ms, 30, 10),
            impact_ms=impact_ms,
            evidence=(
                f"启动区间 Binder blocking {impact_ms:.3f} ms，"
                f"{cls._integer(data.get('event_count'))} 个区间；{detail}"
            ),
            reason="启动主线程同步 IPC 会等待系统或远程服务响应。",
            suggestion=(
                "从最长 Binder Slice 反查调用点，减少启动阶段同步 IPC，"
                "并延后不影响首帧的系统服务或远程服务调用。"
            ),
            expected_impact="减少主线程等待 Binder 响应的时间。",
            verification=(
                "重新执行 Macrobenchmark，对比 TTID、"
                "binder.total_blocking_ms 和最长 Binder Slice。"
            ),
        )

    @classmethod
    def _gc_candidate(
        cls,
        output: list[dict[str, Any]],
        analysis: dict[str, Any],
        startup_ms: float,
    ) -> None:
        data = analysis["gc"]
        impact_ms = cls._number(data.get("total_wall_overlap_ms"))
        event_count = cls._integer(data.get("event_count"))
        if event_count <= 0 or not cls._meets_min_evidence(impact_ms, startup_ms):
            return
        cls._append(
            output,
            category="STARTUP_GC_ALLOCATION",
            severity=cls._severity(impact_ms, startup_ms, 20, 5),
            impact_ms=impact_ms,
            evidence=(
                f"{event_count} 次 GC 与 Startup 区间的 wall duration 重叠 "
                f"{impact_ms:.3f} ms；该数值不等于 STW pause。"
            ),
            reason=(
                "启动区间出现 GC 表明当时存在分配压力，"
                "但仅凭 GC wall duration 不能断言主线程停顿时长。"
            ),
            suggestion=(
                "检查启动关键路径的对象、大对象、Bitmap、JSON 与集合分配，"
                "减少可避免的短命对象；不要将该 wall 指标直接当作 STW 停顿。"
            ),
            expected_impact="降低启动分配压力与触发 GC 的概率。",
            verification=(
                "重新执行 Macrobenchmark，对比 GC event_count 与 "
                "total_wall_overlap_ms；需要停顿结论时应另查明确 STW 证据。"
            ),
        )

    @classmethod
    def _cpu_candidate(
        cls,
        output: list[dict[str, Any]],
        analysis: dict[str, Any],
        startup_ms: float,
    ) -> None:
        data = analysis["cpu"]
        running_ms = cls._number(data.get("main_thread_running_ms"))
        runnable_ms = cls._number(data.get("main_thread_runnable_ms"))
        impact_ms = running_ms + runnable_ms
        if not cls._meets_min_evidence(impact_ms, startup_ms):
            return
        cls._append(
            output,
            category="MAIN_THREAD_CPU_SCHEDULING",
            severity=cls._severity(impact_ms, startup_ms, 50, 20),
            impact_ms=impact_ms,
            evidence=(
                f"主线程 Running {running_ms:.3f} ms，Runnable 等待 "
                f"{runnable_ms:.3f} ms，App 进程 CPU "
                f"{cls._number(data.get('app_process_running_ms')):.3f} ms。"
            ),
            reason="启动关键路径存在主线程 CPU 工作或调度等待。",
            suggestion=(
                "优先沿主线程最长 Slice 定位可拆分的同步计算，"
                "减少首帧前不必要工作；不要盲目新建线程而引入 CPU 竞争。"
            ),
            expected_impact="缩短主线程启动关键路径和调度等待。",
            verification=(
                "重新执行 Macrobenchmark，对比 TTID、主线程 "
                "Running/Runnable 时长与相关长 Slice。"
            ),
        )

    @classmethod
    def _raw_slice_localization_hints(
        cls,
        analysis: dict[str, Any],
    ) -> list[dict[str, Any]]:
        slices = cls._valid_slices(analysis.get("long_main_thread_slices"))
        if not slices:
            return []
        selected_slices = cls._select_raw_localization_slices(slices)
        return [
            {
                "category": "LONG_MAIN_THREAD_TASK",
                "evidence": (
                    cls._slice_evidence(
                        "Raw 主线程嵌套 Slice",
                        selected_slices,
                        max_items=cls.RAW_HINT_MAX_SLICES,
                    )
                    + "这些 inclusive 时长可能重叠或嵌套，禁止相加，"
                    "也不作为独立瓶颈排名；仅用于源码定位。"
                ),
                "duration_kind": "raw_inclusive_slice_duration",
                "ranking_eligible": False,
                "selection": (
                    "top_raw_slices_plus_source_identifier_slices"
                ),
            }
        ]

    @classmethod
    def _select_raw_localization_slices(
        cls,
        slices: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        selected = list(slices[: cls.RAW_HINT_TOP_SLICES])
        source_slices = sorted(
            (item for item in slices if cls._source_identifier_priority(item["name"])),
            key=lambda item: (
                -cls._source_identifier_priority(item["name"]),
                -item["duration_ms"],
            ),
        )
        seen = {(item["name"], item["duration_ms"]) for item in selected}
        for item in source_slices:
            key = (item["name"], item["duration_ms"])
            if key in seen:
                continue
            selected.append(item)
            seen.add(key)
            if len(selected) >= cls.RAW_HINT_MAX_SLICES:
                break
        return selected

    @staticmethod
    def _source_identifier_priority(name: str) -> int:
        if re.search(r"\b(?:[a-zA-Z_]\w*\.){2,}[A-Z]\w*\b", name):
            return 2
        if re.search(r"\b[A-Z][A-Za-z0-9_]*(?:[#.:][A-Za-z_]\w+)+", name):
            return 1
        return 0

    @classmethod
    def _dex_class_candidate(
        cls,
        output: list[dict[str, Any]],
        analysis: dict[str, Any],
        startup_ms: float,
    ) -> None:
        matched = [
            metric
            for reason in ("open_dex_files_from_oat", "verify_class")
            if (metric := cls._reason_metric(analysis, reason)) is not None
        ]
        if not matched:
            return
        impact_ms = sum(metric["duration_ms"] for metric in matched)
        if not cls._meets_min_evidence(impact_ms, startup_ms):
            return
        evidence = ", ".join(
            f"{metric['reason']} {metric['duration_ms']:.3f} ms"
            for metric in matched
        )
        cls._append(
            output,
            category="DEX_CLASS_LOADING",
            severity=cls._severity(impact_ms, startup_ms, 20, 5),
            impact_ms=impact_ms,
            evidence=evidence,
            reason="Dex/OAT 打开或 Class Verification 位于启动区间。",
            suggestion=(
                "先减少启动阶段不必要类加载；如该证据在多次 Trace 中稳定，"
                "后续再评估 Baseline Profile 和 Startup Profile，当前版本不生成 Profile。"
            ),
            expected_impact="降低启动阶段 Dex/Class 加载与校验开销。",
            verification=(
                "修改后重新执行 Macrobenchmark，对比 "
                "open_dex_files_from_oat/verify_class 与 TTID。"
            ),
        )

    @classmethod
    def _first_frame_candidate(
        cls,
        output: list[dict[str, Any]],
        analysis: dict[str, Any],
        startup_ms: float,
    ) -> None:
        frame = cls._reason_metric(analysis, "choreographer_do_frame")
        if frame is None:
            return
        impact_ms = frame["duration_ms"]
        if not cls._meets_min_evidence(impact_ms, startup_ms):
            return
        raw_frame_slices = [
            item
            for item in cls._valid_slices(analysis.get("long_main_thread_slices"))
            if any(
                token in item["name"].lower()
                for token in ("choreographer", "doframe", "traversal")
            )
        ]
        raw_note = (
            " "
            + cls._slice_evidence("Raw 首帧定位 Slice", raw_frame_slices)
            + "这些 raw inclusive Slice 可能嵌套，不与 exclusive breakdown 累加。"
            if raw_frame_slices
            else ""
        )
        cls._append(
            output,
            category="FIRST_FRAME_WORK",
            severity=cls._severity(impact_ms, startup_ms, 50, 16),
            impact_ms=impact_ms,
            evidence=(
                f"choreographer_do_frame 在启动独占分解中累计 "
                f"{impact_ms:.3f} ms，{frame['event_count']} 个唯一归因区间。"
                f"{raw_note}"
            ),
            reason="首帧布局、测量、绘制或首帧前附加工作占用明显时间。",
            suggestion=(
                "检查首屏布局层级、inflate、measure/layout/draw 及首帧前同步任务，"
                "延后非首屏内容和不影响第一次绘制的工作。"
            ),
            expected_impact="缩短首帧生成路径并改善 TTID。",
            verification=(
                "重新执行 Macrobenchmark，对比 TTID、"
                "choreographer_do_frame 和最长 traversal/inflate/draw Slice。"
            ),
        )

    @staticmethod
    def _append(
        output: list[dict[str, Any]],
        *,
        category: str,
        severity: str,
        impact_ms: float,
        evidence: str,
        reason: str,
        suggestion: str,
        expected_impact: str,
        verification: str,
    ) -> None:
        output.append(
            {
                "category": category,
                "severity": severity,
                "impact_ms": round(impact_ms, 6),
                "evidence": evidence,
                "reason": reason,
                "suggestion": suggestion,
                "expected_impact": expected_impact,
                "verification": verification,
            }
        )

    @staticmethod
    def _bottleneck(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "category": item["category"],
            "severity": item["severity"],
            "impact_ms": item["impact_ms"],
            "evidence": item["evidence"],
        }

    @staticmethod
    def _recommendation(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "category": item["category"],
            "severity": item["severity"],
            "evidence": item["evidence"],
            "reason": item["reason"],
            "suggestion": item["suggestion"],
            "expected_impact": item["expected_impact"],
            "verification": item["verification"],
        }

    @classmethod
    def _severity(
        cls,
        impact_ms: float,
        startup_ms: float,
        high_ms: float,
        medium_ms: float,
    ) -> str:
        percentage = impact_ms * 100 / startup_ms if startup_ms > 0 else 0
        if impact_ms >= high_ms or percentage >= 10:
            return "HIGH"
        if impact_ms >= medium_ms or percentage >= 3:
            return "MEDIUM"
        return "LOW"

    @classmethod
    def _meets_min_evidence(
        cls,
        impact_ms: float,
        startup_ms: float,
    ) -> bool:
        percentage = impact_ms * 100 / startup_ms if startup_ms > 0 else 0
        return (
            impact_ms >= cls.MIN_EVIDENCE_IMPACT_MS
            or percentage >= cls.MIN_EVIDENCE_STARTUP_PERCENTAGE
        )

    @classmethod
    def _valid_slices(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            duration_ms = cls._number(item.get("duration_ms"))
            if duration_ms < 0:
                continue
            result.append(
                {
                    "name": str(item.get("name") or "unknown"),
                    "duration_ms": duration_ms,
                }
            )
        return sorted(result, key=lambda item: item["duration_ms"], reverse=True)

    @classmethod
    def _reason_metric(
        cls,
        analysis: dict[str, Any],
        reason: str,
    ) -> dict[str, Any] | None:
        sources = (
            (analysis.get("startup_stages"), "stage"),
            (analysis.get("top_bottlenecks"), "reason"),
        )
        for items, key in sources:
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get(key) != reason:
                    continue
                duration_ms = cls._number(item.get("duration_ms"))
                if duration_ms <= 0:
                    continue
                return {
                    "reason": reason,
                    "duration_ms": duration_ms,
                    "event_count": cls._integer(item.get("event_count")),
                }
        return None

    @staticmethod
    def _slice_evidence(
        label: str,
        slices: list[dict[str, Any]],
        *,
        max_items: int = 5,
    ) -> str:
        if not slices:
            return f"{label} 已被 Perfetto 检测，但无可用时长。"
        details = ", ".join(
            f"{item['name']} {item['duration_ms']:.3f} ms"
            for item in slices[:max_items]
        )
        return f"{label}: {details}。"

    @staticmethod
    def _number(value: Any) -> float:
        if isinstance(value, bool):
            return 0.0
        if isinstance(value, (int, float)):
            return max(float(value), 0.0)
        return 0.0

    @staticmethod
    def _integer(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(value, 0)
        return 0

    @staticmethod
    def _empty_result(*, error_type: str, summary: str) -> dict[str, Any]:
        return {
            "success": False,
            "package_name": None,
            "startup_duration_ms": None,
            "bottlenecks": [],
            "recommendations": [],
            "priority_order": [],
            "verification_plan": [],
            "source_localization_hints": [],
            "evidence_threshold": {
                "minimum_impact_ms": (
                    GenerateStartupOptimizationPlanTool.MIN_EVIDENCE_IMPACT_MS
                ),
                "minimum_startup_percentage": (
                    GenerateStartupOptimizationPlanTool
                    .MIN_EVIDENCE_STARTUP_PERCENTAGE
                ),
                "rule": "impact_ms >= 3 OR percentage_of_startup >= 1%",
            },
            "error_type": error_type,
            "summary": summary,
        }
