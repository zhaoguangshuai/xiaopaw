"""IT-GDF-001 ~ IT-GDF-003: GuardrailDeny propagation flow integration tests.

Each test uses a unique session_id for Langfuse trace isolation.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xiaopaw.hook_framework.crew_adapter import CrewObservabilityAdapter
from xiaopaw.hook_framework.loader import HookLoader
from xiaopaw.hook_framework.registry import EventType, GuardrailDeny, HookContext, HookRegistry

from .conftest import assert_observation_has_io, assert_trace_exists, ensure_trace

SHARED_HOOKS_DIR = Path(__file__).parent.parent.parent / "shared_hooks"


@pytest.mark.integration
class TestGuardrailDenyFlow:
    def test_gdf001_strategy_deny_caught_by_caller(self, unique_session_id):
        sid = unique_session_id
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(SHARED_HOOKS_DIR, layer_name="global")
        ensure_trace(registry, sid)
        cost = loader.strategies["cost_guard"]
        cost._budget = 0.0
        with pytest.raises(GuardrailDeny) as exc_info:
            registry.dispatch_gate(
                EventType.BEFORE_TOOL_CALL,
                HookContext(
                    event_type=EventType.BEFORE_TOOL_CALL,
                    tool_name="search",
                    tool_input={"q": "test"},
                    session_id=sid,
                ),
            )
        assert "budget_exceeded" in str(exc_info.value) or exc_info.value.reason_code == "budget_exceeded"

        trace = assert_trace_exists(sid, min_observations=1)
        obs = assert_observation_has_io(trace, "search")
        assert obs.get("input") == {"q": "test"}

    def test_gdf002_pending_deny_via_adapter(self, unique_session_id):
        sid = unique_session_id
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(SHARED_HOOKS_DIR, layer_name="global")
        adapter = CrewObservabilityAdapter(registry, session_id=sid)
        adapter.on_turn_start(user_message="deny test", sender_id="u-test")
        adapter.on_before_tool_call(
            tool_name="reader", tool_input={"path": "../../etc/passwd"}
        )
        assert adapter._pending_deny is not None
        step_cb = adapter.make_step_callback()
        with pytest.raises(GuardrailDeny):
            step_cb(MagicMock(output="result"))

        trace = assert_trace_exists(sid, min_observations=1)
        obs = assert_observation_has_io(trace, "reader")
        assert obs.get("input") == {"path": "../../etc/passwd"}
        assert obs.get("level") == "ERROR"

    def test_gdf003_dispatch_swallows_guardrail_deny(self, unique_session_id):
        sid = unique_session_id
        registry = HookRegistry()

        def deny_handler(ctx):
            raise GuardrailDeny("test_deny", "should be swallowed")

        handler_after = MagicMock()
        registry.register(EventType.AFTER_TURN, deny_handler)
        registry.register(EventType.AFTER_TURN, handler_after)
        ctx = HookContext(event_type=EventType.AFTER_TURN, session_id=sid)
        registry.dispatch(EventType.AFTER_TURN, ctx)
        handler_after.assert_called_once()
