"""IT-SEC-001 ~ IT-SEC-007: Security chain integration tests.

Each test uses a unique session_id for Langfuse trace isolation.
"""

import json
from pathlib import Path

import pytest

from xiaopaw.hook_framework.loader import HookLoader
from xiaopaw.hook_framework.registry import EventType, GuardrailDeny, HookContext, HookRegistry

from .conftest import (
    assert_deny_observation,
    assert_observation_has_io,
    assert_root_span_exists,
    assert_tool_observation,
    assert_trace_exists,
    assert_trace_has_session,
    assert_tree_structure,
    ensure_trace,
    finalize_trace,
)

SHARED_HOOKS_DIR = Path(__file__).parent.parent.parent / "shared_hooks"


@pytest.fixture
def security_chain(tmp_path, unique_session_id):
    audit_file = tmp_path / "audit.jsonl"
    registry = HookRegistry()
    loader = HookLoader(registry)
    loader.load_from_directory(SHARED_HOOKS_DIR, layer_name="global")
    audit = loader.strategies["audit_logger"]
    audit._audit_file = audit_file
    return registry, loader, audit_file, unique_session_id


def _tool_ctx(session_id, tool_name="knowledge_search", tool_input=None):
    return HookContext(
        event_type=EventType.BEFORE_TOOL_CALL,
        tool_name=tool_name,
        tool_input=tool_input or {"query": "normal"},
        session_id=session_id,
    )


def _dispatch_deny_close(registry, sid, tool_name, tool_input, deny_reason):
    """Dispatch AFTER_TOOL_CALL with deny metadata to close the tool span.

    Mirrors what CrewObservabilityAdapter does after catching GuardrailDeny.
    """
    registry.dispatch(
        EventType.AFTER_TOOL_CALL,
        HookContext(
            event_type=EventType.AFTER_TOOL_CALL,
            tool_name=tool_name,
            tool_input=tool_input,
            success=False,
            session_id=sid,
            metadata={
                "guardrail_deny": True,
                "deny_reason": deny_reason,
            },
        ),
    )


@pytest.mark.integration
class TestSecurityChain:
    def test_sec001_path_traversal_deny_and_audit(self, security_chain):
        registry, loader, audit_file, sid = security_chain
        ensure_trace(registry, sid)
        with pytest.raises(GuardrailDeny) as exc_info:
            registry.dispatch_gate(
                EventType.BEFORE_TOOL_CALL,
                _tool_ctx(sid, tool_input={"path": "../../etc/passwd"}),
            )
        sandbox = loader.strategies["sandbox_guard"]
        assert sandbox.get_metrics()["total_violations"] == 1
        audit = loader.strategies["audit_logger"]
        assert audit.get_metrics()["total_security_events"] >= 1
        _dispatch_deny_close(
            registry, sid, "knowledge_search",
            {"path": "../../etc/passwd"}, str(exc_info.value),
        )
        finalize_trace(registry, sid)

        trace = assert_trace_exists(sid, min_observations=1)
        assert_trace_has_session(trace)
        obs = assert_observation_has_io(trace, "knowledge_search")
        assert obs.get("input") == {"path": "../../etc/passwd"}
        assert_deny_observation(trace, "knowledge_search")

    def test_sec002_permission_deny_and_audit(self, security_chain, tmp_path):
        registry, loader, audit_file, sid = security_chain
        ensure_trace(registry, sid)
        perm = loader.strategies["permission_gate"]
        perm._tool_permissions["shell_executor"] = "deny"
        with pytest.raises(GuardrailDeny) as exc_info:
            registry.dispatch_gate(
                EventType.BEFORE_TOOL_CALL,
                _tool_ctx(sid, tool_name="shell_executor"),
            )
        assert perm.get_metrics()["deny_count"] == 1
        _dispatch_deny_close(
            registry, sid, "shell_executor",
            {"query": "normal"}, str(exc_info.value),
        )
        finalize_trace(registry, sid)

        trace = assert_trace_exists(sid, min_observations=1)
        obs = assert_observation_has_io(trace, "shell_executor")
        assert obs is not None

    def test_sec003_normal_tool_passes_all(self, security_chain):
        registry, _, _, sid = security_chain
        ensure_trace(registry, sid)
        registry.dispatch_gate(
            EventType.BEFORE_TOOL_CALL,
            _tool_ctx(sid, tool_name="knowledge_search", tool_input={"query": "天气"}),
        )
        finalize_trace(registry, sid)

        trace = assert_trace_exists(sid, min_observations=1)
        obs = assert_observation_has_io(trace, "knowledge_search")
        assert obs.get("input") == {"query": "天气"}

    def test_sec004_sandbox_blocks_before_permission(self, security_chain):
        registry, loader, _, sid = security_chain
        ensure_trace(registry, sid)
        perm = loader.strategies["permission_gate"]
        with pytest.raises(GuardrailDeny) as exc_info:
            registry.dispatch_gate(
                EventType.BEFORE_TOOL_CALL,
                _tool_ctx(sid, tool_input={"cmd": "rm -rf /"}),
            )
        assert len(perm.decisions) == 0
        _dispatch_deny_close(
            registry, sid, "knowledge_search",
            {"cmd": "rm -rf /"}, str(exc_info.value),
        )
        finalize_trace(registry, sid)

        trace = assert_trace_exists(sid, min_observations=1)
        obs = assert_observation_has_io(trace, "knowledge_search")
        assert obs.get("input") == {"cmd": "rm -rf /"}

    def test_sec005_shared_audit_instance(self, security_chain):
        _, loader, _, _ = security_chain
        sandbox = loader.strategies["sandbox_guard"]
        perm = loader.strategies["permission_gate"]
        assert sandbox._audit is loader.strategies["audit_logger"]
        assert perm._audit is loader.strategies["audit_logger"]

    def test_sec006_full_lifecycle(self, security_chain):
        registry, loader, audit_file, sid = security_chain
        ensure_trace(registry, sid)
        registry.dispatch_gate(
            EventType.BEFORE_TOOL_CALL,
            _tool_ctx(sid, tool_name="search", tool_input={"q": "safe query"}),
        )
        registry.dispatch_gate(
            EventType.AFTER_TURN,
            HookContext(
                event_type=EventType.AFTER_TURN,
                input_tokens=100, output_tokens=50,
                session_id=sid,
                metadata={"output": "result"},
            ),
        )
        registry.dispatch(
            EventType.SESSION_END,
            HookContext(event_type=EventType.SESSION_END, session_id=sid),
        )
        cost = loader.strategies["cost_guard"]
        assert cost.get_metrics()["total_input_tokens"] == 100
        lines = audit_file.read_text().strip().split("\n")
        last = json.loads(lines[-1])
        assert last["security_event"] == "session_summary"

        trace = assert_trace_exists(sid, min_observations=2)
        assert_trace_has_session(trace)
        assert_root_span_exists(trace)
        assert_tree_structure(trace)
        assert_tool_observation(trace, "search")
        assert_observation_has_io(trace, "session_end")

    def test_sec007_over_budget_blocks_tool(self, security_chain):
        registry, loader, _, sid = security_chain
        ensure_trace(registry, sid)
        cost = loader.strategies["cost_guard"]
        cost._budget = 0.0001
        with pytest.raises(GuardrailDeny):
            registry.dispatch_gate(
                EventType.AFTER_TURN,
                HookContext(
                    event_type=EventType.AFTER_TURN,
                    input_tokens=1_000_000, output_tokens=500_000,
                    session_id=sid,
                    metadata={"output": "big"},
                ),
            )
        with pytest.raises(GuardrailDeny, match="budget_exceeded"):
            registry.dispatch_gate(
                EventType.BEFORE_TOOL_CALL,
                _tool_ctx(sid),
            )
        finalize_trace(registry, sid)

        trace = assert_trace_exists(sid, min_observations=1)
        assert trace is not None
