"""UT-REG-001 ~ UT-REG-012: HookRegistry unit tests."""

from unittest.mock import MagicMock

import pytest

from xiaopaw.hook_framework.registry import (
    EventType,
    GuardrailDeny,
    HookContext,
    HookRegistry,
)


# ── Registration & Dispatch ──────────────────────────────────────────


class TestDispatch:
    def test_reg001_single_handler_dispatched(self, hook_registry):
        handler = MagicMock()
        ctx = HookContext(event_type=EventType.BEFORE_TURN)
        hook_registry.register(EventType.BEFORE_TURN, handler)
        hook_registry.dispatch(EventType.BEFORE_TURN, ctx)
        handler.assert_called_once_with(ctx)

    def test_reg002_multiple_handlers_called_in_order(self, hook_registry):
        call_order = []
        h1 = MagicMock(side_effect=lambda ctx: call_order.append("h1"))
        h2 = MagicMock(side_effect=lambda ctx: call_order.append("h2"))
        h3 = MagicMock(side_effect=lambda ctx: call_order.append("h3"))
        hook_registry.register(EventType.BEFORE_LLM, h1)
        hook_registry.register(EventType.BEFORE_LLM, h2)
        hook_registry.register(EventType.BEFORE_LLM, h3)
        ctx = HookContext(event_type=EventType.BEFORE_LLM)
        hook_registry.dispatch(EventType.BEFORE_LLM, ctx)
        assert call_order == ["h1", "h2", "h3"]

    def test_reg003_dispatch_no_handlers_no_error(self, hook_registry):
        ctx = HookContext(event_type=EventType.SESSION_END)
        hook_registry.dispatch(EventType.SESSION_END, ctx)

    def test_reg004_different_events_isolated(self, hook_registry):
        h1 = MagicMock()
        h2 = MagicMock()
        hook_registry.register(EventType.BEFORE_TURN, h1)
        hook_registry.register(EventType.AFTER_TURN, h2)
        ctx = HookContext(event_type=EventType.BEFORE_TURN)
        hook_registry.dispatch(EventType.BEFORE_TURN, ctx)
        h1.assert_called_once()
        h2.assert_not_called()

    def test_reg012_handler_exception_does_not_break_dispatch(
        self, hook_registry, capsys
    ):
        h1 = MagicMock()
        h2 = MagicMock(side_effect=ValueError("boom"))
        h3 = MagicMock()
        hook_registry.register(EventType.BEFORE_TURN, h1)
        hook_registry.register(EventType.BEFORE_TURN, h2)
        hook_registry.register(EventType.BEFORE_TURN, h3)
        ctx = HookContext(event_type=EventType.BEFORE_TURN)
        hook_registry.dispatch(EventType.BEFORE_TURN, ctx)
        h1.assert_called_once()
        h3.assert_called_once()
        captured = capsys.readouterr()
        assert "boom" in captured.err


# ── dispatch_gate ────────────────────────────────────────────────────


class TestDispatchGate:
    def test_reg005_gate_normal_dispatch(self, hook_registry):
        handler = MagicMock()
        hook_registry.register(EventType.BEFORE_TOOL_CALL, handler)
        ctx = HookContext(event_type=EventType.BEFORE_TOOL_CALL)
        hook_registry.dispatch_gate(EventType.BEFORE_TOOL_CALL, ctx)
        handler.assert_called_once()

    def test_reg006_gate_propagates_guardrail_deny(self, hook_registry):
        def deny_handler(ctx):
            raise GuardrailDeny("budget_exceeded", "budget exceeded")

        hook_registry.register(EventType.BEFORE_TOOL_CALL, deny_handler)
        ctx = HookContext(event_type=EventType.BEFORE_TOOL_CALL)
        with pytest.raises(GuardrailDeny, match="budget_exceeded"):
            hook_registry.dispatch_gate(EventType.BEFORE_TOOL_CALL, ctx)

    def test_reg007_gate_swallows_non_guardrail_errors(
        self, hook_registry, capsys
    ):
        call_order = []
        h1 = MagicMock(side_effect=lambda ctx: call_order.append("h1"))
        h2 = MagicMock(side_effect=RuntimeError("oops"))
        h3 = MagicMock(side_effect=lambda ctx: call_order.append("h3"))
        hook_registry.register(EventType.BEFORE_TOOL_CALL, h1)
        hook_registry.register(EventType.BEFORE_TOOL_CALL, h2)
        hook_registry.register(EventType.BEFORE_TOOL_CALL, h3)
        ctx = HookContext(event_type=EventType.BEFORE_TOOL_CALL)
        hook_registry.dispatch_gate(EventType.BEFORE_TOOL_CALL, ctx)
        assert call_order == ["h1", "h3"]

    def test_reg008_gate_stops_at_first_deny(self, hook_registry):
        h1 = MagicMock()

        def h2_deny(ctx):
            raise GuardrailDeny("sandbox_violation", "path traversal")

        h3 = MagicMock()
        hook_registry.register(EventType.BEFORE_TOOL_CALL, h1)
        hook_registry.register(EventType.BEFORE_TOOL_CALL, h2_deny)
        hook_registry.register(EventType.BEFORE_TOOL_CALL, h3)
        ctx = HookContext(event_type=EventType.BEFORE_TOOL_CALL)
        with pytest.raises(GuardrailDeny):
            hook_registry.dispatch_gate(EventType.BEFORE_TOOL_CALL, ctx)
        h1.assert_called_once()
        h3.assert_not_called()

    def test_reg006b_gate_fail_closed_handler(self, hook_registry):
        def buggy_handler(ctx):
            raise TypeError("unexpected None")

        hook_registry.register(
            EventType.BEFORE_TOOL_CALL, buggy_handler, fail_closed=True
        )
        ctx = HookContext(event_type=EventType.BEFORE_TOOL_CALL)
        with pytest.raises(GuardrailDeny, match="fail-closed"):
            hook_registry.dispatch_gate(EventType.BEFORE_TOOL_CALL, ctx)

    def test_reg006c_gate_fail_open_handler(self, hook_registry, capsys):
        call_order = []

        def buggy_handler(ctx):
            raise TypeError("unexpected None")

        h2 = MagicMock(side_effect=lambda ctx: call_order.append("h2"))
        hook_registry.register(
            EventType.BEFORE_TOOL_CALL, buggy_handler, fail_closed=False
        )
        hook_registry.register(EventType.BEFORE_TOOL_CALL, h2)
        ctx = HookContext(event_type=EventType.BEFORE_TOOL_CALL)
        hook_registry.dispatch_gate(EventType.BEFORE_TOOL_CALL, ctx)
        assert call_order == ["h2"]


# ── GuardrailDeny ────────────────────────────────────────────────────


class TestGuardrailDeny:
    def test_reg009_guardrail_deny_attributes(self):
        e = GuardrailDeny("budget_exceeded", "over $0.50")
        assert e.reason_code == "budget_exceeded"
        assert e.detail == "over $0.50"
        assert "budget_exceeded" in str(e)
        assert "over $0.50" in str(e)


# ── handler_count / summary ──────────────────────────────────────────


class TestRegistryMeta:
    def test_reg010_handler_count(self, hook_registry):
        for _ in range(3):
            hook_registry.register(EventType.AFTER_TURN, MagicMock())
        assert hook_registry.handler_count(EventType.AFTER_TURN) == 3
        assert hook_registry.handler_count(EventType.BEFORE_LLM) == 0

    def test_reg011_summary(self, hook_registry):
        hook_registry.register(EventType.BEFORE_LLM, MagicMock(), name="h1")
        hook_registry.register(EventType.BEFORE_LLM, MagicMock(), name="h2")
        s = hook_registry.summary()
        assert "before_llm" in s
        assert s["before_llm"] == ["h1", "h2"]
