from pathlib import Path

from agent.context import build_agent_instructions


def test_agent_instructions_match_v05_capabilities(tmp_path: Path) -> None:
    instructions = build_agent_instructions(tmp_path, "skill content")

    assert "Android Performance Agent V0.5" in instructions
    assert "Optimization Candidates → Source Localization" in instructions
    assert "analyze_perfetto_trace" in instructions
    assert "locate_startup_bottleneck_source" in instructions
    assert "Macrobenchmark 阶段" not in instructions
    assert "Android Performance Agent V0.1" not in instructions


def test_agent_instructions_keep_project_and_skill(tmp_path: Path) -> None:
    instructions = build_agent_instructions(tmp_path, "custom startup skill")

    assert str(tmp_path) in instructions
    assert "custom startup skill" in instructions
