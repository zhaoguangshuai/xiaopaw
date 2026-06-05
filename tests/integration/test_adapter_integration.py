"""IT-ADP-001 ~ IT-ADP-006: Adapter + Registry + Strategies integration tests.

Each test uses a unique session_id for Langfuse trace isolation.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xiaopaw.hook_framework.crew_adapter import CrewObservabilityAdapter
from xiaopaw.hook_framework.loader import HookLoader
from xiaopaw.hook_framework.registry import EventType, GuardrailDeny, HookContext, HookRegistry

from .conftest import (
    assert_deny_observation,
    assert_generation_exists,
    assert_observation_has_io,
    assert_root_span_exists,
    assert_tool_observation,
    assert_trace_exists,
    assert_trace_has_session,
    assert_tree_structure,
)

SHARED_HOOKS_DIR = Path(__file__).parent.parent.parent / "shared_hooks"


@pytest.fixture
def adapter_chain(tmp_path, unique_session_id):
    registry = HookRegistry()
    loader = HookLoader(registry)
    loader.load_from_directory(SHARED_HOOKS_DIR, layer_name="global")
    adapter = CrewObservabilityAdapter(registry, session_id=unique_session_id)
    return adapter, registry, loader, unique_session_id


@pytest.mark.integration
class TestAdapterIntegration:
    def test_adp_it001_before_llm_dispatches_events(self, adapter_chain):
        adapter, _, loader, sid = adapter_chain
        adapter.on_turn_start(user_message="hello test", sender_id="u-test")
        adapter.on_before_llm(agent_role="XiaoPaw", messages=[{"content": "hello"}])
        adapter.cleanup()

        trace = assert_trace_exists(sid)
        assert_trace_has_session(trace)
        assert_root_span_exists(trace)

    def test_adp_it002_before_tool_sandbox_blocks(self, adapter_chain):
        adapter, _, loader, sid = adapter_chain
        adapter.on_turn_start(user_message="sandbox test", sender_id="u-test")
        adapter.on_before_tool_call(
            tool_name="reader", tool_input={"path": "../../etc/passwd"}
        )
        assert adapter._pending_deny is not None
        sandbox = loader.strategies["sandbox_guard"]
        assert sandbox.get_metrics()["total_violations"] >= 1
        adapter._pending_deny = None
        adapter.cleanup()

        trace = assert_trace_exists(sid, min_observations=1)
        assert_trace_has_session(trace)
        obs = assert_observation_has_io(trace, "reader")
        assert obs.get("input") == {"path": "../../etc/passwd"}
        assert obs.get("level") == "ERROR"
        assert obs["output"]["deny_reason"] == "sandbox_violation"
        assert_deny_observation(trace, "reader")

    def test_adp_it003_step_callback_with_cost(self, adapter_chain):
        adapter, registry, loader, sid = adapter_chain
        adapter.on_turn_start(user_message="step test", sender_id="u-test")
        cost = loader.strategies["cost_guard"]
        loop = loader.strategies["loop_detector"]
        step_cb = adapter.make_step_callback()
        step_cb(MagicMock(output="test result"))
        assert loop.get_metrics()["total_turns"] == 1
        assert cost.get_metrics()["estimated_cost_usd"] == 0.0
        adapter.cleanup()

        trace = assert_trace_exists(sid)
        assert trace is not None

    def test_adp_it004_task_callback(self, adapter_chain):
        adapter, _, loader, sid = adapter_chain
        collected = []
        registry = adapter._registry
        registry.register(EventType.TASK_COMPLETE, lambda ctx: collected.append(ctx))
        task_cb = adapter.make_task_callback()
        task_cb(MagicMock(raw="final output", description="do work"))
        assert len(collected) == 1

    def test_adp_it005_cleanup_triggers_audit(self, adapter_chain, tmp_path):
        adapter, _, loader, sid = adapter_chain
        adapter.on_turn_start(user_message="cleanup test", sender_id="u-test")
        audit = loader.strategies["audit_logger"]
        audit._audit_file = tmp_path / "audit.jsonl"
        adapter.cleanup()
        assert (tmp_path / "audit.jsonl").exists()

        trace = assert_trace_exists(sid, min_observations=1)
        assert_observation_has_io(trace, "session_end")

    def test_adp_it006_full_lifecycle(self, adapter_chain):
        adapter, _, loader, sid = adapter_chain
        adapter.on_turn_start(user_message="hi", sender_id="u-test")
        adapter.on_before_llm(agent_role="Main", messages=[{"content": "hi"}])
        adapter.on_before_llm(agent_role="Main", messages=[{"content": "think"}])
        adapter.on_before_tool_call(
            tool_name="search", tool_input={"q": "safe query"}
        )
        adapter.on_after_tool_call(
            tool_name="search", tool_input={"q": "safe query"}, tool_result="found"
        )
        step_cb = adapter.make_step_callback()
        step_cb(MagicMock(output="step result"))
        adapter.on_before_llm(agent_role="Main", messages=[{"content": "next"}])
        step_cb(MagicMock(output="final step"))
        task_cb = adapter.make_task_callback()
        task_cb(MagicMock(raw="done", description="task"))
        adapter.cleanup()
        assert adapter._turn_count == 2

        trace = assert_trace_exists(sid, min_observations=3)
        assert_trace_has_session(trace)
        assert_root_span_exists(trace)
        assert_tree_structure(trace)

        tool = assert_tool_observation(trace, "search")
        assert tool.get("output", {}).get("result") == "found"
        assert tool.get("endTime") is not None, "Tool span should be closed"

        gens = assert_generation_exists(trace, min_count=1)
        closed_gens = [g for g in gens if g.get("endTime")]
        assert len(closed_gens) >= 1, "At least one generation should be closed with endTime"

        assert_observation_has_io(trace, "session_end")
