"""UT-ADP-001 ~ UT-ADP-013: CrewObservabilityAdapter unit tests.

These tests mock CrewAI's global hook registration to avoid requiring crewai
as a dependency. The adapter's core logic is exercised directly.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from xiaopaw.hook_framework.registry import (
    EventType,
    GuardrailDeny,
    HookContext,
    HookRegistry,
)
from xiaopaw.hook_framework.crew_adapter import CrewObservabilityAdapter


class EventCollector:
    """Test helper to collect dispatched events."""
    def __init__(self):
        self.events: list[HookContext] = []

    def handler(self, ctx: HookContext):
        self.events.append(ctx)

    def filter(self, event_type: EventType) -> list[HookContext]:
        return [e for e in self.events if e.event_type == event_type]

    def count(self, event_type: EventType) -> int:
        return len(self.filter(event_type))


@pytest.fixture
def setup():
    registry = HookRegistry()
    collector = EventCollector()
    for et in EventType:
        registry.register(et, collector.handler)
    adapter = CrewObservabilityAdapter(registry, session_id="test_session")
    return adapter, registry, collector


# ── BEFORE_TURN count logic ──────────────────────────────────────────


class TestBeforeTurn:
    def test_adp001_first_llm_triggers_before_turn_and_before_llm(self, setup):
        adapter, _, collector = setup
        adapter.on_before_llm(agent_role="XiaoPaw", messages=[])
        assert collector.count(EventType.BEFORE_TURN) == 1
        assert collector.count(EventType.BEFORE_LLM) == 1
        assert adapter._turn_count == 1

    def test_adp002_second_llm_only_before_llm(self, setup):
        adapter, _, collector = setup
        adapter.on_before_llm(agent_role="XiaoPaw", messages=[])
        adapter.on_before_llm(agent_role="XiaoPaw", messages=[])
        assert collector.count(EventType.BEFORE_TURN) == 1
        assert collector.count(EventType.BEFORE_LLM) == 2

    def test_adp003_step_callback_resets_turn(self, setup):
        adapter, _, collector = setup
        adapter.on_before_llm(agent_role="XiaoPaw", messages=[])
        step_cb = adapter.make_step_callback()
        step_cb(MagicMock(output="result"))
        adapter.on_before_llm(agent_role="XiaoPaw", messages=[])
        assert collector.count(EventType.BEFORE_TURN) == 2
        assert adapter._turn_count == 2


# ── step_callback / task_callback ────────────────────────────────────


class TestCallbacks:
    def test_adp004_step_callback_triggers_after_turn(self, setup):
        adapter, _, collector = setup
        step_cb = adapter.make_step_callback()
        step_cb(MagicMock(output="result"))
        assert collector.count(EventType.AFTER_TURN) == 1

    def test_adp005_step_callback_resets_flag(self, setup):
        adapter, _, collector = setup
        adapter.on_before_llm(agent_role="XiaoPaw", messages=[])
        assert adapter._current_turn_has_llm is True
        step_cb = adapter.make_step_callback()
        step_cb(MagicMock(output="result"))
        assert adapter._current_turn_has_llm is False

    def test_adp006_task_callback_triggers_task_complete(self, setup):
        adapter, _, collector = setup
        task_cb = adapter.make_task_callback()
        task_cb(MagicMock(raw="final result", description="do stuff"))
        assert collector.count(EventType.TASK_COMPLETE) == 1
        ctx = collector.filter(EventType.TASK_COMPLETE)[0]
        assert "final result" in ctx.metadata.get("raw_output", "")


# ── Tool call mapping ────────────────────────────────────────────────


class TestToolCall:
    def test_adp007_before_tool_call_event(self, setup):
        adapter, _, collector = setup
        adapter.on_before_tool_call(
            tool_name="web_search", tool_input={"query": "test"}
        )
        assert collector.count(EventType.BEFORE_TOOL_CALL) == 1
        ctx = collector.filter(EventType.BEFORE_TOOL_CALL)[0]
        assert ctx.tool_name == "web_search"
        assert ctx.tool_input == {"query": "test"}

    def test_adp008_after_tool_call_truncation(self, setup):
        adapter, _, collector = setup
        long_result = "x" * 3000
        adapter.on_after_tool_call(
            tool_name="search", tool_input={}, tool_result=long_result
        )
        assert collector.count(EventType.AFTER_TOOL_CALL) == 1
        ctx = collector.filter(EventType.AFTER_TOOL_CALL)[0]
        assert "truncated" in ctx.metadata.get("tool_output", "")


# ── Cleanup ──────────────────────────────────────────────────────────


class TestCleanup:
    def test_adp009_cleanup_triggers_session_end(self, setup):
        adapter, _, collector = setup
        adapter.cleanup()
        assert collector.count(EventType.SESSION_END) == 1

    def test_adp010_cleanup_idempotent(self, setup):
        adapter, _, collector = setup
        adapter.cleanup()
        adapter.cleanup()
        assert collector.count(EventType.SESSION_END) == 1


# ── pending_deny ─────────────────────────────────────────────────────


class TestPendingDeny:
    def test_adp011_pending_deny_flow(self):
        registry = HookRegistry()

        def deny_tool(ctx):
            raise GuardrailDeny("sandbox_violation", "blocked")

        registry.register(EventType.BEFORE_TOOL_CALL, deny_tool)

        collector = EventCollector()
        for et in EventType:
            if et != EventType.BEFORE_TOOL_CALL:
                registry.register(et, collector.handler)

        adapter = CrewObservabilityAdapter(registry, session_id="test")
        adapter.on_before_tool_call(
            tool_name="evil_tool", tool_input={"path": "../../etc/passwd"}
        )
        assert adapter._pending_deny is not None

        step_cb = adapter.make_step_callback()
        with pytest.raises(GuardrailDeny, match="sandbox_violation"):
            step_cb(MagicMock(output="result"))

    def test_adp011b_pending_deny_in_task_callback(self):
        registry = HookRegistry()

        def deny_tool(ctx):
            raise GuardrailDeny("permission_denied", "no access")

        registry.register(EventType.BEFORE_TOOL_CALL, deny_tool)

        adapter = CrewObservabilityAdapter(registry, session_id="test")
        adapter.on_before_tool_call(tool_name="shell", tool_input={})
        assert adapter._pending_deny is not None

        task_cb = adapter.make_task_callback()
        with pytest.raises(GuardrailDeny, match="permission_denied"):
            task_cb(MagicMock(raw="result", description="test"))

    def test_adp011c_pending_deny_in_cleanup(self):
        registry = HookRegistry()

        def deny_tool(ctx):
            raise GuardrailDeny("budget_exceeded", "over limit")

        registry.register(EventType.BEFORE_TOOL_CALL, deny_tool)

        adapter = CrewObservabilityAdapter(registry, session_id="test")
        adapter.on_before_tool_call(tool_name="expensive", tool_input={})

        with pytest.raises(GuardrailDeny, match="budget_exceeded"):
            adapter.cleanup()


# ── duration_ms timing (FIX-2) ──────────────────────────────────────


class TestDurationMs:
    def test_adp012_tool_duration_ms_nonzero(self, setup):
        adapter, _, collector = setup
        adapter.on_before_tool_call(tool_name="slow_tool", tool_input={})
        time.sleep(0.015)
        adapter.on_after_tool_call(
            tool_name="slow_tool", tool_input={}, tool_result="done"
        )
        after_events = collector.filter(EventType.AFTER_TOOL_CALL)
        assert len(after_events) == 1
        assert after_events[0].duration_ms >= 10

    def test_adp012b_tool_duration_without_before(self, setup):
        adapter, _, collector = setup
        adapter.on_after_tool_call(
            tool_name="orphan_tool", tool_input={}, tool_result="ok"
        )
        after_events = collector.filter(EventType.AFTER_TOOL_CALL)
        assert len(after_events) == 1
        assert after_events[0].duration_ms == 0

    def test_adp013_deny_duration_ms(self):
        registry = HookRegistry()

        def deny_tool(ctx):
            time.sleep(0.01)
            raise GuardrailDeny("sandbox_violation", "blocked")

        registry.register(EventType.BEFORE_TOOL_CALL, deny_tool)

        collector = EventCollector()
        for et in EventType:
            if et != EventType.BEFORE_TOOL_CALL:
                registry.register(et, collector.handler)

        adapter = CrewObservabilityAdapter(registry, session_id="test")
        adapter.on_before_tool_call(tool_name="evil", tool_input={})

        after_events = collector.filter(EventType.AFTER_TOOL_CALL)
        assert len(after_events) == 1
        assert after_events[0].duration_ms >= 5
        assert after_events[0].success is False
