"""Unit tests for v3 design fixes: dispatch_after_turn, normalize, session_id."""

from unittest.mock import MagicMock

import pytest

from xiaopaw.hook_framework.registry import (
    EventType,
    GuardrailDeny,
    HookContext,
    HookRegistry,
)
from xiaopaw.hook_framework.crew_adapter import CrewObservabilityAdapter


class EventCollector:
    def __init__(self):
        self.events: list[HookContext] = []

    def handler(self, ctx: HookContext):
        self.events.append(ctx)

    def filter(self, event_type: EventType) -> list[HookContext]:
        return [e for e in self.events if e.event_type == event_type]

    def count(self, event_type: EventType) -> int:
        return len(self.filter(event_type))


@pytest.fixture
def adapter_setup():
    registry = HookRegistry()
    collector = EventCollector()
    for et in EventType:
        registry.register(et, collector.handler)
    adapter = CrewObservabilityAdapter(registry, session_id="test_session")
    return adapter, registry, collector


# ── dispatch_after_turn ─────────────────────────────────────────────


class TestDispatchAfterTurn:
    def test_dispatches_after_turn_event(self, adapter_setup):
        adapter, _, collector = adapter_setup
        adapter.on_before_llm(agent_role="orchestrator", messages=[])
        adapter.dispatch_after_turn(output="hello world")
        assert collector.count(EventType.AFTER_TURN) == 1
        ctx = collector.filter(EventType.AFTER_TURN)[0]
        assert ctx.metadata["output"] == "hello world"

    def test_resets_turn_flags(self, adapter_setup):
        adapter, _, _ = adapter_setup
        adapter.on_before_llm(agent_role="orchestrator", messages=[])
        assert adapter._current_turn_has_llm is True
        adapter.dispatch_after_turn(output="done")
        assert adapter._current_turn_has_llm is False
        assert adapter._last_prompt_preview == ""

    def test_captures_agent_id(self, adapter_setup):
        adapter, _, collector = adapter_setup
        adapter.on_before_llm(agent_role="skill_agent", messages=[])
        adapter.dispatch_after_turn(output="result")
        ctx = collector.filter(EventType.AFTER_TURN)[0]
        assert ctx.agent_id == "skill_agent"

    def test_uses_dispatch_gate(self):
        """Verify dispatch_after_turn uses dispatch_gate so guardrails work."""
        registry = HookRegistry()

        def deny_loop(ctx):
            raise GuardrailDeny("loop_detected", "too many iterations")

        registry.register(EventType.AFTER_TURN, deny_loop)
        adapter = CrewObservabilityAdapter(registry, session_id="test")
        adapter.dispatch_after_turn(output="loop output")
        assert adapter._pending_deny is not None
        assert adapter._pending_deny.reason_code == "loop_detected"

    def test_pending_deny_not_overwritten(self):
        """First deny wins — second dispatch_after_turn should not overwrite."""
        registry = HookRegistry()
        call_count = 0

        def deny_always(ctx):
            nonlocal call_count
            call_count += 1
            raise GuardrailDeny("loop_detected", f"attempt {call_count}")

        registry.register(EventType.AFTER_TURN, deny_always)
        adapter = CrewObservabilityAdapter(registry, session_id="test")
        adapter.dispatch_after_turn(output="first")
        adapter.dispatch_after_turn(output="second")
        assert adapter._pending_deny.detail == "attempt 1"

    def test_truncates_long_output(self, adapter_setup):
        adapter, _, collector = adapter_setup
        long_output = "x" * 5000
        adapter.dispatch_after_turn(output=long_output)
        ctx = collector.filter(EventType.AFTER_TURN)[0]
        assert "truncated" in ctx.metadata["output"]
        assert len(ctx.metadata["output"]) < 5000

    def test_enables_new_turn_after_dispatch(self, adapter_setup):
        """After dispatch_after_turn, next on_before_llm should trigger BEFORE_TURN."""
        adapter, _, collector = adapter_setup
        adapter.on_before_llm(agent_role="orch", messages=[])
        assert collector.count(EventType.BEFORE_TURN) == 1
        adapter.dispatch_after_turn(output="done")
        adapter.on_before_llm(agent_role="orch", messages=[])
        assert collector.count(EventType.BEFORE_TURN) == 2


# ── _normalize_tool_input ───────────────────────────────────────────


class TestNormalizeToolInput:
    def test_removes_python_none(self):
        from xiaopaw.agents.main_crew import _normalize_tool_input

        d = {"command": "echo hi", "timeout": "None", "cwd": "None"}
        _normalize_tool_input(d)
        assert d == {"command": "echo hi"}

    def test_converts_python_true_false(self):
        from xiaopaw.agents.main_crew import _normalize_tool_input

        d = {"verbose": "True", "quiet": "False", "name": "test"}
        _normalize_tool_input(d)
        assert d == {"verbose": True, "quiet": False, "name": "test"}

    def test_ignores_lowercase(self):
        from xiaopaw.agents.main_crew import _normalize_tool_input

        d = {"filter": "none", "enabled": "true", "disabled": "false"}
        _normalize_tool_input(d)
        assert d == {"filter": "none", "enabled": "true", "disabled": "false"}

    def test_ignores_non_string_values(self):
        from xiaopaw.agents.main_crew import _normalize_tool_input

        d = {"count": 10, "flag": True, "items": None}
        _normalize_tool_input(d)
        assert d == {"count": 10, "flag": True, "items": None}

    def test_empty_dict(self):
        from xiaopaw.agents.main_crew import _normalize_tool_input

        d = {}
        _normalize_tool_input(d)
        assert d == {}


# ── _is_mcp_sandbox_tool ────────────────────────────────────────────


class TestIsMcpSandboxTool:
    def test_sandbox_tools(self):
        from xiaopaw.agents.main_crew import _is_mcp_sandbox_tool

        assert _is_mcp_sandbox_tool("sandbox_exec") is True
        assert _is_mcp_sandbox_tool("sandbox_write_file") is True
        assert _is_mcp_sandbox_tool("mcp_run_command") is True

    def test_non_sandbox_tools(self):
        from xiaopaw.agents.main_crew import _is_mcp_sandbox_tool

        assert _is_mcp_sandbox_tool("skill_loader") is False
        assert _is_mcp_sandbox_tool("intermediate_reply") is False
        assert _is_mcp_sandbox_tool("web_search") is False


# ── session_id validation ───────────────────────────────────────────


class TestSessionIdValidation:
    def test_valid_session_ids(self):
        from xiaopaw.tools.skill_loader import _SESSION_ID_PATTERN

        assert _SESSION_ID_PATTERN.match("abc123")
        assert _SESSION_ID_PATTERN.match("test-session_01")
        assert _SESSION_ID_PATTERN.match("a" * 64)

    def test_invalid_session_ids(self):
        from xiaopaw.tools.skill_loader import _SESSION_ID_PATTERN

        assert not _SESSION_ID_PATTERN.match("")
        assert not _SESSION_ID_PATTERN.match("../etc/passwd")
        assert not _SESSION_ID_PATTERN.match("test;rm -rf /")
        assert not _SESSION_ID_PATTERN.match("a" * 65)
        assert not _SESSION_ID_PATTERN.match("test session")

    def test_skill_loader_rejects_bad_session_id(self):
        from xiaopaw.tools.skill_loader import SkillLoaderTool

        with pytest.raises(ValueError, match="Invalid session_id"):
            SkillLoaderTool(session_id="../../../etc/passwd")

    def test_skill_loader_accepts_empty_session_id(self):
        from xiaopaw.tools.skill_loader import SkillLoaderTool

        tool = SkillLoaderTool(session_id="")
        assert tool._session_id == ""
