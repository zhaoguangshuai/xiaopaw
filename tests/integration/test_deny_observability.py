"""IT-DNO-001 ~ IT-DNO-004: Deny path observability and propagation tests.

Covers:
  - Runner dispatches AFTER_TURN on GuardrailDeny (Langfuse trace closure)
  - main_crew step_callback re-raises pending_deny (CrewAI termination)

Each test uses a unique session_id for Langfuse trace isolation.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from xiaopaw.hook_framework.crew_adapter import (
    CrewObservabilityAdapter,
    get_current_adapter,
    set_current_adapter,
)
from xiaopaw.hook_framework.loader import HookLoader
from xiaopaw.hook_framework.registry import EventType, GuardrailDeny, HookContext, HookRegistry
from xiaopaw.models import InboundMessage
from xiaopaw.runner import Runner
from xiaopaw.session.models import SessionEntry

from .conftest import (
    assert_deny_observation,
    assert_observation_has_io,
    assert_trace_exists,
    assert_trace_has_session,
    assert_tree_structure,
    finalize_trace,
)

SHARED_HOOKS_DIR = Path(__file__).parent.parent.parent / "shared_hooks"


def _make_mock_sender():
    sender = AsyncMock()
    sender.send = AsyncMock(return_value="msg_id_1")
    sender.send_text = AsyncMock()
    sender.send_thinking = AsyncMock(return_value=None)
    return sender


def _make_mock_session_mgr(session_id):
    mgr = AsyncMock()
    mgr.get_or_create = AsyncMock(return_value=SessionEntry(id=session_id))
    mgr.load_history = AsyncMock(return_value=[])
    return mgr


@pytest.mark.integration
class TestDenyObservability:
    def test_dno001_runner_deny_dispatches_after_turn(self, unique_session_id):
        sid = unique_session_id
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(SHARED_HOOKS_DIR, layer_name="global")

        cost = loader.strategies["cost_guard"]
        cost._budget = 0.0

        after_turn_events = []
        registry.register(
            EventType.AFTER_TURN,
            lambda ctx: after_turn_events.append(ctx),
            name="test_capture",
        )

        sender = _make_mock_sender()
        session_mgr = _make_mock_session_mgr(sid)

        async def deny_agent_fn(*args, **kwargs):
            return "should not reach"

        runner = Runner(
            session_mgr=session_mgr,
            sender=sender,
            agent_fn=deny_agent_fn,
            hook_registry=registry,
        )

        inbound = InboundMessage(
            routing_key="p2p:test_user",
            content="hello",
            msg_id="m1",
            sender_id="u1",
            trace_id=sid,
        )

        asyncio.get_event_loop().run_until_complete(runner._handle(inbound))

        sender.send_text.assert_called_once()
        call_args = sender.send_text.call_args
        assert "安全策略拦截" in call_args[0][1]

        assert len(after_turn_events) == 1
        ctx = after_turn_events[0]
        assert ctx.metadata["guardrail_deny"] is True
        assert ctx.metadata["deny_reason"] == "budget_exceeded"
        assert "安全策略拦截" in ctx.metadata["reply"]
        assert ctx.session_id == sid
        assert ctx.duration_ms > 0

        trace = assert_trace_exists(sid, min_observations=1)
        assert_trace_has_session(trace)
        assert_tree_structure(trace)

    def test_dno002_runner_deny_does_not_call_agent(self, unique_session_id):
        sid = unique_session_id
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(SHARED_HOOKS_DIR, layer_name="global")
        cost = loader.strategies["cost_guard"]
        cost._budget = 0.0

        agent_called = False

        async def track_agent(*args, **kwargs):
            nonlocal agent_called
            agent_called = True
            return "nope"

        runner = Runner(
            session_mgr=_make_mock_session_mgr(sid),
            sender=_make_mock_sender(),
            agent_fn=track_agent,
            hook_registry=registry,
        )

        inbound = InboundMessage(
            routing_key="p2p:test_user",
            content="hello",
            msg_id="m2",
            sender_id="u2",
            trace_id=sid,
        )

        asyncio.get_event_loop().run_until_complete(runner._handle(inbound))
        assert not agent_called

        trace = assert_trace_exists(sid, min_observations=1)
        assert trace["sessionId"] == sid
        assert trace.get("input") == {"message": "hello"}

    def test_dno003_step_callback_reraises_pending_deny(self, unique_session_id):
        from xiaopaw.agents.main_crew import _make_step_callback

        sid = unique_session_id
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(SHARED_HOOKS_DIR, layer_name="global")

        adapter = CrewObservabilityAdapter(registry, session_id=sid)
        adapter.on_turn_start(user_message="deny reraise test", sender_id="u-test")
        adapter.on_before_tool_call(
            tool_name="reader", tool_input={"path": "../../etc/passwd"}
        )
        assert adapter._pending_deny is not None

        token = set_current_adapter(adapter)
        try:
            sender = _make_mock_sender()
            step_cb = _make_step_callback(sender, "p2p:test")

            from crewai.agents.parser import AgentAction

            action = AgentAction(
                thought="trying", tool="reader", tool_input="../../etc/passwd",
                text="Action: reader\nAction Input: ../../etc/passwd",
                result="some result",
            )

            with pytest.raises(GuardrailDeny) as exc_info:
                asyncio.get_event_loop().run_until_complete(step_cb(action))

            assert "sandbox_violation" in str(exc_info.value)
            assert adapter._pending_deny is None
        finally:
            set_current_adapter(None)
        adapter.cleanup()

        trace = assert_trace_exists(sid, min_observations=1)
        assert_deny_observation(trace, "reader")
        assert_tree_structure(trace)

    def test_dno004_step_callback_no_deny_passes_through(self, unique_session_id):
        from xiaopaw.agents.main_crew import _make_step_callback

        sid = unique_session_id
        registry = HookRegistry()
        loader = HookLoader(registry)
        loader.load_from_directory(SHARED_HOOKS_DIR, layer_name="global")

        adapter = CrewObservabilityAdapter(registry, session_id=sid)
        assert adapter._pending_deny is None

        token = set_current_adapter(adapter)
        try:
            sender = _make_mock_sender()
            step_cb = _make_step_callback(sender, "p2p:test")

            from crewai.agents.parser import AgentAction

            action = AgentAction(
                thought="thinking", tool="search", tool_input="safe query",
                text="Action: search\nAction Input: safe query",
                result="search results",
            )

            asyncio.get_event_loop().run_until_complete(step_cb(action))
        finally:
            set_current_adapter(None)
