"""IT-TQ-001 ~ IT-TQ-008: Langfuse trace quality tests (SDK v4).

Verifies trace hierarchy, observation types, parent-child relationships,
metadata completeness, and deny path tracing.
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
    assert_root_span_exists,
    assert_tool_observation,
    assert_trace_exists,
    assert_trace_has_session,
    assert_tree_structure,
    ensure_trace,
)

SHARED_HOOKS_DIR = Path(__file__).parent.parent.parent / "shared_hooks"


@pytest.fixture
def trace_chain(tmp_path, unique_session_id):
    registry = HookRegistry()
    loader = HookLoader(registry)
    loader.load_from_directory(SHARED_HOOKS_DIR, layer_name="global")
    adapter = CrewObservabilityAdapter(registry, session_id=unique_session_id)
    return adapter, registry, loader, unique_session_id


@pytest.mark.integration
class TestTraceQuality:

    def test_tq001_root_span_and_trace_metadata(self, trace_chain):
        """TQ001: BEFORE_TURN creates trace with root span containing source metadata."""
        adapter, _, _, sid = trace_chain
        adapter.on_turn_start(user_message="hello", sender_id="u-test")
        adapter.cleanup()

        trace = assert_trace_exists(sid, min_observations=1)
        assert_trace_has_session(trace)
        assert_root_span_exists(trace)

    def test_tq002_generation_has_model_and_parent(self, trace_chain):
        """TQ002: BEFORE_LLM creates GENERATION with model, parented to root or stack."""
        adapter, _, _, sid = trace_chain
        adapter.on_turn_start(user_message="llm test", sender_id="u-test")
        adapter.on_before_llm(
            agent_role="XiaoPaw",
            messages=[{"content": "test"}],
            model="qwen-plus",
        )
        step_cb = adapter.make_step_callback()
        step_cb(MagicMock(output="result"))
        adapter.cleanup()

        trace = assert_trace_exists(sid, min_observations=2)
        gens = assert_generation_exists(trace, min_count=1)
        assert gens[0].get("model") == "qwen-plus"
        assert gens[0].get("endTime") is not None

    def test_tq003_tool_observation_with_parent(self, trace_chain):
        """TQ003: tool call creates observation with input/output and valid parent."""
        adapter, _, _, sid = trace_chain
        adapter.on_turn_start(user_message="tool test", sender_id="u-test")
        adapter.on_before_llm(agent_role="Main", messages=[{"content": "think"}])
        adapter.on_before_tool_call(
            tool_name="search", tool_input={"q": "test query"}
        )
        adapter.on_after_tool_call(
            tool_name="search",
            tool_input={"q": "test query"},
            tool_result="found it",
        )
        step_cb = adapter.make_step_callback()
        step_cb(MagicMock(output="done"))
        adapter.cleanup()

        trace = assert_trace_exists(sid, min_observations=3)
        tool = assert_tool_observation(trace, "search")
        assert tool.get("input") == {"q": "test query"}
        out = tool.get("output", {})
        assert out.get("success") is True
        assert out.get("result") == "found it"
        assert tool.get("endTime") is not None

    def test_tq004_tree_structure_valid(self, trace_chain):
        """TQ004: all observations form a valid tree (parent IDs reference existing obs)."""
        adapter, _, _, sid = trace_chain
        adapter.on_turn_start(user_message="tree test", sender_id="u-test")
        adapter.on_before_llm(agent_role="Main", messages=[{"content": "hi"}])
        adapter.on_before_tool_call(
            tool_name="knowledge_search", tool_input={"q": "info"}
        )
        adapter.on_after_tool_call(
            tool_name="knowledge_search",
            tool_input={"q": "info"},
            tool_result="knowledge",
        )
        step_cb = adapter.make_step_callback()
        step_cb(MagicMock(output="answer"))
        adapter.cleanup()

        trace = assert_trace_exists(sid, min_observations=3)
        assert_tree_structure(trace)

    def test_tq005_deny_path_error_level(self, trace_chain):
        """TQ005: denied tool call creates ERROR-level observation with deny metadata."""
        adapter, _, _, sid = trace_chain
        adapter.on_turn_start(user_message="deny test", sender_id="u-test")
        adapter.on_before_tool_call(
            tool_name="reader", tool_input={"path": "../../etc/passwd"}
        )
        adapter._pending_deny = None
        adapter.cleanup()

        trace = assert_trace_exists(sid, min_observations=1)
        denied = assert_deny_observation(trace, "reader")
        assert denied.get("level") == "ERROR"

    def test_tq006_multiple_generations_closed(self, trace_chain):
        """TQ006: multiple LLM calls each create a closed GENERATION."""
        adapter, _, _, sid = trace_chain
        adapter.on_turn_start(user_message="multi-gen test", sender_id="u-test")
        adapter.on_before_llm(agent_role="Main", messages=[{"content": "first"}])
        adapter.on_before_llm(agent_role="Main", messages=[{"content": "second"}])
        adapter.on_before_llm(agent_role="Main", messages=[{"content": "third"}])
        step_cb = adapter.make_step_callback()
        step_cb(MagicMock(output="final"))
        adapter.cleanup()

        trace = assert_trace_exists(sid, min_observations=4)
        gens = assert_generation_exists(trace, min_count=3)
        closed = [g for g in gens if g.get("endTime")]
        assert len(closed) >= 3, f"Expected 3 closed gens, got {len(closed)}"

    def test_tq007_session_end_span(self, trace_chain):
        """TQ007: cleanup creates session_end span under root."""
        adapter, _, _, sid = trace_chain
        adapter.on_turn_start(user_message="end test", sender_id="u-test")
        adapter.cleanup()

        trace = assert_trace_exists(sid, min_observations=1)
        obs = trace.get("observations", [])
        end_spans = [o for o in obs if o.get("name") == "session_end"]
        assert end_spans, f"No session_end span. Available: {[o.get('name') for o in obs]}"
        assert end_spans[0].get("endTime") is not None

    def test_tq008_full_lifecycle_quality(self, trace_chain):
        """TQ008: full lifecycle produces complete, well-structured trace."""
        adapter, _, _, sid = trace_chain
        adapter.on_turn_start(user_message="full test", sender_id="u-test")
        adapter.on_before_llm(agent_role="Main", messages=[{"content": "plan"}])
        adapter.on_before_tool_call(
            tool_name="search", tool_input={"q": "safe query"}
        )
        adapter.on_after_tool_call(
            tool_name="search",
            tool_input={"q": "safe query"},
            tool_result="found",
        )
        step_cb = adapter.make_step_callback()
        step_cb(MagicMock(output="step result"))

        adapter.on_before_llm(agent_role="Main", messages=[{"content": "next"}])
        step_cb(MagicMock(output="final step"))

        task_cb = adapter.make_task_callback()
        task_cb(MagicMock(raw="done", description="complete task"))
        adapter.cleanup()

        trace = assert_trace_exists(sid, min_observations=5)
        assert_trace_has_session(trace)
        assert_root_span_exists(trace)
        assert_tree_structure(trace)

        gens = assert_generation_exists(trace, min_count=1)
        assert any(g.get("endTime") for g in gens)

        tool = assert_tool_observation(trace, "search")
        assert tool.get("output", {}).get("result") == "found"

        obs = trace.get("observations", [])
        end_spans = [o for o in obs if o.get("name") == "session_end"]
        assert end_spans
        task_spans = [o for o in obs if o.get("name") == "task-complete"]
        assert task_spans
