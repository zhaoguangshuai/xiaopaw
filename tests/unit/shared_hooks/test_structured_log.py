"""Unit tests for structured_log handlers (FIX-1 + FIX-4)."""

import json
import sys
from io import StringIO
from unittest.mock import MagicMock

from xiaopaw.hook_framework.registry import EventType, HookContext

from shared_hooks.structured_log import (
    after_tool_handler,
    after_turn_handler,
    before_turn_handler,
    session_end_handler,
    task_complete_handler,
)


def _capture_emit(handler, ctx) -> dict:
    buf = StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf
    try:
        handler(ctx)
    finally:
        sys.stderr = old_stderr
    return json.loads(buf.getvalue().strip())


class TestExistingHandlers:
    def test_before_turn_emits_json(self):
        ctx = HookContext(
            event_type=EventType.BEFORE_TURN,
            session_id="s-abc",
            turn_number=1,
            agent_id="runner",
        )
        data = _capture_emit(before_turn_handler, ctx)
        assert data["event"] == "before_turn"
        assert data["session_id"] == "s-abc"
        assert data["turn"] == 1
        assert "timestamp" in data


class TestAfterToolVirtual:
    def test_real_tool_is_virtual_false(self):
        ctx = HookContext(
            event_type=EventType.AFTER_TOOL_CALL,
            session_id="s-1",
            turn_number=1,
            tool_name="web_search",
            success=True,
            duration_ms=42,
        )
        data = _capture_emit(after_tool_handler, ctx)
        assert data["is_virtual"] is False
        assert data["duration_ms"] == 42

    def test_final_answer_is_virtual_true(self):
        ctx = HookContext(
            event_type=EventType.AFTER_TOOL_CALL,
            session_id="s-1",
            turn_number=1,
            tool_name="final_answer",
            success=True,
        )
        data = _capture_emit(after_tool_handler, ctx)
        assert data["is_virtual"] is True


class TestNewHandlers:
    def test_after_turn_handler_emits_json(self):
        ctx = HookContext(
            event_type=EventType.AFTER_TURN,
            session_id="s-abc",
            turn_number=3,
            agent_id="orchestrator",
            duration_ms=1500,
            input_tokens=100,
            output_tokens=200,
        )
        data = _capture_emit(after_turn_handler, ctx)
        assert data["event"] == "after_turn"
        assert data["session_id"] == "s-abc"
        assert data["turn"] == 3
        assert data["agent_id"] == "orchestrator"
        assert data["duration_ms"] == 1500
        assert data["input_tokens"] == 100
        assert data["output_tokens"] == 200
        assert "timestamp" in data

    def test_task_complete_handler_emits_json(self):
        ctx = HookContext(
            event_type=EventType.TASK_COMPLETE,
            session_id="s-abc",
            task_name="main_task",
            agent_id="orchestrator",
        )
        data = _capture_emit(task_complete_handler, ctx)
        assert data["event"] == "task_complete"
        assert data["session_id"] == "s-abc"
        assert data["task_name"] == "main_task"
        assert data["agent_id"] == "orchestrator"

    def test_session_end_handler_emits_json(self):
        ctx = HookContext(
            event_type=EventType.SESSION_END,
            session_id="s-xyz",
        )
        data = _capture_emit(session_end_handler, ctx)
        assert data["event"] == "session_end"
        assert data["session_id"] == "s-xyz"
        assert "timestamp" in data
