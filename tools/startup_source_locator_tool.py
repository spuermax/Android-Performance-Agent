from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError
from tools.file_tool import ReadProjectFileTool
from tools.search_tool import SearchProjectTextTool


class LocateStartupBottleneckSourceTool(BaseTool):
    name = "locate_startup_bottleneck_source"
    description = (
        "根据 generate_startup_optimization_plan 的真实 evidence，在指定 Android "
        "application module 内定位可能相关的源码和 Manifest 位置。Tool 只返回"
        "带置信度的候选与无法定位项，不修改源码，也不会把普通代码存在性直接"
        "断言为性能瓶颈。"
    )

    SUPPORTED_CATEGORIES = {
        "APPLICATION_INITIALIZATION",
        "CONTENT_PROVIDER_INITIALIZATION",
        "MAIN_THREAD_IO",
        "BINDER_IPC",
        "LONG_MAIN_THREAD_TASK",
        "DEX_CLASS_LOADING",
        "FIRST_FRAME_WORK",
    }
    CATEGORY_QUERIES = {
        "APPLICATION_INITIALIZATION": (
            "Application()",
            "extends Application",
        ),
        "CONTENT_PROVIDER_INITIALIZATION": (
            "ContentProvider",
            "extends ContentProvider",
            "androidx.startup",
            "Initializer<",
        ),
        "MAIN_THREAD_IO": (
            "getSharedPreferences(",
            "SharedPreferences",
            "Room.databaseBuilder(",
            "SQLiteDatabase",
            "FileInputStream(",
            ".readText(",
            ".readBytes(",
            "openFileInput(",
        ),
        "BINDER_IPC": (
            "getSystemService(",
            "bindService(",
            "ContentResolver",
            "contentResolver",
        ),
        "LONG_MAIN_THREAD_TASK": ("onCreate(", "doFrame(", "inflate("),
        "DEX_CLASS_LOADING": (),
        "FIRST_FRAME_WORK": (
            "setContentView(",
            "setContent {",
            "inflate(",
        ),
    }
    SOURCE_SUFFIXES = {".java", ".kt"}
    CONFIDENCE_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    MAX_MATCHES_PER_CATEGORY = 12
    MAX_MATCHES = 50

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "optimization_plan": {
                    "type": "object",
                    "description": (
                        "generate_startup_optimization_plan 返回的完整成功 Tool Result。"
                    ),
                },
                "project_path": {
                    "type": "string",
                    "description": "用户最初指定的 Android 项目绝对路径。",
                },
                "target_module": {
                    "type": "string",
                    "description": "要定位源码的 Android application module，例如 app 或 :app。",
                },
                "package_name": {
                    "type": "string",
                    "description": "Macrobenchmark 和 Perfetto 分析对应的目标包名。",
                },
            },
            "required": [
                "optimization_plan",
                "project_path",
                "target_module",
                "package_name",
            ],
            "additionalProperties": False,
        }

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        plan = arguments.get("optimization_plan")
        raw_project = arguments.get("project_path")
        target_module = arguments.get("target_module")
        package_name = arguments.get("package_name")

        if not isinstance(plan, dict):
            raise ToolError("optimization_plan 必须是对象")
        if not isinstance(raw_project, str) or not raw_project.strip():
            raise ToolError("project_path 必须是非空字符串")
        if not isinstance(target_module, str) or not target_module.strip():
            raise ToolError("target_module 必须是非空字符串")
        if not isinstance(package_name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+",
            package_name.strip(),
        ):
            raise ToolError("package_name 不是有效的 Android 包名")
        package_name = package_name.strip()

        project = self.validate_project_path(raw_project)
        module_name, module_path = self._resolve_module(
            project,
            target_module,
        )
        if not module_path.is_dir():
            return self._empty_result(
                package_name=package_name,
                target_module=module_name,
                error_type="MODULE_NOT_FOUND",
                summary=f"目标 module 不存在：{module_name}",
            )

        validation_error = self._validate_plan(plan, package_name)
        if validation_error is not None:
            error_type, summary = validation_error
            return self._empty_result(
                package_name=package_name,
                target_module=module_name,
                error_type=error_type,
                summary=summary,
            )

        recommendations = self._recommendations(plan)
        search_tool = SearchProjectTextTool(allowed_project_path=module_path)
        read_tool = ReadProjectFileTool(allowed_project_path=project)
        matches: list[dict[str, Any]] = []
        unresolved: list[dict[str, str]] = []

        for recommendation in recommendations:
            category = recommendation["category"]
            evidence = recommendation["evidence"]
            if category not in self.SUPPORTED_CATEGORIES:
                unresolved.append(
                    {
                        "category": category,
                        "evidence": evidence,
                        "reason": "V0.5 尚未为该优化类型定义可靠的源码定位规则。",
                    }
                )
                continue

            category_matches = self._locate_category(
                project=project,
                module_path=module_path,
                category=category,
                evidence=evidence,
                search_tool=search_tool,
                read_tool=read_tool,
            )
            if category_matches:
                matches.extend(category_matches)
            else:
                unresolved.append(
                    {
                        "category": category,
                        "evidence": evidence,
                        "reason": (
                            "在目标 module 内没有找到能与该 Perfetto 证据可靠关联的"
                            "源码位置；不根据通用命名猜测。"
                        ),
                    }
                )

        matches = self._deduplicate_and_sort(matches)[: self.MAX_MATCHES]
        summary = (
            f"已基于 V0.4 真实 evidence 在 {module_name} 中定位 "
            f"{len(matches)} 个候选源码位置；{len(unresolved)} 类仍无法可靠定位。"
        )
        return {
            "success": True,
            "package_name": package_name,
            "target_module": module_name,
            "module_path": str(module_path),
            "matches": matches,
            "unresolved": unresolved,
            "error_type": None,
            "summary": summary,
        }

    @staticmethod
    def _validate_plan(
        plan: dict[str, Any],
        package_name: str,
    ) -> tuple[str, str] | None:
        if plan.get("success") is not True:
            return (
                "INVALID_OPTIMIZATION_PLAN",
                "optimization_plan 不是成功的 V0.4 优化计划。",
            )
        plan_package = plan.get("package_name")
        if not isinstance(plan_package, str) or not plan_package.strip():
            return (
                "INVALID_OPTIMIZATION_PLAN",
                "optimization_plan 缺少 package_name。",
            )
        if plan_package != package_name:
            return (
                "PACKAGE_MISMATCH",
                "optimization_plan 与请求的 package_name 不一致，拒绝猜测目标源码。",
            )
        if not isinstance(plan.get("recommendations"), list):
            return (
                "INVALID_OPTIMIZATION_PLAN",
                "optimization_plan 缺少 recommendations。",
            )
        return None

    @staticmethod
    def _recommendations(plan: dict[str, Any]) -> list[dict[str, str]]:
        recommendations: list[dict[str, str]] = []
        for value in plan["recommendations"]:
            if not isinstance(value, dict):
                continue
            category = value.get("category")
            evidence = value.get("evidence")
            if (
                isinstance(category, str)
                and category.strip()
                and isinstance(evidence, str)
                and evidence.strip()
            ):
                recommendations.append(
                    {"category": category.strip(), "evidence": evidence.strip()}
                )
        return recommendations

    @staticmethod
    def _resolve_module(project: Path, raw_module: str) -> tuple[str, Path]:
        module = raw_module.strip().replace(":", "/").strip("/")
        if not module or any(part in {".", ".."} for part in Path(module).parts):
            raise ToolError("target_module 必须是项目内有效的 Gradle module 路径")
        module_path = (project / module).resolve()
        try:
            module_path.relative_to(project)
        except ValueError as exc:
            raise ToolError("拒绝访问项目目录之外的 module") from exc
        return module.replace("/", ":"), module_path

    def _locate_category(
        self,
        *,
        project: Path,
        module_path: Path,
        category: str,
        evidence: str,
        search_tool: SearchProjectTextTool,
        read_tool: ReadProjectFileTool,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        evidence_terms = self._evidence_terms(evidence)

        if category in {
            "APPLICATION_INITIALIZATION",
            "CONTENT_PROVIDER_INITIALIZATION",
        }:
            matches.extend(
                self._manifest_matches(project, module_path, category, evidence)
            )

        queries = list(evidence_terms)
        if category != "DEX_CLASS_LOADING" or evidence_terms:
            queries.extend(self.CATEGORY_QUERIES[category])

        seen_queries: set[str] = set()
        for query in queries:
            query_key = query.casefold()
            if query_key in seen_queries:
                continue
            seen_queries.add(query_key)
            result = search_tool.execute(
                {
                    "project_path": str(module_path),
                    "query": query,
                    "max_results": 100,
                }
            )
            for found in result.get("matches", []):
                module_relative_path = str(found.get("file", ""))
                path = (module_path / module_relative_path).resolve()
                if not self._is_source_in_module(path, module_path):
                    continue
                relative_path = str(path.relative_to(project))
                line = found.get("line")
                if not isinstance(line, int):
                    continue
                exact_evidence = query in evidence_terms
                symbol = self._source_symbol(
                    project,
                    relative_path,
                    line,
                    read_tool,
                )
                confidence = self._confidence(
                    category=category,
                    exact_evidence=exact_evidence,
                    symbol=symbol,
                    line_text=str(found.get("text", "")),
                )
                matches.append(
                    {
                        "category": category,
                        "evidence": evidence,
                        "file_path": relative_path,
                        "line": line,
                        "symbol": symbol,
                        "confidence": confidence,
                        "reason": self._match_reason(
                            category,
                            query,
                            exact_evidence,
                            confidence,
                        ),
                    }
                )
                if len(matches) >= self.MAX_MATCHES_PER_CATEGORY:
                    return matches
        return matches

    @classmethod
    def _manifest_matches(
        cls,
        project: Path,
        module_path: Path,
        category: str,
        evidence: str,
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        src = module_path / "src"
        if not src.is_dir():
            return matches
        for manifest in sorted(src.glob("*/AndroidManifest.xml")):
            if manifest.parent.name in {"test", "androidTest"}:
                continue
            try:
                relative_path = str(manifest.resolve().relative_to(project))
                lines = manifest.read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, ValueError):
                continue
            tag = "application" if category == "APPLICATION_INITIALIZATION" else "provider"
            for index, line in enumerate(lines):
                if f"<{tag}" not in line:
                    continue
                tag_lines: list[str] = []
                for tag_line in lines[index : min(index + 8, len(lines))]:
                    tag_lines.append(tag_line)
                    if ">" in tag_line:
                        break
                block = " ".join(tag_lines)
                if not tag_lines or ">" not in tag_lines[-1]:
                    continue
                name_match = re.search(
                    r'android:name\s*=\s*["\']([^"\']+)["\']',
                    block,
                )
                symbol = name_match.group(1) if name_match else f"<{tag}>"
                evidence_terms = cls._evidence_terms(evidence)
                exact = any(term in block for term in evidence_terms)
                matches.append(
                    {
                        "category": category,
                        "evidence": evidence,
                        "file_path": relative_path,
                        "line": index + 1,
                        "symbol": symbol,
                        "confidence": "HIGH" if exact else "MEDIUM",
                        "reason": (
                            f"Manifest {tag} 声明与 evidence 中的类名一致。"
                            if exact
                            else (
                                f"Manifest 注册了相关 {tag}，它是基于当前瓶颈类型的"
                                "候选配置位置，但尚无类名级 Trace 证据。"
                            )
                        ),
                    }
                )
        return matches

    @staticmethod
    def _evidence_terms(evidence: str) -> list[str]:
        ignored = {
            "Application",
            "ContentProvider",
            "Slice",
            "Startup",
            "Binder",
            "Running",
            "Runnable",
            "MainThread",
            "Class",
            "Dex",
            "TTID",
            "Android",
            "Perfetto",
            "Trace",
        }
        terms: list[str] = []
        patterns = (
            r"\b(?:[a-zA-Z_]\w*\.){2,}[A-Z]\w*\b",
            r"\b[A-Z][A-Za-z0-9_]*(?:Provider|Application|Activity|Initializer|Manager|Sdk|SDK)\b",
            r"\b[A-Z][A-Za-z0-9_]{2,}\b",
        )
        for pattern in patterns:
            for term in re.findall(pattern, evidence):
                if term not in ignored and term not in terms:
                    terms.append(term)
        return terms[:8]

    @classmethod
    def _source_symbol(
        cls,
        project: Path,
        relative_path: str,
        line: int,
        read_tool: ReadProjectFileTool,
    ) -> str:
        result = read_tool.execute(
            {
                "project_path": str(project),
                "relative_path": relative_path,
                "start_line": max(1, line - 30),
                "end_line": line,
            }
        )
        if result.get("success") is not True:
            return Path(relative_path).stem
        patterns = (
            re.compile(r"\b(?:class|object|interface)\s+([A-Za-z_]\w*)"),
            re.compile(r"\bfun\s+([A-Za-z_]\w*)\s*\("),
            re.compile(
                r"\b(?:public|private|protected|static|final|synchronized|override|open|abstract|native|\s)+"
                r"[A-Za-z_$][\w$<>?,.\[\] ]*\s+([A-Za-z_$][\w$]*)\s*\("
            ),
        )
        for item in reversed(result.get("content", [])):
            text = str(item.get("text", ""))
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    return match.group(1)
        return Path(relative_path).stem

    @staticmethod
    def _is_source_in_module(path: Path, module_path: Path) -> bool:
        try:
            relative = path.relative_to(module_path)
        except ValueError:
            return False
        parts = relative.parts
        source_set = parts[parts.index("src") + 1] if "src" in parts else None
        return (
            path.suffix.lower() in LocateStartupBottleneckSourceTool.SOURCE_SUFFIXES
            and source_set not in {None, "test", "androidTest"}
            and not any(part in SearchProjectTextTool.SKIP_DIRS for part in parts)
        )

    @staticmethod
    def _confidence(
        *,
        category: str,
        exact_evidence: bool,
        symbol: str,
        line_text: str,
    ) -> str:
        if exact_evidence:
            return "HIGH"
        lifecycle = symbol in {"onCreate", "attachBaseContext", "query", "insert"}
        if lifecycle or (
            category == "CONTENT_PROVIDER_INITIALIZATION"
            and "ContentProvider" in line_text
        ):
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _match_reason(
        category: str,
        query: str,
        exact_evidence: bool,
        confidence: str,
    ) -> str:
        if exact_evidence:
            return f"源码包含 Perfetto evidence 提取出的标识符 `{query}`。"
        return (
            f"源码包含与 {category} 对应的 `{query}` 调用或声明；"
            f"这是 {confidence} 置信度候选，存在性本身不证明它就是瓶颈。"
        )

    @classmethod
    def _deduplicate_and_sort(
        cls,
        matches: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        unique: dict[tuple[str, str, int], dict[str, Any]] = {}
        for match in matches:
            key = (match["category"], match["file_path"], match["line"])
            previous = unique.get(key)
            if previous is None or cls.CONFIDENCE_ORDER[match["confidence"]] > (
                cls.CONFIDENCE_ORDER[previous["confidence"]]
            ):
                unique[key] = match
        return sorted(
            unique.values(),
            key=lambda item: (
                -cls.CONFIDENCE_ORDER[item["confidence"]],
                item["category"],
                item["file_path"],
                item["line"],
            ),
        )

    @staticmethod
    def _empty_result(
        *,
        package_name: str,
        target_module: str,
        error_type: str,
        summary: str,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "package_name": package_name,
            "target_module": target_module,
            "module_path": None,
            "matches": [],
            "unresolved": [],
            "error_type": error_type,
            "summary": summary,
        }
